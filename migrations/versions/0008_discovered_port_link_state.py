# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Store explicit current link state for discovered ports."""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0008_discovered_port_link_state"
down_revision: str | None = "0007_rack_width_and_shared_shelves"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a nullable link-state field; unknown remains distinct from down."""
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("discovered_ports")}
    if "link_up" not in columns:
        op.add_column("discovered_ports", sa.Column("link_up", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Remove explicit discovered port state."""
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("discovered_ports")}
    if "link_up" in columns:
        op.drop_column("discovered_ports", "link_up")
