# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Reconcile exact cross-source LLDP evidence into physical cable links.

Adapters retain their own source-scoped observations. This module joins the
UniFi LLDP management address and remote port ID to Proxmox node identities and
physical NIC names only when each side resolves uniquely.

Used by: api.observation_actions, collector.client_links, collector.runner,
collector.wireless_clients.
"""

from collections import Counter

import ipaddress
import json
import re
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from labrat.audit import record_audit_event
from labrat.coerce import json_object, mac_address
from labrat.discovery.models import (
    DiscoveredDevice, DiscoveredLink, DiscoveredPort, DiscoverySource,
)
from labrat.discovery.neighbor_matches import neighbor_key, saved_neighbor_matches

PROXMOX_LLDP_EVIDENCE = frozenset({"lldp-proxmox", "lldp-proxmox-name", "lldp-user-match"})


def _addresses(value: object) -> set[str]:
    """Normalize a list of IP addresses from a source observation.

    Args:
        value: An adapter-supplied sequence of IP address strings.

    Returns:
        set[str]: Canonical address strings, excluding malformed values.
    """
    if not isinstance(value, list):
        return set()
    found = set()
    for item in value:
        if not isinstance(item, str):
            continue
        try:
            found.add(str(ipaddress.ip_interface(item).ip))
        except ValueError:
            try:
                found.add(str(ipaddress.ip_address(item)))
            except ValueError:
                continue
    return found


def port_name(value: object) -> str | None:
    """Normalize an LLDP interface-name port ID for exact comparisons.

    Args:
        value: The remote LLDP port identifier.

    Returns:
        str | None: Case-folded interface name, or None for empty/non-string.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    name = value.strip()
    if name.lower().startswith("ifname:"):
        name = name.split(":", 1)[1].strip()
    return name.casefold() or None


def reconcile_proxmox_lldp_links(
    db: Session,
    unifi_source: DiscoverySource,
    proxmox_sources: Iterable[DiscoverySource],
    *,
    seen_at: datetime | None = None,
) -> int:
    """Refresh links whose two physical endpoints match cross-source LLDP.

    The UniFi source owns the LLDP observation, while the server endpoint is a
    physical NIC observed by Proxmox. A management address, or a chassis ID
    equal to a physical NIC MAC, that identifies one node permits a confirmed
    link when the port ID identifies one NIC on that node; if the address and
    chassis ID name different nodes, the neighbor is skipped. If neither
    identifies a node, an exact interface-name or
    MAC match may produce an inferred link only when it identifies one NIC
    across every discovered Proxmox node.

    Args:
        db: Open transaction session.
        unifi_source: Successfully collected UniFi source for this cycle.
        proxmox_sources: Successfully collected Proxmox sources for this cycle.
        seen_at: Observation timestamp; defaults to current UTC time.

    Returns:
        int: Number of current LLDP-to-Proxmox links recorded.
    """
    timestamp = seen_at or datetime.now(timezone.utc)
    source_ids = {source.id for source in proxmox_sources}
    if not source_ids:
        return 0

    # Replace only generated cross-source edges. Ordinary UniFi uplinks and
    # directly identified MikroTik links continue to be reconciled per batch.
    db.execute(delete(DiscoveredLink).where(
        DiscoveredLink.source_id == unifi_source.id,
        DiscoveredLink.evidence.in_(PROXMOX_LLDP_EVIDENCE),
    ))

    node_candidates: dict[str, list[DiscoveredDevice]] = {}
    pve_devices = []
    for device in db.scalars(select(DiscoveredDevice).where(
        DiscoveredDevice.source_id.in_(source_ids),
        DiscoveredDevice.category == "server",
    )):
        if json_object(device.details_json).get("status") == "offline":
            continue
        pve_devices.append(device)
        identifiers = json_object(device.identifiers_json)
        for address in _addresses(identifiers.get("management_ips")):
            node_candidates.setdefault(address, []).append(device)

    pve_device_ids = {device.id for device in pve_devices}
    pve_ports_by_device: dict[int, list[DiscoveredPort]] = {}
    for port in db.scalars(select(DiscoveredPort).where(
        DiscoveredPort.discovered_device_id.in_(pve_device_ids or {-1}),
    )):
        pve_ports_by_device.setdefault(port.discovered_device_id, []).append(port)
    physical_ports_by_name: dict[str, list[tuple[DiscoveredDevice, DiscoveredPort]]] = {}
    physical_ports_by_mac: dict[str, list[tuple[DiscoveredDevice, DiscoveredPort]]] = {}
    pve_by_id = {device.id: device for device in pve_devices}
    for device_id, ports in pve_ports_by_device.items():
        device = pve_by_id[device_id]
        for port in ports:
            for name in {port.name.casefold(), port.remote_id.casefold()}:
                physical_ports_by_name.setdefault(name, []).append((device, port))
            mac = mac_address(port.mac)
            if mac:
                physical_ports_by_mac.setdefault(mac, []).append((device, port))

    unifi_devices = list(db.scalars(select(DiscoveredDevice).where(
        DiscoveredDevice.source_id == unifi_source.id,
        DiscoveredDevice.category.in_(("switch", "router", "firewall", "access_point")),
    )))
    saved = {}
    for match in saved_neighbor_matches(db):
        key = neighbor_key(match["source_id"], match["chassis_id"], match["port_id"])
        saved.setdefault(key, []).append(match)
    occurrences = {}
    for switch in unifi_devices:
        neighbors = json_object(switch.details_json).get("lldp_neighbors", [])
        for neighbor in neighbors if isinstance(neighbors, list) else []:
            if not isinstance(neighbor, dict) or not all(isinstance(neighbor.get(field), str) for field in ("chassis_id", "port_id")):
                continue
            key = neighbor_key(unifi_source.id, neighbor.get("chassis_id", ""), neighbor.get("port_id", ""))
            occurrences.setdefault(key, set()).add((switch.id, str(neighbor.get("local_port"))))
    links: dict[tuple[int, int], DiscoveredLink] = {}
    for switch in unifi_devices:
        details = json_object(switch.details_json)
        neighbors = details.get("lldp_neighbors")
        if not isinstance(neighbors, list):
            continue
        switch_ports = {
            port.remote_id: port for port in db.scalars(select(DiscoveredPort).where(
                DiscoveredPort.discovered_device_id == switch.id,
            ))
        }
        for neighbor in neighbors:
            if not isinstance(neighbor, dict) or not all(isinstance(neighbor.get(field), str) for field in ("chassis_id", "port_id")):
                continue
            local_port_id = neighbor.get("local_port")
            local_port = switch_ports.get(str(local_port_id))
            if local_port is None:
                continue
            matching_nodes = {
                device.id: device
                for address in _addresses(neighbor.get("management_ips"))
                for device in node_candidates.get(address, [])
            }
            # lldpd announces one of the host's NIC MACs as its chassis ID,
            # which identifies the node as exactly as its management address.
            chassis_mac = mac_address(neighbor.get("chassis_id"))
            chassis_nodes = {
                device.id: device
                for device, _ in physical_ports_by_mac.get(chassis_mac, [])
            } if chassis_mac else {}
            if matching_nodes and chassis_nodes and matching_nodes.keys() != chassis_nodes.keys():
                continue  # Address and chassis disagree: do not guess.
            if not matching_nodes and len(chassis_nodes) == 1:
                matching_nodes = chassis_nodes
            port_id = neighbor.get("port_id")
            name_key = port_name(port_id)
            mac_key = mac_address(port_id)
            key = neighbor_key(unifi_source.id, neighbor.get("chassis_id", ""), str(port_id or ""))
            manual = saved.get(key, [])
            if manual:
                if len(manual) != 1 or len(occurrences.get(key, ())) != 1:
                    continue
                match = manual[0]
                targets = [port for node in pve_devices
                           if node.device_id == match["device_id"]
                           and node.source_id == match["target_source_id"]
                           and node.remote_id == match["target_device_remote_id"]
                           for port in pve_ports_by_device.get(node.id, [])
                           if port.remote_id == match["target_port_remote_id"]]
                if len(targets) != 1:
                    continue
                server_port = targets[0]
                evidence, confidence = "lldp-user-match", "confirmed"
            elif len(matching_nodes) > 1:
                continue
            elif len(matching_nodes) == 1:
                node = next(iter(matching_nodes.values()))
                matching_ports = [
                    port for port in pve_ports_by_device.get(node.id, [])
                    if (name_key is not None and name_key in {
                        port.remote_id.casefold(), port.name.casefold(),
                    }) or (mac_key is not None and mac_address(port.mac) == mac_key)
                ]
                if len(matching_ports) != 1:
                    continue
                server_port = matching_ports[0]
                evidence, confidence = "lldp-proxmox", "confirmed"
            else:
                named_candidates = physical_ports_by_name.get(name_key, []) if name_key else []
                mac_candidates = physical_ports_by_mac.get(mac_key, []) if mac_key else []
                unique_candidates = named_candidates or mac_candidates
                # If both identifiers resolve, require that they agree.
                if named_candidates and mac_candidates and {
                    port.id for _, port in named_candidates
                } != {port.id for _, port in mac_candidates}:
                    continue
                if len(unique_candidates) != 1:
                    continue
                _, server_port = unique_candidates[0]
                evidence, confidence = "lldp-proxmox-name", "inferred"
            left_id, right_id = sorted((local_port.id, server_port.id))
            if left_id == right_id:
                continue
            links[(left_id, right_id)] = DiscoveredLink(
                source_id=unifi_source.id,
                a_port_id=left_id,
                b_port_id=right_id,
                evidence=evidence,
                confidence=confidence,
                last_seen_at=timestamp,
            )

    endpoint_counts = Counter(port_id for pair in links for port_id in pair)
    occupied = set()
    for existing in db.scalars(select(DiscoveredLink).where(
        DiscoveredLink.evidence != "unifi-client-mac",
    )):
        occupied.update((existing.a_port_id, existing.b_port_id))
    recorded = 0
    for link in links.values():
        if (endpoint_counts[link.a_port_id] > 1 or endpoint_counts[link.b_port_id] > 1
                or link.a_port_id in occupied or link.b_port_id in occupied):
            continue
        # Fresh LLDP evidence takes precedence over a previous client inference.
        db.execute(delete(DiscoveredLink).where(
            DiscoveredLink.evidence == "unifi-client-mac",
            (DiscoveredLink.a_port_id.in_((link.a_port_id, link.b_port_id))) |
            (DiscoveredLink.b_port_id.in_((link.a_port_id, link.b_port_id))),
        ))
        db.add(link)
        db.flush()
        recorded += 1
        record_audit_event(
            db, actor_kind="collector", actor_id=unifi_source.id,
            actor_name=unifi_source.name, action="discovery.link.created",
            subject_kind="discovered_link", subject_id=link.id,
            details={"evidence": link.evidence, "confidence": link.confidence},
        )
    return recorded
