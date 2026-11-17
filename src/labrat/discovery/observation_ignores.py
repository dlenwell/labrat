# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Validate source-scoped ignore identities against the local discovery cache.

Besides operations conditions, a ``device`` rule ignores one observed device
outright: its observation is hidden from discovery (``visibility`` becomes
``ignored``) and stays hidden on later scans (:func:`apply_device_ignores`).

Used by: api.observation_actions, collector.store.
"""
import json
from sqlalchemy import select
from sqlalchemy.orm import Session
from labrat.discovery.models import DiscoveredDevice, DiscoveredPort, DiscoverySource, ObservationIgnore

CONDITIONS = frozenset({"source-fault", "source-overdue", "wan-stale", "wan-down", "wan-degraded",
                        "server-offline", "device-stale", "port-down", "neighbor-unresolved",
                        "ups-low-battery", "ups-on-battery", "ups-alert", "device"})

IGNORED = "ignored"
"""``DiscoveredDevice.visibility`` of an observation a ``device`` rule hides."""

RECOGNIZED = "recognized"
"""``DiscoveredDevice.visibility`` of an ordinary, visible observation."""


def ignored_device_ids(db: Session, source_id: int) -> set[str]:
    """Return the remote device IDs one source's ``device`` rules ignore.

    Args:
        db: Session with migrated tables.
        source_id: Discovery source whose rules to read.

    Returns:
        set[str]: Source-scoped remote device IDs.
    """
    found = set()
    for target_json in db.scalars(select(ObservationIgnore.target_json).where(ObservationIgnore.source_id == source_id)):
        try:
            target = json.loads(target_json)
        except (TypeError, ValueError):
            continue
        if isinstance(target, dict) and target.get("condition") == "device" and isinstance(target.get("device_remote_id"), str):
            found.add(target["device_remote_id"])
    return found


def apply_device_ignores(db: Session, source_id: int) -> int:
    """Hide or show one source's observations to match its ``device`` rules.

    An observation linked to inventory is never hidden. Called after each
    collected batch, so an ignored device stays hidden when it is seen again,
    and by the rule endpoints, so a change shows at once.

    Args:
        db: Session inside the caller's transaction.
        source_id: Discovery source to reconcile.

    Returns:
        int: Observations whose visibility changed.
    """
    ignored = ignored_device_ids(db, source_id)
    query = select(DiscoveredDevice).where(DiscoveredDevice.source_id == source_id)
    if not ignored:
        # Nothing to hide: only rows a removed rule left hidden can change.
        query = query.where(DiscoveredDevice.visibility == IGNORED)
    changed = 0
    for device in db.scalars(query):
        wanted = IGNORED if device.remote_id in ignored and device.device_id is None else RECOGNIZED
        if device.visibility in (IGNORED, RECOGNIZED) and device.visibility != wanted:
            device.visibility = wanted
            changed += 1
    return changed


def ignore_target(db: Session, payload: object) -> tuple[dict, str]:
    """Normalize one exact condition and derive its safe display label.

    Args:
        db: Transaction session; validation makes no network requests.
        payload: Untrusted target object with condition and source identity.

    Returns:
        tuple[dict, str]: Canonical identity and a cache-derived label.

    Raises:
        ValueError: Target is malformed or references absent evidence.
    """
    if (not isinstance(payload, dict) or not isinstance(payload.get("condition"), str)
            or payload["condition"] not in CONDITIONS):
        raise ValueError("Unknown observation condition")
    if type(payload.get("source_id")) is not int:
        raise ValueError("Provide a discovery source")
    source = db.get(DiscoverySource, payload["source_id"])
    if source is None:
        raise ValueError("Discovery source does not exist")
    condition = payload["condition"]
    result = {"condition": condition, "source_id": source.id}
    if condition.startswith("source-"):
        if set(payload) != set(result):
            raise ValueError("Source conditions must target only a source")
        return result, source.name
    remote = payload.get("device_remote_id")
    if not isinstance(remote, str) or not remote:
        raise ValueError("Provide a source-scoped device identity")
    device = db.scalar(select(DiscoveredDevice).where(
        DiscoveredDevice.source_id == source.id, DiscoveredDevice.remote_id == remote,
    ))
    if device is None:
        raise ValueError("Observed device does not exist")
    result["device_remote_id"] = remote
    label = device.name
    if condition == "device" and device.device_id is not None:
        raise ValueError("Devices in inventory cannot be ignored")
    if condition == "server-offline" and device.category != "server":
        raise ValueError("Offline server conditions require a server")
    if condition.startswith("wan-"):
        try:
            details = json.loads(device.details_json or "{}")
        except (TypeError, ValueError):
            details = {}
        if not isinstance(details, dict) or not isinstance(details.get("wan"), dict):
            raise ValueError("Device has no WAN observations")
    if condition.startswith("ups-"):
        try:
            details = json.loads(device.details_json or "{}")
        except (TypeError, ValueError):
            details = {}
        if device.category != "ups" or not isinstance(details, dict) or not isinstance(details.get("ups"), dict):
            raise ValueError("UPS conditions require a UPS with readings")
    if condition in {"port-down", "neighbor-unresolved"}:
        port_remote = payload.get("port_remote_id")
        if not isinstance(port_remote, str):
            raise ValueError("Provide the physical port identity")
        port = db.scalar(select(DiscoveredPort).where(
            DiscoveredPort.discovered_device_id == device.id, DiscoveredPort.remote_id == port_remote,
        ))
        if port is None:
            raise ValueError("Observed physical port does not exist")
        result["port_remote_id"] = port_remote
        label = f"{device.name} / {port.name}"
    if condition == "neighbor-unresolved":
        chassis, remote_port = payload.get("chassis_id"), payload.get("remote_port_id")
        if not isinstance(chassis, str) or not isinstance(remote_port, str):
            raise ValueError("Provide the reported neighbor identity")
        details = json.loads(device.details_json or "{}")
        neighbors = details.get("lldp_neighbors", []) if isinstance(details, dict) else []
        neighbors = neighbors if isinstance(neighbors, list) else []
        if not any(isinstance(n, dict) and n.get("local_port") == port_remote
                   and n.get("chassis_id") == chassis and n.get("port_id") == remote_port
                   for n in neighbors):
            raise ValueError("Neighbor is no longer observed on that port")
        result.update(chassis_id=chassis, remote_port_id=remote_port)
    if set(payload) != set(result):
        raise ValueError("Unexpected fields in observation target")
    return result, label
