/**
 * @fileoverview Confirm and carry out a delete of a location, room, rack, or
 * place (shelf, surface, shared rack shelf), shared by Locations and the
 * Equipment view.
 *
 * Every delete endpoint accepts ``preview: true``, which says what the delete
 * would remove and how much equipment sits inside, without changing
 * anything. The dialog shows that, then offers either a plain Delete or
 * "Delete and unplace N" when equipment is inside. Unplaced equipment stays
 * in inventory with its ports and cables, ready to place again. A blocked
 * delete (saved attributes, a site's networks) shows why and only closes.
 *
 * @copyright 2026 LabRat contributors
 * @license Apache-2.0
 * SPDX-License-Identifier: Apache-2.0
 *
 * Used by: Equipment and Diagram, Floor plan, Locations, Location detail.
 */

/**
 * Count something in words: "1 rack", "3 racks", "1 piece of equipment".
 *
 * @param {number} count - How many.
 * @param {string} one - Singular noun.
 * @param {string} [many] - Plural noun (default: one + "s").
 * @returns {string} The phrase.
 */
function removalCount(count, one, many = `${one}s`) {
    return `${count} ${count === 1 ? one : many}`;
}

/**
 * Turn a delete preview into the dialog's words. DOM-free, so the Node
 * checks cover it.
 *
 * @param {{kind: string, name: string, rooms: number, racks: number,
 *     places: number, unplaces: number, blocked: (string|null)}} preview -
 *     The endpoint's ``preview: true`` reply.
 * @returns {{title: string, lines: Array<string>, confirm: (string|null),
 *     unplace: boolean}} Title, explanation lines, the confirm button's
 *     label (null when blocked), and whether to send ``unplace``.
 */
function removalWording(preview) {
    const title = `Delete ${preview.kind} “${preview.name}”?`;
    if (preview.blocked) {
        return { title, lines: [preview.blocked], confirm: null, unplace: false };
    }
    const parts = [];
    if (preview.rooms) parts.push(removalCount(preview.rooms, "area"));
    if (preview.racks) parts.push(removalCount(preview.racks, "rack"));
    // Shelves, surfaces, and areas people made; "shelf" reads best for all.
    if (preview.places) parts.push(removalCount(preview.places, "shelf", "shelves"));
    const lines = [];
    if (parts.length) {
        const list = parts.length > 1
            ? `${parts.slice(0, -1).join(", ")} and ${parts.at(-1)}` : parts[0];
        lines.push(`This also deletes ${list}.`);
    }
    if (preview.unplaces) {
        const what = removalCount(preview.unplaces, "piece of equipment", "pieces of equipment");
        lines.push(`${what} inside will be unplaced. ${preview.unplaces === 1 ? "It stays" : "They stay"} `
            + "in inventory with ports and cables, ready to place again.");
    } else {
        lines.push("Nothing is placed inside.");
    }
    lines.push("This cannot be undone.");
    return {
        title, lines, unplace: preview.unplaces > 0,
        confirm: preview.unplaces ? `Delete and unplace ${preview.unplaces}` : "Delete",
    };
}

/**
 * Ask what a delete would do, confirm it in a modal dialog, and do it.
 *
 * @param {string} path - The delete endpoint, e.g. "/api/v1/rooms/4/delete".
 * @param {string} csrfToken - The page's CSRF token.
 * @param {HTMLElement|null} [opener] - Where focus returns on cancel.
 * @returns {Promise<object|null>} The delete's reply, or null if cancelled.
 * @throws {Error} When the preview or the delete fails.
 */
async function confirmRemoval(path, csrfToken, opener = null) {
    const wording = removalWording(await labratJson(path, { payload: { preview: true }, csrfToken }));
    const dialog = document.createElement("dialog");
    dialog.className = "remove-dialog";
    const heading = document.createElement("h2");
    heading.id = "remove-dialog-title";
    heading.textContent = wording.title;
    dialog.setAttribute("aria-labelledby", heading.id);
    dialog.append(heading);
    for (const line of wording.lines) {
        const text = document.createElement("p");
        text.textContent = line;
        dialog.append(text);
    }
    const error = document.createElement("p");
    error.className = "remove-dialog-error";
    error.setAttribute("role", "alert");
    error.hidden = true;
    const row = document.createElement("div");
    row.className = "button-row";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "button button-quiet";
    cancel.textContent = wording.confirm ? "Cancel" : "Close";
    row.append(cancel);
    let confirm = null;
    if (wording.confirm) {
        confirm = document.createElement("button");
        confirm.type = "button";
        confirm.className = "button remove-dialog-confirm";
        confirm.textContent = wording.confirm;
        row.append(confirm);
    }
    dialog.append(error, row);
    document.body.append(dialog);
    return new Promise((resolve) => {
        let result = null;
        dialog.addEventListener("close", () => {
            dialog.remove();
            if (!result) opener?.focus();
            resolve(result);
        });
        cancel.addEventListener("click", () => dialog.close());
        confirm?.addEventListener("click", async () => {
            confirm.disabled = true;
            try {
                result = await labratJson(path, { payload: wording.unplace ? { unplace: true } : {}, csrfToken });
                dialog.close();
            } catch (failure) {
                confirm.disabled = false;
                error.textContent = failure.message;
                error.hidden = false;
            }
        });
        dialog.showModal();
        // Start on Cancel: the safe choice.
        cancel.focus();
    });
}
