# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Build safe schema-driven field views for Settings drawers.

Used by: discovery_settings, integrations.
"""


def field_views(definitions: list, settings: dict, submitted=None) -> list[dict]:
    """Render catalog fields with empty secrets and optional safe non-secret drafts.

    Args:
        definitions: The catalog's field definitions.
        settings: The saved settings.
        submitted: The posted form when re-rendering after an error.

    Returns:
        list[dict]: One view per field: its definition, its value (never a
            secret), and whether a secret is already saved.
    """
    fields = []
    for definition in definitions:
        key = definition['key']
        secret = definition['type'] == 'password'
        value = '' if secret else submitted.get(key, '') if submitted is not None else settings.get(key, definition.get('default', ''))
        if isinstance(value, list):
            value = '\n'.join(value)
        fields.append({'definition': definition, 'value': value,
                       'saved': secret and bool(settings.get(key))})
    return fields
