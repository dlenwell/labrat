# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Model user-defined attributes without losing relational ownership.

``InventoryObject`` is a small, shared identity for sites, rooms, racks, and
physical devices. Exactly one of its four nullable owner foreign keys is set.
SQLite triggers, installed by Alembic revision ``0002_discovery_foundation``,
register new owners automatically. Cascading deletion removes attributes when
the underlying owner is deleted.

Keys deliberately are *not* unique per owner: a host may have many sensors of
the same kind. Attribute values use type-tagged JSON in a TEXT column.

Used by: api.locations, api.observation_actions, discovery.neighbor_matches,
inventory_backup.
"""

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from labrat.database import Base


class InventoryObject(Base):
    """Give one existing inventory entity a cross-type attribute owner ID.

    An owner is precisely one ``Site``, ``Room``, ``Rack``, or ``Device``. The
    nullable foreign keys retain actual referential integrity; a registry ID
    can never silently refer to a deleted object. A room and a server can both
    have numeric ID 42 without colliding in this registry.

    The row is generated when its entity is inserted. Delete cascades from the
    entity to this registry and then to its attributes. Do not directly delete
    registry rows as part of ordinary inventory operations.
    """

    __tablename__ = "inventory_objects"
    __table_args__ = (
        CheckConstraint(
            "(site_id IS NOT NULL) + (room_id IS NOT NULL) + "
            "(rack_id IS NOT NULL) + (device_id IS NOT NULL) = 1",
            name="exactly_one_entity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), unique=True)
    room_id: Mapped[int | None] = mapped_column(ForeignKey("rooms.id", ondelete="CASCADE"), unique=True)
    rack_id: Mapped[int | None] = mapped_column(ForeignKey("racks.id", ondelete="CASCADE"), unique=True)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), unique=True)


class Attribute(Base):
    """Store one independently addressable, optionally nested attribute.

    ``owner_id`` refers to an :class:`InventoryObject`, not a polymorphic
    integer without a foreign key. Two rows can have identical ``owner_id``
    and ``key`` values by design (for example repeated temperature sensors).
    ``parent_id`` can organize related attributes into branches; callers must
    validate that a parent belongs to the same owner and prevent parent cycles.

    ``value`` contains a single type-tagged JSON value, such as
    ``{"int": 2}``, ``{"str": "Basement"}``, or
    ``{"json": {"width": 4.2, "unit": "m"}}``. Use the validation helpers in
    ``labrat.metadata_values`` before persisting values.
    """

    __tablename__ = "attributes"
    __table_args__ = (CheckConstraint("json_valid(value)", name="valid_json"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_objects.id", ondelete="CASCADE"), index=True
    )
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("attributes.id", ondelete="SET NULL"), index=True
    )
    key: Mapped[str] = mapped_column(String(160), index=True)
    value: Mapped[str] = mapped_column(Text)
