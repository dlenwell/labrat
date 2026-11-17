# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Record append-only user and daemon changes to LabRat's inventory state.

Audit events are deliberately separate from operational telemetry and routine
poll timestamps. They describe *meaningful changes*: an inventory edit, a new
observation, or a reconciliation decision. The database prevents UPDATE/DELETE
against the audit table, while the event references use snapshot identifiers
rather than cascading foreign keys so history survives object deletion.

Never pass raw credentials, full integration responses, or arbitrary request
bodies into an audit event. Use deliberately selected, non-secret details.

Used by: api.clients, api.locations, api.observation_actions,
api.rack_devices, collector.client_links, collector.links,
collector.nut_wired, collector.runner, collector.sources, collector.store,
discovery_settings, integrations, pages.activity, pages.public,
pages.settings_backup.
"""

import json
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, func, Index
from sqlalchemy.orm import Mapped, Session, mapped_column

from labrat.database import Base


_ACTOR_KINDS = frozenset({"user", "collector", "system"})
_SECRET_FIELDS = frozenset({
    "password", "passphrase", "secret", "secret_key", "token", "api_key",
    "apikey", "authorization", "credential", "credentials", "cookie",
    "private_key", "access_key", "client_secret",
})


class AuditEvent(Base):
    """Preserve who changed what, when, and why without mutable object FKs.

    ``actor_kind`` identifies a user, collector, or internal system action.
    ``actor_id`` and ``actor_name`` are intentionally snapshots: a user or
    integration may later be removed, but the historical event must remain.
    Likewise, ``subject_kind`` and ``subject_id`` identify an affected record
    without cascading deletion from that record. The optional ``details_json``
    contains explicitly selected, non-secret change context.

    Database-level triggers reject edits and deletion of committed events.
    Rollbacks of an uncommitted transaction also roll back its audit entries.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("actor_kind IN ('user', 'collector', 'system')", name="valid_actor_kind"),
        CheckConstraint("json_valid(details_json)", name="valid_details_json"),
        Index("ix_audit_events_subject", "subject_kind", "subject_id"),
        Index("ix_audit_events_actor", "actor_kind", "actor_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), index=True
    )
    actor_kind: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[int | None] = mapped_column(Integer)
    actor_name: Mapped[str | None] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(100))
    subject_kind: Mapped[str] = mapped_column(String(80))
    subject_id: Mapped[int | None] = mapped_column(Integer)
    details_json: Mapped[str] = mapped_column(Text, default="{}")


def _safe_details(value: Mapping[str, Any]) -> str:
    """Serialize explicitly selected details, rejecting sensitive key names.

    Args:
        value: An application-defined mapping containing non-secret change
            details, not raw HTTP payloads or integration configuration.

    Returns:
        str: Finite JSON string suitable for storage in the audit table.

    Raises:
        TypeError: Details are not a mapping or cannot be encoded as JSON.
        ValueError: Details include a credential-like key or non-finite number.

    Note:
        Key screening is a guardrail, **not** general secret detection. It
        cannot identify a token hidden in an unrelated string. Callers must
        only construct curated, safe details in the first place.
    """
    if not isinstance(value, Mapping):
        raise TypeError("Audit details must be a mapping")

    def check_keys(item: Any) -> None:
        """Recursively reject credential-like keys in nested details."""
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("Audit detail keys must be strings")
                if key.lower().replace("-", "_") in _SECRET_FIELDS:
                    raise ValueError("Audit details must not contain credentials")
                check_keys(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check_keys(child)

    check_keys(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def record_audit_event(
    db: Session,
    *,
    actor_kind: str,
    action: str,
    subject_kind: str,
    subject_id: int | None = None,
    actor_id: int | None = None,
    actor_name: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> AuditEvent:
    """Add an auditable state change in the caller's current transaction.

    Args:
        db: SQLAlchemy session that is already updating application state.
        actor_kind: ``user``, ``collector``, or ``system``.
        action: Stable event name, e.g. ``discovery.device.created``.
        subject_kind: Name of the affected entity, e.g. ``discovered_device``.
        subject_id: Primary key of the affected entity, if known.
        actor_id: Optional internal user or discovery-source ID.
        actor_name: Optional human-readable actor label captured at the time.
        details: Curated, JSON-compatible and non-sensitive event context.

    Returns:
        AuditEvent: New event queued in the same transaction as the change.

    Raises:
        ValueError: Invalid actor, event metadata, or sensitive details.
        TypeError: Non-serializable details.

    Important:
        The caller must commit its transaction. Do not commit here: a failed
        inventory operation must not leave a misleading successful audit event.
    """
    if actor_kind not in _ACTOR_KINDS:
        raise ValueError("Unknown audit actor kind")
    if not action or not subject_kind or len(action) > 100 or len(subject_kind) > 80:
        raise ValueError("Audit action and subject_kind must be nonempty and within limits")
    if actor_name is not None and len(actor_name) > 120:
        raise ValueError("Audit actor name exceeds 120 characters")
    event = AuditEvent(
        actor_kind=actor_kind,
        actor_id=actor_id,
        actor_name=actor_name,
        action=action,
        subject_kind=subject_kind,
        subject_id=subject_id,
        details_json=_safe_details(details or {}),
    )
    db.add(event)
    return event
