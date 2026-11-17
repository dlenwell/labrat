/**
 * @fileoverview Create locations and areas, and rename and delete locations,
 * from the Locations pages. Deletes go through remove-dialog.js.
 * @copyright 2026 LabRat contributors
 * @license Apache-2.0
 * SPDX-License-Identifier: Apache-2.0
 *
 * Used by: Locations, Location detail.
 *
 * A new location comes from the drawer form (device-drawer.js), which the
 * page loads first.
 */

/**
 * Return a required site-page element by its stable identifier.
 *
 * @param {string} id - The element's ID.
 * @returns {HTMLElement} The element.
 * @throws {Error} When the page does not have it.
 */
function siteElement(id) {
    const element = document.getElementById(id);
    if (!element) {
        throw new Error(`Missing site page element: ${id}`);
    }
    return element;
}

/**
 * Submit one explicit site or room creation to the authenticated inventory API.
 *
 * Each form declares what it creates with ``data-location-kind`` ("site" or
 * "room"); room forms also carry the owning ``data-site-id``, because every
 * building on the Locations page has its own "Add a room" form.
 *
 * @param {SubmitEvent} event - Form submission from a Locations page.
 * @returns {Promise<void>} Resolves after navigation to the new record.
 */
async function createSiteLocation(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const root = document.querySelector(".locations-page");
    const csrfToken = root.dataset.csrfToken;
    const payload = {
        kind: form.dataset.locationKind,
        name: form.querySelector('[name="name"]').value.trim(),
    };
    if (payload.kind === "room") {
        payload.site_id = Number(form.dataset.siteId);
    }

    try {
        const result = await labratJson(
            LABRAT_API.locations, { payload, csrfToken, failure: "Could not create this location." }
        );
        // A new site stays on Locations, scrolled to its building; a new room
        // opens so its equipment can be arranged right away.
        window.location.assign(payload.kind === "site"
            ? `/locations#site-${result.id}` : `/areas/${result.id}`);
    } catch (error) {
        const notice = siteElement("locations-notice");
        notice.textContent = error.message;
        notice.hidden = false;
    }
}

for (const form of document.querySelectorAll("form[data-location-kind]")) {
    form.addEventListener("submit", createSiteLocation);
}

/**
 * The New location drawer (owner: a drawer form, as elsewhere): the
 * heading's Add location tool and the empty page's button open it. Closing
 * it after typing a valid name creates the location, like its button;
 * an untouched or empty form adds nothing.
 *
 * @returns {void}
 */
function bindLocationDrawer() {
    const host = document.querySelector(".locations-page");
    const form = document.getElementById("location-add-form");
    if (!host || !form || typeof DeviceDrawer !== "function") return;
    const tool = document.getElementById("location-add-tool");
    const drawer = new DeviceDrawer({
        host, rootView: "closed", closeAtRoot: true,
        keepsOpen: (target) => Boolean(target.closest("#location-add-tool, [data-location-add]")),
        onClose: () => {
            tool?.setAttribute("aria-expanded", "false");
            if (form.dataset.touched === "true" && form.checkValidity()) {
                delete form.dataset.touched;
                form.requestSubmit();
            }
        },
    });
    form.addEventListener("input", () => { form.dataset.touched = "true"; });
    const open = () => {
        drawer.reset("site");
        drawer.open("site-name");
        tool?.setAttribute("aria-expanded", "true");
    };
    tool?.addEventListener("click", open);
    for (const button of document.querySelectorAll("[data-location-add]")) button.addEventListener("click", open);
}

bindLocationDrawer();

/**
 * Post one administrator change to the inventory API.
 *
 * @param {string} path - API path.
 * @param {object} payload - JSON body.
 * @returns {Promise<object>} The parsed reply.
 * @throws {Error} The server's validation message on failure.
 */
function postLocationChange(path, payload) {
    const root = document.querySelector(".locations-page");
    return labratJson(path, { payload, csrfToken: root.dataset.csrfToken, failure: "The change was not saved." });
}

/**
 * Show a Locations page error in its notice line.
 *
 * @param {string} message - The error.
 */
function showLocationNotice(message) {
    const notice = siteElement("locations-notice");
    notice.textContent = message;
    notice.hidden = false;
}

/**
 * Swap a tool pair for an inline rename field (Save, Cancel; Enter and
 * Escape work too).
 *
 * @param {HTMLButtonElement} button - The pressed Rename button.
 * @returns {void}
 */
function startLocationRename(button) {
    const tools = button.closest(".location-tools");
    const { kind, id, name, label } = button.dataset;
    const form = document.createElement("form");
    form.className = "location-rename";
    const input = document.createElement("input");
    input.type = "text";
    input.name = "name";
    input.maxLength = 120;
    input.required = true;
    input.value = name;
    input.setAttribute("aria-label", `New name for ${label} ${name}`);
    const save = document.createElement("button");
    save.type = "submit";
    save.className = "button";
    save.textContent = "Save";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "button button-quiet";
    cancel.textContent = "Cancel";
    form.append(input, save, cancel);
    const close = () => {
        form.remove();
        tools.hidden = false;
        button.focus();
    };
    cancel.addEventListener("click", close);
    form.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            event.preventDefault();
            close();
        }
    });
    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const payload = { name: input.value.trim() };
        save.disabled = true;
        try {
            await postLocationChange(LABRAT_API.place(kind, id), payload);
            window.location.reload();
        } catch (error) {
            save.disabled = false;
            showLocationNotice(error.message);
            input.focus();
        }
    });
    tools.hidden = true;
    tools.after(form);
    input.focus();
    input.select();
}

/**
 * Delete a location after the shared confirmation, which says
 * what goes with it and offers to unplace equipment inside
 * (remove-dialog.js).
 *
 * @param {HTMLButtonElement} button - The pressed Delete button.
 * @returns {Promise<void>}
 */
async function deleteLocation(button) {
    const { kind, id } = button.dataset;
    const root = document.querySelector(".locations-page");
    try {
        const result = await confirmRemoval(`${LABRAT_API.place(kind, id)}/delete`, root.dataset.csrfToken, button);
        if (!result) return;
        // A deleted location's own page no longer exists.
        if (kind === "sites" && window.location.pathname === `/locations/${id}`) {
            window.location.assign("/locations");
        } else {
            window.location.reload();
        }
    } catch (error) {
        showLocationNotice(error.message);
    }
}

for (const button of document.querySelectorAll("[data-location-action]")) {
    button.addEventListener("click", () => {
        if (button.dataset.locationAction === "rename") startLocationRename(button);
        else deleteLocation(button);
    });
}
