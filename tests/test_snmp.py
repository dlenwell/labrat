# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Validate SNMP decoding and failure handling without sending network packets."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from labrat.collector import snmp


class SnmpTests(unittest.TestCase):
    """Keep interface identity and explicit up/down states distinct from unknown."""

    def test_required_settings_trim_and_never_echo_credentials(self):
        """Report the invalid option key while keeping private values out of errors."""
        self.assertEqual(snmp._required_setting({'username': ' user '}, 'username'), 'user')
        for value in (None, '', '  ', 123, ['private-secret']):
            with self.subTest(value=value), self.assertRaises(ValueError) as error:
                snmp._required_setting({'username': value}, 'username')
            self.assertIn('username', str(error.exception))
            self.assertNotIn('private-secret', str(error.exception))

    def test_walk_indexes_rows_and_skips_column_root(self):
        """Table rows retain their interface index; the root is not a row."""
        root = tuple(int(x) for x in snmp._IF_NAME.split('.'))
        async def walk(*args, **kwargs):
            yield None, None, None, [(root, 'skip'), ((*root, 7), 'eth7'), ((*root, 9), 'eth9')]
        with patch.object(snmp, 'walk_cmd', walk):
            result = asyncio.run(snmp._walk_column(Mock(), Mock(), Mock(), snmp._IF_NAME))
        self.assertEqual(result, {7: 'eth7', 9: 'eth9'})

    def test_walk_errors_are_sanitized(self):
        """Both transport errors and agent errors abort a column without raw details."""
        for indication, status in (('private-endpoint', None), (None, 'private-error')):
            async def walk(*args, **kwargs):
                yield indication, status, None, []
            with self.subTest(indication=indication), patch.object(snmp, 'walk_cmd', walk), self.assertRaises(RuntimeError) as error:
                asyncio.run(snmp._walk_column(Mock(), Mock(), Mock(), snmp._IF_NAME))
            self.assertNotIn('private-', str(error.exception))

    def test_interface_names_macs_and_states(self):
        """Use descriptions as fallback, discard invalid MACs, and preserve unknown states."""
        columns = [
            {1: SimpleNamespace(prettyPrint=lambda: ' eth0 '), 2: '', 3: 'eth2', 4: 'eth3', 5: 'eth4', 6: 'eth5', 7: 'eth6'},
            {2: 'fallback', 8: ''},
            {1: bytes.fromhex('020000000001'), 2: b'\0' * 6, 3: b'bad', 4: bytes.fromhex('020000000004')},
            {1: 1, 2: 2, 3: 3, 4: None, 5: 'bad', 6: 7},
        ]
        engine = Mock()
        settings = {'username': 'test', 'auth_passphrase': 'test-auth', 'priv_passphrase': 'test-priv'}
        with patch.object(snmp, 'SnmpEngine', return_value=engine), patch.object(snmp, 'UsmUserData') as auth, patch.object(
            snmp, 'UdpTransportTarget', SimpleNamespace(create=AsyncMock(return_value='target'))
        ) as transport, patch.object(snmp, '_walk_column', AsyncMock(side_effect=columns)):
            result = asyncio.run(snmp._read_interfaces('192.0.2.1', settings))
        self.assertEqual(result['eth0'], {'mac': '02:00:00:00:00:01', 'link_up': True})
        self.assertEqual(result['fallback'], {'mac': None, 'link_up': False})
        for name in ('eth2', 'eth3', 'eth4', 'eth5', 'eth6'):
            self.assertIsNone(result[name]['link_up'])
        self.assertNotIn('', result)
        self.assertIsNone(result['eth2']['mac'])
        self.assertEqual(result['eth3']['mac'], '02:00:00:00:00:04')
        auth.assert_called_once_with('test', 'test-auth', 'test-priv',
            authProtocol=snmp.USM_AUTH_HMAC96_SHA, privProtocol=snmp.USM_PRIV_CFB128_AES)
        transport.create.assert_awaited_once_with(('192.0.2.1', 161), timeout=2, retries=1)
        engine.close_dispatcher.assert_called_once()

    def test_column_failure_closes_dispatcher(self):
        """An interrupted table read releases the SNMP engine."""
        engine = Mock()
        settings = dict(username='test', auth_passphrase='auth', priv_passphrase='priv')
        with patch.object(snmp, 'SnmpEngine', return_value=engine), patch.object(snmp, 'UsmUserData'), patch.object(
            snmp, 'UdpTransportTarget', SimpleNamespace(create=AsyncMock())
        ), patch.object(snmp, '_walk_column', AsyncMock(side_effect=RuntimeError('offline'))), self.assertRaises(RuntimeError):
            asyncio.run(snmp._read_interfaces('192.0.2.1', settings))
        engine.close_dispatcher.assert_called_once()

    def test_public_reader_normalizes_ip_and_rejects_hostnames(self):
        """Only explicit IP endpoints reach the asynchronous SNMP reader."""
        with patch.object(snmp, '_read_interfaces', AsyncMock(return_value={'eth0': {'link_up': True}})) as read:
            self.assertEqual(snmp.read_physical_interfaces('2001:db8::01', {}), {'eth0': {'link_up': True}})
            read.assert_awaited_once_with('2001:db8::1', {})
            with self.assertRaises(ValueError):
                snmp.read_physical_interfaces('not-an-ip.example', {})
            self.assertEqual(read.await_count, 1)
