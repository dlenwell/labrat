# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Build the header's Locations menu: every location and its rooms."""

from typing import Any

from sqlalchemy import func, select

from labrat.models import Room, Site


def location_menu(db: Any) -> list[dict[str, Any]]:
    """Return locations with their rooms for the header's Locations menu.

    Names sort the way people read them (case-insensitive), matching the
    Locations page.

    Args:
        db: Open database session.

    Returns:
        list[dict[str, Any]]: One entry per site with ``id``, ``name``, and
            ``rooms`` (each with ``id`` and ``name``).
    """
    sites = list(db.scalars(select(Site).order_by(func.lower(Site.name), Site.id)))
    rooms: dict[int, list[dict[str, Any]]] = {}
    for room in db.scalars(select(Room).order_by(func.lower(Room.name), Room.id)):
        rooms.setdefault(room.site_id, []).append({"id": room.id, "name": room.name})
    return [{"id": site.id, "name": site.name, "rooms": rooms.get(site.id, [])} for site in sites]
