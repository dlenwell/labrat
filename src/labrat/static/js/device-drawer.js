/*
 * Copyright 2026 LabRat contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Used by: Discovery, Equipment and Diagram, Discovery clients, Floor plan, Locations, Location detail, Settings.
 */

/**
 * @fileoverview Shared slide-out drawer used by Equipment, Discovery, and Settings.
 *
 * The markup comes from the `device_drawer` macro in
 * `templates/private/_device_drawer.html`; styles live in
 * `static/css/device-drawer.css` (console finish in `console.css`). This
 * class owns only the shell: which panel is showing, the slide between
 * panels, the title bar's Back and Close, and closing on Escape or a click
 * outside. Each page keeps its own panel contents and tells the drawer what
 * to do through small callbacks, so a fix to the drawer lands on every page
 * that uses it.
 */

"use strict";

/** Milliseconds a panel takes to slide; matches device-drawer.css. */
const DEVICE_DRAWER_SLIDE_MS = 300;

/**
 * One drawer with a stack of sliding panels.
 *
 * Panels are `.device-drawer-view` sections inside the drawer, each with a
 * `data-view` name and a `.device-drawer-panel-title` heading that becomes
 * the title bar's text. Exactly one panel is active; the others are inert.
 */
class DeviceDrawer {
    /**
     * Bind the drawer's controls once.
     *
     * @param {object} options - Page integration.
     * @param {HTMLElement} options.host - Element that carries
     *     `data-drawer="open|closed"` (the page root); CSS slides on it.
     * @param {string} [options.rootView] - Panel Back returns to when the
     *     stack is empty.
     * @param {boolean} [options.closeAtRoot] - Treat the root panel as the
     *     closed state: stepping back to it closes the drawer (Discovery has
     *     no panel of its own there; Equipment shows its equipment list).
     * @param {(view: string, previous: string) => void} [options.onActivate] -
     *     Called after a panel becomes active.
     * @param {() => void} [options.onClose] - Called after the drawer closes.
     * @param {(target: Element) => boolean} [options.keepsOpen] - True when a
     *     press on this element outside the drawer must not close it (for
     *     example, a card that opens a different panel).
     * @param {() => boolean} [options.ignoreClick] - True when the click that
     *     ends a press should not count (for example, the end of a pan).
     * @param {Document} [options.doc] - Document holding the drawer; tests
     *     pass a stand-in (tests/js/device-drawer.mjs).
     */
    constructor({ host, rootView = "shelf", closeAtRoot = false, onActivate = null,
        onClose = null, keepsOpen = null, ignoreClick = null, doc = document }) {
        this.doc = doc;
        this.host = host;
        this.element = doc.getElementById("device-drawer");
        this.rootView = rootView;
        this.closeAtRoot = closeAtRoot;
        this.onActivate = onActivate;
        this.onClose = onClose;
        this.keepsOpen = keepsOpen || (() => false);
        this.ignoreClick = ignoreClick || (() => false);
        this.currentView = this.element.dataset.view || rootView;
        this.viewStack = [];
        // Panels' tools live in the title bar, shown while their panel is
        // active (updateBar). Moved once, so their IDs stay in the page.
        this.tools = this.doc.getElementById("device-drawer-tools") || null;
        for (const group of this.element.querySelectorAll?.(".device-drawer-panel-tools") || []) {
            this.tools?.append(group);
        }
        this.isOpen = false;
        // Bumped on every open, so the click that opened the drawer can never
        // also close it (its press began while the drawer was closed).
        this.openings = 0;
        this.transitionVersion = 0;
        this.bindEvents();
    }

    /**
     * Back, Close, Escape, and outside clicks.
     *
     * An outside close needs a press that began outside the drawer while it
     * was open, followed by a click that also lands outside. Presses that
     * begin inside (including drags that end outside), keyboard clicks, and
     * anything `keepsOpen` or `ignoreClick` exempts never close it.
     *
     * @returns {void}
     */
    bindEvents() {
        this.doc.getElementById("device-drawer-back").addEventListener("click", () => this.back());
        this.doc.getElementById("device-drawer-close").addEventListener("click", () => this.close());
        this.doc.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && this.isOpen) this.close();
        });
        let outsidePress = null;
        this.doc.addEventListener("pointerdown", (event) => {
            const target = DeviceDrawer.elementOf(event.target);
            outsidePress = this.isOpen && target && !this.element.contains(target)
                && !this.keepsOpen(target) ? this.openings : null;
        }, true);
        this.doc.addEventListener("click", (event) => {
            const press = outsidePress;
            outsidePress = null;
            const target = DeviceDrawer.elementOf(event.target);
            if (press === null || !this.isOpen || press !== this.openings || event.button !== 0
                || (target && (this.element.contains(target) || this.keepsOpen(target)))
                || this.ignoreClick()) {
                return;
            }
            this.close();
        });
    }

    /**
     * The event target when it is an element (text nodes and the document
     * itself are not).
     *
     * @param {*} target - An event target.
     * @returns {Element|null} The element, or null.
     */
    static elementOf(target) {
        return target && typeof target.closest === "function" ? target : null;
    }

    /** @returns {HTMLElement[]} Every panel in the drawer. */
    panels() {
        return [...this.element.querySelectorAll(".device-drawer-view")];
    }

    /**
     * Make one panel active, sliding from the current one.
     *
     * Only the outgoing and incoming panels are visible during a slide. The
     * version check stops an older timeout from hiding a newer slide.
     *
     * @param {string} view - Destination panel's `data-view`.
     * @param {"forward"|"back"|"none"} [direction] - Slide direction.
     * @returns {void}
     */
    activate(view, direction = "none") {
        const previous = this.currentView;
        const panels = this.panels();
        const previousPanel = panels.find((panel) => panel.dataset.view === previous);
        const nextPanel = panels.find((panel) => panel.dataset.view === view);
        if (!nextPanel) {
            throw new Error(`Unknown drawer panel: ${view}`);
        }
        this.transitionVersion += 1;
        for (const panel of panels) {
            panel.classList.remove(
                "is-active", "is-entering-left", "is-entering-right",
                "is-exiting-left", "is-exiting-right",
            );
            if (panel === previousPanel) panel.classList.add("is-active");
        }
        if (previousPanel && previousPanel !== nextPanel && direction !== "none") {
            const entering = direction === "back" ? "is-entering-left" : "is-entering-right";
            const exiting = direction === "back" ? "is-exiting-right" : "is-exiting-left";
            nextPanel.classList.add(entering);
            // Establish the entering position before starting the transition.
            void nextPanel.offsetWidth;
            previousPanel.classList.remove("is-active");
            previousPanel.classList.add(exiting);
            nextPanel.classList.remove(entering);
            nextPanel.classList.add("is-active");
            const version = this.transitionVersion;
            setTimeout(() => {
                if (version === this.transitionVersion) previousPanel.classList.remove(exiting);
            }, DEVICE_DRAWER_SLIDE_MS);
        } else {
            if (previousPanel !== nextPanel) previousPanel?.classList.remove("is-active");
            nextPanel.classList.add("is-active");
        }
        this.currentView = view;
        this.element.dataset.view = view;
        for (const panel of panels) {
            const inactive = panel !== nextPanel;
            panel.inert = inactive;
            panel.setAttribute("aria-hidden", String(inactive));
        }
        this.updateBar();
        this.onActivate?.(view, previous);
    }

    /**
     * Slide forward to a panel, remembering where Back returns.
     *
     * @param {string} view - Destination panel's `data-view`.
     * @returns {void}
     */
    push(view) {
        if (view === this.currentView) return;
        this.viewStack.push(this.currentView);
        this.activate(view, "forward");
    }

    /**
     * Slide back one panel and focus a control there.
     *
     * @param {string|null} [focusId] - Element to focus after returning.
     * @returns {string} The panel now showing.
     */
    back(focusId = null) {
        const previous = this.viewStack.pop() || this.rootView;
        this.activate(previous, "back");
        if (previous === this.rootView && this.closeAtRoot) {
            this.close();
        } else if (focusId) {
            this.doc.getElementById(focusId)?.focus();
        }
        return previous;
    }

    /**
     * Start over at a panel with nothing to go back to.
     *
     * @param {string} view - Panel to show.
     * @param {string[]} [stack] - Panels Back should step through first.
     * @param {"forward"|"back"|"none"} [direction] - Slide direction.
     * @returns {void}
     */
    reset(view, stack = [], direction = "none") {
        this.viewStack = [...stack];
        if (view !== this.currentView) {
            this.activate(view, direction);
        } else {
            this.updateBar();
        }
    }

    /**
     * Show the active panel's title, and Back only when there is somewhere to
     * go back to. Call after changing a panel's title text.
     *
     * @returns {void}
     */
    updateBar() {
        const panel = this.panels().find((item) => item.dataset.view === this.currentView);
        const title = panel?.querySelector(".device-drawer-panel-title")?.textContent.trim();
        this.doc.getElementById("device-drawer-title").textContent = title || "Details";
        this.doc.getElementById("device-drawer-back").hidden = this.viewStack.length === 0;
        for (const group of this.tools?.children || []) {
            group.hidden = group.dataset.toolsFor !== this.currentView;
        }
    }

    /**
     * Slide the drawer in.
     *
     * @param {string|null} [focusId] - Element to focus once open.
     * @returns {void}
     */
    open(focusId = null) {
        this.openings += 1;
        this.isOpen = true;
        this.host.dataset.drawer = "open";
        this.element.inert = false;
        if (focusId) this.doc.getElementById(focusId)?.focus({ preventScroll: true });
    }

    /**
     * Slide the drawer out and let the page clear its selection.
     *
     * @returns {void}
     */
    close() {
        if (!this.isOpen) return;
        this.isOpen = false;
        this.host.dataset.drawer = "closed";
        this.element.inert = true;
        this.onClose?.();
    }
}
