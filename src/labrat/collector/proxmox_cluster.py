# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Read bounded Proxmox cluster telemetry for the Cluster page.

Everything here is a read-only ``GET`` that an audit-only token can make
(``Sys.Audit``, ``Datastore.Audit``): node CPU and memory, a day of averaged
CPU and memory history, storage use, ZFS pool health, and Ceph health and
OSDs. Each part is read independently, so a missing permission or an absent
Ceph cluster leaves that part marked unavailable without failing the rest or
the physical discovery scan.

Results are curated, whitelisted values stored as telemetry on each Proxmox
node observation:

* ``proxmox_node``: that node's own readings, history, storage, and pools.
* ``proxmox_cluster``: cluster-wide facts (name and quorum, storage
  configuration, Ceph health and OSDs), copied to every node so the page can
  read them from whichever node was sampled most recently.

Raw API responses are never stored.

Used by: collector.proxmox_live, collector.runner.
"""

from datetime import datetime, timezone
from functools import partial
import json
import logging
import re
import time
from typing import Any, Callable
from urllib.parse import quote

from sqlalchemy import select

from labrat.coerce import integer, json_object, number, text
from labrat.collector.http import controller_url, optional_read, required_string, tls_verification
from labrat.discovery.models import DiscoveredDevice

_LOGGER = logging.getLogger(__name__)

#: Seconds between cluster telemetry reads per source.
REFRESH_SECONDS = 300
#: Seconds one whole read may take before later parts are skipped.
DEADLINE_SECONDS = 90
#: History bucket width: a day of 15-minute averages is 96 points.
HISTORY_BUCKET_SECONDS = 900

MAX_NODES = 64
MAX_STORAGE_ROWS = 512
MAX_POOLS = 32
MAX_VDEVS = 128
MAX_OSDS = 512
MAX_CHECKS = 32
MAX_TEXT = 300

#: Seconds between disk, SMART, PCI, and certificate reads per node: reading
#: SMART wakes every disk, so it runs hourly rather than with every refresh,
#: and again as soon as the node has rebooted.
HARDWARE_REFRESH_SECONDS = 3600
#: Seconds one node's SMART reads may take in total.
SMART_BUDGET_SECONDS = 20
#: Seconds between guest agent reads per VM, the total time one refresh may
#: spend on them, and how long a VM whose agent did not answer is skipped.
AGENT_REFRESH_SECONDS = 900
AGENT_BUDGET_SECONDS = 30
AGENT_RETRY_SECONDS = 3600
#: Guest history: 30-minute averages of a day, read every 15 minutes per
#: running guest within its own time budget.
GUEST_HISTORY_BUCKET_SECONDS = 1800
GUEST_HISTORY_REFRESH_SECONDS = 900
GUEST_HISTORY_BUDGET_SECONDS = 20
MAX_GUEST_ADDRESSES = 16
MAX_GUEST_FILESYSTEMS = 16
#: Interfaces and filesystems inside a guest that say nothing about it:
#: loopback, container and Kubernetes networks, and VPN or overlay tunnels.
_SKIP_INTERFACES = (
    "lo", "docker", "veth", "br-", "cni", "flannel", "cali", "cilium", "weave", "virbr", "lxcbr", "lxdbr",
    "incusbr", "podman", "kube", "vxlan", "tailscale", "wg", "zt", "tun", "tap", "nebula",
)
_SKIP_FILESYSTEMS = {"tmpfs", "devtmpfs", "overlay", "squashfs", "proc", "sysfs", "devfs", "autofs", "iso9660", "UDF"}
MAX_DISKS = 64
MAX_PCI = 256

#: Storage plugin types that mean Ceph is in use.
CEPH_STORAGE_TYPES = {"rbd", "cephfs"}


# Proxmox sends some numbers as strings; whole numbers round (labrat.coerce).
_number = partial(number, strings=True)
_integer = partial(integer, strings=True)


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    """Read trimmed Proxmox text, longer by default than other sources'.

    Args:
        value: A decoded JSON value.
        limit: Most characters kept.

    Returns:
        str | None: The text, or None when empty or not a string
            (``labrat.coerce.text``).
    """
    return text(value, limit)


def _flag(value: Any) -> bool | None:
    """Read a Proxmox 0/1 flag.

    Args:
        value: ``0``, ``1``, a boolean, or a numeric string.

    Returns:
        bool | None: The flag, or None when missing.
    """
    if isinstance(value, bool):
        return value
    number = _integer(value)
    return None if number is None else number != 0


class _Reader:
    """Issue bounded GETs against one Proxmox API with a shared deadline."""

    def __init__(self, adapter: Any, settings: dict, clock: Callable[[], float] = time.monotonic) -> None:
        """Prepare the base URL, token header, and TLS setting.

        Args:
            adapter: The Proxmox adapter whose HTTP client is reused.
            settings: The source's private connection settings.
            clock: Monotonic clock; tests may supply their own.
        """
        self.client = adapter.client
        self.base = controller_url(settings) + "/api2/json"
        self.headers = {
            "Accept": "application/json",
            "Authorization": "PVEAPIToken=" + required_string(settings, "token_id")
            + "=" + required_string(settings, "token_secret"),
        }
        self.verify = tls_verification(settings)
        self.clock = clock
        self.deadline = clock() + DEADLINE_SECONDS

    def get(self, path: str) -> Any:
        """Return the ``data`` member of one API response.

        Args:
            path: Path under ``/api2/json``, already URL-encoded.

        Returns:
            Any: The decoded ``data`` value.

        Raises:
            TimeoutError: The shared deadline has passed.
            ValueError: The response has no ``data`` member.
            requests.RequestException: The request failed.
        """
        if self.clock() > self.deadline:
            raise TimeoutError("Proxmox cluster read deadline passed")
        payload = self.client.get_json(self.base + path, self.headers, self.verify)
        if not isinstance(payload, dict) or "data" not in payload:
            raise ValueError("Unexpected Proxmox response schema")
        return payload["data"]

    def rows(self, path: str, limit: int) -> list[dict]:
        """Return a bounded list of objects from one API path.

        Args:
            path: Path under ``/api2/json``.
            limit: Most rows accepted.

        Returns:
            list[dict]: The object rows.

        Raises:
            ValueError: The response is not a list of objects or is too long.
        """
        data = self.get(path)
        if not isinstance(data, list) or len(data) > limit:
            raise ValueError("Unexpected or oversized Proxmox collection")
        return [row for row in data if isinstance(row, dict)]


#: RRD fields kept in history, as (stored name, how to read a row).
#: ``cpu`` and ``iowait`` are 0..1 fractions of the CPUs, ``mem`` of memory;
#: ``net_in`` and ``net_out`` are bytes per second; ``psi_*`` are the
#: "some" pressure stall percentages for CPU, IO, and memory.
_HISTORY_FIELDS = (
    ("cpu", lambda row: _number(row.get("cpu"))),
    ("mem", lambda row: (lambda used, total: used / total if used is not None and total else None)(
        _number(row.get("memused")), _number(row.get("memtotal")))),
    ("iowait", lambda row: _number(row.get("iowait"))),
    ("net_in", lambda row: _number(row.get("netin"))),
    ("net_out", lambda row: _number(row.get("netout"))),
    ("psi_cpu", lambda row: _number(row.get("pressurecpusome"))),
    ("psi_io", lambda row: _number(row.get("pressureiosome"))),
    ("psi_mem", lambda row: _number(row.get("pressurememorysome"))),
)


def _history(rows: list[dict]) -> list[dict]:
    """Average RRD rows into 15-minute buckets of the :data:`_HISTORY_FIELDS`.

    Args:
        rows: ``/nodes/{node}/rrddata`` rows.

    Returns:
        list[dict]: Points in time order with ``t`` and each field that had
            a reading in that bucket (missing fields are left out).
    """
    buckets: dict[int, dict[str, list[float]]] = {}
    for row in rows:
        stamp = _integer(row.get("time"))
        if stamp is None:
            continue
        bucket = buckets.setdefault(stamp - stamp % HISTORY_BUCKET_SECONDS, {})
        for name, read in _HISTORY_FIELDS:
            value = read(row)
            if value is not None:
                bucket.setdefault(name, []).append(value)
    points = []
    for stamp in sorted(buckets):
        values = buckets[stamp]
        if not values:
            continue
        point: dict[str, Any] = {"t": stamp}
        for name, _ in _HISTORY_FIELDS:
            if values.get(name):
                average = sum(values[name]) / len(values[name])
                point[name] = round(average, 4) if name in ("cpu", "mem", "iowait") else round(average, 2)
        points.append(point)
    return points[-int(86400 / HISTORY_BUCKET_SECONDS):]


def _tasks(rows: list[dict], now: float, hours: int = 24) -> dict:
    """Summarize the cluster's recent tasks by outcome and type.

    Args:
        rows: ``/cluster/tasks`` rows.
        now: Current Unix time.
        hours: How far back to count.

    Returns:
        dict: Totals by outcome (ok, warning, error, running), counts per
            task type, and up to ten recent failures with a short status.
    """
    since = now - hours * 3600
    summary: dict[str, Any] = {"hours": hours, "ok": 0, "warning": 0, "error": 0, "running": 0, "by_type": {}, "failures": [], "recent": []}
    for row in rows:
        start = _integer(row.get("starttime"))
        if start is None or start < since:
            continue
        status = str(row.get("status") or "")
        outcome = ("running" if not row.get("endtime") and not status else "ok" if status == "OK"
                   else "warning" if status.startswith("WARNINGS") else "error")
        kind = _text(row.get("type"), 40) or "other"
        summary[outcome] += 1
        counts = summary["by_type"].setdefault(kind, {"ok": 0, "warning": 0, "error": 0, "running": 0})
        counts[outcome] += 1
        if len(summary["recent"]) < 200:
            summary["recent"].append({"type": kind, "node": _text(row.get("node"), 160), "id": _text(row.get("id"), 40),
                                      "start": start, "end": _integer(row.get("endtime")), "outcome": outcome})
        if outcome == "error" and len(summary["failures"]) < 10:
            summary["failures"].append({"type": kind, "node": _text(row.get("node"), 160), "id": _text(row.get("id"), 40),
                                        "start": start, "status": _text(status, 120)})
    summary["by_type"] = dict(sorted(summary["by_type"].items(), key=lambda item: -sum(item[1].values()))[:16])
    return summary


def _vdevs(children: Any, depth: int = 0, found: list | None = None) -> list[dict]:
    """Flatten a ZFS pool's device tree into indented rows.

    Args:
        children: The ``children`` list of a pool detail node.
        depth: Nesting level of these children.
        found: Rows collected so far.

    Returns:
        list[dict]: ``name``, ``state``, ``depth``, and error counters, with
            ``/dev/disk/by-id/`` prefixes removed from device names.
    """
    found = [] if found is None else found
    if not isinstance(children, list):
        return found
    for child in children:
        if not isinstance(child, dict) or len(found) >= MAX_VDEVS:
            continue
        name = _text(child.get("name"), 160) or "?"
        for prefix in ("/dev/disk/by-id/", "/dev/"):
            if name.startswith(prefix):
                name = name[len(prefix):]
        found.append({
            "name": name, "state": _text(child.get("state"), 40), "depth": depth,
            "leaf": bool(_flag(child.get("leaf"))),
            "read": _integer(child.get("read")), "write": _integer(child.get("write")),
            "cksum": _integer(child.get("cksum")),
        })
        _vdevs(child.get("children"), depth + 1, found)
    return found


def _zfs_pools(reader: _Reader, node: str) -> list[dict]:
    """Read one node's ZFS pools with health and device state.

    Args:
        reader: The bounded API reader.
        node: Node name.

    Returns:
        list[dict]: One curated record per pool.
    """
    encoded = quote(node, safe="")
    pools = []
    for row in reader.rows(f"/nodes/{encoded}/disks/zfs", MAX_POOLS):
        name = _text(row.get("name"), 120)
        if name is None:
            continue
        pool = {
            "name": name, "health": _text(row.get("health"), 40),
            "size": _integer(row.get("size")), "alloc": _integer(row.get("alloc")),
            "free": _integer(row.get("free")), "frag": _integer(row.get("frag")),
            "dedup": _number(row.get("dedup")),
        }
        try:
            detail = reader.get(f"/nodes/{encoded}/disks/zfs/{quote(name, safe='')}")
        except Exception as exc:  # Detail is optional: keep the summary row.
            _LOGGER.info("ZFS pool detail unavailable (%s)", type(exc).__name__)
            detail = None
        if isinstance(detail, dict):
            pool.update({
                "state": _text(detail.get("state"), 40),
                "errors": _text(detail.get("errors")),
                "scan": _text(detail.get("scan")),
                "status": _text(detail.get("status")),
                "action": _text(detail.get("action")),
                # The top child is the pool itself; its children are vdevs.
                "vdevs": _vdevs(next(
                    (child.get("children") for child in detail.get("children", [])
                     if isinstance(child, dict)), []) if isinstance(detail.get("children"), list) else []),
            })
        pools.append(pool)
    return pools


def _osds(tree: Any) -> list[dict]:
    """Flatten a Ceph OSD tree into one row per OSD.

    Args:
        tree: ``data`` from ``/nodes/{node}/ceph/osd``: ``{"root": {...}}``.

    Returns:
        list[dict]: Curated OSD rows with host, state, class, and usage.
    """
    found: list[dict] = []

    def walk(item: Any, host: str | None) -> None:
        if not isinstance(item, dict) or len(found) >= MAX_OSDS:
            return
        if item.get("type") == "host":
            host = _text(item.get("name"), 160)
        if item.get("type") == "osd":
            found.append({
                "id": _integer(item.get("id")), "name": _text(item.get("name"), 40),
                "host": _text(item.get("host"), 160) or host,
                "status": _text(item.get("status"), 20), "in": _flag(item.get("in")),
                "device_class": _text(item.get("device_class"), 20),
                "percent_used": _number(item.get("percent_used")),
                "total": _integer(item.get("total_space")), "used": _integer(item.get("bytes_used")),
                "pgs": _integer(item.get("pgs")),
                "apply_latency_ms": _integer(item.get("apply_latency_ms")),
                "commit_latency_ms": _integer(item.get("commit_latency_ms")),
            })
        for child in item.get("children", []) if isinstance(item.get("children"), list) else []:
            walk(child, host)

    walk(tree.get("root") if isinstance(tree, dict) else None, None)
    return sorted(found, key=lambda osd: (osd["host"] or "", osd["id"] if osd["id"] is not None else -1))


def _ceph_status(data: Any) -> dict:
    """Curate ``/cluster/ceph/status`` into health, capacity, and PG state.

    Args:
        data: The decoded Ceph status.

    Returns:
        dict: Health and its checks, OSD and monitor counts, capacity,
            placement-group states, and client I/O rates.

    Raises:
        ValueError: The status is not an object.
    """
    if not isinstance(data, dict):
        raise ValueError("Unexpected Ceph status schema")
    health = data.get("health") if isinstance(data.get("health"), dict) else {}
    checks = []
    raw_checks = health.get("checks") if isinstance(health.get("checks"), dict) else {}
    for code, check in list(raw_checks.items())[:MAX_CHECKS]:
        if not isinstance(check, dict):
            continue
        summary = check.get("summary")
        checks.append({
            "code": _text(code, 80), "severity": _text(check.get("severity"), 20),
            "summary": _text(summary.get("message") if isinstance(summary, dict) else summary),
        })
    osdmap = data.get("osdmap") if isinstance(data.get("osdmap"), dict) else {}
    # Older releases nest the counts one level deeper.
    if isinstance(osdmap.get("osdmap"), dict):
        osdmap = osdmap["osdmap"]
    pgmap = data.get("pgmap") if isinstance(data.get("pgmap"), dict) else {}
    monmap = data.get("monmap") if isinstance(data.get("monmap"), dict) else {}
    states = [
        {"state": _text(row.get("state_name"), 80), "count": _integer(row.get("count"))}
        for row in pgmap.get("pgs_by_state", []) if isinstance(row, dict)
    ][:32] if isinstance(pgmap.get("pgs_by_state"), list) else []
    mons = monmap.get("mons") if isinstance(monmap.get("mons"), list) else []
    quorum = data.get("quorum") if isinstance(data.get("quorum"), list) else []
    return {
        "health": _text(health.get("status"), 40), "checks": checks,
        "osds": {"total": _integer(osdmap.get("num_osds")), "up": _integer(osdmap.get("num_up_osds")),
                 "in": _integer(osdmap.get("num_in_osds"))},
        "monitors": {"total": len(mons), "in_quorum": len(quorum)},
        "bytes_total": _integer(pgmap.get("bytes_total")), "bytes_used": _integer(pgmap.get("bytes_used")),
        "bytes_avail": _integer(pgmap.get("bytes_avail")), "data_bytes": _integer(pgmap.get("data_bytes")),
        "pgs": _integer(pgmap.get("num_pgs")), "pg_states": states, "pool_count": _integer(pgmap.get("num_pools")),
        "read_bytes_sec": _integer(pgmap.get("read_bytes_sec")),
        "write_bytes_sec": _integer(pgmap.get("write_bytes_sec")),
    }


#: SMART text fields kept for NVMe disks, by the label smartctl prints.
_NVME_FIELDS = {
    "Temperature": "temperature_c", "Available Spare": "spare_percent",
    "Percentage Used": "worn_percent", "Data Units Written": "written_bytes",
    "Data Units Read": "read_bytes", "Power On Hours": "power_on_hours",
    "Power Cycles": "power_cycles", "Unsafe Shutdowns": "unsafe_shutdowns",
    "Media and Data Integrity Errors": "media_errors", "Critical Warning": "critical_warning",
}

#: SMART attributes kept for ATA disks, by attribute name.
_ATA_FIELDS = {
    "Temperature_Celsius": "temperature_c", "Airflow_Temperature_Cel": "temperature_c",
    "Power_On_Hours": "power_on_hours", "Power_Cycle_Count": "power_cycles",
    "Reallocated_Sector_Ct": "reallocated_sectors", "Current_Pending_Sector": "pending_sectors",
    "Offline_Uncorrectable": "uncorrectable_sectors", "Wear_Leveling_Count": "wear_leveling",
    "Total_LBAs_Written": "lbas_written",
}


def _first_integer(text: str) -> int | None:
    """Return the first whole number in a SMART value, ignoring separators.

    Args:
        text: A raw value such as ``"89,842,403 [45.9 TB]"`` or ``"24 (Min/Max 15/45)"``.

    Returns:
        int | None: The number, or None.
    """
    digits = ""
    for char in text.strip():
        if char.isdigit():
            digits += char
        elif char == "," and digits:
            continue
        elif digits:
            break
    return int(digits) if digits else None


def _smart(data: Any) -> dict:
    """Curate one disk's SMART reading into a few named counters.

    NVMe disks report a text block; ATA disks a list of attributes. Only the
    counters in :data:`_NVME_FIELDS` and :data:`_ATA_FIELDS` are kept.

    Args:
        data: ``data`` from ``/nodes/{node}/disks/smart``.

    Returns:
        dict: ``health`` plus whichever counters the disk reported.
    """
    if not isinstance(data, dict):
        return {}
    found: dict[str, Any] = {"health": _text(data.get("health"), 40)}
    text = data.get("text")
    if isinstance(text, str):
        for line in text.splitlines()[:200]:
            label, _, value = line.partition(":")
            key = _NVME_FIELDS.get(label.strip())
            if key and key not in found:
                number = (int(value.strip(), 16) if key == "critical_warning" and value.strip().startswith("0x")
                          else _first_integer(value))
                if number is not None:
                    # An NVMe data unit is 1000 blocks of 512 bytes.
                    found[key] = number * 512000 if key.endswith("_bytes") else number
    attributes = data.get("attributes")
    if isinstance(attributes, list):
        for row in attributes[:128]:
            if not isinstance(row, dict):
                continue
            key = _ATA_FIELDS.get(str(row.get("name") or ""))
            if key and key not in found:
                number = _first_integer(str(row.get("raw") or ""))
                if key == "wear_leveling":
                    number = _integer(row.get("normalized"))
                if number is not None:
                    found[key] = number
    return {key: value for key, value in found.items() if value is not None}


def _disks(reader: "_Reader", node: str, clock: Callable[[], float]) -> list[dict]:
    """Read one node's physical disks with health, wear, and SMART counters.

    Args:
        reader: The bounded API reader.
        node: Node name.
        clock: Monotonic clock for the SMART time budget.

    Returns:
        list[dict]: One curated record per disk. ``worn_percent`` is how much
            of an SSD's rated life is used (Proxmox's ``wearout`` is what is
            left); ``boot`` marks the disk holding the boot partition.
    """
    encoded = quote(node, safe="")
    disks = []
    budget = clock() + SMART_BUDGET_SECONDS
    for row in reader.rows(f"/nodes/{encoded}/disks/list", MAX_DISKS):
        path = _text(row.get("devpath"), 120)
        if path is None:
            continue
        wearout = _integer(row.get("wearout"))
        used = _text(row.get("used"), 60)
        osds = row.get("osdid-list") if isinstance(row.get("osdid-list"), list) else []
        disk = {
            "path": path, "type": _text(row.get("type"), 20), "model": _text(row.get("model"), 80),
            "serial": _text(row.get("serial"), 60), "size": _integer(row.get("size")),
            "health": _text(row.get("health"), 40), "used": used,
            "rpm": _integer(row.get("rpm")) or None,
            "worn_percent": 100 - wearout if wearout is not None and 0 <= wearout <= 100 else None,
            "osds": [str(osd)[:8] for osd in osds[:8]],
            "boot": used in ("BIOS boot", "EFI"),
        }
        if clock() < budget:
            try:
                disk["smart"] = _smart(reader.get(f"/nodes/{encoded}/disks/smart?disk={quote(path, safe='')}"))
            except Exception as exc:  # SMART is optional per disk.
                _LOGGER.info("SMART unavailable for a disk (%s)", type(exc).__name__)
        if disk.get("smart", {}).get("worn_percent") is not None and disk["worn_percent"] is None:
            disk["worn_percent"] = disk["smart"]["worn_percent"]
        disks.append(disk)
    return disks


def _agent_result(data: Any) -> Any:
    """Unwrap a guest agent reply (``{"result": ...}``).

    Args:
        data: ``data`` from an ``/agent/...`` call.

    Returns:
        Any: The result, or None.
    """
    return data.get("result") if isinstance(data, dict) else None


def _addresses(interfaces: Any) -> list[dict]:
    """Curate a guest's interface list into its meaningful addresses.

    Both the QEMU guest agent and the container ``/interfaces`` call return
    ``name``, ``hardware-address``, and ``ip-addresses``.

    Args:
        interfaces: The interface list.

    Returns:
        list[dict]: ``interface``, ``mac``, ``address``, ``prefix``, and
            ``family``, without loopback, link-local, or container and
            overlay interfaces.
    """
    addresses = []
    for interface in interfaces if isinstance(interfaces, list) else []:
        name = _text(interface.get("name"), 40) if isinstance(interface, dict) else None
        if name is None or name.startswith(_SKIP_INTERFACES):
            continue
        for entry in interface.get("ip-addresses") or []:
            address = _text(entry.get("ip-address"), 64) if isinstance(entry, dict) else None
            if address is None or address.startswith(("127.", "169.254.", "fe80:", "::1")):
                continue
            if len(addresses) < MAX_GUEST_ADDRESSES:
                addresses.append({"interface": name, "mac": _text(interface.get("hardware-address") or interface.get("hwaddr"), 24),
                                  "address": address, "prefix": _integer(entry.get("prefix")),
                                  "family": "ipv6" if ":" in address else "ipv4"})
    return addresses


def _container_addresses(reader: "_Reader", node: str, vmid: int) -> dict:
    """Read a running container's addresses (``/lxc/{vmid}/interfaces``).

    Args:
        reader: The bounded API reader.
        node: Node name.
        vmid: Container ID.

    Returns:
        dict: ``addresses`` in :func:`_addresses` form, no OS or filesystems.
    """
    rows = reader.get(f"/nodes/{quote(node, safe='')}/lxc/{vmid}/interfaces")
    return {"os": None, "addresses": _addresses(rows), "filesystems": []}


def _guest_agent(reader: "_Reader", node: str, vmid: int) -> dict:
    """Read one VM's OS, addresses, and filesystems through its guest agent.

    Uses only the informational agent commands that VM.GuestAgent.Audit
    allows. Loopback, link-local, and container or overlay interfaces are
    left out, as are pseudo and in-memory filesystems.

    Args:
        reader: The bounded API reader.
        node: Node name.
        vmid: VM ID.

    Returns:
        dict: ``os`` (name, version, kernel), ``addresses`` (interface,
            MAC, address, prefix), and ``filesystems`` (mount, type, total,
            used).

    Raises:
        requests.RequestException: The agent is not running or not enabled.
    """
    base = f"/nodes/{quote(node, safe='')}/qemu/{vmid}/agent"
    info = _agent_result(reader.get(f"{base}/get-osinfo")) or {}
    result: dict[str, Any] = {"os": {
        "name": _text(info.get("pretty-name") or info.get("name"), 80), "id": _text(info.get("id"), 30),
        "version": _text(info.get("version-id"), 30), "kernel": _text(info.get("kernel-release"), 80),
    }}
    try:
        interfaces = _agent_result(reader.get(f"{base}/network-get-interfaces")) or []
    except Exception:  # Some agents answer osinfo but not the network call.
        interfaces = []
    addresses = _addresses(interfaces)
    result["addresses"] = addresses
    filesystems = []
    try:
        mounts = _agent_result(reader.get(f"{base}/get-fsinfo")) or []
    except Exception:  # Windows agents and older agents may refuse this.
        mounts = []
    seen = set()
    for mount in mounts if isinstance(mounts, list) else []:
        if not isinstance(mount, dict) or str(mount.get("type")) in _SKIP_FILESYSTEMS:
            continue
        total, used = _integer(mount.get("total-bytes")), _integer(mount.get("used-bytes"))
        point = _text(mount.get("mountpoint"), 120)
        if not total or point is None or (mount.get("name"), total) in seen:
            continue
        seen.add((mount.get("name"), total))
        if len(filesystems) < MAX_GUEST_FILESYSTEMS:
            filesystems.append({"mount": point, "type": _text(mount.get("type"), 20), "total": total, "used": used})
    result["filesystems"] = filesystems
    return result


def _pci(reader: "_Reader", node: str) -> list[dict]:
    """Read a node's notable PCI devices: GPUs, network and storage controllers.

    Args:
        reader: The bounded API reader.
        node: Node name.

    Returns:
        list[dict]: ``kind`` (gpu, network, storage), ``name``, ``vendor``,
            and ``slot``, one per device function ``.0``.
    """
    kinds = {"03": "gpu", "02": "network", "01": "storage"}
    devices = []
    for row in reader.rows(f"/nodes/{quote(node, safe='')}/hardware/pci", MAX_PCI):
        klass = str(row.get("class") or "")
        kind = kinds.get(klass[2:4]) if klass.startswith("0x") else None
        slot = _text(row.get("id"), 20) or ""
        if kind is None:
            continue
        devices.append({
            "kind": kind, "slot": slot, "name": _text(row.get("device_name"), 120),
            "vendor": _text(row.get("vendor_name"), 80),
            "board": _text(row.get("subsystem_device_name"), 120),
        })
    return devices


def _ceph_pools(rows: list[dict]) -> list[dict]:
    """Curate Ceph pools: replication, placement groups, use, and rule.

    Args:
        rows: ``/nodes/{node}/ceph/pool`` rows.

    Returns:
        list[dict]: One record per pool. ``stored`` is logical data and
            ``raw_used`` includes replicas; ``class_capacity`` is the raw size
            of the OSDs the pool's CRUSH rule can use.
    """
    pools = []
    for row in rows:
        name = _text(row.get("pool_name"), 120)
        if name is None:
            continue
        status = row.get("autoscale_status") if isinstance(row.get("autoscale_status"), dict) else {}
        apps = row.get("application_metadata") if isinstance(row.get("application_metadata"), dict) else {}
        pools.append({
            "name": name, "id": _integer(row.get("pool")), "type": _text(row.get("type"), 20),
            "size": _integer(row.get("size")), "min_size": _integer(row.get("min_size")),
            "pg_num": _integer(row.get("pg_num")), "pg_target": _integer(status.get("pg_num_target")),
            "autoscale": _text(row.get("pg_autoscale_mode"), 10),
            "rule": _text(row.get("crush_rule_name"), 80),
            "applications": sorted(str(key)[:20] for key in list(apps)[:4]),
            "stored": _integer(status.get("logical_used")), "raw_used": _integer(row.get("bytes_used")),
            "percent_used": _number(row.get("percent_used")),
            "class_capacity": _integer(status.get("subtree_capacity")),
        })
    return pools


def _issuer_name(issuer: Any) -> str | None:
    """Return the CN (else O) of an OpenSSL-style issuer string.

    Args:
        issuer: Such as ``/C=US/O=Example/CN=Example CA``.

    Returns:
        str | None: The common name or organisation.
    """
    if not isinstance(issuer, str):
        return None
    parts = dict(part.split("=", 1) for part in issuer.strip("/").split("/") if "=" in part)
    return _text(parts.get("CN") or parts.get("O"), 80)


def _certificates(rows: list[dict]) -> list[dict]:
    """Curate a node's certificates to file, expiry, and issuer.

    Args:
        rows: ``/nodes/{node}/certificates/info`` rows (the PEM is dropped).

    Returns:
        list[dict]: ``file``, ``not_after`` (Unix time), ``issuer``.
    """
    return [{"file": _text(row.get("filename"), 80), "not_after": _integer(row.get("notafter")),
             "issuer": _issuer_name(row.get("issuer"))} for row in rows[:16] if _text(row.get("filename"), 80)]


def _services(rows: list[dict]) -> list[dict]:
    """Curate a node's Proxmox services to name and state.

    Args:
        rows: ``/nodes/{node}/services`` rows.

    Returns:
        list[dict]: ``name``, ``state`` (running, dead, ...), ``enabled``.
    """
    return [{"name": _text(row.get("name") or row.get("service"), 60), "state": _text(row.get("state"), 20),
             "enabled": row.get("unit-state") == "enabled"} for row in rows[:64] if _text(row.get("name") or row.get("service"), 60)]


def _kernel_key(version: str) -> tuple:
    """Order kernel versions such as ``7.0.14-26`` numerically.

    Args:
        version: A kernel version string.

    Returns:
        tuple: Its numeric parts, for sorting.
    """
    return tuple(int(part) if part.isdigit() else 0 for part in re.split(r"[.-]", version))


def _versions(rows: list[dict]) -> dict:
    """Summarize installed and upgradable packages from ``apt/versions``.

    Proxmox reports each package's installed version as ``OldVersion`` and
    the newest available as ``Version``; the running manager and kernel
    come from ``ManagerVersion`` and ``RunningKernel``. No privilege beyond
    Sys.Audit is needed (listing updates directly needs Sys.Modify).

    Args:
        rows: ``/nodes/{node}/apt/versions`` rows.

    Returns:
        dict: Running and installed manager and kernel versions, whether a
            newer kernel is installed than the one running (a reboot is
            needed), and the upgradable packages.
    """
    found: dict[str, Any] = {"upgradable": []}
    kernels = []
    for row in rows[:512]:
        package = _text(row.get("Package"), 80)
        installed, candidate = _text(row.get("OldVersion"), 60), _text(row.get("Version"), 60)
        if package is None:
            continue
        if row.get("RunningKernel"):
            found["running_kernel"] = _text(row.get("RunningKernel"), 60)
        if package == "pve-manager":
            found["manager_running"] = _text(str(row.get("ManagerVersion") or "").split("/")[0], 30)
            found["manager_installed"] = installed
            found["manager_available"] = candidate
        if re.fullmatch(r"proxmox-kernel-[\d.]+", package) and installed:
            kernels.append(installed)
        if installed and candidate and installed != candidate and len(found["upgradable"]) < 128:
            found["upgradable"].append({"package": package, "installed": installed, "available": candidate})
    if kernels:
        newest = max(kernels, key=_kernel_key)
        found["kernel_installed"] = newest
        running = (found.get("running_kernel") or "").removesuffix("-pve")
        found["reboot_needed"] = bool(running) and _kernel_key(running) < _kernel_key(newest)
    if found.get("manager_running") and found.get("manager_installed"):
        found["manager_restart_needed"] = found["manager_running"] != found["manager_installed"]
    return found


def _guest_history(rows: list[dict]) -> list[dict]:
    """Average a guest's RRD rows into 30-minute points.

    Args:
        rows: ``/nodes/{node}/{qemu,lxc}/{vmid}/rrddata`` rows.

    Returns:
        list[dict]: ``t`` plus ``cpu`` (0..1 of its vCPUs), ``mem`` (0..1 of
            its memory), and ``net_in``, ``net_out``, ``disk_read``,
            ``disk_write`` in bytes per second, where reported.
    """
    fields = (
        ("cpu", lambda row: _number(row.get("cpu"))),
        ("mem", lambda row: (lambda used, total: used / total if used is not None and total else None)(
            _number(row.get("mem")), _number(row.get("maxmem")))),
        ("net_in", lambda row: _number(row.get("netin"))),
        ("net_out", lambda row: _number(row.get("netout"))),
        ("disk_read", lambda row: _number(row.get("diskread"))),
        ("disk_write", lambda row: _number(row.get("diskwrite"))),
    )
    buckets: dict[int, dict[str, list[float]]] = {}
    for row in rows:
        stamp = _integer(row.get("time"))
        if stamp is None:
            continue
        bucket = buckets.setdefault(stamp - stamp % GUEST_HISTORY_BUCKET_SECONDS, {})
        for name, read in fields:
            value = read(row)
            if value is not None:
                bucket.setdefault(name, []).append(value)
    points = []
    for stamp in sorted(buckets):
        values = buckets[stamp]
        if not values:
            continue
        point: dict[str, Any] = {"t": stamp}
        for name, _ in fields:
            if values.get(name):
                average = sum(values[name]) / len(values[name])
                point[name] = round(average, 4) if name in ("cpu", "mem") else round(average, 1)
        points.append(point)
    return points[-int(86400 / GUEST_HISTORY_BUCKET_SECONDS):]


def _rebooted_since(cached: tuple, uptime: int | None, now: float) -> bool:
    """Whether a node restarted after its hourly reads were cached.

    The cache holds the monotonic time and the node's uptime when it was
    filled; had the node kept running, its uptime would have grown by the
    time since. Less (with two minutes' slack) means it booted in between.

    Args:
        cached: ``(monotonic time, parts, uptime)`` from the hourly cache.
        uptime: The node's uptime in seconds now, if reported.
        now: The monotonic time now.

    Returns:
        bool: True when the node has rebooted since the cache was filled.
    """
    then = cached[2] if len(cached) > 2 else None
    if uptime is None or then is None:
        return False
    return uptime + 120 < then + (now - cached[0])



def cluster_snapshot(adapter: Any, settings: dict, clock: Callable[[], float] = time.monotonic,
                     hardware: dict | None = None) -> dict:
    """Read node, storage, ZFS, and Ceph telemetry for one Proxmox source.

    Args:
        adapter: The Proxmox adapter whose HTTP client is reused.
        settings: The source's private connection settings.
        clock: Monotonic clock for the read deadline.
        hardware: Per-node cache of disk and PCI reads, ``{node: (read at,
            {"disks": [...], "pci": [...]})}``, updated in place. Disks,
            SMART, and PCI are re-read when an entry is older than
            :data:`HARDWARE_REFRESH_SECONDS`; otherwise the cache is reused.

    Returns:
        dict: ``{"cluster": {...}, "nodes": {name: {...}}}``. Each node record
            is that node's ``proxmox_node`` telemetry; ``cluster`` is the
            shared ``proxmox_cluster`` value. Failed parts are named in each
            record's ``errors``.

    Raises:
        ValueError: The cluster resource list itself is unreadable, so no
            meaningful snapshot can be built.
        requests.RequestException: The controller cannot be contacted.
    """
    reader = _Reader(adapter, settings, clock)
    resources = reader.rows("/cluster/resources", MAX_NODES + MAX_STORAGE_ROWS + 1024)
    cluster_errors: dict[str, str] = {}
    nodes: dict[str, dict] = {}
    for row in resources:
        if row.get("type") != "node" or not _text(row.get("node"), 160):
            continue
        name = _text(row.get("node"), 160)
        nodes[name] = {
            "status": _text(row.get("status"), 20),
            "cpu": _number(row.get("cpu")), "maxcpu": _integer(row.get("maxcpu")),
            "mem": _integer(row.get("mem")), "maxmem": _integer(row.get("maxmem")),
            "rootfs_used": _integer(row.get("disk")), "rootfs_total": _integer(row.get("maxdisk")),
            "uptime": _integer(row.get("uptime")),
            "storage": [], "errors": {},
        }
        if len(nodes) > MAX_NODES:
            raise ValueError("Proxmox node bound exceeded")
    for row in resources:
        node = nodes.get(_text(row.get("node"), 160) or "")
        if row.get("type") != "storage" or node is None or not _text(row.get("storage"), 120):
            continue
        node["storage"].append({
            "storage": _text(row.get("storage"), 120), "type": _text(row.get("plugintype"), 40),
            "shared": bool(_flag(row.get("shared"))), "status": _text(row.get("status"), 20),
            "content": sorted(filter(None, str(row.get("content") or "").split(",")))[:16],
            "used": _integer(row.get("disk")), "total": _integer(row.get("maxdisk")),
        })

    status_rows = optional_read(cluster_errors, "Proxmox", "cluster", lambda: reader.rows("/cluster/status", MAX_NODES + 8), [])
    summary = next((row for row in status_rows if row.get("type") == "cluster"), None)
    storage_config = optional_read(cluster_errors, "Proxmox", "storage", lambda: [
        {
            "storage": _text(row.get("storage"), 120), "type": _text(row.get("type"), 40),
            "shared": bool(_flag(row.get("shared"))), "disabled": bool(_flag(row.get("disable"))),
            "nodes": sorted(filter(None, str(row.get("nodes") or "").split(",")))[:MAX_NODES],
            "content": sorted(filter(None, str(row.get("content") or "").split(",")))[:16],
            "pool": _text(row.get("pool"), 120) or _text(row.get("fs-name"), 120),
        }
        for row in reader.rows("/storage", MAX_STORAGE_ROWS) if _text(row.get("storage"), 120)
    ], [])

    online = [name for name, node in nodes.items() if node["status"] == "online"]
    for name in online:
        node, encoded = nodes[name], quote(name, safe="")
        status = optional_read(node["errors"], "Proxmox", "status", lambda: reader.get(f"/nodes/{encoded}/status"))
        if isinstance(status, dict):
            cpuinfo = status.get("cpuinfo") if isinstance(status.get("cpuinfo"), dict) else {}
            swap = status.get("swap") if isinstance(status.get("swap"), dict) else {}
            loadavg = status.get("loadavg") if isinstance(status.get("loadavg"), list) else []
            node.update({
                "cpu_model": _text(cpuinfo.get("model"), 120),
                "sockets": _integer(cpuinfo.get("sockets")), "cores": _integer(cpuinfo.get("cores")),
                "threads": _integer(cpuinfo.get("cpus")),
                "loadavg": [_number(value) for value in loadavg[:3]],
                "swap_used": _integer(swap.get("used")), "swap_total": _integer(swap.get("total")),
                "pve_version": _text(status.get("pveversion"), 80),
            })
        node["history"] = optional_read(node["errors"], "Proxmox", "history", lambda: _history(
            reader.rows(f"/nodes/{encoded}/rrddata?timeframe=day&cf=AVERAGE", 4000)), [])
        node["zfs"] = optional_read(node["errors"], "Proxmox", "zfs", lambda: _zfs_pools(reader, name), [])
        # Packages and services change with maintenance, so they are read
        # every refresh; a failed read keeps the last good one.
        last = hardware.get(name)[1] if hardware is not None and name in hardware else {}
        maintenance = {
            "services": optional_read(node["errors"], "Proxmox", "services", lambda: _services(
                reader.rows(f"/nodes/{encoded}/services", 128)), last.get("services")),
            "versions": optional_read(node["errors"], "Proxmox", "versions", lambda: _versions(
                reader.rows(f"/nodes/{encoded}/apt/versions", 1024)), last.get("versions")),
        }
        cached = hardware.get(name) if hardware is not None else None
        if cached is None or clock() - cached[0] >= HARDWARE_REFRESH_SECONDS or _rebooted_since(cached, node.get("uptime"), clock()):
            previous = cached[1] if cached else {}
            parts = {
                "disks": optional_read(node["errors"], "Proxmox", "disks", lambda: _disks(reader, name, clock), previous.get("disks")),
                "pci": optional_read(node["errors"], "Proxmox", "pci", lambda: _pci(reader, name), previous.get("pci")),
                "certificates": optional_read(node["errors"], "Proxmox", "certificates", lambda: _certificates(
                    reader.rows(f"/nodes/{encoded}/certificates/info", 32)), previous.get("certificates")),
                "hardware_sampled_at": datetime.now(timezone.utc).isoformat(),
            }
            if cached and "disks" in node["errors"]:
                parts["hardware_sampled_at"] = previous.get("hardware_sampled_at")
            cached = (clock(), parts, node.get("uptime"))
        if hardware is not None:
            # The last good packages and services ride along as the
            # fallback for the next refresh's failed read.
            hardware[name] = (cached[0], {**cached[1], **maintenance}, *cached[2:])
        node.update({**cached[1], **maintenance})

    # Guest agents: running VMs only, each at most every AGENT_REFRESH_SECONDS,
    # within one shared time budget; a silent agent is skipped for a while.
    agents = hardware.setdefault("__agents__", {}) if hardware is not None else {}
    budget = clock() + AGENT_BUDGET_SECONDS
    running_vms = [row for row in resources if row.get("type") in ("qemu", "lxc") and row.get("status") == "running"
                   and not _flag(row.get("template")) and _integer(row.get("vmid")) and _text(row.get("node"), 160) in nodes]
    for row in running_vms:
        name, vmid = _text(row.get("node"), 160), _integer(row.get("vmid"))
        last = agents.get(vmid)
        due = last is None or clock() - last[0] >= (AGENT_RETRY_SECONDS if last[1] is None else AGENT_REFRESH_SECONDS)
        if due and clock() < budget and nodes[name]["status"] == "online":
            try:
                read = _guest_agent if row.get("type") == "qemu" else _container_addresses
                last = (clock(), read(reader, name, vmid))
            except Exception as exc:  # No agent, or it did not answer.
                _LOGGER.debug("Guest agent unavailable for a VM (%s)", type(exc).__name__)
                last = (clock(), last[1] if last and last[1] else None)
            agents[vmid] = last
        if last and last[1]:
            nodes[name].setdefault("guest_agent", {})[str(vmid)] = last[1]

    # Guest history: each running guest's day, refreshed every 15 minutes.
    histories = hardware.setdefault("__guest_history__", {}) if hardware is not None else {}
    budget = clock() + GUEST_HISTORY_BUDGET_SECONDS
    running_guests = [row for row in resources if row.get("type") in ("qemu", "lxc") and row.get("status") == "running"
                      and not _flag(row.get("template")) and _integer(row.get("vmid")) and _text(row.get("node"), 160) in nodes]
    for row in running_guests:
        name, vmid, kind = _text(row.get("node"), 160), _integer(row.get("vmid")), row.get("type")
        last = histories.get(vmid)
        if (last is None or clock() - last[0] >= GUEST_HISTORY_REFRESH_SECONDS) and clock() < budget:
            points = optional_read({}, "Proxmox", "guest_history", lambda: _guest_history(reader.rows(
                f"/nodes/{quote(name, safe='')}/{kind}/{vmid}/rrddata?timeframe=day&cf=AVERAGE", 4000)))
            last = (clock(), points if points is not None else (last[1] if last else None))
            histories[vmid] = last
        if last and last[1]:
            nodes[name].setdefault("guest_history", {})[str(vmid)] = last[1]

    ceph = None
    uses_ceph = any(row.get("type") in CEPH_STORAGE_TYPES for row in storage_config) or any(
        item.get("type") in CEPH_STORAGE_TYPES for node in nodes.values() for item in node["storage"])
    if uses_ceph and online:
        ceph = optional_read(cluster_errors, "Proxmox", "ceph", lambda: _ceph_status(reader.get("/cluster/ceph/status")))
        if ceph is not None:
            # Every node reports the whole OSD tree; ask the first that answers.
            for name in online:
                tree = optional_read({}, "Proxmox", "ceph_osds", lambda: reader.get(f"/nodes/{quote(name, safe='')}/ceph/osd"))
                if tree is not None:
                    ceph["osd_list"] = _osds(tree)
                    ceph["pool_list"] = optional_read(cluster_errors, "Proxmox", "ceph_pools", lambda: _ceph_pools(
                        reader.rows(f"/nodes/{quote(name, safe='')}/ceph/pool", 256)), [])
                    break
            else:
                cluster_errors["ceph_osds"] = "unavailable"

    tasks = optional_read(cluster_errors, "Proxmox", "tasks", lambda: _tasks(reader.rows("/cluster/tasks", 2000), time.time()))
    unprotected = optional_read(cluster_errors, "Proxmox", "backups", lambda: sorted(
        vmid for vmid in (_integer(row.get("vmid")) for row in reader.rows("/cluster/backup-info/not-backed-up", 1024))
        if vmid is not None))
    if ceph is not None:
        ceph["flags"] = optional_read(cluster_errors, "Proxmox", "ceph_flags", lambda: sorted(
            _text(row.get("name"), 40) for row in reader.rows("/cluster/ceph/flags", 64)
            if _flag(row.get("value")) and _text(row.get("name"), 40)), [])
    cluster = {
        "tasks": tasks, "not_backed_up": unprotected,
        "name": _text(summary.get("name"), 120) if summary else None,
        "quorate": _flag(summary.get("quorate")) if summary else None,
        "node_count": _integer(summary.get("nodes")) if summary else len(nodes),
        "storage_config": storage_config, "ceph": ceph, "uses_ceph": uses_ceph,
        "errors": cluster_errors,
    }
    return {"cluster": cluster, "nodes": nodes}


def collect_cluster_telemetry(sessions: Any, source: Any, adapter: Any, settings: dict) -> None:
    """Refresh cluster telemetry on a source's node observations, at most every five minutes.

    A failed read keeps each node's previous readings and marks them
    unavailable; it never fails discovery or creates observations.

    Args:
        sessions: Session factory for short write transactions.
        source: The Proxmox discovery source.
        adapter: The Proxmox adapter whose HTTP client is reused.
        settings: The source's private connection settings.
    """
    attempts = getattr(adapter, "_cluster_attempts", {})
    try:
        key = (source.id, controller_url(settings))
    except (ValueError, TypeError, KeyError):  # Incomplete settings: nothing to read.
        return
    now = time.monotonic()
    if now - attempts.get(key, -10 * REFRESH_SECONDS) < REFRESH_SECONDS:
        return
    attempts[key] = now
    adapter._cluster_attempts = attempts
    try:
        hardware = getattr(adapter, "_cluster_hardware", {})
        adapter._cluster_hardware = hardware
        snapshot = cluster_snapshot(adapter, settings, hardware=hardware.setdefault(key, {}))
    except Exception as exc:
        _LOGGER.warning("Proxmox cluster telemetry unavailable (%s)", type(exc).__name__)
        snapshot = None
    sampled_at = datetime.now(timezone.utc).isoformat()
    with sessions.begin() as db:
        for host in db.scalars(select(DiscoveredDevice).where(DiscoveredDevice.source_id == source.id)):
            details = json_object(host.details_json)
            if details.get("platform") != "proxmox":
                continue
            node = snapshot["nodes"].get(host.remote_id) if snapshot else None
            if node is None:
                previous = details.get("proxmox_node")
                if isinstance(previous, dict):
                    details["proxmox_node"] = {**previous, "available": False}
            else:
                details["proxmox_node"] = {**node, "available": True, "sampled_at": sampled_at}
            if snapshot:
                details["proxmox_cluster"] = {**snapshot["cluster"], "available": True, "sampled_at": sampled_at}
            elif isinstance(details.get("proxmox_cluster"), dict):
                details["proxmox_cluster"] = {**details["proxmox_cluster"], "available": False}
            host.details_json = json.dumps(details)
