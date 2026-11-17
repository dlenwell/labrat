/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * @fileoverview Equipment's cable layer: network cables, surface and patch-panel runs,
 * Wi-Fi and UPS lines, their lamps, and hover tracing. Every route is built
 * square and drawn with the shared wiring.js geometry; lanes, trays, and
 * device bundles are shared counters reset on each redraw
 * (planning/specs/equipment.md).
 *
 * Part of the Equipment page's RackViewApp (racks.js): these methods join
 * its prototype through rackAddMethods, so `this` is the page's app. Load
 * after racks.js.
 */

/**
 * Build the geometry and routing helpers one Equipment surface-cable draw
 * shares: card, pin, and lamp lookups; the rack channels and top wire tray;
 * obstacles; and the obstacle-avoiding router with its run separation.
 *
 * drawSurfaceUplinks uses them for room and rack-top cables, and hands them
 * on to the patch-panel, USB, and Wi-Fi layers, so every line in one draw
 * shares the same lanes, trays, and used runs.
 *
 * @param {RackViewApp} app - The Equipment app (its channel and exit counters).
 * @param {HTMLElement} root - The page's main column, which the layer covers.
 * @param {DOMRect} box - The column's box, the origin for every coordinate.
 * @returns {object} The helpers by name, the column's `box`, and
 *     cableIndex(): how many card-to-card cables have been routed so far.
 */
function rackSurfaceRouting(app, root, box) {
    const cardFor = (id) => root.querySelector(
        `.rack-top-item[data-node="${CSS.escape(id)}"], .rack-room-item[data-node="${CSS.escape(id)}"]`,
    );
    // Rack-face pins only: rack-top lamps carry data-pin too, but their
    // cables are drawn here, never by the rack cable layer.
    const pinFor = (id, port) => root.querySelector(
        `.rack-frame [data-pin="${CSS.escape(`${id}::${port}`)}"]`,
    );
    // A rack-top or room card's own lamp for a port, and the plank a
    // rack-top card rests on.
    const lampFor = (card, id, port) => (card?.matches(".rack-top-item, .rack-room-item")
        ? card.querySelector(`[data-pin="${CSS.escape(`${id}::${port}`)}"]`) : null);
    const plankBelow = (card) => card.closest(".rack-top-display")?.querySelector(".rack-top-plank");
    const channelX = (frame) => frame.getBoundingClientRect().right - box.left + 12 + app.nextChannel(frame) * 3;
    // The top wire tray: the strip between a rack's plank and its faces.
    // Every run along it takes the next of RACK_STACK_POSITIONS (8)
    // heights, 4 px under the plank and 3 px apart (fewer when the tray
    // is shorter), so no two share one; only one more rotates back and may
    // overlap (owner). The lowest position, nearest the rack faces, fills
    // first (owner).
    const trayLanes = wireLaneCounter();
    const trayY = (plank) => {
        const bottom = center(plank).bottom;
        const face = plank.closest(".rack-block")?.querySelector(".rack-frame");
        const limit = face ? face.getBoundingClientRect().top - box.top - 3 : bottom + 4 + RACK_STACK_POSITIONS * 3;
        const positions = Math.max(1, Math.min(RACK_STACK_POSITIONS, Math.floor((limit - bottom - 4) / 3) + 1));
        const used = trayLanes(plank);
        return bottom + 4 + (positions - 1 - (used % positions)) * 3;
    };
    const visibleInRack = (element) => {
        const rack = element.closest(".rack-block");
        if (!rack) return true;
        const inner = element.getBoundingClientRect();
        // A narrow panel's rack-top row scrolls on its own (rackTopScrollers):
        // a card scrolled out of the row has no visible cable anchor either.
        return [rack, element.closest(".rack-top-items")].filter(Boolean).every((clip) => {
            const outer = clip.getBoundingClientRect();
            return inner.left >= outer.left - 0.5 && inner.right <= outer.right + 0.5;
        });
    };
    const center = (element) => {
        const rect = element.getBoundingClientRect();
        return { x: rect.left + rect.width / 2 - box.left,
            y: rect.top + rect.height / 2 - box.top,
            halfWidth: rect.width / 2, halfHeight: rect.height / 2,
            top: rect.top - box.top, bottom: rect.bottom - box.top };
    };
    const edgeToward = (from, to) => {
        const dx = to.x - from.x;
        const dy = to.y - from.y;
        if (dx === 0 && dy === 0) return { x: from.x, y: from.bottom };
        const fraction = Math.min(from.halfWidth / Math.abs(dx), from.halfHeight / Math.abs(dy));
        return { x: from.x + dx * fraction, y: from.y + dy * fraction };
    };
    // Cards, rack frames, and panel headers that a cable must not cross,
    // measured once per draw and only when a card-to-card cable needs them.
    let obstacles = null;
    const obstacleBoxes = () => {
        obstacles ||= [...root.querySelectorAll(
            ".rack-room-item, .rack-top-item, .rack-top-plank, .rack-frame, .rack-room-group-header, "
            + ".rack-patch-box .room-patch-panel, #rack-patch-title, .rack-wan-panel, .rack-power-panel",
        )].filter((element) => element.getClientRects?.().length !== 0).map((element) => {
            const rect = element.getBoundingClientRect();
            return { left: rect.left - box.left, right: rect.right - box.left,
                top: rect.top - box.top, bottom: rect.bottom - box.top };
        });
        return obstacles;
    };
    /**
     * Route between two cards with no port lamps (room surfaces) the way
     * the room Diagram does (wireRoute, room-wiring.js): square
     * bends that stay clear of every card, frame, and panel header. Each
     * card may be left from its bottom or its right side; the shortest
     * clear route wins.
     *
     * @returns {string|null} Path data, or null when no route is clear.
     */
    // Whether a straight run stays clear of every obstacle (4 px margin,
    // the router's own test), for smoothing its routes below.
    const runIsClear = (from, to) => !obstacleBoxes().some((item) => {
        const left = item.left - 4, right = item.right + 4, top = item.top - 4, bottom = item.bottom + 4;
        return from.x === to.x
            ? from.x > left && from.x < right && Math.max(from.y, to.y) > top && Math.min(from.y, to.y) < bottom
            : from.y > top && from.y < bottom && Math.max(from.x, to.x) > left && Math.min(from.x, to.x) < right;
    });
    // Replace each zigzag (two parallel runs joined by a short step)
    // with one corner when that corner's runs are clear.
    const straighten = (points) => {
        const route = [...points];
        for (let changed = true; changed;) {
            changed = false;
            for (let index = 0; index + 3 < route.length; index++) {
                const [a, , , d] = route.slice(index, index + 4);
                for (const corner of [{ x: a.x, y: d.y }, { x: d.x, y: a.y }]) {
                    if ((corner.x !== a.x && corner.y !== a.y) || !runIsClear(a, corner)
                            || !runIsClear(corner, d)) continue;
                    route.splice(index + 1, 2, corner);
                    changed = true;
                    break;
                }
                if (changed) break;
            }
        }
        return route;
    };
    let cardCables = 0;
    /**
     * Where a cable may leave one end, for the router: from the card's
     * own port lamp straight down into the gap below the card (or below
     * the plank, on a rack top), or, for a card without that lamp, from
     * its bottom or right side. Downward exits stay inside the 16 px gap
     * between room cards (racks.css), staggered 2 px per cable (four steps,
     * 5-11 px) so runs do not merge. The deepest stays a pixel clear of
     * the router's 4 px margin round the next card down: an exit on
     * that margin is blocked or not by sub-pixel rounding, and the
     * cable would vanish at some window sizes. Side exits step outward
     * 4 px per cable.
     *
     * @param {HTMLElement} card - The card at this end.
     * @param {HTMLElement|null} lamp - Its own lamp for the port, if any.
     * @param {number} index - How many cables were routed before this one.
     * @returns {Array<object>} Socket ({x, y}) and exit ({exitX, exitY}) options.
     */
    const exitsFor = (card, lamp, index) => {
        const c = center(card);
        const drop = 5 + (index % 4) * 2;
        if (lamp) {
            const l = center(lamp);
            const plank = plankBelow(card);
            const below = (plank ? center(plank).bottom + 4 : c.bottom) + drop;
            return [{ x: l.x, y: l.bottom + 2, exitX: l.x, exitY: below }];
        }
        const right = c.x + c.halfWidth;
        return [
            { x: c.x, y: c.bottom, exitX: c.x, exitY: c.bottom + drop },
            { x: right, y: c.y, exitX: right + 10 + index * 4, exitY: c.y },
        ];
    };
    // Runs already drawn by routed cables, so a new cable does not lie
    // on top of one: [{vertical, at, from, to}].
    const usedRuns = [];
    const overlaps = (vertical, at, from, to) => usedRuns.some((run) => run.vertical === vertical
        && Math.abs(run.at - at) < 2 && Math.min(run.to, to) - Math.max(run.from, from) > 2);
    // Shift each inner run (not the ones touching a socket) 4 px at a
    // time until it lies on no earlier cable's run and stays clear.
    const separate = (points) => {
        const route = points.map((point) => ({ ...point }));
        for (let index = 1; index + 2 < route.length; index++) {
            const a = route[index], b = route[index + 1];
            const vertical = a.x === b.x;
            for (let step = 1; step <= 6; step++) {
                const at = vertical ? a.x : a.y;
                const [from, to] = vertical ? [Math.min(a.y, b.y), Math.max(a.y, b.y)]
                    : [Math.min(a.x, b.x), Math.max(a.x, b.x)];
                if (!overlaps(vertical, at, from, to)) break;
                const moved = vertical ? [{ ...a, x: a.x + 4 }, { ...b, x: b.x + 4 }]
                    : [{ ...a, y: a.y + 4 }, { ...b, y: b.y + 4 }];
                const before = route[index - 1], after = route[index + 2];
                if (!runIsClear(before, moved[0]) || !runIsClear(moved[0], moved[1])
                        || !runIsClear(moved[1], after)) break;
                route[index] = moved[0];
                route[index + 1] = moved[1];
                a.x = moved[0].x; a.y = moved[0].y; b.x = moved[1].x; b.y = moved[1].y;
            }
        }
        for (let index = 1; index < route.length; index++) {
            const a = route[index - 1], b = route[index];
            const vertical = a.x === b.x;
            usedRuns.push({ vertical, at: vertical ? a.x : a.y,
                from: Math.min(vertical ? a.y : a.x, vertical ? b.y : b.x),
                to: Math.max(vertical ? a.y : a.x, vertical ? b.y : b.x) });
        }
        return route;
    };
    // preferRight: false takes the shortest clear route (patch cables
    // start at the page's right edge, where "the right" is outside it).
    const routeBetween = (fromOptions, toOptions, { preferRight = true } = {}) => {
        if (typeof wireRoute !== "function") return null;
        let best = null;
        const span = (points) => points.slice(1).reduce((sum, point, index) => sum
            + Math.abs(point.x - points[index].x) + Math.abs(point.y - points[index].y), 0);
        for (const a of fromOptions) {
            for (const b of toOptions) {
                const start = { x: a.exitX, y: a.exitY };
                const end = { x: b.exitX, y: b.exitY };
                // Like Equipment's rack channels, cables run down the
                // right: try with the left of both ends closed first, and
                // go left only when the right is impossible or more than
                // twice as long.
                const leftEdge = Math.min(a.x, b.x, a.exitX, b.exitX);
                const cardLeft = Math.min(...obstacleBoxes().filter((item) => [a, b].some((end) => (
                    end.x >= item.left && end.x <= item.right
                    && end.y >= item.top - 4 && end.y <= item.bottom + 4))).map((item) => item.left), leftEdge);
                const open = wireRoute(start, end, obstacleBoxes());
                const rightward = wireRoute(start, end, [...obstacleBoxes(),
                    { left: -100000, right: cardLeft - 10, top: -100000, bottom: 100000 }]);
                const route = preferRight && rightward.length && (!open.length || span(rightward) <= span(open) * 2)
                    ? rightward : open;
                if (!route.length) continue;
                // A fixed lead-in (a rack-top lamp to its channel) joins
                // each socket to its exit before the routed part.
                const points = [a, ...(a.lead || []), ...straighten(route),
                    ...[...(b.lead || [])].reverse(), b];
                const length = points.slice(1).reduce((sum, point, index) => sum
                    + Math.abs(point.x - points[index].x) + Math.abs(point.y - points[index].y), 0);
                // Fewer bends break ties between equally short routes.
                const score = length + points.length * 2;
                if (!best || score < best.score) best = { points, score };
            }
        }
        if (!best) return null;
        cardCables += 1;
        const tail = best.points.at(-1).tail || "";
        return wirePath(separate(best.points), false) + tail;
    };
    /**
     * Exit options for one end. A rack-top card's lamp keeps the rack-top
     * rule: down under the plank, then out to its own lane in the channel
     * right of the rack, where the routed part begins.
     */
    const endOptions = (card, lamp) => {
        const frames = [...(card.closest(".rack-block")?.querySelectorAll(".rack-frame") || [])];
        const onTop = card.classList.contains("rack-top-item") || Boolean(card.closest?.(".rack-top-patch"));
        if (lamp && onTop && frames.length) {
            const l = center(lamp);
            const plank = plankBelow(card);
            const runY = plank ? trayY(plank) : center(card).bottom + 8;
            const laneX = channelX(frames.at(-1));
            return [{ x: l.x, y: l.bottom + 2, lead: [{ x: l.x, y: runY }], exitX: laneX, exitY: runY }];
        }
        return exitsFor(card, lamp, cardCables);
    };
    /**
     * Into a rack: down the device's own lane beside the frame, then in
     * under its lamp, as rack cables enter.
     *
     * @param {HTMLElement} pin - The rack-face port lamp.
     * @returns {Array<object>|null} One end option, or null off a rack face.
     */
    const rackEntry = (pin) => {
        const frame = pin.closest(".rack-frame");
        const deviceCard = pin.closest(".rack-device, .rack-shared-device");
        if (!frame || !deviceCard) return null;
        const p = center(pin);
        const laneX = channelX(frame);
        const exitY = app.nextExit(deviceCard, center(deviceCard).bottom);
        return [{ x: laneX, y: exitY, exitX: laneX, exitY, tail: ` H ${p.x} V ${p.bottom + 2}` }];
    };
    const routeBetweenCards = (fromCard, toCard, fromLamp = null, toLamp = null) => (
        routeBetween(endOptions(fromCard, fromLamp), endOptions(toCard, toLamp))
    );
    return {
        cardFor, pinFor, lampFor, plankBelow, channelX, trayY, visibleInRack, center, edgeToward, obstacleBoxes, runIsClear, straighten, exitsFor, overlaps, separate, routeBetween, endOptions, rackEntry, routeBetweenCards,
        box, cableIndex: () => cardCables,
    };
}

/** Cables methods of RackViewApp; never constructed itself. */
class RackCables {
    /** Rebuild the shared port-to-cable index (DeviceDetails.index). */
    indexCables() {
        this.details.index();
    }

    /**
     * Return the cable plugged into one port, if a link was observed.
     *
     * @param {string} nodeId - Device node ID.
     * @param {string} portName - Port name on that device.
     * @returns {{link: object, otherId: string, otherPort: string, otherName: string}|undefined}
     */
    cableAt(nodeId, portName) {
        return this.details.cableAt(nodeId, portName);
    }

    /**
     * Redraw the cable layer once layout has settled.
     *
     * Uses the next animation frame, or a zero-delay timer when the page is
     * hidden: browsers do not run animation frames in background tabs.
     *
     * @returns {void}
     */
    queueCables() {
        if (this.cablePending) return;
        this.cablePending = true;
        const draw = () => {
            this.cablePending = false;
            this.drawCables();
            this.markPatchHighlight();
            this.markCableTrace();
        };
        if (document.hidden) setTimeout(draw, 0);
        else requestAnimationFrame(draw);
    }

    /**
     * Draw every observed cable whose two ends are both on screen.
     *
     * Each cable drops below the device into the gap between rack units,
     * then follows a dedicated channel outside the chassis. Connections
     * between faces cross the reserved band above both frames.
     * Color is the cable medium, thickness the link speed; ambiguous links
     * and unknown media are dashed. With a device selected, its cables stay
     * bright and the rest dim.
     *
     * @returns {void}
     */
    /**
     * Next free lane in the cable channel right of a rack face, shared by
     * every layer that runs there (rack cables, room-card cables, router
     * entries, USB and Wi-Fi lines), so no two take the same lane (owner);
     * lanes are 3 px apart, RACK_CHANNEL_POSITIONS (16) of them before the
     * stacking rotates. Reset on each redraw (drawCables).
     *
     * @param {HTMLElement} frame - A `.rack-frame`.
     * @returns {number} Lane index from the face's edge.
     */
    nextChannel(frame) {
        this.channelUse ||= wireLaneCounter();
        return this.channelUse(frame) % RACK_CHANNEL_POSITIONS;
    }

    /**
     * Height where a cable meets a rack device from below. Every run into
     * one device shares it, the middle of the gap under the device
     * (RACK_DEVICE_GAP), so wires converging on a device condense into one
     * tight bundle that fits between it and the device below, with equal
     * space above and below (owner). The bundle separates where it reaches
     * its own lane in the side channel (nextChannel) or the top tray.
     *
     * @param {HTMLElement} device - A rack faceplate.
     * @param {number} bottom - The device's bottom edge in layer coordinates.
     * @returns {number} The meeting height.
     */
    nextExit(device, bottom) {
        return bottom + RACK_DEVICE_GAP / 2;
    }

    /**
     * Draw every network cable between rack-face port lamps, with its moving
     * pulses: each run leaves under its lamp, takes a lane in the channel right
     * of its rack (or between the faces, or overhead between racks), and enters
     * the other device on its lane. Lanes reset on each redraw; the selected
     * device's cables are drawn on top.
     */
    drawCables() {
        this.channelUse = wireLaneCounter();
        const container = document.getElementById("rack-room-racks");
        if (!container) return;
        container.querySelector(".rack-cable-layer")?.remove();
        container.querySelector(".rack-flow-layer")?.remove();
        if (this.wiringView) {
            document.querySelector(".rack-surface-uplink-layer")?.remove();
            this.roomWiring?.draw();
            return;
        }
        this.drawSurfaceUplinks();
        if (!this.links?.length) return;
        const box = container.getBoundingClientRect();
        const ns = "http://www.w3.org/2000/svg";
        const svg = document.createElementNS(ns, "svg");
        svg.setAttribute("class", "rack-cable-layer");
        svg.setAttribute("width", String(container.clientWidth));
        svg.setAttribute("height", String(container.scrollHeight));
        svg.setAttribute("aria-hidden", "true");
        // Pulses get their own layer, faded and glowing once as a whole
        // (racks.css), so pulses stacked in a bundle never add up.
        const flows = document.createElementNS(ns, "svg");
        flows.setAttribute("class", "rack-flow-layer");
        flows.setAttribute("width", String(container.clientWidth));
        flows.setAttribute("height", String(container.scrollHeight));
        flows.setAttribute("aria-hidden", "true");
        const portOf = (nodeId, name) => this.nodes.find((node) => node.id === nodeId)
            ?.ports.find((port) => port.name === name);
        const anchor = (nodeId, portName) => {
            const pin = container.querySelector(`[data-pin="${CSS.escape(`${nodeId}::${portName}`)}"]`);
            const frame = pin?.closest(".rack-frame");
            const card = pin?.closest(".rack-device, .rack-shared-device");
            if (!pin || !frame || !card) return null;
            const p = pin.getBoundingClientRect();
            const f = frame.getBoundingClientRect();
            const c = card.getBoundingClientRect();
            const clip = card.closest(".rack-block").getBoundingClientRect();
            // Horizontally scrolled-out equipment has no visible cable anchor.
            if (p.left < clip.left || p.right > clip.right) return null;
            // Devices sharing a 1U shelf are neighbours; note the shelf.
            const shelf = card.classList.contains("rack-shared-device")
                ? card.closest(".rack-shared-shelf") : null;
            return {
                x: p.left + p.width / 2 - box.left,
                y: p.bottom + 2 - box.top,
                exitY: c.bottom + 1 - box.top,
                card,
                bottom: c.bottom - box.top,
                edge: f.right - box.left,
                top: f.top - box.top,
                frame,
                shelf,
                shelfBottom: shelf ? shelf.getBoundingClientRect().bottom - box.top : null,
            };
        };
        const down = this.downNodeIds();
        const visible = this.links.filter((link) => !this.cableIsDown(link, down)).map((link) => ({
            link, a: anchor(link.from, link.from_port), b: anchor(link.to, link.to_port),
        })).filter(({ a, b }) => a && b);
        // Lanes in each face's channel are shared with the room-card layer.
        const nextLane = (frame) => this.nextChannel(frame);
        const fmt = (value) => value.toFixed(1);
        let overhead = 0;
        for (const { link, a, b } of visible) {
            // Each cable leaving a device steps below the last (nextExit).
            a.exitY = this.nextExit(a.card, a.bottom);
            b.exitY = this.nextExit(b.card, b.bottom);
            const ports = [portOf(link.from, link.from_port), portOf(link.to, link.to_port)];
            // A port's own medium first, else the cable's (the API may take
            // it from an identical twin cable: medium_inferred_from).
            const medium = ports.find((port) => port?.medium)?.medium || link.medium || "unknown";
            const speed = Math.max(...ports.map((port) => Number(port?.speed_mbps) || 0));
            // Every path drops below its device before moving sideways, so no
            // run crosses a device name or another socket in the port row.
            let d;
            if (a.shelf && a.shelf === b.shelf) {
                // Neighbours on one shared shelf: just under its lip, straight
                // across to the other device, not out to the side channel.
                const under = a.shelfBottom + 4;
                d = `M ${fmt(a.x)} ${fmt(a.y)} V ${fmt(under)} H ${fmt(b.x)} V ${fmt(b.y)}`;
            } else if (a.frame === b.frame) {
                // Same face: out to the channel on the frame's right.
                const lane = a.edge + 12 + nextLane(a.frame) * 3;
                d = `M ${fmt(a.x)} ${fmt(a.y)} V ${fmt(a.exitY)} H ${fmt(lane)}`
                    + ` V ${fmt(b.exitY)} H ${fmt(b.x)} V ${fmt(b.y)}`;
            } else if (a.frame.parentElement === b.frame.parentElement) {
                // Front to rear of one rack: down the channel between the two
                // faces, then in from the left under the far device.
                const [left, right] = a.edge < b.edge ? [a, b] : [b, a];
                const lane = left.edge + 12 + nextLane(left.frame) * 3;
                d = `M ${fmt(left.x)} ${fmt(left.y)} V ${fmt(left.exitY)} H ${fmt(lane)}`
                    + ` V ${fmt(right.exitY)} H ${fmt(right.x)} V ${fmt(right.y)}`;
            } else {
                // Different racks: over the top of both.
                const aLane = a.edge + 12 + nextLane(a.frame) * 3;
                const bLane = b.edge + 12 + nextLane(b.frame) * 3;
                const over = Math.min(a.top, b.top) - 6 - overhead * 3;
                overhead += 1;
                d = `M ${fmt(a.x)} ${fmt(a.y)} V ${fmt(a.exitY)} H ${fmt(aLane)}`
                    + ` V ${fmt(over)} H ${fmt(bLane)}`
                    + ` V ${fmt(b.exitY)} H ${fmt(b.x)} V ${fmt(b.y)}`;
            }
            const path = document.createElementNS(ns, "path");
            rackSetCablePath(path, d);
            path.dataset.medium = medium;
            rackTagCableEnds(path, link);
            path.style.strokeWidth = String(wireSpeedStyle(speed).width);
            path.classList.toggle("is-tentative", link.confidence === "ambiguous" || medium === "unknown");
            path.classList.toggle("is-new", Boolean(this.recentChanges?.links.has(rackLinkKey(link))
                && performance.now() < this.recentChanges.until));
            const involved = !this.selectedId || link.from === this.selectedId || link.to === this.selectedId;
            if (this.selectedId) path.classList.add(involved ? "is-active" : "is-dim");
            svg.append(path);
            // A cable whose two ports are both up carries moving pulses.
            const live = ports.every((port, index) => port && this.portAlarmState(
                port, index === 0 ? link.from : link.to) === "up");
            if (live && involved && !path.classList.contains("is-tentative")) {
                const flow = document.createElementNS(ns, "path");
                rackSetCablePath(flow, d);
                flow.setAttribute("class", "rack-cable-flow");
                flow.dataset.medium = medium;
                rackTagCableEnds(flow, link);
                flow.style.strokeWidth = path.style.strokeWidth;
                flow.style.animationDuration = `${wireSpeedStyle(speed).flow}s`;
                flows.append(flow);
            }
        }
        // The selected device's cables go on top, so dimmed runs sharing a
        // lane (a BMC's beside its host's NIC) never cover them.
        for (const active of [...svg.querySelectorAll("path.is-active")]) svg.append(active);
        container.append(svg);
        if (flows.childElementCount) container.append(flows);
    }

    /**
     * Draw cables that have an end without a rack-face port pin (equipment
     * on a rack top, the floor, or another room surface: AP and switch
     * uplinks, WAN upstream modems, a NUT server's inferred wired NIC), and
     * each NUT server's USB line to the UPS it reads.
     *
     * Equipment on rack tops, floors, and room surfaces has normal discovered
     * physical ports and cable links, but its card is not on a rack face. A
     * rack-mounted switch keeps its pin as the cable endpoint; a rack-top
     * item uses its own port lamp, with the run dropping just below its
     * plank; any other card uses its edge. Only links whose two devices are
     * explicitly placed in this room are shown. Links with two rack pins
     * remain in the usual cable layer and are not drawn twice.
     */
    drawSurfaceUplinks() {
        const root = document.querySelector(".rack-main-column");
        if (!root) return;
        root.querySelector(".rack-surface-uplink-layer")?.remove();
        const nodes = new Map(this.nodes.map((node) => [node.id, node]));
        const box = root.getBoundingClientRect();
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("class", "rack-surface-uplink-layer");
        svg.setAttribute("width", String(root.clientWidth));
        svg.setAttribute("height", String(root.scrollHeight));
        svg.setAttribute("aria-hidden", "true");
        const routing = rackSurfaceRouting(this, root, box);
        const {
            cardFor, pinFor, lampFor, plankBelow, channelX, trayY, visibleInRack, center, edgeToward, exitsFor, routeBetween, endOptions, rackEntry,
        } = routing;
        let lane = 0;
        const down = this.downNodeIds();
        for (const link of this.links || []) {
            if (this.cableIsDown(link, down)) continue;
            const wanUpstream = link.evidence === "unifi-wan-upstream" && link.confidence === "inferred";
            const left = nodes.get(link.from);
            const right = nodes.get(link.to);
            if (!left?.placement || !right?.placement
                    || left.placement.container_room_id !== this.roomId
                    || right.placement.container_room_id !== this.roomId) continue;
            // Cables with a rack-face pin at both ends belong to the rack layer.
            const leftPin = pinFor(left.id, link.from_port);
            const rightPin = pinFor(right.id, link.to_port);
            if (leftPin && rightPin) continue;
            // The "surface" end is the one without a rack-face pin.
            const surfaceNode = leftPin ? right : left;
            const surfacePort = surfaceNode === left ? link.from_port : link.to_port;
            const peer = surfaceNode === left ? right : left;
            const peerPort = surfaceNode === left ? link.to_port : link.from_port;
            const apCard = cardFor(surfaceNode.id);
            const peerPin = pinFor(peer.id, peerPort);
            const peerCard = cardFor(peer.id);
            if (!apCard || !peerPin && !peerCard) continue;
            if (!visibleInRack(apCard) || peerPin && !visibleInRack(peerPin)
                    || peerCard && !visibleInRack(peerCard)) continue;
            const d = this.surfaceCableRoute(routing, {
                surfaceNode, surfacePort, peer, peerPort, apCard, peerPin, peerCard,
            });
            if (!d) continue;
            const ports = [[surfaceNode, surfacePort], [peer, peerPort]].map(([node, name]) =>
                node.ports.find((port) => port.name === name));
            const path = document.createElementNS(svg.namespaceURI, "path");
            rackSetCablePath(path, d);
            path.dataset.medium = ports.find((port) => port?.medium)?.medium || link.medium || "unknown";
            rackTagCableEnds(path, link);
            path.classList.toggle("is-inferred-wan", wanUpstream);
            path.classList.toggle("is-tentative", link.confidence === "ambiguous");
            path.classList.toggle("is-dim", Boolean(this.selectedId
                && this.selectedId !== surfaceNode.id && this.selectedId !== peer.id));
            svg.append(path);
        }
        this.drawPatchCables(svg, { root, box, center, cardFor, pinFor, lampFor, visibleInRack,
            exitsFor, endOptions, routeBetween, rackEntry, trayY, cableIndex: routing.cableIndex });
        this.drawUsbLinks(svg, { root, nodes, box, center, edgeToward, plankBelow, nextLane: () => lane++,
            routeBetween, endOptions, visibleInRack, rackEntry, trayY, channelX });
        this.drawWifiLinks(svg, { root, box, center, nextLane: () => lane++, routeBetween, endOptions,
            visibleInRack, rackEntry, trayY, channelX });
        if (svg.childElementCount) root.append(svg);
    }

    /**
     * Route one surface cable: from a rack-top or room card to a rack-face
     * pin or another card. Room cards route clear of everything like the
     * Diagram; a rack-top card drops over its plank and down the channel;
     * two lamps on one plank run straight across under it.
     *
     * @param {object} routing - The draw's shared helpers (rackSurfaceRouting).
     * @param {object} ends - The cable's two ends.
     * @param {object} ends.surfaceNode - The device on the surface.
     * @param {string} ends.surfacePort - Its port.
     * @param {object} ends.peer - The device at the other end.
     * @param {string} ends.peerPort - Its port.
     * @param {HTMLElement} ends.apCard - The surface device's card.
     * @param {?HTMLElement} ends.peerPin - The peer's rack-face lamp, if any.
     * @param {?HTMLElement} ends.peerCard - The peer's card, if any.
     * @returns {?string} Square SVG path data, or null when it cannot be drawn.
     */
    surfaceCableRoute(routing, { surfaceNode, surfacePort, peer, peerPort, apCard, peerPin, peerCard }) {
        const {
            center, lampFor, rackEntry, routeBetween, exitsFor, routeBetweenCards, channelX, edgeToward, plankBelow,
            trayY, box
        } = routing;
        const a = center(apCard);
        let d;
        const roomEnd = apCard.classList.contains("rack-room-item")
            || Boolean(peerCard?.classList.contains("rack-room-item"));
        if (roomEnd) {
            // A room card (around the room): leave its own lamp and route
            // clear of every card, frame, and header, like the Diagram.
            const ownLamp = lampFor(apCard, surfaceNode.id, surfacePort);
            if (peerPin) {
                const entry = rackEntry(peerPin);
                if (entry) d = routeBetween(exitsFor(apCard, ownLamp, routing.cableIndex()), entry);
            } else {
                d = routeBetweenCards(apCard, peerCard, ownLamp, lampFor(peerCard, peer.id, peerPort));
            }
        }
        if (d) {
            // Routed above.
        } else if (peerPin) {
            const frame = peerPin.closest(".rack-frame");
            const deviceCard = peerPin.closest(".rack-device, .rack-shared-device");
            if (!frame || !deviceCard) return null;
            const p = center(peerPin);
            const frameRect = frame.getBoundingClientRect();
            const b = center(deviceCard);
            const laneX = channelX(frame);
            // A rack-top cable leaves its own port lamp and drops over the
            // front of the plank the card rests on.
            const ownPin = apCard.querySelector?.(
                `[data-pin="${CSS.escape(`${surfaceNode.id}::${surfacePort}`)}"]`,
            );
            const start = apCard.classList.contains("rack-top-item")
                ? (ownPin ? { x: center(ownPin).x, y: center(ownPin).bottom + 2 } : { x: a.x, y: a.bottom })
                : edgeToward(a, { x: laneX, y: a.y });
            // Its run over the plank takes the next free tray height.
            const ownPlank = plankBelow(apCard);
            const overY = ownPlank ? trayY(ownPlank) : Math.max(start.y + 8, frameRect.top - box.top - 8);
            d = `M ${start.x} ${start.y} V ${overY} H ${laneX}`
                + ` V ${this.nextExit(deviceCard, b.bottom)} H ${p.x} V ${p.bottom + 2}`;
        } else {
            const b = center(peerCard);
            if (a.x === b.x && a.y === b.y) return null;
            const startLamp = lampFor(apCard, surfaceNode.id, surfacePort);
            const endLamp = lampFor(peerCard, peer.id, peerPort);
            if (startLamp || endLamp) {
                // Leave the lamp downward, run just below the plank, then
                // meet the other end (its lamp, or the nearer card edge).
                const lamp = startLamp || endLamp;
                const lampCard = startLamp ? apCard : peerCard;
                const other = startLamp ? b : a;
                const otherLamp = startLamp ? endLamp : startLamp;
                const from = { x: center(lamp).x, y: center(lamp).bottom + 2 };
                const plank = plankBelow(lampCard);
                const runY = plank ? trayY(plank) : center(lampCard).bottom + 8;
                const frames = [...(lampCard.closest(".rack-block")?.querySelectorAll(".rack-frame") || [])];
                if (otherLamp) {
                    const to = { x: center(otherLamp).x, y: center(otherLamp).bottom + 2 };
                    d = `M ${from.x} ${from.y} V ${runY} H ${to.x} V ${to.y}`;
                } else if (frames.length && other.top > runY) {
                    // Down the cable channel right of the rack, like the
                    // rack's own cables, then across into the card's top.
                    const laneX = channelX(frames.at(-1));
                    const approachY = Math.max(runY, other.top - 10);
                    d = `M ${from.x} ${from.y} V ${runY} H ${laneX} V ${approachY}`
                        + ` H ${other.x} V ${other.top}`;
                } else {
                    const to = edgeToward(other, { x: other.x, y: runY });
                    d = `M ${from.x} ${from.y} V ${runY} H ${to.x} V ${to.y}`;
                }
            } else {
                // Two room cards: route around everything in between,
                // never straight across another card.
                const start = edgeToward(a, b);
                const end = edgeToward(b, a);
                d = routeBetweenCards(apCard, peerCard)
                    || `M ${start.x} ${start.y} L ${end.x} ${end.y}`;
            }
        }
        return d;
    }

    /**
     * Draw each patch panel socket's cable to the local port it leaves from
     * (see renderPatchBox). On a rack top a cable leaves like rack-top
     * equipment's (over the plank, out to the channel right of the rack);
     * in the side box it drops straight out under the panel. Then it is
     * routed like an "Around the room" cable: clear of cards, frames, and
     * panels, into a rack device on its lane, or to a card's own lamp. Ambiguous
     * cables stay tentative; each path carries `data-patch` (its endpoint
     * key) so the open socket's cable can be highlighted.
     *
     * @param {SVGSVGElement} svg - The surface layer being built.
     * @param {object} tools - Shared geometry and routing from `drawSurfaceUplinks`.
     * @returns {void}
     */
    drawPatchCables(svg, { root, box, center, cardFor, pinFor, lampFor, visibleInRack, trayY,
        exitsFor, endOptions, routeBetween, rackEntry, cableIndex }) {
        if (this.wiringView || !this.patchBox?.face?.isConnected) return;
        const face = center(this.patchBox.face);
        const panelRack = this.patchBox.face.closest(".rack-block");
        const plank = panelRack?.querySelector(".rack-top-plank");
        // A down cable (either device or end port down) is not drawn.
        const down = this.downNodeIds();
        const records = [...this.patchBox.sockets.values()]
            .filter((record) => !this.cableIsDown(record.exit.link, down))
            // A socket scrolled out of a narrow panel's rack-top row has no cable.
            .filter((record) => visibleInRack(record.lamp));
        // Cables to a device on the panel's own rack take fixed runs out of
        // the left side of its face (see leftSideRuns); the rest are routed.
        const leftSide = new Map();
        for (const record of records) {
            const pin = pinFor(record.exit.local.id, record.exit.localPort);
            const frame = pin?.closest(".rack-frame");
            if (plank && frame && frame.closest(".rack-block") === panelRack && visibleInRack(pin)) {
                leftSide.set(record, { pin, frame });
            }
        }
        const fixed = this.leftSideRuns(leftSide, { box, center, plank, trayY });
        for (const record of records) {
            const { link, local, localPort } = record.exit;
            let d = fixed.get(record);
            if (!d) {
                const lamp = center(record.lamp);
                // On a rack top: over the plank and out to the rack's channel,
                // like rack-top equipment. In the side box: straight down the
                // socket's column, out under the panel.
                const below = face.bottom + 6 + (cableIndex() % 3) * 2;
                const start = panelRack ? endOptions(record.socket, record.lamp)
                    : [{ x: lamp.x, y: lamp.bottom + 2, exitX: lamp.x, exitY: below }];
                const pin = pinFor(local.id, localPort);
                const card = pin ? null : cardFor(local.id);
                if (pin ? !visibleInRack(pin) : !card || !visibleInRack(card)) continue;
                const end = pin ? rackEntry(pin) : endOptions(card, lampFor(card, local.id, localPort));
                d = end && routeBetween(start, end, { preferRight: Boolean(panelRack) });
            }
            if (!d) continue;
            const port = local.ports?.find((item) => item.name === localPort);
            const path = document.createElementNS(svg.namespaceURI, "path");
            rackSetCablePath(path, d);
            path.dataset.medium = port?.medium || link.medium || "unknown";
            path.dataset.patch = rackLinkKey(link);
            rackTagCableEnds(path, link);
            path.classList.add("is-patch-cable");
            path.classList.toggle("is-tentative", link.confidence === "ambiguous");
            path.classList.toggle("is-dim", Boolean(this.selectedId && this.selectedId !== local.id));
            svg.append(path);
        }
    }

    /**
     * Fixed runs for patch cables between the rack-top panel and a device on
     * that rack's own faces (owner's choice: they leave the rack's left
     * side, which keeps them out of the crowded channel on the right). Each
     * drops from its socket over the front of the plank, runs along under
     * it to a lane just left of the device's face (on a rear face, the gap
     * between the faces), down that lane, and in under the device to its
     * port lamp, as rack cables enter.
     *
     * Lanes follow depth so the runs under the devices never cross a lane:
     * the shallowest device's cable takes the lane nearest the face, the
     * deepest the outermost, and two cables to one device are staggered
     * 2 px (the outer lane lower). Under the plank, a deeper cable heading
     * left runs higher, one heading right (to a rear face) runs lower, so
     * the runs along the plank do not cross the lanes either; left-going
     * runs sit above right-going ones.
     *
     * @param {Map<object, {pin: HTMLElement, frame: HTMLElement}>} ends - Socket record to its rack pin.
     * @param {object} tools - `box`, `center`, and the panel's `plank`.
     * @returns {Map<object, string>} Socket record to SVG path data.
     */
    leftSideRuns(ends, { box, center, plank, trayY }) {
        const runs = new Map();
        if (!ends.size) return runs;
        const plankBottom = center(plank).bottom;
        const geometry = [...ends].map(([record, { pin, frame }]) => {
            const lamp = center(record.lamp);
            const left = frame.getBoundingClientRect().left - box.left;
            const device = pin.closest(".rack-device, .rack-shared-device") || pin;
            return { record, lamp, pin: center(pin), frame, device, left,
                deviceBottom: center(device).bottom, leftward: left < lamp.x };
        });
        // Per face, shallowest first (then by port position): lane 0 is
        // nearest the face. Cables to one device meet it in one tight bundle.
        const byFrame = new Map();
        for (const item of geometry) byFrame.set(item.frame, [...(byFrame.get(item.frame) || []), item]);
        for (const items of byFrame.values()) {
            items.sort((a, b) => a.deviceBottom - b.deviceBottom || a.pin.x - b.pin.x);
            items.forEach((item, rank) => {
                item.rank = rank;
                item.laneX = item.left - 6 - rank * 3;
                // Into the device's tight bundle with every other run to it (nextExit).
                item.exitY = this.nextExit(item.device, item.deviceBottom);
            });
        }
        // Run heights under the plank, top down: left-going cables deepest
        // (outermost lane) first, then right-going ones shallowest first.
        const leftward = geometry.filter((item) => item.leftward).sort((a, b) => b.rank - a.rank);
        const rightward = geometry.filter((item) => !item.leftward).sort((a, b) => a.rank - b.rank);
        // Heights come from the shared top tray (trayY), so these never sit
        // on another run. The tray fills from the bottom up, so claim them in
        // reverse: the first in this top-down order still gets the highest.
        [...leftward, ...rightward].reverse().forEach((item, index, list) => {
            item.runY = trayY ? trayY(plank) : plankBottom + 4 + (list.length - 1 - index) * 3;
        });
        for (const item of geometry) {
            runs.set(item.record, `M ${item.lamp.x} ${item.lamp.bottom + 2} V ${item.runY} H ${item.laneX}`
                + ` V ${item.exitY} H ${item.pin.x} V ${item.pin.bottom + 2}`);
        }
        return runs;
    }

    /**
     * The UPS links in this room: each UPS that a NUT server reads over USB
     * or relays (`ups.attachment`, `server_node_id`), with both placed here.
     *
     * @returns {{ups: object, server: object, kind: string}[]} One per link.
     */
    upsLinks() {
        const nodes = new Map(this.nodes.map((node) => [node.id, node]));
        const kinds = { usb: "usb", relayed: "relay" };
        const links = [];
        for (const ups of this.nodes) {
            const reading = (ups.observations || []).map((item) => item.ups)
                .find((item) => kinds[item?.attachment] && item.server_node_id);
            const server = reading ? nodes.get(reading.server_node_id) : null;
            if (!server || server.id === ups.id
                    || ups.placement?.container_room_id !== this.roomId
                    || server.placement?.container_room_id !== this.roomId) continue;
            links.push({ ups, server, kind: kinds[reading.attachment] });
        }
        return links;
    }

    /**
     * Add a non-network port lamp to every card of a device (UPS and Wi-Fi
     * links), at the right end of its port row, made if the card has none.
     * On a rack face it shows only on the face its ports are on, like its
     * network lamps (portFacing; USB-style, so a full-depth UPS is rear).
     *
     * @param {object} node - Device the lamp belongs to.
     * @param {string} className - Lamp class (`rack-ups-port`, `rack-wifi-port`).
     * @param {Object<string, string>} data - `data-*` values for the lamp;
     *     its `pin` is the trace key markCableTrace matches, like a port's.
     * @param {string} title - Tooltip.
     * @param {{once?: boolean}} [options] - Skip a card that already has one.
     * @returns {void}
     */
    addSidePort(node, className, data, title, { once = false } = {}) {
        const root = document.querySelector(".rack-main-column");
        if (!root) return;
        const id = CSS.escape(node.id);
        for (const card of root.querySelectorAll(`.rack-frame .rack-device[data-node="${id}"], `
                + `.rack-frame .rack-shared-device[data-node="${id}"], `
                + `.rack-top-item[data-node="${id}"], .rack-room-item[data-node="${id}"]`)) {
            const frame = card.closest(".rack-frame");
            if (frame && frame.dataset.face !== this.portFacing(node, { usb: true })) continue;
            if (once && card.querySelector(`.${className}`)) continue;
            let lights = card.querySelector(":scope > .rack-port-lights");
            if (!lights) {
                lights = rackText("span", "rack-port-lights", "");
                lights.setAttribute("aria-label", "Connection ports");
                card.append(lights);
            }
            const lamp = rackText("span", `rack-port-light ${className}`, "");
            Object.assign(lamp.dataset, data);
            lamp.title = title;
            lights.append(lamp);
        }
    }

    /**
     * Give side links port lamps so their lines meet them like network cables
     * meet ports (owner). Each UPS link gets a yellow lamp at both ends
     * (solid USB, hollow "reports through"; `data-ups`). Each Wi-Fi client
     * gets a Wi-Fi lamp (`data-wifi-ap`, its AP), and each AP in this room
     * with a live client here gets one shared lamp that all their lines
     * meet (`data-role="ap"`). A client whose AP is elsewhere keeps its lamp
     * with no line, so nothing reaches the patch panel (owner).
     *
     * @returns {void}
     */
    attachSidePorts() {
        for (const { ups, server, kind } of this.upsLinks()) {
            for (const [node, other] of [[ups, server], [server, ups]]) {
                this.addSidePort(node, "rack-ups-port", {
                    ups: ups.id, kind, pin: `${node.id}::ups:${ups.id}`
                }, kind === "usb"
                    ? `USB · ${node === ups ? `read by ${other.name}` : `reads ${other.name}`}`
                    : `Reports through ${node === ups ? other.name : `this server: ${other.name}`}`);
            }
        }
        const byId = new Map(this.nodes.map((node) => [node.id, node]));
        const down = this.downNodeIds();
        const clientsOf = new Map();
        for (const node of this.nodes) {
            if (node.placement?.container_room_id !== this.roomId) continue;
            const wireless = this.wirelessFor(node);
            if (wireless?.state !== "connected" || !wireless.fresh) continue;
            const apId = wireless.ap_node_id || "";
            this.addSidePort(node, "rack-wifi-port", { wifiAp: apId, role: "client", pin: `${node.id}::wifi` },
                `Wi-Fi${wireless.ap_name ? ` · client of ${wireless.ap_name}` : ""}`);
            const ap = byId.get(apId);
            if (ap?.placement?.container_room_id === this.roomId && !down.has(ap.id) && !down.has(node.id)) {
                clientsOf.set(ap, [...(clientsOf.get(ap) || []), node.name]);
            }
        }
        for (const [ap, clients] of clientsOf) {
            this.addSidePort(ap, "rack-wifi-port", { wifiAp: ap.id, role: "ap", pin: `${ap.id}::wifi` },
                `Wi-Fi · ${clients.length} client${clients.length === 1 ? "" : "s"} here: ${clients.join(", ")}`,
                { once: true });
        }
    }

    /**
     * Route a side link from one port lamp to another, like a network cable:
     * both on one rack top, straight across just under its plank; both on
     * rack faces, down under each and along the channel right of the first's
     * rack; one on a rack face, into it with `rackEntry`; between cards, with
     * the room-card router.
     *
     * @param {HTMLElement} fromLamp - Start lamp.
     * @param {HTMLElement} toLamp - End lamp.
     * @param {object} tools - Geometry and routing from drawSurfaceUplinks.
     * @param {number} [shift] - Offset for a second line from the same card.
     * @returns {string|null} Path data, or null when no route fits.
     */
    lampRoute(
        fromLamp, toLamp, { box, center, nextLane, routeBetween, endOptions, rackEntry, trayY, channelX }, shift = 0
    ) {
        const onFace = (lamp) => Boolean(lamp.closest(".rack-frame"));
        const cardOf = (lamp) => lamp.closest("[data-node]");
        const top = fromLamp.closest(".rack-top-display");
        if (top && top === toLamp.closest(".rack-top-display")) {
            // Both on one rack top: down from each lamp to just under the
            // plank and straight across, never out to the rack's channel.
            const plank = top.querySelector(".rack-top-plank");
            const p = center(fromLamp);
            const q = center(toLamp);
            const y = plank && trayY ? trayY(plank) : Math.max(p.bottom, q.bottom) + 4 + nextLane() * 3 + shift / 2;
            return `M ${p.x} ${p.bottom} V ${y} H ${q.x} V ${q.bottom}`;
        }
        if (onFace(fromLamp) && onFace(toLamp)) {
            const p = center(fromLamp);
            const q = center(toLamp);
            const deviceA = fromLamp.closest(".rack-device, .rack-shared-device") || cardOf(fromLamp);
            const deviceB = toLamp.closest(".rack-device, .rack-shared-device") || cardOf(toLamp);
            const exitA = this.nextExit(deviceA, center(deviceA).bottom);
            const exitB = this.nextExit(deviceB, center(deviceB).bottom);
            const frame = fromLamp.closest(".rack-frame");
            const laneX = channelX ? channelX(frame)
                : frame.getBoundingClientRect().right - box.left + 12 + nextLane() * 4;
            return `M ${p.x} ${p.bottom} V ${exitA} H ${laneX} V ${exitB} H ${q.x} V ${q.bottom}`;
        }
        if (onFace(fromLamp) || onFace(toLamp)) {
            const [cardLamp, faceLamp] = onFace(toLamp) ? [fromLamp, toLamp] : [toLamp, fromLamp];
            const entry = rackEntry(faceLamp);
            return (entry && routeBetween(endOptions(cardOf(cardLamp), cardLamp), entry)) || null;
        }
        return routeBetween(endOptions(cardOf(fromLamp), fromLamp), endOptions(cardOf(toLamp), toLamp)) || null;
    }

    /**
     * Draw each Wi-Fi client's line to its access point, lamp to lamp out the
     * bottom like a network cable (owner): only when both are placed and
     * visible in this room and neither is down; never to a device elsewhere,
     * which would only fill the patch panel (owner). Several clients of one
     * AP meet at its one shared lamp. Not a cable; it never counts.
     *
     * @param {SVGSVGElement} svg - The surface layer being built.
     * @param {object} tools - Shared geometry and routing from drawSurfaceUplinks.
     * @returns {void}
     */
    drawWifiLinks(svg, tools) {
        const { root, visibleInRack } = tools;
        const down = this.downNodeIds();
        const inRoom = new Set(this.nodes.filter((node) => node.placement?.container_room_id === this.roomId)
            .map((node) => node.id));
        const lampOf = (nodeId, apId, role) => [...root.querySelectorAll(
            `.rack-wifi-port[data-wifi-ap="${CSS.escape(apId)}"][data-role="${role}"]`,
        )].find((lamp) => lamp.closest("[data-node]")?.dataset.node === nodeId && visibleInRack(lamp));
        for (const link of this.wirelessLinks || []) {
            if (!inRoom.has(link.from) || !inRoom.has(link.to) || down.has(link.from) || down.has(link.to)) continue;
            const client = lampOf(link.from, link.to, "client");
            const ap = lampOf(link.to, link.to, "ap");
            if (!client || !ap) continue;
            const d = this.lampRoute(client, ap, tools);
            if (!d) continue;
            const path = document.createElementNS(svg.namespaceURI, "path");
            rackSetCablePath(path, d);
            path.classList.add("is-wifi");
            path.dataset.pinA = `${link.from}::wifi`;
            path.dataset.pinB = `${link.to}::wifi`;
            path.classList.toggle("is-dim", Boolean(this.selectedId
                && this.selectedId !== link.from && this.selectedId !== link.to));
            svg.append(path);
        }
    }

    /**
     * Trace the cable on a port light under the pointer (`tracedPin`, set by
     * the hover listener in bindEvents): mark its cable and pulse paths, ring
     * the lamps at both ends, and dim every other cable, so both ends of the
     * wire are clear. Runs after each cable redraw, so a live update keeps
     * the trace while the pointer stays on the light.
     *
     * @returns {void}
     */
    markCableTrace() {
        const root = document.querySelector(".rack-main-column");
        if (!root) return;
        root.classList.remove("is-tracing");
        for (const element of root.querySelectorAll(".is-traced, .is-trace-end")) {
            element.classList.remove("is-traced", "is-trace-end");
        }
        const pin = this.wiringView ? null : this.tracedPin;
        if (!pin) return;
        const traced = [...root.querySelectorAll(".rack-cable-layer path, .rack-flow-layer path, .rack-surface-uplink-layer path")]
            .filter((path) => path.dataset.pinA === pin || path.dataset.pinB === pin);
        if (!traced.length) return;
        root.classList.add("is-tracing");
        const ends = new Set();
        for (const path of traced) {
            path.classList.add("is-traced");
            ends.add(path.dataset.pinA).add(path.dataset.pinB);
        }
        for (const end of ends) {
            for (const lamp of root.querySelectorAll(`.rack-port-light[data-pin="${CSS.escape(end)}"]`)) {
                lamp.classList.add("is-trace-end");
            }
        }
    }

    /**
     * While a patch socket's destination is open, mark exactly its cable and
     * its local device's port, and dim every other cable. The device card is
     * not outlined (owner): the bright cable already leads to it, and an
     * outline crossing the cable made it harder to follow. Runs after each
     * cable redraw.
     *
     * @returns {void}
     */
    markPatchHighlight() {
        const root = document.querySelector(".rack-main-column");
        if (!root) return;
        for (const element of root.querySelectorAll(".is-patch-highlighted, .is-patch-port")) {
            element.classList.remove("is-patch-highlighted", "is-patch-port");
        }
        const exit = this.wiringView ? null : this.patchBox?.open?.exit;
        if (!exit) return;
        for (const pin of root.querySelectorAll(`[data-pin="${CSS.escape(`${exit.local.id}::${exit.localPort}`)}"]`)) {
            pin.classList.add("is-patch-port");
        }
        const key = rackLinkKey(exit.link);
        for (const path of root.querySelectorAll(".rack-cable-layer path, .rack-flow-layer path, .rack-surface-uplink-layer path")) {
            const mine = path.dataset.patch === key;
            path.classList.toggle("is-patch-highlighted", mine);
            if (!mine) path.classList.add("is-dim");
        }
    }

    /**
     * Report a device's run state for its status lamp, from collector data:
     * offline when a source reports it so, stale when not observed for an
     * hour, online when a source says online or saw it within the fresh
     * window, and unknown for inventory with no observation.
     *
     * @param {object} node - Device node.
     * @returns {"online"|"offline"|"stale"|"unknown"} Lamp state.
     */
    /**
     * IDs of devices their source reports down (the faceplate's offline
     * state). Their cables are not drawn (owner): a dead box's wires only
     * add clutter. Port lamps and counts still show them.
     *
     * @returns {Set<string>} Down device IDs.
     */
    downNodeIds() {
        return new Set(this.nodes.filter((node) => ["offline", "standby"].includes(this.deviceState(node)))
            .map((node) => node.id));
    }

    /**
     * Whether a cable is down and so not drawn (owner): either device is
     * down (`down`, from downNodeIds), or either end's port is reported
     * down by its source (`link_up === false`). A port with no reading keeps
     * its cable, since missing data is not a dead link. The port's lamp
     * still shows (amber when it is an alarm).
     *
     * @param {{from: string, from_port: string, to: string, to_port: string}} link - Cable.
     * @param {Set<string>} down - Down device IDs.
     * @returns {boolean} True when the cable should not be drawn.
     */
    cableIsDown(link, down) {
        return [[link.from, link.from_port], [link.to, link.to_port]].some(([id, name]) => {
            const node = this.nodes.find((item) => item.id === id);
            // A powered-off host's BMC cable stays: the BMC still answers.
            const bmcLive = node && deviceManagement(node).ports.has(name) && deviceHostPower(node).bmcReachable;
            return (down.has(id) && !bmcLive) || node?.ports?.find((port) => port.name === name)?.link_up === false;
        });
    }

    /**
     * The patch panel's cables on Equipment: every cable leaving the room
     * (`roomPatchExits`) except down ones (`cableIsDown`), so a dead link
     * gets no socket or destination popup rather than an empty one (owner).
     *
     * @returns {object[]} Patch exits whose cable is drawn.
     */
    livePatchExits() {
        const down = this.downNodeIds();
        return roomPatchExits(this).filter((exit) => !this.cableIsDown(exit.link, down));
    }
}

rackAddMethods(RackCables);
