# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Read an OPNsense firewall without changing it.

OPNsense has no read-only API role: each page privilege also allows that
page's actions. So this module's own contract is the protection
(``planning/specs/opnsense-integration.md``, "Read-only contract"):

- One client (:class:`OpnsenseClient`) makes every request, for polls and
  for Settings' Test connection alike.
- It sends only ``GET``, only to the exact paths in :data:`ALLOWED_PATHS`,
  with no query parameters, to the configured origin, with TLS as
  configured, and it refuses redirects.
- Each response passes through a projection that keeps only named fields;
  everything else is dropped before anything is stored, logged, or
  returned. Errors carry only their type and HTTP status.

OPNsense 26.7 answers ``GET`` on its search endpoints with every row at
once (it ignores paging parameters on ``GET``), so nothing is paged; a
list longer than :data:`MAX_ROWS` is cut there and its section is
``partial``.

The firewall is one observation with the fixed remote ID ``firewall``
(one integration is one firewall), its physical NICs are its ports, and
its health is the ``opnsense`` telemetry key. Each part of a poll ends
``complete``, ``partial``, ``failed`` (kept from the last good poll), or
``not_available`` (a missing privilege or plugin); only
``system_information`` is required.

Used by: api.network_shape, collector.__main__, integrations.
"""

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

import requests

from labrat.coerce import mac_address, number, text
from labrat.collector import watchlist
from labrat.collector.contracts import DeviceObservation, DiscoveryBatch, PortObservation
from labrat.collector.http import controller_url, required_string, tls_verification

#: Seconds before one OPNsense request gives up.
TIMEOUT_SECONDS = 10

#: Rows kept from one list; longer lists are cut and marked partial.
MAX_ROWS = 5000

#: The firewall's observation: one integration is one firewall.
FIREWALL = "firewall"

#: Every path the client may request, under ``/api/``; nothing else. The
#: endpoints the spec defers (lldpd, the ISC DHCP plugin, OpenVPN and IPsec
#: sessions) join only once a fixture shows their responses.
ALLOWED_PATHS = frozenset({
    "core/system/status",
    "core/service/search",
    "diagnostics/system/system_information",
    "diagnostics/system/system_time",
    "diagnostics/system/system_resources",
    "diagnostics/system/system_temperature",
    "diagnostics/system/system_disk",
    "diagnostics/system/system_swap",
    "interfaces/overview/interfaces_info",
    "diagnostics/interface/get_interface_statistics",
    "diagnostics/interface/search_arp",
    "diagnostics/interface/search_ndp",
    "diagnostics/interface/get_routes",
    "diagnostics/interface/get_vip_status",
    "routes/gateway/status",
    "dnsmasq/leases/search",
    "dnsmasq/settings/get",
    "kea/leases4/search",
    "kea/dhcpv4/search_subnet",
    "kea/dhcpv4/search_reservation",
    "unbound/settings/get",
    "unbound/settings/search_dnsbl",
    "unbound/overview/is_block_list_enabled",
    "firewall/filter/search_rule",
    "firewall/alias/search_item",
    "firewall/group/search_item",
    "wireguard/service/show",
    "wireguard/service/status",
})

#: Sections stored as the firewall's health (the ``opnsense`` telemetry key).
HEALTH_SECTIONS = ("interfaces", "statistics", "time", "memory", "temperatures", "disks", "swap")

#: Sections stored as its network snapshot (``opnsense_network``).
NETWORK_SECTIONS = ("gateways", "routes", "dns", "dnsmasq", "kea_subnets", "kea_reservations", "rules", "aliases",
                    "groups", "services", "carp", "blocklist_enabled", "blocklists", "wireguard", "wireguard_service")

#: Sections that report clients; they become sightings, not telemetry.
SIGHTING_SECTIONS = ("arp", "ndp", "dnsmasq_leases", "kea_leases")

#: Rule evaluation stages by the prefix of ``sort_order`` (OPNsense 26.7):
#: floating rules, then interface groups, then interfaces. Other prefixes
#: are the automatic system rules around them.
RULE_SCOPES = {"200000": "floating", "300000": "group", "400000": "interface"}

#: The ``ref`` OPNsense gives the owner's rules still in the old format.
MIGRATION_REF = "ui/firewall/migration"

#: A link speed in a FreeBSD media string, such as ``1000baseT`` or ``10Gbase-SR``.
_MEDIA_SPEED = re.compile(r"(\d+(?:\.\d+)?)\s*(G?)base", re.IGNORECASE)


class RequestRefused(ValueError):
    """The client was asked for something outside its read-only contract."""


class SectionUnavailable(Exception):
    """A part of the poll the key may not read, or that is not installed (403, 404)."""


class OpnsenseClient:
    """The only way this module talks to OPNsense: allowlisted GETs."""

    def __init__(self, settings: Mapping[str, Any], session: requests.Session | None = None) -> None:
        """Bind the client to one firewall's settings.

        Args:
            settings: The integration's private settings (``url``,
                ``api_key``, ``api_secret``, TLS options).
            session: Optional HTTP session for tests or reuse.

        Raises:
            ValueError: The URL, credential, or TLS settings are invalid.
        """
        self.root = controller_url(settings)
        self.auth = (required_string(settings, "api_key"), required_string(settings, "api_secret"))
        self.verify = tls_verification(settings)
        self.session = session if session is not None else requests.Session()

    def get(self, path: str) -> Any:
        """Read one allowlisted endpoint and decode its JSON.

        Args:
            path: A path from :data:`ALLOWED_PATHS`, without ``/api/``.

        Returns:
            Any: The decoded JSON.

        Raises:
            RequestRefused: The path is not allowlisted, or the firewall
                answered with a redirect.
            requests.RequestException: The request failed or returned an
                error status.
            ValueError: The body was not JSON.
        """
        if path not in ALLOWED_PATHS:
            raise RequestRefused("path not allowed")
        response = self.session.request("GET", f"{self.root}/api/{path}", auth=self.auth, timeout=TIMEOUT_SECONDS,
                                        verify=self.verify, allow_redirects=False)
        if 300 <= response.status_code < 400:
            raise RequestRefused("redirect refused")
        response.raise_for_status()
        return response.json()


def _rows(payload: Any) -> tuple[list[dict], bool]:
    """The rows of a search answer, capped.

    Args:
        payload: A search endpoint's JSON (``{"rows": [...]}``).

    Returns:
        tuple[list[dict], bool]: The object rows, and whether they are all of
            them: false when the list was cut or held anything but objects.

    Raises:
        ValueError: The answer has no list of rows.
    """
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("no rows")
    kept = [row for row in rows[:MAX_ROWS] if isinstance(row, dict)]
    return kept, len(rows) <= MAX_ROWS and len(kept) == len(rows)


def _shape(payload: Any, key: str | None, kind: type) -> Any:
    """The part of an answer a projection reads, or a refusal.

    An answer of the wrong shape is a failed read, never a valid empty one.

    Args:
        payload: The decoded answer.
        key: The member holding the data, or None for the answer itself.
        kind: The type that part must have (``dict`` or ``list``).

    Returns:
        Any: That part.

    Raises:
        ValueError: The answer does not have that shape.
    """
    part = payload.get(key) if key is not None and isinstance(payload, dict) else (payload if key is None else None)
    if not isinstance(part, kind):
        raise ValueError(f"unexpected {key or 'answer'} shape")
    return part


def _number(value: Any) -> float | None:
    """A number OPNsense may send as a number or as numeric text.

    Args:
        value: The decoded value.

    Returns:
        float | None: The number, or None when it is not one.
    """
    return number(value, strings=True)


def media_speed(media: Any) -> int | None:
    """The link speed in a FreeBSD media string.

    Args:
        media: Such as ``1000baseT <full-duplex>`` or ``10Gbase-SR``.

    Returns:
        int | None: Megabits per second, or None when it names no speed.
    """
    found = _MEDIA_SPEED.search(media) if isinstance(media, str) else None
    if not found:
        return None
    value = float(found.group(1)) * (1000 if found.group(2) else 1)
    return int(value)


def _mac(value: Any) -> str | None:
    """A MAC address in lowercase, or None for a missing or all-zero one.

    Args:
        value: A reported MAC address.

    Returns:
        str | None: Such as ``02:00:00:00:00:01``.
    """
    digits = mac_address(value)
    if not digits or digits == "0" * 12:
        return None
    return ":".join(digits[index:index + 2] for index in range(0, 12, 2))


def _integer(value: Any) -> int | None:
    """A whole number OPNsense may send as a number or as numeric text.

    Args:
        value: The decoded value.

    Returns:
        int | None: The number, or None when it is not a whole one.
    """
    found = _number(value)
    return int(found) if found is not None and found == int(found) else None


def _address(value: Any) -> str | None:
    """An IP address without its zone (``fe80::1%em0``), or None.

    Args:
        value: A reported address.

    Returns:
        str | None: The normalized address, or None when it is not one.
    """
    if not isinstance(value, str):
        return None
    try:
        return str(ipaddress.ip_address(value.split("%", 1)[0].strip()))
    except ValueError:
        return None


def _addresses(value: Any, limit: int = 8) -> list[str]:
    """The valid addresses in a comma-separated list.

    Args:
        value: Such as ``"192.0.2.1,192.0.2.2"``.
        limit: Most addresses kept.

    Returns:
        list[str]: The addresses, in order.
    """
    if not isinstance(value, str):
        return []
    return [address for part in value.split(",") if (address := _address(part))][:limit]


def _selected(value: Any) -> list[str]:
    """The chosen keys of a settings answer's option field.

    Args:
        value: ``{"lan": {"value": "LAN", "selected": 1}, ...}``, or a
            comma-separated string.

    Returns:
        list[str]: The selected keys (such as ``["lan", "opt1"]``).
    """
    if isinstance(value, dict):
        return [str(key)[:40] for key, item in value.items()
                if isinstance(item, dict) and item.get("selected") in (1, True, "1")][:32]
    if isinstance(value, str):
        return [part.strip()[:40] for part in value.split(",") if part.strip()][:32]
    return []


def _items(value: Any) -> list[dict]:
    """The entries of a settings answer's list (a UUID-keyed object, or ``[]`` when empty).

    Args:
        value: The list field.

    Returns:
        list[dict]: Its entries, capped at :data:`MAX_ROWS`.

    Raises:
        ValueError: It is neither.
    """
    if isinstance(value, dict):
        return [item for item in list(value.values())[:MAX_ROWS] if isinstance(item, dict)]
    if isinstance(value, list):
        return [item for item in value[:MAX_ROWS] if isinstance(item, dict)]
    raise ValueError("unexpected list shape")


def _flag(value: Any) -> bool:
    """A settings answer's boolean (``"1"`` or ``"0"``).

    Args:
        value: The field.

    Returns:
        bool: Whether it is on.
    """
    return value in ("1", 1, True)


def _measure(value: Any) -> float | None:
    """A dpinger reading such as ``"1.2 ms"`` or ``"0.0 %"``; ``~`` is none.

    Args:
        value: The reported reading.

    Returns:
        float | None: The number, or None when it is not measured.
    """
    if isinstance(value, str):
        value = value.replace("ms", "").replace("%", "").strip()
    return _number(value)


# ---------------------------------------------------------------------------
# Projections: the only fields kept from each answer.
# ---------------------------------------------------------------------------

def project_system(payload: Any) -> dict:
    """Keep the firewall's name and software versions.

    Args:
        payload: ``diagnostics/system/system_information``.

    Returns:
        dict: ``hostname``, ``product`` (such as ``OPNsense 26.7-amd64``),
            and ``versions``.

    Raises:
        ValueError: The answer names no firewall.
    """
    if not isinstance(payload, dict) or not text(payload.get("name"), 255):
        raise ValueError("no system information")
    versions = [text(item, 80) for item in payload.get("versions") or [] if isinstance(item, str)][:6]
    product = next((item for item in versions if item and item.lower().startswith("opnsense")), None)
    return {"hostname": text(payload.get("name"), 255), "product": product, "versions": versions}


def project_time(payload: Any) -> dict:
    """Keep uptime, boot time, and load.

    Args:
        payload: ``diagnostics/system/system_time``.

    Returns:
        dict: ``uptime``, ``boottime``, and ``loadavg`` (three numbers).

    Raises:
        ValueError: The answer is not an object with an uptime.
    """
    payload = _shape(payload, None, dict)
    if not isinstance(payload.get("uptime"), str):
        raise ValueError("no uptime")
    load = [_number(part) for part in str(payload.get("loadavg") or "").split(",")][:3]
    return {"uptime": text(payload.get("uptime"), 40), "boottime": text(payload.get("boottime"), 60),
            "loadavg": load if all(value is not None for value in load) and load else None}


def project_memory(payload: Any) -> dict:
    """Keep memory use in bytes.

    Args:
        payload: ``diagnostics/system/system_resources``.

    Returns:
        dict: ``total`` and ``used`` bytes.

    Raises:
        ValueError: The answer has no memory object.
    """
    memory = _shape(payload, "memory", dict)
    return {"total": _number(memory.get("total")), "used": _number(memory.get("used"))}


def project_temperatures(payload: Any) -> list[dict]:
    """Keep each temperature sensor's name and reading.

    Args:
        payload: ``diagnostics/system/system_temperature`` (a list).

    Returns:
        list[dict]: ``sensor`` and ``celsius`` per sensor; empty on most VMs.

    Raises:
        ValueError: The answer is not a list.
    """
    return [{"sensor": text(row.get("device") or row.get("type_translated") or row.get("type"), 80),
             "celsius": _number(row.get("temperature"))}
            for row in _shape(payload, None, list)[:32] if isinstance(row, dict)]


def project_disks(payload: Any) -> list[dict]:
    """Keep each mounted file system's use.

    Args:
        payload: ``diagnostics/system/system_disk``.

    Returns:
        list[dict]: ``mountpoint``, ``type``, ``used_pct``, and the sizes as
            reported (such as ``2.8G``).

    Raises:
        ValueError: The answer has no list of devices.
    """
    devices = _shape(payload, "devices", list)
    return [{"mountpoint": text(row.get("mountpoint"), 120), "type": text(row.get("type"), 20),
             "used_pct": _number(row.get("used_pct")), "size": text(row.get("blocks"), 20),
             "used": text(row.get("used"), 20), "available": text(row.get("available"), 20)}
            for row in devices[:16] if isinstance(row, dict)]


def project_swap(payload: Any) -> list[dict]:
    """Keep each swap device's use.

    Args:
        payload: ``diagnostics/system/system_swap``.

    Returns:
        list[dict]: ``device``, ``total``, and ``used`` per swap device.

    Raises:
        ValueError: The answer has no list of swap devices.
    """
    swap = _shape(payload, "swap", list)
    return [{"device": text(row.get("device"), 80), "total": _number(row.get("total")), "used": _number(row.get("used"))}
            for row in swap[:8] if isinstance(row, dict)]


def project_interfaces(payload: Any) -> tuple[list[dict], bool]:
    """Keep each interface's identity, link, and addresses.

    Args:
        payload: ``interfaces/overview/interfaces_info``.

    Returns:
        tuple[list[dict], bool]: ``device``, ``identifier``,
            ``description``, ``physical``, ``status``, ``media``, ``speed``,
            ``mac``, ``mtu``, ``ipv4``, ``ipv6`` (the primary address with
            its prefix), ``enabled``, ``vlan`` (its tag), ``parent`` (a
            VLAN's NIC), and ``gateways``
            (the gateway addresses it routes through: a WAN) per interface; and
            whether the list is complete. A row without a device name or
            an explicit physical flag makes it incomplete: it might be a
            NIC, so no NIC may be removed on its account.

    Raises:
        ValueError: The answer has no list of rows.
    """
    rows, complete = _rows(payload)
    found = []
    for row in rows:
        device = text(row.get("device"), 40)
        if not device or not isinstance(row.get("is_physical"), bool):
            complete = False
            continue
        found.append({
            "device": device, "identifier": text(row.get("identifier"), 40), "description": text(row.get("description"), 80),
            "physical": row.get("is_physical") is True, "status": text(row.get("status"), 12),
            "media": text(row.get("media"), 80), "speed": media_speed(row.get("media")),
            "mac": _mac(row.get("macaddr_hw")) or _mac(row.get("macaddr")), "mtu": _number(row.get("mtu")),
            "ipv4": [text(row.get("addr4"), 64)] if text(row.get("addr4"), 64) else [],
            "ipv6": [text(row.get("addr6"), 64)] if text(row.get("addr6"), 64) else [],
            "enabled": row.get("enabled") is not False, "vlan": _integer(row.get("vlan_tag")),
            "parent": text(row["vlan"].get("parent"), 40) if isinstance(row.get("vlan"), dict) else None,
            "gateways": [address for item in (row.get("gateways") or [])[:8] if (address := _address(item))],
        })
    return found, complete


def project_statistics(payload: Any) -> dict[str, dict]:
    """Keep each interface's link-level counters, by interface name.

    The answer is keyed by display strings with one entry per address; the
    link-level entry (``network`` such as ``<Link#1>``) holds the counters.

    Args:
        payload: ``diagnostics/interface/get_interface_statistics``.

    Returns:
        dict[str, dict]: Bytes, packets, errors, drops, and collisions by
            interface name.

    Raises:
        ValueError: The answer has no statistics.
    """
    statistics = payload.get("statistics") if isinstance(payload, dict) else None
    if not isinstance(statistics, dict):
        raise ValueError("no statistics")
    found: dict[str, dict] = {}
    for entry in statistics.values():
        if not isinstance(entry, dict) or not str(entry.get("network") or "").startswith("<Link"):
            continue
        name = text(entry.get("name"), 40)
        if name:
            found[name] = {key.replace("-", "_"): _number(entry.get(key)) for key in (
                "received-bytes", "sent-bytes", "received-packets", "sent-packets", "received-errors",
                "send-errors", "dropped-packets", "collisions")}
    return found


def project_gateways(payload: Any) -> tuple[list[dict], bool]:
    """Keep each gateway's status as dpinger reports it.

    Args:
        payload: ``routes/gateway/status``.

    Returns:
        tuple[list[dict], bool]: ``name``, ``address``, ``status`` (such as
            ``none`` for up, ``down``, ``loss``, ``delay``), ``label`` (as
            the GUI shows it), ``monitored`` (None when not reported),
            ``delay_ms``, ``stddev_ms``,
            and ``loss_pct`` (None when not monitored: never zero) per
            gateway; and whether every row was read.

    Raises:
        ValueError: The answer has no list of gateways.
    """
    items = _shape(payload, "items", list)
    found, complete = [], len(items) <= MAX_ROWS
    for row in items[:MAX_ROWS]:
        name = text(row.get("name"), 64) if isinstance(row, dict) else None
        if not name:
            complete = False
            continue
        found.append({
            "name": name, "address": _address(row.get("address")), "status": (text(row.get("status"), 20) or "").lower() or None,
            "label": text(row.get("status_translated"), 40), "monitored": _monitored(row.get("monitor")),
            "delay_ms": _measure(row.get("delay")), "stddev_ms": _measure(row.get("stddev")),
            "loss_pct": _measure(row.get("loss")),
        })
    return found, complete


def project_routes(payload: Any) -> tuple[list[dict], bool]:
    """Keep the default routes: which gateway each address family uses now.

    Args:
        payload: ``diagnostics/interface/get_routes`` (a list).

    Returns:
        tuple[list[dict], bool]: ``family`` (``ipv4`` or ``ipv6``),
            ``gateway``, ``device``, and ``interface`` (its description)
            per default route; and whether the table was read in full.

    Raises:
        ValueError: The answer is not a list.
    """
    rows = _shape(payload, None, list)
    found = [{
        "family": text(row.get("proto"), 8), "gateway": _address(row.get("gateway")),
        "device": text(row.get("netif"), 40), "interface": text(row.get("intf_description"), 80),
    } for row in rows[:MAX_ROWS] if isinstance(row, dict) and row.get("destination") == "default"]
    return found, len(rows) <= MAX_ROWS


def project_dns(payload: Any) -> dict:
    """Keep how the Unbound resolver answers: on, where, and whom it forwards to.

    Everything else in the settings answer is dropped.

    Args:
        payload: ``unbound/settings/get``.

    Returns:
        dict: ``enabled``, ``port``, ``interfaces`` (empty is all),
            ``dnssec``, ``forwarding`` (to the system's servers), and
            ``forwarders`` (``server``, ``port``, ``domain``, ``tls``).

    Raises:
        ValueError: The answer has no Unbound settings.
    """
    unbound = _shape(payload, "unbound", dict)
    general = _shape(unbound, "general", dict)
    dots = unbound.get("dots") if isinstance(unbound.get("dots"), dict) else {}
    forwarders = [{
        "server": _address(row.get("server")), "port": _integer(row.get("port")),
        "domain": text(row.get("domain"), 120), "tls": _selected(row.get("type")) == ["dot"],
    } for row in _items(dots.get("dot", []))[:32] if _flag(row.get("enabled"))]
    return {
        "enabled": _flag(general.get("enabled")), "port": _integer(general.get("port")),
        "interfaces": _selected(general.get("active_interface")), "dnssec": _flag(general.get("dnssec")),
        "forwarding": _flag((unbound.get("forwarding") or {}).get("enabled")) if isinstance(unbound.get("forwarding"), dict) else False,
        "forwarders": [item for item in forwarders if item["server"]],
    }


def project_dnsmasq(payload: Any) -> dict:
    """Keep dnsmasq's DHCP ranges, reservations, and DNS options.

    Everything else in the settings answer is dropped.

    Args:
        payload: ``dnsmasq/settings/get``.

    Returns:
        dict: ``enabled``; ``ranges`` (``interface``, ``start``, ``end``,
            ``domain``); ``reservations`` (``hostname``, ``ip``, ``mac``);
            and ``dns`` (servers handed out, by interface, when set).

    Raises:
        ValueError: The answer has no dnsmasq settings.
    """
    dnsmasq = _shape(payload, "dnsmasq", dict)
    ranges = [{
        "interface": (_selected(row.get("interface")) or [None])[0], "start": text(row.get("start_addr"), 64),
        "end": text(row.get("end_addr"), 64), "domain": text(row.get("domain"), 120),
    } for row in _items(dnsmasq.get("dhcp_ranges", []))[:256]]
    reservations = [{
        "hostname": text(row.get("host"), 120),
        "ip": next((address for item in _selected(row.get("ip")) if (address := _address(item))), None),
        "mac": _mac((_selected(row.get("hwaddr")) or [None])[0]),
    } for row in _items(dnsmasq.get("hosts", []))[:MAX_ROWS]]
    dns: dict[str, list[str]] = {}
    for row in _items(dnsmasq.get("dhcp_options", []))[:512]:
        if _selected(row.get("option")) == ["6"]:
            for interface in _selected(row.get("interface")) or [""]:
                dns.setdefault(interface, []).extend(_addresses(row.get("value")))
    return {"enabled": _flag(dnsmasq.get("enable")), "ranges": ranges,
            "reservations": [item for item in reservations if item["mac"]], "dns": dns}


def project_kea_subnets(payload: Any) -> tuple[list[dict], bool]:
    """Keep each Kea subnet's range and the options it hands out.

    Args:
        payload: ``kea/dhcpv4/search_subnet``.

    Returns:
        tuple[list[dict], bool]: ``id``, ``subnet``, ``pools`` (such as
            ``192.0.2.10-192.0.2.20``), ``dns``, ``routers``, ``domain``,
            ``automatic`` (options taken from the interface), and
            ``description`` per subnet; and whether every row was read.

    Raises:
        ValueError: The answer has no rows.
    """
    rows, complete = _rows(payload)
    found = []
    for row in rows:
        try:
            subnet = str(ipaddress.ip_network(str(row.get("subnet")), strict=False))
        except ValueError:
            complete = False
            continue
        found.append({
            "id": text(row.get("uuid"), 40), "subnet": subnet,
            "pools": [part.strip()[:80] for part in str(row.get("pools") or "").replace(",", "\n").splitlines() if part.strip()][:16],
            "dns": _addresses(row.get("option_data.domain_name_servers")),
            "routers": _addresses(row.get("option_data.routers")),
            "domain": text(row.get("option_data.domain_name"), 120),
            "automatic": _flag(row.get("option_data_autocollect")), "description": text(row.get("description"), 120),
        })
    return found, complete


def project_kea_reservations(payload: Any) -> tuple[list[dict], bool]:
    """Keep each Kea reservation's address, MAC, and name.

    Args:
        payload: ``kea/dhcpv4/search_reservation``.

    Returns:
        tuple[list[dict], bool]: ``subnet`` (its ID), ``ip``, ``mac``, and
            ``hostname`` per reservation; and whether every row was read.

    Raises:
        ValueError: The answer has no rows.
    """
    rows, complete = _rows(payload)
    return [{"subnet": text(row.get("subnet"), 40), "ip": _address(row.get("ip_address")),
             "mac": _mac(row.get("hw_address")), "hostname": text(row.get("hostname"), 120)}
            for row in rows], complete


def project_leases(payload: Any) -> tuple[list[dict], bool]:
    """Keep each DHCP lease's client: MAC, address, name, and interface.

    dnsmasq and Kea answer with the same fields for these.

    Args:
        payload: ``dnsmasq/leases/search`` or ``kea/leases4/search``.

    Returns:
        tuple[list[dict], bool]: ``mac``, ``ip``, ``hostname``,
            ``interface`` (such as ``opt1``), ``device`` (such as ``em2``),
            ``expires`` (Unix time), and ``reserved`` per lease with a MAC
            and an address; and whether every row was read.

    Raises:
        ValueError: The answer has no rows.
    """
    rows, complete = _rows(payload)
    found = []
    for row in rows:
        mac, address = _mac(row.get("hwaddr")), _address(row.get("address"))
        if mac and address:
            hostname = text(row.get("hostname"), 120)
            found.append({"mac": mac, "ip": address, "hostname": None if hostname in ("*", None) else hostname,
                          "interface": text(row.get("if_name"), 40), "device": text(row.get("if"), 40),
                          "expires": _integer(row.get("expire")), "reserved": bool(row.get("is_reserved"))})
    return found, complete


def project_arp(payload: Any) -> tuple[list[dict], bool]:
    """Keep each ARP entry the firewall learned (its own permanent ones skipped).

    Args:
        payload: ``diagnostics/interface/search_arp``.

    Returns:
        tuple[list[dict], bool]: ``mac``, ``ip``, ``device``, ``hostname``
            (as the firewall resolved it), and ``manufacturer`` per entry
            with a MAC (incomplete entries have none); and whether every
            row was read.

    Raises:
        ValueError: The answer has no rows.
    """
    rows, complete = _rows(payload)
    return [{"mac": mac, "ip": address, "device": text(row.get("intf"), 40),
             "hostname": text(row.get("hostname"), 120), "manufacturer": text(row.get("manufacturer"), 80)}
            for row in rows if row.get("permanent") is not True and row.get("expired") is not True
            and (mac := _mac(row.get("mac"))) and (address := _address(row.get("ip")))], complete


def project_ndp(payload: Any) -> tuple[list[dict], bool]:
    """Keep each IPv6 neighbour the firewall knows.

    Args:
        payload: ``diagnostics/interface/search_ndp``.

    Returns:
        tuple[list[dict], bool]: ``mac``, ``ip`` (without its zone), and
            ``device`` per entry; and whether every row was read.

    Raises:
        ValueError: The answer has no rows.
    """
    rows, complete = _rows(payload)
    return [{"mac": mac, "ip": address, "device": text(row.get("intf"), 40), "manufacturer": text(row.get("manufacturer"), 80)}
            for row in rows if (mac := _mac(row.get("mac"))) and (address := _address(row.get("ip")))], complete


def _quick(value: Any) -> bool | None:
    """A rule's ``quick`` flag: True, False, or None when not reported.

    Args:
        value: ``"1"``, ``"0"``, a boolean, or None (old-format rules often
            leave it out; their default depends on the rule's kind).

    Returns:
        bool | None: The flag.
    """
    if value in (True, 1, "1"):
        return True
    if value in (False, 0, "0"):
        return False
    return None


def _endpoint(row: dict, side: str) -> dict:
    """A rule's source or destination: network or alias, port, and negation.

    Args:
        row: A rule row.
        side: ``source`` or ``destination``.

    Returns:
        dict: ``net`` (empty or ``any`` is anything), ``port``, and ``not``.
    """
    return {"net": text(row.get(f"{side}_net"), 300) or "any", "port": text(row.get(f"{side}_port"), 120),
            "not": _flag(row.get(f"{side}_not"))}


def project_rules(payload: Any) -> tuple[list[dict], bool]:
    """Keep each firewall rule's match and action, in evaluation order.

    OPNsense 26.7 returns every rule: new-format ones, automatic system
    rules (``is_automatic``), and rules still in the old format
    (``legacy``, with ``ref``, the page that manages them). Only these
    fields are kept; gateways, tags, schedules, and the rest are dropped.

    Args:
        payload: ``firewall/filter/search_rule``.

    Returns:
        tuple[list[dict], bool]: ``id``, ``order`` (``sort_order``),
            ``priority`` (``prio_group``), ``scope`` (``floating``,
            ``group``, ``interface``, or ``system``: the evaluation stage,
            from the order's prefix), ``interfaces`` (identifiers or group
            names; old-format rules name interfaces by description; empty
            is every interface), ``interface_label``,
            ``direction``, ``action``, ``quick`` (None when not reported),
            ``ip_version``, ``protocol``, ``source``, ``destination``,
            ``enabled``, ``log``, ``description``, ``legacy`` (the owner's
            rule still in the old format, waiting for migration),
            ``automatic`` (a system rule), and ``ref`` (the page that
            manages it) per rule, sorted by ``order``; and
            whether every row was read.

    Raises:
        ValueError: The answer has no rows.
    """
    rows, complete = _rows(payload)
    found = []
    for row in rows:
        order = text(row.get("sort_order"), 40)
        if not order:
            complete = False
            continue
        ref = text(str(row.get("ref") or "").split("?", 1)[0], 120) or ""
        found.append({
            "id": text(row.get("uuid"), 64), "order": order, "priority": text(row.get("prio_group"), 20),
            "interfaces": [part.strip()[:40] for part in str(row.get("interface") or "").split(",") if part.strip()][:32],
            "interface_label": text(row.get("%interface"), 200),
            "direction": (text(row.get("direction"), 8) or "").lower() or None,
            "action": (text(row.get("action"), 12) or "").lower() or None, "quick": _quick(row.get("quick")),
            "ip_version": text(row.get("ipprotocol"), 12), "protocol": text(row.get("protocol"), 20),
            "source": _endpoint(row, "source"), "destination": _endpoint(row, "destination"),
            "enabled": _flag(row.get("enabled")) and row.get("disabled") is not True, "log": _flag(row.get("log")),
            "description": text(row.get("description"), 255), "scope": RULE_SCOPES.get(order.split(".", 1)[0], "system"),
            # An old-format rule of the owner's waits for migration; OPNsense
            # marks it automatic too, but it is not a system rule.
            "legacy": ref.startswith(MIGRATION_REF),
            "automatic": row.get("is_automatic") is True and not ref.startswith(MIGRATION_REF), "ref": ref or None,
        })
    return sorted(found, key=lambda item: item["order"]), complete


def project_aliases(payload: Any) -> tuple[list[dict], bool]:
    """Keep each alias's name, type, and size; never its content or credentials.

    URL table aliases can hold a user name and password, and any alias's
    content can list internal addresses: neither is kept.

    Args:
        payload: ``firewall/alias/search_item``.

    Returns:
        tuple[list[dict], bool]: ``name``, ``type``, ``description``,
            ``enabled``, and ``items`` (``current_items``) per alias; and
            whether every row was read.

    Raises:
        ValueError: The answer has no rows.
    """
    rows, complete = _rows(payload)
    return [{"name": text(row.get("name"), 64), "type": text(row.get("type"), 40),
             "description": text(row.get("description"), 255), "enabled": _flag(row.get("enabled")),
             "items": _integer(row.get("current_items"))} for row in rows if text(row.get("name"), 64)], complete


def project_groups(payload: Any) -> tuple[list[dict], bool]:
    """Keep each interface group and its members.

    Args:
        payload: ``firewall/group/search_item``.

    Returns:
        tuple[list[dict], bool]: ``name``, ``members`` (interface
            identifiers), and ``description`` per group; and whether every
            row was read.

    Raises:
        ValueError: The answer has no rows.
    """
    rows, complete = _rows(payload)
    return [{"name": text(row.get("ifname"), 40),
             "members": [part[:40] for part in re.split(r"[,\s]+", str(row.get("members") or "")) if part][:64],
             "description": text(row.get("descr"), 255)} for row in rows if text(row.get("ifname"), 40)], complete


def project_services(payload: Any) -> tuple[list[dict], bool]:
    """Keep which services run.

    Args:
        payload: ``core/service/search``.

    Returns:
        tuple[list[dict], bool]: ``id``, ``name``, ``description``, and
            ``running`` per service; and whether every row was read.

    Raises:
        ValueError: The answer has no rows.
    """
    rows, complete = _rows(payload)
    return [{"id": text(row.get("id"), 80), "name": text(row.get("name"), 80),
             "description": text(row.get("description"), 160), "running": row.get("running") in (1, True, "1")}
            for row in rows if text(row.get("id"), 80)], complete


def project_carp(payload: Any) -> tuple[dict, bool]:
    """Keep each CARP address's state, and the node's CARP status.

    The answer lists every address that rides a VHID: the CARP virtual IP
    itself (``mode`` ``carp``) and any IP alias on that VHID (``ipalias``).
    A VHID's state is its CARP address's; an alias is not a node of its own.

    Args:
        payload: ``diagnostics/interface/get_vip_status``.

    Returns:
        tuple[dict, bool]: ``addresses`` (``interface``, ``vhid``,
            ``address``, ``mode``, and ``state`` such as ``master``,
            ``backup``, ``init``, or ``disabled``), ``demotion``,
            ``maintenance``, and ``message``; and whether every row was
            read (a cut or malformed list is partial).

    Raises:
        ValueError: The answer has no rows or no CARP status.
    """
    rows, complete = _rows(payload)
    carp = _shape(payload, "carp", dict)
    addresses = []
    for row in rows:
        vhid, mode = _integer(row.get("vhid")), (text(row.get("mode"), 20) or "").lower() or None
        if vhid is None or mode is None:
            complete = False
            continue
        addresses.append({"interface": text(row.get("interface"), 40), "vhid": vhid,
                          "address": text(row.get("subnet"), 64), "mode": mode,
                          "state": (text(row.get("status"), 20) or "").lower() or None})
    return ({"addresses": addresses, "demotion": _integer(carp.get("demotion")),
             "maintenance": carp.get("maintenancemode") is True, "message": text(carp.get("status_msg"), 160)},
            complete)


def project_blocklist_enabled(payload: Any) -> bool:
    """Whether Unbound's DNS blocklist is on.

    Args:
        payload: ``unbound/overview/is_block_list_enabled``.

    Returns:
        bool: Its ``enabled``.

    Raises:
        ValueError: The answer has no boolean ``enabled``.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
        raise ValueError("no blocklist state")
    return payload["enabled"]


def _monitored(value: Any) -> bool | None:
    """Whether dpinger monitors a gateway: ``~`` is explicitly off.

    Args:
        value: The reported monitor address, or ``~``.

    Returns:
        bool | None: True for an address, False for ``~``, None when
            missing or malformed (unknown, which is not "off").
    """
    if value == "~":
        return False
    return True if isinstance(value, str) and value.strip() else None


def _keepalive(value: Any) -> int | None:
    """A peer's persistent keepalive: ``off`` is explicitly 0.

    Args:
        value: The reported keepalive, such as ``"25"`` or ``"off"``.

    Returns:
        int | None: Seconds, 0 for ``off``, None when missing or malformed
            (unknown, which is not "off").
    """
    if value == "off":
        return 0
    found = _integer(value)
    return found if found is not None and found >= 0 else None


def _epoch(value: Any) -> int | None:
    """A Unix time that is set (WireGuard reports 0 for never).

    Args:
        value: The reported time.

    Returns:
        int | None: The time, or None when never or not a number.
    """
    found = _integer(value)
    return found if found else None


def project_wireguard(payload: Any) -> tuple[dict, bool]:
    """Keep WireGuard instances and peers: public keys, endpoints, handshakes, transfer.

    Args:
        payload: ``wireguard/service/show``.

    Returns:
        tuple[dict, bool]: ``instances`` (``name``, ``interface``,
            ``port``, ``public_key``, ``status``) and ``peers`` (``name``,
            ``instance``, ``interface`` (such as ``wg0``), ``public_key``, ``endpoint``, ``allowed_ips``,
            ``handshake`` (Unix time, None if never), ``rx_bytes``,
            ``tx_bytes``, ``keepalive`` (seconds; 0 when off, None when
            not reported), and
            ``status``); and whether every row was read.

    Raises:
        ValueError: The answer has no rows.
    """
    rows, complete = _rows(payload)
    instances, peers = [], []
    for row in rows:
        if row.get("type") == "interface":
            instances.append({"name": text(row.get("name"), 64), "interface": text(row.get("if"), 20),
                              "port": _integer(row.get("listen-port")), "public_key": text(row.get("public-key"), 64),
                              "status": text(row.get("status"), 20)})
        elif row.get("type") == "peer":
            endpoint = text(row.get("endpoint"), 120)
            peers.append({
                "name": text(row.get("name"), 64), "instance": text(row.get("ifname"), 64),
                "interface": text(row.get("if"), 20), "public_key": text(row.get("public-key"), 64), "endpoint": None if endpoint == "(none)" else endpoint,
                "allowed_ips": [part.strip()[:64] for part in str(row.get("allowed-ips") or "").split(",") if part.strip()][:32],
                "handshake": _epoch(row.get("latest-handshake")), "rx_bytes": _number(row.get("transfer-rx")),
                "tx_bytes": _number(row.get("transfer-tx")), "keepalive": _keepalive(row.get("persistent-keepalive")),
                "status": text(row.get("peer-status"), 20),
            })
        else:
            complete = False
    return {"instances": instances, "peers": peers}, complete


def project_wireguard_service(payload: Any) -> str | None:
    """Keep the WireGuard service's reported status.

    Args:
        payload: ``wireguard/service/status``.

    Returns:
        str | None: Such as ``running``, ``stopped``, ``disabled``, or ``unknown``.

    Raises:
        ValueError: The answer is not an object.
    """
    return text(_shape(payload, None, dict).get("status"), 20)


def project_blocklists(payload: Any) -> tuple[list[dict], bool]:
    """Keep each DNS blocklist's name and size, never its URLs or domains.

    Args:
        payload: ``unbound/settings/search_dnsbl``.

    Returns:
        tuple[list[dict], bool]: ``description``, ``enabled``, ``lists``
            (the predefined lists chosen, such as ``sb``), ``urls`` (how
            many of the owner's own list URLs: URLs can hold tokens),
            ``domains`` (how many blocked domains and wildcards), and
            ``allowed`` (how many allowed domains) per blocklist; and
            whether every row was read.

    Raises:
        ValueError: The answer has no rows.
    """
    def count(value: Any) -> int:
        return len([part for part in str(value or "").split(",") if part.strip()])

    rows, complete = _rows(payload)
    return [{"description": text(row.get("description"), 255), "enabled": _flag(row.get("enabled")),
             "lists": _selected(row.get("type")), "urls": count(row.get("lists")),
             "domains": count(row.get("blocklists")) + count(row.get("wildcards")),
             "allowed": count(row.get("allowlists"))} for row in rows], complete


def peer_key(peer: dict) -> str | None:
    """A WireGuard peer's identity: its interface and a digest of its public key.

    Names can be edited and repeated; the key cannot. Only the digest is
    ever shown.

    Args:
        peer: A projected peer (``interface``, ``instance``, ``public_key``).

    Returns:
        str | None: Such as ``wg0:1a2b3c4d5e6f7a8b``, or None without a key.
    """
    if not peer.get("public_key"):
        return None
    return f"{peer.get('interface') or peer.get('instance')}:{hashlib.sha256(peer['public_key'].encode()).hexdigest()[:16]}"


def watch_reading(reading: dict) -> dict:
    """This poll's watchlist reading: gateways, keepalive peers, and CARP virtual IPs.

    Args:
        reading: :meth:`OpnsenseAdapter.read`'s result.

    Returns:
        dict: ``sampled_at``, ``sections`` (``gateway``, ``wireguard``,
            ``carp``: whether each was read in full), and ``items`` for
            ``collector/watchlist.py``.
    """
    sections = reading["sections"]
    items = [{"key": f"gateway:{item['name']}", "kind": "gateway", "label": item["name"],
              "monitored": item.get("monitored"), "status": item.get("status")}
             for item in reading["gateways"] or []]
    for peer in (reading["wireguard"] or {}).get("peers") or []:
        key = peer_key(peer)
        if key:
            items.append({"key": f"wireguard:{key}", "kind": "wireguard", "label": peer.get("name"),
                          "keepalive": peer.get("keepalive"), "handshake": peer.get("handshake")})
    for address in (reading["carp"] or {}).get("addresses") or []:
        if address.get("mode") == "carp":
            items.append({"key": f"carp:{address.get('interface')}:{address.get('vhid')}", "kind": "carp",
                          "label": address.get("address"), "state": address.get("state")})
    return {"sampled_at": reading["sampled_at"], "items": items,
            "sections": {"gateway": sections["gateways"]["status"] == "complete",
                         "wireguard": sections["wireguard"]["status"] == "complete",
                         "carp": sections["carp"]["status"] == "complete"}}


def sightings(interfaces: list[dict] | None, arp: list[dict] | None, ndp: list[dict] | None,
              leases: list[dict]) -> tuple[list[dict], bool]:
    """Join ARP, NDP, and lease rows into one sighting per MAC per interface.

    A sighting is router evidence that a MAC used an address on a network
    recently, never a wired attachment. Its scope is the interface's
    identifier (such as ``opt1``), found through the interface list: a row
    whose interface is not in it (the list failed, was cut, or the row
    names an unknown interface) is skipped, and the join is then
    incomplete, so nothing ages on its account and no sighting is ever
    keyed by a bare device name. The same MAC on two interfaces is two
    sightings. ARP and NDP rows are ``seen`` at this poll; a lease alone
    is not. The firewall's own MACs and interfaces with gateways (WANs,
    whose neighbours are upstream equipment) are skipped.

    Args:
        interfaces: Projected interfaces, if read.
        arp: Projected ARP entries, if read.
        ndp: Projected NDP entries, if read.
        leases: Projected leases from every DHCP server read, each with
            ``server``.

    Returns:
        tuple[list[dict], bool]: ``scope``, ``network`` (the interface's
            description), ``mac``, ``ipv4``, ``ipv6``, ``hostname``,
            ``manufacturer``, ``seen``, ``lease`` (``server``, ``ip``,
            ``expires``, ``reserved``, or None), and ``feeds`` (the
            sections that reported it, such as ``arp`` and
            ``dnsmasq_leases``) per sighting; and whether every row found
            its interface.
    """
    by_device = {item["device"]: item for item in interfaces or [] if item.get("identifier")}
    by_identifier = {item["identifier"]: item for item in interfaces or [] if item.get("identifier")}
    own = {item["mac"] for item in interfaces or [] if item.get("mac")}
    found: dict[tuple[str, str], dict] = {}
    mapped = True

    def entry(device: str | None, identifier: str | None, mac: str) -> dict | None:
        nonlocal mapped
        interface = by_identifier.get(identifier or "") or by_device.get(device or "")
        if interface is None:
            mapped = False
            return None
        if mac in own or interface.get("gateways"):
            return None
        return found.setdefault((interface["identifier"], mac), {
            "scope": interface["identifier"], "network": interface.get("description"), "mac": mac, "ipv4": [],
            "ipv6": [], "hostname": None, "manufacturer": None, "seen": False, "lease": None, "feeds": [],
        })

    for row in arp or []:
        if (item := entry(row["device"], None, row["mac"])) is not None:
            item["seen"] = True
            item["feeds"].append("arp")
            item["ipv4"].append(row["ip"])
            item["hostname"] = item["hostname"] or row.get("hostname")
            item["manufacturer"] = item["manufacturer"] or row.get("manufacturer")
    for row in ndp or []:
        if (item := entry(row["device"], None, row["mac"])) is not None:
            item["seen"] = True
            item["feeds"].append("ndp")
            item["ipv6"].append(row["ip"])
            item["manufacturer"] = item["manufacturer"] or row.get("manufacturer")
    for row in leases:
        if (item := entry(row.get("device"), row.get("interface"), row["mac"])) is not None:
            item["lease"] = {key: row.get(key) for key in ("server", "ip", "expires", "reserved")}
            item["feeds"].append(f"{row['server']}_leases")
            item["hostname"] = row.get("hostname") or item["hostname"]
    for item in found.values():
        item["feeds"] = sorted(set(item["feeds"]))
        item["ipv4"] = sorted(set(item["ipv4"]), key=ipaddress.ip_address)
        # Global addresses before link-local ones.
        item["ipv6"] = sorted(set(item["ipv6"]), key=lambda value: (ipaddress.ip_address(value).is_link_local, value))
    return sorted(found.values(), key=lambda item: (item["scope"], item["mac"])), mapped and interfaces is not None


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

class OpnsenseAdapter:
    """Read-only OPNsense firewall: its device, NICs, and health."""

    def __init__(self, session: requests.Session | None = None) -> None:
        """Create the adapter.

        Args:
            session: Optional HTTP session for tests or reuse.
        """
        self.session = session
        #: Readings from the last poll, by observation and telemetry key; the
        #: runner stores them with ``record_telemetry`` (see ``poll``).
        self.last_telemetry: dict[str, dict[str, Any]] = {}
        #: Client sightings from the last poll; the runner reconciles them
        #: into the derived clients source (``collector/sightings.py``).
        self.last_sightings: dict[str, Any] | None = None

    def client(self, settings: Mapping[str, Any]) -> OpnsenseClient:
        """The read-only client for one firewall.

        Args:
            settings: The integration's private settings.

        Returns:
            OpnsenseClient: Bound to those settings.
        """
        return OpnsenseClient(settings, self.session)

    @staticmethod
    def _section(sections: dict, name: str, read: Callable[[], Any], sampled_at: str) -> Any:
        """Read one part of a poll and record how it ended.

        Args:
            sections: Section outcomes, filled in place.
            name: The section's name.
            read: Reads and projects it; may return ``(value, complete)``.
            sampled_at: This poll's time.

        Returns:
            Any: The projected value, or None when it was not read.

        Raises:
            requests.HTTPError: A 401: the key itself was refused.
        """
        try:
            value = read()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 401:
                raise
            outcome = "not_available" if status in (403, 404) else "failed"
            sections[name] = {"status": outcome, "sampled_at": None, "error": f"HTTP {status}"}
            return None
        except (requests.RequestException, ValueError) as exc:
            sections[name] = {"status": "failed", "sampled_at": None, "error": type(exc).__name__}
            return None
        complete = True
        if isinstance(value, tuple):
            value, complete = value
        sections[name] = {"status": "complete" if complete else "partial", "sampled_at": sampled_at}
        return value

    def read(self, settings: Mapping[str, Any]) -> dict:
        """Read the firewall's system, interfaces, health, networks, and neighbours.

        Args:
            settings: The integration's private settings.

        Returns:
            dict: ``system`` (required); ``interfaces``, ``statistics``,
                ``time``, ``memory``, ``temperatures``, ``disks``, ``swap``,
                ``gateways``, ``routes``, ``dns``, ``dnsmasq``,
                ``kea_subnets``, ``kea_reservations``, ``arp``, ``ndp``,
                ``dnsmasq_leases``, ``kea_leases``, ``rules``, ``aliases``,
                ``groups``, ``services``, ``carp``, ``blocklist_enabled``,
                ``blocklists``, ``wireguard``, and ``wireguard_service``
                (each None when not read); ``sections``; and ``sampled_at``.

        Raises:
            requests.RequestException: The firewall could not be reached, or
                refused the key (401).
            ValueError: ``system_information`` was malformed or settings
                are invalid.
        """
        client = self.client(settings)
        sampled_at = datetime.now(timezone.utc).isoformat()
        system = project_system(client.get("diagnostics/system/system_information"))
        sections: dict[str, dict] = {"system": {"status": "complete", "sampled_at": sampled_at}}
        reads = {
            "interfaces": lambda: project_interfaces(client.get("interfaces/overview/interfaces_info")),
            "statistics": lambda: project_statistics(client.get("diagnostics/interface/get_interface_statistics")),
            "time": lambda: project_time(client.get("diagnostics/system/system_time")),
            "memory": lambda: project_memory(client.get("diagnostics/system/system_resources")),
            "temperatures": lambda: project_temperatures(client.get("diagnostics/system/system_temperature")),
            "disks": lambda: project_disks(client.get("diagnostics/system/system_disk")),
            "swap": lambda: project_swap(client.get("diagnostics/system/system_swap")),
            "gateways": lambda: project_gateways(client.get("routes/gateway/status")),
            "routes": lambda: project_routes(client.get("diagnostics/interface/get_routes")),
            "dns": lambda: project_dns(client.get("unbound/settings/get")),
            "dnsmasq": lambda: project_dnsmasq(client.get("dnsmasq/settings/get")),
            "kea_subnets": lambda: project_kea_subnets(client.get("kea/dhcpv4/search_subnet")),
            "kea_reservations": lambda: project_kea_reservations(client.get("kea/dhcpv4/search_reservation")),
            "arp": lambda: project_arp(client.get("diagnostics/interface/search_arp")),
            "ndp": lambda: project_ndp(client.get("diagnostics/interface/search_ndp")),
            "dnsmasq_leases": lambda: project_leases(client.get("dnsmasq/leases/search")),
            "kea_leases": lambda: project_leases(client.get("kea/leases4/search")),
            "rules": lambda: project_rules(client.get("firewall/filter/search_rule")),
            "aliases": lambda: project_aliases(client.get("firewall/alias/search_item")),
            "groups": lambda: project_groups(client.get("firewall/group/search_item")),
            "services": lambda: project_services(client.get("core/service/search")),
            "carp": lambda: project_carp(client.get("diagnostics/interface/get_vip_status")),
            "blocklist_enabled": lambda: project_blocklist_enabled(client.get("unbound/overview/is_block_list_enabled")),
            "blocklists": lambda: project_blocklists(client.get("unbound/settings/search_dnsbl")),
            "wireguard": lambda: project_wireguard(client.get("wireguard/service/show")),
            "wireguard_service": lambda: project_wireguard_service(client.get("wireguard/service/status")),
        }
        found = {name: self._section(sections, name, read, sampled_at) for name, read in reads.items()}
        return {"system": system, **found, "sections": sections, "sampled_at": sampled_at}

    def poll(self, settings: Mapping[str, Any]) -> DiscoveryBatch:
        """Report the firewall and its physical NICs.

        Ports are complete only when the interface list was read in full,
        so a failed or capped read never removes a NIC. Health goes to
        ``last_telemetry`` as the ``opnsense`` key, with the identity
        evidence (hostname and hardware MACs) as ``identity_evidence`` when
        the interface list is complete.

        Args:
            settings: The integration's private settings.

        Returns:
            DiscoveryBatch: The ``firewall`` observation and its ports.
        """
        self.last_telemetry, self.last_sightings = {}, None
        reading = self.read(settings)
        system, interfaces = reading["system"], reading["interfaces"]
        complete = reading["sections"]["interfaces"]["status"] == "complete"
        host = urlsplit(controller_url(settings)).hostname or ""
        identifiers: dict[str, Any] = {"hostname": system["hostname"]}
        try:
            identifiers["management_ips"] = [str(ipaddress.ip_address(host))]
        except ValueError:
            pass
        physical = [item for item in interfaces or [] if item["physical"]]
        macs = sorted({item["mac"] for item in physical if item["mac"]})
        if macs:
            identifiers["macs"] = macs
        ports = tuple(PortObservation(
            device_remote_id=FIREWALL, remote_id=item["device"], name=item["device"], mac=item["mac"],
            speed_mbps=item["speed"], link_up={"up": True, "down": False}.get(item["status"] or ""),
        ) for item in physical)
        device = DeviceObservation(
            remote_id=FIREWALL, name=system["hostname"], category="firewall", identifiers=identifiers,
            details={"platform": "opnsense", "product": system["product"], "ports_complete": complete},
        )
        sections = reading["sections"]
        telemetry: dict[str, Any] = {"opnsense": {
            **{key: reading[key] for key in HEALTH_SECTIONS + ("sampled_at", "system")},
            "sections": {name: sections[name] for name in ("system", *HEALTH_SECTIONS)},
        }, "opnsense_network": {
            **{key: reading[key] for key in NETWORK_SECTIONS + ("sampled_at",)},
            # The client reads' outcomes too (their rows become sightings).
            "sections": {name: sections[name] for name in NETWORK_SECTIONS + SIGHTING_SECTIONS},
        }}
        telemetry["watchlist"] = watch_reading(reading)
        if complete and macs:
            telemetry["identity_evidence"] = {"hostname": system["hostname"], "macs": macs,
                                              "sampled_at": reading["sampled_at"]}
        self.last_telemetry = {FIREWALL: telemetry}
        self.last_sightings = self._sightings(reading)
        return DiscoveryBatch(devices=(device,), ports=ports)

    @staticmethod
    def _sightings(reading: dict) -> dict:
        """This poll's client sightings and which feeds finished.

        Args:
            reading: :meth:`read`'s result.

        Returns:
            dict: ``records`` (see :func:`sightings`), ``sampled_at``, and
                ``finished``: the feeds (``arp``, ``ndp``,
                ``dnsmasq_leases``, ``kea_leases``) whose answer is the
                whole picture, so a sighting they no longer report is no
                longer current. A feed finishes when it was read in full,
                or, for a DHCP server, when it answered 404 (not
                installed: it has no leases). A refused (403), failed, or
                capped feed has not finished, so nothing it reported ages;
                and none finishes when the interface list was incomplete
                or a row named an unknown interface.
        """
        sections = reading["sections"]
        leases = [{**row, "server": name.removesuffix("_leases")}
                  for name in ("dnsmasq_leases", "kea_leases") for row in reading[name] or []]
        records, mapped = sightings(reading["interfaces"], reading["arp"], reading["ndp"], leases)
        finished = []
        if mapped and sections["interfaces"]["status"] == "complete":
            finished = [name for name in SIGHTING_SECTIONS if sections[name]["status"] == "complete"
                        or (name.endswith("_leases") and sections[name].get("error") == "HTTP 404")]
        return {"records": records, "sampled_at": reading["sampled_at"], "finished": finished}

    #: Telemetry that keeps history across polls, by remote ID; the runner
    #: marks it interrupted when a poll fails or is held.
    INTERRUPTED = {FIREWALL: ("watchlist",)}

    @staticmethod
    def interrupt_telemetry(key: str, stored: Any) -> Any:
        """A stored history after a failed or held poll.

        Args:
            key: The telemetry key (``watchlist``).
            stored: Its stored value.

        Returns:
            Any: The watchlist marked interrupted; anything else unchanged.
        """
        return watchlist.interrupt(stored) if key == "watchlist" else stored

    @staticmethod
    def merge_telemetry(key: str, previous: Any, current: Any) -> Any:
        """Keep the last good reading of every section this poll could not read.

        The ``watchlist`` key is this poll's watch reading, which advances
        the stored watchlist instead (``collector/watchlist.py``).

        A section that failed or is no longer available keeps its previous
        value, and its ``sampled_at`` stays that value's time; its
        ``status`` and ``error`` say what went wrong now.

        Args:
            key: The telemetry key being stored.
            previous: Its stored value, if any.
            current: This poll's value.

        Returns:
            Any: ``current``, with unread sections' values and timestamps
                carried over from ``previous``.
        """
        if key == "watchlist" and isinstance(current, dict) and "items" in current and "sections" in current:
            return watchlist.advance(previous, current)
        if key not in ("opnsense", "opnsense_network") or not isinstance(previous, dict) or not isinstance(current, dict):
            return current
        merged = {**current, "sections": dict(current.get("sections") or {})}
        for name, outcome in merged["sections"].items():
            before = (previous.get("sections") or {}).get(name) or {}
            unread = outcome.get("status") in ("failed", "not_available")
            if unread and before.get("sampled_at") and previous.get(name) is not None:
                merged[name] = previous[name]
                merged["sections"][name] = {**outcome, "sampled_at": before["sampled_at"]}
        return merged

    def test_connection(self, settings: Mapping[str, Any]) -> tuple[bool, str]:
        """Check the address and key with the same client a poll uses, for Settings.

        Args:
            settings: Submitted settings (never stored by this call).

        Returns:
            tuple[bool, str]: Success and a safe message.
        """
        try:
            client = self.client(settings)
            client.get("core/system/status")
            system = project_system(client.get("diagnostics/system/system_information"))
        except requests.exceptions.SSLError:
            return False, ("Certificate verification failed. OPNsense's own certificate is self-signed: "
                           "add its CA or adjust certificate checking.")
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 401:
                return False, "OPNsense refused the API key and secret."
            if status == 403:
                return False, "The API key lacks the Dashboard or System: Status privilege."
            return False, "OPNsense returned an unexpected HTTP response. Check the address."
        except RequestRefused:
            return False, "OPNsense answered with a redirect. Use its HTTPS address and port."
        except requests.exceptions.RequestException:
            return False, "OPNsense could not be reached. Check its address, and that LabRat may reach its web interface."
        except (ValueError, OSError):
            return False, "The address did not answer like OPNsense, or a field is missing."
        product = system["product"] or "OPNsense"
        note = "" if product.startswith("OPNsense 26.7") else " This release is untested; LabRat is tested on 26.7."
        return True, f"Connection successful. {product} answered as {system['hostname']}.{note}"
