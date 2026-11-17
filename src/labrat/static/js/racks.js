/*
 * Copyright 2026 LabRat contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Rack View uses cached discovery and canonical inventory placement only.
 * Device names, ports, and links are rendered as text, never as HTML source.
 */

"use strict";

/**
 * Racks are drawn to scale: one rack unit is 1.75 inches tall, and the
 * mounting width is the rack's rail width (19 or 10 inches). applyProportions
 * takes the pixels per inch from the bay a 19-inch face would get, so
 * two-sided racks of either width share one scale; a front-only rack fills
 * its panel instead, scaled to its own rail width (owner). --rack-u in
 * rack-elevation.css is only the first guess.
 */
const RACK_UNIT_INCHES = 1.75;

/**
 * A source's collection counts as fresh for this long; the telemetry gauge
 * drains over it and the Scan stale tile lights after it (matches the
 * header's FRESH_WINDOW in pages/status.py).
 */
const RACK_FRESH_MILLISECONDS = 30 * 60000;

/**
 * Describe where a source is in its collection cycle. With a recorded loop
 * interval, the next poll is due one interval after the last attempt, and
 * the source is overdue once two intervals and a minute have passed with no
 * attempt (one missed cycle is tolerated). Without an interval, a source is
 * overdue when its last success is older than the fresh window.
 *
 * @param {object} source - Source record from the topology API.
 * @param {number} now - Current time in milliseconds.
 * @returns {{interval: number|null, untilNext: number|null, overdue: boolean, sinceSuccess: number}}
 *     Interval and time to the next poll (negative once due), in milliseconds.
 */
function rackScanCycle(source, now) {
    const sinceSuccess = source.last_success_at ? now - new Date(source.last_success_at).getTime() : Infinity;
    const interval = Number(source.poll_interval_seconds) > 0 ? source.poll_interval_seconds * 1000 : null;
    const attempt = source.last_attempt_at ? new Date(source.last_attempt_at).getTime() : null;
    if (!interval || attempt === null) {
        return { interval: null, untilNext: null, overdue: sinceSuccess > RACK_FRESH_MILLISECONDS, sinceSuccess };
    }
    const untilNext = attempt + interval - now;
    return { interval, untilNext, overdue: untilNext < -(interval + 60000), sinceSuccess };
}

/**
 * Describe a duration as a short clock readout (shared: deviceAgeReadout in
 * device-details.js).
 *
 * @param {number} milliseconds - Elapsed time.
 * @returns {string} Readout text.
 */
function rackAgeReadout(milliseconds) {
    return deviceAgeReadout(milliseconds);
}

/**
 * Live updates: the page asks /api/v1/sources this often for a new
 * collection, sooner while one is due, and reloads the room only when a
 * source's last attempt or result changed.
 */
const RACK_POLL_IDLE_MILLISECONDS = 60000;
const RACK_POLL_DUE_MILLISECONDS = 10000;

/** How long changed devices, lamps, and new cables stay highlighted. */
const RACK_CHANGE_FLASH_MILLISECONDS = 4000;

/** Drawer views with an unsaved form; live updates wait until they close. */
const RACK_EDITING_VIEWS = new Set([
    "placement", "place", "manual", "rack-edit", "area-edit", "neighbor-match", "rename"
]);

/** Drawer panels about the selected device; the address names it while they show. */
const RACK_DEVICE_VIEWS = new Set(["details", "rename", "neighbor-match", "placement", "place"]);

/**
 * Key a cable by its two endpoints, in a stable order. Link IDs are not
 * stable: the collector replaces generated links on every cycle.
 *
 * @param {object} link - Link record.
 * @returns {string} Endpoint key.
 */
function rackLinkKey(link) {
    return [`${link.from}::${link.from_port}`, `${link.to}::${link.to_port}`].sort().join("|");
}

/**
 * Record a cable path's two end ports (`data-pin-a`, `data-pin-b`), in the
 * port lights' `data-pin` form, so hovering a light can trace its cable.
 *
 * @param {SVGPathElement} path - A cable or pulse path.
 * @param {{from: string, from_port: string, to: string, to_port: string}} link - Its link.
 * @returns {void}
 */
function rackTagCableEnds(path, link) {
    path.dataset.pinA = `${link.from}::${link.from_port}`;
    path.dataset.pinB = `${link.to}::${link.to_port}`;
}

/**
 * Largest bend radius for Equipment cables. Parallel lanes run 3-4 px apart,
 * so turns stay tight enough that neighbouring cables never merge; shorter
 * segments shrink it further (wirePath).
 */
const RACK_CABLE_BEND_RADIUS = 6;

/**
 * Round the turns of a square cable path (absolute M, L, H, and V commands
 * from Equipment's cable drawing) with the shared `wirePath` (wiring.js),
 * keeping its first and last points. Anything else (another subpath,
 * curves, unparseable numbers) is returned unchanged.
 *
 * @param {string} d - Square SVG path data.
 * @param {number} [radius=RACK_CABLE_BEND_RADIUS] - Largest bend radius.
 * @returns {string} Path data with rounded turns.
 */
function rackRoundedPath(d, radius = RACK_CABLE_BEND_RADIUS) {
    const points = wireSquarePoints(d);
    return points && points.length >= 3 ? wirePath(points, true, radius) : d;
}

/**
 * Give an Equipment cable its path with rounded turns. The square route it
 * was built from stays on `data-route` for checks and debugging.
 *
 * @param {SVGPathElement} path - The cable, pulse, or power line.
 * @param {string} d - Square SVG path data.
 * @returns {void}
 */
function rackSetCablePath(path, d) {
    path.dataset.route = d;
    path.setAttribute("d", rackRoundedPath(d));
}

/**
 * The first visible, focusable element after a container in page order,
 * where Tab goes once a patch popup's last link is passed.
 *
 * @param {HTMLElement} container - The element to step past.
 * @returns {HTMLElement|null} The next focus target, if any.
 */
function rackFocusableAfter(container) {
    const candidates = document.querySelectorAll("a[href], button:not([disabled]), input, select, textarea, [tabindex='0']");
    return [...candidates].find((element) => !container.contains(element)
        && (container.compareDocumentPosition(element) & Node.DOCUMENT_POSITION_FOLLOWING)
        && element.getClientRects().length > 0) || null;
}

/**
 * Describe a cable medium for people (shared: deviceMediumLabel in
 * device-details.js).
 *
 * @param {string|null} medium - Stored medium value.
 * @returns {string} Readable label, or "unknown cable".
 */
function rackMediumLabel(medium) {
    return deviceMediumLabel(medium);
}

/**
 * Positions cables stack into before the stacking rotates back to the first
 * (owner): the top tray under a plank, and runs leaving a device downward.
 */
const RACK_STACK_POSITIONS = 8;

/**
 * Gap between stacked rack devices, in pixels: odd, so a wire bundle along
 * its middle (nextExit) has the same clear space above and below (owner).
 * Each device is inset by half of it at its top and bottom.
 */
const RACK_DEVICE_GAP = 5;

/** Lanes in the side channel right of a rack face before they rotate (owner). */
const RACK_CHANNEL_POSITIONS = 16;

/** Below this device height only the name and a port strip are shown. */
const RACK_UNIT_COMPACT_PIXELS = 30;
/**
 * Below this height (and from the compact one up) the stacked faceplate has
 * no room: the name and full-size lights share one row, lights at the bottom.
 */
const RACK_UNIT_SHORT_PIXELS = 40;
/** Below this device height lamps are short and the name small (is-tiny). */
const RACK_UNIT_TINY_PIXELS = 15;

/**
 * Return a required page element with a useful development error.
 *
 * @param {string} id - The element's ID.
 * @returns {HTMLElement} The element.
 * @throws {Error} When the template does not have it.
 */
function rackElement(id) {
    const element = document.getElementById(id);
    if (!element) {
        throw new Error(`Rack View element is missing: ${id}`);
    }
    return element;
}

/**
 * Create a safe text-only DOM element.
 *
 * @param {string} tag - The element's tag name.
 * @param {string} className - Its class attribute.
 * @param {*} value - Its text, converted with String().
 * @returns {HTMLElement} The element.
 */
function rackText(tag, className, value) {
    const element = document.createElement(tag);
    element.className = className;
    element.textContent = String(value);
    return element;
}

/**
 * Convert API class names into readable labels without changing identity.
 *
 * @param {?string} value - A category, such as "access_point".
 * @returns {string} The label, such as "access point"; "equipment" when missing.
 */
function rackCategory(value) {
    return String(value || "equipment").replaceAll("_", " ");
}

/**
 * Read JSON through the current signed browser session.
 *
 * @param {string} path - Same-origin API path.
 * @param {Object|null} payload - Optional JSON request body.
 * @param {string} csrfToken - Signed session's CSRF token for writes.
 * @returns {Promise<Object>} Parsed response body.
 */
/**
 * Line icons for the rack toolbar, drawn like Discovery's device icons
 * (24-unit grid, stroked paths, sized and colored by CSS).
 */
const RACK_TOOL_ICONS = {
    // Four-way arrows: reorder up, down, and sideways.
    arrange: "M12 3v18M3 12h18M9 6l3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3M18 9l3 3-3 3",
    // A plus inside a rack slot.
    add: "M3 5h18v14H3zM12 9v6M9 12h6",
    // A pencil.
    edit: "M4 20h4L19 9l-4-4L4 16zM14 6l4 4",
    rename: "M4 20h4L19 9l-4-4L4 16zM14 6l4 4",
    // A bin; deletes ask first (remove-dialog.js).
    delete: "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3",
};

/**
 * Build one icon button for a rack's toolbar.
 *
 * The visible label is an icon only, so the button carries an accessible
 * name and a styled tooltip (`data-tooltip`, drawn by racks.css on hover and
 * keyboard focus) with the same text.
 *
 * @param {"arrange"|"add"|"edit"|"rename"|"delete"} tool - Which tool, also its icon.
 * @param {string} label - What the button does, e.g. "Edit rack".
 * @param {Function} onClick - Click handler.
 * @returns {HTMLButtonElement} The button.
 */
function rackToolButton(tool, label, onClick) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "rack-tool";
    button.dataset.tool = tool;
    button.dataset.tooltip = label;
    button.setAttribute("aria-label", label);
    const namespace = "http://www.w3.org/2000/svg";
    const icon = document.createElementNS(namespace, "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("aria-hidden", "true");
    icon.setAttribute("focusable", "false");
    icon.classList.add("rack-tool-icon");
    const path = document.createElementNS(namespace, "path");
    path.setAttribute("d", RACK_TOOL_ICONS[tool]);
    icon.append(path);
    button.append(icon);
    button.addEventListener("click", onClick);
    return button;
}

/** Coordinate rack elevations, discovery selection, and explicit edits. */
class RackViewApp {
    /**
     * Initialize in-memory presentation state; inventory remains on the server.
     *
     * @param {HTMLElement} root - The page's #rack-app element, carrying the
     *     room ID and CSRF token in its data attributes.
     */
    constructor(root) {
        this.root = root;
        this.csrfToken = root.dataset.csrfToken || "";
        this.roomId = Number(root.dataset.roomId) || null;
        this.allRacks = [];
        this.racks = [];
        this.roomContainers = [];
        this.nodes = [];
        this.links = [];
        this.inventory = [];
        // The rack whose details or placements are being edited. The page
        // shows every rack in the room; this is just the one in focus.
        this.rackId = null;
        this.selectedId = null;
        // Right-column panels reorder by their grips, per browser
        // (rack-panel-order.js); cables to moved panels redraw.
        this.panelOrder = new RackPanelOrder(root.querySelector(".rack-side-column"), {
            onChange: () => this.queueCables(),
        });
        this.panelOrder.init();
        // The shared drawer shell (device-drawer.js) owns the panel stack,
        // the slide, Back/Close, Escape, and outside-click dismissal.
        this.drawer = new DeviceDrawer({
            host: root,
            rootView: "shelf",
            // Leaving an editing panel releases a held live update.
            onActivate: (view, previous) => {
                // Leaving the rack editor saves it (rack-editor.js commit).
                if (previous === "rack-edit" && view !== "rack-edit") void this.rackEditor?.commit();
                if (previous === "area-edit" && view !== "area-edit") void this.areaEditor?.commit();
                if (this.updateHeld && !RACK_EDITING_VIEWS.has(view)) setTimeout(() => this.applyUpdate(), 0);
                // The address names the device only while its panels show.
                if (!RACK_DEVICE_VIEWS.has(view)) this.showDeviceInUrl(null);
            },
            onClose: () => this.drawerClosed(),
        });
        // The shared rack editor (rack-editor.js): its Rack menu, shelves,
        // Delete rack, and saving on close are all its own.
        this.rackEditor = new RackEditor({
            csrfToken: this.csrfToken,
            racks: () => this.racks.map((rack) => ({ ...rack, location: `${rack.siteName} / ${rack.roomName}` })),
            shelfCount: (shelfId) => this.nodes.filter((node) => node.placement?.container_id === shelfId).length,
            reload: () => this.load({ quiet: true }),
            notify: (message, ok) => this.showNotice(message, ok),
            close: () => this.closeDrawer(),
        });
        // Edit area (area-editor.js), from the heading's pencil (owner):
        // the drawer's first panel, saved when it closes.
        this.areaEditor = new AreaEditor({
            csrfToken: this.csrfToken,
            show: () => {
                this.openDrawer();
                this.viewStack = [];
                this.activateView("area-edit", "forward");
                this.updateToolStates();
            },
            notify: (message, ok) => this.showNotice(message, ok),
        });
        // Shared details, rename, and neighbor-match panels (device-details.js).
        this.details = new DeviceDetails({
            drawer: this.drawer,
            csrfToken: this.csrfToken,
            nodes: () => this.nodes,
            links: () => this.links,
            neighborMatches: () => this.neighborMatches || [],
            locations: () => this.locationsData || null,
            reload: () => this.load({ quiet: true }),
            notify: (message, ok = false) => this.showNotice(message, ok),
            // Open on the rack in focus when the device is not placed yet.
            onPlace: (node) => this.placement.open(node, {
                roomId: this.roomId, containerId: this.currentRack()?.containerId || null,
            }),
        });
        // Shared placement editor and "Add new place" (device-placement.js).
        this.placement = new DevicePlacement({
            drawer: this.drawer,
            csrfToken: this.csrfToken,
            locations: () => this.locationsData,
            reload: () => this.load({ quiet: true }),
            notify: (message, ok = false) => this.showNotice(message, ok),
            // A discovery placed for the first time gets an inventory ID.
            onSaved: (result) => { this.selectedId = `device:${result.device_id}`; },
        });
        this.notice = "";
        // Arrange mode: a RackArrangement draft (rack-arrange.js) while the
        // administrator drags devices, else null. `drag` is the pointer drag
        // in progress.
        this.arrangement = null;
        this.drag = null;
        this.sources = [];
        this.ignoreTargets = new Set();
        this.events = [];
        this.linkState = "live";
        // Which port lamps a crowded faceplate draws (rack-port-lights.js).
        this.portLightMode = rackReadPortLightMode();
        this.bindEvents();
    }

    /** Attach controls once so rerendering device lists does not duplicate events. */
    bindEvents() {
        // Hovering a port light (on a faceplate or a card) traces
        // its cable to the other end; leaving the light clears it.
        document.addEventListener("pointerover", (event) => {
            const light = event.target.closest?.(".rack-port-light");
            const pin = light?.dataset.pin || null;
            if (pin === this.tracedPin) return;
            this.tracedPin = pin;
            this.markCableTrace();
        });
        rackElement("rack-refresh").addEventListener("click", () => this.refreshNow());
        rackElement("rack-arrange-cancel").addEventListener("click", () => this.cancelArrange());
        rackElement("rack-arrange-save").addEventListener("click", () => this.saveArrange());
        // The room's Arrange and Add equipment tools sit in the frame heading
        // beside Refresh (owner); Delete rack is in the rack editor (below).
        rackElement("rack-empty-add").addEventListener("click", () => {
            this.toolOpener = { tool: "add", rackId: null };
            this.openEquipmentList();
        });
        rackElement("rack-arrange-tool").addEventListener("click", () => {
            this.toolOpener = { tool: "arrange", rackId: null };
            this.startArrange();
        });
        // Edit rack is a heading tool too: it opens the rack last worked on,
        // or the first; a room with several racks switches in the editor.
        rackElement("rack-edit-tool").addEventListener("click", () => {
            if (!this.currentRack()) this.rackId = this.racks[0]?.id ?? null;
            this.toolOpener = { tool: "edit", rackId: null };
            this.openRackEditor();
        });
        rackElement("room-wiring-zoom-in").addEventListener("click", () => this.roomWiring?.zoom(1.25));
        rackElement("room-wiring-zoom-out").addEventListener("click", () => this.roomWiring?.zoom(0.8));
        rackElement("room-wiring-zoom-fit").addEventListener("click", () => this.roomWiring?.zoomToFit());
        rackElement("room-wiring-style").addEventListener("click", () => this.roomWiring?.toggleWireStyle());
        // Port lights: cycle the crowded-faceplate variants (owner compares them).
        const portLightTool = document.getElementById("rack-port-lights-tool");
        rackUpdatePortLightButton(portLightTool, this.portLightMode);
        portLightTool?.addEventListener("click", () => {
            this.portLightMode = rackNextPortLightMode(this.portLightMode);
            rackSavePortLightMode(this.portLightMode);
            rackUpdatePortLightButton(portLightTool, this.portLightMode);
            this.render({ preserveForms: true });
        });
        // Surface and USB wires are measured against the racks column when
        // drawn. Redraw them when anything there changes size (a badge,
        // late fonts, wrapping) and, while the racks panel is sticky, when
        // the page scrolls and the panel moves within the column.
        this.layoutObserver = "ResizeObserver" in window
            ? new ResizeObserver(() => this.queueCables()) : null;
        window.addEventListener("scroll", () => {
            const panel = document.querySelector(".rack-main-panel");
            if (panel && getComputedStyle(panel).position === "sticky") this.queueCables();
        }, { passive: true });
        // Refit rack unit heights when the window changes size.
        let resizeTimer = null;
        window.addEventListener("resize", () => {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(() => this.render({ preserveForms: true }), 150);
        });
        // The shared Add equipment list (equipment-list.js): search, filter
        // toggles, and rows that open a device's details.
        this.equipmentList = new EquipmentList({
            items: () => this.nodes,
            inRoom: (node) => node.placement?.container_room_id === this.roomId,
            where: (node) => (node.placement
                ? `in ${this.roomNameFor(node.placement.container_room_id) || node.placement.container_name || "another place"}`
                : (node.discovery_id ? "unplaced" : "manual, unplaced")),
            onSelect: (node) => this.select(node.id),
            onManual: () => this.openManualView(),
            isSelected: (node) => node.id === this.selectedId,
        });
        rackElement("rack-manual-cancel").addEventListener("click", () => this.cancelManualView());
        rackElement("rack-manual-form").addEventListener("submit", (event) => this.createManual(event));
        rackElement("rack-manual-u").addEventListener("input", () => this.manualFeedback(""));
        rackElement("rack-manual-place").addEventListener("change", () => this.updateManualFields());
        rackElement("rack-manual-face").addEventListener("change", () => this.suggestManualPosition());
        rackElement("rack-manual-height").addEventListener("change", () => this.suggestManualPosition());
    }

    /**
     * Load cached graph and inventory records without contacting controllers.
     *
     * @param {object} [options] - Load options.
     * @param {boolean} [options.quiet=false] - Keep the summary line as it is
     *     (a background refresh).
     * @returns {Promise<void>} Resolves once the room is drawn.
     */
    async load({ quiet = false } = {}) {
        if (!quiet) rackElement("rack-summary").textContent = "Loading area equipment…";
        try {
            const [topology, locations, policies] = await Promise.all([
                labratJson(LABRAT_API.topology),
                labratJson(LABRAT_API.locations),
                labratJson(LABRAT_API.observationIgnores),
            ]);
            const containers = new Map(locations.containers.filter((item) => item.kind === "rack")
                .map((item) => [item.rack_id, item]));
            const rackTops = new Map(locations.containers.filter((item) => item.kind === "rack_top")
                .map((item) => [item.parent_id, item]));
            const shelvesByRack = new Map();
            for (const shelf of locations.containers.filter((item) => item.kind === "rack_shelf")) {
                if (!shelvesByRack.has(shelf.parent_id)) shelvesByRack.set(shelf.parent_id, []);
                shelvesByRack.get(shelf.parent_id).push(shelf);
            }
            const rooms = new Map(locations.rooms.map((room) => [room.id, room]));
            const sites = new Map(locations.sites.map((site) => [site.id, site]));
            this.allRacks = locations.racks.map((rack) => {
                const container = containers.get(rack.id);
                const room = rooms.get(rack.room_id);
                const site = sites.get(room?.site_id);
                return {
                    ...rack,
                    name: rack.name || container?.name || `Rack ${rack.id}`,
                    containerId: container?.id || null,
                    topContainerId: rackTops.get(container?.id)?.id || null,
                    rackShelves: shelvesByRack.get(container?.id) || [],
                    roomName: room?.name || "Unknown area",
                    siteName: site?.name || "Unknown location",
                };
            });
            this.racks = this.allRacks.filter((rack) => rack.room_id === this.roomId);
            this.roomContainers = locations.containers.filter((item) => item.room_id === this.roomId);
            this.links = topology.links;
            this.wirelessLinks = topology.wireless_links || [];
            this.sources = topology.sources || [];
            this.setIgnoreRules(policies.ignores || []);
            this.inventory = locations.devices;
            this.locationsData = locations;
            this.nodes = this.combineNodes(topology.nodes, locations.devices);
            this.neighborMatches = topology.neighbor_matches || [];
            this.indexCables();
            // An old /racks/<id> link arrives as #rack-<id>: focus that rack.
            const hashMatch = window.location.hash.match(/^#rack-(\d+)$/);
            const requestedId = hashMatch ? Number(hashMatch[1]) : this.rackId;
            this.rackId = this.racks.some((rack) => rack.id === requestedId)
                ? requestedId : (this.racks[0]?.id || null);
            if (!this.nodes.some((node) => node.id === this.selectedId)) {
                this.selectedId = null;
                if (["details", "placement"].includes(this.currentView)) {
                    this.viewStack = [];
                    this.activateView("shelf", "back");
                }
            }
            this.showNotice("");
            this.render();
            if (!this.inspectedFromUrl) {
                this.inspectedFromUrl = true;
                // A device link (/areas/1/equipment/35) or an older ?inspect=
                // link opens that device's details.
                const requestedNode = this.root.dataset.openDevice
                    || new URLSearchParams(window.location.search).get("inspect");
                if (this.nodes.some((node) => node.id === requestedNode)) this.select(requestedNode);
                else if (requestedNode) this.showDeviceInUrl(null);
            }
            if (hashMatch && !this.scrolledToHash) {
                this.scrolledToHash = true;
                document.getElementById(`rack-${this.rackId}`)?.scrollIntoView({ block: "start" });
            }
        } catch (error) {
            if (!quiet) rackElement("rack-summary").textContent = "Area equipment could not be loaded.";
            this.showNotice(error.message);
            throw error;
        }
    }

    /**
     * Add inventory-only devices to the discovery graph for manual equipment.
     *
     * @param {Array<Object>} observed - Grouped recognized observations.
     * @param {Array<Object>} inventory - Canonical devices from the locations API.
     * @returns {Array<Object>} Unified display records without guessing identity.
     */
    combineNodes(observed, inventory) {
        const nodes = [...observed];
        const inventoryById = new Map(inventory.map((device) => [device.id, device]));
        for (const node of nodes) {
            const device = inventoryById.get(node.device_id);
            if (device) {
                node.port_facing = device.port_facing;
            }
        }
        const seen = new Set(observed.filter((node) => node.device_id !== null)
            .map((node) => node.device_id));
        for (const device of inventory) {
            if (seen.has(device.id)) {
                continue;
            }
            nodes.push({
                id: `device:${device.id}`,
                discovery_id: null,
                device_id: device.id,
                name: device.name,
                category: device.class_name || "manual",
                height_u: device.height_u,
                placement: device.placement,
                observations: [],
                ports: [],
            });
        }
        return nodes.sort((left, right) => left.name.localeCompare(right.name));
    }

    /**
     * Return the currently selected rack definition, if one exists.
     *
     * @returns {?object} The rack, or null.
     */
    currentRack() {
        return this.racks.find((rack) => rack.id === this.rackId) || null;
    }

    /**
     * Return the device currently shown in the inspector.
     *
     * @returns {?object} The device record, or null.
     */
    selectedNode() {
        return this.nodes.find((node) => node.id === this.selectedId) || null;
    }

    /** @returns {string} The drawer panel now showing. */
    get currentView() {
        return this.drawer.currentView;
    }

    /** @returns {string[]} Panels Back steps through. */
    get viewStack() {
        return this.drawer.viewStack;
    }

    /** @param {string[]} stack - Panels Back should step through. */
    set viewStack(stack) {
        this.drawer.viewStack = stack;
    }

    /** @returns {boolean} Whether the drawer is open. */
    get drawerOpen() {
        return this.drawer.isOpen;
    }

    /**
     * Show a drawer panel (see DeviceDrawer.activate).
     *
     * @param {string} view - Destination panel's data-view value.
     * @param {string} direction - "forward", "back", or "none".
     * @returns {void}
     */
    activateView(view, direction = "none") {
        this.drawer.activate(view, direction);
    }

    /**
     * Slide forward and remember which view should receive Save or Cancel.
     *
     * @param {string} view - Destination panel's data-view value.
     * @returns {void}
     */
    pushView(view) {
        this.drawer.push(view);
    }

    /**
     * Slide back one step and put keyboard focus in the restored view.
     *
     * @param {string|null} focusId - Optional control to focus after returning.
     * @returns {void}
     */
    returnView(focusId = null) {
        const previous = this.drawer.back();
        const target = focusId || {
            shelf: "equipment-search", details: "device-details-place",
            placement: "device-add-place",
        }[previous];
        if (target) {
            rackElement(target).focus();
        }
    }

    /**
     * Whether a span fits on a rack face (shared: DevicePlacement.spanIsFree).
     *
     * @param {object} rack - The rack.
     * @param {string} face - "front", "rear", or "full_depth".
     * @param {number} height - Units the device needs.
     * @param {number} unit - Its bottom unit.
     * @param {?number} [movingDeviceId=null] - A device being moved, ignored as
     *     an obstacle.
     * @returns {boolean} True when every unit is free.
     */
    spanIsFree(rack, face, height, unit, movingDeviceId = null) {
        return this.placement.spanIsFree(rack, face, height, unit, movingDeviceId);
    }

    /**
     * Lowest free span (shared: DevicePlacement.firstAvailableUnit).
     *
     * @param {object} rack - The rack.
     * @param {string} face - "front", "rear", or "full_depth".
     * @param {number} height - Units the device needs.
     * @param {?number} [movingDeviceId=null] - A device being moved.
     * @param {?number} [preferredUnit=null] - Try this unit first.
     * @returns {?number} The bottom unit, or null when nothing fits.
     */
    firstAvailableUnit(rack, face, height, movingDeviceId = null, preferredUnit = null) {
        return this.placement.firstAvailableUnit(rack, face, height, movingDeviceId, preferredUnit);
    }

    /**
     * Report manual inventory write results beside its own submit button.
     *
     * @param {string} message - The message; empty hides it.
     * @param {boolean} [success=false] - Style it as a success.
     */
    manualFeedback(message, success = false) {
        const feedback = rackElement("rack-manual-feedback");
        feedback.hidden = !message;
        feedback.textContent = message;
        feedback.dataset.state = success ? "success" : "error";
    }

    /** Slide the manual equipment form into the sidebar. */
    openManualView() {
        this.manualFeedback("");
        this.updateManualFields();
        this.pushView("manual");
        rackElement("rack-manual-name").focus();
    }

    /** Discard unfinished manual equipment fields and return to the shelf. */
    cancelManualView() {
        rackElement("rack-manual-form").reset();
        rackElement("rack-manual-u").value = "";
        this.manualFeedback("");
        this.updateManualFields();
        this.returnView("equipment-manual-open");
    }

    /**
     * Slide the equipment drawer in from the right.
     *
     * @param {string} [focusId] - Element to focus once it is open.
     * @returns {void}
     */
    openDrawer(focusId = null) {
        this.drawer.open(focusId);
        this.updateToolStates();
    }

    /**
     * "Add equipment" always opens on the equipment list, ready to search.
     * The list is the drawer's starting view, so on a fresh page the drawer
     * already names it while its panel is still hidden: always activate it.
     */
    openEquipmentList() {
        this.viewStack = [];
        this.activateView("shelf", this.currentView === "shelf" ? "none" : "back");
        this.openDrawer("equipment-search");
    }

    /**
     * The toolbar button that opened the drawer or arrange mode, found again
     * after a redraw (rack headers are rebuilt on every render).
     *
     * @returns {HTMLElement|null} That button, another rack's same tool, or
     *     the empty-room Add equipment button.
     */
    toolOpenerElement() {
        const opener = this.toolOpener || { tool: "add", rackId: null };
        // Arrange, Add equipment, and Edit rack are heading tools.
        const heading = { arrange: "rack-arrange-tool", add: "rack-empty-add", edit: "rack-edit-tool" }[opener.tool];
        if (heading && !rackElement(heading).hidden) return rackElement(heading);
        return this.root.querySelector(`#rack-${opener.rackId} .rack-tool[data-tool="${opener.tool}"]`)
            || this.root.querySelector(`.rack-tool[data-tool="${opener.tool}"]`)
            || (!rackElement("rack-empty-add").hidden ? rackElement("rack-empty-add") : null);
    }

    /** Keep every rack toolbar's pressed, expanded, and disabled states current. */
    updateToolStates() {
        const arranging = Boolean(this.arrangement);
        for (const button of this.root.querySelectorAll(".rack-tool")) {
            const tool = button.dataset.tool;
            button.disabled = arranging;
            if (tool === "arrange") {
                button.setAttribute("aria-pressed", String(arranging));
            } else {
                button.setAttribute("aria-expanded", String(Boolean(this.drawerOpen)));
            }
        }
        rackElement("rack-empty-add").disabled = arranging;
        const arrange = rackElement("rack-arrange-tool");
        arrange.disabled = arranging;
        arrange.setAttribute("aria-pressed", String(arranging));
        const edit = rackElement("rack-edit-tool");
        edit.disabled = arranging;
        edit.setAttribute("aria-expanded", String(Boolean(this.drawerOpen) && this.currentView === "rack-edit"));
    }

    /** Close the equipment drawer (Close, Escape, or an outside click). */
    closeDrawer() {
        this.drawer.close();
    }

    /** After the drawer closes: return focus, clear selection, apply held updates. */
    drawerClosed() {
        void this.rackEditor?.commit();
        void this.areaEditor?.commit();
        this.showDeviceInUrl(null);
        this.updateToolStates();
        this.toolOpenerElement()?.focus({ preventScroll: true });
        // Nothing is selected once the drawer is closed: undim all cables.
        if (this.selectedId) {
            this.selectedId = null;
            this.render();
        }
        if (this.updateHeld) setTimeout(() => this.applyUpdate(), 0);
    }

    /**
     * Slide the shared rack editor (rack-editor.js) into view for the
     * current rack. It saves itself when the drawer closes or moves on.
     *
     * @param {{refresh?: boolean}} [options] - ``refresh`` redraws only the
     *     shelf list of an open editor after a reload, keeping unsaved
     *     fields; with its rack gone the editor steps back.
     * @returns {void}
     */
    openRackEditor({ refresh = false } = {}) {
        if (refresh) {
            if (this.rackEditor.current()) this.rackEditor.refresh();
            else this.returnView();
            return;
        }
        const rack = this.currentRack();
        if (!rack || !this.rackEditor.open(rack.id)) return;
        this.openDrawer();
        // The editor is the drawer's first panel, as on the Floor plan: no
        // Back to Add equipment (owner).
        this.viewStack = [];
        this.activateView("rack-edit", "forward");
        this.updateToolStates();
        rackElement("rack-edit-name").focus();
    }

    /**
     * Build Rename (and Delete) icon tools for a shelf or other place.
     *
     * @param {HTMLElement} host - The header the rename field replaces.
     * @param {{id: number, name: string}} container - The place.
     * @param {{deletable?: boolean}} [options] - Offer Delete (shelves,
     *     surfaces, areas, shared rack shelves; not floors, walls, ceilings).
     * @returns {HTMLElement} The toolbar.
     */
    placeTools(host, container, { deletable = false } = {}) {
        const tools = rackText("div", "rack-tools rack-place-tools", "");
        tools.setAttribute("role", "group");
        tools.setAttribute("aria-label", `${container.name} tools`);
        tools.append(rackToolButton("rename", `Rename ${container.name}`, (event) => (
            this.startRename(host, event.currentTarget, {
                name: container.name,
                label: container.kind === "rack_shelf" ? "shared shelf" : container.kind,
                path: LABRAT_API.container(container.id),
            })
        )));
        if (deletable) {
            tools.append(rackToolButton("delete", `Delete ${container.name}: asks first, can unplace what is on it`,
                (event) => this.removePlace(`${LABRAT_API.container(container.id)}/delete`,
                    event.currentTarget, container.name)));
        }
        return tools;
    }

    /**
     * Offer back and full depth only for racks with a rear (shared).
     *
     * @param {string} selectId - The face select's ID.
     * @param {?object} rack - The chosen rack.
     */
    updateFaceOptions(selectId, rack) {
        this.placement.updateFaceOptions(rackElement(selectId), rack);
    }

    /** Suggest a free U position for newly entered manual equipment. */
    suggestManualPosition() {
        const rack = this.currentRack();
        const sharedShelf = rackElement("rack-manual-place").value === "rack_shelf";
        const face = rackElement("rack-manual-face").value;
        if (sharedShelf && rack) {
            const shelf = rack.rackShelves.find((item) => item.rack_face === face
                && this.nodes.some((node) => node.placement?.container_id === item.id));
            if (shelf) {
                rackElement("rack-manual-u").value = String(shelf.rack_u);
                return;
            }
        }
        const height = Number(rackElement("rack-manual-height").value) || 1;
        const unit = this.firstAvailableUnit(rack, face, sharedShelf ? 1 : height);
        rackElement("rack-manual-u").value = unit === null ? "" : String(unit);
    }

    /**
     * Delete a rack or place after the shared confirmation, which says what
     * goes with it and offers to unplace equipment inside (remove-dialog.js).
     *
     * @param {string} path - The delete endpoint.
     * @param {HTMLElement} opener - Where focus returns on cancel.
     * @param {string} name - What is being deleted, for the notice.
     * @returns {Promise<void>}
     */
    async removePlace(path, opener, name) {
        try {
            const result = await confirmRemoval(path, this.csrfToken, opener);
            if (!result) return;
            await this.load();
            if (this.currentView === "rack-edit") this.openRackEditor({ refresh: true });
            this.showNotice(result.unplaces
                ? `Deleted ${name} and unplaced ${result.unplaces} `
                    + `${result.unplaces === 1 ? "piece" : "pieces"} of equipment.`
                : `Deleted ${name}.`, true);
        } catch (error) {
            this.showNotice(error.message);
        }
    }

    /**
     * Swap a header's contents for an inline rename field (Save, Cancel;
     * Enter and Escape work too). Live updates wait while it is open.
     *
     * @param {HTMLElement} host - The header holding the name and tools.
     * @param {HTMLElement} opener - The Rename button, refocused on cancel.
     * @param {{name: string, label: string, path: string, extra?: object}} target -
     *     Current name, what to call it, endpoint, and other fields to resend.
     * @returns {void}
     */
    startRename(host, opener, { name, label, path, extra = {} }) {
        const form = document.createElement("form");
        form.className = "rack-rename";
        const input = document.createElement("input");
        input.type = "text";
        input.maxLength = 120;
        input.required = true;
        input.value = name;
        input.setAttribute("aria-label", `New name for ${label} ${name}`);
        const save = rackText("button", "button", "Save");
        save.type = "submit";
        const cancel = rackText("button", "button button-quiet", "Cancel");
        cancel.type = "button";
        form.append(input, save, cancel);
        const hidden = [...host.children].filter((child) => !child.hidden);
        const close = (refocus) => {
            this.renaming = false;
            form.remove();
            for (const child of hidden) child.hidden = false;
            if (refocus) opener.focus();
            if (this.updateHeld) setTimeout(() => this.applyUpdate(), 0);
        };
        cancel.addEventListener("click", () => close(true));
        form.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                close(true);
            }
        });
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            save.disabled = true;
            try {
                const result = await labratJson(
                    path, { payload: { ...extra, name: input.value.trim() }, csrfToken: this.csrfToken }
                );
                close(false);
                await this.load();
                if (this.currentView === "rack-edit") this.openRackEditor({ refresh: true });
                this.showNotice(`Renamed ${label} to ${result.name}.`, true);
            } catch (error) {
                save.disabled = false;
                this.showNotice(error.message);
                input.focus();
            }
        });
        for (const child of hidden) child.hidden = true;
        this.renaming = true;
        host.prepend(form);
        input.focus();
        input.select();
    }

    /**
     * Show an error or status message without inserting untrusted HTML.
     *
     * @param {string} message - The message; empty hides it.
     * @param {boolean} [success=false] - Style it as a success.
     */
    showNotice(message, success = false) {
        const notice = rackElement("rack-notice");
        notice.hidden = !message;
        notice.textContent = message;
        notice.dataset.state = success ? "success" : "error";
    }

    /**
     * Whether this page selects the room diagram instead of rack elevations.
     *
     * @returns {boolean} True on the Diagram view, or for a room without racks
     *     when no view is named.
     */
    get wiringView() {
        return this.root?.dataset.roomView === "wiring"
            || (!this.root?.dataset.roomView && this.racks?.length === 0);
    }

    /**
     * Refresh rack elevations and cables, optionally retaining editor input.
     *
     * @param {Object} options - Rendering options for window size changes.
     * @param {boolean} options.preserveForms - Keep unsaved drawer input intact.
     */
    render({ preserveForms = false } = {}) {
        // Held by reference: it may sit inside a rack header cleared below.
        this.cableKey ||= rackElement("rack-cable-key");
        const racksTarget = rackElement("rack-room-racks");
        racksTarget.replaceChildren();
        const treeMode = this.wiringView;
        // A room without racks shows only "Around the room", full width
        // ("surfaces"): no empty rack area, Add equipment in the header.
        const surfacesOnly = !treeMode && this.racks.length === 0;
        this.root.dataset.equipmentMode = treeMode ? "tree" : surfacesOnly ? "surfaces" : "racks";
        rackElement("room-wiring").hidden = !treeMode;
        const roomHasEquipment = this.nodes.some((node) => node.placement?.container_room_id === this.roomId);
        rackElement("rack-empty").hidden = treeMode || surfacesOnly ? roomHasEquipment : this.racks.length > 0;
        // Add equipment is always in the heading; Arrange only where racks are drawn.
        rackElement("rack-empty-add").hidden = false;
        rackElement("rack-arrange-tool").hidden = treeMode || this.racks.length === 0;
        rackElement("rack-edit-tool").hidden = treeMode || this.racks.length === 0;
        let mountedTotal = 0;
        for (const rack of treeMode ? [] : this.racks) {
            mountedTotal += this.renderRackBlock(rack, racksTarget);
        }
        const elsewhere = this.renderRoomOther();
        if (treeMode) {
            this.roomWiring ||= new RoomWiring();
            this.roomWiring.render(this);
            rackElement("rack-room-other-panel").hidden = true;
        }
        this.renderPatchBox(treeMode);
        if (!treeMode) this.attachSidePorts();
        this.renderRoomPower();
        // The WAN cluster shows only gateways placed in this room (wan.js).
        const wanPanel = document.getElementById("rack-wan");
        if (wanPanel) {
            wanPanel.dataset.nodes = JSON.stringify(this.nodes
                .filter((node) => node.placement?.container_room_id === this.roomId).map((node) => node.id));
            window.labratWan?.render();
        }
        // The cable key rides in the first rack's header, right of its name
        // (owner); with no rack drawn it goes back under the racks.
        // On Diagram it floats over the canvas, above the zoom controls.
        const firstHeader = racksTarget.querySelector(".rack-block-header");
        const diagramKey = document.getElementById("room-wiring-key-panel");
        if (treeMode && diagramKey) diagramKey.prepend(this.cableKey);
        else if (firstHeader) firstHeader.append(this.cableKey);
        else racksTarget.after(this.cableKey);
        const diagramKeyBox = document.getElementById("room-wiring-key");
        if (diagramKeyBox) diagramKeyBox.hidden = !treeMode || !this.roomWiring.data.nodes.length;
        this.cableKey.hidden = treeMode ? !this.roomWiring.data.nodes.length
            : surfacesOnly && !this.links.some((link) => [link.from, link.to].every((id) => this.nodes
                .find((node) => node.id === id)?.placement?.container_room_id === this.roomId));
        // The canvas fills the window once the page around it is final.
        if (treeMode) this.roomWiring.fitHeight();
        const rackCount = this.racks.length;
        this.renderSummary(rackCount, mountedTotal, elsewhere);
        if (!preserveForms) {
            this.renderInspector();
            this.equipmentList.render();
            this.updateManualFields();
        }
        this.applyProportions(racksTarget);
        this.renderArrangeBar();
        this.renderAnnunciator();
        this.renderTelemetry();
        this.markChanges();
        this.observeLayout();
        this.queueCables();
        this.fillDriveBays();
    }

    /** Watch the freshly rendered racks column for size changes (see bindEvents). */
    observeLayout() {
        if (!this.layoutObserver) return;
        this.layoutObserver.disconnect();
        for (const element of document.querySelectorAll(
            ".rack-main-column, .rack-main-panel, .rack-side-column, .rack-block, .rack-top-display",
        )) {
            this.layoutObserver.observe(element);
        }
    }

    /**
     * Size each rack to its real proportions: measure the bay width and set
     * the unit height so width : unit is the rail width : 1.75 inches.
     * Devices, rulers, and shelves are positioned in units, so this one
     * value lays them all out. Devices too short to read get compact styles.
     *
     * @param {HTMLElement} target - The racks container.
     * @returns {void}
     */
    applyProportions(target) {
        for (const elevations of target.querySelectorAll(".rack-elevations")) {
            const bay = elevations.querySelector(".rack-slots");
            if (!bay) continue;
            const inches = Number(elevations.dataset.widthInches) || 19;
            // A two-sided rack's face column is laid out for a 19-inch rack; a
            // narrower rack keeps the same inches-to-pixels scale and gets a
            // narrower face. A front-only rack fills its panel (owner): its
            // own rail width spans the whole column.
            const single = elevations.classList.contains("rack-elevations-single");
            const frames = elevations.querySelectorAll(".rack-frame");
            for (const frame of frames) frame.style.width = "";
            const pixelsPerInch = bay.clientWidth / (single ? inches : 19);
            if (!single && inches !== 19) {
                const chrome = frames[0].offsetWidth - bay.clientWidth;
                for (const frame of frames) frame.style.width = `${(pixelsPerInch * inches + chrome).toFixed(1)}px`;
            }
            const unit = pixelsPerInch * RACK_UNIT_INCHES;
            elevations.style.setProperty("--rack-u", `${unit.toFixed(2)}px`);
            for (const item of elevations.querySelectorAll("[data-units]")) {
                const drawn = unit * Number(item.dataset.units) - RACK_DEVICE_GAP;
                item.classList.toggle("is-compact", drawn < RACK_UNIT_COMPACT_PIXELS);
                item.classList.toggle("is-short", drawn >= RACK_UNIT_COMPACT_PIXELS && drawn < RACK_UNIT_SHORT_PIXELS);
                item.classList.toggle("is-tiny", drawn < RACK_UNIT_TINY_PIXELS);
            }
        }
    }

    /**
     * Draw each UPS's line to the NUT server it reports through
     * (`ups.server_node_id`), as Discovery does. A USB attachment
     * (`attachment === "usb"`) is drawn in the UPS color with a dash-dot
     * pattern; a relayed (`dummy-ups`) unit, which only reports through the
     * server and states no connection, gets Discovery's fainter, sparse
     * dotted "reports through" line, offset so it never lies on the
     * server's USB line. Neither is a network cable or ever counts as one.
     *
     * @param {SVGSVGElement} svg - The surface layer being built.
     * @param {object} tools - Shared geometry from `drawSurfaceUplinks`.
     * @returns {void}
     */
    drawUsbLinks(svg, {
        root, nodes, box, center, edgeToward, plankBelow, nextLane, routeBetween, endOptions, visibleInRack,
        rackEntry, trayY, channelX }) {
        const cardOf = (id) => root.querySelector(
            `.rack-frame .rack-device[data-node="${CSS.escape(id)}"], `
            + `.rack-frame .rack-shared-device[data-node="${CSS.escape(id)}"], `
            + `.rack-top-item[data-node="${CSS.escape(id)}"], .rack-room-item[data-node="${CSS.escape(id)}"]`,
        );
        const kinds = { usb: "usb", relayed: "relay" };
        for (const ups of nodes.values()) {
            const reading = (ups.observations || []).map((item) => item.ups)
                .find((item) => kinds[item?.attachment] && item.server_node_id);
            const relay = kinds[reading?.attachment] === "relay";
            // The relayed line leaves its server 6 px aside and runs 3 px
            // lower than a USB line from that server would.
            const shift = relay ? 6 : 0;
            const server = reading ? nodes.get(reading.server_node_id) : null;
            if (!server || server.id === ups.id
                    || ups.placement?.container_room_id !== this.roomId
                    || server.placement?.container_room_id !== this.roomId) continue;
            // A full-depth device has a card on both faces; take the one that
            // holds this link's lamp (attachSidePorts puts it on the face its
            // ports are on), so the line never meets the other face's card.
            const cardWithLamp = (id) => [...root.querySelectorAll(`.rack-ups-port[data-ups="${CSS.escape(ups.id)}"]`)]
                .map((lamp) => lamp.closest("[data-node]")).find((card) => card?.dataset.node === id);
            const from = cardWithLamp(server.id) || cardOf(server.id);
            const to = cardWithLamp(ups.id) || cardOf(ups.id);
            // A card scrolled out of view (a narrow panel's rack-top row) has no line.
            if (!from || !to || !visibleInRack(from) || !visibleInRack(to)) continue;
            const a = center(from);
            const b = center(to);
            const rackFrame = [from, to].map((card) => card.closest?.(".rack-frame")).find(Boolean);
            // Lamp to lamp, like a network cable (attachSidePorts).
            const lampOf = (card) => card.querySelector(`.rack-ups-port[data-ups="${CSS.escape(ups.id)}"]`);
            const fromLamp = lampOf(from);
            const toLamp = lampOf(to);
            let d = fromLamp && toLamp ? this.lampRoute(fromLamp, toLamp,
                { box, center, nextLane, routeBetween, endOptions, rackEntry, trayY, channelX }, shift) : null;
            if (d) {
                // Routed lamp to lamp above.
            } else if (rackFrame) {
                // A rack device takes part: run down the channel right of its
                // rack and meet each card from that side (a rack-top card from
                // under its plank).
                const laneX = channelX ? channelX(rackFrame)
                    : rackFrame.getBoundingClientRect().right - box.left + 12 + nextLane() * 4;
                const leave = (card, point) => {
                    if (card.classList.contains("rack-top-item")) {
                        const plank = plankBelow(card);
                        const y = plank
                            && trayY
                            ? trayY(plank)
                            : (plank ? center(plank).bottom : point.bottom) + 8 + shift / 2;
                        return `M ${point.x - shift} ${point.bottom} V ${y} H ${laneX}`;
                    }
                    if (card.closest?.(".rack-frame")) {
                        return `M ${point.x + point.halfWidth} ${point.y + shift} H ${laneX}`;
                    }
                    const edge = edgeToward(point, { x: laneX, y: point.y });
                    return `M ${edge.x} ${edge.y} H ${laneX}`;
                };
                const arrive = (card, point) => {
                    if (card.closest?.(".rack-frame")) return ` V ${point.y} H ${point.x + point.halfWidth}`;
                    if (card.classList.contains("rack-top-item")) {
                        const plank = plankBelow(card);
                        const y = plank && trayY ? trayY(plank) : (plank ? center(plank).bottom : point.bottom) + 8;
                        return ` V ${y} H ${point.x} V ${point.bottom}`;
                    }
                    const edge = edgeToward(point, { x: laneX, y: point.y });
                    return ` V ${edge.y} H ${edge.x}`;
                };
                d = leave(from, a) + arrive(to, b);
            } else if (Math.abs(a.x - b.x) < a.halfWidth + b.halfWidth) {
                // Cards stacked in one column (a server and its UPS on the
                // floor): a bracket down their left edges, which stays
                // visible however small the gap between them is.
                const side = Math.min(a.x - a.halfWidth, b.x - b.halfWidth) - 7 - shift;
                d = `M ${a.x - a.halfWidth} ${a.y + shift} H ${side} V ${b.y} H ${b.x - b.halfWidth}`;
            } else {
                // Cards apart (a rack-top server and a UPS around the room):
                // routed like room-card cables, never straight across other
                // cards. A rack-top card leaves from its middle under its
                // plank, out to the rack's channel; a room card from its
                // bottom or right side.
                const options = (card) => endOptions(card, card.classList.contains("rack-top-item") ? card : null);
                const first = options(from).map((option) => ({ ...option, x: option.x - shift,
                    lead: option.lead?.map((point) => ({ ...point, x: point.x - shift, y: point.y + shift / 2 })),
                    exitY: option.exitY + shift / 2 }));
                d = routeBetween?.(first, options(to));
                if (!d) {
                    const start = edgeToward(a, b);
                    const end = edgeToward(b, a);
                    d = `M ${start.x} ${start.y} L ${end.x} ${end.y}`;
                }
            }
            const path = document.createElementNS(svg.namespaceURI, "path");
            rackSetCablePath(path, d);
            path.classList.add(relay ? "is-relay" : "is-usb");
            path.dataset.pinA = `${server.id}::ups:${ups.id}`;
            path.dataset.pinB = `${ups.id}::ups:${ups.id}`;
            path.classList.toggle("is-dim", Boolean(this.selectedId
                && this.selectedId !== server.id && this.selectedId !== ups.id));
            svg.append(path);
        }
    }

    /**
     * Draw one rack: a header with its size and Edit button, front and rear
     * elevations (or front only), and anything sitting on top of it.
     *
     * @param {object} rack - Rack record from load().
     * @param {HTMLElement} target - Container for rack blocks.
     * @returns {number} Number of devices mounted in the rack.
     */
    renderRackBlock(rack, target) {
        const rackShelfIds = new Set(rack.rackShelves.map((shelf) => shelf.id));
        const placed = this.nodes.filter((node) => (
            node.placement?.container_id === rack.containerId
            || rackShelfIds.has(node.placement?.container_id)
        ));
        const topPlaced = this.nodes.filter((node) => node.placement?.container_id === rack.topContainerId);

        const block = rackText("article", "rack-block", "");
        block.id = `rack-${rack.id}`;
        block.addEventListener("scroll", () => this.queueCables(), { passive: true });
        block.classList.toggle("is-focused", rack.id === this.rackId);
        const header = rackText("header", "rack-block-header", "");
        const title = rackText("div", "rack-block-title", "");
        // Just the name: size and counts were extraneous (owner).
        title.append(rackText("h3", "", rack.name));
        header.append(title);
        block.append(header);
        // The rack bay: a sunk, gridded box holding the rack top and faces,
        // with a wide left gutter for the patch panel's cable tray (racks.css).
        const bay = rackText("div", "rack-bay", "");
        block.append(bay);

        // The room's patch panel rests on the first rack's top, so that rack
        // gets a plank even with nothing else on it (renderPatchBox).
        const carriesPatch = rack === this.racks[0] && this.livePatchExits().length > 0;
        // The bay's left padding is the cable tray. Patch cables to devices on
        // this rack's front face drop down it (leftSideRuns: 6 px off the
        // face, 3 px apart), so size it to those lanes plus 6 px of clearance,
        // and keep it as small as the bay's other sides (12 px) when none run.
        // Set before layout, since applyProportions measures the faces.
        const rackContainers = new Set([rack.containerId, ...rack.rackShelves.map((shelf) => shelf.id)]);
        const frontLanes = carriesPatch ? this.livePatchExits().filter((exit) => (
            rackContainers.has(exit.local?.placement?.container_id)
            && ["front", "full_depth"].includes(exit.local.placement.face || "front")
        )).length : 0;
        bay.style.setProperty("--rack-tray-width", `${Math.max(12, frontLanes ? 6 + 3 * (frontLanes - 1) + 6 : 0)}px`);
        if (topPlaced.length || carriesPatch) {
            // Equipment rests on a steel plank: the rack's own top surface.
            const top = rackText("div", "rack-top-display", "");
            top.setAttribute("aria-label", `Equipment on top of ${rack.name}`);
            const items = rackText("div", "rack-top-items", "");
            items.dataset.container = String(rack.topContainerId);
            this.renderTop(topPlaced, items);
            const plank = rackText("div", "rack-top-plank", "");
            plank.append(rackText("span", "rack-top-plank-label", "On top"));
            top.append(items, plank, ...this.rackTopScrollers(items));
            bay.append(top);
        }

        bay.append(rackText("p", "rack-scroll-hint", "Scroll sideways to explore the rack faces and ports."));
        const elevations = rackText("div", "rack-elevations", "");
        elevations.classList.toggle("rack-elevations-single", !rack.has_rear);
        elevations.dataset.widthInches = String(rack.width_inches || 19);
        const visibleContainers = new Set(this.racks.flatMap((item) => [
            item.containerId, ...item.rackShelves.map((shelf) => shelf.id),
        ]));
        const mountedIds = new Set(this.nodes.filter((node) => visibleContainers.has(node.placement?.container_id)).map((node) => node.id));
        // Size the cable channels to the cables actually drawn (down ones are not).
        const downIds = this.downNodeIds();
        const cableCount = this.links.filter((link) => mountedIds.has(link.from) && mountedIds.has(link.to)
            && !this.cableIsDown(link, downIds)).length;
        // Only cables between different racks cross over the top.
        const rackOf = new Map(this.nodes.map((node) => [node.id, this.racks.find((item) => (
            node.placement?.container_id === item.containerId
            || item.rackShelves.some((shelf) => shelf.id === node.placement?.container_id)
        ))?.id]));
        const overheadCount = this.links.filter((link) => (
            mountedIds.has(link.from) && mountedIds.has(link.to)
            && rackOf.get(link.from) !== rackOf.get(link.to)
            && [rackOf.get(link.from), rackOf.get(link.to)].includes(rack.id)
        )).length;
        elevations.style.setProperty("--rack-cable-gutter", `${Math.max(64, 24 + cableCount * 3)}px`);
        elevations.style.setProperty("--rack-cable-headroom", `${10 + overheadCount * 3}px`);
        for (const face of rack.has_rear ? ["front", "rear"] : ["front"]) {
            elevations.append(this.renderFace(rack, face, placed));
        }
        bay.append(elevations);
        target.append(block);
        return placed.length;
    }

    /**
     * Draw one face of a rack using canonical U positions, U1 at the bottom.
     *
     * @param {object} rack - Rack record.
     * @param {"front"|"rear"} face - Face to draw.
     * @param {Array<object>} placed - Devices mounted in this rack.
     * @returns {HTMLElement} The rack frame element.
     */
    renderFace(rack, face, placed) {
        const frame = rackText("div", "rack-frame", "");
        frame.dataset.face = face;
        const topBar = rackText("div", "rack-frame-top", "");
        topBar.setAttribute("aria-hidden", "true");
        topBar.append(rackText("span", "", rack.name), rackText("span", "", face === "front" ? "Front" : "Rear"));
        const body = rackText("div", "rack-frame-body", "");
        const ruler = rackText("div", "rack-ruler", "");
        const slots = rackText("div", "rack-slots", "");
        const right = rackText("div", "rack-ruler rack-ruler-right", "");
        ruler.setAttribute("aria-hidden", "true");
        right.setAttribute("aria-hidden", "true");
        slots.setAttribute("aria-label", `${rack.name} ${face} rack units and equipment`);
        slots.dataset.container = String(rack.containerId);
        slots.dataset.heightU = String(rack.height_u);
        body.append(ruler, slots, right);
        const bottom = rackText("div", "rack-frame-bottom", "");
        bottom.setAttribute("aria-hidden", "true");
        frame.append(topBar, body, bottom);

        // Positions are in rack units (U1 at the bottom); applyProportions
        // sets the unit height from the rack's real width.
        const units = (count, adjust = 0) => `calc(var(--rack-u) * ${count}${adjust ? ` + ${adjust}px` : ""})`;
        for (const item of [ruler, right, slots]) {
            item.style.height = units(rack.height_u);
        }
        for (let number = rack.height_u; number >= 1; number -= 1) {
            for (const target of [ruler, right]) {
                const mark = rackText("span", "rack-unit-number", number);
                mark.style.top = units(rack.height_u - number);
                target.append(mark);
            }
        }
        const sharedGroups = new Map();
        for (const node of placed) {
            if (node.placement.container_kind === "rack_shelf") {
                if ((node.placement.face || "front") !== face && node.placement.face !== "full_depth") continue;
                if (!sharedGroups.has(node.placement.container_id)) sharedGroups.set(node.placement.container_id, []);
                sharedGroups.get(node.placement.container_id).push(node);
                continue;
            }
            if ((node.placement.face || "front") !== face && node.placement.face !== "full_depth") continue;
            if (!node.placement.rack_u) continue;
            const height = Math.max(1, Number(node.height_u) || 1);
            const top = rack.height_u - node.placement.rack_u - height + 1;
            const card = document.createElement("button");
            card.type = "button";
            card.className = "rack-device";
            card.dataset.node = node.id;
            card.dataset.category = node.category;
            card.dataset.units = String(height);
            card.setAttribute("aria-pressed", String(node.id === this.selectedId));
            card.setAttribute("aria-label", `${node.name}, ${face}, U${node.placement.rack_u}, ${height} units`);
            card.title = `${node.name} · ${rackCategory(node.category)} · U${node.placement.rack_u} · ${height}U`;
            card.style.top = units(Math.max(0, top), RACK_DEVICE_GAP / 2);
            card.style.height = units(height, -RACK_DEVICE_GAP);
            const label = rackText("span", "rack-device-label", "");
            const name = rackText("span", "rack-device-name", "");
            name.append(this.statusLamp(node), document.createTextNode(node.name));
            label.append(name);
            label.append(rackText(
                "span", "rack-device-meta", `${rackCategory(node.category)} · U${node.placement.rack_u}`
            ));
            card.append(label);
            // A server's front shows its drives as hot-swap bays (owner),
            // filled once the cluster data arrives (fillDriveBays).
            if (node.category === "server" && face === "front") {
                const bays = rackText("span", "rack-drive-bays", "");
                bays.dataset.node = node.id;
                card.append(bays);
            }
            this.appendUpsBadge(card, node, `${node.name}, ${face}, U${node.placement.rack_u}, ${height} units`);
            this.appendWirelessBadge(card, node);
            const inferredPortFacing = this.portFacing(node);
            if (node.ports.length && inferredPortFacing === face) this.appendPortLights(card, node, face);
            card.addEventListener("click", () => this.select(node.id));
            if (this.arrangement?.devices.has(node.device_id)) this.bindArrange(card, node, face);
            slots.append(card);
        }
        for (const [containerId, devices] of sharedGroups) {
            const shelf = rack.rackShelves.find((item) => item.id === containerId);
            if (shelf) this.renderSharedShelf(rack, shelf, devices, face, slots);
        }
        return frame;
    }

    /**
     * Append the shared physical-port lamps for a device's visible face.
     *
     * @param {HTMLElement} card - Device faceplate receiving the lamps.
     * @param {object} node - Equipment node and discovered physical ports.
     * @param {string|null} face - Visible rack face, when applicable.
     * @param {object} [options] - Surface visibility and preview limit.
     * @param {boolean} [options.onTop] - Show ports independently of rack face.
     * @param {number} [options.limit] - Lamp limit; room diagrams show every port.
     * @returns {void}
     */
    /**
     * Which rack face a device's ports are on, so its lamps show on that face
     * only: its recorded `port_facing`, else the face it is mounted on; a
     * full-depth server's are on the rear and any other full-depth device's
     * are unknown (no lamps), unless `usb` asks for a UPS link's port, which
     * on a full-depth UPS is on the rear too (the USB socket's usual place).
     *
     * @param {object} node - Device node.
     * @param {{usb?: boolean}} [options] - Whether this is for a UPS link lamp.
     * @returns {string|null} "front", "rear", or null when unknown.
     */
    portFacing(node, { usb = false } = {}) {
        if (node.port_facing) return node.port_facing;
        if (node.placement?.face === "full_depth") {
            return node.category === "server" || (usb && node.category === "ups") ? "rear" : null;
        }
        return node.placement?.face || "front";
    }

    /**
     * Add a device's network port lamps to its card.
     *
     * Lamps show on the face the ports are on (portFacing); a rack-top or
     * surface card always shows them.
     *
     * @param {HTMLElement} card - The device's card.
     * @param {object} node - The device record.
     * @param {string} face - The rack face being drawn.
     * @param {object} [options] - Drawing options.
     * @param {boolean} [options.onTop=false] - The card sits off the rack faces.
     * @param {number} [options.limit=RACK_PORT_LIGHT_LIMIT] - Most lamps drawn
     *     in the limited Port lights variants; Infinity draws every port.
     */
    appendPortLights(card, node, face, { onTop = false, limit = RACK_PORT_LIGHT_LIMIT } = {}) {
        const inferredPortFacing = this.portFacing(node);
        // Equipment on top of a rack has no face: its lights always show.
        if (!node.ports?.length || (!onTop && inferredPortFacing !== face)) return;
        const lights = rackText("span", "rack-port-lights", "");
        lights.setAttribute("aria-label", `${node.ports.length} network ports`);
        const bonds = this.details.networkInterfacesFor(node).filter((item) => item.kind === "bond");
        const down = this.downNodeIds();
        // A cable is drawn only from a drawn lamp, so cabled ports always show.
        const cabled = new Set(node.ports.flatMap((port, index) => (this.cableAt(node.id, port.name) ? [index] : [])));
        const slots = rackPortLightSlots(node.ports.length, limit, this.portLightMode, cabled);
        lights.classList.toggle("is-dense", slots.length > RACK_PORT_LIGHT_LIMIT);
        for (const slot of slots) {
            if (typeof slot === "object") {
                const more = rackText("span", "rack-port-more", "");
                const label = `${slot.more} more port${slot.more === 1 ? "" : "s"}: open the device for all of them`;
                more.title = label;
                more.setAttribute("aria-label", label);
                lights.append(more);
                continue;
            }
            const index = slot;
            const port = node.ports[index];
            const linked = this.portIsUp(port, node.id);
            const cable = this.cableAt(node.id, port.name);
            // A down cable (its port or far device reported down; cableIsDown)
            // is not drawn, so its port gets no cable ring either (owner): a
            // switch remembers where a device was last plugged in.
            const cableDown = Boolean(cable && this.cableIsDown(cable.link, down));
            const bond = this.details.bondForPort(port, bonds);
            const indicator = rackText("span", "rack-port-light", "");
            indicator.dataset.linked = String(linked);
            indicator.dataset.state = this.portAlarmState(port, node.id);
            if (indicator.dataset.state === "up") {
                // Each lamp flickers on its own phase so a row never blinks in step.
                const timing = wireSpeedStyle(Number(port.speed_mbps) || 0);
                indicator.style.setProperty("--flicker-period", `${timing.flicker}s`);
                indicator.style.setProperty("--flicker-delay", `${(-Math.random() * timing.flicker).toFixed(2)}s`);
            }
            // A lit port shows its cable medium's color; ports with a drawn cable get a ring.
            indicator.dataset.medium = port.medium || "unknown";
            indicator.dataset.pin = `${node.id}::${port.name}`;
            indicator.classList.toggle("is-cabled", Boolean(cable) && !cableDown);
            indicator.classList.toggle("is-bonded", Boolean(bond));
            const far = cable ? ` · ${cableDown ? "last seen to" : "to"} ${cable.otherName} ${cable.otherPort}` : "";
            indicator.title = `${port.name || `Port ${index + 1}`}: ${this.portStatus(port, node.id)}`
                + `${bond ? ` · member of ${bond.name}${bond.mode ? ` (${bond.mode})` : ""}` : ""}`
                + `${port.medium ? ` · ${rackMediumLabel(port.medium)}` : ""}${far}`;
            lights.append(indicator);
        }
        card.append(lights);
    }

    /**
     * Draw several devices as side-by-side items inside one rack U.
     *
     * @param {object} rack - The rack.
     * @param {object} shelf - The shared shelf container.
     * @param {object[]} devices - Devices on the shelf, in their saved order.
     * @param {string} face - The face being drawn.
     * @param {HTMLElement} slots - The face's device layer; the shelf's
     *     group is added to it.
     */
    renderSharedShelf(rack, shelf, devices, face, slots) {
        const group = rackText("div", "rack-shared-shelf", "");
        group.dataset.units = "1";
        group.style.top = `calc(var(--rack-u) * ${rack.height_u - shelf.rack_u} + ${RACK_DEVICE_GAP / 2}px)`;
        group.style.height = `calc(var(--rack-u) - ${RACK_DEVICE_GAP}px)`;
        group.setAttribute("aria-label", `${shelf.name}, U${shelf.rack_u}, ${devices.length} devices`);
        const ordered = [...devices].sort((left, right) => (
            (left.placement.position?.x ?? 0.5) - (right.placement.position?.x ?? 0.5)
        ));
        for (const device of ordered) {
            const item = document.createElement("button");
            item.type = "button";
            item.className = "rack-shared-device";
            // Same device-type stripe as a full rack faceplate.
            item.dataset.category = device.category;
            item.draggable = Boolean(device.device_id);
            item.setAttribute("aria-pressed", String(device.id === this.selectedId));
            item.setAttribute("aria-label", `${device.name}, shared shelf U${shelf.rack_u}, ${face}`);
            const itemName = rackText("span", "rack-shared-device-name", "");
            itemName.append(this.statusLamp(device), document.createTextNode(device.name));
            item.append(itemName);
            this.appendUpsBadge(item, device, `${device.name}, shared shelf U${shelf.rack_u}, ${face}`);
            this.appendWirelessBadge(item, device);
            this.appendPortLights(item, device, face);
            item.dataset.node = device.id;
            item.addEventListener("click", () => this.select(device.id));
            item.addEventListener("dragstart", (event) => {
                this.draggedShelfDeviceId = device.device_id;
                event.dataTransfer.setData("text/plain", String(device.device_id));
                event.dataTransfer.effectAllowed = "move";
            });
            item.addEventListener("dragover", (event) => {
                event.preventDefault();
                event.dataTransfer.dropEffect = "move";
            });
            item.addEventListener("drop", (event) => this.reorderRackShelf(event, rack, shelf, device));
            group.append(item);
        }
        slots.append(group);
    }

    /**
     * Move a dragged shared-shelf device before or after its drop target.
     *
     * @param {DragEvent} event - The drop.
     * @param {object} rack - The rack.
     * @param {object} shelf - The shared shelf.
     * @param {object} targetDevice - The device it was dropped on.
     * @returns {Promise<void>} Resolves once the order is saved.
     */
    async reorderRackShelf(event, rack, shelf, targetDevice) {
        event.preventDefault();
        event.stopPropagation();
        const deviceId = Number(event.dataTransfer.getData("text/plain")) || this.draggedShelfDeviceId;
        if (!deviceId || deviceId === targetDevice.device_id || !targetDevice.device_id) return;
        const bounds = event.currentTarget.getBoundingClientRect();
        const targetX = targetDevice.placement.position?.x ?? 0.5;
        const offset = event.clientX < bounds.left + bounds.width / 2 ? -0.0001 : 0.0001;
        try {
            await labratJson(`${LABRAT_API.rackDevice(deviceId)}/placement`, { payload: {
                container_id: rack.containerId, placement_mode: "rack_shelf",
                rack_u: shelf.rack_u, face: shelf.rack_face,
                position: { x: Math.max(0, Math.min(1, targetX + offset)) },
            }, csrfToken: this.csrfToken });
            this.draggedShelfDeviceId = null;
            await this.load();
        } catch (error) {
            this.showNotice(error.message);
        }
    }

    /**
     * Return whether a port appears in a trusted discovery link. Shared with
     * the details panel (DeviceDetails).
     *
     * @param {string} nodeId - The device.
     * @param {string} portName - The port.
     * @returns {boolean} True when a link uses it.
     */
    isPortLinked(nodeId, portName) {
        return this.details.isPortLinked(nodeId, portName);
    }

    /**
     * Return active state only when the port is not explicitly reported down.
     * Shared with the details panel (DeviceDetails).
     *
     * @param {object} port - The port record.
     * @param {string} nodeId - Its device.
     * @returns {boolean} True when it counts as up.
     */
    portIsUp(port, nodeId) {
        return this.details.portIsUp(port, nodeId);
    }

    /**
     * Describe a port using negotiated speed and trusted discovery evidence.
     * Shared with the details panel (DeviceDetails).
     *
     * @param {object} port - The port record.
     * @param {string} nodeId - Its device.
     * @returns {string} "up", "down", the link's confidence and evidence
     *     (such as "inferred connection · lldp"), or "unknown".
     */
    portStatus(port, nodeId) {
        return this.details.portStatus(port, nodeId);
    }

    /**
     * Connector glyph, shared with the details panel (DeviceDetails).
     *
     * @param {?string} connectorType - The port's connector, such as "sfp_plus".
     * @returns {HTMLElement} The glyph, labelled with the connector's name.
     */
    portConnectorIcon(connectorType) {
        return this.details.portConnectorIcon(connectorType);
    }

    /** Show devices resting on the selected rack without assigning rack units. */
    /**
     * Scroll arrows for a rack-top row. In a narrow rack panel the row keeps
     * its one line on the plank but scrolls sideways (racks.css); these ‹ ›
     * buttons show only when there is more to see in that direction, scroll
     * by most of the row's width, and redraw the cables as it moves.
     *
     * @param {HTMLElement} items - The `.rack-top-items` row.
     * @returns {HTMLButtonElement[]} The previous and next buttons.
     */
    rackTopScrollers(items) {
        const make = (direction, label, glyph) => {
            const button = rackText("button", `rack-top-scroll rack-top-scroll-${direction}`, glyph);
            button.type = "button";
            button.hidden = true;
            button.setAttribute("aria-label", label);
            button.title = label;
            button.addEventListener("click", () => items.scrollBy({
                left: (direction === "prev" ? -1 : 1) * Math.max(80, items.clientWidth * 0.8),
                behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
            }));
            return button;
        };
        const prev = make("prev", "Scroll rack top left", "‹");
        const next = make("next", "Scroll rack top right", "›");
        const update = () => {
            prev.hidden = items.scrollLeft <= 1;
            next.hidden = items.scrollLeft + items.clientWidth >= items.scrollWidth - 1;
        };
        items.addEventListener("scroll", () => {
            update();
            this.queueCables();
        }, { passive: true });
        if ("ResizeObserver" in window) new ResizeObserver(update).observe(items);
        requestAnimationFrame(update);
        return [prev, next];
    }

    /**
     * A rack-top card's head row (owner): a device-type icon (none for
     * "other physical") and the name, cut off at the card's edge with no
     * ellipsis, so every character that fits shows. The card's tooltip
     * (and its text, for screen readers) keeps the full name.
     *
     * @param {object} device - Device node.
     * @returns {HTMLElement} The head row.
     */
    rackTopHead(device) {
        const head = rackText("span", "rack-top-head", "");
        if (device.category !== "other_physical" && typeof labratDeviceIcon === "function") {
            head.append(labratDeviceIcon(device.category, "rack-top-icon"));
        }
        head.append(rackText("span", "rack-top-name", device.name));
        return head;
    }

    /**
     * Draw the rack-top equipment as buttons along the rack's top plank.
     *
     * @param {object[]} devices - Devices on the rack top.
     * @param {HTMLElement} target - The plank's item row, replaced.
     */
    renderTop(devices, target) {
        target.replaceChildren();
        for (const device of rackTopOrder(devices)) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "rack-top-item";
            button.dataset.node = device.id;
            // Same device-type stripe as a rack faceplate.
            button.dataset.category = device.category;
            button.setAttribute("aria-pressed", String(device.id === this.selectedId));
            button.title = `${device.name} · ${rackCategory(device.category)}`;
            button.append(this.rackTopHead(device));
            this.appendUpsBadge(button, device, `${device.name}, on top of rack`);
            this.appendWirelessBadge(button, device);
            // The same port lamps as rack faceplates; surface cables start at them.
            this.appendPortLights(button, device, null, { onTop: true });
            button.addEventListener("click", () => this.select(device.id));
            if (this.arrangement?.topOf.has(device.device_id)) this.bindArrangeTop(button, device);
            target.append(button);
        }
    }

    /**
     * Draw everything in the room that is not in a rack, as grouped panels:
     * each shelf as a board with its items left to right, then the floor and
     * other surfaces, the walls by side, and the ceiling. Each panel for one
     * place has Rename (and, for shelves and surfaces, Delete) tools. An
     * empty floor, wall, or ceiling is left out.
     *
     * @returns {number} Number of devices drawn.
     */
    renderRoomOther() {
        const target = rackElement("rack-room-other");
        target.replaceChildren();
        const inRoom = this.nodes.filter((node) => node.placement?.container_room_id === this.roomId);
        const byContainer = new Map();
        for (const node of inRoom) {
            const id = node.placement.container_id;
            if (!byContainer.has(id)) byContainer.set(id, []);
            byContainer.get(id).push(node);
        }
        const itemsIn = (container) => [...(byContainer.get(container.id) || [])].sort((left, right) => (
            (left.placement.position?.x ?? left.placement.position?.along ?? 0.5)
            - (right.placement.position?.x ?? right.placement.position?.along ?? 0.5)
        ));
        let count = 0;
        const groups = [];
        for (const shelf of this.roomContainers.filter((item) => item.kind === "shelf")) {
            groups.push({ kind: "shelf", title: shelf.name, items: itemsIn(shelf),
                container: shelf, deletable: true });
        }
        for (const kind of ["floor", "surface", "area"]) {
            // Surfaces and areas may rest on the floor or another place; the
            // room's floor itself is top-level.
            for (const container of this.roomContainers.filter((item) => (
                item.kind === kind && (kind !== "floor" || !item.parent_id)
            ))) {
                groups.push({ kind, title: container.name,
                    items: itemsIn(container), container, deletable: kind !== "floor" });
            }
        }
        const walls = this.roomContainers.filter((item) => item.kind === "wall");
        const order = ["north", "east", "south", "west"];
        walls.sort((left, right) => order.indexOf(left.wall_side) - order.indexOf(right.wall_side));
        const wallItems = walls.flatMap((wall) => itemsIn(wall).map((node) => ({ node, side: wall.wall_side })));
        groups.push({ kind: "walls", title: "Walls", items: wallItems.map((entry) => entry.node), sides: wallItems });
        for (const ceiling of this.roomContainers.filter((item) => item.kind === "ceiling")) {
            groups.push({ kind: "ceiling", title: ceiling.name, items: itemsIn(ceiling),
                container: ceiling });
        }

        // Built-in surfaces (floor, walls, ceiling) show only with something
        // on them; shelves and surfaces a person made always show, so an
        // empty one can be renamed or deleted. With none at all, the whole
        // "Around the room" section hides and racks get the width.
        const shown = groups.filter((group) => group.items.length > 0 || group.deletable);
        rackElement("rack-room-other").closest(".rack-room-other-panel").hidden = shown.length === 0;
        for (const group of shown) {
            const panel = rackText("section", `rack-room-group rack-room-group-${group.kind}`, "");
            const header = rackText("header", "rack-room-group-header", "");
            // Just the name: no "standing · 1" style note (owner).
            header.append(rackText("h3", "", group.title));
            if (group.container) {
                header.append(this.placeTools(header, group.container, { deletable: group.deletable }));
            }
            panel.append(header);
            const list = rackText("div", "rack-room-items", "");
            for (const node of group.items) {
                const card = document.createElement("button");
                card.type = "button";
                card.className = "rack-room-item";
                card.dataset.node = node.id;
                card.dataset.category = rackCategory(node.category).replaceAll(" ", "_");
                card.setAttribute("aria-pressed", String(node.id === this.selectedId));
                const itemName = rackText("span", "rack-room-item-name", "");
                itemName.append(this.statusLamp(node), document.createTextNode(node.name));
                card.append(itemName);
                const side = group.sides?.find((entry) => entry.node === node)?.side;
                card.append(rackText("span", "rack-room-item-meta",
                    side ? `${side} wall · ${rackCategory(node.category)}` : rackCategory(node.category)));
                this.appendUpsBadge(card, node, `${node.name}, ${group.title}, ${rackCategory(node.category)}`);
                this.appendWirelessBadge(card, node);
                // The same port lamps as a shelf faceplate; cables start at them.
                this.appendPortLights(card, node, null, { onTop: true });
                card.addEventListener("click", () => this.select(node.id));
                list.append(card);
            }
            if (!group.items.length) list.append(rackText("p", "rack-room-group-empty", "Nothing here yet."));
            panel.append(list);
            target.append(panel);
            count += group.items.length;
        }
        return count;
    }

    /**
     * Draw the room's patch panel: the Diagram's boundary (RoomPatchPanel,
     * room-wiring.js) with one socket per cable leaving this room, the same
     * destination popups, and its cables drawn to their local ports by
     * drawSurfaceUplinks. In a room with racks it rests on the first rack's
     * top, leftmost; without racks it is the first side panel. The Diagram
     * draws its own as a card on its canvas, so neither shows there.
     *
     * @param {boolean} treeMode - Whether the Diagram is showing.
     * @returns {void}
     */
    renderPatchBox(treeMode) {
        const box = document.getElementById("rack-patch");
        const target = document.getElementById("rack-patch-face");
        if (!box || !target || typeof RoomPatchPanel !== "function") return;
        // Destination popups open above the panel, so they never cover the
        // rack and the highlighted cable below it (owner).
        this.patchBox ||= new RoomPatchPanel({ app: this, idPrefix: "rack-patch-details", placement: "above",
            after: () => rackFocusableAfter(box) });
        const exits = treeMode ? [] : this.livePatchExits();
        // Sized to its cables, with no empty positions (roomPatchShape).
        const face = this.patchBox.render(exits, roomPatchShape(exits.length));
        const rackTop = document.querySelector("#rack-room-racks .rack-block .rack-top-items");
        if (face && rackTop) {
            // Leftmost on the plank; not a .rack-top-item, so arrange mode
            // and rack-top ordering never move it.
            const holder = rackText("div", "rack-top-patch", "");
            holder.setAttribute("aria-label", "Patch panel");
            holder.append(face);
            rackTop.prepend(holder);
        }
        target.replaceChildren(...(face && !rackTop ? [face] : []));
        box.hidden = !face || Boolean(rackTop);
    }

    /**
     * Draw current UPS readings for equipment placed in this room. Each card
     * opens the existing inspector, where voltage, alerts, and provenance are
     * available. Unknown values stay unknown rather than becoming zero.
     *
     * @returns {void}
     */
    renderRoomPower() {
        const panel = rackElement("rack-power");
        const target = rackElement("rack-power-items");
        const focusedId = document.activeElement?.closest?.(".rack-power-card")?.dataset.node;
        const nodes = this.nodes.filter((node) => node.category === "ups"
            && node.placement?.container_room_id === this.roomId);
        panel.hidden = nodes.length === 0;
        target.replaceChildren();
        for (const node of nodes) {
            const found = typeof upsReading === "function" ? upsReading(node) : null;
            const reading = found?.reading;
            const age = reading?.sampled_at ? Date.now() - Date.parse(reading.sampled_at) : Infinity;
            const stale = !Number.isFinite(age) || age > UPS_STALE_MILLISECONDS;
            const condition = reading ? upsCondition(reading) : null;
            const charge = Number.isFinite(reading?.battery_charge)
                ? Math.min(100, Math.max(0, reading.battery_charge)) : null;
            const card = rackText("button", "rack-power-card", "");
            card.type = "button";
            card.dataset.node = node.id;
            card.dataset.level = !reading ? "unknown" : stale ? "stale" : condition.level;
            card.setAttribute("aria-pressed", String(node.id === this.selectedId));
            // A status lamp left of the name in place of the state word
            // (owner); the word stays in the tooltip and for screen readers.
            const stateLabel = !reading ? "No reading" : stale ? "Reading stale" : condition.label;
            const head = rackText("span", "rack-power-head", "");
            const lamp = rackText("span", "rack-power-lamp", "");
            lamp.setAttribute("aria-hidden", "true");
            lamp.title = stateLabel;
            head.append(
                lamp, rackText("strong", "", node.name), rackText("span", "visually-hidden", `, ${stateLabel}`)
            );
            card.append(head);
            // A thick gauge with the charge printed inside it, no caption
            // (owner): the figure is drawn twice, in the gauge color over the
            // empty track and in the card color over the fill, the second
            // copy clipped to the fill so each part reads inverted.
            const battery = rackText("span", "rack-power-battery", "");
            if (charge === null) {
                battery.append(rackText("span", "rack-power-charge", "—"));
            } else {
                const percent = `${Math.round(charge)}%`;
                const gauge = rackText("span", "rack-power-gauge", "");
                gauge.setAttribute("role", "meter");
                gauge.setAttribute("aria-label", `${node.name} battery charge`);
                gauge.setAttribute("aria-valuemin", "0");
                gauge.setAttribute("aria-valuemax", "100");
                gauge.setAttribute("aria-valuenow", String(charge));
                gauge.setAttribute("aria-valuetext", percent);
                gauge.style.setProperty("--charge", `${Math.max(0, Math.min(100, charge))}%`);
                // Under 10% the bar turns red whatever the UPS reports (owner).
                gauge.classList.toggle("is-low", charge < 10);
                const fill = rackText("span", "rack-power-gauge-fill", "");
                const label = rackText("span", "rack-power-gauge-label", percent);
                const inverse = rackText("span", "rack-power-gauge-label is-inverse", percent);
                label.setAttribute("aria-hidden", "true");
                inverse.setAttribute("aria-hidden", "true");
                gauge.append(fill, label, inverse);
                battery.append(gauge);
            }
            card.append(battery);
            const facts = rackText("span", "rack-power-facts", "");
            const fact = (label, value) => {
                const item = rackText("span", "rack-power-fact", "");
                item.append(rackText("small", "", label), rackText("strong", "", value));
                facts.append(item);
            };
            fact("Runtime", reading ? upsRuntime(reading.battery_runtime_s) : "—");
            fact("Load", Number.isFinite(reading?.load_percent) ? `${Math.round(reading.load_percent)}%` : "—");
            fact("Power", Number.isFinite(reading?.real_power_w) ? `${Math.round(reading.real_power_w)} W`
                : Number.isFinite(reading?.apparent_power_va) ? `${Math.round(reading.apparent_power_va)} VA` : "—");
            card.append(facts);
            // No "Read … ago" line (owner); only a missing reading is said.
            if (!reading) card.append(rackText("small", "rack-power-read", "No NUT reading yet"));
            if (condition?.alerts.length) card.append(rackText(
                "span", "rack-power-alerts", condition.alerts.join(" · ")
            ));
            card.addEventListener("click", () => this.select(node.id));
            target.append(card);
        }
        if (focusedId) {
            [...target.children].find((card) => card.dataset.node === focusedId)?.focus({ preventScroll: true });
        }
    }

    /**
     * The inferred Wi-Fi client sample, shared with DeviceDetails.
     *
     * @param {object} node - The device record.
     * @returns {?object} The wireless sample, or null.
     */
    wirelessFor(node) {
        return this.details.wirelessFor(node);
    }

    /**
     * Name a placed device with a fresh Wi-Fi client match as one in its
     * accessible label. Its visual is a Wi-Fi port lamp (attachSidePorts),
     * not a text badge (owner).
     *
     * @param {HTMLElement} card - The device's card.
     * @param {object} node - The device record.
     */
    appendWirelessBadge(card, node) {
        const wireless = this.wirelessFor(node);
        if (wireless?.state !== "connected" || !wireless.fresh) return;
        card.setAttribute("aria-label", `${card.getAttribute("aria-label") || node.name}, `
            + `inferred Wi-Fi client${wireless.ap_name ? ` of ${wireless.ap_name}` : ""}`);
    }

    /**
     * Return a small battery icon and charge percentage for a placed UPS.
     * The reading's age controls its appearance; a missing charge remains an
     * outlined unknown battery rather than an empty one.
     *
     * @param {object} node - UPS topology node.
     * @returns {HTMLElement} Visual badge with a stable node identifier.
     */
    upsBadge(node) {
        const reading = typeof upsReading === "function" ? upsReading(node)?.reading : null;
        const age = reading?.sampled_at ? Date.now() - Date.parse(reading.sampled_at) : Infinity;
        const stale = !Number.isFinite(age) || age > UPS_STALE_MILLISECONDS;
        const charge = Number.isFinite(reading?.battery_charge)
            ? Math.min(100, Math.max(0, reading.battery_charge)) : null;
        const condition = reading ? upsCondition(reading) : null;
        const badge = rackText("span", "rack-ups-badge", "");
        badge.dataset.node = node.id;
        badge.dataset.level = !reading ? "unknown" : stale ? "stale" : condition.level;
        badge.setAttribute("aria-hidden", "true");
        const icon = rackText("span", "rack-ups-battery-icon", "");
        if (charge !== null) {
            const fill = rackText("span", "rack-ups-battery-fill", "");
            fill.style.width = `${charge}%`;
            icon.append(fill);
        }
        badge.append(
            icon, rackText("span", "rack-ups-battery-value", charge === null ? "—" : `${Math.round(charge)}%`)
        );
        badge.title = this.upsBatteryLabel(node);
        return badge;
    }

    /**
     * Describe a UPS charge and state for the containing equipment button.
     *
     * @param {object} node - The UPS's device record.
     * @returns {string} Such as "battery 87%, on battery", "battery 87%,
     *     reading stale", or "battery reading unavailable".
     */
    upsBatteryLabel(node) {
        const reading = typeof upsReading === "function" ? upsReading(node)?.reading : null;
        if (!reading) return "battery reading unavailable";
        const charge = Number.isFinite(reading.battery_charge)
            ? `${Math.round(Math.min(100, Math.max(0, reading.battery_charge)))}%` : "unknown";
        const age = reading.sampled_at ? Date.now() - Date.parse(reading.sampled_at) : Infinity;
        return `battery ${charge}, ${!Number.isFinite(age) || age > UPS_STALE_MILLISECONDS
            ? "reading stale" : upsCondition(reading).label.toLowerCase()}`;
    }

    /**
     * Add the UPS badge and accessible battery summary to a placement card.
     *
     * @param {HTMLElement} card - The UPS's card.
     * @param {object} node - Its device record.
     * @param {string} baseLabel - The card's label before the battery summary.
     */
    appendUpsBadge(card, node, baseLabel) {
        if (node.category !== "ups") return;
        card.dataset.upsLabelBase = baseLabel;
        card.setAttribute("aria-label", `${baseLabel}, ${this.upsBatteryLabel(node)}`);
        card.append(this.upsBadge(node));
    }

    /** Refresh placed UPS indicators after a telemetry read without redrawing racks. */
    refreshUpsBadges() {
        const nodes = new Map(this.nodes.filter((node) => node.category === "ups")
            .map((node) => [node.id, node]));
        for (const badge of this.root.querySelectorAll(".rack-ups-badge")) {
            const node = nodes.get(badge.dataset.node);
            if (!node) continue;
            const card = badge.parentElement;
            badge.replaceWith(this.upsBadge(node));
            card.setAttribute("aria-label", `${card.dataset.upsLabelBase}, ${this.upsBatteryLabel(node)}`);
        }
    }

    /**
     * Summarize a device's power state for its lamp.
     *
     * The host's own sources decide; an inferred BMC record does not
     * (deviceManagement in device-details.js). Off with the BMC still answering
     * is "standby", not plain offline (owner).
     *
     * @param {object} node - The device record.
     * @returns {string} "online", "standby", "offline", "stale", or "unknown".
     */
    deviceState(node) {
        // The host's own sources decide; an inferred BMC record does not
        // (deviceManagement in device-details.js). Off with the BMC still
        // answering is "standby", not plain offline (owner).
        const managed = new Set(deviceManagement(node).observations.map((item) => item.id));
        const host = (node.observations || []).filter((item) => !managed.has(item.id));
        if (!host.length) return node.observations?.length ? "online" : "unknown";
        if (host.some((item) => item.status === "offline")) {
            return deviceHostPower(node).bmcReachable ? "standby" : "offline";
        }
        const newest = Math.max(...host.map((item) => new Date(item.last_seen_at || 0).getTime()));
        if (Date.now() - newest > 60 * 60000) return "stale";
        if (host.some((item) => item.status === "online")
            || Date.now() - newest <= RACK_FRESH_MILLISECONDS) return "online";
        return "unknown";
    }

    /**
     * Fill each front-facing server's drive bays (owner: hot-swap bays with
     * health lamps on the rack view): the device is matched to a Proxmox
     * node by observation (device-cluster.js, which caches the cluster API
     * for a minute), and its drives are the same bays, grading, and pool
     * colours as the Storage overview (shelf-model.js), drawn by the bay
     * strip widget. A device that is no node keeps an empty, hidden holder.
     *
     * @returns {Promise<void>} Resolves when the bays are drawn.
     */
    async fillDriveBays() {
        const holders = [...this.root.querySelectorAll(".rack-drive-bays")];
        const clusters = window.LabratDeviceCluster;
        const shelves = window.LabratShelfModel;
        const widgets = window.LabratWidgets;
        if (!holders.length || !clusters?.cluster || !shelves || !widgets?.bayStrip) return;
        const data = await clusters.cluster();
        if (!data) return;
        const bySource = new Map();
        for (const holder of holders) {
            const node = this.nodes.find((item) => item.id === holder.dataset.node);
            const found = holder.isConnected && node ? clusters.match(data, node) : null;
            if (!found || found.kind !== "node") continue;
            if (!bySource.has(found.source)) bySource.set(found.source, shelves.drivesByNode(found.source));
            const bays = bySource.get(found.source).get(found.node.name) || [];
            holder.replaceChildren(...(bays.length ? [widgets.bayStrip(bays)] : []));
        }
    }

    /**
     * Build a device's faceplate status lamp.
     *
     * @param {object} node - Device node.
     * @returns {HTMLElement} Lamp element (decorative; state is in its title).
     */
    statusLamp(node) {
        const state = this.deviceState(node);
        const lamp = rackText("span", "rack-status-lamp", "");
        lamp.dataset.state = state;
        lamp.title = { online: "Online", offline: "Reported offline", standby: "Powered off · BMC reachable",
            stale: "Not seen for an hour", unknown: "No collector data" }[state];
        lamp.setAttribute("aria-hidden", "true");
        return lamp;
    }

    /**
     * Fill the room counts readout. On the first load the numbers roll up
     * from zero (a short flourish); later updates set them directly.
     *
     * @param {number} rackCount - Racks in the room.
     * @param {number} mounted - Devices mounted in racks.
     * @param {number} elsewhere - Devices elsewhere in the room.
     * @returns {void}
     */
    renderSummary(rackCount, mounted, elsewhere) {
        const target = rackElement("rack-summary");
        const inRoom = new Set(this.nodes.filter((node) => node.placement?.container_room_id === this.roomId).map((node) => node.id));
        const cables = this.links.filter((link) => inRoom.has(link.from) || inRoom.has(link.to)).length;
        const portsUp = this.nodes.filter((node) => inRoom.has(node.id))
            .reduce((sum, node) => sum + node.ports.filter((port) => this.portIsUp(port, node.id)).length, 0);
        const roomCables = this.links.filter(link => inRoom.has(link.from) && inRoom.has(link.to)).length;
        const values = rackCount && !this.wiringView
            ? [
                ["Racks", rackCount], ["Mounted", mounted], ["Elsewhere", elsewhere], ["Cables", cables],
                ["Ports up", portsUp]
            ]
            : [
                ["Devices", inRoom.size], ["Area cables", roomCables], ["External cables", cables - roomCables],
                ["Ports up", portsUp]
            ];
        target.setAttribute("aria-label", values.map(([label, value]) => `${value} ${label.toLowerCase()}`).join(", "));
        target.replaceChildren();
        const readouts = [];
        for (const [label, value] of values) {
            const cell = rackText("span", "rack-telemetry-cell", "");
            const number = rackText("span", "rack-telemetry-value", String(value).padStart(2, "0"));
            cell.append(rackText("span", "rack-telemetry-label", label), number);
            target.append(cell);
            readouts.push([number, value]);
        }
        if (this.summaryRolled
            || document.hidden
            || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
        this.summaryRolled = true;
        const started = performance.now();
        const roll = () => {
            const progress = Math.min(1, (performance.now() - started) / 700);
            const eased = 1 - (1 - progress) ** 3;
            for (const [number, value] of readouts) number.textContent = String(Math.round(value * eased)).padStart(2, "0");
            if (progress < 1) requestAnimationFrame(roll);
        };
        requestAnimationFrame(roll);
    }

    /**
     * Name the open device in the address (/areas/1/equipment/35, or
     * /wiring/35 on the Diagram), so a refresh or a shared link reopens it;
     * null returns to the view's own address.
     *
     * @param {string|null} nodeId - Open device's node ID, or null.
     * @returns {void}
     */
    showDeviceInUrl(nodeId) {
        if (!this.roomId) return;
        labratShowDeviceInUrl(`/areas/${this.roomId}/${this.wiringView ? "wiring" : "equipment"}`, nodeId);
    }

    /**
     * Select a device and reveal its details and editable placement fields.
     *
     * @param {string} nodeId - The device's ID.
     */
    select(nodeId) {
        if (this.arrangement) return;
        this.selectedId = nodeId;
        // Focus the rack the device is mounted in, so its editor defaults match.
        const node = this.nodes.find((item) => item.id === nodeId);
        const mountedIn = this.racks.find((rack) => (
            node?.placement?.container_id === rack.containerId
            || node?.placement?.container_id === rack.topContainerId
            || rack.rackShelves.some((shelf) => shelf.id === node?.placement?.container_id)
        ));
        if (mountedIn) this.rackId = mountedIn.id;
        this.openDrawer();
        // Details is where this drawer starts: no Back to Add equipment, a
        // leftover of the early page (owner), as on Discovery and Clients.
        this.viewStack = [];
        this.activateView("details", "forward");
        this.showDeviceInUrl(nodeId);
        this.render();
        // The drawer is fixed to the viewport at every width. Focus its
        // visible title without scrolling to the old inline sidebar or the
        // hidden panel heading while the opening animation is in progress.
        rackElement("device-drawer-title").focus({ preventScroll: true });
    }

    /**
     * Switch-side LLDP observations, shared with DeviceDetails.
     *
     * @param {object} node - The device record.
     * @returns {object[]} The neighbors seen on its ports.
     */
    neighborsFor(node) {
        return this.details.neighborsFor(node);
    }

    /** Show the selected device in the shared details panel (DeviceDetails). */
    renderInspector() {
        this.details.render(this.selectedNode());
    }

    /**
     * Name a room for the Add equipment list ("in Garage").
     *
     * @param {number|null} roomId - Room ID.
     * @returns {string|null} Its name.
     */
    roomNameFor(roomId) {
        return this.locationsData?.rooms?.find((room) => room.id === roomId)?.name || null;
    }

    /** Enable rack position fields only when manual equipment is being mounted. */
    updateManualFields() {
        const mode = rackElement("rack-manual-place").value;
        const inRack = mode === "rack" || mode === "rack_shelf";
        const standardRackPlacement = mode === "rack";
        const onTop = mode === "on_top";
        const fields = rackElement("rack-manual-position-fields");
        fields.hidden = !inRack;
        rackElement("rack-manual-height-field").hidden = !standardRackPlacement;
        rackElement("rack-manual-u-label").textContent = mode === "rack_shelf"
            ? "Shared shelf U" : "Bottom rack unit";
        rackElement("rack-manual-shared-shelf-help").hidden = mode !== "rack_shelf";
        this.updateFaceOptions("rack-manual-face", this.currentRack());
        rackElement("rack-manual-u").required = inRack;
        rackElement("rack-manual-height").required = standardRackPlacement;
        rackElement("rack-manual-u").max = String(this.currentRack()?.height_u || 128);
        if (inRack) this.suggestManualPosition();
        rackElement("rack-manual-form").querySelector("button[type='submit']").disabled =
            (inRack && !this.currentRack()?.containerId)
            || (onTop && !this.currentRack()?.topContainerId);
    }

    /**
     * Create manual inventory for equipment not supplied by an adapter.
     *
     * @param {SubmitEvent} event - The manual form's submit.
     * @returns {Promise<void>} Resolves once saved and redrawn.
     */
    async createManual(event) {
        event.preventDefault();
        const rack = this.currentRack();
        const payload = {
            name: rackElement("rack-manual-name").value,
            class_name: rackElement("rack-manual-class").value,
            height_u: Number(rackElement("rack-manual-height").value),
            port_facing: rackElement("rack-manual-port-facing").value || null,
        };
        const mode = rackElement("rack-manual-place").value;
        if (mode === "rack") {
            payload.container_id = rack?.containerId;
            payload.rack_u = Number(rackElement("rack-manual-u").value);
            payload.face = rackElement("rack-manual-face").value;
            payload.placement_mode = "rack";
        } else if (mode === "rack_shelf") {
            payload.container_id = rack?.containerId;
            payload.rack_u = Number(rackElement("rack-manual-u").value);
            payload.face = rackElement("rack-manual-face").value;
            payload.placement_mode = "rack_shelf";
            payload.position = { x: 1 };
        } else if (mode === "on_top") {
            payload.container_id = rack?.topContainerId;
            payload.position = { x: 0.5, y: 0.5 };
        }
        try {
            const result = await labratJson(LABRAT_API.rackDevices, { payload, csrfToken: this.csrfToken });
            this.selectedId = `device:${result.device_id}`;
            rackElement("rack-manual-form").reset();
            rackElement("rack-manual-u").value = "";
            await this.load();
            this.returnView("equipment-manual-open");
            this.showNotice("Equipment added.", true);
        } catch (error) {
            this.manualFeedback(error.message);
        }
    }
}

/**
 * Join a part's methods to RackViewApp. The Equipment page splits the app
 * by concern (rack-cables.js, rack-arrange-mode.js, rack-live.js); each part
 * is a class of methods that is never constructed, only copied in here, so
 * its methods run with `this` as the page's app.
 *
 * @param {Function} part - A class whose prototype methods join RackViewApp.
 * @returns {void}
 */
function rackAddMethods(part) {
    for (const name of Object.getOwnPropertyNames(part.prototype)) {
        if (name === "constructor") continue;
        Object.defineProperty(RackViewApp.prototype, name, Object.getOwnPropertyDescriptor(part.prototype, name));
    }
}

/**
 * Begin Rack View once the page and every deferred script (the app's parts
 * included) have loaded.
 */
const rackRoot = document.getElementById("rack-app");
if (rackRoot) {
    document.addEventListener("DOMContentLoaded", () => {
        const rackApp = new RackViewApp(rackRoot);
        rackApp.load().then(() => rackApp.startLiveUpdates(), () => rackApp.startLiveUpdates());
    });
}
