# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""The changelog drafter: which commits count, where they go, and the entry."""

import importlib.util
from pathlib import Path
import re
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("changelog", ROOT / "scripts" / "changelog.py")
changelog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(changelog)


class ChangelogTests(unittest.TestCase):
    """Check the drafter's rules against made-up commits."""

    def test_quiet_commits_are_left_out(self):
        self.assertFalse(changelog.worth_listing("Add tests for racks", ["tests/test_racks.py"]))
        self.assertFalse(changelog.worth_listing("Tidy the docs", ["docs/setup.md", "README.md"]))
        self.assertFalse(changelog.worth_listing("Backlog: plan a thing", ["src/labrat/app.py"]))
        self.assertTrue(changelog.worth_listing("Add a rack view", ["src/labrat/static/js/racks.js", "tests/x.py"]))
        self.assertTrue(changelog.worth_listing("Fix the image", ["Dockerfile"]))

    def test_sections_follow_the_first_word(self):
        self.assertEqual(changelog.section_for("Show UPS power"), "Added")
        self.assertEqual(changelog.section_for("Limit failed sign-ins"), "Fixed")
        self.assertEqual(changelog.section_for("Rework the drawer"), "Changed")
        self.assertEqual(changelog.section_for(""), "Changed")

    def test_draft_groups_and_deduplicates(self):
        fake = [
            ("Add a rack view", ["src/labrat/a.py"]),
            ("Fix a cable bend.", ["src/labrat/b.js"]),
            ("Add a rack view", ["src/labrat/c.py"]),
            ("Document setup", ["src/labrat/templates/docs/setup.html"]),
            ("Rework the drawer", ["src/labrat/d.css"]),
        ]
        with patch.object(changelog, "commits", return_value=fake):
            text = changelog.draft("0.5.2", "v0.5.1")
        self.assertRegex(text, r"^## 0\.5\.2 \(\d{4}-\d\d-\d\d\)")
        self.assertEqual(text.count("- Add a rack view."), 1)
        self.assertIn("### Fixed\n\n- Fix a cable bend.", text)
        self.assertNotIn("Document setup", text)
        self.assertLess(text.index("### Added"), text.index("### Changed"))

    def test_current_version_matches_pyproject(self):
        version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
        self.assertEqual(changelog.current_version(), version)

    def test_changelog_has_an_entry_for_the_current_version(self):
        text = (ROOT / "CHANGELOG.md").read_text()
        self.assertRegex(text, rf"(?m)^## {re.escape(changelog.current_version())}\b")


if __name__ == "__main__":
    unittest.main()
