# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Give every room a default ceiling for overhead equipment such as APs."""

from typing import Sequence

from alembic import op


revision: str = "0010_room_ceilings"
down_revision: str | None = "0009_port_type_and_max_speed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Default surfaces created for each new room, in insertion order.
_FLOOR_AND_WALLS = (
    "INSERT INTO placement_containers (room_id, name, kind) "
    "VALUES (NEW.id, 'Floor', 'floor'); "
    "INSERT INTO placement_containers (room_id, name, kind, wall_side) "
    "VALUES (NEW.id, 'North wall', 'wall', 'north'); "
    "INSERT INTO placement_containers (room_id, name, kind, wall_side) "
    "VALUES (NEW.id, 'East wall', 'wall', 'east'); "
    "INSERT INTO placement_containers (room_id, name, kind, wall_side) "
    "VALUES (NEW.id, 'South wall', 'wall', 'south'); "
    "INSERT INTO placement_containers (room_id, name, kind, wall_side) "
    "VALUES (NEW.id, 'West wall', 'wall', 'west'); "
)
_CEILING = (
    "INSERT INTO placement_containers (room_id, name, kind) "
    "VALUES (NEW.id, 'Ceiling', 'ceiling'); "
)


def upgrade() -> None:
    """Add a ceiling to existing rooms and to every room created later.

    The ceiling is an ordinary top-level placement container, like the
    default floor, so no table changes are needed. Existing rooms that
    somehow already have one are left alone.
    """
    op.execute(
        "INSERT INTO placement_containers (room_id, name, kind) "
        "SELECT r.id, 'Ceiling', 'ceiling' FROM rooms AS r "
        "WHERE NOT EXISTS (SELECT 1 FROM placement_containers AS pc "
        "WHERE pc.room_id = r.id AND pc.kind = 'ceiling' AND pc.parent_id IS NULL)"
    )
    op.execute("DROP TRIGGER IF EXISTS labrat_new_room_surfaces")
    op.execute(
        "CREATE TRIGGER labrat_new_room_surfaces AFTER INSERT ON rooms BEGIN "
        + _FLOOR_AND_WALLS + _CEILING + "END"
    )


def downgrade() -> None:
    """Stop creating ceilings for new rooms while keeping existing layout data.

    Existing ceiling containers and anything placed on them are retained, as
    with the earlier default-surface revision.
    """
    op.execute("DROP TRIGGER IF EXISTS labrat_new_room_surfaces")
    op.execute(
        "CREATE TRIGGER labrat_new_room_surfaces AFTER INSERT ON rooms BEGIN "
        + _FLOOR_AND_WALLS + "END"
    )
