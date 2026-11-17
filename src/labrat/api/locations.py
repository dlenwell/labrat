# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Locations, areas, racks, and containers: list, create, rename, lay out, delete.

Every write requires an administrator and the session's CSRF token and
records an audit event. Deleting a place unplaces its equipment rather than
deleting inventory.
"""

import json
from typing import Any

from flask import Blueprint, jsonify
from flask.typing import ResponseReturnValue
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from labrat.api.common import PlacementError, positive_int, valid_name, write_payload
from labrat.api.placement_rules import (
    layout_position, layout_value, placement_snapshot, shelf_size, SHELF_SIZE_KEYS,
)
from labrat.audit import record_audit_event
from labrat.auth.session import api_login_required, current_user, session_factory
from labrat.metadata import Attribute, InventoryObject
from labrat.models import Device, DeviceClass, Network, Rack, Room, Site
from labrat.placement import DevicePlacement, PlacementContainer


locations_bp = Blueprint("locations_api", __name__, url_prefix="/api/v1")


@locations_bp.get("/locations")
@api_login_required
def locations() -> ResponseReturnValue:
    """List selectable inventory locations and existing canonical devices.

    Returns:
        ResponseReturnValue: Sites, rooms, racks, mounting surfaces and devices
            for the placement editor; no credentials or integration details.
    """
    with session_factory()() as db:
        sites = list(db.scalars(select(Site).order_by(Site.name)))
        rooms = list(db.scalars(select(Room).order_by(Room.name)))
        racks = list(db.scalars(select(Rack).order_by(Rack.name)))
        containers = list(db.scalars(select(PlacementContainer).order_by(PlacementContainer.name)))
        devices = list(db.scalars(select(Device).order_by(Device.name)))
        class_names = {item.id: item.name for item in db.scalars(select(DeviceClass))}
        return jsonify(
            sites=[{"id": s.id, "name": s.name} for s in sites],
            rooms=[{"id": r.id, "name": r.name, "site_id": r.site_id} for r in rooms],
            racks=[{
                "id": r.id, "room_id": r.room_id, "name": r.name,
                "height_u": r.height_u, "width_inches": r.width_inches,
                "has_rear": r.has_rear,
            } for r in racks],
            containers=[{
                "id": c.id, "name": c.name, "kind": c.kind,
                "room_id": c.room_id, "rack_id": c.rack_id,
                "parent_id": c.parent_id, "wall_side": c.wall_side,
                "rack_u": c.rack_u, "rack_face": c.rack_face,
                "position": layout_value(c.position_json),
            } for c in containers],
            devices=[{
                "id": d.id, "name": d.name, "height_u": d.height_u,
                "port_facing": d.port_facing,
                "class_name": class_names.get(d.device_class_id),
                "placement": placement_snapshot(db, d.id),
                "placed": db.get(DevicePlacement, d.id) is not None,
            } for d in devices],
        )


@locations_bp.get("/rooms/<int:room_id>/layout")
@api_login_required
def room_layout(room_id: int) -> ResponseReturnValue:
    """Return one room's containers, rack footprints, and canonical equipment.

    Args:
        room_id: Room identifier selected by the signed-in operator.

    Returns:
        ResponseReturnValue: Relative room layout data or a not-found response.
    """
    with session_factory()() as db:
        room = db.get(Room, room_id)
        if room is None:
            return jsonify(error="Area does not exist"), 404
        site = db.get(Site, room.site_id)
        containers = list(db.scalars(
            select(PlacementContainer)
            .where(PlacementContainer.room_id == room.id)
            .order_by(PlacementContainer.kind, PlacementContainer.name)
        ))
        racks = list(db.scalars(select(Rack).where(Rack.room_id == room.id).order_by(Rack.name)))
        devices = list(db.scalars(select(Device).order_by(Device.name)))
        class_names = {item.id: item.name for item in db.scalars(select(DeviceClass))}
        return jsonify(
            room={"id": room.id, "name": room.name, "site_id": room.site_id},
            site={"id": site.id, "name": site.name} if site is not None else None,
            containers=[{
                "id": item.id, "name": item.name, "kind": item.kind,
                "parent_id": item.parent_id, "rack_id": item.rack_id,
                "wall_side": item.wall_side,
                "rack_u": item.rack_u, "rack_face": item.rack_face,
                "position": layout_value(item.position_json),
            } for item in containers],
            racks=[{
                "id": item.id, "room_id": item.room_id, "name": item.name,
                "height_u": item.height_u, "width_inches": item.width_inches,
                "has_rear": item.has_rear,
            } for item in racks],
            devices=[{
                "id": item.id, "name": item.name, "height_u": item.height_u,
                "port_facing": item.port_facing,
                "class_name": class_names.get(item.device_class_id),
                "placement": placement_snapshot(db, item.id),
            } for item in devices],
        )


@locations_bp.post("/locations")
@api_login_required
def create_location() -> ResponseReturnValue:
    """Create a site, room, rack, or non-rack mounting surface.

    Location creation is explicit, administrator-only, and recorded in the
    same database transaction as an append-only audit event.

    Returns:
        ResponseReturnValue: Created entity type and ID, or validation error.
    """
    try:
        payload = write_payload()
        kind = payload.get("kind")
        label = valid_name(payload.get("name"))
        with session_factory().begin() as db:
            if kind == "site":
                record = Site(name=label)
                db.add(record)
            elif kind == "room":
                site_id = positive_int(payload.get("site_id"), "site_id")
                if db.get(Site, site_id) is None:
                    raise PlacementError("Selected site does not exist", 404)
                record = Room(site_id=site_id, name=label)
                db.add(record)
            elif kind == "rack":
                room_id = positive_int(payload.get("room_id"), "room_id")
                height_u = positive_int(payload.get("height_u"), "height_u")
                width_inches = positive_int(payload.get("width_inches", 19), "width_inches")
                has_rear = payload.get("has_rear", True)
                if type(has_rear) is not bool:
                    raise PlacementError("has_rear must be true or false")
                if width_inches not in {10, 19}:
                    raise PlacementError("Rack width must be 10 or 19 inches")
                if height_u > 128:
                    raise PlacementError("Rack height must not exceed 128U")
                if db.get(Room, room_id) is None:
                    raise PlacementError("Selected area does not exist", 404)
                record = Rack(
                    room_id=room_id, name=label, height_u=height_u,
                    width_inches=width_inches, has_rear=has_rear,
                )
                db.add(record)
            elif kind in {"shelf", "wall", "floor", "surface"}:
                room_id = positive_int(payload.get("room_id"), "room_id")
                if db.get(Room, room_id) is None:
                    raise PlacementError("Selected area does not exist", 404)
                parent_id = positive_int(payload.get("parent_id"), "parent_id", required=False)
                parent = None
                if parent_id is not None:
                    parent = db.get(PlacementContainer, parent_id)
                    if parent is None or parent.room_id != room_id:
                        raise PlacementError("Parent surface must be in the same area")
                wall_side = payload.get("wall_side")
                if kind == "wall" and (
                    not isinstance(wall_side, str)
                    or wall_side not in {"north", "east", "south", "west"}
                ):
                    raise PlacementError("Choose north, east, south, or west for the wall")
                if kind != "wall" and wall_side is not None:
                    raise PlacementError("Only a wall location accepts wall_side")
                if kind == "wall" and parent_id is not None:
                    raise PlacementError("Area walls cannot be nested under another surface")
                if kind == "shelf" and parent is not None and parent.kind not in {
                    "wall", "floor", "surface", "rack_top",
                }:
                    raise PlacementError("Shelves can be placed on a wall, floor area, or rack top")
                coordinate_keys = {"along"} if parent is not None and parent.kind == "wall" else {"x", "y"}
                if kind == "wall":
                    coordinate_keys = set()
                position_json = layout_position(
                    payload.get("position"), coordinate_keys,
                ) if coordinate_keys else None
                record = PlacementContainer(
                    room_id=room_id, name=label, kind=kind, parent_id=parent_id,
                    wall_side=wall_side, position_json=position_json,
                )
                db.add(record)
            else:
                raise PlacementError("Unknown location type")
            db.flush()
            actor = current_user()
            record_audit_event(
                db, actor_kind="user", actor_id=actor.id,
                actor_name=actor.username, action="inventory.location.created",
                subject_kind=kind, subject_id=record.id,
                details={"name": label, "kind": kind},
            )
            result = {"id": record.id, "kind": kind, "name": label}
        return jsonify(result), 201
    except PlacementError as exc:
        return jsonify(error=str(exc)), exc.status
    except IntegrityError:
        return jsonify(error="Location already exists or violates a database constraint"), 409


@locations_bp.post("/containers/<int:container_id>/layout")
@api_login_required
def update_container_layout(container_id: int) -> ResponseReturnValue:
    """Save a container's dimensionless position in its room or parent surface.

    A shelf may also send its relative ``width`` and ``depth`` (fractions of
    the room); a move that leaves them out keeps the saved size. A rack may
    send ``wall`` (north, east, south, or west) when it stands flush against
    that wall; a move without it stands free.

    Args:
        container_id: Container identifier selected from the room inventory.

    Returns:
        ResponseReturnValue: Updated position or a safe validation error.
    """
    try:
        payload = write_payload()
        with session_factory().begin() as db:
            container = db.get(PlacementContainer, container_id)
            if container is None:
                raise PlacementError("Placement container does not exist", 404)
            if container.kind in {"wall", "rack_top", "rack_shelf"}:
                raise PlacementError("This container is anchored to its parent location")
            parent = db.get(PlacementContainer, container.parent_id) if container.parent_id else None
            allowed = {"along"} if parent is not None and parent.kind == "wall" else {"x", "y"}
            before = layout_value(container.position_json)
            position = payload.get("position")
            # A shelf may also carry its size; a plain move keeps the saved one.
            # A rack may name the wall it stands flush against; a move that
            # leaves it out stands free.
            size = {}
            if container.kind == "shelf":
                size = shelf_size(position, before)
                if isinstance(position, dict):
                    position = {key: value for key, value in position.items() if key not in SHELF_SIZE_KEYS}
            elif container.kind == "rack" and isinstance(position, dict) and "wall" in position:
                if position["wall"] not in {"north", "east", "south", "west"}:
                    raise PlacementError("Rack wall must be north, east, south, or west")
                size = {"wall": position["wall"]}
                position = {key: value for key, value in position.items() if key != "wall"}
            stored = layout_position(position, allowed, required=True)
            container.position_json = (json.dumps({**json.loads(stored), **size}, separators=(",", ":"), sort_keys=True)
                                       if size else stored)
            db.flush()
            actor = current_user()
            record_audit_event(
                db, actor_kind="user", actor_id=actor.id,
                actor_name=actor.username, action="inventory.container.positioned",
                subject_kind="placement_container", subject_id=container.id,
                details={"before": before, "after": layout_value(container.position_json)},
            )
            result = {
                "id": container.id, "position": layout_value(container.position_json),
            }
        return jsonify(result), 200
    except PlacementError as exc:
        return jsonify(error=str(exc)), exc.status
    except IntegrityError:
        return jsonify(error="Container position is invalid"), 409


@locations_bp.post("/racks/<int:rack_id>")
@api_login_required
def edit_rack(rack_id: int) -> ResponseReturnValue:
    """Edit rack details without invalidating saved equipment placements.

    The backing placement container carries the same name as its rack. Name
    changes update both records in one audited transaction. A height reduction
    or rear-face removal is rejected while a device occupies the affected area.

    Args:
        rack_id: Canonical rack identifier selected by the administrator.

    Returns:
        ResponseReturnValue: Updated rack details or a validation error.
    """
    try:
        payload = write_payload()
        name = valid_name(payload.get("name"))
        height_u = positive_int(payload.get("height_u"), "height_u")
        width_inches = positive_int(payload.get("width_inches"), "width_inches", required=False)
        has_rear = payload.get("has_rear")
        if height_u > 128:
            raise PlacementError("Rack height must not exceed 128U")
        if type(has_rear) is not bool:
            raise PlacementError("has_rear must be true or false")
        with session_factory().begin() as db:
            rack = db.get(Rack, rack_id)
            if rack is None:
                raise PlacementError("Rack does not exist", 404)
            width_inches = width_inches or rack.width_inches
            if width_inches not in {10, 19}:
                raise PlacementError("Rack width must be 10 or 19 inches")
            container = db.scalar(select(PlacementContainer).where(
                PlacementContainer.rack_id == rack.id,
            ))
            if container is None:
                raise PlacementError("Rack placement container is missing", 409)
            for placement in db.scalars(select(DevicePlacement).where(
                DevicePlacement.container_id == container.id,
            )):
                device = db.get(Device, placement.device_id)
                if placement.rack_u is not None and (
                    placement.rack_u + (device.height_u or 1) - 1 > height_u
                ):
                    raise PlacementError("Move equipment above the new rack height first", 409)
                if not has_rear and placement.face in {"rear", "full_depth"}:
                    raise PlacementError("Move or unplace rear equipment first", 409)
            for shelf in db.scalars(select(PlacementContainer).where(
                PlacementContainer.parent_id == container.id,
                PlacementContainer.kind == "rack_shelf",
            )):
                if shelf.rack_u is not None and shelf.rack_u > height_u:
                    raise PlacementError("Move or remove shared shelves above the new rack height first", 409)
            if not has_rear and db.scalar(select(PlacementContainer.id).where(
                PlacementContainer.parent_id == container.id,
                PlacementContainer.kind == "rack_shelf",
                PlacementContainer.rack_face.in_(["rear", "full_depth"]),
            ).limit(1)) is not None:
                raise PlacementError("Move or remove rear shared shelves first", 409)
            before = {
                "name": rack.name, "height_u": rack.height_u,
                "width_inches": rack.width_inches, "has_rear": rack.has_rear,
            }
            rack.name = name
            rack.height_u = height_u
            rack.width_inches = width_inches
            rack.has_rear = has_rear
            container.name = name
            db.flush()
            actor = current_user()
            after = {
                "name": name, "height_u": height_u,
                "width_inches": width_inches, "has_rear": has_rear,
            }
            record_audit_event(
                db, actor_kind="user", actor_id=actor.id,
                actor_name=actor.username, action="inventory.rack.updated",
                subject_kind="rack", subject_id=rack.id,
                details={"before": before, "after": after},
            )
            result = {"id": rack.id, **after}
        return jsonify(result), 200
    except PlacementError as exc:
        return jsonify(error=str(exc)), exc.status
    except IntegrityError:
        return jsonify(error="Rack name conflicts with another rack in this area"), 409


def _location_audit(db: Session, action: str, kind: str, subject_id: int,
                    details: dict[str, Any]) -> None:
    """Record one administrator change to a site, room, or rack.

    Args:
        db: The open write session.
        action: The audit action, such as ``site.renamed``.
        kind: The subject kind: ``site``, ``room``, ``rack``, or ``container``.
        subject_id: The changed record's ID.
        details: Curated details for the audit trail.
    """
    actor = current_user()
    record_audit_event(
        db, actor_kind="user", actor_id=actor.id, actor_name=actor.username,
        action=action, subject_kind=kind, subject_id=subject_id, details=details,
    )


@locations_bp.post("/sites/<int:site_id>")
@api_login_required
def rename_site(site_id: int) -> ResponseReturnValue:
    """Rename a location (site).

    Args:
        site_id: Site being renamed.

    Returns:
        ResponseReturnValue: The site's ID and new name, or a validation error.
    """
    try:
        name = valid_name(write_payload().get("name"))
        with session_factory().begin() as db:
            site = db.get(Site, site_id)
            if site is None:
                raise PlacementError("Location does not exist", 404)
            before = site.name
            site.name = name
            db.flush()
            _location_audit(db, "inventory.site.renamed", "site", site.id,
                            {"before": {"name": before}, "after": {"name": name}})
            result = {"id": site.id, "name": name}
        return jsonify(result), 200
    except PlacementError as exc:
        return jsonify(error=str(exc)), exc.status
    except IntegrityError:
        return jsonify(error="Another location already has that name"), 409


@locations_bp.post("/rooms/<int:room_id>")
@api_login_required
def rename_room(room_id: int) -> ResponseReturnValue:
    """Rename a room.

    Args:
        room_id: Room being renamed.

    Returns:
        ResponseReturnValue: The room's ID and new name, or a validation error.
    """
    try:
        name = valid_name(write_payload().get("name"))
        with session_factory().begin() as db:
            room = db.get(Room, room_id)
            if room is None:
                raise PlacementError("Area does not exist", 404)
            before = room.name
            room.name = name
            db.flush()
            _location_audit(db, "inventory.room.renamed", "room", room.id,
                            {"before": {"name": before}, "after": {"name": name}})
            result = {"id": room.id, "name": name}
        return jsonify(result), 200
    except PlacementError as exc:
        return jsonify(error=str(exc)), exc.status
    except IntegrityError:
        return jsonify(error="This location already has an area with that name"), 409


# Places a person can rename or delete from the Equipment view. A rack's own
# container follows the rack (rename it with the rack editor), and a rack top,
# a room's walls, floor, and ceiling are part of what they belong to.
RENAMABLE_CONTAINER_KINDS = {"shelf", "surface", "area", "rack_shelf", "wall", "floor", "ceiling"}


DELETABLE_CONTAINER_KINDS = {"shelf", "surface", "area", "rack_shelf"}


class _Removal:
    """Everything one delete would take with it, gathered before any change.

    Deleting a site, room, rack, or place removes the records below it. The
    equipment placed inside is never deleted: with ``unplace`` it becomes
    unplaced (identity, ports, and cables kept); without it the delete is
    refused. Curated attributes and a site's networks always block.
    """

    def __init__(self, db: Session, kind: str, record: Any) -> None:
        """Start an empty plan for deleting one place.

        Args:
            db: The open write session.
            kind: What is deleted: ``site``, ``room``, ``rack``, or ``container``.
            record: The record itself.
        """
        self.db = db
        self.kind = kind
        self.record = record
        self.sites: list[Site] = []
        self.rooms: list[Room] = []
        self.racks: list[Rack] = []
        self.containers: dict[int, PlacementContainer] = {}
        self.blocked: str | None = None
        if kind == "site":
            self.sites.append(record)
            if db.scalar(select(Network.id).where(Network.site_id == record.id).limit(1)) is not None:
                self.block("This location still has networks")
            self._attributes(InventoryObject.site_id, record.id, "location")
            for room in db.scalars(select(Room).where(Room.site_id == record.id)):
                self._add_room(room)
        elif kind == "room":
            self._add_room(record)
        elif kind == "rack":
            self._add_rack(record)
        else:
            self._add_containers([record.id])
        self.placements = list(db.scalars(select(DevicePlacement).where(
            DevicePlacement.container_id.in_(list(self.containers)),
        ))) if self.containers else []
        placed = {item.device_id for item in self.placements}
        # Equipment whose legacy room or rack columns still point here.
        conditions = []
        if self.rooms:
            conditions.append(Device.room_id.in_([room.id for room in self.rooms]))
        if self.racks:
            conditions.append(Device.rack_id.in_([rack.id for rack in self.racks]))
        self.references = [
            device for condition in conditions
            for device in db.scalars(select(Device).where(condition))
            if device.id not in placed
        ]

    def block(self, reason: str) -> None:
        """Keep the first reason this delete cannot go ahead.

        Args:
            reason: Why the delete is refused, shown to the administrator.
        """
        self.blocked = self.blocked or reason

    def _attributes(self, column: Any, entity_id: int, label: str) -> None:
        """Block on curated attributes, which a delete would cascade away.

        Args:
            column: The ``InventoryObject`` column naming this kind of place.
            entity_id: The place's ID.
            label: The place's kind, for the refusal message.
        """
        owner = self.db.scalar(select(InventoryObject.id).where(column == entity_id))
        if owner is not None and self.db.scalar(select(Attribute.id).where(
            Attribute.owner_id == owner,
        ).limit(1)) is not None:
            self.block(f"This {label} has saved attributes; it cannot be deleted here")

    def _add_room(self, room: Room) -> None:
        """Add an area, its racks, and its containers to the delete.

        Args:
            room: The area.
        """
        self.rooms.append(room)
        self._attributes(InventoryObject.room_id, room.id, "area")
        for rack in self.db.scalars(select(Rack).where(Rack.room_id == room.id)):
            self._add_rack(rack)
        self._add_containers(list(self.db.scalars(select(PlacementContainer.id).where(
            PlacementContainer.room_id == room.id,
        ))))

    def _add_rack(self, rack: Rack) -> None:
        """Add a rack and the containers built on it to the delete.

        Args:
            rack: The rack.
        """
        self.racks.append(rack)
        self._attributes(InventoryObject.rack_id, rack.id, "rack")
        self._add_containers(list(self.db.scalars(select(PlacementContainer.id).where(
            PlacementContainer.rack_id == rack.id,
        ))))

    def _add_containers(self, roots: list[int]) -> None:
        """Add containers and everything nested under them.

        Args:
            roots: IDs of the containers to add; their children follow.
        """
        pending = [item for item in roots if item not in self.containers]
        while pending:
            batch = list(self.db.scalars(select(PlacementContainer).where(
                PlacementContainer.id.in_(pending),
            )))
            for item in batch:
                self.containers[item.id] = item
            pending = [item for item in self.db.scalars(select(PlacementContainer.id).where(
                PlacementContainer.parent_id.in_([item.id for item in batch]),
            )) if item not in self.containers]

    @property
    def places(self) -> int:
        """Count removed places a person made (shelves, surfaces), not built-ins.

        Returns:
            int: How many of the removed containers are user-made places.
        """
        return sum(1 for item in self.containers.values() if item.kind in DELETABLE_CONTAINER_KINDS)

    def summary(self) -> dict[str, Any]:
        """Describe what the delete removes and unplaces, for the confirmation.

        Returns:
            dict[str, Any]: The place's kind, ID, and name, and counts of the
                areas, racks, places, and devices it removes or unplaces, plus any
                reason it is blocked.
        """
        names = {"site": "location", "room": "area", "rack": "rack"}
        places = {"rack_shelf": "shared shelf", "shelf": "shelf", "surface": "surface", "area": "area"}
        return {
            "kind": names.get(self.kind) or places.get(self.record.kind, "place"), "id": self.record.id, "name": self.record.name,
            "rooms": len(self.rooms) - (self.kind == "room"),
            "racks": len(self.racks) - (self.kind == "rack"),
            "places": self.places - (self.kind == "container" and self.record.kind in DELETABLE_CONTAINER_KINDS),
            "unplaces": len(self.placements) + len(self.references),
            "blocked": self.blocked,
        }

    def apply(self, *, unplace: bool) -> dict[str, Any]:
        """Unplace the equipment inside if allowed, then delete bottom-up.

        Args:
            unplace: Unplace equipment inside instead of refusing the delete.

        Returns:
            dict[str, Any]: The ``summary`` of what was deleted and unplaced.

        Raises:
            PlacementError: Blocked, or equipment inside without ``unplace``.
        """
        summary = self.summary()
        if self.blocked:
            raise PlacementError(self.blocked, 409)
        if summary["unplaces"] and not unplace:
            raise PlacementError(
                f"Move or unplace this {summary['kind']}'s equipment first, "
                "or delete it with its equipment unplaced", 409,
            )
        actor = current_user()
        for placement in self.placements:
            device = self.db.get(Device, placement.device_id)
            before = placement_snapshot(self.db, device.id)
            self.db.delete(placement)
            device.room_id = device.rack_id = device.rack_u = device.placement = None
            record_audit_event(
                self.db, actor_kind="user", actor_id=actor.id, actor_name=actor.username,
                action="inventory.device.unplaced", subject_kind="device", subject_id=device.id,
                details={"before": before, "after": None, "shelf_layout_changes": [],
                         "removed_with": {"kind": summary["kind"], "id": self.record.id}},
            )
        for device in self.references:
            device.room_id = device.rack_id = device.rack_u = None
        self.db.flush()
        # Children before parents (a wall shelf before its wall).
        remaining = dict(self.containers)
        while remaining:
            parents = {item.parent_id for item in remaining.values()}
            leaves = [item for key, item in remaining.items() if key not in parents]
            if not leaves:
                raise PlacementError("These places are nested in a loop", 409)
            for item in leaves:
                self.db.delete(item)
                del remaining[item.id]
            self.db.flush()
        for group in (self.racks, self.rooms, self.sites):
            for item in group:
                self.db.delete(item)
            self.db.flush()
        return summary


def _remove(kind: str, model: Any, entity_id: int, missing: str, action: str,
            details: Any) -> ResponseReturnValue:
    """Preview or carry out one delete (see ``_Removal``).

    The JSON body may carry ``preview: true`` (describe, change nothing) and
    ``unplace: true`` (unplace the equipment inside instead of refusing).

    Args:
        kind: What is deleted: ``site``, ``room``, ``rack``, or ``container``.
        model: Its SQLAlchemy model.
        entity_id: Its ID.
        missing: The error message when it does not exist.
        action: The audit action recorded on deletion.
        details: Builds the audit details from the record before it is
            deleted.

    Returns:
        ResponseReturnValue: JSON with the preview or the deletion summary,
            or an error with its HTTP status.
    """
    try:
        payload = write_payload()
        preview = payload.get("preview", False)
        unplace = payload.get("unplace", False)
        if type(preview) is not bool or type(unplace) is not bool:
            raise PlacementError("preview and unplace must be true or false")
        with session_factory().begin() as db:
            record = db.get(model, entity_id)
            if record is None:
                raise PlacementError(missing, 404)
            if kind == "container" and record.kind not in DELETABLE_CONTAINER_KINDS:
                raise PlacementError("This place belongs to its area or rack and cannot be deleted", 400)
            removal = _Removal(db, kind, record)
            if preview:
                return jsonify(removal.summary()), 200
            audit = details(record)
            summary = removal.apply(unplace=unplace)
            audit.update({key: summary[key] for key in ("rooms", "racks", "places", "unplaces") if summary[key]})
            if removal.rooms and kind == "site":
                audit["room_names"] = [room.name for room in removal.rooms]
            if removal.racks and kind in {"site", "room"}:
                audit["rack_names"] = [rack.name for rack in removal.racks]
            if kind == "room":
                audit["containers_removed"] = len(removal.containers)
            _location_audit(db, action, "placement_container" if kind == "container" else kind,
                            entity_id, audit)
        return jsonify(id=entity_id, **{key: summary[key] for key in ("unplaces", "rooms", "racks", "places")}), 200
    except PlacementError as exc:
        return jsonify(error=str(exc)), exc.status
    except IntegrityError:
        return jsonify(error="This is still in use"), 409


@locations_bp.post("/sites/<int:site_id>/delete")
@api_login_required
def delete_site(site_id: int) -> ResponseReturnValue:
    """Delete a location (site), its rooms, racks, and places.

    Without ``unplace`` only a location with nothing placed inside can be
    deleted. Networks and saved attributes always block. ``preview`` returns
    what would be removed without changing anything.

    Args:
        site_id: Site being deleted.

    Returns:
        ResponseReturnValue: The deleted ID and counts, a preview, or why
            it cannot be deleted.
    """
    return _remove("site", Site, site_id, "Location does not exist", "inventory.site.deleted",
                   lambda site: {"name": site.name})


@locations_bp.post("/rooms/<int:room_id>/delete")
@api_login_required
def delete_room(room_id: int) -> ResponseReturnValue:
    """Delete a room with its racks, floor, walls, ceiling, and places.

    Without ``unplace`` a room with placed equipment, or equipment that
    still refers to it, is refused. Saved attributes always block.

    Args:
        room_id: Room being deleted.

    Returns:
        ResponseReturnValue: The deleted ID and counts, a preview, or why
            it cannot be deleted.
    """
    return _remove("room", Room, room_id, "Room does not exist", "inventory.room.deleted",
                   lambda room: {"name": room.name, "site_id": room.site_id})


@locations_bp.post("/racks/<int:rack_id>/delete")
@api_login_required
def delete_rack(rack_id: int) -> ResponseReturnValue:
    """Delete a rack with its rack top, shared shelves, and shelves on top.

    Without ``unplace`` a rack with equipment mounted, on a shared shelf, or
    on top is refused. Saved attributes always block.

    Args:
        rack_id: Rack being deleted.

    Returns:
        ResponseReturnValue: The deleted ID and counts, a preview, or why
            it cannot be deleted.
    """
    return _remove("rack", Rack, rack_id, "Rack does not exist", "inventory.rack.deleted",
                   lambda rack: {"name": rack.name, "room_id": rack.room_id})


@locations_bp.post("/containers/<int:container_id>/delete")
@api_login_required
def delete_container(container_id: int) -> ResponseReturnValue:
    """Delete a shelf, surface, area, or shared rack shelf and what sits on it.

    Shelves resting on the place go with it. Without ``unplace`` a place
    with equipment on it is refused.

    Args:
        container_id: Place being deleted.

    Returns:
        ResponseReturnValue: The deleted ID and counts, a preview, or why
            it cannot be deleted.
    """
    return _remove("container", PlacementContainer, container_id, "Place does not exist",
                   "inventory.container.deleted",
                   lambda item: {"name": item.name, "kind": item.kind, "room_id": item.room_id})


@locations_bp.post("/containers/<int:container_id>")
@api_login_required
def rename_container(container_id: int) -> ResponseReturnValue:
    """Rename a shelf, surface, area, wall, floor, ceiling, or shared shelf.

    Args:
        container_id: Place being renamed.

    Returns:
        ResponseReturnValue: The place's ID and new name, or a validation error.
    """
    try:
        name = valid_name(write_payload().get("name"))
        with session_factory().begin() as db:
            container = db.get(PlacementContainer, container_id)
            if container is None:
                raise PlacementError("Place does not exist", 404)
            if container.kind not in RENAMABLE_CONTAINER_KINDS:
                raise PlacementError(
                    "Rename the rack itself" if container.kind == "rack"
                    else "This place takes its name from its rack", 400)
            before = container.name
            container.name = name
            db.flush()
            _location_audit(db, "inventory.container.renamed", "placement_container", container.id,
                            {"before": {"name": before}, "after": {"name": name}, "kind": container.kind})
            result = {"id": container.id, "name": name}
        return jsonify(result), 200
    except PlacementError as exc:
        return jsonify(error=str(exc)), exc.status
    except IntegrityError:
        return jsonify(error="Another place already has that name"), 409
