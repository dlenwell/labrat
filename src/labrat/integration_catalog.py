# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Load public integration form definitions separately from private settings.

The packaged TOML catalog supplies category navigation, labels, required fields,
validation, and the read-only connection check. It contains no credentials.
Adapters still implement collection; adding a form does not invent an adapter.

Used by: collector.sources, integrations.
"""

from importlib.resources import files
import re
from string import Formatter
import tomllib


def load_catalog() -> tuple[tuple[dict, ...], tuple[dict, ...]]:
    """Load and check the trusted form catalog bundled with the application.

    Returns:
        tuple: Ordered category and integration definitions, each with a key.

    Raises:
        ValueError: A definition is incomplete, ambiguous, or unsupported.
    """
    raw = files("labrat").joinpath("integration_catalog.toml").read_text(encoding="utf-8")
    data = tomllib.loads(raw)
    if data.get("version") != 1:
        raise ValueError("Unsupported integration catalog version")
    categories = tuple({"key": key, **definition} for key, definition in data["categories"].items())
    integrations = tuple({"kind": key, **definition} for key, definition in data["integrations"].items())
    category_keys = {entry["key"] for entry in categories}
    reserved = {"name", "display_name", "kind", "is_new", "revision", "_csrf_token", "enabled", "tls_mode", "ca_bundle"}
    for entry in integrations:
        if entry["category"] not in category_keys:
            raise ValueError("Unknown integration category")
        names = set()
        required_names = {field["key"] for field in entry["fields"] if field.get("required")}
        for field in entry["fields"]:
            key = field["key"]
            if key in names or key in reserved or not re.fullmatch(r"[a-z][a-z0-9_]*", key):
                raise ValueError("Duplicate or reserved integration field")
            names.add(key)
            if field["type"] not in {"url", "text", "password", "number", "select", "checkbox"}:
                raise ValueError("Unsupported integration field type")
            if "pattern" in field:
                re.compile(field["pattern"])
            if field["type"] == "select" and not field.get("choices"):
                raise ValueError("Select fields need choices")
        if "url" not in names:
            raise ValueError("An API integration needs a controller address")
        test = entry["test"]
        if not test["path"].startswith("/") or test["path"].startswith("//") or any(ord(c) < 32 for c in test["path"]):
            raise ValueError("A connection test must use a controller-relative path")
        # The generic header test fills its template from required fields;
        # an adapter-driven test reads the settings itself, so its secret may
        # be optional (a Pi-hole without a password).
        template_fields = names if test.get("style") == "adapter" else required_names
        for _literal, field_name, format_spec, conversion in Formatter().parse(test["auth_template"]):
            if field_name is not None and (field_name not in template_fields or format_spec or conversion):
                raise ValueError("Connection test references an unknown credential field")
    return categories, integrations


CATEGORIES, CATALOG = load_catalog()
KINDS = {entry["kind"]: entry["label"] for entry in CATALOG}
DEFINITIONS = {entry["kind"]: entry for entry in CATALOG}
