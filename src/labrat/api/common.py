# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Request and validation helpers shared by the inventory API modules.

Write endpoints raise ``PlacementError`` with an HTTP status;
``write_payload`` checks the administrator, CSRF token, and JSON body every
write needs.

Used by: api.clients, api.locations, api.observation_actions,
api.placement_rules, api.rack_devices, api.topology.
"""

from datetime import datetime, timezone
import hmac
import json
import re
from typing import Any

from flask import request, session

from labrat.auth.session import current_user
from labrat.coerce import json_object
from labrat.discovery.models import DiscoveredDevice


def port_sort_key(name: str | None) -> list[tuple[int, int | str]]:
    """Sort port names the way people read them: "Port 2" before "Port 10".

    Digit runs compare as numbers and everything else compares
    case-insensitively, so ``nic0, nic1, nic2`` and ``Port 1 .. Port 24``
    come out in order regardless of the order a collector stored them.

    Args:
        name: Port name as reported by the source, possibly empty.

    Returns:
        list[tuple[int, int | str]]: Comparable key; numbers sort before text.
    """
    return [
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", name or "") if part
    ]


class PlacementError(ValueError):
    """Represent a rejected user placement without leaking internal errors."""

    def __init__(self, message: str, status: int = 400) -> None:
        """Associate a human-readable validation message with an HTTP status.

        Args:
            message: Safe explanation of the rejected operation.
            status: HTTP status returned to the browser.
        """
        super().__init__(message)
        self.status = status


def as_iso(value: datetime | None) -> str | None:
    """Convert a database timestamp into an API-friendly ISO string.

    Timestamps are stored in UTC, but SQLite returns them without a zone.
    Without one, browsers read the text as local time, so naive values are
    marked as UTC before formatting.

    Args:
        value: Recorded datetime or an absent timestamp.

    Returns:
        str | None: ISO-8601 timestamp with a UTC offset, or ``None`` when no
            timestamp exists.
    """
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def positive_int(value: Any, field: str, *, required: bool = True) -> int | None:
    """Validate a positive JSON integer without accepting booleans.

    Args:
        value: Untrusted decoded JSON value.
        field: Field label used in validation errors.
        required: Whether ``None`` is allowed.

    Returns:
        int | None: Validated identifier or absent optional value.

    Raises:
        PlacementError: A required value is missing or invalid.
    """
    if value is None and not required:
        return None
    if type(value) is not int or value < 1:
        raise PlacementError(f"{field} must be a positive integer")
    return value


def valid_name(value: Any) -> str:
    """Validate a short, nonempty human-authored inventory label.

    Args:
        value: Untrusted JSON value.

    Returns:
        str: Trimmed label suitable for an inventory model.

    Raises:
        PlacementError: Empty, non-string, or too-long label.
    """
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 120:
        raise PlacementError("Name must contain 1–120 characters")
    return value.strip()


def is_virtual_guest(observed: DiscoveredDevice) -> bool:
    """Return whether a client observation is a matched Proxmox VM or container.

    Guests are not physical equipment: they are never added to inventory or
    placed from Discovery (a later Proxmox cluster view will show them).

    Args:
        observed: Any discovered device row.

    Returns:
        bool: True for a client the collector matched to a guest NIC.
    """
    client = json_object(observed.details_json).get("client")
    return isinstance(client, dict) and client.get("virtual_endpoint") is True


def json_string_list(value: str | None) -> list[str]:
    """Decode a JSON array containing interface names only.

    Args:
        value: Stored JSON array from a logical interface observation.

    Returns:
        list[str]: Nonempty interface names, or an empty list if malformed.
    """
    if value is None:
        return []
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(decoded, list):
        return []
    return [item for item in decoded if isinstance(item, str) and item.strip()]


def write_payload() -> dict[str, Any]:
    """Require an administrator, CSRF token, and JSON object for state changes.

    Returns:
        dict[str, Any]: Untrusted request object after transport-level checks.

    Raises:
        PlacementError: Insufficient permission or malformed/forged request.
    """
    user = current_user()
    if user is None or not user.is_admin:
        raise PlacementError("Administrator permission required", 403)
    expected = session.get("csrf_token")
    submitted = request.headers.get("X-CSRF-Token")
    if not (
        isinstance(expected, str) and expected
        and isinstance(submitted, str)
        and hmac.compare_digest(expected, submitted)
    ):
        raise PlacementError("Missing or invalid CSRF token", 403)
    if not request.is_json:
        raise PlacementError("Expected application/json")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise PlacementError("Expected a JSON object")
    return payload
