# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Keep one web app and one collector per database, and track its schema.

LabRat runs as one instance: SQLite allows one writer, and sign-in limits
and collector scheduling live in memory. Each role (``web`` and
``collector``) holds a lease row in ``instance_leases`` that its process
renews every :data:`RENEW_SECONDS`. A second process of the same role that
finds a live lease held by someone else has a conflict, and the caller
decides what to do: wait, take the lease over (the old holder steps down
at its next renewal), refuse to start, or (web only) fork a private copy of
the database and run against that.

A holder is ``<hostname>:<pid>``, with the Gunicorn master's pid for the
web app, so all of one server's workers share a lease, and a container
restarted in the same pod takes its own lease straight back. A lease not
renewed for :data:`STALE_SECONDS` is stale: its holder is presumed dead,
and anyone may take it.

The module also reads the schema's state against Alembic's scripts and
runs the migration under a file lock, for the web app's startup page.

Used by: collector.__main__, collector.runner, startup.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from typing import Any

from sqlalchemy import Integer, String, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from labrat.database import Base, database_path

ROLES = ("web", "collector")
"""The roles that each hold one lease."""

RENEW_SECONDS = 20
"""How often a holder renews its lease."""

STALE_SECONDS = 90
"""How long an unrenewed lease stays live before anyone may take it."""


class InstanceLease(Base):
    """Record which process currently owns a role on this database.

    Times are whole Unix seconds, so comparing them needs no timestamp
    parsing. ``released_by`` names the holder that took the lease over, so
    the old holder can say who replaced it.
    """

    __tablename__ = "instance_leases"

    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    holder: Mapped[str] = mapped_column(String(300))
    hostname: Mapped[str] = mapped_column(String(255))
    pid: Mapped[int] = mapped_column(Integer)
    version: Mapped[str] = mapped_column(String(64), server_default="")
    acquired_at: Mapped[int] = mapped_column(Integer)
    renewed_at: Mapped[int] = mapped_column(Integer)
    released_by: Mapped[str] = mapped_column(String(300), server_default="")


@dataclass(frozen=True)
class Lease:
    """A lease row as plain values, safe to keep after its connection closes.

    Attributes:
        role: ``web`` or ``collector``.
        holder: The holder's ``<hostname>:<pid>``.
        hostname: The holder's host name.
        pid: The holder's process id.
        version: The holder's LabRat version.
        acquired_at: Unix seconds when it took the lease.
        renewed_at: Unix seconds of its last renewal.
        released_by: The holder that took this lease over, if one did.
    """

    role: str
    holder: str
    hostname: str
    pid: int
    version: str
    acquired_at: int
    renewed_at: int
    released_by: str

    def is_stale(self, now: int | None = None) -> bool:
        """Say whether the holder has stopped renewing.

        Args:
            now: Unix seconds to compare with; defaults to the current time.

        Returns:
            bool: True once the lease is older than :data:`STALE_SECONDS`.
        """
        return (now if now is not None else int(time.time())) - self.renewed_at > STALE_SECONDS

    def describe(self) -> dict[str, str]:
        """Describe the holder for a person deciding what to do.

        Returns:
            dict[str, str]: Labelled host, process, version, and times.
        """
        def when(seconds: int) -> str:
            """Format Unix seconds as UTC."""
            return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        return {
            "Host": self.hostname,
            "Process": str(self.pid),
            "Version": self.version or "unknown",
            "Holding since": when(self.acquired_at),
            "Last renewed": when(self.renewed_at),
        }


def holder_id(pid: int | None = None) -> str:
    """Name a lease holder by host and process.

    Args:
        pid: The process that owns the lease; defaults to this one.

    Returns:
        str: ``<hostname>:<pid>``.
    """
    return f"{socket.gethostname()}:{pid if pid is not None else os.getpid()}"


def has_lease_table(engine: Engine) -> bool:
    """Say whether the database has been migrated far enough to hold leases.

    Args:
        engine: The database engine.

    Returns:
        bool: True when ``instance_leases`` exists.
    """
    return inspect(engine).has_table(InstanceLease.__tablename__)


def _row(connection: Any, role: str) -> Lease | None:
    """Read one role's lease.

    Args:
        connection: An open SQLAlchemy connection.
        role: The role to read.

    Returns:
        Lease | None: The lease, or None when nobody has held it.
    """
    row = connection.execute(
        text(
            "SELECT role, holder, hostname, pid, version, acquired_at, renewed_at, released_by "
            "FROM instance_leases WHERE role = :role"
        ),
        {"role": role},
    ).first()
    return Lease(*row) if row is not None else None


def read_lease(engine: Engine, role: str) -> Lease | None:
    """Read who holds a role, live or stale.

    Args:
        engine: The database engine.
        role: ``web`` or ``collector``.

    Returns:
        Lease | None: The current row, or None when there is none.
    """
    with engine.connect() as connection:
        return _row(connection, role)


def acquire(engine: Engine, role: str, holder: str, version: str = "") -> Lease | None:
    """Take a role's lease if it is free, stale, or already ours.

    One statement does the check and the write, so two processes racing
    for a free lease cannot both win.

    Args:
        engine: The database engine.
        role: ``web`` or ``collector``.
        holder: This process's holder id.
        version: This process's LabRat version, shown to others.

    Returns:
        Lease | None: None when we now hold the lease; otherwise the live
            lease of the process that holds it.
    """
    now = int(time.time())
    hostname, _, pid = holder.rpartition(":")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO instance_leases "
                "(role, holder, hostname, pid, version, acquired_at, renewed_at, released_by) "
                "VALUES (:role, :holder, :hostname, :pid, :version, :now, :now, '') "
                "ON CONFLICT(role) DO UPDATE SET "
                "acquired_at = CASE WHEN instance_leases.holder = excluded.holder "
                "THEN instance_leases.acquired_at ELSE excluded.acquired_at END, "
                "holder = excluded.holder, hostname = excluded.hostname, pid = excluded.pid, "
                "version = excluded.version, renewed_at = excluded.renewed_at, released_by = '' "
                "WHERE instance_leases.holder = excluded.holder "
                "OR instance_leases.renewed_at < :stale_before"
            ),
            {
                "role": role, "holder": holder, "hostname": hostname,
                "pid": int(pid) if pid.isdigit() else 0, "version": version,
                "now": now, "stale_before": now - STALE_SECONDS,
            },
        )
        current = _row(connection, role)
    return None if current is not None and current.holder == holder else current


def renew(engine: Engine, role: str, holder: str) -> Lease | None:
    """Renew our lease, and notice when someone has taken it over.

    Args:
        engine: The database engine.
        role: ``web`` or ``collector``.
        holder: This process's holder id.

    Returns:
        Lease | None: None while we still hold the lease; otherwise the row
            as it stands now (its ``holder`` or ``released_by`` names who
            replaced us).
    """
    with engine.begin() as connection:
        updated = connection.execute(
            text("UPDATE instance_leases SET renewed_at = :now WHERE role = :role AND holder = :holder"),
            {"now": int(time.time()), "role": role, "holder": holder},
        ).rowcount
        if updated:
            return None
        current = _row(connection, role)
    return current or Lease(role, "", "", 0, "", 0, 0, "")


def take_over(
    engine: Engine, holder: str, version: str = "", role: str = "web",
    release: tuple[str, ...] = ("collector",),
) -> None:
    """Take a lease from a live holder, and free its companion roles.

    The old holder finds the lease gone at its next renewal and steps
    down. Each role in ``release`` is handed back too (marked stale and
    released by us), so the replaced instance's collector stops at its
    next renewal and a waiting collector picks the lease up.

    Args:
        engine: The database engine.
        holder: This process's holder id.
        version: This process's LabRat version.
        role: The role to take, ``web`` by default.
        release: Other roles to free for this instance's own processes.
    """
    now = int(time.time())
    hostname, _, pid = holder.rpartition(":")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO instance_leases "
                "(role, holder, hostname, pid, version, acquired_at, renewed_at, released_by) "
                "VALUES (:role, :holder, :hostname, :pid, :version, :now, :now, '') "
                "ON CONFLICT(role) DO UPDATE SET holder = excluded.holder, "
                "hostname = excluded.hostname, pid = excluded.pid, version = excluded.version, "
                "acquired_at = excluded.acquired_at, renewed_at = excluded.renewed_at, released_by = ''"
            ),
            {"role": role, "holder": holder, "hostname": hostname,
             "pid": int(pid) if pid.isdigit() else 0, "version": version, "now": now},
        )
        for other in release:
            connection.execute(
                text(
                    "UPDATE instance_leases SET holder = :marker, renewed_at = 0, released_by = :holder "
                    "WHERE role = :role AND holder != :holder"
                ),
                {"marker": f"released-by:{holder}", "holder": holder, "role": other},
            )


def release(engine: Engine, role: str, holder: str) -> None:
    """Give up our lease on a clean shutdown, so a successor need not wait.

    Args:
        engine: The database engine.
        role: ``web`` or ``collector``.
        holder: This process's holder id.
    """
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM instance_leases WHERE role = :role AND holder = :holder"),
            {"role": role, "holder": holder},
        )


class LeaseLost(Exception):
    """Another process took this one's lease over."""


def fence_writes(sessions: sessionmaker, role: str, holder: str) -> None:
    """Refuse every commit from these sessions once the lease is lost.

    Before each commit, the transaction renews the lease with an UPDATE
    that matches only this holder. SQLite has one writer, so that check and
    the transaction's own writes commit together: a takeover either
    happened before (the UPDATE matches nothing, the commit raises and
    rolls back) or waits until after. Reads never commit, so they are
    unaffected.

    Args:
        sessions: The session factory whose commits to fence.
        role: The lease to require.
        holder: This process's holder id.
    """
    @event.listens_for(sessions, "before_commit")
    def check_lease(session: Session) -> None:
        """Renew the lease in this transaction, or refuse the commit.

        Args:
            session: The committing session.

        Raises:
            LeaseLost: Another process holds the lease now.
        """
        renewed = session.execute(
            text("UPDATE instance_leases SET renewed_at = :now WHERE role = :role AND holder = :holder"),
            {"now": int(time.time()), "role": role, "holder": holder},
        ).rowcount
        if not renewed:
            raise LeaseLost(role)


def fork_destination(source: Path, holder: str) -> Path:
    """Claim a new, empty file for a fork, unique to this holder and moment.

    The name carries the time and a short digest of the holder, and the
    file is created with ``O_EXCL``, so two servers forking in the same
    second (or one forking twice) never share, and so never overwrite, a
    copy.

    Args:
        source: The database being forked.
        holder: The forking server's holder id.

    Returns:
        Path: The claimed file, created empty with mode 600.

    Raises:
        FileExistsError: Every candidate name is already taken.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = hashlib.sha256(holder.encode("utf-8")).hexdigest()[:8]
    for attempt in range(1000):
        suffix = f"-{attempt}" if attempt else ""
        target = source.with_name(f"{source.stem}-fork-{stamp}-{tag}{suffix}{source.suffix}")
        try:
            os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            continue
        return target
    raise FileExistsError(f"No free fork name beside {source.name}")


def fork_database(config: dict[str, Any], holder: str) -> Path:
    """Copy the database beside the original, for an instance that forks.

    SQLite's backup API copies a consistent snapshot while the other
    instance keeps writing, into a destination no other fork can claim
    (:func:`fork_destination`). The copy's own leases are cleared, so the
    fork starts as the only holder of its database.

    Args:
        config: Parsed local configuration.
        holder: This process's holder id, folded into the copy's name.

    Returns:
        Path: The new database file.
    """
    source = database_path(config)
    target = fork_destination(source, holder)
    with sqlite3.connect(source) as original, sqlite3.connect(target) as copy:
        original.backup(copy)
        copy.execute("DELETE FROM instance_leases")
    original.close()
    copy.close()
    os.chmod(target, 0o600)
    return target


# --- Schema state and migration ------------------------------------------


def alembic_ini() -> Path | None:
    """Find ``alembic.ini``: ``LABRAT_ALEMBIC_INI``, the working directory, or the source tree.

    Returns:
        Path | None: The file, or None when this install has no migrations.
    """
    candidates = [
        os.environ.get("LABRAT_ALEMBIC_INI", ""),
        str(Path.cwd() / "alembic.ini"),
        str(Path(__file__).resolve().parents[2] / "alembic.ini"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


@dataclass(frozen=True)
class SchemaState:
    """Where the database's schema stands against this code's migrations.

    Attributes:
        current: The database's revision, or None when it has none.
        head: The newest revision this code ships.
    """

    current: str | None
    head: str | None

    @property
    def behind(self) -> bool:
        """Say whether the database needs ``alembic upgrade head``.

        Returns:
            bool: True when its revision is not the code's newest.
        """
        return self.head is not None and self.current != self.head


def schema_state(engine: Engine) -> SchemaState | None:
    """Read the database's revision and the code's newest one.

    Args:
        engine: The database engine.

    Returns:
        SchemaState | None: Both revisions, or None when this install has
            no Alembic scripts to compare with.
    """
    ini = alembic_ini()
    if ini is None:
        return None
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(Config(str(ini))).get_current_head()
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    return SchemaState(current, head)


@contextmanager
def migration_lock(config: dict[str, Any]) -> Iterator[None]:
    """Hold an exclusive lock beside the database while one process migrates.

    Every worker of a server may try to migrate at once; the first takes
    the lock and the rest wait for it, then find nothing left to do.

    Args:
        config: Parsed local configuration.

    Yields:
        None: While the lock is held.
    """
    path = database_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(f"{path}.migrate.lock", "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run_migration() -> int:
    """Run ``alembic upgrade head`` in a child process.

    A child keeps Alembic's logging setup out of the server's own logging,
    and its output goes to the server's log.

    Returns:
        int: Alembic's exit status (0 on success, 2 with no alembic.ini).
    """
    ini = alembic_ini()
    if ini is None:
        return 2
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ini), "upgrade", "head"],
        cwd=ini.parent, check=False,
    ).returncode
