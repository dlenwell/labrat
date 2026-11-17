# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""UniFi collection across controller kinds and models LabRat does not know."""

import unittest
from unittest.mock import patch

from labrat.collector import unifi_network as un
from labrat.collector.adapters.unifi import UniFiAdapter
from labrat.collector.http import unifi_network_root

import test_step5_links as links
import test_unifi_network as network


class NetworkRootTests(unittest.TestCase):
    """Build the Network application root for each controller kind."""

    def test_controller_kinds(self):
        """A console proxies the APIs; a self-hosted application serves them at its root."""
        self.assertEqual(unifi_network_root({"url": "https://console.example/"}),
                         "https://console.example/proxy/network")
        self.assertEqual(unifi_network_root({"url": "https://console.example", "controller_type": "unifi_os"}),
                         "https://console.example/proxy/network")
        self.assertEqual(unifi_network_root({"url": "https://controller.example:8443",
                                             "controller_type": "network_application"}),
                         "https://controller.example:8443")

    def test_unknown_controller_type_is_refused(self):
        """Name the setting rather than guess a path."""
        with self.assertRaisesRegex(ValueError, "controller_type"):
            unifi_network_root({"url": "https://console.example", "controller_type": "cloud"})


class SelfHostedPollTests(unittest.TestCase):
    """A full adapter poll against a controller without the console prefix."""

    def test_poll_reads_unprefixed_paths(self):
        """Every request drops /proxy/network, and the batch matches a console's."""
        client = links.unifi_fixture(links.CLASSIC_DEVICES)
        client.responses = {url.replace("/proxy/network", ""): value for url, value in client.responses.items()}
        batch = UniFiAdapter(client).poll({"url": links.BASE, "api_key": "fake",
                                           "controller_type": "network_application"})
        console = UniFiAdapter(links.unifi_fixture(links.CLASSIC_DEVICES)).poll({"url": links.BASE, "api_key": "fake"})
        self.assertEqual(sorted(d.remote_id for d in batch.devices), sorted(d.remote_id for d in console.devices))
        self.assertEqual(len(batch.links), len(console.links))


class UnknownModelTests(unittest.TestCase):
    """An adopted device LabRat cannot classify is kept, not dropped."""

    def test_unclassified_device_is_other_equipment(self):
        """No type, an unfamiliar model, and no features: listed as other equipment."""
        client = links.unifi_fixture(links.CLASSIC_DEVICES)
        listing = client.responses[f"{links.INTEGRATION}/sites/site-1/devices?offset=0&limit=100"]
        listing["data"].append({"id": "new", "model": "ZZ-Future-1", "macAddress": "aa:aa:aa:00:00:09"})
        listing["totalCount"] += 1
        client.responses[f"{links.INTEGRATION}/sites/site-1/devices/new"] = {"id": "new", "name": "Future box"}
        batch = UniFiAdapter(client).poll({"url": links.BASE, "api_key": "fake"})
        device = next(d for d in batch.devices if d.remote_id == "site-1:new")
        self.assertEqual((device.name, device.category), ("Future box", "other_physical"))


class DeviceReadLimitTests(unittest.TestCase):
    """Devices past the per-snapshot read limit are flagged."""

    def test_limited_device_detail_is_recorded(self):
        """Over the limit, devices keep their list row and errors say so."""
        with patch.object(un, "MAX_DEVICE_READS", 0):
            snapshot = un.network_snapshot(network.routed(network.responses()), network.SETTINGS)
        self.assertTrue(snapshot["devices"])
        self.assertEqual(snapshot["errors"], {"device_detail": "limited"})


if __name__ == "__main__":
    unittest.main()
