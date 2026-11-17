# LabRat topology and physical placement decisions

Copyright 2026 LabRat contributors. Licensed under Apache-2.0.

This note records the current product and data boundaries for Discovery, Room
View, and Rack View. The implementation in the repository is authoritative if
details in an older design discussion differ.

## Discovery observations are not inventory

The independent collector stores source-scoped observations. Flask and browser
JavaScript read that cache; the browser never calls UniFi or Proxmox directly.
The Discovery map includes recognized physical equipment and evidenced direct
links. Confirmed and inferred links are trusted by default; ambiguous links
remain tentative. Graph movement and pinned coordinates are visual preferences,
not physical placement or cable evidence.

Discovered devices do not silently become canonical inventory. When placing an
unlinked observation in Discovery, the default is to create a new inventory
device. An operator may instead choose an existing device and link the
observation to it. Linking preserves both inventory records. Similar names and
MAC addresses do not automatically merge records. A source cannot map two
distinct observations to the same canonical device. A discovered link never
rewrites canonical interface cabling.

## Physical locations and placements

A site contains rooms. A room's dimensionless top-down layout can include floor
and wall-mounted items, shelves, rack footprints, and items placed on top of a
rack. It does not draw rack interiors. Relative coordinates range from 0 to 1
and are not physical measurements. Devices on a shelf are auto-packed and can
be reordered horizontally.

`placement_containers` describe room-scoped locations and may be nested where
the container type allows it. `device_placements` holds at most one active
placement per canonical device. Placement changes are explicit and audited;
unplacing removes the active placement without deleting inventory or discovery
evidence.

Rack View draws a front elevation and, when the rack has rear access, a rear
elevation beside it. A front-only rack has no rear view. Rack-mounted equipment
occupies the front, rear, or full depth. Full-depth equipment appears in both
views and participates in collision checks on both faces. A shared rack shelf
uses one U on a selected face and can contain several small devices arranged
side by side. Rack width is 10 or 19 inches. Network-port facing is stored
independently from rack placement.

Port activity follows source state where available. Explicit down is different
from unknown and overrides positive speed. Proxmox uses the API's `active`
field. Connector profiles are shipped as offline package data; no runtime LLM
or external lookup is needed to display known UniFi layouts.

## Persistence and safety

- SQLAlchemy models and Alembic revisions define the database. Flask does not
  create or mutate schema at startup.
- Placement, location, and inventory writes require an authenticated
  administrator and CSRF protection, validate container and rack constraints,
  and add an append-only audit event in the same transaction.
- Discovery retains source, evidence, confidence, and last-seen state. A
  complete successful scan can reconcile vanished ports and links; temporary
  source failure is not evidence that a device should be deleted.
- Credentials remain in ignored local configuration and never enter SQLite,
  audit details, documentation, or Git.

## Current views and interaction

- **Sites** lists locations and drills into site and room pages.
- **Room View** edits a room's top-down layout and can place manual or
  discovered inventory on a floor, wall, shelf, rack, or rack top.
- **Discovery** shows the observation graph, a persistent unplaced-only filter,
  equipment categories, device details, and the room-first placement flow.
- **Rack View** shows saved rack positions and ports, with a single sliding
  editor column for equipment details, placement, manual inventory, and rack
  settings. Rack selection uses `/racks/<id>-<rack-slug>` so refresh and browser
  history retain the selected rack.

The API reference at `/docs/api` is generated from registered Flask routes and
view docstrings. The public guides at `/docs` describe the current inventory
and collector boundaries.

## Schema history and intentionally unimplemented areas

Revisions `0001` through `0009` are documented in
[`database-schema.md`](database-schema.md). Revisions 0008 and 0009 add live
port state, maximum speed, and connector type. The group tables from revision
0003 currently have no group editor, API, or collector assignment workflow.

This build does not automatically reconcile observations into canonical
cabling. Power distribution, telemetry time series, and rack heatmaps are not
implemented. Any change that needs a new schema revision should preserve
existing inventory and discovery rows and be reviewed before implementation.
