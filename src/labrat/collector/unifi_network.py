# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Read a curated UniFi network snapshot for the Networks page.

Every request is a read-only ``GET`` with the integration's API key:
networks (VLAN, subnet, DHCP and its DNS servers), firewall zones and named
policies, traffic matching lists, Wi-Fi broadcasts, WANs, devices with their
latest statistics, connected clients, and from the classic API each
client's network and radio and each subsystem's health. Results are
whitelisted and stored as the ``unifi_network`` telemetry key on the
source's gateway observation; raw responses and configuration blobs (such
as Wi-Fi passphrases) are never kept.

A failed part is named in ``errors`` and leaves the rest of the snapshot
usable; a failed refresh keeps the previous snapshot and marks it
unavailable. Nothing here changes the controller or discovery state.
"""

from datetime import datetime, timezone
from functools import partial
import json
import logging
import time
from typing import Any, Callable
from urllib.parse import quote

from sqlalchemy import select

from labrat.coerce import integer, json_object, number, text
from labrat.collector.http import controller_url, required_string, tls_verification, unifi_network_root
from labrat.discovery.models import DiscoveredDevice

_LOGGER = logging.getLogger(__name__)

#: Seconds between snapshots per source, and the time one may take.
REFRESH_SECONDS = 300
DEADLINE_SECONDS = 60
#: Most rows read from any one collection, and most devices whose detail and
#: statistics are read (two requests each; the shared deadline still bounds
#: the snapshot). Devices past the limit keep their list row only, and the
#: snapshot's errors record ``device_detail: limited``.
MAX_ROWS = 2000
MAX_DEVICE_READS = 256


# Whole numbers from the integration API truncate (labrat.coerce).
_integer = partial(integer, truncate=True)


class _Reader:
    """Bounded GETs against one UniFi controller with a shared deadline."""

    def __init__(self, adapter: Any, settings: dict, clock: Callable[[], float] = time.monotonic) -> None:
        """Prepare the base URL, key header, and TLS setting.

        Args:
            adapter: The UniFi adapter whose HTTP client is reused.
            settings: The source's private connection settings.
            clock: Monotonic clock; tests may supply their own.
        """
        self.client = adapter.client
        self.root = unifi_network_root(settings)
        self.base = self.root + "/integration/v1"
        self.headers = {"Accept": "application/json", "X-API-Key": required_string(settings, "api_key")}
        self.verify = tls_verification(settings)
        self.clock = clock
        self.deadline = clock() + DEADLINE_SECONDS

    def get(self, url: str) -> Any:
        """Return one decoded response.

        Args:
            url: Absolute URL.

        Returns:
            Any: The decoded JSON.

        Raises:
            TimeoutError: The shared deadline has passed.
        """
        if self.clock() > self.deadline:
            raise TimeoutError("UniFi network read deadline passed")
        return self.client.get_json(url, self.headers, self.verify)

    def pages(self, path: str) -> list[dict]:
        """Read every page of an integration collection.

        Args:
            path: Path under ``/integration/v1``.

        Returns:
            list[dict]: All rows.

        Raises:
            ValueError: Malformed paging or too many rows.
        """
        rows: list[dict] = []
        offset = 0
        joiner = "&" if "?" in path else "?"
        for _ in range(40):
            payload = self.get(f"{self.base}{path}{joiner}offset={offset}&limit=200")
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                raise ValueError("Unexpected UniFi collection schema")
            rows.extend(row for row in data if isinstance(row, dict))
            total = _integer(payload.get("totalCount"))
            if len(rows) > MAX_ROWS:
                raise ValueError("UniFi collection bound exceeded")
            if not data or total is None or len(rows) >= total:
                return rows
            offset += len(data)
        raise ValueError("UniFi collection did not finish paging")

    def classic(self, site: str, path: str) -> list[dict]:
        """Read a classic API list for a site (``/api/s/{site}/...``).

        Args:
            site: The site's internal reference (such as ``default``).
            path: Path after the site, such as ``stat/sta``.

        Returns:
            list[dict]: The ``data`` rows.

        Raises:
            ValueError: When the reply is not the classic API's list envelope.
        """
        url = f"{self.root}/api/s/{quote(site, safe='')}/{path}"
        try:
            payload = self.get(url)
        except TimeoutError:
            raise
        except Exception:  # The classic API sometimes drops one read; retry once.
            payload = self.get(url)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) > MAX_ROWS:
            raise ValueError("Unexpected classic UniFi response")
        return [row for row in data if isinstance(row, dict)]


def _origin(row: dict) -> str | None:
    """Return a row's metadata origin (USER_DEFINED, SYSTEM_DEFINED, ...).

    Args:
        row: An integration API record.

    Returns:
        str | None: Its origin, or None when it has none.
    """
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return text(metadata.get("origin"), 30)


def _network(row: dict) -> dict:
    """Curate a network detail: VLAN, subnet, gateway, DHCP, and switches.

    Args:
        row: A network detail from ``/sites/{site}/networks/{id}``.

    Returns:
        dict: Identity, VLAN, zone, IPv4 gateway and prefix, DHCP mode,
            range, DNS servers, domain, and isolation and internet flags.
    """
    ipv4 = row.get("ipv4Configuration") if isinstance(row.get("ipv4Configuration"), dict) else {}
    dhcp = ipv4.get("dhcpConfiguration") if isinstance(ipv4.get("dhcpConfiguration"), dict) else {}
    span = dhcp.get("ipAddressRange") if isinstance(dhcp.get("ipAddressRange"), dict) else {}
    dns = dhcp.get("dnsServerIpAddressesOverride")
    return {
        "id": text(row.get("id"), 64), "name": text(row.get("name"), 80), "vlan": _integer(row.get("vlanId")),
        "zone_id": text(row.get("zoneId"), 64), "enabled": row.get("enabled") is not False,
        "management": text(row.get("management"), 20), "origin": _origin(row), "default": row.get("default") is True,
        "gateway": text(ipv4.get("hostIpAddress"), 64), "prefix": _integer(ipv4.get("prefixLength")),
        "dhcp": {
            "mode": text(dhcp.get("mode"), 20),
            "start": text(span.get("start"), 64), "stop": text(span.get("stop"), 64),
            "dns": [value for value in (dns if isinstance(dns, list) else []) if isinstance(value, str)][:8],
            "domain": text(dhcp.get("domainName"), 80),
            "lease_seconds": _integer(dhcp.get("leaseTimeSeconds")),
        } if dhcp else None,
        "isolated": row.get("isolationEnabled") is True,
        "internet": row.get("internetAccessEnabled") is not False,
        "mdns": row.get("mdnsForwardingEnabled") is True,
    }


def _filter(endpoint: dict, lists: dict[str, dict]) -> dict:
    """Summarize one policy endpoint's traffic filter for display.

    Args:
        endpoint: A policy ``source`` or ``destination``.
        lists: Traffic matching lists by ID.

    Returns:
        dict: ``zone``, and when filtered: ``kind`` (addresses, ports,
            networks, other), the ``values`` it names (resolving traffic
            matching lists by name), ``ports``, and ``negate``.
    """
    found: dict[str, Any] = {"zone": text(endpoint.get("zoneId"), 64)}
    traffic = endpoint.get("trafficFilter") if isinstance(endpoint.get("trafficFilter"), dict) else None
    if not traffic:
        return found

    def values(block: Any) -> tuple[list[str], bool]:
        if not isinstance(block, dict):
            return [], False
        negate = block.get("matchOpposite") is True
        if block.get("type") == "TRAFFIC_MATCHING_LIST":
            named = lists.get(str(block.get("trafficMatchingListId")))
            return ([f"list: {named['name']}"] if named else ["list: unknown"]), negate
        out = []
        for item in block.get("items") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") in ("IP_ADDRESS", "SUBNET", "PORT_NUMBER"):
                out.append(str(item.get("value")))
            elif item.get("type") in ("IP_ADDRESS_RANGE", "PORT_NUMBER_RANGE"):
                out.append(f"{item.get('start')}–{item.get('stop')}")
        if block.get("networkIds"):
            out.extend(f"network:{value}" for value in block["networkIds"][:16])
        return out[:24], negate

    kind = str(traffic.get("type") or "").upper()
    addresses, negate_addresses = values(traffic.get("ipAddressFilter") or traffic.get("networkFilter"))
    ports, negate_ports = values(traffic.get("portFilter"))
    found.update({
        "kind": {"IP_ADDRESS": "addresses", "PORT": "ports", "NETWORK": "networks"}.get(kind, "other"),
        "values": addresses, "ports": ports, "negate": negate_addresses or negate_ports,
        "raw_kind": text(kind, 40),
    })
    return found


def _policy(row: dict, lists: dict[str, dict]) -> dict:
    """Curate one firewall policy for the zone graph.

    Args:
        row: A policy from ``/sites/{site}/firewall/policies``.
        lists: Traffic matching lists by ID.

    Returns:
        dict: Name, order, action, enabled, origin, source and destination
            summaries, protocol, IP version, and whether it logs.
    """
    action = row.get("action") if isinstance(row.get("action"), dict) else {}
    scope = row.get("ipProtocolScope") if isinstance(row.get("ipProtocolScope"), dict) else {}
    protocol = scope.get("protocolFilter") if isinstance(scope.get("protocolFilter"), dict) else {}
    named = protocol.get("protocol") if isinstance(protocol.get("protocol"), dict) else {}
    preset = protocol.get("preset") if isinstance(protocol.get("preset"), dict) else {}
    return {
        "id": text(row.get("id"), 64), "name": text(row.get("name"), 120), "index": _integer(row.get("index")),
        "action": text(action.get("type"), 20), "enabled": row.get("enabled") is not False, "origin": _origin(row),
        "source": _filter(row.get("source") or {}, lists), "destination": _filter(row.get("destination") or {}, lists),
        "protocol": text(named.get("name") or preset.get("name"), 20),
        "protocol_negate": protocol.get("matchOpposite") is True,
        "ip_version": text(scope.get("ipVersion"), 20),
        "states": [str(value)[:20] for value in (row.get("connectionStateFilter") or [])][:6]
        if isinstance(row.get("connectionStateFilter"), list) else None,
        "logging": row.get("loggingEnabled") is True,
    }


def _device(row: dict, stats: dict | None) -> dict:
    """Curate a UniFi device with its port summary and latest statistics.

    Args:
        row: A device detail.
        stats: Its ``statistics/latest``, if read.

    Returns:
        dict: Identity, model, state, address, firmware, uplink, ports
            (up, total, speeds), and uptime, CPU, memory, and uplink rates.
    """
    interfaces = row.get("interfaces") if isinstance(row.get("interfaces"), dict) else {}
    ports = [port for port in interfaces.get("ports") or [] if isinstance(port, dict)]
    radios = [radio for radio in interfaces.get("radios") or [] if isinstance(radio, dict)]
    uplink = row.get("uplink") if isinstance(row.get("uplink"), dict) else {}
    stats = stats if isinstance(stats, dict) else {}
    uplink_stats = stats.get("uplink") if isinstance(stats.get("uplink"), dict) else {}
    features = row.get("features")
    return {
        "id": text(row.get("id"), 64), "name": text(row.get("name"), 80), "model": text(row.get("model"), 80),
        "state": text(row.get("state"), 20), "ip": text(row.get("ipAddress"), 64), "mac": text(row.get("macAddress"), 24),
        "firmware": text(row.get("firmwareVersion"), 40), "updatable": row.get("firmwareUpdatable") is True,
        "features": sorted(features) if isinstance(features, dict) else [str(item)[:20] for item in features or []][:8],
        "uplink_device": text(uplink.get("deviceId"), 64),
        "ports": [{"idx": _integer(port.get("idx")), "state": text(port.get("state"), 12),
                   "connector": text(port.get("connector"), 16), "speed": _integer(port.get("speedMbps")),
                   "max_speed": _integer(port.get("maxSpeedMbps")),
                   "poe": text((port.get("poe") or {}).get("state"), 16) if isinstance(port.get("poe"), dict) else None}
                  for port in ports[:64]],
        "radios": [{"band": number(radio.get("frequencyGHz")), "channel": _integer(radio.get("channel")),
                    "width": _integer(radio.get("channelWidthMHz")), "standard": text(radio.get("wlanStandard"), 20)}
                   for radio in radios[:6]],
        "uptime": _integer(stats.get("uptimeSec")), "cpu": number(stats.get("cpuUtilizationPct")),
        "memory": number(stats.get("memoryUtilizationPct")),
        "tx_bps": number(uplink_stats.get("txRateBps")), "rx_bps": number(uplink_stats.get("rxRateBps")),
    }


def _classic_client(row: dict) -> dict:
    """Curate the classic API's view of a client: network and radio.

    Args:
        row: A ``stat/sta`` row.

    Returns:
        dict: Hostname, network, SSID, radio, channel, signal, satisfaction,
            wired rate, vendor guess flags, and traffic counters.
    """
    return {
        "hostname": text(row.get("hostname"), 80), "network": text(row.get("network"), 80),
        "essid": text(row.get("essid"), 64), "radio": text(row.get("radio_proto") or row.get("radio"), 12),
        "channel": _integer(row.get("channel")), "signal": _integer(row.get("signal")),
        "satisfaction": _integer(row.get("satisfaction")), "wired_rate": _integer(row.get("wired_rate_mbps")),
        "oui": text(row.get("oui"), 60), "is_guest": row.get("is_guest") is True,
        "tx_bytes": _integer(row.get("tx_bytes")), "rx_bytes": _integer(row.get("rx_bytes")),
        "uptime": _integer(row.get("uptime")),
    }


def network_snapshot(adapter: Any, settings: dict, clock: Callable[[], float] = time.monotonic) -> dict:
    """Read one UniFi source's network snapshot (first site only).

    Args:
        adapter: The UniFi adapter whose HTTP client is reused.
        settings: The source's private connection settings.
        clock: Monotonic clock for the deadline.

    Returns:
        dict: ``site``, ``networks``, ``zones``, ``policies``, ``lists``,
            ``wifi``, ``wans``, ``devices``, ``clients``, ``health``, and
            ``errors`` naming any part that failed.

    Raises:
        ValueError: The site list is unreadable.
        requests.RequestException: The controller cannot be contacted.
    """
    reader = _Reader(adapter, settings, clock)
    sites = reader.pages("/sites")
    if not sites:
        raise ValueError("No UniFi site")
    site = sites[0]
    site_id = str(site.get("id"))
    prefix = f"/sites/{quote(site_id, safe='')}"
    errors: dict[str, str] = {}

    def attempt(part: str, read: Callable[[], Any], default: Any) -> Any:
        try:
            return read()
        except Exception as exc:  # Each part is optional.
            _LOGGER.info("UniFi %s unavailable (%s)", part, type(exc).__name__)
            errors[part] = "unavailable"
            return default

    summaries = attempt("networks", lambda: reader.pages(f"{prefix}/networks"), [])
    networks = []
    for row in summaries[:128]:
        detail = attempt("network_detail", lambda: reader.get(f"{reader.base}{prefix}/networks/{quote(str(row.get('id')), safe='')}"), row)
        networks.append(_network(detail if isinstance(detail, dict) else row))
    lists = {str(row.get("id")): {"name": text(row.get("name"), 80), "type": text(row.get("type"), 30),
                                  "items": [str(item.get("value") or f"{item.get('start')}–{item.get('stop')}")
                                            for item in row.get("items") or [] if isinstance(item, dict)][:64]}
             for row in attempt("lists", lambda: reader.pages(f"{prefix}/traffic-matching-lists"), [])}
    zones = [{"id": text(row.get("id"), 64), "name": text(row.get("name"), 80), "origin": _origin(row),
              "network_ids": [str(value) for value in row.get("networkIds") or []][:64]}
             for row in attempt("zones", lambda: reader.pages(f"{prefix}/firewall/zones"), [])]
    policies = [_policy(row, lists) for row in attempt("policies", lambda: reader.pages(f"{prefix}/firewall/policies"), [])]
    wifi = [{"name": text(row.get("name"), 64), "type": text(row.get("type"), 30), "enabled": row.get("enabled") is not False,
             "security": text((row.get("securityConfiguration") or {}).get("type"), 40) if isinstance(row.get("securityConfiguration"), dict) else None,
             "bands": [value for value in row.get("broadcastingFrequenciesGHz") or [] if isinstance(value, (int, float))][:4],
             "network": text((row.get("network") or {}).get("networkId") or (row.get("network") or {}).get("type"), 64)
             if isinstance(row.get("network"), dict) else None,
             "devices": len(((row.get("broadcastingDeviceFilter") or {}).get("deviceIds") or []))
             if isinstance(row.get("broadcastingDeviceFilter"), dict) else None}
            for row in attempt("wifi", lambda: reader.pages(f"{prefix}/wifi/broadcasts"), [])]
    wans = [{"id": text(row.get("id"), 64), "name": text(row.get("name"), 64)}
            for row in attempt("wans", lambda: reader.pages(f"{prefix}/wans"), [])]
    devices = []
    for index, row in enumerate(attempt("devices", lambda: reader.pages(f"{prefix}/devices"), [])):
        device_id = quote(str(row.get("id")), safe="")
        detail = attempt("device_detail", lambda: reader.get(f"{reader.base}{prefix}/devices/{device_id}"), row) if index < MAX_DEVICE_READS else row
        stats = attempt("device_stats", lambda: reader.get(f"{reader.base}{prefix}/devices/{device_id}/statistics/latest"), None) if index < MAX_DEVICE_READS else None
        devices.append(_device(detail if isinstance(detail, dict) else row, stats))
    if len(devices) > MAX_DEVICE_READS:
        errors.setdefault("device_detail", "limited")
    classic_site = text(site.get("internalReference"), 64) or "default"
    classic = {str(row.get("mac") or "").lower(): _classic_client(row)
               for row in attempt("classic_clients", lambda: reader.classic(classic_site, "stat/sta"), []) if row.get("mac")}
    clients = []
    for row in attempt("clients", lambda: reader.pages(f"{prefix}/clients"), []):
        mac = str(row.get("macAddress") or "").lower()
        access = row.get("access") if isinstance(row.get("access"), dict) else {}
        clients.append({
            "name": text(row.get("name"), 80), "type": text(row.get("type"), 16), "ip": text(row.get("ipAddress"), 64),
            "mac": mac or None, "uplink_device": text(row.get("uplinkDeviceId"), 64),
            "connected_at": text(row.get("connectedAt"), 40), "access": text(access.get("type"), 30),
            **classic.get(mac, {}),
        })
    health = [{"subsystem": text(row.get("subsystem"), 20), "status": text(row.get("status"), 20),
               "users": _integer(row.get("num_user")), "guests": _integer(row.get("num_guest")),
               "adopted": _integer(row.get("num_adopted")), "disconnected": _integer(row.get("num_disconnected")),
               "tx_bps": number(row.get("tx_bytes-r")), "rx_bps": number(row.get("rx_bytes-r"))}
              for row in attempt("health", lambda: reader.classic(classic_site, "stat/health"), [])]
    # Neighbouring Wi-Fi: counts per channel only. Their names and addresses
    # describe other people's networks, so they are never stored.
    neighbours: dict[str, dict] = {}
    for row in attempt("neighbours", lambda: reader.classic(classic_site, "stat/rogueap"), []):
        channel = _integer(row.get("channel"))
        if channel is None:
            continue
        band = "2.4" if channel <= 14 else "6" if (_integer(row.get("freq")) or 0) > 5925 else "5"
        item = neighbours.setdefault(f"{band}:{channel}", {"band": band, "channel": channel, "count": 0, "strongest": None})
        item["count"] += 1
        rssi = _integer(row.get("rssi"))
        if rssi is not None and (item["strongest"] is None or rssi > item["strongest"]):
            item["strongest"] = rssi
    return {"site": {"id": text(site_id, 64), "name": text(site.get("name"), 80)}, "networks": networks, "zones": zones,
            "neighbours": sorted(neighbours.values(), key=lambda item: (item["band"], item["channel"])),
            "policies": policies, "lists": list(lists.values()), "wifi": wifi, "wans": wans, "devices": devices,
            "clients": clients, "health": health, "errors": errors}


def collect_network_snapshot(sessions: Any, source: Any, adapter: Any, settings: dict) -> None:
    """Refresh the Networks page snapshot at most every five minutes.

    Stored as ``unifi_network`` telemetry on the source's gateway
    observation (the one with WAN telemetry, else the first router). A
    failed refresh keeps the previous snapshot and marks it unavailable.

    Args:
        sessions: Session factory for short write transactions.
        source: The UniFi discovery source.
        adapter: The UniFi adapter whose HTTP client is reused.
        settings: The source's private connection settings.
    """
    attempts = getattr(adapter, "_network_attempts", {})
    try:
        key = (source.id, controller_url(settings))
    except (ValueError, TypeError, KeyError):  # Incomplete settings: nothing to read.
        return
    now = time.monotonic()
    if now - attempts.get(key, -10 * REFRESH_SECONDS) < REFRESH_SECONDS:
        return
    attempts[key] = now
    adapter._network_attempts = attempts
    try:
        snapshot = network_snapshot(adapter, settings)
    except Exception as exc:
        _LOGGER.warning("UniFi network snapshot unavailable (%s)", type(exc).__name__)
        snapshot = None
    with sessions.begin() as db:
        hosts = list(db.scalars(select(DiscoveredDevice).where(DiscoveredDevice.source_id == source.id)))
        gateway = next((host for host in hosts if "wan" in json_object(host.details_json)), None) or next(
            (host for host in hosts if host.category == "router"), None)
        if gateway is None:
            return
        details = json_object(gateway.details_json)
        if snapshot is not None:
            details["unifi_network"] = {**snapshot, "available": True, "sampled_at": datetime.now(timezone.utc).isoformat()}
        elif isinstance(details.get("unifi_network"), dict):
            details["unifi_network"] = {**details["unifi_network"], "available": False}
        else:
            return
        gateway.details_json = json.dumps(details)
