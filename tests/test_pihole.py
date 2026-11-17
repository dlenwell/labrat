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
            raise requests.HTTPError(str(self.status_code))

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
            answers: Replies keyed by (method, path); an exception is raised.
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
        self.calls.append((method, path))
        answer = self.answers.get((method, path))
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return FakeResponse({}, 404)
        return FakeResponse(answer)


def v6_answers():
    """A v6 Pi-hole that accepts the application password.

    Returns:
        dict: Replies keyed by (method, path).
    """
    return {
        ("GET", "/api/auth"): {"session": {"valid": False, "sid": None}},
        ("POST", "/api/auth"): {"session": {"valid": True, "sid": "sid-1"}},
        ("DELETE", "/api/auth"): None,
        ("GET", "/api/stats/summary"): {"queries": {"total": 100, "blocked": 25, "percent_blocked": 25.0},
                                        "clients": {"active": 4}, "gravity": {"domains_being_blocked": 1000}},
        ("GET", "/api/history"): {"history": [{"timestamp": 1, "total": 10, "blocked": 2}]},
        ("GET", "/api/stats/top_domains?count=10"): {"domains": [{"domain": "example.com", "count": 9}]},
        ("GET", "/api/stats/top_domains?blocked=true&count=10"): {"domains": [{"domain": "ads.example", "count": 5}]},
        ("GET", "/api/stats/top_clients?count=10"): {"clients": [{"name": "laptop", "ip": "192.0.2.10", "count": 50}]},
        ("GET", "/api/info/version"): {"version": {"core": {"local": {"version": "v6.0"}}}},
        ("GET", "/api/dns/blocking"): {"blocking": "enabled"},
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

    def test_v5_and_refusals(self):
        """v5 reads its summary with the token; refusals and outages give safe messages."""
        v5 = FakeSession({("GET", "/api/auth"): requests.HTTPError("404"),
                          ("GET", "/admin/api.php"): {"dns_queries_today": 7, "ads_blocked_today": 1, "status": "enabled"}})
        self.assertEqual(PiholeAdapter(v5).snapshot(SETTINGS)["queries"]["total"], 7.0)
        refused = v6_answers()
        refused[("POST", "/api/auth")] = {"session": {"valid": False}}
        ok, message = PiholeAdapter(FakeSession(refused)).test_connection(SETTINGS)
        self.assertFalse(ok)
        self.assertIn("refused", message)
        down = FakeSession({("GET", "/api/auth"): requests.ConnectionError("down"), ("GET", "/admin/api.php"): requests.ConnectionError("down")})
        self.assertIn("firewall", PiholeAdapter(down).test_connection(SETTINGS)[1])
        self.assertTrue(PiholeAdapter(FakeSession(v6_answers())).test_connection(SETTINGS)[0])

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


if __name__ == "__main__":
    unittest.main()
