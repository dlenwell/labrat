# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Exercise placement validation, shelf ordering and atomic inventory writes."""

import json
import unittest
from sqlalchemy import func, select
import test_step4c as fixture
from labrat.audit import AuditEvent
from labrat.discovery.models import DiscoveredDevice
from labrat.models import Device
from labrat.placement import DevicePlacement, PlacementContainer


class PlacementBoundaryTests(unittest.TestCase):
    """Use real authenticated routes and disposable migrated storage."""

    setUp = fixture.Step4CTests.setUp
    post = fixture.Step4CTests.post
    create_locations = fixture.Step4CTests.create_locations
    add_observations = fixture.Step4CTests.add_observations

    def manual(self, **changes):
        """Create manual equipment and assert that the whole write succeeds.

        Args:
            **changes: Fields added to or overriding the request.

        Returns:
            dict: The created device's JSON.
        """
        response = self.post('/api/v1/rack-devices', {'name': 'Test equipment', 'height_u': 1, **changes})
        self.assertEqual(response.status_code, 201, response.json)
        return response.json

    def test_shared_shelf_conversion_reorder_and_removal(self):
        """Convert a one-U occupant, share its shelf, reorder it, then close gaps."""
        _, room, rack = self.create_locations()
        first = self.manual(name='First', container_id=rack, rack_u=5, face='front')
        second = self.manual(name='Second', container_id=rack, rack_u=5, face='front', placement_mode='rack_shelf')
        shelf = second['placement']['container_id']
        self.assertNotEqual(shelf, rack)
        with self.factory() as db:
            a = db.get(DevicePlacement, first['device_id'])
            self.assertEqual(a.container_id, shelf)
            self.assertLess(json.loads(a.position_json)['x'], second['placement']['position']['x'])
        third = self.manual(name='Third', container_id=shelf, position={'x': 0})
        move = self.post(f"/api/v1/rack-devices/{second['device_id']}/placement", {'container_id': shelf, 'position': {'x': 0}})
        self.assertEqual(move.status_code, 200, move.json)
        with self.factory() as db:
            ordered = sorted((json.loads(p.position_json)['x'], p.device_id) for p in db.scalars(
                select(DevicePlacement).where(DevicePlacement.container_id == shelf)))
            self.assertEqual([identity for _, identity in ordered], [second['device_id'], third['device_id'], first['device_id']])
            event = db.scalar(select(AuditEvent).where(AuditEvent.action == 'inventory.device.created', AuditEvent.subject_id == second['device_id']))
            self.assertTrue(json.loads(event.details_json)['shelf_layout_changes'])
        for identity in (second['device_id'], third['device_id'], first['device_id']):
            removed = self.post(f'/api/v1/rack-devices/{identity}/unplace', {})
            self.assertEqual(removed.status_code, 200)
        with self.factory() as db:
            self.assertIsNone(db.get(PlacementContainer, shelf))
            self.assertIsNotNone(db.get(Device, first['device_id']))
        self.assertEqual(self.post(f"/api/v1/rack-devices/{first['device_id']}/unplace", {}).status_code, 200)

    def test_shelf_moves_and_rack_bounds_are_atomic(self):
        """Reject overlap and invalid faces while preserving existing placement."""
        _, _, rack = self.create_locations()
        large = self.manual(container_id=rack, rack_u=4, height_u=3)
        for changes in ({'rack_u': 5}, {'rack_u': 4, 'face': 'full_depth'}, {'rack_u': 37}, {'rack_u': 2, 'face': 'side'}):
            with self.subTest(changes=changes):
                response = self.post('/api/v1/rack-devices', {'name': 'Rejected shelf', 'height_u': 1,
                    'container_id': rack, 'placement_mode': 'rack_shelf', **changes})
                self.assertIn(response.status_code, (400, 409), response.json)
        front = self.manual(container_id=rack, rack_u=8, placement_mode='rack_shelf')
        rear = self.manual(container_id=rack, rack_u=8, face='rear', placement_mode='rack_shelf')
        rejected = self.post('/api/v1/rack-devices', {'name': 'Overlap', 'height_u': 1,
            'container_id': rack, 'rack_u': 8, 'face': 'full_depth', 'placement_mode': 'rack_shelf'})
        self.assertEqual(rejected.status_code, 409)
        for position in (None, {}, {'x': True}, {'x': -1}, {'x': 2}, {'x': '0.5'}, {'x': .5, 'y': .5}):
            response = self.post(f"/api/v1/rack-devices/{large['device_id']}/placement", {
                'container_id': front['placement']['container_id'], 'position': position})
            self.assertEqual(response.status_code, 400, response.json)
        with self.factory() as db:
            self.assertEqual(db.get(DevicePlacement, large['device_id']).rack_u, 4)
        moved = self.post(f"/api/v1/rack-devices/{front['device_id']}/placement", {'container_id': rack, 'rack_u': 10})
        self.assertEqual(moved.status_code, 200, moved.json)
        with self.factory() as db:
            self.assertIsNone(db.get(PlacementContainer, front['placement']['container_id']))
            self.assertIsNotNone(db.get(PlacementContainer, rear['placement']['container_id']))

    def test_surface_layout_and_shelf_packing(self):
        """Wall shelves use along; their devices use ordered horizontal positions."""
        _, room, rack = self.create_locations()
        containers = self.client.get('/api/v1/locations').json['containers']
        wall = next(c for c in containers if c['kind'] == 'wall' and c['wall_side'] == 'north')
        shelf = self.post('/api/v1/locations', {'kind': 'shelf', 'name': 'Wall shelf', 'room_id': room,
            'parent_id': wall['id'], 'position': {'along': .25}})
        self.assertEqual(shelf.status_code, 201, shelf.json)
        shelf_id = shelf.json['id']
        positioned = self.post(f'/api/v1/containers/{shelf_id}/layout', {'position': {'along': .75}})
        self.assertEqual(positioned.json['position'], {'along': .75})
        # A shelf can carry its relative size; a later move keeps it.
        sized = self.post(f'/api/v1/containers/{shelf_id}/layout', {'position': {'along': .6, 'width': .3, 'depth': .15}})
        self.assertEqual(sized.json['position'], {'along': .6, 'width': .3, 'depth': .15})
        moved = self.post(f'/api/v1/containers/{shelf_id}/layout', {'position': {'along': .75}})
        self.assertEqual(moved.json['position'], {'along': .75, 'width': .3, 'depth': .15})
        # A rack can stand flush against a wall; a move without it stands free.
        rack_container = next(c for c in self.client.get('/api/v1/locations').json['containers']
                              if c['kind'] == 'rack' and c['room_id'] == room)
        glued = self.post(f"/api/v1/containers/{rack_container['id']}/layout",
            {'position': {'x': .5, 'y': .1, 'wall': 'north'}})
        self.assertEqual(glued.json['position'], {'x': .5, 'y': .1, 'wall': 'north'})
        freed = self.post(f"/api/v1/containers/{rack_container['id']}/layout", {'position': {'x': .5, 'y': .5}})
        self.assertEqual(freed.json['position'], {'x': .5, 'y': .5})
        self.assertEqual(self.post(f"/api/v1/containers/{rack_container['id']}/layout",
            {'position': {'x': .5, 'y': .5, 'wall': 'up'}}).status_code, 400)
        for size in ({'width': 0}, {'depth': 1.5}, {'width': True}):
            self.assertEqual(self.post(f'/api/v1/containers/{shelf_id}/layout',
                {'position': {'along': .5, **size}}).status_code, 400)
        first = self.manual(container_id=shelf_id, position={'x': 0})
        second = self.manual(container_id=shelf_id, position={'x': 1}, class_name='Test appliance')
        self.manual(class_name='Test appliance')
        self.assertEqual(self.post(f"/api/v1/rack-devices/{first['device_id']}/placement", {
            'container_id': wall['id'], 'position': {'along': .5}}).status_code, 200)
        with self.factory() as db:
            self.assertEqual(json.loads(db.get(DevicePlacement, second['device_id']).position_json), {'x': .5})
        for position in (None, {}, [], {'x': .5}, {'along': True}, {'along': 2}):
            self.assertEqual(self.post(f'/api/v1/containers/{shelf_id}/layout', {'position': position}).status_code, 400)
        self.assertEqual(self.post(f"/api/v1/containers/{wall['id']}/layout", {'position': {'along': .5}}).status_code, 400)
        self.assertEqual(self.post('/api/v1/containers/99999/layout', {'position': {'x': .5, 'y': .5}}).status_code, 404)

    def test_location_validation(self):
        """Reject invalid nesting and dimensions before any partial location write."""
        _, room, rack = self.create_locations()
        for extra, status in [
            ({'kind': 'unknown'}, 400), ({'kind': 'room', 'site_id': 9999}, 404),
            ({'kind': 'rack', 'room_id': 9999, 'height_u': 8}, 404),
            ({'kind': 'rack', 'height_u': 129}, 400), ({'kind': 'rack', 'width_inches': 12, 'height_u': 8}, 400),
            ({'kind': 'rack', 'has_rear': 'false', 'height_u': 8}, 400),
            ({'kind': 'surface', 'room_id': 9999}, 404),
            ({'kind': 'shelf', 'parent_id': 9999}, 400), ({'kind': 'shelf', 'parent_id': rack}, 400),
            ({'kind': 'wall', 'wall_side': 'up'}, 400),
            ({'kind': 'wall', 'wall_side': 'north', 'parent_id': rack}, 400),
            ({'kind': 'surface', 'wall_side': 'north'}, 400),
        ]:
            with self.subTest(extra=extra):
                response = self.post('/api/v1/locations', {'kind': 'surface', 'name': 'Rejected', 'room_id': room, **extra})
                self.assertEqual(response.status_code, status, response.json)
        for name in (None, '', ' ', 1, 'x' * 121):
            self.assertEqual(self.post('/api/v1/locations', {'kind': 'site', 'name': name}).status_code, 400)
        self.assertEqual(self.post('/api/v1/locations', {'kind': 'site', 'name': 'Home'}).status_code, 409)

    def test_manual_input_and_missing_identity(self):
        """Invalid requests roll back newly inserted devices and device classes."""
        for extra, status in [({'height_u': 129}, 400), ({'height_u': True}, 400),
            ({'class_name': 1}, 400), ({'class_name': 'x' * 81}, 400),
            ({'port_facing': 'side'}, 400), ({'container_id': 99999, 'class_name': 'Rollback class'}, 404)]:
            with self.subTest(extra=extra):
                self.assertEqual(self.post('/api/v1/rack-devices', {'name': 'Rejected', 'height_u': 1, **extra}).status_code, status)
        with self.factory() as db:
            self.assertEqual(db.scalar(select(func.count(Device.id))), 0)
        for endpoint in ('name', 'unplace', 'placement'):
            payload = {'name': 'Missing', 'container_id': 99999}
            self.assertEqual(self.post(f'/api/v1/rack-devices/99999/{endpoint}', payload).status_code, 404)
        self.assertEqual(self.post('/api/v1/placements', {'discovery_id': 99999, 'container_id': 99999}).status_code, 404)
        headers = {'X-CSRF-Token': self.csrf}
        for body in ('null', '[]', '{bad'):
            self.assertEqual(self.client.post('/api/v1/rack-devices', data=body, content_type='application/json', headers=headers).status_code, 400)
        self.assertEqual(self.client.post('/api/v1/rack-devices', data='a=1', headers=headers).status_code, 400)

    def test_discovery_isp_label_preserves_inventory_name(self):
        """Expose the modem's observed ISP name separately from canonical identity."""
        observed, _ = self.add_observations()
        _, _, rack = self.create_locations()
        with self.factory.begin() as db:
            modem = db.get(DiscoveredDevice, observed)
            modem.category = 'modem'
            modem.name = 'Primary ISP WAN1 upstream'
            modem.details_json = json.dumps({'platform': 'unifi-wan-inference'})
        placed = self.post('/api/v1/placements', {'discovery_id': observed, 'container_id': rack, 'rack_u': 1})
        self.assertEqual(placed.status_code, 200)
        identity = placed.json['device_id']
        self.assertEqual(self.post(f'/api/v1/rack-devices/{identity}/name', {'name': 'My modem'}).status_code, 200)
        node = next(n for n in self.client.get('/api/v1/topology').json['nodes'] if n['device_id'] == identity)
        self.assertEqual(node['name'], 'My modem')
        self.assertTrue(node['observations'][0]['wan_upstream'])
        self.assertEqual(node['observations'][0]['reported_name'], 'Primary ISP WAN1 upstream')
