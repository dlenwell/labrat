# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Exercise report-based Proxmox NIC MACs and chassis-ID host matching."""

import json
import unittest

import requests
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import labrat.models  # Register canonical models referenced by discovery tables.
from labrat.collector.adapters.proxmox import ProxmoxAdapter
from labrat.collector.links import reconcile_proxmox_lldp_links
from labrat.collector.proxmox_report import interface_macs
from labrat.database import Base
from labrat.discovery.models import DiscoveredDevice, DiscoveredLink, DiscoveredPort, DiscoverySource

BASE = "https://pve.example:8006/api2/json"
SETTINGS = {"url": "https://pve.example:8006", "token_id": "a@pve!ro", "token_secret": "not-real"}

#: Shaped like ``ip -details link`` output inside a pvereport.
REPORT = """# ip -details -statistics link
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN
    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
2: eno1: <BROADCAST,MULTICAST,SLAVE,UP,LOWER_UP> mtu 1500 master bond0 state UP
    link/ether 02:00:00:00:00:01 brd ff:ff:ff:ff:ff:ff permaddr 02:00:00:00:00:0a promiscuity 0
    bond_slave state ACTIVE mii_status UP
    altname enp1s0f0
3: eno2: <BROADCAST,MULTICAST,SLAVE,UP,LOWER_UP> mtu 1500 master bond0 state UP
    link/ether 02:00:00:00:00:01 brd ff:ff:ff:ff:ff:ff promiscuity 0
4: vlan20@bond0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
    link/ether 02:00:00:00:00:01 brd ff:ff:ff:ff:ff:ff
5: eno3: <BROADCAST,MULTICAST> mtu 1500 state DOWN

# cat /etc/hosts
    link/ether 02:00:00:00:00:99 brd ff:ff:ff:ff:ff:ff
"""


class FakeClient:
    """Serve canned Proxmox responses and record requested paths."""

    def __init__(self, report):
        """Serve a node, its interfaces, and a scripted report.

        Args:
            report: The report text, or an exception to raise.
        """
        self.report = report
        self.paths = []

    def get_json(self, url, headers, verify):
        """Answer one Proxmox API read, recording its path.

        Args:
            url: The requested URL.
            headers: Request headers (unused).
            verify: TLS verification (unused).

        Returns:
            dict: The API envelope for that path.

        Raises:
            Exception: The scripted report exception, or AssertionError for a
                path the fake does not know.
        """
        self.paths.append(url[len(BASE):])
        if url == f"{BASE}/cluster/status":
            return {"data": []}
        if url == f"{BASE}/nodes":
            return {"data": [{"node": "one", "status": "online"}]}
        if url == f"{BASE}/nodes/one/network":
            return {"data": [
                {"iface": "eno1", "type": "eth", "exists": 1},
                {"iface": "eno2", "type": "eth", "exists": 1},
                {"iface": "eno3", "type": "eth", "exists": 1, "hwaddress": "02:00:00:00:00:0c"},
            ]}
        if url == f"{BASE}/nodes/one/report":
            if isinstance(self.report, Exception):
                raise self.report
            return {"data": self.report}
        raise AssertionError(f"Unexpected API path: {url}")


class ReportParserTests(unittest.TestCase):
    """Read only interface blocks, preferring each NIC's own address."""

    def test_permanent_address_altnames_and_parents(self):
        macs = interface_macs(REPORT)
        self.assertEqual(macs["eno1"], "02:00:00:00:00:0a")
        self.assertEqual(macs["enp1s0f0"], "02:00:00:00:00:0a")
        self.assertEqual(macs["eno2"], "02:00:00:00:00:01")
        self.assertEqual(macs["vlan20"], "02:00:00:00:00:01")
        self.assertNotIn("lo", macs)

    def test_current_addresses_are_kept_separate_from_permanent_nic_identity(self):
        """Bond member runtime MACs identify the host, never replace NIC identity."""
        current = interface_macs(REPORT, permanent=False)
        self.assertEqual(current["eno1"], "02:00:00:00:00:01")
        self.assertEqual(interface_macs(REPORT)["eno1"], "02:00:00:00:00:0a")

    def test_address_from_a_later_section_is_not_borrowed(self):
        self.assertNotIn("eno3", interface_macs(REPORT))

    def test_conflicting_addresses_are_dropped(self):
        report = ("2: eno1: <UP> mtu 1500\n    link/ether 02:00:00:00:00:01 brd x\n"
                  "7: eno1: <UP> mtu 1500\n    link/ether 02:00:00:00:00:02 brd x\n")
        self.assertEqual(interface_macs(report), {})


class AdapterReportTests(unittest.TestCase):
    """Fill missing MACs from the report on a bounded schedule."""

    def setUp(self):
        self.now = 1000.0

    def adapter(self, report):
        """Build an adapter on a fake client serving this report.

        Args:
            report: The report text, or an exception to raise.

        Returns:
            tuple: The adapter and its fake client.
        """
        client = FakeClient(report)
        return ProxmoxAdapter(client, clock=lambda: self.now), client

    def macs(self, batch):
        """Map each port in a batch to its MAC.

        Args:
            batch: The adapter's batch.

        Returns:
            dict: MACs by port name.
        """
        return {port.name: port.mac for port in batch.ports}

    def test_report_fills_missing_macs_and_api_values_win(self):
        adapter, _ = self.adapter(REPORT)
        self.assertEqual(self.macs(adapter.poll(SETTINGS)), {
            "eno1": "02:00:00:00:00:0a", "eno2": "02:00:00:00:00:01",
            "eno3": "02:00:00:00:00:0c",
        })

    def test_logical_mac_refresh_when_physical_macs_are_complete(self):
        """A bond without API MAC still triggers the cached runtime report."""
        adapter, client = self.adapter(REPORT)
        client.report += "\n6: bond0: <BROADCAST,MASTER,UP> mtu 1500\n    link/ether 02:00:00:00:00:01 brd ff:ff:ff:ff:ff:ff\n"
        original = client.get_json

        def get_json(url, headers, verify):
            if url.endswith("/network"):
                return {"data": [
                    {"iface": "eno1", "type": "eth", "exists": 1, "hwaddress": "02:00:00:00:00:0a"},
                    {"iface": "bond0", "type": "bond", "slaves": "eno1"},
                ]}
            return original(url, headers, verify)

        client.get_json = get_json
        batch = adapter.poll(SETTINGS)
        self.assertIn("/nodes/one/report", client.paths)
        identities = {(item["name"], item["mac"]) for item in batch.devices[0].details["host_interfaces"]}
        self.assertIn(("bond0", "02:00:00:00:00:01"), identities)
        self.assertIn(("eno1", "02:00:00:00:00:01"), identities)
        self.assertIn(("eno1", "02:00:00:00:00:0a"), identities)
        self.assertNotIn("vlan20", {name for name, _ in identities})
        self.assertEqual([port.name for port in batch.ports], ["eno1"])
        self.assertEqual(batch.ports[0].mac, "02:00:00:00:00:0a")

    def test_report_is_read_once_per_interval(self):
        adapter, client = self.adapter(REPORT)
        adapter.poll(SETTINGS)
        self.now += 3599
        adapter.poll(SETTINGS)
        self.assertEqual(client.paths.count("/nodes/one/report"), 1)
        self.now += 2
        adapter.poll(SETTINGS)
        self.assertEqual(client.paths.count("/nodes/one/report"), 2)

    def test_failure_keeps_previous_macs_and_waits_an_interval(self):
        adapter, client = self.adapter(REPORT)
        adapter.poll(SETTINGS)
        client.report = requests.Timeout()
        self.now += 4000
        self.assertEqual(self.macs(adapter.poll(SETTINGS))["eno1"], "02:00:00:00:00:0a")
        self.now += 60
        adapter.poll(SETTINGS)
        self.assertEqual(client.paths.count("/nodes/one/report"), 2)

    def test_first_failure_does_not_fail_discovery(self):
        adapter, _ = self.adapter(requests.HTTPError("403"))
        self.assertEqual(self.macs(adapter.poll(SETTINGS))["eno1"], None)

    def test_zero_disables_and_bad_values_are_rejected(self):
        adapter, client = self.adapter(REPORT)
        adapter.poll({**SETTINGS, "report_interval_seconds": 0})
        self.assertNotIn("/nodes/one/report", client.paths)
        for value in (60, 100000, "3600", True):
            with self.assertRaises(ValueError):
                adapter.poll({**SETTINGS, "report_interval_seconds": value})

    def test_shared_address_is_not_assigned_to_two_nics(self):
        report = ("2: eno1: <UP> mtu 1500\n    link/ether 02:00:00:00:00:01 brd x\n"
                  "3: eno2: <UP> mtu 1500\n    link/ether 02:00:00:00:00:01 brd x\n")
        adapter, _ = self.adapter(report)
        macs = self.macs(adapter.poll(SETTINGS))
        self.assertEqual((macs["eno1"], macs["eno2"]), (None, None))


class ChassisMatchTests(unittest.TestCase):
    """Identify a host by its LLDP chassis MAC when its address does not match."""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.unifi = DiscoverySource(name="unifi", kind="unifi", config_name="default")
        self.proxmox = DiscoverySource(name="proxmox", kind="proxmox", config_name="default")
        self.db.add_all([self.unifi, self.proxmox]); self.db.flush()
        self.switch = DiscoveredDevice(source_id=self.unifi.id, remote_id="switch", name="Switch",
                                       category="switch")
        servers = [
            DiscoveredDevice(source_id=self.proxmox.id, remote_id=name, name=name, category="server",
                             identifiers_json=json.dumps({"management_ips": [address]}))
            for name, address in (("one", "192.0.2.1"), ("two", "192.0.2.2"))
        ]
        self.db.add_all([self.switch, *servers]); self.db.flush()
        # Both hosts have a NIC named eno1, so the name alone is ambiguous.
        self.nics = {
            (server.remote_id, name): DiscoveredPort(
                discovered_device_id=server.id, remote_id=name, name=name, mac=mac, link_up=True)
            for server, entries in zip(servers, (
                (("eno1", "02:00:00:00:01:01"), ("eno2", "02:00:00:00:01:02")),
                (("eno1", "02:00:00:00:02:01"),),
            ))
            for name, mac in entries
        }
        self.switch_port = DiscoveredPort(discovered_device_id=self.switch.id, remote_id="5",
                                          name="Port 5", link_up=True)
        self.db.add_all([*self.nics.values(), self.switch_port]); self.db.flush()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def reconcile(self, **neighbor):
        """Store one LLDP neighbor on the switch and rebuild the cross-source links.

        Args:
            **neighbor: The neighbor's fields, such as ``chassis_id``.

        Returns:
            int: How many links the rebuild made.
        """
        self.switch.details_json = json.dumps({"lldp_neighbors": [
            {"local_port": "5", "management_ips": ["198.51.100.9"], **neighbor},
        ]})
        self.db.flush()
        return reconcile_proxmox_lldp_links(self.db, self.unifi, [self.proxmox])

    def test_chassis_mac_identifies_the_node(self):
        self.assertEqual(self.reconcile(chassis_id="02:00:00:00:01:02", port_id="eno1"), 1)
        link = self.db.scalar(select(DiscoveredLink))
        self.assertEqual((link.evidence, link.confidence), ("lldp-proxmox", "confirmed"))
        self.assertIn(self.nics[("one", "eno1")].id, (link.a_port_id, link.b_port_id))

    def test_unknown_chassis_with_ambiguous_name_is_skipped(self):
        self.assertEqual(self.reconcile(chassis_id="02:00:00:00:09:09", port_id="eno1"), 0)

    def test_address_and_chassis_that_disagree_are_skipped(self):
        self.assertEqual(self.reconcile(chassis_id="02:00:00:00:02:01", port_id="eno1",
                                        management_ips=["192.0.2.1"]), 0)


if __name__ == "__main__":
    unittest.main()
