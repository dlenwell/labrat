# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Persist connector type and maximum discovered port speed."""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0009_port_type_and_max_speed"
down_revision: str | None = "0008_discovered_port_link_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable maximum speed and connector type to discovered ports."""
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("discovered_ports")}
    if "max_speed_mbps" not in columns:
        op.add_column("discovered_ports", sa.Column("max_speed_mbps", sa.Integer(), nullable=True))
    if "connector_type" not in columns:
        op.add_column("discovered_ports", sa.Column("connector_type", sa.String(length=24), nullable=True))


def downgrade() -> None:
    """Remove optional physical connector metadata."""
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("discovered_ports")}
    if "connector_type" in columns:
        op.drop_column("discovered_ports", "connector_type")
    if "max_speed_mbps" in columns:
        op.drop_column("discovered_ports", "max_speed_mbps")
