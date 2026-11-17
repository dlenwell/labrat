# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Summarize discovery source health for page templates.

The header status strip and the home page both show one indicator per
configured discovery source. This module reads only the local database; it
never contacts a controller, so pages stay fast when an integration is down.

Used by: app, pages.activity.
"""

from datetime import datetime, timedelta, timezone
import ipaddress
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from labrat.discovery.models import DiscoveredDevice, DiscoveredLink, DiscoveredPort, DiscoverySource

#: A successful collection newer than this is shown as live (pulsing light).
FRESH_WINDOW = timedelta(minutes=30)

#: Sources the collector derives from another integration's endpoint, mapped
#: to the parent's kind; they match the parent by ``config_name``.
DERIVED_SOURCE_PARENTS = {"unifi_clients": "unifi"}

#: Short names for derived sources when listed inside their parent's tooltip.
DERIVED_SOURCE_SHORT = {"unifi_clients": "Clients"}

#: Worst-first ranking used when several entries share one header light.
_STATE_RANK = {"error": 3, "warning": 2, "ready": 1, "waiting": 0}


def _as_utc(moment: datetime) -> datetime:
    """Treat naive database timestamps as UTC.

    Args:
        moment: Timestamp read from SQLite, which may drop the zone.

    Returns:
        datetime: The same instant with an explicit UTC zone.
    """
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def describe_age(moment: datetime | None, now: datetime) -> str:
    """Describe how long ago something happened in a few words.

    Args:
        moment: When the event happened, or ``None`` if it never has.
        now: Current time, passed in so callers and tests agree on it.

    Returns:
        str: Short phrase such as ``"just now"``, ``"4 min ago"``,
            ``"3 h ago"``, ``"2 d ago"``, or ``"never"``.
    """
    if moment is None:
        return "never"
    seconds = max(0, int((now - _as_utc(moment)).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def source_health(db: Session, now: datetime | None = None) -> list[dict[str, Any]]:
    """Return display-ready health for every discovery source, by name.

    States: ``error`` when the last attempt recorded an error, ``ready``
    after at least one successful collection, otherwise ``waiting``. A ready
    source is ``fresh`` when its last success is within :data:`FRESH_WINDOW`.
    Error text is not included, because it may contain controller details.

    Args:
        db: Open database session.
        now: Current time; defaults to the real clock in UTC.

    Returns:
        list[dict[str, Any]]: One mapping per source with ``name``, ``state``,
            ``label``, ``fresh``, and ``age`` keys.
    """
    now = now or datetime.now(timezone.utc)
    health = []
    sources = list(db.scalars(select(DiscoverySource).order_by(DiscoverySource.name)))
    # A derived source (such as UniFi clients) reads the same controller as
    # its parent integration; the header folds it into that parent's light.
    parents = {
        (source.kind, source.config_name): source.name
        for source in sources if source.kind in DERIVED_SOURCE_PARENTS.values()
    }
    for source in sources:
        if source.last_error:
            state, label = "error", "Needs attention"
        elif source.last_success_at:
            state, label = "ready", "Collected successfully"
        else:
            state, label = "waiting", "Awaiting first run"
        fresh = bool(
            state == "ready"
            and now - _as_utc(source.last_success_at) <= FRESH_WINDOW
        )
        parent_kind = DERIVED_SOURCE_PARENTS.get(source.kind)
        health.append({
            "name": source.name,
            "state": state,
            "label": label,
            "fresh": fresh,
            "age": describe_age(source.last_success_at, now),
            "parent": parents.get((parent_kind, source.config_name)) if parent_kind else None,
            "short": DERIVED_SOURCE_SHORT.get(source.kind),
        })
    unifi_sources = [source for source in sources if source.kind == "unifi"]
    if unifi_sources:
        unifi_ids = {source.id for source in unifi_sources}
        neighbor_count = 0
        proxmox_sources = [source for source in sources if source.kind == "proxmox"]
        proxmox_ids = {source.id for source in proxmox_sources}
        node_addresses: dict[str, set[int]] = {}
        for device in db.scalars(select(DiscoveredDevice).where(
            DiscoveredDevice.source_id.in_(proxmox_ids or {-1}),
            DiscoveredDevice.category == "server",
        )):
            try:
                identifiers = json.loads(device.identifiers_json)
            except (TypeError, json.JSONDecodeError):
                continue
            values = identifiers.get("management_ips") if isinstance(identifiers, dict) else None
            if not isinstance(values, list):
                continue
            for value in values:
                if not isinstance(value, str):
                    continue
                try:
                    address = str(ipaddress.ip_interface(value).ip)
                except ValueError:
                    try:
                        address = str(ipaddress.ip_address(value))
                    except ValueError:
                        continue
                node_addresses.setdefault(address, set()).add(device.id)
        # lldpd announces one of the host's NIC MACs as its chassis ID.
        node_macs: dict[str, set[int]] = {}
        for device_id, mac in db.execute(
            select(DiscoveredPort.discovered_device_id, DiscoveredPort.mac)
            .join(DiscoveredDevice, DiscoveredPort.discovered_device_id == DiscoveredDevice.id)
            .where(DiscoveredDevice.source_id.in_(proxmox_ids or {-1}),
                   DiscoveredDevice.category == "server",
                   DiscoveredPort.mac.is_not(None))
        ):
            node_macs.setdefault(mac.strip().lower(), set()).add(device_id)
        matched_hosts: set[int] = set()
        for device in db.scalars(select(DiscoveredDevice).where(
            DiscoveredDevice.source_id.in_(unifi_ids),
        )):
            try:
                details = json.loads(device.details_json)
            except (TypeError, json.JSONDecodeError):
                continue
            neighbors = details.get("lldp_neighbors") if isinstance(details, dict) else None
            if isinstance(neighbors, list):
                neighbor_count += len(neighbors)
                for neighbor in neighbors:
                    if not isinstance(neighbor, dict):
                        continue
                    chassis = neighbor.get("chassis_id")
                    owners = node_macs.get(chassis.strip().lower(), set()) if isinstance(chassis, str) else set()
                    if len(owners) == 1:
                        matched_hosts.update(owners)
                    values = neighbor.get("management_ips")
                    if not isinstance(values, list):
                        continue
                    for value in values:
                        if not isinstance(value, str):
                            continue
                        try:
                            address = str(ipaddress.ip_interface(value).ip)
                        except ValueError:
                            try:
                                address = str(ipaddress.ip_address(value))
                            except ValueError:
                                continue
                        matched_hosts.update(node_addresses.get(address, set()))
        link_count = db.scalar(select(func.count(DiscoveredLink.id)).where(
            DiscoveredLink.source_id.in_(unifi_ids),
            DiscoveredLink.evidence.in_(("lldp-proxmox", "lldp-proxmox-name", "lldp-user-match")),
        )) or 0
        latest_success = max(
            (source.last_success_at for source in unifi_sources if source.last_success_at),
            default=None,
        )
        any_success = latest_success is not None
        has_error = any(source.last_error for source in unifi_sources)
        lldp_state = "error" if has_error else "ready" if any_success else "waiting"
        if not any_success:
            lldp_label = "Awaiting UniFi discovery"
        elif neighbor_count:
            neighbor_label = "neighbor" if neighbor_count == 1 else "neighbors"
            host_label = "host" if len(matched_hosts) == 1 else "hosts"
            link_label = "link" if link_count == 1 else "links"
            lldp_label = (
                f"{neighbor_count} {neighbor_label} · {len(matched_hosts)} Proxmox {host_label} · "
                f"{link_count} Proxmox physical {link_label}"
            )
        else:
            lldp_label = "No LLDP neighbors observed"
        lldp_age = describe_age(latest_success, now)
        health.append({
            "name": "LLDP",
            "state": lldp_state,
            "label": lldp_label,
            "fresh": bool(any_success and now - _as_utc(latest_success) <= FRESH_WINDOW),
            "age": lldp_age,
        })
    health.extend(wan_health(db, now))
    return health


def _rate_label(bits_per_second: object) -> str:
    """Format a bit rate compactly, such as ``3.2 Mb/s`` or ``940 kb/s``.

    Args:
        bits_per_second: The rate, or anything else when unknown.

    Returns:
        str: The rate with its unit, or ``--`` when it is not a number.
    """
    if not isinstance(bits_per_second, (int, float)) or isinstance(bits_per_second, bool):
        return "--"
    for unit, size in (("Gb/s", 1e9), ("Mb/s", 1e6), ("kb/s", 1e3)):
        if bits_per_second >= size:
            value = bits_per_second / size
            return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"
    return f"{bits_per_second:.0f} b/s"


def header_sources(health: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Condense source health into the header strip's one-line lights.

    WAN entries come first. Derived sources that read the same endpoint as
    an integration (such as UniFi clients) fold into that integration's
    light, which takes the worst state among them; a waiting child never
    dims a parent that has collected. Each light is labelled by the part of
    its name before the first colon (``unifi:default`` becomes ``unifi``),
    unless two lights would then share a label. The full name, state,
    and age of every folded entry go in the light's tooltip lines.

    Args:
        health: Entries from :func:`source_health`, in its order.

    Returns:
        list[dict[str, Any]]: One mapping per light with ``name`` (short
            label), ``state``, ``fresh``, ``detail`` (visible text or
            ``None``), and ``lines`` (tooltip lines) keys.
    """
    def line(entry: dict[str, Any], name: str) -> str:
        """Describe one entry for a tooltip line."""
        text = f"{name}: {entry['label']}"
        return text if entry.get("detail") else f"{text} · {entry['age']}"

    lights: dict[str, dict[str, Any]] = {}
    children = []
    for entry in health:
        if entry.get("parent"):
            children.append(entry)
            continue
        lights[entry["name"]] = {
            "name": entry["name"].split(":", 1)[0].strip() or entry["name"],
            "full_name": entry["name"],
            "state": entry["state"],
            "fresh": entry["fresh"],
            "detail": entry["label"] if entry.get("detail") else None,
            "lines": [line(entry, entry["name"])],
        }
    for entry in children:
        light = lights.get(entry["parent"])
        name = entry.get("short") or entry["name"]
        if light is None:
            # The parent is missing (disabled or deleted): stand alone.
            lights[entry["name"]] = {
                "name": name, "full_name": entry["name"], "state": entry["state"],
                "fresh": entry["fresh"], "detail": None, "lines": [line(entry, name)],
            }
            continue
        if entry["state"] != "waiting" and _STATE_RANK[entry["state"]] > _STATE_RANK[light["state"]]:
            light["state"] = entry["state"]
            light["fresh"] = False
        light["lines"].append(line(entry, name))
    ordered = sorted(lights.values(), key=lambda light: light["full_name"] != "WAN")
    counts: dict[str, int] = {}
    for light in ordered:
        counts[light["name"]] = counts.get(light["name"], 0) + 1
    for light in ordered:
        if counts[light["name"]] > 1:
            light["name"] = light["full_name"]
    return ordered


def wan_health(db: Session, now: datetime | None = None) -> list[dict[str, Any]]:
    """Summarize each gateway's internet uplinks for the header strip.

    States: ``error`` (red) when every enabled uplink is down, ``warning``
    (amber) when one is down or traffic runs on a backup uplink, otherwise
    ``ready``. A reading newer than two minutes is fresh.

    Args:
        db: Open database session.
        now: Current time; defaults to the real clock in UTC.

    Returns:
        list[dict[str, Any]]: One header entry named ``WAN`` per gateway.
    """
    now = now or datetime.now(timezone.utc)
    entries = []
    for device in db.scalars(select(DiscoveredDevice).where(DiscoveredDevice.category == "router")):
        try:
            details = json.loads(device.details_json or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        snapshot = details.get("wan") if isinstance(details, dict) else None
        if not isinstance(snapshot, dict):
            continue
        uplinks = [item for item in snapshot.get("uplinks", []) if isinstance(item, dict) and item.get("enabled")]
        up = [item for item in uplinks if item.get("up")]
        active = next((item for item in uplinks if item.get("active")), None)
        if uplinks and not up:
            state, label = "error", "No internet: every uplink is down"
        else:
            on_backup = bool(active and uplinks and active is not uplinks[0])
            state = "warning" if len(up) < len(uplinks) or on_backup else "ready"
            current = active or (up[0] if up else None)
            label = (f"{current.get('name', 'WAN')} · ↓ {_rate_label(current.get('rx_bps'))} "
                     f"↑ {_rate_label(current.get('tx_bps'))}") if current else "No uplink reported"
            if current and isinstance(current.get("latency_ms"), (int, float)):
                label += f" · {current['latency_ms']:.0f} ms"
            if state == "warning":
                label += " · on backup" if on_backup else " · an uplink is down"
        sampled = snapshot.get("sampled_at")
        try:
            sampled_at = datetime.fromisoformat(sampled) if isinstance(sampled, str) else None
        except ValueError:
            sampled_at = None
        entries.append({
            "name": "WAN",
            "state": state,
            "label": label,
            "fresh": bool(sampled_at and now - _as_utc(sampled_at) <= timedelta(minutes=2)),
            "age": describe_age(sampled_at, now),
            "detail": True,
        })
    return entries
