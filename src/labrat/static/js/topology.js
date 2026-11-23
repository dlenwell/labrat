/**
 * @fileoverview Layered map of discovered physical infrastructure.
 *
 * All nodes and observed cables come from LabRat's authenticated, cached API.
 * `topology-layout.js` arranges them in tiers that follow the cables: routers
 * at the top, switches by hop count, and endpoints stacked under the device
 * they connect to. That drawing tree is presentation only, never evidence of
 * an uplink. Dragging a card pins it on the diagram and never changes
 * physical inventory. Explicit forms handle placement.
 *
 * This file intentionally uses readable browser JavaScript, not a generated
 * bundle or external graph framework, so homelab contributors can edit it.
 *
 * @copyright 2026 LabRat contributors
 * @license Apache-2.0
 * SPDX-License-Identifier: Apache-2.0
 */

const LABRAT_UNPLACED_FILTER_KEY = "labrat.topology.unplacedOnly";
const LABRAT_KEY_OPEN_KEY = "labrat.topology.keyOpen";
/** Canvases narrower than this start with the map key collapsed. */
const LABRAT_KEY_NARROW_WIDTH = 640;
const LABRAT_PINS_KEY = "labrat.topology.layeredPins";
const LABRAT_LEGACY_PINS_KEY = "labrat.topology.pins";
/** Card labels short enough to share a line with the "Unplaced" flag. */
const LABRAT_SHORT_CATEGORY = Object.freeze({ access_point: "AP" });
/** Canvas never shrinks below this, in CSS pixels, on very short windows. */
const LABRAT_MIN_CANVAS_HEIGHT = 320;
/** Gap between the map panel and the standard page footer. */
const LABRAT_CANVAS_BOTTOM_GAP = 16;
/** Distance from a card's bottom-right corner to its pin button's center. */
const LABRAT_PIN_INSET = 13;
/** Smallest zoom used to fit the map; below it the canvas scrolls instead. */
const LABRAT_MIN_SCALE = 0.62;

/** Store browser-only graph state; inventory is authoritative in SQLite. */
const labratGraph = {
    nodes: new Map(),
    links: [],
    wirelessLinks: [],
    powerLinks: [],
    layout: null,
    stageScale: 1,
    wireStyle: "curved",
    selectedId: null,
    hoverId: null,
    drag: null,
    suppressClick: false,
    panMoved: false,
    unplacedOnly: false,
    /** Shared drawer shell (device-drawer.js); created at startup. */
    drawer: null,
    /** Shared details panels (device-details.js); created at startup. */
    details: null,
    /** Shared placement editor (device-placement.js); created at startup. */
    placement: null,
    neighborMatches: [],
    locations: { sites: [], rooms: [], racks: [], containers: [], devices: [] },
};

/**
 * Restore the user's unplaced-only preference from browser-local storage.
 *
 * @returns {boolean} Whether the filter was enabled on the previous visit.
 */
function labratReadUnplacedPreference() {
    try {
        return localStorage.getItem(LABRAT_UNPLACED_FILTER_KEY) === "true";
    } catch {
        return false;
    }
}

/**
 * Save the unplaced-only preference without making storage a requirement.
 *
 * @returns {void}
 */
function labratSaveUnplacedPreference() {
    try {
        localStorage.setItem(LABRAT_UNPLACED_FILTER_KEY, String(labratGraph.unplacedOnly));
    } catch {
        // The filter remains active for the current page even if storage is unavailable.
    }
}

/**
 * Locate a required DOM element by ID.
 *
 * @param {string} id - Template element identifier.
 * @returns {HTMLElement} Matching element.
 */
function labratElement(id) {
    return document.getElementById(id);
}

/**
 * Create a safe text element without interpreting discovered names as HTML.
 *
 * @param {string} tag - HTML element name.
 * @param {string} text - User-visible text.
 * @param {string} [className] - Optional stylesheet class.
 * @returns {HTMLElement} DOM element containing plain text.
 */
function labratText(tag, text, className = "") {
    const result = document.createElement(tag);
    result.textContent = text;
    if (className) {
        result.className = className;
    }
    return result;
}

/**
 * Report a recoverable error or successful update to the user.
 *
 * @param {string} message - Safe status message.
 * @param {boolean} [isError] - Whether the message describes a failure.
 * @returns {void}
 */
function labratNotice(message, isError = false) {
    const target = labratElement("topology-notice");
    target.textContent = message;
    target.dataset.state = isError ? "error" : "ok";
}

/**
 * Restore pinned display positions from browser-local preferences.
 *
 * Pins are stage pixels from the layered layout. The spring layout's old
 * `labrat.topology.pins` used another coordinate system and is discarded.
 *
 * @returns {Object<string, {x:number,y:number}>} Valid pins by node ID.
 */
function labratReadPins() {
    try {
        localStorage.removeItem(LABRAT_LEGACY_PINS_KEY);
        const stored = JSON.parse(localStorage.getItem(LABRAT_PINS_KEY) || "{}");
        const pins = {};
        for (const [id, value] of Object.entries(stored || {})) {
            if (value && Number.isFinite(value.x) && Number.isFinite(value.y)) {
                pins[id] = { x: value.x, y: value.y };
            }
        }
        return pins;
    } catch {
        // Browser storage may be disabled; the map works without it.
        return {};
    }
}

/**
 * Save or remove a node's pinned display position without touching inventory.
 *
 * @param {object} node - Local graph node.
 * @returns {void}
 */
function labratStorePin(node) {
    const pins = labratReadPins();
    if (node.pinned) {
        pins[node.id] = { x: Math.round(node.x), y: Math.round(node.y) };
    } else {
        delete pins[node.id];
    }
    try {
        localStorage.setItem(LABRAT_PINS_KEY, JSON.stringify(pins));
    } catch {
        // Storage is optional; the pin still lasts for this page session.
    }
}

/**
 * Pick out devices the map leaves off: every source reporting one says it
 * is offline (an adopted access point or switch that is unplugged right
 * now), and no cable or Wi-Fi link reaches it. With a link it stays, so a
 * dead connection is still visible; once it connects it comes back.
 *
 * @param {object} topology - Cached topology API document.
 * @returns {Set<string>} Node IDs to leave off the map.
 */
function labratDisconnectedNodes(topology) {
    const linked = new Set();
    for (const link of [...(topology.links || []), ...(topology.wireless_links || [])]) {
        linked.add(link.from);
        linked.add(link.to);
    }
    return new Set(topology.nodes.filter((record) => !linked.has(record.id)
        && record.observations.length
        && record.observations.every((item) => item.controller_state === "offline")).map((record) => record.id));
}

/**
 * Synchronize API observations with the local node objects and redraw.
 *
 * The layout is recomputed from the data on every refresh, so it stays
 * stable while nothing changes; pinned nodes keep their dragged position.
 *
 * @param {object} topology - Cached topology API document.
 * @returns {void}
 */
function labratSyncTopology(topology) {
    const previousSelection = labratGraph.nodes.get(labratGraph.selectedId)?.record;
    const currentIds = new Set();
    const pins = labratReadPins();
    const disconnected = labratDisconnectedNodes(topology);
    for (const record of topology.nodes.filter((item) => !item.client_only && !disconnected.has(item.id))) {
        currentIds.add(record.id);
        const old = labratGraph.nodes.get(record.id);
        if (old) {
            old.record = record;
            continue;
        }
        labratGraph.nodes.set(record.id, {
            id: record.id, record, x: 0, y: 0,
            pinned: Boolean(pins[record.id]), element: null,
        });
    }
    for (const id of labratGraph.nodes.keys()) {
        if (!currentIds.has(id)) {
            labratGraph.nodes.delete(id);
        }
    }
    if (previousSelection && !labratGraph.nodes.has(labratGraph.selectedId)) {
        const previousObservations = new Set(previousSelection.observations.map((item) => item.id));
        const match = [...labratGraph.nodes.values()].find((item) =>
            item.record.observations.some((observation) => previousObservations.has(observation.id))
        );
        labratGraph.selectedId = match?.id || null;
    }
    if (!labratGraph.selectedId && ["details", "placement"].includes(labratGraph.drawer.currentView)) {
        labratCloseDrawer();
    }
    labratGraph.links = topology.links.filter(
        (link) => labratGraph.nodes.has(link.from) && labratGraph.nodes.has(link.to)
    );
    labratGraph.neighborMatches = topology.neighbor_matches || [];
    labratGraph.details.index();
    labratGraph.wirelessLinks = (topology.wireless_links || []).filter(
        (link) => labratGraph.nodes.has(link.from) && labratGraph.nodes.has(link.to)
    );
    labratGraph.powerLinks = labratPowerLinks();
    labratApplyLayout();
    labratDrawNodes();
    labratDrawLinks();
    labratRenderEmptyState();
    labratCenterOnRoot();
    // Source health is shown in the header strip on every page, so the
    // summary only counts devices and links. Only a missing source is noted.
    const countLinks = (confidence) => labratGraph.links.filter(
        (link) => link.confidence === confidence,
    ).length;
    const linkTotal = labratGraph.links.length;
    labratElement("topology-summary").textContent =
        `${labratGraph.nodes.size} physical devices · `
        + `${linkTotal} ${linkTotal === 1 ? "link" : "links"} `
        + `(${countLinks("confirmed")} confirmed · ${countLinks("inferred")} inferred · `
        + `${countLinks("ambiguous")} tentative)`
        + (topology.sources.length ? "" : " · No collectors configured");
    labratRenderInspector();
    labratUpdateDrawerBar();
    labratFitCanvas();
}

/**
 * Update the graph's empty state for discovery and the unplaced highlight.
 *
 * @returns {void}
 */
function labratRenderEmptyState() {
    const noDevices = labratGraph.nodes.size === 0;
    const noUnplaced = labratGraph.unplacedOnly && ![...labratGraph.nodes.values()]
        .some((node) => !node.record.placement);
    labratElement("topology-empty").hidden = !noDevices;
    labratElement("topology-filter-empty").hidden = noDevices || !noUnplaced;
    labratElement("topology-canvas").classList.toggle("is-unplaced-focus", labratGraph.unplacedOnly);
}

// labratIconPath and labratDeviceIcon (device-type icons) live in device-details.js.

/**
 * List UPS units with the NUT server they report through.
 *
 * NUT's `usb` attachment is a real USB connection to that computer. A
 * `relayed` (`dummy-ups`) unit only reports through it, with no known
 * connection, so it gets its own fainter "reports through" line. Units with
 * an unknown attachment are left unconnected.
 *
 * @returns {{from:string, to:string, kind:string}[]} Server to UPS relations.
 */
function labratPowerLinks() {
    const kinds = { usb: "usb", relayed: "relay" };
    const links = [];
    for (const node of labratGraph.nodes.values()) {
        for (const observation of node.record.observations) {
            const server = observation.ups?.server_node_id;
            const kind = kinds[observation.ups?.attachment];
            if (kind && server && server !== node.id && labratGraph.nodes.has(server)) {
                links.push({ from: server, to: node.id, kind });
            }
        }
    }
    return links;
}

/**
 * Compute the layered layout and move unpinned nodes to their tier.
 *
 * The result has new link pairs without SVG paths; callers must follow
 * with `labratDrawLinks()` (which also paints) to rebuild them.
 *
 * @returns {void}
 */
function labratApplyLayout() {
    const records = [...labratGraph.nodes.values()].map((node) => node.record);
    labratGraph.layout = labratLayeredLayout(
        records, labratGraph.links, labratGraph.wirelessLinks, labratGraph.powerLinks,
    );
    const pins = labratReadPins();
    for (const node of labratGraph.nodes.values()) {
        const position = (node.pinned && pins[node.id]) || labratGraph.layout.positions.get(node.id);
        if (position && node.id !== labratGraph.drag?.id) {
            node.x = position.x;
            node.y = position.y;
        }
    }
    labratFitStage();
}

/**
 * Size the stage for the layout and shrink it to fit narrower screens.
 *
 * The map scales down to fit the canvas width, but never below
 * `LABRAT_MIN_SCALE`; past that the canvas scrolls so names stay readable.
 *
 * @returns {void}
 */
function labratFitStage() {
    const layout = labratGraph.layout;
    if (!layout) {
        return;
    }
    // Pinned nodes may sit outside the computed layout.
    let width = layout.width;
    let height = layout.height;
    for (const node of labratGraph.nodes.values()) {
        width = Math.max(width, node.x + LABRAT_LAYOUT.nodeWidth / 2 + LABRAT_LAYOUT.margin);
        height = Math.max(height, node.y + LABRAT_LAYOUT.nodeHeight / 2 + LABRAT_LAYOUT.margin);
    }
    const canvas = labratElement("topology-canvas");
    const available = canvas.clientWidth || width;
    const scale = Math.min(1, Math.max(LABRAT_MIN_SCALE, available / width));
    labratGraph.stageScale = scale;
    const stage = labratElement("topology-stage");
    stage.style.width = `${width}px`;
    stage.style.height = `${height}px`;
    stage.style.transform = `scale(${scale})`;
    const sizer = labratElement("topology-sizer");
    sizer.style.width = `${width * scale}px`;
    sizer.style.height = `${height * scale}px`;
    const wires = labratElement("topology-wires");
    wires.setAttribute("viewBox", `0 0 ${width} ${height}`);
    wires.setAttribute("width", String(width));
    wires.setAttribute("height", String(height));
    const label = labratElement("topology-orphan-label");
    label.hidden = layout.orphanTop === null;
    if (layout.orphanTop !== null) {
        label.style.top = `${layout.orphanTop}px`;
        label.style.left = `${LABRAT_LAYOUT.margin}px`;
    }
}

/**
 * Open or collapse the floating map key and remember the choice.
 *
 * Collapsed, the panel slides off to the left and only the "Key" tab is
 * reachable; open, only the panel is. Focus follows to the control that
 * replaces the one just used.
 *
 * @param {boolean} open - Whether the key should be visible.
 * @param {boolean} [remember] - Save the choice in browser storage.
 * @param {boolean} [moveFocus] - Focus the newly visible control.
 * @returns {void}
 */
function labratSetKeyOpen(open, remember = true, moveFocus = false) {
    const key = labratElement("topology-key");
    key.dataset.open = String(open);
    labratElement("topology-key-panel").inert = !open;
    labratElement("topology-key-show").inert = open;
    labratElement("topology-key-hide").setAttribute("aria-expanded", String(open));
    labratElement("topology-key-show").setAttribute("aria-expanded", String(open));
    if (remember) {
        try {
            localStorage.setItem(LABRAT_KEY_OPEN_KEY, String(open));
        } catch {
            // The key still toggles for this page if storage is unavailable.
        }
    }
    if (moveFocus) {
        labratElement(open ? "topology-key-hide" : "topology-key-show").focus();
    }
}

/**
 * Restore the key's last state, or start collapsed on a narrow canvas.
 *
 * @returns {void}
 */
function labratStartKey() {
    let saved = null;
    try {
        saved = localStorage.getItem(LABRAT_KEY_OPEN_KEY);
    } catch {
        // No stored preference; fall back to the canvas width.
    }
    const open = saved === null
        ? labratElement("topology-canvas").clientWidth >= LABRAT_KEY_NARROW_WIDTH
        : saved === "true";
    labratSetKeyOpen(open, false);
    labratElement("topology-key-hide").addEventListener("click", () => labratSetKeyOpen(false, true, true));
    labratElement("topology-key-show").addEventListener("click", () => labratSetKeyOpen(true, true, true));
}

/**
 * Size the canvas to leave the standard page footer inside the browser window.
 *
 * The page itself should not scroll: the map scrolls and pans inside the
 * canvas instead. A very short window keeps a usable minimum height, and
 * then the page scrolls rather than squashing the map.
 *
 * @returns {void}
 */
function labratFitCanvas() {
    const canvas = labratElement("topology-canvas");
    const surface = canvas.closest(".topology-surface");
    const top = canvas.getBoundingClientRect().top + window.scrollY;
    // Space the surrounding panel adds under the canvas (padding and frame),
    // plus a small gap above the standard footer.
    const below = surface.getBoundingClientRect().bottom - canvas.getBoundingClientRect().bottom;
    const footerHeight = document.querySelector(".site-footer")?.getBoundingClientRect().height || 0;
    const height = Math.max(LABRAT_MIN_CANVAS_HEIGHT,
        Math.floor(window.innerHeight - top - below - footerHeight - LABRAT_CANVAS_BOTTOM_GAP));
    if (canvas.style.height !== `${height}px`) {
        canvas.style.height = `${height}px`;
    }
    labratFitStage();
}

/**
 * Let a mouse or pen drag on the map background pan the canvas.
 *
 * Touch keeps the browser's native scrolling, which already pans. Dragging a
 * device card moves that card instead (see `labratStartDrag`). A drag that
 * moved is remembered so the click that ends it does not close the drawer.
 *
 * @returns {void}
 */
function labratStartPanning() {
    const canvas = labratElement("topology-canvas");
    let pan = null;
    // Every new press starts unpanned, wherever it lands, so a finished pan
    // never stops a later outside click from closing the drawer.
    document.addEventListener("pointerdown", () => {
        labratGraph.panMoved = false;
    }, true);
    canvas.addEventListener("pointerdown", (event) => {
        if (event.button !== 0 || event.pointerType === "touch"
            || (event.target instanceof Element && event.target.closest(".topology-node, .topology-node-pin"))) {
            return;
        }
        pan = {
            pointerId: event.pointerId, x: event.clientX, y: event.clientY,
            left: canvas.scrollLeft, top: canvas.scrollTop,
        };
    });
    canvas.addEventListener("pointermove", (event) => {
        if (!pan || event.pointerId !== pan.pointerId) {
            return;
        }
        const dx = event.clientX - pan.x;
        const dy = event.clientY - pan.y;
        if (!labratGraph.panMoved && Math.hypot(dx, dy) < 4) {
            return;
        }
        if (!labratGraph.panMoved) {
            labratGraph.panMoved = true;
            canvas.classList.add("is-panning");
            try {
                canvas.setPointerCapture(event.pointerId);
            } catch {
                // Capture keeps the pan going past the canvas edge; optional.
            }
        }
        canvas.scrollLeft = pan.left - dx;
        canvas.scrollTop = pan.top - dy;
    });
    const stop = (event) => {
        if (pan && event.pointerId === pan.pointerId) {
            pan = null;
            canvas.classList.remove("is-panning");
        }
    };
    canvas.addEventListener("pointerup", stop);
    canvas.addEventListener("pointercancel", stop);
}

/**
 * On first load, scroll a map wider than the canvas so its first root shows.
 *
 * Runs once per page; later refreshes leave the viewer's scroll alone.
 *
 * @returns {void}
 */
function labratCenterOnRoot() {
    const canvas = labratElement("topology-canvas");
    if (labratGraph.centered || !labratGraph.layout || canvas.scrollWidth <= canvas.clientWidth) {
        return;
    }
    const root = labratGraph.nodes.get(labratGraph.layout.roots[0]);
    if (root) {
        labratGraph.centered = true;
        canvas.scrollLeft = root.x * labratGraph.stageScale - canvas.clientWidth / 2;
    }
}

/**
 * Release a pinned card so it returns to its computed tier.
 *
 * @param {object} node - Local graph node.
 * @returns {void}
 */
function labratUnpin(node) {
    if (!node.pinned) {
        return;
    }
    node.pinned = false;
    labratStorePin(node);
    // A new layout has new link pairs, so the cable paths are rebuilt
    // (repainting alone would leave the old paths where the card was).
    labratApplyLayout();
    labratDrawLinks();
}

/**
 * Choose the current discovered label for source-inferred WAN modem cards.
 *
 * Inventory names are user-owned, while the UniFi reported name can follow
 * an ISP label that changes with the observed circuit. Keep that dynamic name
 * on the Discovery map without changing the canonical inventory label.
 *
 * @param {object} record - Topology API node record.
 * @returns {string} Name to display on the Discovery card.
 */
function labratNodeDisplayName(record) {
    const wan = record.observations?.find((item) => item.wan_upstream && item.reported_name);
    return wan?.reported_name || record.name;
}

/**
 * Create the small decorative pin icon used on pinned cards.
 *
 * @returns {SVGSVGElement} Accessible-hidden line icon.
 */
function labratPinIcon() {
    const namespace = "http://www.w3.org/2000/svg";
    const icon = document.createElementNS(namespace, "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("aria-hidden", "true");
    icon.setAttribute("focusable", "false");
    const path = document.createElementNS(namespace, "path");
    path.setAttribute("d", "M9 3h6l-1 6 4 4H6l4-4zM12 13v8");
    icon.append(path);
    return icon;
}

/**
 * Render one safely escaped, keyboard-accessible card per physical device.
 *
 * @returns {void}
 */
function labratDrawNodes() {
    const holder = labratElement("topology-nodes");
    holder.replaceChildren();
    for (const node of labratGraph.nodes.values()) {
        const displayName = labratNodeDisplayName(node.record);
        const button = document.createElement("button");
        button.type = "button";
        button.className = `topology-node topology-node-${node.record.category}`;
        button.classList.toggle("is-unplaced", !node.record.placement);
        button.dataset.nodeId = node.id;
        button.title = displayName;
        button.setAttribute("aria-label", `Inspect ${displayName}`
            + (node.record.placement ? "" : " (unplaced)"));
        const kind = labratText("span", "", "topology-node-kind");
        kind.append(
            labratDeviceIcon(node.record.category, "topology-node-icon"),
            labratText("span", LABRAT_SHORT_CATEGORY[node.record.category]
                || node.record.category.replaceAll("_", " "), "topology-node-category"),
        );
        if (!node.record.placement) {
            kind.append(labratText("span", "Unplaced", "topology-node-flag"));
        }
        button.append(kind, labratText("strong", displayName, "topology-node-name"));
        button.addEventListener("click", () => {
            // A drag ends with a click; it should not reopen the inspector.
            if (labratGraph.suppressClick) {
                labratGraph.suppressClick = false;
                return;
            }
            labratSelect(node.id);
        });
        button.addEventListener("dblclick", () => labratUnpin(node));
        button.addEventListener("pointerenter", () => labratSetHover(node.id));
        button.addEventListener("pointerleave", () => labratSetHover(null));
        button.addEventListener("focus", () => labratSetHover(node.id));
        button.addEventListener("blur", () => labratSetHover(null));
        button.addEventListener("pointerdown", (event) => labratStartDrag(event, node));
        button.addEventListener("pointermove", (event) => labratMoveDrag(event, node));
        button.addEventListener("pointerup", (event) => labratEndDrag(event, node));
        button.addEventListener("pointercancel", (event) => labratEndDrag(event, node));
        node.element = button;
        // The pin marker is its own button beside the card (buttons cannot
        // nest); clicking it releases the card back to its tier.
        const pin = document.createElement("button");
        pin.type = "button";
        pin.className = "topology-node-pin";
        pin.title = "Unpin: return this card to its tier";
        pin.setAttribute("aria-label", `Unpin ${labratNodeDisplayName(node.record)}`);
        pin.append(labratPinIcon());
        pin.addEventListener("click", () => labratUnpin(node));
        node.pinElement = pin;
        holder.append(button, pin);
    }
    labratPaint();
}

/**
 * Find the reported port record a link names on one of its devices.
 *
 * @param {string} nodeId - Graph node ID.
 * @param {string} portName - Port name from the link.
 * @returns {object|undefined} Port record from the topology API.
 */
function labratLinkPort(nodeId, portName) {
    return labratGraph.nodes.get(nodeId)?.record.ports.find((port) => port.name === portName);
}

/**
 * Describe how one device pair's cables should look.
 *
 * Color follows the cable medium and width follows link speed, as in the
 * Equipment view; the strongest link in the pair sets the dash style.
 *
 * @param {object} pair - Link group from the layered layout.
 * @returns {{classes:string[], title:string}} CSS classes and tooltip text.
 */
function labratPairStyle(pair) {
    const classes = ["topology-cable"];
    if (!pair.links.length && pair.usb.length) {
        classes.push("is-usb");
        return { classes, title: "USB to the NUT server it reports through · not a network cable" };
    }
    if (!pair.links.length && pair.relay.length) {
        classes.push("is-relay");
        return { classes, title: "Reports through this NUT server · values relayed into NUT "
            + "by dummy-ups, not a USB or network connection" };
    }
    if (!pair.links.length) {
        classes.push("is-wireless");
        return { classes, title: "Inferred Wi-Fi client path · not a cable" };
    }
    const rank = { confirmed: 3, inferred: 2, ambiguous: 1 };
    const best = pair.links.reduce((top, link) => (
        (rank[link.confidence] || 0) > (rank[top.confidence] || 0) ? link : top
    ));
    if (best.confidence === "inferred") classes.push("is-inferred");
    if (best.confidence === "ambiguous") classes.push("is-ambiguous");
    let medium = null;
    let speed = 0;
    for (const link of pair.links) {
        for (const port of [labratLinkPort(link.from, link.from_port), labratLinkPort(link.to, link.to_port)]) {
            medium = medium || port?.medium || null;
            if (port?.link_up !== false) speed = Math.max(speed, Number(port?.speed_mbps) || 0);
        }
    }
    classes.push(`medium-${medium ? medium.replaceAll("_", "-") : "unknown"}`);
    classes.push(speed >= 10000 ? "speed-fast" : speed >= 2500 ? "speed-mid" : "speed-base");
    if (pair.links.length > 1) classes.push("is-multiple");
    const lines = pair.links.map((link) => (
        `${link.from_port} ↔ ${link.to_port} · ${link.confidence} · ${link.evidence} · ${link.source}`
    ));
    const count = pair.links.length > 1 ? `${pair.links.length} cables` : "1 cable";
    return { classes, title: `${count}${medium ? ` · ${medium.replaceAll("_", " ")}` : ""}`
        + `${speed ? ` · ${labratFormatSpeed(speed)}` : ""}\n${lines.join("\n")}` };
}

/**
 * Create one SVG path per connected device pair, routed by its tree role.
 *
 * @returns {void}
 */
function labratDrawLinks() {
    const layer = labratElement("topology-lines");
    layer.replaceChildren();
    const layout = labratGraph.layout;
    if (!layout) {
        return;
    }
    for (const pair of layout.pairs.values()) {
        const childOf = (child, above) => layout.parent.get(child) === above;
        let kind = "cross";
        let upper = pair.from;
        let lower = pair.to;
        if (childOf(pair.to, pair.from) || childOf(pair.from, pair.to)) {
            [upper, lower] = childOf(pair.to, pair.from) ? [pair.from, pair.to] : [pair.to, pair.from];
            kind = layout.stacked.has(lower) ? "stack" : "tree";
        } else if (pair.links.some((link) => link.evidence === "unifi-wan-upstream")) {
            // Several WAN modems can share one gateway. Keep their cables
            // entering the gateway from above even though a drawing tree can
            // assign only one modem as its parent.
            [upper, lower] = layout.positions.get(pair.from).y <= layout.positions.get(pair.to).y
                ? [pair.from, pair.to] : [pair.to, pair.from];
            kind = "tree";
        } else {
            // A second link into a stacked endpoint from a device drawn above
            // it (a server's management NIC) gets right angles too.
            const [a, b] = [layout.positions.get(pair.from), layout.positions.get(pair.to)];
            if (layout.stacked.has(pair.to) && a.y < b.y) {
                kind = "side";
            } else if (layout.stacked.has(pair.from) && b.y < a.y) {
                [upper, lower] = [pair.to, pair.from];
                kind = "side";
            }
        }
        const style = labratPairStyle(pair);
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.classList.add(...style.classes, `route-${kind}`);
        const tooltip = document.createElementNS("http://www.w3.org/2000/svg", "title");
        tooltip.textContent = style.title;
        path.append(tooltip);
        pair.element = path;
        pair.route = { kind, upper, lower };
        layer.append(path);
    }
    labratPaint();
}

/**
 * Set or clear the hovered device whose path to the top is traced.
 *
 * @param {string|null} id - Hovered node ID, or none.
 * @returns {void}
 */
function labratSetHover(id) {
    if (labratGraph.drag) {
        return;
    }
    labratGraph.hoverId = id;
    labratPaint();
}

/**
 * Paint cards and cables at their positions and trace the focused path.
 *
 * The hovered device, or else the selected one, is traced to the top of its
 * tree; unrelated cards and cables are dimmed.
 *
 * @returns {void}
 */
function labratPaint() {
    const layout = labratGraph.layout;
    const focus = labratGraph.hoverId || labratGraph.selectedId;
    const traced = new Set(focus && layout ? labratPathToRoot(layout.parent, focus) : []);
    labratElement("topology-canvas").classList.toggle("is-tracing", traced.size > 0);
    for (const node of labratGraph.nodes.values()) {
        if (!node.element) {
            continue;
        }
        node.element.style.left = `${node.x}px`;
        node.element.style.top = `${node.y}px`;
        node.element.classList.toggle("is-selected", node.id === labratGraph.selectedId);
        node.element.classList.toggle("is-pinned", node.pinned);
        if (node.pinElement) {
            // Inside the card at the end of the name row, only while pinned.
            node.pinElement.hidden = !node.pinned;
            node.pinElement.style.left = `${node.x + LABRAT_LAYOUT.nodeWidth / 2 - LABRAT_PIN_INSET}px`;
            node.pinElement.style.top = `${node.y + LABRAT_LAYOUT.nodeHeight / 2 - LABRAT_PIN_INSET}px`;
        }
        node.element.classList.toggle("is-traced", traced.has(node.id));
    }
    for (const pair of layout?.pairs.values() || []) {
        if (!pair.element) {
            continue;
        }
        const upper = labratGraph.nodes.get(pair.route.upper);
        const lower = labratGraph.nodes.get(pair.route.lower);
        if (!upper || !lower) {
            continue;
        }
        // A relayed UPS runs beside its server's USB line instead of on it,
        // so the dotted reporting line never merges with a real connection.
        const offset = pair.relay.length && !pair.usb.length && !pair.links.length ? -6 : 0;
        pair.element.setAttribute(
            "d", labratRoutePath(pair.route.kind, upper, lower, offset, labratGraph.wireStyle === "curved")
        );
        const onPath = pair.route.kind !== "cross"
            && traced.has(pair.route.upper) && traced.has(pair.route.lower);
        const touches = focus && (pair.from === focus || pair.to === focus);
        pair.element.classList.toggle("is-traced", Boolean(onPath));
        pair.element.classList.toggle("is-related", Boolean(touches && !onPath));
    }
}

/**
 * Convert a pointer event into unscaled stage coordinates.
 *
 * @param {PointerEvent} event - Browser pointer coordinates.
 * @returns {{x:number,y:number}} Stage position in pixels.
 */
function labratPointerCoordinates(event) {
    const bounds = labratElement("topology-stage").getBoundingClientRect();
    const scale = labratGraph.stageScale || 1;
    return {
        x: (event.clientX - bounds.left) / scale,
        y: (event.clientY - bounds.top) / scale,
    };
}

/**
 * Begin a possible drag of one diagram card, never its inventory placement.
 *
 * @param {PointerEvent} event - Pointer-down event.
 * @param {object} node - Local node to manipulate.
 * @returns {void}
 */
function labratStartDrag(event, node) {
    if (event.button !== 0) {
        return;
    }
    const point = labratPointerCoordinates(event);
    labratGraph.drag = {
        id: node.id, pointerId: event.pointerId, moved: false,
        dx: node.x - point.x, dy: node.y - point.y, startX: point.x, startY: point.y,
    };
}

/**
 * Move a card once the pointer has travelled far enough to mean a drag.
 *
 * @param {PointerEvent} event - Pointer-move event.
 * @param {object} node - Dragged graph node.
 * @returns {void}
 */
function labratMoveDrag(event, node) {
    const drag = labratGraph.drag;
    if (!drag || drag.id !== node.id || drag.pointerId !== event.pointerId) {
        return;
    }
    const point = labratPointerCoordinates(event);
    if (!drag.moved && Math.hypot(point.x - drag.startX, point.y - drag.startY) < 5) {
        return;
    }
    if (!drag.moved) {
        drag.moved = true;
        try {
            node.element.setPointerCapture(event.pointerId);
        } catch {
            // Capture keeps a fast drag on the card; dragging works without it.
        }
        node.element.classList.add("is-dragging");
    }
    const L = LABRAT_LAYOUT;
    node.x = Math.max(L.nodeWidth / 2, point.x + drag.dx);
    node.y = Math.max(L.nodeHeight / 2, point.y + drag.dy);
    labratPaint();
}

/**
 * Finish a drag by pinning the card where it was dropped.
 *
 * @param {PointerEvent} event - Pointer-up or cancellation event.
 * @param {object} node - Formerly dragged node.
 * @returns {void}
 */
function labratEndDrag(event, node) {
    const drag = labratGraph.drag;
    if (!drag || drag.id !== node.id || drag.pointerId !== event.pointerId) {
        return;
    }
    labratGraph.drag = null;
    node.element.classList.remove("is-dragging");
    if (!drag.moved) {
        return;
    }
    labratGraph.suppressClick = event.type === "pointerup";
    node.pinned = true;
    labratStorePin(node);
    labratFitStage();
    labratPaint();
}

/**
 * Highlight the selected device and populate its inspector.
 *
 * @param {string} id - Stable graph node identifier.
 * @returns {void}
 */
function labratSelect(id) {
    labratGraph.selectedId = id;
    if (labratGraph.drawer.currentView !== "details") {
        // Details is the first panel; Back from it closes the drawer.
        labratGraph.drawer.reset("details", [], "forward");
    }
    labratGraph.drawer.open();
    // The address names the open device, so a refresh or a link reopens it.
    labratShowDeviceInUrl("/topology", id);
    labratPaint();
    labratRenderInspector();
    labratUpdateDrawerBar();
    labratFitCanvas();
    labratElement("device-drawer-title").focus({ preventScroll: true });
}

/**
 * Close the tools drawer (Close, Escape, an outside click, or Back from the
 * first panel). Nothing in inventory changes.
 *
 * @returns {void}
 */
function labratCloseDrawer() {
    labratGraph.drawer.close();
}

/**
 * After the drawer closes: rest on the empty "shelf" panel and clear the
 * selection it was showing.
 *
 * @returns {void}
 */
function labratDrawerClosed() {
    labratShowDeviceInUrl("/topology", null);
    labratGraph.selectedId = null;
    labratGraph.drawer.reset("shelf");
    labratPaint();
}

/**
 * Show the active panel's title in the drawer bar (the shared details panel
 * titles itself with the selected device's name).
 *
 * @returns {void}
 */
function labratUpdateDrawerBar() {
    labratGraph.drawer.updateBar();
}

/**
 * Show the selected device in the shared details panel (DeviceDetails).
 *
 * @returns {void}
 */
function labratRenderInspector() {
    labratGraph.details.render(labratGraph.nodes.get(labratGraph.selectedId)?.record || null);
}

/**
 * Load rooms, places, racks, and inventory for the placement editor.
 *
 * @returns {Promise<void>} Resolves when the locations are stored.
 */
async function labratLoadLocations() {
    labratGraph.locations = await labratJson(LABRAT_API.locations);
}

/**
 * Refresh the cached topology without resetting positions for existing nodes.
 *
 * @returns {Promise<void>} Resolves after API data is displayed.
 */
async function labratRefresh() {
    const data = await labratJson(LABRAT_API.topology);
    await labratLoadLocations();
    labratSyncTopology(data);
}

/**
 * Clear diagram-only pins and return every card to its tier.
 *
 * @returns {void}
 */
function labratResetLayout() {
    try {
        localStorage.removeItem(LABRAT_PINS_KEY);
    } catch {
        // Local storage is optional.
    }
    for (const node of labratGraph.nodes.values()) {
        node.pinned = false;
    }
    labratApplyLayout();
    labratDrawLinks();
}

/**
 * Switch every Discovery path's bends without changing layout or selection.
 *
 * @returns {void}
 */
function labratToggleWireStyle() {
    labratGraph.wireStyle = labratGraph.wireStyle === 'curved' ? 'square' : 'curved';
    labratSaveWireStyle('labrat.topology.wireStyle', labratGraph.wireStyle);
    labratUpdateWireStyleButton(labratElement('topology-wire-style'), labratGraph.wireStyle);
    labratPaint();
}

/**
 * Initialize the topology page's graph, forms, and refresh loop.
 *
 * @returns {Promise<void>} Resolves after the initial topology load.
 */
async function labratStartTopology() {
    if (!labratElement("topology-app")) {
        return;
    }
    // The shared drawer shell owns the panel stack, slide, Back/Close,
    // Escape, and outside-click dismissal. Cards, pins, and the map key keep
    // it open, and the click that ends a pan never closes it.
    labratGraph.drawer = new DeviceDrawer({
        host: labratElement("topology-app"),
        rootView: "shelf",
        closeAtRoot: true,
        onClose: labratDrawerClosed,
        keepsOpen: (target) => Boolean(target.closest(".topology-node, .topology-node-pin, .topology-key")),
        ignoreClick: () => labratGraph.panMoved,
    });
    // Shared details, rename, and neighbor-match panels (device-details.js).
    labratGraph.details = new DeviceDetails({
        ignorable: true,
        drawer: labratGraph.drawer,
        csrfToken: labratElement("topology-app").dataset.csrfToken,
        nodes: () => [...labratGraph.nodes.values()].map((node) => node.record),
        links: () => labratGraph.links,
        neighborMatches: () => labratGraph.neighborMatches,
        locations: () => labratGraph.locations,
        reload: labratRefresh,
        notify: (message, ok = false) => labratNotice(message, !ok),
        onPlace: (record) => labratGraph.placement.open(record),
    });
    // Shared placement editor and "Add new place" (device-placement.js).
    labratGraph.placement = new DevicePlacement({
        drawer: labratGraph.drawer,
        csrfToken: labratElement("topology-app").dataset.csrfToken,
        locations: () => labratGraph.locations,
        reload: labratRefresh,
        notify: (message, ok = false) => labratNotice(message, !ok),
    });
    labratGraph.wireStyle = labratReadWireStyle('labrat.topology.wireStyle', 'curved');
    const wireButton = labratElement('topology-wire-style');
    labratUpdateWireStyleButton(wireButton, labratGraph.wireStyle);
    wireButton.addEventListener('click', labratToggleWireStyle);
    labratGraph.unplacedOnly = labratReadUnplacedPreference();
    labratElement("topology-unplaced-only").checked = labratGraph.unplacedOnly;
    labratElement("topology-refresh").addEventListener("click", async () => {
        try {
            await labratRefresh();
        } catch (error) {
            labratNotice(error.message, true);
        }
    });
    labratElement("topology-reset").addEventListener("click", labratResetLayout);
    // Keep the canvas above the standard footer. The header, footer and
    // loaded counts can change height as text wraps or web fonts load, so
    // watch the whole page. The fit only writes a changed height, so its
    // own resize settles after one pass.
    labratFitCanvas();
    window.addEventListener("resize", labratFitCanvas);
    document.fonts?.ready.then(labratFitCanvas);
    if ("ResizeObserver" in window) {
        new ResizeObserver(() => labratFitCanvas()).observe(document.body);
    }
    labratElement("topology-unplaced-only").addEventListener("change", (event) => {
        labratGraph.unplacedOnly = event.currentTarget.checked;
        labratSaveUnplacedPreference();
        labratRenderEmptyState();
    });
    labratStartPanning();
    labratStartKey();
    try {
        await labratRefresh();
        // A device link (/topology/35) opens that device's details.
        const requested = labratElement("topology-app").dataset.openDevice;
        if (requested && labratGraph.nodes.has(requested)) labratSelect(requested);
        else if (requested) labratShowDeviceInUrl("/topology", null);
    } catch (error) {
        labratNotice(error.message, true);
    }
    // Poll only LabRat's SQLite-backed API; never query a controller in JS.
    window.setInterval(async () => {
        if (document.hidden) {
            return;
        }
        try {
            await labratRefresh();
        } catch (error) {
            labratNotice(`Refresh failed: ${error.message}`, true);
        }
    }, 12000);
}

void labratStartTopology();
