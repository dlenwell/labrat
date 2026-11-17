# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Add rack rail width and rack-mounted shared shelf coordinates."""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0007_rack_width_and_shared_shelves"
down_revision: str | None = "0006_default_room_surfaces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    """Resume a partially applied additive SQLite migration safely.

    Args:
        table: The table to change.
        column: The column to add when it is missing.
    """
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
    if column.name not in columns:
        op.add_column(table, column)


def upgrade() -> None:
    """Store standard rack widths and one-U shared shelf locations."""
    _add_column_if_missing(
        "racks", sa.Column("width_inches", sa.Integer(), nullable=False, server_default="19"),
    )
    _add_column_if_missing("placement_containers", sa.Column("rack_u", sa.Integer()))
    _add_column_if_missing(
        "placement_containers", sa.Column("rack_face", sa.String(length=16)),
    )
    index_names = {
        item["name"] for item in sa.inspect(op.get_bind()).get_indexes("placement_containers")
    }
    if "uq_rack_shelf_slot" not in index_names:
        op.create_index(
            "uq_rack_shelf_slot", "placement_containers",
            ["parent_id", "rack_u", "rack_face"], unique=True,
            sqlite_where=sa.text("kind = 'rack_shelf'"),
        )


def downgrade() -> None:
    """Remove shared shelf coordinates and width while retaining other layout data."""
    op.drop_index("uq_rack_shelf_slot", table_name="placement_containers")
    op.drop_column("placement_containers", "rack_face")
    op.drop_column("placement_containers", "rack_u")
    op.drop_column("racks", "width_inches")
