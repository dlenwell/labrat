# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Represent flexible physical placement independently from network wiring.

Rack placement is one specialized case of a more general container: equipment
can be on a shelf, mounted to a wall, sitting on the floor, or atop a rack.
Containers may nest, so a shelf can be associated with a parent rack. Moves
change a device's active placement without altering its identity or links.

Existing ``devices.room_id``, ``devices.rack_id``, and ``devices.rack_u`` are
legacy placement columns retained for compatibility during the transition.
New placement editing should use these tables as the source of truth.

Used by: api.locations, api.placement_rules, api.rack_devices, integrations,
pages.activity, pages.private.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from labrat.database import Base


class PlacementContainer(Base):
    """Describe a rack or another surface where equipment can be placed.

    Each container belongs to a room. A child container may have a parent (for
    example a shelf atop a rack). ``rack_id`` points to the *existing* rack
    record for rack-type containers, rather than storing a second rack height.
    ``wall_side`` identifies one of a room's four walls. ``position_json`` stores
    relative canvas coordinates, not measured physical dimensions.
    ``rack_u`` and ``rack_face`` locate a shared shelf nested inside a rack;
    the shelf contents use relative x positions just like wall shelves.

    Room/parent consistency, cycles, and rack/room agreement must be validated
    by the placement service when editing the hierarchy.
    """

    __tablename__ = "placement_containers"
    __table_args__ = (
        CheckConstraint("parent_id IS NULL OR parent_id != id", name="no_self_parent"),
        CheckConstraint(
            "(kind = 'rack' AND rack_id IS NOT NULL) OR "
            "(kind != 'rack' AND rack_id IS NULL)",
            name="rack_has_reference",
        ),
        Index(
            "uq_rack_shelf_slot", "parent_id", "rack_u", "rack_face", unique=True,
            sqlite_where=text("kind = 'rack_shelf'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("placement_containers.id"), index=True)
    rack_id: Mapped[int | None] = mapped_column(ForeignKey("racks.id"), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(40))
    wall_side: Mapped[str | None] = mapped_column(String(8))
    rack_u: Mapped[int | None] = mapped_column(Integer)
    rack_face: Mapped[str | None] = mapped_column(String(16))
    position_json: Mapped[str | None] = mapped_column(Text)


class DevicePlacement(Base):
    """Store the single active physical placement of a device.

    ``device_id`` is both primary key and foreign key, so a device cannot have
    multiple conflicting active placements. An absent row means *unplaced*.
    The ``container_id`` determines the room through its container hierarchy.

    Rack placements can specify 1-based ``rack_u`` and ``face`` (front, rear,
    or full-depth). Non-rack placements store normalized coordinates or
    ordering in ``position_json``. Network cables are never changed by moving a
    device. The device record stores its port direction so it persists across
    placement changes.
    """

    __tablename__ = "device_placements"
    __table_args__ = (
        CheckConstraint("rack_u IS NULL OR rack_u > 0", name="valid_rack_u"),
        CheckConstraint(
            "face IS NULL OR face IN ('front', 'rear', 'full_depth')",
            name="valid_face",
        ),
    )

    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    container_id: Mapped[int] = mapped_column(ForeignKey("placement_containers.id"), index=True)
    rack_u: Mapped[int | None] = mapped_column(Integer)
    face: Mapped[str | None] = mapped_column(String(16))
    position_json: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )
