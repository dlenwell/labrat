# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""SNMPv3 protocol and port settings, and errors that name the likely fix."""

import asyncio
import unittest
from unittest.mock import patch

from labrat.collector import snmp


class SnmpOptionTests(unittest.TestCase):
    """Choose protocols and port from settings, with today's defaults."""

    def test_defaults_match_the_net_snmp_example(self):
        """No options: SHA, AES-128, UDP 161."""
        self.assertEqual(snmp.snmp_options({}),
                         (snmp.AUTH_PROTOCOLS["sha"], snmp.PRIV_PROTOCOLS["aes"], 161))

    def test_named_protocols_and_port(self):
        """Each Net-SNMP protocol name maps to its pysnmp protocol."""
        auth, priv, port = snmp.snmp_options({"auth_protocol": "sha256", "priv_protocol": "aes256", "port": 1161})
        self.assertEqual((auth, priv, port), (snmp.AUTH_PROTOCOLS["sha256"], snmp.PRIV_PROTOCOLS["aes256"], 1161))
        self.assertNotEqual(snmp.PRIV_PROTOCOLS["aes256"], snmp.PRIV_PROTOCOLS["aes256c"])

    def test_invalid_options_name_the_setting(self):
        """Unknown protocols and bad ports are refused by name."""
        for settings, key in (({"auth_protocol": "md5"}, "auth_protocol"), ({"priv_protocol": "des"}, "priv_protocol"),
                              ({"port": 0}, "port"), ({"port": "161"}, "port")):
            with self.subTest(settings=settings), self.assertRaisesRegex(ValueError, key):
                snmp.snmp_options(settings)


class IndicationTests(unittest.TestCase):
    """An agent's refusal names the setting to check."""

    def test_wrong_digest_points_at_the_authentication_protocol(self):
        """A digest mismatch says to check auth_protocol, without any secret."""
        async def refused(*args, **kwargs):
            yield "wrongDigest", None, None, []

        with patch.object(snmp, "walk_cmd", refused), self.assertRaises(RuntimeError) as caught:
            asyncio.run(snmp._walk_column(None, None, None, "1.3.6.1.2.1.31.1.1.1.1"))
        self.assertIn("wrongDigest", str(caught.exception))
        self.assertIn("auth_protocol", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
