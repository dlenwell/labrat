/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * @fileoverview Room-scoped Equipment faceplates and wiring for rooms without racks.
 * Layout is presentation only. This module never writes inventory or connections.
 */

/**
 * Measure observed physical hops toward WAN devices or the known router tier.
 *
 * Only infrastructure carries paths; servers cannot become transit switches.
 * This is diagram ordering, not a routing-table or uplink claim. Ambiguous
 * cables do not establish direction, and missing evidence keeps neutral order.
 *
 * @param {object} app - All cached topology nodes and physical links.
 * @returns {Map<string, number>} Hop distance from the observed edge tier.
 */
function roomWiringEdgeDistances(app) {
    const byId = new Map(app.nodes.map(node => [node.id, node]));
    const edge = app.nodes.filter(node => node.wan_upstream || node.category === "modem");
    const roots = edge.length ? edge : app.nodes.filter(node => ["router", "firewall"].includes(node.category));
    const neighbors = new Map(app.nodes.map(node => [node.id, new Set()]));
    for (const link of app.links || []) {
        if (link.confidence === "ambiguous" || !byId.has(link.from) || !byId.has(link.to)) continue;
        neighbors.get(link.from).add(link.to); neighbors.get(link.to).add(link.from);
    }
    const distances = new Map(roots.map(node => [node.id, 0]));
    const pending = roots.map(node => node.id);
    for (let i = 0; i < pending.length; i++) {
        const id = pending[i];
        if (!LABRAT_INFRASTRUCTURE.has(byId.get(id).category)) continue;
        for (const other of neighbors.get(id)) {
            if (distances.has(other)) continue;
            distances.set(other, distances.get(id) + 1); pending.push(other);
        }
    }
    return distances;
}

/**
 * Every physical cable with exactly one end placed in this room, as a patch
 * panel exit, edge-facing exits first (see roomWiringEdgeDistances).
 *
 * @param {object} app - All cached nodes and links plus the current room ID.
 * @returns {object[]} Exits: `{link, local, remote, localPort, remotePort}`.
 */
function roomPatchExits(app) {
    const ids = new Set(app.nodes.filter(n => n.placement?.container_room_id === app.roomId).map(n => n.id));
    const byId = new Map(app.nodes.map(node => [node.id, node]));
    const crossing = (app.links || []).filter(link => ids.has(link.from) !== ids.has(link.to))
        .filter(link => byId.has(link.from) && byId.has(link.to));
    const edgeDistances = roomWiringEdgeDistances(app);
    const upstream = exit => (edgeDistances.get(exit.remote.id) ?? Infinity)
        < (edgeDistances.get(exit.local.id) ?? Infinity);
    return crossing.map(link => {
        const fromInside = ids.has(link.from);
        return { link, local: byId.get(fromInside ? link.from : link.to),
            remote: byId.get(fromInside ? link.to : link.from),
            localPort: fromInside ? link.from_port : link.to_port,
            remotePort: fromInside ? link.to_port : link.from_port };
    }).sort((a, b) => Number(upstream(b)) - Number(upstream(a))
        || a.remote.name.localeCompare(b.remote.name)
        || a.remote.id.localeCompare(b.remote.id) || a.local.id.localeCompare(b.local.id)
        || a.localPort.localeCompare(b.localPort) || a.remotePort.localeCompare(b.remotePort));
}

/** Most sockets in one patch panel row (Equipment and the Diagram). */
const ROOM_PATCH_COLUMNS = 8;

/**
 * A patch panel's grid, sized to its cables with no empty positions
 * (owner): as few rows as ROOM_PATCH_COLUMNS sockets a row allows, the
 * sockets spread evenly across them.
 *
 * @param {number} count - Sockets (live cables leaving the room).
 * @returns {{columns: number, rows: number, slots: number}} Grid.
 */
function roomPatchShape(count) {
    const rows = Math.max(1, Math.ceil(count / ROOM_PATCH_COLUMNS));
    return { columns: Math.max(1, Math.ceil(count / rows)), rows, slots: count };
}

/** Stage ID of the Diagram's patch panel card (never a device ID). */
const ROOM_PATCH_ID = "room-patch";

/**
 * The room's conceptual patch panel: one unlabeled, focusable socket per
 * cable leaving the room, with an interactive destination popup. Shared by
 * the Diagram (a movable card on its canvas) and Equipment (on a rack top
 * or in its own side panel).
 * It is a drawing boundary only, never equipment or an observation.
 */
class RoomPatchPanel {
    /**
     * @param {object} options - Panel wiring.
     * @param {object} options.app - Equipment controller (locations, port state, select, redraws).
     * @param {string} options.idPrefix - Unique prefix for popup element IDs.
     * @param {function(): (HTMLElement|null)} options.after - Where Tab goes after the last popup.
     * @param {string} [options.placement] - "above" opens destination popups above
     *     the whole panel, clear of what lies below it (Equipment's rack top);
     *     the default opens them below the socket.
     */
    constructor({ app, idPrefix, after, placement = "below" }) {
        this.app = app;
        this.idPrefix = idPrefix;
        this.after = after;
        this.placement = placement;
        this.sockets = new Map();
        this.face = null;
        this.open = null;
    }

    /**
     * Build the panel face for these exits; unused positions fill the grid.
     *
     * @param {object[]} exits - From `roomPatchExits`.
     * @param {{columns: number, slots: number, width?: number, height?: number}} shape - Grid size.
     * @returns {HTMLElement|null} The panel face, or null when no cable leaves.
     */
    render(exits, shape) {
        this.close();
        this.sockets = new Map();
        this.face = null;
        if (!exits.length) return null;
        const face = document.createElement("div");
        face.className = "room-patch-panel";
        face.setAttribute("role", "group");
        face.setAttribute("aria-label", "Connections outside this area");
        if (shape.width) Object.assign(face.style, { width: `${shape.width}px`, height: `${shape.height}px` });
        face.style.setProperty("--patch-columns", shape.columns);
        exits.forEach((exit, index) => face.append(this.socket(exit, index)));
        for (let index = exits.length; index < shape.slots; index += 1) {
            const unused = document.createElement("span"); unused.className = "room-patch-unused";
            unused.setAttribute("aria-hidden", "true"); face.append(unused);
        }
        this.face = face;
        return face;
    }

    /**
     * One socket, its lamp, and its (detached) destination popup.
     *
     * @param {object} exit - The cable leaving the room: its local and remote
     *     ends, link, and ports.
     * @param {number} index - The socket's position on the panel.
     * @returns {HTMLElement} The socket.
     */
    socket(exit, index) {
        const { app } = this;
        const { local, remote, link, localPort, remotePort } = exit;
        const roomId = remote.placement?.container_room_id;
        const room = app.locationsData?.rooms?.find(room => room.id === roomId);
        const roomName = room?.name || (roomId ? "Other area" : "Unplaced");
        const socket = document.createElement("button");
        socket.type = "button"; socket.className = "room-patch-socket";
        socket.setAttribute("aria-label", `Connection to ${remote.name}, ${remotePort}, ${roomName}`);
        socket.setAttribute("aria-expanded", "false");
        const id = `${this.idPrefix}-${index}`;
        socket.setAttribute("aria-controls", id);
        const port = local.ports?.find(port => port.name === localPort);
        const lamp = document.createElement("span");
        lamp.className = "rack-port-light room-patch-lamp";
        lamp.dataset.medium = port?.medium || "unknown";
        lamp.dataset.linked = String(Boolean(port && app.portIsUp(port, local.id)));
        lamp.dataset.state = port ? app.portAlarmState(port, local.id) : "off";
        lamp.setAttribute("aria-hidden", "true"); socket.append(lamp);
        const details = document.createElement("div");
        details.className = "room-patch-details"; details.id = id;
        details.setAttribute("role", "group"); details.setAttribute("aria-label", "Connection destination");
        // Condensed (owner): the far device, then its port and this room's
        // end on one line, and a pill that opens the far device in its room.
        // No reporting status, evidence, or source.
        const name = document.createElement("strong"); name.textContent = remote.name;
        const near = document.createElement("p");
        near.textContent = `${remotePort} ← ${local.name} · ${localPort}`;
        const follow = document.createElement(roomId ? "a" : "button");
        follow.className = "room-patch-follow";
        const label = document.createElement("span");
        if (roomId) {
            const segment = typeof labratDeviceSegment === "function" ? labratDeviceSegment(remote.id) : null;
            follow.href = segment
                ? `/areas/${roomId}/${segment}`
                : `/areas/${roomId}?inspect=${encodeURIComponent(remote.id)}`;
            label.textContent = roomName;
            follow.setAttribute("aria-label", `Open ${remote.name} in ${roomName}`);
        } else {
            follow.type = "button"; label.textContent = "Unplaced";
            follow.setAttribute("aria-label", `Open ${remote.name}'s details`);
            follow.addEventListener("click", () => { this.close(); app.select(remote.id); });
        }
        const arrow = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        arrow.setAttribute("viewBox", "0 0 24 24"); arrow.setAttribute("aria-hidden", "true");
        const shaft = document.createElementNS(arrow.namespaceURI, "path");
        shaft.setAttribute("d", "M5 12h14M13 6l6 6-6 6"); arrow.append(shaft);
        follow.append(label, arrow);
        details.append(name, near, follow);
        const record = { socket, lamp, details, follow, hovered: false, popupHovered: false, exit };
        this.sockets.set(link, record);
        const show = () => { if (!this.suppressFocus) this.show(record); };
        socket.addEventListener("pointerenter", event => {
            if (event.pointerType === "touch") return;
            record.hovered = true; show();
        });
        socket.addEventListener("pointerleave", () => { record.hovered = false; this.scheduleClose(record); });
        socket.addEventListener("focus", show);
        socket.addEventListener("blur", () => this.scheduleClose(record));
        socket.addEventListener("click", show);
        socket.addEventListener("keydown", event => {
            if ((event.key !== "Tab" && event.key !== "ArrowDown") || event.shiftKey) return;
            event.preventDefault(); show(); follow.focus();
        });
        details.addEventListener("keydown", event => {
            if (event.key !== "Tab") return;
            event.preventDefault();
            if (event.shiftKey) { socket.focus(); return; }
            const entries = [...this.sockets.values()];
            const next = entries[entries.indexOf(record) + 1]?.socket || this.after?.();
            this.close(); next?.focus({ preventScroll: true });
        });
        details.addEventListener("pointerenter", () => { record.popupHovered = true; clearTimeout(this.timer); });
        details.addEventListener("pointerleave", () => { record.popupHovered = false; this.scheduleClose(record); });
        details.addEventListener("focusin", () => clearTimeout(this.timer));
        details.addEventListener("focusout", () => this.scheduleClose(record));
        return socket;
    }

    /**
     * Show interactive destination details outside any scrolling or clipping parent.
     *
     * @param {object} record - The socket's record: its element, popup, and
     *     hover state.
     */
    show(record) {
        clearTimeout(this.timer);
        if (this.open === record) return;
        this.close();
        if (!record.socket.isConnected) return;
        this.open = record;
        this.app.queueCables();
        record.socket.setAttribute("aria-expanded", "true");
        document.body.append(record.details);
        const socket = record.socket.getBoundingClientRect();
        const popup = record.details.getBoundingClientRect();
        const viewportWidth = document.documentElement?.clientWidth || window.innerWidth;
        record.details.style.left = `${Math.max(8, Math.min(socket.left, viewportWidth - popup.width - 8))}px`;
        const below = socket.bottom + 8;
        // Above the panel's top edge (so the cables below stay in view), or
        // below the socket; either falls back to the other side when it does
        // not fit in the window.
        const above = (this.face?.getBoundingClientRect().top ?? socket.top) - popup.height - 8;
        const fitsBelow = below + popup.height <= window.innerHeight;
        const top = this.placement === "above"
            ? (above >= 8 ? above : below)
            : (fitsBelow ? below : socket.top - popup.height - 8);
        record.details.style.top = `${Math.max(8, top)}px`;
        this.outside = event => {
            if (!record.details.contains(event.target) && !record.socket.contains(event.target)) this.close();
        };
        this.escape = event => {
            if (event.key !== "Escape") return;
            event.preventDefault(); event.stopPropagation();
            this.close();
            this.suppressFocus = true;
            record.socket.focus({ preventScroll: true });
            this.suppressFocus = false;
        };
        document.addEventListener("pointerdown", this.outside, true);
        document.addEventListener("keydown", this.escape, true);
    }

    /**
     * Leave enough time to move from a socket to the popup's room link.
     *
     * @param {object} record - The socket's record.
     */
    scheduleClose(record) {
        clearTimeout(this.timer);
        this.timer = setTimeout(() => {
            if (!record.hovered && !record.popupHovered && !record.socket.contains(document.activeElement)
                    && !record.details.contains(document.activeElement)) this.close();
        }, 180);
    }

    /** Remove popup listeners and release held source refreshes when inspection ends. */
    close() {
        clearTimeout(this.timer);
        const record = this.open;
        if (!record) return;
        this.open = null;
        this.app.queueCables();
        record.socket.setAttribute("aria-expanded", "false"); record.details.remove();
        document.removeEventListener("pointerdown", this.outside, true);
        document.removeEventListener("keydown", this.escape, true);
        if (this.app?.updateHeld) setTimeout(() => {
            if (!this.app.isEditing()) this.app.applyUpdate();
        }, 0);
    }
}

/** Diagram geometry, in stage pixels. */
const ROOM_WIRING = Object.freeze({
    margin: 32,       // around the drawing
    cardWidth: 220,   // a device faceplate
    cardHeight: 64,   // a faceplate with few ports
    portStep: 14,     // between ports down a card's side
    portPad: 12,      // above the first and below the last port
    lane: 5,          // between parallel wires in a channel or the top run
    rowGap: 20,       // between cards in a column
    channelPad: 22,   // either side of a column channel's lanes
    topGap: 18,       // between the top runs and the first cards
    socketStep: 22,   // patch panel sockets (room-wiring.css .room-patch-panel)
    patchPad: 16,     // patch card and face padding above the first socket
});

/**
 * The Diagram's columns, a left-to-right tree (owner): WAN modems (and the
 * patch panel, added by RoomWiring) in column 0, routers and firewalls in
 * column 1, then switches and access points one column per hop from them,
 * and every other device one column right of the device it hangs from (its
 * deepest placed neighbor). Islands without a router start at column 1;
 * devices with no placed neighbor go in a last column.
 *
 * A UPS on a USB or "reports through" line (`power`) sits in its server's
 * column, right below it (owner), whatever else it is linked to.
 *
 * @param {object[]} records - Room devices.
 * @param {object[]} links - Network and Wi-Fi relations `{from, to, confidence?}`.
 * @param {object[]} [power] - UPS lines `{from: server, to: ups}`.
 * @returns {{column: Map<string, number>, parent: Map<string, string|null>,
 *     follows: Map<string, string>}} Column per device, the device it hangs
 *     from, and each UPS's server.
 */
function roomWiringColumns(records, links, power = []) {
    const byId = new Map(records.map(record => [record.id, record]));
    const neighbors = new Map(records.map(record => [record.id, new Map()]));
    for (const link of links) {
        if (!byId.has(link.from) || !byId.has(link.to) || link.from === link.to) continue;
        const weight = link.confidence === "ambiguous" ? 0.5 : 1;
        for (const [a, b] of [[link.from, link.to], [link.to, link.from]]) {
            neighbors.get(a).set(b, (neighbors.get(a).get(b) || 0) + weight);
        }
    }
    const isEdge = record => record.category === "modem" || Boolean(record.wan_upstream);
    const isRouter = record => ["router", "firewall"].includes(record.category);
    const isInfra = record => LABRAT_INFRASTRUCTURE.has(record.category) && !isEdge(record);
    const sorted = [...records].sort(labratCompareRecords);
    const rank = new Map(sorted.map((record, index) => [record.id, index]));
    const column = new Map(), parent = new Map();
    for (const record of sorted) {
        if (isEdge(record)) { column.set(record.id, 0); parent.set(record.id, null); }
    }
    // Infrastructure grows level by level from its seeds, never through a
    // host, each device under its strongest neighbor on the level before.
    const grow = seeds => {
        for (const id of seeds) { column.set(id, 1); parent.set(id, null); }
        let level = seeds;
        while (level.length) {
            const next = new Map();
            for (const id of level) {
                for (const [other, score] of neighbors.get(id)) {
                    if (column.has(other) || !isInfra(byId.get(other))) continue;
                    const best = next.get(other);
                    if (!best || score > best.score) next.set(other, { parent: id, score });
                }
            }
            level = [...next.keys()].sort((a, b) => rank.get(a) - rank.get(b));
            for (const id of level) {
                column.set(id, column.get(next.get(id).parent) + 1);
                parent.set(id, next.get(id).parent);
            }
        }
    };
    const routers = sorted.filter(record => isRouter(record) && !column.has(record.id)).map(record => record.id);
    if (routers.length) grow(routers);
    for (;;) {
        const left = sorted.filter(record => isInfra(record) && !column.has(record.id));
        if (!left.length) break;
        const touchesEdge = record => [...neighbors.get(record.id).keys()].some(other => column.get(other) === 0);
        const degree = record => [...neighbors.get(record.id).values()].reduce((sum, value) => sum + value, 0);
        const seed = left.find(touchesEdge) || [...left].sort((a, b) => degree(b) - degree(a))[0];
        grow([seed.id]);
    }
    // Everything else hangs one column right of its deepest placed neighbor
    // (a server with two NICs sits right of both switches), repeated so a
    // UPS can hang from the server that reads it.
    for (let changed = true; changed;) {
        changed = false;
        for (const record of sorted) {
            if (column.has(record.id)) continue;
            const placed = [...neighbors.get(record.id)].filter(([other]) => column.has(other));
            if (!placed.length) continue;
            placed.sort(([a, sa], [b, sb]) => column.get(b) - column.get(a) || sb - sa || rank.get(a) - rank.get(b));
            column.set(record.id, column.get(placed[0][0]) + 1);
            parent.set(record.id, placed[0][0]);
            changed = true;
        }
    }
    const follows = new Map();
    for (const line of power) {
        if (byId.has(line.from) && byId.has(line.to) && !follows.has(line.to)) follows.set(line.to, line.from);
    }
    const isFollower = id => follows.has(id) && !follows.has(follows.get(id));
    const last = Math.max(1, ...[...column.entries()].filter(([id]) => !isFollower(id)).map(([, value]) => value + 1));
    for (const record of sorted) {
        if (!column.has(record.id)
            && !isFollower(record.id)) { column.set(record.id, last); parent.set(record.id, null); }
    }
    for (const [ups, server] of follows) {
        if (!isFollower(ups)) { follows.delete(ups); continue; }
        column.set(ups, column.get(server)); parent.set(ups, server);
    }
    return { column, parent, follows };
}

/**
 * Order lanes in a column channel so wires cross as little as they can:
 * each wire is inserted where it adds the fewest crossings. A wire runs
 * from the channel's left (its `left` ends) and to its right (`right`), and
 * along the channel between its highest and lowest end.
 *
 * @param {Array<{left: number[], right: number[]}>} items - Channel wires.
 * @returns {Array<object>} The same wires, leftmost lane first.
 */
function roomWiringLaneOrder(items) {
    const span = item => [Math.min(...item.left, ...item.right), Math.max(...item.left, ...item.right)];
    const inside = (y, [top, bottom]) => y > top + 0.5 && y < bottom - 0.5;
    // Crossings when `a` is left of `b`: b's left ends cross a's run, and
    // a's right ends cross b's run.
    const cost = (a, b) => b.left.filter(y => inside(y, span(a))).length + a.right.filter(y => inside(y, span(b))).length;
    const order = [];
    const ranked = [...items].sort((a, b) => (span(b)[1] - span(b)[0]) - (span(a)[1] - span(a)[0]));
    for (const item of ranked) {
        let best = 0, bestCost = Infinity;
        for (let at = 0; at <= order.length; at += 1) {
            let total = 0;
            for (let i = 0; i < order.length; i += 1) total += i < at ? cost(order[i], item) : cost(item, order[i]);
            if (total < bestCost) { bestCost = total; best = at; }
        }
        order.splice(best, 0, item);
    }
    return order;
}

/**
 * A relaxed, swoopy path along a lane route (owner: no 90° turns): every
 * corner becomes the control point of a curve that passes through the
 * middle of the segments either side of it, so the wire still crosses its
 * channel in its own lane but never turns sharply.
 *
 * @param {Array<{x:number,y:number}>} points - Port, lane corners, port.
 * @returns {string} SVG path data.
 */
function roomWiringSwoop(points) {
    const unique = points.filter((point, i) => i === 0 || point.x !== points[i - 1].x || point.y !== points[i - 1].y);
    if (!unique.length) return "";
    const f = value => Math.round(value * 10) / 10;
    let d = `M ${f(unique[0].x)} ${f(unique[0].y)}`;
    if (unique.length === 2) return `${d} L ${f(unique[1].x)} ${f(unique[1].y)}`;
    for (let i = 1; i < unique.length - 1; i += 1) {
        const corner = unique[i], next = unique[i + 1];
        const end = i === unique.length - 2 ? next : { x: (corner.x + next.x) / 2, y: (corner.y + next.y) / 2 };
        d += ` C ${f(corner.x)} ${f(corner.y)} ${f(corner.x)} ${f(corner.y)} ${f(end.x)} ${f(end.y)}`;
    }
    return d;
}

/**
 * Room diagram: a left-to-right tree of Equipment faceplates (owner), each
 * column from one top line, wired like the rack view: every wire leaves
 * its port sideways into its own lane of the channel beside its column and
 * enters the far card's facing side; a wire that skips columns runs along
 * its own lane across the top, and the patch panel's row sits north of it
 * all. No pathfinding, so the same data always draws the same picture.
 */
class RoomWiring {
    /**
     * Prepare room-only data without inferring any extra equipment or cables.
     *
     * @param {object} app - Equipment data and the current room identity.
     * @returns {object} Room nodes, connections, columns, and patch panel size.
     */
    static model(app) {
        const nodes = app.nodes.filter(n => n.placement?.container_room_id === app.roomId);
        const ids = new Set(nodes.map(n => n.id));
        const inside = l => ids.has(l.from) && ids.has(l.to);
        // Equipment's rules (owner): a down cable (cableIsDown) or a link to
        // a down device (downNodeIds) is not drawn, and gets no patch socket.
        const down = app.downNodeIds();
        const live = l => !down.has(l.from) && !down.has(l.to);
        const links = (app.links || []).filter(inside).filter(l => !app.cableIsDown(l, down));
        const byId = new Map(app.nodes.map(node => [node.id, node]));
        // No wire or socket for a cable to an unplaced device (owner): it
        // has no room to lead to.
        const exits = app.livePatchExits().filter(exit => exit.remote.placement?.container_room_id);
        const crossing = exits.map(exit => exit.link);
        const wireless = (app.wirelessLinks || []).filter(inside).filter(live);
        // NUT power relations, as Discovery draws them (labratPowerLinks):
        // a USB attachment, or a relayed (dummy-ups) unit that only reports
        // through its server. Neither is a network cable or counts as one.
        const powerKinds = { usb: "usb", relayed: "relay" };
        const usb = nodes.flatMap(node => {
            const reading = (node.observations || []).map(o => o.ups)
                .find(u => powerKinds[u?.attachment] && ids.has(u.server_node_id) && u.server_node_id !== node.id);
            return reading ? [{ from: reading.server_node_id, to: node.id, kind: powerKinds[reading.attachment] }] : [];
        }).filter(live);
        const geometry = { nodeWidth: ROOM_WIRING.cardWidth, nodeHeight: ROOM_WIRING.cardHeight };
        const { column, parent, follows } = roomWiringColumns(nodes, [...links, ...wireless], usb);
        // The patch panel is its own row, north of every column (owner), so
        // its cables drop cleanly to the devices and the WAN modems start
        // level with the rest: a row of sockets facing south, sized to its
        // cables (roomPatchShape).
        const shape = roomPatchShape(exits.length);
        const panel = exits.length ? { ...shape, faceWidth: shape.columns * ROOM_WIRING.socketStep + 14,
            width: shape.columns * ROOM_WIRING.socketStep + 30,
            height: shape.rows * ROOM_WIRING.socketStep - 2 + ROOM_WIRING.patchPad * 2 } : null;
        return { nodes, links, crossing, exits, panel, byId, wireless, usb, geometry, column, parent, follows };
    }

    /**
     * Each column's devices from the top, in tree order: by where the device
     * it hangs from sits in its column (a modem by its router's place), then
     * by fewest wires and then fewest direct children first (owner), then
     * routers and switches before hosts, then name. The patch panel heads
     * column 0 (owner: north of the rest, so its wires run clear). Cards
     * never move by hand (owner).
     *
     * @returns {Map<number, string[]>} Device IDs per column.
     */
    columnOrder() {
        const { column, parent, byId, links, nodes, follows } = this.data;
        const columns = new Map();
        for (const [id, index] of column) {
            if (follows.has(id)) continue;
            if (!columns.has(index)) columns.set(index, []);
            columns.get(index).push(id);
        }
        const place = new Map();
        const groupOf = id => {
            // The patch panel heads column 0, north of everything (owner).
            if (id === ROOM_PATCH_ID) return -1e9;
            const up = parent.get(id);
            if (up) return place.get(up) ?? -1;
            if (column.get(id) !== 0) return -1;
            // A modem sits beside the router it feeds.
            const near = links.filter(link => link.from === id || link.to === id)
                .map(link => place.get(link.from === id ? link.to : link.from)).filter(value => value !== undefined);
            return near.length ? Math.min(...near) : nodes.length;
        };
        // Among siblings, devices carrying fewer wires come first, then
        // fewer direct children (owner): thin chains line up across the top
        // rows and busy hubs sit below, out of their way.
        const wireCount = new Map();
        for (const line of [...links, ...this.data.crossing, ...this.data.wireless, ...this.data.usb]) {
            for (const end of [line.from, line.to]) wireCount.set(end, (wireCount.get(end) || 0) + 1);
        }
        const children = new Map();
        for (const [id, up] of parent) {
            if (!up) continue;
            if (!children.has(up)) children.set(up, []);
            children.get(up).push(id);
        }
        // Direct children only, not grandchildren (owner).
        const childCount = id => (children.get(id) || []).length;
        const indexes = [...columns.keys()].sort((a, b) => a - b);
        // Column 1 first, so column 0 can follow its routers.
        for (const index of [...indexes.filter(value => value !== 0), ...indexes.filter(value => value === 0)]) {
            const ids = columns.get(index);
            const group = new Map(ids.map(id => [id, groupOf(id)]));
            ids.sort((a, b) => group.get(a) - group.get(b)
                || (wireCount.get(a) || 0) - (wireCount.get(b) || 0)
                || childCount(a) - childCount(b)
                || labratCompareRecords(byId.get(a), byId.get(b)));
            // A UPS goes right below the server that reads it (owner).
            for (let at = ids.length - 1; at >= 0; at -= 1) {
                const under = [...follows.keys()].filter(ups => follows.get(ups) === ids[at])
                    .sort((a, b) => labratCompareRecords(byId.get(a), byId.get(b)));
                ids.splice(at + 1, 0, ...under);
                for (const ups of under) group.set(ups, `under:${ids[at]}`);
            }
            ids.forEach((id, at) => place.set(id, at));
        }
        return columns;
    }

    /**
     * The wires this draw shows, each with its two ends' cards and side:
     * a wire between neighboring columns leaves the left card's right side
     * and enters the right card's left; one within a column uses the
     * channel right of it at both ends; one that skips columns runs along
     * the top between the channel right of its left card and the channel
     * left of its right card.
     *
     * @returns {object[]} `{link, key, kind, a, b, via}` per wire, `a` left.
     */
    wires() {
        const { links, crossing, wireless, usb, column } = this.data;
        const all = [...links, ...crossing, ...wireless.map(link => ({ ...link, kind: "wireless" })), ...usb];
        const result = [];
        for (const link of all) {
            const physical = !link.kind;
            const exit = this.exitSockets.get(link);
            const end = (id, port) => {
                if (column.has(id)) return { id, port: physical ? port : null };
                const record = this.data.exits.find(item => item.link === link);
                return record ? { id: ROOM_PATCH_ID, port: null, exit: record } : null;
            };
            let a = end(link.from, link.from_port), b = end(link.to, link.to_port);
            if (!a || !b || !this.cards.has(a.id) || !this.cards.has(b.id)) continue;
            if (b.id === ROOM_PATCH_ID) [a, b] = [b, a];
            if (a.id === ROOM_PATCH_ID) {
                // Down from its socket into the band above the columns, then
                // along it and down the channel before its device.
                a.side = "bottom"; b.side = "left";
                const key = ["cable", link.from, link.from_port || "", link.to, link.to_port || ""].join("|");
                result.push({ link, key, kind: link.kind, a, b, via: "patch" });
                continue;
            }
            if (column.get(a.id) > column.get(b.id)) [a, b] = [b, a];
            const gap = column.get(b.id) - column.get(a.id);
            const via = gap === 0 ? "same" : gap === 1 ? "next" : "top";
            // East-bound ends on a card's right side, west-bound on its left
            // (owner), so no wire wraps around its card.
            a.side = "right"; b.side = via === "same" ? "right" : "left";
            const key = [link.kind || "cable", link.from, link.from_port || "", link.to, link.to_port || ""].join("|");
            result.push({ link, key, kind: link.kind, a, b, via });
        }
        return result;
    }

    /**
     * Lay the cards out: columns left to right, each stacked from the top
     * (owner), every card tall enough for the ports down its sides, with
     * room between columns for their channel lanes and above everything for
     * the top runs. Then put each wire end on its card's side, in the order
     * of where the wires go, so they cross as little as they can.
     *
     * @returns {void}
     */
    layout() {
        const L = ROOM_WIRING;
        const { column, panel } = this.data;
        const columns = this.columnOrder();
        const wires = this.wires();
        const sides = new Map();
        const sideOf = (id, side) => {
            if (!sides.has(id)) sides.set(id, { left: [], right: [], top: [], bottom: [] });
            return sides.get(id)[side];
        };
        const channelCount = new Map();
        let topCount = 0, patchCount = 0;
        for (const wire of wires) {
            sideOf(wire.a.id, wire.a.side).push({ wire, end: wire.a, far: wire.b });
            sideOf(wire.b.id, wire.b.side).push({ wire, end: wire.b, far: wire.a });
            if (wire.via === "patch") {
                const colB = column.get(wire.b.id);
                channelCount.set(colB - 1, (channelCount.get(colB - 1) || 0) + 1);
                patchCount += 1;
                continue;
            }
            const colA = column.get(wire.a.id), colB = column.get(wire.b.id);
            channelCount.set(colA, (channelCount.get(colA) || 0) + 1);
            if (wire.via === "top") {
                channelCount.set(colB - 1, (channelCount.get(colB - 1) || 0) + 1);
                topCount += 1;
            }
        }
        // Card heights: room for the busier side's ports.
        for (const id of this.cards.keys()) {
            if (id === ROOM_PATCH_ID) continue;
            const ports = Math.max(sideOf(id, "left").length, sideOf(id, "right").length);
            const height = Math.max(L.cardHeight, ports * L.portStep + L.portPad * 2);
            this.sizes.set(id, { width: L.cardWidth, height });
            this.cards.get(id).style.height = `${height}px`;
        }
        // Columns left to right, a channel after each (and one before column
        // 0 when the patch panel feeds a device there).
        const indexes = [...columns.keys()].sort((a, b) => a - b);
        const last = indexes.at(-1) ?? 0;
        this.columnsX = new Map(); this.channelX = new Map();
        let x = L.margin;
        if (channelCount.get(-1)) {
            this.channelX.set(-1, x);
            x += L.channelPad + (channelCount.get(-1) - 1) * L.lane;
        }
        for (let index = 0; index <= last; index += 1) {
            this.columnsX.set(index, x);
            const width = Math.max(0, ...(columns.get(index) || []).map(id => this.size(id).width));
            this.channelX.set(index, x + width + L.channelPad);
            x += width + L.channelPad * 2 + Math.max(0, (channelCount.get(index) || 0) - 1) * L.lane;
        }
        // The patch panel's row, then its lanes and the top runs, then every
        // column from the same top line (owner).
        this.positions = new Map();
        let bandY = L.margin;
        if (panel && this.cards.has(ROOM_PATCH_ID)) {
            const card = this.cards.get(ROOM_PATCH_ID);
            this.positions.set(ROOM_PATCH_ID, { x: L.margin, y: L.margin, width: panel.width, height: panel.height });
            card.style.left = `${L.margin}px`; card.style.top = `${L.margin}px`;
            bandY = L.margin + panel.height + L.topGap;
        }
        this.patchLaneY = bandY;
        this.topY = bandY + patchCount * L.lane;
        const firstY = this.topY + topCount * L.lane + (patchCount + topCount ? L.topGap : 0);
        let bottom = firstY;
        for (const index of indexes) {
            let y = firstY;
            for (const id of columns.get(index)) {
                // Level with the device it hangs from when nothing above is
                // in the way (owner), so a chain reads across one row.
                const up = this.data.parent.get(id);
                if (up && !this.data.follows.has(id) && this.positions.has(up)) y = Math.max(
                    y, this.positions.get(up).y
                );
                const size = this.size(id);
                this.positions.set(id, { x: this.columnsX.get(index), y, width: size.width, height: size.height });
                const card = this.cards.get(id);
                if (card) { card.style.left = `${this.columnsX.get(index)}px`; card.style.top = `${y}px`; }
                y += size.height + L.rowGap;
            }
            bottom = Math.max(bottom, y - L.rowGap);
        }
        // Each side's ports in the order of where their wires go: a wire
        // over the top first, then by the far card's height on the page.
        const farY = item => (item.wire.via === "top" && item.end === item.wire.a ? -Infinity
            : this.positions.get(item.far.id).y + this.size(item.far.id).height / 2);
        for (const [id, both] of sides) {
            for (const side of ["left", "right"]) {
                const items = both[side].sort((p, q) => farY(p) - farY(q) || p.wire.key.localeCompare(q.wire.key));
                const box = this.positions.get(id);
                const start = box.y + (box.height - items.length * L.portStep) / 2 + L.portStep / 2;
                items.forEach((item, index) => {
                    item.end.x = side === "left" ? box.x : box.x + box.width;
                    item.end.y = start + index * L.portStep;
                });
            }
            const box = this.positions.get(id);
            if (id === ROOM_PATCH_ID) {
                // Sockets left to right in the columns' order, east to west
                // (owner): nearest column first, and within a column the
                // lowest card first. The leftmost takes the lowest lane and
                // the leftmost channel lane, so the runs stack without
                // crossing however many go to each column.
                const items = both.bottom.sort((p, q) => column.get(p.far.id) - column.get(q.far.id)

                        || this.positions.get(q.far.id).y - this.positions.get(p.far.id).y
                        || p.wire.key.localeCompare(q.wire.key));
                const columnsWide = panel.columns;
                items.forEach((item, index) => {
                    const col = index % columnsWide, row = Math.floor(index / columnsWide);
                    item.end.x = box.x + 17 + col * L.socketStep + 10;
                    item.end.y = box.y + 17 + row * L.socketStep + 20;
                    item.wire.patchLane = this.patchLaneY + (items.length - 1 - index) * L.lane;
                });
            }
        }
        this.placePorts(sides);
        if (panel && this.cards.has(ROOM_PATCH_ID)) this.renderPatchFace(sideOf(ROOM_PATCH_ID, "bottom"));
        this.wireList = wires;
        this.channels = channelCount;
        this.topCount = topCount;
        this.columns = columns;
        this.layoutSize = { width: x - L.channelPad + L.margin, height: bottom + L.margin };
    }

    /**
     * Show only the ports that carry a drawn wire (owner), down the card's
     * sides: Equipment's own lamps (state, medium, tooltip), and a small
     * dot for a Wi-Fi or UPS line, which has no port.
     *
     * @param {Map<string, {left: object[], right: object[]}>} sides - Ends per card side.
     * @returns {void}
     */
    placePorts(sides) {
        for (const [id, card] of this.cards) {
            if (id === ROOM_PATCH_ID) continue;
            for (const old of [...card.children].filter(child => child.classList?.contains("room-wiring-port"))) old.remove();
            const box = this.positions.get(id);
            for (const side of ["left", "right", "top", "bottom"]) {
                for (const { wire, end } of sides.get(id)?.[side] || []) {
                    let lamp = end.port ? this.lamps.get(`${id}::${end.port}`)?.cloneNode(true) : null;
                    if (!lamp) {
                        lamp = document.createElement("span");
                        lamp.className = "rack-port-light room-wiring-dot";
                        lamp.dataset.kind = wire.kind || "cable";
                        lamp.title = {
                            wireless: "Wi-Fi", usb: "USB to UPS", relay: "Reports through NUT"
                        }[wire.kind] || "Cable";
                    }
                    lamp.classList.add("room-wiring-port");
                    lamp.dataset.side = side;
                    lamp.setAttribute("aria-hidden", "true");
                    if (side === "top" || side === "bottom") lamp.style.left = `${end.x - box.x - 5}px`;
                    else lamp.style.top = `${end.y - box.y - 5}px`;
                    end.lamp = lamp;
                    card.append(lamp);
                }
            }
        }
    }

    /**
     * Re-draw the patch face with its sockets in their wires' order.
     *
     * @param {object[]} items - The laid-out wire ends, in order.
     */
    renderPatchFace(items) {
        const { panel } = this.data;
        const exits = items.map(item => item.end.exit).filter(Boolean);
        const face = this.patch.render(exits, { ...panel, width: panel.faceWidth, height: panel.height - 16 });
        this.patchCard.replaceChildren(face);
    }

    /**
     * Read this browser's wire style for the Diagram.
     *
     * @returns {string} "curved" (the default) or "square".
     */
    readWireStyle() {
        // Swoopy curves by default (owner); 90° bends stay a choice.
        return labratReadWireStyle("labrat.diagram.wireStyle", "curved");
    }

    /** Describe the active wire style for pointer and keyboard users. */
    updateWireStyleButton() {
        labratUpdateWireStyleButton(document.getElementById("room-wiring-style"), this.wireStyle);
    }

    /** Toggle cable bends without changing device positions or discovery data. */
    toggleWireStyle() {
        this.wireStyle = this.wireStyle === "curved" ? "square" : "curved";
        labratSaveWireStyle("labrat.diagram.wireStyle", this.wireStyle);
        this.updateWireStyleButton();
        this.draw();
        this.announce(`Wire style: ${this.wireStyle === 'curved' ? 'Curved' : '90°'}.`);
    }

    /** Pan the background with mouse or pen, retaining native touch scrolling. */
    bindPan() {
        let pan = null;
        this.canvas.addEventListener("pointerdown", event => {
            if (event.button !== 0 || event.pointerType === "touch"
                    || event.target.closest("button, a, input, select")) return;
            pan = { pointer: event.pointerId, x: event.clientX, y: event.clientY,
                left: this.canvas.scrollLeft, top: this.canvas.scrollTop };
        });
        this.canvas.addEventListener("pointermove", event => {
            if (!pan || event.pointerId !== pan.pointer) return;
            const dx = event.clientX - pan.x, dy = event.clientY - pan.y;
            if (!this.panning && Math.hypot(dx, dy) < 4) return;
            this.panning = true;
            this.canvas.classList.add("is-panning");
            this.canvas.setPointerCapture(event.pointerId);
            event.preventDefault();
            this.canvas.scrollLeft = pan.left - dx;
            this.canvas.scrollTop = pan.top - dy;
        });
        const finish = event => {
            if (!pan || event.pointerId !== pan.pointer) return;
            pan = null; this.panning = false;
            this.canvas.classList.remove("is-panning");
            if (this.canvas.hasPointerCapture(event.pointerId)) this.canvas.releasePointerCapture(event.pointerId);
            if (this.app.updateHeld) setTimeout(() => this.app.applyUpdate(), 0);
        };
        this.canvas.addEventListener("pointerup", finish);
        this.canvas.addEventListener("pointercancel", finish);
        this.canvas.addEventListener("lostpointercapture", finish);
        // The wheel zooms about the pointer (owner); dragging the background pans.
        this.canvas.addEventListener("wheel", event => {
            if (!this.stage?.isConnected || !event.deltaY) return;
            event.preventDefault();
            const lines = event.deltaMode === 1 ? 40 : event.deltaMode === 2 ? 800 : 1;
            const box = this.canvas.getBoundingClientRect();
            this.zoom(Math.exp(-event.deltaY * lines * 0.0015),
                { x: event.clientX - box.left, y: event.clientY - box.top });
        }, { passive: false });
    }

    /**
     * Zoom, keeping the stage point under `anchor` (canvas pixels; the
     * visible center by default) in place. Wires scale with the stage and
     * are not redrawn.
     *
     * @param {number} factor - Scale multiplier.
     * @param {{x: number, y: number}} [anchor] - Fixed point in the canvas.
     * @returns {void}
     */
    zoom(factor, anchor = { x: this.canvas.clientWidth / 2, y: this.canvas.clientHeight / 2 }) {
        if (!this.stage?.isConnected) return;
        const point = { x: (this.canvas.scrollLeft + anchor.x - this.offset.x) / this.scale,
            y: (this.canvas.scrollTop + anchor.y - this.offset.y) / this.scale };
        this.zoomScale = Math.max(0.1, Math.min(2, this.scale * factor));
        this.fit();
        this.canvas.scrollLeft = Math.max(0, point.x * this.scale + this.offset.x - anchor.x);
        this.canvas.scrollTop = Math.max(0, point.y * this.scale + this.offset.y - anchor.y);
    }

    /** Show the complete diagram at once. */
    zoomToFit() {
        if (!this.stage?.isConnected) return;
        this.fit();
        this.zoomScale = Math.max(0.1, Math.min(1, this.canvas.clientWidth / this.width,
            this.canvas.clientHeight / this.height));
        this.fit();
        this.canvas.scrollLeft = this.slack.x; this.canvas.scrollTop = this.slack.y;
    }

    /**
     * The floating cable key's hide arrow and "Key" tab, as Discovery's map
     * key: open by default, the choice kept in this browser.
     *
     * @returns {void}
     */
    bindKey() {
        const hide = document.getElementById("room-wiring-key-hide");
        const show = document.getElementById("room-wiring-key-show");
        if (!hide || !show) return;
        let open = true;
        try { open = localStorage.getItem("labrat.diagram.keyOpen") !== "false"; } catch { /* Optional preference. */ }
        const set = (value, focus = false) => {
            document.getElementById("room-wiring-key").dataset.open = String(value);
            document.getElementById("room-wiring-key-panel").inert = !value;
            show.inert = value;
            hide.setAttribute("aria-expanded", String(value));
            show.setAttribute("aria-expanded", String(value));
            // Never scroll: the slid-out panel would drag the canvas frame along.
            if (focus) (value ? hide : show).focus({ preventScroll: true });
        };
        set(open);
        const toggle = value => {
            set(value, true);
            try { localStorage.setItem("labrat.diagram.keyOpen", String(value)); } catch { /* Optional preference. */ }
        };
        hide.addEventListener("click", () => toggle(false));
        show.addEventListener("click", () => toggle(true));
    }

    /**
     * Announce diagram moves independently of inventory editing notices.
     *
     * @param {string} message - What to announce.
     */
    announce(message) {
        const status = document.getElementById("room-wiring-status");
        if (status) status.textContent = message;
    }

    /**
     * Draw selectable Equipment faceplates in their columns; wires are
     * measured after layout settles (draw).
     *
     * @param {object} app - Existing Equipment controller and shared port helpers.
     * @returns {void}
     */
    render(app) {
        if (this.panning) return;
        this.closePatchDetails();
        this.app = app;
        this.wireStyle = this.readWireStyle();
        this.updateWireStyleButton();
        this.data = RoomWiring.model(app);
        const { nodes, links, crossing, geometry } = this.data;
        this.canvas = document.getElementById("room-wiring-canvas");
        this.diagram = document.getElementById("room-wiring-diagram");
        this.svg?.remove();
        if (!this.scrollBound) {
            this.bindPan();
            window.addEventListener?.("resize", () => { this.fitHeight(); this.fit(); });
            this.bindKey();
            this.scrollBound = true;
        }
        this.fitHeight();
        const previousScroll = { left: this.canvas.scrollLeft, top: this.canvas.scrollTop };
        this.canvas.replaceChildren();
        this.sizes = new Map(nodes.map(node => [node.id, { width: geometry.nodeWidth, height: geometry.nodeHeight }]));
        if (this.data.panel) this.sizes.set(
            ROOM_PATCH_ID, { width: this.data.panel.width, height: this.data.panel.height }
        );
        document.getElementById("room-wiring-summary").textContent = `${nodes.length} devices · ${links.length} area cables`
            + (crossing.length ? ` · ${crossing.length} external` : "");
        if (!nodes.length) return;
        const stage = document.createElement("div");
        stage.className = "room-wiring-stage";
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("class", "rack-cable-layer room-wiring-lines");
        svg.setAttribute("aria-label", "Observed wiring in this area");
        this.stage = stage; this.svg = svg; this.cards = new Map(); this.lamps = new Map();
        for (const node of nodes) {
            const card = document.createElement("button");
            card.type = "button";
            card.className = "rack-device room-wiring-node";
            card.dataset.node = node.id; card.dataset.category = node.category;
            card.setAttribute("aria-pressed", String(node.id === app.selectedId));
            card.setAttribute("aria-label", `Inspect ${node.name}`);
            card.style.width = `${geometry.nodeWidth}px`;
            card.style.height = `${geometry.nodeHeight}px`;
            const name = document.createElement("strong");
            name.className = "rack-device-name";
            name.append(app.statusLamp(node), document.createTextNode(node.name));
            const place = document.createElement("span");
            place.className = "room-wiring-place";
            const position = node.placement;
            place.textContent = `${rackCategory(node.category)} · ${position.container_name || rackCategory(position.container_kind)}`;
            card.append(name, place);
            // Equipment's lamps, kept aside: placePorts shows the wired ones.
            app.appendPortLights(card, node, null, { onTop: true, limit: Infinity });
            const lights = [...card.children].find(child => child.className === "rack-port-lights");
            for (const lamp of lights?.children || []) this.lamps.set(lamp.dataset.pin, lamp);
            lights?.remove();
            // Cards are fixed in the tree (owner: no drag); a click opens details.
            card.addEventListener("click", () => this.app.select(node.id));
            this.cards.set(node.id, card);
            stage.append(card);
        }
        this.renderPatchPanel();
        this.layout();
        stage.append(svg);
        this.sizer = document.createElement("div");
        this.sizer.className = "room-wiring-size";
        this.sizer.append(stage);
        this.canvas.append(this.sizer);
        const first = this.slack === undefined;
        this.fit();
        // The tree reads from the top left (owner); later renders keep the view.
        this.canvas.scrollLeft = first ? this.slack.x : previousScroll.left;
        this.canvas.scrollTop = first ? this.slack.y : previousScroll.top;
    }

    /**
     * The room's patch panel as a card at the top of column 0, above the
     * WAN modems (owner): one column of sockets facing east, one per live
     * cable leaving the room.
     *
     * @returns {void}
     */
    renderPatchPanel() {
        this.patch ||= new RoomPatchPanel({ app: this.app, idPrefix: "room-patch-details",
            after: () => this.cards.values().next().value });
        this.patch.app = this.app;
        const { panel, exits } = this.data;
        if (!panel) { this.patch.render([], {}); return; }
        const face = this.patch.render(exits, { ...panel, width: panel.faceWidth, height: panel.height - 16 });
        const card = document.createElement("div");
        card.className = "room-wiring-patch";
        card.tabIndex = 0;
        card.setAttribute("role", "group");
        card.setAttribute("aria-label", `Patch panel, ${exits.length} ${exits.length === 1 ? 'cable' : 'cables'} `
            + "leaving this area.");
        card.append(face);
        card.style.width = `${panel.width}px`;
        card.style.height = `${panel.height}px`;
        this.cards.set(ROOM_PATCH_ID, card);
        this.stage.append(card);
    }

    /** Sockets by link (shared RoomPatchPanel records). */
    get exitSockets() { return this.patch?.sockets || new Map(); }

    /** The patch panel face (inside its canvas card), if a live cable leaves the room. */
    get patchPanel() { return this.patch?.face || null; }

    /** The patch panel's canvas card, if any. */
    get patchCard() { return this.cards?.get(ROOM_PATCH_ID) || null; }

    /** @param {string} id - Stage item. @returns {{width: number, height: number}} Its card size. */
    size(id) {
        return this.sizes?.get(id) || { width: this.data.geometry.nodeWidth, height: this.data.geometry.nodeHeight };
    }

    /** The socket whose destination popup is open, if any. */
    get patchDetails() { return this.patch?.open || null; }

    /** Close any open destination popup. */
    closePatchDetails() { this.patch?.close(); }

    /** Size the stage and scroll area to the drawing at the current zoom. */
    fit() {
        if (!this.stage?.isConnected) return;
        this.width = this.layoutSize.width;
        this.height = this.layoutSize.height;
        if (!this.panning) this.scale = this.zoomScale
            ?? Math.min(1, Math.max(0.75, this.canvas.clientWidth / this.width));
        const zoomLabel = document.getElementById("room-wiring-zoom-value");
        if (zoomLabel) zoomLabel.textContent = `${Math.round(this.scale * 100)}%`;
        const zoomOut = document.getElementById("room-wiring-zoom-out");
        const zoomIn = document.getElementById("room-wiring-zoom-in");
        if (zoomOut) zoomOut.disabled = this.scale <= 0.1;
        if (zoomIn) zoomIn.disabled = this.scale >= 2;
        this.stage.style.width = `${this.width}px`; this.stage.style.height = `${this.height}px`;
        this.svg.setAttribute("width", this.width);
        this.svg.setAttribute("height", this.height);
        this.stage.style.transform = `scale(${this.scale})`;
        // Room to pan past the drawing (owner): half the visible canvas on
        // every side, so the whole room moves even when it all fits.
        this.slack = { x: Math.round(this.canvas.clientWidth / 2), y: Math.round(this.canvas.clientHeight / 2) };
        this.offset = { ...this.slack };
        this.stage.style.left = `${this.offset.x}px`;
        this.stage.style.top = `${this.offset.y}px`;
        this.sizer.style.width = `${this.width * this.scale + 2 * this.slack.x}px`;
        this.sizer.style.height = `${this.height * this.scale + 2 * this.slack.y}px`;
    }

    /**
     * Pin the canvas to the window's height (owner, as the Floor plan does):
     * whatever the page above and below it leaves, footer included, never
     * under 360 px. Zooming never changes it. Phones keep their own height.
     *
     * @returns {void}
     */
    fitHeight() {
        const canvas = this.canvas;
        if (!canvas?.style) return;
        if (typeof window.matchMedia !== "function") return;
        if (window.matchMedia("(max-width: 600px)").matches) {
            canvas.style.removeProperty?.("height");
            return;
        }
        // Measured in place: what lies below the canvas does not depend on
        // its height, and clearing the height first would reset its scroll.
        const frame = canvas.closest?.(".view-frame");
        const main = canvas.closest?.("main");
        const footer = document.querySelector(".site-footer");
        if (!frame || !main) return;
        const box = canvas.getBoundingClientRect();
        const style = (element, key) => parseFloat(getComputedStyle(element)[key]) || 0;
        // Measured from the content itself: the page body is at least the
        // window's height, so its scroll height cannot show spare room.
        const below = frame.getBoundingClientRect().bottom - box.bottom + style(frame, "marginBottom")
            + style(main, "paddingBottom") + (footer?.offsetHeight || 0);
        const top = box.top + window.scrollY;
        canvas.style.setProperty("height", `${Math.max(360, Math.floor(window.innerHeight - top - below))}px`);
    }

    /**
     * Draw every wire on its fixed path, the rack view's way (owner): out of
     * its port sideways into its own lane of the channel beside the card,
     * along it, and into the far card's facing side; a wire that skips
     * columns rises to its own lane across the top and drops down the
     * channel just before its far card. Lanes in a channel are ordered so
     * wires cross as little as they can (roomWiringLaneOrder); top lanes
     * nest, the longest highest. Existing elements are updated in place.
     *
     * @returns {void}
     */
    draw() {
        if (!this.stage?.isConnected) return;
        this.fit();
        const { app, data, svg } = this;
        const L = ROOM_WIRING;
        if (this.wireLayer !== svg) { this.wireLayer = svg; this.wireElements = new Map(); }
        const wires = this.wireList || [];
        // Top lanes: shortest span lowest, so a run never crosses one it nests in.
        const tops = wires.filter(wire => wire.via === "top")
            .sort((p, q) => (data.column.get(p.b.id) - data.column.get(p.a.id)) - (data.column.get(q.b.id) - data.column.get(q.a.id))
                || p.a.y - q.a.y);
        tops.forEach((wire, index) => { wire.topY = this.topY + (tops.length - 1 - index) * L.lane; });
        // Channel lanes, per channel.
        const channels = new Map();
        const add = (index, item) => {
            if (!channels.has(index)) channels.set(index, []);
            channels.get(index).push(item);
        };
        for (const wire of wires) {
            const colA = data.column.get(wire.a.id), colB = data.column.get(wire.b.id);
            if (wire.via === "patch") { add(
                colB - 1, { wire, part: "b", left: [wire.patchLane], right: [wire.b.y] }
            ); continue; }
            if (wire.via === "next") add(colA, { wire, part: "a", left: [wire.a.y], right: [wire.b.y] });
            if (wire.via === "same") add(colA, { wire, part: "a", left: [wire.a.y, wire.b.y], right: [] });
            if (wire.via === "top") {
                add(colA, { wire, part: "a", left: [wire.a.y], right: [wire.topY] });
                add(colB - 1, { wire, part: "b", left: [wire.topY], right: [wire.b.y] });
            }
        }
        for (const [index, items] of channels) {
            roomWiringLaneOrder(items).forEach((item, lane) => {
                item.wire[`lane_${item.part}`] = this.channelX.get(index) + lane * L.lane;
            });
        }
        const highlighted = this.patchDetails?.exit;
        for (const [id, card] of this.cards) {
            card.classList.toggle("is-patch-highlighted", id === highlighted?.local.id);
        }
        this.highlightedPort?.classList.remove("is-patch-port");
        this.highlightedPort = null;
        // A selected device lights its own wires (to its parents and
        // children) and every route from it toward the gateway (owner):
        // each wire to a device further left (or to the server a UPS hangs
        // under), followed on until the routers.
        const routeKeys = new Set();
        if (app.selectedId) {
            const pending = [app.selectedId], reached = new Set(pending);
            while (pending.length) {
                const id = pending.shift();
                if ((data.column.get(id) ?? 0) <= 1) continue;
                for (const wire of wires) {
                    if (wire.a.id !== id && wire.b.id !== id) continue;
                    const other = wire.a.id === id ? wire.b.id : wire.a.id;
                    const upward = other !== ROOM_PATCH_ID && (data.column.get(other) < data.column.get(id)
                        || data.follows.get(id) === other);
                    if (!upward) continue;
                    routeKeys.add(wire.key);
                    if (!reached.has(other)) { reached.add(other); pending.push(other); }
                }
            }
        }
        const seen = new Set();
        for (const wire of wires) {
            const { link, key, a, b } = wire;
            const physical = !link.kind;
            const layer = svg;
            const points = wire.via === "patch" ? [a, {
                x: a.x, y: wire.patchLane
            }, { x: wire.lane_b, y: wire.patchLane },
                    { x: wire.lane_b, y: b.y }, b]
                : wire.via === "top"
                ? [a, { x: wire.lane_a, y: a.y }, { x: wire.lane_a, y: wire.topY },
                    { x: wire.lane_b, y: wire.topY }, { x: wire.lane_b, y: b.y }, b]
                : [a, { x: wire.lane_a, y: a.y }, { x: wire.lane_a, y: b.y }, b];
            seen.add(key);
            const plain = points.map(point => ({ x: point.x, y: point.y }));
            const d = this.wireStyle === "curved" ? roomWiringSwoop(plain) : wirePath(plain, false);
            if (link === highlighted?.link) {
                const end = [a, b].find(item => item.id === highlighted.local.id);
                this.highlightedPort = end?.lamp || null;
                this.highlightedPort?.classList.add("is-patch-port");
            }
            const ports = [link.from, link.to].map((id, i) => data.byId.get(id)?.ports
                ?.find(p => p.name === (i ? link.to_port : link.from_port)));
            // A port's own medium first, else the cable's: the API may take it
            // from an identical twin cable (medium_inferred_from).
            const medium = ports.find(p => p?.medium)?.medium || link.medium || "unknown";
            const speed = Math.max(...ports.map(p => Number(p?.speed_mbps) || 0));
            const involved = highlighted ? link === highlighted.link
                : !app.selectedId || link.from === app.selectedId || link.to === app.selectedId
                    || routeKeys.has(key);
            let entry = this.wireElements.get(key);
            if (!entry) {
                const path = document.createElementNS(svg.namespaceURI, "path");
                path.dataset.from = link.from; path.dataset.to = link.to;
                path.dataset.fromPort = link.from_port || ""; path.dataset.toPort = link.to_port || "";
                if (link.kind) path.classList.add(`is-${link.kind}`);
                const title = document.createElementNS(svg.namespaceURI, "title");
                title.textContent = physical
                    ? `${link.from_port} ↔ ${link.to_port} · ${link.confidence} · ${link.evidence} · ${link.source || ''}`
                        + (link.medium_inferred_from ? " · medium from its twin cable" : "")
                    : link.kind === "usb" ? "USB attachment reported by NUT"
                        : link.kind === "relay"
                            ? "Reports through this NUT server · values relayed into NUT by dummy-ups, "
                            + "not a USB or network connection"
                        : "Inferred Wi-Fi association";
                path.append(title); layer.append(path);
                entry = { path, flow: null };
                this.wireElements.set(key, entry);
            }
            const { path } = entry;
            if (path.getAttribute?.("d") !== d) path.setAttribute("d", d);
            path.dataset.medium = medium;
            path.classList.toggle("is-dim", !involved);
            path.classList.toggle("is-active", involved);
            path.classList.toggle("is-patch-highlighted", link === highlighted?.link);
            path.classList.toggle("is-tentative", link.confidence === "ambiguous" || physical && medium === "unknown");
            path.style.strokeWidth = String(wireSpeedStyle(speed).width);
            const flowing = physical && involved && medium !== "unknown" && link.confidence !== "ambiguous"
                && ports.every((p, i) => p && app.portAlarmState(p, i ? link.to : link.from) === "up");
            if (flowing && !entry.flow) {
                entry.flow = document.createElementNS(svg.namespaceURI, "path");
                entry.flow.setAttribute("class", "rack-cable-flow");
                layer.append(entry.flow);
            } else if (!flowing && entry.flow) {
                entry.flow.remove(); entry.flow = null;
            }
            if (entry.flow) {
                if (entry.flow.getAttribute?.("d") !== d) entry.flow.setAttribute("d", d);
                entry.flow.dataset.medium = medium; entry.flow.style.strokeWidth = path.style.strokeWidth;
                entry.flow.style.animationDuration = `${wireSpeedStyle(speed).flow}s`;
            }
        }
        for (const [key, entry] of this.wireElements) {
            if (seen.has(key)) continue;
            entry.path.remove(); entry.flow?.remove();
            this.wireElements.delete(key);
        }
    }
}
