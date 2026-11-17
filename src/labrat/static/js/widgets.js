// Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
// Used by: Home, Discovery, Equipment and Diagram, Discovery clients, Cluster, Networks, Floor plan, Widgets.
/**
 * @file LabRat widgets: small, self-contained charts and panels that any
 * page can stitch in. Exposed as window.LabratWidgets.
 *
 * Every widget is a function `(data, options) => HTMLElement`. The data is
 * plain (labels, values, Unix times), never a Proxmox or UniFi record, so
 * a page turns its API data into widget input and appends the result. The
 * look is widgets.css: one hue per widget from the nearest `data-hue`
 * (`--hue`), categorical slots in a validated fixed order, and status
 * colours for state. Nothing needs markup in the page: the tooltip makes
 * itself on first use.
 *
 * Declarative use: an element with `data-widget` is drawn by `mount()`
 * (run on load) from inline JSON (a child `<script type="application/json">`
 * holding `{data, options}`) or from `data-widget-src` through a feed
 * registered with `register()` (see widget-feeds.js). See
 * planning/specs/widgets.md for each widget's data.
 *
 * The content security policy forbids inline style attributes, so chart
 * geometry is SVG attributes and sizes set from script go through the
 * CSSOM (element.style), which the policy allows.
 */
(() => {
    "use strict";

    const SVG = "http://www.w3.org/2000/svg";

    // ------------------------------------------------------------------
    // Element builders and formatters
    // ------------------------------------------------------------------

    /**
     * Create an HTML element.
     * @param {string} tag Tag name.
     * @param {object} [attrs] Attributes; `class` and `text` are shortcuts.
     * @param {Array<Node|string|null>} [children] Child nodes or text.
     * @returns {HTMLElement} The element.
     */
    function el(tag, attrs = {}, children = []) {
        const node = document.createElement(tag);
        for (const [key, value] of Object.entries(attrs)) {
            if (value === null || value === undefined || value === false) {
                continue;
            }
            if (key === "text") {
                node.textContent = value;
            } else if (key === "class") {
                node.className = value;
            } else {
                node.setAttribute(key, value === true ? "" : String(value));
            }
        }
        for (const child of children) {
            if (child !== null && child !== undefined) {
                node.append(child);
            }
        }
        return node;
    }

    /**
     * Create an SVG element with attributes.
     * @param {string} tag SVG tag name.
     * @param {object} [attrs] Attributes.
     * @returns {SVGElement} The element.
     */
    function svg(tag, attrs = {}) {
        const node = document.createElementNS(SVG, tag);
        for (const [key, value] of Object.entries(attrs)) {
            if (value !== null && value !== undefined) {
                node.setAttribute(key, String(value));
            }
        }
        return node;
    }

    /**
     * Add an SVG text element.
     * @param {SVGElement} parent Where to add it.
     * @param {object} attrs Attributes.
     * @param {string} content Text.
     * @returns {SVGElement} The text element.
     */
    function text(parent, attrs, content) {
        const node = svg("text", attrs);
        node.textContent = content;
        parent.append(node);
        return node;
    }

    /** Whether a value is a finite number. */
    const isNumber = (value) => typeof value === "number" && Number.isFinite(value);

    /**
     * Format bytes in binary units (GiB, TiB).
     * @param {number|null} value Bytes.
     * @returns {string} Readable size, or an em dash.
     */
    function bytes(value) {
        if (!isNumber(value)) {
            return "—";
        }
        const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
        let size = value;
        let unit = 0;
        while (size >= 1024 && unit < units.length - 1) {
            size /= 1024;
            unit += 1;
        }
        const digits = size >= 100 || unit === 0 ? 0 : 1;
        return `${size.toFixed(digits).replace(/\.0$/, "")} ${units[unit]}`;
    }

    /**
     * Format a byte rate (bytes per second) as bits per second.
     * @param {number|null} value Bytes per second.
     * @returns {string} Such as "12.4 Mb/s".
     */
    function rate(value) {
        if (!isNumber(value)) {
            return "—";
        }
        const units = ["b/s", "kb/s", "Mb/s", "Gb/s", "Tb/s"];
        let size = value * 8;
        let unit = 0;
        while (size >= 1000 && unit < units.length - 1) {
            size /= 1000;
            unit += 1;
        }
        return `${size.toFixed(size >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`;
    }

    /**
     * Format a 0..1 fraction as a whole percentage.
     * @param {number|null} value Fraction.
     * @returns {string} Percentage text.
     */
    function percent(value) {
        return isNumber(value) ? `${Math.round(value * 100)}%` : "—";
    }

    /**
     * Format a plain number with grouping.
     * @param {number|null} value Number.
     * @returns {string} Such as "12,400".
     */
    function number(value) {
        return isNumber(value) ? value.toLocaleString() : "—";
    }

    /**
     * Format seconds of uptime briefly.
     * @param {number|null} seconds Uptime.
     * @returns {string} Such as "8 d" or "5 h".
     */
    function uptime(seconds) {
        if (!isNumber(seconds) || seconds <= 0) {
            return "—";
        }
        const days = Math.floor(seconds / 86400);
        return days >= 1 ? `${days} d` : `${Math.max(1, Math.floor(seconds / 3600))} h`;
    }

    /**
     * Describe how long ago an ISO timestamp was.
     * @param {string|null} value ISO timestamp.
     * @returns {string} Such as "2 min ago".
     */
    function ago(value) {
        const stamp = value ? Date.parse(value) : NaN;
        if (!Number.isFinite(stamp)) {
            return "not sampled yet";
        }
        const minutes = Math.round((Date.now() - stamp) / 60000);
        const days = Math.round(minutes / 1440);
        return minutes < 1
            ? "just now"
            : minutes < 90
            ? `${minutes} min ago`
            : minutes < 2880
            ? `${Math.round(minutes / 60)} h ago`
            : `${days} day${days === 1 ? "" : "s"} ago`;
    }

    /**
     * Format a Unix time as a local clock time.
     * @param {number} seconds Unix time.
     * @returns {string} Such as "14:30".
     */
    function clock(seconds) {
        return new Date(seconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }

    /**
     * Sum a numeric field over records, ignoring missing values.
     * @param {Array<object>} rows Records.
     * @param {Function} pick Returns the number for one record.
     * @returns {number} The total.
     */
    function sum(rows, pick) {
        return rows.reduce((total, row) => {
            const value = pick(row);
            return total + (isNumber(value) ? value : 0);
        }, 0);
    }

    /**
     * Count or total records by a key, largest first: donut and bar input.
     * @param {Array<object>} rows Records.
     * @param {Function} key Label for a record.
     * @param {Function} [value] Amount for a record (default 1).
     * @returns {Array<{label: string, value: number}>} Slices.
     */
    function tally(rows, key, value = () => 1) {
        const counts = new Map();
        for (const item of rows || []) {
            const label = key(item);
            counts.set(label, (counts.get(label) || 0) + (value(item) || 0));
        }
        return [...counts.entries()].map(([label, amount]) => ({
            label, value: amount
        })).sort((a, b) => b.value - a.value);
    }

    /**
     * The severity class for a usage fraction.
     * @param {number} fraction Used share, 0..1.
     * @returns {string} "", "is-warning", or "is-alarm".
     */
    function severity(fraction) {
        return fraction >= 0.9 ? "is-alarm" : fraction >= 0.8 ? "is-warning" : "";
    }

    // ------------------------------------------------------------------
    // Scales (pure; tested in tests/js/widgets.mjs)
    // ------------------------------------------------------------------

    /**
     * Round a peak up to a readable top of scale (1, 2, 2.5, 5 × 10^n).
     * @param {number} peak Largest value.
     * @returns {number} The top of the scale.
     */
    function niceCeil(peak) {
        if (!isNumber(peak) || peak <= 0) {
            return 1;
        }
        const power = 10 ** Math.floor(Math.log10(peak));
        const step = [1, 2, 2.5, 5, 10].find((candidate) => candidate * power >= peak);
        return step * power;
    }

    /**
     * The top of a 0..1 scale: the peak rounded up to the next tenth, at
     * least 10%, at most 100%.
     * @param {number} peak Largest fraction.
     * @returns {number} The top of the scale.
     */
    function fractionCeil(peak) {
        return Math.min(1, Math.max(0.1, Math.ceil(peak * 10 + 0.5) / 10));
    }

    /**
     * Time ticks back from the end of a series, a readable step apart.
     * @param {number} t0 First time (Unix seconds).
     * @param {number} t1 Last time.
     * @returns {Array<{t: number, label: string}>} Ticks, oldest first.
     */
    function timeTicks(t0, t1) {
        const span = Math.max(1, t1 - t0);
        const steps = [300, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800];
        const step = steps.find((candidate) => span / candidate <= 4.5) || steps[steps.length - 1];
        const label = (back) => (back === 0 ? "now" : back >= 172800 ? `−${Math.round(back / 86400)} d`
            : back >= 3600 ? `−${Math.round(back / 3600)} h` : `−${Math.round(back / 60)} min`);
        const ticks = [];
        for (let back = 0; back <= span + 1; back += step) {
            ticks.unshift({ t: t1 - back, label: label(back) });
        }
        return ticks;
    }

    /** Categorical slots in fixed order (validated palette, widgets.css). */
    const SLOTS = 5;

    /** Status tones a slice, bar, or mark may take instead of a slot. */
    const TONES = ["ok", "warning", "alarm", "muted"];

    /**
     * Fold slices past the palette into "Other", keeping fixed colours: a
     * slice's colour follows its position in the given order, never its
     * rank after filtering. Slices with a status `tone` keep that tone.
     * @param {Array<{label: string, value: number, tone?: string}>} slices Slices.
     * @returns {Array<object>} Slices with `slot` ("1".."5", "other", or a tone).
     */
    function foldSlices(slices) {
        const kept = slices.filter((slice) => slice.value > 0);
        const plain = kept.filter((slice) => !slice.tone);
        const toned = kept.filter((slice) => slice.tone);
        const head = plain.slice(
            0, plain.length > SLOTS ? SLOTS - 1 : SLOTS
        ).map((slice, index) => ({ ...slice, slot: String(index + 1) }));
        const tail = plain.slice(head.length);
        if (tail.length) {
            head.push({ label: `Other (${tail.length})`, value: sum(tail, (slice) => slice.value), slot: "other",
                detail: tail.map((slice) => slice.label).join(", ") });
        }
        return [...head, ...toned.map((slice) => ({ ...slice, slot: slice.tone }))];
    }

    /**
     * SVG path for a ring segment (or a pie wedge when inner is 0).
     * @param {number} c Centre (x and y).
     * @param {number} outer Outer radius.
     * @param {number} inner Inner radius.
     * @param {number} from Start angle, 0..1 of a turn from 12 o'clock.
     * @param {number} to End angle.
     * @returns {string} Path data.
     */
    function segment(c, outer, inner, from, to) {
        if (to - from > 0.999) {
            // A whole ring: two halves, since one arc cannot close on itself.
            return segment(c, outer, inner, from, from + 0.5) + segment(c, outer, inner, from + 0.5, from + 1 - 1e-6);
        }
        const span = Math.min(0.99999, Math.max(0, to - from));
        const a0 = (from * 2 - 0.5) * Math.PI;
        const a1 = ((from + span) * 2 - 0.5) * Math.PI;
        const large = span > 0.5 ? 1 : 0;
        const p = (r, a) => `${(c + r * Math.cos(a)).toFixed(2)},${(c + r * Math.sin(a)).toFixed(2)}`;
        if (inner <= 0) {
            return `M${c},${c}L${p(outer, a0)}A${outer},${outer} 0 ${large} 1 ${p(outer, a1)}Z`;
        }
        return `M${p(outer, a0)}A${outer},${outer} 0 ${large} 1 ${p(outer, a1)}L${p(inner, a1)}A${inner},${inner} 0 ${large} 0 ${p(inner, a0)}Z`;
    }

    /**
     * Lay items out as a squarified treemap inside a box.
     * @param {Array<{value: number}>} items Items, largest first for best shapes.
     * @param {{x: number, y: number, w: number, h: number}} box Area to fill.
     * @returns {Array<{item: object, x: number, y: number, w: number, h: number}>} Placed rectangles.
     */
    function squarify(items, box) {
        const placed = [];
        let { x, y, w, h } = box;
        let rest = items.filter((item) => item.value > 0);
        const total = sum(rest, (item) => item.value);
        if (!total || w <= 0 || h <= 0) {
            return placed;
        }
        const scale = (w * h) / total;
        const worst = (row, side) => {
            const areas = row.map((item) => item.value * scale);
            const s = areas.reduce((a, b) => a + b, 0);
            return Math.max(...areas.map((area) => Math.max(
                (side * side * area) / (s * s), (s * s) / (side * side * area)
            )));
        };
        while (rest.length) {
            const side = Math.min(w, h);
            const row = [rest[0]];
            let i = 1;
            while (i < rest.length && worst([...row, rest[i]], side) <= worst(row, side)) {
                row.push(rest[i]);
                i += 1;
            }
            rest = rest.slice(i);
            const area = sum(row, (item) => item.value * scale);
            if (w >= h) {
                const columnWidth = area / h;
                let offset = y;
                for (const item of row) {
                    const height = (item.value * scale) / columnWidth;
                    placed.push({ item, x, y: offset, w: columnWidth, h: height });
                    offset += height;
                }
                x += columnWidth;
                w -= columnWidth;
            } else {
                const rowHeight = area / w;
                let offset = x;
                for (const item of row) {
                    const width = (item.value * scale) / rowHeight;
                    placed.push({ item, x: offset, y, w: width, h: rowHeight });
                    offset += width;
                }
                y += rowHeight;
                h -= rowHeight;
            }
        }
        return placed;
    }

    // ------------------------------------------------------------------
    // Tooltip, lamp, meter, key
    // ------------------------------------------------------------------

    /**
     * The page's one tooltip, made on first use.
     * @returns {HTMLElement} The tooltip.
     */
    function tooltipNode() {
        let node = document.getElementById("widget-tooltip");
        if (!node) {
            node = el("div", { class: "widget-tooltip", id: "widget-tooltip", role: "tooltip", hidden: true });
            document.body.append(node);
        }
        return node;
    }

    /**
     * Attach the shared hover/focus tooltip to an element.
     * @param {Element} target Element that shows the tip.
     * @param {Function|string|Array<string>} content Lines (the first is the
     *     title), or a function of the pointer event returning them.
     */
    function tip(target, content) {
        if (!target) {
            return;
        }
        const place = (event) => {
            const node = tooltipNode();
            const box = target.getBoundingClientRect();
            const x = event && "clientX" in event && event.clientX ? event.clientX : box.left + box.width / 2;
            const y = event && "clientY" in event && event.clientY ? event.clientY : box.top;
            const width = node.offsetWidth;
            const left = Math.min(window.innerWidth - width - 8, Math.max(8, x - width / 2));
            const top = y - node.offsetHeight - 12 < 8 ? y + 18 : y - node.offsetHeight - 12;
            node.style.left = `${left}px`;
            node.style.top = `${top}px`;
        };
        const show = (event) => {
            const lines = typeof content === "function" ? content(event) : content;
            if (!lines || (Array.isArray(lines) && !lines.length)) {
                return;
            }
            const node = tooltipNode();
            node.replaceChildren(...[].concat(lines).filter((line) => line !== ""
                && line !== null
                && line !== undefined)
                .map((line, index) => el(
                    "div", { class: index === 0 ? "widget-tooltip-title" : "widget-tooltip-line", text: line }
                )));
            node.hidden = false;
            place(event);
        };
        target.addEventListener("pointerenter", show);
        target.addEventListener("pointermove", (event) => (typeof content === "function" ? show(event) : place(event)));
        target.addEventListener("focus", show);
        for (const name of ["pointerleave", "blur"]) {
            target.addEventListener(name, () => { tooltipNode().hidden = true; });
        }
    }

    /**
     * A status lamp.
     * @param {"on"|"warn"|"alarm"|"off"} tone Lamp state.
     * @param {string} label Accessible state name.
     * @returns {HTMLElement} The lamp.
     */
    function lamp(tone, label) {
        return el(
            "span", { class: `led widget-led widget-led-${tone}`, role: "img", "aria-label": label, title: label }
        );
    }

    /**
     * A horizontal meter: a used fill, an optional second step, over a track.
     * @param {object} options Meter values.
     * @param {number} options.total Whole (the track).
     * @param {number} options.used First, strongest fill.
     * @param {number} [options.reserved] Second, lighter fill after used
     *     (such as memory allocated to guests but not in use).
     * @param {string} options.label Accessible description.
     * @param {string} [options.tone] "is-warning" or "is-alarm" for the used
     *     fill; "auto" picks from used / total.
     * @param {Array<string>} [options.tip] Tooltip lines.
     * @returns {HTMLElement} The meter (widths set through the CSSOM).
     */
    function meter({ total, used, reserved = 0, label, tone = "", tip: lines = null }) {
        const whole = Math.max(total || 0, (used || 0) + (reserved || 0));
        const resolved = tone === "auto" ? severity(total ? (used || 0) / total : 0) : tone;
        const node = el(
            "span",
            { class: `widget-meter ${resolved}`.trim(), role: "img", "aria-label": label, tabindex: lines ? 0 : null }
        );
        const share = (value) => (whole > 0 ? Math.min(100, Math.max(0, (value / whole) * 100)) : 0);
        if (reserved > 0) {
            const step = el("span", { class: "widget-meter-reserved" });
            step.style.width = `${share((used || 0) + reserved)}%`;
            node.append(step);
        }
        const fill = el("span", { class: "widget-meter-fill" });
        fill.style.width = `${share(used || 0)}%`;
        node.append(fill);
        if (lines) {
            tip(node, lines);
        }
        return node;
    }

    /**
     * A legend row of swatches and labels.
     * @param {Array<{label: string, tone?: string, slot?: string, className?: string}>} items
     *     Each swatch takes a status tone, a categorical slot, or a class.
     * @returns {HTMLElement} The key.
     */
    function key(items) {
        return el("p", { class: "widget-key" }, items.map((item) => el("span", { class: "widget-key-item" }, [
            el("span", { class: `widget-swatch ${item.className || `widget-slot-${item.slot || item.tone || "1"}`}`, "aria-hidden": "true" }),
            el("span", { text: item.label }),
        ])));
    }

    /**
     * A muted line for a widget with nothing to draw.
     * @param {string} message What is missing.
     * @returns {HTMLElement} The note.
     */
    function empty(message) {
        return el("p", { class: "widget-muted widget-none", text: message });
    }

    // ------------------------------------------------------------------
    // Panels and tiles
    // ------------------------------------------------------------------

    /**
     * A sunk panel with a heading inside the frame.
     * @param {string} title Heading text.
     * @param {Array<Node>} children Panel content.
     * @param {object} [options] `hue` (a data-hue name), `aside` (text or a
     *     node right of the title), `className`.
     * @returns {HTMLElement} The panel.
     */
    function panel(title, children, { hue = null, aside = null, className = "" } = {}) {
        return el("section", { class: `widget-panel ${className}`.trim(), "data-hue": hue }, [
            el("header", { class: "widget-panel-heading" }, [
                el("h2", { text: title }),
                aside ? (typeof aside === "string" ? el(
                    "span", { class: "widget-panel-aside", text: aside }
                ) : aside) : null,
            ]),
            ...children,
        ]);
    }

    /**
     * A stat tile: label, figure, a short caption, and an optional meter.
     * @param {object} tile Tile content.
     * @param {string} tile.label What the figure is.
     * @param {string} tile.value The figure, already formatted.
     * @param {string} [tile.caption] A short line under it.
     * @param {object} [tile.meter] Options for meter().
     * @param {{tone: string, label: string}} [tile.lamp] A lamp before the figure.
     * @param {string} [tile.hue] A data-hue name (default: the surrounding hue).
     * @param {Array<string>} [tile.tip] Tooltip lines.
     * @returns {HTMLElement} The tile.
     */
    function statTile({ label, value, caption, meter: meterOptions, lamp: lampOptions, hue = null, tip: lines }) {
        const node = el("article", { class: "widget-stat", "data-hue": hue, tabindex: lines ? 0 : null }, [
            el("h3", { class: "widget-stat-label", text: label }),
            el(
                "strong", { class: "widget-stat-value" },
                [lampOptions ? lamp(lampOptions.tone, lampOptions.label) : null, value]
            ),
            caption ? el("p", { class: "widget-stat-caption", text: caption }) : null,
            meterOptions ? meter(meterOptions) : null,
        ]);
        if (lines) {
            tip(node, lines);
        }
        return node;
    }

    /**
     * A grid of stat tiles.
     * @param {Array<object>} tiles statTile() inputs, or tiles already made.
     * @param {object} [options] `hue` for every tile without its own.
     * @returns {HTMLElement} The grid.
     */
    function stats(tiles, { hue = null } = {}) {
        return el(
            "div", { class: "widget-stat-grid", "data-hue": hue },
            (tiles || []).map((tile) => (tile instanceof Node ? tile : statTile(tile)))
        );
    }

    /**
     * A responsive row of panels or charts, one column on phones.
     * @param {Array<Node>} children Row content.
     * @param {object} [options] `wide` for wider minimum columns.
     * @returns {HTMLElement} The row.
     */
    function row(children, { wide = false } = {}) {
        return el("div", { class: `widget-row${wide ? " is-wide" : ""}` }, children);
    }

    // ------------------------------------------------------------------
    // Time series
    // ------------------------------------------------------------------

    /**
     * An area chart of one series over time, with a hover crosshair.
     * @param {Array<object>} points Points with `t` (Unix seconds) and the field.
     * @param {object} options Chart options.
     * @param {string} options.label Accessible chart name (and tooltip label).
     * @param {string} [options.field] Series field (default "value").
     * @param {boolean} [options.compact] A sparkline: no axes.
     * @param {number} [options.max] Top of the scale (default: fit to data).
     * @param {Function} [options.format] Value formatter (default percent,
     *     which also treats values as 0..1 fractions when fitting).
     * @returns {HTMLElement} The chart.
     */
    function area(points, { label, field = "value", compact = false, max, format = percent }) {
        const series = (points || []).filter((point) => isNumber(point[field]) && isNumber(point.t));
        const width = compact ? 240 : 560;
        const height = compact ? 48 : 180;
        const pad = compact ? { l: 2, r: 6, t: 6, b: 2 } : { l: 52, r: 12, t: 12, b: 24 };
        const wrap = el("div", { class: compact ? "widget-spark" : "widget-chart" });
        const chart = svg(
            "svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": label, class: "widget-svg" }
        );
        wrap.append(chart);
        if (series.length < 2) {
            wrap.append(empty("No history yet"));
            return wrap;
        }
        const peak = Math.max(...series.map((point) => point[field]));
        const top = max || (format === percent ? fractionCeil(peak) : niceCeil(peak));
        const t0 = series[0].t;
        const t1 = series[series.length - 1].t;
        const x = (t) => pad.l + ((t - t0) / Math.max(1, t1 - t0)) * (width - pad.l - pad.r);
        const y = (value) => pad.t + (1 - Math.min(value, top) / top) * (height - pad.t - pad.b);
        if (!compact) {
            for (const step of [0, 0.5, 1]) {
                chart.append(svg(
                    "line", { class: "widget-grid", x1: pad.l, x2: width - pad.r, y1: y(top * step), y2: y(top * step) }
                ));
                text(
                    chart, { class: "widget-axis", x: pad.l - 6, y: y(top * step) + 4, "text-anchor": "end" },
                    format(top * step)
                );
            }
            for (const tick of timeTicks(t0, t1)) {
                if (tick.t >= t0) {
                    text(
                        chart,
                        {
                            class: "widget-axis", x: x(tick.t), y: height - 6,
                            "text-anchor": tick.t === t1 ? "end" : "middle"
                        },
                        tick.label
                    );
                }
            }
        }
        const line = series.map((point, index) => `${index ? "L" : "M"}${x(point.t).toFixed(1)},${y(point[field]).toFixed(1)}`).join("");
        const base = y(0);
        chart.append(svg(
            "path", { class: "widget-area", d: `${line}L${x(t1).toFixed(1)},${base}L${x(t0).toFixed(1)},${base}Z` }
        ));
        chart.append(svg("path", { class: "widget-line", d: line }));
        const last = series[series.length - 1];
        chart.append(svg("circle", { class: "widget-dot", cx: x(last.t), cy: y(last[field]), r: compact ? 3 : 4 }));
        // Hover: a crosshair and the nearest reading.
        const cross = svg(
            "line", { class: "widget-cross", y1: pad.t, y2: height - pad.b, x1: 0, x2: 0, visibility: "hidden" }
        );
        const marker = svg(
            "circle", { class: "widget-dot widget-dot-hover", r: compact ? 3 : 4, cx: 0, cy: 0, visibility: "hidden" }
        );
        const hit = svg(
            "rect",
            { class: "widget-hit", x: pad.l, y: 0, width: width - pad.l - pad.r, height, tabindex: compact ? null : 0 }
        );
        chart.append(cross, marker, hit);
        tip(hit, (event) => {
            const box = chart.getBoundingClientRect();
            const px = event && event.clientX ? ((event.clientX - box.left) / box.width) * width : x(last.t);
            const point = series.reduce(
                (best, item) => (Math.abs(x(item.t) - px) < Math.abs(x(best.t) - px) ? item : best), series[0]
            );
            for (const node of [cross, marker]) {
                node.setAttribute("visibility", "visible");
            }
            cross.setAttribute("x1", x(point.t));
            cross.setAttribute("x2", x(point.t));
            marker.setAttribute("cx", x(point.t));
            marker.setAttribute("cy", y(point[field]));
            return [clock(point.t), `${label}: ${format(point[field])}`];
        });
        hit.addEventListener("pointerleave", () => {
            cross.setAttribute("visibility", "hidden");
            marker.setAttribute("visibility", "hidden");
        });
        return wrap;
    }

    /**
     * A sparkline: area() without axes.
     * @param {Array<object>} points Points with `t` and the field.
     * @param {object} options As area().
     * @returns {HTMLElement} The sparkline.
     */
    function sparkline(points, options) {
        return area(points, { ...options, compact: true });
    }

    /**
     * A mirrored area: one series above the axis (such as traffic in), the
     * other below (traffic out), on one shared scale, with a crosshair that
     * reads both.
     * @param {Array<object>} points Points with `t` and both fields.
     * @param {object} options Chart options.
     * @param {string} options.label Accessible name.
     * @param {{field: string, label: string}} [options.up] Series above (default `in`).
     * @param {{field: string, label: string}} [options.down] Series below (default `out`).
     * @param {Function} [options.format] Value formatter (default rate).
     * @returns {HTMLElement} The chart.
     */
    function mirror(
        points, { label, up = { field: "in", label: "In" }, down = { field: "out", label: "Out" }, format = rate }
    ) {
        const series = (points || []).filter((point) => isNumber(point[up.field]) || isNumber(point[down.field]));
        const width = 560;
        const height = 180;
        const pad = { l: 64, r: 12, t: 10, b: 22 };
        const wrap = el("div", { class: "widget-chart" });
        const chart = svg(
            "svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": label, class: "widget-svg" }
        );
        wrap.append(chart);
        if (series.length < 2) {
            wrap.append(empty("No history yet"));
            return wrap;
        }
        const top = Math.max(1, ...series.map((point) => Math.max(point[up.field] || 0, point[down.field] || 0)));
        const mid = pad.t + (height - pad.t - pad.b) / 2;
        const half = (height - pad.t - pad.b) / 2;
        const t0 = series[0].t;
        const t1 = series[series.length - 1].t;
        const x = (t) => pad.l + ((t - t0) / Math.max(1, t1 - t0)) * (width - pad.l - pad.r);
        const yUp = (value) => mid - (value / top) * half;
        const yDown = (value) => mid + (value / top) * half;
        for (const [y, label2] of [
            [pad.t, `${up.label} ${format(top)}`], [mid, "0"], [height - pad.b, `${down.label} ${format(top)}`]
        ]) {
            chart.append(svg("line", { class: "widget-grid", x1: pad.l, x2: width - pad.r, y1: y, y2: y }));
            text(chart, { class: "widget-axis", x: pad.l - 6, y: y + 4, "text-anchor": "end" }, label2);
        }
        const draw = (field, y, kind) => {
            const line = series.map((point, index) => `${index ? "L" : "M"}${x(point.t).toFixed(1)},${y(point[field] || 0).toFixed(1)}`).join("");
            chart.append(svg(
                "path",
                { class: `widget-area ${kind}`, d: `${line}L${x(t1).toFixed(1)},${mid}L${x(t0).toFixed(1)},${mid}Z` }
            ));
            chart.append(svg("path", { class: `widget-line ${kind}`, d: line }));
        };
        draw(up.field, yUp, "is-up");
        draw(down.field, yDown, "is-down");
        const cross = svg(
            "line", { class: "widget-cross", y1: pad.t, y2: height - pad.b, x1: 0, x2: 0, visibility: "hidden" }
        );
        const hit = svg(
            "rect", { class: "widget-hit", x: pad.l, y: 0, width: width - pad.l - pad.r, height, tabindex: 0 }
        );
        chart.append(cross, hit);
        tip(hit, (event) => {
            const box = chart.getBoundingClientRect();
            const px = event && event.clientX ? ((event.clientX - box.left) / box.width) * width : x(t1);
            const point = series.reduce(
                (best, item) => (Math.abs(x(item.t) - px) < Math.abs(x(best.t) - px) ? item : best), series[0]
            );
            cross.setAttribute("visibility", "visible");
            cross.setAttribute("x1", x(point.t));
            cross.setAttribute("x2", x(point.t));
            return [
                clock(point.t), `${up.label} ${format(point[up.field])}`, `${down.label} ${format(point[down.field])}`
            ];
        });
        hit.addEventListener("pointerleave", () => cross.setAttribute("visibility", "hidden"));
        return wrap;
    }

    /**
     * Small multiples: one sparkline per item on one shared scale.
     * @param {Array<{label: string, caption?: string, points: Array<object>}>} items Items in order.
     * @param {object} [options] `field` (default "value"), `format`, `label` (accessible name prefix).
     * @returns {HTMLElement} The grid.
     */
    function multiples(items, { field = "value", format = percent, label = "" } = {}) {
        const drawn = (items || []).filter((item) => (item.points || []).length > 1);
        if (!drawn.length) {
            return empty("No history yet.");
        }
        const peak = Math.max(...drawn.flatMap((item) => item.points.map((point) => point[field] || 0)));
        const top = format === percent ? Math.max(0.05, peak) : niceCeil(peak);
        return el("div", { class: "widget-multiples" }, drawn.map((item) => el("div", { class: "widget-multiple" }, [
            el(
                "p", { class: "widget-multiple-name" },
                [
                    el("strong", { text: item.label }),
                    item.caption ? el("span", { class: "widget-muted", text: ` ${item.caption}` }) : null
                ]
            ),
            sparkline(item.points, { field, format, max: top, label: `${item.label}${label ? ` ${label}` : ""}` }),
        ])));
    }

    /**
     * A 24-hour radial clock: one petal per point, its length the value,
     * midnight at the top and now marked.
     * @param {Array<object>} points Points with `t` and the field.
     * @param {object} options `label`, `field` (default "value"), `max`, `format`.
     * @returns {HTMLElement} The chart.
     */
    function radialClock(points, { label, field = "value", max, format = percent }) {
        const series = (points || []).filter((point) => isNumber(point[field]));
        const size = 260;
        const c = size / 2;
        const inner = 34;
        const outer = 118;
        const top = max || Math.max(0.05, ...series.map((point) => point[field])) * 1.1;
        const chart = svg(
            "svg",
            { class: "widget-round-svg widget-clock", viewBox: `0 0 ${size} ${size}`, role: "img", "aria-label": label }
        );
        for (const ring of [0.5, 1]) {
            chart.append(svg(
                "circle", { class: "widget-clock-ring", cx: c, cy: c, r: inner + (outer - inner) * ring }
            ));
        }
        for (let hour = 0; hour < 24; hour += 6) {
            const angle = (hour / 24) * 2 * Math.PI - Math.PI / 2;
            text(
                chart,
                {
                    class: "widget-clock-hour", x: c + (outer + 10) * Math.cos(angle),
                    y: c + (outer + 10) * Math.sin(angle) + 4, "text-anchor": "middle"
                },
                String(hour).padStart(2, "0")
            );
        }
        const at = (t) => {
            const date = new Date(t * 1000);
            return (date.getHours() * 60 + date.getMinutes()) / 1440;
        };
        for (const point of series) {
            const radius = inner + (outer - inner) * Math.min(1, point[field] / top);
            chart.append(svg(
                "path",
                {
                    class: "widget-clock-petal",
                    d: segment(c, radius, inner, at(point.t), at(point.t) + (1 / 96) * 0.85)
                }
            ));
        }
        const last = series[series.length - 1];
        if (last) {
            const angle = at(last.t) * 2 * Math.PI - Math.PI / 2;
            chart.append(svg(
                "line",
                {
                    class: "widget-clock-now", x1: c + inner * Math.cos(angle), y1: c + inner * Math.sin(angle),
                    x2: c + (outer + 4) * Math.cos(angle), y2: c + (outer + 4) * Math.sin(angle)
                }
            ));
        }
        text(
            chart, { class: "widget-round-centre is-small", x: c, y: c + 6, "text-anchor": "middle" },
            last ? format(last[field]) : "—"
        );
        const wrap = el("div", { class: "widget-clock-wrap", tabindex: 0 }, [chart]);
        tip(
            wrap,
            [
                label,
                `Petals: readings around the clock, longest ${format(Math.max(0, ...series.map((point) => point[field])))}`,
                `Rings at ${format(top / 2)} and ${format(top)}`
            ]
        );
        return wrap;
    }

    /**
     * Events on a time axis, one lane each: a dot per event (a bar when it
     * lasted longer than `barAfter`), coloured by status tone.
     * @param {{lanes: Array<{id: string, label: string}>, events: Array<object>}} data
     *     Events have `lane`, `start` (Unix seconds), optional `end`, `tone`
     *     ("ok", "warning", "alarm", "muted" or a slot), and `tip` lines.
     * @param {object} [options] `label`, `span` (seconds back from `now`,
     *     default a day), `now`, `barAfter` (seconds, default 120), `legend`
     *     (key() items; default the four tones).
     * @returns {HTMLElement} The chart with its key.
     */
    function timeline(
        { lanes = [], events = [] },
        { label = "Timeline", span = 86400, now = Date.now() / 1000, barAfter = 120, legend } = {}
    ) {
        const rows = [...lanes];
        for (const event of events) {
            if (event.lane && !rows.some((lane) => lane.id === event.lane)) {
                rows.push({ id: event.lane, label: event.lane });
            }
        }
        const t0 = now - span;
        const width = 1000;
        const lane = 26;
        const pad = { l: 90, r: 14, t: 8, b: 22 };
        const height = pad.t + pad.b + lane * Math.max(1, rows.length);
        const x = (t) => pad.l + ((Math.max(t0, t) - t0) / span) * (width - pad.l - pad.r);
        const chart = svg(
            "svg",
            {
                class: "widget-svg", viewBox: `0 0 ${width} ${height}`, role: "img",
                "aria-label": `${label}: ${events.length} events`
            }
        );
        rows.forEach((item, index) => {
            const y = pad.t + lane * index + lane / 2;
            chart.append(svg("line", { class: "widget-grid", x1: pad.l, x2: width - pad.r, y1: y, y2: y }));
            text(chart, { class: "widget-axis", x: pad.l - 8, y: y + 4, "text-anchor": "end" }, item.label);
        });
        for (const tick of timeTicks(t0, now)) {
            text(
                chart,
                { class: "widget-axis", x: x(tick.t), y: height - 6, "text-anchor": tick.t === now ? "end" : "middle" },
                tick.label
            );
        }
        // Alarms last, so they draw on top.
        const rank = (event) => (event.tone === "alarm" ? 1 : 0);
        for (const event of [...events].filter((item) => isNumber(item.start)).sort((a, b) => rank(a) - rank(b))) {
            const y = pad.t + lane * Math.max(0, rows.findIndex((item) => item.id === event.lane)) + lane / 2;
            const end = isNumber(event.end) ? event.end : now;
            const tone = event.tone || "muted";
            const mark = end - event.start > barAfter
                ? svg(
                    "rect",
                    {
                        class: `widget-event widget-slot-${tone}`, x: x(event.start), y: y - 4,
                        width: Math.max(8, x(end) - x(event.start)), height: 8, rx: 4, tabindex: 0
                    }
                )
                : svg(
                    "circle",
                    { class: `widget-event widget-slot-${tone}`, cx: x(event.start), cy: y, r: 4.5, tabindex: 0 }
                );
            tip(mark, event.tip || [event.label || "", clock(event.start)]);
            chart.append(mark);
        }
        const keyItems = legend || [
            { tone: "ok", label: "OK" }, { tone: "warning", label: "warnings" }, { tone: "alarm", label: "failed" },
            { tone: "muted", label: "running" }
        ];
        return el("div", { class: "widget-timeline" }, [chart, keyItems.length ? key(keyItems) : null]);
    }

    // ------------------------------------------------------------------
    // Parts of a whole
    // ------------------------------------------------------------------

    /**
     * A donut (or pie) with a legend, a centre figure, and hover details.
     * Slices are separated by a 2px surface gap; hovering a slice or its
     * legend row lights both.
     * @param {Array<{label: string, value: number, tone?: string, detail?: string}>} slices
     *     In a fixed order. `tone` ("ok", "warning", "alarm", "muted") uses a
     *     status colour instead of a categorical slot.
     * @param {object} options Chart options.
     * @param {string} options.label Accessible name.
     * @param {Function} [options.format] Value formatter.
     * @param {string} [options.centre] Big centre text (donut only).
     * @param {string} [options.caption] Small text under the centre.
     * @param {boolean} [options.pie] Draw a pie instead of a donut.
     * @returns {HTMLElement} The chart with its legend.
     */
    function donut(slices, { label, format = String, centre = "", caption = "", pie = false }) {
        const parts = foldSlices(slices || []);
        const total = sum(parts, (slice) => slice.value);
        const size = 200;
        const c = size / 2;
        const outer = 92;
        const inner = pie ? 0 : 60;
        const chart = svg("svg", { class: "widget-round-svg", viewBox: `0 0 ${size} ${size}`, role: "img",
            "aria-label": `${label}: ${parts.map((slice) => `${slice.label} ${format(slice.value)}`).join(", ") || "nothing yet"}` });
        const legend = el("ul", { class: "widget-legend" });
        const rows = [];
        const light = (active) => {
            chart.classList.toggle("is-focused", Boolean(active));
            for (const [path, item] of rows) {
                path.classList.toggle("is-lit", path === active);
                item.classList.toggle("is-lit", path === active);
            }
        };
        if (!total) {
            chart.append(svg(
                "circle",
                {
                    class: "widget-round-empty", cx: c, cy: c, r: (outer + inner) / 2,
                    "stroke-width": outer - inner || outer
                }
            ));
        }
        let at = 0;
        for (const slice of parts) {
            const share = total ? slice.value / total : 0;
            const path = svg(
                "path",
                {
                    class: `widget-slice widget-slot-${slice.slot}`, d: segment(c, outer, inner, at, at + share),
                    tabindex: 0
                }
            );
            at += share;
            chart.append(path);
            const item = el("li", { class: "widget-legend-row", tabindex: -1 }, [
                el("span", { class: `widget-swatch widget-slot-${slice.slot}`, "aria-hidden": "true" }),
                el("span", { class: "widget-legend-label", text: slice.label }),
                el("span", { class: "widget-legend-value", text: format(slice.value) }),
                el("span", { class: "widget-legend-share", text: percent(share) }),
            ]);
            legend.append(item);
            rows.push([path, item]);
            const lines = [slice.label, `${format(slice.value)} · ${percent(share)}`, slice.detail || ""];
            for (const node of [path, item]) {
                tip(node, lines);
                node.addEventListener("pointerenter", () => light(path));
                node.addEventListener("focus", () => light(path));
                node.addEventListener("pointerleave", () => light(null));
                node.addEventListener("blur", () => light(null));
            }
        }
        if (!pie) {
            text(
                chart, { class: "widget-round-centre", x: c, y: c + (caption ? 4 : 8), "text-anchor": "middle" }, centre
            );
            if (caption) {
                text(chart, { class: "widget-round-caption", x: c, y: c + 24, "text-anchor": "middle" }, caption);
            }
        }
        return el("div", { class: "widget-donut" }, [chart, legend]);
    }

    /**
     * A pie: donut() without the hole.
     * @param {Array<object>} slices As donut().
     * @param {object} options As donut().
     * @returns {HTMLElement} The chart with its legend.
     */
    function pie(slices, options) {
        return donut(slices, { ...options, pie: true });
    }

    /**
     * A two-ring sunburst: an inner ring of groups and an outer ring of
     * their members, each member in a lighter or darker step of its group's
     * colour, so the hierarchy reads by hue.
     * @param {Array<{label: string, children: Array<{label: string, value: number}>}>} groups Groups in fixed order.
     * @param {object} options `label`, `format`, `centre`, `caption`.
     * @returns {HTMLElement} The chart with a legend of groups.
     */
    function sunburst(groups, { label, format = String, centre = "", caption = "" }) {
        const size = 260;
        const c = size / 2;
        const sized = (groups || []).map((group) => ({
            ...group, value: sum(group.children || [], (child) => child.value)
        })).filter((group) => group.value > 0);
        const folded = foldSlices(sized.map((group) => ({ label: group.label, value: group.value, group })));
        const total = sum(folded, (slice) => slice.value);
        const chart = svg(
            "svg",
            {
                class: "widget-round-svg widget-sunburst", viewBox: `0 0 ${size} ${size}`, role: "img",
                "aria-label": label
            }
        );
        const legend = el("ul", { class: "widget-legend" });
        let at = 0;
        for (const slice of folded) {
            const share = total ? slice.value / total : 0;
            const inner = svg(
                "path",
                { class: `widget-slice widget-slot-${slice.slot}`, d: segment(c, 78, 48, at, at + share), tabindex: 0 }
            );
            tip(inner, [slice.label, `${format(slice.value)} · ${percent(share)}`]);
            chart.append(inner);
            const children = slice.group
                ? [...slice.group.children].filter((child) => child.value > 0).sort((a, b) => b.value - a.value)
                : [{ label: slice.label, value: slice.value }];
            let childAt = at;
            children.forEach((child, index) => {
                const childShare = total ? child.value / total : 0;
                const path = svg(
                    "path",
                    {
                        class: `widget-slice widget-slice-outer widget-slot-${slice.slot} step-${index % 2}`,
                        d: segment(c, 124, 82, childAt, childAt + childShare), tabindex: 0
                    }
                );
                tip(path, [child.label, `${format(child.value)} · ${percent(childShare)} of all`, `in ${slice.label}`]);
                chart.append(path);
                childAt += childShare;
            });
            at += share;
            legend.append(el("li", { class: "widget-legend-row" }, [
                el("span", { class: `widget-swatch widget-slot-${slice.slot}`, "aria-hidden": "true" }),
                el("span", { class: "widget-legend-label", text: slice.label }),
                el("span", { class: "widget-legend-value", text: format(slice.value) }),
                el("span", { class: "widget-legend-share", text: `${children.length}` }),
            ]));
        }
        text(chart, { class: "widget-round-centre", x: c, y: c + 4, "text-anchor": "middle" }, centre);
        text(chart, { class: "widget-round-caption", x: c, y: c + 22, "text-anchor": "middle" }, caption);
        return el("div", { class: "widget-donut" }, [chart, legend]);
    }

    /**
     * One bar split into parts, a 2px surface gap between them, with a key.
     * @param {Array<{label: string, value: number, tone?: string}>} parts
     *     Parts in order; `tone` is a status tone or slot (default slots in order).
     * @param {object} [options] `label`, `format`, `key` (false to hide it).
     * @returns {HTMLElement} The bar and its key.
     */
    function segments(parts, { label = "", format = String, key: showKey = true } = {}) {
        const kept = (parts || []).filter((part) => part.value > 0).map((part, index) => ({
            ...part, tone: part.tone || String((index % SLOTS) + 1)
        }));
        const total = sum(kept, (part) => part.value) || 1;
        const bar = el("div", { class: "widget-segments", role: "img", "aria-label": `${label}: ${kept.map((part) => `${part.label} ${format(part.value)}`).join(", ")}` });
        for (const part of kept) {
            const node = el("span", { class: `widget-segment widget-slot-${part.tone}`, tabindex: 0 });
            node.style.flexGrow = String(part.value);
            tip(node, [part.label, `${format(part.value)} of ${format(total)} (${percent(part.value / total)})`]);
            bar.append(node);
        }
        return el(
            "div", { class: "widget-segmented" },
            [
                bar,
                showKey ? key(kept.map((part) => ({
                    tone: part.tone, label: `${part.label} ${format(part.value)}`
                }))) : null
            ]
        );
    }

    // ------------------------------------------------------------------
    // Bars and columns
    // ------------------------------------------------------------------

    /**
     * One bar-list row: name, a graphic, a figure.
     * @param {object} item Row data with `label`, optional `sub`, `tip`.
     * @param {Node} graphic The bar.
     * @param {string} figure Text right of the bar.
     * @returns {HTMLElement} The row.
     */
    function barRow(item, graphic, figure) {
        const node = el("li", { class: "widget-bar-row", tabindex: 0 }, [
            el(
                "span", { class: "widget-bar-name" },
                [
                    el("strong", { text: item.label }),
                    item.sub ? el("span", { class: "widget-muted", text: ` ${item.sub}` }) : null
                ]
            ),
            graphic,
            el("span", { class: "widget-bar-figure", text: figure }),
        ]);
        return node;
    }

    /**
     * Horizontal bars: name, meter, figure.
     * @param {Array<object>} rows Rows: `label`, optional `sub` (muted, after
     *     the name), `value`, `total` (for scale "row"), `reserved`, `tone`,
     *     `figure` (text; default from the format), `tip` (lines).
     * @param {object} [options] Options.
     * @param {Function} [options.format] Value formatter.
     * @param {"shared"|"row"} [options.scale] "shared" (default): every bar on
     *     the largest value's scale, for ranking. "row": each bar against its
     *     own `total`, coloured by how full it is.
     * @param {boolean} [options.sort] Largest first (default true).
     * @param {number} [options.limit] Most rows to show.
     * @param {string} [options.empty] Text when nothing has a value.
     * @returns {HTMLElement} The bar list.
     */
    function bars(
        rows,
        {
            format = String, scale = "shared", sort = true, limit = Infinity, empty: emptyText = "Nothing to show yet."
        } = {}
    ) {
        const measure = (item) => (scale === "row" && item.total ? (item.value || 0) / item.total : item.value || 0);
        const kept = (rows || []).filter((item) => (item.value || 0) > 0 || scale === "row");
        const ordered = (sort ? [...kept].sort((a, b) => measure(b) - measure(a)) : kept).slice(0, limit);
        if (!ordered.length) {
            return empty(emptyText);
        }
        const top = Math.max(...ordered.map((item) => item.value || 0), 0) || 1;
        return el("ul", { class: "widget-bars" }, ordered.map((item) => {
            const whole = scale === "row" ? item.total || 0 : top;
            const fraction = whole ? (item.value || 0) / whole : 0;
            const figure = item.figure
                || (scale === "row" ? `${format(item.value)} / ${format(item.total)}` : format(item.value));
            const node = barRow(item, meter({
                total: whole, used: item.value || 0, reserved: item.reserved || 0,
                label: `${item.label}: ${figure}`, tone: item.tone ?? (scale === "row" ? severity(fraction) : ""),
            }), figure);
            tip(
                node,
                item.tip || [
                    item.sub ? `${item.label} · ${item.sub}` : item.label,
                    scale === "row" ? `${figure} (${percent(fraction)})` : figure
                ]
            );
            return node;
        }));
    }

    /**
     * Allocation bars: each row's use on one scale shared by every row, with
     * a tick at the row's own capacity, so overcommit reads past the tick.
     * @param {Array<{label: string, used: number, total: number, tip?: Array<string>}>} rows Rows in order.
     * @param {object} [options] `format`, `unit` (what the capacity is, for the tooltip).
     * @returns {HTMLElement} The bar list.
     */
    function allocation(rows, { format = String, unit = "" } = {}) {
        const top = Math.max(1, ...(rows || []).map((item) => Math.max(item.used || 0, item.total || 0)));
        return el("ul", { class: "widget-bars" }, (rows || []).map((item) => {
            const fraction = item.total ? (item.used || 0) / item.total : 0;
            const track = el("span", { class: `widget-alloc${fraction > 1 ? " is-over" : ""}`, role: "img", "aria-label": `${item.label}: ${format(item.used)} of ${format(item.total)}` });
            const fill = el("span", { class: "widget-alloc-fill" });
            fill.style.width = `${((item.used || 0) / top) * 100}%`;
            const cap = el("span", { class: "widget-alloc-cap" });
            cap.style.left = `${((item.total || 0) / top) * 100}%`;
            track.append(fill, cap);
            const node = barRow(item, track, `${format(item.used)} / ${format(item.total)}`);
            tip(node, item.tip || [item.label, `${format(item.used)} of ${format(item.total)}${unit ? ` ${unit}` : ""} (${percent(fraction)})`]);
            return node;
        }));
    }

    /**
     * Split bars: two or three parts of each row laid end to end (such as
     * data in and out), every row on one scale, with a key.
     * @param {Array<{label: string, sub?: string, values: Array<number>, tip?: Array<string>}>} rows Rows.
     * @param {object} [options] `parts` (labels of the parts, default
     *     ["in", "out"]), `format`, `limit`, `sort` (default true).
     * @returns {HTMLElement} The bar list and key.
     */
    function splitBars(rows, { parts = ["in", "out"], format = String, limit = Infinity, sort = true } = {}) {
        const total = (item) => sum(item.values || [], (value) => value);
        const kept = (rows || []).filter((item) => total(item) > 0);
        const ordered = (sort ? [...kept].sort((a, b) => total(b) - total(a)) : kept).slice(0, limit);
        if (!ordered.length) {
            return empty("Nothing to show yet.");
        }
        const top = Math.max(1, ...ordered.map(total));
        const list = el("ul", { class: "widget-bars" }, ordered.map((item) => {
            const track = el("span", { class: "widget-split", role: "img",
                "aria-label": `${item.label}: ${parts.map((part, index) => `${format(item.values[index])} ${part}`).join(", ")}` });
            parts.forEach((_, index) => {
                const piece = el("span", { class: `widget-split-part step-${index}` });
                piece.style.width = `${((item.values[index] || 0) / top) * 100}%`;
                track.append(piece);
            });
            const node = barRow(item, track, format(total(item)));
            tip(
                node,
                item.tip || [
                    item.label, parts.map((part, index) => `${part} ${format(item.values[index])}`).join(" · ")
                ]
            );
            return node;
        }));
        return el(
            "div", {},
            [list, key(parts.map((part, index) => ({ label: part, className: `widget-split-part step-${index}` })))]
        );
    }

    /**
     * Vertical columns with rounded tops, square bases, and optional
     * reference lines (such as a near-full threshold).
     * @param {Array<{label: string, value: number, tone?: string, mark?: boolean, tip?: Array<string>}>} rows
     *     Columns in order; `tone` "warning" or "alarm" recolours one;
     *     `mark` lights it in the full hue and dims the unmarked ones.
     * @param {object} [options] `label`, `max` (default fitted), `format`,
     *     `lines` (values drawn across, `{value, warn}`), `warnAt` (columns at
     *     or above turn warning), `width` (the drawing's width, default
     *     1100), `axis` (false drops the value scale), `values` (print each
     *     column's value above it).
     * @returns {HTMLElement} The chart.
     */
    function columns(
        rows,
        {
            label = "Columns", max, format = String, lines = [], warnAt = null, width = 1100, axis = true,
            values = false
        } = {}
    ) {
        const height = 150;
        const pad = { l: axis ? 46 : 8, r: 8, t: values ? 18 : 8, b: 22 };
        const top = max || niceCeil(Math.max(0, ...(rows || []).map((item) => item.value || 0)));
        const marked = (rows || []).some((item) => item.mark);
        const chart = svg("svg", { class: `widget-svg${marked ? " has-marks" : ""}`, viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": label });
        const y = (value) => pad.t + (1 - Math.min(value, top) / top) * (height - pad.t - pad.b);
        const guides = axis ? [{ value: 0 }, { value: top / 2 }, { value: top }, ...lines] : [{ value: 0 }, ...lines];
        for (const guide of guides) {
            chart.append(svg(
                "line",
                {
                    class: guide.warn ? "widget-grid widget-grid-warn" : "widget-grid", x1: pad.l, x2: width - pad.r,
                    y1: y(guide.value), y2: y(guide.value)
                }
            ));
            if (axis) {
                text(
                    chart, { class: "widget-axis", x: pad.l - 6, y: y(guide.value) + 4, "text-anchor": "end" },
                    format(guide.value)
                );
            }
        }
        const band = (width - pad.l - pad.r) / Math.max(1, (rows || []).length);
        const barWidth = Math.max(2, Math.min(24, band - 4));
        (rows || []).forEach((item, index) => {
            const value = item.value || 0;
            const x = pad.l + band * index + (band - barWidth) / 2;
            const tone = item.tone || (warnAt !== null && value >= warnAt ? "warning" : "");
            const top2 = y(value);
            const h = Math.max(1, y(0) - top2);
            const r = Math.min(4, h, barWidth / 2);
            const path = svg("path", {
                class: `widget-col${tone ? ` is-${tone}` : ""}${item.mark ? " is-marked" : ""}`, tabindex: 0,
                d: `M${x},${y(0)}V${top2 + r}Q${x},${top2} ${x + r},${top2}H${x + barWidth - r}Q${x + barWidth},${top2} ${x + barWidth},${top2 + r}V${y(0)}Z`,
            });
            tip(path, item.tip || [item.label, format(value)]);
            chart.append(path);
            if (band >= 14) {
                text(
                    chart, { class: "widget-axis", x: x + barWidth / 2, y: height - 6, "text-anchor": "middle" },
                    item.label
                );
            }
            if (values && band >= 14) {
                text(
                    chart, { class: "widget-axis", x: x + barWidth / 2, y: top2 - 4, "text-anchor": "middle" },
                    format(value)
                );
            }
        });
        return el("div", { class: "widget-columns" }, [chart]);
    }

    /**
     * Several small column charts on one shared scale, side by side (such
     * as neighbouring Wi-Fi networks per channel, one chart per band). Each
     * chart is as wide as its columns need (at least 240px) and never
     * stretches past that.
     * @param {Array<{label: string, rows: Array<object>}>} groups Groups in
     *     order; rows as for `columns`.
     * @param {object} [options] `label`, `format`, `note` (a line under the
     *     charts), `values` (default true), `axis` (default false).
     * @returns {HTMLElement} The charts.
     */
    function columnGroups(groups, { label = "Columns", format = String, note = "", values = true, axis = false } = {}) {
        const list = (groups || []).filter((group) => (group.rows || []).length);
        if (!list.length) {
            return empty("Nothing to chart yet.");
        }
        const max = niceCeil(Math.max(1, ...list.flatMap((group) => group.rows.map((item) => item.value || 0))));
        return el("div", { class: "widget-column-groups-wrap" }, [
            el("div", { class: "widget-column-groups" }, list.map((group) => {
                const width = Math.max(240, group.rows.length * 34 + (axis ? 54 : 16));
                const chart = columns(
                    group.rows, { label: `${label}: ${group.label}`, max, format, width, axis, values }
                );
                chart.firstChild.style.maxWidth = `${width}px`;
                return el(
                    "div", { class: "widget-column-group" },
                    [el("h3", { class: "widget-group-title", text: group.label }), chart]
                );
            })),
            note ? el("p", { class: "widget-note", text: note }) : null,
        ]);
    }

    // ------------------------------------------------------------------
    // DNS sinkholes
    // ------------------------------------------------------------------

    /**
     * DNS sinkholes (custom DNS servers, likely Pi-holes): a card each with
     * the evidence for it and, when a Pi-hole integration has read it, the
     * day's queries, blocked share, and top blocked domains.
     * @param {Array<object>} items Sinkholes: `ip`, `names`, `likely_pihole`,
     *     `evidence` (lines), and `integration` (`{error, stats}` or null).
     * @param {object} [options] `evidence` (false hides the evidence lines),
     *     `empty` (text with none).
     * @returns {HTMLElement} The cards.
     */
    function sinkholes(
        items,
        { evidence = true, empty: none = "No custom DNS server found in DHCP, and no client named like Pi-hole." } = {}
    ) {
        if (!(items || []).length) {
            return empty(none);
        }
        return el("div", { class: "widget-sinkholes" }, items.map((item) => el("article", { class: `widget-card widget-sinkhole${item.likely_pihole ? " is-pihole" : ""}` }, [
            el("header", { class: "widget-sinkhole-heading" }, [
                el("span", { class: "widget-pihole-mark", "aria-hidden": "true" }),
                el("div", {}, [el("h3", { text: (item.names || [])[0] || item.ip }),
                    el("p", { class: "widget-muted", text: `${item.ip}${(item.names || []).length > 1 ? ` · ${item.names.slice(1).join(", ")}` : ""}` })]),
                el("span", { class: `widget-pill${item.likely_pihole ? " is-pihole" : ""}`, text: item.likely_pihole ? "Pi-hole" : "DNS server" }),
            ]),
            evidence && (item.evidence || []).length ? el(
                "ul", { class: "widget-evidence" }, item.evidence.map((line) => el("li", { text: line }))
            ) : null,
            piholeStats(item),
        ])));
    }

    /**
     * A sinkhole's Pi-hole statistics when its integration has read them;
     * otherwise what is missing (no key yet, or the last read failed).
     * @param {object} item Sinkhole entry with its `integration`.
     * @returns {HTMLElement} The statistics or a note.
     */
    function piholeStats(item) {
        const integration = item.integration;
        if (!integration) {
            return el("p", { class: "widget-sinkhole-foot" }, [lamp("off", "Not connected"),
                el(
                    "span",
                    { text: item.likely_pihole
                        ? "No Pi-hole integration yet: add its application password in Settings to see queries, blocking, and top domains."
                        : "Not set up as a Pi-hole integration." }
                )]);
        }
        const stats = integration.stats;
        if (!stats) {
            return el("p", { class: "widget-sinkhole-foot" }, [lamp(
                integration.error ? "alarm" : "off", integration.error ? "Failed" : "Waiting"
            ),
                el(
                    "span",
                    { text: integration.error
                        ? `The integration could not read it: ${integration.error}.`
                        : "Integration added; waiting for the first read." }
                )]);
        }
        const queries = stats.queries || {};
        const blocked = queries.blocked || 0;
        const total = queries.total || 0;
        const history = (stats.history || []).filter((point) => typeof point.t === "number").map((point) => ({
            t: point.t, v: point.total ? (point.blocked || 0) / point.total : 0
        }));
        return el("div", { class: "widget-pihole-stats" }, [
            donut([
                { label: "Blocked", value: blocked, tone: "alarm" },
                { label: "Allowed", value: Math.max(0, total - blocked) }
            ],
                {
                    label: "Today's queries, blocked and allowed", centre: percent(total ? blocked / total : null),
                    caption: "blocked"
                }),
            el("dl", { class: "widget-facts" }, [
                el("dt", { text: "Queries" }), el("dd", { text: total.toLocaleString() }),
                el("dt", { text: "Clients" }), el("dd", { text: String(queries.clients ?? "—") }),
                el("dt", { text: "On lists" }), el("dd", { text: (queries.domains_on_lists ?? 0).toLocaleString() }),
                el("dt", { text: "Blocking" }), el("dd", { text: stats.blocking || "—" }),
                el("dt", { text: "Version" }), el("dd", { text: `${stats.api}${stats.version ? ` ${stats.version}` : ""}` }),
            ]),
            history.length > 1 ? el("div", { class: "widget-pihole-history" }, [el(
                "span", { class: "widget-muted", text: "blocked share" }
            ),
                area(
                    history,
                    {
                        field: "v", label: "Share of queries blocked over the day", compact: true,
                        max: Math.max(0.05, ...history.map((point) => point.v))
                    }
                )]) : null,
            (stats.top_blocked || []).length
                ? el("ol", { class: "widget-top-blocked" }, stats.top_blocked.slice(0, 5).map((row) => el("li", {}, [
                el(
                    "span", { text: row.name }
                ), el("span", { class: "widget-muted", text: ` ${(row.count || 0).toLocaleString()}` })]))) : null,
        ]);
    }

    // ------------------------------------------------------------------
    // Treemap and Attention
    // ------------------------------------------------------------------

    /**
     * A two-level treemap: each group a block sized by its members, each
     * member a tile sized by its value, optionally with a fill bar along its
     * foot (such as memory in use) and dimmed (such as stopped).
     * @param {Array<{label: string, children: Array<object>}>} groups Groups;
     *     children have `label`, `value`, optional `detail` (second line),
     *     `fill` (0..1 foot bar), `dim`, and `tip` lines.
     * @param {object} [options] `label`, `format`, `width` and `height` (the
     *     drawing's proportions, default 1000 × 560).
     * @returns {HTMLElement} The map.
     */
    function treemap(groups, { label = "Treemap", format = String, width = 1000, height = 560 } = {}) {
        const chart = svg(
            "svg",
            { class: "widget-treemap-svg", viewBox: `0 0 ${width} ${height}`, role: "group", "aria-label": label }
        );
        const blocks = (groups || []).map((group) => ({
            group, value: sum(group.children || [], (child) => child.value)
        }))
            .sort((a, b) => b.value - a.value);
        for (const block of squarify(blocks, { x: 0, y: 0, w: width, h: height })) {
            const { group } = block.item;
            const pad = 3;
            const inner = {
                x: block.x + pad, y: block.y + 20, w: Math.max(0, block.w - pad * 2), h: Math.max(0, block.h - 20 - pad)
            };
            chart.append(svg(
                "rect",
                {
                    class: "widget-treemap-group", x: block.x + 1, y: block.y + 1, width: Math.max(0, block.w - 2),
                    height: Math.max(0, block.h - 2), rx: 6
                }
            ));
            text(
                chart, { class: "widget-treemap-group-label", x: block.x + 8, y: block.y + 14 },
                block.w > 60 ? `${group.label} · ${format(block.item.value)}` : ""
            );
            const tiles = (group.children || []).map((child) => ({ ...child })).sort((a, b) => b.value - a.value);
            for (const tile of squarify(tiles, inner)) {
                const child = tile.item;
                const g = svg("g", { class: `widget-treemap-tile${child.dim ? " is-dim" : ""}`, tabindex: 0 });
                g.append(svg(
                    "rect",
                    {
                        x: tile.x + 1, y: tile.y + 1, width: Math.max(0, tile.w - 2), height: Math.max(0, tile.h - 2),
                        rx: 4
                    }
                ));
                if (isNumber(child.fill) && tile.h > 26) {
                    g.append(svg(
                        "rect",
                        {
                            class: "widget-treemap-use", x: tile.x + 4, y: tile.y + tile.h - 7,
                            width: Math.max(0, (tile.w - 8) * Math.min(1, child.fill)), height: 3, rx: 1.5
                        }
                    ));
                }
                if (tile.w > 46 && tile.h > 18) {
                    const name = String(child.label);
                    text(g, { class: "widget-treemap-label", x: tile.x + 6, y: tile.y + 15 },
                        name.length * 7 > tile.w - 10
                            ? `${name.slice(0, Math.max(1, Math.floor((tile.w - 16) / 7)))}…`
                            : name);
                    if (tile.h > 40 && child.detail) {
                        text(g, { class: "widget-treemap-detail", x: tile.x + 6, y: tile.y + 29 }, child.detail);
                    }
                }
                tip(g, child.tip || [child.label, `${format(child.value)} in ${group.label}`]);
                chart.append(g);
            }
        }
        return el("div", { class: "widget-treemap" }, [chart]);
    }

    /**
     * An Attention list: a lamp and a linked sentence per item, worst first.
     * @param {Array<{tone: "alarm"|"warn"|"info", text: string, href?: string}>} items Items.
     * @param {object} [options] `title` (wrap in a panel with this heading
     *     and a count aside; default "Attention"; null for the list alone),
     *     `hue`, `clear` (text when there is nothing), `limit` (most items
     *     shown; the rest are counted in a last line), and `more` (where
     *     that line links).
     * @returns {HTMLElement} The panel, or the list.
     */
    function attention(
        items,
        { title = "Attention", hue = null, clear = "Nothing needs attention.", limit = Infinity, more = null } = {}
    ) {
        const order = { alarm: 0, warn: 1, info: 2 };
        const sorted = [...(items || [])].sort((a, b) => (order[a.tone] ?? 3) - (order[b.tone] ?? 3));
        const counts = { alarm: 0, warn: 0, info: 0 };
        for (const item of sorted) {
            counts[item.tone] = (counts[item.tone] || 0) + 1;
        }
        const shown = sorted.slice(0, limit);
        const hiddenCount = sorted.length - shown.length;
        const rest = hiddenCount > 0 ? el("li", { class: "widget-attention-more" }, [
            more ? el("a", { href: more, text: `${hiddenCount} more →` }) : el("span", { text: `${hiddenCount} more` }),
        ]) : null;
        const body = sorted.length
            ? el("ul", { class: "widget-attention" }, [...shown.map((item) => el("li", { class: `is-${item.tone}` }, [
            lamp(
                item.tone === "info" ? "off" : item.tone,
                { alarm: "Alert", warn: "Caution", info: "Note" }[item.tone] || "Note"
            ),
            item.href ? el("a", { href: item.href, text: item.text }) : el("span", { text: item.text }),
        ])), rest]) : el(
            "p", { class: "widget-attention-clear" }, [lamp("on", "All clear"), el("span", { text: clear })]
        );
        if (title === null) {
            return body;
        }
        const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;
        return panel(title, [body], {
            hue, className: "widget-attention-panel",
            aside: sorted.length ? `${plural(counts.alarm, "alert")} · ${plural(counts.warn, "caution")} · ${plural(counts.info, "note")}` : "all clear",
        });
    }

    // ------------------------------------------------------------------
    // Declarative mounting
    // ------------------------------------------------------------------

    /** Widgets drawable by name from markup: each `(data, options)`. */
    const WIDGETS = {
        area, sparkline, mirror, multiples, radialClock, timeline, donut, pie, sunburst, segments,
        bars, allocation, splitBars, columns, columnGroups, treemap, attention, sinkholes,
        stats: (data, options) => stats(data, options),
        stat: (data) => statTile(data),
    };

    /** Named value formatters, for options given as JSON. */
    const FORMATS = { bytes, rate, percent, number, uptime, text: String };

    /** Registered feeds: name → function(json, element) → widget input. */
    const FEEDS = new Map();

    /**
     * Register a feed: a function that turns an API response into a
     * widget's input, so markup can name it in `data-widget-feed`.
     * @param {string} name Feed name, such as "cluster.guest-states".
     * @param {Function} feed `(json, element) => {widget?, data, options?,
     *     title?, aside?}`; `widget` overrides the element's `data-widget`.
     */
    function register(name, feed) {
        FEEDS.set(name, feed);
    }

    /**
     * Resolve formatter names in options given as JSON.
     * @param {object} options Options.
     * @returns {object} Options with `format` a function.
     */
    function resolveOptions(options) {
        const resolved = { ...(options || {}) };
        if (typeof resolved.format === "string") {
            resolved.format = FORMATS[resolved.format] || String;
        }
        return resolved;
    }

    /**
     * Draw one widget by name, inside a panel when a title is given.
     * @param {string} name Widget name (a key of WIDGETS).
     * @param {*} data Widget data.
     * @param {object} [options] Widget options.
     * @param {object} [frame] `title`, `aside`, `hue` for a wrapping panel.
     * @returns {HTMLElement} The widget.
     */
    function render(name, data, options = {}, frame = {}) {
        const draw = WIDGETS[name];
        if (!draw) {
            return empty(`Unknown widget “${name}”.`);
        }
        const node = draw(data, resolveOptions(options));
        if (frame.title && name !== "attention") {
            return panel(frame.title, [node], { hue: frame.hue || null, aside: frame.aside || null });
        }
        if (frame.hue && !node.dataset.hue) {
            node.dataset.hue = frame.hue;
        }
        return node;
    }

    /**
     * Draw every `[data-widget]` or `[data-widget-feed]` element under a
     * root. Each element takes either `data-widget` (name) and a child
     * `<script type="application/json">` with `{data, options}`, or
     * `data-widget-src` (an API address) and `data-widget-feed` (a
     * registered feed, which names the widget); optional `data-widget-title`,
     * `data-widget-aside`, `data-widget-hue`, and `data-widget-refresh`
     * (seconds between redraws from the source).
     * @param {ParentNode} [root] Where to look (default the document).
     * @returns {Promise<void>} Resolves when every widget has been drawn.
     */
    async function mount(root = document) {
        const fetches = new Map();
        const load = (url) => {
            if (!fetches.has(url)) {
                fetches.set(url, labratJson(url));
            }
            return fetches.get(url);
        };
        const targets = [...root.querySelectorAll("[data-widget], [data-widget-feed]")].filter((node) => !node.dataset.widgetState);
        await Promise.all(targets.map((node) => draw(node, load)));
    }

    /**
     * Draw one declarative widget element, and schedule its refresh.
     * @param {HTMLElement} node The element.
     * @param {Function} load Fetches JSON by address (shared per mount).
     * @returns {Promise<void>} Resolves when drawn.
     */
    async function draw(node, load) {
        const frame = { title: node.dataset.widgetTitle, aside: node.dataset.widgetAside, hue: node.dataset.widgetHue };
        const inline = node.querySelector(":scope > script[type='application/json']");
        node.dataset.widgetState = "loading";
        try {
            let spec;
            if (inline) {
                spec = JSON.parse(inline.textContent || "{}");
            } else {
                const feed = FEEDS.get(node.dataset.widgetFeed || "");
                if (!feed) {
                    throw new Error(`No feed named “${node.dataset.widgetFeed || ""}”`);
                }
                spec = feed(await load(node.dataset.widgetSrc), node) || {};
            }
            const name = spec.widget || node.dataset.widget;
            const drawn = render(
                name, spec.data, spec.options,
                { ...frame, title: frame.title ?? spec.title, aside: frame.aside ?? spec.aside }
            );
            node.replaceChildren(...(inline ? [inline] : []), drawn);
            node.dataset.widgetState = "ready";
            delete node.dataset.widgetError;
        } catch (error) {
            node.dataset.widgetError = String(error && error.message ? error.message : error);
            node.replaceChildren(...(inline ? [inline] : []), frame.title ? panel(
                frame.title, [empty("Could not load this widget.")], { hue: frame.hue || null }
            )
                : empty("Could not load this widget."));
            node.dataset.widgetState = "error";
        }
        const every = Number(node.dataset.widgetRefresh);
        if (!inline && every >= 30 && !node.dataset.widgetTimer) {
            node.dataset.widgetTimer = "on";
            window.setInterval(() => {
                if (document.visibilityState === "visible" && node.isConnected) {
                    const fresh = new Map();
                    draw(node, (url) => {
                        if (!fresh.has(url)) {
                            fresh.set(url, labratJson(url));
                        }
                        return fresh.get(url);
                    });
                }
            }, every * 1000);
        }
    }

    window.LabratWidgets = {
        // Builders and formatters
        el, svg, bytes, rate, percent, number, uptime, ago, clock, sum, tally, severity,
        // Pieces
        tip, lamp, meter, key, empty, panel, statTile, stats, row,
        // Charts
        area, sparkline, mirror, multiples, radialClock, timeline, donut, pie, sunburst, segments,
        bars, allocation, splitBars, columns, columnGroups, treemap, attention, sinkholes,
        // Declarative use
        register, render, mount, WIDGETS, FORMATS, TONES,
        // Pure helpers, for tests and page code
        internals: { niceCeil, fractionCeil, timeTicks, foldSlices, segment, squarify },
    };

    // Draw markup widgets once every deferred script (feeds included) has
    // run: DOMContentLoaded fires after them. A kit loaded later than that
    // draws on the next task.
    if (document.readyState === "complete") {
        window.setTimeout(() => mount(), 0);
    } else {
        document.addEventListener("DOMContentLoaded", () => mount(), { once: true });
    }
})();
