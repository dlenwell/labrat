# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Rename and delete locations, rooms, racks, and places, optionally unplacing what is inside."""

import json
import unittest

from sqlalchemy import func, select

import test_step4c as fixture
from labrat.audit import AuditEvent
from labrat.metadata import Attribute, InventoryObject
from labrat.models import Device, Network, Rack, Room, Site, User
from labrat.placement import PlacementContainer


class LocationEditTests(unittest.TestCase):
    """Use real authenticated routes and disposable migrated storage."""

    setUp = fixture.Step4CTests.setUp
    post = fixture.Step4CTests.post
    create_locations = fixture.Step4CTests.create_locations

    def rack_id(self, container_id):
        """Return the rack behind a rack placement container.

        Args:
            container_id: The rack's placement container.

        Returns:
            int: The rack's ID.
        """
        with self.factory() as db:
            return db.get(PlacementContainer, container_id).rack_id

    def events(self, action):
        """Count audit events with one action.

        Args:
            action: The audit action.

        Returns:
            int: How many events have it.
        """
        with self.factory() as db:
            return db.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.action == action))

    def test_rename_site_and_room_with_audit_and_conflicts(self):
        """Renames are validated, audited, and refuse duplicate names."""
        site, room, _ = self.create_locations()
        other = self.post("/api/v1/locations", {"kind": "site", "name": "Cabin"}).json["id"]
        self.post("/api/v1/locations", {"kind": "room", "site_id": site, "name": "Office"})
        response = self.post(f"/api/v1/sites/{site}", {"name": "  Main house "})
        self.assertEqual((response.status_code, response.json["name"]), (200, "Main house"))
        self.assertEqual(self.post(f"/api/v1/rooms/{room}", {"name": "Server room"}).status_code, 200)
        with self.factory() as db:
            self.assertEqual((db.get(Site, site).name, db.get(Room, room).name), ("Main house", "Server room"))
            event = db.scalar(select(AuditEvent).where(AuditEvent.action == "inventory.site.renamed"))
            self.assertEqual(json.loads(event.details_json)["before"], {"name": "Home"})
        cases = [
            (f"/api/v1/sites/{other}", {"name": "Main house"}, 409),
            (f"/api/v1/rooms/{room}", {"name": "Office"}, 409),
            (f"/api/v1/sites/{site}", {"name": " "}, 400),
            (f"/api/v1/rooms/{room}", {"name": "x" * 121}, 400),
            ("/api/v1/sites/99999", {"name": "Nowhere"}, 404),
            ("/api/v1/rooms/99999", {"name": "Nowhere"}, 404),
        ]
        for endpoint, payload, status in cases:
            with self.subTest(endpoint=endpoint, payload=payload):
                self.assertEqual(self.post(endpoint, payload).status_code, status)
        self.assertEqual(self.events("inventory.room.renamed"), 1)

    def place(self, container, **extra):
        """Add one manual device to a container; return its device ID.

        Args:
            container: The container ID.
            **extra: Fields added to the request (``name`` among them).

        Returns:
            int: The new device's ID.
        """
        response = self.post("/api/v1/rack-devices", {"name": extra.pop("name", "Box"), "height_u": 1,
                                                       "container_id": container, **extra})
        self.assertEqual(response.status_code, 201, response.json)
        return response.json["device_id"]

    def containers(self, room, kind):
        """Return a room's containers of one kind from the locations API.

        Args:
            room: The room's ID.
            kind: The container kind, such as ``wall``.

        Returns:
            list[dict]: The matching containers.
        """
        return [c for c in self.client.get("/api/v1/locations").json["containers"]
                if c["kind"] == kind and c["room_id"] == room]

    def test_empty_structure_deletes_in_one_step(self):
        """An empty site goes with its empty rooms and racks; equipment refuses."""
        site, room, container = self.create_locations()
        rack = self.rack_id(container)
        device = self.place(container, name="Switch", rack_u=3)
        for endpoint in (f"/api/v1/sites/{site}/delete", f"/api/v1/rooms/{room}/delete",
                         f"/api/v1/racks/{rack}/delete"):
            with self.subTest(endpoint=endpoint):
                response = self.post(endpoint, {})
                self.assertEqual(response.status_code, 409, response.json)
                self.assertIn("unplace", response.json["error"])
        self.assertEqual(self.post(f"/api/v1/rack-devices/{device}/unplace", {}).status_code, 200)
        preview = self.post(f"/api/v1/sites/{site}/delete", {"preview": True}).json
        self.assertEqual({k: preview[k] for k in ("kind", "name", "rooms", "racks", "unplaces", "blocked")},
                         {"kind": "location", "name": "Home", "rooms": 1, "racks": 1, "unplaces": 0,
                          "blocked": None})
        with self.factory() as db:
            self.assertIsNotNone(db.get(Site, site))
        response = self.post(f"/api/v1/sites/{site}/delete", {})
        self.assertEqual((response.status_code, response.json["rooms"], response.json["racks"]), (200, 1, 1))
        with self.factory() as db:
            self.assertIsNone(db.get(Site, site))
            self.assertIsNone(db.get(Room, room))
            self.assertIsNone(db.get(Rack, rack))
            self.assertEqual(db.scalar(select(func.count(PlacementContainer.id))), 0)
            event = db.scalar(select(AuditEvent).where(AuditEvent.action == "inventory.site.deleted"))
            details = json.loads(event.details_json)
        self.assertEqual((details["room_names"], details["rack_names"]), (["Basement"], ["Main Rack"]))
        self.assertEqual(self.post(f"/api/v1/sites/{site}/delete", {}).status_code, 404)
        self.assertEqual(self.post(f"/api/v1/rooms/{room}/delete", {"unplace": "yes"}).status_code, 400)

    def test_unplace_keeps_equipment_and_removes_everything_inside(self):
        """With unplace, equipment everywhere inside is unplaced, never deleted."""
        site, room, container = self.create_locations()
        rack = self.rack_id(container)
        top = next(c["id"] for c in self.containers(room, "rack_top"))
        mounted = self.place(container, name="Switch", rack_u=3)
        shared = self.place(container, name="Mini", rack_u=8, placement_mode="rack_shelf")
        on_top = self.place(top, name="Modem")
        # A shelf resting on the rack top, with something on it.
        shelf = self.post("/api/v1/locations", {"kind": "shelf", "room_id": room, "name": "Top shelf",
                                                "parent_id": top, "position": {"x": 0.5, "y": 0.5}}).json["id"]
        on_shelf = self.place(shelf, name="Pi")
        floor = self.containers(room, "floor")[0]["id"]
        standing = self.place(floor, name="UPS")
        preview = self.post(f"/api/v1/racks/{rack}/delete", {"preview": True}).json
        self.assertEqual((preview["unplaces"], preview["places"]), (4, 2))
        response = self.post(f"/api/v1/racks/{rack}/delete", {"unplace": True})
        self.assertEqual((response.status_code, response.json["unplaces"]), (200, 4), response.json)
        placed = {d["id"]: d["placement"] for d in self.client.get("/api/v1/locations").json["devices"]}
        for device in (mounted, shared, on_top, on_shelf):
            self.assertIsNone(placed[device], device)
        self.assertIsNotNone(placed[standing])
        with self.factory() as db:
            events = list(db.scalars(select(AuditEvent).where(AuditEvent.action == "inventory.device.unplaced")))
            self.assertEqual(len(events), 4)
            self.assertEqual(json.loads(events[0].details_json)["removed_with"], {"kind": "rack", "id": rack})
            self.assertEqual(json.loads(db.scalar(select(AuditEvent.details_json).where(
                AuditEvent.action == "inventory.rack.deleted")))["unplaces"], 4)
        # The room still holds the standing UPS; unplace takes it with the room.
        self.assertEqual(self.post(f"/api/v1/rooms/{room}/delete", {}).status_code, 409)
        self.assertEqual(self.post(f"/api/v1/sites/{site}/delete", {"unplace": True}).status_code, 200)
        placed = {d["id"]: d["placement"] for d in self.client.get("/api/v1/locations").json["devices"]}
        self.assertEqual(len(placed), 5)
        self.assertIsNone(placed[standing])

    def test_places_rename_and_delete(self):
        """Shelves and surfaces rename and delete; built-in places only rename."""
        _, room, container = self.create_locations()
        floor = self.containers(room, "floor")[0]["id"]
        top = next(c["id"] for c in self.containers(room, "rack_top"))
        shelf = self.post("/api/v1/locations", {"kind": "shelf", "room_id": room, "name": "Shelf",
                                                "parent_id": floor, "position": {"x": 0.2, "y": 0.2}}).json["id"]
        self.place(shelf, name="NAS")
        renamed = self.post(f"/api/v1/containers/{shelf}", {"name": "Media shelf"})
        self.assertEqual((renamed.status_code, renamed.json["name"]), (200, "Media shelf"))
        self.assertEqual(self.post(f"/api/v1/containers/{floor}", {"name": "Raised floor"}).status_code, 200)
        for endpoint, payload, status in [
            (f"/api/v1/containers/{container}", {"name": "X"}, 400),
            (f"/api/v1/containers/{top}", {"name": "X"}, 400),
            ("/api/v1/containers/99999", {"name": "X"}, 404),
            (f"/api/v1/containers/{floor}/delete", {}, 400),
            (f"/api/v1/containers/{container}/delete", {}, 400),
            ("/api/v1/containers/99999/delete", {}, 404),
            (f"/api/v1/containers/{shelf}/delete", {}, 409),
        ]:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.post(endpoint, payload).status_code, status)
        preview = self.post(f"/api/v1/containers/{shelf}/delete", {"preview": True}).json
        self.assertEqual((preview["kind"], preview["name"], preview["places"], preview["unplaces"]),
                         ("shelf", "Media shelf", 0, 1))
        self.assertEqual(self.post(f"/api/v1/containers/{shelf}/delete", {"unplace": True}).status_code, 200)
        with self.factory() as db:
            self.assertIsNone(db.get(PlacementContainer, shelf))
            self.assertEqual(db.get(PlacementContainer, floor).name, "Raised floor")
        self.assertEqual(self.events("inventory.container.renamed"), 2)
        self.assertEqual(self.events("inventory.container.deleted"), 1)
        # A shared rack shelf is a place too.
        self.place(container, name="Mini", rack_u=8, placement_mode="rack_shelf")
        rack_shelf = self.containers(room, "rack_shelf")[0]["id"]
        self.assertEqual(self.post(f"/api/v1/containers/{rack_shelf}", {"name": "Pi tray"}).status_code, 200)
        self.assertEqual(self.post(f"/api/v1/containers/{rack_shelf}/delete", {"unplace": True}).status_code, 200)
        self.assertEqual(self.containers(room, "rack_shelf"), [])

    def test_room_with_placed_equipment_networks_and_attributes_are_kept(self):
        """Placed equipment, site networks, and saved attributes block deletes."""
        site, room, container = self.create_locations()
        self.assertEqual(self.post(f"/api/v1/racks/{self.rack_id(container)}/delete", {}).status_code, 200)
        floor = next(c["id"] for c in self.client.get("/api/v1/locations").json["containers"]
                     if c["kind"] == "floor" and c["room_id"] == room)
        device = self.post("/api/v1/rack-devices", {"name": "UPS", "height_u": 1,
                                                     "container_id": floor}).json["device_id"]
        refused = self.post(f"/api/v1/rooms/{room}/delete", {})
        self.assertEqual(refused.status_code, 409)
        self.assertIn("equipment", refused.json["error"])
        self.assertEqual(self.post(f"/api/v1/rack-devices/{device}/unplace", {}).status_code, 200)
        with self.factory.begin() as db:
            # A legacy room reference without a placement is cleared by unplace.
            db.get(Device, device).room_id = room
            owner = db.scalar(select(InventoryObject).where(InventoryObject.room_id == room))
            if owner is None:
                owner = InventoryObject(room_id=room)
                db.add(owner)
                db.flush()
            db.add(Attribute(owner_id=owner.id, key="note", value=json.dumps({"type": "text", "value": "x"})))
            db.add(Network(site_id=site, vlan_id=10, name="Lab"))
        refused = self.post(f"/api/v1/rooms/{room}/delete", {})
        self.assertEqual(refused.status_code, 409)
        self.assertIn("attributes", refused.json["error"])
        self.assertIn("attributes", self.post(f"/api/v1/rooms/{room}/delete", {"preview": True}).json["blocked"])
        self.assertEqual(self.post(f"/api/v1/rooms/{room}/delete", {"unplace": True}).status_code, 409)
        with self.factory.begin() as db:
            for attribute in db.scalars(select(Attribute)):
                db.delete(attribute)
        self.assertEqual(self.post(f"/api/v1/rooms/{room}/delete", {}).status_code, 409)
        self.assertEqual(self.post(f"/api/v1/rooms/{room}/delete", {"unplace": True}).status_code, 200)
        with self.factory() as db:
            self.assertIsNone(db.get(Device, device).room_id)
        refused = self.post(f"/api/v1/sites/{site}/delete", {"unplace": True})
        self.assertEqual(refused.status_code, 409)
        self.assertIn("networks", refused.json["error"])

    def test_permissions(self):
        """Non-administrators and forged requests change nothing."""
        site, room, _ = self.create_locations()
        self.assertEqual(self.client.post(f"/api/v1/sites/{site}/delete", json={}).status_code, 403)
        with self.factory.begin() as db:
            db.scalar(select(User)).is_admin = False
        for endpoint, payload in ((f"/api/v1/sites/{site}", {"name": "X"}),
                                  (f"/api/v1/rooms/{room}/delete", {})):
            self.assertEqual(self.post(endpoint, payload).status_code, 403)
        with self.factory() as db:
            self.assertEqual(db.get(Site, site).name, "Home")
            self.assertIsNotNone(db.get(Room, room))


if __name__ == "__main__":
    unittest.main()
