# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Turn untrusted decoded values into the plain types LabRat stores.

The collector and its adapters read source values with ``number``,
``integer``, and ``text``; the API and collector read stored observation
JSON with ``json_object`` and ``mac_address``. Adapters keep their own
variant as a one-line ``functools.partial`` of these, so each module states
in one place how its source's values are read.

Used by: api.clients, api.cluster, api.common, api.network,
api.observation_actions, api.topology, collector.adapters.nut,
collector.adapters.opnsense, collector.adapters.pihole,
collector.adapters.unifi_classic, collector.client_links, collector.clients,
collector.identity, collector.links, collector.nut_wired,
collector.proxmox_clients, collector.proxmox_cluster,
collector.proxmox_live, collector.runner, collector.sightings,
collector.store, collector.unifi_network, collector.vpn,
collector.wireless_clients.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

_MAC_DIGITS = re.compile(r"[0-9a-fA-F]{12}")
_MAC_SEPARATED = re.compile(r"(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}")


def json_object(value: str | None) -> dict[str, Any]:
    """Decode stored JSON text that should hold an object.

    Args:
        value: JSON text from an observation or telemetry row, or None.

    Returns:
        dict[str, Any]: The decoded object, or an empty mapping when the
            text is missing, malformed, or not an object.
    """
    if value is None:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def mac_address(value: object) -> str | None:
    """Normalize a MAC address to twelve lowercase hexadecimal digits.

    Args:
        value: A port or interface identifier that may be a MAC address,
            bare or with ``:`` or ``-`` separators.

    Returns:
        str | None: The normalized address, or None when it is not one.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if _MAC_DIGITS.fullmatch(raw):
        return raw.lower()
    if _MAC_SEPARATED.fullmatch(raw):
        return re.sub(r"[:-]", "", raw).lower()
    return None


def number(value: Any, *, strings: bool = False, minimum: float | None = None) -> float | None:
    """Read a finite number from a decoded JSON value.

    Booleans are never numbers, and NaN and infinities are refused.

    Args:
        value: The decoded value.
        strings: Also accept numeric text (Proxmox and NUT send numbers
            as strings).
        minimum: Refuse values below this (counters and rates use 0).

    Returns:
        float | None: The number, or None when it is missing, not numeric,
            not finite, or below ``minimum``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif strings and isinstance(value, str):
        try:
            result = float(value)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        return None
    return result


def integer(value: Any, *, strings: bool = False, truncate: bool = False) -> int | None:
    """Read a whole number from a decoded JSON value.

    Args:
        value: The decoded value.
        strings: Also accept numeric text.
        truncate: Drop the fraction instead of rounding to the nearest.

    Returns:
        int | None: The whole number, or None when ``number`` refuses it.
    """
    result = number(value, strings=strings)
    if result is None:
        return None
    return int(result) if truncate else int(round(result))


def text(value: Any, limit: int = 160, *, strip: bool = True) -> str | None:
    """Read bounded text from a decoded JSON value.

    Args:
        value: The decoded value.
        limit: Most characters kept.
        strip: Trim surrounding whitespace, and treat blank text as missing.

    Returns:
        str | None: The text cut to ``limit``, or None when it is not a
            string or is empty.
    """
    if not isinstance(value, str):
        return None
    if strip:
        value = value.strip()
    return value[:limit] if value else None
