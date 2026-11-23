# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Check machine API tokens, their Settings panel, and the MNDP report endpoint."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import urllib.error

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from labrat import api_tokens
from labrat.collector import mndp_push
from labrat.collector.adapters.mndp import decode_packet
from labrat.discovery.models import DiscoveredDevice, DiscoveredPort, DiscoverySource

ANNOUNCEMENT = {
    "identity": "test-router", "board": "hEX S", "version": "7.16", "software_id": "TEST-0001",
    "interface": "ether2", "mac": "02:00:00:00:00:01", "ipv4": "192.0.2.1", "uptime_seconds": 7200,
}


class TokenTestCase(unittest.TestCase):
    """A migrated database, an administrator, and a signed-in client."""

    def setUp(self) -> None:
        """Migrate, create the administrator, and sign in."""
        temporary = TemporaryDirectory(prefix="labrat-token-test-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = root / "config.toml"
        config.write_text(
            "[database]\n"
            f"path = {json.dumps(str(root / 'db.sqlite3'))}\n"
            "[auth]\n"
            f"secret_key = {json.dumps('test-' + 't' * 64)}\n",
            encoding="utf-8",
        )
        environment = patch.dict(os.environ, {"LABRAT_CONFIG": str(config)})
        environment.start()
        self.addCleanup(environment.stop)
        command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
        from labrat.app import create_app

        self.app = create_app()
        self.addCleanup(self.app.extensions["labrat_engine"].dispose)
        self.sessions = self.app.extensions["labrat_session_factory"]
        self.client = self.app.test_client()
        self.client.get("/setup")
        with self.client.session_transaction() as state:
            token = state["csrf_token"]
        self.client.post("/setup", data={
            "_csrf_token": token,
            "setup_code": self.app.extensions["labrat_setup_code_path"].read_text().strip(),
            "username": "admin", "password": "safe-test-password-2026",
            "confirm_password": "safe-test-password-2026",
        })
        with self.client.session_transaction() as state:
            self.csrf = state["csrf_token"]

    def make_token(self, name: str = "listener", expiry: str = "never") -> str:
        """Create a token through Settings and read it off the page.

        Args:
            name: The token's name.
            expiry: An expiry choice.

        Returns:
            str: The full token.
        """
        page = self.client.post("/settings/tokens", data={
            "_csrf_token": self.csrf, "name": name, "scope": "observations:mndp", "expiry": expiry,
        })
        self.assertEqual(page.status_code, 201)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        return page.get_data(as_text=True).split('id="new-token" class="token-reveal-value" type="text" value="', 1)[1].split('"', 1)[0]

    def report(self, token: str | None, body: object) -> object:
        """Post an MNDP report.

        Args:
            token: The bearer token, or None for none.
            body: The JSON body.

        Returns:
            object: The response.
        """
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.post("/api/v1/machine/mndp", json=body, headers=headers)


class SettingsPanelTests(TokenTestCase):
    """Create, list, and revoke tokens in Settings."""

    def test_token_is_shown_once_and_only_its_digest_is_stored(self) -> None:
        token = self.make_token()
        self.assertRegex(token, api_tokens.TOKEN_PATTERN)
        with self.sessions() as db:
            row = db.scalar(select(api_tokens.ApiToken))
            # The secret is urlsafe base64, which may itself contain "_".
            self.assertNotIn(token.split("_", 2)[2], row.secret_digest)
            self.assertEqual(json.loads(row.scopes), ["observations:mndp"])
        listing = self.client.get("/settings?panel=tokens").get_data(as_text=True)
        self.assertIn("listener", listing)
        self.assertNotIn(token, listing)
        self.assertIn("Never used", listing)

    def test_invalid_requests_explain_themselves(self) -> None:
        cases = (
            ({"name": "", "scope": "observations:mndp", "expiry": "never"}, "1 to 80 characters"),
            ({"name": "a", "scope": "write:everything", "expiry": "never"}, "listed permissions"),
            ({"name": "a", "scope": "observations:mndp", "expiry": "forever"}, "how long"),
        )
        for form, message in cases:
            with self.subTest(message=message):
                page = self.client.post("/settings/tokens", data={"_csrf_token": self.csrf, **form})
                self.assertEqual(page.status_code, 400)
                self.assertIn(message, page.get_data(as_text=True))
        self.make_token("dup")
        page = self.client.post("/settings/tokens", data={"_csrf_token": self.csrf, "name": "dup", "scope": "observations:mndp", "expiry": "30"})
        self.assertIn("already has that name", page.get_data(as_text=True))

    def test_revoke_stops_the_token_and_is_audited(self) -> None:
        token = self.make_token()
        self.assertEqual(self.report(token, {"announcements": []}).status_code, 202)
        with self.sessions() as db:
            token_id = db.scalar(select(api_tokens.ApiToken.id))
        response = self.client.post(f"/settings/tokens/{token_id}/revoke", data={"_csrf_token": self.csrf})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.report(token, {"announcements": []}).status_code, 401)
        again = self.client.post(f"/settings/tokens/{token_id}/revoke", data={"_csrf_token": self.csrf})
        self.assertIn("already revoked", again.get_data(as_text=True))
        missing = self.client.post("/settings/tokens/999/revoke", data={"_csrf_token": self.csrf})
        self.assertEqual(missing.status_code, 400)
        from labrat.audit import AuditEvent

        with self.sessions() as db:
            actions = set(db.scalars(select(AuditEvent.action)))
        self.assertTrue({"api_token.created", "api_token.revoked"} <= actions)

    def test_panel_and_writes_need_an_administrator_and_csrf(self) -> None:
        self.assertEqual(self.client.post("/settings/tokens", data={"name": "x"}).status_code, 400)
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/settings?panel=tokens").status_code, 302)


class ReportTests(TokenTestCase):
    """The MNDP report endpoint."""

    def test_a_report_becomes_devices_and_ports_in_the_tokens_own_source(self) -> None:
        token = self.make_token()
        response = self.report(token, {"announcements": [ANNOUNCEMENT, {**ANNOUNCEMENT, "interface": "ether3", "mac": "02:00:00:00:00:02"}], "interval_seconds": 60})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json, {"accepted": 2, "devices": 1, "ports": 2})
        with self.sessions() as db:
            source = db.scalar(select(DiscoverySource).where(DiscoverySource.kind == "mndp_push"))
            self.assertEqual((source.name, source.poll_interval_seconds), ("mndp:listener", 60))
            self.assertIsNotNone(source.last_success_at)
            device = db.scalar(select(DiscoveredDevice).where(DiscoveredDevice.source_id == source.id))
            self.assertEqual((device.name, device.category), ("test-router", "router"))
            ports = db.scalars(select(DiscoveredPort.name).where(DiscoveredPort.discovered_device_id == device.id)).all()
            self.assertEqual(sorted(ports), ["ether2", "ether3"])
            token_row = db.scalar(select(api_tokens.ApiToken))
            self.assertIsNotNone(token_row.last_used_at)
        # A second report reuses the source.
        self.assertEqual(self.report(token, {"announcements": [ANNOUNCEMENT]}).status_code, 202)
        with self.sessions() as db:
            self.assertEqual(len(db.scalars(select(DiscoverySource).where(DiscoverySource.kind == "mndp_push")).all()), 1)

    def test_bad_tokens_are_refused_alike(self) -> None:
        self.make_token()
        missing = self.report(None, {"announcements": []})
        self.assertEqual((missing.status_code, missing.json["error"]), (401, "token_required"))
        self.assertIn("Bearer", missing.headers["WWW-Authenticate"])
        for bad in ("nonsense", "labrat_000000000000_" + "a" * 43):
            with self.subTest(token=bad):
                self.assertEqual(self.report(bad, {"announcements": []}).json["error"], "invalid_token")

    def test_expired_and_unscoped_tokens(self) -> None:
        token = self.make_token(expiry="30")
        with self.sessions.begin() as db:
            row = db.scalar(select(api_tokens.ApiToken))
            row.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        self.assertEqual(self.report(token, {"announcements": []}).status_code, 401)
        with self.sessions.begin() as db:
            row = db.scalar(select(api_tokens.ApiToken))
            row.expires_at = None
            row.scopes = "[]"
        forbidden = self.report(token, {"announcements": []})
        self.assertEqual((forbidden.status_code, forbidden.json["error"]), (403, "insufficient_scope"))
        listing = self.client.get("/settings?panel=tokens").get_data(as_text=True)
        self.assertIn("listener", listing)

    def test_malformed_reports_name_the_field(self) -> None:
        token = self.make_token()
        cases = (
            ([], "JSON object"),
            ({"announcements": "x"}, "must be a list"),
            ({"announcements": [ANNOUNCEMENT] * 501}, "at most 500"),
            ({"announcements": [], "interval_seconds": 1}, "interval_seconds"),
            ({"announcements": ["x"]}, "must be an object"),
            ({"announcements": [{"board": "x"}]}, "identity is required"),
            ({"announcements": [{**ANNOUNCEMENT, "board": 5}]}, "board must be text"),
            ({"announcements": [{**ANNOUNCEMENT, "mac": "zz"}]}, "mac must look like"),
            ({"announcements": [{**ANNOUNCEMENT, "ipv4": "999.1.1.1"}]}, "ipv4"),
            ({"announcements": [{**ANNOUNCEMENT, "uptime_seconds": -1}]}, "uptime_seconds"),
        )
        for body, message in cases:
            with self.subTest(message=message):
                response = self.report(token, body)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json["detail"])
        plain = self.client.post("/api/v1/machine/mndp", data="x", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(plain.status_code, 400)

    def test_tokens_never_open_browser_pages_or_the_session_api(self) -> None:
        token = self.make_token()
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code, 401)
        self.assertEqual(anonymous.get("/home", headers={"Authorization": f"Bearer {token}"}).status_code, 302)

    def test_same_named_sources_get_the_token_id(self) -> None:
        with self.sessions.begin() as db:
            db.add(DiscoverySource(name="mndp:listener", kind="mndp", config_name="default", enabled=True))
        token = self.make_token()
        self.assertEqual(self.report(token, {"announcements": [ANNOUNCEMENT]}).status_code, 202)
        with self.sessions() as db:
            names = set(db.scalars(select(DiscoverySource.name)))
        self.assertTrue(any(name.startswith("mndp:listener (") for name in names))


def packet(fields: dict[int, bytes]) -> bytes:
    """Build an MNDP packet from type/value records.

    Args:
        fields: TLV type to value.

    Returns:
        bytes: The packet.
    """
    body = b"".join(kind.to_bytes(2, "big") + len(value).to_bytes(2, "big") + value for kind, value in fields.items())
    return b"\x00\x00\x00\x00" + body


class StandaloneListenerTests(unittest.TestCase):
    """The ``mndp_push`` command."""

    def args(self, **overrides: object) -> argparse.Namespace:
        """Build parsed options.

        Args:
            **overrides: Options to change.

        Returns:
            argparse.Namespace: The options.
        """
        values = {"url": "https://labrat.example.lan", "token_file": None, "listen_seconds": 5,
                  "interval": 60, "ca_file": None, "once": True}
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_heard_packets_are_reported_once(self) -> None:
        heard = [packet({5: b"test-router", 12: b"hEX S", 16: b"ether2"}), packet({7: b"7.16"})]
        self.assertEqual(decode_packet(heard[0])["identity"], "test-router")
        sent = []
        with patch.dict(os.environ, {"LABRAT_API_TOKEN": "labrat_x"}), self.assertLogs(level="INFO"):
            status = mndp_push.run(self.args(), listen=lambda seconds: heard,
                                   post=lambda url, token, items, interval, context: sent.append((url, token, items, interval)) or {"devices": 1})
        self.assertEqual(status, 0)
        self.assertEqual(sent[0][1], "labrat_x")
        self.assertEqual(sent[0][2], [{"identity": "test-router", "board": "hEX S", "interface": "ether2"}])

    def test_failures_and_a_missing_token(self) -> None:
        with patch.dict(os.environ, {"LABRAT_API_TOKEN": ""}), self.assertLogs(level="ERROR"):
            self.assertEqual(mndp_push.run(self.args(), listen=lambda seconds: []), 2)

        def refused(*args: object) -> dict:
            """Fail like a refused report."""
            raise urllib.error.HTTPError("https://labrat.example.lan", 403, "Forbidden", {}, None)

        with patch.dict(os.environ, {"LABRAT_API_TOKEN": "labrat_x"}), self.assertLogs(level="WARNING"):
            self.assertEqual(mndp_push.run(self.args(), listen=lambda seconds: [], post=refused), 1)

        def unreachable(*args: object) -> dict:
            """Fail like an unreachable LabRat."""
            raise urllib.error.URLError("down")

        with patch.dict(os.environ, {"LABRAT_API_TOKEN": "labrat_x"}), self.assertLogs(level="WARNING"):
            self.assertEqual(mndp_push.run(self.args(url="http://labrat.example.lan"), listen=lambda seconds: [], post=unreachable), 1)

    def test_token_file_and_option_checks(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "token"
            path.write_text("labrat_file\n", encoding="utf-8")
            self.assertEqual(mndp_push.read_token(str(path)), "labrat_file")
        for argv in (["--url", "ftp://x"], ["--url", "https://x", "--listen-seconds", "1"],
                     ["--url", "https://x", "--interval", "10"]):
            with self.subTest(argv=argv), patch("sys.stderr"), self.assertRaises(SystemExit):
                mndp_push.main(argv)

    def test_the_documented_command_runs(self) -> None:
        """Only --url (and --once): the defaults agree, and it listens and reports."""
        seen = {}

        def fake_run(args):
            """Capture the parsed options instead of listening.

            Args:
                args: Parsed options.

            Returns:
                int: Success.
            """
            seen["args"] = args
            return 0

        with patch.object(mndp_push, "run", fake_run):
            self.assertEqual(mndp_push.main(["--url", "https://labrat.example.lan", "--once"]), 0)
            self.assertEqual(mndp_push.main(["--url", "https://labrat.example.lan"]), 0)
            self.assertEqual((seen["args"].listen_seconds, seen["args"].interval), (65, 65))
            mndp_push.main(["--url", "https://labrat.example.lan", "--listen-seconds", "10"])
            self.assertEqual(seen["args"].interval, 60)

    def test_report_posts_json_with_the_token(self) -> None:
        captured = {}

        class Answer:
            """A fake HTTP response."""

            def __enter__(self):
                """Open it."""
                return self

            def __exit__(self, *exc: object) -> None:
                """Close it."""

            def read(self) -> bytes:
                """Return the body."""
                return b'{"devices": 1}'

        def fake_open(request, timeout, context):
            """Record the request."""
            captured["request"] = request
            return Answer()

        with patch.object(mndp_push.urllib.request, "urlopen", fake_open):
            answer = mndp_push.report("https://labrat.example.lan/", "labrat_x", [ANNOUNCEMENT], 60, None)
        self.assertEqual(answer, {"devices": 1})
        request = captured["request"]
        self.assertEqual(request.full_url, "https://labrat.example.lan/api/v1/machine/mndp")
        self.assertEqual(request.get_header("Authorization"), "Bearer labrat_x")
        self.assertEqual(json.loads(request.data)["interval_seconds"], 60)


if __name__ == "__main__":
    unittest.main()
