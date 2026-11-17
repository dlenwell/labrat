# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Record whether a rack has a usable rear face.

Existing racks keep both faces. Single-sided racks use only the front face;
placements are validated by the application before the rear is disabled.
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0004_rack_faces"
down_revision: str | None = "0003_device_groups"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the rear-face flag and retain both faces on existing racks."""
    op.add_column(
        "racks",
        sa.Column("has_rear", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    """Remove the rear-face flag without changing rack placements."""
    op.drop_column("racks", "has_rear")
