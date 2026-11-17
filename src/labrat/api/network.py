# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Serve the Networks page: UniFi networks, zones, firewall paths, Wi-Fi, devices, clients.

Built from the ``unifi_network`` telemetry the collector stores on each
UniFi gateway observation; nothing here contacts a controller. Besides the
curated snapshot it derives, for every pair of firewall zones, the
effective verdict (the first broad rule in index order) and the narrower
rules before it (the exceptions), and it lists likely DNS sinkholes such as
Pi-hole from the evidence available.
"""

import ipaddress
import re
from typing import Any

from flask import Blueprint, jsonify
from sqlalchemy import select

from labrat.auth.session import api_login_required, session_factory
from labrat.coerce import json_object
from labrat.discovery.models import DiscoveredDevice, DiscoverySource

network_bp = Blueprint("network_api", __name__, url_prefix="/api/v1")

#: Names that suggest a DNS sinkhole (Pi-hole and its common spellings).
_PIHOLE_NAME = re.compile(r"pi[-_ .]?hole", re.IGNORECASE)


def _narrow(policy: dict) -> bool:
    """Whether a policy matches less than all traffic between its zones.

    Args:
        policy: A curated policy.

    Returns:
        bool: True when it filters by address, port, network, list, or
            protocol, or only certain connection states.
    """
    for side in ("source", "destination"):
        endpoint = policy.get(side) or {}
        if endpoint.get("kind") or endpoint.get("values") or endpoint.get("ports"):
            return True
    return bool(policy.get("protocol") or policy.get("states"))


def zone_paths(zones: list[dict], policies: list[dict]) -> list[dict]:
    """Work out each zone pair's effective verdict and its exceptions.

    Policies are taken per (source zone, destination zone) in ascending
    ``index``. The first enabled policy that matches all traffic between the
    pair decides the default; enabled narrower policies before it are
    exceptions. A pair with no broad policy has no known default.

    Args:
        zones: Curated zones.
        policies: Curated policies.

    Returns:
        list[dict]: One row per pair that has any policy: ``source``,
            ``destination``, ``verdict`` (allow, block, or unknown),
            ``decided_by`` (policy name), and ``exceptions`` (name, action,
            and what they match).
    """
    by_pair: dict[tuple[str, str], list[dict]] = {}
    for policy in policies:
        if not policy.get("enabled"):
            continue
        pair = ((policy.get("source") or {}).get("zone"), (policy.get("destination") or {}).get("zone"))
        if pair[0] and pair[1]:
            by_pair.setdefault(pair, []).append(policy)
    rows = []
    for (source, destination), items in by_pair.items():
        items.sort(key=lambda item: item.get("index") if item.get("index") is not None else 1 << 40)
        verdict, decided_by, exceptions = "unknown", None, []
        for policy in items:
            action = "allow" if policy.get("action") == "ALLOW" else "block" if policy.get("action") in ("BLOCK", "REJECT") else "unknown"
            if _narrow(policy):
                exceptions.append({
                    "name": policy.get("name"), "action": action, "origin": policy.get("origin"),
                    "source": (policy.get("source") or {}).get("values") or [],
                    "destination": (policy.get("destination") or {}).get("values") or [],
                    "ports": ((policy.get("destination") or {}).get("ports") or []) + ((policy.get("source") or {}).get("ports") or []),
                    "protocol": policy.get("protocol"), "states": policy.get("states"),
                    "negate": bool((policy.get("source") or {}).get("negate") or (policy.get("destination") or {}).get("negate")),
                })
                continue
            verdict, decided_by = action, policy.get("name")
            break
        rows.append({"source": source, "destination": destination, "verdict": verdict,
                     "decided_by": decided_by, "exceptions": exceptions[:40]})
    return rows


def _cidr(network: dict) -> str | None:
    """Give a network's subnet in CIDR form.

    Args:
        network: A curated network with ``gateway`` and ``prefix``.

    Returns:
        str | None: The subnet, such as ``192.0.2.0/24``, or None when the
            gateway or prefix is missing or invalid.
    """
    try:
        return str(ipaddress.ip_interface(f"{network['gateway']}/{network['prefix']}").network)
    except (KeyError, TypeError, ValueError):
        return None


def dns_sinkholes(snapshot: dict, guests: list[dict]) -> list[dict]:
    """List likely DNS sinkholes (such as Pi-hole) and the evidence for each.

    Evidence: a DHCP DNS server that is not the network's own gateway;
    a client or Proxmox guest whose name looks like Pi-hole. Candidates
    are grouped by address.

    Args:
        snapshot: The curated UniFi snapshot.
        guests: Proxmox guests (name, addresses) from the cluster, if any.

    Returns:
        list[dict]: ``ip``, ``names``, ``evidence`` (sentences),
            ``networks`` (that hand it out as DNS), ``client`` (the UniFi
            client at that address, if seen), and ``guest`` (a matching
            Proxmox guest name).
    """
    gateways = {network.get("gateway") for network in snapshot.get("networks", [])}
    found: dict[str, dict] = {}

    def entry(ip: str) -> dict:
        return found.setdefault(ip, {"ip": ip, "names": [], "evidence": [], "networks": [], "client": None, "guest": None})

    for network in snapshot.get("networks", []):
        for server in ((network.get("dhcp") or {}).get("dns") or []):
            try:
                address = ipaddress.ip_address(server)
            except ValueError:
                continue
            if server in gateways or not address.is_private:
                continue
            item = entry(server)
            item["networks"].append(network.get("name"))
    for item in found.values():
        item["evidence"].append(f"Handed out as DNS by DHCP on {', '.join(item['networks'])}.")
    clients = {client.get("ip"): client for client in snapshot.get("clients", []) if client.get("ip")}
    for client in snapshot.get("clients", []):
        names = [client.get("name"), client.get("hostname")]
        if client.get("ip") and any(isinstance(name, str) and _PIHOLE_NAME.search(name) for name in names):
            entry(client["ip"])["evidence"].append(f"A UniFi client named {client.get('name') or client.get('hostname')} is at this address.")
    for guest in guests:
        if not isinstance(guest.get("name"), str) or not _PIHOLE_NAME.search(guest["name"]):
            continue
        for address in guest.get("addresses") or []:
            if address in found or address in clients:
                item = entry(address)
                item["guest"] = guest["name"]
                item["evidence"].append(f"Proxmox guest {guest['name']} has this address.")
        if not any(address in found for address in guest.get("addresses") or []) and guest.get("addresses"):
            item = entry(guest["addresses"][0])
            item["guest"] = guest["name"]
            item["evidence"].append(f"Proxmox guest {guest['name']} is named like Pi-hole.")
    for ip, item in found.items():
        client = clients.get(ip)
        if client:
            item["client"] = {key: client.get(key) for key in ("name", "hostname", "mac", "type", "network", "uplink_device")}
            item["names"] = sorted({name for name in (client.get("name"), client.get("hostname"), item.get("guest")) if name})
        elif item.get("guest"):
            item["names"] = [item["guest"]]
        item["likely_pihole"] = any(_PIHOLE_NAME.search(name) for name in item["names"]) or bool(item.get("guest"))
    return sorted(found.values(), key=lambda item: (not item["likely_pihole"], item["ip"]))


def _guests(db) -> list[dict]:
    """List Proxmox guests with their agent-reported addresses, for cross-reference.

    Args:
        db: An open database session.

    Returns:
        list[dict]: One entry per guest: its name, VM ID, node, and addresses.
    """
    guests = []
    for host in db.scalars(select(DiscoveredDevice)):
        details = json_object(host.details_json)
        if details.get("platform") != "proxmox":
            continue
        agents = (details.get("proxmox_node") or {}).get("guest_agent") or {}
        for guest in (details.get("proxmox_clients") or {}).get("guests") or []:
            if isinstance(guest, dict):
                agent = agents.get(str(guest.get("vmid"))) or {}
                guests.append({"name": guest.get("name"), "node": host.remote_id, "vmid": guest.get("vmid"),
                               "macs": guest.get("macs") or [],
                               "addresses": [item.get("address") for item in agent.get("addresses") or [] if item.get("address")]})
    return guests


def _piholes(db) -> dict[str, dict]:
    """Configured Pi-hole integrations by address, with their latest statistics.

    Args:
        db: Read session.

    Returns:
        dict[str, dict]: ``name``, ``source_id``, ``error``, and ``stats``
            (the ``pihole`` telemetry, if read yet) by IP address.
    """
    found: dict[str, dict] = {}
    for source in db.scalars(select(DiscoverySource).where(DiscoverySource.kind == "pihole")):
        for host in db.scalars(select(DiscoveredDevice).where(DiscoveredDevice.source_id == source.id)):
            details = json_object(host.details_json)
            identifiers = json_object(host.identifiers_json)
            addresses = identifiers.get("management_ips") or [identifiers.get("hostname")]
            for address in addresses:
                if isinstance(address, str):
                    found[address] = {"name": source.name, "source_id": source.id, "error": source.last_error,
                                      "stats": details.get("pihole") if isinstance(details.get("pihole"), dict) else None}
        if not any(item["source_id"] == source.id for item in found.values()):
            found[f"source:{source.id}"] = {"name": source.name, "source_id": source.id, "error": source.last_error, "stats": None}
    return found


@network_bp.get("/network")
@api_login_required
def network() -> Any:
    """Return each UniFi source's network snapshot with derived zone paths.

    Returns:
        Response: ``{"sources": [...]}``, each with the curated snapshot,
            ``cidr`` on networks, ``paths`` between zones, ``sinkholes``,
            and the Proxmox guests matched to clients by MAC.
    """
    with session_factory()() as db:
        guests = _guests(db)
        piholes = _piholes(db)
        by_mac = {mac.lower(): guest for guest in guests for mac in guest["macs"]}
        views = []
        sources = db.scalars(select(DiscoverySource).where(
            DiscoverySource.kind == "unifi", DiscoverySource.enabled.is_(True)).order_by(DiscoverySource.name))
        for source in sources:
            snapshot = None
            for host in db.scalars(select(DiscoveredDevice).where(DiscoveredDevice.source_id == source.id)):
                value = json_object(host.details_json).get("unifi_network")
                if isinstance(value, dict):
                    snapshot = value
                    break
            if snapshot is None:
                views.append({"id": source.id, "name": source.name, "snapshot": None})
                continue
            for item in snapshot.get("networks", []):
                item["cidr"] = _cidr(item)
            for client in snapshot.get("clients", []):
                guest = by_mac.get(str(client.get("mac") or "").lower())
                client["guest"] = {"name": guest["name"], "node": guest["node"], "vmid": guest["vmid"]} if guest else None
            views.append({
                "id": source.id, "name": source.name, "snapshot": snapshot,
                "paths": zone_paths(snapshot.get("zones", []), snapshot.get("policies", [])),
                "sinkholes": [{**item, "integration": piholes.get(item["ip"])} for item in dns_sinkholes(snapshot, guests)],
            })
    return jsonify(sources=views)
