# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Record the cable medium observed on a discovered port."""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0011_port_cable_medium"
down_revision: str | None = "0010_room_ceilings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a nullable cable medium to discovered ports.

    Values are ``copper`` (RJ45 Ethernet), ``dac`` (direct-attach copper
    twinax), ``fiber_multimode``, ``fiber_singlemode``, or ``fiber`` (optical
    of unknown mode). NULL means the source did not say.
    """
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("discovered_ports")}
    if "medium" not in columns:
        op.add_column("discovered_ports", sa.Column("medium", sa.String(length=24), nullable=True))


def downgrade() -> None:
    """Remove the optional cable medium."""
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("discovered_ports")}
    if "medium" in columns:
        with op.batch_alter_table("discovered_ports") as batch:
            batch.drop_column("medium")
