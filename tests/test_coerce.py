# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Checks for the shared value readers in ``labrat.coerce``."""

import unittest

from labrat.coerce import integer, json_object, mac_address, number, text


class CoerceTests(unittest.TestCase):
    """Each reader refuses what it cannot trust and keeps what it can."""

    def test_json_object_accepts_only_objects(self) -> None:
        """Missing, malformed, and non-object JSON all read as empty."""
        self.assertEqual(json_object('{"a": 1}'), {"a": 1})
        for value in (None, "", "not json", "[1, 2]", "3"):
            self.assertEqual(json_object(value), {})

    def test_mac_address_normalizes_separators(self) -> None:
        """Bare, colon, and dash forms become twelve lowercase digits."""
        for value in ("AABBCCDDEEFF", "aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", " aabbccddeeff "):
            self.assertEqual(mac_address(value), "aabbccddeeff")
        for value in (None, 12, "aa:bb:cc", "eth0"):
            self.assertIsNone(mac_address(value))

    def test_number_refuses_booleans_text_and_non_finite(self) -> None:
        """Strings count only when asked; a minimum refuses smaller values."""
        self.assertEqual(number(3), 3.0)
        self.assertIsNone(number(True))
        self.assertIsNone(number("3"))
        self.assertEqual(number("3.5", strings=True), 3.5)
        self.assertIsNone(number("abc", strings=True))
        self.assertIsNone(number(float("nan")))
        self.assertIsNone(number(float("inf")))
        self.assertIsNone(number(-1, minimum=0))
        self.assertEqual(number(0, minimum=0), 0.0)

    def test_integer_rounds_unless_truncating(self) -> None:
        """Whole numbers round to nearest by default."""
        self.assertEqual(integer(2.6), 3)
        self.assertEqual(integer(2.6, truncate=True), 2)
        self.assertEqual(integer("7", strings=True), 7)
        self.assertIsNone(integer(None))

    def test_text_trims_and_bounds(self) -> None:
        """Blank text is missing unless spacing is kept."""
        self.assertEqual(text("  name  "), "name")
        self.assertEqual(text("abcdef", 3), "abc")
        self.assertIsNone(text("   "))
        self.assertEqual(text("  x", strip=False), "  x")
        self.assertIsNone(text(5))


if __name__ == "__main__":
    unittest.main()
