/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * @file The shared rack editor (templates/private/_rack_editor.html).
 *
 * One editor for every page that edits racks (owner): name, height, rail
 * width, and faces; a Rack menu when the room has several; the rack's shared
 * shelves, renamed in place or deleted. Nothing has a Save button: `commit`
 * saves whatever changed when the panel closes or another panel opens, and
 * sends nothing when nothing changed. Delete rack is a tool in the drawer
 * bar and asks first (remove-dialog.js confirmRemoval).
 *
 * The page shows the panel (its drawer view is "rack-edit") and calls
 * `commit` when the drawer closes or moves to another panel.
 *
 * Used by: Equipment and Diagram, Floor plan.
 */

"use strict";

class RackEditor {
    /**
     * @param {object} options - Page integration.
     * @param {string} options.csrfToken - Session CSRF token for writes.
     * @param {() => object[]} options.racks - The room's racks: `{id, name,
     *     height_u, width_inches, has_rear, rackShelves: [{id, name, rack_u,
     *     rack_face}], location?}`.
     * @param {(shelfId: number) => number} options.shelfCount - Equipment on
     *     a shared shelf.
     * @param {() => Promise<void>} options.reload - Reload the page's data.
     * @param {(message: string, ok?: boolean) => void} options.notify - Report.
     * @param {() => void} options.close - Close the drawer (after a delete).
     * @param {Document} [options.doc] - Document holding the panel.
     */
    constructor({ csrfToken, racks, shelfCount, reload, notify, close, doc = document }) {
        this.csrfToken = csrfToken;
        this.racks = racks;
        this.shelfCount = shelfCount;
        this.reload = reload;
        this.notify = notify;
        this.close = close;
        this.doc = doc;
        this.rackId = null;
        this.start = null;
        this.element("rack-edit-form").addEventListener("submit", (event) => {
            // Enter closes the panel, which saves it.
            event.preventDefault();
            this.close();
        });
        this.element("rack-edit-pick").addEventListener("change", async (event) => {
            const next = Number(event.currentTarget.value);
            await this.commit();
            this.open(next);
        });
        this.element("rack-edit-delete").addEventListener("click", (event) => this.remove(event.currentTarget));
    }

    /** @param {string} id - Element ID. @returns {HTMLElement} Required element. */
    element(id) {
        const found = this.doc.getElementById(id);
        if (!found) throw new Error(`Rack editor element is missing: ${id}`);
        return found;
    }

    /** @returns {object|null} The rack being edited. */
    current() {
        return this.racks().find((rack) => rack.id === this.rackId) || null;
    }

    /**
     * Fill the panel for a rack and remember its values, so `commit` saves
     * only what changes. The page then shows the "rack-edit" panel.
     *
     * @param {number} rackId - Rack to edit.
     * @returns {boolean} Whether the rack was found.
     */
    open(rackId) {
        this.rackId = rackId;
        const rack = this.current();
        if (!rack) {
            this.rackId = null;
            return false;
        }
        const racks = this.racks();
        const pick = this.element("rack-edit-pick");
        this.element("rack-edit-pick-field").hidden = racks.length < 2;
        pick.replaceChildren(...racks.map((each) => {
            const option = this.doc.createElement("option");
            option.value = String(each.id);
            option.textContent = each.name;
            return option;
        }));
        pick.value = String(rack.id);
        this.element("rack-edit-location").textContent = rack.location || "";
        this.element("rack-edit-location").hidden = !rack.location;
        this.element("rack-edit-name").value = rack.name;
        this.element("rack-edit-height").value = String(rack.height_u);
        this.element("rack-edit-width").value = String(rack.width_inches || 19);
        this.element("rack-edit-faces").value = rack.has_rear ? "both" : "front";
        this.feedback("");
        this.renderShelves(rack);
        this.start = this.draft();
        return true;
    }

    /** Redraw the shelf list after a reload, keeping unsaved rack fields. */
    refresh() {
        const rack = this.current();
        if (rack) {
            const names = this.draft().shelves;
            this.renderShelves(rack);
            for (const input of this.element("rack-edit-shelves").querySelectorAll("input[data-shelf-id]")) {
                if (names[input.dataset.shelfId] !== undefined) input.value = names[input.dataset.shelfId];
            }
        }
    }

    /**
     * List the rack's shared shelves, top unit first: a name field (saved
     * with the rack) and a Delete tool each.
     *
     * @param {object} rack - Rack with `rackShelves`.
     * @returns {void}
     */
    renderShelves(rack) {
        const list = this.element("rack-edit-shelves");
        list.replaceChildren();
        const shelves = [...(rack.rackShelves || [])].sort((left, right) => right.rack_u - left.rack_u);
        this.element("rack-edit-shelves-panel").hidden = shelves.length === 0;
        for (const shelf of shelves) {
            const row = this.doc.createElement("li");
            row.className = "rack-editor-shelf";
            const input = this.doc.createElement("input");
            input.type = "text";
            input.maxLength = 120;
            input.value = shelf.name;
            input.dataset.shelfId = String(shelf.id);
            input.setAttribute("aria-label", `Name of the shared shelf at U${shelf.rack_u}`);
            const count = this.shelfCount(shelf.id);
            const meta = this.doc.createElement("span");
            meta.className = "rack-editor-shelf-meta";
            meta.textContent = `U${shelf.rack_u} · ${(shelf.rack_face || "front").replace("_", " ")} · `
                + `${count} ${count === 1 ? "device" : "devices"}`;
            const remove = this.doc.createElement("button");
            remove.type = "button";
            remove.className = "button button-quiet room-icon-button device-danger-tool";
            const label = `Delete ${shelf.name}: asks first, can unplace what is on it`;
            remove.setAttribute("aria-label", label);
            remove.title = label;
            remove.dataset.tooltip = "Delete shelf";
            const icon = this.doc.createElementNS("http://www.w3.org/2000/svg", "svg");
            icon.setAttribute("viewBox", "0 0 24 24");
            icon.setAttribute("aria-hidden", "true");
            const path = this.doc.createElementNS(icon.namespaceURI, "path");
            path.setAttribute("d", "M4 7h16M10 11v6M14 11v6M5 7l1 13h12l1-13M9 7V4h6v3");
            icon.append(path);
            remove.append(icon);
            remove.addEventListener("click", () => this.removeShelf(shelf, remove));
            const text = this.doc.createElement("span");
            text.className = "rack-editor-shelf-text";
            text.append(input, meta);
            row.append(text, remove);
            list.append(row);
        }
    }

    /** @returns {object} The panel's current values. */
    draft() {
        const shelves = {};
        for (const input of this.element("rack-edit-shelves").querySelectorAll("input[data-shelf-id]")) {
            shelves[input.dataset.shelfId] = input.value.trim();
        }
        return {
            name: this.element("rack-edit-name").value.trim(),
            height_u: Number(this.element("rack-edit-height").value),
            width_inches: Number(this.element("rack-edit-width").value),
            has_rear: this.element("rack-edit-faces").value === "both",
            shelves,
        };
    }

    /**
     * Show a validation message in the panel (empty hides it).
     *
     * @param {string} message - The message; empty hides it.
     */
    feedback(message) {
        const element = this.element("rack-edit-feedback");
        element.hidden = !message;
        element.textContent = message;
    }

    /**
     * Save what changed since the panel opened, then forget the rack. A
     * failure is reported through `notify`, since the panel may be gone.
     *
     * @returns {Promise<void>} Resolves after any save and reload.
     */
    async commit() {
        const rack = this.current();
        const start = this.start;
        this.start = null;
        this.rackId = null;
        if (!rack || !start) return;
        const draft = this.draft();
        const rackChanged = ["name", "height_u", "width_inches", "has_rear"].some((key) => draft[key] !== start[key]);
        const shelfChanges = Object.entries(draft.shelves)
            .filter(([id, name]) => name && name !== start.shelves[id]);
        if (!rackChanged && !shelfChanges.length) return;
        try {
            if (rackChanged) {
                if (!draft.name) throw new Error("A rack name is required, so the rack was not saved.");
                await labratJson(LABRAT_API.rack(rack.id), { payload: {
                    name: draft.name, height_u: draft.height_u,
                    width_inches: draft.width_inches, has_rear: draft.has_rear,
                }, csrfToken: this.csrfToken });
            }
            for (const [id, name] of shelfChanges) {
                await labratJson(LABRAT_API.container(id), { payload: { name }, csrfToken: this.csrfToken });
            }
            await this.reload();
            this.notify(`Saved ${draft.name || rack.name}.`, true);
        } catch (error) {
            await this.reload();
            this.notify(error.message || "Could not save the rack.", false);
        }
    }

    /**
     * Delete the rack after the shared confirmation; nothing then saves.
     *
     * @param {HTMLElement} opener - The Delete button, focused again if the
     *     dialog is cancelled.
     * @returns {Promise<void>} Resolves once deleted, or cancelled.
     */
    async remove(opener) {
        const rack = this.current();
        if (!rack) return;
        try {
            const result = await confirmRemoval(`${LABRAT_API.rack(rack.id)}/delete`, this.csrfToken, opener);
            if (!result) return;
            this.start = null;
            this.rackId = null;
            this.close();
            await this.reload();
            this.notify(result.unplaces
                ? `Deleted ${rack.name} and unplaced ${result.unplaces} `
                    + `${result.unplaces === 1 ? "piece" : "pieces"} of equipment.`
                : `Deleted ${rack.name}.`, true);
        } catch (error) {
            this.notify(error.message, false);
        }
    }

    /**
     * Delete one shared shelf after the shared confirmation.
     *
     * @param {object} shelf - The shared shelf.
     * @param {HTMLElement} opener - Its Delete button.
     * @returns {Promise<void>} Resolves once deleted, or cancelled.
     */
    async removeShelf(shelf, opener) {
        try {
            const result = await confirmRemoval(`${LABRAT_API.container(shelf.id)}/delete`, this.csrfToken, opener);
            if (!result) return;
            await this.reload();
            this.refresh();
            this.notify(`Deleted ${shelf.name}.`, true);
        } catch (error) {
            this.notify(error.message, false);
        }
    }
}

window.RackEditor = RackEditor;
