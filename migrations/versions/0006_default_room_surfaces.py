# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Create default floor and cardinal wall containers for every room."""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0006_default_room_surfaces"
down_revision: str | None = "0005_room_layout_and_rack_depth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Give existing and future rooms a floor area and four wall surfaces."""
    op.execute(
        "INSERT INTO placement_containers (room_id, name, kind) "
        "SELECT r.id, 'Floor', 'floor' FROM rooms AS r "
        "WHERE NOT EXISTS (SELECT 1 FROM placement_containers AS pc "
        "WHERE pc.room_id = r.id AND pc.kind = 'floor' AND pc.parent_id IS NULL)"
    )
    for side in ("north", "east", "south", "west"):
        op.execute(
            "INSERT INTO placement_containers (room_id, name, kind, wall_side) "
            f"SELECT r.id, '{side.title()} wall', 'wall', '{side}' FROM rooms AS r "
            "WHERE NOT EXISTS (SELECT 1 FROM placement_containers AS pc "
            f"WHERE pc.room_id = r.id AND pc.kind = 'wall' AND pc.wall_side = '{side}')"
        )

    op.execute(
        "CREATE TRIGGER labrat_new_room_surfaces AFTER INSERT ON rooms BEGIN "
        "INSERT INTO placement_containers (room_id, name, kind) "
        "VALUES (NEW.id, 'Floor', 'floor'); "
        "INSERT INTO placement_containers (room_id, name, kind, wall_side) "
        "VALUES (NEW.id, 'North wall', 'wall', 'north'); "
        "INSERT INTO placement_containers (room_id, name, kind, wall_side) "
        "VALUES (NEW.id, 'East wall', 'wall', 'east'); "
        "INSERT INTO placement_containers (room_id, name, kind, wall_side) "
        "VALUES (NEW.id, 'South wall', 'wall', 'south'); "
        "INSERT INTO placement_containers (room_id, name, kind, wall_side) "
        "VALUES (NEW.id, 'West wall', 'wall', 'west'); END"
    )


def downgrade() -> None:
    """Stop creating default surfaces while retaining any user layout data."""
    op.execute("DROP TRIGGER IF EXISTS labrat_new_room_surfaces")
