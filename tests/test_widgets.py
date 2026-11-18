# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""The widget gallery page and the widget kit's markup (planning/specs/widgets.md)."""

import unittest

import test_step4c as fixture
from labrat.discovery.models import DiscoverySource


class WidgetGalleryTests(unittest.TestCase):
    """The gallery renders for a signed-in viewer and loads the widget kit."""

    setUp = fixture.Step4CTests.setUp

    def test_gallery_tabs_render_and_unknown_tabs_404(self):
        """Samples and Live render; anything else is a 404."""
        samples = self.client.get("/settings/widgets")
        self.assertEqual(samples.status_code, 200)
        page = samples.get_data(as_text=True)
        self.assertIn("css/widgets.css", page)
        self.assertIn("js/widgets.js", page)
        self.assertIn('id="widget-gallery-items"', page)
        live = self.client.get("/settings/widgets/live").get_data(as_text=True)
        self.assertIn('data-widget-feed="cluster.attention"', live)
        self.assertIn("js/widget-feeds.js", live)
        self.assertEqual(self.client.get("/settings/widgets/other").status_code, 404)

    def test_gallery_is_reached_by_url_only(self):
        """Settings no longer lists the gallery, but its address still opens it."""
        self.assertNotIn('href="/settings/widgets"', self.client.get("/settings").get_data(as_text=True))
        self.assertEqual(self.client.get("/settings/widgets").status_code, 200)

    def test_gallery_needs_sign_in(self):
        """A signed-out visitor never sees the gallery."""
        self.assertNotEqual(self.app.test_client().get("/settings/widgets").status_code, 200)

    def test_pages_load_the_kit_without_a_page_tooltip(self):
        """Cluster and Networks load the kit; the tooltip makes itself."""
        for path in ("/cluster", "/networks"):
            page = self.client.get(path).get_data(as_text=True)
            self.assertIn("js/widgets.js", page, path)
            self.assertNotIn("proxmox-tooltip", page, path)

    def test_networks_and_gallery_load_the_network_model_before_their_code(self):
        """Networks and the gallery feeds read the network model, loaded first."""
        for path, user in (("/networks", "js/networks.js"), ("/settings/widgets/live", "js/widget-feeds.js")):
            page = self.client.get(path).get_data(as_text=True)
            self.assertIn("js/network-model.js", page, path)
            self.assertLess(page.index("js/network-model.js"), page.index(user), path)
        live = self.client.get("/settings/widgets/live").get_data(as_text=True)
        for feed in ("network.wifi-channels", "network.sinkholes"):
            self.assertIn(feed, live)

    def test_drawer_pages_load_the_proxmox_section(self):
        """Pages with the device drawer load the widget kit and its Proxmox section."""
        for path in ("/topology", "/topology/clients"):
            page = self.client.get(path).get_data(as_text=True)
            for asset in ("js/widgets.js", "js/cluster-model.js", "js/device-cluster.js", "css/widgets.css"):
                self.assertIn(asset, page, f"{path}: {asset}")

    def test_home_lab_health_follows_enabled_sources(self):
        """Home's Lab health shows each half only when a source can feed it."""
        page = self.client.get("/home").get_data(as_text=True)
        self.assertNotIn("home-live-section", page)
        self.assertNotIn("js/widgets.js", page)
        with self.factory.begin() as db:
            db.add(DiscoverySource(name="proxmox:default", kind="proxmox", config_name="default"))
        page = self.client.get("/home").get_data(as_text=True)
        self.assertIn('data-widget-feed="cluster.attention"', page)
        self.assertIn('data-widget-limit="5"', page)
        self.assertNotIn("network.clients-by-network", page)
        self.assertIn("js/widget-feeds.js", page)
        with self.factory.begin() as db:
            db.add(DiscoverySource(name="unifi:default", kind="unifi", config_name="default"))
            db.query(DiscoverySource).filter_by(kind="proxmox").one().enabled = False
        page = self.client.get("/home").get_data(as_text=True)
        self.assertIn("network.clients-by-network", page)
        self.assertNotIn("cluster.attention", page)


if __name__ == "__main__":
    unittest.main()
