# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Check OPNsense networks, gateways, DHCP, and client sightings (build step 3).

Answers come from ``tests/fixtures/opnsense``, recorded from the test bed
(``scripts/opnsense_testbed.py``) with pretend clients on its private
links: ``workstation`` (on ``opt1`` and its VLAN ``opt3``), ``phone`` (a
dnsmasq lease, never seen), ``nas`` (a dnsmasq reservation), and, from
the Kea pass, ``sensor`` and ``printer`` (Kea leases).
"""

import json
import unittest
from unittest.mock import patch

from sqlalchemy import select

from labrat.api.clients import scan_permission
from labrat.collector.adapters import opnsense
from labrat.collector.adapters.opnsense import (
    FIREWALL, project_arp, project_dnsmasq, project_gateways, project_ndp, project_routes, sightings,
)
from labrat.collector.sightings import EVIDENCE
from labrat.discovery.models import DiscoveredDevice, DiscoveredLink, DiscoveredPort, DiscoverySource
from test_opnsense import NICS, SETTINGS, StoredFirewall, adapter, answers

WORKSTATION, PHONE, NAS = "02:00:00:00:10:01", "02:00:00:00:10:02", "02:00:00:00:10:03"


def poll(replies=None):
    """Poll the scripted firewall with the adapter alone.

    Args:
        replies: The script; the recorded answers by default.

    Returns:
        OpnsenseAdapter: The adapter after its poll.
    """
    reader, _session = adapter(replies)
    reader.poll(SETTINGS)
    return reader


def by_key(report):
    """Sightings by scope and MAC.

    Args:
        report: The adapter's ``last_sightings``.

    Returns:
        dict: Each sighting by ``(scope, mac)``.
    """
    return {(item["scope"], item["mac"]): item for item in report["records"]}


class ProjectionTests(unittest.TestCase):
    """Each new read keeps only its named fields, and refuses the wrong shape."""

    def test_network_reads_are_complete_and_stored_apart(self):
        """The recorded firewall: every new section complete, under its own telemetry key."""
        reader = poll()
        network = reader.last_telemetry[FIREWALL]["opnsense_network"]
        self.assertTrue(all(item["status"] == "complete" for item in network["sections"].values()))
        self.assertNotIn("arp", network)  # Neighbours become sightings, not telemetry.
        self.assertEqual(network["routes"], [
            {"family": "ipv4", "gateway": "198.51.100.2", "device": "em1", "interface": "WAN"},
            {"family": "ipv6", "gateway": "fe80::2", "device": "em1", "interface": "WAN"}])
        self.assertEqual(network["dnsmasq"]["ranges"], [
            {"interface": "opt1", "start": "203.0.113.10", "end": "203.0.113.20", "domain": "lab.example"}])
        self.assertEqual(network["dnsmasq"]["reservations"], [{"hostname": "nas", "ip": "203.0.113.30", "mac": NAS}])
        [subnet] = network["kea_subnets"]
        self.assertEqual((subnet["subnet"], subnet["pools"], subnet["automatic"]),
                         ("203.0.113.64/26", ["203.0.113.70-203.0.113.80"], True))
        self.assertEqual(network["kea_reservations"][0]["hostname"], "printer")
        self.assertEqual(network["dns"], {"enabled": True, "port": 53, "interfaces": [], "dnssec": False,
                                          "forwarding": False, "forwarders": []})
        interfaces = {item["identifier"]: item for item in reader.last_telemetry[FIREWALL]["opnsense"]["interfaces"]
                      if item["identifier"]}
        self.assertEqual((interfaces["opt3"]["vlan"], interfaces["opt3"]["parent"]), (10, "em2"))
        self.assertEqual(interfaces["wan"]["gateways"], ["198.51.100.2", "fe80::2"])

    def test_unmonitored_gateways_have_no_readings(self):
        """``~`` is not monitored: no latency or loss, never zero; dpinger readings are numbers."""
        recorded = {item["name"]: item for item in project_gateways(answers()["routes/gateway/status"])[0]}
        self.assertEqual({(item["monitored"], item["delay_ms"], item["loss_pct"]) for name, item in recorded.items()
                          if name.startswith("WAN")}, {(False, None, None)})
        # The test bed's gateway to its peer firewall is monitored: real readings.
        self.assertTrue(recorded["PEER_GW"]["monitored"])
        self.assertIsInstance(recorded["PEER_GW"]["delay_ms"], float)
        [down], complete = project_gateways({"items": [{
            "name": "WAN_GW", "address": "192.0.2.254", "status": "down", "status_translated": "Offline",
            "monitor": "192.0.2.254", "delay": "12.5 ms", "stddev": "1.5 ms", "loss": "100.0 %"}]})
        self.assertTrue(complete)
        self.assertEqual((down["status"], down["monitored"], down["delay_ms"], down["loss_pct"]),
                         ("down", True, 12.5, 100.0))
        self.assertFalse(project_gateways({"items": [{"address": "192.0.2.1"}]})[1])

    def test_only_default_routes_are_kept(self):
        """The routing table is reduced to the default routes."""
        routes, complete = project_routes([{"destination": "192.0.2.0/24", "gateway": "link#1", "netif": "em0"},
                                           {"destination": "default", "gateway": "fe80::1%em1", "netif": "em1",
                                            "proto": "ipv6", "intf_description": "WAN"}])
        self.assertTrue(complete)
        self.assertEqual(routes, [{"family": "ipv6", "gateway": "fe80::1", "device": "em1", "interface": "WAN"}])

    def test_dnsmasq_dns_option_names_the_servers_handed_out(self):
        """A DHCP option 6 per interface is the DNS that interface's clients get."""
        settings = answers()["dnsmasq/settings/get"]
        settings["dnsmasq"]["dhcp_options"] = {"uuid-1": {
            "option": {"": {"selected": 0}, "6": {"selected": 1, "value": "dns-server [6]"}},
            "interface": {"opt1": {"selected": 1, "value": "DHCPLAB"}}, "value": "203.0.113.53,203.0.113.54"}}
        self.assertEqual(project_dnsmasq(settings)["dns"], {"opt1": ["203.0.113.53", "203.0.113.54"]})

    def test_wrong_shapes_fail_and_lists_are_capped(self):
        """A wrong shape is a failed section; a list over the cap is partial and ages nothing."""
        replies = answers()
        replies["dnsmasq/settings/get"] = {}
        replies["diagnostics/interface/search_arp"] = {"rows": "none"}
        replies["diagnostics/interface/get_routes"] = {"rows": []}
        sections = poll(replies).last_telemetry[FIREWALL]["opnsense_network"]["sections"]
        self.assertEqual({sections[name]["status"] for name in ("dnsmasq", "arp", "routes")}, {"failed"})
        with patch.object(opnsense, "MAX_ROWS", 1):
            reader = poll()
        self.assertEqual(reader.last_telemetry[FIREWALL]["opnsense_network"]["sections"]["arp"]["status"], "partial")
        self.assertNotIn("arp", reader.last_sightings["finished"])

    def test_neighbour_rows(self):
        """The firewall's own and incomplete ARP entries are skipped; NDP zones are dropped."""
        rows, complete = project_arp({"rows": [
            {"mac": "02:00:00:00:00:03", "ip": "203.0.113.1", "intf": "em2", "permanent": True},
            {"mac": "(incomplete)", "ip": "203.0.113.9", "intf": "em2", "expired": False},
            {"mac": WORKSTATION, "ip": "203.0.113.12", "intf": "em2", "permanent": False, "hostname": "ws"}]})
        self.assertTrue(complete)
        self.assertEqual([(row["mac"], row["ip"], row["hostname"]) for row in rows], [(WORKSTATION, "203.0.113.12", "ws")])
        self.assertEqual(project_ndp({"rows": [{"mac": WORKSTATION, "ip": "fe80::1%em2", "intf": "em2"}]})[0][0]["ip"],
                         "fe80::1")


class SightingTests(unittest.TestCase):
    """ARP, NDP, and leases join into one sighting per MAC per network."""

    def test_recorded_sightings(self):
        """One MAC on two networks is two sightings; a lease alone is not seen; WAN neighbours are skipped."""
        report = poll().last_sightings
        found = by_key(report)
        self.assertEqual(report["finished"], ["arp", "ndp", "dnsmasq_leases", "kea_leases"])
        self.assertEqual(found[("opt1", WORKSTATION)]["feeds"], ["arp", "dnsmasq_leases", "ndp"])
        self.assertTrue(found[("opt1", WORKSTATION)]["seen"])
        self.assertEqual(found[("opt1", WORKSTATION)]["lease"]["server"], "dnsmasq")
        self.assertEqual(found[("opt1", WORKSTATION)]["ipv6"], ["fe80::ff:fe00:1001"])  # ARP and NDP: one sighting.
        self.assertEqual(found[("opt3", WORKSTATION)]["ipv4"], ["203.0.113.140"])
        self.assertIsNone(found[("opt3", WORKSTATION)]["lease"])
        phone = found[("opt1", PHONE)]
        self.assertEqual((phone["seen"], phone["ipv4"], phone["lease"]["ip"]), (False, [], "203.0.113.13"))
        self.assertTrue(found[("opt1", NAS)]["lease"]["reserved"])
        self.assertTrue({("opt2", "02:00:00:00:20:01"), ("opt2", "02:00:00:00:20:02")} <= set(found))
        self.assertEqual(found[("opt2", "02:00:00:00:20:01")]["lease"]["server"], "kea")
        self.assertFalse(any(scope == "wan" for scope, _mac in found))
        own = {item["mac"] for item in poll().last_telemetry[FIREWALL]["opnsense"]["interfaces"] if item["mac"]}
        self.assertFalse(own & {mac for _scope, mac in found})

    def test_dual_stack_client(self):
        """ARP and NDP rows of one MAC on one interface are one sighting, global IPv6 first."""
        interfaces = [{"device": "em2", "identifier": "opt1", "description": "DHCPLAB", "mac": "02:00:00:00:00:03",
                       "gateways": []}]
        [item], mapped = sightings(interfaces, [{"mac": WORKSTATION, "ip": "203.0.113.12", "device": "em2"}],
                                   [{"mac": WORKSTATION, "ip": "fe80::ff:fe00:1001", "device": "em2"},
                                    {"mac": WORKSTATION, "ip": "2001:db8:1::a", "device": "em2"}], [])
        self.assertTrue(mapped)
        self.assertEqual((item["ipv4"], item["ipv6"]), (["203.0.113.12"], ["2001:db8:1::a", "fe80::ff:fe00:1001"]))

    def test_rows_without_a_known_interface_are_never_keyed_by_device(self):
        """No interface list, or an unknown interface: the row is skipped and the join is incomplete."""
        row = [{"mac": WORKSTATION, "ip": "203.0.113.12", "device": "em2"}]
        self.assertEqual(sightings(None, row, [], []), ([], False))
        self.assertEqual(sightings([{"device": "em0", "identifier": "lan", "gateways": []}], row, [], []), ([], False))
        replies = answers()
        replies["interfaces/overview/interfaces_info"] = ({}, 500)
        report = poll(replies).last_sightings
        self.assertEqual((report["records"], report["finished"]), ([], []))

    def test_only_full_reads_and_missing_dhcp_servers_finish(self):
        """A refused (403) or failed feed has not finished; a DHCP server's 404 (not installed) has."""
        replies = answers()
        replies["diagnostics/interface/search_ndp"] = ({}, 403)
        replies["diagnostics/interface/search_arp"] = ({}, 500)
        replies["kea/leases4/search"] = ({}, 404)
        replies["dnsmasq/leases/search"] = ({}, 403)
        self.assertEqual(poll(replies).last_sightings["finished"], ["kea_leases"])


class StoredSightingTests(StoredFirewall):
    """Sightings become client observations of the firewall's clients source."""

    def clients(self):
        """The stored sightings' client details by remote ID.

        Returns:
            dict: Client details by remote ID.
        """
        with self.factory() as db:
            source = db.scalar(select(DiscoverySource).where(DiscoverySource.kind == "opnsense_clients"))
            return {device.remote_id: json.loads(device.details_json)
                    for device in db.scalars(select(DiscoveredDevice).where(DiscoveredDevice.source_id == source.id))}

    def test_sightings_are_clients_never_attachments(self):
        """Each sighting is a client observation with no status, NIC, or cable."""
        self.configure()
        self.poll()
        stored = self.clients()
        workstation = stored["opt1:020000001001"]
        self.assertEqual(workstation["role"], "client")
        self.assertNotIn("status", workstation)
        self.assertEqual((workstation["client"]["state"], workstation["client"]["evidence"]), ("seen", EVIDENCE))
        self.assertEqual(stored["opt3:020000001001"]["client"]["ip_address"], "203.0.113.140")
        phone = stored["opt1:020000001002"]["client"]
        self.assertEqual((phone["state"], phone["seen_at"], phone["ip_address"], phone["lease"]["ip"]),
                         ("lease", None, None, "203.0.113.13"))
        with self.factory() as db:
            source = db.scalar(select(DiscoverySource).where(DiscoverySource.kind == "opnsense_clients"))
            self.assertEqual(source.config_name, "edge")
            devices = [device.id for device in db.scalars(select(DiscoveredDevice).where(
                DiscoveredDevice.source_id == source.id))]
            self.assertFalse(list(db.scalars(select(DiscoveredPort).where(DiscoveredPort.discovered_device_id.in_(devices)))))
            self.assertFalse(list(db.scalars(select(DiscoveredLink))))
        self.assertEqual(self.ports(), NICS)

    def test_failed_read_ages_nothing_then_an_empty_read_ages_everything(self):
        """complete → failed → complete and empty: the middle poll changes no state, the last ages all."""
        self.configure()
        self.poll()
        first = self.clients()
        failed = answers()
        failed["diagnostics/interface/search_arp"] = ({}, 503)
        failed["diagnostics/interface/search_ndp"] = requests_timeout()
        failed["dnsmasq/leases/search"] = requests_timeout()
        self.poll(failed)
        middle = self.clients()
        for remote_id, details in first.items():
            self.assertEqual(middle[remote_id]["client"]["state"], details["client"]["state"], remote_id)
            self.assertEqual(middle[remote_id]["client"]["seen_at"], details["client"]["seen_at"], remote_id)
        empty = answers()
        for path in ("diagnostics/interface/search_arp", "diagnostics/interface/search_ndp",
                     "dnsmasq/leases/search", "kea/leases4/search"):
            empty[path] = {"rows": [], "total": 0}
        self.poll(empty)
        last = self.clients()
        self.assertEqual({details["client"]["state"] for details in last.values()}, {"not_seen"})
        self.assertEqual(last["opt1:020000001001"]["client"]["seen_at"], first["opt1:020000001001"]["client"]["seen_at"])

    def test_a_sighting_that_leaves_arp_keeps_its_lease(self):
        """Out of ARP and NDP but still leased: a lease, its last sighting time kept."""
        self.configure()
        self.poll()
        seen_at = self.clients()["opt1:020000001001"]["client"]["seen_at"]
        gone = answers()
        for path in ("diagnostics/interface/search_arp", "diagnostics/interface/search_ndp"):
            gone[path]["rows"] = [row for row in gone[path]["rows"] if row["mac"] != WORKSTATION]
        self.poll(gone)
        stored = self.clients()
        self.assertEqual((stored["opt1:020000001001"]["client"]["state"], stored["opt1:020000001001"]["client"]["seen_at"]),
                         ("lease", seen_at))
        self.assertEqual(stored["opt3:020000001001"]["client"]["state"], "not_seen")

    def test_a_held_firewall_records_no_sightings(self):
        """While a different firewall is pending, its neighbours are not stored."""
        self.configure()
        self.poll()
        before = self.clients()
        other = self.other_firewall()
        other["diagnostics/interface/search_arp"]["rows"].append(
            {"mac": "02:00:00:00:30:01", "ip": "203.0.113.44", "intf": "em2", "permanent": False, "expired": False})
        self.poll(other)
        self.assertEqual(self.clients(), before)

    def test_retired_firewall_stops_its_clients(self):
        """Answering "different" disables the old clients source with its firewall."""
        self.configure()
        self.poll()
        self.poll(self.other_firewall())
        self.answer("different", self.shown_evidence())
        with self.factory() as db:
            source = db.scalar(select(DiscoverySource).where(
                DiscoverySource.kind == "opnsense_clients", DiscoverySource.config_name == "edge"))
            self.assertFalse(source.enabled)

    def test_network_api_serves_the_firewall(self):
        """The Networks API gives the firewall's networks, DHCP, gateways, and clients without verdicts."""
        self.configure()
        self.poll()
        [entry] = [item for item in self.client.get("/api/v1/network").get_json()["sources"] if item.get("kind") == "opnsense"]
        network = entry["network"]
        self.assertEqual(network["capabilities"], {"verdicts": False})
        by_name = {item["name"]: item for item in network["networks"]}
        # The peer link has a gateway (to the peer firewall): upstream, so not a network.
        self.assertEqual(sorted(by_name), ["DHCPLAB", "KEALAB", "LABVLAN", "LAN"])
        self.assertEqual(by_name["DHCPLAB"]["dhcp"]["mode"], "dnsmasq")
        self.assertEqual((by_name["KEALAB"]["dhcp"]["start"], by_name["KEALAB"]["dhcp"]["stop"]),
                         ("203.0.113.70", "203.0.113.80"))
        self.assertEqual((by_name["LABVLAN"]["vlan"], by_name["LABVLAN"]["cidr"]), (10, "203.0.113.128/26"))
        self.assertEqual(by_name["LAN"]["id"], f"opnsense:{entry['id']}:network:lan")
        gateways = {item["name"]: item for item in network["gateways"]}
        self.assertEqual({name: (item["interface"], item["default"]) for name, item in gateways.items()},
                         {"WAN_DHCP": ("WAN", True), "WAN_DHCP6": ("WAN", True), "PEER_GW": ("PEERLINK", False)})
        self.assertEqual(network["sections"]["policies"]["status"], "complete")
        self.assertEqual(network["sections"]["wifi"]["status"], "not_available")
        self.assertEqual(network["sections"]["clients"]["status"], "complete")
        states = {(item["mac"], item["network"]): item["state"] for item in network["clients"]}
        self.assertEqual(states[(PHONE, "DHCPLAB")], "lease")
        self.assertEqual(states[(WORKSTATION, "LABVLAN")], "seen")
        self.assertEqual(network["devices"][0]["roles"], ["gateway"])

    def test_clients_api_lists_sightings_and_checks_their_source(self):
        """The Discovery clients list shows sightings; the port check needs a current one."""
        self.configure()
        self.poll()
        graph = self.client.get("/api/v1/clients").get_json()
        found = {observation["client"]["remote_id"]: observation["client"] for node in graph["clients"]
                 for observation in node["observations"] if observation.get("client")}
        self.assertTrue(found["opt1:020000001001"]["source_enabled"])
        self.assertIn("opt1:020000001002", found)
        with self.factory() as db:
            source = db.scalar(select(DiscoverySource).where(DiscoverySource.kind == "opnsense_clients"))
            config = {"integrations": {"opnsense": {"edge": SETTINGS}}, "scan": {"ranges": ["203.0.113.0/24"]}}
            self.assertEqual(scan_permission(found["opt1:020000001001"], source, db, config)[0], "203.0.113.12")
            self.assertIn("no longer current", scan_permission(found["opt1:020000001002"], source, db, config)[1])
            self.assertIn("disabled", scan_permission(found["opt1:020000001001"], source, db, {})[1])
            # Seen by NDP only: its IPv4 address comes from the lease, unconfirmed.
            leased = {**found["opt1:020000001001"], "ipv4": []}
            self.assertIn("no longer current", scan_permission(leased, source, db, config)[1])

    def test_a_refused_feed_ages_nothing_it_reported(self):
        """seen → ARP refused (NDP and leases fine) → ARP empty: the middle poll keeps ARP's sightings."""
        self.configure()
        self.poll()
        first = self.clients()
        refused = answers()
        refused["diagnostics/interface/search_arp"] = ({}, 403)
        self.poll(refused)
        middle = self.clients()
        for remote_id in ("opt1:020000001003", "opt3:020000001001"):  # Seen by ARP only.
            for key in ("state", "seen_at", "ip_address", "ipv4"):
                self.assertEqual(middle[remote_id]["client"][key], first[remote_id]["client"][key], (remote_id, key))
        empty = answers()
        empty["diagnostics/interface/search_arp"] = {"rows": [], "total": 0}
        self.poll(empty)
        last = self.clients()
        nas = last["opt1:020000001003"]["client"]
        self.assertEqual((nas["state"], nas["ip_address"], nas["lease"]["ip"], nas["seen_at"]),
                         ("lease", None, "203.0.113.30", first["opt1:020000001003"]["client"]["seen_at"]))
        self.assertEqual(last["opt3:020000001001"]["client"]["state"], "not_seen")
        self.assertEqual(last["opt1:020000001001"]["client"]["state"], "seen")  # NDP still sees it.

    def test_addresses_come_only_from_what_saw_them(self):
        """Seen by NDP with an IPv4 lease: no IPv4 address in use; a partial read keeps the seen address."""
        self.configure()
        ndp_only = answers()
        ndp_only["diagnostics/interface/search_arp"]["rows"] = [
            row for row in ndp_only["diagnostics/interface/search_arp"]["rows"] if row["mac"] != WORKSTATION]
        self.poll(ndp_only)
        workstation = self.clients()["opt1:020000001001"]["client"]
        self.assertEqual((workstation["state"], workstation["ip_address"], workstation["ipv6"], workstation["lease"]["ip"]),
                         ("seen", None, ["fe80::ff:fe00:1001"], "203.0.113.12"))
        self.poll()
        changed = answers()
        changed["diagnostics/interface/search_arp"] = ({}, 503)
        for row in changed["dnsmasq/leases/search"]["rows"]:
            if row["hwaddr"] == NAS:
                row["address"] = "203.0.113.31"
        self.poll(changed)
        nas = self.clients()["opt1:020000001003"]["client"]
        self.assertEqual((nas["state"], nas["ip_address"], nas["lease"]["ip"]), ("seen", "203.0.113.30", "203.0.113.31"))

    def test_a_switched_off_dnsmasq_does_not_mask_kea(self):
        """A saved range of a switched-off dnsmasq is ignored; a Kea subnet without leases is only configured."""
        self.configure()
        replies = answers()
        replies["dnsmasq/settings/get"]["dnsmasq"]["enable"] = "0"
        replies["kea/dhcpv4/search_subnet"]["rows"].append(
            {**replies["kea/dhcpv4/search_subnet"]["rows"][0], "uuid": "kea-opt1", "subnet": "203.0.113.0/26",
             "pools": "203.0.113.40-203.0.113.50", "option_data.domain_name_servers": "203.0.113.53"})
        self.poll(replies)
        [entry] = [item for item in self.client.get("/api/v1/network").get_json()["sources"] if item.get("kind") == "opnsense"]
        by_name = {item["name"]: item for item in entry["network"]["networks"]}
        self.assertEqual((by_name["DHCPLAB"]["dhcp"]["mode"], by_name["DHCPLAB"]["dhcp"]["serving"]), ("kea", False))
        self.assertEqual((by_name["KEALAB"]["dhcp"]["mode"], by_name["KEALAB"]["dhcp"]["serving"]), ("kea", True))
        self.assertFalse(any("203.0.113.53" == item["ip"] for item in entry["network"]["sinkholes"]))

    def kealab_dhcp(self):
        """The Kea network's DHCP as the Networks API shows it.

        Returns:
            dict: Its ``dhcp``.
        """
        [entry] = [item for item in self.client.get("/api/v1/network").get_json()["sources"] if item.get("kind") == "opnsense"]
        return {item["name"]: item for item in entry["network"]["networks"]}["KEALAB"]["dhcp"]

    def test_only_a_current_kea_lease_shows_kea_serving(self):
        """A Kea lease that disappears, has expired, or predates a failed poll proves nothing now."""
        self.configure()
        self.poll()
        self.assertTrue(self.kealab_dhcp()["serving"])
        # The same snapshot read after the lease expired, with no new poll.
        expiry = min(row["expire"] for row in answers()["kea/leases4/search"]["rows"])
        with patch("labrat.api.network_shape.now", return_value=expiry + 1):
            self.assertFalse(self.kealab_dhcp()["serving"])
        expired = answers()
        for row in expired["kea/leases4/search"]["rows"]:
            row["expire"] = 1000
        self.poll(expired)
        self.assertFalse(self.kealab_dhcp()["serving"])
        unknown = answers()
        for row in unknown["kea/leases4/search"]["rows"]:
            row["expire"] = None
        self.poll(unknown)
        self.assertFalse(self.kealab_dhcp()["serving"])
        self.poll()
        gone = answers()
        gone["kea/leases4/search"] = {"rows": [], "total": 0}
        self.poll(gone)  # The sightings keep their old Kea leases as history.
        self.assertFalse(self.kealab_dhcp()["serving"])
        self.poll()
        failed = answers()
        failed["diagnostics/system/system_information"] = ({}, 500)
        self.poll(failed)
        self.assertFalse(self.kealab_dhcp()["serving"])
        self.assertEqual(self.kealab_dhcp()["mode"], "kea")


def requests_timeout():
    """A request that times out.

    Returns:
        Exception: The scripted failure.
    """
    import requests
    return requests.Timeout("timed out")
