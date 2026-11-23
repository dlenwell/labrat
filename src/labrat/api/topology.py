# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Read the cached discovery graph: devices, ports, links, sources, and WAN.

Every route here is a GET that reads only the local SQLite inventory and
discovery cache, so a controller outage cannot hold up a browser request.
Observed links keep their evidence and confidence; they never change
canonical cable records. Writes live beside it: places in
``api.locations``, equipment in ``api.rack_devices``, and owner decisions
about observations in ``api.observation_actions``.

Used by: api.clients, app.
"""

from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, jsonify
from flask.typing import ResponseReturnValue
from sqlalchemy import select
from sqlalchemy.orm import Session

from labrat.api.common import as_iso, json_string_list, port_sort_key
from labrat.api.placement_rules import placement_snapshot
from labrat.auth.session import api_login_required, session_factory
from labrat.collector.watchlist import conditions as watch_conditions
from labrat.coerce import json_object, mac_address
from labrat.discovery.models import (
    DiscoveredDevice, DiscoveredLink, DiscoveredNetworkInterface, DiscoveredPort,
    DiscoverySource,
)
from labrat.discovery.neighbor_matches import neighbor_key, saved_neighbor_matches
from labrat.models import Device


topology_bp = Blueprint("topology_api", __name__, url_prefix="/api/v1")


def _infer_twin_media(records: list[dict[str, Any]]) -> None:
    """Give a cable with no reported medium the medium of its twin.

    Two devices are often joined by a pair of identical cables (two DACs to
    one server, say), and a module sometimes fails to identify itself. When
    a cable's ends report no medium, and every other cable between the same
    two devices at the same speed reports one and the same medium, that
    medium is used, and ``medium_inferred_from`` names the twin it came
    from. Reported media and port observations are never changed; a cable
    with no twin, or twins that disagree, stays unknown.

    Args:
        records: Link records with ``id``, ``from``, ``to``, ``medium``, and
            a private ``_speed`` (removed here).
    """
    pairs: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        pairs.setdefault(tuple(sorted((record["from"], record["to"]))), []).append(record)
    for group in pairs.values():
        for record in group:
            if record["medium"] or not record["_speed"]:
                continue
            twins = [other for other in group if other is not record
                     and other["medium"] and other["medium_inferred_from"] is None
                     and other["_speed"] == record["_speed"]]
            media = {twin["medium"] for twin in twins}
            if len(media) == 1:
                record["medium"] = media.pop()
                record["medium_inferred_from"] = twins[0]["id"]
    for record in records:
        record.pop("_speed", None)


# Bond modes that run LACP. Proxmox reports ``802.3ad``; ``4`` is the numeric
# Linux bonding mode for the same thing.
LACP_BOND_MODES = frozenset({"802.3ad", "4", "lacp"})


def _lacp_bond_members(
    interfaces: list[DiscoveredNetworkInterface],
) -> dict[int, set[str]]:
    """Collect the member NIC names of each device's LACP bonds.

    Args:
        interfaces: Logical network interface observations.

    Returns:
        dict[int, set[str]]: Case-folded member names keyed by the discovered
            device ID that owns the bond.
    """
    members: dict[int, set[str]] = {}
    for interface in interfaces:
        if interface.kind != "bond" or (interface.mode or "").casefold() not in LACP_BOND_MODES:
            continue
        members.setdefault(interface.discovered_device_id, set()).update(
            name.casefold() for name in json_string_list(interface.members_json)
        )
    return members


def _is_lacp_secondary_identity(
    chassis_id: str,
    port_id: str,
    other: DiscoveredPort | None,
    lacp_members: dict[int, set[str]],
) -> bool:
    """Decide whether a neighbor is an extra identity of a cabled LACP member.

    Some bonded NICs announce a second LLDP identity, using an address next to
    their own MAC, on the same switch port as the host's ordinary LLDP
    neighbor. When that switch port already has a cable to a physical NIC that
    is a member of an LACP bond, and the reported MAC shares that NIC's first
    five bytes, the neighbor describes the cabled NIC rather than unknown
    equipment. Every other unmatched neighbor still counts as unresolved.

    Args:
        chassis_id: Neighbor chassis ID reported by the switch.
        port_id: Neighbor port ID reported by the switch.
        other: Far endpoint of a cable attached to the switch port.
        lacp_members: LACP bond member names keyed by discovered device ID.

    Returns:
        bool: True when the neighbor is a secondary identity of that NIC.
    """
    if other is None:
        return False
    members = lacp_members.get(other.discovered_device_id, set())
    if not {other.name.casefold(), other.remote_id.casefold()} & members:
        return False
    reported = mac_address(port_id) or mac_address(chassis_id)
    nic_mac = mac_address(other.mac)
    if reported is None or nic_mac is None or reported == nic_mac:
        return False
    # Normalized MACs are twelve hex digits, so five bytes are ten digits.
    return reported[:10] == nic_mac[:10]


#: UPS telemetry fields passed to pages (collector/adapters/nut.py writes them).
_UPS_READING_FIELDS = (
    "state", "flags", "status_raw", "alarm", "test_result", "sampled_at",
    "battery_charge", "battery_charge_low", "battery_runtime_s", "battery_runtime_low_s",
    "battery_voltage", "load_percent", "real_power_w", "apparent_power_va",
    "real_power_nominal_w", "apparent_power_nominal_va", "input_voltage",
    "input_frequency_hz", "output_voltage", "output_frequency_hz", "temperature_c",
)


def _ups_record(details: dict[str, Any], server_names: dict[tuple[int, str], str],
                source_id: int) -> dict[str, Any] | None:
    """Describe a UPS observation's readings and provenance for pages.

    Args:
        details: The observation's stored details.
        server_names: NUT server observation names by (source ID, remote ID).
        source_id: The observation's source.

    Returns:
        dict[str, Any] | None: Whitelisted readings plus driver, attachment
            (``usb``, ``relayed``, or None), maker, model, and the name of the
            NUT server it was read through; None for non-UPS observations.
    """
    readings = details.get("ups")
    if not isinstance(readings, dict):
        return None
    record = {field: readings.get(field) for field in _UPS_READING_FIELDS}
    for field in ("driver", "attachment", "manufacturer", "model"):
        value = details.get(field)
        record[field] = value if isinstance(value, str) else None
    server = details.get("server_remote_id")
    record["server_name"] = server_names.get((source_id, server)) if isinstance(server, str) else None
    return record


def _wireless_record(details: dict[str, Any]) -> dict[str, Any] | None:
    """Return whitelisted, age-qualified UniFi evidence for a NUT server.

    This is an inferred client attachment. It is deliberately separate from
    discovered physical ports and cable links, and never adds to port counts.

    Args:
        details: The NUT server or client observation's stored details.

    Returns:
        dict[str, Any] | None: The wireless evidence with its age, or None
            when there is none, it is stale, or the role does not carry it.
    """
    if details.get("role") not in {"nut_server", "client"}:
        return None
    saved = details.get("wireless")
    if not isinstance(saved, dict):
        return None
    sampled_at = saved.get("sampled_at")
    try:
        sample = datetime.fromisoformat(sampled_at)
        if sample.tzinfo is None:
            return None
        age = (datetime.now(timezone.utc) - sample).total_seconds()
    except (TypeError, ValueError):
        return None
    fresh = 0 <= age <= 600
    record = {field: saved.get(field) for field in (
        "state", "evidence", "source", "ip_address", "mac_address",
        "connected_at", "sampled_at",
    )}
    record["fresh"] = fresh
    return record


def _source_record(source: DiscoverySource) -> dict[str, Any]:
    """Describe one discovery source's collection state for pages.

    Args:
        source: Discovery source row.

    Returns:
        dict[str, Any]: Name, kind, last attempt and success times, the
            looping collector's interval, and the stored error summary
            (an error type only, never raw exception text).
    """
    return {
        "id": source.id,
        "name": source.name, "kind": source.kind,
        "last_success_at": as_iso(source.last_success_at),
        "last_attempt_at": as_iso(source.last_attempt_at),
        "poll_interval_seconds": source.poll_interval_seconds,
        "last_error": source.last_error,
    }


@topology_bp.get("/sources")
@api_login_required
def sources() -> ResponseReturnValue:
    """Return each discovery source's collection state, and nothing else.

    Pages poll this small response to notice a new collection, then fetch
    the full topology only when something was collected.

    Returns:
        ResponseReturnValue: ``{"sources": [...]}`` as in ``/topology``.
    """
    with session_factory()() as db:
        rows = list(db.scalars(select(DiscoverySource).order_by(DiscoverySource.name)))
        return jsonify(sources=[_source_record(source) for source in rows])


@topology_bp.get("/wan")
@api_login_required
def wan_status() -> ResponseReturnValue:
    """Return each gateway's internet uplink state and one-day history.

    The collector stores these readings (see ``collector.store.record_wan``);
    this endpoint never contacts a controller. ``node_id`` matches the
    gateway's node in ``/topology``. History rows are
    ``[bucket_start_epoch, rx_bps, tx_bps, latency_ms, samples, degraded]``.
    The optional ``wan.vpn`` object contains sanitized logical-tunnel state,
    connection time, bit/s rates, remote-access count, and its own sample age.
    It never includes raw VPN configuration or authentication material.
    ``vpn_history`` maps each tunnel's ``id`` to one-day rows of
    ``[bucket_start_epoch, rx_bps, tx_bps, samples]``.

    Returns:
        ResponseReturnValue: ``{"gateways": [...]}``.
    """
    with session_factory()() as db:
        source_names = {source.id: source.name for source in db.scalars(select(DiscoverySource))}
        gateways = []
        for observed in db.scalars(select(DiscoveredDevice).where(
            DiscoveredDevice.visibility == "recognized",
        ).order_by(DiscoveredDevice.id)):
            details = json_object(observed.details_json)
            snapshot = details.get("wan")
            if not isinstance(snapshot, dict):
                continue
            history = details.get("wan_history")
            vpn_history = details.get("vpn_history")
            gateways.append({
                "node_id": (f"device:{observed.device_id}" if observed.device_id is not None
                            else f"observed:{observed.id}"),
                "name": observed.name,
                "source": source_names.get(observed.source_id, "unknown"),
                "wan": snapshot,
                "history": history if isinstance(history, list) else [],
                "vpn_history": vpn_history if isinstance(vpn_history, dict) else {},
            })
        return jsonify(gateways=gateways)


class _TopologyGraph:
    """The discovery cache as one graph: nodes, ports, cables, Wi-Fi, and neighbors.

    ``topology`` builds it in steps; each step reads the rows and indexes
    loaded here and adds to ``grouped_nodes`` or returns edges.
    """

    def __init__(self, db: Session) -> None:
        """Load every row the graph needs and index it.

        Args:
            db: An open read session.
        """
        self.db = db
        self.sources = list(self.db.scalars(select(DiscoverySource).order_by(DiscoverySource.name)))
        self.observations = list(self.db.scalars(
            select(DiscoveredDevice)
            .where(DiscoveredDevice.visibility == "recognized")
            .order_by(DiscoveredDevice.id)
        ))
        self.ports = list(self.db.scalars(select(DiscoveredPort).order_by(DiscoveredPort.id)))
        self.network_interfaces = list(self.db.scalars(
            select(DiscoveredNetworkInterface)
            .order_by(DiscoveredNetworkInterface.discovered_device_id,
                      DiscoveredNetworkInterface.kind,
                      DiscoveredNetworkInterface.name)
        ))
        self.links = list(self.db.scalars(select(DiscoveredLink).order_by(DiscoveredLink.id)))
        self.source_names = {item.id: item.name for item in self.sources}
        self.observed_by_identity = {(item.source_id, item.remote_id): item for item in self.observations}
        self.observation_sources = {item.id: self.source_names.get(item.source_id, "unknown") for item in self.observations}
        self.matches = saved_neighbor_matches(self.db)
        self.network_by_device: dict[int, list[dict[str, Any]]] = {}
        for interface in self.network_interfaces:
            self.network_by_device.setdefault(interface.discovered_device_id, []).append({
                "name": interface.name,
                "kind": interface.kind,
                "parent_name": interface.parent_name,
                "vlan_id": interface.vlan_id,
                "mode": interface.mode,
                "members": json_string_list(interface.members_json),
                "active": interface.active,
                "details": json_object(interface.details_json),
            })
        self.lacp_members = _lacp_bond_members(self.network_interfaces)
        # Physical NIC MACs name their host: lldpd announces one of them as
        # the chassis ID, so a blank-port neighbor still identifies a device.
        self.devices_by_mac: dict[str, set[int]] = {}
        for port in self.ports:
            port_mac = mac_address(port.mac)
            if port_mac:
                self.devices_by_mac.setdefault(port_mac, set()).add(port.discovered_device_id)

    def group_nodes(self) -> None:
        """Fold observations into graph nodes.

        One node per inventory device, else one per observation; an exact-MAC
        client alias folds into its host while it has no inventory decision.
        """
        self.observation_index = {item.id: item for item in self.observations}
        self.node_by_observation: dict[int, str] = {}
        self.grouped_nodes: dict[str, dict[str, Any]] = {}
        # NUT servers by identity, so each UPS can name the server it was read through.
        self.server_names = {(item.source_id, item.remote_id): item.name for item in self.observations}
        for observed in self.observations:
            client_details = json_object(observed.details_json).get("client")
            represented = self.observation_index.get(client_details.get("represented_by")) if isinstance(client_details, dict) else None
            # Fold exact-MAC display aliases only while no explicit inventory
            # decision exists on the client. This never writes canonical identity.
            display_observation = represented if represented and observed.device_id is None else observed
            node_id = (
                f"device:{display_observation.device_id}" if display_observation.device_id is not None
                else f"observed:{display_observation.id}"
            )
            self.node_by_observation[observed.id] = node_id
            existing = self.grouped_nodes.get(node_id)
            # The source's own run state, such as a Proxmox node's "online"
            # or "offline"; None when the source reports none.
            observed_details = json_object(observed.details_json)
            status = observed_details.get("status")
            entry = {
                "id": observed.id, "source": self.source_names.get(observed.source_id, "unknown"),
                "source_id": observed.source_id, "remote_id": observed.remote_id,
                "reported_name": observed.name,
                "wan_upstream": observed_details.get("platform") == "unifi-wan-inference",
                "last_seen_at": as_iso(observed.last_seen_at),
                "status": status if isinstance(status, str) else None,
                # The controller's own view of an adopted device (UniFi's
                # ONLINE/OFFLINE), lower-cased; None when it reports none.
                "controller_state": (observed_details["state"].lower()
                                     if isinstance(observed_details.get("state"), str) else None),
                "network_interfaces": self.network_by_device.get(observed.id, []),
                "neighbors": [],
                "ups": _ups_record(observed_details, self.server_names, observed.source_id),
                "wireless": _wireless_record(observed_details),
                "client": observed_details.get("client") if observed_details.get("role") == "client" else None,
                # A router's watchlist conditions (collector/watchlist.py).
                "watch_conditions": watch_conditions(observed_details.get("watchlist")),
                "identifiers": json_object(observed.identifiers_json),
            }
            if existing is None:
                inventory = self.db.get(Device, display_observation.device_id) if display_observation.device_id else None
                self.grouped_nodes[node_id] = {
                    "id": node_id,
                    "discovery_id": display_observation.id,
                    "device_id": display_observation.device_id,
                    "name": inventory.name if inventory else display_observation.name,
                    "category": display_observation.category,
                    "height_u": inventory.height_u if inventory else None,
                    "placement": placement_snapshot(self.db, display_observation.device_id),
                    "observations": [entry],
                    "ports": [],
                }
            else:
                existing["observations"].append(entry)
        for grouped in self.grouped_nodes.values():
            grouped["client_only"] = all(item.get("client") is not None for item in grouped["observations"])

    def attach_ports(self) -> None:
        """Give every node its physical ports, in natural order."""
        self.known_ports: dict[int, DiscoveredPort] = {}
        for port in self.ports:
            node_id = self.node_by_observation.get(port.discovered_device_id)
            if node_id is None:
                continue
            self.grouped_nodes[node_id]["ports"].append({
                "id": port.id, "source": self.observation_sources[port.discovered_device_id],
                "observation_id": port.discovered_device_id, "remote_id": port.remote_id,
                "name": port.name, "mac": port.mac,
                "speed_mbps": port.speed_mbps,
                "link_up": port.link_up,
                "max_speed_mbps": port.max_speed_mbps,
                "connector_type": port.connector_type,
                "medium": port.medium,
            })
            self.known_ports[port.id] = port
        # Show self.ports in natural order (nic0, nic1 ... Port 2 before Port 10).
        for grouped in self.grouped_nodes.values():
            grouped["ports"].sort(key=lambda port: port_sort_key(port["name"]))

    def link_ups_servers(self) -> None:
        """Name, on each UPS, the node of the NUT server it was read through.

        Pages relate them by this rather than by (user-editable) names.
        """
        for observed in self.observations:
            details = json_object(observed.details_json)
            entry = next(item for item in self.grouped_nodes[self.node_by_observation[observed.id]]["observations"]
                         if item["id"] == observed.id)
            if entry["ups"] is None:
                continue
            server = self.observed_by_identity.get((observed.source_id, details.get("server_remote_id")))
            entry["ups"]["server_node_id"] = self.node_by_observation.get(server.id) if server else None

    def wireless_edges(self) -> list[dict[str, Any]]:
        """Draw each fresh Wi-Fi client to its access point, once per pair.

        Returns:
            list[dict[str, Any]]: The wireless edges.
        """
        wireless_edges: list[dict[str, Any]] = []
        wireless_pairs: set[tuple[str, str]] = set()
        for observed in self.observations:
            saved = json_object(observed.details_json).get("wireless")
            wireless = _wireless_record(json_object(observed.details_json))
            if not wireless or wireless.get("state") != "connected":
                continue
            if not isinstance(saved, dict):
                continue
            ap = self.observed_by_identity.get((saved.get("source_id"), saved.get("ap_remote_id")))
            if ap is None or ap.category != "access_point":
                continue
            from_id = self.node_by_observation[observed.id]
            to_id = self.node_by_observation[ap.id]
            if wireless["fresh"] and from_id != to_id and (from_id, to_id) not in wireless_pairs:
                wireless_pairs.add((from_id, to_id))
                wireless_edges.append({
                    "from": from_id, "to": to_id, "evidence": wireless["evidence"],
                    "sampled_at": wireless["sampled_at"],
                })
            entry = next(item for item in self.grouped_nodes[from_id]["observations"]
                         if item["id"] == observed.id)
            entry["wireless"]["ap_name"] = self.grouped_nodes[to_id]["name"]
            entry["wireless"]["ap_node_id"] = to_id
        return wireless_edges

    def cable_edges(self) -> list[dict[str, Any]]:
        """List the cables between nodes, with media inferred from twins.

        Returns:
            list[dict[str, Any]]: The cable edges.
        """
        edge_records: list[dict[str, Any]] = []
        for link in self.links:
            left = self.known_ports.get(link.a_port_id)
            right = self.known_ports.get(link.b_port_id)
            if left is None or right is None:
                continue
            from_id = self.node_by_observation.get(left.discovered_device_id)
            to_id = self.node_by_observation.get(right.discovered_device_id)
            if not from_id or not to_id or from_id == to_id:
                continue
            edge_records.append({
                "id": link.id, "from": from_id, "to": to_id,
                "from_port": left.name, "to_port": right.name,
                "evidence": link.evidence, "confidence": link.confidence,
                "source": self.source_names.get(link.source_id, "unknown"),
                "last_seen_at": as_iso(link.last_seen_at),
                # The cable's medium as either end reports it; a cable whose
                # ends report none may take its twin's (_infer_twin_media).
                "medium": left.medium or right.medium, "medium_inferred_from": None,
                "_speed": max(left.speed_mbps or 0, right.speed_mbps or 0),
            })
        _infer_twin_media(edge_records)
        return edge_records

    def attach_neighbors(self) -> None:
        """Add each switch port's LLDP neighbors, marking which a cable explains."""
        for observed in self.observations:
            node = self.grouped_nodes[self.node_by_observation[observed.id]]
            entry = next(item for item in node["observations"] if item["id"] == observed.id)
            neighbors = json_object(observed.details_json).get("lldp_neighbors", [])
            for neighbor in neighbors if isinstance(neighbors, list) else []:
                if not isinstance(neighbor, dict):
                    continue
                local = next((p for p in self.ports if p.discovered_device_id == observed.id
                              and p.remote_id == str(neighbor.get("local_port"))), None)
                if local is None:
                    continue
                chassis, remote = neighbor.get("chassis_id", ""), neighbor.get("port_id", "")
                if not isinstance(chassis, str) or not isinstance(remote, str):
                    continue
                key = neighbor_key(observed.source_id, chassis, remote)
                saved = next((m for m in self.matches if neighbor_key(m["source_id"], m["chassis_id"], m["port_id"]) == key), None)
                attached = [link for link in self.links if local.id in (link.a_port_id, link.b_port_id)]
                resolved = False
                for link in attached:
                    other = self.known_ports.get(link.b_port_id if link.a_port_id == local.id else link.a_port_id)
                    if other is None:
                        continue
                    reported_mac = mac_address(remote) or (mac_address(chassis) if not remote else None)
                    if (remote and remote.casefold() in {other.name.casefold(), other.remote_id.casefold()}
                            or reported_mac is not None and reported_mac == mac_address(other.mac)
                            or saved and link.evidence == "lldp-user-match"):
                        resolved = True
                    elif (not remote and reported_mac is not None
                          and self.devices_by_mac.get(reported_mac) == {other.discovered_device_id}):
                        # No port reported, but the chassis names exactly the
                        # host this cable reaches, through another of its NICs.
                        resolved = True
                lacp_secondary = not resolved and any(
                    _is_lacp_secondary_identity(
                        chassis, remote, self.known_ports.get(
                            link.b_port_id if link.a_port_id == local.id else link.a_port_id,
                        ), self.lacp_members,
                    )
                    for link in attached
                )
                entry["neighbors"].append({
                    "switch_observation_id": observed.id, "local_port": local.remote_id,
                    "local_port_name": local.name, "chassis_id": chassis,
                    "port_id": remote, "management_ips": neighbor.get("management_ips", []),
                    "resolved": resolved or lacp_secondary,
                    "lacp_secondary": lacp_secondary, "occupied": bool(attached),
                    "match_id": saved["id"] if saved else None,
                })


@topology_bp.get("/topology")
@api_login_required
def topology() -> ResponseReturnValue:
    """Return recognized physical equipment and evidence-backed cable links.

    Matched observations from multiple discovery sources share one canonical
    graph node. Unmatched observations remain separate until a human confirms a
    match; a similar hostname or MAC is not proof of physical identity.

    Returns:
        ResponseReturnValue: Source status, physical graph nodes and confirmed
            links from the cached discovery database.
    """
    with session_factory()() as db:
        graph = _TopologyGraph(db)
        graph.group_nodes()
        graph.attach_ports()
        graph.link_ups_servers()
        wireless_edges = graph.wireless_edges()
        edge_records = graph.cable_edges()
        graph.attach_neighbors()
        return jsonify(
            nodes=list(graph.grouped_nodes.values()), links=edge_records,
            wireless_links=wireless_edges,
            neighbor_matches=graph.matches,
            sources=[_source_record(source) for source in graph.sources],
        )
