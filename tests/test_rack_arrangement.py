# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Exercise the Equipment arrange mode's batch rack move endpoint."""

import json
import unittest
from sqlalchemy import func, select
import test_step4c as fixture
from labrat.audit import AuditEvent
from labrat.models import User
from labrat.placement import DevicePlacement

ENDPOINT = '/api/v1/rack-arrangement'


class RackArrangementTests(unittest.TestCase):
    """Use real authenticated routes and disposable migrated storage."""

    setUp = fixture.Step4CTests.setUp
    post = fixture.Step4CTests.post
    create_locations = fixture.Step4CTests.create_locations

    def manual(self, **changes):
        """Create manual rack equipment and return its device ID.

        Args:
            **changes: Fields added to or overriding the request.

        Returns:
            int: The new device's ID.
        """
        response = self.post('/api/v1/rack-devices', {'name': 'Test equipment', 'height_u': 1, **changes})
        self.assertEqual(response.status_code, 201, response.json)
        return response.json['device_id']

    def rack(self, room, name, **changes):
        """Create another rack in a room and return its placement container ID.

        Args:
            room: The room's ID.
            name: The rack's name.
            **changes: Fields added to or overriding the request.

        Returns:
            int: The rack's placement container ID.
        """
        response = self.post('/api/v1/locations', {'kind': 'rack', 'room_id': room, 'name': name,
            'height_u': 12, **changes})
        self.assertEqual(response.status_code, 201, response.json)
        containers = self.client.get('/api/v1/locations').json['containers']
        return next(c['id'] for c in containers if c['rack_id'] == response.json['id'])

    def positions(self, *device_ids):
        """Return each device's (container ID, bottom U, face).

        Args:
            *device_ids: The devices to look up.

        Returns:
            list[tuple]: One (container ID, bottom U, face) per device.
        """
        with self.factory() as db:
            return [(p.container_id, p.rack_u, p.face) for p in (
                db.get(DevicePlacement, identity) for identity in device_ids)]

    def move(self, device_id, from_container, from_u, to_container, to_u):
        """Build one arrangement move.

        Args:
            device_id: The device.
            from_container: Its current container.
            from_u: Its current bottom unit.
            to_container: The container to move it to.
            to_u: Its new bottom unit.

        Returns:
            dict: The move, as the arrangement API takes it.
        """
        return {'device_id': device_id, 'from': {'container_id': from_container, 'rack_u': from_u},
            'to': {'container_id': to_container, 'rack_u': to_u}}

    def placed_events(self):
        """Count audited placement events.

        Returns:
            int: The number of placement audit events.
        """
        with self.factory() as db:
            return db.scalar(select(func.count(AuditEvent.id)).where(
                AuditEvent.action == 'inventory.device.placed'))

    def test_swap_and_cross_rack_move_save_together(self):
        """Trade two devices' units and move a third rack in one audited write."""
        _, room, main = self.create_locations()
        other = self.rack(room, 'Second rack')
        top = self.manual(name='Top', container_id=main, rack_u=10, height_u=2)
        bottom = self.manual(name='Bottom', container_id=main, rack_u=3, height_u=2, face='rear')
        third = self.manual(name='Third', container_id=main, rack_u=20)
        response = self.post(ENDPOINT, {'moves': [
            self.move(top, main, 10, main, 3),
            self.move(bottom, main, 3, main, 10),
            self.move(third, main, 20, other, 1),
        ]})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(len(response.json['placements']), 3)
        self.assertEqual(self.positions(top, bottom, third),
            [(main, 3, 'front'), (main, 10, 'rear'), (other, 1, 'front')])
        with self.factory() as db:
            events = list(db.scalars(select(AuditEvent).where(
                AuditEvent.action == 'inventory.device.placed')))
        self.assertEqual(len(events), 3)
        details = json.loads(next(e for e in events if e.subject_id == top).details_json)
        self.assertEqual((details['before']['rack_u'], details['after']['rack_u']), (10, 3))
        self.assertEqual(details['arrangement_moves'], 3)

    def test_invalid_final_layout_saves_nothing(self):
        """Reject collisions, bounds, faces, and stale drafts atomically."""
        _, room, main = self.create_locations()
        front_only = self.rack(room, 'Front only', has_rear=False)
        first = self.manual(name='First', container_id=main, rack_u=5)
        second = self.manual(name='Second', container_id=main, rack_u=9, height_u=2)
        rear = self.manual(name='Rear', container_id=main, rack_u=20, face='rear')
        before = self.positions(first, second, rear)
        cases = [
            # A valid first move does not survive a second move into occupied units.
            ([self.move(first, main, 5, main, 1), self.move(second, main, 9, main, 1)], 409),
            ([self.move(second, main, 9, main, 36)], 400),
            ([self.move(rear, main, 20, front_only, 1)], 400),
            ([self.move(first, main, 4, main, 1)], 409),
            ([self.move(first, front_only, 5, main, 1)], 409),
        ]
        for moves, status in cases:
            with self.subTest(moves=moves):
                response = self.post(ENDPOINT, {'moves': moves})
                self.assertEqual(response.status_code, status, response.json)
        self.assertEqual(self.positions(first, second, rear), before)
        self.assertEqual(self.placed_events(), 0)

    def test_scope_is_rack_mounted_devices_in_one_room(self):
        """Keep shelves, other containers, and other rooms out of arrangements."""
        site, room, main = self.create_locations()
        elsewhere = self.post('/api/v1/locations', {'kind': 'room', 'site_id': site, 'name': 'Office'})
        self.assertEqual(elsewhere.status_code, 201, elsewhere.json)
        far = self.rack(elsewhere.json['id'], 'Office rack')
        mounted = self.manual(name='Mounted', container_id=main, rack_u=2)
        shared = self.post('/api/v1/rack-devices', {'name': 'Shared', 'height_u': 1,
            'container_id': main, 'rack_u': 8, 'placement_mode': 'rack_shelf'}).json
        floor = next(c['id'] for c in self.client.get('/api/v1/locations').json['containers']
            if c['kind'] == 'floor' and c['room_id'] == room)
        cases = [
            ([self.move(mounted, main, 2, far, 1)], 400),
            ([self.move(mounted, main, 2, floor, 1)], 400),
            ([self.move(shared['device_id'], shared['placement']['container_id'], 8, main, 1)], 409),
            ([self.move(mounted, main, 2, 99999, 1)], 404),
            ([self.move(99999, main, 2, main, 1)], 404),
        ]
        for moves, status in cases:
            with self.subTest(moves=moves):
                self.assertEqual(self.post(ENDPOINT, {'moves': moves}).status_code, status)
        self.assertEqual(self.positions(mounted), [(main, 2, 'front')])

    def test_payload_validation_and_permissions(self):
        """Reject malformed requests, non-administrators, and forged writes."""
        _, _, main = self.create_locations()
        mounted = self.manual(container_id=main, rack_u=2)
        good = self.move(mounted, main, 2, main, 4)
        for payload in ({}, {'moves': []}, {'moves': {}}, {'moves': [1]},
                {'moves': [good, good]}, {'moves': [good] * 257},
                {'moves': [{**good, 'to': None}]}, {'moves': [{**good, 'from': {'container_id': main}}]},
                {'moves': [{**good, 'device_id': True}]}):
            with self.subTest(payload=str(payload)[:60]):
                self.assertEqual(self.post(ENDPOINT, payload).status_code, 400)
        forged = self.client.post(ENDPOINT, json={'moves': [good]})
        self.assertEqual(forged.status_code, 403)
        with self.factory.begin() as db:
            db.scalar(select(User)).is_admin = False
        self.assertEqual(self.post(ENDPOINT, {'moves': [good]}).status_code, 403)
        self.assertEqual(self.positions(mounted), [(main, 2, 'front')])
        self.client.post('/signout', data={'_csrf_token': self.csrf})
        self.assertEqual(self.post(ENDPOINT, {'moves': [good]}).status_code, 401)


    def top_container(self, rack):
        """Return the rack-top container of a rack container.

        Args:
            rack: The rack's placement container ID.

        Returns:
            int: Its rack-top container ID.
        """
        return next(c['id'] for c in self.client.get('/api/v1/locations').json['containers']
            if c['kind'] == 'rack_top' and c['parent_id'] == rack)

    def top_x(self, *device_ids):
        """Return each rack-top device's stored position.

        Args:
            *device_ids: The rack-top devices.

        Returns:
            list[dict]: Each one's stored position.
        """
        with self.factory() as db:
            return [json.loads(db.get(DevicePlacement, identity).position_json) for identity in device_ids]

    def test_rack_top_order_saves_with_rack_moves(self):
        """Store evenly spaced x in the chosen order, keeping y, in one write."""
        _, room, main = self.create_locations()
        top = self.top_container(main)
        first = self.manual(name='First', container_id=top, position={'x': 0.5, 'y': 0.2})
        second = self.manual(name='Second', container_id=top)
        third = self.manual(name='Third', container_id=top)
        mounted = self.manual(name='Mounted', container_id=main, rack_u=2)
        response = self.post(ENDPOINT, {
            'moves': [self.move(mounted, main, 2, main, 6)],
            'top_orders': [{'container_id': top, 'device_ids': [third, first, second]}],
        })
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.top_x(third, first, second), [
            {'x': 0.25, 'y': 0.5}, {'x': 0.5, 'y': 0.2}, {'x': 0.75, 'y': 0.5}])
        self.assertEqual(self.positions(mounted), [(main, 6, 'front')])
        with self.factory() as db:
            subjects = sorted(e.subject_id for e in db.scalars(select(AuditEvent).where(
                AuditEvent.action == 'inventory.device.placed')))
        # First kept its x (0.5), so only the other three changed.
        self.assertEqual(subjects, sorted([third, second, mounted]))
        self.assertEqual(len(response.json['placements']), 3)

    def test_rack_top_order_rejects_stale_or_foreign_tops(self):
        """Refuse changed contents, other containers, and malformed orders."""
        site, room, main = self.create_locations()
        top = self.top_container(main)
        office = self.post('/api/v1/locations', {'kind': 'room', 'site_id': site, 'name': 'Office'}).json['id']
        far_top = self.top_container(self.rack(office, 'Office rack'))
        first = self.manual(name='First', container_id=top)
        second = self.manual(name='Second', container_id=top)
        far = self.manual(name='Far', container_id=far_top)
        mounted = self.manual(name='Mounted', container_id=main, rack_u=2)
        before = self.top_x(first, second)
        cases = [
            ({'top_orders': [{'container_id': top, 'device_ids': [second]}]}, 409),
            ({'top_orders': [{'container_id': top, 'device_ids': [second, first, mounted]}]}, 409),
            ({'top_orders': [{'container_id': main, 'device_ids': [mounted]}]}, 400),
            ({'top_orders': [{'container_id': 99999, 'device_ids': [first]}]}, 404),
            ({'moves': [self.move(mounted, main, 2, main, 5)],
              'top_orders': [{'container_id': far_top, 'device_ids': [far]}]}, 400),
            ({'top_orders': [{'container_id': top, 'device_ids': []}]}, 400),
            ({'top_orders': [{'container_id': top, 'device_ids': [first, first]}]}, 400),
            ({'top_orders': [{'container_id': top, 'device_ids': [first, second]}] * 2}, 400),
            ({'top_orders': [{'container_id': top}]}, 400),
            ({'top_orders': [7]}, 400),
            ({'top_orders': {}}, 400),
        ]
        for payload, status in cases:
            with self.subTest(payload=str(payload)[:70]):
                self.assertEqual(self.post(ENDPOINT, payload).status_code, status)
        self.assertEqual(self.top_x(first, second), before)
        self.assertEqual(self.positions(mounted), [(main, 2, 'front')])
        self.assertEqual(self.placed_events(), 0)


if __name__ == '__main__':
    unittest.main()
