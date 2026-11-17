# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Persist source-scoped observations, never modifying canonical inventory.

Repeated polling refreshes timestamps and fields for the same remote identity.
Ports are reconciled for devices whose adapter marks the list complete, while
offline Proxmox nodes retain their last known ports.
This module does *not* match devices between sources, create inventory devices,
or modify canonical cabling; those decisions belong to reconciliation in 4B.

Used by: collector.clients, collector.runner, collector.wireless_clients.
"""

from collections.abc import Mapping
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from labrat.audit import record_audit_event
from labrat.coerce import json_object
from labrat.collector.contracts import (
    CABLE_MEDIA, DiscoveryBatch, PHYSICAL_CATEGORIES, WanObservation,
)
from labrat.discovery.models import (
    DiscoveredDevice, DiscoveredLink, DiscoveredNetworkInterface, DiscoveredPort,
    DiscoverySource,
)
from labrat.discovery.observation_ignores import apply_device_ignores


#: Detail keys written by live telemetry (record_wan) rather than by a scan.
#: A full scan keeps them, and they never count as a device change, so
#: frequent readings add no audit events.
TELEMETRY_DETAIL_KEYS = (
    "wan", "wan_history", "vpn_history", "ups", "wireless", "client", "proxmox_clients",
    "proxmox_node", "proxmox_cluster", "unifi_network", "pihole",
)

#: WAN history: one averaged bucket per five minutes, kept for a day.
WAN_HISTORY_BUCKET_SECONDS = 300
WAN_HISTORY_SECONDS = 86400

#: Most VPN tunnels kept in ``vpn_history`` per gateway (most recent first).
VPN_HISTORY_MAX_TUNNELS = 32


def _extend_vpn_history(
    previous: Any, vpn: dict | None, bucket: int,
) -> dict[str, list[list[float]]]:
    """Add one VPN reading to the per-tunnel one-day traffic history.

    Rows are ``[bucket_start, rx_bps, tx_bps, samples]``, averaged per
    five-minute bucket like ``wan_history`` (the shared chart reads the
    first three columns). Only an available reading adds points, so an
    unavailable VPN endpoint leaves a gap rather than a false zero; a
    tunnel with no reported rates adds nothing. Rows older than a day are
    dropped, then tunnels with no rows left.

    Args:
        previous: The stored ``vpn_history`` value (validated here).
        vpn: The gateway's sanitized ``wan.vpn`` snapshot, or None.
        bucket: Start of the current five-minute bucket (epoch seconds).

    Returns:
        dict[str, list[list[float]]]: Tunnel ID to its history rows.
    """
    history: dict[str, list[list[float]]] = {}
    if isinstance(previous, dict):
        for tunnel, rows in previous.items():
            if not isinstance(tunnel, str) or not isinstance(rows, list):
                continue
            kept = [row for row in rows if isinstance(row, list) and len(row) == 4
                    and isinstance(row[0], (int, float)) and row[0] > bucket - WAN_HISTORY_SECONDS]
            if kept:
                history[tunnel] = kept
    if isinstance(vpn, dict) and vpn.get("available"):
        for connection in vpn.get("connections") or []:
            if not isinstance(connection, dict) or not isinstance(connection.get("id"), str):
                continue
            rx, tx = connection.get("rx_bps"), connection.get("tx_bps")
            if not isinstance(rx, (int, float)) and not isinstance(tx, (int, float)):
                continue
            rx = rx if isinstance(rx, (int, float)) else 0
            tx = tx if isinstance(tx, (int, float)) else 0
            rows = history.setdefault(connection["id"], [])
            if rows and rows[-1][0] == bucket:
                start, avg_rx, avg_tx, count = rows[-1]
                total = count + 1
                rows[-1] = [start, (avg_rx * count + rx) / total, (avg_tx * count + tx) / total, total]
            else:
                rows.append([bucket, rx, tx, 1])
    # Bound the stored size: keep the tunnels seen most recently.
    newest = sorted(history, key=lambda tunnel: history[tunnel][-1][0], reverse=True)
    return {tunnel: history[tunnel] for tunnel in newest[:VPN_HISTORY_MAX_TUNNELS]}


def _json_text(value: object) -> str:
    """Serialize adapter-provided metadata to finite JSON text.

    Args:
        value: JSON-compatible data returned by the adapter.

    Returns:
        str: Compact JSON suitable for persistent storage.

    Raises:
        TypeError: Data contains an unsupported Python value.
        ValueError: Data contains a non-finite number.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def apply_batch(
    db: Session,
    source: DiscoverySource,
    batch: DiscoveryBatch,
    seen_at: datetime | None = None,
) -> None:
    """Upsert one source's physical devices, ports, and candidate links.

    Caller owns the transaction; commit only after this operation succeeds.
    Canonical inventory and manual link locks are not modified. Each phase
    (devices, ports, logical interfaces, links) validates its records,
    upserts them, and audits what changed; then the snapshot removes what
    the source no longer reports, and WAN readings are stored.

    Args:
        db: SQLAlchemy session with migrated LabRat tables.
        source: Persisted discovery source associated with this batch.
        batch: Atomic observations from exactly this source.
        seen_at: Timestamp of the successful scan, or the current UTC time.

    Raises:
        ValueError: Unknown device category, duplicate IDs, missing port
            references, or invalid link evidence.
    """
    timestamp = seen_at or datetime.now(timezone.utc)
    observed_devices = _upsert_devices(db, source, batch, timestamp)
    observed_ports = _upsert_ports(db, source, batch, observed_devices, timestamp)
    observed_network_interfaces = _upsert_network_interfaces(db, source, batch, observed_devices, timestamp)
    observed_link_ids = _upsert_links(db, source, batch, observed_ports, timestamp)
    _remove_stale_links(db, source, observed_link_ids)
    # A successful online snapshot reconciles the node's logical interfaces.
    # Offline nodes retain their last known bond, bridge, and VLAN details.
    _remove_unreported(
        db, DiscoveredNetworkInterface, _complete_devices(batch, "network_interfaces_complete"),
        observed_devices, observed_network_interfaces.values(),
    )
    # Remove ports absent from each complete per-device port list. This also
    # clears duplicate aliases after a successful Proxmox refresh.
    _remove_unreported(
        db, DiscoveredPort, _complete_devices(batch, "ports_complete"),
        observed_devices, observed_ports.values(),
    )
    db.flush()
    for reading in batch.wan:
        record_wan(db, source, reading, timestamp)

    source.last_success_at = timestamp
    source.last_error = None


def _audit_changes(db: Session, source: DiscoverySource, subject_kind: str,
                   changes: list[tuple[Any, str, list[str]]]) -> None:
    """Record one audit event per created or changed observation row.

    Args:
        db: The open session.
        source: The source that reported the rows, recorded as the actor.
        subject_kind: The rows' table, such as ``discovered_port``.
        changes: Each row with its audit action and changed field names.
    """
    for row, action, fields in changes:
        record_audit_event(
            db, actor_kind="collector", actor_id=source.id,
            actor_name=source.name, action=action,
            subject_kind=subject_kind, subject_id=row.id,
            details={"changed_fields": fields},
        )


def _complete_devices(batch: DiscoveryBatch, flag: str) -> set[str]:
    """Name the devices whose batch details say a list of theirs is complete.

    Args:
        batch: The source's batch.
        flag: The detail that marks it, such as ``ports_complete``.

    Returns:
        set[str]: The devices' remote IDs.
    """
    return {item.remote_id for item in batch.devices if item.details.get(flag) is True}


def _upsert_devices(db: Session, source: DiscoverySource, batch: DiscoveryBatch,
                    timestamp: datetime) -> dict[str, DiscoveredDevice]:
    """Validate and upsert the batch's devices, keeping stored telemetry.

    Args:
        db: The open session.
        source: The reporting source.
        batch: The source's batch.
        timestamp: When the scan succeeded.

    Returns:
        dict[str, DiscoveredDevice]: The device rows by remote ID.

    Raises:
        ValueError: An unknown category, a missing ID or name, or a
            duplicate ID.
    """
    device_changes: list[tuple[DiscoveredDevice, str, list[str]]] = []
    observed_devices: dict[str, DiscoveredDevice] = {}
    for item in batch.devices:
        if item.category not in PHYSICAL_CATEGORIES:
            raise ValueError(f"Unsupported physical equipment category: {item.category}")
        if not item.remote_id or not item.name:
            raise ValueError("Observed devices require a remote ID and name")
        if item.remote_id in observed_devices:
            raise ValueError("Duplicate remote device ID in one batch")
        device = db.scalar(
            select(DiscoveredDevice).where(
                DiscoveredDevice.source_id == source.id,
                DiscoveredDevice.remote_id == item.remote_id,
            )
        )
        identifiers_json = _json_text(dict(item.identifiers))
        details = dict(item.details)
        previous = json_object(device.details_json) if device is not None else {}
        for key in TELEMETRY_DETAIL_KEYS:
            if key in previous and key not in details:
                details[key] = previous[key]
        details_json = _json_text(details)
        scanned = {key: value for key, value in details.items() if key not in TELEMETRY_DETAIL_KEYS}
        scanned_before = {key: value for key, value in previous.items() if key not in TELEMETRY_DETAIL_KEYS}
        if device is None:
            device = DiscoveredDevice(source_id=source.id, remote_id=item.remote_id)
            db.add(device)
            action = "discovery.device.created"
            changed_fields = []
        else:
            changed_fields = [
                key for key, value in (
                    ("name", item.name), ("category", item.category),
                    ("identifiers_json", identifiers_json),
                ) if getattr(device, key) != value
            ] + (["details_json"] if scanned != scanned_before else [])
            action = "discovery.device.changed"
        device.name = item.name
        device.category = item.category
        device.identifiers_json = identifiers_json
        device.details_json = details_json
        device.last_seen_at = timestamp
        observed_devices[item.remote_id] = device
        if action == "discovery.device.created" or changed_fields:
            device_changes.append((device, action, changed_fields))
    db.flush()
    # An ignored device stays hidden when the source reports it again.
    apply_device_ignores(db, source.id)
    _audit_changes(db, source, "discovered_device", device_changes)
    return observed_devices


def _upsert_ports(db: Session, source: DiscoverySource, batch: DiscoveryBatch,
                  observed_devices: dict[str, DiscoveredDevice],
                  timestamp: datetime) -> dict[tuple[str, str], DiscoveredPort]:
    """Validate and upsert the batch's physical ports.

    Args:
        db: The open session.
        source: The reporting source.
        batch: The source's batch.
        observed_devices: This batch's device rows by remote ID.
        timestamp: When the scan succeeded.

    Returns:
        dict[tuple[str, str], DiscoveredPort]: The port rows by device and
            port remote ID.

    Raises:
        ValueError: A duplicate or invalid port, or one on a device missing
            from the batch.
    """
    observed_ports: dict[tuple[str, str], DiscoveredPort] = {}
    port_changes: list[tuple[DiscoveredPort, str, list[str]]] = []
    for item in batch.ports:
        key = (item.device_remote_id, item.remote_id)
        if key in observed_ports:
            raise ValueError("Duplicate remote port ID in one batch")
        device = observed_devices.get(item.device_remote_id)
        if device is None:
            raise ValueError("Port refers to a device missing from the batch")
        if not item.remote_id or not item.name or (
            item.speed_mbps is not None and item.speed_mbps <= 0
        ) or (
            item.max_speed_mbps is not None and item.max_speed_mbps <= 0
        ):
            raise ValueError("Invalid discovered physical port")
        if item.connector_type is not None and item.connector_type not in {
            "rj45", "sfp", "sfp_plus", "sfp28", "combo",
        }:
            raise ValueError("Invalid discovered physical connector type")
        if item.medium is not None and item.medium not in CABLE_MEDIA:
            raise ValueError("Invalid discovered cable medium")
        port = db.scalar(
            select(DiscoveredPort).where(
                DiscoveredPort.discovered_device_id == device.id,
                DiscoveredPort.remote_id == item.remote_id,
            )
        )
        if port is None:
            port = DiscoveredPort(
                discovered_device_id=device.id, remote_id=item.remote_id
            )
            db.add(port)
            action = "discovery.port.created"
            changed_fields = []
        else:
            changed_fields = [
                field for field, value in (
                    ("name", item.name), ("mac", item.mac),
                    ("speed_mbps", item.speed_mbps), ("link_up", item.link_up),
                    ("max_speed_mbps", item.max_speed_mbps),
                    ("connector_type", item.connector_type),
                    ("medium", item.medium),
                ) if getattr(port, field) != value
            ]
            action = "discovery.port.changed"
        port.name = item.name
        port.mac = item.mac
        port.speed_mbps = item.speed_mbps
        port.link_up = item.link_up
        port.max_speed_mbps = item.max_speed_mbps
        port.connector_type = item.connector_type
        port.medium = item.medium
        port.last_seen_at = timestamp
        observed_ports[key] = port
        if action == "discovery.port.created" or changed_fields:
            port_changes.append((port, action, changed_fields))
    db.flush()
    _audit_changes(db, source, "discovered_port", port_changes)
    return observed_ports


def _upsert_network_interfaces(
    db: Session, source: DiscoverySource, batch: DiscoveryBatch,
    observed_devices: dict[str, DiscoveredDevice], timestamp: datetime,
) -> dict[tuple[str, str], DiscoveredNetworkInterface]:
    """Validate and upsert the batch's bonds, bridges, and VLANs.

    Args:
        db: The open session.
        source: The reporting source.
        batch: The source's batch.
        observed_devices: This batch's device rows by remote ID.
        timestamp: When the scan succeeded.

    Returns:
        dict[tuple[str, str], DiscoveredNetworkInterface]: The interface rows
            by device and interface remote ID.

    Raises:
        ValueError: A duplicate or invalid interface, or one on a device
            missing from the batch.
    """
    observed_network_interfaces: dict[tuple[str, str], DiscoveredNetworkInterface] = {}
    interface_changes: list[tuple[DiscoveredNetworkInterface, str, list[str]]] = []
    for item in batch.network_interfaces:
        key = (item.device_remote_id, item.remote_id)
        if key in observed_network_interfaces:
            raise ValueError("Duplicate remote network-interface ID in one batch")
        device = observed_devices.get(item.device_remote_id)
        if device is None:
            raise ValueError("Network interface refers to a device missing from the batch")
        if (
            not item.remote_id or not item.name
            or item.kind not in {"bond", "bridge", "vlan"}
            or (item.vlan_id is not None and not 1 <= item.vlan_id <= 4094)
            or any(not isinstance(member, str) or not member.strip() for member in item.members)
            or len(set(item.members)) != len(item.members)
        ):
            raise ValueError("Invalid discovered logical network interface")
        members_json = _json_text(list(item.members))
        details_json = _json_text(dict(item.details))
        interface = db.scalar(
            select(DiscoveredNetworkInterface).where(
                DiscoveredNetworkInterface.discovered_device_id == device.id,
                DiscoveredNetworkInterface.remote_id == item.remote_id,
            )
        )
        if interface is None:
            interface = DiscoveredNetworkInterface(
                discovered_device_id=device.id,
                remote_id=item.remote_id,
            )
            db.add(interface)
            action = "discovery.network_interface.created"
            changed_fields = []
        else:
            changed_fields = [
                field for field, value in (
                    ("name", item.name), ("kind", item.kind),
                    ("parent_name", item.parent_name), ("vlan_id", item.vlan_id),
                    ("mode", item.mode), ("members_json", members_json),
                    ("active", item.active), ("details_json", details_json),
                ) if getattr(interface, field) != value
            ]
            action = "discovery.network_interface.changed"
        interface.name = item.name
        interface.kind = item.kind
        interface.parent_name = item.parent_name
        interface.vlan_id = item.vlan_id
        interface.mode = item.mode
        interface.members_json = members_json
        interface.active = item.active
        interface.details_json = details_json
        interface.last_seen_at = timestamp
        observed_network_interfaces[key] = interface
        if action == "discovery.network_interface.created" or changed_fields:
            interface_changes.append((interface, action, changed_fields))
    db.flush()
    _audit_changes(db, source, "discovered_network_interface", interface_changes)
    return observed_network_interfaces


def _upsert_links(db: Session, source: DiscoverySource, batch: DiscoveryBatch,
                  observed_ports: dict[tuple[str, str], DiscoveredPort],
                  timestamp: datetime) -> set[int]:
    """Validate and upsert the batch's links between its ports.

    Args:
        db: The open session.
        source: The reporting source.
        batch: The source's batch.
        observed_ports: This batch's port rows by device and port remote ID.
        timestamp: When the scan succeeded.

    Returns:
        set[int]: The IDs of every link the batch reported.

    Raises:
        ValueError: A link without two distinct ports from the batch, with
            missing evidence or an unknown confidence, or reported twice.
    """
    unique_links: set[tuple[int, int]] = set()
    observed_link_ids: set[int] = set()
    link_changes: list[tuple[DiscoveredLink, str, list[str]]] = []
    for item in batch.links:
        left = observed_ports.get((item.a_device_remote_id, item.a_port_remote_id))
        right = observed_ports.get((item.b_device_remote_id, item.b_port_remote_id))
        if left is None or right is None or left.id == right.id:
            raise ValueError("Link requires two distinct ports in this batch")
        if item.confidence not in {"confirmed", "inferred", "ambiguous"} or not item.evidence:
            raise ValueError("Link requires evidence and a recognized confidence")
        a_port_id, b_port_id = sorted((left.id, right.id))
        if (a_port_id, b_port_id) in unique_links:
            raise ValueError("Duplicate link in one batch")
        unique_links.add((a_port_id, b_port_id))
        link = db.scalar(
            select(DiscoveredLink).where(
                DiscoveredLink.source_id == source.id,
                DiscoveredLink.a_port_id == a_port_id,
                DiscoveredLink.b_port_id == b_port_id,
            )
        )
        if link is None:
            link = DiscoveredLink(
                source_id=source.id,
                a_port_id=a_port_id,
                b_port_id=b_port_id,
            )
            db.add(link)
            action = "discovery.link.created"
            changed_fields = []
        else:
            changed_fields = [
                field for field, value in (
                    ("evidence", item.evidence), ("confidence", item.confidence),
                ) if getattr(link, field) != value
            ]
            action = "discovery.link.changed"
        link.evidence = item.evidence
        link.confidence = item.confidence
        link.last_seen_at = timestamp
        db.flush()
        observed_link_ids.add(link.id)
        if action == "discovery.link.created" or changed_fields:
            link_changes.append((link, action, changed_fields))
    db.flush()
    _audit_changes(db, source, "discovered_link", link_changes)
    return observed_link_ids


def _remove_stale_links(db: Session, source: DiscoverySource, observed_link_ids: set[int]) -> None:
    """Delete this source's links the batch no longer reports.

    Each successful adapter batch is a complete snapshot of its link data,
    so vanished observations go and stale evidence cannot keep ports green.

    Args:
        db: The open session.
        source: The reporting source.
        observed_link_ids: The links the batch reported.
    """
    stale_link_ids = [
        link.id for link in db.scalars(select(DiscoveredLink).where(
            DiscoveredLink.source_id == source.id,
            # Cross-source LLDP links are rebuilt by collector.links after
            # both UniFi and Proxmox have completed a successful poll; UniFi
            # client inferences by client_links and nut_wired afterwards.
            DiscoveredLink.evidence.notin_({
                "lldp-proxmox", "lldp-proxmox-name", "lldp-user-match", "unifi-client-mac",
                "unifi-client-ip",
            }),
        )) if link.id not in observed_link_ids
    ]
    if stale_link_ids:
        db.execute(delete(DiscoveredLink).where(DiscoveredLink.id.in_(stale_link_ids)))



def _remove_unreported(db: Session, model: Any, complete: set[str],
                       observed_devices: dict[str, DiscoveredDevice], observed: Any) -> None:
    """Delete a device's ports or interfaces missing from a complete list.

    Args:
        db: The open session.
        model: ``DiscoveredPort`` or ``DiscoveredNetworkInterface``.
        complete: Remote IDs of the devices whose list is complete.
        observed_devices: This batch's device rows by remote ID.
        observed: The rows the batch reported, of that model.
    """
    observed = list(observed)
    for remote_id in complete:
        device = observed_devices[remote_id]
        current_ids = {row.id for row in observed if row.discovered_device_id == device.id}
        stale_ids = [
            row.id for row in db.scalars(select(model).where(model.discovered_device_id == device.id))
            if row.id not in current_ids
        ]
        if stale_ids:
            db.execute(delete(model).where(model.id.in_(stale_ids)))


def record_wan(
    db: Session,
    source: DiscoverySource,
    reading: WanObservation,
    seen_at: datetime | None = None,
) -> bool:
    """Store a gateway's live uplink state and extend its one-day history.

    The snapshot replaces the gateway observation's ``wan`` detail. History
    keeps five-minute buckets of the averaged receive and transmit rates and
    latency over all up uplinks, plus whether any enabled uplink was down;
    buckets older than a day are dropped. ``vpn_history`` keeps the same
    buckets per VPN tunnel (``_extend_vpn_history``). None of these keys
    counts as a device change, and no audit event is written.

    Args:
        db: Open transaction session.
        source: The source that read the gateway.
        reading: Gateway MAC and snapshot from the adapter.
        seen_at: Reading time; defaults to now in UTC.

    Returns:
        bool: True when a matching gateway observation was updated.
    """
    timestamp = seen_at or datetime.now(timezone.utc)
    mac = reading.gateway_mac.lower()
    gateway = next((
        device for device in db.scalars(select(DiscoveredDevice).where(
            DiscoveredDevice.source_id == source.id,
        )) if str(json_object(device.identifiers_json).get("mac", "")).lower() == mac
    ), None)
    if gateway is None:
        return False
    details = json_object(gateway.details_json)
    snapshot = dict(reading.snapshot)
    # A restarted adapter has no in-memory VPN cache. An unavailable optional
    # endpoint must not erase the last persisted, sanitized VPN sample.
    vpn = snapshot.get("vpn")
    previous_wan = details.get("wan")
    previous_vpn = previous_wan.get("vpn") if isinstance(previous_wan, dict) else None
    if (isinstance(vpn, dict) and not vpn.get("available") and not vpn.get("sampled_at")
            and isinstance(previous_vpn, dict)):
        snapshot["vpn"] = {**previous_vpn, "available": False}
    snapshot["sampled_at"] = timestamp.isoformat()
    details["wan"] = snapshot
    uplinks = [item for item in snapshot.get("uplinks", []) if isinstance(item, dict)]
    live = [item for item in uplinks if item.get("up")]
    rx = sum(item.get("rx_bps") or 0 for item in live)
    tx = sum(item.get("tx_bps") or 0 for item in live)
    latencies = [item["latency_ms"] for item in live if isinstance(item.get("latency_ms"), (int, float))]
    latency = min(latencies) if latencies else None
    degraded = any(item.get("enabled") and not item.get("up") for item in uplinks)
    bucket = int(timestamp.timestamp()) // WAN_HISTORY_BUCKET_SECONDS * WAN_HISTORY_BUCKET_SECONDS
    history = [row for row in details.get("wan_history", []) if isinstance(row, list) and len(row) == 6
               and row[0] > bucket - WAN_HISTORY_SECONDS]
    if history and history[-1][0] == bucket:
        start, avg_rx, avg_tx, avg_latency, count, was_degraded = history[-1]
        total = count + 1
        history[-1] = [
            start, (avg_rx * count + rx) / total, (avg_tx * count + tx) / total,
            avg_latency if latency is None else latency if avg_latency is None
            else (avg_latency * count + latency) / total,
            total, bool(was_degraded or degraded),
        ]
    else:
        history.append([bucket, rx, tx, latency, 1, degraded])
    details["wan_history"] = history
    vpn_history = _extend_vpn_history(details.get("vpn_history"), snapshot.get("vpn"), bucket)
    if vpn_history or "vpn_history" in details:
        details["vpn_history"] = vpn_history
    gateway.details_json = _json_text(details)
    db.flush()
    return True


def record_telemetry(
    db: Session,
    source: DiscoverySource,
    remote_id: str,
    key: str,
    value: Mapping[str, Any],
) -> bool:
    """Replace one telemetry detail on an existing observation, unaudited.

    Used by fast polls between scans (such as UPS readings). Only keys in
    :data:`TELEMETRY_DETAIL_KEYS` may be written this way, and only on an
    observation a scan already created.

    Args:
        db: Open transaction session.
        source: The source that owns the observation.
        remote_id: The observation's source-scoped remote ID.
        key: Telemetry detail key.
        value: New telemetry value.

    Returns:
        bool: True when the observation exists and was updated.

    Raises:
        ValueError: ``key`` is not a telemetry key.
    """
    if key not in TELEMETRY_DETAIL_KEYS:
        raise ValueError(f"Not a telemetry detail key: {key}")
    device = db.scalar(select(DiscoveredDevice).where(
        DiscoveredDevice.source_id == source.id,
        DiscoveredDevice.remote_id == remote_id,
    ))
    if device is None:
        return False
    details = json_object(device.details_json)
    details[key] = dict(value)
    device.details_json = _json_text(details)
    db.flush()
    return True
