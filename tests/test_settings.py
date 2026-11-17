# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Check the Settings page, the per-browser theme cookie, and the account menu."""

import re
import unittest
from pathlib import Path
from unittest.mock import patch

import test_step4c as fixture
from labrat import themes
from labrat.themes import ACCENT_COOKIE, ACCENTS, THEME_COOKIE, Theme

MAIN_CSS = Path(__file__).resolve().parents[1] / "src/labrat/static/css/main.css"


def _luminance(color):
    """Relative luminance of a #rrggbb color (WCAG 2).

    Args:
        color: The color.

    Returns:
        float: Its luminance, 0 to 1.
    """
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first, second):
    """WCAG contrast ratio between two #rrggbb colors.

    Args:
        first: One color.
        second: The other.

    Returns:
        float: The ratio, 1 to 21.
    """
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class SettingsTests(unittest.TestCase):
    """Use real authenticated routes and disposable migrated storage."""

    setUp = fixture.Step4CTests.setUp

    def save(self, theme, token=None, **extra):
        """Post the appearance form as the signed-in admin.

        Args:
            theme: The theme to choose.
            token: The CSRF token; the session's by default.
            **extra: Form fields added to the submission.

        Returns:
            The response.
        """
        return self.client.post("/settings/appearance", data={
            "_csrf_token": self.csrf if token is None else token, "theme": theme, **extra,
        })

    def test_settings_page_and_account_menu(self):
        """Settings lists every theme; the header menu links to it."""
        page = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('<html lang="en" data-theme="disco" data-accent="blue">', page)
        for label in ("Disco", "Dark", "Light", "Match my system"):
            self.assertIn(label, page)
        self.assertIn('value="disco"\n                 checked', page)
        self.assertNotIn("Coming soon", page)
        self.assertIn('class="nav-menu account-menu"', page)
        self.assertIn('href="/settings" aria-current="page"><span>Settings</span>', page)
        self.assertIn('href="/profile"><span>Profile</span>', page)
        self.assertIn("><span>Sign out</span>", page)
        self.assertEqual(page.count('class="page-icon account-menu-icon"'), 3)
        self.assertNotIn("Appearance saved", page)

    def test_save_sets_a_browser_cookie(self):
        """A ready theme is stored in a long-lived, HTTP-only cookie."""
        response = self.save("disco")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/settings?saved=appearance"))
        cookie = response.headers["Set-Cookie"]
        for part in (f"{THEME_COOKIE}=disco", "HttpOnly", "SameSite=Lax", "Path=/", "Max-Age=31536000"):
            self.assertIn(part, cookie)
        self.assertIn("Appearance saved", self.client.get("/settings?saved=appearance").get_data(as_text=True))

    def test_refused_choices_and_forged_requests(self):
        """Unknown or unready themes and bad tokens change nothing."""
        for theme, token, status in (("neon", None, 400), ("", None, 400),
                                     ("disco", "forged", 400)):
            with self.subTest(theme=theme, token=token):
                response = self.save(theme, token)
                self.assertEqual(response.status_code, status)
                self.assertNotIn("Set-Cookie", response.headers)

    def test_cookie_drives_every_page_and_bad_values_fall_back(self):
        """Pages draw the cookie's theme; a stale or edited value is ignored."""
        self.assertEqual(self.save("light").status_code, 302)
        self.assertIn('data-theme="light"', self.client.get("/home").get_data(as_text=True))
        # Signed-out pages use it too: it belongs to the browser.
        self.client.post("/signout", data={"_csrf_token": self.csrf})
        self.client.set_cookie(THEME_COOKIE, "system")
        self.assertIn('data-theme="system"', self.client.get("/").get_data(as_text=True))
        # A theme still being built cannot be chosen or kept.
        unready = tuple(Theme(t.key, t.label, t.description, ready=t.key != "light") for t in themes.THEMES)
        with patch.object(themes, "THEMES", unready):
            self.client.set_cookie(THEME_COOKIE, "light")
            page = self.client.get("/").get_data(as_text=True)
            self.assertIn('data-theme="disco"', page)
        for value in ("neon", '"><script>'):
            self.client.set_cookie(THEME_COOKIE, value)
            self.assertIn('data-theme="disco"', self.client.get("/").get_data(as_text=True))

    def test_accent_choice(self):
        """Settings offers four accents; the choice is a cookie on <html>."""
        page = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('data-accent="blue"', page)
        for label in ("Blue", "Red", "Purple", "Pink"):
            self.assertIn(f'<span class="accent-choice-name">{label}</span>', page)
        response = self.save("light", accent="purple")
        self.assertEqual(response.status_code, 302)
        cookies = response.headers.getlist("Set-Cookie")
        self.assertTrue(any(c.startswith(f"{ACCENT_COOKIE}=purple") and "HttpOnly" in c for c in cookies))
        page = self.client.get("/home").get_data(as_text=True)
        self.assertIn('data-theme="light" data-accent="purple"', page)
        # A form without an accent keeps the current one; a bad one is refused.
        self.assertEqual(self.save("dark").status_code, 302)
        self.assertIn('data-accent="purple"', self.client.get("/home").get_data(as_text=True))
        refused = self.save("dark", accent="green")
        self.assertEqual(refused.status_code, 400)
        self.assertNotIn("Set-Cookie", refused.headers)
        self.client.set_cookie(ACCENT_COOKIE, "orange")
        self.assertIn('data-accent="blue"', self.client.get("/home").get_data(as_text=True))

    def test_accents_stay_readable(self):
        """Each accent keeps 4.5:1 contrast on its theme's surfaces."""
        css = MAIN_CSS.read_text(encoding="utf-8")

        def light_dark(token):
            match = re.search(rf"--{token}: light-dark\((#[0-9a-f]{{6}}), (#[0-9a-f]{{6}})\)", css)
            self.assertIsNotNone(match, token)
            return match.groups()

        surfaces = [light_dark(name) for name in (
            "color-page-background", "color-panel-background", "color-surface-hover",
            "color-surface-raised")]
        for accent in ACCENTS:
            for index, mode in enumerate(("light", "dark")):
                color = re.search(rf"--accent-choice-{accent.key}-{mode}: (#[0-9a-f]{{6}});", css).group(1)
                for surface in surfaces:
                    with self.subTest(accent=accent.key, mode=mode, surface=surface[index]):
                        self.assertGreaterEqual(_contrast(color, surface[index]), 4.5)

    def test_sign_in_required(self):
        """Settings belongs to signed-in users."""
        self.client.post("/signout", data={"_csrf_token": self.csrf})
        self.assertEqual(self.client.get("/settings").status_code, 302)
        self.assertEqual(self.client.post("/settings/appearance", data={"theme": "disco"}).status_code, 302)


if __name__ == "__main__":
    unittest.main()
