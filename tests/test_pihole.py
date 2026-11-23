# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Check the read-only Pi-hole adapter, its Settings test, telemetry, and Networks use."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests

import test_step4c as fixture
from labrat.collector.adapters.pihole import PiholeAdapter
from labrat.collector.contracts import DiscoveryBatch
from labrat.collector import runner
from labrat.discovery.models import DiscoveredDevice, DiscoverySource
from labrat.integration_catalog import DEFINITIONS
from labrat.integrations import test_connection

SETTINGS = {"url": "https://192.0.2.53", "password": "test-only-app-password"}


class FakeResponse:
    """A minimal requests.Response stand-in."""

    def __init__(self, payload, status=200):
        """Hold a scripted reply.

        Args:
            payload: The JSON body, or None for an empty one.
            status: The HTTP status.
        """
        self.payload, self.status_code = payload, status
        self.content = b"" if payload is None else b"x"

    def raise_for_status(self):
        """Fail like requests does for an error status.

        Raises:
            requests.HTTPError: When the status is 400 or more.
        """
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)

    def json(self):
        """Return the scripted body.

        Returns:
            The payload.
        """
        return self.payload


class FakeSession:
    """Answers by (method, path) and records every call."""

    def __init__(self, answers):
        """Script the Pi-hole's answers and record each request.

        Args:
            answers: Replies keyed by (method, path): a JSON body, a
                ``(body, status)`` pair, or an exception to raise.
        """
        self.answers, self.calls = answers, []

    def request(self, method, url, **kwargs):
        """Answer one request from the script.

        Args:
            method: The HTTP method.
            url: The full URL.
            **kwargs: Request options (unused).

        Returns:
            FakeResponse: The scripted reply, or a 404.

        Raises:
            Exception: The scripted exception, when the answer is one.
        """
        path = url.split("192.0.2.53", 1)[1]
        params = kwargs.get("params") or {}
        if params:
            # Keyed by parameter names only, so a token's value is never a key.
            path = f"{path}?{'&'.join(sorted(params))}"
        self.calls.append((method, path))
        self.sent_auth = getattr(self, "sent_auth", False) or "auth" in params
        answer = self.answers.get((method, path))
        if isinstance(answer, Exception):
            raise answer
        if answer is None and (method, path) not in self.answers:
            return FakeResponse({}, 404)
        if isinstance(answer, tuple):
            return FakeResponse(*answer)
        return FakeResponse(answer)


def v6_answers():
    """A v6 Pi-hole that accepts the application password.

    Returns:
        dict: Replies keyed by (method, path).
    """
    return {
        # Recorded from Pi-hole v6.6: a password-protected Pi-hole answers
        # the unauthenticated probe with 401 and a session object.
        ("GET", "/api/auth"): ({"session": {"valid": False, "totp": False, "sid": None, "validity": -1,
                                            "message": "no SID provided"}}, 401),
        ("POST", "/api/auth"): {"session": {"valid": True, "sid": "sid-1"}},
        ("DELETE", "/api/auth"): None,
        ("GET", "/api/stats/summary"): {"queries": {"total": 100, "blocked": 25, "percent_blocked": 25.0},
                                        "clients": {"active": 4}, "gravity": {"domains_being_blocked": 1000}},
        ("GET", "/api/history"): {"history": [{"timestamp": 1, "total": 10, "blocked": 2}]},
        ("GET", "/api/stats/top_domains?count=10"): {"domains": [{"domain": "example.com", "count": 9}]},
        ("GET", "/api/stats/top_domains?blocked=true&count=10"): {"domains": [{"domain": "ads.example", "count": 5}]},
        ("GET", "/api/stats/top_clients?count=10"): {"clients": [{"name": "laptop", "ip": "192.0.2.10", "count": 50},
                                                                {"name": "", "ip": "192.0.2.11", "count": 3}]},
        ("GET", "/api/info/version"): {"version": {"core": {"local": {"version": "v6.0"}}}},
        ("GET", "/api/dns/blocking"): {"blocking": "enabled"},
    }


def v5_answers():
    """A v5 Pi-hole: no /api, a credential-free versions answer, then its summary.

    Returns:
        dict: Replies keyed by (method, path with parameter names).
    """
    return {
        ("GET", "/admin/api.php?versions"): {"core_current": "v5.18", "FTL_current": "v5.25", "web_current": "v5.21"},
        ("GET", "/admin/api.php?auth&summaryRaw"): {"dns_queries_today": 7, "ads_blocked_today": 1, "status": "enabled"},
    }


class AdapterTests(unittest.TestCase):
    """Reads only, and ends its own session."""

    def test_v6_reads_and_signs_out(self):
        """Sign in, read statistics with GETs, then end the session."""
        session = FakeSession(v6_answers())
        adapter = PiholeAdapter(session)
        batch = adapter.poll(SETTINGS)
        self.assertIsInstance(batch, DiscoveryBatch)
        self.assertEqual(batch.devices[0].identifiers["management_ips"], ["192.0.2.53"])
        stats = adapter.last_telemetry["pihole"]["pihole"]
        self.assertEqual((stats["api"], stats["queries"]["blocked"], stats["top_blocked"][0]["name"]), ("v6", 25.0, "ads.example"))
        writes = [call for call in session.calls if call[0] != "GET"]
        self.assertEqual(writes, [("POST", "/api/auth"), ("DELETE", "/api/auth")])
        self.assertNotIn("test-only-app-password", json.dumps(stats))
        # A v6 password never goes into the v5 query string.
        self.assertFalse(any(path.startswith("/admin/api.php") for _, path in session.calls))
        # An unnamed client is named by its address.
        self.assertEqual([row["name"] for row in stats["top_clients"]], ["laptop", "192.0.2.11"])

    def test_a_pihole_without_a_password_needs_no_session(self):
        """An open v6 Pi-hole answers the probe as signed in: read without signing in."""
        answers = v6_answers()
        answers[("GET", "/api/auth")] = {"session": {"valid": True, "sid": None, "validity": -1}}
        session = FakeSession(answers)
        stats = PiholeAdapter(session).snapshot(SETTINGS)
        self.assertEqual(stats["api"], "v6")
        self.assertEqual([call for call in session.calls if call[0] != "GET"], [])

    def test_a_wrong_password_is_refused_not_unreachable(self):
        """v6 answers a wrong password with 401, which Settings reports as refused."""
        answers = v6_answers()
        answers[("POST", "/api/auth")] = ({"session": {"valid": False, "message": "password incorrect"}}, 401)
        ok, message = PiholeAdapter(FakeSession(answers)).test_connection(SETTINGS)
        self.assertFalse(ok)
        self.assertIn("refused", message)
        answers[("POST", "/api/auth")] = ({"error": "server"}, 500)
        ok, message = PiholeAdapter(FakeSession(answers)).test_connection(SETTINGS)
        self.assertIn("could not be reached", message)

    def test_a_non_json_probe_is_not_v6(self):
        """A server answering the probe with something else is read as v5."""
        class NotJson(FakeResponse):
            """A reply whose body is not JSON."""

            def json(self):
                """Fail like requests does on HTML.

                Raises:
                    ValueError: Always.
                """
                raise ValueError("not json")

        session = FakeSession(v5_answers())
        original = session.request

        def request(method, url, **kwargs):
            """Answer the probe with HTML, everything else from the script."""
            if url.endswith("/api/auth"):
                return NotJson({}, 200)
            return original(method, url, **kwargs)

        session.request = request
        self.assertEqual(PiholeAdapter(session).snapshot(SETTINGS)["api"], "v5")

    def test_v5_and_refusals(self):
        """v5 reads its summary with the token; refusals and outages give safe messages."""
        # A v5 Pi-hole has no /api: the probe gets a 404, and its
        # credential-free versions answer identifies it.
        v5 = FakeSession(v5_answers())
        stats = PiholeAdapter(v5).snapshot(SETTINGS)
        self.assertEqual((stats["api"], stats["version"], stats["queries"]["total"]), ("v5", "v5.18", 7.0))
        self.assertTrue(v5.sent_auth)
        refused = v6_answers()
        refused[("POST", "/api/auth")] = {"session": {"valid": False}}
        ok, message = PiholeAdapter(FakeSession(refused)).test_connection(SETTINGS)
        self.assertFalse(ok)
        self.assertIn("refused", message)
        down = FakeSession({("GET", "/api/auth"): requests.ConnectionError("down"), ("GET", "/admin/api.php?versions"): requests.ConnectionError("down")})
        self.assertIn("firewall", PiholeAdapter(down).test_connection(SETTINGS)[1])
        self.assertTrue(PiholeAdapter(FakeSession(v6_answers())).test_connection(SETTINGS)[0])

    def test_an_inconclusive_probe_fails_closed(self):
        """A proxy error, a redirect, or another server's 401 never gets the password."""
        for status, body in ((502, {"error": "bad gateway"}), (302, None), (401, {"error": "unauthorized"})):
            with self.subTest(status=status):
                session = FakeSession({("GET", "/api/auth"): (body, status),
                                       ("GET", "/admin/api.php?versions"): ({"error": "nope"}, status)})
                ok, message = PiholeAdapter(session).test_connection(SETTINGS)
                self.assertFalse(ok)
                self.assertIn("did not answer like a Pi-hole", message)
                self.assertFalse(session.sent_auth)
                self.assertNotIn("POST", [method for method, _ in session.calls])
        # A 200 that is not v5's versions answer is not v5 either.
        session = FakeSession({("GET", "/admin/api.php?versions"): {"hello": "world"}})
        with self.assertRaises(ValueError):
            PiholeAdapter(session).snapshot(SETTINGS)
        self.assertFalse(session.sent_auth)

    def test_a_missing_password_is_explained(self):
        """No password: an open v6 reads; a protected v6 or a v5 says what it needs."""
        bare = {"url": SETTINGS["url"]}
        open_v6 = v6_answers()
        open_v6[("GET", "/api/auth")] = {"session": {"valid": True, "sid": None}}
        self.assertEqual(PiholeAdapter(FakeSession(open_v6)).snapshot(bare)["api"], "v6")
        for answers in (v6_answers(), v5_answers()):
            ok, message = PiholeAdapter(FakeSession(answers)).test_connection(bare)
            self.assertFalse(ok)
            self.assertIn("needs its application password", message)
        self.assertFalse(DEFINITIONS["pihole"]["fields"][1]["required"])

    def test_settings_test_uses_the_adapter(self):
        """The catalog marks Pi-hole's test as adapter-driven."""
        self.assertEqual(DEFINITIONS["pihole"]["test"]["style"], "adapter")
        with patch("labrat.collector.adapters.pihole.PiholeAdapter.test_connection", return_value=(True, "ok")):
            self.assertEqual(test_connection("pihole", SETTINGS), (True, "ok"))
        self.assertFalse(test_connection("pihole", {})[0])


class StoredTests(unittest.TestCase):
    """The runner stores readings as telemetry; Networks shows them."""

    setUp = fixture.Step4CTests.setUp

    def test_runner_stores_telemetry_and_network_shows_it(self):
        """A poll stores the pihole telemetry; the sinkhole card gets the stats."""
        with self.factory.begin() as db:
            source = DiscoverySource(name="pihole:home", kind="pihole", config_name="home")
            unifi = DiscoverySource(name="unifi:home", kind="unifi", config_name="home")
            db.add_all([source, unifi])
            db.flush()
            db.add(DiscoveredDevice(source_id=unifi.id, remote_id="gw", name="Gateway", category="router", details_json=json.dumps({
                "wan": {}, "unifi_network": {"site": {"name": "x"}, "networks": [{"name": "Home", "gateway": "192.0.2.1", "prefix": 24,
                                                                                    "dhcp": {"dns": ["192.0.2.53"]}}],
                                            "zones": [], "policies": [], "clients": [], "devices": [], "wifi": [], "wans": []}})))
        config = {"pihole": {"home": SETTINGS}}
        adapter = PiholeAdapter(FakeSession(v6_answers()))
        with patch("labrat.collector.runner.effective_sources", return_value={"pihole": {"home": SETTINGS}, "unifi": {}, "proxmox": {}}):
            runner.poll_once(self.factory, config, {"pihole": adapter})
        with self.factory() as db:
            host = db.query(DiscoveredDevice).filter_by(remote_id="pihole").one()
            self.assertEqual(json.loads(host.details_json)["pihole"]["queries"]["total"], 100.0)
        sinkhole = self.client.get("/api/v1/network").json["sources"][0]["sinkholes"][0]
        self.assertEqual(sinkhole["integration"]["stats"]["queries"]["blocked"], 25.0)

    def test_a_pihole_shows_without_any_unifi(self):
        """A Pi-hole-only install still lists its Pi-hole, with its statistics."""
        with self.factory.begin() as db:
            db.add(DiscoverySource(name="pihole:home", kind="pihole", config_name="home"))
        with patch("labrat.collector.runner.effective_sources", return_value={"pihole": {"home": SETTINGS}, "unifi": {}, "proxmox": {}}):
            runner.poll_once(self.factory, {"pihole": {"home": SETTINGS}}, {"pihole": PiholeAdapter(FakeSession(v6_answers()))})
        answer = self.client.get("/api/v1/network").json
        self.assertEqual(answer["sources"], [])
        [pihole] = answer["piholes"]
        self.assertEqual((pihole["ip"], pihole["names"]), ("192.0.2.53", ["home"]))
        self.assertEqual(pihole["integration"]["stats"]["queries"]["total"], 100.0)


if __name__ == "__main__":
    unittest.main()
