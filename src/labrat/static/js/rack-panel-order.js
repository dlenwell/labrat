/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * @file Reorder the Equipment view's side panels by dragging their grips.
 *
 * The side column's movable panels (Internet uplinks, Power, Around the
 * room: each marked `data-panel-order` in racks.html) always carry a grip
 * in their heading, the six-dot grip Observations uses: drag it, or focus
 * it and press the arrow keys. Separate from rack arrange mode (owner). The
 * order is a per-browser preference: saved in localStorage after each move,
 * never sent to the server, and restored on load. The patch panel is not
 * movable and stays first.
 *
 * Used by: Equipment and Diagram.
 */

/** localStorage key holding the panel IDs in order. */
const RACK_PANEL_ORDER_KEY = "labrat.equipment.panel-order";

/** Grip icon dots (as Observations' .noc-grip): two columns of three. */
const RACK_PANEL_GRIP_DOTS = [[2, 2], [8, 2], [2, 8], [8, 8], [2, 14], [8, 14]];

/** A press on a grip becomes a drag after the pointer moves this far. */
const RACK_PANEL_DRAG_THRESHOLD = 4;

class RackPanelOrder {
    /**
     * @param {HTMLElement|null} column - The side column holding the panels.
     * @param {{onChange?: function(): void}} [options] - ``onChange`` runs
     *     after every move, so the page can update its status and cables.
     */
    constructor(column, { onChange = () => {} } = {}) {
        this.column = column;
        this.onChange = onChange;
        this.dragging = null;
    }

    /** @returns {HTMLElement[]} Movable panels, in page order. */
    panels() {
        return this.column ? [...this.column.querySelectorAll(":scope > [data-panel-order]")] : [];
    }

    /** @returns {HTMLElement[]} Movable panels now shown. */
    visible() {
        return this.panels().filter((panel) => !panel.hidden);
    }

    /** @returns {string[]} Panel IDs in page order. */
    ids() {
        return this.panels().map((panel) => panel.id);
    }

    /** @returns {string[]} The order saved in this browser, or none. */
    static saved() {
        try {
            const value = JSON.parse(localStorage.getItem(RACK_PANEL_ORDER_KEY));
            return Array.isArray(value) ? value.filter((id) => typeof id === "string") : [];
        } catch {
            return [];
        }
    }

    /** Put the panels in this browser's saved order. */
    restore() {
        this.apply(RackPanelOrder.saved());
    }

    /**
     * Order the panels by ``ids``; panels it does not name keep their
     * relative order after those it does.
     *
     * @param {string[]} ids - Panel IDs, first to last.
     * @returns {void}
     */
    apply(ids) {
        const panels = this.panels();
        const rank = (panel) => {
            const index = ids.indexOf(panel.id);
            return index === -1 ? ids.length + panels.indexOf(panel) : index;
        };
        // Appending moves each panel to the end, after the fixed patch panel.
        this.column?.append(...[...panels].sort((left, right) => rank(left) - rank(right)));
    }

    /** Restore this browser's order and give every movable panel a grip. */
    init() {
        this.restore();
        for (const panel of this.panels()) panel.querySelector("h2")?.append(this.grip(panel));
    }

    /** Save the current order in this browser, then tell the page. */
    moved() {
        try {
            localStorage.setItem(RACK_PANEL_ORDER_KEY, JSON.stringify(this.ids()));
        } catch { /* Storage blocked: the order lasts until the page reloads. */ }
        this.onChange();
    }

    /**
     * Build one panel's grip: drag it, or press the arrow keys on it.
     *
     * @param {HTMLElement} panel - The panel it moves.
     * @returns {HTMLButtonElement} The grip button.
     */
    grip(panel) {
        const title = panel.querySelector("h2")?.textContent.trim() || "panel";
        const grip = document.createElement("button");
        grip.type = "button";
        grip.className = "rack-panel-grip";
        grip.title = "Drag to move this panel (or focus and use the arrow keys)";
        grip.setAttribute("aria-label", `Move the ${title} panel`);
        const namespace = "http://www.w3.org/2000/svg";
        const icon = document.createElementNS(namespace, "svg");
        icon.setAttribute("viewBox", "0 0 10 16");
        icon.setAttribute("aria-hidden", "true");
        icon.setAttribute("focusable", "false");
        for (const [cx, cy] of RACK_PANEL_GRIP_DOTS) {
            const dot = document.createElementNS(namespace, "circle");
            dot.setAttribute("cx", String(cx));
            dot.setAttribute("cy", String(cy));
            dot.setAttribute("r", "1.4");
            icon.append(dot);
        }
        grip.append(icon);

        grip.addEventListener("keydown", (event) => {
            const step = { ArrowUp: -1, ArrowLeft: -1, ArrowDown: 1, ArrowRight: 1 }[event.key];
            if (!step) return;
            event.preventDefault();
            const shown = this.visible();
            const neighbor = shown[shown.indexOf(panel) + step];
            if (!neighbor) return;
            if (step < 0) neighbor.before(panel);
            else neighbor.after(panel);
            grip.focus({ preventScroll: true });
            panel.scrollIntoView({ block: "nearest" });
            this.moved();
        });
        // The drag listens on the window, not the grip: moving the panel in
        // the page would drop the grip's pointer capture and lose the release.
        grip.addEventListener("pointerdown", (event) => {
            if (event.button !== 0 || this.dragging) return;
            event.preventDefault();
            const drag = {
                panel, pointer: event.pointerId, x: event.clientX, y: event.clientY, active: false, moved: false
            };
            const move = (moveEvent) => {
                if (moveEvent.pointerId !== drag.pointer) return;
                if (!drag.active) {
                    if (Math.hypot(
                        moveEvent.clientX - drag.x, moveEvent.clientY - drag.y
                    ) < RACK_PANEL_DRAG_THRESHOLD) return;
                    drag.active = true;
                    panel.classList.add("is-panel-dragging");
                }
                if (this.dragTo(panel, moveEvent.clientX, moveEvent.clientY)) drag.moved = true;
            };
            const drop = (upEvent) => {
                if (upEvent.pointerId !== drag.pointer) return;
                window.removeEventListener("pointermove", move);
                window.removeEventListener("pointerup", drop);
                window.removeEventListener("pointercancel", drop);
                this.dragging = null;
                panel.classList.remove("is-panel-dragging");
                if (drag.moved) this.moved();
            };
            this.dragging = drag;
            window.addEventListener("pointermove", move);
            window.addEventListener("pointerup", drop);
            window.addEventListener("pointercancel", drop);
        });
        return grip;
    }

    /**
     * Move a dragged panel past the panel under the pointer once the pointer
     * crosses that panel's middle (across a row when they share one, else
     * down the column), so a tall neighbor does not make it flicker back.
     *
     * @param {HTMLElement} panel - The panel being dragged.
     * @param {number} x - Pointer x in viewport pixels.
     * @param {number} y - Pointer y in viewport pixels.
     * @returns {boolean} Whether the panel moved.
     */
    dragTo(panel, x, y) {
        const target = document.elementFromPoint(x, y)?.closest("[data-panel-order]");
        if (!target || target === panel || target.parentElement !== this.column || target.hidden) return false;
        const box = target.getBoundingClientRect();
        const sameRow = Math.abs(box.top - panel.getBoundingClientRect().top) < 4;
        const after = Boolean(panel.compareDocumentPosition(target) & Node.DOCUMENT_POSITION_FOLLOWING);
        const middle = sameRow ? box.left + box.width / 2 : box.top + box.height / 2;
        const at = sameRow ? x : y;
        if (after ? at <= middle : at >= middle) return false;
        if (after) target.after(panel);
        else target.before(panel);
        this.onChange();
        return true;
    }
}

window.RackPanelOrder = RackPanelOrder;
