# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Exercise WAN status: gateway detection, snapshots, polling, and storage."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import labrat.models  # Register canonical models referenced by discovery tables.
from labrat.audit import AuditEvent
from labrat.collector.adapters import unifi_classic
from labrat.collector.adapters.unifi import UniFiAdapter
from labrat.collector.contracts import DeviceObservation, DiscoveryBatch, WanObservation
from labrat.collector.runner import wan_interval
from labrat.collector.store import apply_batch, record_wan
from labrat.database import Base
from labrat.discovery.models import DiscoveredDevice, DiscoverySource
from labrat.pages.status import wan_health

BASE = "https://unifi.example"
INTEGRATION = f"{BASE}/proxy/network/integration/v1"
CLASSIC = f"{BASE}/proxy/network/api/s/default"
GATEWAY_MAC = "aa:aa:aa:00:00:09"


def gateway_record(wan1_up=True, wan2_up=True, active="wan1"):
    """A classic Dream Machine record with two WAN interfaces.

    Args:
        wan1_up: Whether the first WAN is up.
        wan2_up: Whether the second WAN is up.
        active: The WAN carrying traffic.

    Returns:
        dict: The gateway's classic stat/device record.
    """
    def table(key, up, port):
        return {
            "name": key, "enable": True, "up": up, "is_uplink": key == active,
            "speed": 2500, "max_speed": 10000, "latency": 9, "availability": 100,
            "rx_bytes-r": 125000, "tx_bytes-r": 12500, "ip": "192.0.2.10" if key == "wan1" else "198.51.100.7",
            "ipv6": ["2001:db8::1"], "media": "SFP+", "port_idx": port,
        }
    return {
        "mac": GATEWAY_MAC, "type": "udm", "model": "UDMPROSE", "port_table": [],
        "wan1": table("wan1", wan1_up, 10), "wan2": table("wan2", wan2_up, 9),
        "uptime_stats": {"WAN": {"uptime": 3600}, "WAN2": {"uptime": 60}},
    }


HEALTH = [
    {"subsystem": "wan", "gw_mac": GATEWAY_MAC, "isp_name": "Example ISP"},
    {"subsystem": "www", "status": "ok", "latency": 11, "drops": 0, "uptime": 7200,
     "xput_down": 0.0, "xput_up": 0.0, "speedtest_lastrun": 0},
]


class FakeClient:
    """Serve canned controller responses and record requested URLs."""

    def __init__(self, responses):
        """Serve scripted payloads by URL and remember what was asked.

        Args:
            responses: Payloads keyed by full URL.
        """
        self.responses = responses
        self.urls = []

    def get_json(self, url, headers, verify):
        """Return the payload for a URL, recording the request.

        Args:
            url: The requested URL.
            headers: Request headers (unused).
            verify: TLS verification (unused).

        Returns:
            object: The payload.

        Raises:
            AssertionError: For a URL the fixture does not know.
        """
        self.urls.append(url)
        if url not in self.responses:
            raise AssertionError(f"Unexpected API path: {url}")
        return self.responses[url]


class SnapshotTests(unittest.TestCase):
    """Turn classic gateway data into a WAN snapshot."""

    def test_wan_interfaces_mean_router(self):
        self.assertTrue(unifi_classic.has_wan_interfaces({"wan1": {}}))
        self.assertTrue(unifi_classic.has_wan_interfaces({"type": "UDM"}))
        self.assertFalse(unifi_classic.has_wan_interfaces({"type": "usw", "port_table": []}))

    def test_snapshot_units_and_missing_speed_test(self):
        snapshot = unifi_classic.wan_snapshot(gateway_record(), HEALTH)
        wan1, wan2 = snapshot["uplinks"]
        self.assertEqual((wan1["name"], wan1["active"], wan2["active"]), ("WAN1", True, False))
        self.assertEqual(wan1["interface"], "wan1")
        self.assertEqual(wan1["rx_bps"], 1_000_000)  # 125000 bytes/s.
        self.assertEqual((wan1["uptime_s"], wan2["uptime_s"]), (3600, 60))
        self.assertEqual(wan1["ip"], "192.0.2.10")
        self.assertEqual(snapshot["internet"]["isp"], "Example ISP")
        self.assertIsNone(snapshot["internet"]["speedtest"])  # Never ran: no zeros.

    def test_snapshot_reports_a_speed_test_that_ran(self):
        health = [HEALTH[0], {**HEALTH[1], "speedtest_lastrun": 1700000000, "xput_down": 912.5, "xput_up": 40.1}]
        speedtest = unifi_classic.wan_snapshot(gateway_record(), health)["internet"]["speedtest"]
        self.assertEqual((speedtest["down_mbps"], speedtest["ran_at"]), (912.5, 1700000000))

    def test_isp_names_are_scoped_to_the_reporting_uplink(self):
        """Do not lend the active circuit's reported ISP to its standby."""
        record = gateway_record()
        uplinks = unifi_classic.wan_snapshot(record, HEALTH)["uplinks"]
        self.assertEqual(uplinks[0]["isp"], "Example ISP")
        self.assertIsNone(uplinks[1]["isp"])
        record["wan2"]["isp_name"] = "Backup ISP"
        self.assertEqual(unifi_classic.wan_snapshot(record, HEALTH)["uplinks"][1]["isp"], "Backup ISP")

    def test_per_wan_geographic_results_name_standby_without_crossing_circuits(self):
        """Read the stored UniFi map; never copy WAN1's provider to WAN2."""
        record = gateway_record()
        record["geo_info"] = {"WAN": {"isp_name": "Primary ISP"}, "WAN2": {"isp_name": "Backup ISP"}}
        record["active_geo_info"] = {"WAN2": {"isp_name": "Current Backup ISP"}}
        first, second = unifi_classic.wan_snapshot(record, HEALTH)["uplinks"]
        self.assertEqual(first["isp"], "Primary ISP")
        self.assertEqual(second["isp"], "Current Backup ISP")
        record["active_geo_info"] = []
        record["geo_info"] = {"WAN": {"isp_name": "Primary ISP"}}
        record["last_geo_info"] = {"WAN2": {"isp_organization": "Last Backup ISP"}}
        self.assertEqual(unifi_classic.wan_snapshot(record, HEALTH)["uplinks"][1]["isp"], "Last Backup ISP")

    def test_no_wan_interfaces_no_snapshot(self):
        self.assertIsNone(unifi_classic.wan_snapshot({"type": "usw"}, HEALTH))


class AdapterTests(unittest.TestCase):
    """Classify a switching gateway as a router and read its WAN."""

    def fixture(self):
        """Build a controller with one gateway, its two WANs, and site health.

        Returns:
            FakeClient: The fixture client.
        """
        return FakeClient({
            f"{INTEGRATION}/sites?offset=0&limit=100": {
                "data": [{"id": "site-1", "internalReference": "default"}], "totalCount": 1,
            },
            f"{INTEGRATION}/sites/site-1/devices?offset=0&limit=100": {
                "data": [{"id": "gw", "model": "UniFi Dream Machine PRO SE",
                          "features": ["switching"], "macAddress": GATEWAY_MAC.upper()}],
                "totalCount": 1,
            },
            f"{INTEGRATION}/sites/site-1/devices/gw": {
                "id": "gw", "name": "Gateway", "features": {"switching": {}},
                "interfaces": {"ports": [{"idx": 1, "speedMbps": 1000}]},
            },
            f"{CLASSIC}/stat/device": {"data": [gateway_record()]},
            f"{CLASSIC}/stat/health": {"data": HEALTH},
            f"{CLASSIC}/stat/device/{GATEWAY_MAC}": {"data": [gateway_record(wan1_up=False, active="wan2")]},
            f"{BASE}/proxy/network/v2/api/site/default/vpn/connections": {"connections": []},
            f"{CLASSIC}/rest/networkconf": {"data": []},
            f"{CLASSIC}/stat/remoteuservpn": {"data": []},
        })

    def test_dream_machine_is_a_router_with_wan(self):
        batch = UniFiAdapter(self.fixture()).poll({"url": BASE, "api_key": "test"})
        self.assertEqual(batch.devices[0].category, "router")
        self.assertEqual(batch.wan[0].gateway_mac, GATEWAY_MAC)
        self.assertNotIn("wan", batch.devices[0].details)  # Telemetry stays out of scan details.

    def test_fast_poll_reads_only_the_gateway(self):
        client = self.fixture()
        readings = UniFiAdapter(client).poll_wan({"url": BASE, "api_key": "test"})
        self.assertEqual(len(readings), 1)
        self.assertFalse(readings[0].snapshot["uplinks"][0]["up"])
        self.assertNotIn(f"{CLASSIC}/stat/device", client.urls)  # Not the whole device list.

    def test_wan_interval_is_bounded(self):
        self.assertEqual(wan_interval({}), 30)
        self.assertEqual(wan_interval({"wan_interval_seconds": 0}), 0)
        for value in (5, 301, "30", True):
            with self.assertRaises(ValueError):
                wan_interval({"wan_interval_seconds": value})

    def test_modems_have_stable_identity_and_exact_wan_port_links(self):
        """Two circuits produce distinct observations without creating inventory."""
        client = self.fixture()
        gateway = client.responses[f"{CLASSIC}/stat/device"]["data"][0]
        gateway['geo_info'] = {'WAN': {'isp_name': 'Primary ISP'}, 'WAN2': {'isp_name': 'Backup ISP'}}
        gateway['port_table'] = [{'port_idx': 10, 'media': 'GE'}, {'port_idx': 9, 'media': 'GE'}]
        client.responses[f"{INTEGRATION}/sites/site-1/devices/gw"]['interfaces']['ports'].extend(
            [{'idx': 10}, {'idx': 9}]
        )
        first = UniFiAdapter(client).poll({'url': BASE, 'api_key': 'test'})
        modems = {d.details.get('wan_uplink_id'): d for d in first.devices if d.category == 'modem'}
        self.assertEqual(set(modems), {'wan1', 'wan2'})
        self.assertEqual(modems['wan1'].name, 'Primary ISP WAN1 upstream')
        self.assertEqual(modems['wan2'].name, 'Backup ISP WAN2 upstream')
        self.assertEqual({(link.a_port_remote_id, link.b_device_remote_id, link.b_port_remote_id,
                           link.confidence, link.evidence) for link in first.links}, {
            ('10', modems['wan1'].remote_id, 'wan', 'inferred', 'unifi-wan-upstream'),
            ('9', modems['wan2'].remote_id, 'wan', 'inferred', 'unifi-wan-upstream'),
        })
        for modem in modems.values():
            ports = [p for p in first.ports if p.device_remote_id == modem.remote_id]
            self.assertEqual(len(ports), 1)
            self.assertEqual((ports[0].remote_id, ports[0].medium, ports[0].link_up), ('wan', 'copper', True))
            self.assertEqual(modem.identifiers, {})
        gateway['geo_info']['WAN']['isp_name'] = 'Replacement ISP'
        second = UniFiAdapter(client).poll({'url': BASE, 'api_key': 'test'})
        self.assertEqual({d.remote_id for d in first.devices}, {d.remote_id for d in second.devices})
        self.assertIn('Replacement ISP WAN1 upstream', [d.name for d in second.devices])

    def test_unusable_wan_ports_do_not_invent_modems(self):
        """Disabled and noninteger port observations cannot become cable endpoints."""
        for value in (None, True, '10'):
            with self.subTest(port=value):
                client = self.fixture()
                record = client.responses[f"{CLASSIC}/stat/device"]["data"][0]
                record['wan1']['port_idx'] = value
                record['wan2']['enable'] = False
                batch = UniFiAdapter(client).poll({'url': BASE, 'api_key': 'test'})
                self.assertFalse(any(d.category == 'modem' for d in batch.devices))
                self.assertFalse(any(link.evidence == 'unifi-wan-upstream' for link in batch.links))

    def test_unknown_standby_provider_keeps_wan_name(self):
        """An active circuit's provider never becomes the standby modem's name."""
        batch = UniFiAdapter(self.fixture()).poll({'url': BASE, 'api_key': 'test'})
        names = {d.details.get('wan_uplink_id'): d.name for d in batch.devices if d.category == 'modem'}
        self.assertEqual(names, {'wan1': 'Example ISP WAN1 upstream', 'wan2': 'WAN2 upstream'})


class StoreTests(unittest.TestCase):
    """Store WAN readings as telemetry with a one-day, five-minute history."""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.source = DiscoverySource(name="unifi:test", kind="unifi", config_name="test")
        self.db.add(self.source)
        self.db.flush()
        self.scan = DiscoveryBatch(devices=(DeviceObservation(
            remote_id="site:gw", name="Gateway", category="router",
            identifiers={"mac": GATEWAY_MAC}, details={"platform": "unifi"},
        ),))
        apply_batch(self.db, self.source, self.scan)
        self.gateway = self.db.scalar(select(DiscoveredDevice))

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def details(self):
        """Read the gateway observation's stored details.

        Returns:
            dict: The details, freshly loaded.
        """
        self.db.refresh(self.gateway)
        return json.loads(self.gateway.details_json)

    def test_unavailable_vpn_retains_persisted_sample_without_audit_noise(self):
        """Collector restart cannot erase known VPN state or refresh its age."""
        first = {"available": True, "sampled_at": "2026-11-10T16:00:00+00:00",
                 "connections": [{"id": "a", "state": "connected"}]}
        record_wan(self.db, self.source, WanObservation(gateway_mac=GATEWAY_MAC,
                   snapshot={"uplinks": [], "vpn": first}))
        before = self.db.scalar(select(func.count()).select_from(AuditEvent))
        record_wan(self.db, self.source, WanObservation(gateway_mac=GATEWAY_MAC,
                   snapshot={"uplinks": [], "vpn": {"available": False, "sampled_at": None, "connections": []}}))
        stored = self.details()["wan"]["vpn"]
        self.assertFalse(stored["available"])
        self.assertEqual(stored["sampled_at"], first["sampled_at"])
        self.assertEqual(stored["connections"], first["connections"])
        self.assertEqual(self.db.scalar(select(func.count()).select_from(AuditEvent)), before)

    def reading(self, **kwargs):
        """Build a WAN reading from the gateway record.

        Args:
            **kwargs: Passed to ``gateway_record``.

        Returns:
            WanObservation: The reading.
        """
        return WanObservation(GATEWAY_MAC, unifi_classic.wan_snapshot(gateway_record(**kwargs), HEALTH))

    def test_history_buckets_average_and_expire(self):
        start = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
        record_wan(self.db, self.source, self.reading(), start)
        record_wan(self.db, self.source, self.reading(wan2_up=False), start + timedelta(seconds=30))
        history = self.details()["wan_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0][4], 2)  # Two samples in one bucket.
        self.assertTrue(history[0][5])  # An uplink was down during it.
        self.assertEqual(history[0][1], (2_000_000 + 1_000_000) / 2)  # Both up, then only WAN1.
        record_wan(self.db, self.source, self.reading(), start + timedelta(days=1, minutes=5))
        self.assertEqual(len(self.details()["wan_history"]), 1)  # The old bucket expired.

    def test_vpn_history_is_per_tunnel_bucketed_and_gapped(self):
        """Average per tunnel per bucket; unavailable reads add no points."""
        start = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)

        def vpn(available=True, a=(100, 50), b=(None, None)):
            return WanObservation(gateway_mac=GATEWAY_MAC, snapshot={"uplinks": [], "vpn": {
                "available": available, "sampled_at": start.isoformat() if available else None,
                "connections": [{"id": "a", "rx_bps": a[0], "tx_bps": a[1]},
                                {"id": "b", "rx_bps": b[0], "tx_bps": b[1]}]}})

        record_wan(self.db, self.source, vpn(), start)
        record_wan(self.db, self.source, vpn(a=(300, 150)), start + timedelta(seconds=30))
        history = self.details()["vpn_history"]
        bucket = int(start.timestamp())
        self.assertEqual(history, {"a": [[bucket, 200, 100, 2]]})  # b reports no rates.
        events = self.db.scalar(select(func.count()).select_from(AuditEvent))
        record_wan(self.db, self.source, vpn(available=False), start + timedelta(minutes=5))
        self.assertEqual(len(self.details()["vpn_history"]["a"]), 1)  # A gap, not a zero.
        record_wan(self.db, self.source, vpn(a=(0, 0)), start + timedelta(minutes=10))
        self.assertEqual(self.details()["vpn_history"]["a"][-1], [bucket + 600, 0, 0, 1])
        apply_batch(self.db, self.source, self.scan)
        self.assertIn("vpn_history", self.details())  # A scan keeps telemetry.
        self.assertEqual(self.db.scalar(select(func.count()).select_from(AuditEvent)), events)
        record_wan(self.db, self.source, vpn(available=False), start + timedelta(days=1, minutes=15))
        self.assertEqual(self.details()["vpn_history"], {})  # Every row expired.

    def test_scan_keeps_telemetry_and_writes_no_change_event(self):
        record_wan(self.db, self.source, self.reading())
        events = len(list(self.db.scalars(select(AuditEvent))))
        apply_batch(self.db, self.source, self.scan)
        self.assertIn("wan", self.details())
        self.assertEqual(len(list(self.db.scalars(select(AuditEvent)))), events)

    def test_unknown_gateway_is_ignored(self):
        self.assertFalse(record_wan(self.db, self.source, WanObservation("ff:ff:ff:ff:ff:ff", {"uplinks": []})))

    def test_header_states(self):
        cases = [({}, "ready"), ({"wan1_up": False, "active": "wan2"}, "warning"),
                 ({"wan1_up": False, "wan2_up": False, "active": None}, "error")]
        for kwargs, state in cases:
            with self.subTest(state=state):
                record_wan(self.db, self.source, self.reading(**kwargs))
                entry = wan_health(self.db)[0]
                self.assertEqual((entry["name"], entry["state"]), ("WAN", state))


if __name__ == "__main__":
    unittest.main()
