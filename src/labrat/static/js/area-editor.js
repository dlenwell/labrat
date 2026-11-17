/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * @file The Edit area panel (_room_header.html area_editor_panel).
 *
 * Every area view's heading has an Edit area tool (owner): it opens this
 * panel in the shared drawer with the area's name, which saves when the
 * panel closes (nothing is sent when it did not change), and Delete area,
 * a tool in the drawer bar that asks first (remove-dialog.js
 * confirmRemoval) and then opens the area's location. In the data model and
 * API an area is still a room (`/api/v1/rooms/<id>`).
 *
 * The page opens the panel (its drawer view is "area-edit") through `show`
 * and calls `commit` when the drawer closes or moves to another panel.
 *
 * Used by: Equipment and Diagram, Floor plan.
 */

"use strict";

class AreaEditor {
    /**
     * @param {object} options - Page integration.
     * @param {string} options.csrfToken - Session CSRF token for writes.
     * @param {() => void} options.show - Open the drawer on the "area-edit" panel.
     * @param {(message: string, ok?: boolean) => void} options.notify - Report.
     * @param {Document} [options.doc] - Document holding the panel.
     */
    constructor({ csrfToken, show, notify, doc = document }) {
        this.csrfToken = csrfToken;
        this.notify = notify;
        this.doc = doc;
        this.panel = this.element("area-edit-view");
        this.start = null;
        this.element("area-edit-form").addEventListener("submit", (event) => {
            // Enter saves at once, as closing would.
            event.preventDefault();
            void this.commit();
        });
        this.element("area-edit-delete").addEventListener("click", (event) => this.remove(event.currentTarget));
        const tool = this.doc.getElementById("area-edit-tool");
        tool?.addEventListener("click", () => {
            this.open();
            show();
            this.element("area-edit-name").focus();
        });
    }

    /** @param {string} id - Element ID. @returns {HTMLElement} Required element. */
    element(id) {
        const found = this.doc.getElementById(id);
        if (!found) throw new Error(`Area editor element is missing: ${id}`);
        return found;
    }

    /** @returns {number} The area's (room's) ID. */
    get areaId() { return Number(this.panel.dataset.areaId); }

    /** Fill the panel from the page title and remember it, so `commit` saves only a change. */
    open() {
        const title = this.doc.getElementById("room-title");
        const name = this.element("area-edit-name");
        if (title?.textContent) name.value = title.textContent.trim();
        this.start = name.value.trim();
    }

    /**
     * Save a changed name, then show it in the heading and the tab title.
     *
     * @returns {Promise<void>} Resolves after any save.
     */
    async commit() {
        const start = this.start;
        this.start = null;
        const name = this.element("area-edit-name").value.trim();
        if (start === null || name === start) return;
        if (!name) {
            this.notify("An area name is required, so the area was not renamed.", false);
            return;
        }
        try {
            await labratJson(LABRAT_API.room(this.areaId), { payload: { name }, csrfToken: this.csrfToken });
            const title = this.doc.getElementById("room-title");
            if (title) title.textContent = name;
            this.doc.title = this.doc.title.replace(start, name);
            this.notify(`Renamed the area to ${name}.`, true);
        } catch (error) {
            this.notify(error.message || "Could not rename the area.", false);
        }
    }

    /**
     * Delete the area after the shared confirmation, then open its location.
     *
     * @param {HTMLElement} opener - The Delete button, focused again if the
     *     dialog is cancelled.
     * @returns {Promise<void>} Resolves once deleted, or cancelled.
     */
    async remove(opener) {
        try {
            const result = await confirmRemoval(`${LABRAT_API.room(this.areaId)}/delete`, this.csrfToken, opener);
            if (!result) return;
            this.start = null;
            this.doc.defaultView.location.assign(`/locations/${this.panel.dataset.siteId}`);
        } catch (error) {
            this.notify(error.message, false);
        }
    }
}

window.AreaEditor = AreaEditor;
