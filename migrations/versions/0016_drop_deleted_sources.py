# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Remove the source rows of integrations deleted before deletion removed them."""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0016_drop_deleted_sources"
down_revision: str | None = "0015_user_session_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Delete switched-off sources whose integration was deleted.

    Deleting an integration used to switch its source row off and keep it,
    so the header still listed it and its name could not be reused. A source
    is gone when it is switched off and its latest audit event is
    ``integration.deleted``; a source merely switched off in Settings has a
    later save event and stays. Sources derived from a removed integration
    (UniFi clients) go with it. The database's ON DELETE CASCADE removes
    their discovered devices, ports, links, and ignore rules; saved LLDP port
    matches that name a missing source are dropped, as they can never match.
    Curated inventory is not touched.
    """
    bind = op.get_bind()
    deleted = [row[0] for row in bind.execute(sa.text("""
        SELECT source.id FROM discovery_sources AS source
        WHERE source.enabled = 0 AND (
            SELECT event.action FROM audit_events AS event
            WHERE event.subject_kind = 'discovery_source' AND event.subject_id = source.id
            ORDER BY event.id DESC LIMIT 1
        ) = 'integration.deleted'
    """))]
    if deleted:
        ids = sa.bindparam("ids", expanding=True)
        bind.execute(sa.text("""
            DELETE FROM discovery_sources
            WHERE kind = 'unifi_clients' AND config_name IN (
                SELECT config_name FROM discovery_sources WHERE kind = 'unifi' AND id IN :ids
            )
        """).bindparams(ids), {"ids": deleted})
        bind.execute(sa.text("DELETE FROM discovery_sources WHERE id IN :ids").bindparams(ids), {"ids": deleted})
    bind.execute(sa.text("""
        DELETE FROM attributes
        WHERE key = 'discovery.lldp_port_match' AND json_valid(value) AND (
            json_extract(value, '$.json.source_id') NOT IN (SELECT id FROM discovery_sources)
            OR json_extract(value, '$.json.target_source_id') NOT IN (SELECT id FROM discovery_sources)
        )
    """))


def downgrade() -> None:
    """Nothing to restore: the removed rows belonged to deleted integrations."""
