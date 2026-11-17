# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Invented Proxmox and UniFi Network telemetry for the demo lab.

``demo_seed.py`` stores these on the demo's observations, in the shapes the
collector writes, so the Cluster and Networks pages, the storage shelves,
and the rack view's drive bays have something to show in screenshots:

- a six-node Proxmox cluster: three rack servers (pve1 to pve3) running
  Ceph on NVMe, with a ZFS "tank" of four hard disks on pve3, and three
  mini PCs (mini1 to mini3) on single NVMe drives; guests, history,
  services, certificates, and pending updates (one node waits on a reboot);
- the gateway's network snapshot: three networks, two Wi-Fi networks,
  firewall zones and policies, the adopted devices, and clients.

Every name is invented and every address is a documentation range.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

GIB = 1024 ** 3
TIB = 1024 ** 4
DAY = 86400

RACK_NODES = ("pve1", "pve2", "pve3")
MINI_NODES = ("mini1", "mini2", "mini3")

#: Guests: vmid, name, node, kind, vcpus, memory GiB, disk GiB, running, OS.
GUESTS = (
    (100, "home-assistant", "pve1", "vm", 2, 4, 32, True, "Home Assistant OS"),
    (101, "media", "pve1", "vm", 4, 8, 64, True, "Debian 12"),
    (102, "git", "pve2", "container", 2, 2, 16, True, "Debian 12"),
    (103, "monitoring", "pve2", "vm", 2, 6, 48, True, "Ubuntu 24.04"),
    (104, "build-runner", "pve3", "vm", 8, 16, 120, True, "Ubuntu 24.04"),
    (105, "backup", "pve3", "container", 2, 4, 24, True, "Debian 12"),
    (106, "dns", "mini1", "container", 1, 1, 8, True, "Debian 12"),
    (107, "k3s-a", "mini1", "vm", 2, 6, 40, True, "Ubuntu 24.04"),
    (108, "k3s-b", "mini2", "vm", 2, 6, 40, True, "Ubuntu 24.04"),
    (109, "k3s-c", "mini3", "vm", 2, 6, 40, True, "Ubuntu 24.04"),
    (110, "sandbox", "mini3", "vm", 2, 4, 32, False, "Windows 11"),
    (9000, "ubuntu-template", "pve2", "vm", 2, 2, 20, False, "Ubuntu 24.04"),
)


def _wave(step: int, phase: float, period: float = 288) -> float:
    """Make a smooth wave between 0 and 1 for invented readings.

    Args:
        step: The reading's index.
        phase: Shift, in radians, so nodes differ.
        period: Steps per full wave.

    Returns:
        float: The wave's value.
    """
    return 0.5 + 0.5 * math.sin(step / period * 2 * math.pi + phase)


def _history(now: datetime, seed: int, load: float, memory: float) -> list[dict]:
    """Invent a day of node readings, five minutes apart, ending now.

    Args:
        now: The last reading's time.
        seed: Varies the waves from node to node.
        load: Typical CPU load, 0 to 1.
        memory: Typical memory use, 0 to 1.

    Returns:
        list[dict]: The readings, oldest first, as the collector stores them.
    """
    points = []
    end = int(now.timestamp())
    for step in range(288, -1, -1):
        wave = _wave(step, seed)
        burst = 0.25 if (step + seed * 17) % 61 < 3 else 0.0
        points.append({
            "t": end - step * 300,
            "cpu": round(min(0.98, load * (0.6 + 0.6 * wave) + burst), 4),
            "mem": round(memory + 0.04 * wave, 4),
            "iowait": round(0.002 + 0.01 * wave, 4),
            "net_in": round(2e5 + 4e6 * wave + 8e6 * burst, 1),
            "net_out": round(1e5 + 1.5e6 * wave + 3e6 * burst, 1),
            "psi_cpu": round(0.4 + 3 * wave * load + 20 * burst, 2),
            "psi_io": round(0.1 + 1.2 * wave, 2),
            "psi_mem": round(0.02 * wave, 3),
        })
    return points


def _guest_history(now: datetime, seed: int, load: float) -> list[dict]:
    """Invent a day of guest readings, five minutes apart.

    Args:
        now: The last reading's time.
        seed: Varies the waves from guest to guest.
        load: Typical CPU load, 0 to 1.

    Returns:
        list[dict]: The readings, oldest first.
    """
    end = int(now.timestamp())
    return [{
        "t": end - step * 300,
        "cpu": round(load * (0.4 + 0.8 * _wave(step, seed)), 4),
        "mem": round(0.45 + 0.1 * _wave(step, seed + 1), 4),
        "net_in": round(5e4 + 6e5 * _wave(step, seed + 2), 1),
        "net_out": round(2e4 + 2e5 * _wave(step, seed + 3), 1),
        "disk_read": round(1e5 * _wave(step, seed + 4), 1),
        "disk_write": round(2e5 * _wave(step, seed + 5), 1),
    } for step in range(288, -1, -1)]


def _smart_nvme(worn: int, hours: int, temperature: int) -> dict:
    """Invent SMART readings for an NVMe drive.

    Args:
        worn: Percentage of rated life used.
        hours: Power-on hours.
        temperature: Temperature in degrees Celsius.

    Returns:
        dict: The curated SMART counters.
    """
    return {"health": "PASSED", "critical_warning": 0, "temperature_c": temperature, "spare_percent": 100,
            "worn_percent": worn, "read_bytes": hours * 9 * GIB, "written_bytes": hours * 14 * GIB,
            "power_cycles": 31, "power_on_hours": hours, "unsafe_shutdowns": 4, "media_errors": 0}


def _smart_hdd(hours: int, temperature: int) -> dict:
    """Invent SMART readings for a hard disk.

    Args:
        hours: Power-on hours.
        temperature: Temperature in degrees Celsius.

    Returns:
        dict: The curated SMART counters.
    """
    return {"health": "PASSED", "temperature_c": temperature, "power_on_hours": hours, "power_cycles": 18,
            "reallocated_sectors": 0, "pending_sectors": 0, "uncorrectable_sectors": 0}


def _disk(path: str, kind: str, model: str, serial: str, size: int, used: str, smart: dict, *,
          boot: bool = False, osds: tuple[str, ...] = (), rpm: int | None = None) -> dict:
    """Build one curated disk record, as the collector stores it.

    Args:
        path: Device path, such as ``/dev/nvme0n1``.
        kind: ``nvme``, ``ssd``, or ``hdd``.
        model: Made-up model name.
        serial: Made-up serial number.
        size: Size in bytes.
        used: What Proxmox says uses it, such as ``ZFS``.
        smart: Its SMART readings.
        boot: Whether it is the boot disk.
        osds: Ceph OSDs on it.
        rpm: Spindle speed for a hard disk.

    Returns:
        dict: The disk record.
    """
    return {"path": path, "type": kind, "model": model, "serial": serial, "size": size, "health": "PASSED",
            "used": used, "rpm": rpm, "worn_percent": smart.get("worn_percent"), "osds": list(osds), "boot": boot,
            "smart": smart}


def _disks(name: str, index: int) -> list[dict]:
    """List a node's drives: a boot NVMe, Ceph NVMe on the rack servers, and
    the ZFS node's four hard disks.

    Args:
        name: The node's name.
        index: Its position, which varies wear and temperature.

    Returns:
        list[dict]: The disk records.
    """
    boot = _disk("/dev/nvme0n1", "nvme", "Example NVMe 500GB", f"DEMO{index}B0", 500 * 10 ** 9, "BIOS boot",
                 _smart_nvme(3 + index, 9000 + 400 * index, 38 + index), boot=True)
    if name in MINI_NODES:
        return [_disk("/dev/nvme0n1", "nvme", "Example NVMe 1TB", f"DEMO{index}M0", 10 ** 12, "ZFS",
                      _smart_nvme(11 + 4 * index, 7000 + 300 * index, 44 + index), boot=True)]
    rack = RACK_NODES.index(name)
    disks = [boot] + [
        _disk(f"/dev/nvme{slot}n1", "nvme", "Example NVMe 2TB", f"DEMO{index}N{slot}", 2 * 10 ** 12, "LVM",
              # One drive past half its rated life shows an amber wear lamp.
              _smart_nvme(56 if (rack, slot) == (1, 2) else 8 + 3 * slot, 12000 + 500 * slot, 41 + slot),
              osds=(str(rack * 2 + slot - 1),))
        for slot in (1, 2)
    ]
    if name == "pve3":
        disks += [_disk(f"/dev/sd{letter}", "hdd", "Example 8TB NAS HDD", f"DEMOHDD{number}", 8 * 10 ** 12, "ZFS",
                        _smart_hdd(21000 + 900 * number, 34 + number), rpm=7200)
                  for number, letter in enumerate("abcd", start=1)]
    return disks


def _zfs(name: str) -> list[dict]:
    """List a node's ZFS pools: a raidz1 tank of four hard disks on the ZFS
    node, rpool on the minis.

    Args:
        name: The node's name.

    Returns:
        list[dict]: The curated pools, with their device trees.
    """
    if name == "pve3":
        size = 4 * 8 * 10 ** 12
        return [{"name": "tank", "health": "ONLINE", "size": size, "alloc": int(size * 0.47), "free": int(size * 0.53),
                 "frag": 6, "dedup": 1.0, "state": "ONLINE", "errors": "No known data errors",
                 "scan": "scrub repaired 0B in 07:12:44 with 0 errors", "status": "", "action": "",
                 "vdevs": [{"name": "tank", "state": "ONLINE", "depth": 0, "leaf": False, "read": 0, "write": 0, "cksum": 0},
                           {"name": "raidz1-0", "state": "ONLINE", "depth": 1, "leaf": False, "read": 0, "write": 0, "cksum": 0}]
                 + [{"name": f"sd{letter}", "state": "ONLINE", "depth": 2, "leaf": True, "read": 0, "write": 0, "cksum": 0}
                    for letter in "abcd"]}]
    if name in MINI_NODES:
        size = 10 ** 12
        return [{"name": "rpool", "health": "ONLINE", "size": size, "alloc": int(size * 0.31), "free": int(size * 0.69),
                 "frag": 3, "dedup": 1.0, "state": "ONLINE", "errors": "No known data errors",
                 "scan": "scrub repaired 0B in 00:04:10 with 0 errors", "status": "", "action": "",
                 "vdevs": [{"name": "rpool", "state": "ONLINE", "depth": 0, "leaf": False, "read": 0, "write": 0, "cksum": 0},
                           {"name": "nvme0n1", "state": "ONLINE", "depth": 1, "leaf": True, "read": 0, "write": 0, "cksum": 0}]}]
    return []


def _storage(name: str) -> list[dict]:
    """List a node's storage, as Proxmox reports it.

    Args:
        name: The node's name.

    Returns:
        list[dict]: One row per storage, with its type, content, and use.
    """
    rows = [{"storage": "local", "type": "dir", "shared": False, "status": "available",
             "content": ["iso", "vztmpl", "backup"], "used": 18 * GIB, "total": 94 * GIB}]
    if name in RACK_NODES:
        rows.append({"storage": "local-lvm", "type": "lvmthin", "shared": False, "status": "available",
                     "content": ["images", "rootdir"], "used": 120 * GIB, "total": 340 * GIB})
        rows.append({"storage": "vm-pool", "type": "rbd", "shared": True, "status": "available",
                     "content": ["images", "rootdir"], "used": int(1.9 * TIB), "total": int(3.6 * TIB)})
    else:
        rows.append({"storage": "local-zfs", "type": "zfspool", "shared": False, "status": "available",
                     "content": ["images", "rootdir"], "used": 290 * GIB, "total": 900 * GIB})
    if name == "pve3":
        rows.append({"storage": "tank", "type": "zfspool", "shared": False, "status": "available",
                     "content": ["images", "backup"], "used": int(13.5 * TIB), "total": int(28.5 * TIB)})
    return rows


def _guests_on(name: str, now: datetime) -> tuple[list[dict], dict, dict]:
    """Build a node's guests, their guest-agent reports, and their histories.

    Args:
        name: The node's name.
        now: The sample time.

    Returns:
        tuple[list[dict], dict, dict]: The guest records, the agent reports
            by VM ID, and the histories by VM ID.
    """
    guests, agents, histories = [], {}, {}
    for vmid, guest_name, node, kind, vcpus, memory, disk, running, os_name in GUESTS:
        if node != name:
            continue
        mac = f"02:00:00:00:{vmid // 256:02x}:{vmid % 256:02x}"
        guests.append({
            "vmid": vmid, "kind": kind, "name": guest_name, "host": name, "macs": [mac],
            "bridges": {mac: "vmbr0"}, "vlans": {}, "vcpus": vcpus, "memory": memory * GIB, "disk": disk * GIB,
            "mem_used": int(memory * GIB * (0.55 if running else 0)), "cpu": 0.08 + (vmid % 7) / 40 if running else 0.0,
            "uptime": (9 * DAY + vmid * 600) if running else 0, "template": vmid >= 9000, "onboot": running,
            "tags": ["k3s"] if guest_name.startswith("k3s") else [],
            "ostype": "l26" if "Windows" not in os_name else "win11",
            "status": "running" if running else "stopped",
        })
        if running and kind == "vm":
            agents[str(vmid)] = {
                "os": {"name": os_name, "id": os_name.split()[0].lower(), "version": os_name.split()[-1], "kernel": "6.8.0"},
                "addresses": [{"interface": "eth0", "mac": mac, "address": f"192.0.2.{vmid - 50}", "prefix": 25, "family": "ipv4"}],
                "filesystems": [{"mount": "/", "type": "ext4", "total": disk * GIB, "used": int(disk * GIB * 0.4)}],
            }
        if running:
            histories[str(vmid)] = _guest_history(now, vmid % 11, 0.05 + (vmid % 5) / 20)
    return guests, agents, histories


def node_telemetry(name: str, index: int, now: datetime) -> dict:
    """Build the ``proxmox_node`` telemetry for one node.

    Args:
        name: The node's name.
        index: Its position in the cluster.
        now: The sample time.

    Returns:
        dict: The node's readings, history, disks, pools, and guests.
    """
    rack = name in RACK_NODES
    threads = 32 if rack else 8
    maxmem = (128 if rack else 32) * GIB
    load = 0.28 + 0.08 * index if rack else 0.18 + 0.05 * index
    memory = 0.46 + 0.05 * index if rack else 0.52
    history = _history(now, index, load, memory)
    latest = history[-1]
    _, agents, guest_history = _guests_on(name, now)
    pending = [{"package": "proxmox-kernel-6.8", "installed": "6.8.12-4", "available": "6.8.12-5"},
               {"package": "pve-manager", "installed": "8.3.1", "available": "8.3.2"},
               {"package": "libpve-common-perl", "installed": "8.2.9", "available": "8.3.0"}]
    reboot = name == "pve2"
    return {
        "status": "online", "cpu": latest["cpu"], "maxcpu": threads, "mem": int(maxmem * latest["mem"]), "maxmem": maxmem,
        "rootfs_used": 14 * GIB + index * GIB, "rootfs_total": 94 * GIB, "uptime": (21 - 3 * index) * DAY,
        "storage": _storage(name), "errors": {},
        "cpu_model": "Example Xeon 16-core @ 2.6GHz" if rack else "Example Mobile 4-core @ 3.1GHz",
        "sockets": 1, "cores": threads // 2, "threads": threads,
        "loadavg": [round(threads * latest["cpu"] * f, 2) for f in (1.0, 0.92, 0.85)],
        "swap_used": 0, "swap_total": 8 * GIB, "pve_version": "8.3.1",
        "history": history, "zfs": _zfs(name), "disks": _disks(name, index),
        "pci": ([{"kind": "network", "slot": "0000:41:00.0", "name": "Example 10GbE SFP+ adapter", "vendor": "Example", "board": None},
                 {"kind": "storage", "slot": "0000:01:00.0", "name": "Example NVMe controller", "vendor": "Example", "board": None}]
                + ([{"kind": "display", "slot": "0000:81:00.0", "name": "Example GPU 16GB", "vendor": "Example", "board": None}]
                   if name == "pve3" else [])) if rack else
               [{"kind": "network", "slot": "0000:02:00.0", "name": "Example 2.5GbE controller", "vendor": "Example", "board": None}],
        "certificates": [
            {"file": "pve-root-ca.pem", "not_after": int((now + timedelta(days=3100)).timestamp()), "issuer": "Proxmox Virtual Environment"},
            {"file": "pve-ssl.pem", "not_after": int((now + timedelta(days=430 - 40 * index)).timestamp()), "issuer": "Proxmox Virtual Environment"},
        ],
        "hardware_sampled_at": now.isoformat(),
        "services": [{"name": service, "state": "running", "enabled": True}
                     for service in ("pveproxy", "pvedaemon", "pvestatd", "pve-cluster", "corosync", "chronyd")
                     + (("ceph-mon", "ceph-mgr", "ceph-osd") if rack else ())]
                    + [{"name": "postfix", "state": "dead", "enabled": False}],
        "versions": {"upgradable": pending if index % 2 else pending[2:], "running_kernel": "6.8.12-4-pve",
                     "manager_running": "8.3.1", "manager_installed": "8.3.1", "manager_available": "8.3.2",
                     "kernel_installed": "6.8.12-5-pve" if reboot else "6.8.12-4-pve", "reboot_needed": reboot,
                     "manager_restart_needed": False},
        "guest_agent": agents, "guest_history": guest_history,
        "available": True, "sampled_at": now.isoformat(), "live_at": now.isoformat(),
    }


def clients_telemetry(name: str, now: datetime) -> dict:
    """Build the ``proxmox_clients`` telemetry (guests) for one node.

    Args:
        name: The node's name.
        now: The sample time.

    Returns:
        dict: The guest list, available and fresh.
    """
    guests, _, _ = _guests_on(name, now)
    return {"available": True, "sampled_at": now.isoformat(), "guests": guests, "live_at": now.isoformat()}


def cluster_telemetry(now: datetime) -> dict:
    """Build the shared ``proxmox_cluster`` telemetry: quorum, tasks,
    storage, and Ceph.

    Args:
        now: The sample time.

    Returns:
        dict: The cluster record copied to every node.
    """
    start = int(now.timestamp())
    osds = []
    for rack, node in enumerate(RACK_NODES):
        for slot in (1, 2):
            osd_id = rack * 2 + slot - 1
            used = 0.48 + 0.03 * osd_id
            osds.append({"id": osd_id, "name": f"osd.{osd_id}", "host": node, "status": "up", "in": True,
                         "device_class": "nvme", "percent_used": round(used * 100, 1), "total": 2 * 10 ** 12,
                         "used": int(2 * 10 ** 12 * used), "pgs": 43 + osd_id, "apply_latency_ms": 2 + osd_id % 3,
                         "commit_latency_ms": 2 + osd_id % 3})
    total = 12 * 10 ** 12
    return {
        "tasks": {"hours": 24, "ok": 46, "warning": 1, "error": 0, "running": 0,
                  "by_type": {"qmstart": {"ok": 6, "warning": 0, "error": 0, "running": 0},
                              "vzdump": {"ok": 24, "warning": 1, "error": 0, "running": 0},
                              "aptupdate": {"ok": 6, "warning": 0, "error": 0, "running": 0},
                              "qmigrate": {"ok": 2, "warning": 0, "error": 0, "running": 0},
                              "vncproxy": {"ok": 8, "warning": 0, "error": 0, "running": 0}},
                  "failures": [],
                  "recent": [{"type": kind, "node": node, "id": vmid, "start": start - offset, "end": start - offset + 40,
                              "outcome": "OK"}
                             for kind, node, vmid, offset in (("vzdump", "pve3", "104", 3600), ("qmigrate", "pve1", "101", 9000),
                                                              ("aptupdate", "pve2", None, 14400), ("qmstart", "mini3", "110", 30000))]},
        "not_backed_up": [110], "name": "lab", "quorate": True, "node_count": 6,
        "storage_config": [
            {"storage": "local", "type": "dir", "shared": False, "disabled": False, "nodes": [], "content": ["iso", "vztmpl", "backup"], "pool": None},
            {"storage": "vm-pool", "type": "rbd", "shared": True, "disabled": False, "nodes": list(RACK_NODES), "content": ["images", "rootdir"], "pool": "vm-pool"},
            {"storage": "tank", "type": "zfspool", "shared": False, "disabled": False, "nodes": ["pve3"], "content": ["images", "backup"], "pool": "tank"},
        ],
        "ceph": {
            "health": "HEALTH_OK", "checks": [], "osds": {"total": 6, "up": 6, "in": 6},
            "monitors": {"total": 3, "in_quorum": 3}, "bytes_total": total, "bytes_used": int(total * 0.52),
            "bytes_avail": int(total * 0.48), "data_bytes": int(total * 0.17), "pgs": 129,
            "pg_states": [{"state": "active+clean", "count": 129}], "pool_count": 2,
            "read_bytes_sec": 3_400_000, "write_bytes_sec": 1_900_000, "osd_list": osds,
            "pool_list": [
                {"name": "vm-pool", "id": 2, "type": "replicated", "size": 3, "min_size": 2, "pg_num": 128, "pg_target": 128,
                 "autoscale": "on", "rule": "replicated_rule", "applications": ["rbd"], "stored": int(1.9 * TIB),
                 "raw_used": int(5.7 * TIB), "percent_used": 51.2, "class_capacity": total},
                {"name": ".mgr", "id": 1, "type": "replicated", "size": 3, "min_size": 2, "pg_num": 1, "pg_target": 1,
                 "autoscale": "on", "rule": "replicated_rule", "applications": ["mgr"], "stored": 18 * 1024 ** 2,
                 "raw_used": 54 * 1024 ** 2, "percent_used": 0.0, "class_capacity": total},
            ],
            "flags": [],
        },
        "uses_ceph": True, "errors": {}, "available": True, "sampled_at": now.isoformat(), "live_at": now.isoformat(),
    }


def network_snapshot(now: datetime, devices: list[dict], clients: list[dict]) -> dict:
    """The gateway's ``unifi_network`` snapshot.

    Args:
        now: Sample time.
        devices: ``(id, name, model, mac, ip, features, ports, uplink)`` rows as dicts.
        clients: Client rows already shaped for the snapshot.

    Returns:
        dict: The snapshot: networks, zones, policies, Wi-Fi, devices, and
            clients.
    """
    networks = [
        {"id": "net-default", "name": "Default", "vlan": 1, "gateway": "192.0.2.1", "prefix": 25, "enabled": True,
         "default": True, "internet": True, "isolated": False, "mdns": True, "management": "gateway", "origin": "USER_DEFINED",
         "zone_id": "zone-internal",
         "dhcp": {"mode": "server", "start": "192.0.2.20", "stop": "192.0.2.120", "lease_seconds": 86400,
                  "dns": ["192.0.2.56"], "domain": "home.example"}},
        {"id": "net-iot", "name": "IoT", "vlan": 20, "gateway": "192.0.2.129", "prefix": 26, "enabled": True,
         "default": False, "internet": True, "isolated": True, "mdns": True, "management": "gateway", "origin": "USER_DEFINED",
         "zone_id": "zone-iot",
         "dhcp": {"mode": "server", "start": "192.0.2.140", "stop": "192.0.2.190", "lease_seconds": 86400,
                  "dns": ["192.0.2.56"], "domain": "iot.example"}},
        {"id": "net-guest", "name": "Guest", "vlan": 30, "gateway": "192.0.2.193", "prefix": 26, "enabled": True,
         "default": False, "internet": True, "isolated": True, "mdns": False, "management": "gateway", "origin": "USER_DEFINED",
         "zone_id": "zone-hotspot",
         "dhcp": {"mode": "server", "start": "192.0.2.200", "stop": "192.0.2.250", "lease_seconds": 7200,
                  "dns": ["192.0.2.193"], "domain": "guest.example"}},
    ]
    zones = [{"id": "zone-internal", "name": "Internal", "network_ids": ["net-default"], "origin": "SYSTEM_DEFINED"},
             {"id": "zone-iot", "name": "IoT", "network_ids": ["net-iot"], "origin": "USER_DEFINED"},
             {"id": "zone-hotspot", "name": "Hotspot", "network_ids": ["net-guest"], "origin": "SYSTEM_DEFINED"},
             {"id": "zone-external", "name": "External", "network_ids": [], "origin": "SYSTEM_DEFINED"}]

    def policy(index: int, name: str, action: str, source: str, destination: str) -> dict:
        return {"id": f"policy-{index}", "index": index, "name": name, "action": action, "enabled": True, "logging": False,
                "ip_version": "both", "origin": "USER_DEFINED", "protocol": None, "protocol_negate": False, "states": None,
                "source": {"zone": source, "kind": "any", "raw_kind": "ANY", "negate": False, "ports": [], "values": []},
                "destination": {"zone": destination}}

    return {
        "available": True, "sampled_at": now.isoformat(), "errors": {},
        "site": {"id": "default", "name": "Default"},
        "health": [{"subsystem": "wan", "status": "ok", "adopted": 1, "disconnected": 0, "users": 0, "guests": 0, "rx_bps": 2.4e7, "tx_bps": 4.1e6},
                   {"subsystem": "lan", "status": "ok", "adopted": 3, "disconnected": 0, "users": 9, "guests": 0, "rx_bps": 1.2e7, "tx_bps": 9.0e6},
                   {"subsystem": "wlan", "status": "ok", "adopted": 2, "disconnected": 0, "users": 9, "guests": 1, "rx_bps": 3.1e6, "tx_bps": 1.4e6},
                   {"subsystem": "vpn", "status": "ok", "adopted": 0, "disconnected": 0, "users": 0, "guests": 0, "rx_bps": 6.5e5, "tx_bps": 2.5e5}],
        "networks": networks, "zones": zones,
        "policies": [policy(1, "IoT to Internal", "BLOCK", "zone-iot", "zone-internal"),
                     policy(2, "Guest to Internal", "BLOCK", "zone-hotspot", "zone-internal"),
                     policy(3, "Internal to IoT", "ALLOW", "zone-internal", "zone-iot")],
        "wifi": [{"name": "Example WiFi", "network": "Default", "security": "wpa2-wpa3", "type": "standard", "enabled": True,
                  "bands": [2.4, 5.0], "devices": 2},
                 {"name": "Example IoT", "network": "IoT", "security": "wpa2", "type": "iot", "enabled": True,
                  "bands": [2.4], "devices": 2},
                 {"name": "Example Guest", "network": "Guest", "security": "open", "type": "hotspot", "enabled": True,
                  "bands": [2.4, 5.0], "devices": 2}],
        "neighbours": [{"band": "2.4", "channel": 1, "count": 4, "strongest": -61}, {"band": "2.4", "channel": 11, "count": 3, "strongest": -70},
                       {"band": "5", "channel": 44, "count": 2, "strongest": -74}, {"band": "5", "channel": 149, "count": 1, "strongest": -81}],
        "lists": [{"name": "Lab servers", "type": "address", "items": ["192.0.2.48/29"]}],
        "wans": [{"id": "wan1", "name": "WAN1"}, {"id": "wan2", "name": "WAN2"}],
        "devices": devices, "clients": clients,
    }
