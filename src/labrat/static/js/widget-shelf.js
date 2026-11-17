// Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
// Used by: Home, Discovery, Equipment and Diagram, Discovery clients, Cluster, Networks, Floor plan, Widgets.
/**
 * @file The disk shelf widget (part of the widget kit, after widgets.js):
 * drives drawn as 3.5" hot-swap bays in rack chassis, beside an optional
 * health box. It takes plain data, like every widget; shelf-model.js
 * turns the cluster API's ZFS pools, Ceph pools, and node drives into
 * it. Adds `shelf` (and helpers) to window.LabratWidgets and registers
 * `shelf` for markup (data-widget="shelf").
 *
 *     LabratWidgets.shelf({
 *         health: { state: "OK", tone: "on", title: "tank", subtitle: "", facts: [["Free", "2 TiB"]], found: [] },
 *         chassis: [{ name: "node-a", aside: "I/O idle", groups: [{ name: "raidz1-0", bays: [bay, ...] }] }],
 *     });
 *
 * A bay: `label`, `device`, `lines` (pairs of strings or `{text, tone}`),
 * `issues` (`{tone: alarm|warn|info, text}`; notes never colour a bay),
 * `verdict`, `state`, `down`, `m2` (drawn as an M.2 board), `pools`
 * (`{slot, name}`: the colour stripe), `gauges` (`{kind: "fill"|"dial"}`
 * specs, left to right), `activity` (`{level: idle|light|busy, source}`;
 * the lamp is simulated and says so), and `tip` (lines).
 */
(() => {
    "use strict";

    const W = window.LabratWidgets;
    const { el, svg, lamp, tip, meter, key } = W;

    /** Blink timing per level: seconds per cycle, picked at random in range. */
    const BLINK = { idle: [5, 9], light: [1.6, 3.2], busy: [0.35, 0.9] };
    const RANK = { alarm: 0, warn: 1, info: 2 };

    /**
     * The worst tone of a list of issues; notes do not count.
     * @param {Array<{tone: string}>} issues Issues.
     * @returns {"on"|"warn"|"alarm"} Tone.
     */
    function worst(issues) {
        return issues.some((item) => item.tone === "alarm")
            ? "alarm"
            : issues.some((item) => item.tone === "warn")
            ? "warn"
            : "on";
    }

    /**
     * The activity lamp: a CSS blink whose speed and phase are set per bay
     * (through the CSSOM, as the CSP forbids style attributes).
     * @param {{level: string, source: string}} activity Level and what sets it.
     * @param {boolean} dark Whether the drive is out of service (no blink).
     * @returns {HTMLElement} The lamp.
     */
    function activityLamp(activity, dark) {
        const level = (activity && BLINK[activity.level]) ? activity.level : "idle";
        const led = el("span", { class: `shelf-led shelf-led-activity${dark ? " is-dark" : ` is-${level}`}`, "aria-hidden": "true" });
        if (!dark) {
            const [low, high] = BLINK[level];
            led.style.animationDuration = `${(low + Math.random() * (high - low)).toFixed(2)}s`;
            led.style.animationDelay = `${(-Math.random() * high).toFixed(2)}s`;
        }
        tip(led, ["Activity (simulated)", `Nothing reports per-drive I/O; this lamp blinks at a rate set by ${(activity && activity.source) || "nothing"}.`]);
        return led;
    }

    /**
     * The readout's count: warnings first, else notes.
     * @param {Array<{tone: string}>} issues A bay's issues.
     * @returns {string} Such as "1 warning", "2 notes", or "".
     */
    function readoutCount(issues) {
        const serious = issues.filter((item) => item.tone !== "info").length;
        const count = serious || issues.length;
        return count ? `${count} ${serious ? "warning" : "note"}${count === 1 ? "" : "s"}` : "";
    }

    /**
     * The colour stripe down a bay's left edge: one segment per pool.
     * @param {Array<{slot: string, name: string}>} pools The bay's pools.
     * @returns {HTMLElement|null} The stripe.
     */
    function poolStripe(pools) {
        if (!pools || !pools.length) {
            return null;
        }
        return el(
            "span", { class: "shelf-pool-stripe", "aria-hidden": "true" },
            pools.map((pool) => el("span", { class: `widget-slot-${pool.slot}` }))
        );
    }

    /**
     * A recessed slot filling from the bottom, where a vent grille would
     * be. Its level is set through the CSSOM.
     * @param {{value: number|null, tone: string, text: string, tip: Array<string>}} spec
     *     `value` 0..1 (clamped), `tone` (on, warn, alarm), `text` (the
     *     accessible reading).
     * @returns {HTMLElement} The gauge.
     */
    function fillGauge(spec) {
        const value = typeof spec.value === "number" ? Math.max(0, Math.min(1, spec.value)) : null;
        const level = el("span", { class: `shelf-gauge-fill is-${value === null ? "off" : spec.tone || "on"}` });
        level.style.setProperty("--fill", value === null ? "0" : String(value));
        const node = el("span", { class: "shelf-gauge", role: "img", "aria-label": spec.text || "" }, [level]);
        tip(node, spec.tip || [spec.text || ""]);
        return node;
    }

    /**
     * A small radial speed gauge: a half-circle from the left stop (0) to
     * the right (1), faint coloured zones, the arc filled to the needle in
     * the reading's tone, and a short reading under the hub.
     * @param {{
         value: number|null, tone: string, text: string, reading: string,
         zones: Array<{from: number, to: number, tone: string}>, tip: Array<string>
     }} spec
     *     `value` 0..1 (pinned at the stops), `zones` as fractions.
     * @returns {SVGElement} The dial.
     */
    function dialGauge(spec) {
        const cx = 20;
        const cy = 21;
        const r = 15;
        const point = (fraction) => {
            const angle = Math.PI * (1 - Math.max(0, Math.min(1, fraction)));
            return [cx + r * Math.cos(angle), cy - r * Math.sin(angle)];
        };
        const arc = (from, to) => {
            const [x0, y0] = point(from);
            const [x1, y1] = point(to);
            return `M${x0.toFixed(2)},${y0.toFixed(2)}A${r},${r} 0 0 1 ${x1.toFixed(2)},${y1.toFixed(2)}`;
        };
        const node = svg(
            "svg", { class: "shelf-dial", viewBox: "0 0 40 30", role: "img", "aria-label": spec.text || "" }
        );
        node.append(svg("path", { class: "shelf-dial-track", d: arc(0, 1) }));
        for (const zone of spec.zones || []) {
            node.append(svg("path", { class: `shelf-dial-zone is-${zone.tone}`, d: arc(zone.from, zone.to) }));
        }
        if (typeof spec.value === "number") {
            const value = Math.max(0, Math.min(1, spec.value));
            if (value > 0.005) {
                node.append(svg("path", { class: `shelf-dial-value is-${spec.tone || "on"}`, d: arc(0, value) }));
            }
            const [nx, ny] = point(value);
            node.append(svg(
                "line",
                {
                    class: "shelf-dial-needle", x1: cx, y1: cy, x2: (cx + (nx - cx) * 0.85).toFixed(2),
                    y2: (cy + (ny - cy) * 0.85).toFixed(2)
                }
            ));
        }
        node.append(svg("circle", { class: "shelf-dial-hub", cx, cy, r: 1.8 }));
        const label = svg("text", { class: "shelf-dial-text", x: cx, y: 29, "text-anchor": "middle" });
        label.textContent = spec.reading || "—";
        node.append(label);
        tip(node, spec.tip || [spec.text || ""]);
        return node;
    }

    /**
     * One hot-swap bay: sled front with lamps, gauges, readout, and latch
     * (or an M.2 board's gold contacts).
     * @param {object} bay Bay data (see the file comment).
     * @returns {HTMLElement} The bay.
     */
    function sled(bay) {
        const issues = bay.issues || [];
        const tone = worst(issues);
        const cell = (value) => (value && typeof value === "object"
            ? el("span", { class: value.tone ? `shelf-cell is-${value.tone}` : "", text: value.text })
            : el("span", { text: value ?? "" }));
        const readout = el("div", { class: "shelf-readout" }, [
            el(
                "span", { class: "shelf-readout-line" },
                [el("b", { text: bay.label }), el("span", { text: bay.device || "" })]
            ),
            ...(bay.lines || []).map(([left, right]) => el(
                "span", { class: "shelf-readout-line" }, [cell(left), cell(right)]
            )),
            el("span", { class: `shelf-readout-line shelf-readout-state is-${tone}` }, [
                el("span", { text: bay.verdict || (tone === "on" ? "OK" : tone === "alarm" ? "FAIL" : "REPLACE") }),
                el("span", { class: `shelf-readout-count${tone === "on" && issues.length ? " shelf-readout-note" : ""}`, text: readoutCount(issues) || bay.state || "" })]),
        ]);
        const gauges = el(
            "span", { class: "shelf-gauges" },
            (bay.gauges || []).map((spec) => (spec.kind === "dial" ? dialGauge(spec) : fillGauge(spec)))
        );
        const node = el("article", {
            class: `shelf-sled is-${tone}${bay.down ? " is-down" : ""}${bay.pools && bay.pools.length ? " has-pools" : ""}${bay.m2 ? " is-m2" : ""}`, tabindex: 0,
            "aria-label": `${bay.label}: ${bay.device || ""}, ${issues.length ? issues.map((item) => item.text).join("; ") : "healthy"}`,
        }, [
            el("div", { class: "shelf-sled-lamps" }, [
                el("span", { class: `shelf-led shelf-led-status is-${tone}`, "aria-hidden": "true" }),
                activityLamp(bay.activity, bay.down),
            ]),
            gauges,
            readout,
            el("span", { class: bay.m2 ? "shelf-m2-contacts" : "shelf-latch", "aria-hidden": "true" }),
            poolStripe(bay.pools),
        ]);
        tip(node, [...(bay.tip || []), ...issues.map((item) => `${item.tone === "info" ? "Note" : item.tone === "alarm" ? "Failing" : "Warning"}: ${item.text}`)].filter(Boolean));
        return node;
    }

    /** Re-lays each bay grid out when its width changes. */
    const bayWatcher = typeof ResizeObserver === "function" ? new ResizeObserver((entries) => {
        for (const entry of entries) {
            layoutBays(entry.target);
        }
    }) : null;

    /**
     * Columns for a bay grid (owner: balance rows and columns, so 8 drives
     * make two rows of 4). Wide sleds look balanced at about twice as many
     * columns as rows, so rows start near √(drives ÷ 2) and grow when the
     * box fits fewer columns. No empty slots are drawn (owner).
     * @param {number} drives Sleds in the grid.
     * @param {number} fit Columns the grid's width can hold.
     * @returns {number} Columns; 0 leaves the layout to CSS.
     */
    function bayLayout(drives, fit) {
        if (fit < 2 || !drives) {
            return 0;
        }
        const rows = Math.max(Math.ceil(drives / fit), Math.round(Math.sqrt(drives / 2)), 1);
        return Math.ceil(drives / rows);
    }

    /**
     * Lay a bay grid out by {@link bayLayout}: only the column count is set
     * here; columns are fluid, so sleds scale smoothly (keeping their
     * aspect ratio) as the window is dragged. Too narrow for two bays
     * (phones), CSS lays it out.
     * @param {HTMLElement} grid A `.shelf-bays` or `.shelf-mini` element
     *     (its children are the bays).
     */
    function layoutBays(grid) {
        const style = getComputedStyle(grid);
        const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
        const narrowest = (parseFloat(style.getPropertyValue("--bay-min"))
            || parseFloat(style.getPropertyValue("--bay-width"))) * rem;
        const gap = parseFloat(style.columnGap) || 0;
        const fit = narrowest > 0 ? Math.floor((grid.clientWidth + gap) / (narrowest + gap)) : 0;
        // A shelf asked for a fixed column count (a drawer: one drive per
        // row) keeps it, full width; otherwise rows and columns balance.
        const fixed = Number(grid.dataset.columns) || 0;
        const columns = fixed || bayLayout(grid.children.length, fit);
        const value = fixed
            ? `repeat(${fixed}, minmax(0, 1fr))`
            : columns
            ? `repeat(${columns}, minmax(0, var(--bay-width)))`
            : "";
        if (grid.style.gridTemplateColumns !== value) {
            grid.style.gridTemplateColumns = value;
        }
    }

    /**
     * A chassis with ears, holding groups of bays.
     * @param {{name: string, aside: string, groups: Array<{name: string|null, bays: Array<object>}>}} box The chassis.
     * @param {boolean} [flat] One bay grid for every group, unlabelled (each
     *     bay names its role), so a chassis takes fewer rows.
     * @param {number} [columns] A fixed column count (bays full width).
     * @returns {HTMLElement} The chassis.
     */
    function chassis(box, flat = false, columns = 0) {
        const groups = flat ? [{
            name: null, bays: (box.groups || []).flatMap((group) => group.bays || [])
        }] : box.groups || [];
        return el("div", { class: "shelf-chassis is-shelf" }, [
            el("span", { class: "shelf-ear is-left", "aria-hidden": "true" }),
            el("div", { class: "shelf-chassis-body" }, [
                el("header", { class: "shelf-chassis-label" }, [
                    el("span", { class: "shelf-chassis-name", text: box.name || "" }),
                    el("span", { class: "shelf-chassis-io", text: box.aside || "" }),
                ]),
                ...groups.map((group) => {
                    const grid = el("div", { class: "shelf-bays" }, (group.bays || []).map(sled));
                    if (columns) {
                        grid.dataset.columns = String(columns);
                        grid.style.gridTemplateColumns = `repeat(${columns}, minmax(0, 1fr))`;
                    }
                    if (bayWatcher) {
                        bayWatcher.observe(grid);
                    }
                    return el(
                        "section", { class: "shelf-group" },
                        [group.name ? el("h4", { class: "shelf-group-name", text: group.name }) : null, grid]
                    );
                }),
                groups.some((group) => (group.bays || []).length) ? null : el(
                    "p", { class: "widget-muted", text: "No drives listed." }
                ),
            ]),
            el("span", { class: "shelf-ear is-right", "aria-hidden": "true" }),
        ]);
    }

    /**
     * The health box beside the shelves: state, facts, a pool key, status
     * lines, then every warning and note (worst first).
     * @param {object} spec `state`, `tone`, `title`, `subtitle`, `meter`
     *     (meter options), `facts` (`[term, value]`), `key` (`{slot,
     *     label}` pool colours), `keyTitle`, `lines` (`{tone, label,
     *     text}`), `found` (`{tone, text}`), `note`.
     * @returns {HTMLElement} The box.
     */
    function shelfHealth(spec) {
        const found = (spec.found || []).slice().sort((a, b) => RANK[a.tone] - RANK[b.tone]);
        const warnings = found.filter((item) => item.tone !== "info");
        const notes = found.filter((item) => item.tone === "info");
        // The box's edge shows the worst of the state and every finding.
        const edge = spec.tone === "alarm" || warnings.some((item) => item.tone === "alarm") ? "alarm"
            : spec.tone === "warn" || warnings.length ? "warn" : "on";
        return el("aside", { class: `shelf-health is-${edge}` }, [
            el("header", { class: "shelf-health-heading" }, [
                el("div", { class: `shelf-health-state is-${spec.tone || "on"}` }, [lamp(spec.tone || "on", spec.state || ""), el("strong", { text: spec.state || "" })]),
                el("h3", { text: spec.title || "" }),
                spec.subtitle ? el("p", { class: "widget-muted", text: spec.subtitle }) : null,
            ]),
            spec.meter ? meter(spec.meter) : null,
            (spec.facts || []).length ? el(
                "dl", { class: "widget-facts" },
                spec.facts.flatMap(([term, value]) => [el("dt", { text: term }), el("dd", { text: value })])
            ) : null,
            spec.key && spec.key.length ? el(
                "h4", { class: "shelf-health-subheading", text: spec.keyTitle || "Pools (stripe on each bay)" }
            ) : null,
            spec.key && spec.key.length ? key(spec.key) : null,
            ...(spec.lines || []).map((line) => el(
                "p", { class: "shelf-health-line" },
                [lamp(line.tone, line.label || ""), el("span", { text: line.text })]
            )),
            el(
                "h4",
                {
                    class: "shelf-health-subheading",
                    text: warnings.length ? `Warnings (${warnings.length})` : "Warnings"
                }
            ),
            warnings.length
                ? el(
                    "ul", { class: "shelf-warnings" },
                    warnings.map((item) => el(
                        "li", { class: `is-${item.tone}` },
                        [lamp(item.tone, item.tone === "alarm" ? "Alarm" : "Warning"), el("span", { text: item.text })]
                    ))
                )
                : el(
                    "p", { class: "shelf-health-line" },
                    [lamp("on", "All clear"), el("span", { text: "Nothing needs replacing." })]
                ),
            notes.length ? el("h4", { class: "shelf-health-subheading", text: `Notes (${notes.length})` }) : null,
            notes.length ? el(
                "ul", { class: "shelf-warnings" },
                notes.map((item) => el(
                    "li", { class: "is-info" }, [lamp("off", "Note"), el("span", { text: item.text })]
                ))
            ) : null,
            spec.note ? el("p", { class: "shelf-health-note", text: spec.note }) : null,
        ]);
    }

    /**
     * One small bay for a server's faceplate: status and activity lamps,
     * the device name, a thin fill bar, the pool stripe, and the M.2
     * board look for NVMe; everything else is in its tooltip.
     * @param {object} bay Bay data (as for the shelf).
     * @returns {HTMLElement} The bay.
     */
    function miniBay(bay) {
        const issues = bay.issues || [];
        const tone = worst(issues);
        const fill = (bay.gauges || []).find((spec) => spec.kind === "fill" && typeof spec.value === "number");
        const bar = el("span", { class: `shelf-mini-fill is-${fill ? fill.tone || "on" : "off"}` });
        if (fill) {
            bar.style.setProperty("--fill", String(Math.max(0, Math.min(1, fill.value))));
        }
        const node = el("span", {
            class: `shelf-mini-bay is-${tone}${bay.down ? " is-down" : ""}${bay.m2 ? " is-m2" : ""}${bay.pools && bay.pools.length ? " has-pools" : ""}`,
            role: "img", "aria-label": `${bay.label} ${bay.device || ""}: ${issues.length ? issues.map((item) => item.text).join("; ") : "healthy"}`,
        }, [
            el("span", { class: "shelf-mini-lamps" }, [
                el("span", { class: `shelf-led shelf-led-status is-${tone}`, "aria-hidden": "true" }),
                activityLamp(bay.activity, bay.down),
            ]),
            el("span", { class: "shelf-mini-name", text: bay.label }),
            el("span", { class: bay.m2 ? "shelf-mini-contacts" : "shelf-mini-latch", "aria-hidden": "true" }),
            bar,
            poolStripe(bay.pools),
        ]);
        tip(node, [`${bay.label}${bay.device ? ` · ${bay.device}` : ""}`, ...(bay.tip || []),
            ...issues.map((item) => `${item.tone === "info" ? "Note" : item.tone === "alarm" ? "Failing" : "Warning"}: ${item.text}`)].filter(Boolean));
        return node;
    }

    /**
     * A strip of small bays for a server's faceplate (owner: the rack
     * view's servers show their drives as hot-swap bays with health
     * lamps). Rows and columns balance like the shelf's; rows share the
     * height the faceplate leaves them.
     * @param {Array<object>} bays Bay data (as for the shelf).
     * @returns {HTMLElement} The strip.
     */
    function bayStrip(bays) {
        const grid = el("div", { class: "shelf-mini", role: "group", "aria-label": `${bays.length} drive${bays.length === 1 ? "" : "s"}` }, bays.map(miniBay));
        if (bayWatcher) {
            bayWatcher.observe(grid);
        }
        return grid;
    }

    /**
     * The disk shelf: a health box (optional) beside chassis stacked one
     * per row, or, with `masonry`, packed into as many columns as fit,
     * each chassis keeping its own height (owner, for the cluster-wide
     * overview).
     * @param {{health: object|null, chassis: Array<object>}} data Health box spec and chassis.
     * @param {{masonry?: boolean, flat?: boolean, columns?: number}} [options] Layout:
     *     `masonry` packs chassis into columns; `flat` puts each chassis's
     *     bays in one grid without group headings (owner, to fit a whole
     *     cluster on one screen); `columns` fixes the bays per row (1 in
     *     the device drawer: one drive per row, owner).
     * @returns {HTMLElement} The shelf.
     */
    function shelf(data, { masonry = false, flat = false, columns = 0 } = {}) {
        const stack = el("div", { class: `shelf-stack${masonry ? " is-masonry" : ""}` }, ((data && data.chassis) || []).map((box) => chassis(box, flat, columns)));
        return data && data.health ? el(
            "div", { class: "shelf-pool" }, [shelfHealth(data.health), stack]
        ) : el("div", { class: "shelf-pool is-bare" }, [stack]);
    }

    Object.assign(W, { shelf, shelfHealth, bayStrip, shelfInternals: { bayLayout, worst, readoutCount } });
    W.WIDGETS.shelf = (data, options) => shelf(data, options);
    W.WIDGETS.bayStrip = (data) => bayStrip(data || []);
})();
