# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Check the single-instance leases, the schema check, and database forks."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config

from labrat import instance
from labrat.config import load_local_config
from labrat.database import make_engine

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def temporary_config(test: unittest.TestCase, migrate: bool = True) -> Path:
    """Point LABRAT_CONFIG at a fresh database for one test.

    Args:
        test: The running test, which cleans everything up.
        migrate: Whether to bring the new database to head.

    Returns:
        Path: The temporary directory holding the config and database.
    """
    temporary = TemporaryDirectory(prefix="labrat-lease-test-")
    test.addCleanup(temporary.cleanup)
    root = Path(temporary.name)
    (root / "config.toml").write_text(
        "[database]\n"
        f"path = {json.dumps(str(root / 'labrat.sqlite3'))}\n"
        "[auth]\n"
        f"secret_key = {json.dumps('test-' + 'l' * 64)}\n",
        encoding="utf-8",
    )
    environment = patch.dict(os.environ, {"LABRAT_CONFIG": str(root / "config.toml")})
    environment.start()
    test.addCleanup(environment.stop)
    if migrate:
        command.upgrade(Config(str(ALEMBIC_INI)), "head")
    return root


class LeaseTests(unittest.TestCase):
    """Take, renew, lose, and release leases on a migrated database."""

    def setUp(self) -> None:
        """Migrate a fresh database and open an engine on it."""
        self.root = temporary_config(self)
        self.engine = make_engine(load_local_config())
        self.addCleanup(self.engine.dispose)

    def age(self, role: str, seconds: int) -> None:
        """Pretend a lease was last renewed some seconds ago.

        Args:
            role: The lease to age.
            seconds: How long ago it was renewed.
        """
        with sqlite3.connect(self.root / "labrat.sqlite3") as connection:
            connection.execute(
                "UPDATE instance_leases SET renewed_at = ? WHERE role = ?",
                (int(time.time()) - seconds, role),
            )

    def test_first_holder_takes_a_free_lease(self) -> None:
        self.assertTrue(instance.has_lease_table(self.engine))
        self.assertIsNone(instance.acquire(self.engine, "web", "host-a:10", "1.0"))
        lease = instance.read_lease(self.engine, "web")
        self.assertEqual((lease.holder, lease.hostname, lease.pid, lease.version), ("host-a:10", "host-a", 10, "1.0"))

    def test_a_live_lease_is_a_conflict_and_a_stale_one_is_free(self) -> None:
        instance.acquire(self.engine, "web", "host-a:10")
        other = instance.acquire(self.engine, "web", "host-b:20")
        self.assertIsNotNone(other)
        self.assertEqual(other.holder, "host-a:10")
        self.assertFalse(other.is_stale())
        self.assertIn("Host", other.describe())
        self.age("web", instance.STALE_SECONDS + 5)
        self.assertIsNone(instance.acquire(self.engine, "web", "host-b:20"))
        self.assertEqual(instance.read_lease(self.engine, "web").holder, "host-b:20")

    def test_holding_again_keeps_the_original_start(self) -> None:
        instance.acquire(self.engine, "collector", "host-a:10")
        first = instance.read_lease(self.engine, "collector").acquired_at
        self.age("collector", 30)
        self.assertIsNone(instance.acquire(self.engine, "collector", "host-a:10"))
        self.assertEqual(instance.read_lease(self.engine, "collector").acquired_at, first)

    def test_renew_notices_a_takeover_and_frees_the_collector(self) -> None:
        instance.acquire(self.engine, "web", "host-a:10")
        instance.acquire(self.engine, "collector", "host-a:11")
        self.assertIsNone(instance.renew(self.engine, "web", "host-a:10"))
        instance.take_over(self.engine, "host-b:20", "2.0")
        replaced = instance.renew(self.engine, "web", "host-a:10")
        self.assertEqual(replaced.holder, "host-b:20")
        # The old collector loses its lease too, and a new one may take it.
        self.assertIsNotNone(instance.renew(self.engine, "collector", "host-a:11"))
        self.assertIsNone(instance.acquire(self.engine, "collector", "host-b:21"))

    def test_renew_without_a_row_reports_it_lost(self) -> None:
        lost = instance.renew(self.engine, "web", "host-a:10")
        self.assertIsNotNone(lost)
        self.assertEqual(lost.holder, "")

    def test_release_frees_only_our_own_lease(self) -> None:
        instance.acquire(self.engine, "web", "host-a:10")
        instance.release(self.engine, "web", "host-b:20")
        self.assertIsNotNone(instance.read_lease(self.engine, "web"))
        instance.release(self.engine, "web", "host-a:10")
        self.assertIsNone(instance.read_lease(self.engine, "web"))

    def test_holder_id_names_host_and_process(self) -> None:
        self.assertTrue(instance.holder_id(42).endswith(":42"))
        self.assertTrue(instance.holder_id().endswith(f":{os.getpid()}"))

    def test_fork_copies_the_database_without_its_leases(self) -> None:
        instance.acquire(self.engine, "web", "host-a:10")
        fork = instance.fork_database(load_local_config(), "host-b:20")
        self.assertTrue(fork.is_file())
        self.assertNotEqual(fork, self.root / "labrat.sqlite3")
        self.assertEqual(oct(fork.stat().st_mode & 0o777), "0o600")
        with sqlite3.connect(fork) as copy:
            self.assertEqual(copy.execute("SELECT count(*) FROM instance_leases").fetchone()[0], 0)
            self.assertGreater(copy.execute("SELECT count(*) FROM alembic_version").fetchone()[0], 0)
        copy.close()

    def test_forks_in_the_same_second_never_share_a_file(self) -> None:
        config = load_local_config()
        frozen = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        with patch.object(instance, "datetime") as clock:
            clock.now.return_value = frozen
            first = instance.fork_database(config, "host-a:10")
            # A write to the first fork must survive the second fork.
            with sqlite3.connect(first) as copy:
                copy.execute("INSERT INTO instance_leases VALUES ('web', 'host-a:10', 'host-a', 10, '', 1, 1, '')")
            copy.close()
            second = instance.fork_database(config, "host-b:20")
            again = instance.fork_database(config, "host-a:10")
        self.assertEqual(len({first, second, again}), 3)
        with sqlite3.connect(first) as copy:
            self.assertEqual(copy.execute("SELECT count(*) FROM instance_leases").fetchone()[0], 1)
        copy.close()
        self.assertTrue(again.name.endswith("-1.sqlite3"))


class SchemaTests(unittest.TestCase):
    """Read the schema's revision and migrate under the lock."""

    def test_new_database_is_behind_then_current_after_migrating(self) -> None:
        temporary_config(self, migrate=False)
        config = load_local_config()
        engine = make_engine(config)
        self.addCleanup(engine.dispose)
        state = instance.schema_state(engine)
        self.assertTrue(state.behind)
        self.assertIsNone(state.current)
        self.assertFalse(instance.has_lease_table(engine))
        with instance.migration_lock(config):
            self.assertEqual(instance.run_migration(), 0)
        state = instance.schema_state(engine)
        self.assertFalse(state.behind)
        self.assertEqual(state.current, state.head)
        self.assertTrue(instance.has_lease_table(engine))

    def test_without_alembic_scripts_there_is_nothing_to_compare(self) -> None:
        temporary_config(self, migrate=False)
        engine = make_engine(load_local_config())
        self.addCleanup(engine.dispose)
        with patch.object(instance, "alembic_ini", return_value=None):
            self.assertIsNone(instance.schema_state(engine))
            self.assertEqual(instance.run_migration(), 2)

    def test_alembic_ini_can_be_named_by_the_environment(self) -> None:
        with patch.dict(os.environ, {"LABRAT_ALEMBIC_INI": str(ALEMBIC_INI)}):
            self.assertEqual(instance.alembic_ini(), ALEMBIC_INI)


if __name__ == "__main__":
    unittest.main()
