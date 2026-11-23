# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Check the OPNsense adapter: its read-only contract, poll, identity hold, and Settings.

Answers come from ``tests/fixtures/opnsense``, recorded from an OPNsense
26.7 test bed (``scripts/opnsense_testbed.py``) and sanitized.
"""

import copy
import json
from pathlib import Path
import re
import time
import unittest
from unittest.mock import patch

import requests
from sqlalchemy import select

import test_step4c as fixture
from labrat.collector import identity, runner
from labrat.collector.adapters import opnsense
from labrat.collector.adapters.opnsense import (
    ALLOWED_PATHS, FIREWALL, OpnsenseAdapter, OpnsenseClient, RequestRefused, media_speed,
)
from labrat.discovery.models import DiscoveredDevice, DiscoveredPort, DiscoverySource
from labrat.integrations import read_settings
from labrat.models import Device, Room, Site

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "opnsense"
ORIGIN = "https://198.51.100.1:8443"
#: The recorded firewall's NICs: LAN, WAN, the two private links, and the peer link.
NICS = ["em0", "em1", "em2", "em3", "em4"]
SETTINGS = {"url": ORIGIN, "api_key": "key-0123456789", "api_secret": "secret-0123456789"}
#: Credential-like values seeded into unexpected fields; none may leak.
SEEDS = {"password": "seed-password-1", "secret": "seed-secret-2", "key": "seed-key-3", "psk": "seed-psk-4",
         "token": "seed-token-5", "apikey": "seed-apikey-6"}


def answers() -> dict[str, object]:
    """The recorded answers, by path under ``/api/``.

    DHCP lease expiries are moved to an hour from now, as they were when
    recorded, so a lease is not expired just because the test runs later.

    Returns:
        dict[str, object]: Decoded JSON by path.
    """
    found = {path.stem.replace("__", "/"): json.loads(path.read_text(encoding="utf-8"))
             for path in FIXTURES.glob("*.json")}
    for path in ("dnsmasq/leases/search", "kea/leases4/search"):
        for row in found[path]["rows"]:
            row["expire"] = int(time.time()) + 3600
    return found


class FakeResponse:
    """A minimal requests.Response stand-in."""

    def __init__(self, payload, status=200):
        """Hold a scripted reply.

        Args:
            payload: The JSON body.
            status: The HTTP status.
        """
        self.payload, self.status_code = payload, status

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

        Raises:
            ValueError: The scripted body is not JSON.
        """
        if isinstance(self.payload, bytes):
            raise ValueError("not JSON")
        return self.payload


class FakeSession:
    """Answers by path and records every request with its options."""

    def __init__(self, replies):
        """Script the firewall.

        Args:
            replies: By path: a JSON body, a ``(body, status)`` pair, or an
                exception to raise.
        """
        self.replies, self.calls = replies, []

    def request(self, method, url, **kwargs):
        """Answer one request from the script.

        Args:
            method: The HTTP method.
            url: The full URL.
            **kwargs: Request options, recorded.

        Returns:
            FakeResponse: The scripted reply, or a 404.

        Raises:
            Exception: The scripted exception, when the reply is one.
        """
        self.calls.append((method, url, kwargs))
        path = url.split("/api/", 1)[1]
        reply = self.replies.get(path)
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            return FakeResponse({"errorMessage": "Endpoint not found"}, 404)
        if isinstance(reply, tuple):
            return FakeResponse(*reply)
        return FakeResponse(copy.deepcopy(reply))


def adapter(replies=None) -> tuple[OpnsenseAdapter, FakeSession]:
    """An adapter over a scripted firewall.

    Args:
        replies: The script; the recorded answers by default.

    Returns:
        tuple: The adapter and its session.
    """
    session = FakeSession(answers() if replies is None else replies)
    return OpnsenseAdapter(session), session


class ContractTests(unittest.TestCase):
    """The client sends only allowlisted GETs to the configured origin."""

    def test_every_request_keeps_the_contract(self):
        """A poll and a Test connection send GETs to allowlisted paths, nothing else."""
        reader, session = adapter()
        reader.poll(SETTINGS)
        self.assertTrue(reader.test_connection(SETTINGS)[0])
        self.assertTrue(session.calls)
        for method, url, options in session.calls:
            self.assertEqual(method, "GET")
            self.assertTrue(url.startswith(f"{ORIGIN}/api/"), url)
            self.assertIn(url.split("/api/", 1)[1], ALLOWED_PATHS)
            self.assertNotIn("?", url)
            self.assertNotIn("params", options)
            self.assertFalse(options["allow_redirects"])
            self.assertEqual(options["auth"], ("key-0123456789", "secret-0123456789"))
            self.assertEqual(options["timeout"], opnsense.TIMEOUT_SECONDS)

    def test_paths_outside_the_allowlist_are_refused_unsent(self):
        """A deferred or changing endpoint is refused before any request."""
        session = FakeSession({})
        client = OpnsenseClient(SETTINGS, session)
        for path in ("lldpd/service/neighbor", "openvpn/service/search_sessions", "core/firmware/status",
                     "diagnostics/interface/flush_arp", "core/system/status?x=1", "../core/system/status"):
            with self.assertRaises(RequestRefused, msg=path):
                client.get(path)
        self.assertEqual(session.calls, [])
        self.assertIn("core/system/status", ALLOWED_PATHS)
        self.assertNotIn("lldpd/service/neighbor", ALLOWED_PATHS)

    def test_redirects_are_refused(self):
        """A 3xx answer is an error, never followed."""
        reader, _session = adapter({"core/system/status": ({}, 302)})
        ok, message = reader.test_connection(SETTINGS)
        self.assertFalse(ok)
        self.assertIn("redirect", message)

    def test_settings_must_be_https_with_a_key_and_secret(self):
        """Plain HTTP, a path, or a missing credential never reaches the network."""
        for broken in ({**SETTINGS, "url": "http://198.51.100.1"}, {**SETTINGS, "url": f"{ORIGIN}/api"},
                       {key: value for key, value in SETTINGS.items() if key != "api_secret"}):
            with self.assertRaises(ValueError):
                OpnsenseClient(broken, FakeSession({}))

    def test_tls_follows_the_settings(self):
        """Verification is on by default, off or a CA bundle when configured."""
        self.assertIs(OpnsenseClient(SETTINGS, FakeSession({})).verify, True)
        self.assertIs(OpnsenseClient({**SETTINGS, "verify_tls": False}, FakeSession({})).verify, False)

    def test_seeded_secrets_never_leave_the_projections(self):
        """Credential-like values in unexpected fields reach no stored reading."""
        replies = answers()

        def seed(value):
            if isinstance(value, dict):
                return {**{key: seed(item) for key, item in value.items()}, **SEEDS}
            if isinstance(value, list):
                return [seed(item) for item in value]
            return value

        replies = {path: seed(value) for path, value in replies.items()}
        reader, _session = adapter(replies)
        batch = reader.poll(SETTINGS)
        stored = json.dumps([batch.devices[0].__dict__, [port.__dict__ for port in batch.ports],
                             reader.last_telemetry, reader.last_sightings], default=str)
        stored += reader.test_connection(SETTINGS)[1]
        for value in SEEDS.values():
            self.assertNotIn(value, stored)


class PollTests(unittest.TestCase):
    """A poll reports the firewall, its NICs, and its health."""

    def test_recorded_firewall(self):
        """The recorded answers give one firewall with its four NICs and its health."""
        reader, _session = adapter()
        batch = reader.poll(SETTINGS)
        [device] = batch.devices
        self.assertEqual((device.remote_id, device.name, device.category), (FIREWALL, "OPNsense.internal", "firewall"))
        self.assertEqual(device.identifiers["macs"], [f"02:00:00:00:00:0{index}" for index in range(1, 6)])
        self.assertEqual(device.identifiers["management_ips"], ["198.51.100.1"])
        self.assertEqual(device.details, {"platform": "opnsense", "product": "OPNsense 26.7-amd64", "ports_complete": True})
        self.assertEqual([(port.remote_id, port.mac, port.speed_mbps, port.link_up) for port in batch.ports],
                         [(name, f"02:00:00:00:00:0{index}", 1000, True) for index, name in enumerate(NICS, 1)])
        telemetry = reader.last_telemetry[FIREWALL]
        reading = telemetry["opnsense"]
        self.assertTrue(all(item["status"] == "complete" for item in reading["sections"].values()))
        self.assertEqual(reading["memory"]["total"], 2102894592.0)
        self.assertEqual(len(reading["time"]["loadavg"]), 3)
        self.assertEqual(reading["statistics"]["em0"]["received_errors"], 0.0)
        self.assertEqual([item["device"] for item in reading["interfaces"]], [*NICS, "lo0", "enc0", "pflog0", "vlan01", "wg0"])
        self.assertEqual(telemetry["identity_evidence"]["macs"], device.identifiers["macs"])

    def test_media_speeds(self):
        """FreeBSD media strings give link speeds."""
        self.assertEqual(media_speed("1000baseT <full-duplex>"), 1000)
        self.assertEqual(media_speed("10Gbase-SR <full-duplex>"), 10000)
        self.assertEqual(media_speed("2500Base-T <full-duplex>"), 2500)
        self.assertIsNone(media_speed("autoselect"))
        self.assertIsNone(media_speed(None))

    def test_section_outcomes(self):
        """403 and 404 are not available, 5xx and malformed answers failed; 401 fails the poll."""
        replies = answers()
        replies["interfaces/overview/interfaces_info"] = ({"status": 403}, 403)
        replies["diagnostics/interface/get_interface_statistics"] = ({}, 500)
        replies["diagnostics/system/system_disk"] = b"<html>"
        del replies["diagnostics/system/system_swap"]
        reader, _session = adapter(replies)
        batch = reader.poll(SETTINGS)
        sections = reader.last_telemetry[FIREWALL]["opnsense"]["sections"]
        self.assertEqual(sections["interfaces"], {"status": "not_available", "sampled_at": None, "error": "HTTP 403"})
        self.assertEqual(sections["statistics"]["status"], "failed")
        self.assertEqual(sections["disks"], {"status": "failed", "sampled_at": None, "error": "ValueError"})
        self.assertEqual(sections["swap"]["status"], "not_available")
        self.assertFalse(batch.devices[0].details["ports_complete"])
        self.assertEqual(batch.ports, ())
        self.assertNotIn("identity_evidence", reader.last_telemetry[FIREWALL])
        replies["diagnostics/system/system_information"] = ({}, 401)
        with self.assertRaises(requests.HTTPError):
            adapter(replies)[0].poll(SETTINGS)

    def test_capped_interface_list_is_partial(self):
        """More rows than the cap: the NICs read so far, but nothing may be removed."""
        with patch.object(opnsense, "MAX_ROWS", 1):
            reader, _session = adapter()
            batch = reader.poll(SETTINGS)
        self.assertEqual(reader.last_telemetry[FIREWALL]["opnsense"]["sections"]["interfaces"]["status"], "partial")
        self.assertFalse(batch.devices[0].details["ports_complete"])
        self.assertEqual([port.remote_id for port in batch.ports], ["em0"])

    def test_failed_sections_keep_their_last_good_reading(self):
        """A failed section carries over the last good value and its time."""
        reader, _session = adapter()
        reader.poll(SETTINGS)
        before = reader.last_telemetry[FIREWALL]["opnsense"]
        replies = answers()
        replies["diagnostics/system/system_resources"] = ({}, 503)
        later, _session = adapter(replies)
        later.poll(SETTINGS)
        now = later.last_telemetry[FIREWALL]["opnsense"]
        merged = OpnsenseAdapter.merge_telemetry("opnsense", before, now)
        self.assertEqual(merged["memory"], before["memory"])
        self.assertEqual(merged["sections"]["memory"]["status"], "failed")
        self.assertEqual(merged["sections"]["memory"]["sampled_at"], before["sections"]["memory"]["sampled_at"])
        self.assertIs(OpnsenseAdapter.merge_telemetry("other", before, now), now)

    def test_test_connection_messages(self):
        """Test connection names the firewall, a refused key, a missing privilege, and an untested release."""
        ok, message = adapter()[0].test_connection(SETTINGS)
        self.assertTrue(ok)
        self.assertIn("OPNsense 26.7-amd64 answered as OPNsense.internal", message)
        self.assertNotIn("untested", message)
        older = answers()
        older["diagnostics/system/system_information"]["versions"] = ["OPNsense 25.1-amd64"]
        self.assertIn("untested", adapter(older)[0].test_connection(SETTINGS)[1])
        for status, words in ((401, "refused the API key"), (403, "lacks"), (500, "unexpected")):
            self.assertIn(words, adapter({"core/system/status": ({}, status)})[0].test_connection(SETTINGS)[1])
        self.assertIn("could not be reached",
                      adapter({"core/system/status": requests.ConnectionError("down")})[0].test_connection(SETTINGS)[1])
        self.assertIn("Certificate", adapter({"core/system/status": requests.exceptions.SSLError("x")})[0]
                      .test_connection(SETTINGS)[1])
        self.assertFalse(OpnsenseAdapter(FakeSession({})).test_connection({"url": ORIGIN})[0])


class StoredFirewall(unittest.TestCase):
    """Helpers that configure the firewall and poll it through the collector."""

    setUp = fixture.Step4CTests.setUp

    def poll(self, replies=None, name="edge"):
        """Run one collector pass over one configured firewall.

        Args:
            replies: The scripted firewall; the recorded one by default.
            name: The integration's config name.

        Returns:
            DiscoverySource: The source afterwards.
        """
        sources = {"opnsense": {name: SETTINGS}}
        with patch("labrat.collector.runner.effective_sources", return_value=sources):
            runner.poll_once(self.factory, {"integrations": sources}, {"opnsense": adapter(replies)[0]})
        with self.factory() as db:
            return db.scalar(select(DiscoverySource).where(DiscoverySource.kind == "opnsense", DiscoverySource.config_name == name))

    def device(self, name="edge"):
        """One integration's stored firewall observation.

        Args:
            name: The integration's config name.

        Returns:
            DiscoveredDevice: It.
        """
        with self.factory() as db:
            source = db.scalar(select(DiscoverySource).where(DiscoverySource.kind == "opnsense", DiscoverySource.config_name == name))
            return db.scalar(select(DiscoveredDevice).where(DiscoveredDevice.source_id == source.id))

    def ports(self, name="edge"):
        """One integration's stored NIC names.

        Args:
            name: The integration's config name.

        Returns:
            list[str]: The ports' remote IDs.
        """
        with self.factory() as db:
            return sorted(db.scalars(select(DiscoveredPort.remote_id).where(
                DiscoveredPort.discovered_device_id == self.device(name).id)))

    def link_inventory(self, name="edge"):
        """Link an integration's firewall observation to an inventory device.

        Args:
            name: The integration's config name.

        Returns:
            int: The inventory device's ID.
        """
        observation = self.device(name).id
        with self.factory.begin() as db:
            site = Site(name=f"Site {name}")
            db.add(site)
            db.flush()
            room = Room(site_id=site.id, name=f"Room {name}")
            db.add(room)
            db.flush()
            device = Device(name=f"Firewall {name}", room_id=room.id, placement="floor")
            db.add(device)
            db.flush()
            db.get(DiscoveredDevice, observation).device_id = device.id
            return device.id

    def shown_evidence(self, name="edge"):
        """The evidence fingerprint the editor puts in its answer form.

        Args:
            name: The integration's config name.

        Returns:
            str: The fingerprint.
        """
        page = self.client.get(f"/settings?panel=integrations&kind=opnsense&name={name}").get_data(as_text=True)
        return re.search(r'name="evidence" value="([0-9a-f]+)"', page).group(1)

    def answer(self, choice, evidence, name="edge"):
        """Answer a held poll in Settings.

        Args:
            choice: ``same`` or ``different``.
            evidence: The fingerprint the page showed.
            name: The integration's config name.

        Returns:
            The response.
        """
        revision = read_settings(self.app.extensions["labrat_config_path"])[2]
        return self.client.post("/settings/integrations/identity", data={
            "_csrf_token": self.csrf, "revision": revision, "kind": "opnsense", "name": name, "choice": choice,
            "evidence": evidence})

    def configure(self):
        """Store the integration in the private settings and as a source."""
        path = self.app.extensions["labrat_config_path"]
        path.write_text(path.read_text() + '\n[integrations.opnsense.edge]\nurl = "https://198.51.100.1:8443"\n'
                        'api_key = "key-0123456789"\napi_secret = "secret-0123456789"\ndisplay_name = "Edge"\n')
        with self.factory.begin() as db:
            db.add(DiscoverySource(name="opnsense:Edge", kind="opnsense", config_name="edge"))

    @staticmethod
    def other_firewall():
        """A different machine: another hostname and other NICs.

        Returns:
            dict: Its answers.
        """
        replies = answers()
        replies["diagnostics/system/system_information"]["name"] = "replacement.example"
        for row in replies["interfaces/overview/interfaces_info"]["rows"]:
            if row.get("macaddr_hw"):
                row["macaddr_hw"] = row["macaddr"] = row["macaddr_hw"].replace("02:00", "0a:00")
        return replies



class IdentityTests(StoredFirewall):
    """A poll is held when a different firewall answers; Settings answers it."""

    def test_first_poll_stores_device_ports_and_evidence(self):
        """A first poll stores the firewall, its NICs, health, and identity evidence."""
        self.configure()
        source = self.poll()
        self.assertIsNone(source.last_error)
        details = json.loads(self.device().details_json)
        self.assertEqual(details["identity_evidence"]["hostname"], "OPNsense.internal")
        self.assertEqual(details["opnsense"]["sections"]["system"]["status"], "complete")
        with self.factory() as db:
            source = db.scalar(select(DiscoverySource).where(DiscoverySource.kind == "opnsense"))
            self.assertEqual(len(list(db.scalars(select(DiscoveredDevice).where(DiscoveredDevice.source_id == source.id)))), 1)

    def test_a_different_firewall_is_held_until_answered(self):
        """Another hostname and no shared NIC: nothing applied, Settings asks."""
        self.configure()
        self.poll()
        held = self.poll(self.other_firewall())
        self.assertEqual(held.last_error, identity.HELD_MESSAGE)
        device = self.device()
        self.assertEqual(device.name, "OPNsense.internal")
        details = json.loads(device.details_json)
        self.assertEqual(details["identity_pending"]["hostname"], "replacement.example")
        editor = self.client.get("/settings?panel=integrations&kind=opnsense&name=edge").get_data(as_text=True)
        self.assertIn("A different device may be answering", editor)
        self.assertIn("Collection paused", editor)
        self.assertNotIn("Collection failed", editor)
        self.assertIn("replacement.example", editor)
        self.assertNotIn("0a:00", editor)
        self.assertEqual(self.answer("same", self.shown_evidence()).status_code, 302)
        resumed = self.poll(self.other_firewall())
        self.assertIsNone(resumed.last_error)
        details = json.loads(self.device().details_json)
        self.assertNotIn("identity_pending", details)
        self.assertEqual(self.device().name, "replacement.example")

    def test_a_nic_swap_or_new_hostname_alone_is_not_held(self):
        """Sharing one NIC, or keeping the hostname, is the same firewall."""
        self.configure()
        self.poll()
        swapped = answers()
        swapped["interfaces/overview/interfaces_info"]["rows"][1]["macaddr_hw"] = "0a:00:00:00:00:09"
        swapped["diagnostics/system/system_information"]["name"] = "renamed.example"
        self.assertIsNone(self.poll(swapped).last_error)
        renamed = self.other_firewall()
        renamed["diagnostics/system/system_information"]["name"] = "renamed.example"
        self.assertIsNone(self.poll(renamed).last_error)

    def test_different_firewall_answer_keeps_the_old_one(self):
        """A different firewall gets a new integration; the old one keeps its observation, disabled."""
        self.configure()
        self.poll()
        self.poll(self.other_firewall())
        path = self.app.extensions["labrat_config_path"]
        answer = self.answer("different", self.shown_evidence())
        self.assertEqual(answer.status_code, 302)
        self.assertIn("name=edge-2", answer.headers["Location"])
        _raw, config, _revision = read_settings(path)
        family = config["integrations"]["opnsense"]
        self.assertEqual(family["edge"]["display_name"], "Edge (replaced)")
        self.assertEqual(family["edge-2"]["api_secret"], "secret-0123456789")
        with self.factory() as db:
            sources = {row.config_name: row for row in db.scalars(select(DiscoverySource).where(DiscoverySource.kind == "opnsense"))}
            self.assertFalse(sources["edge"].enabled)
            self.assertTrue(sources["edge-2"].enabled)
            old = db.scalar(select(DiscoveredDevice).where(DiscoveredDevice.source_id == sources["edge"].id))
            self.assertEqual(old.name, "OPNsense.internal")
            self.assertNotIn("identity_pending", json.loads(old.details_json))

    def test_nothing_pending_refuses_an_answer(self):
        """An answer without a held poll changes nothing."""
        self.configure()
        self.poll()
        self.assertEqual(self.answer("same", "0" * 16).status_code, 400)

    def test_an_incomplete_poll_stays_held(self):
        """While a different firewall is pending, a poll without evidence writes nothing."""
        self.configure()
        self.poll()
        inventory = self.link_inventory()
        before = self.device()
        self.poll(self.other_firewall())
        partial = self.other_firewall()
        partial["interfaces/overview/interfaces_info"] = ({}, 403)
        held = self.poll(partial)
        self.assertEqual(held.last_error, identity.HELD_MESSAGE)
        after = self.device()
        self.assertEqual((after.id, after.name, after.device_id), (before.id, "OPNsense.internal", inventory))
        self.assertEqual(self.ports(), NICS)
        stored = json.loads(after.details_json)
        self.assertEqual(stored["opnsense"], json.loads(before.details_json)["opnsense"])
        self.assertEqual(stored["identity_evidence"]["hostname"], "OPNsense.internal")
        capped = self.other_firewall()
        with patch.object(opnsense, "MAX_ROWS", 1):
            self.assertEqual(self.poll(capped).last_error, identity.HELD_MESSAGE)
        self.assertEqual(self.device().name, "OPNsense.internal")
        # The known firewall back, read in full, resumes collection.
        self.assertIsNone(self.poll().last_error)
        self.assertNotIn("identity_pending", json.loads(self.device().details_json))

    def test_the_answer_applies_only_to_the_evidence_shown(self):
        """Repeated readings of the same candidate keep the answer; a new candidate refuses it."""
        self.configure()
        self.poll()
        self.poll(self.other_firewall())
        shown = self.shown_evidence()
        self.poll(self.other_firewall())  # the same candidate, read again later
        third = self.other_firewall()
        third["diagnostics/system/system_information"]["name"] = "third.example"
        for row in third["interfaces/overview/interfaces_info"]["rows"]:
            if row.get("macaddr_hw"):
                row["macaddr_hw"] = row["macaddr_hw"].replace("0a:00", "0e:00")
        self.poll(third)
        refused = self.answer("same", shown)
        self.assertEqual(refused.status_code, 400)
        self.assertIn("changed since this page opened", refused.get_data(as_text=True))
        self.assertEqual(json.loads(self.device().details_json)["identity_pending"]["hostname"], "third.example")
        self.assertEqual(self.answer("same", self.shown_evidence()).status_code, 302)
        self.assertIsNone(self.poll(third).last_error)
        self.assertEqual(self.device().name, "third.example")

    def test_repeated_reading_keeps_the_answer_usable(self):
        """The same candidate read again after the page opened is still the one answered."""
        self.configure()
        self.poll()
        self.poll(self.other_firewall())
        shown = self.shown_evidence()
        self.poll(self.other_firewall())
        self.assertEqual(self.answer("same", shown).status_code, 302)


class InFlightTests(StoredFirewall):
    """A poll still reading when its source is retired records nothing."""

    def poll_while_answering(self, replies, fail=False):
        """Poll, answering "different" in Settings while the adapter reads.

        Args:
            replies: The scripted firewall.
            fail: Make the adapter fail after the answer, for the error path.

        Returns:
            DiscoverySource: The old source afterwards.
        """
        evidence = self.shown_evidence()
        test = self

        class Answering(FakeSession):
            """Answers in Settings on the adapter's first request."""

            def request(self, method, url, **kwargs):
                """Answer once, then serve the script (or fail).

                Args:
                    method: The HTTP method.
                    url: The full URL.
                    **kwargs: Request options.

                Returns:
                    FakeResponse: The scripted reply.

                Raises:
                    requests.ConnectionError: When failing.
                """
                if not self.calls:
                    test.assertEqual(test.answer("different", evidence).status_code, 302)
                if fail:
                    raise requests.ConnectionError("down")
                return super().request(method, url, **kwargs)

        sources = {"opnsense": {"edge": SETTINGS}}
        with patch("labrat.collector.runner.effective_sources", return_value=sources):
            runner.poll_once(self.factory, {"integrations": sources}, {"opnsense": OpnsenseAdapter(Answering(replies))})
        with self.factory() as db:
            return db.scalar(select(DiscoverySource).where(DiscoverySource.kind == "opnsense", DiscoverySource.config_name == "edge"))

    def held_firewall(self):
        """A firewall with an inventory link, then a held poll from another machine.

        Returns:
            tuple: The observation before, and its inventory device's ID.
        """
        self.configure()
        self.poll()
        inventory = self.link_inventory()
        self.poll(self.other_firewall())
        return self.device(), inventory

    def assert_untouched(self, before, inventory, source):
        """The retired source and its observation are as Settings left them.

        Args:
            before: The observation before the in-flight poll.
            inventory: Its inventory device's ID.
            source: The old source afterwards.
        """
        after = self.device()
        self.assertEqual((after.id, after.name, after.device_id), (before.id, "OPNsense.internal", inventory))
        self.assertEqual(json.loads(after.details_json)["opnsense"], json.loads(before.details_json)["opnsense"])
        self.assertEqual(self.ports(), NICS)
        self.assertFalse(source.enabled)
        self.assertIsNone(source.last_error)

    def test_incomplete_result_after_retirement_is_discarded(self):
        """An incomplete reading of the new machine never reaches the retired observation."""
        before, inventory = self.held_firewall()
        with self.factory() as db:
            success = db.scalar(select(DiscoverySource.last_success_at).where(DiscoverySource.kind == "opnsense", DiscoverySource.config_name == "edge"))
        partial = self.other_firewall()
        partial["interfaces/overview/interfaces_info"] = ({}, 403)
        source = self.poll_while_answering(partial)
        self.assert_untouched(before, inventory, source)
        self.assertEqual(source.last_success_at, success)

    def test_complete_result_after_retirement_is_discarded(self):
        """A complete reading of the new machine is not applied to the retired source either."""
        before, inventory = self.held_firewall()
        self.assert_untouched(before, inventory, self.poll_while_answering(self.other_firewall()))

    def test_failure_after_retirement_records_nothing(self):
        """The error path does not mark a retired source failed."""
        before, inventory = self.held_firewall()
        self.assert_untouched(before, inventory, self.poll_while_answering(self.other_firewall(), fail=True))


class ReconciliationTests(StoredFirewall):
    """Only a complete read removes NICs; unread sections keep their last good reading."""

    def test_malformed_interface_rows_keep_known_nics(self):
        """A row that might be a NIC makes the list incomplete; a valid empty list removes them."""
        self.configure()
        self.poll()
        self.assertEqual(self.ports(), NICS)
        for broken in ("flag", "object", "name"):
            replies = answers()
            rows = replies["interfaces/overview/interfaces_info"]["rows"]
            if broken == "flag":
                del rows[0]["is_physical"]
            elif broken == "object":
                rows[0] = "em0"
            else:
                rows[0]["device"] = None
            self.assertIsNone(self.poll(replies).last_error)
            self.assertEqual(self.ports(), NICS, broken)
            sections = json.loads(self.device().details_json)["opnsense"]["sections"]
            self.assertEqual(sections["interfaces"]["status"], "partial", broken)
        empty = answers()
        empty["interfaces/overview/interfaces_info"] = {"total": 0, "rowCount": 0, "current": 1, "rows": []}
        self.poll(empty)
        self.assertEqual(self.ports(), [])

    def test_unread_sections_keep_their_last_good_reading(self):
        """Success, then a 403 or a malformed 200, then recovery."""
        self.configure()
        self.poll()
        first = json.loads(self.device().details_json)["opnsense"]
        replies = answers()
        replies["diagnostics/system/system_resources"] = ({}, 403)
        replies["diagnostics/system/system_disk"] = {}
        self.poll(replies)
        held = json.loads(self.device().details_json)["opnsense"]
        self.assertEqual(held["memory"], first["memory"])
        self.assertEqual(held["sections"]["memory"]["status"], "not_available")
        self.assertEqual(held["sections"]["memory"]["sampled_at"], first["sections"]["memory"]["sampled_at"])
        self.assertEqual(held["disks"], first["disks"])
        self.assertEqual(held["sections"]["disks"]["status"], "failed")
        self.assertEqual(held["sections"]["disks"]["sampled_at"], first["sections"]["disks"]["sampled_at"])
        self.poll()
        recovered = json.loads(self.device().details_json)["opnsense"]
        self.assertEqual(recovered["sections"]["memory"]["status"], "complete")
        self.assertNotEqual(recovered["sections"]["memory"]["sampled_at"], first["sections"]["memory"]["sampled_at"])

    def test_malformed_shapes_fail_rather_than_read_empty(self):
        """Every health answer of the wrong shape is a failed section, never a valid empty one."""
        replies = answers()
        replies["diagnostics/system/system_disk"] = {}
        replies["diagnostics/system/system_swap"] = {"swap": "none"}
        replies["diagnostics/system/system_temperature"] = {}
        replies["diagnostics/system/system_resources"] = {"memory": []}
        replies["diagnostics/system/system_time"] = []
        reader, _session = adapter(replies)
        reader.poll(SETTINGS)
        sections = reader.last_telemetry[FIREWALL]["opnsense"]["sections"]
        for name in ("disks", "swap", "temperatures", "memory", "time"):
            self.assertEqual(sections[name]["status"], "failed", name)
        reader, _session = adapter()
        reader.poll(SETTINGS)
        self.assertEqual(reader.last_telemetry[FIREWALL]["opnsense"]["temperatures"], [])
        self.assertEqual(reader.last_telemetry[FIREWALL]["opnsense"]["sections"]["temperatures"]["status"], "complete")


class ReplacementTests(StoredFirewall):
    """Changing a firewall's address asks whether it is the same firewall."""

    def form(self, **changes):
        """An edit of the configured firewall.

        Args:
            **changes: Fields added or overridden.

        Returns:
            dict: The form.
        """
        revision = read_settings(self.app.extensions["labrat_config_path"])[2]
        return {"_csrf_token": self.csrf, "revision": revision, "kind": "opnsense", "name": "edge", "is_new": "0",
                "display_name": "Edge", "url": "https://198.51.100.1:8443", "api_key": "", "api_secret": "",
                "tls_mode": "verify", "ca_bundle": "", "enabled": "1", **changes}

    def test_new_key_on_the_same_address_asks_nothing(self):
        """A rotated key keeps the integration and its source."""
        self.configure()
        self.poll()
        saved = self.client.post("/settings/integrations/save", data=self.form(api_key="key-new", api_secret="secret-new"))
        self.assertEqual(saved.status_code, 302)
        _raw, config, _revision = read_settings(self.app.extensions["labrat_config_path"])
        self.assertEqual(config["integrations"]["opnsense"]["edge"]["api_key"], "key-new")
        self.assertEqual(set(config["integrations"]["opnsense"]), {"edge"})

    def test_new_address_needs_an_answer(self):
        """Without "same" or "different", a new address is not saved."""
        self.configure()
        refused = self.client.post("/settings/integrations/save", data=self.form(
            url="https://198.51.100.9", api_key="key-1", api_secret="secret-1"))
        self.assertEqual(refused.status_code, 400)
        self.assertIn("same device at a new address", refused.get_data(as_text=True))

    def test_same_firewall_at_a_new_address(self):
        """"Same" moves the integration, keeping its source."""
        self.configure()
        self.poll()
        saved = self.client.post("/settings/integrations/save", data=self.form(
            url="https://198.51.100.9", api_key="key-1", api_secret="secret-1", replacement="same"))
        self.assertEqual(saved.status_code, 302)
        _raw, config, _revision = read_settings(self.app.extensions["labrat_config_path"])
        self.assertEqual(config["integrations"]["opnsense"]["edge"]["url"], "https://198.51.100.9")
        self.assertEqual(set(config["integrations"]["opnsense"]), {"edge"})

    def test_two_successive_replacements(self):
        """Replacing the replacement keeps both retired integrations and their observations."""
        self.configure()
        self.poll()
        first, first_inventory = self.device().id, self.link_inventory()
        saved = self.client.post("/settings/integrations/save", data=self.form(
            url="https://198.51.100.9", api_key="key-1", api_secret="secret-1", replacement="different"))
        self.assertEqual(saved.status_code, 302)
        self.poll(self.other_firewall(), name="edge-2")
        second, second_inventory = self.device("edge-2").id, self.link_inventory("edge-2")
        saved = self.client.post("/settings/integrations/save", data=self.form(
            name="edge-2", url="https://198.51.100.10", api_key="key-2", api_secret="secret-2", replacement="different"))
        self.assertEqual(saved.status_code, 302)
        _raw, config, _revision = read_settings(self.app.extensions["labrat_config_path"])
        family = config["integrations"]["opnsense"]
        self.assertEqual({name: settings["display_name"] for name, settings in family.items()},
                         {"edge": "Edge (replaced)", "edge-2": "Edge (replaced 2)", "edge-2-2": "Edge"})
        with self.factory() as db:
            sources = {row.config_name: row for row in db.scalars(select(DiscoverySource).where(DiscoverySource.kind == "opnsense"))}
            self.assertEqual({name: row.enabled for name, row in sources.items()},
                             {"edge": False, "edge-2": False, "edge-2-2": True})
            for observation, inventory, name in ((first, first_inventory, "edge"), (second, second_inventory, "edge-2")):
                kept = db.get(DiscoveredDevice, observation)
                self.assertEqual((kept.source_id, kept.device_id), (sources[name].id, inventory))

    def test_different_firewall_at_a_new_address_even_with_unchanged_evidence(self):
        """"Different" starts a new source; the old one keeps its observation and inventory link."""
        self.configure()
        self.poll()
        with self.factory.begin() as db:
            device = db.scalar(select(DiscoveredDevice).where(DiscoveredDevice.remote_id == FIREWALL))
            device_id = device.id
        saved = self.client.post("/settings/integrations/save", data=self.form(
            url="https://198.51.100.9", api_key="key-1", api_secret="secret-1", replacement="different"))
        self.assertEqual(saved.status_code, 302)
        _raw, config, _revision = read_settings(self.app.extensions["labrat_config_path"])
        family = config["integrations"]["opnsense"]
        self.assertEqual(family["edge"]["url"], "https://198.51.100.1:8443")
        self.assertEqual(family["edge"]["display_name"], "Edge (replaced)")
        self.assertEqual(family["edge-2"]["url"], "https://198.51.100.9")
        with self.factory() as db:
            sources = {row.config_name: row for row in db.scalars(select(DiscoverySource).where(DiscoverySource.kind == "opnsense"))}
            self.assertFalse(sources["edge"].enabled)
            self.assertTrue(sources["edge-2"].enabled)
            self.assertEqual(db.get(DiscoveredDevice, device_id).source_id, sources["edge"].id)


if __name__ == "__main__":
    unittest.main()
