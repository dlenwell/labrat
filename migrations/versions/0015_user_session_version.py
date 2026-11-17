# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Let an account's sessions be revoked server-side."""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0015_user_session_version"
down_revision: str | None = "0014_observation_ignores"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a session version to users, starting at zero.

    Sessions are signed cookies, so the server cannot delete one. Each
    session records its account's version when it starts; bumping the
    version (as signing out does) makes every earlier session invalid.
    Sessions from before this revision carry no version and end once.
    """
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("users")}
    if "session_version" not in columns:
        op.add_column("users", sa.Column("session_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    """Remove the session version."""
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("users")}
    if "session_version" in columns:
        with op.batch_alter_table("users") as batch:
            batch.drop_column("session_version")
