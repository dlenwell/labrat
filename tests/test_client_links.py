# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Exercise targeted client-based cable inference without network access."""

import json
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

import requests
from urllib.parse import unquote
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import labrat.models  # Register canonical models referenced by discovery tables.
from labrat.database import Base
from labrat.collector.client_links import collect_client_links, reconcile_client_links
from labrat.discovery.models import DiscoverySource, DiscoveredDevice, DiscoveredPort, DiscoveredLink


class FakeAdapter:
    """Supply one switch client record for the requested physical NIC."""

    def __init__(self):
        """Script one wired classic client record and its lookups."""
        self.client = self
        self.record = {"mac": "00:11:22:33:44:55", "is_wired": True,
                       "last_seen": time.time(), "sw_port": 5,
                       "sw_mac": "aa:bb:cc:dd:ee:ff"}
        self.fail = False
        self.unknown = set()
        self.paths = []

    def _page(self, base, path, headers, verify):
        """Return the controller's one site.

        Args:
            base: The controller root (unused).
            path: The API path (unused).
            headers: Request headers (unused).
            verify: TLS verification (unused).

        Returns:
            list: The site.
        """
        return [{"id": "site", "internalReference": "default"}]

    def get_json(self, url, headers, verify):
        """Answer a classic station lookup, recording the URL.

        Args:
            url: The stat/user URL, ending in the MAC.
            headers: Request headers (unused).
            verify: TLS verification (unused).

        Returns:
            dict: The classic envelope with the record.

        Raises:
            requests.Timeout: When set to fail.
            requests.HTTPError: For a MAC the controller does not know.
        """
        self.paths.append(url)
        if self.fail:
            raise requests.Timeout()
        mac = unquote(url.rsplit("/", 1)[-1])
        if mac in self.unknown:
            response = requests.Response()
            response.status_code = 400
            message = "api.err.UnknownStation" if self.unknown_message is None else self.unknown_message
            response._content = ('{"meta":{"rc":"error","msg":"%s"},"data":[]}' % message).encode()
            raise requests.HTTPError(response=response)
        return {"data": [self.record]}

    unknown_message = None


class ClientLinkTests(unittest.TestCase):
    """Verify freshness, uniqueness, errors, and priority of link evidence."""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.unifi = DiscoverySource(name="unifi", kind="unifi", config_name="default")
        self.proxmox = DiscoverySource(name="proxmox", kind="proxmox", config_name="default")
        self.db.add_all([self.unifi, self.proxmox]); self.db.flush()
        switch = DiscoveredDevice(source_id=self.unifi.id, remote_id="switch", name="Switch", category="switch", identifiers_json='{"mac":"aa:bb:cc:dd:ee:ff"}', details_json='{"site_id":"site"}')
        server = DiscoveredDevice(source_id=self.proxmox.id, remote_id="server", name="Server", category="server")
        self.db.add_all([switch, server]); self.db.flush()
        self.nic = DiscoveredPort(discovered_device_id=server.id, remote_id="nic0", name="nic0", mac="00:11:22:33:44:55", link_up=True)
        self.switch_port = DiscoveredPort(discovered_device_id=switch.id, remote_id="5", name="Port 5", link_up=True)
        self.db.add_all([self.nic, self.switch_port]); self.db.flush()
        self.adapter = FakeAdapter()
        self.settings = {"url": "https://example.invalid", "api_key": "test"}

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def collect(self):
        """Run the client-link collection against the fakes.

        Returns:
            The collection's result.
        """
        return collect_client_links(self.db, self.unifi, [self.proxmox], self.adapter, self.settings)

    def test_fresh_unique_client_and_existing_link_priority(self):
        pair = tuple(sorted((self.nic.id, self.switch_port.id)))
        self.assertEqual(self.collect(), {pair})
        self.assertEqual(len(self.adapter.paths), 1)
        self.assertIn("00%3A11%3A22%3A33%3A44%3A55", self.adapter.paths[0])
        self.assertEqual(reconcile_client_links(self.db, self.unifi, {pair}), 1)
        self.db.flush()
        link = self.db.scalar(select(DiscoveredLink))
        self.assertEqual((link.evidence, link.confidence), ("unifi-client-mac", "inferred"))
        link.evidence = "lldp-proxmox"; link.confidence = "confirmed"; self.db.flush()
        self.assertEqual(reconcile_client_links(self.db, self.unifi, {pair}), 0)
        self.assertEqual(self.db.scalar(select(DiscoveredLink)).evidence, "lldp-proxmox")

    def test_stale_and_shared_mac_are_not_mapped(self):
        self.adapter.record["last_seen"] = time.time() - 1000
        self.assertEqual(self.collect(), set())
        self.adapter.record["last_seen"] = time.time()
        self.db.add(DiscoveredPort(discovered_device_id=self.nic.discovered_device_id, remote_id="nic1", name="nic1", mac=self.nic.mac))
        self.db.flush()
        self.assertEqual(self.collect(), set())

    def test_current_field_aliases_and_conflicts(self):
        """Accept equivalent API formats while rejecting contradictory ports."""
        self.adapter.record["sw_mac"] = ""
        self.adapter.record["sw_port"] = ""
        self.adapter.record["uplink"] = {"uplink_device_mac": "aa:bb:cc:dd:ee:ff", "port_idx": "Port 5"}
        self.assertEqual(len(self.collect()), 1)
        self.adapter.record["sw_port"] = 6
        self.assertEqual(self.collect(), set())
        self.adapter.record.pop("sw_port")
        self.adapter.record["last_uplink"] = self.adapter.record.pop("uplink")
        self.assertEqual(self.collect(), set())

    def test_response_time_is_used_for_freshness(self):
        """Accept a client refreshed while the controller request is in flight."""
        fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
        def fetch(url, headers, verify):
            clock.now.return_value = fixed
            self.adapter.record["last_seen"] = fixed.timestamp()
            return {"data": [self.adapter.record]}
        self.adapter.get_json = fetch
        with patch("labrat.collector.client_links.datetime") as clock:
            clock.now.return_value = fixed - timedelta(seconds=2)
            self.assertEqual(len(self.collect()), 1)

    def test_successful_empty_snapshot_removes_inference(self):
        """Remove a previous cable once a successful lookup no longer sees it."""
        reconcile_client_links(self.db, self.unifi, self.collect())
        self.db.flush()
        reconcile_client_links(self.db, self.unifi, set())
        self.assertIsNone(self.db.scalar(select(DiscoveredLink)))

    def test_controller_error_payload_does_not_clear_links(self):
        """Treat a classic API error envelope as unavailable, not empty data."""
        self.adapter.get_json = lambda *args: {"meta": {"rc": "error"}, "data": []}
        self.assertIsNone(self.collect())

    def unknown_nic(self):
        """Add a second server NIC that the controller has never seen."""
        server_id = self.nic.discovered_device_id
        self.db.add(DiscoveredPort(discovered_device_id=server_id, remote_id="nic1", name="nic1",
                                   mac="00:11:22:33:44:66", link_up=True)); self.db.flush()
        self.adapter.unknown = {"00:11:22:33:44:66"}

    def test_unknown_station_is_an_empty_answer(self):
        self.unknown_nic()
        self.assertEqual(self.collect(), {tuple(sorted((self.nic.id, self.switch_port.id)))})

    def test_other_client_errors_still_fail_the_lookup(self):
        self.unknown_nic()
        self.adapter.unknown_message = "api.err.NoPermission"
        self.assertIsNone(self.collect())

    def own_chassis(self, neighbors):
        """File the NIC behind its own server's LLDP chassis, as some controllers do.

        The server's other NIC (down, so never looked up) supplies the chassis
        MAC; the client record names it as the NIC's switch.

        Args:
            neighbors: The switch's ``lldp_neighbors`` details.

        Returns:
            DiscoveredDevice: The switch, current as of ``SNAPSHOT``.
        """
        if self.db.scalar(select(DiscoveredPort).where(DiscoveredPort.remote_id == "nic1")) is None:
            self.db.add(DiscoveredPort(discovered_device_id=self.nic.discovered_device_id, remote_id="nic1",
                                       name="nic1", mac="00:11:22:33:44:66", link_up=False))
        switch = self.db.get(DiscoveredDevice, self.switch_port.discovered_device_id)
        switch.details_json = json.dumps({"site_id": "site", "lldp_neighbors": neighbors})
        self.unifi.last_success_at = switch.last_seen_at = self.SNAPSHOT
        self.db.flush()
        self.adapter.record["sw_mac"] = "00:11:22:33:44:66"
        return switch

    SNAPSHOT = datetime(2026, 1, 2, tzinfo=timezone.utc)
    HEARD = {"chassis_id": "00:11:22:33:44:66", "port_id": "", "local_port": "5"}

    def second_switch(self, seen_at, neighbors):
        """Add another switch on the site whose LLDP table hears neighbors.

        Args:
            seen_at: When the source last reported it.
            neighbors: Its ``lldp_neighbors`` details.
        """
        other = DiscoveredDevice(source_id=self.unifi.id, remote_id="other-switch", name="Other", category="switch",
                                 identifiers_json='{"mac":"aa:bb:cc:dd:ee:01"}', last_seen_at=seen_at,
                                 details_json=json.dumps({"site_id": "site", "lldp_neighbors": neighbors}))
        self.db.add(other); self.db.flush()
        self.db.add(DiscoveredPort(discovered_device_id=other.id, remote_id="5", name="Port 5", link_up=True))
        self.db.flush()

    def test_nic_filed_behind_its_own_chassis_takes_the_port_hearing_it(self):
        """Resolve the controller's own-chassis answer through the switch's LLDP table."""
        self.own_chassis([{"chassis_id": "00:11:22:33:44:66", "port_id": "", "local_port": "5"}])
        self.assertEqual(self.collect(), {tuple(sorted((self.nic.id, self.switch_port.id)))})

    def test_own_chassis_needs_one_agreeing_port(self):
        """Refuse when LLDP disagrees with the record's port or hears the chassis twice."""
        self.own_chassis([{"chassis_id": "00:11:22:33:44:66", "port_id": "", "local_port": "6"}])
        self.assertEqual(self.collect(), set())
        self.own_chassis([{"chassis_id": "00:11:22:33:44:66", "port_id": "", "local_port": "5"},
                          {"chassis_id": "00:11:22:33:44:66", "port_id": "", "local_port": "6"}])
        self.assertEqual(self.collect(), set())
        self.own_chassis([])
        self.assertEqual(self.collect(), set())

    def test_chassis_of_another_server_is_not_resolved(self):
        """A NIC filed behind some other server's chassis is not this case."""
        self.own_chassis([{"chassis_id": "00:11:22:33:44:66", "port_id": "", "local_port": "5"}])
        other = DiscoveredDevice(source_id=self.proxmox.id, remote_id="other", name="Other", category="server")
        self.db.add(other); self.db.flush()
        nic1 = self.db.scalar(select(DiscoveredPort).where(DiscoveredPort.remote_id == "nic1"))
        nic1.discovered_device_id = other.id; self.db.flush()
        self.assertEqual(self.collect(), set())


    def test_own_chassis_uses_only_current_online_switches(self):
        """Ignore LLDP rows kept by a switch outside the latest snapshot or reported offline."""
        pair = {tuple(sorted((self.nic.id, self.switch_port.id)))}
        switch = self.own_chassis([self.HEARD])
        switch.last_seen_at = self.SNAPSHOT - timedelta(days=1); self.db.flush()
        self.assertEqual(self.collect(), set(), "a stale switch alone")
        switch.last_seen_at = self.SNAPSHOT
        switch.details_json = json.dumps({"site_id": "site", "state": "OFFLINE", "lldp_neighbors": [self.HEARD]})
        self.db.flush()
        self.assertEqual(self.collect(), set(), "an offline switch")
        self.own_chassis([self.HEARD])
        self.second_switch(self.SNAPSHOT - timedelta(days=1), [self.HEARD])
        self.assertEqual(self.collect(), pair, "a stale switch beside a current one")

    def test_repeated_rows_for_one_port_are_one_sighting(self):
        self.own_chassis([self.HEARD, dict(self.HEARD)])
        self.assertEqual(self.collect(), {tuple(sorted((self.nic.id, self.switch_port.id)))})

    def test_own_chassis_refused_when_lldp_names_another_nic(self):
        """A remote port ID naming the server's other NIC overrides the fallback."""
        pair = {tuple(sorted((self.nic.id, self.switch_port.id)))}
        for port_id in ("nic1", "ifname:NIC1", "00:11:22:33:44:66"):
            self.own_chassis([dict(self.HEARD, port_id=port_id)])
            self.assertEqual(self.collect(), set(), port_id)
        for port_id in ("nic0", "00:11:22:33:44:55", "unknown-port"):
            self.own_chassis([dict(self.HEARD, port_id=port_id)])
            self.assertEqual(self.collect(), pair, port_id)

    def test_timeout_retains_previous_inference(self):
        pair = tuple(sorted((self.nic.id, self.switch_port.id)))
        reconcile_client_links(self.db, self.unifi, {pair}); self.db.flush()
        self.adapter.fail = True
        self.assertIsNone(self.collect())
        reconcile_client_links(self.db, self.unifi, None)
        self.assertIsNotNone(self.db.scalar(select(DiscoveredLink)))


if __name__ == "__main__":
    unittest.main()
