# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Protect atomic source snapshots against invalid ports, links and interfaces."""

from dataclasses import replace
import json
import unittest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from labrat import models as inventory_models  # Register inventory foreign keys.
from labrat.audit import AuditEvent
from labrat.collector.contracts import DeviceObservation, DiscoveryBatch, LinkObservation, NetworkInterfaceObservation, PortObservation
from labrat.collector.store import apply_batch, record_telemetry
from labrat.database import Base
from labrat.discovery.models import DiscoveredDevice, DiscoveredLink, DiscoveredNetworkInterface, DiscoveredPort, DiscoverySource
from labrat.models import Device


class StoreBoundaryTests(unittest.TestCase):
    """Use database transactions to verify rejection without partial persistence."""

    def setUp(self):
        """Create an empty in-memory source with two endpoint observations."""
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        self.sessions = sessionmaker(engine)
        with self.sessions.begin() as db:
            source = DiscoverySource(name='test', kind='unifi', config_name='test')
            db.add(source)
            db.flush()
            self.source_id = source.id
        self.devices = (DeviceObservation('a', 'A', 'switch'), DeviceObservation('b', 'B', 'server'))
        self.ports = (PortObservation('a', '1', 'Port 1'), PortObservation('b', 'eth0', 'eth0'))
        self.link = LinkObservation('a', '1', 'b', 'eth0', 'lldp', 'confirmed')
        self.nic = NetworkInterfaceObservation('b', 'bond0', 'bond0', 'bond', members=('eth0',), mode='802.3ad')

    def apply(self, batch):
        """Commit one valid source snapshot through the production store.

        Args:
            batch: The snapshot to store.
        """
        with self.sessions.begin() as db:
            apply_batch(db, db.get(DiscoverySource, self.source_id), batch)

    def test_invalid_batches_leave_no_partial_observations(self):
        """Invalid identity, media, members and cable evidence roll back together."""
        batches = [
            DiscoveryBatch(devices=(replace(self.devices[0], category='vm'),)),
            DiscoveryBatch(devices=(replace(self.devices[0], remote_id=''),)),
            DiscoveryBatch(devices=(replace(self.devices[0], name=''),)),
            DiscoveryBatch(devices=(self.devices[0], self.devices[0])),
            DiscoveryBatch(devices=self.devices, ports=(PortObservation('missing', '1', 'Port'),)),
            DiscoveryBatch(devices=self.devices, ports=(self.ports[0], self.ports[0])),
        ]
        for field, value in (('remote_id',''), ('name',''), ('speed_mbps',0), ('max_speed_mbps',-1),
                             ('connector_type','bad'), ('medium','SFP+')):
            batches.append(DiscoveryBatch(devices=self.devices, ports=(replace(self.ports[0], **{field:value}),)))
        for nic in (replace(self.nic, device_remote_id='missing'), replace(self.nic, remote_id=''),
                    replace(self.nic, name=''), replace(self.nic, kind='physical'), replace(self.nic, vlan_id=4095),
                    replace(self.nic, members=('',)), replace(self.nic, members=('eth0','eth0'))):
            batches.append(DiscoveryBatch(devices=self.devices, network_interfaces=(nic,)))
        batches.append(DiscoveryBatch(devices=self.devices, network_interfaces=(self.nic,self.nic)))
        for link in (replace(self.link, b_device_remote_id='missing'), replace(self.link, b_device_remote_id='a', b_port_remote_id='1'),
                     replace(self.link, confidence='certain'), replace(self.link, evidence='')):
            batches.append(DiscoveryBatch(devices=self.devices, ports=self.ports, links=(link,)))
        batches.append(DiscoveryBatch(devices=self.devices, ports=self.ports, links=(self.link, replace(self.link,
            a_device_remote_id='b', a_port_remote_id='eth0', b_device_remote_id='a', b_port_remote_id='1'))))
        for index, batch in enumerate(batches):
            with self.subTest(case=index), self.assertRaises(ValueError):
                self.apply(batch)
            with self.sessions() as db:
                for model in (DiscoveredDevice, DiscoveredPort, DiscoveredLink, DiscoveredNetworkInterface, AuditEvent, Device):
                    self.assertEqual(db.scalar(select(func.count()).select_from(model)), 0)
                self.assertIsNone(db.get(DiscoverySource,self.source_id).last_success_at)

    def test_changed_snapshot_is_audited_and_complete_snapshots_remove_stale_ports(self):
        """Updates preserve device identity and retire only complete port/interface data."""
        self.apply(DiscoveryBatch(devices=self.devices, ports=self.ports, links=(self.link,), network_interfaces=(self.nic,)))
        self.apply(DiscoveryBatch(devices=self.devices, ports=self.ports,
            links=(replace(self.link, confidence='inferred', evidence='new-evidence'),),
            network_interfaces=(replace(self.nic, members=('eth0','eth1'), active=True),)))
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(DiscoveredLink)).confidence, 'inferred')
            self.assertTrue(db.scalar(select(DiscoveredNetworkInterface)).active)
            actions = [e.action for e in db.scalars(select(AuditEvent))]
            self.assertIn('discovery.link.changed', actions)
            self.assertIn('discovery.network_interface.changed', actions)
        # An incomplete/offline scan retains physical and logical interface rows.
        self.apply(DiscoveryBatch(devices=self.devices))
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count(DiscoveredPort.id))), 2)
            self.assertEqual(db.scalar(select(func.count(DiscoveredNetworkInterface.id))), 1)
            self.assertEqual(db.scalar(select(func.count(DiscoveredLink.id))), 0)
        complete = tuple(replace(d, details={'ports_complete': True,'network_interfaces_complete': True}) for d in self.devices)
        self.apply(DiscoveryBatch(devices=complete))
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count(DiscoveredPort.id))), 0)
            self.assertEqual(db.scalar(select(func.count(DiscoveredNetworkInterface.id))), 0)
            self.assertEqual(db.scalar(select(func.count(DiscoveredDevice.id))), 2)

    def test_fast_telemetry_is_whitelisted_unaudited_and_does_not_create_inventory(self):
        """Only known observations accept telemetry; later scans retain that sample."""
        self.apply(DiscoveryBatch(devices=self.devices))
        with self.sessions.begin() as db:
            source = db.get(DiscoverySource,self.source_id)
            before = db.scalar(select(func.count(AuditEvent.id)))
            self.assertTrue(record_telemetry(db, source, 'a', 'ups', {'state':'online'}))
            self.assertFalse(record_telemetry(db, source, 'absent', 'ups', {}))
            with self.assertRaises(ValueError):
                record_telemetry(db, source, 'a', 'credentials', {'secret':'test'})
            self.assertEqual(db.scalar(select(func.count(AuditEvent.id))),before)
        self.apply(DiscoveryBatch(devices=self.devices))
        with self.sessions() as db:
            device = db.scalar(select(DiscoveredDevice).where(DiscoveredDevice.remote_id=='a'))
            self.assertEqual(json.loads(device.details_json)['ups'], {'state':'online'})
            self.assertEqual(db.scalar(select(func.count(Device.id))),0)
