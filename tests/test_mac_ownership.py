# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Check derived host-interface MAC ownership rules and their ambiguity limits."""
import json
import unittest
from unittest.mock import patch

import test_clients as fixtures
from labrat.discovery.mac_ownership import interface_owners
from labrat.discovery.models import DiscoverySource, DiscoveredDevice

CONFIG = fixtures.CONFIG
# Synthetic universal addresses (U/L bit clear) for a two-port host adapter.
PORT_A, PORT_B = '00005e0010a0', '00005e0010a4'


class InterfaceOwnerRuleTests(unittest.TestCase):
    """Exercise the pure matching rules without storage."""

    index = {PORT_A: {'host-a'}, PORT_B: {'host-a'}}

    def test_exact_match(self):
        """A known MAC belongs to its owners."""
        self.assertEqual(interface_owners(PORT_A, self.index), ({'host-a'}, 'exact'))

    def test_locally_administered_twin(self):
        """A U/L-flipped copy of a known universal MAC is the same interface."""
        self.assertEqual(interface_owners('02005e0010a4', self.index), ({'host-a'}, 'locally_administered'))
        self.assertEqual(interface_owners('02005e0010a5', self.index), (set(), None))

    def test_adapter_block_needs_opt_in(self):
        """A sibling in the adapter block matches only when the caller allows it."""
        self.assertEqual(interface_owners('00005e0010a5', self.index), (set(), None))
        self.assertEqual(interface_owners('00005e0010a5', self.index, adapter_block=True), ({'host-a'}, 'adapter_block'))
        self.assertEqual(interface_owners('00005e0010a8', self.index, adapter_block=True), (set(), None))

    def test_adapter_block_reports_ambiguity(self):
        """Two owners in one block are both returned so callers refuse them."""
        index = {**self.index, '00005e0010a2': {'host-b'}}
        self.assertEqual(interface_owners('00005e0010a5', index, adapter_block=True)[0], {'host-a', 'host-b'})

    def test_multicast_and_missing(self):
        """Group addresses and missing values never match."""
        self.assertEqual(interface_owners('01005e0010a0', self.index, adapter_block=True), (set(), None))
        self.assertEqual(interface_owners(None, self.index), (set(), None))


class DerivedClientFoldTests(unittest.TestCase):
    """Fold derived host addresses through collection and the clients API."""
    setUp = fixtures.ClientTests.setUp
    sources = fixtures.ClientTests.sources
    record = fixtures.ClientTests.record
    collect = fixtures.ClientTests.collect

    def host(self, remote_id='host-a', macs=(PORT_A, PORT_B)):
        """Add one Proxmox server reporting host interface MACs.

        Args:
            remote_id: The server's remote ID.
            macs: The MACs its interfaces report.

        Returns:
            int: The server observation's ID.
        """
        with self.factory.begin() as db:
            source = db.query(DiscoverySource).filter_by(kind='proxmox').one_or_none()
            if source is None:
                source = DiscoverySource(name='Proxmox example', kind='proxmox', config_name='default')
                db.add(source); db.flush()
            host = DiscoveredDevice(source_id=source.id, remote_id=remote_id, name=f'Example {remote_id}', category='server',
                details_json=json.dumps({'platform': 'proxmox', 'host_interfaces': [{'name': f'nic{i}', 'mac': m} for i, m in enumerate(macs)]}))
            db.add(host); db.flush()
            return host.id

    def api_client(self, client_id):
        """Return one client's API record.

        Args:
            client_id: The client observation's ID.

        Returns:
            dict: Its record from the clients API.
        """
        with patch('labrat.api.clients._config', return_value=CONFIG):
            result = self.client.get('/api/v1/clients').json
        return next(o['client'] for n in result['clients'] for o in n['observations'] if o['id'] == client_id)

    def stored(self, client_id):
        """Return one client's stored curated detail.

        Args:
            client_id: The client observation's ID.

        Returns:
            dict: Its stored ``client`` details.
        """
        with self.factory() as db:
            return json.loads(db.get(DiscoveredDevice, client_id).details_json)['client']

    def test_locally_administered_bond_address_folds_into_host(self):
        """A bond's locally administered copy of a member NIC hides with its host."""
        self.sources(); host_id = self.host()
        client_id = self.collect([self.record(name='02:00:5e:00:10:a0', mac_address='02:00:5e:00:10:a0', ip_address=None)])
        stored = self.stored(client_id)
        self.assertEqual(stored['represented_by'], host_id)
        self.assertEqual(stored['represented_by_match'], 'locally_administered')
        client = self.api_client(client_id)
        self.assertEqual(client['server_interfaces'], ['Example host-a'])
        self.assertEqual(client['server_interface_match'], 'locally_administered')

    def test_adapter_sibling_folds_only_without_own_identity(self):
        """An offload sibling address folds; one with an IP or hostname stays a client."""
        self.sources(); host_id = self.host()
        client_id = self.collect([self.record(name='00:00:5e:00:10:a1', mac_address='00:00:5e:00:10:a1', ip_address=None)])
        self.assertEqual(self.stored(client_id)['represented_by'], host_id)
        self.assertEqual(self.api_client(client_id)['server_interface_match'], 'adapter_block')
        self.collect([self.record(mac_address='00:00:5e:00:10:a1', ip_address=None, hostname='device')])
        self.assertNotIn('represented_by', self.stored(client_id))
        self.assertEqual(self.api_client(client_id)['server_interfaces'], [])
        self.collect([self.record(mac_address='00:00:5e:00:10:a1')])
        self.assertNotIn('represented_by', self.stored(client_id))

    def test_shared_adapter_block_stays_unassociated(self):
        """Two hosts in one adapter block leave the sibling visible and unowned."""
        self.sources(); self.host(); self.host('host-b', ('00005e0010a2',))
        client_id = self.collect([self.record(mac_address='00:00:5e:00:10:a1', ip_address=None)])
        self.assertNotIn('represented_by', self.stored(client_id))
        client = self.api_client(client_id)
        self.assertEqual(client['server_interfaces'], [])
        self.assertIsNone(client['server_interface_match'])

    def test_switch_port_sibling_hides_through_collector_fold(self):
        """A sibling of a switch's own MAC hides even though no server owns it."""
        self.sources()
        with self.factory.begin() as db:
            switch = DiscoveredDevice(source_id=self.parent_id, remote_id='site:switch-b', name='Example switch',
                category='switch', identifiers_json=json.dumps({'mac': '00:00:5e:00:20:b0'}))
            db.add(switch); db.flush(); switch_id = switch.id
        client_id = self.collect([self.record(mac_address='00:00:5e:00:20:b4', ip_address=None)])
        self.assertEqual(self.stored(client_id)['represented_by'], switch_id)
        client = self.api_client(client_id)
        self.assertEqual(client['server_interfaces'], ['Example switch'])
        self.assertEqual(client['server_interface_match'], 'adapter_block')


if __name__ == '__main__':
    unittest.main()
