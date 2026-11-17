/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * @file Choose which port lamps a device card draws when it has more ports
 * than its faceplate row holds comfortably.
 *
 * Lamps stay in one row (cables drop out of them without passing another
 * socket), so a device with many ports either narrows its lamps or hides
 * some. The Equipment view's Port lights tool cycles three variants for the
 * owner to compare (a per-browser preference in localStorage):
 *
 * - All: every port; lamps narrow to fit.
 * - Ends: the first ports and the last few (where uplink and SFP ports
 *   usually sit), with a "more" marker for each hidden run.
 * - Capped: the first ports up to the limit, with a "more" marker.
 *
 * Ends and Capped never hide a port with a cable: a cable needs its lamp to
 * be drawn at all, so cabled ports are kept first and only uncabled ones
 * are left out.
 *
 * Used by: Equipment and Diagram.
 */

/** Most lamps drawn on one device in the Ends and Capped variants. */
const RACK_PORT_LIGHT_LIMIT = 24;

/** Ports kept from the end of the list in the Ends variant. */
const RACK_PORT_LIGHT_TAIL = 4;

/** The variants, in the order the tool cycles them. */
const RACK_PORT_LIGHT_MODES = ["all", "ends", "capped"];

/** Readable names for the tool's tooltip. */
const RACK_PORT_LIGHT_LABELS = {
    all: "All ports",
    ends: "First and last ports",
    capped: `First ${RACK_PORT_LIGHT_LIMIT} ports`,
};

/** localStorage key holding the chosen variant. */
const RACK_PORT_LIGHT_MODE_KEY = "labrat.equipment.port-lights";

/**
 * Pick the lamps to draw and where hidden runs go.
 *
 * @param {number} count - The device's port count.
 * @param {number} limit - Most lamps for the limited variants; Infinity
 *     draws every port.
 * @param {string} mode - ``all``, ``ends``, or ``capped`` (anything else is
 *     treated as ``all``).
 * @param {Set<number>} [keep] - Port indices that must be drawn (cabled).
 * @returns {Array<number|{more: number}>} Port indices in order, with a
 *     ``{more}`` marker in place of each run of hidden ports.
 */
function rackPortLightSlots(count, limit, mode, keep = new Set()) {
    const every = Array.from({ length: count }, (_, index) => index);
    // Any mode other than the two limited ones draws every port.
    if (!["ends", "capped"].includes(mode) || !Number.isFinite(limit) || count <= limit) return every;
    const shown = new Set([...keep].filter((index) => index >= 0 && index < count));
    const room = Math.max(0, limit - shown.size);
    const tail = mode === "ends" ? Math.min(RACK_PORT_LIGHT_TAIL, room) : 0;
    const order = [...every.slice(count - tail), ...every];
    for (const index of order) {
        if (shown.size >= Math.max(limit, keep.size)) break;
        shown.add(index);
    }
    const slots = [];
    let hidden = 0;
    for (const index of every) {
        if (shown.has(index)) {
            if (hidden) slots.push({ more: hidden });
            hidden = 0;
            slots.push(index);
        } else {
            hidden += 1;
        }
    }
    if (hidden) slots.push({ more: hidden });
    return slots;
}

/**
 * Read the saved variant, falling back when storage is absent or invalid.
 *
 * @returns {string} A value from RACK_PORT_LIGHT_MODES.
 */
function rackReadPortLightMode() {
    try {
        const value = localStorage.getItem(RACK_PORT_LIGHT_MODE_KEY);
        return RACK_PORT_LIGHT_MODES.includes(value) ? value : RACK_PORT_LIGHT_MODES[0];
    } catch {
        return RACK_PORT_LIGHT_MODES[0];
    }
}

/**
 * Save the chosen variant without requiring browser storage.
 *
 * @param {string} mode - The variant.
 * @returns {void}
 */
function rackSavePortLightMode(mode) {
    try { localStorage.setItem(RACK_PORT_LIGHT_MODE_KEY, mode); } catch { /* Optional preference. */ }
}

/**
 * The variant after this one in the tool's cycle.
 *
 * @param {string} mode - The current variant.
 * @returns {string} The next variant.
 */
function rackNextPortLightMode(mode) {
    const index = RACK_PORT_LIGHT_MODES.indexOf(mode);
    return RACK_PORT_LIGHT_MODES[(index + 1) % RACK_PORT_LIGHT_MODES.length];
}

/**
 * Keep the tool's tooltip and accessible name on the current variant and
 * the one the next press shows (as the Wire style tool does).
 *
 * @param {HTMLElement|null} button - The Port lights tool.
 * @param {string} mode - The current variant.
 * @returns {void}
 */
function rackUpdatePortLightButton(button, mode) {
    if (!button) return;
    const next = rackNextPortLightMode(mode);
    const label = `Port lights: ${RACK_PORT_LIGHT_LABELS[mode]}. Switch to ${RACK_PORT_LIGHT_LABELS[next]}`;
    button.setAttribute("aria-label", label);
    button.title = label;
    button.dataset.tooltip = label;
    button.dataset.mode = mode;
}
