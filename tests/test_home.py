# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Check the home page's equipment-type composition bar."""

import re
import unittest

import test_step4c as fixture
from labrat.discovery.models import DiscoveredDevice, DiscoverySource
from labrat.pages.private import _lab_composition


class HomeCompositionTests(unittest.TestCase):
    """Use real authenticated routes and disposable migrated storage."""

    setUp = fixture.Step4CTests.setUp
    add_observations = fixture.Step4CTests.add_observations

    def test_empty_lab_has_no_composition_bar(self):
        """With nothing observed, the bar and its legend are left out."""
        page = self.client.get("/home").get_data(as_text=True)
        self.assertNotIn("home-mix-bar", page)

    def test_types_are_counted_ordered_and_folded(self):
        """Known types keep their order; unknown types fold into Other."""
        self.add_observations()
        with self.factory.begin() as db:
            source = db.query(DiscoverySource).one()
            db.add_all([
                DiscoveredDevice(source_id=source.id, remote_id="server-a", name="A", category="server"),
                DiscoveredDevice(source_id=source.id, remote_id="nas-a", name="N", category="storage"),
                DiscoveredDevice(source_id=source.id, remote_id="hidden", name="H", category="server",
                                 visibility="ignored"),
            ])
        with self.factory() as db:
            parts = _lab_composition(db)
        self.assertEqual([(p["category"], p["count"]) for p in parts],
                         [("server", 1), ("switch", 2), ("other", 1)])
        self.assertAlmostEqual(sum(p["share"] for p in parts), 1.0)
        page = self.client.get("/home").get_data(as_text=True)
        self.assertIn("What's in your lab", page)
        self.assertIn("Switches <strong>2</strong>", page)
        # Segment widths are SVG attributes (no inline styles) and fill the bar.
        widths = [float(value) for value in re.findall(r'<rect data-category="[a-z_]+" x="[0-9.]+" y="0" width="([0-9.]+)"', page)]
        self.assertEqual(len(widths), 3)
        self.assertAlmostEqual(sum(widths), 1000, places=1)
        self.assertNotIn('style="', page.split("home-mix")[1].split("</ul>")[0])


if __name__ == "__main__":
    unittest.main()
