/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * @fileoverview Cable geometry shared by every page that draws wiring.
 *
 * DOM-free, so the Node checks in tests/js load it directly. Pages build a
 * cable as a list of square-cornered points and hand it here to become an
 * SVG path; the bend radius, the speed-to-line rules, and the obstacle
 * router live in this one place so every drawing of a cable agrees.
 *
 * Used by: Landing, Discovery, Equipment and Diagram, Locations map.
 */

/**
 * Convert an obstacle-avoiding route to square or gently curved cable bends.
 *
 * Curves stay within the router's clearance corridor. Short segments limit
 * the bend radius so nearby sockets and tight routes retain their endpoints.
 *
 * @param {object[]} points - Ordered socket and route coordinates.
 * @param {boolean} [curved=true] - Round bends when true; keep square bends
 *     otherwise.
 * @param {number} [bendRadius=12] - Maximum radius at the current zoom.
 * @param {?number} [precision=null] - Decimal places kept in the output
 *     (null keeps every digit; the landing page keeps one).
 * @returns {string} SVG path with the original first and last points.
 */
function wirePath(points, curved = true, bendRadius = 12, precision = null) {
    const fix = precision === null ? (n) => n : (n) => Math.round(n * 10 ** precision) / 10 ** precision;
    const route = [];
    for (const raw of points) {
        const point = { x: fix(raw.x), y: fix(raw.y) };
        const last = route.at(-1);
        if (last && last.x === point.x && last.y === point.y) continue;
        while (route.length > 1) {
            const a = route.at(-2), b = route.at(-1);
            if (!((a.x === b.x && b.x === point.x && (b.y - a.y) * (point.y - b.y) >= 0)
                    || (a.y === b.y && b.y === point.y && (b.x - a.x) * (point.x - b.x) >= 0))) break;
            route.pop();
        }
        route.push(point);
    }
    if (!route.length) return '';
    let path = `M ${route[0].x} ${route[0].y}`;
    for (let i = 1; i < route.length; i++) {
        const here = route[i], previous = route[i - 1], next = route[i + 1];
        if (!curved || !next) { path += ` L ${here.x} ${here.y}`; continue; }
        const before = Math.hypot(here.x - previous.x, here.y - previous.y);
        const after = Math.hypot(next.x - here.x, next.y - here.y);
        const radius = Math.min(bendRadius, before / 2, after / 2);
        const entry = { x: here.x + (previous.x - here.x) * radius / before,
            y: here.y + (previous.y - here.y) * radius / before };
        const exit = { x: here.x + (next.x - here.x) * radius / after,
            y: here.y + (next.y - here.y) * radius / after };
        path += ` L ${fix(entry.x)} ${fix(entry.y)} Q ${here.x} ${here.y} ${fix(exit.x)} ${fix(exit.y)}`;
    }
    return path;
}

/**
 * Read the corner points of a square SVG path (absolute M, L, H, and V
 * commands, one subpath), so it can be redrawn with rounded turns.
 *
 * @param {string} d - Square SVG path data.
 * @returns {?Array<{x: number, y: number}>} Its points in order, or null
 *     when the path uses anything else (curves, a second subpath, relative
 *     commands, unreadable numbers).
 */
function wireSquarePoints(d) {
    const tokens = String(d).match(/[A-Za-z]|-?\d*\.?\d+(?:e[-+]?\d+)?/g) || [];
    const points = [];
    let command = null;
    let at = { x: 0, y: 0 };
    for (let index = 0; index < tokens.length;) {
        const token = tokens[index];
        if (/^[A-Za-z]$/.test(token)) {
            if (!"MLHV".includes(token) || (token === "M" && points.length)) return null;
            command = token;
            index += 1;
            continue;
        }
        if (command === "H") at = { x: Number(tokens[index++]), y: at.y };
        else if (command === "V") at = { x: at.x, y: Number(tokens[index++]) };
        else at = { x: Number(tokens[index++]), y: Number(tokens[index++]) };
        if (!Number.isFinite(at.x) || !Number.isFinite(at.y)) return null;
        points.push(at);
    }
    return points;
}

/**
 * Line width, pulse cycle, and lamp flicker for a link speed. Equipment,
 * the room Diagram, and the landing preview all draw cables by these steps.
 *
 * @param {number} mbps - Link speed in Mb/s (0 or less when unknown).
 * @returns {{width: number, flow: number, flicker: number}} Stroke width in
 *     px, seconds for a pulse to move one pattern length, and seconds per
 *     port-lamp flicker cycle.
 */
function wireSpeedStyle(mbps) {
    if (mbps >= 10000) return { width: 2.2, flow: 0.7, flicker: 1.7 };
    if (mbps >= 2500) return { width: 1.8, flow: 1.1, flicker: 2.6 };
    if (mbps >= 1000) return { width: 1.4, flow: 1.6, flicker: 3.6 };
    if (mbps > 0) return { width: 1, flow: 3.2, flicker: 6.5 };
    return { width: 1, flow: 4.5, flicker: 9 };
}

/** Extra cost of one bend, in pixels of length: a straighter route wins. */
const WIRE_BEND_COST = 40;

/**
 * Route between two below-card sockets through a rectilinear visibility grid.
 * Every segment stays outside padded device rectangles, including cross links.
 *
 * A* over the grid of obstacle edges (plus clearance) and the two ends, with
 * the travel direction in the state so each bend costs WIRE_BEND_COST
 * (owner: no staircases). Equipment's surface and patch cables use it; the
 * Diagram draws fixed lane paths instead (RoomWiring.draw).
 *
 * @param {{x:number,y:number}} start - Source cable exit below its faceplate.
 * @param {{x:number,y:number}} end - Destination cable exit below its faceplate.
 * @param {object[]} boxes - Device rectangles in viewport coordinates.
 * @param {number} [scale=1] - Zoom factor for socket and obstacle clearance.
 * @returns {object[]} Ordered route points, or an empty array if obstructed.
 */
function wireRoute(start, end, boxes, scale = 1) {
    const clearance = 12 * scale;
    const xs = [...new Set([
        start.x, end.x, ...boxes.flatMap(b => [b.left - clearance, b.right + clearance])
    ])].sort((a, b) => a - b);
    const ys = [...new Set([
        start.y, end.y, ...boxes.flatMap(b => [b.top - clearance, b.bottom + clearance])
    ])].sort((a, b) => a - b);
    const clear = (a, b) => !boxes.some(box => {
        const left = box.left - 4 * scale, right = box.right + 4 * scale;
        const top = box.top - 4 * scale, bottom = box.bottom + 4 * scale;
        return a.x === b.x
            ? a.x > left && a.x < right && Math.max(a.y, b.y) > top && Math.min(a.y, b.y) < bottom
            : a.y > top && a.y < bottom && Math.max(a.x, b.x) > left && Math.min(a.x, b.x) < right;
    });
    const goal = { x: xs.indexOf(end.x), y: ys.indexOf(end.y) };
    const estimate = (x, y) => Math.abs(xs[x] - end.x) + Math.abs(ys[y] - end.y);
    // Binary heap on cost + estimate.
    const heap = [];
    const push = item => {
        heap.push(item);
        for (let i = heap.length - 1; i > 0;) {
            const parent = (i - 1) >> 1;
            if (heap[parent].score <= heap[i].score) break;
            [heap[parent], heap[i]] = [heap[i], heap[parent]]; i = parent;
        }
    };
    const pop = () => {
        const top = heap[0], last = heap.pop();
        if (heap.length) {
            heap[0] = last;
            for (let i = 0; ;) {
                const l = 2 * i + 1, r = l + 1;
                let m = i;
                if (l < heap.length && heap[l].score < heap[m].score) m = l;
                if (r < heap.length && heap[r].score < heap[m].score) m = r;
                if (m === i) break;
                [heap[m], heap[i]] = [heap[i], heap[m]]; i = m;
            }
        }
        return top;
    };
    const best = new Map();
    const startX = xs.indexOf(start.x), startY = ys.indexOf(start.y);
    push({ x: startX, y: startY, dir: -1, cost: 0, score: estimate(startX, startY), previous: null });
    const steps = [[-1, 0], [1, 0], [0, -1], [0, 1]];
    while (heap.length) {
        const here = pop();
        const id = `${here.x},${here.y},${here.dir}`;
        if (best.has(id) && best.get(id) < here.cost) continue;
        if (here.x === goal.x && here.y === goal.y) {
            const route = [];
            for (let p = here; p; p = p.previous) route.unshift({ x: xs[p.x], y: ys[p.y] });
            return route.filter((point, i) => i === 0 || point.x !== route[i - 1].x || point.y !== route[i - 1].y);
        }
        const a = { x: xs[here.x], y: ys[here.y] };
        steps.forEach(([dx, dy], dir) => {
            const x = here.x + dx, y = here.y + dy;
            if (x < 0 || y < 0 || x >= xs.length || y >= ys.length) return;
            // Never turn straight back.
            if (here.dir >= 0 && (dir ^ 1) === here.dir) return;
            const b = { x: xs[x], y: ys[y] };
            if (!clear(a, b)) return;
            const length = Math.abs(a.x - b.x) + Math.abs(a.y - b.y);
            const cost = here.cost + length
                + (here.dir >= 0 && here.dir !== dir ? WIRE_BEND_COST * scale : 0);
            const key = `${x},${y},${dir}`;
            if (best.has(key) && best.get(key) <= cost) return;
            best.set(key, cost);
            push({ x, y, dir, cost, score: cost + estimate(x, y), previous: here });
        });
    }
    return [];
}

/**
 * Hand out lanes one key at a time: each call for a key returns how many
 * cables took a lane there before it (0, 1, 2 ...). Equipment keys it by
 * rack channel and wire tray; the Locations map by building row and spine.
 * A page makes a fresh counter for each redraw.
 *
 * @returns {function(*): number} The counter: give it a key (any value a
 *     Map accepts), get that key's next lane index.
 */
function wireLaneCounter() {
    const used = new Map();
    return (key) => {
        const lane = used.get(key) || 0;
        used.set(key, lane + 1);
        return lane;
    };
}

