# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Read operator-confirmed LLDP identities from inventory-owned attributes.

These are curated identity mappings, not telemetry or static cable placements.
The collector creates a cable only while the corresponding neighbor is observed.

Used by: api.observation_actions, api.topology, collector.links,
integrations.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from labrat.metadata import Attribute, InventoryObject
from labrat.metadata_values import decode_value

MATCH_ATTRIBUTE = "discovery.lldp_port_match"


def neighbor_key(source_id: int, chassis_id: str, port_id: str) -> tuple[int, str, str]:
    """Scope one reported neighbor identity to its UniFi discovery source.

    Args:
        source_id: Database ID of the controller discovery source.
        chassis_id: Reported LLDP chassis identifier.
        port_id: Reported remote port identifier, possibly empty.

    Returns:
        tuple[int, str, str]: Case-normalized identity used for exact matching.
    """
    return source_id, chassis_id.strip().casefold(), port_id.strip().casefold()


def saved_neighbor_matches(db: Session) -> list[dict]:
    """Load validated identity mappings with their canonical inventory owner.

    Args:
        db: Open read or transaction session.

    Returns:
        list[dict]: Curated mappings with attribute and inventory device IDs.
        Malformed attributes are ignored rather than treated as identities.
    """
    result = []
    rows = db.execute(select(Attribute, InventoryObject.device_id).join(
        InventoryObject, Attribute.owner_id == InventoryObject.id,
    ).where(Attribute.key == MATCH_ATTRIBUTE, InventoryObject.device_id.is_not(None)))
    for attribute, device_id in rows:
        try:
            value = decode_value(attribute.value).get("json")
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        if any(type(value.get(key)) is not int for key in ("source_id", "target_source_id")):
            continue
        fields = ("chassis_id", "port_id", "target_device_remote_id", "target_port_remote_id")
        if any(not isinstance(value.get(key), str) for key in fields):
            continue
        result.append({"id": attribute.id, "device_id": device_id,
                       **{key: value[key] for key in ("source_id", "target_source_id", *fields)}})
    return result
