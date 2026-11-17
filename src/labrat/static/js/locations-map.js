/*
 * Copyright 2026 LabRat contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Locations map (owner: one large canvas with the rooms and racks, their
 * equipment, and the links between rooms going through patch panels at
 * the edge). Drawn in SVG from the locations and topology APIs:
 *
 * - each location is a building outline, its areas rooms in a row;
 * - each rack is a front elevation, its equipment bars at their units, in
 *   the order the floor plan stands them; other placed equipment (floor,
 *   walls, shelves, rack tops, ceiling) is a column of cards;
 * - a cable inside an area runs in the gutter beside its equipment; a
 *   cable to another area leaves through a socket on its room's patch
 *   panel (the strip along the room's top edge), runs along its own lane
 *   of the corridor above the rooms, and drops into the other room's
 *   panel.
 *
 * Navigation: drag to pan, the wheel (or pinch) to zoom about the
 * pointer, the heading's zoom and fit tools, keyboard arrows, +, -, and
 * 0 on the focused canvas, the minimap, and Find. Selecting a device
 * opens its panel (where it is, its cables, each a jump to the other
 * end, and Open in its area); an area's name opens the area.
 */
(() => {
    "use strict";

    const NS = "http://www.w3.org/2000/svg";
    const page = document.querySelector(".locations-map-page");
    if (!page) return;

    // World geometry, in SVG units (1 unit is one CSS pixel at 100%).
    const U = 13;                 // One rack unit.
    const RACK_WIDTH = { 19: 176, 10: 118 };
    const RAIL = 10;              // Rack rail width on each side.
    const CARD = { width: 176, height: 26, gap: 6 };
    const ROOM = { pad: 22, head: 30, panel: 18, gutter: 26, gap: 44, rowGap: 16, minWidth: 260 };
    const LANE = 9;               // Corridor lane spacing.
    const SOCKET = 12;            // Patch-panel socket pitch.
    const SITE = { pad: 28, head: 34, gap: 120 };
    const STACK_GAP = ROOM.panel + 3 * LANE; // Between stacked areas: a panel and its lanes.

    const siteId = Number(page.dataset.siteId) || null;
    const areaBase = (page.dataset.areaUrl || "/areas/0").replace(/0$/, "");
    const host = document.getElementById("locations-map");
    const svgRoot = document.getElementById("locations-map-svg");
    const world = document.getElementById("locations-map-world");
    const tip = document.getElementById("locations-map-tip");
    const empty = document.getElementById("locations-map-empty");
    const notice = document.getElementById("locations-notice");

    const mini = document.getElementById("locations-map-mini");
    const storageKey = `labrat.locationsMap.view.${siteId ?? "all"}`;
    const view = { x: 0, y: 0, k: 1 };
    const state = { bounds: null, roomCables: true };

    /**
     * Make an SVG element with attributes.
     * @param {string} name Element name.
     * @param {object} [attrs] Attributes.
     * @param {Array<Node|string>} [children] Children; strings become text.
     * @returns {SVGElement} The element.
     */
    function el(name, attrs = {}, children = []) {
        const node = document.createElementNS(NS, name);
        for (const [key, value] of Object.entries(attrs)) {
            if (value !== null && value !== undefined && value !== false) node.setAttribute(key, String(value));
        }
        for (const child of children) node.append(typeof child === "string" ? document.createTextNode(child) : child);
        return node;
    }

    /**
     * Fetch JSON from the app's API.
     * @param {string} path Same-origin path.
     * @returns {Promise<object>} Parsed response.
     */
    async function api(path) {
        return labratJson(path, { failure: `Could not load ${path}` });
    }

    // ------------------------------------------------------------------
    // Model: sites, rooms, racks, and where each device sits.
    // ------------------------------------------------------------------

    /**
     * Join the locations and topology documents into what the map draws.
     * @param {object} locations `/api/v1/locations`.
     * @param {object} topology `/api/v1/topology`.
     * @returns {{sites: Array<object>, devices: Map<string, object>, links: Array<object>}} The model.
     */
    function model(locations, topology) {
        const containers = new Map(locations.containers.map((item) => [item.id, item]));
        const nodes = new Map(topology.nodes.map((node) => [node.id, node]));
        const rackOf = (container) => {
            // A rack's own container, its top, and shelves nested under it.
            for (let item = container; item; item = containers.get(item.parent_id)) {
                if (item.rack_id) return item;
            }
            return null;
        };
        const rooms = new Map(locations.rooms.map((room) => [room.id, { ...room, racks: [], loose: [] }]));
        const racks = new Map();
        for (const rack of locations.racks) {
            const own = locations.containers.find((item) => item.kind === "rack" && item.rack_id === rack.id);
            const entry = { ...rack, devices: [], top: [], x: own?.position?.x ?? 0.5 };
            racks.set(rack.id, entry);
            rooms.get(rack.room_id)?.racks.push(entry);
        }
        const devices = new Map();
        for (const device of locations.devices) {
            const placement = device.placement;
            if (!placement || !rooms.has(placement.container_room_id)) continue;
            const key = `device:${device.id}`;
            const node = nodes.get(key);
            const record = {
                key, id: device.id, name: device.name, heightU: device.height_u || 1,
                category: node?.category || device.class_name || "other", placement,
                room: placement.container_room_id,
            };
            devices.set(key, record);
            const container = containers.get(placement.container_id);
            const holder = container ? rackOf(container) : null;
            const rack = holder ? racks.get(holder.rack_id) : null;
            if (rack && placement.container_kind === "rack" && placement.rack_u) {
                rack.devices.push(record);
                record.rack = rack.id;
            } else {
                record.where = placement.container_kind === "rack_top" && rack ? `on ${rack.name}`
                    : placement.wall_side
                        ? `${placement.wall_side} wall`
                        : (placement.container_name || placement.container_kind || "").toLowerCase();
                rooms.get(record.room).loose.push(record);
            }
        }
        for (const room of rooms.values()) {
            room.racks.sort((a, b) => a.x - b.x || a.name.localeCompare(b.name));
            room.loose.sort((a, b) => a.where.localeCompare(b.where) || a.name.localeCompare(b.name));
        }
        const sites = locations.sites
            .filter((site) => siteId === null || site.id === siteId)
            .map((site) => ({
                ...site,
                rooms: [...rooms.values()].filter((room) => room.site_id === site.id)
                    .sort((a, b) => a.name.localeCompare(b.name)),
            }));
        const links = topology.links.filter((link) => devices.has(link.from) && devices.has(link.to));
        return { sites, devices, links };
    }

    // ------------------------------------------------------------------
    // Layout: sizes and positions in world units.
    // ------------------------------------------------------------------

    /**
     * Arrange one room's racks and cards from its top-left corner, and give
     * each device the point its cables leave from (the right edge, into
     * the gutter beside it).
     * @param {object} room Room with racks and loose devices.
     * @param {number} originX Room's left edge.
     * @param {number} originY Room's top edge.
     * @returns {{width: number, height: number}} The room's size.
     */
    function arrangeRoom(room, originX, originY) {
        let x = originX + ROOM.pad;
        const contentTop = originY + ROOM.head;
        let deepest = 0;
        for (const rack of room.racks) {
            rack.width = RACK_WIDTH[rack.width_inches] || RACK_WIDTH[19];
            rack.height = rack.height_u * U + 2 * RAIL;
            rack.x = x;
            rack.y = contentTop + 16;
            for (const device of rack.devices) {
                const units = Math.max(1, device.heightU);
                const topU = device.placement.rack_u + units - 1;
                device.x = rack.x + RAIL;
                device.y = rack.y + RAIL + (rack.height_u - topU) * U;
                device.w = rack.width - 2 * RAIL;
                device.h = units * U;
                device.anchor = { x: device.x + device.w, y: device.y + device.h / 2, gutter: rack.x + rack.width };
            }
            deepest = Math.max(deepest, rack.y + rack.height - contentTop);
            x += rack.width + ROOM.gutter;
        }
        if (room.loose.length) {
            let y = contentTop + 16;
            for (const device of room.loose) {
                device.x = x;
                device.y = y;
                device.w = CARD.width;
                device.h = CARD.height;
                device.anchor = { x: x + CARD.width, y: y + CARD.height / 2, gutter: x + CARD.width };
                y += CARD.height + CARD.gap;
            }
            deepest = Math.max(deepest, y - contentTop);
            x += CARD.width + ROOM.gutter;
        }
        return {
            width: Math.max(ROOM.minWidth, x - originX + ROOM.pad - ROOM.gutter),
            height: ROOM.head + Math.max(80, deepest) + ROOM.pad,
        };
    }

    /**
     * Stack a building's areas into columns no taller than its tallest
     * area (owner: no wasted space beside a tall, racked area), tallest
     * first, each short area joining the first column it fits under.
     * @param {Array<object>} rooms Rooms with measured `size`.
     * @returns {{stacks: Array<Array<object>>, height: number}} Columns and their height.
     */
    function stack(rooms) {
        const height = Math.max(0, ...rooms.map((room) => room.size.height));
        const stacks = [];
        const order = [...rooms].sort((a, b) => b.size.height - a.size.height || a.name.localeCompare(b.name));
        for (const room of order) {
            const fits = stacks.find((column) => column.used + STACK_GAP + room.size.height <= height
                && Math.abs(column.width - room.size.width) <= 0.35 * Math.max(column.width, room.size.width));
            if (fits) {
                fits.rooms.push(room);
                fits.used += STACK_GAP + room.size.height;
                fits.width = Math.max(fits.width, room.size.width);
            } else {
                stacks.push({ rooms: [room], used: room.size.height, width: room.size.width });
            }
        }
        return { stacks: stacks.map((column) => column.rooms), height };
    }

    /**
     * How many columns a row holds: the count whose rows come closest to
     * the canvas's shape (owner: the map should fill a laptop screen).
     * @param {Array<Array<object>>} stacks Columns of rooms.
     * @param {number} height Column height.
     * @param {number} target Canvas width over height, per building.
     * @returns {number} Columns per row.
     */
    function bestColumns(stacks, height, target) {
        let best = stacks.length || 1;
        let bestScore = Infinity;
        for (let columns = 1; columns <= stacks.length; columns += 1) {
            let width = 0;
            let rows = 0;
            for (let start = 0; start < stacks.length; start += columns) {
                const row = stacks.slice(start, start + columns);
                width = Math.max(
                    width,
                    row.reduce(
                        (total, column) => total + Math.max(...column.map((room) => room.size.width)), 0
                    ) + (row.length - 1) * ROOM.gap
                );
                rows += 1;
            }
            const total = rows * (height + 4 * LANE + ROOM.rowGap);
            const score = Math.abs(Math.log((width / total) / target));
            if (score < bestScore - 1e-9) {
                best = columns;
                bestScore = score;
            }
        }
        return best;
    }

    /**
     * Sort the cables between rooms by the corridors they use: one lane
     * in the corridor above each end's row (a stacked area's cable reaches
     * it up a riser beside its column), the building's spine when the
     * rows or buildings differ, and the campus band across the top when
     * the buildings differ.
     * @param {object} data The model, rooms already in rows.
     * @returns {Array<object>} One route per cable between rooms.
     */
    function routes(data) {
        const found = [];
        for (const link of data.links) {
            const a = data.devices.get(link.from);
            const b = data.devices.get(link.to);
            if (a.room === b.room) continue;
            const ra = data.rooms.get(a.room);
            const rb = data.rooms.get(b.room);
            const kind = ra.site !== rb.site ? "site" : ra.row !== rb.row ? "row" : "same";
            found.push({ link, ends: [a, b], rooms: [ra, rb], kind });
        }
        return found;
    }

    /**
     * Place every site, row, column, room, rack, and device: areas in
     * columns, columns in rows, each row under its own corridor, a spine
     * down the building's left side, and a campus band over the buildings.
     * @param {object} data The model.
     * @returns {{width: number, height: number}} World size.
     */
    function layout(data) {
        data.rooms = new Map();
        const target = Math.max(
            0.8, host.clientWidth / Math.max(1, host.clientHeight)
        ) / Math.max(1, data.sites.length);
        for (const site of data.sites) {
            for (const room of site.rooms) {
                room.size = arrangeRoom(room, 0, 0);
                room.site = site;
                data.rooms.set(room.id, room);
            }
            const packed = stack(site.rooms);
            const columns = bestColumns(packed.stacks, packed.height, target);
            site.rows = [];
            packed.stacks.forEach((rooms, index) => {
                const rowIndex = Math.floor(index / columns);
                const row = (site.rows[rowIndex] ||= { stacks: [], lanes: 0 });
                const column = { rooms, risers: 0, riser: 0 };
                row.stacks.push(column);
                rooms.forEach((room, level) => Object.assign(room, { row: rowIndex, column, level }));
            });
            site.spineLanes = 0;
        }
        data.routes = routes(data);
        let campusLanes = 0;
        for (const route of data.routes) {
            for (const room of route.rooms) {
                room.site.rows[room.row].lanes += route.kind === "same" ? 0.5 : 1;
                if (room.level > 0) room.column.risers += 1;
            }
            if (route.kind === "row") route.rooms[0].site.spineLanes += 1;
            if (route.kind === "site") {
                for (const room of route.rooms) room.site.spineLanes += 1;
                campusLanes += 1;
            }
        }
        data.campusLanes = campusLanes;
        const campus = campusLanes ? (campusLanes + 1) * LANE + 16 : 0;
        let cursorX = 0;
        for (const site of data.sites) {
            site.x = cursorX;
            site.y = campus;
            site.spineWidth = site.spineLanes ? (site.spineLanes + 1) * LANE + 8 : 0;
            const left = site.x + SITE.pad + site.spineWidth;
            let y = site.y + SITE.head;
            let widest = 0;
            for (const row of site.rows) {
                row.lanes = Math.ceil(row.lanes);
                row.corridorY = y;
                y += Math.max(2, row.lanes + 1) * LANE + ROOM.panel / 2;
                row.y = y;
                let x = left;
                let tallest = 0;
                row.stacks.forEach((column, index) => {
                    // Risers run up the gap on the column's left.
                    if (index > 0 || column.risers) x += column.risers * LANE;
                    column.x = x;
                    column.width = Math.max(...column.rooms.map((room) => room.size.width));
                    let top = y;
                    for (const room of column.rooms) {
                        const size = arrangeRoom(room, x, top);
                        Object.assign(room, { x, y: top, width: column.width, height: size.height });
                        top += size.height + STACK_GAP;
                    }
                    column.bottom = top - STACK_GAP;
                    tallest = Math.max(tallest, column.bottom - y);
                    x += column.width + ROOM.gap;
                });
                // The last area in each column takes up the slack.
                for (const column of row.stacks) {
                    const last = column.rooms[column.rooms.length - 1];
                    last.height += y + tallest - column.bottom;
                }
                row.height = tallest;
                widest = Math.max(widest, x - ROOM.gap - left);
                y += tallest + ROOM.rowGap;
            }
            site.width = Math.max(320, SITE.pad * 2 + site.spineWidth + widest);
            site.height = y - ROOM.rowGap + SITE.pad - site.y;
            cursorX += site.width + SITE.gap;
        }
        return {
            width: Math.max(1, cursorX - SITE.gap),
            height: Math.max(1, ...data.sites.map((site) => site.y + site.height)),
        };
    }

    // ------------------------------------------------------------------
    // Drawing.
    // ------------------------------------------------------------------

    /**
     * A device type's CSS colour tokens, falling back for unknown types.
     * @param {string} category Device category.
     * @returns {string} A class naming the type.
     */
    function typeClass(category) {
        const known = ["server", "switch", "router", "firewall", "ap", "ups", "modem"];
        const name = category === "access_point" ? "ap" : category;
        return known.includes(name) ? `is-type-${name}` : "is-type-other";
    }

    /**
     * A cable's medium class.
     * @param {object} link Topology link.
     * @returns {string} Class.
     */
    function mediumClass(link) {
        const medium = (link.medium || "unknown").replace(
            "fiber_multimode", "fiber-mm"
        ).replace("fiber_singlemode", "fiber-sm");
        return `is-medium-${medium}${link.confidence === "ambiguous" ? " is-tentative" : ""}`;
    }

    /**
     * Cut text to a number of characters, ending in an ellipsis.
     * @param {string} text Text.
     * @param {number} most Characters allowed.
     * @returns {string} The text, cut if needed.
     */
    function clip(text, most) {
        return text.length <= most ? text : `${text.slice(0, Math.max(1, most - 1))}…`;
    }

    /**
     * Draw one device: a bar in its rack, or a card with where it sits.
     * @param {object} device Placed device.
     * @returns {SVGElement} The device group.
     */
    function drawDevice(device) {
        const group = el("g", {
            class: `map-device ${typeClass(device.category)}${device.rack ? " is-racked" : " is-card"}`,
            "data-device": device.key, tabindex: 0, role: "link",
            "aria-label": `${device.name}${device.where ? `, ${device.where}` : ""}. Open in its area.`,
        });
        group.append(el(
            "rect",
            {
                class: "map-device-body", x: device.x, y: device.y, width: device.w, height: device.h,
                rx: device.rack ? 2 : 4
            }
        ));
        group.append(el(
            "rect", { class: "map-device-stripe", x: device.x, y: device.y, width: 4, height: device.h, rx: 1 }
        ));
        // Names and places are cut to the space they have (monospace: a
        // character is about 6.5 units at the name's size, 6 at the place's);
        // the tooltip has them in full.
        const where = device.where ? clip(device.where, Math.max(6, Math.floor(device.w * 0.38 / 6))) : "";
        const room = device.w - 18 - (where ? where.length * 6 + 10 : 0);
        group.append(el("text", {
            class: "map-device-name", x: device.x + 10, y: device.y + Math.min(device.h, CARD.height) / 2 + 4
        },
            [clip(device.name, Math.max(4, Math.floor(room / 6.5)))]));
        if (where) {
            group.append(el(
                "text",
                {
                    class: "map-device-where", x: device.x + device.w - 8, y: device.y + device.h / 2 + 4,
                    "text-anchor": "end"
                },
                [where]
            ));
        }
        return group;
    }

    /**
     * Draw a rack's front elevation with its equipment.
     * @param {object} rack Laid-out rack.
     * @returns {SVGElement} The rack group.
     */
    function drawRack(rack) {
        const group = el("g", { class: "map-rack" });
        group.append(el(
            "text", { class: "map-rack-name", x: rack.x, y: rack.y - 5 }, [`${rack.name} · ${rack.height_u}U`]
        ));
        group.append(el(
            "rect", { class: "map-rack-frame", x: rack.x, y: rack.y, width: rack.width, height: rack.height, rx: 3 }
        ));
        group.append(el(
            "rect",
            {
                class: "map-rack-bay", x: rack.x + RAIL, y: rack.y + RAIL, width: rack.width - 2 * RAIL,
                height: rack.height_u * U
            }
        ));
        for (let unit = 1; unit < rack.height_u; unit += 1) {
            const y = rack.y + RAIL + unit * U;
            group.append(el(
                "line", { class: "map-rack-unit", x1: rack.x + RAIL, x2: rack.x + rack.width - RAIL, y1: y, y2: y }
            ));
        }
        for (const device of rack.devices) group.append(drawDevice(device));
        return group;
    }

    /**
     * Rounded orthogonal path through points (the shared `wirePath`, so map
     * cables turn like Equipment's).
     * @param {Array<[number, number]>} points Corners, in order.
     * @param {number} [radius] Corner radius.
     * @returns {string} Path data.
     */
    function orthogonal(points, radius = 6) {
        return wirePath(points.map(([x, y]) => ({ x, y })), true, radius);
    }

    /**
     * Draw everything.
     * @param {object} data The model, laid out.
     * @returns {void}
     */
    function draw(data) {
        const sites = el("g", { class: "map-sites" });
        const cables = el("g", { class: "map-cables" });
        const inner = el("g", { class: "map-cables-inner" });
        const equipment = el("g", { class: "map-equipment" });
        const panels = el("g", { class: "map-panels" });
        const rooms = [...data.sites.flatMap((site) => site.rooms)];
        const roomById = new Map(rooms.map((room) => [room.id, room]));

        for (const site of data.sites) {
            const building = el("g", { class: "map-site" });
            building.append(el(
                "rect",
                { class: "map-site-outline", x: site.x, y: site.y, width: site.width, height: site.height, rx: 10 }
            ));
            building.append(el("text", { class: "map-site-name", x: site.x + SITE.pad, y: site.y + 22 }, [site.name]));
            for (const room of site.rooms) {
                const group = el("g", { class: "map-room", "data-room": room.id });
                group.append(el(
                    "rect",
                    { class: "map-room-floor", x: room.x, y: room.y, width: room.width, height: room.height, rx: 6 }
                ));
                const name = el("a", { href: `${areaBase}${room.id}`, class: "map-room-link" }, [
                    el(
                        "text",
                        {
                            class: "map-room-name", x: room.x + ROOM.pad, y: room.y + 20,
                            "data-fit": room.width - 2 * ROOM.pad
                        },
                        [room.name]
                    ),
                ]);
                group.append(name);
                if (!room.racks.length && !room.loose.length) {
                    group.append(el(
                        "text", { class: "map-room-empty", x: room.x + ROOM.pad, y: room.y + ROOM.head + 24 },
                        ["Nothing placed yet"]
                    ));
                }
                building.append(group);
                for (const rack of room.racks) equipment.append(drawRack(rack));
                for (const device of room.loose) equipment.append(drawDevice(device));
            }
            sites.append(building);
        }

        // Cables inside one area run in the gutter beside their equipment.
        if (state.roomCables) {
            for (const link of data.links) {
                const a = data.devices.get(link.from);
                const b = data.devices.get(link.to);
                if (a.room !== b.room) continue;
                const gutter = Math.max(a.anchor.gutter, b.anchor.gutter) + 6 + (link.id % 4) * 3;
                inner.append(el("path", {
                    class: `map-cable ${mediumClass(link)}`, "data-link": link.id,
                    d: orthogonal(
                        [
                            [a.anchor.x, a.anchor.y], [gutter, a.anchor.y], [gutter, b.anchor.y],
                            [b.anchor.x, b.anchor.y]
                        ],
                        5
                    ),
                }));
            }
        }

        // Cables between rooms: each takes a socket on both rooms' patch
        // panels, a lane in each end's row corridor, and, between rows or
        // buildings, a lane on the spine and the campus band.
        const exits = new Map(rooms.map((room) => [room.id, []]));
        for (const route of data.routes) {
            route.ends.forEach((end, index) => {
                const other = route.rooms[1 - index];
                exits.get(end.room).push({ route, end, toward: route.kind === "same" ? other.x : -Infinity });
            });
        }
        for (const [roomId, list] of exits) {
            if (!list.length) continue;
            const room = roomById.get(roomId);
            // Sockets ordered toward where each cable goes, so lanes cross less.
            list.sort((p, q) => (p.toward - q.toward) || (p.end.anchor.y - q.end.anchor.y));
            const panelWidth = Math.max(60, list.length * SOCKET + 24);
            const panelX = room.x + room.width - ROOM.pad - panelWidth;
            const panelY = room.y - ROOM.panel / 2;
            const panel = el("g", { class: "map-panel-group" });
            panel.append(el(
                "rect", { class: "map-panel", x: panelX, y: panelY, width: panelWidth, height: ROOM.panel, rx: 3 }
            ));
            panel.append(el(
                "text",
                { class: "map-panel-name", x: panelX - 6, y: panelY + ROOM.panel / 2 + 4, "text-anchor": "end" },
                ["Patch"]
            ));
            list.forEach((exit, index) => {
                const x = panelX + 12 + index * SOCKET + SOCKET / 2;
                exit.socket = { x, y: panelY + ROOM.panel / 2 };
                exit.route[exit.end === exit.route.ends[0] ? "socketA" : "socketB"] = exit.socket;
                panel.append(el("rect", {
                    class: `map-socket ${mediumClass(exit.route.link)}`, "data-link": exit.route.link.id,
                    x: x - 4, y: exit.socket.y - 4, width: 8, height: 8, rx: 1.5,
                }));
            });
            panels.append(panel);
        }
        const rowLanes = wireLaneCounter();
        const spineLanes = wireLaneCounter();
        let campusLane = 0;
        const laneIn = (room) => {
            const lane = rowLanes(`${room.site.id}:${room.row}`);
            return room.site.rows[room.row].corridorY + (lane + 1) * LANE;
        };
        const spineAt = (site) => {
            const lane = spineLanes(site.id);
            return site.x + SITE.pad + (lane + 1) * LANE;
        };
        // A stacked area's cables leave its panel sideways, above its top
        // edge, to a riser on its column's left, and climb to the corridor.
        const riseFrom = (room, socket) => {
            if (room.level === 0) return { points: [], x: socket.x };
            const column = room.column;
            column.riser += 1;
            const x = column.x - column.riser * LANE;
            const y = room.y - ROOM.panel / 2 - LANE;
            return { points: [[socket.x, y], [x, y]], x };
        };
        const inside = (end, socket, link) => {
            // Device to the gutter, up inside the room, over to the socket.
            const gutter = end.anchor.gutter + 8 + (link.id % 4) * 3;
            const roomTop = roomById.get(end.room).y + 8;
            return [
                [end.anchor.x, end.anchor.y], [gutter, end.anchor.y], [gutter, roomTop], [socket.x, roomTop],
                [socket.x, socket.y]
            ];
        };
        for (const route of data.routes) {
            const [a, b] = route.ends;
            const [ra, rb] = route.rooms;
            const sa = route.socketA;
            const sb = route.socketB;
            const ua = riseFrom(ra, sa);
            const ub = riseFrom(rb, sb);
            let middle;
            if (route.kind === "same") {
                const y = laneIn(ra);
                middle = [[ua.x, y], [ub.x, y]];
            } else if (route.kind === "row") {
                const ya = laneIn(ra);
                const yb = laneIn(rb);
                const x = spineAt(ra.site);
                middle = [[ua.x, ya], [x, ya], [x, yb], [ub.x, yb]];
            } else {
                const ya = laneIn(ra);
                const yb = laneIn(rb);
                const xa = spineAt(ra.site);
                const xb = spineAt(rb.site);
                campusLane += 1;
                const yc = campusLane * LANE;
                middle = [[ua.x, ya], [xa, ya], [xa, yc], [xb, yc], [xb, yb], [ub.x, yb]];
            }
            middle = [...ua.points, ...middle, ...ub.points.slice().reverse()];
            const path = [...inside(a, sa, route.link), ...middle, ...inside(b, sb, route.link).reverse()];
            cables.append(el(
                "path",
                {
                    class: `map-cable is-between ${mediumClass(route.link)}`, "data-link": route.link.id,
                    d: orthogonal(path, 6)
                }
            ));
        }

        world.replaceChildren(sites, inner, cables, panels, equipment);
        state.links = new Map(data.links.map((link) => [String(link.id), link]));
        state.devices = data.devices;
    }

    // ------------------------------------------------------------------
    // Pan and zoom.
    // ------------------------------------------------------------------

    let saveTimer = 0;

    /**
     * Apply the view transform, the zoom's level of detail (owner: names
     * give way to shapes when zoomed far out, and area names grow), and
     * the minimap's viewport; remember the view for this browser.
     */
    function apply() {
        world.setAttribute("transform", `translate(${view.x} ${view.y}) scale(${view.k})`);
        host.style.setProperty("--map-zoom", String(view.k));
        const low = view.k < 0.45;
        if (host.dataset.detail !== (low ? "low" : "high") || low) {
            host.dataset.detail = low ? "low" : "high";
            // Far out, area names grow to stay readable, but never past
            // their own room's width (a monospace letter is about 0.62 em).
            for (const name of world.querySelectorAll(".map-room-name")) {
                if (!low) {
                    name.style.removeProperty("font-size");
                    continue;
                }
                const fits = Number(name.dataset.fit) / (Math.max(1, name.textContent.length) * 0.62);
                name.style.setProperty("font-size", `${Math.min(11 / view.k, fits)}px`);
            }
        }
        const frame = mini.querySelector(".map-mini-view");
        if (frame && state.miniScale) {
            const s = state.miniScale;
            frame.setAttribute("x", String((-view.x / view.k) * s));
            frame.setAttribute("y", String((-view.y / view.k) * s));
            frame.setAttribute("width", String((host.clientWidth / view.k) * s));
            frame.setAttribute("height", String((host.clientHeight / view.k) * s));
        }
        window.clearTimeout(saveTimer);
        saveTimer = window.setTimeout(() => {
            try {
                window.localStorage.setItem(storageKey, JSON.stringify(view));
            } catch {
                // Storage can be unavailable; the view is a convenience.
            }
        }, 300);
    }

    /**
     * The view saved in this browser, if it is still sensible.
     * @returns {{x: number, y: number, k: number}|null} Saved view.
     */
    function savedView() {
        try {
            const saved = JSON.parse(window.localStorage.getItem(storageKey) || "null");
            if (saved && [
                saved.x, saved.y, saved.k
            ].every(Number.isFinite) && saved.k >= 0.1 && saved.k <= 4) return saved;
        } catch {
            // Fall through to fitting the map.
        }
        return null;
    }

    /**
     * Zoom so a world rectangle fills the canvas.
     * @param {{x: number, y: number, width: number, height: number}} box World box.
     * @param {number} [most] Largest zoom.
     */
    function zoomTo(box, most = 1.4) {
        const margin = 32;
        // Keep the bottom clear of the key and minimap.
        const below = Math.max(margin, (mini.getBoundingClientRect().height || 0) + 24);
        const k = Math.min(
            most, (host.clientWidth - 2 * margin) / box.width, (host.clientHeight - margin - below) / box.height
        );
        view.k = Math.max(0.1, k);
        view.x = (host.clientWidth - box.width * view.k) / 2 - box.x * view.k;
        view.y = margin + (host.clientHeight - margin - below - box.height * view.k) / 2 - box.y * view.k;
        apply();
    }

    /**
     * Draw the minimap: buildings and areas, scaled to fit its box.
     * @param {object} data Laid-out model.
     */
    function drawMini(data) {
        const width = 200;
        const scale = Math.min(width / state.bounds.width, 130 / state.bounds.height);
        state.miniScale = scale;
        mini.setAttribute("viewBox", `0 0 ${state.bounds.width * scale} ${state.bounds.height * scale}`);
        mini.style.setProperty("width", `${Math.ceil(state.bounds.width * scale) + 2}px`);
        mini.style.setProperty("height", `${Math.ceil(state.bounds.height * scale) + 2}px`);
        const shapes = [];
        for (const site of data.sites) {
            shapes.push(el(
                "rect",
                {
                    class: "map-mini-site", x: site.x * scale, y: site.y * scale, width: site.width * scale,
                    height: site.height * scale, rx: 2
                }
            ));
            for (const room of site.rooms) {
                shapes.push(el(
                    "rect",
                    {
                        class: "map-mini-room", x: room.x * scale, y: room.y * scale, width: room.width * scale,
                        height: room.height * scale, rx: 1
                    }
                ));
            }
        }
        shapes.push(el("rect", { class: "map-mini-view" }));
        mini.replaceChildren(...shapes);
    }

    mini.addEventListener("pointerdown", (event) => {
        // Centre the view on the clicked point; the canvas does not pan.
        event.stopPropagation();
        const box = mini.getBoundingClientRect();
        const move = (moveEvent) => {
            const wx = (moveEvent.clientX - box.left) / state.miniScale;
            const wy = (moveEvent.clientY - box.top) / state.miniScale;
            view.x = host.clientWidth / 2 - wx * view.k;
            view.y = host.clientHeight / 2 - wy * view.k;
            state.fitted = false;
            apply();
        };
        move(event);
        mini.setPointerCapture(event.pointerId);
        mini.addEventListener("pointermove", move);
        mini.addEventListener("pointerup", () => mini.removeEventListener("pointermove", move), { once: true });
    });
    document.getElementById("locations-map-key")?.addEventListener("pointerdown", (event) => event.stopPropagation());
    host.addEventListener("dblclick", (event) => {
        const room = event.target.closest?.("[data-room]");
        if (!room || !data) return;
        const found = data.rooms.get(Number(room.dataset.room));
        if (found) {
            zoomTo({ x: found.x - 20, y: found.y - 30, width: found.width + 40, height: found.height + 50 }, 2.5);
            state.fitted = false;
        }
    });

    /**
     * Zoom by a factor about a point in the canvas.
     * @param {number} factor Multiplier.
     * @param {number} [cx] Canvas x (default: centre).
     * @param {number} [cy] Canvas y.
     */
    function zoom(factor, cx = host.clientWidth / 2, cy = host.clientHeight / 2) {
        state.fitted = false;
        const k = Math.min(4, Math.max(0.1, view.k * factor));
        view.x = cx - ((cx - view.x) * k) / view.k;
        view.y = cy - ((cy - view.y) * k) / view.k;
        view.k = k;
        apply();
    }

    /** Fit the whole world in the canvas. */
    function fit() {
        if (state.bounds) zoomTo({ x: 0, y: 0, width: state.bounds.width, height: state.bounds.height });
        state.fitted = true;
    }

    /** Size the canvas so the page, footer included, fits the window. */
    function fitCanvas() {
        host.style.removeProperty("height");
        const natural = host.getBoundingClientRect().height;
        const overflow = document.documentElement.scrollHeight - window.innerHeight;
        host.style.setProperty("height", `${Math.max(360, Math.floor(natural - overflow))}px`);
    }

    let drag = null;
    // Two fingers pinch to zoom about their midpoint (touch).
    const touches = new Map();
    let pinch = null;
    host.addEventListener("pointerdown", (event) => {
        if (event.pointerType === "touch") {
            touches.set(event.pointerId, { x: event.clientX, y: event.clientY });
            if (touches.size === 2) {
                const [a, b] = [...touches.values()];
                pinch = { distance: Math.hypot(a.x - b.x, a.y - b.y) || 1 };
                drag = null;
                return;
            }
        }
        if (event.button !== 0) return;
        drag = { x: event.clientX, y: event.clientY, vx: view.x, vy: view.y, moved: false, id: event.pointerId };
    });
    host.addEventListener("pointermove", (event) => {
        if (pinch && touches.has(event.pointerId)) {
            touches.set(event.pointerId, { x: event.clientX, y: event.clientY });
            const [a, b] = [...touches.values()];
            const distance = Math.hypot(a.x - b.x, a.y - b.y) || 1;
            const box = host.getBoundingClientRect();
            zoom(distance / pinch.distance, (a.x + b.x) / 2 - box.left, (a.y + b.y) / 2 - box.top);
            pinch.distance = distance;
            return;
        }
        if (drag && event.pointerId === drag.id) {
            const dx = event.clientX - drag.x;
            const dy = event.clientY - drag.y;
            if (!drag.moved && Math.hypot(dx, dy) > 4) {
                drag.moved = true;
                host.setPointerCapture(event.pointerId);
                host.classList.add("is-panning");
            }
            if (drag.moved) {
                state.fitted = false;
                view.x = drag.vx + dx;
                view.y = drag.vy + dy;
                apply();
            }
            return;
        }
        hover(event);
    });
    const endDrag = (event) => {
        if (!drag || event.pointerId !== drag.id) return;
        const moved = drag.moved;
        drag = null;
        host.classList.remove("is-panning");
        if (!moved) select(event.target);
    };
    const endTouch = (event) => {
        touches.delete(event.pointerId);
        if (touches.size < 2) pinch = null;
    };
    host.addEventListener("pointerup", (event) => {
        endTouch(event);
        endDrag(event);
    });
    host.addEventListener("pointercancel", (event) => {
        endTouch(event);
        drag = null;
        host.classList.remove("is-panning");
    });
    host.addEventListener("wheel", (event) => {
        event.preventDefault();
        const box = host.getBoundingClientRect();
        zoom(
            Math.exp(-event.deltaY * (event.ctrlKey ? 0.01 : 0.0015)), event.clientX - box.left, event.clientY - box.top
        );
    }, { passive: false });
    host.addEventListener("keydown", (event) => {
        const step = 60;
        const moves = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
        if (moves[event.key]) {
            state.fitted = false;
            view.x += moves[event.key][0];
            view.y += moves[event.key][1];
            apply();
        } else if (event.key === "+" || event.key === "=") zoom(1.25);
        else if (event.key === "-") zoom(0.8);
        else if (event.key === "0") fit();
        else if (event.key === "Enter" && event.target.closest?.("[data-device]")) select(event.target);
        else if (event.key === "Escape" && !panel.hidden) closePanel();
        else return;
        event.preventDefault();
    });

    const panel = document.getElementById("locations-map-panel");
    const panelBody = document.getElementById("locations-map-panel-body");

    /**
     * Select the device under a click or key press, or clear the selection
     * on the empty map.
     * @param {EventTarget} target Clicked or focused element.
     */
    function select(target) {
        const node = target.closest?.("[data-device]");
        if (!node) {
            if (!target.closest?.(".locations-map-panel")) closePanel();
            return;
        }
        const device = state.devices.get(node.dataset.device);
        if (device) openPanel(device);
    }

    /**
     * Light a device and its cables, and show its panel: where it is, its
     * cables (each other end a jump), and Open in its area (owner: the map
     * keeps you on the map; the area view is one click away).
     * @param {object} device Placed device.
     */
    function openPanel(device) {
        world.querySelectorAll(".is-selected, .is-chosen").forEach((item) => item.classList.remove(
            "is-selected", "is-chosen"
        ));
        world.querySelector(`[data-device="${CSS.escape(device.key)}"]`)?.classList.add("is-selected");
        const room = data.rooms.get(device.room);
        const links = [...state.links.values()].filter((link) => link.from === device.key || link.to === device.key);
        for (const link of links) world.querySelectorAll(`[data-link="${link.id}"]`).forEach((item) => item.classList.add("is-chosen"));
        document.getElementById("locations-map-panel-title").textContent = device.name;
        const facts = document.createElement("dl");
        const fact = (term, value) => {
            if (!value) return;
            const dt = document.createElement("dt");
            dt.textContent = term;
            const dd = document.createElement("dd");
            dd.textContent = value;
            facts.append(dt, dd);
        };
        const rack = device.rack ? room.racks.find((item) => item.id === device.rack) : null;
        fact("Type", (device.category || "other").replace("_", " "));
        fact("Area", room ? `${room.site.name} / ${room.name}` : "");
        fact("Place", rack ? `${rack.name}, U${device.placement.rack_u}${device.heightU > 1 ? `–${device.placement.rack_u + device.heightU - 1}` : ""}` : device.where);
        const list = document.createElement("ul");
        list.className = "locations-map-panel-cables";
        for (const link of links) {
            const mine = link.from === device.key ? link.from_port : link.to_port;
            const otherKey = link.from === device.key ? link.to : link.from;
            const otherPort = link.from === device.key ? link.to_port : link.from_port;
            const other = state.devices.get(otherKey);
            const item = document.createElement("li");
            const swatch = document.createElement("span");
            swatch.className = `map-key-line ${mediumClass(link)}`;
            const jump = document.createElement("button");
            jump.type = "button";
            jump.className = "locations-map-panel-jump";
            const away = other && other.room !== device.room ? ` · ${data.rooms.get(other.room)?.name || ""}` : "";
            jump.textContent = `${mine || "port"} → ${other?.name || otherKey} ${otherPort || ""}${away}`;
            jump.addEventListener("click", () => {
                if (!other) return;
                flyTo(other);
                openPanel(other);
            });
            item.append(swatch, jump);
            list.append(item);
        }
        const open = document.createElement("a");
        open.className = "button button-secondary locations-map-panel-open";
        open.href = `${areaBase}${device.room}/equipment/${device.id}`;
        open.textContent = "Open in its area";
        const heading = document.createElement("h3");
        heading.textContent = links.length ? `Cables (${links.length})` : "No observed cables";
        panelBody.replaceChildren(facts, heading, ...(links.length ? [list] : []), open);
        panel.hidden = false;
    }

    /** Hide the device panel and clear the selection. */
    function closePanel() {
        panel.hidden = true;
        world.querySelectorAll(".is-selected, .is-chosen").forEach((item) => item.classList.remove(
            "is-selected", "is-chosen"
        ));
    }

    /**
     * Bring a device into view, zoomed in enough to read.
     * @param {object} device Placed device.
     */
    function flyTo(device) {
        const box = { x: device.x - 160, y: device.y - 120, width: device.w + 320, height: device.h + 240 };
        zoomTo(box, Math.max(1.2, view.k));
        // On a wide canvas, keep the device clear of the panel on the right.
        if (host.clientWidth > 900) {
            view.x -= 170;
            apply();
        }
        state.fitted = false;
    }

    document.getElementById("locations-map-panel-close")?.addEventListener("click", closePanel);
    panel.addEventListener("pointerdown", (event) => event.stopPropagation());
    panel.addEventListener("wheel", (event) => event.stopPropagation());

    // Find: matches stay lit and the rest dim; Enter flies to the next.
    const find = document.getElementById("map-find");
    const findCount = document.getElementById("map-find-count");
    let matches = [];
    let matchIndex = -1;
    find?.addEventListener("input", () => {
        const query = find.value.trim().toLowerCase();
        matches = query
            && data
            ? [...data.devices.values()].filter((device) => device.name.toLowerCase().includes(query))
            : [];
        matchIndex = -1;
        host.classList.toggle("is-finding", Boolean(query));
        world.querySelectorAll(".is-match").forEach((item) => item.classList.remove("is-match"));
        for (const device of matches) world.querySelector(`[data-device="${CSS.escape(device.key)}"]`)?.classList.add("is-match");
        findCount.textContent = query ? `${matches.length} found` : "";
    });
    find?.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            find.value = "";
            find.dispatchEvent(new Event("input"));
            return;
        }
        if (event.key !== "Enter" || !matches.length) return;
        event.preventDefault();
        matchIndex = (matchIndex + 1) % matches.length;
        flyTo(matches[matchIndex]);
        openPanel(matches[matchIndex]);
        findCount.textContent = `${matchIndex + 1} of ${matches.length}`;
    });

    /**
     * Show a tooltip for the device or cable under the pointer, and light
     * a hovered cable or every cable of a hovered device.
     * @param {PointerEvent} event Pointer move.
     */
    function hover(event) {
        const deviceNode = event.target.closest?.("[data-device]");
        const cableNode = event.target.closest?.("[data-link]");
        world.querySelectorAll(".is-lit").forEach((node) => node.classList.remove("is-lit"));
        let lines = null;
        if (deviceNode) {
            const device = state.devices.get(deviceNode.dataset.device);
            const links = [...state.links.values()].filter((link) => link.from === device.key
                || link.to === device.key);
            for (const link of links) world.querySelector(`[data-link="${link.id}"]`)?.classList.add("is-lit");
            lines = [device.name, device.where || (device.rack ? `U${device.placement.rack_u}` : ""), `${links.length} cable${links.length === 1 ? "" : "s"}`].filter(Boolean);
        } else if (cableNode) {
            const link = state.links.get(cableNode.dataset.link);
            cableNode.classList.add("is-lit");
            const name = (key) => state.devices.get(key)?.name || key;
            lines = [`${name(link.from)} ${link.from_port || ""} ↔ ${name(link.to)} ${link.to_port || ""}`,
                [link.medium, link.confidence, link.evidence].filter(Boolean).join(" · ")];
        }
        if (!lines) {
            tip.hidden = true;
            return;
        }
        tip.replaceChildren(...lines.map((line, index) => {
            const item = document.createElement(index ? "span" : "strong");
            item.textContent = line;
            return item;
        }));
        const box = host.getBoundingClientRect();
        tip.style.setProperty("left", `${Math.min(event.clientX - box.left + 14, box.width - 240)}px`);
        tip.style.setProperty("top", `${event.clientY - box.top + 14}px`);
        tip.hidden = false;
    }
    host.addEventListener("pointerleave", () => { tip.hidden = true; });

    // ------------------------------------------------------------------
    // Load.
    // ------------------------------------------------------------------

    let data = null;

    /** Lay out and draw the current data, then fit it. */
    function render() {
        state.bounds = layout(data);
        draw(data);
        drawMini(data);
        const count = data.sites.reduce((total, site) => total + site.rooms.length, 0);
        empty.hidden = count > 0;
        if (!count) empty.textContent = data.sites.length
            ? "No areas yet. Add areas from the Areas view."
            : "No locations yet. Add one from the Areas view.";
    }

    /** Fetch both documents and draw the map. */
    async function load() {
        try {
            const [locations, topology] = await Promise.all([api(LABRAT_API.locations), api(LABRAT_API.topology)]);
            data = model(locations, topology);
            fitCanvas();

            render();
            const saved = savedView();
            if (saved) {
                Object.assign(view, saved);
                apply();
            } else {
                fit();
            }
        } catch (error) {
            notice.textContent = error.message;
            notice.hidden = false;
        }
    }

    document.getElementById("map-zoom-in")?.addEventListener("click", () => zoom(1.25));
    document.getElementById("map-zoom-out")?.addEventListener("click", () => zoom(0.8));
    document.getElementById("map-fit")?.addEventListener("click", fit);
    document.getElementById("map-room-cables")?.addEventListener("click", (event) => {
        state.roomCables = !state.roomCables;
        event.currentTarget.setAttribute("aria-pressed", String(state.roomCables));
        if (data) render();
    });
    // A fitted map stays fitted when the window or frame changes size.
    const resized = () => {
        fitCanvas();
        if (state.fitted) fit();
        else apply();
    };
    window.addEventListener("resize", resized);
    if ("ResizeObserver" in window) {
        let last = "";
        new ResizeObserver(() => {
            const size = `${host.clientWidth}x${host.clientHeight}`;
            if (size !== last && last) resized();
            last = size;
        }).observe(host);
    }
    load();
})();
