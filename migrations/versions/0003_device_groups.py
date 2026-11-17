# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Allow devices to belong to arbitrary, independently sourced groups.

Revision 0003 adds a reusable group catalog and device-to-group memberships.
No existing inventory, placements, discoveries, or cable policies are changed.
Multiple sources can assert a group for one device without overwriting a user
assignment. The canonical device model remains authoritative for inventory.

Downgrading drops all group definitions and memberships; back up the database
before running this downgrade in a real installation.
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0003_device_groups"
down_revision: str | None = "0002_discovery_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the groups catalog and source-qualified device memberships."""
    op.create_table(
        "groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(80, collation="NOCASE"), nullable=False, unique=True),
        sa.Column("description", sa.Text()),
        sa.CheckConstraint("length(trim(name)) > 0", name="nonempty_group_name"),
    )
    op.create_table(
        "device_groups",
        sa.Column("device_id", sa.Integer(), sa.ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("source", sa.String(160), primary_key=True, server_default="manual"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint("length(trim(source)) > 0", name="nonempty_group_source"),
    )
    # The composite primary key efficiently indexes searches by device_id;
    # this secondary index supports reverse lookups by group_id.
    op.create_index("ix_device_groups_group_id", "device_groups", ["group_id"])


def downgrade() -> None:
    """Drop the groups catalog and memberships, leaving earlier data intact."""
    op.drop_index("ix_device_groups_group_id", table_name="device_groups")
    op.drop_table("device_groups")
    op.drop_table("groups")
