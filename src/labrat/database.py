# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Provide SQLite engines and SQLAlchemy sessions for LabRat components.

The API and future collector processes will share an on-disk SQLite database.
Foreign-key enforcement and WAL mode are enabled on database connections.
Alembic alone manages schema changes: this module does not create tables.

Used by: api.common, api_tokens, app, audit, collector.__main__,
collector.access_collection, collector.client_links, discovery.models,
discovery_settings, instance, integrations, metadata, models,
pages.settings_backup, pages.status, placement, startup.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, URL
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    """Provide shared SQLAlchemy metadata for all persistent LabRat models."""


def as_utc(value: datetime | None) -> datetime | None:
    """Mark a timestamp read back from SQLite as UTC.

    LabRat stores every timestamp in UTC, but SQLite returns them without a
    zone; comparing or formatting a naive value would treat it as local
    time. Aware values pass through unchanged.

    Args:
        value: A stored timestamp, or None.

    Returns:
        datetime | None: The same instant, timezone-aware, or None.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def database_path(config: dict[str, Any]) -> Path:
    """Find the local SQLite database using the optional configuration section.

    Args:
        config: Parsed TOML dictionary, optionally containing
            ``[database] path = "data/labrat.sqlite3"``.

    Returns:
        Path: An absolute path resolved relative to the working directory.

    Raises:
        ValueError: The configured database path is empty or not a string.
        TypeError: The database configuration section is not a dictionary.
    """
    settings = config.get("database", {})
    if not isinstance(settings, dict):
        raise TypeError("[database] must be a TOML table")
    filename = settings.get("path", "data/labrat.sqlite3")
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("[database].path must be a nonempty string")
    return Path(filename).expanduser().resolve()


def make_engine(config: dict[str, Any]) -> Engine:
    """Build a SQLite engine for a local LabRat database.

    Parent directories are created if missing, but **tables are not**: run
    ``alembic upgrade head`` before using database-backed features.

    Args:
        config: Parsed local configuration dictionary.

    Returns:
        Engine: An engine with foreign keys, WAL, and a busy timeout enabled.

    Raises:
        OSError: The database parent directory cannot be created.
    """
    path = database_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        URL.create("sqlite+pysqlite", database=str(path)),
        connect_args={"timeout": 5.0},
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: Any, connection_record: Any) -> None:
        """Apply required SQLite PRAGMAs on each new DB-API connection.

        Args:
            dbapi_connection: The raw SQLite connection.
            connection_record: SQLAlchemy pool bookkeeping (unused).
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    """Create a factory for independent, short-lived SQLAlchemy sessions.

    Args:
        engine: A configured LabRat database engine.

    Returns:
        sessionmaker: A factory whose sessions retain attributes after commit.
    """
    return sessionmaker(bind=engine, expire_on_commit=False)
