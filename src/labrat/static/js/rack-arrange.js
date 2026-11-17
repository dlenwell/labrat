/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * @file Draft model for the Equipment view's arrange mode.
 *
 * Holds a browser-only copy of the room's rack-mounted devices while an
 * administrator drags them to new units, within a rack or onto another
 * rack in the same room. Nothing is saved until the page posts `moves()` to
 * `POST /api/v1/rack-arrangement`, which validates the final layout again.
 *
 * Rules (decided with the owner):
 * - Devices keep their face (front, rear, full depth) and height.
 * - A drop onto free units moves the device there.
 * - A drop onto exactly one device of the same height swaps the two, when
 *   each fits in the other's place. Any other overlap is refused.
 * - Shared 1U shelves do not move; occupied shelves block their unit like
 *   a device does.
 * - Equipment on top of a rack reorders left to right within that rack top
 *   (`reorderTop`, `nudgeTop`); it does not leave the rack top.
 *
 * DOM-free so `node tests/js/rack-arrange.mjs` can check it.
 */

/**
 * Whether two rack faces collide: full depth occupies both faces.
 *
 * @param {string} face - One face (`front`, `rear`, or `full_depth`).
 * @param {string} otherFace - The other face.
 * @returns {boolean} True when the faces share space.
 */
function rackFacesOverlap(face, otherFace) {
    return face === "full_depth" || otherFace === "full_depth" || face === otherFace;
}

/**
 * Sort rack-top equipment left to right: by saved `x`, then by name.
 *
 * Shared by the Equipment view's drawing and arrange mode, so the starting
 * draft matches what is on screen.
 *
 * @param {Array<{name: string, placement?: {position?: {x?: number}}}>} nodes
 *     Equipment on one rack top.
 * @returns {Array<object>} A new, ordered array.
 */
function rackTopOrder(nodes) {
    const x = (node) => {
        const value = node.placement?.position?.x;
        return typeof value === "number" ? value : 0.5;
    };
    return [...nodes].sort((left, right) => (x(left) - x(right)) || left.name.localeCompare(right.name));
}

/**
 * Evenly spaced `x` for each position of a rack top, as the server stores it.
 *
 * @param {number} index - Zero-based position from the left.
 * @param {number} count - Number of items on the rack top.
 * @returns {number} Relative x between 0 and 1.
 */
function rackTopX(index, count) {
    return Math.round(((index + 1) / (count + 1)) * 1e6) / 1e6;
}

class RackArrangement {
    /**
     * Copy the starting layout.
     *
     * @param {object} layout - Starting layout.
     * @param {Array<{containerId: number, name: string, height_u: number,
     *     has_rear: boolean, shelves: Array<{rack_u: number, rack_face: string,
     *     occupied: boolean}>}>} layout.racks - Racks in the room, keyed by
     *     their placement container ID.
     * @param {Array<{id: number, name: string, height: number, face: string,
     *     containerId: number, rack_u: number}>} layout.devices - Inventory
     *     devices mounted directly in those racks (not on shared shelves).
     * @param {Array<{containerId: number, rackName: string,
     *     devices: Array<{id: number, name: string}>}>} [layout.tops] - Each
     *     rack top's equipment, already in left-to-right order.
     */
    constructor({ racks, devices, tops = [] }) {
        this.racks = new Map(racks.map((rack) => [rack.containerId, rack]));
        this.devices = new Map(devices.map((device) => [device.id, {
            ...device,
            height: Math.max(1, Number(device.height) || 1),
            face: device.face || "front",
        }]));
        this.original = new Map(devices.map((device) => [device.id, {
            containerId: device.containerId, rack_u: device.rack_u,
        }]));
        this.tops = new Map(tops.map((top) => [top.containerId, {
            containerId: top.containerId,
            rackName: top.rackName,
            names: new Map(top.devices.map((device) => [device.id, device.name])),
            start: top.devices.map((device) => device.id),
            order: top.devices.map((device) => device.id),
        }]));
        this.topOf = new Map(tops.flatMap((top) => top.devices.map((device) => [device.id, top.containerId])));
    }

    /**
     * Describe what occupies a span, ignoring some devices.
     *
     * @param {number} containerId - Rack container.
     * @param {string} face - Face of the span.
     * @param {number} height - Span height in U.
     * @param {number} unit - Bottom U of the span.
     * @param {Set<number>} [ignore] - Device IDs to leave out.
     * @returns {{inBounds: boolean, devices: Array<object>, shelf: boolean}}
     *     Whether the span fits the rack, devices in it, and whether an
     *     occupied shared shelf is in it.
     */
    occupants(containerId, face, height, unit, ignore = new Set()) {
        const rack = this.racks.get(containerId);
        if (!rack) return { inBounds: false, devices: [], shelf: false };
        const inBounds = Number.isInteger(unit) && unit >= 1 && unit + height - 1 <= rack.height_u;
        const top = unit + height - 1;
        const devices = [...this.devices.values()].filter((device) => (
            !ignore.has(device.id) && device.containerId === containerId
            && rackFacesOverlap(face, device.face)
            && unit <= device.rack_u + device.height - 1 && device.rack_u <= top
        ));
        const shelf = (rack.shelves || []).some((item) => (
            item.occupied && rackFacesOverlap(face, item.rack_face || "front")
            && item.rack_u >= unit && item.rack_u <= top
        ));
        return { inBounds, devices, shelf };
    }

    /**
     * Whether a device may sit at a span once some devices are out of the way.
     *
     * @param {object} device - Draft device.
     * @param {number} containerId - Rack container.
     * @param {number} unit - Bottom U.
     * @param {Set<number>} ignore - Device IDs to leave out.
     * @returns {string|null} Why it cannot, or null when it fits.
     */
    blocked(device, containerId, unit, ignore) {
        const rack = this.racks.get(containerId);
        if (!rack) return "That rack is not in this area";
        if (device.face !== "front" && !rack.has_rear) return `${rack.name} has no rear`;
        const found = this.occupants(containerId, device.face, device.height, unit, ignore);
        if (!found.inBounds) return `${device.name} does not fit there`;
        if (found.shelf) return "A shared shelf is in the way";
        if (found.devices.length) return `${found.devices[0].name} is in the way`;
        return null;
    }

    /**
     * Work out what dropping a device at a span would do, without changing it.
     *
     * @param {number} deviceId - Device being dragged.
     * @param {number} containerId - Rack container under the pointer.
     * @param {number} unit - Bottom U under the dragged device.
     * @returns {{kind: "move"|"swap"|"none"|"refused", reason: string,
     *     unit: number|null, partner: object|null,
     *     moves: Array<{id: number, containerId: number, rack_u: number}>}}
     *     The result; `unit` is where the device would land (snapped to the
     *     partner's unit for a swap).
     */
    plan(deviceId, containerId, unit) {
        const device = this.devices.get(deviceId);
        const refuse = (reason) => ({ kind: "refused", reason, unit: null, partner: null, moves: [] });
        if (!device) return refuse("Only rack-mounted inventory can be arranged");
        const rack = this.racks.get(containerId);
        if (!rack) return refuse("That rack is not in this area");
        if (device.containerId === containerId && device.rack_u === unit) {
            return { kind: "none", reason: "", unit, partner: null, moves: [] };
        }
        if (device.face !== "front" && !rack.has_rear) return refuse(`${rack.name} has no rear`);
        const self = new Set([device.id]);
        const found = this.occupants(containerId, device.face, device.height, unit, self);
        if (!found.inBounds) return refuse(`${device.name} does not fit there`);
        if (found.shelf) return refuse("A shared shelf is in the way");
        if (!found.devices.length) {
            return {
                kind: "move", reason: "", unit, partner: null,
                moves: [{ id: device.id, containerId, rack_u: unit }],
            };
        }
        const [partner] = found.devices;
        if (found.devices.length > 1) return refuse("Several devices are in the way");
        if (partner.height !== device.height) {
            return refuse(`${partner.name} is a different height; only same-height devices swap`);
        }
        const pair = new Set([device.id, partner.id]);
        const here = this.blocked(device, partner.containerId, partner.rack_u, pair);
        if (here) return refuse(here);
        const there = this.blocked(partner, device.containerId, device.rack_u, pair);
        if (there) return refuse(`Cannot swap: ${there}`);
        return {
            kind: "swap", reason: "", unit: partner.rack_u, partner,
            moves: [
                { id: device.id, containerId: partner.containerId, rack_u: partner.rack_u },
                { id: partner.id, containerId: device.containerId, rack_u: device.rack_u },
            ],
        };
    }

    /**
     * Apply a planned move or swap to the draft.
     *
     * @param {ReturnType<RackArrangement["plan"]>} result - From `plan`.
     * @returns {boolean} True when anything changed.
     */
    apply(result) {
        if (result.kind === "reorder") {
            this.tops.get(result.containerId).order = [...result.order];
            return true;
        }
        if (!["move", "swap"].includes(result.kind)) return false;
        for (const move of result.moves) {
            const device = this.devices.get(move.id);
            device.containerId = move.containerId;
            device.rack_u = move.rack_u;
        }
        return true;
    }

    /**
     * Plan a keyboard nudge of whole units within the device's rack.
     *
     * Moving into a same-height neighbour swaps with it, so repeated presses
     * walk a device past its neighbours.
     *
     * @param {number} deviceId - Device being moved.
     * @param {number} delta - Units up (positive) or down (negative).
     * @returns {ReturnType<RackArrangement["plan"]>} The planned result.
     */
    nudge(deviceId, delta) {
        const device = this.devices.get(deviceId);
        if (!device) return this.plan(deviceId, null, null);
        const step = delta > 0 ? 1 : -1;
        // A taller neighbour is passed in one press: jump to the first unit
        // beyond the device it would otherwise overlap.
        let unit = device.rack_u + delta;
        const result = this.plan(deviceId, device.containerId, unit);
        if (result.kind !== "refused") return result;
        const ahead = this.occupants(device.containerId, device.face, device.height, unit, new Set([device.id]));
        if (ahead.devices.length !== 1) return result;
        const [neighbour] = ahead.devices;
        unit = step > 0 ? neighbour.rack_u + neighbour.height : neighbour.rack_u - device.height;
        const beyond = this.plan(deviceId, device.containerId, unit);
        return beyond.kind === "move" ? beyond : result;
    }

    /**
     * Plan moving a rack-top item to a new place in its rack top's order.
     *
     * @param {number} deviceId - Item being moved.
     * @param {number} containerId - Rack top it was dropped on.
     * @param {number} index - Zero-based place among the other items (the
     *     order with the moved item taken out).
     * @returns {{kind: "reorder"|"none"|"refused", reason: string,
     *     containerId: number|null, index: number|null, order: Array<number>}}
     *     The planned order, or why not.
     */
    reorderTop(deviceId, containerId, index) {
        const home = this.topOf.get(deviceId);
        const refuse = (reason) => ({ kind: "refused", reason, containerId: null, index: null, order: [] });
        if (home === undefined) return refuse("Only equipment on top of a rack can be reordered there");
        if (home !== containerId) return refuse("Equipment on top stays on its own rack");
        const top = this.tops.get(home);
        const rest = top.order.filter((id) => id !== deviceId);
        const place = Math.max(0, Math.min(rest.length, Number.isInteger(index) ? index : rest.length));
        const order = [...rest.slice(0, place), deviceId, ...rest.slice(place)];
        const changed = order.some((id, at) => id !== top.order[at]);
        return { kind: changed ? "reorder" : "none", reason: "", containerId: home, index: place, order };
    }

    /**
     * Plan a keyboard move of a rack-top item one place left or right.
     *
     * @param {number} deviceId - Item being moved.
     * @param {number} delta - Places right (positive) or left (negative).
     * @returns {ReturnType<RackArrangement["reorderTop"]>} The planned order.
     */
    nudgeTop(deviceId, delta) {
        const home = this.topOf.get(deviceId);
        if (home === undefined) return this.reorderTop(deviceId, null, 0);
        const order = this.tops.get(home).order;
        const index = order.indexOf(deviceId) + (delta > 0 ? 1 : -1);
        if (index < 0 || index >= order.length) {
            return { kind: "refused", reason: `Already at the ${delta > 0 ? "right" : "left"} end`,
                containerId: null, index: null, order: [] };
        }
        return this.reorderTop(deviceId, home, index);
    }

    /**
     * A rack top's draft order, left to right.
     *
     * @param {number} containerId - Rack-top container.
     * @returns {Array<number>} Device IDs, or an empty list.
     */
    topOrder(containerId) {
        return [...(this.tops.get(containerId)?.order || [])];
    }

    /**
     * Rack tops whose order changed, in the API's shape.
     *
     * @returns {Array<{container_id: number, device_ids: Array<number>}>}
     *     One entry per reordered rack top, its items left to right.
     */
    topOrders() {
        return [...this.tops.values()]
            .filter((top) => top.order.some((id, at) => id !== top.start[at]))
            .map((top) => ({ container_id: top.containerId, device_ids: [...top.order] }));
    }

    /**
     * Summarize unsaved changes for the arrange bar.
     *
     * @returns {{moved: number, tops: number, any: boolean}} Moved rack
     *     devices, reordered rack tops, and whether anything changed.
     */
    changes() {
        const moved = this.moves().length;
        const tops = this.topOrders().length;
        return { moved, tops, any: moved + tops > 0 };
    }

    /**
     * Where a device currently sits in the draft.
     *
     * @param {number} deviceId - Device ID.
     * @returns {{containerId: number, rack_u: number}|null} Draft position.
     */
    position(deviceId) {
        const device = this.devices.get(deviceId);
        return device ? { containerId: device.containerId, rack_u: device.rack_u } : null;
    }

    /**
     * Moves that differ from the starting layout, in the API's shape.
     *
     * @returns {Array<{device_id: number, from: {container_id: number,
     *     rack_u: number}, to: {container_id: number, rack_u: number}}>}
     *     One entry per moved device; a device moved back is left out.
     */
    moves() {
        const result = [];
        for (const device of this.devices.values()) {
            const start = this.original.get(device.id);
            if (start.containerId === device.containerId && start.rack_u === device.rack_u) continue;
            result.push({
                device_id: device.id,
                from: { container_id: start.containerId, rack_u: start.rack_u },
                to: { container_id: device.containerId, rack_u: device.rack_u },
            });
        }
        return result;
    }
}
