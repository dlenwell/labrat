# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Read physical-link facts from the UniFi Network "classic" device list.

The Network Integration API (``<network root>/integration/v1``) lists adopted
devices and their ports, but not cable media, uplinks, or LLDP neighbors. The
older device list (``<network root>/api/s/<site>/stat/device``) has all three
and accepts the same API key. This module turns that read-only response into
plain values; the UniFi adapter decides how to use them.

Only facts the controller reports are used:

* ``medium_for_port`` reads a port's ``media`` and SFP module fields.
* ``uplinks`` reads each device's own uplink record (port, upstream device
  MAC, upstream port), which the controller reports for wired uplinks.
* ``lldp_neighbors`` reads each device's LLDP table. UniFi exposes only the
  neighbor's chassis ID, port ID, and management IPs (no system name), so
  the vendor is inferred from a vendor table (``profiles/lldp_vendors.json``
  plus a connection's ``lldp_vendors`` entries): network-OS port naming or a
  MAC prefix. Neighbors that cannot be identified as network equipment keep
  no vendor rather than a guessed one.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache, partial
from importlib.resources import files
import json
import re
from typing import Any

from labrat.coerce import number

#: A MAC prefix (OUI) as the vendor table writes it: three lowercase octets.
_OUI = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){2}$")

#: Longest vendor name a table entry may give; it becomes part of a device name.
_MAX_VENDOR = 40


@dataclass(frozen=True)
class VendorRule:
    """One way to recognize a network equipment vendor from LLDP identifiers.

    Attributes:
        vendor: Vendor name used to label the neighbor device.
        port_pattern: Compiled pattern the whole announced port ID must
            match, or None when only MAC prefixes identify the vendor.
        ouis: Lowercase colon-separated MAC prefixes of the chassis ID.
    """

    vendor: str
    port_pattern: re.Pattern[str] | None
    ouis: frozenset[str]


def _vendor_rule(entry: object, origin: str) -> VendorRule:
    """Validate one vendor table entry.

    Args:
        entry: A table with ``vendor`` and at least one of ``port_pattern``
            or ``ouis``.
        origin: Where the entry came from, for error messages.

    Returns:
        VendorRule: The compiled rule.

    Raises:
        ValueError: The entry is not a table, names no vendor, has an invalid
            pattern or MAC prefix, or has nothing to match.
    """
    if not isinstance(entry, Mapping):
        raise ValueError(f"{origin}: each LLDP vendor entry must be a table")
    vendor = entry.get("vendor")
    if not isinstance(vendor, str) or not vendor.strip() or len(vendor.strip()) > _MAX_VENDOR:
        raise ValueError(f"{origin}: LLDP vendor entries need a vendor name of 1 to {_MAX_VENDOR} characters")
    raw_pattern = entry.get("port_pattern")
    pattern = None
    if raw_pattern is not None:
        if not isinstance(raw_pattern, str) or not raw_pattern.strip():
            raise ValueError(f"{origin}: port_pattern for {vendor.strip()} must be a nonempty string")
        try:
            pattern = re.compile(raw_pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"{origin}: port_pattern for {vendor.strip()} is not a valid pattern ({exc})") from None
    raw_ouis = entry.get("ouis", [])
    if not isinstance(raw_ouis, list):
        raise ValueError(f"{origin}: ouis for {vendor.strip()} must be a list")
    ouis = set()
    for value in raw_ouis:
        normalized = value.strip().lower().replace("-", ":") if isinstance(value, str) else ""
        if not _OUI.match(normalized):
            raise ValueError(f"{origin}: ouis for {vendor.strip()} must look like 00:0c:42")
        ouis.add(normalized)
    if pattern is None and not ouis:
        raise ValueError(f"{origin}: LLDP vendor {vendor.strip()} needs a port_pattern or ouis")
    return VendorRule(vendor=vendor.strip(), port_pattern=pattern, ouis=frozenset(ouis))


@lru_cache(maxsize=1)
def packaged_vendor_rules() -> tuple[VendorRule, ...]:
    """Load the packaged LLDP vendor table.

    Returns:
        tuple[VendorRule, ...]: Rules from ``profiles/lldp_vendors.json``.
    """
    resource = files("labrat.collector").joinpath("profiles/lldp_vendors.json")
    table = json.loads(resource.read_text(encoding="utf-8"))
    return tuple(_vendor_rule(entry, "lldp_vendors.json") for entry in table.get("vendors", []))


def vendor_rules(extra: object = None) -> tuple[VendorRule, ...]:
    """Combine a connection's own vendor entries with the packaged table.

    Args:
        extra: The connection's ``lldp_vendors`` setting: a list of tables,
            or None. Its entries are checked before the packaged ones.

    Returns:
        tuple[VendorRule, ...]: Rules in the order they are tried.

    Raises:
        ValueError: The setting is not a list, or an entry is invalid.
    """
    if extra is None:
        return packaged_vendor_rules()
    if not isinstance(extra, list):
        raise ValueError("lldp_vendors must be a list of tables")
    return tuple(_vendor_rule(entry, "lldp_vendors") for entry in extra) + packaged_vendor_rules()

#: Port ``media`` values UniFi uses for copper RJ45 ports.
_COPPER_MEDIA = {"FE", "GE", "2P5GE", "2.5GE", "5GE", "10GE"}

#: Fragments of SFP part numbers that identify direct-attach copper cables.
_DAC_PARTS = ("DAC", "-CU", "CU1M", "CU2M", "CU3M", "CU5M", "JD095", "JD096", "JD097", "J9281", "J9283")


@dataclass(frozen=True)
class Uplink:
    """One device's wired uplink as the controller reports it.

    Attributes:
        device_mac: MAC of the device whose uplink this is.
        local_port: That device's port index carrying the uplink.
        upstream_mac: MAC of the device it plugs into.
        upstream_port: Port index on the upstream device.
        speed_mbps: Negotiated speed, when reported.
    """

    device_mac: str
    local_port: str
    upstream_mac: str
    upstream_port: str
    speed_mbps: int | None


@dataclass(frozen=True)
class LldpNeighbor:
    """A non-UniFi device a UniFi port hears over LLDP.

    Attributes:
        device_mac: MAC of the UniFi device that heard it.
        local_port: That device's port index.
        chassis_id: The neighbor's announced chassis ID (lowercased).
        port_id: The neighbor's announced port ID, as sent.
        management_ips: IP addresses announced by the neighbor.
        vendor: Vendor from the LLDP vendor table; None means
            the neighbor can be matched to another discovery source.
    """

    device_mac: str
    local_port: str
    chassis_id: str
    port_id: str
    management_ips: tuple[str, ...]
    vendor: str | None


def _mac(value: Any) -> str | None:
    """Return a lowercased MAC-like string, or None for anything else.

    Args:
        value: A controller field that should hold a MAC.

    Returns:
        str | None: The value trimmed and lowercased, or None when blank or
            not text.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower()


#: Port ``media`` values for SFP cages, by the connector type LabRat stores.
_CAGE_MEDIA = {"SFP": "sfp", "SFP+": "sfp_plus", "SFP28": "sfp28"}


def connector_for_port(port: Mapping[str, Any]) -> str | None:
    """Read a port's connector from its classic ``media`` value.

    The controller reports ``media`` for every model, so this needs no
    per-model port layout.

    Args:
        port: One port from a device's ``port_table``.

    Returns:
        str | None: ``rj45`` for copper media, ``sfp``, ``sfp_plus``, or
            ``sfp28`` for a cage, or None for anything else.
    """
    media = str(port.get("media") or "").strip().upper()
    if media in _COPPER_MEDIA:
        return "rj45"
    return _CAGE_MEDIA.get(media)


def medium_for_port(port: Mapping[str, Any]) -> str | None:
    """Classify the cable medium on one classic ``port_table`` entry.

    Args:
        port: One port from a device's ``port_table``.

    Returns:
        str | None: ``copper``, ``dac``, ``fiber_multimode``,
            ``fiber_singlemode``, ``fiber``, or None when unknown (for
            example an empty SFP cage or an unrecognized module).
    """
    media = str(port.get("media") or "").upper()
    if media in _COPPER_MEDIA:
        return "copper"
    if not media.startswith("SFP") and not port.get("sfp_found"):
        return None
    if not port.get("sfp_found"):
        return None  # An empty cage says nothing about a cable.
    compliance = str(port.get("sfp_compliance") or "").upper()
    part = str(port.get("sfp_part") or "").upper()
    if "DAC" in compliance or "CX" in compliance or any(fragment in part for fragment in _DAC_PARTS):
        return "dac"
    if "BASE-T" in compliance or "RJ45" in compliance or "RJ45" in part:
        return "copper"
    if re.search(r"\b(SR|SX|SR4|MM)\b|BASE-S", compliance) or "-MM-" in part or "MMF" in part:
        return "fiber_multimode"
    # Also catches run-together forms such as "1000LX" or "10GLR".
    if (re.search(r"\b(LR|LX|ER|ZR|BX|LH|SM)\b|BASE-L|\d(LX|LR|ZR|ER)\b", compliance)
            or "LH-SMD" in part or "SMF" in part):
        return "fiber_singlemode"
    if compliance in {"", "UNKNOWN"}:
        return None
    return "fiber"


def cable_assembly_media(modules: Mapping[tuple[str, str], Mapping[str, Any]]) -> dict[tuple[str, str], str]:
    """Classify modules that are the two ends of one cable assembly.

    A direct-attach cable (or an active optical cable) is one part: both of
    its ends report the same module serial. So two ports on different
    devices showing the same serial are joined by that cable, even when the
    module names no type (``sfp_compliance`` "Unknown" and a part number
    ``medium_for_port`` does not recognize). It is a DAC, or ``fiber`` when
    the part or compliance names an AOC.

    Args:
        modules: Classic ``port_table`` entries the per-port classifier could
            not identify, by ``(device remote ID, port index)``.

    Returns:
        dict[tuple[str, str], str]: Medium for each port proven an end of a
            cable assembly; other ports are left out.
    """
    by_serial: dict[str, list[tuple[tuple[str, str], Mapping[str, Any]]]] = {}
    for key, port in modules.items():
        serial = str(port.get("sfp_serial") or "").strip()
        if port.get("sfp_found") and len(serial) >= 4:
            by_serial.setdefault(serial, []).append((key, port))
    media: dict[tuple[str, str], str] = {}
    for ends in by_serial.values():
        if len(ends) != 2 or ends[0][0][0] == ends[1][0][0]:
            continue
        names = " ".join(str(port.get(field) or "") for _, port in ends
                         for field in ("sfp_part", "sfp_compliance")).upper()
        medium = "fiber" if "AOC" in names else "dac"
        for key, _ in ends:
            media[key] = medium
    return media


def uplinks(devices: Iterable[Mapping[str, Any]]) -> list[Uplink]:
    """List wired uplinks reported by the controller for each device.

    Args:
        devices: Records from ``stat/device``.

    Returns:
        list[Uplink]: One entry per device with a complete wired uplink.
    """
    found = []
    for device in devices:
        record = device.get("uplink")
        if not isinstance(record, Mapping) or record.get("type") not in (None, "wire"):
            continue
        device_mac = _mac(device.get("mac"))
        upstream_mac = _mac(record.get("uplink_mac"))
        local_port = record.get("port_idx")
        upstream_port = record.get("uplink_remote_port")
        if not device_mac or not upstream_mac or local_port is None or upstream_port is None:
            continue
        speed = record.get("speed")
        found.append(Uplink(
            device_mac=device_mac,
            local_port=str(local_port),
            upstream_mac=upstream_mac,
            upstream_port=str(upstream_port),
            speed_mbps=speed if type(speed) is int and speed > 0 else None,
        ))
    return found


def neighbor_vendor(chassis_id: str, port_id: str, rules: tuple[VendorRule, ...] | None = None) -> str | None:
    """Infer a neighbor's vendor from its LLDP identifiers.

    Port naming is tried first across all rules, then MAC prefixes, so a
    distinctive port name wins over a prefix shared through resale.

    Args:
        chassis_id: Announced chassis ID (often a MAC).
        port_id: Announced port ID.
        rules: Vendor rules to try, in order; the packaged table by default.

    Returns:
        str | None: The first matching rule's vendor, otherwise None.
    """
    rules = packaged_vendor_rules() if rules is None else rules
    port = port_id.strip()
    for rule in rules:
        if rule.port_pattern is not None and rule.port_pattern.fullmatch(port):
            return rule.vendor
    prefix = chassis_id[:8].lower()
    for rule in rules:
        if prefix in rule.ouis:
            return rule.vendor
    return None


def lldp_neighbors(
    devices: Iterable[Mapping[str, Any]],
    unifi_macs: set[str],
    rules: tuple[VendorRule, ...] | None = None,
) -> tuple[list[LldpNeighbor], int]:
    """List non-UniFi LLDP neighbors heard by UniFi devices.

    Args:
        devices: Records from ``stat/device``.
        unifi_macs: MACs of adopted UniFi devices; neighbors with these
            chassis IDs are already covered by uplink records.
        rules: Vendor rules for naming neighbors; the packaged table by
            default.

    Returns:
        tuple[list[LldpNeighbor], int]: Valid neighbor records, including
            unrecognized hosts that may match another discovery source, and
            the count without an inferred equipment vendor.
    """
    found: list[LldpNeighbor] = []
    skipped = 0
    for device in devices:
        device_mac = _mac(device.get("mac"))
        table = device.get("lldp_table")
        if not device_mac or not isinstance(table, list):
            continue
        for entry in table:
            if not isinstance(entry, Mapping):
                continue
            chassis = _mac(entry.get("chassis_id"))
            port_id = entry.get("port_id")
            local_port = entry.get("local_port_idx")
            if not chassis or not isinstance(port_id, str) or local_port is None:
                continue
            if chassis in unifi_macs or _mac(port_id) in unifi_macs:
                continue
            vendor = neighbor_vendor(chassis, port_id, rules) if port_id.strip() else None
            if vendor is None:
                skipped += 1
            raw_ips = entry.get("mgmt_ips")
            management_ips = tuple(dict.fromkeys(
                value.strip() for value in raw_ips
                if isinstance(value, str) and value.strip()
            )) if isinstance(raw_ips, list) else ()
            found.append(LldpNeighbor(
                device_mac=device_mac, local_port=str(local_port),
                chassis_id=chassis, port_id=port_id.strip(),
                management_ips=management_ips, vendor=vendor,
            ))
    return found, skipped


#: Classic device types for UniFi gateways (consoles, USG, and UXG).
_GATEWAY_TYPES = frozenset({"udm", "ugw", "uxg"})

#: Classic WAN interface keys on a gateway record, in priority order.
_WAN_KEYS = ("wan1", "wan2", "wan3", "wan4")


def has_wan_interfaces(record: Mapping[str, Any]) -> bool:
    """Tell whether a classic device record describes a gateway.

    A device with WAN interfaces routes, even when it also has switch ports
    (a Dream Machine, for example). The classic list reports WAN interfaces
    as ``wan1``, ``wan2`` ... tables and gateway types as ``udm``/``ugw``/``uxg``.

    Args:
        record: One classic ``stat/device`` record.

    Returns:
        bool: True for a router or gateway.
    """
    kind = record.get("type")
    if isinstance(kind, str) and kind.lower() in _GATEWAY_TYPES:
        return True
    return any(isinstance(record.get(key), Mapping) for key in _WAN_KEYS)


# Classic counters and rates are never negative (labrat.coerce).
_number = partial(number, minimum=0)


def _rate_bps(table: Mapping[str, Any], key: str) -> float | None:
    """Convert a controller byte rate (bytes per second) to bits per second.

    Args:
        table: A controller record.
        key: The field holding a byte rate.

    Returns:
        float | None: The rate in bits per second, or None when missing.
    """
    value = _number(table.get(key))
    return value * 8 if value is not None else None


def wan_snapshot(record: Mapping[str, Any], health: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Describe a gateway's internet uplinks from read-only controller data.

    Args:
        record: The gateway's classic ``stat/device`` record.
        health: The site's ``stat/health`` subsystems (``wan`` and ``www``).

    Returns:
        dict[str, Any] | None: ``internet`` (status, ISP, latency, drops,
            uptime, last speed test) and ``uplinks`` (one per WAN interface:
            name, enabled, up, active, link speed, latency, availability,
            receive and transmit rates in bit/s, and addresses). None when the
            record has no WAN interfaces.
    """
    subsystems = {item.get("subsystem"): item for item in health if isinstance(item, Mapping)}
    wan = subsystems.get("wan", {})
    www = subsystems.get("www", {})
    monitor_stats = record.get("uptime_stats") if isinstance(record.get("uptime_stats"), Mapping) else {}
    uplinks = []
    for index, key in enumerate(_WAN_KEYS):
        table = record.get(key)
        if not isinstance(table, Mapping):
            continue
        # Uptime and monitor availability are keyed WAN, WAN2, WAN3 ...
        monitor = monitor_stats.get("WAN" if index == 0 else f"WAN{index + 1}")
        monitor = monitor if isinstance(monitor, Mapping) else {}
        name = table.get("name")
        ipv6 = table.get("ipv6")
        # UniFi's geographic results retain one provider per WAN circuit,
        # including standby links. Never lend the active ISP to another WAN.
        provider = table.get("isp_name") or table.get("isp_organization")
        geo_key = "WAN" if index == 0 else f"WAN{index + 1}"
        for field in ("active_geo_info", "geo_info", "last_geo_info"):
            geo = record.get(field)
            info = geo.get(geo_key) if isinstance(geo, Mapping) else None
            if not provider and isinstance(info, Mapping):
                provider = info.get("isp_name") or info.get("isp_organization")
        if not provider and table.get("is_uplink") is True:
            provider = wan.get("isp_name") or wan.get("isp_organization")
        uplinks.append({
            "id": key,
            "name": key.upper(),
            "isp": provider if isinstance(provider, str) and provider else None,
            "interface": name if isinstance(name, str) and name else None,
            "enabled": table.get("enable") is not False,
            "up": table.get("up") is True,
            "active": table.get("is_uplink") is True,
            "media": table.get("media") if isinstance(table.get("media"), str) else None,
            "port": table.get("port_idx") if isinstance(table.get("port_idx"), int) else None,
            "speed_mbps": _number(table.get("speed")),
            "max_speed_mbps": _number(table.get("max_speed")),
            "latency_ms": _number(table.get("latency")),
            "availability": _number(table.get("availability")),
            "uptime_s": _number(monitor.get("uptime")),
            "rx_bps": _rate_bps(table, "rx_bytes-r"),
            "tx_bps": _rate_bps(table, "tx_bytes-r"),
            "ip": table.get("ip") if isinstance(table.get("ip"), str) and table.get("ip") else None,
            "ipv6": [item for item in ipv6 if isinstance(item, str)] if isinstance(ipv6, list) else [],
        })
    if not uplinks:
        return None
    # A speed test that never ran reports zeros; keep it out rather than show 0 Mb/s.
    ran_at = _number(www.get("speedtest_lastrun"))
    speedtest = None
    if ran_at and ran_at > 0:
        speedtest = {
            "down_mbps": _number(www.get("xput_down")),
            "up_mbps": _number(www.get("xput_up")),
            "ping_ms": _number(www.get("speedtest_ping")),
            "ran_at": int(ran_at),
        }
    isp = wan.get("isp_name") or wan.get("isp_organization")
    return {
        "internet": {
            "status": www.get("status") if isinstance(www.get("status"), str) else None,
            "isp": isp if isinstance(isp, str) and isp else None,
            "latency_ms": _number(www.get("latency")),
            "drops": _number(www.get("drops")),
            "uptime_s": _number(www.get("uptime")),
            "speedtest": speedtest,
        },
        "uplinks": uplinks,
    }
