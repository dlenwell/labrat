# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Rack, shelf, and surface placement rules for explicit inventory edits.

Locations use them for rack edits and removals, rack devices for
placement and arrangement, and backups for rack space checks on restore.
Only explicit user actions reach these; collector data never changes
placement.

Used by: api.locations, api.rack_devices, api.topology, inventory_backup.
"""

import json
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from labrat.api.common import PlacementError, positive_int
from labrat.models import Device, Rack
from labrat.placement import DevicePlacement, PlacementContainer


def layout_position(value: Any, allowed: set[str], *, required: bool = False) -> str | None:
    """Validate relative canvas coordinates and encode them as JSON.

    Args:
        value: Untrusted coordinate object from a JSON request.
        allowed: Coordinate names valid for this surface, such as ``x``/``y``
            or ``along`` for a wall.
        required: Whether the request must include coordinates.

    Returns:
        str | None: Compact JSON for the validated relative coordinates.

    Raises:
        PlacementError: The object contains unknown, missing, or out-of-range
            coordinates. Values are fractions and never physical measurements.
    """
    if value is None:
        if required:
            raise PlacementError("A relative position is required for this location")
        return None
    if not isinstance(value, dict) or not value or set(value) != allowed:
        raise PlacementError("Position must contain supported relative coordinates")
    for coordinate in allowed:
        if coordinate not in value:
            continue
        amount = value[coordinate]
        if (
            type(amount) not in {int, float}
            or amount < 0 or amount > 1 or not math.isfinite(amount)
        ):
            raise PlacementError(f"Position {coordinate} must be between 0 and 1")
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


#: A shelf's optional size, as fractions of the room (never real measures):
#: ``width`` along its wall, ``depth`` out from it.
SHELF_SIZE_KEYS = ("width", "depth")


def shelf_size(value: Any, before: dict[str, Any] | None) -> dict[str, float]:
    """Validate a shelf's relative size, keeping the saved one when absent.

    Args:
        value: Untrusted position object that may carry ``width``/``depth``.
        before: The shelf's stored position, whose size is kept for any key
            the request leaves out (a move without a resize).

    Returns:
        dict[str, float]: The size keys to store; empty for an unsized shelf.

    Raises:
        PlacementError: A size is not a number between 0.02 and 1.
    """
    size = {key: before[key] for key in SHELF_SIZE_KEYS if before and key in before}
    for key in SHELF_SIZE_KEYS:
        if not isinstance(value, dict) or key not in value:
            continue
        amount = value[key]
        if type(amount) not in {int, float} or not math.isfinite(amount) or not 0.02 <= amount <= 1:
            raise PlacementError(f"Shelf {key} must be between 0.02 and 1")
        size[key] = amount
    return size


def layout_value(value: str | None) -> dict[str, Any] | None:
    """Decode a stored coordinate object for an authenticated API response.

    Args:
        value: JSON text stored with an inventory location or placement.

    Returns:
        dict[str, Any] | None: Relative coordinates, or ``None`` when absent.
    """
    if value is None:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def port_facing(value: Any) -> str | None:
    """Validate the direction of a device's network ports independently.

    Args:
        value: ``front``, ``rear``, or ``None`` from a placement request.

    Returns:
        str | None: Validated port direction.

    Raises:
        PlacementError: A direction other than front or rear was submitted.
    """
    if value is None:
        return None
    if not isinstance(value, str) or value not in {"front", "rear"}:
        raise PlacementError("Network ports must face front or rear")
    return value


def placement_snapshot(db: Session, device_id: int | None) -> dict[str, Any] | None:
    """Describe a device's active placement without changing its state.

    Args:
        db: Open SQLAlchemy session.
        device_id: Canonical device identifier, if matched.

    Returns:
        dict[str, Any] | None: Location details or no assigned placement.
    """
    if device_id is None:
        return None
    place = db.get(DevicePlacement, device_id)
    if place is None:
        return None
    container = db.get(PlacementContainer, place.container_id)
    device = db.get(Device, device_id)
    return {
        "device_id": device_id, "container_id": place.container_id,
        "container_name": container.name if container is not None else None,
        "container_kind": container.kind if container is not None else None,
        "container_room_id": container.room_id if container is not None else None,
        "wall_side": container.wall_side if container is not None else None,
        "rack_u": place.rack_u, "face": place.face,
        "port_facing": device.port_facing if device is not None else None,
        "position": layout_value(place.position_json),
    }


def check_rack_space(
    db: Session, container: PlacementContainer, device_id: int | None,
    rack_u: int, height: int, face: str,
) -> Rack:
    """Validate a face-specific rack position shared by placement editors.

    Args:
        db: Open inventory transaction.
        container: Selected rack-backed placement container.
        device_id: Device being moved, or ``None`` for a new manual device.
        rack_u: One-based bottom unit.
        height: Number of occupied rack units.
        face: ``front``, ``rear``, or ``full_depth`` rack placement.

    Returns:
        Rack: Backing rack definition after bounds and occupancy checks.

    Raises:
        PlacementError: The position is invalid or occupied on the same face.
    """
    rack = db.get(Rack, container.rack_id)
    if rack is None:
        raise PlacementError("Rack definition is missing", 409)
    if not isinstance(face, str) or face not in {"front", "rear", "full_depth"}:
        raise PlacementError("Rack placement must be front, rear, or full depth")
    if face in {"rear", "full_depth"} and not rack.has_rear:
        raise PlacementError("This rack has no rear placement face")
    if height > 128 or rack_u + height - 1 > rack.height_u:
        raise PlacementError("Device extends beyond the rack")
    placements = list(db.scalars(select(DevicePlacement).where(
        DevicePlacement.container_id == container.id,
    )))
    for occupied in placements:
        if occupied.device_id == device_id:
            continue
        occupied_face = occupied.face or "front"
        if face != "full_depth" and occupied_face != "full_depth" and occupied_face != face:
            continue
        other = db.get(Device, occupied.device_id)
        other_height = other.height_u or 1
        if occupied.rack_u is not None and (
            rack_u <= occupied.rack_u + other_height - 1
            and occupied.rack_u <= rack_u + height - 1
        ):
            raise PlacementError("Rack units are occupied on this face", 409)
    shelves = db.scalars(select(PlacementContainer).where(
        PlacementContainer.parent_id == container.id,
        PlacementContainer.kind == "rack_shelf",
    ))
    for shelf in shelves:
        shelf_face = shelf.rack_face or "front"
        if face != "full_depth" and shelf_face != "full_depth" and shelf_face != face:
            continue
        shelf_u = shelf.rack_u
        if shelf_u is not None and rack_u <= shelf_u <= rack_u + height - 1:
            peer_id = db.scalar(select(DevicePlacement.device_id).where(
                DevicePlacement.container_id == shelf.id,
                DevicePlacement.device_id != device_id,
            ).limit(1))
            if peer_id is not None:
                raise PlacementError("Rack units are occupied on this face", 409)
    return rack


def _rack_shelf_for_slot(
    db: Session, rack_container: PlacementContainer, rack_u: int, face: str,
    device_id: int,
) -> tuple[PlacementContainer, Rack, list[dict[str, Any]]]:
    """Find or create a shared shelf in one rack unit and face.

    Args:
        db: Active inventory transaction.
        rack_container: Container backed by the selected rack.
        rack_u: One-based rack unit consumed by the shared shelf.
        face: Front, rear, or full-depth shelf face.
        device_id: Device being moved into the shared shelf.

    Returns:
        tuple[PlacementContainer, Rack, list[dict[str, Any]]]: Shared shelf,
            rack, and any one-U occupant converted into the shelf.

    Raises:
        PlacementError: Rack bounds, face, or occupancy rules are violated.
    """
    rack = db.get(Rack, rack_container.rack_id)
    if rack is None:
        raise PlacementError("Rack definition is missing", 409)
    if not isinstance(face, str) or face not in {"front", "rear", "full_depth"}:
        raise PlacementError("Rack shelf must use front, rear, or full depth")
    if face in {"rear", "full_depth"} and not rack.has_rear:
        raise PlacementError("This rack has no rear placement face")
    if rack_u > rack.height_u:
        raise PlacementError("Rack shelf extends beyond the rack")

    shelves = list(db.scalars(select(PlacementContainer).where(
        PlacementContainer.parent_id == rack_container.id,
        PlacementContainer.kind == "rack_shelf",
    )))
    matching_shelf = None
    for shelf in shelves:
        shelf_face = shelf.rack_face or "front"
        if shelf.rack_u == rack_u and shelf_face == face:
            matching_shelf = shelf
            continue
        faces_overlap = face == "full_depth" or shelf_face == "full_depth" or face == shelf_face
        if faces_overlap and shelf.rack_u == rack_u:
            raise PlacementError("This rack unit is already used by another shelf face", 409)

    convertible: tuple[DevicePlacement, Device] | None = None
    for placement in db.scalars(select(DevicePlacement).where(
        DevicePlacement.container_id == rack_container.id,
    )):
        if placement.device_id == device_id:
            continue
        occupied_face = placement.face or "front"
        faces_overlap = face == "full_depth" or occupied_face == "full_depth" or face == occupied_face
        if not faces_overlap or placement.rack_u is None:
            continue
        device = db.get(Device, placement.device_id)
        occupied_height = device.height_u or 1 if device is not None else 1
        if placement.rack_u <= rack_u <= placement.rack_u + occupied_height - 1:
            if (
                placement.rack_u == rack_u and occupied_height == 1
                and occupied_face == face and device is not None and convertible is None
            ):
                convertible = (placement, device)
                continue
            raise PlacementError("Rack units are occupied on this face", 409)

    if matching_shelf is not None:
        if convertible is not None:
            raise PlacementError("A regular device already occupies this shared shelf", 409)
        return matching_shelf, rack, []

    label_face = "Full depth" if face == "full_depth" else face.title()
    shelf = PlacementContainer(
        room_id=rack_container.room_id, parent_id=rack_container.id,
        name=f"U{rack_u} {label_face} shared shelf", kind="rack_shelf",
        rack_u=rack_u, rack_face=face,
    )
    db.add(shelf)
    db.flush()
    if convertible is not None:
        old_placement, old_device = convertible
        before = placement_snapshot(db, old_device.id)
        old_placement.container_id = shelf.id
        old_placement.position_json = json.dumps(
            {"x": 0.5}, separators=(",", ":"), sort_keys=True,
        )
        old_device.room_id = rack_container.room_id
        old_device.rack_id = rack.id
        old_device.rack_u = rack_u
        old_device.placement = "rack_shelf"
        db.flush()
        changes = [{
            "device_id": old_device.id, "action": "converted_to_shared_rack_shelf",
            "before": before, "after": placement_snapshot(db, old_device.id),
        }]
    else:
        changes = []
    return shelf, rack, changes


def _device_position(container: PlacementContainer, payload: dict[str, Any]) -> str | None:
    """Validate relative position coordinates for the selected container.

    Args:
        container: Container receiving the device.
        payload: Untrusted placement request fields.

    Returns:
        str | None: Encoded layout data appropriate for the selected container.

    Raises:
        PlacementError: Coordinates do not match the container type.
    """
    if container.kind == "rack":
        return None
    if container.kind == "wall":
        allowed = {"along"}
        default = {"along": 0.5}
    elif container.kind == "shelf":
        allowed = {"x"}
        default = {"x": 0.5}
    elif container.kind == "rack_shelf":
        allowed = {"x"}
        default = {"x": 1.0}
    else:
        allowed = {"x", "y"}
        default = {"x": 0.5, "y": 0.5}
    value = payload.get("position", default)
    return layout_position(value, allowed, required=True)


def _save_shelf_order(
    db: Session, container: PlacementContainer, device_id: int,
    position_json: str | None,
) -> tuple[str, list[dict[str, Any]]]:
    """Repack shelf contents evenly while honoring the requested left-to-right order.

    Args:
        db: Open inventory transaction.
        container: Shelf receiving the device.
        device_id: Device being inserted or dragged.
        position_json: Requested normalized shelf coordinate.

    Returns:
        tuple[str, list[dict[str, Any]]]: The moved device's coordinate and
            before/after positions for any shelf neighbors that shifted.
    """
    requested = layout_value(position_json) or {"x": 0.5}
    target_x = requested.get("x", 0.5)
    peers: list[tuple[float, DevicePlacement]] = []
    for placement in db.scalars(select(DevicePlacement).where(
        DevicePlacement.container_id == container.id,
        DevicePlacement.device_id != device_id,
    )):
        coordinates = layout_value(placement.position_json) or {}
        peer_x = coordinates.get("x", 0.5)
        if type(peer_x) not in {int, float} or not 0 <= peer_x <= 1:
            peer_x = 0.5
        peers.append((peer_x, placement))
    peers.sort(key=lambda item: item[0])
    insertion_index = sum(1 for position, _ in peers if position <= target_x)
    ordered = [placement for _, placement in peers]
    ordered.insert(insertion_index, None)
    device_position = ""
    layout_changes: list[dict[str, Any]] = []
    for index, placement in enumerate(ordered):
        coordinates = {"x": round((index + 1) / (len(ordered) + 1), 6)}
        encoded = json.dumps(coordinates, separators=(",", ":"), sort_keys=True)
        if placement is None:
            device_position = encoded
        else:
            previous = layout_value(placement.position_json)
            if previous != coordinates:
                layout_changes.append({
                    "device_id": placement.device_id,
                    "before": previous,
                    "after": coordinates,
                })
            placement.position_json = encoded
    return device_position, layout_changes


def repack_shelf_after_removal(
    db: Session, container: PlacementContainer, device_id: int,
) -> list[dict[str, Any]]:
    """Close gaps after a device leaves a horizontal shelf.

    Args:
        db: Active inventory transaction.
        container: Previous room or rack shelf.
        device_id: Device moving away from the shelf.

    Returns:
        list[dict[str, Any]]: Other device positions changed by repacking.
    """
    peers: list[tuple[float, DevicePlacement]] = []
    for placement in db.scalars(select(DevicePlacement).where(
        DevicePlacement.container_id == container.id,
        DevicePlacement.device_id != device_id,
    )):
        coordinates = layout_value(placement.position_json) or {}
        peer_x = coordinates.get("x", 0.5)
        if type(peer_x) not in {int, float} or not 0 <= peer_x <= 1:
            peer_x = 0.5
        peers.append((peer_x, placement))
    peers.sort(key=lambda item: item[0])
    changes: list[dict[str, Any]] = []
    for index, (_, placement) in enumerate(peers):
        coordinates = {"x": round((index + 1) / (len(peers) + 1), 6)}
        before = layout_value(placement.position_json)
        if before != coordinates:
            changes.append({"device_id": placement.device_id, "before": before, "after": coordinates})
            placement.position_json = json.dumps(
                coordinates, separators=(",", ":"), sort_keys=True,
            )
    return changes


def apply_device_placement(
    db: Session, device: Device, container: PlacementContainer,
    payload: dict[str, Any], *, requested_height: int | None = None,
) -> tuple[DevicePlacement, list[dict[str, Any]]]:
    """Validate and apply one device's rack or room-container placement.

    Args:
        db: Open inventory transaction.
        device: Canonical inventory device being placed.
        container: Selected rack, wall, floor, shelf, or rack-top surface.
        payload: User-submitted placement fields.
        requested_height: Optional rack height to store for a manual edit.

    Returns:
        DevicePlacement: The updated active placement row.

    Raises:
        PlacementError: Rack bounds, collision, or coordinate rules are violated.
    """
    placement = db.get(DevicePlacement, device.id)
    previous_container = db.get(PlacementContainer, placement.container_id) if placement else None
    rack: Rack | None = None
    rack_u: int | None = None
    face: str | None = None
    shelf_layout_changes: list[dict[str, Any]] = []
    # "ceiling" (revision 0010) takes x/y like the floor, for overhead gear such as APs.
    if container.kind not in {
        "rack", "rack_shelf", "rack_top", "wall", "floor", "ceiling", "area", "surface", "shelf",
    }:
        raise PlacementError("This container does not accept equipment")
    placement_mode = payload.get("placement_mode")
    if container.kind == "rack" and placement_mode == "rack_shelf":
        rack_u = positive_int(payload.get("rack_u"), "rack_u")
        face = payload.get("face", "front")
        container, rack, converted_layout_changes = _rack_shelf_for_slot(
            db, container, rack_u, face, device.id,
        )
        shelf_layout_changes.extend(converted_layout_changes)
        position_json = _device_position(container, payload)
        position_json, shelf_layout_changes = _save_shelf_order(
            db, container, device.id, position_json,
        )
    elif container.kind == "rack":
        if placement_mode not in {None, "rack"}:
            raise PlacementError("Choose rack, shared shelf, or on top placement")
        rack_u = positive_int(payload.get("rack_u"), "rack_u")
        face = payload.get("face", "front")
        height = requested_height or device.height_u or 1
        rack = check_rack_space(db, container, device.id, rack_u, height, face)
        device.height_u = height
        position_json = None
    elif container.kind == "rack_shelf":
        parent = db.get(PlacementContainer, container.parent_id) if container.parent_id else None
        if parent is None or parent.kind != "rack" or parent.rack_id is None:
            raise PlacementError("Shared rack shelf is not attached to a rack", 409)
        rack = db.get(Rack, parent.rack_id)
        if rack is None or container.rack_u is None or container.rack_face is None:
            raise PlacementError("Shared rack shelf location is incomplete", 409)
        if container.rack_face in {"rear", "full_depth"} and not rack.has_rear:
            raise PlacementError("This rack has no rear placement face")
        rack_u, face = container.rack_u, container.rack_face
        position_json = _device_position(container, payload)
        position_json, shelf_layout_changes = _save_shelf_order(
            db, container, device.id, position_json,
        )
    else:
        if payload.get("rack_u") is not None or payload.get("face") is not None:
            raise PlacementError("Only rack containers accept U and rack-face positions")
        if container.kind == "rack_top":
            parent = db.get(PlacementContainer, container.parent_id) if container.parent_id else None
            if parent is None or parent.kind != "rack" or parent.rack_id is None:
                raise PlacementError("Rack-top surface is not attached to a rack", 409)
        if container.kind == "wall" and container.wall_side not in {
            "north", "east", "south", "west",
        }:
            raise PlacementError("Wall placement requires a selected area wall", 409)
        position_json = _device_position(container, payload)
        if container.kind == "shelf":
            position_json, shelf_layout_changes = _save_shelf_order(
                db, container, device.id, position_json,
            )

    device.port_facing = port_facing(payload.get("port_facing", device.port_facing))
    if placement is None:
        placement = DevicePlacement(device_id=device.id, container_id=container.id)
        db.add(placement)
    placement.container_id = container.id
    placement.rack_u = rack_u
    placement.face = face
    placement.position_json = position_json
    device.room_id = container.room_id
    device.rack_id = rack.id if rack is not None else None
    device.rack_u = rack_u
    device.placement = container.kind
    if previous_container is not None and previous_container.id != container.id:
        if previous_container.kind in {"shelf", "rack_shelf"}:
            shelf_layout_changes.extend(
                repack_shelf_after_removal(db, previous_container, device.id)
            )
        db.flush()
        if previous_container.kind == "rack_shelf" and db.scalar(select(DevicePlacement.device_id).where(
            DevicePlacement.container_id == previous_container.id,
        ).limit(1)) is None:
            db.delete(previous_container)
    return placement, shelf_layout_changes
