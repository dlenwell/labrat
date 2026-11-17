# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Serve LabRat HTML pages that require an active authenticated user.

Authentication is enforced by shared session utilities, rather than repeated
in each route. HTML navigation redirects anonymous visitors to ``/signin``.
"""

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, abort, make_response, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue
from sqlalchemy import func, select

from labrat.auth.session import current_user, login_required, require_csrf, session_factory
from labrat.discovery.models import DiscoveredDevice, DiscoveredLink, DiscoverySource
from labrat.models import AuthIdentity, Device, DeviceClass, LocalCredential, Rack, Room, Site
from labrat.pages.activity import recent_user_activity
from labrat.placement import DevicePlacement, PlacementContainer
from labrat.themes import (
    ACCENT_COOKIE, THEME_COOKIE, THEME_COOKIE_MAX_AGE, accent_choices, chosen_accent, chosen_theme,
    theme_choices,
)

from labrat.pages.settings_backup import backup_bp
from labrat.pages.settings_integrations import integrations_bp, settings_context
from labrat.integrations import IntegrationError
from labrat.pages.settings_discovery import discovery_bp, settings_context as discovery_context

private_bp = Blueprint("private", __name__)
private_bp.register_blueprint(backup_bp)
private_bp.register_blueprint(integrations_bp)
private_bp.register_blueprint(discovery_bp)


def _rack_slug(value: str) -> str:
    """Make a readable, stable-path component from a rack's current name.

    Args:
        value: The rack's name.

    Returns:
        str: Lowercase ASCII words joined by hyphens, or ``rack`` when nothing
            is left.
    """
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-") or "rack"


#: Equipment types in the home page's "What's in your lab" bar, in order,
#: with their labels. Anything else is counted as "Other".
_COMPOSITION_TYPES = (
    ("server", "Servers"), ("switch", "Switches"), ("router", "Routers"),
    ("firewall", "Firewalls"), ("access_point", "Access points"),
    ("modem", "Modems"), ("ups", "UPS units"),
)


def _lab_composition(db: Any) -> list[dict[str, Any]]:
    """Count recognized observations by equipment type for the home page.

    Args:
        db: Open read session.

    Returns:
        list[dict[str, Any]]: ``category``, ``label``, ``count``, and
            ``share`` (0 to 1) for each type present, in a fixed order, with
            unlisted types folded into ``other``.
    """
    rows = dict(db.execute(
        select(DiscoveredDevice.category, func.count())
        .where(DiscoveredDevice.visibility == "recognized")
        .group_by(DiscoveredDevice.category)
    ).all())
    known = {category for category, _ in _COMPOSITION_TYPES}
    entries = [(category, label, rows.get(category, 0)) for category, label in _COMPOSITION_TYPES]
    entries.append(("other", "Other", sum(count for category, count in rows.items() if category not in known)))
    total = sum(count for _, _, count in entries)
    return [
        {"category": category, "label": label, "count": count, "share": count / total}
        for category, label, count in entries if count
    ]


@private_bp.get("/home")
@login_required
def home() -> ResponseReturnValue:
    """Render a cached overview of inventory and collector state.

    Only local database counts are queried. A page request never polls a
    controller or changes inventory, so the overview remains responsive when
    an integration is unavailable.

    Returns:
        ResponseReturnValue: Private dashboard with current saved counts and
            source health indicators.
    """
    with session_factory()() as db:
        counts = {
            "observed": db.scalar(select(func.count()).select_from(DiscoveredDevice).where(
                DiscoveredDevice.visibility == "recognized"
            )) or 0,
            "inventory": db.scalar(select(func.count()).select_from(Device)) or 0,
            "racks": db.scalar(select(func.count()).select_from(Rack)) or 0,
            "placed": db.scalar(select(func.count()).select_from(DevicePlacement)) or 0,
            # Every observed link counts: confirmed, inferred, and ambiguous
            # cables are all shown in Discovery with their confidence.
            "observed_links": db.scalar(
                select(func.count()).select_from(DiscoveredLink)
            ) or 0,
        }
        composition = _lab_composition(db)
        # The Lab health widgets read the cluster and network APIs; show
        # each half only when an enabled source can feed it.
        kinds = set(db.scalars(select(DiscoverySource.kind).where(DiscoverySource.enabled.is_(True))))
    # Source health comes from the shared ``source_status`` template global.
    return render_template(
        "private/home.html", counts=counts, composition=composition,
        live={"cluster": "proxmox" in kinds, "network": "unifi" in kinds},
    )


@private_bp.get("/observations")
@login_required
def observations() -> ResponseReturnValue:
    """Render the live, read-only operations screen for the lab.

    Rooms provide an optional presentation filter. Live readings come from
    cached discovery APIs; opening this page never polls infrastructure or
    changes inventory, placements, or collector settings.

    Returns:
        ResponseReturnValue: Authenticated observations console.
    """
    with session_factory()() as db:
        rooms = list(db.execute(
            select(Room.id, Room.name, Site.name.label("site_name"))
            .join(Site, Site.id == Room.site_id)
            .order_by(func.lower(Site.name), func.lower(Room.name))
        ))
    return render_template("private/observations.html", rooms=rooms)


@private_bp.get("/profile")
@login_required
def profile() -> ResponseReturnValue:
    """Render the signed-in user's account details and recent changes.

    Shows how the account signs in (provider names only, never secrets) and
    the user's newest audited inventory changes.

    Returns:
        ResponseReturnValue: Authenticated profile HTML.
    """
    viewer = current_user()
    now = datetime.now(timezone.utc)
    with session_factory()() as db:
        sign_in_methods = []
        if db.get(LocalCredential, viewer.id) is not None:
            sign_in_methods.append("Local password")
        sign_in_methods.extend(
            provider for provider in db.scalars(
                select(AuthIdentity.provider)
                .where(AuthIdentity.user_id == viewer.id)
                .order_by(AuthIdentity.provider)
            )
            if provider != "local"
        )
        activity, change_count = recent_user_activity(db, viewer.id, now)
    return render_template(
        "private/profile.html",
        member_since=viewer.created_at.date().isoformat() if viewer.created_at else None,
        sign_in_methods=sign_in_methods,
        activity=activity,
        change_count=change_count,
    )


@private_bp.get("/settings")
@login_required
def settings() -> ResponseReturnValue:
    """Render Settings with appearance and administrator inventory backups.

    Returns:
        ResponseReturnValue: Authenticated settings HTML.
    """
    panel = request.args.get("panel", "appearance")
    if panel not in {"appearance", "backup", "integrations", "discovery"}:
        abort(404)
    if panel in {"backup", "integrations", "discovery"} and not current_user().is_admin:
        abort(403)
    context = {}
    if panel == "integrations":
        try:
            context = settings_context()
        except IntegrationError as exc:
            context = {"integration_message": str(exc), "integration_ok": False}
    if panel == "discovery":
        try:
            context = discovery_context()
        except IntegrationError as exc:
            context = {"discovery_error": str(exc)}
    return render_template(
        "private/settings.html",
        **context,
        panel=panel,
        integration_saved=request.args.get("saved") == "integration",
        integration_removed=request.args.get("saved") == "removed",
        restored=request.args.get("saved") == "restore",
        themes=theme_choices(),
        accents=accent_choices(),
        saved=request.args.get("saved") == "appearance",
    )


WIDGET_VIEWS = ("samples", "live")


@private_bp.get("/settings/widgets")
@private_bp.get("/settings/widgets/<view>")
@login_required
def widgets(view: str = "samples") -> ResponseReturnValue:
    """Render the widget gallery: every widget with sample data, and live feeds.

    The gallery is where widgets are tried in each theme and hue before a
    page stitches them in (planning/specs/widgets.md). Samples draw from
    made-up data in the page script; Live draws the registered feeds from
    this lab's own APIs.

    Args:
        view: "samples" or "live"; anything else is a 404.

    Returns:
        ResponseReturnValue: Authenticated gallery HTML.
    """
    if view not in WIDGET_VIEWS:
        abort(404)
    return render_template("private/widgets.html", view=view, views=WIDGET_VIEWS)


@private_bp.post("/settings/appearance")
@login_required
def save_appearance() -> ResponseReturnValue:
    """Save this browser's theme and accent in cookies and return to Settings.

    The theme is a per-browser preference (the owner's choice), so nothing
    is written to the database or the audit log. Only a ready theme and a
    known accent are accepted; a form without an accent keeps the current
    one.

    Returns:
        ResponseReturnValue: A redirect to Settings, or 400 for an unknown
            or unavailable theme.
    """
    require_csrf()
    theme = chosen_theme(request.form.get("theme"))
    if theme is None:
        abort(400, description="Choose one of the available themes.")
    accent = request.form.get("accent")
    if accent is not None and chosen_accent(accent) is None:
        abort(400, description="Choose one of the available accent colors.")
    response = make_response(redirect(url_for("private.settings", saved="appearance")))
    for name, value in ((THEME_COOKIE, theme), (ACCENT_COOKIE, accent)):
        if value is not None:
            response.set_cookie(
                name, value, max_age=THEME_COOKIE_MAX_AGE, path="/",
                secure=request.is_secure, httponly=True, samesite="Lax",
            )
    return response


@private_bp.get("/topology")
@private_bp.get("/topology/<device>")
@login_required
def topology(device: str | None = None) -> ResponseReturnValue:
    """Render the interactive physical-infrastructure discovery graph.

    The graph is a read-only view of observed links until the user explicitly
    makes a placement choice. Graph dragging does not edit inventory records.
    A device link (`/topology/35`) opens that device's details.

    Args:
        device: Optional device link segment (see `_device_node_id`).

    Returns:
        ResponseReturnValue: Authenticated topology page HTML.
    """
    return render_template("private/topology.html", open_device=_device_node_id(device))


CLUSTER_VIEWS = ("summary", "compute", "networks", "storage", "nodes")


@private_bp.get("/cluster")
@private_bp.get("/cluster/<view>")
@private_bp.get("/cluster/<view>/<node>")
@login_required
def cluster(view: str = "summary", node: str | None = None) -> ResponseReturnValue:
    """Render the Proxmox cluster view: Summary, Compute, Networks, Storage, Nodes.

    Each tab has its own address so it can be linked and reloaded; the page
    switches between them in place (``cluster.js``) from one API read.

    Args:
        view: The tab to open; anything unknown is a 404.
        node: On the Nodes tab, the node to open (the first when missing).

    Returns:
        ResponseReturnValue: Authenticated cluster page HTML.
    """
    if view not in CLUSTER_VIEWS or (node is not None and view != "nodes"):
        abort(404)
    return render_template("private/cluster.html", view=view, views=CLUSTER_VIEWS, node=node or "")


NETWORK_VIEWS = ("overview", "zones", "networks", "wifi", "devices", "clients")


@private_bp.get("/networks")
@private_bp.get("/networks/<view>")
@login_required
def networks(view: str = "overview") -> ResponseReturnValue:
    """Render the Networks page: UniFi networks, firewall zones, Wi-Fi, devices, clients.

    Args:
        view: The tab to open; anything unknown is a 404.

    Returns:
        ResponseReturnValue: Authenticated networks page HTML.
    """
    if view not in NETWORK_VIEWS:
        abort(404)
    return render_template("private/networks.html", view=view, views=NETWORK_VIEWS)


@private_bp.get("/nodes")
@private_bp.get("/nodes/<node>")
@login_required
def nodes(node: str | None = None) -> ResponseReturnValue:
    """Send the old Nodes address to the Cluster page's Nodes tab.

    Args:
        node: Proxmox node name from the address.

    Returns:
        ResponseReturnValue: A redirect to ``/cluster/nodes[/<node>]``.
    """
    return redirect(url_for("private.cluster", view="nodes", node=node))


@private_bp.get("/topology/clients")
@private_bp.get("/topology/clients/<device>")
@login_required
def discovery_clients(device: str | None = None) -> ResponseReturnValue:
    """Render controller-reported clients with optional inventory actions.

    A device link (`/topology/clients/observed-110`) opens its details.

    Args:
        device: A device from the address, such as ``observed-110``, or None.

    Returns:
        ResponseReturnValue: The Clients page, opening that device's details
            when one is named.
    """
    return render_template("private/clients.html", open_device=_device_node_id(device))


def _room_context(db, room_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the room and site records shared by both room views.

    Args:
        db: Open database session.
        room_id: Selected room identifier.

    Returns:
        tuple[dict[str, Any], dict[str, Any]]: Room and site display records.

    Raises:
        werkzeug.exceptions.NotFound: The room or its site does not exist.
    """
    room = db.get(Room, room_id)
    if room is None:
        abort(404)
    site = db.get(Site, room.site_id)
    if site is None:
        abort(404)
    return {"id": room.id, "name": room.name}, {"id": site.id, "name": site.name}


@private_bp.get("/racks")
@login_required
def racks() -> ResponseReturnValue:
    """Send the Racks navigation link to a room's Equipment view.

    Rack View is now the Equipment view of a room, which shows every rack in
    the room plus its shelves and other equipment. This opens the first room
    (by location and room name) that has a rack, or any room, or Locations
    when no room exists yet.

    Returns:
        ResponseReturnValue: A redirect to a room's Equipment view or Locations.
    """
    with session_factory()() as db:
        room_id = db.scalar(
            select(Room.id).join(Site, Site.id == Room.site_id).join(Rack, Rack.room_id == Room.id)
            .order_by(func.lower(Site.name), func.lower(Room.name)).limit(1)
        ) or db.scalar(
            select(Room.id).join(Site, Site.id == Room.site_id)
            .order_by(func.lower(Site.name), func.lower(Room.name)).limit(1)
        )
    if room_id is None:
        return redirect(url_for("private.locations"))
    return redirect(url_for("private.room_equipment", room_id=room_id))


@private_bp.get("/racks/<int:rack_id>-<rack_slug>")
@login_required
def rack_detail(rack_id: int, rack_slug: str) -> ResponseReturnValue:
    """Send an old shareable rack address to its room's Equipment view.

    The rack's own panel is the URL fragment, so the page scrolls to it.

    Args:
        rack_id: Stable rack identifier.
        rack_slug: Readable name suffix; ignored, since the ID is canonical.

    Returns:
        ResponseReturnValue: A redirect, or a not-found response.
    """
    with session_factory()() as db:
        rack = db.get(Rack, rack_id)
        if rack is None:
            abort(404)
        room_id = rack.room_id
    return redirect(
        url_for("private.room_equipment", room_id=room_id) + f"#rack-{rack_id}"
    )


@private_bp.get("/areas/<int:room_id>")
@login_required
def room_default(room_id: int) -> ResponseReturnValue:
    """Open Equipment for a room with racks, otherwise open Room wiring.

    Args:
        room_id: Selected room identifier.

    Returns:
        ResponseReturnValue: Authenticated default room page or a 404 response.
    """
    with session_factory()() as db:
        room, site = _room_context(db, room_id)
        has_racks = db.scalar(select(Rack.id).where(Rack.room_id == room_id).limit(1)) is not None
    return render_template("private/racks.html", room=room, site=site,
                           view="equipment" if has_racks else "wiring")


def _device_node_id(device: str | None) -> str | None:
    """Turn a device link's path segment into a topology node ID.

    Device links name an inventory device by its number ("35") and a device
    only observed by discovery as "observed-110". Anything else is not a
    device link.

    Args:
        device: The path segment, or None for a link without a device.

    Returns:
        str | None: "device:35" or "observed:110", or None without a device.
    """
    if device is None:
        return None
    match = re.fullmatch(r"(observed-)?(\d+)", device)
    if match is None:
        abort(404)
    return f"{'observed' if match.group(1) else 'device'}:{match.group(2)}"


@private_bp.get("/areas/<int:room_id>/wiring")
@private_bp.get("/areas/<int:room_id>/wiring/<device>")
@login_required
def room_wiring(room_id: int, device: str | None = None) -> ResponseReturnValue:
    """Render the room wiring diagram, including rooms that contain racks.

    Args:
        room_id: Selected room identifier.
        device: Optional device link segment; the page opens its details.

    Returns:
        ResponseReturnValue: Authenticated room wiring page or a 404 response.
    """
    open_device = _device_node_id(device)
    with session_factory()() as db:
        room, site = _room_context(db, room_id)
    return render_template("private/racks.html", room=room, site=site, view="wiring",
                           open_device=open_device)


@private_bp.get("/areas/<int:room_id>/equipment")
@private_bp.get("/areas/<int:room_id>/equipment/<device>")
@private_bp.get("/areas/<int:room_id>/<device>")
@login_required
def room_equipment(room_id: int, device: str | None = None) -> ResponseReturnValue:
    """Render a room's Equipment view: racks front and rear, shelves, and more.

    This view shows rack elevations alongside equipment around the room. It
    reads cached observations and saved placements through the existing APIs;
    viewing it never changes inventory or discovery data.

    A device link (`/areas/<area>/equipment/<device>`, or just
    `/areas/<area>/<device>`) opens that device's details in the drawer, so
    a refresh keeps it open and the address can be shared.

    Args:
        room_id: Selected room identifier.
        device: Optional device link segment; the page opens its details.

    Returns:
        ResponseReturnValue: Authenticated Equipment view or a 404 response.
    """
    open_device = _device_node_id(device)
    with session_factory()() as db:
        room, site = _room_context(db, room_id)
    return render_template("private/racks.html", room=room, site=site, view="equipment",
                           open_device=open_device)


# A room's structural containers, and the top-level holders that sit on them.
_AREA_STRUCTURE = frozenset({"floor", "wall", "ceiling"})
_AREA_HOLDERS = frozenset({"shelf", "surface", "area"})


def _location_tree(db, site_id: int | None = None) -> list[dict[str, Any]]:
    """Build the site -> room -> rack tree shown on the Locations pages.

    Counts come from saved placements only. A device counts toward a rack
    when its container is the rack itself or nested under it (shared rack
    shelves and the rack top).

    Args:
        db: Open database session.
        site_id: Limit the tree to one site; ``None`` returns every site.

    Returns:
        list[dict[str, Any]]: Sites ordered by name. Each has ``rooms``
            (ordered by name, each with its ``racks`` and its top-level
            ``contents``: racks, shelves and surfaces on its walls, floor,
            or ceiling, and devices placed straight on those, each with a
            ``kind``, ``name``, and ``meta`` line) and summary counts.
    """
    # Order names the way people read them: "bedroom" sorts with "Office".
    site_query = select(Site).order_by(func.lower(Site.name))
    if site_id is not None:
        site_query = site_query.where(Site.id == site_id)
    sites = list(db.scalars(site_query))
    site_ids = [site.id for site in sites]
    rooms = list(db.scalars(
        select(Room).where(Room.site_id.in_(site_ids)).order_by(func.lower(Room.name))
    )) if site_ids else []
    room_ids = [room.id for room in rooms]
    racks = list(db.scalars(
        select(Rack).where(Rack.room_id.in_(room_ids)).order_by(func.lower(Rack.name))
    )) if room_ids else []
    containers = {
        container.id: container for container in db.scalars(
            select(PlacementContainer).where(PlacementContainer.room_id.in_(room_ids))
        )
    } if room_ids else {}

    def owning_rack(container_id: int | None) -> int | None:
        """Walk up nested containers to the rack that holds them, if any."""
        seen: set[int] = set()
        while container_id is not None and container_id not in seen:
            seen.add(container_id)
            container = containers.get(container_id)
            if container is None:
                return None
            if container.kind == "rack":
                return container.rack_id
            container_id = container.parent_id
        return None

    def owning_shelf(container_id: int | None) -> int | None:
        """The top-level shelf or surface a container sits in, if any."""
        seen: set[int] = set()
        top = None
        while container_id is not None and container_id not in seen:
            seen.add(container_id)
            container = containers.get(container_id)
            if container is None or container.kind in _AREA_STRUCTURE:
                break
            if container.kind in _AREA_HOLDERS:
                top = container.id
            container_id = container.parent_id
        return top

    placed_by_room: dict[int, int] = {}
    placed_by_rack: dict[int, int] = {}
    placed_by_shelf: dict[int, int] = {}
    loose: list[DevicePlacement] = []
    placements = list(db.scalars(select(DevicePlacement).where(
        DevicePlacement.container_id.in_(list(containers))
    ))) if containers else []
    if containers:
        for placement in placements:
            container = containers[placement.container_id]
            if container.kind in _AREA_STRUCTURE:
                loose.append(placement)
            shelf_id = owning_shelf(container.id)
            if shelf_id is not None:
                placed_by_shelf[shelf_id] = placed_by_shelf.get(shelf_id, 0) + 1
            container = containers[placement.container_id]
            placed_by_room[container.room_id] = placed_by_room.get(container.room_id, 0) + 1
            rack_id = owning_rack(container.id)
            if rack_id is not None:
                placed_by_rack[rack_id] = placed_by_rack.get(rack_id, 0) + 1

    racks_by_room: dict[int, list[dict[str, Any]]] = {}
    for rack in racks:
        racks_by_room.setdefault(rack.room_id, []).append({
            "id": rack.id,
            "name": rack.name,
            "slug": _rack_slug(rack.name),
            "height_u": rack.height_u,
            "width_inches": rack.width_inches,
            "has_rear": bool(rack.has_rear),
            "device_count": placed_by_rack.get(rack.id, 0),
        })
    # Each area's top level (owner): racks, then shelves and surfaces on its
    # walls, floor, or ceiling, then devices placed straight on those.
    device_ids = [placement.device_id for placement in loose]
    devices = {device.id: device for device in db.scalars(
        select(Device).where(Device.id.in_(device_ids))
    )} if device_ids else {}
    class_ids = {device.device_class_id for device in devices.values() if device.device_class_id}
    classes = {item.id: item.name for item in db.scalars(
        select(DeviceClass).where(DeviceClass.id.in_(class_ids))
    )} if class_ids else {}
    contents_by_room: dict[int, list[dict[str, Any]]] = {}
    for room_id, room_racks in racks_by_room.items():
        contents_by_room.setdefault(room_id, []).extend({
            "kind": "rack", "name": rack["name"], "rack": rack,
            "meta": f'{rack["width_inches"]} inch · {rack["height_u"]}U · {rack["device_count"]} mounted',
        } for rack in room_racks)
    holders = sorted((container for container in containers.values() if container.kind in _AREA_HOLDERS
                      and (container.parent_id is None
                           or containers.get(container.parent_id) is None
                           or containers[container.parent_id].kind in _AREA_STRUCTURE)),
                     key=lambda container: container.name.lower())
    for container in holders:
        count = placed_by_shelf.get(container.id, 0)
        contents_by_room.setdefault(container.room_id, []).append({
            "kind": container.kind, "name": container.name,
            "meta": f'{container.kind.replace("_", " ").capitalize()} · {count} {"item" if count == 1 else "items"}',
        })
    places = {"floor": "on the floor", "wall": "on a wall", "ceiling": "on the ceiling"}
    for placement in sorted(loose, key=lambda item: (devices[item.device_id].name.lower()
                                                     if item.device_id in devices else "")):
        device = devices.get(placement.device_id)
        if device is None:
            continue
        container = containers[placement.container_id]
        contents_by_room.setdefault(container.room_id, []).append({
            "kind": "device", "name": device.name,
            # Its type when inventory has one, then where it sits.
            "meta": " · ".join(part for part in (classes.get(device.device_class_id),
                                                 places.get(container.kind)) if part).capitalize(),
        })
    rooms_by_site: dict[int, list[dict[str, Any]]] = {}
    for room in rooms:
        rooms_by_site.setdefault(room.site_id, []).append({
            "id": room.id,
            "name": room.name,
            "racks": racks_by_room.get(room.id, []),
            "contents": contents_by_room.get(room.id, []),
            "placed_count": placed_by_room.get(room.id, 0),
        })
    tree = []
    for site in sites:
        site_rooms = rooms_by_site.get(site.id, [])
        tree.append({
            "id": site.id,
            "name": site.name,
            "rooms": site_rooms,
            "room_count": len(site_rooms),
            "rack_count": sum(len(room["racks"]) for room in site_rooms),
            "placed_count": sum(room["placed_count"] for room in site_rooms),
        })
    return tree


@private_bp.get("/locations")
@login_required
def locations() -> ResponseReturnValue:
    """Render every location: sites as buildings, with rooms and racks nested.

    With exactly one location this redirects to its own page (owner).

    Returns:
        ResponseReturnValue: Authenticated Locations page, or a redirect.
    """
    with session_factory()() as db:
        tree = _location_tree(db)
    # With a single location its own page is the Locations page (owner).
    if len(tree) == 1:
        return redirect(url_for("private.location_detail", site_id=tree[0]["id"]))
    return render_template("private/locations.html", sites=tree,
                           all_sites=[{"id": site["id"], "name": site["name"]} for site in tree])


@private_bp.get("/locations/<int:site_id>")
@login_required
def location_detail(site_id: int) -> ResponseReturnValue:
    """Render one site with its rooms and racks nested inside it.

    Args:
        site_id: Selected site identifier.

    Returns:
        ResponseReturnValue: Authenticated site page or a 404 response.
    """
    with session_factory()() as db:
        tree = _location_tree(db, site_id)
        # Every location's name, for the heading's location tabs.
        all_sites = [{"id": site.id, "name": site.name}
                     for site in db.scalars(select(Site).order_by(func.lower(Site.name)))]
    if not tree:
        abort(404)
    return render_template("private/location_detail.html", site=tree[0], all_sites=all_sites)


@private_bp.get("/locations/map")
@private_bp.get("/locations/<int:site_id>/map")
@login_required
def locations_map(site_id: int | None = None) -> ResponseReturnValue:
    """Render the Locations map: one canvas of buildings, areas, and racks.

    The canvas (``locations-map.js``) draws from the locations and topology
    APIs; this page only supplies the frame, the heading, and the scope.
    Unlike ``/locations``, the all-locations map never redirects, so it is
    reachable with a single location too.

    Args:
        site_id: Limit the map to one location; ``None`` draws them all.

    Returns:
        ResponseReturnValue: Authenticated map page or a 404 response.
    """
    with session_factory()() as db:
        all_sites = [{"id": site.id, "name": site.name}
                     for site in db.scalars(select(Site).order_by(func.lower(Site.name)))]
    site = next((item for item in all_sites if item["id"] == site_id), None)
    if site_id is not None and site is None:
        abort(404)
    return render_template("private/locations_map.html", site=site, all_sites=all_sites)


@private_bp.get("/sites")
def legacy_sites() -> ResponseReturnValue:
    """Permanently redirect the former Sites address to Locations.

    Returns:
        ResponseReturnValue: A 301 redirect to ``/locations``.
    """
    return redirect(url_for("private.locations"), code=301)


@private_bp.get("/sites/<int:site_id>")
def legacy_site_detail(site_id: int) -> ResponseReturnValue:
    """Permanently redirect a former site address to its Locations page.

    Args:
        site_id: Site identifier from the old ``/sites/<id>`` address.

    Returns:
        ResponseReturnValue: A 301 redirect to ``/locations/<id>``.
    """
    return redirect(url_for("private.location_detail", site_id=site_id), code=301)


@private_bp.get("/rooms/<int:room_id>")
@private_bp.get("/rooms/<int:room_id>/<path:rest>")
def legacy_room(room_id: int, rest: str = "") -> ResponseReturnValue:
    """Redirect an old room address to its area (rooms are now areas).

    The interface calls a room an **area** (it can be outdoors or a roof);
    the data model, API, and code keep the name ``Room``. Old ``/rooms/...``
    page addresses, bookmarks and device links included, move permanently,
    keeping the rest of the path and the query string.

    Args:
        room_id: The area's (room's) identifier.
        rest: Anything after it, such as ``equipment/35``.

    Returns:
        ResponseReturnValue: A 301 redirect to ``/areas/...``.
    """
    target = f"/areas/{room_id}" + (f"/{rest}" if rest else "")
    if request.query_string:
        target += "?" + request.query_string.decode("latin-1")
    return redirect(target, code=301)


@private_bp.get("/areas/<int:room_id>/floor-plan")
@private_bp.get("/areas/<int:room_id>/floor-plan/<device>")
@login_required
def room_view(room_id: int, device: str | None = None) -> ResponseReturnValue:
    """Render the dimensionless top-down physical view of one room.

    Args:
        room_id: Selected room identifier.
        device: Optional device link segment; the page opens its details.

    Returns:
        ResponseReturnValue: Authenticated room-layout page or a 404 response.
    """
    open_device = _device_node_id(device)
    with session_factory()() as db:
        room, site = _room_context(db, room_id)
    return render_template("private/room.html", room=room, site=site, view="floor",
                           open_device=open_device)
