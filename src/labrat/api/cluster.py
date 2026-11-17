# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Serve the Cluster page's Proxmox nodes, guests, networks, and storage.

Everything comes from telemetry the collector already stored on Proxmox node
observations (``proxmox_node``, ``proxmox_cluster``, ``proxmox_clients``)
and from their logical network interfaces. Nothing here contacts Proxmox.
"""

from datetime import datetime, timezone
import json
import re
from typing import Any

from flask import Blueprint, jsonify
from sqlalchemy import select

from labrat.auth.session import api_login_required, session_factory
from labrat.coerce import json_object
from labrat.discovery.models import (
    DiscoveredDevice, DiscoveredNetworkInterface, DiscoveredPort, DiscoverySource,
)

cluster_bp = Blueprint("cluster_api", __name__, url_prefix="/api/v1")

#: Number words, so nodes named "one", "two", ... sort by value.
_NUMBER_WORDS = {
    word: value for value, word in enumerate((
        "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
        "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
        "eighteen", "nineteen", "twenty"))
}
_NUMBER_WORDS.update({"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90})


def _spelled_number(name: str) -> int | None:
    """Read a name that is a number, spelled out or in digits.

    Args:
        name: A node name such as ``"three"``, ``"twenty-one"``, or ``"7"``.

    Returns:
        int | None: Its value, or None when the name is not a number.
    """
    text = name.strip().lower()
    if text.isdigit():
        return int(text)
    parts = re.split(r"[-_ ]", text)
    if not parts or any(part not in _NUMBER_WORDS for part in parts):
        return None
    if len(parts) == 2 and _NUMBER_WORDS[parts[0]] >= 20 and _NUMBER_WORDS[parts[1]] < 10:
        return _NUMBER_WORDS[parts[0]] + _NUMBER_WORDS[parts[1]]
    return _NUMBER_WORDS[parts[0]] if len(parts) == 1 else None


def node_order(name: str) -> tuple:
    """Sort key for node names: numbers (spelled or digits) first by value,
    then everything else in natural order (``gpu2`` before ``gpu10``).

    Args:
        name: Node name.

    Returns:
        tuple: A sort key.
    """
    value = _spelled_number(name)
    if value is not None:
        return (0, value, "")
    pieces = re.split(r"(\d+)", name.lower())
    return (1, 0, tuple((0, int(piece), "") if piece.isdigit() else (1, 0, piece) for piece in pieces if piece))


#: A VLAN sub-interface named ``<parent>.<id>``.
_DOTTED_VLAN = re.compile(r"^[^.]+\.(\d{1,4})$")


def _sampled(value: Any) -> datetime | None:
    """Parse a stored ISO timestamp.

    Args:
        value: Stored ``sampled_at`` text.

    Returns:
        datetime | None: The aware timestamp, or None.
    """
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _bridge_networks(interfaces: list[DiscoveredNetworkInterface]) -> dict[str, dict]:
    """Describe each bridge on one node with the VLAN it carries.

    A bridge carries one VLAN when a member is a VLAN interface (``vlan200``
    with an ID, or ``bond0.200``). A VLAN-aware bridge is a trunk: guests
    choose their VLAN with a tag. Otherwise the bridge is untagged.

    Args:
        interfaces: The node's logical interfaces.

    Returns:
        dict[str, dict]: Bridge name to ``vlan``, ``vlan_aware``, ``vids``,
            ``cidr``, ``members``, ``uplink``, and ``active``.
    """
    by_name = {item.name: item for item in interfaces}
    bridges = {}
    for item in interfaces:
        if item.kind != "bridge":
            continue
        details = json_object(item.details_json)
        try:
            members = [name for name in json.loads(item.members_json or "[]") if isinstance(name, str)]
        except ValueError:
            members = []
        vlan, uplink = None, None
        for member in members:
            other = by_name.get(member)
            if other is not None and other.kind == "vlan" and other.vlan_id:
                vlan, uplink = other.vlan_id, other.parent_name
            elif (match := _DOTTED_VLAN.match(member)) and 1 <= int(match.group(1)) <= 4094:
                vlan, uplink = int(match.group(1)), member.split(".", 1)[0]
            else:
                uplink = uplink or member
        aware = details.get("bridge_vlan_aware") in (1, True, "1", "yes")
        bridges[item.name] = {
            "vlan": vlan, "vlan_aware": aware,
            "vids": str(details["bridge_vids"]) if aware and details.get("bridge_vids") else None,
            "cidr": details.get("cidr") if isinstance(details.get("cidr"), str) else None,
            "members": members, "uplink": uplink, "active": item.active,
        }
    return bridges


def _guest_nics(guest: dict, bridges: dict[str, dict]) -> list[dict]:
    """Resolve each guest NIC to its bridge and VLAN.

    Args:
        guest: One stored guest record.
        bridges: The hosting node's bridges from :func:`_bridge_networks`.

    Returns:
        list[dict]: ``mac``, ``bridge``, ``vlan``, and ``vlan_source``
            (``tag``, ``bridge``, or None for an untagged NIC).
    """
    nics = []
    vlans = guest.get("vlans") if isinstance(guest.get("vlans"), dict) else {}
    named = guest.get("bridges") if isinstance(guest.get("bridges"), dict) else {}
    for mac in guest.get("macs") or []:
        bridge = named.get(mac)
        tag = vlans.get(mac)
        carried = (bridges.get(bridge) or {}).get("vlan") if bridge else None
        vlan, source = (tag, "tag") if tag else (carried, "bridge") if carried else (None, None)
        nics.append({"mac": mac, "bridge": bridge, "vlan": vlan, "vlan_source": source})
    return nics


def _source_view(source: DiscoverySource, hosts: list[DiscoveredDevice],
                 interfaces: dict[int, list[DiscoveredNetworkInterface]],
                 ports: dict[int, list[DiscoveredPort]] | None = None) -> dict:
    """Build one Proxmox source's cluster view.

    Args:
        source: The Proxmox discovery source.
        hosts: Its node observations.
        interfaces: Logical interfaces by observation ID.
        ports: Physical NIC ports by observation ID.

    Returns:
        dict: Cluster facts, nodes, guests, networks, and storage.
    """
    nodes, guests, cluster, cluster_at = [], [], None, None
    bridges_by_node: dict[str, dict[str, dict]] = {}
    for host in sorted(hosts, key=lambda item: node_order(item.name)):
        details = json_object(host.details_json)
        telemetry = details.get("proxmox_node") if isinstance(details.get("proxmox_node"), dict) else {}
        shared = details.get("proxmox_cluster")
        if isinstance(shared, dict) and (cluster is None or (_sampled(shared.get("sampled_at")) or datetime.min.replace(
                tzinfo=timezone.utc)) > (cluster_at or datetime.min.replace(tzinfo=timezone.utc))):
            cluster, cluster_at = shared, _sampled(shared.get("sampled_at"))
        bridges = _bridge_networks(interfaces.get(host.id, []))
        bridges_by_node[host.remote_id] = bridges
        clients = details.get("proxmox_clients") if isinstance(details.get("proxmox_clients"), dict) else {}
        agent = telemetry.get("guest_agent") if isinstance(telemetry.get("guest_agent"), dict) else {}
        history = telemetry.get("guest_history") if isinstance(telemetry.get("guest_history"), dict) else {}
        for guest in clients.get("guests") or []:
            if isinstance(guest, dict):
                guests.append({"agent": agent.get(str(guest.get("vmid"))), "history": history.get(str(guest.get("vmid"))), **{key: guest.get(key) for key in (
                    "vmid", "kind", "name", "status", "template", "vcpus", "memory", "disk",
                    "mem_used", "cpu", "uptime", "onboot", "tags", "ostype")}, "node": host.remote_id,
                    "nics": _guest_nics(guest, bridges)})
        nodes.append({
            "id": host.id, "name": host.name, "remote_id": host.remote_id,
            "status": telemetry.get("status") or details.get("status"),
            "guests_sampled_at": clients.get("sampled_at"),
            **{key: telemetry.get(key) for key in (
                "cpu", "maxcpu", "mem", "maxmem", "rootfs_used", "rootfs_total", "uptime",
                "cpu_model", "sockets", "cores", "threads", "loadavg", "swap_used", "swap_total",
                "pve_version", "history", "storage", "zfs", "disks", "pci", "hardware_sampled_at",
                "certificates", "services", "versions",
                "errors", "available", "sampled_at", "live_at")},
            "nics": [
                {"name": port.name, "link_up": port.link_up, "speed_mbps": port.speed_mbps or port.max_speed_mbps,
                 "medium": port.medium, "connector": port.connector_type}
                for port in sorted((ports or {}).get(host.id, []), key=lambda port: port.name)
            ],
            "bonds": [
                {"name": item.name, "mode": item.mode, "members": json.loads(item.members_json or "[]")}
                for item in interfaces.get(host.id, []) if item.kind == "bond"
            ],
        })
    networks: dict[str, dict] = {}
    for node, bridges in bridges_by_node.items():
        for name, bridge in bridges.items():
            row = networks.setdefault(name, {"bridge": name, "nodes": {}, "guests": 0})
            row["nodes"][node] = bridge
    for guest in guests:
        for nic in guest["nics"]:
            if nic["bridge"] in networks:
                networks[nic["bridge"]]["guests"] += 1
    for row in networks.values():
        vlans = {bridge["vlan"] for bridge in row["nodes"].values() if bridge["vlan"]}
        row["vlan"] = vlans.pop() if len(vlans) == 1 else None
        row["vlan_conflict"] = len(vlans) > 0
        row["vlan_aware"] = any(bridge["vlan_aware"] for bridge in row["nodes"].values())
    return {
        "id": source.id, "name": source.name,
        "last_success_at": source.last_success_at.isoformat() if source.last_success_at else None,
        "cluster": cluster, "nodes": nodes,
        "guests": sorted(guests, key=lambda guest: guest.get("vmid") or 0),
        "networks": sorted(networks.values(), key=lambda row: (row["vlan"] is None, row["vlan"] or 0, row["bridge"])),
    }


@cluster_bp.get("/cluster")
@api_login_required
def cluster() -> Any:
    """Return every Proxmox source's nodes, guests, networks, and storage.

    Returns:
        Response: ``{"sources": [...]}``, one entry per enabled Proxmox source,
            built from stored telemetry only.
    """
    with session_factory()() as db:
        sources = list(db.scalars(select(DiscoverySource).where(
            DiscoverySource.kind == "proxmox", DiscoverySource.enabled.is_(True)).order_by(DiscoverySource.name)))
        views = []
        for source in sources:
            hosts = [host for host in db.scalars(select(DiscoveredDevice).where(
                DiscoveredDevice.source_id == source.id)) if json_object(host.details_json).get("platform") == "proxmox"]
            interfaces: dict[int, list] = {}
            ports: dict[int, list] = {}
            if hosts:
                for port in db.scalars(select(DiscoveredPort).where(
                        DiscoveredPort.discovered_device_id.in_([host.id for host in hosts]))):
                    ports.setdefault(port.discovered_device_id, []).append(port)
                for item in db.scalars(select(DiscoveredNetworkInterface).where(
                        DiscoveredNetworkInterface.discovered_device_id.in_([host.id for host in hosts]))):
                    interfaces.setdefault(item.discovered_device_id, []).append(item)
            views.append(_source_view(source, hosts, interfaces, ports))
    return jsonify(sources=views)
