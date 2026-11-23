# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Drive the startup holding page through each of its states."""

import os
import sqlite3
import time
import unittest
from unittest.mock import patch

from werkzeug.test import Client

from labrat import instance, startup
from labrat.config import load_local_config
from test_instance_lease import temporary_config

OTHER = "other-server:4242"
"""A holder that is not this test process."""


def hold_as_other(database, seconds_ago: int = 0) -> None:
    """Write a live web lease for another server straight into the database.

    Args:
        database: The SQLite file.
        seconds_ago: How long ago the other server last renewed.
    """
    now = int(time.time()) - seconds_ago
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO instance_leases VALUES ('web', ?, 'other-server', 4242, '0.0.1', ?, ?, '')",
            (OTHER, now, now),
        )
    connection.close()


class GateTestCase(unittest.TestCase):
    """Shared set-up: fast timings and a gate that stops after each test."""

    migrate = True

    def setUp(self) -> None:
        """Shorten the gate's waits and prepare a database."""
        self.root = temporary_config(self, migrate=self.migrate)
        self.database = self.root / "labrat.sqlite3"
        for target, name, value in (
            (startup, "RETRY_SECONDS", 0.1),
            (startup, "SCHEMA_POLL_SECONDS", 0.1),
            (instance, "RENEW_SECONDS", 0.1),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def gate(self) -> startup.StartupGate:
        """Start a gate on the test database.

        Returns:
            startup.StartupGate: A started gate, stopped at cleanup.
        """
        from labrat.app import create_app

        gate = startup.StartupGate(create_app)
        gate.start(server_pid=os.getpid())
        self.addCleanup(gate.stop)
        return gate

    def wait_for(self, gate: startup.StartupGate, state: str) -> None:
        """Wait until the gate reaches a state.

        Args:
            gate: The gate under test.
            state: The state to wait for.
        """
        deadline = time.monotonic() + 30
        while gate.state != state:
            if time.monotonic() > deadline:
                self.fail(f"gate stayed {gate.state!r}, expected {state!r}")
            time.sleep(0.05)


class ReadyTests(GateTestCase):
    """A migrated database with no other server opens straight away."""

    def test_ready_gate_hands_requests_to_the_app(self) -> None:
        gate = self.gate()
        self.wait_for(gate, "ready")
        client = Client(gate)
        self.assertEqual(client.get("/api/v1/health").status_code, 200)
        live = client.get("/api/v1/live")
        self.assertEqual((live.status_code, live.json["startup"]), (200, "ready"))
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(instance.read_lease(make_engine_for(self), "web").holder, instance.holder_id(os.getpid()))

    def test_takeover_elsewhere_makes_it_step_down(self) -> None:
        gate = self.gate()
        self.wait_for(gate, "ready")
        hold_as_other(self.database)
        self.wait_for(gate, "stepped_down")
        page = Client(gate).get("/home")
        self.assertEqual(page.status_code, 503)
        self.assertIn(b"Another server took over", page.data)
        self.assertIn(b"other-server", page.data)

    def test_first_request_starts_the_gate(self) -> None:
        from labrat.app import create_app

        gate = startup.StartupGate(create_app)
        self.addCleanup(gate.stop)
        response = Client(gate).get("/api/v1/health")
        self.assertIn(response.status_code, (200, 503))
        self.wait_for(gate, "ready")


class HoldingPageTests(GateTestCase):
    """What the gate answers before it is ready."""

    migrate = False

    def test_unmigrated_database_waits_with_the_command_to_run(self) -> None:
        gate = self.gate()
        self.wait_for(gate, "waiting")
        client = Client(gate)
        page = client.get("/home")
        self.assertEqual(page.status_code, 503)
        self.assertEqual(page.headers["Retry-After"], "0.1")
        self.assertIn(b"alembic -c alembic.ini upgrade head", page.data)
        self.assertIn(b'http-equiv="refresh"', page.data)
        self.assertIn("default-src 'self'", page.headers["Content-Security-Policy"])
        health = client.get("/api/v1/health")
        self.assertEqual((health.status_code, health.json["status"]), (503, "waiting"))
        self.assertEqual(client.get("/api/v1/live").status_code, 200)
        self.assertEqual(client.get("/static/css/startup.css").status_code, 200)
        self.assertEqual(client.get("/static/../app.py").status_code, 404)
        self.assertEqual(client.get("/static/css/missing.css").status_code, 404)

    def test_migrate_on_start_migrates_then_opens(self) -> None:
        with patch.dict(os.environ, {"LABRAT_MIGRATE_ON_START": "1"}):
            gate = self.gate()
            self.wait_for(gate, "ready")
        self.assertFalse(instance.schema_state(make_engine_for(self)).behind)

    def test_a_failed_migration_shows_an_error_and_retries(self) -> None:
        with patch.dict(os.environ, {"LABRAT_MIGRATE_ON_START": "true"}), \
                patch.object(instance, "run_migration", return_value=1):
            gate = self.gate()
            self.wait_for(gate, "error")
            page = Client(gate).get("/")
            self.assertIn(b"migration failed", page.data)
            self.assertIn(b"Alembic exit status 1", page.data)

    def test_a_bad_config_is_explained_and_recovers_once_fixed(self) -> None:
        config = self.root / "config.toml"
        good = config.read_text(encoding="utf-8")
        config.write_text(good.replace("secret_key", "secret_key_unused"), encoding="utf-8")
        gate = self.gate()
        self.wait_for(gate, "error")
        self.assertIn(b"secret_key", Client(gate).get("/").data)
        config.write_text(good, encoding="utf-8")
        self.wait_for(gate, "waiting")


class ConflictTests(GateTestCase):
    """Another server holds the web lease."""

    def setUp(self) -> None:
        """Hold the lease as another server and make an administrator."""
        super().setUp()
        from labrat.app import create_app

        self.flask = create_app()
        self.addCleanup(self.flask.extensions["labrat_engine"].dispose)
        hold_as_other(self.database)

    def signed_in_client(self, gate: startup.StartupGate) -> tuple[Client, str]:
        """Sign an administrator in through the gate's pass-through sign-in.

        Args:
            gate: The gate in its conflict state.

        Returns:
            tuple[Client, str]: The signed-in client and its CSRF token.
        """
        setup = self.flask.test_client()
        setup.get("/setup")
        with setup.session_transaction() as state:
            token = state["csrf_token"]
        setup.post("/setup", data={
            "_csrf_token": token,
            "setup_code": self.flask.extensions["labrat_setup_code_path"].read_text().strip(),
            "username": "admin", "password": "safe-test-password-2026",
            "confirm_password": "safe-test-password-2026",
        })
        client = Client(gate)
        page = client.get("/signin")
        self.assertEqual(page.status_code, 200)
        token = page.get_data(as_text=True).split('name="_csrf_token" value="', 1)[1].split('"', 1)[0]
        signed = client.post("/signin", data={"_csrf_token": token, "username": "admin", "password": "safe-test-password-2026"})
        self.assertEqual(signed.status_code, 302)
        holding = client.get("/home").get_data(as_text=True)
        self.assertIn("Take over the database from here", holding)
        return client, holding.split('name="_csrf_token" value="', 1)[1].split('"', 1)[0]

    def test_anonymous_visitors_see_the_holder_but_no_choices(self) -> None:
        gate = self.gate()
        self.wait_for(gate, "conflict")
        client = Client(gate)
        page = client.get("/home")
        self.assertIn(b"This database is already in use", page.data)
        self.assertIn(b"other-server", page.data)
        self.assertNotIn(b"Take over the database", page.data)
        self.assertIn(b"sign in", page.data)
        refused = client.post("/startup/decision", data={"decision": "take-over"})
        self.assertEqual(refused.status_code, 403)

    def test_a_stale_lease_frees_itself(self) -> None:
        gate = self.gate()
        self.wait_for(gate, "conflict")
        hold_as_other(self.database, seconds_ago=instance.STALE_SECONDS + 5)
        self.wait_for(gate, "ready")

    def test_take_over_opens_the_app_and_is_audited(self) -> None:
        gate = self.gate()
        self.wait_for(gate, "conflict")
        client, token = self.signed_in_client(gate)
        self.assertEqual(client.post("/startup/decision", data={"_csrf_token": "wrong", "decision": "take-over"}).status_code, 400)
        self.assertEqual(client.post("/startup/decision", data={"_csrf_token": token, "decision": "nonsense"}).status_code, 400)
        chosen = client.post("/startup/decision", data={"_csrf_token": token, "decision": "take-over"})
        self.assertEqual(chosen.status_code, 303)
        self.wait_for(gate, "ready")
        with sqlite3.connect(self.database) as connection:
            actions = [row[0] for row in connection.execute("SELECT action FROM audit_events")]
        connection.close()
        self.assertIn("instance.take-over", actions)

    def test_refuse_stops_the_server(self) -> None:
        gate = self.gate()
        self.wait_for(gate, "conflict")
        client, token = self.signed_in_client(gate)
        with patch.object(startup.threading, "Timer") as timer:
            client.post("/startup/decision", data={"_csrf_token": token, "decision": "refuse"})
            self.wait_for(gate, "refused")
        self.assertEqual(timer.call_args.kwargs["args"][0], os.getpid())
        self.assertIn(b"stood down", Client(gate).get("/").data)

    def test_fork_runs_on_a_copy(self) -> None:
        gate = self.gate()
        self.wait_for(gate, "conflict")
        client, token = self.signed_in_client(gate)
        client.post("/startup/decision", data={"_csrf_token": token, "decision": "fork"})
        self.wait_for(gate, "ready")
        forks = sorted(self.root.glob("labrat-fork-*.sqlite3"))
        self.assertEqual(len(forks), 1)
        marker = startup.fork_marker(load_local_config(), instance.holder_id(os.getpid()))
        self.assertEqual(marker.read_text(encoding="utf-8"), str(forks[0]))
        # The original still belongs to the other server.
        self.assertEqual(instance.read_lease(make_engine_for(self), "web").holder, OTHER)
        startup.clear_fork_marker(instance.holder_id(os.getpid()))
        self.assertFalse(marker.exists())


class HelperTests(GateTestCase):
    """Small helpers around the gate."""

    def test_migrate_on_start_reads_the_environment_and_config(self) -> None:
        with patch.dict(os.environ, {"LABRAT_MIGRATE_ON_START": ""}):
            self.assertFalse(startup.migrate_on_start({}))
            self.assertTrue(startup.migrate_on_start({"server": {"migrate_on_start": True}}))
            self.assertFalse(startup.migrate_on_start({"server": "bad"}))
        with patch.dict(os.environ, {"LABRAT_MIGRATE_ON_START": "yes"}):
            self.assertTrue(startup.migrate_on_start({}))

    def test_errors_never_echo_unexpected_details(self) -> None:
        self.assertEqual(startup.describe_error(RuntimeError("Set [auth] secret_key")), "Set [auth] secret_key")
        self.assertNotIn("secret", startup.describe_error(OSError("/private/secret path")))

    def test_release_web_lease_frees_it_on_exit(self) -> None:
        holder = instance.holder_id(os.getpid())
        engine = make_engine_for(self)
        instance.acquire(engine, "web", holder)
        startup.release_web_lease(holder)
        self.assertIsNone(instance.read_lease(engine, "web"))


def make_engine_for(test: unittest.TestCase):
    """Open an engine on the test's database, disposed at cleanup.

    Args:
        test: The running test.

    Returns:
        Engine: The engine.
    """
    from labrat.database import make_engine

    engine = make_engine(load_local_config())
    test.addCleanup(engine.dispose)
    return engine


if __name__ == "__main__":
    unittest.main()
