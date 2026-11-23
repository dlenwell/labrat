# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Turn audit events into short, human-readable activity entries.

The Profile page lists a user's recent changes. Audit events already hold
only curated, non-secret details; this module picks a readable label and a
subject name from them and never exposes the raw ``details_json``.
"""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from labrat.audit import AuditEvent
from labrat.models import Device, Rack, Room, Site
from labrat.pages.status import describe_age
from labrat.placement import PlacementContainer

#: Friendly labels for known audit actions. Unknown actions fall back to a
#: label built from the last part of the action name.
ACTION_LABELS = {
    "auth.password.changed": "Changed your password",
    "inventory.container.positioned": "Moved in a room layout",
    "inventory.device.created": "Added equipment",
    "inventory.device.placed": "Placed equipment",
    "inventory.device.unplaced": "Removed a placement",
    "inventory.location.created": "Created a location",
    "inventory.rack.updated": "Edited rack details",
}

#: Detail keys that may hold the subject's own name, in order of preference.
#: ``container_name`` is not here: on a placement it names the destination.
_NAME_KEYS = ("name", "device_name", "rack_name", "room_name", "site_name")

#: Records whose current name can be looked up for an audit subject.
_SUBJECT_MODELS = {
    "device": Device,
    "placement_container": PlacementContainer,
    "rack": Rack,
    "room": Room,
    "site": Site,
}


def _details(details_json: str) -> dict[str, Any]:
    """Parse stored audit details, treating anything malformed as empty.

    Args:
        details_json: The event's stored JSON details.

    Returns:
        dict[str, Any]: Parsed details, or an empty mapping.
    """
    try:
        details = json.loads(details_json or "{}")
    except ValueError:
        return {}
    return details if isinstance(details, dict) else {}


def _subject_name(details: dict[str, Any]) -> str | None:
    """Find the subject's name inside curated audit details.

    Looks at the top level, then the ``after`` and ``before`` snapshots.

    Args:
        details: Parsed audit details.

    Returns:
        str | None: The first non-empty name found, or ``None``.
    """
    for scope in (details, details.get("after"), details.get("before")):
        if not isinstance(scope, dict):
            continue
        for key in _NAME_KEYS:
            value = scope.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def describe_event(
    event: AuditEvent, now: datetime, current_name: str | None = None
) -> dict[str, Any]:
    """Build one display entry for an audit event.

    Args:
        event: Stored audit event.
        now: Current time, used for the relative age.
        current_name: The subject's name today, if it still exists. Preferred
            over the name stored in the event, which may be out of date.

    Returns:
        dict[str, Any]: ``label``, ``subject``, ``age``, and ISO ``when``.
    """
    label = ACTION_LABELS.get(event.action)
    if label is None:
        label = event.action.rsplit(".", 1)[-1].replace("_", " ").capitalize()
    kind = event.subject_kind.replace("_", " ")
    details = _details(event.details_json)
    name = current_name or _subject_name(details)
    if name:
        subject = f"{kind} · {name}"
    elif event.subject_id is not None:
        subject = f"{kind} #{event.subject_id}"
    else:
        subject = kind
    # A placement also names where the equipment went.
    after = details.get("after")
    if isinstance(after, dict) and isinstance(after.get("container_name"), str):
        subject = f"{subject} → {after['container_name']}"
    return {
        "label": label,
        "subject": subject,
        "age": describe_age(event.occurred_at, now),
        "when": event.occurred_at.isoformat() if event.occurred_at else "",
    }


def recent_user_activity(
    db: Session, user_id: int, now: datetime, limit: int = 10
) -> tuple[list[dict[str, Any]], int]:
    """Return a user's newest audited changes and their total count.

    Args:
        db: Open database session.
        user_id: The acting user's ID.
        now: Current time, used for relative ages.
        limit: Maximum number of entries to return.

    Returns:
        tuple[list[dict[str, Any]], int]: Newest-first entries and the total
            number of changes the user has made.
    """
    mine = (AuditEvent.actor_kind == "user", AuditEvent.actor_id == user_id)
    events = db.scalars(
        select(AuditEvent).where(*mine).order_by(AuditEvent.id.desc()).limit(limit)
    ).all()
    total = db.scalar(select(func.count()).select_from(AuditEvent).where(*mine)) or 0

    # Look up today's names for the subjects in one query per kind.
    names: dict[tuple[str, int], str] = {}
    for kind, model in _SUBJECT_MODELS.items():
        ids = {e.subject_id for e in events if e.subject_kind == kind and e.subject_id is not None}
        if ids:
            for row_id, name in db.execute(select(model.id, model.name).where(model.id.in_(ids))):
                if name:
                    names[(kind, row_id)] = name
    return [
        describe_event(event, now, names.get((event.subject_kind, event.subject_id)))
        for event in events
    ], total
