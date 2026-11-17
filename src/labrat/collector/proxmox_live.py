# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Pull fast-changing Proxmox state between full scans.

The full Proxmox telemetry (``proxmox_cluster.py``) runs every five
minutes and costs several requests per node. Between scans, this module
reads the cheap whole-cluster lists on a short interval: one
``/cluster/resources`` request carries every node's and guest's status,
CPU, memory, and uptime, and one ``/cluster/tasks`` request the recent
tasks. A node whose uptime shows it restarted also has its services and
package versions read again at once, so a pending reboot or update clears
within one pull rather than at the next scan.

Everything here is a read-only GET with the source's existing token; the
owner chose pulling over Proxmox pushing to LabRat (metric servers or
webhooks). Readings merge into the stored ``proxmox_node``,
``proxmox_clients``, and ``proxmox_cluster`` telemetry with a
``live_at`` time; nothing else in a record changes, and a failed pull is
only logged, keeping what was stored.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import time
from typing import Any, Mapping
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from labrat.coerce import json_object
from labrat.collector.proxmox_cluster import (
    _integer, MAX_NODES, _number, _Reader, _services, _tasks, _text, _versions,
)
from labrat.collector.sources import effective_sources
from labrat.discovery.models import DiscoveredDevice, DiscoverySource

_LOGGER = logging.getLogger(__name__)

#: Default and allowed seconds between live pulls of one Proxmox source.
DEFAULT_LIVE_INTERVAL_SECONDS = 20
MIN_LIVE_INTERVAL_SECONDS = 10
MAX_LIVE_INTERVAL_SECONDS = 300
#: Most guests merged from one pull, matching the guest collection bound.
MAX_GUESTS = 512
#: Slack, in seconds, before a shorter uptime counts as a reboot.
REBOOT_SLACK_SECONDS = 120


def live_interval(settings: Mapping[str, Any]) -> int:
    """Read a Proxmox source's live pull interval; 0 turns it off.

    Args:
        settings: The source's private settings (``live_interval_seconds``).

    Returns:
        int: Seconds between pulls, or 0 when disabled.

    Raises:
        ValueError: The value is not 0 or a whole number in range.
    """
    value = settings.get("live_interval_seconds", DEFAULT_LIVE_INTERVAL_SECONDS)
    if type(value) is not int or not (value == 0 or MIN_LIVE_INTERVAL_SECONDS <= value <= MAX_LIVE_INTERVAL_SECONDS):
        raise ValueError(
            f"Proxmox live_interval_seconds must be 0 or an integer from "
            f"{MIN_LIVE_INTERVAL_SECONDS} to {MAX_LIVE_INTERVAL_SECONDS}")
    return value


def live_poll_sources(sessions: sessionmaker, config: Mapping[str, Any]) -> list[tuple[int, Mapping[str, Any], int]]:
    """List enabled Proxmox sources that want live pulls between scans.

    Args:
        sessions: Factory for short-lived database transactions.
        config: Parsed local TOML configuration (never log this mapping).

    Returns:
        list[tuple[int, Mapping[str, Any], int]]: Source ID, settings, and
            interval for each source with live pulls on.
    """
    sources_config = effective_sources(config)
    with sessions() as db:
        sources = list(db.scalars(select(DiscoverySource).where(
            DiscoverySource.enabled.is_(True), DiscoverySource.kind == "proxmox",
        )))
    found = []
    for source in sources:
        settings = sources_config.get("proxmox", {}).get(source.config_name)
        if not isinstance(settings, dict):
            continue
        try:
            interval = live_interval(settings)
        except ValueError:
            _LOGGER.warning("Invalid live_interval_seconds for %s; live Proxmox pulls are off", source.name)
            continue
        if interval:
            found.append((source.id, settings, interval))
    return found


def live_snapshot(adapter: Any, settings: dict) -> dict:
    """Read every node's and guest's current state and the recent tasks.

    Args:
        adapter: The Proxmox adapter whose HTTP client is reused.
        settings: The source's private connection settings.

    Returns:
        dict: ``nodes`` (name → status, cpu, maxcpu, mem, maxmem, uptime),
            ``guests`` (vmid → node, status, cpu, mem_used, uptime), and
            ``tasks`` (the same summary the full scan stores, or None when
            that read failed), and ``reader`` (for follow-up reads within
            the same deadline).

    Raises:
        ValueError: The resource list is unreadable.
        requests.RequestException: The controller cannot be contacted.
    """
    reader = _Reader(adapter, settings)
    nodes: dict[str, dict] = {}
    guests: dict[int, dict] = {}
    for row in reader.rows("/cluster/resources", MAX_NODES + MAX_GUESTS + 1024):
        kind = row.get("type")
        if kind == "node" and _text(row.get("node"), 160):
            nodes[_text(row.get("node"), 160)] = {
                "status": _text(row.get("status"), 20),
                "cpu": _number(row.get("cpu")), "maxcpu": _integer(row.get("maxcpu")),
                "mem": _integer(row.get("mem")), "maxmem": _integer(row.get("maxmem")),
                "uptime": _integer(row.get("uptime")),
            }
        elif kind in ("qemu", "lxc") and type(row.get("vmid")) is int and len(guests) < MAX_GUESTS:
            status = row.get("status")
            cpu = _number(row.get("cpu"))
            guests[row["vmid"]] = {
                "node": _text(row.get("node"), 160),
                "status": status if status in {"running", "stopped", "paused"} else "unknown",
                "cpu": round(cpu, 4) if cpu is not None and cpu >= 0 else None,
                "mem_used": _integer(row.get("mem")), "uptime": _integer(row.get("uptime")),
            }
    try:
        tasks = _tasks(reader.rows("/cluster/tasks", 2000), time.time())
    except Exception as exc:  # Tasks are optional; node and guest state still count.
        _LOGGER.info("Proxmox live tasks unavailable (%s)", type(exc).__name__)
        tasks = None
    return {"nodes": nodes, "guests": guests, "tasks": tasks, "reader": reader}


def _rebooted(previous: Mapping[str, Any], uptime: int | None, now: datetime) -> bool:
    """Whether a node restarted since its stored reading.

    Args:
        previous: The stored ``proxmox_node`` record.
        uptime: The node's uptime now, in seconds.
        now: The time of this reading.

    Returns:
        bool: True when its uptime is shorter than the stored uptime plus
            the time since, by more than :data:`REBOOT_SLACK_SECONDS`.
    """
    before = _integer(previous.get("uptime"))
    stamp = previous.get("live_at") or previous.get("sampled_at")
    if uptime is None or before is None or not isinstance(stamp, str):
        return False
    try:
        elapsed = (now - datetime.fromisoformat(stamp)).total_seconds()
    except ValueError:
        return False
    return uptime + REBOOT_SLACK_SECONDS < before + max(0.0, elapsed)


def poll_proxmox_live_once(sessions: sessionmaker, source_id: int, settings: Mapping[str, Any], adapter: Any) -> int:
    """Pull one source's live node, guest, and task state and store it.

    Live pulls do not change the source's attempt or success times (those
    describe full scans), and a failure is only logged: the next pull or
    full scan tries again.

    Args:
        sessions: Factory for short-lived database transactions.
        source_id: Proxmox discovery source to pull.
        settings: That source's private settings.
        adapter: The Proxmox adapter whose HTTP client is reused.

    Returns:
        int: Number of node observations updated.
    """
    with sessions() as db:
        source = db.get(DiscoverySource, source_id)
        if source is None or not source.enabled:
            return 0
    try:
        snapshot = live_snapshot(adapter, dict(settings))
    except Exception as exc:
        # Never log raw exception text: it may contain URLs or tokens.
        _LOGGER.warning("Proxmox live pull failed (%s)", type(exc).__name__)
        return 0
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    reader = snapshot["reader"]
    updated = 0
    with sessions.begin() as db:
        hosts = [host for host in db.scalars(select(DiscoveredDevice).where(DiscoveredDevice.source_id == source_id))
                 if json_object(host.details_json).get("platform") == "proxmox"]
        for host in hosts:
            details = json_object(host.details_json)
            reading = snapshot["nodes"].get(host.remote_id)
            previous = details.get("proxmox_node") if isinstance(details.get("proxmox_node"), dict) else None
            if reading is not None and previous is not None:
                node = {**previous, **reading, "live_at": stamp}
                if reading["status"] == "online" and _rebooted(previous, reading["uptime"], now):
                    node.update(_maintenance(reader, host.remote_id, previous))
                details["proxmox_node"] = node
                updated += 1
            clients = details.get("proxmox_clients")
            if isinstance(clients, dict) and isinstance(clients.get("guests"), list):
                merged = []
                for guest in clients["guests"]:
                    live = snapshot["guests"].get(guest.get("vmid")) if isinstance(guest, dict) else None
                    # A guest that moved node waits for the full scan to move it.
                    if live is not None and live["node"] == host.remote_id:
                        guest = {**guest, **{key: value for key, value in live.items() if key != "node"}}
                    merged.append(guest)
                details["proxmox_clients"] = {**clients, "guests": merged, "live_at": stamp}
            shared = details.get("proxmox_cluster")
            if snapshot["tasks"] is not None and isinstance(shared, dict):
                details["proxmox_cluster"] = {**shared, "tasks": snapshot["tasks"], "live_at": stamp}
            host.details_json = json.dumps(details)
    return updated


def _maintenance(reader: Any, name: str, previous: Mapping[str, Any]) -> dict:
    """Read a rebooted node's services and package versions again.

    Args:
        reader: The pull's bounded reader.
        name: The node's name.
        previous: Its stored record, whose values a failed read keeps.

    Returns:
        dict: ``services`` and ``versions``, fresh where the reads worked.
    """
    encoded = quote(name, safe="")
    found = {}
    for key, read in (
        ("services", lambda: _services(reader.rows(f"/nodes/{encoded}/services", 128))),
        ("versions", lambda: _versions(reader.rows(f"/nodes/{encoded}/apt/versions", 1024))),
    ):
        try:
            found[key] = read()
        except Exception as exc:  # The next full scan reads them again.
            _LOGGER.info("Proxmox live %s unavailable (%s)", key, type(exc).__name__)
            found[key] = previous.get(key)
    return found
