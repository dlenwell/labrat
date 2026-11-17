# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Store shared, reversible operations-condition ignore rules."""
from alembic import op
import sqlalchemy as sa

revision = "0014_observation_ignores"
down_revision = "0013_source_poll_interval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add policies separately from collected data and curated cable records."""
    op.create_table(
        "observation_ignores",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("discovery_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_json", sa.Text(), nullable=False, unique=True),
        sa.Column("label", sa.String(300), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("json_valid(target_json)", name="valid_ignore_target"),
    )
    op.create_index("ix_observation_ignores_source_id", "observation_ignores", ["source_id"])


def downgrade() -> None:
    """Remove ignore policies; collected data and audit history remain intact."""
    op.drop_index("ix_observation_ignores_source_id", table_name="observation_ignores")
    op.drop_table("observation_ignores")
