# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Add relative room layouts, wall sides, full-depth mounting and port facing.

The migration preserves existing device placements. Rack-top containers are
added for existing racks and created automatically for future racks. Downgrade
removes the new rack-top container rows and their placements.
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0005_room_layout_and_rack_depth"
down_revision: str | None = "0004_rack_faces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    """Finish an interrupted SQLite upgrade without duplicating a column.

    SQLite commits some DDL statements individually. A prior attempt may have
    added a column before a later table operation failed, so the migration
    safely skips only columns that are already present.

    Args:
        table: Existing table receiving the additive column.
        column: Column definition to add when absent.
    """
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
    if column.name not in columns:
        op.add_column(table, column)


def upgrade() -> None:
    """Store dimensionless room layout and expanded rack placement details."""
    _add_column_if_missing("placement_containers", sa.Column("wall_side", sa.String(8)))
    _add_column_if_missing("placement_containers", sa.Column("position_json", sa.Text()))

    with op.batch_alter_table("device_placements") as batch_op:
        batch_op.drop_constraint("valid_face", type_="check")
        batch_op.alter_column(
            "face", existing_type=sa.String(8), type_=sa.String(16), nullable=True,
        )
        batch_op.create_check_constraint(
            "valid_face", "face IS NULL OR face IN ('front', 'rear', 'full_depth')",
        )

    # These additions remain in place without table rebuilds because both
    # tables have incoming foreign keys. API writes validate their values.
    _add_column_if_missing("devices", sa.Column("port_facing", sa.String(8)))

    # A rack-top surface is nested under its rack's existing placement
    # container, so room layouts can show one rack footprint with its top items.
    op.execute(
        "INSERT INTO placement_containers (room_id, parent_id, name, kind) "
        "SELECT rack_container.room_id, rack_container.id, 'On top', 'rack_top' "
        "FROM placement_containers AS rack_container "
        "WHERE rack_container.kind = 'rack'"
    )

    # Replace the legacy rack trigger with a trigger that creates both the rack
    # container and its nested top surface for each newly created rack.
    op.execute("DROP TRIGGER IF EXISTS labrat_new_rack_container")
    op.execute(
        "CREATE TRIGGER labrat_new_rack_container AFTER INSERT ON racks BEGIN "
        "INSERT INTO placement_containers (room_id, rack_id, name, kind) "
        "VALUES (NEW.room_id, NEW.id, NEW.name, 'rack'); "
        "INSERT INTO placement_containers (room_id, parent_id, name, kind) "
        "SELECT NEW.room_id, id, 'On top', 'rack_top' FROM placement_containers "
        "WHERE rack_id = NEW.id; END"
    )


def downgrade() -> None:
    """Remove rack-top layout rows and restore the original rack trigger."""
    op.execute(
        "DELETE FROM device_placements WHERE container_id IN "
        "(SELECT id FROM placement_containers WHERE kind = 'rack_top')"
    )
    op.execute("DELETE FROM placement_containers WHERE kind = 'rack_top'")
    op.execute("DROP TRIGGER IF EXISTS labrat_new_rack_container")
    op.execute(
        "CREATE TRIGGER labrat_new_rack_container AFTER INSERT ON racks "
        "BEGIN INSERT INTO placement_containers (room_id, rack_id, name, kind) "
        "VALUES (NEW.room_id, NEW.id, NEW.name, 'rack'); END"
    )

    op.drop_column("devices", "port_facing")

    with op.batch_alter_table("device_placements") as batch_op:
        batch_op.drop_constraint("valid_face", type_="check")
        batch_op.alter_column(
            "face", existing_type=sa.String(16), type_=sa.String(8), nullable=True,
        )
        batch_op.create_check_constraint(
            "valid_face", "face IS NULL OR face IN ('front', 'rear')",
        )

    op.drop_column("placement_containers", "position_json")
    op.drop_column("placement_containers", "wall_side")
