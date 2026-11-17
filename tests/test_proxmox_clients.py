# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Check optional VM MAC identity and host-interface presentation boundaries."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from sqlalchemy import select
import test_clients as fixtures
CONFIG, MAC = fixtures.CONFIG, fixtures.MAC
from labrat.collector.proxmox_clients import guest_snapshot, collect_guest_identities
from labrat.discovery.models import DiscoverySource, DiscoveredDevice

SETTINGS={'url':'https://pve.example.test','token_id':'test','token_secret':'test-only'}

class ProxmoxClientTests(unittest.TestCase):
    """Use disposable migrated storage and mock all Proxmox requests."""
    setUp=fixtures.ClientTests.setUp
    sources=fixtures.ClientTests.sources
    record=fixtures.ClientTests.record
    collect=fixtures.ClientTests.collect
    post=fixtures.ClientTests.post
    create_locations=fixtures.ClientTests.create_locations

    def test_guest_snapshot_whitelists_mac_and_host(self):
        """Read only configured net fields; never retain full guest configuration."""
        adapter=SimpleNamespace(client=Mock())
        adapter.client.get_json.side_effect=[{'data':[{'type':'qemu','vmid':101,'node':'host-a','name':'Example VM','status':'running'}, {'type':'lxc','vmid':102,'node':'host-b','name':'Example container'}]},
            {'data':{'net0':'virtio='+MAC+',bridge=vmbr0','secret':'not-retained'}},
            {'data':{'net0':'name=eth0,hwaddr=02:00:00:00:00:55,bridge=vmbr0'}}]
        rows=guest_snapshot(adapter,SETTINGS)
        self.assertEqual(rows[0]['macs'],[MAC]);self.assertEqual(rows[0]['host'],'host-a')
        self.assertEqual(rows[0]['bridges'],{MAC:'vmbr0'});self.assertEqual(rows[1]['bridges'],{'02:00:00:00:00:55':'vmbr0'})
        self.assertEqual(rows[1]['kind'],'container');self.assertNotIn('not-retained',json.dumps(rows))
        adapter.client.get_json.side_effect=None
        adapter.client.get_json.return_value={'data':[{}]*129}
        with self.assertRaises(ValueError):guest_snapshot(adapter,SETTINGS)

    def test_exact_guest_match_beats_host_interface_default_hide(self):
        """A matched guest remains visible even when its MAC appears on a host."""
        self.sources();client_id=self.collect()
        with self.factory.begin() as db:
            source=DiscoverySource(name='Proxmox example',kind='proxmox',config_name='default');db.add(source);db.flush()
            host=DiscoveredDevice(source_id=source.id,remote_id='host-a',name='Example host',category='server',details_json=json.dumps({
                'platform':'proxmox','host_interfaces':[{'name':'vmbr0','mac':MAC}],
                'proxmox_clients':{'available':True,'sampled_at':datetime.now(timezone.utc).isoformat(),
                    'guests':[{'kind':'vm','vmid':101,'name':'Example VM','host':'host-a','status':'running','macs':[MAC],'bridges':{MAC:'vmbr0'}}]}}))
            db.add(host);db.flush();host_id=host.id
        with patch('labrat.api.clients._config',return_value=CONFIG):
            result=self.client.get('/api/v1/clients').json
        client=next(o['client'] for n in result['clients'] for o in n['observations'] if o['id']==client_id)
        self.assertEqual(client['virtualization']['host'],'host-a');self.assertEqual(client['virtualization']['bridge'],'vmbr0');self.assertEqual(client['server_interfaces'],[])
        with self.factory.begin() as db:
            host=db.get(DiscoveredDevice,host_id);data=json.loads(host.details_json);data['proxmox_clients']['guests']=[];host.details_json=json.dumps(data)
        with patch('labrat.api.clients._config',return_value=CONFIG):result=self.client.get('/api/v1/clients').json
        client=next(o['client'] for n in result['clients'] for o in n['observations'] if o['id']==client_id)
        self.assertEqual(client['server_interfaces'],['Example host']);self.assertIsNone(client['virtualization'])

    def test_collection_failure_retains_original_sample(self):
        """An optional permission failure marks previous VM metadata unavailable."""
        self.sources()
        with self.factory.begin() as db:
            source=db.get(DiscoverySource,self.parent_id)
            host=DiscoveredDevice(source_id=source.id,remote_id='host-a',name='Example host',category='server',details_json=json.dumps({'platform':'proxmox'}));db.add(host);db.flush();host_id=host.id
        with self.factory() as db:source=db.get(DiscoverySource,self.parent_id)
        adapter=SimpleNamespace(client=Mock())
        with patch('labrat.collector.proxmox_clients.guest_snapshot',return_value=[{'host':'host-a','vmid':101,'macs':[MAC]}]):collect_guest_identities(self.factory,source,adapter,SETTINGS)
        with self.factory() as db:before=json.loads(db.get(DiscoveredDevice,host_id).details_json)['proxmox_clients']
        adapter._client_guest_attempts={}
        with patch('labrat.collector.proxmox_clients.guest_snapshot',side_effect=ValueError('unavailable')):collect_guest_identities(self.factory,source,adapter,SETTINGS)
        with self.factory() as db:after=json.loads(db.get(DiscoveredDevice,host_id).details_json)['proxmox_clients']
        self.assertFalse(after['available']);self.assertEqual(after['sampled_at'],before['sampled_at']);self.assertEqual(after['guests'],before['guests'])

    def test_guest_snapshot_rejects_partial_and_invalid_identity(self):
        """Malformed inventories must not replace a valid earlier snapshot."""
        adapter=SimpleNamespace(client=Mock())
        for rows in (None,[{'type':'qemu','node':'host-a','vmid':False}],
                     [{'type':'qemu','node':'host-a','vmid':101}]*2):
            adapter.client.get_json.side_effect=None
            adapter.client.get_json.return_value={'data':rows}
            with self.subTest(rows=rows),self.assertRaises(ValueError):guest_snapshot(adapter,SETTINGS)
        adapter.client.get_json.side_effect=[{'data':[{'type':'qemu','node':'host-a','vmid':101}]},{'data':None}]
        with self.assertRaises(ValueError):guest_snapshot(adapter,SETTINGS)

    def test_guest_refresh_cooldown(self):
        """Normal collector cycles reuse optional guest evidence within its cadence."""
        self.sources()
        with self.factory() as db:source=db.get(DiscoverySource,self.parent_id)
        adapter=SimpleNamespace(client=Mock())
        with patch('labrat.collector.proxmox_clients.guest_snapshot',return_value=[]) as query:
            collect_guest_identities(self.factory,source,adapter,SETTINGS)
            collect_guest_identities(self.factory,source,adapter,SETTINGS)
            self.assertEqual(query.call_count,1)
    def test_virtual_client_has_no_inferred_physical_cable(self):
        """Guest switch learning cannot manufacture a physical guest NIC or cable."""
        self.sources()
        fixtures.ClientTests.switch(self)
        with self.factory.begin() as db:
            source=DiscoverySource(name='Proxmox example',kind='proxmox',config_name='default')
            db.add(source);db.flush()
            db.add(DiscoveredDevice(source_id=source.id,remote_id='host-a',name='Example host',category='server',
                details_json=json.dumps({'platform':'proxmox','proxmox_clients':{'guests':[{'vmid':101,'macs':[MAC]}]}})))
        client_id=self.collect()
        from labrat.discovery.models import DiscoveredPort, DiscoveredLink
        with self.factory() as db:
            details=json.loads(db.get(DiscoveredDevice,client_id).details_json)['client']
            self.assertTrue(details['virtual_endpoint'])
            self.assertIsNone(db.scalar(select(DiscoveredPort).where(DiscoveredPort.discovered_device_id==client_id)))
            self.assertIsNone(db.scalar(select(DiscoveredLink).where(DiscoveredLink.source_id==self.source_id)))

    def test_guest_is_never_added_to_inventory_or_placed(self):
        """VMs and containers are not physical equipment: both writes refuse."""
        self.sources()
        with self.factory.begin() as db:
            source=DiscoverySource(name='Proxmox example',kind='proxmox',config_name='default')
            db.add(source);db.flush()
            db.add(DiscoveredDevice(source_id=source.id,remote_id='host-a',name='Example host',category='server',
                details_json=json.dumps({'platform':'proxmox','proxmox_clients':{'guests':[{'vmid':101,'macs':[MAC]}]}})))
        client_id=self.collect()
        response=self.post(f'/api/v1/clients/{client_id}/inventory',{})
        self.assertEqual(response.status_code,409,response.json)
        _,room,_=self.create_locations()
        floor=next(c for c in self.client.get('/api/v1/locations').json['containers'] if c['room_id']==room and c['kind']=='floor')
        self.assertEqual(self.post('/api/v1/placements',{'discovery_id':client_id,'container_id':floor['id']}).status_code,409)
        with self.factory() as db:
            self.assertIsNone(db.get(DiscoveredDevice,client_id).device_id)

    def test_bond_mac_folds_into_host_without_ip_or_identity_write(self):
        """A unique host interface MAC displays under its server without a cable."""
        self.sources()
        with self.factory.begin() as db:
            source=DiscoverySource(name='Proxmox example',kind='proxmox',config_name='default')
            db.add(source);db.flush()
            host=DiscoveredDevice(source_id=source.id,remote_id='host-a',name='Example host',category='server',
                details_json=json.dumps({'platform':'proxmox','host_interfaces':[{'name':'bond0','mac':MAC}]}))
            db.add(host);db.flush();host_id=host.id
        client_id=self.collect([self.record(name=MAC,ip_address=None)])
        with self.factory() as db:
            observed=db.get(DiscoveredDevice,client_id)
            self.assertEqual(json.loads(observed.details_json)['client']['represented_by'],host_id)
            self.assertIsNone(observed.device_id)
            from labrat.discovery.models import DiscoveredPort
            self.assertIsNone(db.scalar(select(DiscoveredPort).where(DiscoveredPort.discovered_device_id==client_id)))
        with patch('labrat.api.clients._config',return_value=CONFIG):
            result=self.client.get('/api/v1/clients').json
        node=next(n for n in result['clients'] if any(o['id']==client_id for o in n['observations']))
        self.assertEqual(node['name'],'Example host')
        with self.factory.begin() as db:
            db.add(DiscoveredDevice(source_id=source.id,remote_id='host-b',name='Other host',category='server',
                details_json=json.dumps({'host_interfaces':[{'name':'bond0','mac':MAC}]})))
        self.collect([self.record(name=MAC,ip_address=None)])
        with self.factory() as db:
            self.assertNotIn('represented_by',json.loads(db.get(DiscoveredDevice,client_id).details_json)['client'])
