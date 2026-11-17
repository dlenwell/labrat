/* Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0 */
/**
 * @fileoverview Equipment's arrange mode: drag rack devices to new units and rack-top
 * items to new places. The draft and its rules are RackArrangement
 * (rack-arrange.js); nothing is saved until Save arrangement posts every
 * move to /api/v1/rack-arrangement.
 *
 * Part of the Equipment page's RackViewApp (racks.js): these methods join
 * its prototype through rackAddMethods, so `this` is the page's app. Load
 * after racks.js.
 */

/** ArrangeMode methods of RackViewApp; never constructed itself. */
class RackArrangeMode {
    /**
     * Describe this room's racks and what arrange mode can move: inventory
     * mounted directly in a rack (up, down, and between racks) and equipment
     * on top of a rack (left to right on its own rack top). Shared-shelf
     * equipment stays put; an occupied shared shelf blocks its unit.
     *
     * @returns {{racks: Array<object>, devices: Array<object>, tops: Array<object>}}
     *     Starting layout.
     */
    arrangeLayout() {
        const containers = new Set(this.racks.map((rack) => rack.containerId).filter(Boolean));
        const occupied = new Set(this.nodes.map((node) => node.placement?.container_id).filter(Boolean));
        const racks = this.racks.filter((rack) => rack.containerId).map((rack) => ({
            containerId: rack.containerId,
            name: rack.name,
            height_u: rack.height_u,
            has_rear: Boolean(rack.has_rear),
            shelves: rack.rackShelves.map((shelf) => ({
                rack_u: shelf.rack_u, rack_face: shelf.rack_face, occupied: occupied.has(shelf.id),
            })),
        }));
        const devices = this.nodes.filter((node) => (
            node.device_id && node.placement?.container_kind === "rack"
            && containers.has(node.placement.container_id) && node.placement.rack_u
        )).map((node) => ({
            id: node.device_id,
            name: node.name,
            height: Math.max(1, Number(node.height_u) || 1),
            face: node.placement.face || "front",
            containerId: node.placement.container_id,
            rack_u: node.placement.rack_u,
        }));
        // Rack tops with something to reorder (two or more items).
        const tops = this.racks.filter((rack) => rack.topContainerId).map((rack) => ({
            containerId: rack.topContainerId,
            rackName: rack.name,
            devices: rackTopOrder(this.nodes.filter((node) => (
                node.device_id && node.placement?.container_id === rack.topContainerId
            ))).map((node) => ({ id: node.device_id, name: node.name })),
        })).filter((top) => top.devices.length > 1);
        return { racks, devices, tops };
    }

    /** Enter arrange mode: close the drawer, hold live updates, and unlock dragging. */
    startArrange() {
        if (this.arrangement) return;
        const layout = this.arrangeLayout();
        if (!layout.devices.length && !layout.tops.length) {
            this.showNotice("There is no rack-mounted inventory in this area to arrange.");
            return;
        }
        if (this.drawerOpen) this.closeDrawer();
        this.selectedId = null;
        this.arrangement = new RackArrangement(layout);
        this.arrangeMessage = "";
        this.root.dataset.arranging = "true";
        this.render({ preserveForms: true });
        this.root.querySelector(".rack-device.is-movable, .rack-top-item.is-movable")?.focus({ preventScroll: true });
    }

    /** Leave arrange mode without touching the server. */
    endArrange() {
        this.cancelDrag();
        this.arrangement = null;
        this.arrangeMessage = "";
        delete this.root.dataset.arranging;
        // The reload that follows shows the latest collection, so nothing is held.
        this.updateHeld = false;
        this.heldTrigger = null;
    }

    /** Discard the draft and reload the saved layout. */
    async cancelArrange() {
        this.endArrange();
        try {
            await this.load({ quiet: true });
        } catch { /* load() already showed the error. */ }
        this.toolOpenerElement()?.focus({ preventScroll: true });
    }

    /** Save every move in one request, then reload; keep the draft on failure. */
    async saveArrange() {
        if (!this.arrangement || this.arrangeSaving) return;
        const moves = this.arrangement.moves();
        const topOrders = this.arrangement.topOrders();
        if (!moves.length && !topOrders.length) {
            await this.cancelArrange();
            return;
        }
        this.arrangeSaving = true;
        this.arrangeMessage = "Saving…";
        this.renderArrangeBar();
        try {
            await labratJson(
                LABRAT_API.rackArrangement, { payload: { moves, top_orders: topOrders }, csrfToken: this.csrfToken }
            );
            this.endArrange();
            await this.load({ quiet: true });
            this.showNotice(`Saved arrangement: ${this.describeChanges(moves.length, topOrders.length)}.`, true);
            this.toolOpenerElement()?.focus({ preventScroll: true });
        } catch (error) {
            if (this.arrangement) this.arrangeMessage = `Not saved: ${error.message}`;
        } finally {
            this.arrangeSaving = false;
            this.renderArrangeBar();
        }
    }

    /** Show or hide the arrange bar and keep its controls and status current. */
    renderArrangeBar() {
        const active = Boolean(this.arrangement);
        rackElement("rack-arrange-bar").hidden = !active;
        this.updateToolStates();
        if (!active) return;
        const changes = this.arrangement.changes();
        const summary = changes.any
            ? `${this.describeChanges(changes.moved, changes.tops)}, not saved yet.`
            : "No changes yet.";
        rackElement("rack-arrange-status").textContent = this.arrangeMessage
            ? `${this.arrangeMessage} ${this.arrangeSaving ? "" : summary}`.trim()
            : summary;
        rackElement("rack-arrange-save").disabled = !changes.any || Boolean(this.arrangeSaving);
    }

    /**
     * Phrase unsaved or saved arrange changes.
     *
     * @param {number} moved - Rack devices moved.
     * @param {number} tops - Rack tops reordered.
     * @returns {string} For example "2 devices moved, 1 rack top reordered".
     */
    describeChanges(moved, tops) {
        const parts = [];
        if (moved) parts.push(`${moved} device${moved === 1 ? "" : "s"} moved`);
        if (tops) parts.push(`${tops} rack top${tops === 1 ? "" : "s"} reordered`);
        return parts.join(", ");
    }

    /** Copy the draft's positions onto the page's nodes so racks and cables redraw there. */
    syncArrangement() {
        for (const node of this.nodes) {
            const position = node.device_id ? this.arrangement.position(node.device_id) : null;
            if (!position || !node.placement) continue;
            node.placement.container_id = position.containerId;
            node.placement.rack_u = position.rack_u;
        }
        for (const top of this.arrangement.tops.values()) {
            top.order.forEach((deviceId, index) => {
                const node = this.nodes.find((item) => item.device_id === deviceId);
                if (!node?.placement) return;
                node.placement.position = { ...(node.placement.position || {}), x: rackTopX(index, top.order.length) };
            });
        }
    }

    /**
     * Apply a planned drop or nudge, redraw, and report it in the arrange bar.
     *
     * @param {object} node - Device being moved.
     * @param {ReturnType<RackArrangement["plan"]>} result - The plan.
     * @returns {boolean} True when the draft changed.
     */
    commitArrangePlan(node, result) {
        if (result.kind === "refused") {
            this.arrangeMessage = `${result.reason}.`;
            this.renderArrangeBar();
            return false;
        }
        if (result.kind === "none") return false;
        this.arrangement.apply(result);
        this.syncArrangement();
        if (result.kind === "reorder") {
            const top = this.arrangement.tops.get(result.containerId);
            this.arrangeMessage = `${node.name} to place ${result.index + 1} of ${top.order.length} on top of ${top.rackName}.`;
        } else {
            const rack = this.racks.find((item) => item.containerId === result.moves[0].containerId);
            this.arrangeMessage = result.kind === "swap"
                ? `${node.name} swapped with ${result.partner.name}.`
                : `${node.name} to ${rack?.name || "rack"} U${result.unit}.`;
        }
        this.render({ preserveForms: true });
        return true;
    }

    /**
     * Make a rack card draggable and arrow-key movable in arrange mode.
     *
     * @param {HTMLElement} card - The device's rack card.
     * @param {object} node - The device.
     * @param {string} face - The elevation the card is drawn on.
     * @returns {void}
     */
    bindArrange(card, node, face) {
        card.classList.add("is-movable");
        card.dataset.device = String(node.device_id);
        card.setAttribute("aria-roledescription", "movable rack device");
        card.setAttribute("aria-keyshortcuts", "ArrowUp ArrowDown");
        card.addEventListener("pointerdown", (event) => this.beginDrag(event, card, node));
        card.addEventListener("keydown", (event) => {
            if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
            event.preventDefault();
            const result = this.arrangement.nudge(node.device_id, event.key === "ArrowUp" ? 1 : -1);
            this.commitArrangePlan(node, result);
            (this.root.querySelector(`.rack-frame[data-face="${face}"] .rack-device[data-device="${node.device_id}"]`)
                || this.root.querySelector(`.rack-device[data-device="${node.device_id}"]`))
                ?.focus({ preventScroll: false });
        });
    }

    /**
     * Make a rack-top card draggable sideways and left/right-arrow movable.
     *
     * @param {HTMLElement} card - The rack-top card.
     * @param {object} node - Its device.
     * @returns {void}
     */
    bindArrangeTop(card, node) {
        card.classList.add("is-movable");
        card.dataset.device = String(node.device_id);
        card.setAttribute("aria-roledescription", "movable rack-top equipment");
        card.setAttribute("aria-keyshortcuts", "ArrowLeft ArrowRight");
        card.addEventListener("pointerdown", (event) => this.beginDrag(event, card, node, "top"));
        card.addEventListener("keydown", (event) => {
            if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
            event.preventDefault();
            this.commitArrangePlan(
                node, this.arrangement.nudgeTop(node.device_id, event.key === "ArrowRight" ? 1 : -1)
            );
            this.root.querySelector(`.rack-top-item[data-device="${node.device_id}"]`)?.focus({ preventScroll: true });
        });
    }

    /**
     * Start tracking a possible drag; it begins once the pointer moves a few pixels.
     *
     * @param {PointerEvent} event - The press on a movable card.
     * @param {HTMLElement} card - The card.
     * @param {object} node - Its device.
     * @param {"rack"|"top"} [kind] - A rack device (moves by units) or a
     *     rack-top item (reorders sideways).
     * @returns {void}
     */
    beginDrag(event, card, node, kind = "rack") {
        if (event.button !== 0 || this.drag || !this.arrangement) return;
        const bounds = card.getBoundingClientRect();
        const onMove = (moveEvent) => this.moveDrag(moveEvent);
        const onUp = (upEvent) => this.endDrag(upEvent, true);
        const onCancel = (cancelEvent) => this.endDrag(cancelEvent, false);
        const onKey = (keyEvent) => {
            if (keyEvent.key !== "Escape") return;
            // Escape cancels the drag only; it must not also close anything.
            keyEvent.preventDefault();
            keyEvent.stopPropagation();
            this.endDrag(null, false);
        };
        this.drag = {
            node, card, bounds, kind,
            pointerId: event.pointerId,
            startX: event.clientX,
            startY: event.clientY,
            grabY: event.clientY - bounds.top,
            started: false,
            plan: null,
            ghost: null,
            cleanup: () => {
                document.removeEventListener("pointermove", onMove);
                document.removeEventListener("pointerup", onUp);
                document.removeEventListener("pointercancel", onCancel);
                document.removeEventListener("keydown", onKey, true);
            },
        };
        document.addEventListener("pointermove", onMove);
        document.addEventListener("pointerup", onUp);
        document.addEventListener("pointercancel", onCancel);
        document.addEventListener("keydown", onKey, true);
    }

    /**
     * Move the floating copy with the pointer and preview where it would land.
     *
     * Rack blocks scroll on their own and would clip the card, so a fixed
     * copy follows the pointer while the original stays, faded, in place.
     *
     * @param {PointerEvent} event - Pointer movement.
     * @returns {void}
     */
    moveDrag(event) {
        const drag = this.drag;
        if (!drag || event.pointerId !== drag.pointerId) return;
        const dx = event.clientX - drag.startX;
        const dy = event.clientY - drag.startY;
        if (!drag.started) {
            if (Math.hypot(dx, dy) < 5) return;
            drag.started = true;
            const ghost = drag.card.cloneNode(true);
            ghost.classList.add("rack-drag-ghost");
            ghost.removeAttribute("id");
            ghost.setAttribute("aria-hidden", "true");
            Object.assign(ghost.style, {
                left: `${drag.bounds.left}px`, top: `${drag.bounds.top}px`,
                width: `${drag.bounds.width}px`, height: `${drag.bounds.height}px`,
            });
            document.body.append(ghost);
            drag.ghost = ghost;
            drag.card.classList.add("is-drag-source");
        }
        event.preventDefault();
        drag.ghost.style.transform = `translate(${dx}px, ${dy}px)`;
        if (drag.kind === "top") {
            this.previewTopDrop(event);
            return;
        }
        const slots = document.elementFromPoint(event.clientX, event.clientY)?.closest(".rack-slots[data-container]");
        if (!slots) {
            drag.plan = null;
            this.showDropTarget(null);
            return;
        }
        const rackHeight = Number(slots.dataset.heightU);
        const height = this.arrangement.devices.get(drag.node.device_id).height;
        const rect = slots.getBoundingClientRect();
        const topIndex = Math.round((event.clientY - drag.grabY - rect.top) / (rect.height / rackHeight));
        const unit = rackHeight - topIndex - height + 1;
        drag.plan = this.arrangement.plan(drag.node.device_id, Number(slots.dataset.container), unit);
        this.showDropTarget(slots, drag.plan, unit, height);
    }

    /**
     * Preview a rack-top reorder: the insertion point among the other items,
     * in reading order (rack tops wrap onto more than one row).
     *
     * @param {PointerEvent} event - Pointer movement.
     * @returns {void}
     */
    previewTopDrop(event) {
        const drag = this.drag;
        const items = document.elementFromPoint(
            event.clientX, event.clientY
        )?.closest(".rack-top-items[data-container]");
        this.showDropTarget(null);
        if (!items) {
            drag.plan = null;
            return;
        }
        const others = [...items.querySelectorAll(".rack-top-item")].filter((card) => card !== drag.card);
        let index = others.length;
        for (const [at, card] of others.entries()) {
            const box = card.getBoundingClientRect();
            if (event.clientY < box.top || (event.clientY <= box.bottom && event.clientX < box.left + box.width / 2)) {
                index = at;
                break;
            }
        }
        drag.plan = this.arrangement.reorderTop(drag.node.device_id, Number(items.dataset.container), index);
        if (drag.plan.kind === "none") return;
        const marker = rackText("span", `rack-top-drop is-${drag.plan.kind === "reorder" ? "move" : "refused"}`, "");
        marker.setAttribute("aria-hidden", "true");
        if (drag.plan.kind === "refused") marker.textContent = drag.plan.reason;
        items.insertBefore(marker, others[index] || null);
        this.dropTarget = marker;
    }

    /**
     * Draw where a drop would land: the units, a swap partner, or why not.
     *
     * @param {HTMLElement|null} slots - Rack face under the pointer, or null to clear.
     * @param {object} [result] - Plan for that position.
     * @param {number} [unit] - Bottom U under the dragged card.
     * @param {number} [height] - Dragged device height in U.
     * @returns {void}
     */
    showDropTarget(slots, result = null, unit = 1, height = 1) {
        this.dropTarget?.remove();
        this.dropTarget = null;
        if (!slots || !result || result.kind === "none") return;
        const rackHeight = Number(slots.dataset.heightU);
        const landing = result.unit ?? unit;
        const top = Math.min(Math.max(0, rackHeight - landing - height + 1), Math.max(0, rackHeight - height));
        const target = rackText("div", `rack-drop-target is-${result.kind}`, "");
        target.setAttribute("aria-hidden", "true");
        target.style.top = `calc(var(--rack-u) * ${top})`;
        target.style.height = `calc(var(--rack-u) * ${Math.min(height, rackHeight)})`;
        target.append(rackText("span", "rack-drop-label", result.kind === "swap"
            ? `Swap with ${result.partner.name}`
            : result.kind === "move" ? `U${landing}` : result.reason));
        slots.append(target);
        this.dropTarget = target;
    }

    /**
     * Finish a drag: drop where the preview said, or put the card back.
     *
     * @param {PointerEvent|null} event - Pointer release or cancellation.
     * @param {boolean} drop - Whether to apply the previewed result.
     * @returns {void}
     */
    endDrag(event, drop) {
        const drag = this.drag;
        if (!drag || (event && event.pointerId !== drag.pointerId)) return;
        this.cancelDrag();
        if (!drag.started) return;
        if (!drop) {
            this.arrangeMessage = "Move cancelled.";
            this.renderArrangeBar();
        } else if (!drag.plan) {
            this.arrangeMessage = drag.kind === "top"
                ? "Drop on the same rack top to reorder."
                : "Drop onto a rack to move a device.";
            this.renderArrangeBar();
        } else {
            this.commitArrangePlan(drag.node, drag.plan);
        }
    }

    /** Remove drag listeners, the floating copy, and the drop preview. */
    cancelDrag() {
        const drag = this.drag;
        if (!drag) return;
        drag.cleanup();
        drag.ghost?.remove();
        drag.card.classList.remove("is-drag-source");
        this.showDropTarget(null);
        this.drag = null;
    }
}

rackAddMethods(RackArrangeMode);
