# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Check the header's Locations menu and Docs living in the footer."""

import unittest

import test_step4c as fixture


class NavigationTests(unittest.TestCase):
    """Use real authenticated routes and disposable migrated storage."""

    setUp = fixture.Step4CTests.setUp
    post = fixture.Step4CTests.post
    create_locations = fixture.Step4CTests.create_locations

    @staticmethod
    def header(page):
        """Return the header markup of a rendered page.

        Args:
            page: A rendered page's HTML.

        Returns:
            str: The markup inside its site header.
        """
        return page.split('<header class="site-header">')[1].split("</header>")[0]

    def test_locations_menu_lists_locations_and_rooms(self):
        """All locations first, then each location with its rooms, by name."""
        site, room, _ = self.create_locations()
        cabin = self.post("/api/v1/locations", {"kind": "site", "name": "cabin"}).json["id"]
        office = self.post("/api/v1/locations", {"kind": "room", "site_id": site, "name": "Office"}).json["id"]
        empty = self.post("/api/v1/locations", {"kind": "site", "name": "Annex"}).json["id"]
        header = self.header(self.client.get("/home").get_data(as_text=True))
        menu = header.split('class="nav-menu locations-menu"')[1].split("</details>")[0]
        self.assertLess(menu.index("All locations"), menu.index(">Annex<"))
        # Case-insensitive order: Annex, cabin, Home; rooms Basement, Office.
        self.assertLess(menu.index(">Annex<"), menu.index(">cabin<"))
        self.assertLess(menu.index(">cabin<"), menu.index(">Home<"))
        self.assertLess(menu.index(f'href="/areas/{room}"'), menu.index(f'href="/areas/{office}"'))
        self.assertIn(f'class="locations-menu-rooms" id="nav-rooms-{site}" aria-label="Areas in Home"', menu)
        self.assertEqual(menu.count('class="locations-menu-site"'), 3)
        self.assertNotIn("locations-menu-toggle", menu)
        self.assertNotIn("aria-expanded", menu)
        # Rooms belong to their location in one inline list, without controls.
        section = menu.split('>Home</a>')[1].split('</ul>')[0]
        self.assertIn(f'href="/areas/{room}"', section)
        self.assertIn(f'href="/areas/{office}"', section)
        # Locations without rooms still link directly to their detail page.
        for bare in (cabin, empty):
            self.assertNotIn(f"nav-rooms-{bare}", menu)
        self.assertIn(f'href="/locations/{empty}"', menu)

    def test_current_location_and_room_are_marked(self):
        """The menu marks the open location or room and reads as current."""
        site, room, _ = self.create_locations()
        page = self.header(self.client.get(f"/areas/{room}").get_data(as_text=True))
        self.assertIn('class="nav-menu-button is-current"', page)
        self.assertIn(f'href="/areas/{room}" title="Basement" aria-label="Basement" aria-current="page"', page)
        page = self.header(self.client.get(f"/locations/{site}").get_data(as_text=True))
        self.assertIn(f'href="/locations/{site}" aria-current="page"', page)
        page = self.header(self.client.get("/home").get_data(as_text=True))
        self.assertNotIn("is-current", page)
        # Only the Home tab is current: no location, room, or other page.
        self.assertEqual(page.count('aria-current="page"'), 1)
        self.assertIn('href="/home" aria-current="page">Home</a>', page)
        page = self.header(self.client.get("/topology/clients").get_data(as_text=True))
        self.assertIn('href="/topology" aria-current="page">Discovery</a>', page)

    def test_long_room_labels_keep_their_full_accessible_name(self):
        """Cap visible room names at twelve characters without losing the name."""
        site, _, _ = self.create_locations()
        room = self.post("/api/v1/locations", {
            "kind": "room", "site_id": site, "name": "A long room label",
        }).json["id"]
        menu = self.header(self.client.get("/home").get_data(as_text=True))
        self.assertIn(
            f'href="/areas/{room}" title="A long room label" '
            'aria-label="A long room label">A long room…</a>', menu,
        )

    def test_docs_moved_to_the_footer(self):
        """Docs leaves the header, signed in or out, and is in the footer."""
        for signed_in in (True, False):
            if not signed_in:
                self.client.post("/signout", data={"_csrf_token": self.csrf})
            page = self.client.get("/").get_data(as_text=True)
            with self.subTest(signed_in=signed_in):
                self.assertNotIn('href="/docs"', self.header(page))
                footer = page.split('<footer class="site-footer">')[1]
                self.assertIn('<a href="/docs">Docs</a>', footer)
        # Signed out, there is no Locations menu at all.
        self.assertNotIn("locations-menu", self.client.get("/").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
