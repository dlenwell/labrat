# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Add scoped, revocable API tokens for machines."""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0018_api_tokens"
down_revision: str | None = "0017_instance_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``api_tokens``: a public id, a digest of the secret, and scopes.

    The token itself is never stored. Nothing existing changes.
    """
    if sa.inspect(op.get_bind()).has_table("api_tokens"):
        return
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(length=12), nullable=False, unique=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("secret_digest", sa.String(length=64), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop the tokens; machines can no longer report over the API."""
    if sa.inspect(op.get_bind()).has_table("api_tokens"):
        op.drop_table("api_tokens")
