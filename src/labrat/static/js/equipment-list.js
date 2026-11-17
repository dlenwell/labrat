/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * @file The shared "Add equipment" list (templates/private/_equipment_list.html).
 *
 * One list on every room view that adds equipment (owner): everything known
 * that is not in this room, found by a search and filter toggles like
 * Discovery clients' (Infrastructure, Clients, Unplaced only). VMs and
 * containers are never listed (owner): they run on their host and cannot be
 * placed (the API refuses them too). Each row is
 * the device's shared type icon (labratDeviceIcon in device-details.js),
 * its name, and where it is now; a click opens its details. A page can make
 * rows draggable (the Floor plan drags them onto the plan) through
 * `decorate`.
 *
 * Used by: Equipment and Diagram, Floor plan.
 */

"use strict";

/**
 * Whether a device is a VM or container guest: a client observation the
 * collector matched to a guest NIC (`virtual_endpoint`, as the API's
 * _is_virtual_guest reads it) or a Proxmox guest record (`virtualization`).
 *
 * @param {object} item - Device record with `observations`.
 * @returns {boolean} True for a guest, which cannot be placed.
 */
function equipmentIsVirtualGuest(item) {
    return (item.observations || []).some((observation) => (
        observation.client?.virtual_endpoint === true || Boolean(observation.client?.virtualization)
    ));
}

class EquipmentList {
    /**
     * @param {object} options - Page integration.
     * @param {() => object[]} options.items - Every known device record
     *     (`{id, name, category, client_only?, placement?, discovery_id?}`).
     * @param {(item: object) => boolean} options.inRoom - Whether it is
     *     already in this room (those are not listed).
     * @param {(item: object) => string} options.where - Where it is now.
     * @param {(item: object) => void} options.onSelect - Open its details.
     * @param {() => void} options.onManual - Open the page's own form for
     *     equipment discovery cannot see.
     * @param {(row: HTMLElement, item: object) => void} [options.decorate] -
     *     Add page behavior to a row (drag on the Floor plan).
     * @param {(item: object) => boolean} [options.isSelected] - Mark the
     *     row whose details are open.
     * @param {Document} [options.doc] - Document holding the panel.
     */
    constructor({
        items, inRoom, where, onSelect, onManual, decorate = null, isSelected = () => false, doc = document
    }) {
        Object.assign(this, { items, inRoom, where, onSelect, decorate, isSelected, doc });
        this.element("equipment-search").addEventListener("input", () => this.render());
        for (const toggle of this.doc.querySelectorAll("[data-equipment-filter]")) {
            toggle.addEventListener("change", () => this.render());
        }
        this.element("equipment-manual-open").addEventListener("click", () => onManual());
    }

    /** @param {string} id - Element ID. @returns {HTMLElement} Required element. */
    element(id) {
        const found = this.doc.getElementById(id);
        if (!found) throw new Error(`Equipment list element is missing: ${id}`);
        return found;
    }

    /** @returns {{infrastructure: boolean, clients: boolean, unplaced: boolean}} Toggles. */
    filters() {
        const on = (key) => Boolean(this.doc.querySelector(`[data-equipment-filter="${key}"]`)?.checked);
        return { infrastructure: on("infrastructure"), clients: on("clients"), unplaced: on("unplaced") };
    }

    /**
     * The devices the list shows: not in this room, matching the search and
     * toggles, by name.
     *
     * @returns {object[]} Shown records.
     */
    shown() {
        const query = this.element("equipment-search").value.trim().toLowerCase();
        const filters = this.filters();
        return this.items().filter((item) => {
            if (this.inRoom(item) || equipmentIsVirtualGuest(item)) return false;
            if (item.client_only ? !filters.clients : !filters.infrastructure) return false;
            if (filters.unplaced && item.placement) return false;
            return !query || `${item.name} ${item.category || ""}`.toLowerCase().includes(query);
        }).sort((left, right) => String(left.name).localeCompare(String(right.name)));
    }

    /** Redraw the rows and the count. */
    render() {
        const list = this.element("equipment-list");
        const shown = this.shown();
        list.replaceChildren();
        for (const item of shown) {
            const row = this.doc.createElement("button");
            row.type = "button";
            row.className = "equipment-list-item";
            row.setAttribute("role", "listitem");
            row.setAttribute("aria-pressed", String(this.isSelected(item)));
            if (typeof labratDeviceIcon === "function") row.append(labratDeviceIcon(
                item.category, "equipment-list-icon"
            ));
            const text = this.doc.createElement("span");
            text.className = "equipment-list-text";
            const name = this.doc.createElement("span");
            name.className = "equipment-list-name";
            name.textContent = item.name;
            const subtitle = this.doc.createElement("span");
            subtitle.className = "equipment-list-where";
            subtitle.textContent = `${String(item.category || "equipment").replaceAll("_", " ")} · ${this.where(item)}`;
            text.append(name, subtitle);
            row.append(text);
            row.title = `${item.name} · ${this.where(item)}`;
            row.addEventListener("click", () => this.onSelect(item));
            this.decorate?.(row, item);
            list.append(row);
        }
        if (!shown.length) {
            const empty = this.doc.createElement("p");
            empty.className = "equipment-list-empty";
            empty.textContent = this.element("equipment-search").value.trim()
                ? "No matching equipment." : "Nothing else to add with these filters.";
            list.append(empty);
        }
    }
}

window.EquipmentList = EquipmentList;
