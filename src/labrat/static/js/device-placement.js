/*
 * Copyright 2026 LabRat contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Used by: Discovery, Equipment and Diagram, Discovery clients, Floor plan.
 */

/**
 * @fileoverview Shared placement editor and "Add new place" panel.
 *
 * Equipment and Discovery place devices with the same drawer panels: choose a
 * room, then a place in it. A rack offers inside the rack, a shared 1U shelf,
 * or on top, with a bottom U (a free span is suggested), the equipment's
 * height, and front, back, or full depth (back and full depth only when the
 * rack has a rear). Which side the network ports face can always be set. An
 * observation that is not yet in inventory first chooses a new or an
 * existing inventory device. "Add new place" creates a rack, wall mount,
 * shelf, floor area, or surface in the chosen room and returns with it
 * selected. Unplace removes the active placement and keeps the inventory.
 *
 * Rack space is checked against canonical inventory placements (the
 * locations API's devices), so manual equipment counts on every page; the
 * server validates again on save. Panel markup comes from the macros in
 * templates/private/_device_drawer.html; styles are device-details.css.
 *
 * Needs device-details.js (deviceText, deviceCategory) and main.js (labratJson).
 */

"use strict";

/** Places offered in step 2; rack tops and shared shelves are rack options. */
const DEVICE_PLACE_KINDS = new Set(["rack", "wall", "shelf", "floor", "ceiling", "surface", "area"]);

/** Placement editor and place creator for one page's drawer. */
class DevicePlacement {
    /**
     * Bind the placement and add-place panels once.
     *
     * @param {object} options - Page integration.
     * @param {DeviceDrawer} options.drawer - The page's shared drawer.
     * @param {string} options.csrfToken - Session CSRF token for writes.
     * @param {() => object} options.locations - Latest locations API
     *     document (`sites`, `rooms`, `racks`, `containers`, `devices`).
     * @param {() => Promise<void>} options.reload - Reload the page's data
     *     (including locations) after a write.
     * @param {(message: string, ok?: boolean) => void} options.notify - Show
     *     a page notice.
     * @param {(result: object, node: object) => void} [options.onSaved] -
     *     Called with the API result after a placement is saved, before the
     *     page reloads (for example, to follow the device's new inventory ID).
     */
    constructor({ drawer, csrfToken, locations, reload, notify, onSaved = null }) {
        this.drawer = drawer;
        this.csrfToken = csrfToken;
        this.locations = locations;
        this.reload = reload;
        this.notify = notify;
        this.onSaved = onSaved;
        this.node = null;
        this.selection = { roomId: null, containerId: null };
        // Default walls stay out of step 2 until occupied or just added.
        this.activeWallIds = new Set();
        this.bindEvents();
    }

    /** @param {string} id - Element ID. @returns {HTMLElement} Required element. */
    element(id) {
        const found = document.getElementById(id);
        if (!found) throw new Error(`Device drawer element is missing: ${id}`);
        return found;
    }

    /** Attach the panels' controls. */
    bindEvents() {
        const choose = (event) => this.choose(event);
        this.element("device-placement-rooms").addEventListener("click", choose);
        this.element("device-placement-containers").addEventListener("click", choose);
        for (const radio of this.element("device-placement-form").querySelectorAll('[name="device_identity"]')) {
            radio.addEventListener("change", () => this.updateIdentity());
        }
        this.element("device-placement-mode").addEventListener("change", () => this.updateRackFields());
        this.element("device-face").addEventListener("change", () => this.suggest());
        this.element("device-height").addEventListener("change", () => this.suggest());
        this.element("device-rack-u").addEventListener("input", () => this.feedback(""));
        this.element("device-placement-form").addEventListener("submit", (event) => this.save(event));
        this.element("device-placement-cancel").addEventListener(
            "click", () => this.drawer.back("device-details-place")
        );
        this.element("device-unplace").addEventListener("click", () => this.unplace());
        this.element("device-add-place").addEventListener("click", () => this.openPlace());
        this.element("device-place-kind").addEventListener("change", () => this.updatePlaceFields());
        this.element("device-place-wall").addEventListener("change", () => this.updatePlaceFields());
        this.element("device-place-form").addEventListener("submit", (event) => this.createPlace(event));
        this.element("device-place-cancel").addEventListener("click", () => this.cancelPlace());
    }

    // ------------------------------------------------------------------
    // Rack model and free space (also used by Equipment's manual form)
    // ------------------------------------------------------------------

    /**
     * Describe the rack behind a rack container.
     *
     * @param {number|null} containerId - A `rack` container's ID.
     * @returns {object|null} `{id, containerId, topContainerId, rackShelves,
     *     height_u, has_rear, name, room_id}`, or null.
     */
    rackFor(containerId) {
        const data = this.locations();
        const container = data.containers.find((item) => item.id === containerId && item.kind === "rack");
        const rack = container && data.racks.find((item) => item.id === container.rack_id);
        if (!rack) return null;
        return {
            ...rack,
            name: rack.name || container.name,
            containerId: container.id,
            topContainerId: data.containers.find((item) => item.kind === "rack_top" && item.parent_id === container.id)?.id
                || null,
            rackShelves: data.containers.filter((item) => item.kind === "rack_shelf"
                && item.parent_id === container.id),
        };
    }

    /** @returns {object[]} Inventory devices with an active placement. */
    placedDevices() {
        return (this.locations().devices || []).filter((device) => device.placement);
    }

    /**
     * Whether a full span fits on a rack face without covering a device.
     *
     * Full-depth equipment occupies both faces. A shared shelf blocks its U
     * only while something sits on it.
     *
     * @param {object} rack - From `rackFor` (or Equipment's matching shape).
     * @param {string} face - "front", "rear", or "full_depth".
     * @param {number} height - Span height in U.
     * @param {number} unit - Bottom U.
     * @param {number|null} [movingDeviceId] - Inventory ID being moved.
     * @returns {boolean} True when the span is free.
     */
    spanIsFree(rack, face, height, unit, movingDeviceId = null) {
        if (!rack || unit < 1 || unit + height - 1 > rack.height_u) return false;
        const overlaps = (otherFace) => face === "full_depth" || otherFace === "full_depth" || otherFace === face;
        const devices = this.placedDevices();
        const direct = devices.some((device) => {
            const place = device.placement;
            if (device.id === movingDeviceId || place.container_id !== rack.containerId
                || !place.rack_u || !overlaps(place.face || "front")) {
                return false;
            }
            const otherHeight = Math.max(1, Number(device.height_u) || 1);
            return unit <= place.rack_u + otherHeight - 1 && place.rack_u <= unit + height - 1;
        });
        if (direct) return false;
        return !rack.rackShelves.some((shelf) => (
            overlaps(shelf.rack_face || "front")
            && shelf.rack_u >= unit && shelf.rack_u <= unit + height - 1
            && devices.some((device) => device.id !== movingDeviceId && device.placement.container_id === shelf.id)
        ));
    }

    /**
     * Find the lowest free span, keeping a preferred unit when it still fits.
     *
     * @param {object|null} rack - Rack model.
     * @param {string} face - Rack face.
     * @param {number} height - Span height in U.
     * @param {number|null} [movingDeviceId] - Inventory ID being moved.
     * @param {number|null} [preferredUnit] - Unit to keep if free.
     * @returns {number|null} Bottom U, or null when nothing fits.
     */
    firstAvailableUnit(rack, face, height, movingDeviceId = null, preferredUnit = null) {
        if (!rack) return null;
        if (preferredUnit && this.spanIsFree(rack, face, height, preferredUnit, movingDeviceId)) return preferredUnit;
        for (let unit = 1; unit + height - 1 <= rack.height_u; unit += 1) {
            if (this.spanIsFree(rack, face, height, unit, movingDeviceId)) return unit;
        }
        return null;
    }

    /**
     * Offer back and full depth only when the rack has a rear.
     *
     * @param {HTMLSelectElement} selector - A face select.
     * @param {object|null} rack - Rack model.
     * @returns {void}
     */
    updateFaceOptions(selector, rack) {
        for (const value of ["rear", "full_depth"]) {
            const option = selector.querySelector(`option[value="${value}"]`);
            if (!option) continue;
            option.hidden = !rack?.has_rear;
            option.disabled = !rack?.has_rear;
            if (!rack?.has_rear && selector.value === value) selector.value = "front";
        }
    }

    // ------------------------------------------------------------------
    // Placement panel
    // ------------------------------------------------------------------

    /**
     * Show or clear the panel's feedback line.
     *
     * @param {string} message - The message; empty hides it.
     */
    feedback(message) {
        const element = this.element("device-placement-feedback");
        element.hidden = !message;
        element.textContent = message;
    }

    /**
     * Fill the editor for a device and slide it in.
     *
     * The device's current place is selected. An unplaced device (or one
     * placed elsewhere when the page passes a preset) starts on the preset,
     * such as Equipment's rack in focus.
     *
     * @param {object} node - Device record (topology node or inventory-only).
     * @param {{roomId?: number, containerId?: number}} [preset] - Starting
     *     room and place when the device has no placement to show.
     * @returns {void}
     */
    open(node, preset = {}) {
        this.node = node;
        const place = node.placement;
        // Rack tops and shared shelves are shown as their rack.
        const parentRack = place && ["rack_top", "rack_shelf"].includes(place.container_kind)
            ? this.locations().containers.find((item) => item.id === place.container_id)?.parent_id
            : null;
        this.selection = place
            ? { roomId: place.container_room_id, containerId: parentRack || place.container_id }
            : { roomId: preset.roomId || null, containerId: preset.containerId || null };
        if (place?.container_kind === "wall") this.activeWallIds.add(place.container_id);
        // Identity choice: only an observation not yet in inventory has one.
        const form = this.element("device-placement-form");
        const unlinked = !node.device_id && Boolean(node.discovery_id);
        this.element("device-identity").hidden = !unlinked;
        const options = (this.locations().devices || []).map((device) => new Option(
            `${device.name} (inventory #${device.id})`, String(device.id)));
        this.element("device-existing-device").replaceChildren(
            new Option("Choose an inventory device", ""), ...options
        );
        form.querySelector('[name="device_identity"][value="new"]').checked = true;
        this.updateIdentity();
        // Rack options start from the current placement.
        this.element("device-placement-mode").value = place?.container_kind === "rack_top" ? "on_top"
            : place?.container_kind === "rack_shelf" ? "rack_shelf" : "rack";
        this.element("device-rack-u").value = place?.rack_u ? String(place.rack_u) : "";
        this.element("device-height").value = String(node.height_u || 1);
        this.element("device-face").value = place?.face || "front";
        this.element("device-port-facing").value = node.port_facing ?? place?.port_facing ?? "";
        this.element("device-unplace").hidden = !node.device_id || !place;
        this.feedback("");
        this.renderChoices();
        this.drawer.push("placement");
        const first = unlinked
            ? form.querySelector('[name="device_identity"]:checked')
            : form.querySelector('#device-placement-containers [aria-pressed="true"], #device-placement-rooms [aria-pressed="true"], #device-placement-rooms button');
        first?.focus();
    }

    /** Show the inventory selector only when linking to an existing device. */
    updateIdentity() {
        const form = this.element("device-placement-form");
        const existing = form.querySelector('[name="device_identity"]:checked')?.value === "existing";
        this.element("device-existing-picker").hidden = !existing;
        this.element("device-existing-device").required = existing && !this.element("device-identity").hidden;
    }

    /**
     * Build one choice button.
     *
     * @param {string} title - Main label.
     * @param {string} detail - Smaller second line.
     * @param {boolean} pressed - Whether it is selected.
     * @returns {HTMLButtonElement} Button.
     */
    choiceButton(title, detail, pressed) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "device-choice-button";
        button.setAttribute("aria-pressed", String(pressed));
        button.append(deviceText("strong", "", title), deviceText("small", "", detail));
        return button;
    }

    /** Render step 1 (rooms) and step 2 (places in the chosen room). */
    renderChoices() {
        const data = this.locations();
        const selection = this.selection;
        const rooms = this.element("device-placement-rooms");
        const places = this.element("device-placement-containers");
        rooms.replaceChildren();
        places.replaceChildren();
        const siteNames = new Map(data.sites.map((site) => [site.id, site.name]));
        for (const room of data.rooms) {
            const button = this.choiceButton(
                room.name, siteNames.get(room.site_id) || "Location", selection.roomId === room.id
            );
            button.dataset.roomId = String(room.id);
            rooms.append(button);
        }
        this.element("device-mount-picker").hidden = !selection.roomId;
        if (!selection.roomId) {
            selection.containerId = null;
            this.updateRackFields();
            return;
        }
        const occupied = new Set(this.placedDevices().map((device) => device.placement.container_id));
        const containers = data.containers.filter((container) => (
            container.room_id === selection.roomId && DEVICE_PLACE_KINDS.has(container.kind)
            && (container.kind !== "wall" || occupied.has(container.id) || this.activeWallIds.has(container.id))
        ));
        for (const container of containers) {
            const button = this.choiceButton(
                container.name, deviceCategory(container.kind), selection.containerId === container.id
            );
            button.dataset.containerId = String(container.id);
            places.append(button);
        }
        this.element("device-no-mounts").hidden = containers.length > 0;
        if (!containers.some((container) => container.id === selection.containerId)) selection.containerId = null;
        this.updateRackFields();
    }

    /**
     * Apply a room or place choice.
     *
     * @param {MouseEvent} event - A click inside the room or place list.
     */
    choose(event) {
        const button = event.target.closest("button[data-room-id], button[data-container-id]");
        if (!button) return;
        if (button.dataset.roomId) {
            const roomId = Number(button.dataset.roomId);
            if (this.selection.roomId !== roomId) this.selection = { roomId, containerId: null };
        } else {
            this.selection.containerId = Number(button.dataset.containerId);
        }
        this.feedback("");
        this.renderChoices();
    }

    /** @returns {object|null} The rack model for the chosen place, if it is a rack. */
    selectedRack() {
        return this.rackFor(this.selection.containerId);
    }

    /** Show rack options for a rack, and U/height/face for the chosen mode. */
    updateRackFields() {
        const rack = this.selectedRack();
        const modeSelect = this.element("device-placement-mode");
        this.element("device-rack-fields").hidden = !rack;
        modeSelect.querySelector('option[value="on_top"]').disabled = !rack?.topContainerId;
        if (modeSelect.value === "on_top" && !rack?.topContainerId) modeSelect.value = "rack";
        const mode = rack ? modeSelect.value : null;
        const inRack = mode === "rack" || mode === "rack_shelf";
        this.element("device-unit-fields").hidden = !inRack;
        this.element("device-rack-u").required = inRack;
        this.element("device-height-field").hidden = mode !== "rack";
        this.element("device-height").required = mode === "rack";
        this.element("device-position-label").textContent = mode === "rack_shelf"
            ? "Shared shelf U"
            : "Bottom rack unit";
        this.element("device-shared-shelf-help").hidden = mode !== "rack_shelf";
        this.element("device-placement-save").disabled = !this.selection.containerId;
        if (inRack) this.suggest();
    }

    /**
     * Suggest a free U for the chosen mode and face, keeping a valid entry.
     * A shared shelf prefers the device's own or another in-use shelf on that
     * face.
     */
    suggest() {
        const rack = this.selectedRack();
        const mode = this.element("device-placement-mode").value;
        if (!rack || mode === "on_top") return;
        const faceSelect = this.element("device-face");
        this.updateFaceOptions(faceSelect, rack);
        const face = faceSelect.value;
        const moving = this.node?.device_id || null;
        const input = this.element("device-rack-u");
        let unit;
        if (mode === "rack_shelf") {
            const devices = this.placedDevices();
            const shelf = rack.rackShelves.find((item) => item.id === this.node?.placement?.container_id
                && item.rack_face === face)
                || rack.rackShelves.find((item) => item.rack_face === face
                    && devices.some((device) => device.placement.container_id === item.id));
            unit = shelf?.rack_u || this.firstAvailableUnit(rack, face, 1, moving, Number(input.value) || null);
        } else {
            const height = Number(this.element("device-height").value) || 1;
            unit = this.firstAvailableUnit(rack, face, height, moving, Number(input.value) || null);
        }
        input.value = unit === null ? "" : String(unit);
        input.max = String(rack.height_u);
        this.feedback(unit === null ? "No free span fits on this rack face. Try the other face." : "");
    }

    /**
     * Build the API payload for the chosen place and options.
     *
     * @returns {object|null} Payload, or null when nothing is chosen.
     */
    payload() {
        const containerId = this.selection.containerId;
        if (!containerId) return null;
        const payload = { port_facing: this.element("device-port-facing").value || null };
        const rack = this.selectedRack();
        if (!rack) return { ...payload, container_id: containerId };
        const mode = this.element("device-placement-mode").value;
        if (mode === "on_top") {
            return { ...payload, container_id: rack.topContainerId, position: { x: 0.5, y: 0.5 } };
        }
        payload.container_id = rack.containerId;
        payload.rack_u = Number(this.element("device-rack-u").value);
        payload.face = this.element("device-face").value;
        payload.placement_mode = mode;
        if (mode === "rack_shelf") {
            // Stay put on the same shelf; otherwise join at the right end.
            payload.position = this.node?.placement?.container_kind === "rack_shelf"
                ? this.node.placement.position || { x: 0.5 } : { x: 1 };
        } else {
            payload.height_u = Number(this.element("device-height").value);
        }
        return payload;
    }

    /**
     * Save the placement through the audited API, then return to details.
     *
     * @param {SubmitEvent} event - The placement form's submit.
     * @returns {Promise<void>} Resolves once saved.
     */
    async save(event) {
        event.preventDefault();
        const node = this.node;
        const payload = this.payload();
        if (!node || !payload) {
            this.feedback("Choose an area and a place in it before saving.");
            return;
        }
        try {
            let result;
            if (node.discovery_id) {
                payload.discovery_id = node.discovery_id;
                const form = this.element("device-placement-form");
                const existing = !this.element("device-identity").hidden
                    && form.querySelector('[name="device_identity"]:checked')?.value === "existing";
                if (existing) payload.existing_device_id = Number(this.element("device-existing-device").value);
                result = await labratJson(LABRAT_API.placements, { payload, csrfToken: this.csrfToken });
            } else {
                result = await labratJson(
                    `${LABRAT_API.rackDevice(node.device_id)}/placement`, { payload, csrfToken: this.csrfToken }
                );
            }
            this.onSaved?.(result, node);
            await this.reload();
            this.drawer.back("device-details-place");
            this.notify(`Placement saved for ${node.name}.`, true);
        } catch (error) {
            this.feedback(error.message);
        }
    }

    /** Remove the active placement; inventory and discovery are kept. */
    async unplace() {
        const node = this.node;
        if (!node?.device_id || !node.placement) return;
        try {
            await labratJson(
                `${LABRAT_API.rackDevice(node.device_id)}/unplace`, { payload: {}, csrfToken: this.csrfToken }
            );
            await this.reload();
            this.drawer.back("device-details-place");
            this.notify(`${node.name} unplaced. Its inventory record and discovery are unchanged.`, true);
        } catch (error) {
            this.feedback(error.message);
        }
    }

    // ------------------------------------------------------------------
    // Add new place panel
    // ------------------------------------------------------------------

    /** Open the place creator for the room chosen in step 1. */
    openPlace() {
        const data = this.locations();
        const room = data.rooms.find((item) => item.id === this.selection.roomId);
        if (!room) return;
        this.element("device-place-form").reset();
        this.element("device-place-room").textContent = `In ${room.name}`;
        const parents = data.containers.filter((item) => item.room_id === room.id
            && ["wall", "floor", "surface", "rack_top"].includes(item.kind));
        this.element("device-place-parent").replaceChildren(new Option("No parent surface", ""),
            ...parents.map((item) => new Option(`${item.name} (${deviceCategory(item.kind)})`, String(item.id))));
        this.placeFeedback("");
        this.updatePlaceFields();
        this.drawer.push("place");
        this.element("device-place-kind").focus();
    }

    /**
     * Show or clear the place creator's feedback line.
     *
     * @param {string} message - The message; empty hides it.
     */
    placeFeedback(message) {
        const element = this.element("device-place-feedback");
        element.hidden = !message;
        element.textContent = message;
    }

    /** Show only the fields the chosen place type needs. */
    updatePlaceFields() {
        const kind = this.element("device-place-kind").value;
        this.element("device-place-rack-fields").hidden = kind !== "rack";
        this.element("device-place-wall-field").hidden = kind !== "wall";
        this.element("device-place-name-field").hidden = kind === "wall";
        this.element("device-place-name").required = kind !== "wall";
        this.element("device-place-parent-field").hidden = !["shelf", "floor", "surface"].includes(kind);
    }

    /** Discard the new place and return to placement. */
    cancelPlace() {
        this.element("device-place-form").reset();
        this.placeFeedback("");
        this.drawer.back("device-add-place");
    }

    /**
     * Create the place through the audited locations API, select it, and
     * return to placement. A default wall that already exists is selected
     * instead of duplicated.
     *
     * @param {SubmitEvent} event - The place form's submit.
     * @returns {Promise<void>} Resolves once created and selected.
     */
    async createPlace(event) {
        event.preventDefault();
        const roomId = this.selection.roomId;
        const kind = this.element("device-place-kind").value;
        const data = this.locations();
        if (kind === "wall") {
            const side = this.element("device-place-wall").value;
            const wall = data.containers.find((item) => item.room_id === roomId
                && item.kind === "wall"
                && item.wall_side === side);
            if (wall) {
                this.activeWallIds.add(wall.id);
                this.selection.containerId = wall.id;
                this.renderChoices();
                this.drawer.back("device-add-place");
                this.notify(`Selected ${wall.name} for placement.`, true);
                return;
            }
        }
        const payload = { kind, room_id: roomId, name: this.element("device-place-name").value };
        if (kind === "rack") {
            payload.height_u = Number(this.element("device-place-height").value);
            payload.width_inches = Number(this.element("device-place-width").value);
            payload.has_rear = this.element("device-place-faces").value === "both";
        }
        if (kind === "wall") {
            payload.wall_side = this.element("device-place-wall").value;
            payload.name = `${payload.wall_side} wall`;
        }
        const parent = this.element("device-place-parent").value;
        if (parent && ["shelf", "floor", "surface"].includes(kind)) payload.parent_id = Number(parent);
        try {
            const created = await labratJson(LABRAT_API.locations, { payload, csrfToken: this.csrfToken });
            await this.reload();
            // A rack's result is the rack's ID; placement uses its container.
            const containers = this.locations().containers;
            const id = kind === "rack"
                ? containers.find((item) => item.kind === "rack" && item.rack_id === created.id)?.id
                : created.id;
            if (kind === "wall" && id) this.activeWallIds.add(id);
            this.selection.containerId = id || null;
            this.renderChoices();
            this.drawer.back("device-add-place");
            this.notify(`Created ${deviceCategory(kind)}: ${payload.name}.`, true);
        } catch (error) {
            this.placeFeedback(error.message);
        }
    }
}
