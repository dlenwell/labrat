# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Choose the color theme a browser sees.

The theme is a per-browser preference kept in a cookie, by the owner's
choice: it needs no account or schema change, and each browser or device
remembers its own. The server reads the cookie while drawing every page and
writes the choice to ``<html data-theme>``, because the content security
policy forbids inline scripts that could set it before the page paints;
without that, the default theme would flash on every load.

``system`` follows the device's light or dark mode in CSS
(``prefers-color-scheme``); the others are fixed. A theme that is not
``ready`` is listed in Settings but cannot be chosen yet.

Dark, Light, and Match my system also take an accent color from
``ACCENTS`` (a second cookie, written to ``<html data-accent>``). Disco has
its own palette and ignores it. Every accent's shades are chosen in
``main.css`` to keep at least 4.5:1 contrast as text on the theme's
surfaces and under text on solid accent fills.

Used by: app, pages.private, startup.
"""

from dataclasses import dataclass

from flask import request

THEME_COOKIE = "labrat_theme"
DEFAULT_THEME = "disco"
ACCENT_COOKIE = "labrat_accent"
DEFAULT_ACCENT = "blue"
# A year: long enough to feel permanent, short enough to expire on its own.
THEME_COOKIE_MAX_AGE = 365 * 24 * 60 * 60


@dataclass(frozen=True)
class Theme:
    """One choice on the Settings page.

    Attributes:
        key: Stable value stored in the cookie and ``data-theme``.
        label: Name shown in Settings.
        description: One line on what it looks like.
        ready: Whether its styles exist yet; unready themes cannot be chosen.
    """

    key: str
    label: str
    description: str
    ready: bool = True


THEMES: tuple[Theme, ...] = (
    Theme("disco", "Disco", "The control-room console: phosphor green, glowing lamps, signal colors."),
    Theme("dark", "Dark", "A calm, neutral dark theme without glows or scanlines."),
    Theme("light", "Light", "A neutral light theme for bright rooms."),
    Theme("system", "Match my system", "Dark or Light, following this device's setting."),
)


@dataclass(frozen=True)
class Accent:
    """One accent color for the Dark and Light themes.

    Attributes:
        key: Stable value stored in the cookie and ``data-accent``.
        label: Name shown in Settings.
    """

    key: str
    label: str


ACCENTS: tuple[Accent, ...] = (
    Accent("blue", "Blue"),
    Accent("red", "Red"),
    Accent("purple", "Purple"),
    Accent("pink", "Pink"),
)


def accent_choices() -> tuple[Accent, ...]:
    """List every accent in the order Settings shows them.

    Returns:
        tuple[Accent, ...]: The accents.
    """
    return ACCENTS


def chosen_accent(value: str | None) -> str | None:
    """Accept an accent key only if LabRat defines it.

    Args:
        value: A submitted or stored accent key.

    Returns:
        str | None: The key, or None when it is unknown or missing.
    """
    return value if any(accent.key == value for accent in ACCENTS) else None


def current_accent() -> str:
    """Read this browser's accent from its cookie.

    Returns:
        str: The chosen accent, or the default when none or an unknown one
            is stored.
    """
    return chosen_accent(request.cookies.get(ACCENT_COOKIE)) or DEFAULT_ACCENT


def theme_choices() -> tuple[Theme, ...]:
    """List every theme in the order Settings shows them.

    Returns:
        tuple[Theme, ...]: The themes.
    """
    return THEMES


def chosen_theme(value: str | None) -> str | None:
    """Return a theme key that may be chosen, or ``None``.

    Args:
        value: A submitted or stored theme key.

    Returns:
        str | None: The key when it names a ready theme.
    """
    for theme in THEMES:
        if theme.key == value and theme.ready:
            return theme.key
    return None


def current_theme() -> str:
    """Return this browser's theme, falling back to the default.

    An unknown or not-yet-ready value in the cookie is ignored rather than
    trusted, so a stale or edited cookie can never break a page.

    Returns:
        str: A ready theme key.
    """
    return chosen_theme(request.cookies.get(THEME_COOKIE)) or DEFAULT_THEME
