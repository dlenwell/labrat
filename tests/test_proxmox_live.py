# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Live Proxmox pulls between full scans (planning/specs/proxmox-cluster-view.md)."""

from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

import requests
from sqlalchemy import select

import test_step4c as fixture
from test_proxmox_cluster import SETTINGS, routed
from labrat.collector import proxmox_live as live
from labrat.discovery.models import DiscoveredDevice, DiscoverySource


def stored(uptime, at):
    """A stored node record as the full scan leaves it.

    Args:
        uptime: The node's uptime in seconds.
        at: When the scan sampled it.

    Returns:
        dict: The stored ``proxmox_node`` record.
    """
    return {"status": "online", "cpu": 0.5, "uptime": uptime, "cores": 4, "sampled_at": at.isoformat(), "available": True,
            "versions": {"reboot_needed": True, "running_kernel": "6.8.12-2-pve", "upgradable": [{"package": "example"}]},
            "services": [{"name": "pveproxy", "state": "dead", "enabled": True}]}


class LivePullTests(unittest.TestCase):
    """One pull merges node, guest, and task state into stored telemetry."""

    setUp = fixture.Step4CTests.setUp

    def add_node(self, node):
        """Store one Proxmox source and node observation; return the source ID.

        Args:
            node: The stored node record.

        Returns:
            int: The source's ID.
        """
        with self.factory.begin() as db:
            source = DiscoverySource(name="Proxmox example", kind="proxmox", config_name="default")
            db.add(source)
            db.flush()
            db.add(DiscoveredDevice(source_id=source.id, remote_id="node-a", name="node-a", category="server",
                                    details_json=json.dumps({
                                        "platform": "proxmox", "proxmox_node": node,
                                        "proxmox_cluster": {"name": "example", "tasks": {"ok": 0}},
                                        "proxmox_clients": {"guests": [
                                            {"vmid": 101, "name": "Example VM", "host": "node-a", "status": "stopped", "cpu": 0, "macs": []},
                                            {"vmid": 102, "name": "Moved VM", "host": "node-a", "status": "running", "cpu": 0.1, "macs": []},
                                        ]}})))
            return source.id

    def details(self):
        """The node observation's stored details.

        Returns:
            dict: The details.
        """
        with self.factory() as db:
            return json.loads(db.scalar(select(DiscoveredDevice.details_json)))

    @staticmethod
    def responses(uptime):
        """Resources, tasks, and maintenance reads for one node.

        Args:
            uptime: The node's current uptime in seconds.

        Returns:
            dict: Payloads keyed by API path.
        """
        return {
            "/cluster/resources": [
                {"type": "node", "node": "node-a", "status": "online", "cpu": 0.07, "maxcpu": 8, "mem": 6, "maxmem": 16, "uptime": uptime},
                {"type": "qemu", "vmid": 101, "node": "node-a", "status": "running", "cpu": 0.2, "mem": 512, "uptime": 60},
                {"type": "qemu", "vmid": 102, "node": "node-b", "status": "running", "cpu": 0.3, "mem": 256, "uptime": 90},
                {"type": "storage", "node": "node-a", "storage": "local"},
            ],
            "/cluster/tasks": [{"type": "qmstart", "node": "node-a", "starttime": int(datetime.now().timestamp()) - 30,
                                "endtime": int(datetime.now().timestamp()) - 29, "status": "OK"}],
            "/nodes/node-a/services": [{"name": "pveproxy", "state": "running", "unit-state": "enabled"}],
            "/nodes/node-a/apt/versions": [{"Package": "proxmox-kernel-6.8", "OldVersion": "6.8.12-4", "Version": "6.8.12-4",
                                            "RunningKernel": "6.8.12-4-pve"}],
        }

    def test_a_pull_merges_state_without_touching_the_rest(self):
        """Status, CPU, and uptime update; hourly facts and source times stay."""
        source_id = self.add_node(stored(5000, datetime.now(timezone.utc) - timedelta(seconds=60)))
        adapter = routed(self.responses(5060))
        self.assertEqual(live.poll_proxmox_live_once(self.factory, source_id, SETTINGS, adapter), 1)
        details = self.details()
        node = details["proxmox_node"]
        self.assertEqual((node["cpu"], node["uptime"], node["cores"]), (0.07, 5060, 4))
        self.assertIn("live_at", node)
        # No reboot: maintenance is left to the full scan.
        self.assertTrue(node["versions"]["reboot_needed"])
        self.assertNotIn("/nodes/node-a/apt/versions", adapter.calls)
        guests = {guest["vmid"]: guest for guest in details["proxmox_clients"]["guests"]}
        self.assertEqual((guests[101]["status"], guests[101]["cpu"], guests[101]["mem_used"]), ("running", 0.2, 512))
        self.assertEqual(guests[101]["macs"], [])
        # A guest now on another node keeps its record until the full scan moves it.
        self.assertEqual(guests[102]["cpu"], 0.1)
        self.assertEqual(details["proxmox_cluster"]["tasks"]["ok"], 1)
        with self.factory() as db:
            self.assertIsNone(db.get(DiscoverySource, source_id).last_success_at)

    def test_a_reboot_rereads_services_and_packages(self):
        """A shorter uptime than expected clears a stale pending reboot at once."""
        source_id = self.add_node(stored(900000, datetime.now(timezone.utc) - timedelta(seconds=300)))
        adapter = routed(self.responses(90))
        live.poll_proxmox_live_once(self.factory, source_id, SETTINGS, adapter)
        node = self.details()["proxmox_node"]
        self.assertFalse(node["versions"]["reboot_needed"])
        self.assertEqual(node["versions"]["upgradable"], [])
        self.assertEqual(node["services"][0]["state"], "running")

    def test_a_failed_pull_keeps_what_was_stored(self):
        """An unreachable controller changes nothing and is only logged."""
        node = stored(5000, datetime.now(timezone.utc))
        source_id = self.add_node(node)
        failing = routed({"/cluster/resources": requests.ConnectionError("down")})
        with patch.object(live, "_LOGGER") as logger:
            self.assertEqual(live.poll_proxmox_live_once(self.factory, source_id, SETTINGS, failing), 0)
        self.assertEqual(self.details()["proxmox_node"], node)
        logger.warning.assert_called_once()
        self.assertNotIn("pve.example.test", repr(logger.warning.call_args))

    def test_interval_setting(self):
        """Default 20 s; 0 turns pulls off; out-of-range values are refused."""
        self.assertEqual(live.live_interval({}), 20)
        self.assertEqual(live.live_interval({"live_interval_seconds": 0}), 0)
        for value in (5, 301, True, "20"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                live.live_interval({"live_interval_seconds": value})


if __name__ == "__main__":
    unittest.main()
