# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Export a whole LabRat install and restore it, replacing everything, elsewhere.

For moving LabRat to a new way of hosting it (another server, a container,
Kubernetes): the export is one ZIP file with a consistent snapshot of the
whole database (inventory, discoveries, accounts, audit history) and the
local configuration's connection settings, credentials included. Importing
it replaces the database and those settings outright, so the new install
is the old one again.

Three configuration sections describe the host rather than the lab and stay
with the new install: ``[server]`` (the host names it answers to),
``[database]`` (where its file lives), and ``[auth]`` (its session key and
cookie settings). They are neither exported nor replaced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import tomllib
import zipfile

from alembic import command
from alembic.config import Config
from alembic.util import CommandError
from alembic.script import ScriptDirectory
import tomlkit

from labrat.collector.sources import configured_sources

FORMAT = "labrat-instance"
VERSION = 1
#: Configuration sections that belong to the host, not the lab.
HOST_SECTIONS = ("server", "database", "auth")
MANIFEST = "manifest.json"
DATABASE = "labrat.sqlite3"
CONFIG = "local_config.toml"
#: Largest upload accepted, and largest unpacked content (against ZIP bombs).
MAX_UPLOAD_BYTES = 256 * 1024 * 1024
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
#: Files beside the database that are derived from it and rebuilt as needed.
DERIVED_SUFFIXES = (".access-evidence", ".restore-previews", ".inventory-instance")


class TransferError(ValueError):
    """Explain, safely, why an export could not be made or an import was refused."""


@dataclass(frozen=True)
class Transfer:
    """A validated export, ready to import.

    Attributes:
        manifest: The export's manifest.
        database: The database snapshot's bytes.
        config: The exported configuration (no host sections), as TOML text.
    """

    manifest: dict
    database: bytes
    config: str


def alembic_config() -> Config:
    """Return migration settings for the ``migrations`` folder LabRat runs beside.

    No ``alembic.ini`` is read: its logging setup would replace the running
    app's. The migrations find the database through the local configuration,
    as the app does.

    Returns:
        Config: Alembic configuration pointing at the migration scripts.

    Raises:
        TransferError: When the migrations are not in the working directory.
    """
    scripts = Path.cwd() / "migrations"
    if not (scripts / "env.py").is_file():
        raise TransferError("LabRat's migration files were not found. Run LabRat from its install folder.")
    config = Config()
    config.set_main_option("script_location", str(scripts))
    return config


def _lab_settings(raw: str) -> tomlkit.TOMLDocument:
    """Parse configuration and drop the host sections.

    Args:
        raw: TOML text.

    Returns:
        tomlkit.TOMLDocument: The lab's settings only.
    """
    document = tomlkit.parse(raw)
    for section in HOST_SECTIONS:
        document.pop(section, None)
    return document


def export_instance(database: Path, config_path: Path, app_version: str) -> tuple[bytes, dict]:
    """Snapshot the database and connection settings into one ZIP file.

    SQLite's backup API copies a consistent snapshot while LabRat keeps
    running, including anything still in the write-ahead log.

    Args:
        database: The live SQLite file.
        config_path: The live configuration file.
        app_version: This LabRat's version, for the manifest.

    Returns:
        tuple[bytes, dict]: The ZIP file and its manifest.
    """
    with tempfile.TemporaryDirectory(prefix=".labrat-export-", dir=database.parent) as folder:
        snapshot = Path(folder) / DATABASE
        source = sqlite3.connect(database, timeout=30)
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
            # One self-contained file: no write-ahead log beside it.
            target.execute("PRAGMA journal_mode=DELETE")
            revision = target.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        finally:
            target.close()
            source.close()
        data = snapshot.read_bytes()
    manifest = {"format": FORMAT, "version": VERSION, "app_version": app_version, "schema": revision,
                "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    settings = tomlkit.dumps(_lab_settings(config_path.read_text(encoding="utf-8")))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST, json.dumps(manifest, indent=2) + "\n")
        archive.writestr(DATABASE, data)
        archive.writestr(CONFIG, settings)
    return buffer.getvalue(), manifest


def read_instance(raw: bytes) -> Transfer:
    """Check an uploaded export without changing anything.

    Args:
        raw: The uploaded file.

    Returns:
        Transfer: The validated contents.

    Raises:
        TransferError: Not a LabRat export, damaged, or from a newer LabRat.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise TransferError("This is not a LabRat export file.") from exc
    with archive:
        names = {item.filename for item in archive.infolist()}
        if names != {MANIFEST, DATABASE, CONFIG}:
            raise TransferError("This is not a LabRat export file.")
        if sum(item.file_size for item in archive.infolist()) > MAX_UNPACKED_BYTES:
            raise TransferError("This export unpacks to more than 1 GB, which LabRat does not accept.")
        try:
            manifest = json.loads(archive.read(MANIFEST))
            database = archive.read(DATABASE)
            config = archive.read(CONFIG).decode("utf-8")
        except (ValueError, zipfile.BadZipFile, UnicodeError) as exc:
            raise TransferError("This export file is damaged.") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise TransferError("This is not a LabRat export file.")
    if manifest.get("version") != VERSION:
        raise TransferError("This export was made by a different LabRat version and cannot be imported here.")
    try:
        configured_sources(tomllib.loads(config))
    except ValueError as exc:
        raise TransferError("The export's connection settings are damaged.") from exc
    revision = _check_database(database)
    try:
        known = ScriptDirectory.from_config(alembic_config()).get_revision(revision) is not None
    except CommandError:
        known = False
    if not known:
        raise TransferError("This export comes from a newer LabRat. Update this install first, then import it.")
    return Transfer(manifest={**manifest, "schema": revision}, database=database, config=config)


def _check_database(data: bytes) -> str:
    """Confirm the snapshot is an intact LabRat database and return its schema.

    Args:
        data: The snapshot's bytes.

    Returns:
        str: Its migration revision.

    Raises:
        TransferError: When it is not an intact LabRat database.
    """
    with tempfile.TemporaryDirectory(prefix=".labrat-check-") as folder:
        path = Path(folder) / DATABASE
        path.write_bytes(data)
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise TransferError("The export's database is damaged.")
                row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            finally:
                connection.close()
        except sqlite3.DatabaseError as exc:
            raise TransferError("The export's database is damaged or is not a LabRat database.") from exc
    if row is None or not isinstance(row[0], str):
        raise TransferError("The export's database is not a LabRat database.")
    return row[0]


def import_instance(database: Path, config_path: Path, transfer: Transfer) -> None:
    """Replace this install's database and connection settings with an export's.

    The snapshot is copied into the live database with SQLite's backup API,
    so connections LabRat already holds see the new contents, then migrated
    to this LabRat's schema. The configuration keeps this host's
    ``[server]``, ``[database]``, and ``[auth]`` sections and takes every
    other section from the export, replacing the connections outright.
    Files derived from the old database are removed.

    Args:
        database: The live SQLite file.
        config_path: The live configuration file.
        transfer: A validated export from :func:`read_instance`.

    Raises:
        TransferError: When LabRat is too busy to replace the database.
    """
    current = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    merged = _lab_settings(transfer.config)
    for section in HOST_SECTIONS:
        if section in current:
            merged[section] = current[section]
    settings = tomlkit.dumps(merged)
    configured_sources(tomllib.loads(settings))
    descriptor, name = tempfile.mkstemp(prefix=".labrat-import-", suffix=".sqlite3", dir=database.parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(transfer.database)
        source = sqlite3.connect(staged)
        target = sqlite3.connect(database, timeout=30)
        try:
            source.backup(target)
        except sqlite3.OperationalError as exc:
            raise TransferError("LabRat is busy writing to its database. Nothing was changed; try again shortly.") from exc
        finally:
            target.close()
            source.close()
    finally:
        staged.unlink(missing_ok=True)
    command.upgrade(alembic_config(), "head")
    _write_config(config_path, settings)
    for suffix in DERIVED_SUFFIXES:
        derived = database.with_name(database.name + suffix)
        if derived.is_dir():
            shutil.rmtree(derived, ignore_errors=True)
        else:
            derived.unlink(missing_ok=True)


def _write_config(path: Path, settings: str) -> None:
    """Replace the configuration file atomically, readable by its owner only.

    Args:
        path: The configuration file.
        settings: Its new contents.
    """
    descriptor, name = tempfile.mkstemp(prefix=".labrat-config-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(settings)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
