/*
 * Copyright 2026 LabRat contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Used by: Observations, Discovery, Equipment and Diagram, Discovery clients, Cluster, Networks, Floor plan, Locations, Location detail, Locations map.
 */

/**
 * @fileoverview Phone ☰ tool menus in the shared view frame heading
 * (Discovery and the room views).
 *
 * Each `[data-view-menu]` (templates/private/_view_frame.html)
 * holds a toggle button and a panel. Wide screens show the panel's buttons
 * inline and never the toggle (view-frame.css). On phones the toggle
 * opens the panel; Escape, an outside click, or widening the window closes it.
 */
"use strict";

/**
 * Bind every view frame tool menu on the page.
 * @returns {void}
 */
function bindViewMenus() {
    for (const menu of document.querySelectorAll("[data-view-menu]")) {
        const button = menu.querySelector(".view-menu-toggle");
        const setOpen = (open) => {
            menu.toggleAttribute("data-open", open);
            button.setAttribute("aria-expanded", String(open));
        };
        button.addEventListener("click", () => setOpen(!menu.hasAttribute("data-open")));
        document.addEventListener("click", (event) => {
            if (menu.hasAttribute("data-open") && !menu.contains(event.target)) setOpen(false);
        });
        menu.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && menu.hasAttribute("data-open")) {
                setOpen(false);
                button.focus();
            }
        });
        matchMedia("(min-width: 641px)").addEventListener("change", (query) => {
            if (query.matches) setOpen(false);
        });
    }
}

bindViewMenus();

/**
 * Build a row of subtabs: the second level of a view frame's pill tabs,
 * for drilling into part of a view (Storage → Ceph → one pool). Rows can
 * nest; `level` 2 draws the deeper, quieter row. Arrow keys move between
 * tabs, and the caller redraws on select (and keeps the address in step).
 *
 * @param {object} options Subtab options.
 * @param {string} options.label Accessible name for the row.
 * @param {Array<{key: string, label: string, detail?: string, tone?: string}>} options.items
 *     Tabs in order; `detail` is a small count or note after the label and
 *     `tone` ("warning", "alarm") adds a status dot.
 * @param {string} options.current Key of the current tab.
 * @param {Function} options.onSelect Called with the chosen key.
 * @param {number} [options.level] 1 for the first subtab row, 2 for a row under it.
 * @returns {HTMLElement} The `nav.view-subtabs` element.
 */
function viewSubtabs({ label, items, current, onSelect, level = 1 }) {
    const nav = document.createElement("nav");
    nav.className = `view-subtabs view-subtabs-level-${level}`;
    nav.setAttribute("aria-label", label);
    const track = document.createElement("div");
    track.className = "view-subtabs-track";
    nav.append(track);
    const buttons = items.map((item) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "view-subtab";
        button.dataset.key = item.key;
        if (item.key === current) {
            button.setAttribute("aria-current", "true");
        }
        if (item.tone) {
            const dot = document.createElement("span");
            dot.className = `view-subtab-dot is-${item.tone}`;
            dot.setAttribute("aria-hidden", "true");
            button.append(dot);
        }
        const text = document.createElement("span");
        text.textContent = item.label;
        button.append(text);
        if (item.detail) {
            const detail = document.createElement("span");
            detail.className = "view-subtab-detail";
            detail.textContent = item.detail;
            button.append(detail);
        }
        button.addEventListener("click", () => onSelect(item.key));
        track.append(button);
        return button;
    });
    track.addEventListener("keydown", (event) => {
        const index = buttons.indexOf(document.activeElement);
        if (index < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
            return;
        }
        event.preventDefault();
        const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
            : (index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
        buttons[next].focus();
    });
    // Keep the current tab in view when the row scrolls sideways.
    requestAnimationFrame(() => {
        const active = buttons.find((button) => button.hasAttribute("aria-current"));
        if (active && track.scrollWidth > track.clientWidth) {
            track.scrollLeft = active.offsetLeft - track.clientWidth / 2 + active.offsetWidth / 2;
        }
    });
    return nav;
}

/** Width at and under which the view heading stacks (view-frame.css). */
const VIEW_SPREAD_MIN_WIDTH = 641;

/**
 * Place a view's first level of subtabs. On wide screens the lit tab in the
 * frame's view switch spreads open and the subtabs sit inside it as an inner
 * strip (owner idea: the pill opens to reveal them). On phones, where the
 * switch already fills the heading, they become a row (viewSubtabs) that
 * the caller puts at the top of the content.
 *
 * Any strip left from an earlier call is removed first, so a view can call
 * this on every redraw; call it with `items: []` to close the strip.
 *
 * @param {object} options viewSubtabs options plus `tab`: the switch's lit
 *     tab element to spread from.
 * @returns {HTMLElement|null} A row for the caller to place, or null when
 *     the subtabs were placed in the switch (or there are none).
 */
function placeSubtabs(options) {
    const { tab } = options;
    for (const old of document.querySelectorAll(".view-tab-strip")) {
        old.previousElementSibling?.classList.remove("is-spread");
        old.remove();
    }
    if (!options.items || !options.items.length) {
        return null;
    }
    if (!tab || window.innerWidth < VIEW_SPREAD_MIN_WIDTH) {
        return viewSubtabs(options);
    }
    const row = viewSubtabs(options);
    const strip = document.createElement("div");
    strip.className = "view-tab-strip";
    strip.setAttribute("role", "group");
    strip.setAttribute("aria-label", options.label);
    const inner = row.querySelector(".view-subtabs-track");
    inner.className = "view-tab-strip-track";
    strip.append(inner);
    // Only animate the first opening, not every redraw of the same view.
    if (tab.dataset.spreadOnce === options.label) {
        strip.classList.add("is-open");
    }
    tab.dataset.spreadOnce = options.label;
    tab.classList.add("is-spread");
    tab.after(strip);
    requestAnimationFrame(() => strip.classList.add("is-open"));
    return null;
}

window.LabratViewFrame = Object.freeze({ subtabs: viewSubtabs, placeSubtabs });
