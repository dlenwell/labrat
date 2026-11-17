# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Track discovered Proxmox bonds, bridges, and VLAN interfaces."""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0012_logical_network_interfaces"
down_revision: str | None = "0011_port_cable_medium"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create source-scoped storage for logical network configuration.

    Physical cable endpoints remain in ``discovered_ports``. The new table
    stores only logical interfaces and member names, so bonds, bridges, and
    VLANs cannot become cable endpoints by accident.
    """
    op.create_table(
        "discovered_network_interfaces",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("discovered_device_id", sa.Integer(), nullable=False),
        sa.Column("remote_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("parent_name", sa.String(length=160), nullable=True),
        sa.Column("vlan_id", sa.Integer(), nullable=True),
        sa.Column("mode", sa.String(length=80), nullable=True),
        sa.Column("members_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("active", sa.Boolean(), nullable=True),
        sa.Column("details_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(), nullable=False,
        ),
        sa.CheckConstraint("kind IN ('bond', 'bridge', 'vlan')"),
        sa.CheckConstraint("vlan_id IS NULL OR vlan_id BETWEEN 1 AND 4094"),
        sa.ForeignKeyConstraint(
            ["discovered_device_id"], ["discovered_devices.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("discovered_device_id", "remote_id"),
    )
    op.create_index(
        "ix_discovered_network_interfaces_discovered_device_id",
        "discovered_network_interfaces", ["discovered_device_id"],
    )
    op.create_index(
        "ix_discovered_network_interfaces_kind",
        "discovered_network_interfaces", ["kind"],
    )


def downgrade() -> None:
    """Drop source-scoped logical network observations."""
    op.drop_index(
        "ix_discovered_network_interfaces_kind",
        table_name="discovered_network_interfaces",
    )
    op.drop_index(
        "ix_discovered_network_interfaces_discovered_device_id",
        table_name="discovered_network_interfaces",
    )
    op.drop_table("discovered_network_interfaces")
