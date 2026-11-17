# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""The project website build: docs rendered from the app, links made relative."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_site", ROOT / "scripts" / "build_site.py")
build_site = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_site)


class LinkRewriteTests(unittest.TestCase):
    """Map app URLs to website files and back to relative links."""

    def test_targets(self):
        self.assertEqual(build_site.target_for("/"), "./")
        self.assertEqual(build_site.target_for("/docs/tour#nodes"), "docs/tour/#nodes")
        self.assertEqual(build_site.target_for("/static/css/main.css"), "static/css/main.css")
        self.assertIsNone(build_site.target_for("/signin"))
        self.assertIsNone(build_site.target_for("/static/css/missing.css"))

    def test_relative_links(self):
        self.assertEqual(build_site.relative("static/css/main.css", "docs/tour/index.html"), "../../static/css/main.css")
        self.assertEqual(build_site.relative("docs/setup/", "docs/tour/index.html"), "../setup/")
        self.assertEqual(build_site.relative("./", "docs/index.html"), "../")
        self.assertEqual(build_site.relative("docs/", "index.html"), "docs/")

    def test_app_only_links_keep_their_text(self):
        html = '<p>Add one in <a class="x" href="/locations">Locations</a> or <a href="/docs/tour">the tour</a>.</p>'
        out = build_site.rewrite(html, "docs/setup/index.html")
        self.assertIn('<span class="x">Locations</span>', out)
        self.assertIn('<a href="../tour/">the tour</a>', out)

    def test_unknown_asset_fails(self):
        with self.assertRaises(build_site.SiteError):
            build_site.rewrite('<img src="/elsewhere.png">', "index.html")


class BuildTests(unittest.TestCase):
    """Build the whole website once into a temporary folder."""

    @classmethod
    def setUpClass(cls):
        """Build the site."""
        cls.temp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.temp.name) / "site"
        cls.pages = build_site.build(cls.out, "https://example.invalid/labrat")

    @classmethod
    def tearDownClass(cls):
        """Remove the build."""
        cls.temp.cleanup()

    def test_every_docs_page_and_the_landing_page_are_built(self):
        self.assertEqual(self.pages, 7)
        for page in build_site.DOCS_PAGES.values():
            self.assertTrue((self.out / page).is_file(), page)
        self.assertEqual((self.out / "CNAME").read_text().strip(), "labrat.foo")

    def test_landing_page_carries_the_apps_animated_hero(self):
        html = (self.out / "index.html").read_text()
        self.assertIn('class="rack-preview"', html)
        self.assertIn("landing-rat-peek", html)
        self.assertNotIn("<!-- app:", html)
        self.assertNotIn("api-status", html)
        self.assertIn("https://example.invalid/labrat", html)
        self.assertNotIn("{{ repo_url }}", html)

    def test_docs_use_the_website_header_and_relative_links(self):
        html = (self.out / "docs" / "tour" / "index.html").read_text()
        self.assertNotIn("Sign in", html)
        self.assertIn('href="https://example.invalid/labrat">Source</a>', html)
        self.assertNotIn('href="/', html)
        self.assertNotIn('src="/', html)

    def test_no_broken_links(self):
        self.assertEqual(build_site.check_links(self.out), [])


if __name__ == "__main__":
    unittest.main()
