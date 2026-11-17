# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Infer a NUT server's Wi-Fi attachment from current UniFi client records.

NUT reports its server address, but not its host NICs. UniFi's connected-client
API can identify the same address as a wireless client and name the adopted AP
it is using. The match is useful evidence, not a cable or an agent-reported
interface. Only a small whitelist is persisted on the NUT server observation.

Used by: collector.nut_wired, collector.runner.
"""

from collections import defaultdict
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
import ipaddress
from typing import Any
from urllib.parse import quote

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from labrat.coerce import json_object, mac_address
from labrat.collector.adapters.unifi import UniFiAdapter
from labrat.collector.http import required_string, tls_verification, unifi_network_root
from labrat.collector.links import _addresses
from labrat.collector.store import record_telemetry
from labrat.discovery.models import DiscoveredDevice, DiscoverySource


def nut_servers_by_address(db: Session) -> dict[str, list[DiscoveredDevice]]:
    """Index recognized NUT server computers by exact management address.

    An address used by more than one NUT server remains ambiguous and cannot
    justify attributing a UniFi client record to either observation.

    Args:
        db: Read session containing the latest source observations.

    Returns:
        dict[str, list[DiscoveredDevice]]: Canonical IP -> possible servers.
    """
    found: dict[str, list[DiscoveredDevice]] = defaultdict(list)
    for device in db.scalars(select(DiscoveredDevice).join(
        DiscoverySource, DiscoveredDevice.source_id == DiscoverySource.id,
    ).where(
        DiscoverySource.kind == "nut",
        DiscoverySource.enabled.is_(True),
        DiscoveredDevice.category == "server",
        DiscoveredDevice.visibility == "recognized",
    )):
        if json_object(device.details_json).get("role") != "nut_server":
            continue
        for address in _addresses(json_object(device.identifiers_json).get("management_ips")):
            found[address].append(device)
    return dict(found)


def nut_client_matches(
    adapter: UniFiAdapter, settings: Mapping[str, Any],
    servers: Mapping[str, list[DiscoveredDevice]],
) -> Iterator[tuple[Mapping[str, Any], Mapping[str, Any], str, DiscoveredDevice]]:
    """Walk current UniFi clients whose exact IP names exactly one NUT server.

    Shared by the Wi-Fi (this module) and wired (``nut_wired``) inference so
    both use the same address rules. Reads only the documented local
    Integration API's sites and connected clients; API and payload errors
    propagate so each caller can keep its previous evidence.

    Args:
        adapter: The UniFi source's read-only HTTP client.
        settings: Private local UniFi configuration.
        servers: NUT servers by canonical management address
            (``nut_servers_by_address``).

    Yields:
        tuple: The site record, the client record, the canonical address,
            and the one NUT server observation with that address.

    Raises:
        requests.RequestException, ValueError, TypeError, KeyError: The
            controller or its response could not be read.
    """
    base = unifi_network_root(settings) + "/integration/v1"
    headers = {"Accept": "application/json", "X-API-Key": required_string(settings, "api_key")}
    verify = tls_verification(settings)
    for site in adapter._page(base, "sites", headers, verify):
        site_id = site.get("id")
        if not isinstance(site_id, str) or not site_id:
            raise ValueError("UniFi site has no ID")
        for client in adapter._page(base, f"sites/{quote(site_id, safe='')}/clients", headers, verify):
            raw_ip = client.get("ipAddress")
            if not isinstance(raw_ip, str):
                continue
            try:
                address = str(ipaddress.ip_address(raw_ip))
            except ValueError:
                continue
            possible = servers.get(address, [])
            if len(possible) == 1:
                yield site, client, address, possible[0]


def _client_record(
    client: Mapping[str, Any], source: DiscoverySource, site_id: str,
    observed_aps: set[str], sampled_at: datetime,
) -> dict[str, Any] | None:
    """Keep only a current wireless client's identity and adopted AP reference.

    Args:
        client: One Integration API connected-client overview record.
        source: UniFi source that supplied it.
        site_id: Site UUID from the same API response.
        observed_aps: AP remote IDs from this source's saved device snapshot.
        sampled_at: Time of this read.

    Returns:
        dict[str, Any] | None: Bounded whitelisted evidence, or None when the
            client cannot establish a wireless interface.
    """
    if str(client.get("type", "")).upper() != "WIRELESS":
        return None
    mac = mac_address(client.get("macAddress"))
    if mac is None:
        return None
    uplink = client.get("uplinkDeviceId")
    ap_remote_id = f"{site_id}:{uplink}" if isinstance(uplink, str) and uplink else None
    if ap_remote_id not in observed_aps:
        ap_remote_id = None
    connected_at = client.get("connectedAt")
    if not isinstance(connected_at, str) or len(connected_at) > 40:
        connected_at = None
    return {
        "state": "connected", "evidence": "unifi-connected-client-ip",
        "source_id": source.id, "source": source.name,
        "mac_address": ":".join(mac[i:i + 2] for i in range(0, 12, 2)),
        "ap_remote_id": ap_remote_id,
        "connected_at": connected_at,
        "sampled_at": sampled_at.isoformat(),
    }


def collect_nut_wireless(
    db: Session, source: DiscoverySource, adapter: UniFiAdapter,
    settings: Mapping[str, Any], *, sampled_at: datetime | None = None,
) -> dict[int, list[dict[str, Any]]] | None:
    """Match connected UniFi wireless clients to NUT servers by exact IP.

    This reads the documented local Integration API's sites and connected
    clients. It never imports ordinary clients into inventory. A failed API
    response returns None so previous evidence can age instead of being
    replaced by a false disconnection.

    Args:
        db: Read session with NUT servers and adopted UniFi APs.
        source: Successfully polled UniFi source.
        adapter: Its read-only HTTP client.
        settings: Private local UniFi configuration.
        sampled_at: Current UTC time, injectable for tests.

    Returns:
        dict[int, list[dict[str, Any]]] | None: Server observation ID to
            wireless candidates, or None when the optional lookup failed.
    """
    servers = nut_servers_by_address(db)
    if not servers:
        return {}
    now = sampled_at or datetime.now(timezone.utc)
    observed_aps = set(db.scalars(select(DiscoveredDevice.remote_id).where(
        DiscoveredDevice.source_id == source.id,
        DiscoveredDevice.category == "access_point",
        DiscoveredDevice.visibility == "recognized",
    )))
    matches: dict[int, list[dict[str, Any]]] = defaultdict(list)
    try:
        for site, client, address, server in nut_client_matches(adapter, settings, servers):
            record = _client_record(client, source, site["id"], observed_aps, now)
            if record is not None:
                record["ip_address"] = address
                matches[server.id].append(record)
    except (requests.RequestException, ValueError, TypeError, KeyError):
        return None
    return dict(matches)


def reconcile_nut_wireless(
    db: Session, candidates: Mapping[int, list[dict[str, Any]]],
    *, sampled_at: datetime | None = None,
) -> None:
    """Store one unambiguous wireless match, or an explicit unknown state.

    Args:
        db: Write transaction; no audit events are generated for telemetry.
        candidates: Aggregated matches across all successfully read UniFi
            sources and sites, keyed by NUT server observation ID.
        sampled_at: Time used for an absent or ambiguous current match.
    """
    now = sampled_at or datetime.now(timezone.utc)
    servers = {device.id: device for matches in nut_servers_by_address(db).values() for device in matches}
    for server in servers.values():
        records = candidates.get(server.id, [])
        record = records[0] if len(records) == 1 else {
            "state": "ambiguous" if records else "not_seen",
            "evidence": "unifi-connected-client-ip", "sampled_at": now.isoformat(),
        }
        source = db.get(DiscoverySource, server.source_id)
        if source is not None:
            record_telemetry(db, source, server.remote_id, "wireless", record)
