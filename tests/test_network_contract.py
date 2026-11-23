# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Hold ``GET /api/v1/network`` to its recorded answers.

The Networks page moved to a source-neutral shape (the ``network`` key on
each source) without changing what the API already served. These tests
replay four stored states against answers recorded before that change:
a UniFi snapshot, a UniFi source with none yet, a snapshot whose last
refresh failed, and a Pi-hole with no UniFi at all. The legacy keys
(everything except ``network``) must match the recordings exactly.

Set ``LABRAT_RECORD_NETWORK=1`` to rewrite the recordings, only when the
legacy answer is meant to change.
"""

import json
import os
from pathlib import Path
import unittest

import test_step4c as fixture
from labrat.collector import unifi_network as un
from labrat.discovery.models import DiscoveredDevice, DiscoverySource
from test_unifi_network import BASE, SETTINGS, page, responses, routed

RECORDINGS = Path(__file__).resolve().parent / "fixtures" / "network_api"
SAMPLED_AT = "2026-01-02T03:04:05+00:00"


def legacy(answer: dict) -> dict:
    """Drop the shared ``network`` form, leaving what the API served before it.

    Args:
        answer: A ``GET /api/v1/network`` response.

    Returns:
        dict: The same response without each source's ``network`` key.
    """
    return {**answer, "sources": [{key: value for key, value in source.items() if key != "network"}
                                  for source in answer["sources"]]}


class StoredNetwork(unittest.TestCase):
    """Helpers that store the states these tests replay."""

    setUp = fixture.Step4CTests.setUp

    def add_unifi(self, snapshot: dict | None) -> None:
        """Store a UniFi source whose gateway holds ``snapshot``, if any.

        Args:
            snapshot: The stored ``unifi_network`` value, or None.
        """
        details = {"wan": {"state": "up"}}
        if snapshot is not None:
            details["unifi_network"] = snapshot
        with self.factory.begin() as db:
            source = DiscoverySource(name="UniFi example", kind="unifi", config_name="default")
            db.add(source)
            db.flush()
            db.add(DiscoveredDevice(source_id=source.id, remote_id="site-1:gw", name="Gateway", category="router",
                                    details_json=json.dumps(details)))

    def add_pihole(self) -> None:
        """Store a configured Pi-hole with statistics."""
        with self.factory.begin() as db:
            source = DiscoverySource(name="pihole:home", kind="pihole", config_name="home")
            db.add(source)
            db.flush()
            db.add(DiscoveredDevice(
                source_id=source.id, remote_id="pihole", name="pihole", category="server",
                identifiers_json=json.dumps({"management_ips": ["192.0.2.53"]}),
                details_json=json.dumps({"pihole": {"queries": {"total": 100.0, "blocked": 25.0}}})))

    @staticmethod
    def snapshot(available: bool) -> dict:
        """A curated snapshot from the shared UniFi test responses.

        Args:
            available: Whether its last refresh succeeded.

        Returns:
            dict: The value the collector stores.
        """
        return {**un.network_snapshot(routed(responses()), SETTINGS), "available": available,
                "sampled_at": SAMPLED_AT}

    def check(self, name: str) -> dict:
        """Compare the legacy answer with its recording (or record it).

        Args:
            name: The recording's file name, without ``.json``.

        Returns:
            dict: The full answer, for further checks.
        """
        answer = self.client.get("/api/v1/network").json
        path = RECORDINGS / f"{name}.json"
        if os.environ.get("LABRAT_RECORD_NETWORK") == "1":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(legacy(answer), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        self.assertEqual(legacy(answer), json.loads(path.read_text(encoding="utf-8")))
        return answer


class NetworkContractTests(StoredNetwork):
    """Each stored state answers exactly as recorded."""

    def test_unifi_snapshot(self):
        """A stored snapshot answers with its derived paths and sinkholes."""
        self.add_unifi(self.snapshot(available=True))
        self.check("unifi_snapshot")

    def test_unifi_without_snapshot(self):
        """A UniFi source with no snapshot yet is listed without one."""
        self.add_unifi(None)
        self.check("unifi_without_snapshot")

    def test_unifi_stale_snapshot(self):
        """A snapshot whose last refresh failed is still served, marked unavailable."""
        self.add_unifi(self.snapshot(available=False))
        self.check("unifi_stale_snapshot")

    def test_pihole_only(self):
        """A Pi-hole with no UniFi source appears in the global list."""
        self.add_pihole()
        self.check("pihole_only")


class SharedFormTests(StoredNetwork):
    """The shared ``network`` form carries the same data, source-neutral."""

    def shared(self) -> dict:
        """The first source's shared form.

        Returns:
            dict: Its ``network`` value.
        """
        return self.client.get("/api/v1/network").json["sources"][0]["network"]

    def test_ids_and_words_are_source_neutral(self):
        """IDs are scoped to the source; UniFi's words are normalized."""
        self.add_unifi(self.snapshot(available=True))
        network = self.shared()
        self.assertEqual(network["source"], {"id": 1, "kind": "unifi", "name": "UniFi example"})
        self.assertEqual((network["site"], network["sampled_at"], network["available"]), ("Example", SAMPLED_AT, True))
        self.assertEqual(network["capabilities"], {"verdicts": True})
        zones = {zone["name"]: zone for zone in network["zones"]}
        self.assertEqual(zones["Internal"]["id"], "unifi:1:zone:zone-in")
        self.assertEqual((zones["Internal"]["origin"], zones["Lab"]["origin"]), ("system", "user"))
        home = network["networks"][0]
        self.assertEqual((home["id"], home["zone"]), ("unifi:1:network:net-a", "unifi:1:zone:zone-in"))
        self.assertEqual((home["ipv4"], home["ipv6"], home["cidr"]), (["192.0.2.1/24"], [], "192.0.2.0/24"))
        self.assertEqual(zones["Internal"]["networks"], [home["id"]])
        self.assertEqual(network["gateways"], [{"id": "unifi:1:network:wan-1", "name": "Internet 1"}])
        self.assertEqual(sorted(policy["action"] for policy in network["policies"]), ["allow", "allow", "block"])
        self.assertEqual(network["policies"][0]["source"]["zone"], "unifi:1:zone:zone-in")
        [path] = network["paths"]
        self.assertEqual((path["source"], path["destination"], path["verdict"]),
                         ("unifi:1:zone:zone-in", "unifi:1:zone:zone-lab", "block"))
        self.assertEqual(path["exceptions"][0]["origin"], "user")
        [device] = network["devices"]
        self.assertEqual((device["id"], device["kind"], device["roles"], device["state"], device["ports"][0]["state"]),
                         ("unifi:1:device:dev-1", "switch", ["switch"], "online", "up"))
        [client] = network["clients"]
        self.assertEqual((client["connection"], client["uplink"], client["network"]), ("wired", device["id"], "Home"))
        self.assertEqual(network["sinkholes"][0]["ip"], "192.0.2.53")
        self.assertTrue(all(section["status"] == "complete" for section in network["sections"].values()))

    def test_stale_and_partial_sections(self):
        """A failed refresh is unavailable; failed and limited reads mark their sections."""
        snapshot = self.snapshot(available=False)
        snapshot["errors"] = {"clients": "ConnectionError", "device_detail": "limited"}
        self.add_unifi(snapshot)
        network = self.shared()
        self.assertFalse(network["available"])
        self.assertEqual(network["sections"]["clients"], {"status": "failed", "sampled_at": SAMPLED_AT})
        self.assertEqual(network["sections"]["devices"]["status"], "partial")
        self.assertEqual(network["sections"]["networks"]["status"], "complete")

    def test_console_that_routes_is_still_an_access_point(self):
        """A gateway model that broadcasts Wi-Fi keeps both roles, gateway first."""
        snapshot = self.snapshot(available=True)
        snapshot["devices"].append({"id": "dev-2", "name": "Console", "model": "Dream Machine Pro", "state": "ONLINE",
                                    "features": ["accessPoint", "switching"], "radios": [{"band": 2.4, "channel": 6}]})
        self.add_unifi(snapshot)
        console = self.shared()["devices"][1]
        self.assertEqual((console["kind"], console["roles"]), ("gateway", ["gateway", "access_point", "switch"]))

    def test_absent_section_is_not_available(self):
        """A stored sample without a section says so rather than claiming it empty."""
        snapshot = self.snapshot(available=True)
        del snapshot["neighbours"]
        snapshot["health"] = []
        self.add_unifi(snapshot)
        sections = self.shared()["sections"]
        self.assertEqual(sections["neighbours"], {"status": "not_available", "sampled_at": None})
        self.assertEqual(sections["health"]["status"], "complete")

    def test_capped_network_list_is_partial(self):
        """More networks than the collector keeps leave the section partial."""
        answers = responses()
        extra = [{"id": f"net-{index}", "name": f"Net {index}"} for index in range(un.MAX_NETWORKS)]
        answers[f"{BASE}/networks"] = page(answers[f"{BASE}/networks"]["data"] + extra)
        snapshot = un.network_snapshot(routed(answers), SETTINGS)
        self.assertEqual(snapshot["errors"]["network_list"], "limited")
        self.assertEqual(len(snapshot["networks"]), un.MAX_NETWORKS)
        self.add_unifi({**snapshot, "available": True, "sampled_at": SAMPLED_AT})
        self.assertEqual(self.shared()["sections"]["networks"]["status"], "partial")

    def test_no_snapshot_has_no_shared_form(self):
        """A source with no snapshot yet has ``network`` None."""
        self.add_unifi(None)
        self.assertIsNone(self.shared())


if __name__ == "__main__":
    unittest.main()
