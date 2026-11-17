# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Check the Networks page's UniFi snapshot, zone paths, DNS sinkholes, API, and routes."""

import json
from types import SimpleNamespace
import unittest

import requests
from sqlalchemy import select

import test_step4c as fixture
from labrat.api.network import dns_sinkholes, zone_paths
from labrat.collector import unifi_network as un
from labrat.discovery.models import DiscoveredDevice, DiscoverySource

SETTINGS = {"url": "https://unifi.example.test", "api_key": "test-only"}
SITE = "site-1"
BASE = f"/sites/{SITE}"


def page(rows):
    """A one-page integration envelope.

    Args:
        rows: The page's records.

    Returns:
        dict: The envelope.
    """
    return {"offset": 0, "limit": 200, "count": len(rows), "totalCount": len(rows), "data": rows}


def routed(responses):
    """A fake client answering by path after the controller root.

    Args:
        responses: Payloads keyed by path after the controller root.

    Returns:
        Mock: A client whose ``get_json`` serves them.
    """
    def get_json(url, headers, verify):
        path = url.split("unifi.example.test", 1)[1]
        path = path.replace("/proxy/network/integration/v1", "")
        path = path.split("?offset")[0].split("&offset")[0]
        value = responses.get(path)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise requests.HTTPError("404")
        return value
    return SimpleNamespace(client=SimpleNamespace(get_json=get_json))


def responses():
    """A small site: two zones, two networks, three policies, a Pi-hole client.

    Returns:
        dict: Payloads keyed by path.
    """
    return {
        "/sites": page([{"id": SITE, "name": "Example", "internalReference": "default"}]),
        f"{BASE}/networks": page([{"id": "net-a"}, {"id": "net-b"}]),
        f"{BASE}/networks/net-a": {"id": "net-a", "name": "Home", "vlanId": 1, "zoneId": "zone-in", "enabled": True,
                                   "ipv4Configuration": {"hostIpAddress": "192.0.2.1", "prefixLength": 24,
                                                         "dhcpConfiguration": {"mode": "SERVER", "dnsServerIpAddressesOverride": ["192.0.2.53", "192.0.2.1"]}}},
        f"{BASE}/networks/net-b": {"id": "net-b", "name": "Lab", "vlanId": 20, "zoneId": "zone-lab", "enabled": True,
                                   "ipv4Configuration": {"hostIpAddress": "198.51.100.1", "prefixLength": 24}},
        f"{BASE}/traffic-matching-lists": page([{"id": "list-1", "name": "trusted", "type": "IPV4_ADDRESSES", "items": [{"type": "IP_ADDRESS", "value": "192.0.2.10"}]}]),
        f"{BASE}/firewall/zones": page([{"id": "zone-in", "name": "Internal", "networkIds": ["net-a"], "metadata": {"origin": "SYSTEM_DEFINED"}},
                                        {"id": "zone-lab", "name": "Lab", "networkIds": ["net-b"], "metadata": {"origin": "USER_DEFINED"}}]),
        f"{BASE}/firewall/policies": page([
            {"id": "p1", "name": "trusted can reach lab", "index": 10, "enabled": True, "action": {"type": "ALLOW"},
             "source": {"zoneId": "zone-in", "trafficFilter": {"type": "IP_ADDRESS", "ipAddressFilter": {"type": "TRAFFIC_MATCHING_LIST", "trafficMatchingListId": "list-1"}}},
             "destination": {"zoneId": "zone-lab"}, "metadata": {"origin": "USER_DEFINED"}},
            {"id": "p2", "name": "Block lab", "index": 20, "enabled": True, "action": {"type": "BLOCK"},
             "source": {"zoneId": "zone-in"}, "destination": {"zoneId": "zone-lab"}, "metadata": {"origin": "USER_DEFINED"}},
            {"id": "p3", "name": "disabled", "index": 5, "enabled": False, "action": {"type": "ALLOW"},
             "source": {"zoneId": "zone-in"}, "destination": {"zoneId": "zone-lab"}},
        ]),
        f"{BASE}/wifi/broadcasts": page([{"name": "Example Wi-Fi", "type": "STANDARD", "enabled": True,
                                          "securityConfiguration": {"type": "WPA2_PERSONAL", "passphrase": "never-kept"},
                                          "broadcastingFrequenciesGHz": [2.4, 5]}]),
        f"{BASE}/wans": page([{"id": "wan-1", "name": "Internet 1"}]),
        f"{BASE}/devices": page([{"id": "dev-1"}]),
        f"{BASE}/devices/dev-1": {"id": "dev-1", "name": "Example switch", "model": "Example", "state": "ONLINE",
                                  "features": {"switching": {}}, "interfaces": {"ports": [{"idx": 1, "state": "UP", "speedMbps": 1000}]}},
        f"{BASE}/devices/dev-1/statistics/latest": {"uptimeSec": 100, "cpuUtilizationPct": 5.5, "memoryUtilizationPct": 30},
        f"{BASE}/clients": page([{"type": "WIRED", "name": "pihole", "ipAddress": "192.0.2.53", "macAddress": "02:00:00:00:00:53", "uplinkDeviceId": "dev-1"}]),
        "/proxy/network/api/s/default/stat/sta": {"data": [{"mac": "02:00:00:00:00:53", "hostname": "pihole", "network": "Home", "satisfaction": 100}]},
        "/proxy/network/api/s/default/stat/health": {"data": [{"subsystem": "lan", "status": "ok", "num_user": 1}]},
        "/proxy/network/api/s/default/stat/rogueap": {"data": [{"channel": 6, "rssi": 20, "essid": "neighbour", "bssid": "02:00:00:00:00:99"}]},
    }


class SnapshotTests(unittest.TestCase):
    """The snapshot keeps curated fields and nothing secret or third-party."""

    def test_snapshot_curates_every_part(self):
        """Networks, policies, Wi-Fi, devices, clients, and neighbours are curated."""
        snapshot = un.network_snapshot(routed(responses()), SETTINGS)
        self.assertEqual(snapshot["errors"], {})
        home = snapshot["networks"][0]
        self.assertEqual((home["vlan"], home["gateway"], home["prefix"], home["dhcp"]["dns"]), (1, "192.0.2.1", 24, ["192.0.2.53", "192.0.2.1"]))
        first = snapshot["policies"][0]
        self.assertEqual(first["source"]["values"], ["list: trusted"])
        self.assertEqual(snapshot["devices"][0]["cpu"], 5.5)
        self.assertEqual(snapshot["clients"][0]["hostname"], "pihole")
        self.assertEqual(snapshot["neighbours"], [{"band": "2.4", "channel": 6, "count": 1, "strongest": 20}])
        text = json.dumps(snapshot)
        self.assertNotIn("never-kept", text)
        self.assertNotIn("neighbour", text.replace("neighbours", ""))

    def test_failed_part_is_named(self):
        """A refused part is named; the rest is read."""
        answers = responses()
        answers[f"{BASE}/wifi/broadcasts"] = requests.HTTPError("403")
        snapshot = un.network_snapshot(routed(answers), SETTINGS)
        self.assertEqual(snapshot["errors"], {"wifi": "unavailable"})
        self.assertEqual(len(snapshot["zones"]), 2)

    def test_zone_paths_and_sinkholes(self):
        """The first broad rule decides; narrower ones before it are exceptions."""
        snapshot = un.network_snapshot(routed(responses()), SETTINGS)
        [path] = zone_paths(snapshot["zones"], snapshot["policies"])
        self.assertEqual((path["verdict"], path["decided_by"]), ("block", "Block lab"))
        self.assertEqual([(item["name"], item["action"]) for item in path["exceptions"]], [("trusted can reach lab", "allow")])
        [sinkhole] = dns_sinkholes(snapshot, [{"name": "pihole-vm", "addresses": ["192.0.2.53"], "macs": []}])
        self.assertEqual(sinkhole["ip"], "192.0.2.53")
        self.assertTrue(sinkhole["likely_pihole"])
        self.assertEqual(len(sinkhole["evidence"]), 3)


class CurationEdgeTests(unittest.TestCase):
    """Filters, radios, paging, and verdict edges."""

    def test_filters_ports_ranges_and_negation(self):
        """Port ranges, address ranges, networks, and negation are summarized."""
        endpoint = {"zoneId": "z", "trafficFilter": {"type": "PORT", "portFilter": {"type": "PORTS", "matchOpposite": True, "items": [
            {"type": "PORT_NUMBER", "value": 53}, {"type": "PORT_NUMBER_RANGE", "start": 8000, "stop": 8080}]},
            "ipAddressFilter": {"type": "IP_ADDRESSES", "items": [{"type": "IP_ADDRESS_RANGE", "start": "192.0.2.10", "stop": "192.0.2.20"}]}}}
        summary = un._filter(endpoint, {})
        self.assertEqual((summary["kind"], summary["ports"], summary["negate"]), ("ports", ["53", "8000–8080"], True))
        self.assertEqual(summary["values"], ["192.0.2.10–192.0.2.20"])
        networks = un._filter({"zoneId": "z", "trafficFilter": {"type": "NETWORK", "networkFilter": {"networkIds": ["n1"]}}}, {})
        self.assertEqual((networks["kind"], networks["values"]), ("networks", ["network:n1"]))
        missing = un._filter({"zoneId": "z", "trafficFilter": {"type": "IP_ADDRESS", "ipAddressFilter": {"type": "TRAFFIC_MATCHING_LIST", "trafficMatchingListId": "gone"}}}, {})
        self.assertEqual(missing["values"], ["list: unknown"])
        policy = un._policy({"id": "p", "action": {"type": "ALLOW"}, "ipProtocolScope": {"ipVersion": "IPV4", "protocolFilter": {"preset": {"name": "TCP_UDP"}}},
                             "connectionStateFilter": ["NEW"], "source": {"zoneId": "a"}, "destination": {"zoneId": "b"}}, {})
        self.assertEqual((policy["protocol"], policy["states"]), ("TCP_UDP", ["NEW"]))

    def test_device_radios_and_paging(self):
        """Radios are kept; paging follows totals and refuses runaway pages."""
        device = un._device({"id": "ap", "features": ["accessPoint"], "interfaces": {"radios": [{"frequencyGHz": 5, "channel": 36, "channelWidthMHz": 80}]}}, None)
        self.assertEqual(device["radios"][0]["channel"], 36)
        pages = iter([{"totalCount": 3, "data": [{"id": 1}, {"id": 2}]}, {"totalCount": 3, "data": [{"id": 3}]}])
        reader = un._Reader(SimpleNamespace(client=SimpleNamespace(get_json=lambda *args: next(pages))), SETTINGS)
        self.assertEqual(len(reader.pages("/x")), 3)
        reader = un._Reader(SimpleNamespace(client=SimpleNamespace(get_json=lambda *args: {"data": "bad"})), SETTINGS)
        with self.assertRaises(ValueError):
            reader.pages("/x")
        late = un._Reader(SimpleNamespace(client=SimpleNamespace(get_json=lambda *args: {})), SETTINGS, clock=iter([0, 999]).__next__)
        with self.assertRaises(TimeoutError):
            late.get("https://unifi.example.test/x")

    def test_unknown_verdict_without_a_broad_rule(self):
        """Only narrow rules leave the pair's default unknown."""
        rows = zone_paths([], [{"enabled": True, "name": "dns", "index": 1, "action": "ALLOW",
                                "source": {"zone": "a"}, "destination": {"zone": "b", "ports": ["53"]}}])
        self.assertEqual((rows[0]["verdict"], len(rows[0]["exceptions"])), ("unknown", 1))
        named = dns_sinkholes({"networks": [], "clients": []}, [{"name": "pi-hole", "addresses": ["192.0.2.9"], "macs": []}])
        self.assertEqual((named[0]["ip"], named[0]["names"]), ("192.0.2.9", ["pi-hole"]))


class StoredSnapshotTests(unittest.TestCase):
    """The snapshot lands on the gateway; the API and pages read it back."""

    setUp = fixture.Step4CTests.setUp

    def add_gateway(self):
        """Store a UniFi source with a gateway observation.

        Returns:
            int: The source's ID.
        """
        with self.factory.begin() as db:
            source = DiscoverySource(name="UniFi example", kind="unifi", config_name="default")
            db.add(source)
            db.flush()
            db.add(DiscoveredDevice(source_id=source.id, remote_id="site-1:gw", name="Gateway", category="router",
                                    details_json=json.dumps({"wan": {"state": "up"}})))
            return source.id

    def test_source_without_snapshot_and_guest_match(self):
        """A source with no snapshot is listed; clients match Proxmox guests by MAC."""
        source_id = self.add_gateway()
        self.assertIsNone(self.client.get("/api/v1/network").json["sources"][0]["snapshot"])
        with self.factory.begin() as db:
            proxmox = DiscoverySource(name="Proxmox example", kind="proxmox", config_name="default")
            db.add(proxmox)
            db.flush()
            db.add(DiscoveredDevice(source_id=proxmox.id, remote_id="node-a", name="node-a", category="server", details_json=json.dumps({
                "platform": "proxmox", "proxmox_node": {"guest_agent": {"105": {"addresses": [{"address": "192.0.2.53"}]}}},
                "proxmox_clients": {"guests": [{"vmid": 105, "name": "dns-vm", "macs": ["02:00:00:00:00:53"]}]}})))
        un.collect_network_snapshot(self.factory, SimpleNamespace(id=source_id), routed(responses()), SETTINGS)
        data = self.client.get("/api/v1/network").json["sources"][0]
        self.assertEqual(data["snapshot"]["clients"][0]["guest"]["name"], "dns-vm")

    def test_collect_api_and_routes(self):
        """Collect, serve with derived paths, and render every tab."""
        source_id = self.add_gateway()
        un.collect_network_snapshot(self.factory, SimpleNamespace(id=source_id), routed(responses()), SETTINGS)
        with self.factory() as db:
            details = json.loads(db.scalar(select(DiscoveredDevice.details_json)))
        self.assertTrue(details["unifi_network"]["available"])
        data = self.client.get("/api/v1/network").json["sources"][0]
        self.assertEqual(data["paths"][0]["verdict"], "block")
        self.assertEqual(data["snapshot"]["networks"][0]["cidr"], "192.0.2.0/24")
        self.assertEqual(data["sinkholes"][0]["ip"], "192.0.2.53")
        for path in ("/networks", "/networks/zones", "/networks/clients"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertEqual(self.client.get("/networks/unknown").status_code, 404)
        adapter = routed(responses())
        un.collect_network_snapshot(self.factory, SimpleNamespace(id=source_id), adapter, SETTINGS)
        un.collect_network_snapshot(self.factory, SimpleNamespace(id=source_id), adapter, SETTINGS)  # throttled
        un.collect_network_snapshot(self.factory, SimpleNamespace(id=source_id), adapter, {"api_key": "x"})  # no URL
        failing = SimpleNamespace(client=SimpleNamespace(get_json=lambda *args: (_ for _ in ()).throw(requests.ConnectionError("down"))))
        un.collect_network_snapshot(self.factory, SimpleNamespace(id=source_id), failing, SETTINGS)
        with self.factory() as db:
            details = json.loads(db.scalar(select(DiscoveredDevice.details_json)))
        self.assertFalse(details["unifi_network"]["available"])


if __name__ == "__main__":
    unittest.main()
