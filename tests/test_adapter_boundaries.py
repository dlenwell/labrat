# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Exercise optional adapter data, defensive schemas and read-only transport."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import requests
from labrat.collector.adapters import proxmox, mndp
from labrat.collector.http import controller_url, required_string, tls_verification, JsonGetClient


class AdapterBoundaryTests(unittest.TestCase):
    """Use fake HTTP and socket peers so no lab endpoint is contacted."""

    def client(self, rows=None, status=None, network=None):
        """Serve minimal Proxmox API data and reject unexpected requests.

        Args:
            rows: The node list; one online node by default.
            status: The cluster status rows.
            network: The node's network interfaces.

        Returns:
            Mock: A client serving those responses.
        """
        responses = {'/nodes': {'data': rows if rows is not None else [{'node':'host','status':'online'}]},
                     '/cluster/status': {'data': status if status is not None else [{'type':'node','name':'host','ip':'192.0.2.1/24'}]},
                     '/nodes/host/network': {'data': network if network is not None else [
                         {'iface':'eth0','type':'eth','hwaddress':'02:00:00:00:00:01','active':1},
                         {'iface':'eth1','type':'eth'}, {'iface':'eth2','type':'eth'}]}}
        client = Mock()
        def get(url, headers, verify):
            path = url.split('/api2/json')[-1]
            result = responses[path]
            if isinstance(result, Exception):
                raise result
            return result
        client.get_json.side_effect = get
        return client, responses

    def settings(self, **changes):
        """Return illustrative private settings with report reading disabled.

        Args:
            **changes: Settings to override.

        Returns:
            dict: The settings.
        """
        return {'url':'https://pve.example','token_id':'test!readonly','token_secret':'test',
                'report_interval_seconds':0, **changes}

    def test_snmp_supplements_only_requested_known_nodes_and_physical_ports(self):
        """SNMP fills missing MACs, respects API identity and uses explicit state."""
        client, _ = self.client()
        with patch.object(proxmox,'read_physical_interfaces',return_value={
            'eth0':{'mac':'02:00:00:00:00:99','link_up':False},
            'eth1':{'mac':'02:00:00:00:00:02','link_up':None},
            'vmbr0':{'mac':'02:00:00:00:00:03','link_up':True}}) as read:
            batch = proxmox.ProxmoxAdapter(client).poll(self.settings(snmp={'nodes':['host','absent']}))
        read.assert_called_once_with('192.0.2.1', {'nodes':['host','absent']})
        ports = {p.name:p for p in batch.ports}
        self.assertEqual(set(ports),{'eth0','eth1','eth2'})
        self.assertEqual(ports['eth0'].mac,'02:00:00:00:00:01')
        self.assertIs(ports['eth0'].link_up,False)
        self.assertEqual(ports['eth1'].mac,'02:00:00:00:00:02')
        self.assertIs(ports['eth1'].link_up,False)
        self.assertIsNone(ports['eth2'].mac)
        with patch.object(proxmox,'read_physical_interfaces',side_effect=TimeoutError('private-token')):
            fallback = proxmox.ProxmoxAdapter(client).poll(self.settings(snmp={'nodes':['host']}))
        self.assertTrue(next(p for p in fallback.ports if p.name=='eth0').link_up)
        for nodes in ('host', [None]):
            with self.subTest(nodes=nodes), self.assertRaises(ValueError):
                proxmox.ProxmoxAdapter(client).poll(self.settings(snmp={'nodes':nodes}))

    def test_bad_or_offline_node_schemas(self):
        """Offline nodes retain identity without network calls; malformed lists fail."""
        client, responses = self.client(rows=[{'node':'offline','status':'offline'}])
        responses['/cluster/status'] = requests.RequestException('unavailable')
        batch = proxmox.ProxmoxAdapter(client).poll(self.settings())
        self.assertFalse(batch.devices[0].details['ports_complete'])
        self.assertFalse(batch.ports)
        self.assertEqual(client.get_json.call_count,2)
        for rows in ([{}], [{'node':1}], [{'node':' '}], None, [None]):
            client, responses = self.client()
            responses['/nodes'] = {'data':rows}
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                proxmox.ProxmoxAdapter(client).poll(self.settings())
        status = [{'type':'cluster'}, {'type':'node','name':None,'ip':'192.0.2.1'},
                  {'type':'node','name':'host','ip':'invalid'}, {'type':'node','name':'host','ip':'192.0.2.1'}]
        client, _ = self.client(status=status)
        batch = proxmox.ProxmoxAdapter(client).poll(self.settings())
        self.assertEqual(batch.devices[0].identifiers['management_ips'],['192.0.2.1'])

    def test_logical_interfaces_keep_config_separate_from_physical_ports(self):
        """OVS aliases, invalid VLAN tags and unknown states are normalized safely."""
        for kind, expected in (('ovs_bond','bond'),('OVSBridge','bridge'),('OVSIntPort','bridge'),('OVSPort','bridge'),('vlan','vlan')):
            for tag in ('bad', 0, 4095, 100):
                row = {'iface':' logical0 ', 'type':kind, 'slaves':['eth0','eth0',None,'none',' eth1 '],
                       'bridge_ports':'eth0 eth1 none', 'vlan-id':tag, 'vlan-raw-device':'eth0', 'active':'unknown',
                       'password':'must-not-persist', 'address':{'invalid':'shape'}}
                result = proxmox._logical_interface(row,'host')
                self.assertEqual((result.kind,result.name),(expected,'logical0'))
                self.assertIsNone(result.active)
                self.assertNotIn('password',result.details)
                self.assertNotIn('address',result.details)
                self.assertEqual(result.vlan_id,100 if expected=='vlan' and tag==100 else None)
                if expected!='vlan':
                    self.assertEqual(result.members,('eth0','eth1'))
        self.assertIsNone(proxmox._logical_interface({'iface':None,'type':'bond'},'host'))
        self.assertFalse(proxmox._is_physical_nic({'type':'eth','iface':'veth0'}))
        self.assertFalse(proxmox._is_physical_nic({'type':'eth','iface':'eth0'},exists_metadata_available=True))

    def test_controller_urls_and_tls_options_reject_credential_forwarding(self):
        """Credentials belong in headers and redirects remain disabled."""
        for url in (None, '', 'http://example.test', 'https://user:pass@example.test',
                    'https://example.test/path', 'https://example.test?token=test', 'https://example.test#fragment'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                controller_url({'url':url})
        for value in (None, '', 1):
            with self.assertRaises(ValueError):
                required_string({'token':value},'token')
        self.assertEqual(required_string({'token':' test '},'token'),'test')
        for options in ({'verify_tls':'false'}, {'ca_bundle':''}, {'verify_tls':False,'ca_bundle':'test.pem'}):
            with self.assertRaises(ValueError):
                tls_verification(options)
        self.assertEqual(tls_verification({'ca_bundle':' test.pem '}),'test.pem')
        session = Mock()
        session.get.return_value.json.side_effect = ValueError('invalid JSON')
        with self.assertRaises(ValueError):
            JsonGetClient(session).get_json('https://example.test',{'Authorization':'test'},True)
        session.get.assert_called_once_with('https://example.test',headers={'Authorization':'test'},timeout=(3.0,15.0),allow_redirects=False,verify=True)

    def test_passive_mndp_listener_times_out_and_always_closes(self):
        """Passive discovery binds and receives without sending any packets."""
        listener = Mock()
        listener.recvfrom.side_effect = [TimeoutError(), (b'packet', ('192.0.2.1',5678))]
        with patch.object(mndp.socket,'socket',return_value=listener), patch.object(mndp.time,'monotonic',side_effect=[0,1,2,6]):
            self.assertEqual(mndp._listen(5),[b'packet'])
        listener.close.assert_called_once()
        listener.send.assert_not_called()
        listener.sendto.assert_not_called()
        listener = Mock()
        listener.bind.side_effect = OSError('busy')
        with patch.object(mndp.socket,'socket',return_value=listener), self.assertRaises(OSError):
            mndp._listen(5)
        listener.close.assert_called_once()
