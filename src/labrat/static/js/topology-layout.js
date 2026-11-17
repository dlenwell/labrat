/**
 * @fileoverview Layered (tiered) layout for the Discovery map.
 *
 * The map is arranged as a tree that follows the observed cables: routers
 * and firewalls at the top, switches below them by hop count, and endpoints
 * (servers, access points, UPS units, and anything else) stacked in a column
 * under the device they are cabled to. Network gear with connections of
 * its own beyond its parent keeps a slot in the row instead of stacking.
 * Cables that do not belong to that tree, such as a server's second
 * connection to another switch, are still drawn as side or cross links. A USB-attached UPS hangs under the
 * NUT server it reports through, on its own line style, since USB is not a
 * network cable. A relayed UPS (`dummy-ups`) hangs there on a fainter
 * "reports through" line: a data relationship with no known connection.
 *
 * Everything here is a pure function of the topology API's nodes and links,
 * so the same data always produces the same picture. The tree is a drawing
 * aid only: a parent here means "drawn above", never a recorded uplink or a
 * placement. Coordinates are CSS pixels of the unscaled map stage.
 *
 * This file has no DOM access so it can be checked under Node
 * (`node tests/js/topology-layout.mjs`).
 *
 * @copyright 2026 LabRat contributors
 * @license Apache-2.0
 * SPDX-License-Identifier: Apache-2.0
 *
 * Used by: Discovery, Equipment and Diagram.
 */

/** Card and spacing sizes, in stage pixels. Keep in step with topology.css. */
const LABRAT_LAYOUT = Object.freeze({
    nodeWidth: 148,
    nodeHeight: 50,
    // Wide enough that a stack's right-hand trunk and the next column's
    // left-hand trunk run in separate lanes.
    slotWidth: 182,
    rowHeight: 118,
    stackStep: 62,
    margin: 32,
    treeGap: 40,
    orphanGap: 72,
});

/** Categories that carry other equipment and may sit higher in the tree. */
const LABRAT_INFRASTRUCTURE = new Set(["modem", "router", "firewall", "switch", "access_point"]);

/** Categories that start a tree when present. */
const LABRAT_ROOT_CATEGORIES = new Set(["modem", "router", "firewall"]);

/** Sibling order: network gear first, then hosts, then power and the rest. */
const LABRAT_CATEGORY_ORDER = ["modem", "router", "firewall", "switch", "access_point", "server", "ups"];

/**
 * Sort key for a category so siblings appear in a stable, readable order.
 *
 * @param {string} category - Device category from the API.
 * @returns {number} Lower values are drawn first (further left or higher).
 */
function labratCategoryRank(category) {
    const index = LABRAT_CATEGORY_ORDER.indexOf(category);
    return index === -1 ? LABRAT_CATEGORY_ORDER.length : index;
}

/**
 * Compare two node records by category rank, then by name.
 *
 * @param {object} a - Topology node record.
 * @param {object} b - Topology node record.
 * @returns {number} Sort comparison result.
 */
function labratCompareRecords(a, b) {
    return labratCategoryRank(a.category) - labratCategoryRank(b.category)
        || a.name.localeCompare(b.name, undefined, { numeric: true })
        || a.id.localeCompare(b.id);
}

/**
 * Unordered key for a pair of node IDs.
 *
 * @param {string} a - Node ID.
 * @param {string} b - Node ID.
 * @returns {string} The same key for (a, b) and (b, a).
 */
function labratPairKey(a, b) {
    return a < b ? `${a}|${b}` : `${b}|${a}`;
}

/**
 * Group cables, Wi-Fi paths, and USB attachments by the devices they join.
 *
 * Bonded servers report one cable per member, so a pair can carry several
 * links. The group keeps all of them for styling and tooltips.
 *
 * @param {object[]} links - Physical links from the topology API.
 * @param {object[]} wirelessLinks - Inferred Wi-Fi client paths.
 * @param {object[]} powerLinks - UPS-to-NUT-server relations (`from` server,
 *     `to` UPS, `kind` "usb" or "relay").
 * @returns {Map<string, object>} Pair key to
 *     `{from, to, links, wireless, usb, relay}`.
 */
function labratGroupLinks(links, wirelessLinks = [], powerLinks = []) {
    const pairs = new Map();
    const pair = (from, to) => {
        const key = labratPairKey(from, to);
        if (!pairs.has(key)) pairs.set(key, { key, from, to, links: [], wireless: [], usb: [], relay: [] });
        return pairs.get(key);
    };
    for (const link of powerLinks) {
        if (link.from === link.to) continue;
        pair(link.from, link.to)[link.kind === "relay" ? "relay" : "usb"].push(link);
    }
    for (const link of links) {
        if (link.from !== link.to) pair(link.from, link.to).links.push(link);
    }
    for (const link of wirelessLinks) {
        if (link.from !== link.to) pair(link.from, link.to).wireless.push(link);
    }
    return pairs;
}

/**
 * Score how strongly a pair supports drawing one device under the other.
 *
 * More cables beat fewer, confirmed beats inferred, and any cable beats a
 * tentative one. A USB attachment reported by NUT ranks below a cable, a
 * relayed UPS's reporting relation below that, and an inferred Wi-Fi path
 * is the weakest evidence.
 *
 * @param {object} group - Link group from `labratGroupLinks`.
 * @returns {number} Higher is stronger.
 */
function labratPairScore(group) {
    let score = group.wireless.length ? 1 : 0;
    score += group.usb.length ? 6 : 0;
    score += group.relay.length ? 2 : 0;
    for (const link of group.links) {
        score += link.confidence === "confirmed" ? 12 : link.confidence === "inferred" ? 9 : 3;
    }
    return score;
}

/**
 * Arrange the topology as tiered trees with stacked endpoint columns.
 *
 * @param {object[]} records - Topology API node records (id, name, category).
 * @param {object[]} links - Physical links (`from`, `to`, `confidence`).
 * @param {object[]} [wirelessLinks] - Inferred Wi-Fi paths (`from`, `to`).
 * @param {object[]} [powerLinks] - UPS-to-NUT-server relations (`from`
 *     server, `to` UPS, `kind` "usb" or "relay").
 * @param {object} [geometry] - Card and spacing sizes; Equipment uses larger faceplates.
 * @returns {{
 *   positions: Map<string, {x:number, y:number}>,
 *   parent: Map<string, string>,
 *   stacked: Set<string>,
 *   roots: string[],
 *   orphans: string[],
 *   orphanTop: number|null,
 *   pairs: Map<string, object>,
 *   width: number,
 *   height: number,
 * }} Node centers, drawing tree, and stage size.
 */
function labratLayeredLayout(records, links, wirelessLinks = [], powerLinks = [], geometry = LABRAT_LAYOUT) {
    const L = geometry;
    const byId = new Map(records.map((record) => [record.id, record]));
    const pairs = labratGroupLinks(
        links.filter((link) => byId.has(link.from) && byId.has(link.to)),
        wirelessLinks.filter((link) => byId.has(link.from) && byId.has(link.to)),
        powerLinks.filter((link) => byId.has(link.from) && byId.has(link.to)),
    );
    const neighbors = new Map(records.map((record) => [record.id, new Map()]));
    for (const group of pairs.values()) {
        const score = labratPairScore(group);
        neighbors.get(group.from).set(group.to, score);
        neighbors.get(group.to).set(group.from, score);
    }
    const isInfra = (id) => LABRAT_INFRASTRUCTURE.has(byId.get(id).category);
    const parent = new Map();
    const depth = new Map();
    const roots = [];

    /**
     * Grow a tree level by level through infrastructure only, so a server
     * with two uplinks never becomes the parent of a switch. Each newly
     * reached device hangs under its strongest neighbor on the level above.
     */
    const growInfrastructure = (root) => {
        roots.push(root);
        depth.set(root, 0);
        let level = [root];
        while (level.length) {
            const next = new Map();
            for (const id of level) {
                for (const [other, score] of neighbors.get(id)) {
                    if (depth.has(other) || !isInfra(other)) continue;
                    // WAN upstream modems are separate top-row roots. When a
                    // gateway has multiple WANs, do not absorb the other
                    // modem into the first modem's tree beneath the gateway.
                    if (byId.get(other).category === "modem" && id !== root) continue;
                    const best = next.get(other);
                    if (!best || score > best.score
                        || (score === best.score && labratCompareRecords(byId.get(id), byId.get(best.parent)) < 0)) {
                        next.set(other, { parent: id, score });
                    }
                }
            }
            level = [];
            for (const [id, choice] of next) {
                parent.set(id, choice.parent);
                depth.set(id, depth.get(choice.parent) + 1);
                level.push(id);
            }
        }
    };

    const sorted = [...records].sort(labratCompareRecords);
    for (const record of sorted) {
        if (LABRAT_ROOT_CATEGORIES.has(record.category) && !depth.has(record.id)) {
            growInfrastructure(record.id);
        }
    }
    // Infrastructure islands with no router: start from their best-connected device.
    const infraDegree = (id) => [...neighbors.get(id).keys()].filter(isInfra).length;
    for (;;) {
        const island = sorted.filter((record) => isInfra(record.id) && !depth.has(record.id)
            && infraDegree(record.id) > 0)
            .sort((a, b) => infraDegree(b.id) - infraDegree(a.id) || labratCompareRecords(a, b))[0];
        if (!island) break;
        growInfrastructure(island.id);
    }

    // Attach everything else under its strongest drawn neighbor, repeating so
    // chains (a UPS behind a server, say) are reached. Ties go to the device
    // nearer the top, then to the usual sibling order.
    for (;;) {
        let attached = false;
        for (const record of sorted) {
            if (depth.has(record.id)) continue;
            let best = null;
            for (const [other, score] of neighbors.get(record.id)) {
                if (!depth.has(other)) continue;
                if (!best || score > best.score
                    || (score === best.score && depth.get(other) < depth.get(best.parent))
                    || (score === best.score && depth.get(other) === depth.get(best.parent)
                        && labratCompareRecords(byId.get(other), byId.get(best.parent)) < 0)) {
                    best = { parent: other, score };
                }
            }
            if (best) {
                parent.set(record.id, best.parent);
                depth.set(record.id, depth.get(best.parent) + 1);
                attached = true;
            }
        }
        if (attached) continue;
        // A group cabled only among endpoints (two servers back to back).
        const loose = sorted.filter((record) => !depth.has(record.id) && neighbors.get(record.id).size > 0)
            .sort((a, b) => neighbors.get(b.id).size - neighbors.get(a.id).size || labratCompareRecords(a, b))[0];
        if (!loose) break;
        roots.push(loose.id);
        depth.set(loose.id, 0);
    }

    const children = new Map(records.map((record) => [record.id, []]));
    for (const [child, above] of parent) children.get(above).push(child);
    for (const list of children.values()) {
        list.sort((a, b) => labratCompareRecords(byId.get(a), byId.get(b)));
    }
    // End devices stack in one column under their parent; the rest spread
    // across slots. A device spreads when something hangs under it in the
    // drawing, and so does network gear with any connection besides its
    // parent: a management switch whose servers are drawn under their data
    // switch still carries those cables, so it should not look like an end
    // device.
    const spreads = (id) => children.get(id).length > 0
        || (isInfra(id) && neighbors.get(id).size > 1);
    const branchesOf = (id) => children.get(id).filter(child => L.stackLeaves === false || spreads(child));
    const leavesOf = (id) => children.get(id).filter(child => L.stackLeaves !== false && !spreads(child));
    const widths = new Map();
    const slotsFor = (id) => {
        if (widths.has(id)) return widths.get(id);
        const total = branchesOf(id).reduce((sum, child) => sum + slotsFor(child), 0)
            + (leavesOf(id).length ? 1 : 0);
        widths.set(id, Math.max(1, total));
        return widths.get(id);
    };

    const positions = new Map();
    const stacked = new Set();
    let bottom = 0;
    const rowY = (level) => L.margin + L.nodeHeight / 2 + level * L.rowHeight;
    const place = (id, left, level) => {
        const centers = [];
        let cursor = left;
        const leaves = leavesOf(id);
        if (leaves.length) {
            const x = cursor + L.slotWidth / 2;
            leaves.forEach((leaf, index) => {
                const y = rowY(level + 1) + index * L.stackStep;
                positions.set(leaf, { x, y });
                stacked.add(leaf);
                bottom = Math.max(bottom, y);
            });
            centers.push(x);
            cursor += L.slotWidth;
        }
        for (const branch of branchesOf(id)) {
            centers.push(place(branch, cursor, level + 1));
            cursor += slotsFor(branch) * L.slotWidth;
        }
        // Sit over the middle of what hangs below, or in the slot's center.
        const x = centers.length
            ? (Math.min(...centers) + Math.max(...centers)) / 2
            : left + L.slotWidth / 2;
        positions.set(id, { x, y: rowY(level) });
        bottom = Math.max(bottom, rowY(level));
        return x;
    };

    let left = L.margin;
    roots.sort((a, b) => labratCompareRecords(byId.get(a), byId.get(b)));
    // Trees with a router first; the larger of the rest next.
    roots.sort((a, b) => Number(LABRAT_ROOT_CATEGORIES.has(byId.get(b).category))
        - Number(LABRAT_ROOT_CATEGORIES.has(byId.get(a).category)) || slotsFor(b) - slotsFor(a));
    for (const root of roots) {
        place(root, left, 0);
        left += slotsFor(root) * L.slotWidth + L.treeGap;
    }
    let width = Math.max(left - L.treeGap + L.margin, L.margin * 2 + L.slotWidth);

    // Devices with no observed connection sit on a shelf below the trees.
    const orphans = sorted.filter((record) => !depth.has(record.id)).map((record) => record.id);
    let orphanTop = null;
    if (orphans.length) {
        orphanTop = roots.length ? bottom + L.nodeHeight / 2 + L.orphanGap : L.margin;
        const perRow = Math.max(4, Math.floor((width - L.margin * 2) / L.slotWidth));
        orphans.forEach((id, index) => {
            const x = L.margin + (index % perRow) * L.slotWidth + L.slotWidth / 2;
            const y = orphanTop + 28 + L.nodeHeight / 2 + Math.floor(index / perRow) * L.stackStep;
            positions.set(id, { x, y });
            bottom = Math.max(bottom, y);
        });
        width = Math.max(width, L.margin * 2 + Math.min(orphans.length, perRow) * L.slotWidth);
    }
    const height = bottom + L.nodeHeight / 2 + L.margin;
    return { positions, parent, stacked, roots, orphans, orphanTop, pairs, width, height };
}

/**
 * Build an SVG path for one drawn device pair.
 *
 * Tree links drop from the parent's bottom edge and enter the child's top
 * edge with right-angle bends. Stacked endpoints hang off a trunk running
 * down the left of their column. A second link from a device above into a
 * stacked endpoint ("side") runs down a trunk on the column's right and
 * enters the card's right edge, so a server's data and management cables
 * arrive from opposite sides. Cross-links leave from facing card edges.
 * An explicit style rounds or squares every route; the omitted style retains
 * the original tree bends and sweeping cross-links for existing callers.
 *
 * @param {"tree"|"stack"|"side"|"cross"} kind - Routing style.
 * @param {{x:number,y:number}} a - Upper (parent) or first node center.
 * @param {{x:number,y:number}} b - Child or second node center.
 * @param {number} [offset] - Sideways offset for parallel cables.
 * @param {?boolean} [curved=null] - Round all routes, use square bends, or retain legacy paths.
 * @returns {string} SVG path data.
 */
function labratRoutePath(kind, a, b, offset = 0, curved = null) {
    const L = LABRAT_LAYOUT;
    const halfW = L.nodeWidth / 2;
    const halfH = L.nodeHeight / 2;
    const path = (points, legacy) => curved === null ? legacy : wirePath(points, curved);
    if (kind === "stack") {
        const trunk = b.x - halfW - 10 + offset;
        const startY = a.y + halfH;
        const turn = Math.min(startY + (L.rowHeight - L.nodeHeight) / 2, b.y - halfH - 6);
        return path([
            { x: a.x + offset, y: startY }, { x: a.x + offset, y: turn },
            { x: trunk, y: turn }, { x: trunk, y: b.y }, { x: b.x - halfW, y: b.y },
        ], `M${a.x + offset} ${startY} V${turn} H${trunk} V${b.y} H${b.x - halfW}`);
    }
    if (kind === "side") {
        const trunk = b.x + halfW + 8 + offset;
        const startY = a.y + halfH;
        // Turn closer to the source than tree links do, so the two buses
        // between rows stay apart.
        const turn = Math.min(startY + 14, b.y - halfH - 6);
        return path([
            { x: a.x + offset, y: startY }, { x: a.x + offset, y: turn },
            { x: trunk, y: turn }, { x: trunk, y: b.y }, { x: b.x + halfW, y: b.y },
        ], `M${a.x + offset} ${startY} V${turn} H${trunk} V${b.y} H${b.x + halfW}`);
    }
    if (kind === "tree") {
        const startY = a.y + halfH;
        const endY = b.y - halfH;
        const mid = (startY + endY) / 2;
        return path([
            { x: a.x + offset, y: startY }, { x: a.x + offset, y: mid },
            { x: b.x + offset, y: mid }, { x: b.x + offset, y: endY },
        ], `M${a.x + offset} ${startY} V${mid} H${b.x + offset} V${endY}`);
    }
    // Leave from whichever edges face each other.
    const vertical = Math.abs(b.y - a.y) > L.nodeHeight;
    const [x1, y1, x2, y2] = vertical
        ? [a.x + offset, a.y + Math.sign(b.y - a.y) * halfH, b.x + offset, b.y - Math.sign(b.y - a.y) * halfH]
        : [a.x + Math.sign(b.x - a.x) * halfW, a.y + offset, b.x - Math.sign(b.x - a.x) * halfW, b.y + offset];
    if (curved !== null) {
        const mid = vertical ? (y1 + y2) / 2 : (x1 + x2) / 2;
        return wirePath([
            { x: x1, y: y1 }, vertical ? { x: x1, y: mid } : { x: mid, y: y1 },
            vertical ? { x: x2, y: mid } : { x: mid, y: y2 }, { x: x2, y: y2 },
        ], curved);
    }
    const bend = vertical ? [0, (y2 - y1) / 2] : [(x2 - x1) / 2, 0];
    return `M${x1} ${y1} C${x1 + bend[0]} ${y1 + bend[1]} ${x2 - bend[0]} ${y2 - bend[1]} ${x2} ${y2}`;
}

/**
 * List a device and every device drawn above it, up to its tree's root.
 *
 * @param {Map<string, string>} parent - Drawing tree from the layout.
 * @param {string} id - Starting node ID.
 * @returns {string[]} IDs from the device to its root.
 */
function labratPathToRoot(parent, id) {
    const path = [id];
    const seen = new Set(path);
    let current = id;
    while (parent.has(current) && !seen.has(parent.get(current))) {
        current = parent.get(current);
        path.push(current);
        seen.add(current);
    }
    return path;
}
