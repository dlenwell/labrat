# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Infer physical links using targeted UniFi client observations.

Only MAC addresses belonging to already discovered Proxmox physical ports are
queried. The classic API is optional and its client records are never stored.

Used by: collector.clients, collector.nut_wired, collector.runner.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any, TYPE_CHECKING
from datetime import datetime, timezone
from urllib.parse import quote

import re

import requests
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from labrat.audit import record_audit_event
from labrat.coerce import json_object, mac_address
from labrat.collector.http import required_string, tls_verification, unifi_network_root
from labrat.collector.links import port_name
from labrat.database import as_utc
from labrat.discovery.models import (
    DiscoveredDevice, DiscoveredLink, DiscoveredPort, DiscoverySource,
)

if TYPE_CHECKING:
    from labrat.collector.adapters.unifi import UniFiAdapter

EVIDENCE = "unifi-client-mac"
MAX_AGE_SECONDS = 300


def client_attachment(record: Mapping[str, Any]) -> tuple[str, int] | None:
    """Read consistent current switch identifiers across classic API formats.

    Args:
        record: One fresh wired client record, already matched to a known MAC.

    Returns:
        tuple[str, int] | None: Normalized switch MAC and positive port index.
        Conflicting aliases and historical last-uplink records are not used.
    """
    records = [record]
    if isinstance(record.get("uplink"), Mapping):
        records.append(record["uplink"])
    macs = set()
    ports = set()
    for item in records:
        for key in ("sw_mac", "uplink_mac", "uplink_device_mac"):
            if isinstance(item.get(key), str) and not item[key].strip():
                continue
            if item.get(key) is not None:
                mac = mac_address(item[key])
                if mac is None:
                    return None
                macs.add(mac)
        for key in ("sw_port", "uplink_remote_port", "port_idx"):
            if isinstance(item.get(key), str) and not item[key].strip():
                continue
            if item.get(key) is not None:
                value = item[key]
                if type(value) is int and value > 0:
                    ports.add(value)
                elif isinstance(value, str) and (match := re.fullmatch(r"(?:Port\s+)?([1-9]\d*)", value.strip(), re.IGNORECASE)):
                    ports.add(int(match.group(1)))
                else:
                    return None
    if len(macs) != 1 or len(ports) != 1:
        return None
    return next(iter(macs)), next(iter(ports))


#: One switch port hearing a chassis: the switch and the remote port IDs it reports.
Sighting = tuple[DiscoveredDevice, set[str]]


def _chassis_ports(
    switches: Iterable[DiscoveredDevice], snapshot_at: datetime | None,
) -> dict[tuple[str, str], dict[tuple[int, str], Sighting]]:
    """Index the current switches' LLDP tables by the chassis each port hears.

    Only switches in the source's latest successful snapshot count, and not
    ones the controller reports offline: a stored row keeps the LLDP table
    it last had. Repeated rows for one port are one sighting.

    Args:
        switches: The UniFi source's switches, routers, and access points.
        snapshot_at: When the source last collected successfully; None
            means there is no current snapshot, so nothing is indexed.

    Returns:
        dict[tuple[str, str], dict[tuple[int, str], Sighting]]: For each site
        ID and chassis MAC, the switch ports hearing it, keyed by switch ID
        and local port index.
    """
    heard: dict[tuple[str, str], dict[tuple[int, str], Sighting]] = defaultdict(dict)
    snapshot_at = as_utc(snapshot_at)
    if snapshot_at is None:
        return heard
    for switch in switches:
        details = json_object(switch.details_json)
        site_id, neighbors = details.get("site_id"), details.get("lldp_neighbors")
        seen_at = as_utc(switch.last_seen_at)
        if (seen_at is None or seen_at < snapshot_at
                or details.get("state") == "OFFLINE"
                or not isinstance(site_id, str) or not isinstance(neighbors, list)):
            continue
        for neighbor in neighbors:
            if not isinstance(neighbor, Mapping) or neighbor.get("local_port") is None:
                continue
            chassis = mac_address(neighbor.get("chassis_id"))
            if not chassis:
                continue
            key = (switch.id, str(neighbor["local_port"]))
            _, port_ids = heard[(site_id, chassis)].setdefault(key, (switch, set()))
            if isinstance(neighbor.get("port_id"), str) and neighbor["port_id"].strip():
                port_ids.add(neighbor["port_id"])
    return heard


def _names_other_nic(port_ids: Iterable[str], server_port: DiscoveredPort,
                     server_ports: Iterable[DiscoveredPort]) -> bool:
    """Tell whether an LLDP remote port ID identifies another NIC of the server.

    Uses the LLDP path's matching (``links.port_name``, or the ID as a MAC).
    An ID naming no known NIC, or the looked-up NIC itself, does not count.

    Args:
        port_ids: Remote port IDs the switch reports for the chassis.
        server_port: The NIC that was looked up.
        server_ports: Every discovered port of that NIC's server.

    Returns:
        bool: True when some ID positively names a different NIC.
    """
    ports = list(server_ports)
    for port_id in port_ids:
        name, mac = port_name(port_id), mac_address(port_id)
        named = {port.id for port in ports
                 if (name is not None and name in {port.remote_id.casefold(), port.name.casefold()})
                 or (mac is not None and mac_address(port.mac) == mac)}
        if named - {server_port.id}:
            return True
    return False


def _own_chassis_switch(
    site_id: str, chassis: str, port_idx: int, server_port: DiscoveredPort,
    server_ports: Mapping[int, list[DiscoveredPort]],
    heard: Mapping[tuple[str, str], Mapping[tuple[int, str], Sighting]],
) -> DiscoveredDevice | None:
    """Find the switch a NIC hangs off when UniFi files it behind its own host.

    A server running lldpd announces one of its NIC MACs as its chassis ID,
    and a controller may then list the server's other NICs as clients of that
    "device" instead of the switch. The switch's own LLDP table still says
    which port hears the chassis. That port is the NIC's only when the chassis
    MAC belongs to the NIC's server alone, exactly one current switch port in
    the site hears it, that port is the one the client record names, and its
    LLDP remote port ID does not name a different NIC of the server.

    Args:
        site_id: The site the client record came from.
        chassis: The normalized MAC the record names as the NIC's switch.
        port_idx: The port index the record names.
        server_port: The NIC that was looked up.
        server_ports: Every discovered server port, by server ID.
        heard: Current switch ports hearing each chassis (``_chassis_ports``).

    Returns:
        DiscoveredDevice | None: The switch, or None when the record is not
        this case or the evidence does not settle it.
    """
    owners = {port.discovered_device_id for ports in server_ports.values() for port in ports
              if mac_address(port.mac) == chassis}
    if owners != {server_port.discovered_device_id}:
        return None
    sightings = heard.get((site_id, chassis), {})
    if len(sightings) != 1:
        return None
    (_, local_port), (switch, port_ids) = next(iter(sightings.items()))
    if local_port != str(port_idx):
        return None
    if _names_other_nic(port_ids, server_port, server_ports.get(server_port.discovered_device_id, [])):
        return None
    return switch

def _unknown_station(error: requests.HTTPError) -> bool:
    """Tell whether a client lookup failed only because the MAC is unknown.

    The classic endpoint answers HTTP 400 with ``api.err.UnknownStation`` for
    a MAC it has never seen, such as a down NIC or a second bond member that
    never sources traffic. That is an empty answer, not a controller failure.

    Args:
        error: Error raised for one ``stat/sta/{mac}`` request.

    Returns:
        bool: True for the unknown-station answer only.
    """
    response = error.response
    if response is None or response.status_code != 400:
        return False
    try:
        meta = response.json().get("meta")
    except (ValueError, AttributeError):
        return False
    return isinstance(meta, dict) and meta.get("msg") == "api.err.UnknownStation"


def collect_client_links(
    db: Session, source: DiscoverySource,
    proxmox_sources: Iterable[DiscoverySource], adapter: "UniFiAdapter",
    settings: Mapping[str, Any],
) -> set[tuple[int, int]] | None:
    """Return inferred port pairs, or None when the optional API fails.

    A MAC must identify exactly one discovered physical port across all online
    Proxmox servers. Each controller query targets that MAC alone. A fresh wired
    client must identify a known switch and port, or name its own server's LLDP
    chassis on a port whose switch hears that chassis (``_own_chassis_switch``).
    Conflicting results are omitted.
    No client details are persisted or logged.

    Args:
        db: Read session containing successfully collected observations.
        source: UniFi source that owns the inferred link evidence.
        proxmox_sources: Proxmox sources successfully collected this cycle.
        adapter: UniFi adapter whose HTTP client performs bounded GET requests.
        settings: Source settings read from private local configuration.

    Returns:
        set[tuple[int, int]] | None: Ordered physical-port ID pairs, an empty
        set for a successful snapshot without matches, or None on API failure.
    """
    source_ids = [item.id for item in proxmox_sources]
    servers = list(db.scalars(select(DiscoveredDevice).where(
        DiscoveredDevice.source_id.in_(source_ids), DiscoveredDevice.category == "server")))
    online = {item.id for item in servers if json_object(item.details_json).get("status") != "offline"}
    all_ports = list(db.scalars(select(DiscoveredPort).where(
        DiscoveredPort.discovered_device_id.in_([item.id for item in servers] or [-1]))))
    by_mac = defaultdict(list)
    for port in all_ports:
        mac = mac_address(port.mac)
        if mac:
            by_mac[mac].append(port)
    candidates = {mac: ports[0] for mac, ports in by_mac.items()
                  if len(ports) == 1 and ports[0].discovered_device_id in online
                  and ports[0].link_up is not False}
    switches = list(db.scalars(select(DiscoveredDevice).where(
        DiscoveredDevice.source_id == source.id,
        DiscoveredDevice.category.in_(("switch", "router", "firewall", "access_point")))))
    server_ports: dict[int, list[DiscoveredPort]] = defaultdict(list)
    for port in all_ports:
        server_ports[port.discovered_device_id].append(port)
    current = db.get(DiscoverySource, source.id)
    heard = _chassis_ports(switches, current.last_success_at if current else None)
    switch_by_key = defaultdict(list)
    for switch in switches:
        mac = mac_address(json_object(switch.identifiers_json).get("mac"))
        site_id = json_object(switch.details_json).get("site_id")
        if mac and isinstance(site_id, str):
            switch_by_key[(site_id, mac)].append(switch)
    ports_by_key = {(p.discovered_device_id, p.remote_id): p for p in db.scalars(
        select(DiscoveredPort).where(DiscoveredPort.discovered_device_id.in_([s.id for s in switches] or [-1])))}
    try:
        base = unifi_network_root(settings)
        headers = {"Accept": "application/json", "X-API-Key": required_string(settings, "api_key")}
        verify = tls_verification(settings)
        sites = adapter._page(base + "/integration/v1", "sites", headers, verify)
        if not sites or any(not isinstance(site.get("id"), str) or
                            not isinstance(site.get("internalReference"), str) or
                            not site.get("internalReference") for site in sites):
            return None
        pairs = []
        for site in sites:
            site_id, reference = site.get("id"), site.get("internalReference")
            if not isinstance(site_id, str) or not isinstance(reference, str) or not reference:
                continue
            root = base + "/api/s/" + quote(reference, safe="") + "/stat/sta/"
            for mac, server_port in candidates.items():
                address = ":".join(mac[i:i + 2] for i in range(0, 12, 2))
                try:
                    payload = adapter.client.get_json(root + quote(address, safe=""), headers, verify)
                except requests.HTTPError as exc:
                    if _unknown_station(exc):
                        continue  # The controller has never seen this NIC.
                    raise
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    return None
                if isinstance(payload.get("meta"), dict) and payload["meta"].get("rc", "ok") != "ok":
                    return None
                records = [r for r in payload["data"] if isinstance(r, dict) and mac_address(r.get("mac")) == mac]
                if len(records) != 1:
                    continue
                record = records[0]
                now = datetime.now(timezone.utc).timestamp()
                last_seen = record.get("last_seen")
                attachment = client_attachment(record)
                if (record.get("is_wired") is not True or type(last_seen) not in (int, float)
                        or not 0 <= now - last_seen <= MAX_AGE_SECONDS
                        or attachment is None):
                    continue
                switch_mac, port_idx = attachment
                matches = switch_by_key.get((site_id, switch_mac), [])
                if not matches:
                    own = _own_chassis_switch(site_id, switch_mac, port_idx, server_port, server_ports, heard)
                    matches = [own] if own is not None else []
                if len(matches) != 1:
                    continue
                switch_port = ports_by_key.get((matches[0].id, str(port_idx)))
                if switch_port is None or switch_port.link_up is False:
                    continue
                pairs.append(tuple(sorted((server_port.id, switch_port.id))))
    except (requests.RequestException, ValueError, TypeError, KeyError):
        return None
    counts = Counter(port_id for pair in pairs for port_id in pair)
    return {pair for pair in pairs if all(counts[port_id] == 1 for port_id in pair)}


def reconcile_client_links(
    db: Session, source: DiscoverySource, pairs: set[tuple[int, int]] | None,
    *, seen_at: datetime | None = None,
) -> int:
    """Replace client-derived inferences while respecting stronger link evidence.

    A failed lookup is represented by None and leaves earlier observations in
    place. Existing links touching either port take precedence over inference.

    Args:
        db: Transaction session; the caller owns the commit.
        source: UniFi source that owns these inferred observations.
        pairs: Successful lookup result, or None when lookup failed.
        seen_at: Observation time, defaulting to current UTC time.

    Returns:
        int: Number of inferred links written after conflict checks.
    """
    if pairs is None:
        return 0
    db.execute(delete(DiscoveredLink).where(DiscoveredLink.source_id == source.id,
                                             DiscoveredLink.evidence == EVIDENCE))
    db.flush()
    occupied = set()
    for link in db.scalars(select(DiscoveredLink)):
        occupied.update((link.a_port_id, link.b_port_id))
    timestamp = seen_at or datetime.now(timezone.utc)
    count = 0
    for left, right in sorted(pairs):
        if left in occupied or right in occupied:
            continue
        link = DiscoveredLink(source_id=source.id, a_port_id=left, b_port_id=right,
                              evidence=EVIDENCE, confidence="inferred", last_seen_at=timestamp)
        db.add(link)
        db.flush()
        record_audit_event(
            db, actor_kind="collector", actor_id=source.id, actor_name=source.name,
            action="discovery.link.created", subject_kind="discovered_link",
            subject_id=link.id, details={"evidence": EVIDENCE, "confidence": "inferred"},
        )
        occupied.update((left, right))
        count += 1
    return count
