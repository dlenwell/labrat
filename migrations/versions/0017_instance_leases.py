# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Add the leases that keep one web app and one collector per database."""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0017_instance_leases"
down_revision: str | None = "0016_drop_deleted_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``instance_leases``, one row per role (``web``, ``collector``).

    Nothing existing changes: a database without rows here simply has no
    running holder yet, and the first process of each role takes its lease.
    """
    if sa.inspect(op.get_bind()).has_table("instance_leases"):
        return
    op.create_table(
        "instance_leases",
        sa.Column("role", sa.String(length=32), primary_key=True),
        sa.Column("holder", sa.String(length=300), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("acquired_at", sa.Integer(), nullable=False),
        sa.Column("renewed_at", sa.Integer(), nullable=False),
        sa.Column("released_by", sa.String(length=300), nullable=False, server_default=""),
    )


def downgrade() -> None:
    """Drop the leases; processes then run unguarded, as before."""
    if sa.inspect(op.get_bind()).has_table("instance_leases"):
        op.drop_table("instance_leases")
