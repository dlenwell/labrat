# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Check Proxmox cluster telemetry, its curation, the cluster API, and the page routes."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import requests
from sqlalchemy import select

import test_step4c as fixture
from labrat.collector import proxmox_cluster as pc
from labrat.collector.proxmox_clients import guest_snapshot
from labrat.collector.store import TELEMETRY_DETAIL_KEYS
from labrat.discovery.models import DiscoveredDevice, DiscoveredNetworkInterface, DiscoverySource

SETTINGS = {"url": "https://pve.example.test", "token_id": "test@pve!audit", "token_secret": "test-only"}

NVME_TEXT = (
    "Critical Warning:                   0x00\n"
    "Temperature:                        41 Celsius\n"
    "Available Spare:                    100%\n"
    "Percentage Used:                    12%\n"
    "Data Units Written:                 2,000 [1.02 GB]\n"
    "Power On Hours:                     1,234\n"
    "Media and Data Integrity Errors:    0\n"
    "Temperature Sensor 2:               55 Celsius\n"
)


def routed(responses):
    """Return a fake JSON client answering by URL path suffix.

    Args:
        responses: Map of path (after ``/api2/json``) to a ``data`` value, or
            to an exception instance to raise.

    Returns:
        SimpleNamespace: An adapter-like object with ``client.get_json``.
    """
    calls = []

    def get_json(url, headers, verify):
        path = url.split("/api2/json", 1)[1]
        calls.append(path)
        value = responses.get(path)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise requests.HTTPError("403")
        return {"data": value}

    return SimpleNamespace(client=SimpleNamespace(get_json=get_json), calls=calls)


class CurationTests(unittest.TestCase):
    """Pure curation helpers keep named values and drop everything else."""

    def test_smart_reads_nvme_text_and_ata_attributes(self):
        """NVMe text and ATA attribute lists become the same named counters."""
        nvme = pc._smart({"health": "PASSED", "type": "text", "text": NVME_TEXT})
        self.assertEqual(nvme["temperature_c"], 41)
        self.assertEqual(nvme["worn_percent"], 12)
        self.assertEqual(nvme["written_bytes"], 2000 * 512000)
        self.assertEqual(nvme["power_on_hours"], 1234)
        self.assertEqual(nvme["critical_warning"], 0)
        ata = pc._smart({"health": "PASSED", "attributes": [
            {"name": "Temperature_Celsius", "raw": "24 (Min/Max 15/45)", "normalized": 72},
            {"name": "Reallocated_Sector_Ct", "raw": "3", "normalized": 100},
            {"name": "Unrelated_Attribute", "raw": "99"},
        ]})
        self.assertEqual(ata, {"health": "PASSED", "temperature_c": 24, "reallocated_sectors": 3})

    def test_disks_turn_wearout_into_life_used_and_mark_boot(self):
        """Proxmox wearout is life left; the boot partition's disk is marked."""
        adapter = routed({
            "/nodes/node-a/disks/list": [
                {"devpath": "/dev/nvme0n1", "type": "nvme", "size": 10, "wearout": 88, "used": "BIOS boot", "health": "PASSED"},
                {"devpath": "/dev/sda", "type": "hdd", "size": 20, "wearout": "N/A", "used": "LVM", "osdid-list": ["4"]},
            ],
            "/nodes/node-a/disks/smart?disk=%2Fdev%2Fnvme0n1": {"health": "PASSED", "text": NVME_TEXT},
            "/nodes/node-a/disks/smart?disk=%2Fdev%2Fsda": requests.HTTPError("500"),
        })
        reader = pc._Reader(adapter, SETTINGS)
        boot, data = pc._disks(reader, "node-a", lambda: 0)
        self.assertEqual((boot["worn_percent"], boot["boot"]), (12, True))
        self.assertEqual(boot["smart"]["temperature_c"], 41)
        self.assertEqual((data["worn_percent"], data["boot"], data["osds"]), (None, False, ["4"]))
        self.assertNotIn("smart", data)

    def test_guest_agent_keeps_meaningful_addresses_and_filesystems(self):
        """Loopback, link-local, overlay interfaces and pseudo filesystems are dropped."""
        base = "/nodes/node-a/qemu/101/agent"
        adapter = routed({
            f"{base}/get-osinfo": {"result": {"pretty-name": "Example OS 1", "id": "example", "kernel-release": "1.0"}},
            f"{base}/network-get-interfaces": {"result": [
                {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1", "prefix": 8}]},
                {"name": "eth0", "hardware-address": "02:00:00:00:00:10", "ip-addresses": [
                    {"ip-address": "192.0.2.10", "prefix": 24}, {"ip-address": "fe80::1", "prefix": 64},
                    {"ip-address": "2001:db8::10", "prefix": 64}]},
                {"name": "docker0", "ip-addresses": [{"ip-address": "198.51.100.1", "prefix": 16}]},
                # VPN and overlay tunnels describe other networks, not the guest.
                {"name": "wg0", "ip-addresses": [{"ip-address": "198.51.100.2", "prefix": 24}]},
                {"name": "ztabcdef12", "ip-addresses": [{"ip-address": "198.51.100.3", "prefix": 24}]},
                {"name": "podman1", "ip-addresses": [{"ip-address": "198.51.100.4", "prefix": 24}]},
                {"name": "tun0", "ip-addresses": [{"ip-address": "198.51.100.5", "prefix": 24}]},
            ]},
            f"{base}/get-fsinfo": {"result": [
                {"name": "sda1", "mountpoint": "/", "type": "ext4", "total-bytes": 100, "used-bytes": 40},
                {"name": "tmpfs", "mountpoint": "/run", "type": "tmpfs", "total-bytes": 10, "used-bytes": 1},
            ]},
        })
        result = pc._guest_agent(pc._Reader(adapter, SETTINGS), "node-a", 101)
        self.assertEqual(result["os"]["name"], "Example OS 1")
        self.assertEqual([(a["address"], a["family"]) for a in result["addresses"]],
                         [("192.0.2.10", "ipv4"), ("2001:db8::10", "ipv6")])
        self.assertEqual([fs["mount"] for fs in result["filesystems"]], ["/"])
        containers = routed({"/nodes/node-a/lxc/105/interfaces": [
            {"name": "eth0", "hwaddr": "02:00:00:00:00:11", "ip-addresses": [{"ip-address": "192.0.2.11", "prefix": "24"}]}]})
        self.assertEqual(pc._container_addresses(pc._Reader(containers, SETTINGS), "node-a", 105)["addresses"][0]["prefix"], 24)

    def test_versions_show_updates_and_a_pending_reboot(self):
        """Installed is OldVersion, available is Version; a newer installed kernel means reboot."""
        versions = pc._versions([
            {"Package": "proxmox-ve", "OldVersion": "9.0", "Version": "9.0", "RunningKernel": "7.0.1-2-pve"},
            {"Package": "pve-manager", "OldVersion": "9.2.1", "Version": "9.2.3", "ManagerVersion": "9.2.1/abc"},
            {"Package": "proxmox-kernel-7.0", "OldVersion": "7.0.1-10", "Version": "7.0.1-10"},
            {"Package": "example-tool", "OldVersion": "1.0", "Version": "1.0"},
        ])
        self.assertTrue(versions["reboot_needed"])
        self.assertFalse(versions["manager_restart_needed"])
        self.assertEqual([item["package"] for item in versions["upgradable"]], ["pve-manager"])
        certs = pc._certificates([{"filename": "pve-ssl.pem", "notafter": 5, "issuer": "/C=US/O=Example/CN=Example CA", "pem": "x"}])
        self.assertEqual(certs, [{"file": "pve-ssl.pem", "not_after": 5, "issuer": "Example CA"}])

    def test_guest_history_averages_half_hours(self):
        """Guest RRD rows become 30-minute CPU, memory, and IO points."""
        rows = [{"time": 1800, "cpu": 0.5, "mem": 2, "maxmem": 4, "netin": 10, "diskwrite": 4},
                {"time": 1900, "cpu": 0.3, "mem": 2, "maxmem": 4, "netin": 30, "diskwrite": 8}]
        self.assertEqual(pc._guest_history(rows), [{"t": 1800, "cpu": 0.4, "mem": 0.5, "net_in": 20.0, "disk_write": 6.0}])

    def test_tasks_summarize_by_outcome(self):
        """Recent tasks count by outcome and type; old ones are left out."""
        rows = [{"type": "vzdump", "starttime": 9000, "endtime": 9100, "status": "OK"},
                {"type": "qmigrate", "starttime": 9000, "endtime": 9100, "status": "migration aborted", "node": "node-a"},
                {"type": "vzdump", "starttime": 9500, "status": ""},
                {"type": "vzdump", "starttime": 10, "endtime": 20, "status": "OK"}]
        summary = pc._tasks(rows, now=10000, hours=1)
        self.assertEqual((summary["ok"], summary["error"], summary["running"]), (1, 1, 1))
        self.assertEqual(summary["failures"][0]["type"], "qmigrate")
        self.assertEqual(summary["by_type"]["vzdump"]["running"], 1)
        self.assertEqual([task["outcome"] for task in summary["recent"]], ["ok", "error", "running"])

    def test_history_averages_into_quarter_hours(self):
        """RRD rows average into 15-minute CPU and memory fractions."""
        rows = [{"time": 900, "cpu": 0.2, "memused": 1, "memtotal": 4},
                {"time": 960, "cpu": 0.4, "memused": 3, "memtotal": 4},
                {"time": 1800, "cpu": None, "memused": None, "memtotal": 4}]
        self.assertEqual(pc._history(rows), [{"t": 900, "cpu": 0.3, "mem": 0.5}])

    def test_ceph_curation_flattens_osds_and_pools(self):
        """The OSD tree flattens by host; pools keep replication and rule."""
        tree = {"root": {"children": [{"type": "root", "children": [{"type": "host", "name": "node-b", "children": [
            {"type": "osd", "id": "1", "name": "osd.1", "status": "up", "in": 1, "device_class": "nvme",
             "percent_used": 10.5, "total_space": 100, "bytes_used": 10, "ceph_version": "not kept"}]}]}]}}
        osds = pc._osds(tree)
        self.assertEqual(osds[0]["host"], "node-b")
        self.assertEqual((osds[0]["in"], osds[0]["device_class"]), (True, "nvme"))
        self.assertNotIn("ceph_version", osds[0])
        pools = pc._ceph_pools([{"pool_name": "example-pool", "size": 3, "min_size": 2, "crush_rule_name": "replicated_nvme",
                                 "bytes_used": 30, "application_metadata": {"rbd": {}},
                                 "autoscale_status": {"logical_used": 10, "subtree_capacity": 300}}])
        self.assertEqual(pools[0]["rule"], "replicated_nvme")
        self.assertEqual((pools[0]["stored"], pools[0]["class_capacity"], pools[0]["applications"]), (10, 300, ["rbd"]))
        status = pc._ceph_status({"health": {"status": "HEALTH_WARN", "checks": {"OSD_DOWN": {
            "severity": "HEALTH_WARN", "summary": {"message": "1 osds down"}}}},
            "osdmap": {"num_osds": 3, "num_up_osds": 2, "num_in_osds": 3},
            "pgmap": {"num_pools": 2, "pgs_by_state": [{"state_name": "active+clean", "count": 5}]},
            "monmap": {"mons": [{}, {}]}, "quorum": [0, 1]})
        self.assertEqual(status["checks"], [{"code": "OSD_DOWN", "severity": "HEALTH_WARN", "summary": "1 osds down"}])
        self.assertEqual((status["osds"]["up"], status["pool_count"], status["monitors"]["in_quorum"]), (2, 2, 2))


class NodeOrderTests(unittest.TestCase):
    """Node names that are numbers sort by value, before the rest."""

    def test_spelled_numbers_then_natural_order(self):
        """Spelled-out and digit names come first in numeric order."""
        from labrat.api.cluster import node_order
        names = ["gpu10", "four", "three", "gpu2", "one", "twenty-one", "two", "7", "alpha"]
        self.assertEqual(sorted(names, key=node_order),
                         ["one", "two", "three", "four", "7", "twenty-one", "alpha", "gpu2", "gpu10"])


class SnapshotTests(unittest.TestCase):
    """One snapshot reads each part independently and bounds what it keeps."""

    def responses(self):
        """Answers for a one-node cluster with Ceph, ZFS, and one failing part.

        Returns:
            dict: Payloads keyed by API path; one is an exception.
        """
        return {
            "/cluster/resources": [
                {"type": "node", "node": "node-a", "status": "online", "cpu": 0.25, "maxcpu": 8, "mem": 4, "maxmem": 16},
                {"type": "storage", "node": "node-a", "storage": "ceph-pool", "plugintype": "rbd", "shared": 1,
                 "disk": 5, "maxdisk": 50, "status": "available", "content": "images"},
            ],
            "/cluster/status": [{"type": "cluster", "name": "example", "quorate": 1, "nodes": 1}],
            "/storage": [{"storage": "ceph-pool", "type": "rbd", "pool": "ceph-pool", "shared": 1, "content": "images"}],
            "/nodes/node-a/status": {"cpuinfo": {"model": "Example CPU", "sockets": 1, "cores": 4, "cpus": 8},
                                     "loadavg": ["0.5", "0.4", "0.3"], "swap": {"used": 0, "total": 0}},
            "/nodes/node-a/rrddata?timeframe=day&cf=AVERAGE": [{"time": 900, "cpu": 0.1, "memused": 1, "memtotal": 2}],
            "/nodes/node-a/disks/zfs": requests.HTTPError("403"),
            "/nodes/node-a/disks/list": [],
            "/nodes/node-a/hardware/pci": [{"class": "0x030000", "id": "0000:01:00.0", "device_name": "Example GPU"},
                                           {"class": "0x0c0330", "id": "0000:02:00.0", "device_name": "USB"}],
            "/nodes/node-a/certificates/info": [{"filename": "pve-ssl.pem", "notafter": 2000000000, "issuer": "/O=Example/CN=Example CA", "pem": "dropped"}],
            "/nodes/node-a/services": [{"name": "pveproxy", "state": "running", "unit-state": "enabled"}],
            "/nodes/node-a/apt/versions": [],
            "/cluster/ceph/status": {"health": {"status": "HEALTH_OK"}, "osdmap": {}, "pgmap": {}},
            "/nodes/node-a/ceph/osd": {"root": {"children": []}},
            "/nodes/node-a/ceph/pool": [{"pool_name": "ceph-pool", "size": 3}],
        }

    def test_snapshot_keeps_good_parts_when_one_fails(self):
        """A refused ZFS read is named in errors; everything else is read."""
        adapter = routed(self.responses())
        snapshot = pc.cluster_snapshot(adapter, SETTINGS, hardware={})
        node = snapshot["nodes"]["node-a"]
        self.assertEqual(node["errors"], {"zfs": "unavailable"})
        self.assertEqual((node["cores"], node["threads"], node["cpu_model"]), (4, 8, "Example CPU"))
        self.assertEqual(node["history"], [{"t": 900, "cpu": 0.1, "mem": 0.5}])
        self.assertEqual([item["kind"] for item in node["pci"]], ["gpu"])
        cluster = snapshot["cluster"]
        self.assertEqual((cluster["name"], cluster["quorate"], cluster["uses_ceph"]), ("example", True, True))
        self.assertEqual(cluster["storage_config"][0]["pool"], "ceph-pool")
        self.assertEqual(cluster["ceph"]["pool_list"][0]["name"], "ceph-pool")

    def test_hardware_is_reused_until_due(self):
        """Disks, SMART, PCI, and certificates are read once per hour, not every refresh."""
        hardware = {}
        adapter = routed(self.responses())
        pc.cluster_snapshot(adapter, SETTINGS, clock=lambda: 100.0, hardware=hardware)
        adapter.calls.clear()
        pc.cluster_snapshot(adapter, SETTINGS, clock=lambda: 200.0, hardware=hardware)
        self.assertNotIn("/nodes/node-a/disks/list", adapter.calls)
        pc.cluster_snapshot(adapter, SETTINGS, clock=lambda: 100.0 + pc.HARDWARE_REFRESH_SECONDS, hardware=hardware)
        self.assertIn("/nodes/node-a/disks/list", adapter.calls)

    def test_packages_and_services_are_read_every_refresh(self):
        """Updates and a pending reboot follow maintenance within one refresh, not the hourly reads."""
        hardware = {}
        responses = self.responses()
        responses["/nodes/node-a/apt/versions"] = [
            {"Package": "proxmox-kernel-6.8", "OldVersion": "6.8.12-4", "Version": "6.8.12-4", "RunningKernel": "6.8.12-2-pve"}]
        adapter = routed(responses)
        first = pc.cluster_snapshot(adapter, SETTINGS, clock=lambda: 100.0, hardware=hardware)
        self.assertTrue(first["nodes"]["node-a"]["versions"]["reboot_needed"])
        responses["/nodes/node-a/apt/versions"][0]["RunningKernel"] = "6.8.12-4-pve"
        adapter.calls.clear()
        later = pc.cluster_snapshot(adapter, SETTINGS, clock=lambda: 400.0, hardware=hardware)
        self.assertFalse(later["nodes"]["node-a"]["versions"]["reboot_needed"])
        self.assertEqual(later["nodes"]["node-a"]["services"][0]["name"], "pveproxy")
        self.assertNotIn("/nodes/node-a/disks/list", adapter.calls)
        # A failed read keeps the last good packages.
        responses["/nodes/node-a/apt/versions"] = RuntimeError("unavailable")
        kept = pc.cluster_snapshot(adapter, SETTINGS, clock=lambda: 700.0, hardware=hardware)
        self.assertEqual(kept["nodes"]["node-a"]["versions"]["running_kernel"], "6.8.12-4-pve")
        self.assertEqual(kept["nodes"]["node-a"]["errors"]["versions"], "unavailable")

    def test_a_reboot_refreshes_the_hourly_reads(self):
        """A node whose uptime fell since the hourly reads gets them again at once."""
        hardware = {}
        responses = self.responses()
        responses["/cluster/resources"][0]["uptime"] = 5000
        adapter = routed(responses)
        pc.cluster_snapshot(adapter, SETTINGS, clock=lambda: 100.0, hardware=hardware)
        responses["/cluster/resources"][0]["uptime"] = 5300
        adapter.calls.clear()
        pc.cluster_snapshot(adapter, SETTINGS, clock=lambda: 400.0, hardware=hardware)
        self.assertNotIn("/nodes/node-a/disks/list", adapter.calls)
        responses["/cluster/resources"][0]["uptime"] = 200
        pc.cluster_snapshot(adapter, SETTINGS, clock=lambda: 700.0, hardware=hardware)
        self.assertIn("/nodes/node-a/disks/list", adapter.calls)

    def test_telemetry_keys_are_kept_by_scans(self):
        """Both stored keys are telemetry, so scans keep them unaudited."""
        self.assertIn("proxmox_node", TELEMETRY_DETAIL_KEYS)
        self.assertIn("proxmox_cluster", TELEMETRY_DETAIL_KEYS)

    def test_guest_snapshot_adds_sizing_and_nic_vlans(self):
        """Guest records add sizing facts and a NIC's own VLAN tag."""
        adapter = SimpleNamespace(client=Mock())
        adapter.client.get_json.side_effect = [
            {"data": [{"type": "lxc", "vmid": 105, "node": "node-a", "name": "Example container", "status": "running",
                       "maxcpu": 2, "maxmem": 1024, "maxdisk": 2048, "mem": 512, "cpu": 0.5, "template": 0}]},
            {"data": {"net0": "name=eth0,bridge=vmbr0,hwaddr=02:00:00:00:00:66,tag=30", "onboot": 1, "secret": "dropped"}},
        ]
        guest = guest_snapshot(adapter, SETTINGS)[0]
        self.assertEqual(guest["vlans"], {"02:00:00:00:00:66": 30})
        self.assertEqual((guest["vcpus"], guest["memory"], guest["onboot"], guest["template"]), (2, 1024, True, False))
        self.assertNotIn("dropped", json.dumps(guest))


class StoredTelemetryTests(unittest.TestCase):
    """Telemetry lands on node observations; the API and pages read it back."""

    setUp = fixture.Step4CTests.setUp

    def add_node(self):
        """Store one Proxmox node with bridges and a guest snapshot.

        Returns:
            int: The source's ID.
        """
        with self.factory.begin() as db:
            source = DiscoverySource(name="Proxmox example", kind="proxmox", config_name="default")
            db.add(source)
            db.flush()
            host = DiscoveredDevice(source_id=source.id, remote_id="node-a", name="node-a", category="server",
                                    details_json=json.dumps({"platform": "proxmox", "proxmox_clients": {"guests": [
                                        {"vmid": 101, "kind": "vm", "name": "Example VM", "host": "node-a", "status": "running",
                                         "macs": ["02:00:00:00:00:10", "02:00:00:00:00:11"],
                                         "bridges": {"02:00:00:00:00:10": "br20", "02:00:00:00:00:11": "vmbr0"},
                                         "vlans": {"02:00:00:00:00:11": 30}, "vcpus": 2, "memory": 1024}]}}))
            db.add(host)
            db.flush()
            db.add_all([
                DiscoveredNetworkInterface(discovered_device_id=host.id, remote_id="br20", name="br20", kind="bridge",
                                           members_json='["vlan20"]'),
                DiscoveredNetworkInterface(discovered_device_id=host.id, remote_id="vlan20", name="vlan20", kind="vlan",
                                           parent_name="bond0", vlan_id=20),
                DiscoveredNetworkInterface(discovered_device_id=host.id, remote_id="vmbr0", name="vmbr0", kind="bridge",
                                           members_json='["bond0"]'),
            ])
            return source.id

    def test_collect_writes_and_failure_marks_unavailable(self):
        """A good read stores both keys; a failed read keeps them, unavailable."""
        source_id = self.add_node()
        source = SimpleNamespace(id=source_id)
        adapter = routed(SnapshotTests.responses(SnapshotTests()))
        pc.collect_cluster_telemetry(self.factory, source, adapter, SETTINGS)
        with self.factory() as db:
            details = json.loads(db.scalar(select(DiscoveredDevice.details_json)))
        self.assertTrue(details["proxmox_node"]["available"])
        self.assertEqual(details["proxmox_cluster"]["name"], "example")
        failing = routed({"/cluster/resources": requests.ConnectionError("down")})
        pc.collect_cluster_telemetry(self.factory, source, failing, SETTINGS)
        with self.factory() as db:
            details = json.loads(db.scalar(select(DiscoveredDevice.details_json)))
        self.assertFalse(details["proxmox_node"]["available"])
        self.assertEqual(details["proxmox_node"]["cores"], 4)

    def test_api_resolves_guest_vlans_from_tag_or_bridge(self):
        """A tag wins; otherwise the bridge's VLAN member names the VLAN."""
        self.add_node()
        data = self.client.get("/api/v1/cluster").json["sources"][0]
        nics = {nic["bridge"]: nic for nic in data["guests"][0]["nics"]}
        self.assertEqual((nics["br20"]["vlan"], nics["br20"]["vlan_source"]), (20, "bridge"))
        self.assertEqual((nics["vmbr0"]["vlan"], nics["vmbr0"]["vlan_source"]), (30, "tag"))
        networks = {row["bridge"]: row for row in data["networks"]}
        self.assertEqual((networks["br20"]["vlan"], networks["br20"]["guests"]), (20, 1))
        self.assertIsNone(networks["vmbr0"]["vlan"])

    def test_routes(self):
        """Every tab renders, unknown tabs 404, and old Nodes links redirect."""
        for path in ("/cluster", "/cluster/compute", "/cluster/storage", "/cluster/nodes", "/cluster/nodes/node-a"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertEqual(self.client.get("/cluster/unknown").status_code, 404)
        self.assertEqual(self.client.get("/cluster/compute/node-a").status_code, 404)
        response = self.client.get("/nodes/node-a")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/cluster/nodes/node-a"))
        signed_out = self.app.test_client()
        self.assertNotEqual(signed_out.get("/api/v1/cluster").status_code, 200)
        self.assertNotEqual(signed_out.get("/cluster").status_code, 200)


if __name__ == "__main__":
    unittest.main()
