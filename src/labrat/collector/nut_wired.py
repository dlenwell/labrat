# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Infer a NUT server's wired switch port from UniFi client records.

NUT reports its server address but not its host NICs. When UniFi's
connected-client list shows that exact address as a *wired* client, the
classic per-MAC client record names the switch and port it is plugged into
(the same fields ``client_links`` uses for Proxmox NICs). That is good
evidence of one physical cable, so this module gives the NUT server one
inferred port ("Wired NIC") and an inferred link to that switch port.

The UniFi source owns the link (evidence ``unifi-client-ip``). The port
belongs to the NUT server observation; the NUT adapter never marks its port
list complete, so NUT scans keep it. When the client is no longer seen wired,
both are removed; when the lookup fails, both are kept. Stronger link
evidence on the switch port always wins. No client record is stored beyond
the NIC's MAC on the inferred port.
"""

from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from labrat.audit import record_audit_event
from labrat.coerce import json_object, mac_address
from labrat.collector.adapters.unifi import UniFiAdapter
from labrat.collector.client_links import client_attachment, MAX_AGE_SECONDS, _unknown_station
from labrat.collector.http import required_string, tls_verification, unifi_network_root
from labrat.collector.wireless_clients import nut_client_matches, nut_servers_by_address
from labrat.discovery.models import (
    DiscoveredDevice, DiscoveredLink, DiscoveredPort, DiscoverySource,
)

#: Link evidence for a NUT server matched to a wired UniFi client by its IP.
EVIDENCE = "unifi-client-ip"
#: Remote ID and display name of the inferred NIC on the NUT server.
PORT_REMOTE_ID = "unifi-wired-nic"
PORT_NAME = "Wired NIC"


def collect_nut_wired(
    db: Session, source: DiscoverySource, adapter: UniFiAdapter,
    settings: Mapping[str, Any], *, now: float | None = None,
) -> dict[int, list[dict[str, Any]]] | None:
    """Find the switch port behind each NUT server seen as a wired client.

    A client must be listed as ``WIRED`` with exactly one NUT server's IP;
    its classic record must be fresh, wired, and name one switch and port
    this UniFi source has observed, and that port must not be reported down.

    Args:
        db: Read session with NUT servers and this source's switches.
        source: Successfully polled UniFi source.
        adapter: Its read-only HTTP client.
        settings: Private local UniFi configuration.
        now: Current UNIX time, injectable for tests.

    Returns:
        dict[int, list[dict[str, Any]]] | None: NUT server observation ID to
            candidate attachments (``switch_port_id``, ``mac``,
            ``source_id``), or None when a lookup failed.
    """
    servers = nut_servers_by_address(db)
    if not servers:
        return {}
    switches = list(db.scalars(select(DiscoveredDevice).where(
        DiscoveredDevice.source_id == source.id,
        DiscoveredDevice.category.in_(("switch", "router", "firewall", "access_point")),
    )))
    switch_by_key: dict[tuple[str, str], list[DiscoveredDevice]] = defaultdict(list)
    for switch in switches:
        mac = mac_address(json_object(switch.identifiers_json).get("mac"))
        site_id = json_object(switch.details_json).get("site_id")
        if mac and isinstance(site_id, str):
            switch_by_key[(site_id, mac)].append(switch)
    ports_by_key = {(port.discovered_device_id, port.remote_id): port for port in db.scalars(
        select(DiscoveredPort).where(DiscoveredPort.discovered_device_id.in_([s.id for s in switches] or [-1])))}
    timestamp = now if now is not None else datetime.now(timezone.utc).timestamp()
    matches: dict[int, list[dict[str, Any]]] = defaultdict(list)
    try:
        base = unifi_network_root(settings)
        headers = {"Accept": "application/json", "X-API-Key": required_string(settings, "api_key")}
        verify = tls_verification(settings)
        for site, client, _, server in nut_client_matches(adapter, settings, servers):
            if str(client.get("type", "")).upper() != "WIRED":
                continue
            mac = mac_address(client.get("macAddress"))
            reference = site.get("internalReference")
            if mac is None or not isinstance(reference, str) or not reference:
                continue
            address = ":".join(mac[i:i + 2] for i in range(0, 12, 2))
            root = base + "/api/s/" + quote(reference, safe="") + "/stat/sta/"
            try:
                payload = adapter.client.get_json(root + quote(address, safe=""), headers, verify)
            except requests.HTTPError as exc:
                if _unknown_station(exc):
                    continue
                raise
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                return None
            if isinstance(payload.get("meta"), dict) and payload["meta"].get("rc", "ok") != "ok":
                return None
            records = [r for r in payload["data"] if isinstance(r, dict) and mac_address(r.get("mac")) == mac]
            if len(records) != 1:
                continue
            record = records[0]
            last_seen = record.get("last_seen")
            attachment = client_attachment(record)
            if (record.get("is_wired") is not True or type(last_seen) not in (int, float)
                    or not 0 <= timestamp - last_seen <= MAX_AGE_SECONDS or attachment is None):
                continue
            switch_mac, port_index = attachment
            found = switch_by_key.get((site["id"], switch_mac), [])
            if len(found) != 1:
                continue
            switch_port = ports_by_key.get((found[0].id, str(port_index)))
            if switch_port is None or switch_port.link_up is False:
                continue
            matches[server.id].append({
                "switch_port_id": switch_port.id, "mac": address, "source_id": source.id,
            })
    except (requests.RequestException, ValueError, TypeError, KeyError):
        return None
    return dict(matches)


def _audit(db: Session, source: DiscoverySource, action: str, kind: str, subject_id: int,
           details: dict[str, Any]) -> None:
    """Record one collector change with the owning UniFi source as actor.

    Args:
        db: The open write session.
        source: The UniFi source the change came from.
        action: The audit action, such as ``discovery.link.removed``.
        kind: The subject kind: ``discovered_port`` or ``discovered_link``.
        subject_id: The changed row's ID.
        details: Curated details for the audit trail.
    """
    record_audit_event(
        db, actor_kind="collector", actor_id=source.id, actor_name=source.name,
        action=action, subject_kind=kind, subject_id=subject_id, details=details,
    )


def _remove(db: Session, port: DiscoveredPort | None, link: DiscoveredLink | None,
            source: DiscoverySource | None) -> None:
    """Remove an inferred cable and the inferred NIC it hung from.

    Args:
        db: The open write session.
        port: The inferred Wired NIC port, if one exists.
        link: The inferred cable, if one exists.
        source: The UniFi source to audit as, or None to skip the audit.
    """
    if link is not None:
        if source is not None:
            _audit(db, source, "discovery.link.removed", "discovered_link", link.id,
                   {"evidence": EVIDENCE})
        db.delete(link)
    if port is not None:
        if source is not None:
            _audit(db, source, "discovery.port.removed", "discovered_port", port.id,
                   {"name": PORT_NAME, "inferred_by": EVIDENCE})
        db.delete(port)
    db.flush()


def _same_nic_as_client(db: Session, link: DiscoveredLink, switch_port: DiscoveredPort, mac: str) -> bool:
    """Tell whether a switch port's link is the clients feed's view of this NIC.

    Args:
        db: The open session.
        link: Another link on the switch port.
        switch_port: The switch port the NUT server is plugged into.
        mac: The NUT server NIC's MAC address.

    Returns:
        bool: True when the link's far end is a UniFi client's port with
            the same MAC.
    """
    far = db.get(DiscoveredPort, link.b_port_id if link.a_port_id == switch_port.id else link.a_port_id)
    if far is None or mac_address(far.mac) != mac_address(mac):
        return False
    device = db.get(DiscoveredDevice, far.discovered_device_id)
    source = db.get(DiscoverySource, device.source_id) if device is not None else None
    return source is not None and source.kind == "unifi_clients"


def reconcile_nut_wired(
    db: Session, candidates: Mapping[int, list[dict[str, Any]]],
    *, seen_at: datetime | None = None,
) -> int:
    """Keep exactly one inferred wired cable per NUT server, or none.

    Call only after every UniFi source's lookup succeeded: absence here
    removes evidence. Changes are audited; an unchanged cable only has its
    last-seen time refreshed.

    Args:
        db: Write transaction; the caller owns the commit.
        candidates: Matches from ``collect_nut_wired`` across all UniFi
            sources, keyed by NUT server observation ID.
        seen_at: Observation time, defaulting to current UTC time.

    Returns:
        int: Number of NUT servers that now have an inferred wired cable.
    """
    now = seen_at or datetime.now(timezone.utc)
    servers = {device.id: device for found in nut_servers_by_address(db).values() for device in found}
    count = 0
    for server in servers.values():
        port = db.scalar(select(DiscoveredPort).where(
            DiscoveredPort.discovered_device_id == server.id,
            DiscoveredPort.remote_id == PORT_REMOTE_ID,
        ))
        link = db.scalar(select(DiscoveredLink).where(
            DiscoveredLink.evidence == EVIDENCE,
            or_(DiscoveredLink.a_port_id == port.id, DiscoveredLink.b_port_id == port.id),
        )) if port is not None else None
        owner = db.get(DiscoverySource, link.source_id) if link is not None else None
        records = candidates.get(server.id, [])
        switch_port = db.get(DiscoveredPort, records[0]["switch_port_id"]) if len(records) == 1 else None
        source = db.get(DiscoverySource, records[0]["source_id"]) if len(records) == 1 else None
        if switch_port is None or source is None:
            _remove(db, port, link, owner)
            continue
        # Stronger evidence (or another inference) on the switch port wins,
        # except the clients feed's own link to this same NIC: that is one
        # cable seen twice, and the client defers to the server once the
        # server's port carries the MAC.
        others = list(db.scalars(select(DiscoveredLink).where(
            or_(DiscoveredLink.a_port_id == switch_port.id, DiscoveredLink.b_port_id == switch_port.id),
            DiscoveredLink.id != (link.id if link is not None else -1),
        )))
        duplicates = [item for item in others if _same_nic_as_client(db, item, switch_port, records[0]["mac"])]
        if len(duplicates) != len(others):
            _remove(db, port, link, owner)
            continue
        for duplicate in duplicates:
            _audit(db, db.get(DiscoverySource, duplicate.source_id) or source, "discovery.link.removed",
                   "discovered_link", duplicate.id, {"evidence": duplicate.evidence, "replaced_by": EVIDENCE})
            db.delete(duplicate)
        db.flush()
        fields = {
            "name": PORT_NAME, "mac": records[0]["mac"], "link_up": True,
            "speed_mbps": switch_port.speed_mbps, "max_speed_mbps": None,
            "connector_type": switch_port.connector_type, "medium": switch_port.medium,
        }
        if port is None:
            port = DiscoveredPort(discovered_device_id=server.id, remote_id=PORT_REMOTE_ID,
                                  last_seen_at=now, **fields)
            db.add(port)
            db.flush()
            _audit(db, source, "discovery.port.created", "discovered_port", port.id,
                   {"name": PORT_NAME, "inferred_by": EVIDENCE})
        else:
            changed = sorted(key for key, value in fields.items() if getattr(port, key) != value)
            for key in changed:
                setattr(port, key, fields[key])
            port.last_seen_at = now
            if changed:
                _audit(db, source, "discovery.port.changed", "discovered_port", port.id,
                       {"changed_fields": changed, "inferred_by": EVIDENCE})
        pair = tuple(sorted((port.id, switch_port.id)))
        if link is not None and ((link.a_port_id, link.b_port_id) != pair or link.source_id != source.id):
            _audit(db, owner or source, "discovery.link.removed", "discovered_link", link.id,
                   {"evidence": EVIDENCE})
            db.delete(link)
            db.flush()
            link = None
        if link is None:
            link = DiscoveredLink(source_id=source.id, a_port_id=pair[0], b_port_id=pair[1],
                                  evidence=EVIDENCE, confidence="inferred", last_seen_at=now)
            db.add(link)
            db.flush()
            _audit(db, source, "discovery.link.created", "discovered_link", link.id,
                   {"evidence": EVIDENCE, "confidence": "inferred"})
        else:
            link.last_seen_at = now
        count += 1
    db.flush()
    return count
