# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Validate and encode type-tagged JSON values for inventory attributes.

Every stored value has exactly one type key. This is a serialization contract,
not an instruction to put changing telemetry measurements in attributes.

Used by: api.observation_actions, discovery.neighbor_matches,
inventory_backup.
"""

import json
from typing import Any


_VALID_TYPES = frozenset({"str", "int", "float", "bool", "json"})


def validate_value(value: dict[str, Any]) -> None:
    """Reject malformed or mismatched typed attribute values.

    Args:
        value: Mapping containing exactly one supported type tag and its value.

    Raises:
        ValueError: The type tag, value type, or JSON structure is invalid.
        TypeError: The supplied object is not a dictionary.
    """
    if not isinstance(value, dict):
        raise TypeError("An attribute value must be a JSON object")
    if len(value) != 1 or next(iter(value), None) not in _VALID_TYPES:
        raise ValueError("Expected exactly one supported type key")
    kind, item = next(iter(value.items()))
    valid = (
        (kind == "str" and isinstance(item, str))
        or (kind == "int" and type(item) is int)
        or (kind == "float" and type(item) in (int, float))
        or (kind == "bool" and type(item) is bool)
        or (kind == "json" and isinstance(item, (dict, list)))
    )
    if not valid:
        raise ValueError(f"Invalid value for attribute type: {kind}")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Value must be valid finite JSON") from exc


def encode_value(value: dict[str, Any]) -> str:
    """Serialize a validated typed value to stable JSON text.

    Args:
        value: The single-key typed value.

    Returns:
        str: Compact JSON suitable for the ``attributes.value`` TEXT column.
    """
    validate_value(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def decode_value(value: str) -> dict[str, Any]:
    """Decode and validate a previously stored attribute value.

    Args:
        value: JSON text from persistent storage.

    Returns:
        dict[str, Any]: One type-tagged JSON value.

    Raises:
        ValueError: The stored representation is invalid.
    """
    try:
        result = json.loads(value)
        validate_value(result)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Invalid stored attribute JSON") from exc
    return result
