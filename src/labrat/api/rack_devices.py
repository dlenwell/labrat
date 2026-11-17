# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Inventory devices in places: place, rename, add manual equipment, arrange.

Placement goes through ``api.placement_rules``; every write is explicit,
CSRF-protected, and audited, and never changes discovery observations.
"""

import json
from typing import Any

from flask import Blueprint, jsonify
from flask.typing import ResponseReturnValue
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from labrat.api.common import (
    is_virtual_guest, PlacementError, positive_int, valid_name, write_payload,
)
from labrat.api.placement_rules import (
    apply_device_placement, layout_value, placement_snapshot, port_facing,
    repack_shelf_after_removal,
)
from labrat.audit import record_audit_event
from labrat.auth.session import api_login_required, current_user, session_factory
from labrat.discovery.models import DiscoveredDevice
from labrat.models import Device, DeviceClass
from labrat.placement import DevicePlacement, PlacementContainer


rack_devices_bp = Blueprint("rack_devices_api", __name__, url_prefix="/api/v1")


@rack_devices_bp.post("/placements")
@api_login_required
def place_discovered_device() -> ResponseReturnValue:
    """Match an observed device to inventory and give it one physical placement.

    The operation preserves discovered observations and network links. A rack
    move validates that the full height fits without colliding with another
    placed device. No inferred identity matching is performed.

    Returns:
        ResponseReturnValue: Canonical ID and placement, or validation error.
    """
    try:
        payload = write_payload()
        observation_id = positive_int(payload.get("discovery_id"), "discovery_id")
        container_id = positive_int(payload.get("container_id"), "container_id")
        existing_id = positive_int(payload.get("existing_device_id"), "existing_device_id", required=False)
        with session_factory().begin() as db:
            observed = db.get(DiscoveredDevice, observation_id)
            container = db.get(PlacementContainer, container_id)
            if observed is None or observed.visibility != "recognized":
                raise PlacementError("Discovery observation does not exist", 404)
            if container is None:
                raise PlacementError("Mounting location does not exist", 404)
            if is_virtual_guest(observed):
                raise PlacementError("Virtual machines and containers are not placed as physical equipment", 409)
            if observed.device_id is not None and existing_id not in {None, observed.device_id}:
                raise PlacementError("This observation is already linked to another inventory device", 409)
            device_id = observed.device_id or existing_id
            device = db.get(Device, device_id) if device_id else None
            if device_id is not None and device is None:
                raise PlacementError("Inventory device does not exist", 404)
            if device is not None and observed.device_id is None:
                same_source = db.scalar(select(DiscoveredDevice.id).where(
                    DiscoveredDevice.device_id == device.id,
                    DiscoveredDevice.source_id == observed.source_id,
                    DiscoveredDevice.id != observed.id,
                ).limit(1))
                if same_source is not None:
                    raise PlacementError(
                        "This inventory device already represents different equipment "
                        "from the same discovery source", 409,
                    )
            if device is None:
                device = Device(name=observed.name, height_u=1)
                db.add(device)
                db.flush()
            old_placement = placement_snapshot(db, device.id)
            requested_height = positive_int(
                payload.get("height_u"), "height_u", required=False
            )
            _, shelf_layout_changes = apply_device_placement(
                db, device, container, payload, requested_height=requested_height,
            )
            observed.device_id = device.id
            db.flush()
            actor = current_user()
            record_audit_event(
                db, actor_kind="user", actor_id=actor.id,
                actor_name=actor.username, action="inventory.device.placed",
                subject_kind="device", subject_id=device.id,
                details={
                    "discovery_id": observation_id,
                    "before": old_placement,
                    "after": placement_snapshot(db, device.id),
                    "shelf_layout_changes": shelf_layout_changes,
                },
            )
            result = {"device_id": device.id, "placement": placement_snapshot(db, device.id)}
        return jsonify(result), 200
    except PlacementError as exc:
        return jsonify(error=str(exc)), exc.status
    except IntegrityError:
        return jsonify(error="Placement conflicts with current inventory"), 409


@rack_devices_bp.post("/rack-devices/<int:device_id>/name")
@api_login_required
def rename_inventory_device(device_id: int) -> ResponseReturnValue:
    """Save a user-owned display name without changing a source observation.

    The canonical inventory name is the label shown across Equipment,
    Discovery, Observations, and room views. Collector scans continue to write
    their own ``discovered_devices.name``; they cannot overwrite this label.

    Args:
        device_id: Canonical inventory device selected by the administrator.

    Returns:
        ResponseReturnValue: Saved display name or a safe validation error.
    """
    try:
        payload = write_payload()
        label = valid_name(payload.get("name"))
        with session_factory().begin() as db:
            device = db.get(Device, device_id)
            if device is None:
                raise PlacementError("Inventory device does not exist", 404)
            before = device.name
            if before != label:
                device.name = label
                actor = current_user()
                record_audit_event(
                    db, actor_kind="user", actor_id=actor.id,
                    actor_name=actor.username, action="inventory.device.renamed",
                    subject_kind="device", subject_id=device.id,
                    details={"before": before, "after": label},
                )
            result = {"device_id": device.id, "name": device.name}
        return jsonify(result), 200
    except PlacementError as exc:
        return jsonify(error=str(exc)), exc.status


@rack_devices_bp.post("/rack-devices")
@api_login_required
def create_manual_rack_device() -> ResponseReturnValue:
    """Create audited manual equipment, optionally placed in a room container.

    This path covers equipment without a discovery adapter, such as a UPS or
    WAN appliance. A device may start unplaced or be assigned to any supported
    rack, rack-top, wall, floor, or shelf container.

    Returns:
        ResponseReturnValue: New inventory device identifier or a safe error.
    """
    try:
        payload = write_payload()
        label = valid_name(payload.get("name"))
        height = positive_int(payload.get("height_u"), "height_u")
        if height > 128:
            raise PlacementError("Device height must not exceed 128U")
        class_name = payload.get("class_name")
        if class_name is not None and (
            not isinstance(class_name, str) or len(class_name.strip()) > 80
        ):
            raise PlacementError("Equipment class must contain at most 80 characters")
        class_name = class_name.strip() if class_name else None
        container_id = positive_int(
            payload.get("container_id"), "container_id", required=False
        )
        with session_factory().begin() as db:
            device_class = None
            if class_name:
                device_class = db.scalar(select(DeviceClass).where(DeviceClass.name == class_name))
                if device_class is None:
                    device_class = DeviceClass(name=class_name)
                    db.add(device_class)
                    db.flush()
            device = Device(
                name=label, height_u=height,
                device_class_id=device_class.id if device_class else None,
                port_facing=port_facing(payload.get("port_facing")),
            )
            db.add(device)
            db.flush()
            if container_id is not None:
                container = db.get(PlacementContainer, container_id)
                if container is None:
                    raise PlacementError("Selected placement container does not exist", 404)
                _, shelf_layout_changes = apply_device_placement(
                    db, device, container, payload, requested_height=height,
                )
            else:
                shelf_layout_changes = []
            db.flush()
            actor = current_user()
            record_audit_event(
                db, actor_kind="user", actor_id=actor.id,
                actor_name=actor.username, action="inventory.device.created",
                subject_kind="device", subject_id=device.id,
                details={"name": label, "class_name": class_name,
                         "shelf_layout_changes": shelf_layout_changes,
                         "placement": placement_snapshot(db, device.id)},
            )
            result = {"device_id": device.id, "placement": placement_snapshot(db, device.id)}
        return jsonify(result), 201
    except PlacementError as exc:
        return jsonify(error=str(exc)), exc.status
    except IntegrityError:
        return jsonify(error="Manual equipment conflicts with current inventory"), 409


@rack_devices_bp.post("/rack-devices/<int:device_id>/placement")
@api_login_required
def move_manual_rack_device(device_id: int) -> ResponseReturnValue:
    """Place or move an inventory device between racks.

    Args:
        device_id: Canonical device identifier selected by the administrator.

    Returns:
        ResponseReturnValue: Updated placement or a validation error.
    """
    try:
        payload = write_payload()
        container_id = positive_int(payload.get("container_id"), "container_id")
        height = positive_int(payload.get("height_u"), "height_u", required=False)
        with session_factory().begin() as db:
            device = db.get(Device, device_id)
            container = db.get(PlacementContainer, container_id)
            if device is None:
                raise PlacementError("Inventory device does not exist", 404)
            if container is None:
                raise PlacementError("Selected placement container does not exist", 404)
            before = placement_snapshot(db, device.id)
            _, shelf_layout_changes = apply_device_placement(
                db, device, container, payload, requested_height=height,
            )
            db.flush()
            actor = current_user()
            record_audit_event(
                db, actor_kind="user", actor_id=actor.id,
                actor_name=actor.username, action="inventory.device.placed",
                subject_kind="device", subject_id=device.id,
                details={
                    "before": before, "after": placement_snapshot(db, device.id),
                    "shelf_layout_changes": shelf_layout_changes,
                },
            )
            result = {"device_id": device.id, "placement": placement_snapshot(db, device.id)}
        return jsonify(result), 200
    except PlacementError as exc:
        return jsonify(error=str(exc)), exc.status
    except IntegrityError:
        return jsonify(error="Placement conflicts with current inventory"), 409


#: Largest arrangement accepted in one request; a room rarely holds this many.
_MAX_ARRANGEMENT_MOVES = 256


def _arrangement_position(value: Any, field: str) -> tuple[int, int]:
    """Validate one ``{"container_id", "rack_u"}`` end of an arrangement move.

    Args:
        value: Untrusted decoded JSON value.
        field: Field label (``from`` or ``to``) used in validation errors.

    Returns:
        tuple[int, int]: Rack container ID and one-based bottom unit.

    Raises:
        PlacementError: The value is not an object with two positive integers.
    """
    if not isinstance(value, dict):
        raise PlacementError(f"{field} must be an object with container_id and rack_u")
    return (
        positive_int(value.get("container_id"), f"{field}.container_id"),
        positive_int(value.get("rack_u"), f"{field}.rack_u"),
    )


@rack_devices_bp.post("/rack-arrangement")
@api_login_required
def save_rack_arrangement() -> ResponseReturnValue:
    """Move rack devices and reorder rack tops in one transaction.

    Saves the Equipment view's arrange mode. Each entry in ``moves`` names
    the device, the rack position the browser started from, and its new rack
    position; every device keeps its face and height. Each entry in
    ``top_orders`` lists every device on one rack top, left to right, and
    stores evenly spaced ``x`` positions in that order. All racks involved
    must be in one room. The final rack layout is validated as a whole, so
    devices may swap places; if any position is invalid or occupied, or the
    stated starting point or rack-top contents no longer match, nothing is
    saved. Each device whose placement changes gets its own audit event.

    Returns:
        ResponseReturnValue: Updated placements, or a validation error.
    """
    try:
        payload = write_payload()
        moves = payload.get("moves", [])
        top_orders = payload.get("top_orders", [])
        if not isinstance(moves, list) or not isinstance(top_orders, list):
            raise PlacementError("moves and top_orders must be lists")
        if not moves and not top_orders:
            raise PlacementError("Nothing to save: send moves or top_orders")
        if len(moves) > _MAX_ARRANGEMENT_MOVES or len(top_orders) > _MAX_ARRANGEMENT_MOVES:
            raise PlacementError(f"At most {_MAX_ARRANGEMENT_MOVES} moves can be saved at once")
        parsed: list[tuple[int, tuple[int, int], tuple[int, int]]] = []
        for move in moves:
            if not isinstance(move, dict):
                raise PlacementError("Each move must be an object")
            parsed.append((
                positive_int(move.get("device_id"), "device_id"),
                _arrangement_position(move.get("from"), "from"),
                _arrangement_position(move.get("to"), "to"),
            ))
        if len({device_id for device_id, _, _ in parsed}) != len(parsed):
            raise PlacementError("Each device can be moved only once per arrangement")
        parsed_tops: list[tuple[int, list[int]]] = []
        for order in top_orders:
            if not isinstance(order, dict) or not isinstance(order.get("device_ids"), list):
                raise PlacementError("Each top order must be an object with container_id and device_ids")
            container_id = positive_int(order.get("container_id"), "container_id")
            device_ids = [positive_int(value, "device_ids") for value in order["device_ids"]]
            if not device_ids or len(set(device_ids)) != len(device_ids):
                raise PlacementError("A top order must list each device on that rack top once")
            parsed_tops.append((container_id, device_ids))
        if len({container_id for container_id, _ in parsed_tops}) != len(parsed_tops):
            raise PlacementError("Each rack top can be reordered only once per arrangement")
        with session_factory().begin() as db:
            rooms: set[int | None] = set()
            work: list[tuple[Device, DevicePlacement, PlacementContainer, int, dict[str, Any] | None]] = []
            for device_id, (from_container, from_u), (to_container, to_u) in parsed:
                device = db.get(Device, device_id)
                placement = db.get(DevicePlacement, device_id)
                if device is None:
                    raise PlacementError("Inventory device does not exist", 404)
                current = db.get(PlacementContainer, placement.container_id) if placement else None
                if current is None or current.kind != "rack":
                    raise PlacementError(f"{device.name} is not mounted in a rack", 409)
                if (placement.container_id, placement.rack_u) != (from_container, from_u):
                    raise PlacementError(
                        f"{device.name} has moved since arranging began; reload and try again", 409,
                    )
                target = db.get(PlacementContainer, to_container)
                if target is None:
                    raise PlacementError("Selected rack does not exist", 404)
                if target.kind != "rack":
                    raise PlacementError("Arrangement moves must stay in racks")
                rooms.update({current.room_id, target.room_id})
                work.append((device, placement, target, to_u, placement_snapshot(db, device.id)))
            top_work: list[tuple[DevicePlacement, dict[str, Any], dict[str, Any] | None]] = []
            for container_id, device_ids in parsed_tops:
                top = db.get(PlacementContainer, container_id)
                if top is None:
                    raise PlacementError("Selected rack top does not exist", 404)
                if top.kind != "rack_top":
                    raise PlacementError("Top orders must name a rack-top surface")
                rooms.add(top.room_id)
                current_ids = set(db.scalars(select(DevicePlacement.device_id).where(
                    DevicePlacement.container_id == top.id,
                )))
                if current_ids != set(device_ids):
                    raise PlacementError(
                        "Equipment on that rack top has changed since arranging began; "
                        "reload and try again", 409,
                    )
                for index, device_id in enumerate(device_ids):
                    placement = db.get(DevicePlacement, device_id)
                    position = layout_value(placement.position_json) or {}
                    # Keep a valid stored y; arrange mode orders left to right only.
                    kept_y = position.get("y")
                    if type(kept_y) not in {int, float} or not 0 <= kept_y <= 1:
                        kept_y = 0.5
                    after = {"x": round((index + 1) / (len(device_ids) + 1), 6), "y": kept_y}
                    if position != after:
                        top_work.append((placement, after, placement_snapshot(db, device_id)))
            if len(rooms) != 1:
                raise PlacementError("Arrangement moves must stay within one area")
            # Lift every moved device out first so the final layout is checked
            # as a whole: two devices may trade places.
            for _, placement, _, _, _ in work:
                placement.rack_u = None
            for device, placement, target, to_u, _ in work:
                apply_device_placement(db, device, target, {
                    "placement_mode": "rack", "rack_u": to_u, "face": placement.face or "front",
                })
            for placement, position, _ in top_work:
                placement.position_json = json.dumps(position, separators=(",", ":"), sort_keys=True)
            db.flush()
            actor = current_user()
            placements = []
            changed = [(device.id, before) for device, _, _, _, before in work] + [
                (placement.device_id, before) for placement, _, before in top_work]
            for device_id, before in changed:
                after = placement_snapshot(db, device_id)
                record_audit_event(
                    db, actor_kind="user", actor_id=actor.id,
                    actor_name=actor.username, action="inventory.device.placed",
                    subject_kind="device", subject_id=device_id,
                    details={
                        "before": before, "after": after, "shelf_layout_changes": [],
                        "arrangement_moves": len(changed),
                    },
                )
                placements.append(after)
        return jsonify(placements=placements), 200
    except PlacementError as exc:
        return jsonify(error=str(exc)), exc.status
    except IntegrityError:
        return jsonify(error="Placement conflicts with current inventory"), 409


@rack_devices_bp.post("/rack-devices/<int:device_id>/unplace")
@api_login_required
def unplace_rack_device(device_id: int) -> ResponseReturnValue:
    """Remove a device's active placement while preserving its identity.

    A discovered observation, its ports, and its confirmed links remain
    attached to the canonical device. The legacy location fields are cleared
    with the placement row in one audited transaction.

    Args:
        device_id: Canonical device identifier selected by the administrator.

    Returns:
        ResponseReturnValue: Unplaced device identifier or validation error.
    """
    try:
        write_payload()
        with session_factory().begin() as db:
            device = db.get(Device, device_id)
            if device is None:
                raise PlacementError("Inventory device does not exist", 404)
            before = placement_snapshot(db, device.id)
            if before is None:
                return jsonify(device_id=device.id, placement=None), 200
            placement = db.get(DevicePlacement, device.id)
            container = db.get(PlacementContainer, placement.container_id)
            shelf_layout_changes = []
            if container is not None and container.kind in {"shelf", "rack_shelf"}:
                shelf_layout_changes = repack_shelf_after_removal(db, container, device.id)
            db.delete(placement)
            device.room_id = None
            device.rack_id = None
            device.rack_u = None
            device.placement = None
            db.flush()
            if container is not None and container.kind == "rack_shelf" and db.scalar(
                select(DevicePlacement.device_id).where(
                    DevicePlacement.container_id == container.id,
                ).limit(1)
            ) is None:
                db.delete(container)
                db.flush()
            actor = current_user()
            record_audit_event(
                db, actor_kind="user", actor_id=actor.id,
                actor_name=actor.username, action="inventory.device.unplaced",
                subject_kind="device", subject_id=device.id,
                details={"before": before, "after": None,
                         "shelf_layout_changes": shelf_layout_changes},
            )
            result = {"device_id": device.id, "placement": None}
        return jsonify(result), 200
    except PlacementError as exc:
        return jsonify(error=str(exc)), exc.status
    except IntegrityError:
        return jsonify(error="Placement conflicts with current inventory"), 409
