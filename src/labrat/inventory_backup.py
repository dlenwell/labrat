# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Export and transactionally restore versioned, curated inventory snapshots.

This format deliberately excludes accounts, secrets, discovery observations,
telemetry, audit history, and observation-ignore rules. Numeric inventory IDs
are valid only for the same instance or an empty inventory. Discovery source
namespaces and inventory associations are portable without copying evidence.
"""

import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from labrat import models
from labrat.discovery.models import (
    DiscoveredDevice, DiscoverySource, InterfaceLinkPolicy,
)
from labrat.metadata import Attribute, InventoryObject
from labrat.metadata_values import decode_value
from labrat.placement import DevicePlacement, PlacementContainer

FORMAT = "labrat-inventory"
VERSION = 1
MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 20000

# Explicit columns prevent later model additions from silently exporting secrets.
TABLES = (
    (models.Site, "Locations", "id name"),
    (models.Room, "Rooms", "id site_id name"),
    (models.Rack, "Racks", "id room_id name height_u width_inches has_rear"),
    (PlacementContainer, "Mounting places", "id room_id parent_id rack_id name kind wall_side rack_u rack_face position_json"),
    (models.DeviceClass, "Equipment types", "id name description"),
    (models.Device, "Equipment", "id name device_class_id parent_id room_id rack_id rack_u height_u placement port_facing manufacturer model serial_number asset_id"),
    (models.Group, "Groups", "id name description"),
    (models.DeviceGroup, "Manual group assignments", "device_id group_id source"),
    (models.Interface, "Inventory interfaces", "id device_id parent_id name kind mac speed_mbps"),
    (models.Network, "Networks", "id site_id vlan_id name"),
    (models.InterfaceMembership, "Interface memberships", "group_interface_id member_interface_id"),
    (models.InterfaceLink, "Inventory cables", "id a_interface_id b_interface_id"),
    (InterfaceLinkPolicy, "Cable policies", "interface_link_id manual_locked suppress_conflicts"),
    (models.InterfaceNetwork, "Interface networks", "interface_id network_id"),
    (models.IPAddress, "Inventory addresses", "id interface_id address prefix_length"),
    (models.DeviceManagementInterface, "Management interfaces", "device_id interface_id"),
    (DevicePlacement, "Equipment placements", "device_id container_id rack_u face position_json"),
)


class BackupError(ValueError):
    """Reject an unsupported, malformed, conflicting, or stale inventory backup."""


def canonical(value: object) -> str:
    """Encode deterministic finite JSON for backup comparison and hashing.

    Args:
        value: JSON-compatible data.

    Returns:
        str: Compact JSON with sorted keys, the same for equal data.
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def instance_id(database: Path) -> str:
    """Read or exclusively create a private instance marker next to SQLite.

    Args:
        database: Absolute path to this checkout's SQLite database.

    Returns:
        str: Persistent UUID used to prevent accidental cross-inventory writes.
    """
    path = database.with_name(database.name + ".inventory-instance")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return str(UUID(path.read_text().strip()))
    identifier = str(uuid4())
    with os.fdopen(fd, "w") as handle:
        handle.write(identifier + "\n")
    return identifier


def adopt_instance(database: Path, identifier: str) -> None:
    """Carry backup lineage forward after successfully restoring empty inventory.

    Args:
        database: Destination SQLite database.
        identifier: Validated source instance UUID to adopt after commit.
    """
    identifier = str(UUID(identifier))
    target = database.with_name(database.name + ".inventory-instance")
    temporary = target.with_name(target.name + "." + uuid4().hex)
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(identifier + "\n")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def snapshot(db: Session) -> dict:
    """Read whitelisted inventory and source identities without operational data.

    Args:
        db: Open database session in a consistent read transaction.

    Returns:
        dict: Inventory tables, owner-addressed attributes, namespaces, and links.
    """
    tables = {}
    for model, _label, fields in TABLES:
        columns = [model.__table__.c[name] for name in fields.split()]
        query = select(*columns).order_by(*model.__table__.primary_key.columns)
        if model is models.DeviceGroup:
            query = query.where(models.DeviceGroup.source == "manual")
        tables[model.__tablename__] = [dict(row) for row in db.execute(query).mappings()]
    attributes = []
    for attribute, owner in db.execute(select(Attribute, InventoryObject).join(
        InventoryObject, Attribute.owner_id == InventoryObject.id,
    ).order_by(Attribute.id)):
        kind = next(key for key in ("site_id", "room_id", "rack_id", "device_id") if getattr(owner, key) is not None)
        attributes.append({"id": attribute.id, "owner_kind": kind,
                           "owner_id": getattr(owner, kind), "parent_id": attribute.parent_id,
                           "key": attribute.key, "value": attribute.value})
    namespaces = [dict(row) for row in db.execute(select(
        DiscoverySource.id, DiscoverySource.name, DiscoverySource.kind, DiscoverySource.config_name,
    ).order_by(DiscoverySource.id)).mappings()]
    associations = [dict(row) for row in db.execute(select(
        DiscoveredDevice.source_id, DiscoveredDevice.remote_id, DiscoveredDevice.device_id,
    ).where(DiscoveredDevice.device_id.is_not(None)).order_by(
        DiscoveredDevice.source_id, DiscoveredDevice.remote_id,
    )).mappings()]
    return {"tables": tables, "attributes": attributes,
            "source_namespaces": namespaces, "discovery_associations": associations}


def fingerprint(db: Session) -> str:
    """Hash curated state, excluding timestamps that normal collector polls change.

    Args:
        db: An open database session.

    Returns:
        str: The SHA-256 of the curated snapshot, in hexadecimal.
    """
    return hashlib.sha256(canonical(snapshot(db)).encode()).hexdigest()


def export_backup(db: Session, identifier: str) -> dict:
    """Build a portable envelope for the current curated inventory snapshot.

    Args:
        db: An open database session, ideally in one read transaction.
        identifier: This instance's marker (``instance_id``).

    Returns:
        dict: The backup: format, version, instance, time, and tables.
    """
    return {"format": FORMAT, "version": VERSION, "instance_id": identifier,
            "created_at": datetime.now(timezone.utc).isoformat(), **snapshot(db)}


def _reject_duplicates(pairs: list) -> dict:
    """Reject duplicate JSON keys instead of silently taking the last value.

    Args:
        pairs: Key and value pairs from ``json.loads``'s ``object_pairs_hook``.

    Returns:
        dict: The object.

    Raises:
        BackupError: When a key appears twice.
    """
    result = {}
    for key, value in pairs:
        if key in result:
            raise BackupError("The backup contains duplicate fields.")
        result[key] = value
    return result


def decode_backup(raw: bytes) -> dict:
    """Decode a bounded backup and validate its complete, versioned structure.

    Args:
        raw: Uploaded UTF-8 JSON, never an archive or executable database.

    Returns:
        dict: Validated snapshot envelope suitable for transactional preview.

    Raises:
        BackupError: Size, format, field, type, or internal reference is invalid.
    """
    if len(raw) > MAX_BYTES:
        raise BackupError("Choose an inventory backup smaller than 5 MB.")
    try:
        data = json.loads(raw, object_pairs_hook=_reject_duplicates,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        expected = {"format", "version", "instance_id", "created_at", "tables", "attributes",
                    "source_namespaces", "discovery_associations"}
        if not isinstance(data, dict) or set(data) != expected:
            raise ValueError()
        if data["format"] != FORMAT or type(data["version"]) is not int or data["version"] != VERSION:
            raise BackupError("This file is not a supported LabRat inventory backup (version 1).")
        UUID(data["instance_id"])
        created = datetime.fromisoformat(data["created_at"])
        if created.tzinfo is None:
            raise ValueError()
        data["created_at"] = created.astimezone(timezone.utc).isoformat()
        tables = data["tables"]
        if not isinstance(tables, dict) or set(tables) != {model.__tablename__ for model, _, _ in TABLES}:
            raise ValueError()
        collections = list(tables.values()) + [data[name] for name in (
            "attributes", "source_namespaces", "discovery_associations")]
        if any(not isinstance(rows, list) for rows in collections):
            raise ValueError()
        if sum(map(len, collections)) > MAX_ROWS:
            raise BackupError("This backup exceeds the 20,000-record inventory limit.")
        total = 0
        for model, _label, fields in TABLES:
            rows = tables[model.__tablename__]
            if not isinstance(rows, list):
                raise ValueError()
            total += len(rows)
            keys = set()
            for row in rows:
                if not isinstance(row, dict) or set(row) != set(fields.split()):
                    raise ValueError()
                for name, value in row.items():
                    column = model.__table__.c[name]
                    if value is None:
                        if not column.nullable:
                            raise ValueError()
                        continue
                    expected_type = column.type.python_type
                    if type(value) is not expected_type:
                        raise ValueError()
                    if expected_type is str and ("\x00" in value or (getattr(column.type, "length", None) and len(value) > column.type.length)):
                        raise ValueError()
                    if expected_type is int and not -(2 ** 63) <= value < 2 ** 63:
                        raise ValueError()
                    if (column.primary_key or column.foreign_keys) and expected_type is int and value < 1:
                        raise ValueError()
                    if name == "name" and not value.strip():
                        raise ValueError()
                key = tuple(row[column.name] for column in model.__table__.primary_key)
                if key in keys:
                    raise ValueError()
                keys.add(key)
                if model is models.DeviceGroup and row["source"] != "manual":
                    raise ValueError()
        for name in ("attributes", "source_namespaces", "discovery_associations"):
            if not isinstance(data[name], list):
                raise ValueError()
            total += len(data[name])
        if total > MAX_ROWS:
            raise BackupError("This backup exceeds the 20,000-record inventory limit.")
        _validate_references(data)
    except BackupError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as exc:
        raise BackupError("The backup is incomplete or contains invalid inventory data.") from exc
    return data


def _validate_references(data: dict) -> None:
    """Require self-contained foreign keys and validate auxiliary record fields.

    Args:
        data: The decoded backup.

    Raises:
        ValueError: When a reference points outside the backup or a field is
            malformed; the caller reports it as an invalid backup.
    """
    tables = data["tables"]
    references = {
        (model.__tablename__, column.name): {row[column.name] for row in tables[model.__tablename__]}
        for model, _, fields in TABLES for column in model.__table__.columns
        if column.primary_key and column.name in fields.split()
    }
    for model, _, _ in TABLES:
        for row in tables[model.__tablename__]:
            for column in model.__table__.columns:
                if column.name not in row or row[column.name] is None:
                    continue
                for foreign in column.foreign_keys:
                    target = foreign.column
                    if row[column.name] not in references[(target.table.name, target.name)]:
                        raise ValueError()
    source_ids = set()
    source_keys = set()
    for source in data["source_namespaces"]:
        if set(source) != {"id", "kind", "name", "config_name"} or type(source["id"]) is not int or source["id"] < 1:
            raise ValueError()
        for key, maximum in (("name", 120), ("kind", 40), ("config_name", 120)):
            if not isinstance(source[key], str) or not source[key].strip() or len(source[key]) > maximum:
                raise ValueError()
        identity = (source["kind"], source["config_name"])
        if source["id"] in source_ids or identity in source_keys:
            raise ValueError()
        source_ids.add(source["id"])
        source_keys.add(identity)
    devices = {row["id"] for row in tables["devices"]}
    associations = set()
    for row in data["discovery_associations"]:
        if set(row) != {"source_id", "remote_id", "device_id"} or type(row["source_id"]) is not int or type(row["device_id"]) is not int:
            raise ValueError()
        if row["source_id"] not in source_ids or row["device_id"] not in devices or not isinstance(row["remote_id"], str) or not 0 < len(row["remote_id"]) <= 255:
            raise ValueError()
        key = (row["source_id"], row["remote_id"])
        if key in associations:
            raise ValueError()
        associations.add(key)
    attributes = {row["id"]: row for row in data["attributes"]}
    if len(attributes) != len(data["attributes"]):
        raise ValueError()
    owner_tables = {"site_id": "sites", "room_id": "rooms", "rack_id": "racks", "device_id": "devices"}
    owner_ids = {kind: {row["id"] for row in tables[table]} for kind, table in owner_tables.items()}
    for row in attributes.values():
        if set(row) != {"id", "owner_kind", "owner_id", "parent_id", "key", "value"} or type(row["id"]) is not int or row["id"] < 1:
            raise ValueError()
        if row["owner_kind"] not in owner_tables or type(row["owner_id"]) is not int:
            raise ValueError()
        if row["owner_id"] not in owner_ids[row["owner_kind"]]:
            raise ValueError()
        if not isinstance(row["key"], str) or not 0 < len(row["key"]) <= 160 or not isinstance(row["value"], str):
            raise ValueError()
        decode_value(row["value"])
        parent = row["parent_id"]
        visited = {row["id"]}
        while parent is not None:
            if type(parent) is not int or parent in visited or parent not in attributes:
                raise ValueError()
            visited.add(parent)
            other = attributes[parent]
            if (other["owner_kind"], other["owner_id"]) != (row["owner_kind"], row["owner_id"]):
                raise ValueError()
            parent = other["parent_id"]


def _upsert(db: Session, model: type, row: dict) -> None:
    """Insert or update only explicitly selected columns by primary key.

    Args:
        db: The open restore session.
        model: The table's model.
        row: One row from the backup, keyed by column.
    """
    table = model.__table__
    keys = [column.name for column in table.primary_key]
    changes = {key: value for key, value in row.items() if key not in keys}
    statement = insert(table).values(**row)
    if changes:
        statement = statement.on_conflict_do_update(index_elements=keys, set_=changes)
    else:
        statement = statement.on_conflict_do_nothing(index_elements=keys)
    db.execute(statement)


def restore_backup(db: Session, data: dict, identifier: str) -> dict:
    """Apply a validated snapshot inside the caller's transaction or savepoint.

    Same-instance IDs are restored in place, keeping extra inventory entities.
    Another instance must have empty curated inventory. Discovery observations
    and audit history are never replaced. Call this inside a rolled-back
    savepoint for preview, then repeat after checking the preview fingerprint.

    Args:
        db: Transaction owned by the route; no commit happens here.
        data: Structurally validated backup envelope.
        identifier: Destination's private persistent instance UUID.

    Returns:
        dict: Per-table create/update/unchanged counts and relinking summary.

    Raises:
        BackupError: The destination, references, or resulting layout conflict.
    """
    before = snapshot(db)
    if data["instance_id"] != identifier and (
        any(before["tables"].values()) or before["attributes"]
    ):
        raise BackupError("This backup belongs to another LabRat instance. Restore it into an empty inventory instead.")
    db.execute(text("PRAGMA defer_foreign_keys=ON"))
    summary = []
    for model, label, _fields in TABLES:
        table = model.__tablename__
        old = {tuple(row[c.name] for c in model.__table__.primary_key): row for row in before["tables"][table]}
        counts = {"label": label, "total": len(data["tables"][table]), "created": 0, "updated": 0, "unchanged": 0}
        for row in data["tables"][table]:
            key = tuple(row[c.name] for c in model.__table__.primary_key)
            counts["created" if key not in old else "unchanged" if old[key] == row else "updated"] += 1
        if counts["total"]:
            summary.append(counts)
    device_ids = [row["id"] for row in data["tables"]["devices"]]
    restored_placements = {row["device_id"] for row in data["tables"]["device_placements"]}
    cleared = sum(row["device_id"] in device_ids and row["device_id"] not in restored_placements for row in before["tables"]["device_placements"])
    # Replace the placement/manual membership sets only for backed-up devices.
    db.execute(delete(DevicePlacement).where(DevicePlacement.device_id.in_(device_ids)))
    db.execute(delete(models.DeviceGroup).where(models.DeviceGroup.device_id.in_(device_ids), models.DeviceGroup.source == "manual"))
    for model, _label, _fields in TABLES:
        for row in data["tables"][model.__tablename__]:
            _upsert(db, model, row)
    source_map = {}
    for source in data["source_namespaces"]:
        found = db.scalar(select(DiscoverySource).where(
            DiscoverySource.kind == source["kind"], DiscoverySource.config_name == source["config_name"],
        ))
        if found is None:
            found = DiscoverySource(name=source["name"], kind=source["kind"], config_name=source["config_name"], enabled=False)
            db.add(found)
            db.flush()
        source_map[source["id"]] = found.id
    for row in data["attributes"]:
        owner = db.scalar(select(InventoryObject.id).where(getattr(InventoryObject, row["owner_kind"]) == row["owner_id"]))
        value = row["value"]
        if row["key"] == "discovery.lldp_port_match":
            decoded = decode_value(value)
            match = decoded.get("json", {})
            for key in ("source_id", "target_source_id"):
                if match.get(key) not in source_map:
                    raise BackupError("A saved identity match refers to a missing discovery source.")
                match[key] = source_map[match[key]]
            value = canonical(decoded)
        _upsert(db, Attribute, {"id": row["id"], "owner_id": owner, "parent_id": row["parent_id"], "key": row["key"], "value": value})
    matched = 0
    for row in data["discovery_associations"]:
        observation = db.scalar(select(DiscoveredDevice).where(
            DiscoveredDevice.source_id == source_map[row["source_id"]], DiscoveredDevice.remote_id == row["remote_id"],
        ))
        if observation is None:
            continue
        if observation.device_id not in (None, row["device_id"]):
            raise BackupError("A discovered device is now linked to different inventory. Resolve that link before restoring.")
        observation.device_id = row["device_id"]
        matched += 1
    db.flush()
    db.expire_all()
    if db.execute(text("PRAGMA foreign_key_check")).first():
        raise BackupError("The backup would leave an invalid inventory reference.")
    _validate_layout(db)
    return {"rows": summary, "cleared_placements": cleared,
            "attributes": len(data["attributes"]), "matched": matched,
            "unmatched": len(data["discovery_associations"]) - matched,
            "same_instance": data["instance_id"] == identifier}


def _validate_layout(db: Session) -> None:
    """Check final hierarchy, coordinates, rack occupancy, and legacy placement.

    Args:
        db: The restore session, after the backup's rows are applied.

    Raises:
        BackupError: When the restored places or equipment break a layout
            rule: a circular or cross-room parent, an unsupported rack or
            place, a misplaced shelf or wall, an invalid height or position,
            or overlapping rack equipment.
    """
    # Import the existing placement rules without duplicating rack collision logic.
    from labrat.api.placement_rules import check_rack_space
    containers = {row.id: row for row in db.scalars(select(PlacementContainer))}
    devices = {row.id: row for row in db.scalars(select(models.Device))}
    for model, rows in ((PlacementContainer, containers), (models.Device, devices)):
        for row in rows.values():
            visited = {row.id}
            parent_id = row.parent_id
            while parent_id is not None:
                if parent_id in visited or parent_id not in rows:
                    raise BackupError("The backup contains a circular or missing parent.")
                visited.add(parent_id)
                parent = rows[parent_id]
                if model is PlacementContainer and parent.room_id != row.room_id:
                    raise BackupError("A mounting place cannot have a parent in another room.")
                parent_id = parent.parent_id
    for rack in db.scalars(select(models.Rack)):
        if rack.width_inches not in {10, 19} or not 1 <= rack.height_u <= 128:
            raise BackupError("The backup contains unsupported rack dimensions.")
    for container in containers.values():
        if container.kind not in {"rack", "rack_top", "rack_shelf", "wall", "floor", "ceiling", "shelf", "surface", "area"}:
            raise BackupError("The backup contains an unsupported mounting place.")
        if container.rack_id:
            rack = db.get(models.Rack, container.rack_id)
            if rack.room_id != container.room_id:
                raise BackupError("A rack and its mounting place must share a room.")
        if container.kind in {"rack_shelf", "rack_top"}:
            parent = containers.get(container.parent_id)
            if parent is None or parent.kind != "rack":
                raise BackupError("A rack shelf or top must belong to a rack.")
        if container.kind == "rack_shelf":
            rack = db.get(models.Rack, containers[container.parent_id].rack_id)
            if container.rack_u is None or not 1 <= container.rack_u <= rack.height_u or container.rack_face not in {"front", "rear", "full_depth"}:
                raise BackupError("A shared shelf has an invalid rack position.")
            if not rack.has_rear and container.rack_face != "front":
                raise BackupError("A shared shelf uses a rear face that does not exist.")
        if container.kind == "wall" and container.wall_side not in {"north", "east", "south", "west"}:
            raise BackupError("A wall must have a compass direction.")
        _validate_position(container.position_json)
    for device in devices.values():
        if device.height_u is not None and not 1 <= device.height_u <= 128:
            raise BackupError("Equipment has an invalid height.")
        if device.port_facing not in {None, "front", "rear"}:
            raise BackupError("Equipment has an invalid port direction.")
    for placement in db.scalars(select(DevicePlacement)):
        container = containers[placement.container_id]
        device = devices[placement.device_id]
        _validate_position(placement.position_json)
        if container.kind == "rack":
            if placement.rack_u is None:
                raise BackupError("A rack placement is missing its starting unit.")
            try:
                check_rack_space(db, container, device.id, placement.rack_u, device.height_u or 1, placement.face)
            except ValueError as exc:
                raise BackupError("The resulting rack layout has an invalid or occupied position.") from exc
        elif container.kind == "rack_shelf":
            if (placement.rack_u, placement.face) != (container.rack_u, container.rack_face):
                raise BackupError("Equipment does not match its shared shelf position.")
        elif placement.rack_u is not None or placement.face is not None:
            raise BackupError("A non-rack placement cannot specify rack units or a face.")
        expected_rack = container.rack_id if container.kind == "rack" else containers[container.parent_id].rack_id if container.kind == "rack_shelf" else None
        if (device.room_id, device.rack_id, device.rack_u, device.placement) != (container.room_id, expected_rack, placement.rack_u, container.kind):
            raise BackupError("Equipment placement references disagree with the saved layout.")


def _validate_position(raw: str | None) -> None:
    """Reject malformed or non-finite normalized placement coordinates.

    Args:
        raw: The stored position JSON, or None.

    Raises:
        BackupError: When it is not an object of finite numbers within range.
    """
    if raw is None:
        return
    try:
        position = json.loads(raw)
        if not isinstance(position, dict) or set(position) - {"x", "y", "along"}:
            raise ValueError()
        for key, value in position.items():
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError()
            if not 0 <= value <= 1:
                raise ValueError()
    except (ValueError, TypeError) as exc:
        raise BackupError("A room layout contains invalid coordinates.") from exc
