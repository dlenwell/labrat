# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Check OPNsense rules, aliases, groups, VPN, DNS blocklist, services, and CARP (build step 4).

Answers come from ``tests/fixtures/opnsense``, recorded from the test bed
after ``scripts/opnsense_testbed.py firewall``: a floating rule, a
non-quick pass followed by a quick block of the same traffic, a rule on
an interface group, overlapping groups (``opt1`` in both), an old-format
rule, a URL table alias behind dummy credentials, a DNS blocklist of
custom domains, and a WireGuard tunnel to a peer firewall (keepalive on)
beside a peer that never connected.
"""

import json
import unittest

from labrat.api.network_shape import from_opnsense
from labrat.collector.adapters.opnsense import (
    FIREWALL, OpnsenseAdapter, project_blocklist_enabled, project_rules,
)
from test_opnsense import SETTINGS, StoredFirewall, adapter, answers


def reading(replies=None):
    """Poll the scripted firewall and return its stored telemetry.

    Args:
        replies: The script; the recorded answers by default.

    Returns:
        dict: The firewall's telemetry by key.
    """
    reader, _session = adapter(replies)
    reader.poll(SETTINGS)
    return reader.last_telemetry[FIREWALL]


def shared(replies=None):
    """The recorded firewall in the shared form.

    Args:
        replies: The script; the recorded answers by default.

    Returns:
        dict: The shared form.
    """
    telemetry = reading(replies)
    return from_opnsense(3, "opnsense:Edge", telemetry["opnsense"], telemetry["opnsense_network"], [], [], True)


class RuleTests(unittest.TestCase):
    """Rules are kept as reported, in evaluation order, with their stage."""

    def test_recorded_rules(self):
        """Floating, group, and interface rules in order; old-format and system rules marked."""
        rules = reading()["opnsense_network"]["rules"]
        own = [rule for rule in rules if not rule["automatic"]]
        self.assertEqual([(rule["scope"], rule["description"]) for rule in own], [
            ("floating", "Floating: block one bad host"), ("group", "Group: HTTPS to lab servers"),
            ("interface", "Default allow LAN to any rule"), ("interface", "Default allow LAN IPv6 to any rule"),
            ("interface", "Lab: allow out (not quick)"), ("interface", "Lab: no SSH to the Kea lab (quick)"),
            ("interface", "Lab links")])
        by_name = {rule["description"]: rule for rule in own}
        self.assertIs(by_name["Lab: allow out (not quick)"]["quick"], False)
        self.assertIs(by_name["Lab: no SSH to the Kea lab (quick)"]["quick"], True)
        self.assertEqual(by_name["Lab: no SSH to the Kea lab (quick)"]["destination"],
                         {"net": "203.0.113.64/26", "port": "22", "not": False})
        legacy = by_name["Lab links"]
        self.assertEqual((legacy["legacy"], legacy["automatic"], legacy["quick"], legacy["ref"]),
                         (True, False, None, "ui/firewall/migration"))
        self.assertTrue(all(rule["scope"] == "system" or rule["order"] >= "200000" for rule in rules))
        self.assertTrue(any(rule["automatic"] and rule["scope"] == "system" for rule in rules))
        self.assertEqual([rule["order"] for rule in rules], sorted(rule["order"] for rule in rules))

    def test_rows_without_an_order_make_the_list_partial(self):
        """A rule with no place in the evaluation order is skipped, and the list is partial."""
        rules, complete = project_rules({"rows": [{"uuid": "x", "action": "pass"}]})
        self.assertEqual((rules, complete), ([], False))

    def test_shared_form_has_no_verdicts(self):
        """Rules become policies by stage, interfaces resolved; no verdict or path is produced."""
        network = shared()
        self.assertEqual(network["capabilities"], {"verdicts": False})
        self.assertEqual(network["paths"], [])
        own = {policy["name"]: policy for policy in network["policies"] if policy["origin"] == "user"}
        self.assertEqual(own["Lab links"]["interfaces"], ["opt1", "opt2", "opt3", "opt4"])
        self.assertTrue(own["Lab links"]["legacy"])
        self.assertEqual(own["Floating: block one bad host"]["interface_names"], ["DHCPLAB", "KEALAB"])
        self.assertEqual(own["Group: HTTPS to lab servers"]["destination"]["values"], ["lab_servers"])
        self.assertEqual(network["sections"]["policies"]["status"], "complete")
        self.assertNotIn("verdict", json.dumps(network["policies"]))

    def test_interface_groups_are_zones(self):
        """Groups with members are zones (opt1 in both); interfaces in no group have their own."""
        zones = {zone["name"]: zone for zone in shared()["zones"]}
        self.assertEqual(sorted(zones), ["LAN", "PEERLINK", "WAN", "dhcpnets", "labnets"])
        self.assertEqual([member.rsplit(":", 1)[1] for member in zones["dhcpnets"]["networks"]], ["opt1", "opt2"])
        self.assertEqual([member.rsplit(":", 1)[1] for member in zones["labnets"]["networks"]], ["opt1", "opt3"])
        networks = {item["name"]: item["zone"].rsplit(":", 1)[1] for item in shared()["networks"]}
        self.assertEqual(networks["LABVLAN"], "labnets")
        self.assertEqual(networks["LAN"], "lan")

    def test_failed_rules_keep_their_last_good_reading(self):
        """A failed rule read keeps the last good rules and their time."""
        before = reading()["opnsense_network"]
        replies = answers()
        replies["firewall/filter/search_rule"] = ({}, 503)
        now = reading(replies)["opnsense_network"]
        merged = OpnsenseAdapter.merge_telemetry("opnsense_network", before, now)
        self.assertEqual(merged["rules"], before["rules"])
        self.assertEqual(merged["sections"]["rules"]["status"], "failed")
        self.assertEqual(merged["sections"]["rules"]["sampled_at"], before["sections"]["rules"]["sampled_at"])


class SecretTests(unittest.TestCase):
    """Alias content and credentials, and blocklist domains and URLs, are never kept."""

    def test_aliases_keep_names_and_sizes_only(self):
        """The URL table's user name and password, and every alias's content, are dropped."""
        network = reading()["opnsense_network"]
        stored = json.dumps(network)
        for value in ("testbed-alias-password", "testbed-user", "https://127.0.0.1/blocked.txt", "203.0.113.30\\n"):
            self.assertNotIn(value, stored)
        aliases = {alias["name"]: alias for alias in network["aliases"]}
        self.assertEqual(aliases["lab_servers"], {"name": "lab_servers", "type": "host", "description": "Reserved lab hosts",
                                                  "enabled": True, "items": 2})
        self.assertEqual(aliases["remote_blocklist"]["type"], "urltable")

    def test_blocklists_keep_names_and_counts(self):
        """A blocklist keeps how many domains and URLs, never which."""
        network = reading()["opnsense_network"]
        self.assertIs(network["blocklist_enabled"], True)
        self.assertEqual(network["blocklists"], [{"description": "Lab blocklist", "enabled": True, "lists": [], "urls": 0,
                                                  "domains": 3, "allowed": 0}])
        self.assertNotIn("ads.example", json.dumps(network))
        with self.assertRaises(ValueError):
            project_blocklist_enabled({"enabled": "yes"})


class VpnServiceTests(unittest.TestCase):
    """WireGuard peers, services, CARP, and the firewall as a DNS sinkhole."""

    def test_wireguard_peers(self):
        """The tunnel's peer has a handshake and keepalive; the idle peer never connected."""
        wireguard = reading()["opnsense_network"]["wireguard"]
        self.assertEqual([item["name"] for item in wireguard["instances"]], ["wg_lab"])
        peers = {peer["name"]: peer for peer in wireguard["peers"]}
        tunnel = peers["peer-firewall"]
        self.assertEqual((tunnel["endpoint"], tunnel["keepalive"], tunnel["allowed_ips"]),
                         ("203.0.113.194:51820", 25, ["203.0.113.246/32"]))
        self.assertIsInstance(tunnel["handshake"], int)
        idle = peers["road-warrior"]
        self.assertEqual((idle["endpoint"], idle["handshake"], idle["keepalive"]), (None, None, 0))
        self.assertNotIn("private", json.dumps(wireguard).lower())
        vpn = {item["name"]: item for item in shared()["vpn"]}
        self.assertNotIn("public_key", vpn["peer-firewall"])
        self.assertEqual(vpn["road-warrior"]["handshake"], None)

    def test_services_and_carp(self):
        """Running services are listed; the recorded CARP virtual IP is master."""
        network = reading()["opnsense_network"]
        services = {service["id"]: service for service in network["services"]}
        self.assertTrue(services["unbound"]["running"])
        # The test bed's virtual IP on the peer link, master (the peer is backup).
        self.assertEqual(network["carp"]["addresses"], [{"interface": "PEERLINK", "vhid": 1, "address": "203.0.113.198",
                                                         "mode": "carp", "state": "master"}])
        self.assertEqual([(vip["vhid"], vip["state"], vip["aliases"]) for vip in shared()["carp"]["vips"]],
                         [(1, "master", [])])

    def test_carp_addresses_by_vhid(self):
        """A CARP address carries its VHID's state; an IP alias on it is not a node of its own.

        The answer is built as OPNsense 26.7 builds it (``getVipStatusAction``):
        one row per address on a VHID, ``mode`` ``carp`` or ``ipalias``, and
        disabled virtual IPs with status ``DISABLED``.
        """
        replies = answers()
        replies["diagnostics/interface/get_vip_status"] = {"total": 4, "rowCount": 4, "current": 1, "rows": [
            {"interface": "LAN", "vhid": "1", "advbase": "1", "advskew": "0", "subnet": "192.0.2.254",
             "status": "MASTER", "mode": "carp"},
            {"interface": "LAN", "vhid": "1", "advbase": "1", "advskew": "0", "subnet": "192.0.2.253",
             "status": "MASTER", "mode": "ipalias"},
            {"interface": "opt1", "vhid": "2", "advbase": "1", "advskew": "100", "subnet": "203.0.113.62",
             "status": "DISABLED", "mode": "carp"}],
            "carp": {"demotion": "0", "allow": "1", "maintenancemode": False, "status_msg": ""}}
        telemetry = reading(replies)["opnsense_network"]
        self.assertEqual(telemetry["sections"]["carp"]["status"], "complete")
        carp = shared(replies)["carp"]
        self.assertEqual([(vip["vhid"], vip["state"], vip["aliases"]) for vip in carp["vips"]],
                         [(1, "master", ["192.0.2.253"]), (2, "disabled", [])])
        broken = answers()
        broken["diagnostics/interface/get_vip_status"] = {**replies["diagnostics/interface/get_vip_status"],
                                                           "rows": [*replies["diagnostics/interface/get_vip_status"]["rows"],
                                                                    {"interface": "LAN", "subnet": "192.0.2.9"}, "x"]}
        self.assertEqual(reading(broken)["opnsense_network"]["sections"]["carp"]["status"], "partial")

    def test_a_peer_is_its_key_not_its_name(self):
        """Renaming a peer keeps its ID; two peers with one name get two IDs; no key is exposed."""
        before = {peer["name"]: peer["id"] for peer in shared()["vpn"]}
        replies = answers()
        rows = replies["wireguard/service/show"]["rows"]
        for row in rows:
            if row.get("name") == "peer-firewall":
                key = row["public-key"]
                row["name"] = "renamed"
            elif row.get("type") == "peer":
                row["name"] = "renamed"
        after = shared(replies)["vpn"]
        self.assertEqual(len({peer["id"] for peer in after}), 2)
        self.assertIn(before["peer-firewall"], {peer["id"] for peer in after})
        self.assertNotIn(key, json.dumps(after))

    def test_the_firewall_is_a_sinkhole_while_its_blocklist_is_on(self):
        """An enabled blocklist adds the firewall to the sinkholes; a disabled one does not."""
        [entry] = shared()["sinkholes"]
        self.assertEqual((entry["kind"], entry["state"], entry["names"], entry["blocklists"][0]["domains"]),
                         ("opnsense", "enforcing", ["OPNsense.internal"], 3))
        self.assertIn("DHCPLAB", entry["networks"])
        replies = answers()
        replies["unbound/overview/is_block_list_enabled"] = {"enabled": False}
        self.assertEqual(shared(replies)["sinkholes"], [])

    def test_a_blocklist_is_enforced_only_by_a_running_unbound(self):
        """Unbound off or stopped: configured, no DHCP claim; a stale reading: last known."""
        off = answers()
        off["unbound/settings/get"]["unbound"]["general"]["enabled"] = "0"
        stopped = answers()
        for row in stopped["core/service/search"]["rows"]:
            if row["id"] == "unbound":
                row["running"] = 0
        for replies in (off, stopped):
            [entry] = shared(replies)["sinkholes"]
            self.assertEqual((entry["state"], entry["networks"]), ("configured", []))
            self.assertIn("nothing is blocked", entry["evidence"][0])
        failed = answers()
        failed["core/service/search"] = ({}, 503)
        telemetry = reading(failed)
        before = reading()["opnsense_network"]
        merged = OpnsenseAdapter.merge_telemetry("opnsense_network", before, telemetry["opnsense_network"])
        [entry] = from_opnsense(3, "Edge", telemetry["opnsense"], merged, [], [], True)["sinkholes"]
        self.assertEqual((entry["state"], entry["networks"]), ("last_known", []))
        telemetry = reading()
        [entry] = from_opnsense(3, "Edge", telemetry["opnsense"], telemetry["opnsense_network"], [], [], False)["sinkholes"]
        self.assertEqual(entry["state"], "last_known")


class StoredFirewallTests(StoredFirewall):
    """Through the collector and the Networks API."""

    def test_network_api_serves_rules_vpn_and_blocklist(self):
        """The API gives rules by stage, WireGuard peers, services, and the blocklist; no secrets."""
        self.configure()
        self.poll()
        answer = self.client.get("/api/v1/network").get_data(as_text=True)
        for value in ("testbed-alias-password", "testbed-user", "ads.example", "telemetry.example"):
            self.assertNotIn(value, answer)
        [entry] = [item for item in json.loads(answer)["sources"] if item.get("kind") == "opnsense"]
        network = entry["network"]
        self.assertEqual({policy["scope"] for policy in network["policies"]}, {"floating", "group", "interface", "system"})
        self.assertEqual(sorted(peer["name"] for peer in network["vpn"]), ["peer-firewall", "road-warrior"])
        self.assertTrue(network["services"])
        self.assertEqual(network["sinkholes"][0]["kind"], "opnsense")
