# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Check that the collector waits for the schema and keeps one collector per database."""

import time
import unittest
from unittest.mock import patch

from labrat import instance
from labrat.collector import __main__ as collector
from labrat.config import load_local_config
from labrat.audit import AuditEvent
from labrat.collector import runner
from labrat.collector.adapters.mndp import batch_from_announcements
from labrat.database import make_engine, make_session_factory
from labrat.discovery.models import DiscoveredDevice, DiscoverySource
from sqlalchemy import select
from test_instance_lease import temporary_config


class CollectorLeaseTests(unittest.TestCase):
    """The collector's lease policies on a migrated database."""

    def setUp(self) -> None:
        """Migrate a database, shorten renewals, and open an engine."""
        temporary_config(self)
        self.engine = make_engine(load_local_config())
        self.addCleanup(self.engine.dispose)
        patcher = patch.object(instance, "RENEW_SECONDS", 0.05)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_free_lease_is_taken_renewed_and_released(self) -> None:
        lease = collector.CollectorLease(self.engine, "wait")
        self.assertTrue(lease.acquire())
        self.assertEqual(instance.read_lease(self.engine, "collector").holder, lease.holder)
        lease.check()
        lease.release()
        self.assertIsNone(instance.read_lease(self.engine, "collector"))

    def test_refuse_gives_up_beside_another_collector(self) -> None:
        instance.acquire(self.engine, "collector", "other:1")
        with self.assertLogs(level="ERROR"):
            self.assertFalse(collector.CollectorLease(self.engine, "refuse").acquire())

    def test_take_over_replaces_the_other_collector(self) -> None:
        instance.acquire(self.engine, "collector", "other:1")
        lease = collector.CollectorLease(self.engine, "take-over")
        self.addCleanup(lease.release)
        with self.assertLogs(level="WARNING"):
            self.assertTrue(lease.acquire())
        self.assertIsNotNone(instance.renew(self.engine, "collector", "other:1"))

    def test_a_one_off_cycle_runs_beside_the_holder(self) -> None:
        instance.acquire(self.engine, "collector", "other:1")
        lease = collector.CollectorLease(self.engine, collector.ONCE_POLICY)
        with self.assertLogs(level="WARNING"):
            self.assertTrue(lease.acquire())
        self.assertTrue(lease.shared)
        lease.check()
        lease.release()
        # The running collector keeps its lease.
        self.assertEqual(instance.read_lease(self.engine, "collector").holder, "other:1")

    def test_wait_takes_the_lease_once_the_other_goes_stale(self) -> None:
        instance.acquire(self.engine, "collector", "other:1")
        lease = collector.CollectorLease(self.engine, "wait")
        self.addCleanup(lease.release)
        with patch.object(instance, "STALE_SECONDS", -1), self.assertLogs(level="WARNING"):
            # Stale at once: the first retry wins after one logged wait.
            with patch.object(instance, "acquire", side_effect=[instance.read_lease(self.engine, "collector"), None]):
                self.assertTrue(lease.acquire())

    def test_losing_the_lease_stops_the_loop(self) -> None:
        lease = collector.CollectorLease(self.engine, "wait")
        lease.acquire()
        instance.take_over(self.engine, "other:2", role="collector", release=())
        deadline = time.monotonic() + 5
        while not lease.lost.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        with self.assertRaises(collector.LeaseLost):
            lease.check()
        lease.release()
        self.assertEqual(instance.read_lease(self.engine, "collector").holder, "other:2")


class WriteFenceTests(unittest.TestCase):
    """Commits stop the moment another collector holds the lease."""

    def setUp(self) -> None:
        """Migrate a database and fence a session factory as holder A."""
        temporary_config(self)
        self.engine = make_engine(load_local_config())
        self.addCleanup(self.engine.dispose)
        self.sessions = make_session_factory(self.engine)
        instance.acquire(self.engine, "collector", "host-a:1")
        instance.fence_writes(self.sessions, "collector", "host-a:1")

    def sources(self) -> list:
        """Read every discovery source name, unfenced.

        Returns:
            list: The names.
        """
        with make_session_factory(self.engine)() as db:
            return list(db.scalars(select(DiscoverySource.name)))

    def test_commits_pass_while_held_and_roll_back_after_a_takeover(self) -> None:
        with self.sessions.begin() as db:
            db.add(DiscoverySource(name="mndp:before", kind="mndp", config_name="before"))
        # Reads never commit, so a lost lease does not stop them.
        instance.take_over(self.engine, "host-b:2", role="collector", release=())
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(DiscoverySource.name)), "mndp:before")
        with self.assertRaises(instance.LeaseLost):
            with self.sessions.begin() as db:
                db.add(DiscoverySource(name="mndp:after", kind="mndp", config_name="after"))
        self.assertEqual(self.sources(), ["mndp:before"])

    def test_a_poll_in_flight_stops_instead_of_recording(self) -> None:
        with self.sessions.begin() as db:
            db.add(DiscoverySource(name="mndp:default", kind="mndp", config_name="default", enabled=True))

        class Adapter:
            """An adapter whose poll outlives the lease."""

            def poll(self, settings):
                """Lose the lease mid-poll, then report one device.

                Args:
                    settings: Unused.

                Returns:
                    DiscoveryBatch: One device.
                """
                instance.take_over(engine, "host-b:2", role="collector", release=())
                return batch_from_announcements([{"identity": "late"}])

        engine = self.engine
        with patch.object(runner, "effective_sources", return_value={"mndp": {"default": {}}}):
            with self.assertRaises(instance.LeaseLost):
                runner.poll_once(self.sessions, {}, {"mndp": Adapter()})
        with make_session_factory(self.engine)() as db:
            self.assertIsNone(db.scalar(select(DiscoveredDevice.id)))
            self.assertIsNone(db.scalar(select(AuditEvent.id).where(AuditEvent.action == "discovery.collection.failed")))


class SchemaWaitTests(unittest.TestCase):
    """The collector waits while the web app migrates."""

    def test_waits_until_the_schema_is_current(self) -> None:
        temporary_config(self, migrate=False)
        engine = make_engine(load_local_config())
        self.addCleanup(engine.dispose)
        behind = instance.SchemaState("0001", "0002")
        current = instance.SchemaState("0002", "0002")
        with patch.object(instance, "schema_state", side_effect=[behind, behind, current]) as state, \
                patch.object(collector, "SCHEMA_WAIT_SECONDS", 0), self.assertLogs(level="INFO"):
            collector.wait_for_schema(engine)
        self.assertEqual(state.call_count, 3)

    def test_an_install_without_migrations_does_not_wait(self) -> None:
        temporary_config(self, migrate=False)
        engine = make_engine(load_local_config())
        self.addCleanup(engine.dispose)
        with patch.object(instance, "schema_state", return_value=None):
            collector.wait_for_schema(engine)


if __name__ == "__main__":
    unittest.main()
