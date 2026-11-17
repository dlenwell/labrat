# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Owner decisions about observations: neighbor matches and ignore rules.

Used from the Discovery and Observations drawers. A neighbor match links
an LLDP neighbor to an observed device; an ignore rule hides a condition
or a device until restored. Both keep their evidence.
"""

import json
from typing import Any

from flask import Blueprint, jsonify
from flask.typing import ResponseReturnValue
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from labrat.api.common import as_iso, PlacementError, positive_int, write_payload
from labrat.audit import record_audit_event
from labrat.auth.session import api_login_required, current_user, session_factory
from labrat.coerce import json_object
from labrat.collector.links import reconcile_proxmox_lldp_links
from labrat.discovery.models import (
    DiscoveredDevice, DiscoveredLink, DiscoveredPort, DiscoverySource, ObservationIgnore,
)
from labrat.discovery.neighbor_matches import MATCH_ATTRIBUTE, neighbor_key, saved_neighbor_matches
from labrat.discovery.observation_ignores import apply_device_ignores, ignore_target
from labrat.metadata import Attribute, InventoryObject
from labrat.metadata_values import encode_value


observation_actions_bp = Blueprint("observation_actions_api", __name__, url_prefix="/api/v1")


@observation_actions_bp.post("/neighbor-matches")
@api_login_required
def match_neighbor() -> ResponseReturnValue:
    """Save an explicit neighbor-to-server-NIC identity and refresh cached cables.

    Only a neighbor present in the cached switch observations may be matched.
    The target must be a discovered physical NIC on an existing inventory
    server. Logical interfaces, ambiguous identities, and conflicting cables
    are rejected. No network request or placement change occurs.

    Returns:
        ResponseReturnValue: The saved attribute ID, or a validation error.
    """
    try:
        payload = write_payload()
        switch_id = positive_int(payload.get("switch_observation_id"), "switch_observation_id")
        target_id = positive_int(payload.get("target_port_id"), "target_port_id")
        if (not isinstance(payload.get("local_port"), str) or not payload["local_port"].strip()
                or not isinstance(payload.get("chassis_id"), str) or not payload["chassis_id"].strip()
                or not isinstance(payload.get("port_id"), str)):
            raise PlacementError("Provide the reported switch port, chassis, and remote port identifiers")
        with session_factory().begin() as db:
            # Serialize identity writes: attribute keys intentionally allow repeats.
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            switch = db.get(DiscoveredDevice, switch_id)
            source = db.get(DiscoverySource, switch.source_id) if switch else None
            if (not switch or switch.visibility != "recognized"
                    or switch.category not in {"switch", "router", "firewall", "access_point"}
                    or not source or source.kind != "unifi"):
                raise PlacementError("UniFi switch observation does not exist", 404)
            neighbors = json_object(switch.details_json).get("lldp_neighbors", [])
            neighbor = next((n for n in (neighbors if isinstance(neighbors, list) else []) if isinstance(n, dict)
                             and all(n.get(field) == payload.get(field) for field in
                                     ("local_port", "chassis_id", "port_id"))), None)
            if neighbor is None:
                raise PlacementError("Neighbor is no longer in the cached observations; refresh first", 409)
            key = neighbor_key(source.id, neighbor["chassis_id"], neighbor["port_id"])
            occurrences = set()
            for observed in db.scalars(select(DiscoveredDevice).where(DiscoveredDevice.source_id == source.id)):
                reported = json_object(observed.details_json).get("lldp_neighbors", [])
                for n in reported if isinstance(reported, list) else []:
                    if not isinstance(n, dict) or not all(isinstance(n.get(field), str) for field in ("chassis_id", "port_id")):
                        continue
                    if neighbor_key(source.id, n.get("chassis_id", ""), n.get("port_id", "")) == key:
                        occurrences.add((observed.id, str(n.get("local_port"))))
            if len(occurrences) != 1:
                raise PlacementError("This neighbor identity appears on multiple switch ports", 409)
            local = db.scalar(select(DiscoveredPort).where(
                DiscoveredPort.discovered_device_id == switch.id,
                DiscoveredPort.remote_id == neighbor["local_port"],
            ))
            target = db.get(DiscoveredPort, target_id)
            server = db.get(DiscoveredDevice, target.discovered_device_id) if target else None
            target_source = db.get(DiscoverySource, server.source_id) if server else None
            if (not server or server.category != "server" or not server.device_id
                    or not target_source or target_source.kind != "proxmox" or local is None):
                raise PlacementError("Choose a physical NIC on an existing inventory server")
            for link in db.scalars(select(DiscoveredLink)):
                if {local.id, target.id} & {link.a_port_id, link.b_port_id}:
                    if {local.id, target.id} != {link.a_port_id, link.b_port_id}:
                        raise PlacementError("One of these ports already has a different mapped cable", 409)
            matches = saved_neighbor_matches(db)
            for match in matches:
                if neighbor_key(match["source_id"], match["chassis_id"], match["port_id"]) == key:
                    raise PlacementError("This neighbor already has a saved match; remove it first", 409)
                if (match["target_source_id"], match["target_device_remote_id"], match["target_port_remote_id"]) == (server.source_id, server.remote_id, target.remote_id):
                    raise PlacementError("This NIC already has a saved neighbor match", 409)
            owner = db.scalar(select(InventoryObject).where(InventoryObject.device_id == server.device_id))
            if owner is None:
                raise PlacementError("Inventory owner is unavailable", 409)
            attribute = Attribute(owner_id=owner.id, key=MATCH_ATTRIBUTE, value=encode_value({"json": {
                "source_id": source.id, "chassis_id": neighbor["chassis_id"], "port_id": neighbor["port_id"],
                "target_source_id": server.source_id, "target_device_remote_id": server.remote_id,
                "target_port_remote_id": target.remote_id,
            }}))
            db.add(attribute)
            db.flush()
            actor = current_user()
            record_audit_event(db, actor_kind="user", actor_id=actor.id, actor_name=actor.username,
                               action="discovery.neighbor.matched", subject_kind="attribute", subject_id=attribute.id,
                               details={"switch_observation_id": switch.id, "target_port_id": target.id})
            proxmox = list(db.scalars(select(DiscoverySource).where(DiscoverySource.kind == "proxmox")))
            reconcile_proxmox_lldp_links(db, source, proxmox, seen_at=source.last_success_at)
            result = {"id": attribute.id}
        return jsonify(result), 201
    except PlacementError as exc:
        return jsonify(error=str(exc)), exc.status
    except IntegrityError:
        return jsonify(error="Neighbor match conflicts with current inventory"), 409


@observation_actions_bp.post("/neighbor-matches/<int:match_id>/remove")
@api_login_required
def remove_neighbor_match(match_id: int) -> ResponseReturnValue:
    """Remove an operator identity match and recompute its observed cables.

    Args:
        match_id: ID of the inventory-owned match attribute.

    Returns:
        ResponseReturnValue: Success or a validation error. Automatic evidence
        may still resolve the same cable after the explicit match is removed.
    """
    try:
        write_payload()
        with session_factory().begin() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            match = next((m for m in saved_neighbor_matches(db) if m["id"] == match_id), None)
            if match is None:
                raise PlacementError("Saved neighbor match does not exist", 404)
            db.delete(db.get(Attribute, match_id))
            db.flush()
            actor = current_user()
            record_audit_event(db, actor_kind="user", actor_id=actor.id, actor_name=actor.username,
                               action="discovery.neighbor.unmatched", subject_kind="attribute", subject_id=match_id,
                               details={"device_id": match["device_id"]})
            source = db.get(DiscoverySource, match["source_id"])
            if source:
                proxmox = list(db.scalars(select(DiscoverySource).where(DiscoverySource.kind == "proxmox")))
                reconcile_proxmox_lldp_links(db, source, proxmox, seen_at=source.last_success_at)
        return jsonify(ok=True)
    except PlacementError as exc:
        return jsonify(error=str(exc)), exc.status


def _ignore_record(rule: ObservationIgnore) -> dict[str, Any]:
    """Serialize a shared ignore rule without changing its target identity.

    Args:
        rule: The stored rule.

    Returns:
        dict[str, Any]: Its ID, decoded target, label, and creation time.
    """
    return {"id": rule.id, "target": json_object(rule.target_json), "label": rule.label,
            "created_at": as_iso(rule.created_at)}


@observation_actions_bp.get("/observation-ignores")
@api_login_required
def observation_ignores() -> ResponseReturnValue:
    """List shared observation ignore rules, including currently clear conditions.

    Returns:
        ResponseReturnValue: JSON with every rule, newest first.
    """
    with session_factory()() as db:
        return jsonify(ignores=[_ignore_record(rule) for rule in db.scalars(
            select(ObservationIgnore).order_by(ObservationIgnore.created_at.desc(), ObservationIgnore.id.desc())
        )])


@observation_actions_bp.post("/observation-ignores")
@api_login_required
def ignore_observation() -> ResponseReturnValue:
    """Ignore one exact cached condition with administrator auth, CSRF, and audit.

    Targets use source-scoped remote device/port identities so regenerated
    cable and port rows do not invalidate a rule. Observed links, port state,
    inventory, and collector behavior are unchanged, except that a ``device``
    rule hides that observation from discovery until it is restored. Duplicate saves are
    idempotent and do not write duplicate audit events.

    Returns:
        ResponseReturnValue: JSON with the saved rule, or an error with its
            HTTP status.
    """
    try:
        payload = write_payload()
        with session_factory().begin() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            target, label = ignore_target(db, payload.get("target"))
            canonical = json.dumps(target, sort_keys=True, separators=(",", ":"))
            rule = db.scalar(select(ObservationIgnore).where(ObservationIgnore.target_json == canonical))
            if rule is None:
                actor = current_user()
                rule = ObservationIgnore(source_id=target["source_id"], target_json=canonical,
                                         label=label[:300], created_by=actor.id)
                db.add(rule)
                db.flush()
                record_audit_event(db, actor_kind="user", actor_id=actor.id, actor_name=actor.username,
                                   action="observation.ignored", subject_kind="observation_ignore", subject_id=rule.id,
                                   details={"condition": target["condition"], "source_id": target["source_id"]})
            if target["condition"] == "device":
                apply_device_ignores(db, target["source_id"])
            result = _ignore_record(rule)
        return jsonify(ignore=result)
    except PlacementError as exc:
        return jsonify(error=str(exc)), exc.status
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@observation_actions_bp.post("/observation-ignores/<int:rule_id>/remove")
@api_login_required
def restore_observation(rule_id: int) -> ResponseReturnValue:
    """Restore one ignored condition; no discovery evidence is changed.

    Args:
        rule_id: Saved policy ID to remove. The audit trail remains intact.

    Returns:
        ResponseReturnValue: JSON confirming the removal, or an error with its
            HTTP status.
    """
    try:
        write_payload()
        with session_factory().begin() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            rule = db.get(ObservationIgnore, rule_id)
            if rule is None:
                raise PlacementError("Ignore rule does not exist", 404)
            actor = current_user()
            record_audit_event(db, actor_kind="user", actor_id=actor.id, actor_name=actor.username,
                               action="observation.restored", subject_kind="observation_ignore", subject_id=rule.id,
                               details={"condition": json_object(rule.target_json).get("condition"), "source_id": rule.source_id})
            db.delete(rule)
            if json_object(rule.target_json).get("condition") == "device":
                db.flush()
                apply_device_ignores(db, rule.source_id)
        return jsonify(ok=True)
    except PlacementError as exc:
        return jsonify(error=str(exc)), exc.status
