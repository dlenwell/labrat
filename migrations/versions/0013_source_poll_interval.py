# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Record how often the running collector polls each discovery source."""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0013_source_poll_interval"
down_revision: str | None = "0012_logical_network_interfaces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a nullable polling interval, in seconds, to discovery sources.

    The looping collector writes its interval on every attempt, so pages can
    show when the next collection is due and flag one that is overdue. NULL
    means no looping collector has recorded an interval.
    """
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("discovery_sources")}
    if "poll_interval_seconds" not in columns:
        op.add_column("discovery_sources", sa.Column("poll_interval_seconds", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Remove the recorded polling interval."""
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("discovery_sources")}
    if "poll_interval_seconds" in columns:
        with op.batch_alter_table("discovery_sources") as batch:
            batch.drop_column("poll_interval_seconds")
