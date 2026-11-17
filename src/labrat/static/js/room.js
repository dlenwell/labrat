/**
 * @fileoverview Dimensionless room footprints and explicit equipment moves.
 *
 * The room canvas stores only relative layout positions. Rack units remain in
 * Rack View, and dragging a footprint changes inventory only after the API
 * acknowledges an explicit move.
 *
 * Unplaced equipment and Build out are panels of the shared device drawer
 * (device-drawer.js), opened by icon tools in the heading; clicking a device
 * opens its shared details (device-details.js) there, and the address names
 * it (/areas/<area>/floor-plan/<device>) so a refresh keeps it open.
 * @copyright 2026 LabRat contributors
 * @license Apache-2.0
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Resolve an element that is required by the Room View template.
 *
 * @param {string} id - The element's ID.
 * @returns {HTMLElement} The element.
 * @throws {Error} When the template does not have it.
 */
function roomElement(id) {
    const element = document.getElementById(id);
    if (!element) {
        throw new Error(`Missing Room View element: ${id}`);
    }
    return element;
}

/**
 * Clamp a finite room coordinate to the unit interval.
 *
 * @param {number} value - A normalized coordinate.
 * @param {number} [fallback=0.5] - Used when the value is not finite.
 * @returns {number} The value between 0 and 1.
 */
function roomFraction(value, fallback = 0.5) {
    return Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : fallback;
}

/**
 * Map a device to one of the shared device-type keys used for color.
 *
 * Discovered devices carry a category; manual equipment only has a free-text
 * class name, so both are matched loosely. The keys match the
 * ``--color-type-*`` tokens in main.css.
 *
 * @param {object} device - Inventory or discovered device record.
 * @returns {string} "server", "switch", "router", "firewall",
 *     "access_point", "ups", or "other".
 */
function roomCategoryKey(device) {
    const kind = `${device.category || device.class_name || ""}`.toLowerCase()
        .replace(/[_-]+/g, " ");
    if (kind.includes("switch")) return "switch";
    if (kind.includes("router") || kind.includes("gateway")) return "router";
    if (kind.includes("firewall")) return "firewall";
    if (kind.includes("access point") || kind.includes("wireless")) return "access_point";
    if (kind.includes("ups")) return "ups";
    if (kind.includes("server")) return "server";
    return "other";
}

/**
 * Return a short, accessible glyph for a broad equipment type.
 *
 * @param {object} device - The device record (its category or class name).
 * @returns {string} A one-character glyph.
 */
function roomDeviceGlyph(device) {
    const kind = `${device.category || device.class_name || ""}`.toLowerCase()
        .replace(/[_-]+/g, " ");
    if (kind.includes("switch")) return "⇄";
    if (kind.includes("router")) return "⌘";
    if (kind.includes("firewall")) return "▥";
    if (kind.includes("access point") || kind.includes("wireless")) return "◉";
    if (kind.includes("ups")) return "ϟ";
    if (kind.includes("server")) return "▤";
    return "▣";
}

/**
 * Manage room layout data, accessible footprint rendering, and drop actions.
 */
class RoomViewApp {
    /** Read room identity and prepare shared UI state. */
    constructor() {
        this.root = roomElement("room-app");
        this.roomId = Number(this.root.dataset.roomId);
        this.csrfToken = this.root.dataset.csrfToken;
        this.layout = null;
        this.topology = null;
        this.devices = [];
        this.dragged = null;
        this.layoutIndex = 0;
        this.noticeTimer = null;
        this.locations = null;
        this.selectedNodeId = null;
        // The shared drawer: Unplaced equipment, Build out, and a device's
        // details. Clicks on devices and the heading tools switch panels
        // rather than closing it.
        this.drawer = new DeviceDrawer({
            host: this.root,
            rootView: "shelf",
            closeAtRoot: true,
            onClose: () => this.drawerClosed(),
            onActivate: (view, previous) => {
                // Leaving the rack editor saves it (rack-editor.js commit),
                // and leaving Build out adds its open, filled-in section.
                if (previous === "rack-edit" && view !== "rack-edit") void this.rackEditor?.commit();
                if (previous === "area-edit" && view !== "area-edit") void this.areaEditor?.commit();
                if (previous === "buildout" && view !== "buildout") this.commitBuildout();
                this.updateTools();
            },
            keepsOpen: (target) => Boolean(target.closest(
                ".room-device, .equipment-list-item, .room-furniture-edit, .room-rack-slot, #room-unplaced-tool, #room-buildout-tool, #area-edit-tool")),
        });
        this.details = new DeviceDetails({
            drawer: this.drawer,
            csrfToken: this.csrfToken,
            nodes: () => this.topology?.nodes || [],
            links: () => this.topology?.links || [],
            neighborMatches: () => this.topology?.neighbor_matches || [],
            locations: () => this.locations,
            reload: () => this.load(),
            notify: (message, ok) => this.showNotice(message, !ok),
            onPlace: (node) => this.placement.open(node),
        });
        // The shared rack editor, the same one Equipment uses (owner).
        this.rackEditor = new RackEditor({
            csrfToken: this.csrfToken,
            racks: () => (this.layout?.racks || []).map((rack) => {
                const container = this.layout.containers.find((item) => item.kind === "rack"
                    && item.rack_id === rack.id);
                return { ...rack, rackShelves: this.layout.containers.filter((item) => (
                    item.kind === "rack_shelf" && item.parent_id === container?.id)) };
            }),
            shelfCount: (shelfId) => this.devices.filter((device) => device.placement?.container_id === shelfId).length,
            reload: () => this.load(),
            notify: (message, ok) => this.showNotice(message, !ok),
            close: () => this.drawer.close(),
        });
        // Edit area (area-editor.js), from the heading's pencil (owner):
        // the drawer's first panel, saved when it closes.
        this.areaEditor = new AreaEditor({
            csrfToken: this.csrfToken,
            show: () => {
                this.drawer.reset("area-edit");
                this.drawer.open();
                this.updateTools();
            },
            notify: (message, ok) => this.showNotice(message, !ok),
        });
        this.placement = new DevicePlacement({
            drawer: this.drawer,
            csrfToken: this.csrfToken,
            locations: () => this.locations,
            reload: () => this.load(),
            notify: (message, ok) => this.showNotice(message, !ok),
        });
    }

    /**
     * Open one of the drawer's room panels from its heading tool.
     *
     * @param {"unplaced"|"buildout"} view - Panel to show.
     * @returns {void}
     */
    openPanel(view) {
        void this.commitFurniture();
        this.selectedNodeId = null;
        this.showDeviceInUrl(null);
        this.drawer.reset(view, [], "forward");
        this.drawer.open();
        if (view === "unplaced") roomElement("equipment-search").focus({ preventScroll: true });
    }

    /**
     * A pencil beside a rack's or shelf's name that opens its edit panel.
     *
     * @param {object} container - The rack or shelf container.
     * @returns {HTMLButtonElement} The button.
     */
    furnitureEditButton(container) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "room-furniture-edit";
        button.draggable = false;
        const label = `Edit ${container.name}: rename or delete`;
        button.setAttribute("aria-label", label);
        button.title = label;
        const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        icon.setAttribute("viewBox", "0 0 24 24");
        icon.setAttribute("aria-hidden", "true");
        const path = document.createElementNS(icon.namespaceURI, "path");
        path.setAttribute("d", "M4 20h4L19 9l-4-4L4 16zM14 6l4 4");
        icon.append(path);
        button.append(icon);
        // Neither a move nor a click on the furniture behind it.
        button.addEventListener("pointerdown", (event) => event.stopPropagation());
        button.addEventListener("click", (event) => {
            event.stopPropagation();
            this.openFurniture(container);
        });
        return button;
    }

    /**
     * Open the edit panel for a rack (the shared rack editor, as on
     * Equipment) or a shelf (its name). Changes save when the panel closes
     * or another panel opens (owner); Delete is a tool in the drawer bar.
     *
     * @param {object} container - The rack or shelf container.
     * @returns {Promise<void>} Resolves once the panel shows.
     */
    async openFurniture(container) {
        await this.commitFurniture();
        if (container.kind === "rack") {
            // Racks use the shared rack editor, the same as Equipment's.
            if (!this.rackEditor.open(container.rack_id)) return;
            this.selectedNodeId = null;
            this.showDeviceInUrl(null);
            this.drawer.reset("rack-edit", [], "forward");
            this.drawer.open();
            roomElement("rack-edit-name").focus();
            return;
        }
        this.editing = container;
        this.selectedNodeId = null;
        this.showDeviceInUrl(null);
        roomElement("room-furniture-title").textContent = "Edit shelf";
        roomElement("room-furniture-name").value = container.name;
        roomElement("room-furniture-help").textContent = "Changes save when you close this panel.";
        const remove = roomElement("room-furniture-delete");
        const label = "Delete shelf: asks first, can unplace what is on it";
        remove.setAttribute("aria-label", label);
        remove.title = label;
        remove.dataset.tooltip = "Delete shelf";
        this.editingStart = this.furnitureDraft();
        this.drawer.reset("furniture", [], "forward");
        this.drawer.open();
        roomElement("room-furniture-name").focus();
    }

    /** @returns {{name: string}} The shelf panel's values. */
    furnitureDraft() {
        return { name: roomElement("room-furniture-name").value.trim() };
    }

    /**
     * Save the open edit panel if anything changed, then forget it. Called
     * when the drawer closes or another panel opens.
     *
     * @returns {Promise<void>} Resolves after the save (or nothing to do).
     */
    async commitFurniture() {
        const container = this.editing;
        const start = this.editingStart;
        this.editing = null;
        this.editingStart = null;
        if (!container || !start) return;
        const draft = this.furnitureDraft();
        if (draft.name === start.name) return;
        if (!draft.name) {
            this.showNotice("A name is required, so nothing was saved.", true);
            return;
        }
        try {
            await this.request(LABRAT_API.container(container.id), { name: draft.name });
            await this.load();
            this.showNotice(`Saved ${draft.name}.`, false);
        } catch (error) {
            this.showNotice(error.message || "Could not save this.", true);
        }
    }

    /**
     * Delete the edited rack or shelf after the shared confirmation.
     *
     * @param {HTMLElement} opener - The Delete button, focused again if the
     *     dialog is cancelled.
     * @returns {Promise<void>} Resolves once deleted and redrawn, or cancelled.
     */
    async deleteFurniture(opener) {
        const container = this.editing;
        if (!container) return;
        const path = `${LABRAT_API.container(container.id)}/delete`;
        try {
            const result = await confirmRemoval(path, this.csrfToken, opener);
            if (!result) return;
            // Deleted: nothing left to save on close.
            this.editing = null;
            this.editingStart = null;
            this.drawer.close();
            await this.load();
            this.showNotice(`Deleted ${container.name}.`, false);
        } catch (error) {
            this.showNotice(error.message || "Could not delete this.", true);
        }
    }

    /**
     * The ? tool's help (owner): it opens under the tool on a mouse hover
     * and closes when the pointer leaves both, unless a click pinned it
     * open; a click toggles it as a native popover (Escape and an outside
     * click close it).
     *
     * @returns {void}
     */
    bindHelp() {
        const help = roomElement("room-help");
        const tool = roomElement("room-help-tool");
        if (!help.showPopover) return;
        let timer = null;
        let pinned = false;
        const place = () => {
            const box = tool.getBoundingClientRect();
            const width = Math.min(help.offsetWidth || 380, window.innerWidth - 16);
            help.style.setProperty("top", `${box.bottom + 8}px`);
            help.style.setProperty(
                "left", `${Math.max(8, Math.min(box.right - width, window.innerWidth - width - 8))}px`
            );
        };
        help.addEventListener("toggle", (event) => {
            if (event.newState === "open") place();
            else pinned = false;
        });
        tool.addEventListener("click", () => { pinned = !help.matches(":popover-open"); });
        const open = () => { clearTimeout(timer); timer = setTimeout(
            () => { if (!help.matches(":popover-open")) help.showPopover(); }, 250
        ); };
        const close = () => {
            clearTimeout(timer);
            timer = setTimeout(() => { if (!pinned && help.matches(":popover-open")) help.hidePopover(); }, 250);
        };
        for (const element of [tool, help]) {
            element.addEventListener("pointerenter", (event) => { if (event.pointerType === "mouse") open(); });
            element.addEventListener("pointerleave", (event) => { if (event.pointerType === "mouse") close(); });
        }
    }

    /** Keep the heading tools' expanded state on the open panel. */
    updateTools() {
        const open = this.drawer.isOpen;
        roomElement("room-unplaced-tool").setAttribute("aria-expanded",
            String(open && this.drawer.currentView === "unplaced"));
        roomElement("room-buildout-tool").setAttribute("aria-expanded",
            String(open && this.drawer.currentView === "buildout"));
    }

    /** After the drawer closes: no device selected, the page's own address. */
    drawerClosed() {
        // Closing resets the drawer to its first panel, so leaving Build out
        // adds its open section through onActivate (commitBuildout).
        void this.commitFurniture();
        void this.rackEditor.commit();
        this.selectedNodeId = null;
        this.showDeviceInUrl(null);
        this.drawer.reset("shelf");
        this.updateTools();
    }

    /**
     * Name the open device in the address (/areas/1/floor-plan/35).
     *
     * @param {string|null} nodeId - Open device's node ID, or null.
     * @returns {void}
     */
    showDeviceInUrl(nodeId) {
        labratShowDeviceInUrl(`/areas/${this.roomId}/floor-plan`, nodeId);
    }

    /**
     * The topology node for a plan record, or a minimal one for equipment
     * only inventory knows, so the shared details can show it.
     *
     * @param {Object} device - Combined plan record.
     * @returns {Object} Node for DeviceDetails.
     */
    nodeFor(device) {
        const id = device.device_id ? `device:${device.device_id}` : String(device.id);
        const nodes = this.topology?.nodes || [];
        return nodes.find((node) => node.id === id) || {
            id, name: device.name, category: device.category || device.class_name || "equipment",
            device_id: device.device_id || null, placement: device.placement || null,
            port_facing: device.port_facing || null, ports: device.ports || [], observations: [],
        };
    }

    /** Attach controls and load the room's saved inventory. */
    async initialize() {
        roomElement("room-refresh").addEventListener("click", () => this.load());
        roomElement("room-unplaced-tool").addEventListener("click", () => this.openPanel("unplaced"));
        this.bindHelp();
        // Enter closes the panel, which saves it.
        roomElement("room-furniture-form").addEventListener("submit", (event) => {
            event.preventDefault();
            this.drawer.close();
        });
        roomElement("room-furniture-delete").addEventListener(
            "click", (event) => this.deleteFurniture(event.currentTarget)
        );
        roomElement("room-buildout-tool").addEventListener("click", () => this.openPanel("buildout"));
        // The Ceiling strip: dropping a device there mounts it overhead.
        this.bindDropTarget(roomElement("room-ceiling"), () => this.dragged?.kind === "device",
            (event) => this.dropEquipment(event));
        // Forget the drag once it ends anywhere, so a stale drag never
        // makes a drop target accept something it should not.
        document.addEventListener("dragend", () => {
            this.dragged = null;
            for (const element of document.querySelectorAll(".is-drop-target")) {
                element.classList.remove("is-drop-target");
            }
        });
        // Dropping a device from the plan onto the equipment list unplaces it.
        this.bindDropTarget(roomElement("equipment-list-panel"),
            () => this.dragged?.kind === "device" && this.draggedDeviceInRoom(),
            () => this.unplaceDragged());
        // The shared Add equipment list (equipment-list.js), the same as
        // Equipment's: rows open details and drag onto the plan.
        this.equipmentList = new EquipmentList({
            items: () => this.devices,
            inRoom: (device) => this.isInRoom(device),
            where: (device) => {
                if (!device.placement) return "unplaced";
                const room = this.locations?.rooms?.find((item) => item.id === device.placement.container_room_id);
                return `in ${room?.name || device.placement.container_name || "another place"}`;
            },
            onSelect: (device) => this.selectDevice(device),
            onManual: () => {
                this.drawer.push("manual");
                roomElement("room-device-name").focus();
            },
            decorate: (row, device) => {
                row.draggable = true;
                row.addEventListener("dragstart", (event) => {
                    this.dragged = { kind: "device", id: device.id };
                    // A row is not the footprint it becomes: drop it centered.
                    this.rememberGrab(event, row, true);
                    event.dataTransfer.setData("text/plain", JSON.stringify(this.dragged));
                    event.dataTransfer.effectAllowed = "move";
                });
            },
            isSelected: (device) => this.nodeFor(device).id === this.selectedNodeId,
        });
        roomElement("room-rack-form").addEventListener("submit", (event) => this.createRack(event));
        roomElement("room-shelf-form").addEventListener("submit", (event) => this.createShelf(event));
        roomElement("room-manual-form").addEventListener("submit", (event) => this.createManualDevice(event));
        // Build out is an accordion (owner): opening a section closes the
        // others (the shared details name does it natively; this covers
        // browsers without it). Typing marks a section filled in.
        const sections = [...document.querySelectorAll(".room-add")];
        for (const section of sections) {
            section.addEventListener("toggle", () => {
                if (!section.open) return;
                for (const other of sections) if (other !== section) other.open = false;
            });
            section.querySelector("form")?.addEventListener("input", (event) => {
                event.currentTarget.dataset.touched = "true";
            });
        }
        roomElement("room-floor").addEventListener("dragover", (event) => this.allowDrop(event));
        roomElement("room-floor").addEventListener("drop", (event) => this.dropEquipment(event));
        // Redraw from saved positions after a resize so footprints refit the floor.
        let resizeTimer = null;
        window.addEventListener("resize", () => {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(() => {
                if (this.layout) this.renderRoom();
            }, 150);
        });
        await this.load();
    }

    /**
     * Read room containers, canonical inventory, and discovery observations.
     *
     * @returns {Promise<void>} Resolves after the room canvas is rendered.
     */
    async load() {
        try {
            const [layout, topology, locations] = await Promise.all([
                this.request(`${LABRAT_API.room(this.roomId)}/layout`),
                this.request(LABRAT_API.topology),
                this.request(LABRAT_API.locations),
            ]);
            this.layout = layout;
            this.topology = topology;
            this.locations = locations;
            this.details.index();
            this.devices = this.combineDevices(layout.devices, topology.nodes);
            roomElement("room-title").textContent = layout.room.name;
            this.configureSurfaces();
            this.renderRoom();
            this.equipmentList.render();
            this.showNotice("");
            // Redraw the open device, or open the one a device link names.
            if (this.selectedNodeId && this.drawer.currentView === "details") {
                const node = (topology.nodes || []).find((item) => item.id === this.selectedNodeId);
                if (node) this.details.render(node);
            } else if (!this.openedFromUrl) {
                this.openedFromUrl = true;
                const requested = this.root.dataset.openDevice;
                const device = requested && this.devices.find((item) => this.nodeFor(item).id === requested);
                if (device) this.selectDevice(device);
                else if (requested) this.showDeviceInUrl(null);
            }
        } catch (error) {
            this.showNotice(`Area layout could not be loaded: ${error.message}`, true);
        }
    }

    /**
     * Call the JSON API with this page's CSRF token (main.js `labratJson`).
     *
     * @param {string} path - Same-origin API path.
     * @param {Object|null} payload - JSON body for a write request.
     * @returns {Promise<Object>} Parsed response object.
     */
    request(path, payload = null) {
        return labratJson(path, { payload, csrfToken: this.csrfToken });
    }

    /**
     * Merge canonical inventory with unlinked discovery observations.
     *
     * @param {Array<Object>} inventory - Saved canonical device records.
     * @param {Array<Object>} observations - Grouped discovered equipment.
     * @returns {Array<Object>} One display record for each known device.
     */
    combineDevices(inventory, observations) {
        const byId = new Map(inventory.map((device) => [`device:${device.id}`, {
            ...device,
            device_id: device.id,
            category: device.class_name || "equipment",
            discovery_id: null,
            ports: [],
        }]));
        for (const observed of observations) {
            if (observed.device_id !== null && byId.has(`device:${observed.device_id}`)) {
                const device = byId.get(`device:${observed.device_id}`);
                device.discovery_id = observed.discovery_id;
                device.category = observed.category || device.category;
                device.ports = observed.ports || [];
                if (observed.placement) device.placement = observed.placement;
                continue;
            }
            byId.set(observed.id, {
                ...observed,
                id: observed.device_id || observed.id,
                class_name: observed.category,
                ports: observed.ports || [],
            });
        }
        return [...byId.values()].sort((left, right) => left.name.localeCompare(right.name));
    }

    /** Configure wall drop targets and shelf-parent choices from saved records. */
    configureSurfaces() {
        const floor = this.layout.containers.find((container) => container.kind === "floor");
        // Revision 0010 gives every room a ceiling; older data may not have one.
        this.ceiling = this.layout.containers.find((container) => container.kind === "ceiling") || null;
        const strip = roomElement("room-ceiling");
        strip.hidden = !this.ceiling;
        strip.dataset.containerId = String(this.ceiling?.id || "");
        const floorTarget = roomElement("room-floor");
        floorTarget.dataset.containerId = floor ? String(floor.id) : "";
        const walls = new Map(this.layout.containers
            .filter((container) => container.kind === "wall")
            .map((container) => [container.wall_side, container]));
        for (const wall of document.querySelectorAll(".room-wall")) {
            const side = wall.dataset.side;
            const container = walls.get(side);
            wall.dataset.containerId = container ? String(container.id) : "";
            if (wall.dataset.dropBound !== "true") {
                wall.addEventListener("dragover", (event) => {
                    this.allowDrop(event);
                    wall.classList.add("is-drop-target");
                });
                // Leaving for a child (a wall device) is not leaving the wall.
                wall.addEventListener("dragleave", (event) => {
                    if (!wall.contains(event.relatedTarget)) wall.classList.remove("is-drop-target");
                });
                wall.addEventListener("drop", (event) => {
                    wall.classList.remove("is-drop-target");
                    this.dropEquipment(event);
                });
                wall.dataset.dropBound = "true";
            }
        }

        const choices = this.layout.containers.filter((container) => container.kind === "wall");
        const select = roomElement("room-shelf-parent");
        select.replaceChildren(new Option("Choose an area surface", ""));
        for (const container of choices) {
            // "East wall" already says it is a wall; only add the kind when
            // the name does not mention it (for example "Desk · surface").
            const kind = container.kind.replaceAll("_", " ");
            const label = container.name.toLowerCase().includes(kind)
                ? container.name : `${container.name} · ${kind}`;
            select.add(new Option(label, String(container.id)));
        }
    }

    /**
     * Return the room container with a matching primary key.
     *
     * @param {number} containerId - The container's ID.
     * @returns {?object} The container, or null.
     */
    containerFor(containerId) {
        return this.layout.containers.find((item) => item.id === containerId) || null;
    }

    /**
     * Return true when a device has an active placement in the current room.
     *
     * @param {object} device - The device record.
     * @returns {boolean} Whether it is placed here.
     */
    isInRoom(device) {
        return device.placement?.container_room_id === this.roomId;
    }

    /**
     * Return the active placement container for a canonical device.
     *
     * @param {object} device - The device record.
     * @returns {?object} Its container, or null when unplaced or elsewhere.
     */
    deviceContainer(device) {
        return device.placement ? this.containerFor(device.placement.container_id) : null;
    }

    /**
     * Size the canvas so the whole page, footer included, fits the window
     * (owner). Phones (600 px and under) keep the canvas's own proportions
     * and scroll.
     *
     * @returns {void}
     */
    fitCanvas() {
        const canvas = roomElement("room-canvas");
        canvas.style.removeProperty("height");
        if (window.matchMedia("(max-width: 600px)").matches) return;
        const natural = canvas.getBoundingClientRect().height;
        const overflow = document.documentElement.scrollHeight - window.innerHeight;
        // Shrink or grow by what the page is over or under the window,
        // never below a usable 360 px.
        canvas.style.setProperty("height", `${Math.max(360, Math.floor(natural - overflow))}px`);
    }

    /**
     * Render racks, shelves, walls, and device footprints in the room canvas.
     *
     * @returns {void}
     */
    renderRoom() {
        this.fitCanvas();
        roomElement("room-ceiling-items").replaceChildren();
        const layer = roomElement("room-layout-items");
        layer.replaceChildren();
        this.layoutIndex = 0;
        for (const container of this.layout.containers) {
            if (container.kind === "rack") this.renderRack(container);
            if (container.kind === "shelf" && this.containerFor(container.parent_id)?.kind === "wall") {
                this.renderShelf(container);
            }
            if (["surface", "area"].includes(container.kind) && !container.parent_id) {
                this.renderArea(container);
            }
        }

        for (const device of this.devices) {
            if (!this.isInRoom(device)) continue;
            const container = this.deviceContainer(device);
            if (!container || ["rack", "rack_top", "rack_shelf", "shelf"].includes(container.kind)) continue;
            if (container.kind === "wall") this.renderWallDevice(device, container);
            else if (container.kind === "ceiling") this.renderCeilingDevice(device);
            else this.renderFloorDevice(device, container);
        }
        this.fitFootprintsInsideFloor();
    }

    /**
     * Create a positioned visual node with shared category and drag metadata.
     *
     * @param {object} device - The device record.
     * @param {string} [className="room-device"] - The node's class.
     * @returns {HTMLButtonElement} The draggable button.
     */
    createFootprint(device, className = "room-device") {
        const item = document.createElement("button");
        item.type = "button";
        item.className = className;
        item.draggable = true;
        item.dataset.deviceId = String(device.id);
        item.dataset.category = roomCategoryKey(device);
        item.setAttribute("aria-label", `${device.name}, ${device.category || "equipment"}`);
        item.append(
            this.textNode("span", "room-device-glyph", roomDeviceGlyph(device)),
            this.textNode("span", "room-device-name", device.name),
        );
        item.addEventListener("dragstart", (event) => {
            this.dragged = { kind: "device", id: device.id };
            this.rememberGrab(event, item);
            event.dataTransfer.setData("text/plain", JSON.stringify(this.dragged));
            event.dataTransfer.effectAllowed = "move";
        });
        item.addEventListener("click", () => this.selectDevice(device));
        return item;
    }

    /**
     * Render one rack footprint, rack-top overlay, and elevated equipment link.
     *
     * @param {object} container - The rack's container.
     */
    renderRack(container) {
        const item = document.createElement("div");
        item.className = "room-rack room-surface-drop";
        item.dataset.containerId = String(container.id);
        item.dataset.dropContainerId = String(this.layout.containers.find((entry) =>
            entry.kind === "rack_top" && entry.parent_id === container.id)?.id || "");
        this.positionElement(item, container.position);
        // Flush against a wall: CSS anchors that side, so the saved offset
        // from the wall is not used (and the floor fit leaves it alone).
        const wall = container.position?.wall;
        if (wall) {
            item.dataset.wall = wall;
            if (["north", "south"].includes(wall)) item.style.removeProperty("top");
            else item.style.removeProperty("left");
        }
        item.draggable = true;
        item.addEventListener("dragstart", (event) => {
            if (event.target !== item) return;
            this.dragged = { kind: "container", id: container.id };
            this.rememberGrab(event, item);
            event.dataTransfer.setData("text/plain", JSON.stringify(this.dragged));
            event.dataTransfer.effectAllowed = "move";
        });
        item.addEventListener("dragover", (event) => this.allowDrop(event));
        item.addEventListener("drop", (event) => this.dropEquipment(event));
        const rack = this.layout.containers.find((entry) => entry.id === container.id);
        const linkedRack = this.layout.racks?.find((entry) => entry.id === rack.rack_id);
        if (linkedRack?.width_inches === 10) item.classList.add("room-rack-compact");
        // Header: just the name; no size or mounted count (owner).
        const mounted = this.devices.filter((device) => {
            const placed = this.containerFor(device.placement?.container_id);
            return placed && (placed.id === container.id
                || (placed.kind === "rack_shelf" && placed.parent_id === container.id));
        });
        const rackName = this.textNode("span", "room-rack-name", container.name);
        rackName.append(this.furnitureEditButton(container));
        item.append(rackName);
        // What sits on top of the rack first, as compact device cards with
        // the shared type icon (owner).
        const topContainer = this.containerFor(Number(item.dataset.dropContainerId));
        const onTop = this.devices.filter((entry) => entry.placement?.container_id === topContainer?.id);
        if (onTop.length) {
            const top = document.createElement("span");
            top.className = "room-rack-top";
            for (const device of onTop) {
                const topItem = this.createFootprint(device, "room-device room-top-device");
                if (typeof labratDeviceIcon === "function") {
                    topItem.querySelector(".room-device-glyph")?.replaceWith(labratDeviceIcon(
                        device.category, "room-chip-icon"
                    ));
                }
                top.append(topItem);
            }
            item.append(top);
        }
        // Then what is mounted inside, top unit first: one line per device
        // with its type stripe, name, and unit, opening its details (owner).
        const contents = document.createElement("ul");
        contents.className = "room-rack-contents";
        contents.setAttribute("aria-label", `Mounted in ${container.name}`);
        const ordered = [...mounted].sort((left, right) => (right.placement?.rack_u
            || 0) - (left.placement?.rack_u
            || 0));
        const shown = ordered.slice(0, 8);
        for (const device of shown) {
            const row = document.createElement("li");
            const button = document.createElement("button");
            button.type = "button";
            button.className = "room-rack-slot";
            button.dataset.category = roomCategoryKey(device);
            button.draggable = false;
            button.append(this.textNode("span", "room-rack-slot-name", device.name));
            if (device.placement?.rack_u) button.append(this.textNode(
                "span", "room-rack-slot-u", `U${device.placement.rack_u}`
            ));
            button.title = device.name;
            button.addEventListener("pointerdown", (event) => event.stopPropagation());
            button.addEventListener("click", (event) => { event.stopPropagation(); this.selectDevice(device); });
            row.append(button);
            contents.append(row);
        }
        if (ordered.length > shown.length) {
            contents.append(this.textNode("li", "room-rack-more", `+${ordered.length - shown.length} more`));
        }
        item.append(contents);
        // No "Front and rear" link: the view switch opens Equipment (owner).
        roomElement("room-layout-items").append(item);
    }

    /**
     * Render a shelf attached to one wall and its left-to-right contents.
     *
     * @param {object} container - The shelf's container.
     */
    renderShelf(container) {
        const parent = this.containerFor(container.parent_id);
        const shelf = document.createElement("div");
        shelf.className = `room-shelf room-surface-drop room-shelf-${parent.wall_side}`;
        shelf.dataset.containerId = String(container.id);
        const along = roomFraction(container.position?.along);
        const side = parent.wall_side;
        const crosswise = ["north", "south"].includes(side);
        if (crosswise) {
            shelf.style.left = `${along * 100}%`;
        } else {
            shelf.style.top = `${along * 100}%`;
        }
        // A saved size is a fraction of the wall's length and of the room's
        // depth away from that wall (owner); otherwise the CSS default.
        const layer = roomElement("room-layout-items");
        const wallLength = crosswise ? layer.clientWidth : layer.clientHeight;
        const roomDepth = crosswise ? layer.clientHeight : layer.clientWidth;
        if (container.position?.width) shelf.style.width = `${container.position.width * wallLength}px`;
        if (container.position?.depth) shelf.style.setProperty(
            "--shelf-depth", `${container.position.depth * roomDepth}px`
        );
        this.addShelfResize(shelf, container, side, { wallLength, roomDepth });
        shelf.draggable = true;
        shelf.addEventListener("dragstart", (event) => {
            // A resize handle never starts a move.
            if (event.target.closest?.(".room-shelf-handle")) { event.preventDefault(); return; }
            if (event.target !== shelf && !event.target.classList.contains("room-shelf-name")) return;
            this.dragged = { kind: "container", id: container.id };
            event.dataTransfer.setData("text/plain", JSON.stringify(this.dragged));
            event.dataTransfer.effectAllowed = "move";
        });
        shelf.addEventListener("dragover", (event) => this.allowDrop(event));
        shelf.addEventListener("drop", (event) => this.dropEquipment(event));
        const shelfName = this.textNode("span", "room-shelf-name", container.name);
        shelfName.append(this.furnitureEditButton(container));
        shelf.append(shelfName);
        const items = this.devices.filter((device) => device.placement?.container_id === container.id);
        for (const device of items) {
            const chip = this.createFootprint(device, "room-shelf-device");
            const coordinates = device.placement.position || {};
            chip.style.left = `${roomFraction(coordinates.x) * 100}%`;
            shelf.append(chip);
        }
        roomElement("room-layout-items").append(shelf);
    }

    /**
     * Give a wall shelf two resize handles (owner): one at its end for its
     * length along the wall, one on its front edge for its depth. The back
     * stays on the wall, and a length change grows the shelf about its
     * center. Releasing saves the size with its position.
     *
     * @param {HTMLElement} shelf - Rendered shelf.
     * @param {object} container - Its container record.
     * @param {"north"|"east"|"south"|"west"} side - Its wall.
     * @param {{wallLength: number, roomDepth: number}} room - Plan size in px.
     * @returns {void}
     */
    addShelfResize(shelf, container, side, room) {
        // Screen axes for the shelf's own length and depth: east and west
        // shelves are turned a quarter, and depth grows away from the wall.
        const axes = {
            north: { length: ["x", 1], depth: ["y", 1] },
            south: { length: ["x", 1], depth: ["y", -1] },
            east: { length: ["y", 1], depth: ["x", -1] },
            west: { length: ["y", -1], depth: ["x", 1] },
        }[side];
        for (const kind of ["length", "depth"]) {
            const handle = document.createElement("span");
            handle.className = `room-shelf-handle is-${kind}`;
            handle.title = kind === "length" ? "Drag to resize along the wall" : "Drag to resize the depth";
            handle.setAttribute("aria-hidden", "true");
            handle.addEventListener("pointerdown", (event) => {
                if (event.button !== 0) return;
                event.preventDefault();
                event.stopPropagation();
                try { handle.setPointerCapture(event.pointerId); } catch { /* Not a live pointer. */ }
                const start = { x: event.clientX, y: event.clientY,
                    width: shelf.offsetWidth, depth: shelf.offsetHeight,
                    along: roomFraction(container.position?.along) };
                let width = start.width;
                let depth = start.depth;
                let along = start.along;
                const move = (moveEvent) => {
                    const delta = (axis, sign) => sign * (axis === "x"
                        ? moveEvent.clientX - start.x : moveEvent.clientY - start.y);
                    if (kind === "length") {
                        width = Math.min(room.wallLength, Math.max(70, start.width + delta(...axes.length)));
                        // Grow about the center: it moves half the change.
                        along = roomFraction(start.along + ((width - start.width) / 2 * axes.length[1]) / room.wallLength);
                        shelf.style.width = `${width}px`;
                        if (["north", "south"].includes(side)) shelf.style.left = `${along * 100}%`;
                        else shelf.style.top = `${along * 100}%`;
                    } else {
                        depth = Math.min(room.roomDepth * 0.6, Math.max(40, start.depth + delta(...axes.depth)));
                        shelf.style.setProperty("--shelf-depth", `${depth}px`);
                    }
                };
                const done = async () => {
                    handle.removeEventListener("pointermove", move);
                    handle.removeEventListener("pointerup", done);
                    handle.removeEventListener("pointercancel", done);
                    if (width === start.width && depth === start.depth) return;
                    try {
                        await this.request(`${LABRAT_API.container(container.id)}/layout`, { position: {
                            along, width: Math.max(0.02, Math.min(1, width / room.wallLength)),
                            depth: Math.max(0.02, Math.min(1, depth / room.roomDepth)),
                        } });
                        await this.load();
                        this.showNotice(`Resized ${container.name}.`, false);
                    } catch (error) {
                        this.showNotice(error.message || "Could not resize this shelf.", true);
                        await this.load();
                    }
                };
                handle.addEventListener("pointermove", move);
                handle.addEventListener("pointerup", done);
                handle.addEventListener("pointercancel", done);
            });
            shelf.append(handle);
        }
    }

    /**
     * Render a generic room area as a movable top-down footprint.
     *
     * @param {object} container - The area's container.
     */
    renderArea(container) {
        const area = document.createElement("div");
        area.className = "room-area room-surface-drop";
        area.dataset.containerId = String(container.id);
        this.positionElement(area, container.position);
        area.append(this.textNode("span", "room-area-name", container.name));
        area.addEventListener("dragover", (event) => this.allowDrop(event));
        area.addEventListener("drop", (event) => this.dropEquipment(event));
        roomElement("room-layout-items").append(area);
    }

    /**
     * Render a floor or free-standing surface device at its saved coordinates.
     *
     * @param {object} device - The device record.
     * @param {object} container - The floor or surface it is on.
     */
    renderFloorDevice(device, container) {
        const item = this.createFootprint(device);
        item.classList.add("room-surface-drop");
        item.dataset.containerId = String(container.id);
        this.positionElement(item, device.placement.position);
        roomElement("room-layout-items").append(item);
    }

    /**
     * Render overhead equipment (for example an AP) as a card on the Ceiling
     * strip above the room, left to right by its position, like rack-top
     * gear (owner).
     *
     * @param {object} device - Device placed on the room's ceiling.
     * @returns {void}
     */
    renderCeilingDevice(device) {
        // Styled like other device cards (steel, type stripe), with the
        // shared type icon in place of the text glyph (owner).
        const item = this.createFootprint(device, "room-device room-ceiling-device");
        if (typeof labratDeviceIcon === "function") {
            item.querySelector(".room-device-glyph")?.replaceWith(labratDeviceIcon(
                device.category, "room-ceiling-icon"
            ));
        }
        item.dataset.x = String(Number(device.placement?.position?.x ?? 0.5));
        item.setAttribute("aria-label", `${device.name}, ${device.category || "equipment"}, on the ceiling`);
        const row = roomElement("room-ceiling-items");
        const after = [...row.children].find((other) => Number(other.dataset.x) > Number(item.dataset.x));
        row.insertBefore(item, after || null);
    }

    /**
     * Render a device along its assigned north/east/south/west wall.
     *
     * @param {object} device - The device record.
     * @param {object} container - The wall.
     */
    renderWallDevice(device, container) {
        const item = this.createFootprint(device, "room-wall-device");
        const wall = document.querySelector(`.room-wall-${container.wall_side}`);
        const along = roomFraction(device.placement.position?.along);
        if (container.wall_side === "north" || container.wall_side === "south") {
            item.style.left = `${along * 100}%`;
        } else {
            item.style.top = `${along * 100}%`;
        }
        wall.append(item);
    }

    /**
     * Place an element from its normalized x/y pair or deterministic fallback.
     *
     * @param {HTMLElement} element - The element to position.
     * @param {?{x: number, y: number}} position - Its saved position.
     * @param {boolean} [alongWall=false] - Position it along a wall only.
     */
    positionElement(element, position, alongWall = false) {
        const index = this.layoutIndex++;
        const fallbackX = 0.16 + (index % 5) * 0.16;
        const fallbackY = 0.22 + (Math.floor(index / 5) % 4) * 0.18;
        const x = roomFraction(position?.x, fallbackX);
        const y = roomFraction(position?.y, fallbackY);
        if (alongWall) {
            element.style.left = `${roomFraction(position?.along) * 100}%`;
        } else {
            element.style.left = `${x * 100}%`;
            element.style.top = `${y * 100}%`;
        }
    }

    /**
     * Build one text-only element so equipment labels remain safely escaped.
     *
     * @param {string} tag - The element's tag name.
     * @param {string} className - Its class attribute.
     * @param {string} value - Its text.
     * @returns {HTMLElement} The element.
     */
    textNode(tag, className, value) {
        const element = document.createElement(tag);
        element.className = className;
        element.textContent = value;
        return element;
    }

    /**
     * Open a device's shared details in the drawer (owner).
     *
     * @param {object} device - The device record.
     */
    selectDevice(device) {
        void this.commitFurniture();
        const node = this.nodeFor(device);
        this.selectedNodeId = node.id;
        this.details.render(node);
        this.drawer.reset("details", [], "forward");
        this.drawer.open("device-drawer-title");
        this.showDeviceInUrl(node.id);
        this.updateTools();
    }

    /**
     * Allow browser-native drag/drop when the target accepts layout moves.
     *
     * @param {DragEvent} event - The dragover event.
     */
    allowDrop(event) {
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
    }

    /**
     * Save a dropped device or container at a normalized room position.
     *
     * @param {DragEvent} event - Drop action on a wall, floor, shelf, or rack.
     * @returns {Promise<void>} Resolves after the layout is reloaded.
     */
    async dropEquipment(event) {
        event.preventDefault();
        event.stopPropagation();
        const target = event.target.closest(".room-surface-drop, .room-floor, .room-wall");
        if (!target) return;
        let dragged = this.dragged;
        try {
            const raw = event.dataTransfer.getData("text/plain");
            if (raw) dragged = JSON.parse(raw);
        } catch {
            // The active drag state remains available if the browser omits data.
        }
        if (!dragged) return;

        try {
        if (dragged.kind === "container") {
            const draggedContainer = this.containerFor(Number(dragged.id));
            const parent = draggedContainer?.parent_id
                ? this.containerFor(draggedContainer.parent_id) : null;
            let position;
            if (parent?.kind === "wall") {
                const bounds = roomElement("room-floor").getBoundingClientRect();
                const along = ["north", "south"].includes(parent.wall_side)
                    ? (event.clientX - bounds.left) / bounds.width
                    : (event.clientY - bounds.top) / bounds.height;
                position = { along: roomFraction(along) };
            } else {
                position = this.footprintCenter(event);
                // A rack let go near a wall stands flush against it (owner).
                if (draggedContainer?.kind === "rack") position = this.snapRackToWall(position);
            }
            await this.request(`${LABRAT_API.container(dragged.id)}/layout`, { position });
            await this.load();
            return;
        }

        let containerId = Number(target.dataset.dropContainerId || target.dataset.containerId);
        let position;
        if (target.classList.contains("room-wall")) {
            containerId = Number(target.dataset.containerId);
            if (!containerId) {
                this.showNotice("This wall is not available yet. Refresh the area layout.", true);
                return;
            }
            const side = target.dataset.side;
            const bounds = target.getBoundingClientRect();
            const along = ["north", "south"].includes(side)
                ? (event.clientX - bounds.left) / bounds.width
                : (event.clientY - bounds.top) / bounds.height;
            position = { along: roomFraction(along) };
        } else if (target.classList.contains("room-shelf")) {
            containerId = Number(target.dataset.containerId);
            position = this.relativePosition(event, target, ["x"]);
        } else if (target.classList.contains("room-rack")) {
            containerId = Number(target.dataset.dropContainerId);
            position = this.relativePosition(event, target, ["x", "y"]);
        } else if (target.classList.contains("room-ceiling-strip")) {
            // Left to right along the strip, like a rack top.
            containerId = this.ceiling?.id;
            position = this.relativePosition(event, target, ["x"]);
        } else {
            position = this.footprintCenter(event);
        }
        if (!containerId) {
            this.showNotice("Choose an available floor, wall, rack top, or shelf first.", true);
            return;
        }
        const device = this.devices.find((item) => String(item.id) === String(dragged.id));
        if (!device) return;
        const payload = { container_id: containerId, position };
        if (device.device_id) {
            await this.request(`${LABRAT_API.rackDevice(device.device_id)}/placement`, payload);
        } else if (device.discovery_id) {
            payload.discovery_id = device.discovery_id;
            await this.request(LABRAT_API.placements, payload);
        } else {
            this.showNotice("This equipment is not linked to inventory yet.", true);
            return;
        }
        this.dragged = null;
        await this.load();
        this.showNotice(containerId === this.ceiling?.id
            ? `Mounted ${device.name} on the ceiling.` : `Placed ${device.name} in the area.`, false);
        } catch (error) {
            this.showNotice(error.message || "Could not move this equipment.", true);
        }
    }

    /**
     * Remember where an item was grabbed and how big it is, so a drop places
     * the item where it was let go instead of centering it on the pointer.
     *
     * @param {DragEvent} event - The dragstart event.
     * @param {HTMLElement} element - The element being dragged.
     * @param {boolean} [fromList=false] - True for a sidebar row, which uses a
     *     typical footprint size and no grab offset.
     * @returns {void}
     */
    rememberGrab(event, element, fromList = false) {
        const bounds = element.getBoundingClientRect();
        this.grab = fromList
            ? { dx: 0, dy: 0, width: 110, height: 64 }
            : {
                dx: event.clientX - (bounds.left + bounds.width / 2),
                dy: event.clientY - (bounds.top + bounds.height / 2),
                width: bounds.width,
                height: bounds.height,
            };
    }

    /**
     * Turn a drop on the floor into a saved center point that keeps the whole
     * footprint inside the room, not just its center.
     *
     * @param {DragEvent} event - The drop event.
     * @returns {{x: number, y: number}} Normalized center on the floor.
     */
    footprintCenter(event) {
        const floor = roomElement("room-floor").getBoundingClientRect();
        const grab = this.grab || { dx: 0, dy: 0, width: 0, height: 0 };
        return {
            x: this.keepInside((event.clientX - grab.dx - floor.left) / floor.width, grab.width / floor.width),
            y: this.keepInside((event.clientY - grab.dy - floor.top) / floor.height, grab.height / floor.height),
        };
    }

    /**
     * Glue a dropped rack to the nearest wall when its edge lands within
     * 48 px of that wall's inner face; otherwise it stands free.
     *
     * @param {{x: number, y: number}} center - Dropped center, 0..1 of the plan.
     * @returns {{x: number, y: number, wall?: string}} Position to save.
     */
    snapRackToWall(center) {
        const layer = roomElement("room-layout-items");
        const width = layer.clientWidth;
        const height = layer.clientHeight;
        const gap = parseFloat(getComputedStyle(roomElement("room-canvas")).getPropertyValue("--floor-gap")) || 0;
        const half = { w: (this.grab?.width || 0) / 2, h: (this.grab?.height || 0) / 2 };
        const cx = center.x * width;
        const cy = center.y * height;
        const distances = {
            north: cy - half.h + gap,
            south: height + gap - (cy + half.h),
            west: cx - half.w + gap,
            east: width + gap - (cx + half.w),
        };
        const [wall, distance] = Object.entries(distances).sort((left, right) => left[1] - right[1])[0];
        return distance <= 48 ? { ...center, wall } : center;
    }

    /**
     * Clamp a normalized center so an item of the given normalized size stays
     * fully inside 0..1. An item larger than the room is centered.
     *
     * @param {number} center - Proposed center, 0..1.
     * @param {number} size - Item size as a fraction of the room.
     * @returns {number} Center that keeps the item inside the room.
     */
    keepInside(center, size) {
        const half = Math.min(0.5, Math.max(0, size) / 2);
        return Math.min(1 - half, Math.max(half, Number.isFinite(center) ? center : 0.5));
    }

    /**
     * Pull every free-standing footprint fully inside the floor after drawing.
     *
     * Saved positions are centers, so older layouts (or a narrower window)
     * can leave part of a rack outside the room. This only changes what is
     * drawn; the saved position updates the next time the item is moved.
     *
     * @returns {void}
     */
    fitFootprintsInsideFloor() {
        const layer = roomElement("room-layout-items");
        const width = layer.clientWidth;
        const height = layer.clientHeight;
        if (!width || !height) return;
        for (const element of layer.querySelectorAll(":scope > .room-rack:not([data-wall]), :scope > .room-area, :scope > .room-device")) {
            const x = parseFloat(element.style.left) / 100;
            const y = parseFloat(element.style.top) / 100;
            element.style.left = `${this.keepInside(x, element.offsetWidth / width) * 100}%`;
            element.style.top = `${this.keepInside(y, element.offsetHeight / height) * 100}%`;
        }
    }

    /**
     * Make an element a highlighted drop target for some drags.
     *
     * @param {HTMLElement} element - Element that accepts drops.
     * @param {() => boolean} accepts - Whether the current drag can drop here.
     * @param {(event: DragEvent) => Promise<void>} onDrop - Drop action.
     * @returns {void}
     */
    bindDropTarget(element, accepts, onDrop) {
        element.addEventListener("dragover", (event) => {
            if (!accepts()) return;
            event.preventDefault();
            event.dataTransfer.dropEffect = "move";
            element.classList.add("is-drop-target");
        });
        element.addEventListener("dragleave", (event) => {
            if (!element.contains(event.relatedTarget)) element.classList.remove("is-drop-target");
        });
        element.addEventListener("drop", async (event) => {
            element.classList.remove("is-drop-target");
            if (!accepts()) return;
            event.preventDefault();
            event.stopPropagation();
            await onDrop(event);
        });
    }

    /**
     * Return the device being dragged, if any.
     *
     * @returns {?object} The device record, or null.
     */
    draggedDevice() {
        if (this.dragged?.kind !== "device") return null;
        return this.devices.find((item) => String(item.id) === String(this.dragged.id)) || null;
    }

    /**
     * Whether the dragged device is currently placed in this room.
     *
     * @returns {boolean} True when it is.
     */
    draggedDeviceInRoom() {
        const device = this.draggedDevice();
        return Boolean(device && this.isInRoom(device));
    }

    /**
     * Take the dragged device out of this room. Its inventory record,
     * discovery link, and history stay; only the placement is removed.
     *
     * @returns {Promise<void>} Resolves after the layout reloads.
     */
    async unplaceDragged() {
        const device = this.draggedDevice();
        this.dragged = null;
        if (!device?.device_id) return;
        try {
            await this.request(`${LABRAT_API.rackDevice(device.device_id)}/unplace`, {});
            await this.load();
            this.showNotice(`Took ${device.name} out of the area. It is back in the list.`, false);
        } catch (error) {
            this.showNotice(error.message || "Could not take this equipment out of the area.", true);
        }
    }

    /**
     * Convert a pointer location within a target to supported normalized axes.
     *
     * @param {DragEvent} event - The drop.
     * @param {HTMLElement} target - The surface dropped on.
     * @param {string[]} axes - The axes it supports: ["x"] along a wall or
     *     shelf, ["x", "y"] on a floor.
     * @returns {{x?: number, y?: number}} The normalized position.
     */
    relativePosition(event, target, axes) {
        const bounds = target.getBoundingClientRect();
        const position = {};
        if (axes.includes("x")) position.x = roomFraction((event.clientX - bounds.left) / bounds.width);
        if (axes.includes("y")) position.y = roomFraction((event.clientY - bounds.top) / bounds.height);
        return position;
    }

    /**
     * Add Build out's open section when the drawer closes or moves on, if
     * it was filled in and is valid (owner: only that one, not every
     * section clicked through). Its own button does the same; a section
     * left untouched or incomplete adds nothing.
     *
     * @returns {void}
     */
    commitBuildout() {
        const open = document.querySelector(".room-add[open] form");
        if (!open || open.dataset.touched !== "true" || !open.checkValidity()) return;
        delete open.dataset.touched;
        open.requestSubmit();
    }

    /**
     * Create a shelf on a selected wall or room surface.
     *
     * @param {SubmitEvent} event - The shelf form's submit.
     * @returns {Promise<void>} Resolves once saved and redrawn.
     */
    async createShelf(event) {
        event.preventDefault();
        const form = event.currentTarget;
        const parentId = Number(form.elements.parent_id.value);
        const parent = this.containerFor(parentId);
        const payload = {
            kind: "shelf", room_id: this.roomId, parent_id: parentId,
            name: form.elements.name.value.trim(),
            position: parent?.kind === "wall" ? { along: 0.5 } : { x: 0.5, y: 0.5 },
        };
        try {
            await this.request(LABRAT_API.locations, payload);
            form.reset();
            delete form.dataset.touched;
            await this.load();
            this.showNotice(`Created ${payload.name}. Drag equipment onto its shelf.`, false);
        } catch (error) {
            this.showNotice(error.message, true);
        }
    }

    /**
     * Create a rack in this room through the audited locations API.
     *
     * The new rack appears as a footprint on the room canvas, which links to
     * its elevation in Rack View.
     *
     * @param {SubmitEvent} event - Submission of the "Add a rack" form.
     * @returns {Promise<void>} Resolves after the room layout reloads.
     */
    async createRack(event) {
        event.preventDefault();
        const form = event.currentTarget;
        const payload = {
            kind: "rack",
            room_id: this.roomId,
            name: form.elements.name.value.trim(),
            height_u: Number(form.elements.height_u.value),
            width_inches: Number(form.elements.width_inches.value),
            has_rear: form.elements.rack_faces.value === "both",
        };
        try {
            await this.request(LABRAT_API.locations, payload);
            form.reset();
            delete form.dataset.touched;
            await this.load();
            this.showNotice(
                `Created ${payload.name}. Select its footprint to open it in Rack View.`, false,
            );
        } catch (error) {
            this.showNotice(error.message, true);
        }
    }

    /**
     * Add manual equipment to inventory so it can be placed on the room canvas.
     *
     * @param {SubmitEvent} event - The manual form's submit.
     * @returns {Promise<void>} Resolves once saved and redrawn.
     */
    async createManualDevice(event) {
        event.preventDefault();
        const form = event.currentTarget;
        const payload = {
            name: form.elements.name.value.trim(),
            class_name: form.elements.class_name.value.trim() || null,
            height_u: 1,
        };
        try {
            await this.request(LABRAT_API.rackDevices, payload);
            form.reset();
            await this.load();
            if (this.drawer.currentView === "manual") this.drawer.back();
            this.showNotice(`Added ${payload.name} to inventory. Drag it into the area to place it.`, false);
        } catch (error) {
            this.showNotice(error.message, true);
        }
    }

    /**
     * Report a result. Errors show above the room; a success only flashes
     * "Saved" in the heading, fading after five seconds, so nothing moves
     * (owner). Screen readers hear the full message.
     *
     * @param {string} message - The message.
     * @param {boolean} [isError=false] - Show it as an error.
     */
    showNotice(message, isError = false) {
        const notice = roomElement("room-notice");
        if (!isError) {
            notice.hidden = true;
            if (!message) return;
            const saved = roomElement("room-saved");
            saved.textContent = "Saved";
            saved.setAttribute("aria-label", message);
            saved.classList.remove("is-shown");
            void saved.offsetWidth;
            saved.classList.add("is-shown");
            if (this.noticeTimer) window.clearTimeout(this.noticeTimer);
            this.noticeTimer = window.setTimeout(() => saved.classList.remove("is-shown"), 5000);
            return;
        }
        notice.textContent = message;
        notice.hidden = !message;
        notice.dataset.state = "error";
    }
}

void new RoomViewApp().initialize();
