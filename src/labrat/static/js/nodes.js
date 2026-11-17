// Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
/**
 * @file The Cluster page's Nodes tab (templates/private/cluster.html).
 *
 * One subtab per node (the spread-open strip in the view switch). Each is
 * a single-screen engineering readout: a core
 * gauge (CPU load, memory, vCPU commitment) ringed by subsystem modules
 * for memory, the boot drive, data drives, network, accelerators, and
 * load, over 24-hour CPU and memory traces and the node's guests.
 * cluster.js fetches /api/v1/cluster and calls window.LabratNodes.draw;
 * the shared drawing helpers are in proxmox-ui.js.
 */
(() => {
    "use strict";

    const W = window.LabratWidgets;
    const {
        el, svg, bytes, percent, uptime, sum, lamp, tip, meter, severity, area, donut, rate,
    } = W;

    // Disk thresholds and tones are shared with the ZFS shelf (cluster-model.js).
    const { tempTone, wearTone, diskTone, WEAR_WARN, WEAR_ALARM, TEMP_WARN, TEMP_ALARM } = window.LabratClusterModel;

    /**
     * A subsystem module: a bracketed frame with a code, a title, and content.
     * @param {string} code Short subsystem code, such as "SYS-02".
     * @param {string} title Module title.
     * @param {Array<Node>} children Content.
     * @param {object} [options] `tone` for the status lamp, `aside` text, `className`.
     * @returns {HTMLElement} The module.
     */
    function module(code, title, children, { tone = null, aside = null, className = "" } = {}) {
        return el("section", { class: `diag-module ${className}` }, [
            el("header", { class: "diag-module-heading" }, [
                el("span", { class: "diag-code", text: code }),
                el("h2", { text: title }),
                aside ? el("span", { class: "diag-aside", text: aside }) : null,
                tone ? lamp(tone, { on: "Nominal", warn: "Caution", alarm: "Alert", off: "No reading" }[tone]) : null,
            ]),
            ...children,
        ]);
    }

    /**
     * Two-column readout pairs.
     * @param {Array<[string, string]>} rows Label and value pairs.
     * @returns {HTMLElement} The list.
     */
    function readout(rows) {
        return el("dl", { class: "diag-readout" }, rows.filter(Boolean).flatMap(([label, value]) => [
            el("dt", { text: label }), el("dd", { text: value }),
        ]));
    }

    /**
     * An SVG arc path on a circle, clockwise from 12 o'clock.
     * @param {number} cx Centre x.
     * @param {number} cy Centre y.
     * @param {number} r Radius.
     * @param {number} from Start, 0..1 of the turn.
     * @param {number} to End, 0..1 of the turn.
     * @returns {string} Path data.
     */
    function arc(cx, cy, r, from, to) {
        const span = Math.max(0, Math.min(0.9999, to - from));
        const a0 = (from * 2 - 0.5) * Math.PI;
        const a1 = ((from + span) * 2 - 0.5) * Math.PI;
        const large = span > 0.5 ? 1 : 0;
        return `M${(cx + r * Math.cos(a0)).toFixed(2)},${(cy + r * Math.sin(a0)).toFixed(2)}A${r},${r} 0 ${large} 1 ${(cx + r * Math.cos(a1)).toFixed(2)},${(cy + r * Math.sin(a1)).toFixed(2)}`;
    }

    // ------------------------------------------------------------------
    // The core gauge
    // ------------------------------------------------------------------

    /**
     * The core: CPU load as 60 lit ticks, memory (in use, then allocated)
     * and vCPU commitment as rings, the load figure in the middle, a
     * legend for the rings under it, and a sweep that makes one turn per
     * data refresh (owner: a wedge that means something), so its place
     * shows the time to the next update.
     * @param {object} node Node view.
     * @param {object} facts Derived facts (vcpus, allocated).
     * @param {{refreshMs?: number, loadedAt?: number}} [refresh] The page's
     *     refresh period and when its data last loaded (ms since the epoch).
     * @returns {HTMLElement} The core figure.
     */
    function core(node, facts, refresh = {}) {
        const size = 420;
        const c = size / 2;
        const chart = svg("svg", { class: "diag-core-svg", viewBox: `0 0 ${size} ${size}`, role: "img",
            "aria-label": `CPU ${percent(node.cpu)}, memory ${percent(node.maxmem ? node.mem / node.maxmem : null)}, ${facts.vcpus} of ${node.maxcpu} vCPUs assigned` });
        // Frame rings and the sweep.
        chart.append(svg("circle", { class: "diag-ring-frame", cx: c, cy: c, r: 200 }));
        chart.append(svg("circle", { class: "diag-ring-frame is-faint", cx: c, cy: c, r: 120 }));
        const sweep = svg("g", { class: "diag-sweep" });
        sweep.append(svg("path", { class: "diag-sweep-wedge", d: `M${c},${c}L${c},${c - 196}${arc(c, c, 196, 0, 0.08).replace(/^M[^A]+/, "")}Z` }));
        // One turn per refresh, phased to when the data loaded, so a redraw
        // for any other reason keeps its place; it rests at the top when an
        // update is late (one iteration, kept at its end).
        if (refresh.refreshMs) {
            sweep.style.animationDuration = `${refresh.refreshMs / 1000}s`;
            sweep.style.animationDelay = `${-Math.max(0, Date.now() - (refresh.loadedAt || Date.now())) / 1000}s`;
        } else {
            sweep.classList.add("is-idle");
        }
        chart.append(sweep);
        // CPU: 60 ticks, lit up to the load.
        const lit = Math.round((node.cpu || 0) * 60);
        const cpuTone = severity(node.cpu || 0);
        for (let index = 0; index < 60; index += 1) {
            const angle = (index / 60) * 2 * Math.PI - Math.PI / 2;
            const inner = 172;
            const outer = index % 5 === 0 ? 192 : 188;
            chart.append(svg("line", {
                class: `diag-tick${index < lit ? ` is-lit ${cpuTone}` : ""}`,
                x1: c + inner * Math.cos(angle), y1: c + inner * Math.sin(angle),
                x2: c + outer * Math.cos(angle), y2: c + outer * Math.sin(angle),
            }));
        }
        // Memory ring: in use, then allocated to running guests.
        const memTotal = Math.max(node.maxmem || 0, facts.allocated);
        const used = memTotal ? (node.mem || 0) / memTotal : 0;
        const allocated = memTotal ? facts.allocated / memTotal : 0;
        chart.append(svg("circle", { class: "diag-track", cx: c, cy: c, r: 154 }));
        if (allocated > 0) {
            chart.append(svg("path", { class: "diag-arc is-reserved", d: arc(c, c, 154, 0, Math.min(1, allocated)) }));
        }
        chart.append(svg(
            "path",
            {
                class: `diag-arc ${severity(node.maxmem ? node.mem / node.maxmem : 0)}`,
                d: arc(c, c, 154, 0, Math.max(0.002, used))
            }
        ));
        // vCPU ring: assigned against threads; past 100% it laps in amber.
        const ratio = node.maxcpu ? facts.vcpus / node.maxcpu : 0;
        chart.append(svg("circle", { class: "diag-track", cx: c, cy: c, r: 136 }));
        chart.append(svg(
            "path", { class: "diag-arc is-thin", d: arc(c, c, 136, 0, Math.max(0.002, Math.min(1, ratio))) }
        ));
        if (ratio > 1) {
            chart.append(svg(
                "path", { class: "diag-arc is-thin is-warning", d: arc(c, c, 136, 0, Math.min(1, ratio - 1)) }
            ));
        }
        // Centre readout, in the gauge's own units so it stays readable when
        // the gauge is drawn small; the rings' readings are in the legend.
        const caption = svg("text", { class: "diag-core-caption", x: c, y: c - 44, "text-anchor": "middle" });
        caption.textContent = "CORE LOAD";
        const big = svg("text", { class: "diag-core-figure", x: c, y: c + 22, "text-anchor": "middle" });
        big.textContent = percent(node.cpu);
        const under = svg("text", { class: "diag-core-caption", x: c, y: c + 66, "text-anchor": "middle" });
        under.textContent = `${node.cores ?? "?"}C / ${node.threads ?? node.maxcpu ?? "?"}T`;
        chart.append(caption, big, under);
        // The rings, outside in, named by shape (they share one colour).
        const legend = el("div", { class: "diag-core-legend" }, [
            ["is-ticks", "CPU", percent(node.cpu), "outer ticks"],
            ["is-thick", "Mem", percent(node.maxmem ? node.mem / node.maxmem : null), "middle ring"],
            ["is-thin", "vCPU", node.maxcpu ? `${ratio.toFixed(2)}×` : "—", "inner ring"],
        ].map(([shape, name, value, where]) => el("span", { class: "diag-core-key", title: `${name}: the ${where}` }, [
            el(
                "span", { class: `diag-core-swatch ${shape}`, "aria-hidden": "true" }
            ), el("span", { text: name }), el("strong", { text: value }),
        ])));
        const figure = el("figure", { class: "diag-core", tabindex: 0 }, [chart, legend,
            el(
                "figcaption",
                {
                    class: "diag-core-model", title: node.cpu_model || "",
                    text: node.cpu_model || "CPU model not reported"
                }
            )]);
        tip(figure, [`${node.name} core`, `CPU ${percent(node.cpu)} of ${node.maxcpu} threads`,
            `Memory ${bytes(node.mem)} in use, ${bytes(facts.allocated)} allocated, ${bytes(node.maxmem)} installed`,
            `${facts.vcpus} vCPUs assigned to running guests (${ratio.toFixed(2)}×)`,
            refresh.refreshMs
                ? `The sweep turns once per ${refresh.refreshMs / 1000} s refresh: where it points shows the time to the next update.`
                : ""].filter(Boolean));
        return figure;
    }

    /**
     * The second gauge (owner: the 24-hour clock beside the core gauge):
     * the node's CPU by time of day from its collected history, midnight
     * at the top and the dashed hand at now, with its peak and mean below.
     * @param {object} node Node view.
     * @returns {HTMLElement} The clock figure.
     */
    function clock(node) {
        const series = (node.history || []).filter((point) => typeof point.cpu === "number");
        const values = series.map((point) => point.cpu);
        const legend = el("div", { class: "diag-core-legend" }, [
            ["Now", values.length ? percent(values[values.length - 1]) : "—"],
            ["Peak", values.length ? percent(Math.max(...values)) : "—"],
            ["Mean", values.length ? percent(values.reduce((total, value) => total + value, 0) / values.length) : "—"],
        ].map(([name, value]) => el(
            "span", { class: "diag-core-key" }, [el("span", { text: name }), el("strong", { text: value })]
        )));
        return el("figure", { class: "diag-core diag-clock" }, [
            W.radialClock(series, { field: "cpu", label: `${node.name} CPU by time of day` }),
            legend,
            el("figcaption", { class: "diag-core-model", text: "CPU · 24 h · midnight at top" }),
        ]);
    }

    // ------------------------------------------------------------------
    // Modules
    // ------------------------------------------------------------------

    /**
     * Memory as a strip of cells: in use, allocated, free.
     * @param {object} node Node view.
     * @param {object} facts Derived facts.
     * @returns {HTMLElement} The module.
     */
    function memoryModule(node, facts) {
        const cells = 40;
        const total = Math.max(node.maxmem || 0, facts.allocated) || 1;
        const usedCells = Math.round(((node.mem || 0) / total) * cells);
        const allocCells = Math.round((facts.allocated / total) * cells);
        const strip = el(
            "div",
            {
                class: "diag-cells", role: "img",
                "aria-label": `${bytes(node.mem)} in use, ${bytes(facts.allocated)} allocated, ${bytes(node.maxmem)} installed`
            }
        );
        for (let index = 0; index < cells; index += 1) {
            strip.append(el("span", { class: `diag-cell${index < usedCells ? " is-used" : index < allocCells ? " is-reserved" : ""}` }));
        }
        const fraction = node.maxmem ? node.mem / node.maxmem : 0;
        return module("SYS-02", "Memory", [strip,
            el("p", { class: "diag-legend" }, [
                el("span", { class: "diag-key is-used" }), el("span", { text: "in use" }),
                el("span", { class: "diag-key is-reserved" }), el("span", { text: "allocated" }),
                el("span", { class: "diag-key" }), el("span", { text: "free" }),
            ]),
            readout([
                ["Installed", bytes(node.maxmem)], ["In use", `${bytes(node.mem)} (${percent(fraction)})`],
                ["Allocated", `${bytes(facts.allocated)} to running guests`],
                ["Swap", node.swap_total ? `${bytes(node.swap_used)} of ${bytes(node.swap_total)}` : "none"],
            ]),
        ], { tone: fraction >= 0.9 ? "alarm" : fraction >= 0.8 ? "warn" : "on" });
    }

    /**
     * A drawing of a drive: an M.2 stick for NVMe, a 2.5" or 3.5" body otherwise.
     * @param {object} disk Disk record.
     * @returns {SVGElement} The drawing.
     */
    function driveArt(disk) {
        const tone = diskTone(disk);
        if (disk.type === "nvme") {
            const art = svg(
                "svg", { class: `diag-drive-art is-${tone}`, viewBox: "0 0 160 40", "aria-hidden": "true" }
            );
            art.append(svg("rect", { class: "diag-board", x: 2, y: 6, width: 150, height: 28, rx: 3 }));
            art.append(svg("rect", { class: "diag-notch", x: 150, y: 14, width: 8, height: 12, rx: 1 }));
            for (let x = 0; x < 9; x += 1) {
                art.append(svg(
                    "rect", { class: "diag-pin", x: 151 + (x % 2) * 0, y: 8 + x * 3, width: 6, height: 1.5 }
                ));
            }
            art.append(svg("rect", { class: "diag-chip is-ctrl", x: 12, y: 11, width: 26, height: 18, rx: 2 }));
            art.append(svg("rect", { class: "diag-chip", x: 48, y: 11, width: 36, height: 18, rx: 2 }));
            art.append(svg("rect", { class: "diag-chip", x: 92, y: 11, width: 36, height: 18, rx: 2 }));
            art.append(svg("circle", { class: "diag-screw", cx: 6, cy: 20, r: 3 }));
            art.append(svg("circle", { class: "diag-led", cx: 140, cy: 12, r: 2.5 }));
            return art;
        }
        const art = svg("svg", { class: `diag-drive-art is-${tone}`, viewBox: "0 0 160 40", "aria-hidden": "true" });
        art.append(svg("rect", { class: "diag-board", x: 2, y: 2, width: 156, height: 36, rx: 4 }));
        if (disk.type === "hdd") {
            art.append(svg("circle", { class: "diag-platter", cx: 52, cy: 20, r: 15 }));
            art.append(svg("circle", { class: "diag-spindle", cx: 52, cy: 20, r: 3 }));
            art.append(svg("path", { class: "diag-arm", d: "M118,30L64,22" }));
        } else {
            art.append(svg("rect", { class: "diag-chip", x: 20, y: 10, width: 40, height: 20, rx: 2 }));
            art.append(svg("rect", { class: "diag-chip", x: 70, y: 10, width: 40, height: 20, rx: 2 }));
        }
        art.append(svg("circle", { class: "diag-led", cx: 146, cy: 10, r: 2.5 }));
        return art;
    }

    /**
     * A half-dial of SSD wear, 0..100% of rated life used.
     * @param {number|null} worn Percent used.
     * @returns {SVGElement} The dial.
     */
    function wearDial(worn) {
        const dial = svg(
            "svg",
            {
                class: "diag-dial", viewBox: "0 0 120 70", role: "img",
                "aria-label": typeof worn === "number" ? `${worn}% of rated life used` : "Wear not reported"
            }
        );
        const half = (from, to) => {
            const a0 = Math.PI * (1 + from);
            const a1 = Math.PI * (1 + to);
            return `M${60 + 48 * Math.cos(a0)},${60 + 48 * Math.sin(a0)}A48,48 0 0 1 ${60 + 48 * Math.cos(a1)},${60 + 48 * Math.sin(a1)}`;
        };
        dial.append(svg("path", { class: "diag-dial-track", d: half(0, 1) }));
        dial.append(svg("path", { class: "diag-dial-zone is-warning", d: half(WEAR_WARN / 100, WEAR_ALARM / 100) }));
        dial.append(svg("path", { class: "diag-dial-zone is-alarm", d: half(WEAR_ALARM / 100, 1) }));
        if (typeof worn === "number") {
            const value = Math.min(100, Math.max(0, worn)) / 100;
            dial.append(svg(
                "path", { class: `diag-dial-fill is-${wearTone(worn)}`, d: half(0, Math.max(0.005, value)) }
            ));
            const angle = Math.PI * (1 + value);
            dial.append(svg(
                "line",
                {
                    class: "diag-dial-needle", x1: 60, y1: 60, x2: 60 + 40 * Math.cos(angle),
                    y2: 60 + 40 * Math.sin(angle)
                }
            ));
        }
        dial.append(svg("circle", { class: "diag-dial-hub", cx: 60, cy: 60, r: 4 }));
        const text = svg("text", { class: "diag-dial-figure", x: 60, y: 50, "text-anchor": "middle" });
        text.textContent = typeof worn === "number" ? `${worn}%` : "—";
        dial.append(text);
        return dial;
    }

    /**
     * The boot drive: drawing, wear dial, capacity left, and SMART counters.
     * @param {object} node Node view.
     * @returns {HTMLElement} The module.
     */
    function bootModule(node) {
        const boots = (node.disks || []).filter((disk) => disk.boot);
        if (!boots.length) {
            return module(
                "SYS-03", "Boot drive",
                [el(
                    "p",
                    {
                        class: "diag-muted",
                        text: node.disks ? "No boot disk identified." : "Disk details not collected yet."
                    }
                )],
                { tone: "off" }
            );
        }
        const root = node.rootfs_total ? node.rootfs_used / node.rootfs_total : null;
        const lvm = (node.storage || []).find((item) => item.storage === "local-lvm" && item.total);
        // More than one drive with a boot partition (a ZFS mirror, or a
        // spare that was once installed to; the disk list cannot tell which
        // holds root) draws each compactly, its name over its readings with
        // life used among them, so the box stays one screen's height.
        const compact = boots.length > 1;
        const parts = boots.map((disk) => {
            const smart = disk.smart || {};
            const name = el("p", { class: "diag-drive-name", title: disk.model || "", text: `${disk.model || "Unknown"} · ${bytes(disk.size)}` });
            return el("div", { class: `diag-boot${compact ? " is-compact" : ""}` }, [
                ...(compact ? [name] : [
                    el("div", { class: "diag-boot-art" }, [driveArt(disk), name]),
                    el(
                        "div", { class: "diag-boot-dial" },
                        [
                            wearDial(disk.worn_percent),
                            el(
                                "p",
                                {
                                    class: "diag-dial-caption",
                                    text: disk.type === "hdd" ? "spinning disk" : "life used"
                                }
                            )
                        ]
                    ),
                ]),
                readout([
                    ["Health", disk.health || "—"],
                    compact && disk.type !== "hdd" ? [
                        "Life used", typeof disk.worn_percent === "number" ? `${disk.worn_percent}%` : "—"
                    ] : null,
                    ["Temp", typeof smart.temperature_c === "number" ? `${smart.temperature_c} °C` : "—"],
                    [
                        "Powered",
                        typeof smart.power_on_hours === "number" ? `${Math.round(smart.power_on_hours / 24)} d` : "—"
                    ],
                    ["Written", typeof smart.written_bytes === "number" ? bytes(smart.written_bytes) : "—"],
                    smart.spare_percent !== undefined ? ["Spare", `${smart.spare_percent}%`] : null,
                    smart.media_errors !== undefined ? ["Media errors", String(smart.media_errors)] : null,
                ]),
            ]);
        });
        const capacity = el("div", { class: "diag-capacity" }, [
            el("div", { class: "diag-capacity-row" }, [el("span", { class: "diag-key-label", text: "Root" }),
                meter({
                    total: node.rootfs_total || 0, used: node.rootfs_used || 0, label: `Root ${percent(root)} used`,
                    tone: severity(root || 0)
                }),
                el(
                    "span",
                    { class: "diag-figure", text: `${bytes((node.rootfs_total || 0) - (node.rootfs_used || 0))} left` }
                )]),
            lvm ? el("div", { class: "diag-capacity-row" }, [el("span", { class: "diag-key-label", text: "LVM-thin" }),
                meter({
                    total: lvm.total, used: lvm.used || 0, label: `local-lvm ${percent(lvm.used / lvm.total)} used`,
                    tone: severity(lvm.used / lvm.total)
                }),
                el("span", { class: "diag-figure", text: `${bytes(lvm.total - (lvm.used || 0))} left` })]) : null,
        ]);
        const tones = boots.map(diskTone);
        return module("SYS-03", boots.length > 1 ? "Boot drives" : "Boot drive", [...parts, capacity], {
            tone: tones.includes("alarm") ? "alarm" : tones.includes("warn") ? "warn" : "on",
            aside: boots.length > 1 ? `${boots.length} with boot partitions` : boots[0].path.replace("/dev/", ""),
            className: "diag-module-boot",
        });
    }

    /**
     * Data drives as bays: art, size, role, lamp, wear and temperature.
     * @param {object} node Node view.
     * @param {object} source The cluster source, for the shared bay strip.
     * @returns {HTMLElement} The module.
     */
    function drivesModule(node, source) {
        const disks = (node.disks || []).filter((disk) => !disk.boot);
        if (!disks.length) {
            return module(
                "SYS-04", "Data drives",
                [el(
                    "p",
                    { class: "diag-muted", text: node.disks ? "No other drives." : "Disk details not collected yet." }
                )],
                { tone: "off" }
            );
        }
        // Compact hot-swap bays, the rack view's and the Storage overview's
        // (owner: the node screen fits one laptop screen), when the page
        // loads the shelf widget and model; else the drawn drive cards.
        const box = window.LabratShelfModel
            && W.bayStrip
            ? window.LabratShelfModel.chassisByNode(source).get(node.name)
            : null;
        if (box) {
            const bays = box.groups.filter((group) => group.name !== "Boot").flatMap((group) => group.bays);
            const worst = W.shelfInternals.worst(bays.flatMap((bay) => bay.issues || []));
            return module("SYS-04", "Data drives", [W.bayStrip(bays)], {
                tone: worst, aside: `${disks.length} · ${bytes(sum(disks, (disk) => disk.size))}`, className: "diag-module-drives",
            });
        }
        const bays = el("div", { class: "diag-bays" }, disks.map((disk, index) => {
            const smart = disk.smart || {};
            const tone = diskTone(disk);
            const role = disk.osds && disk.osds.length ? `OSD ${disk.osds.join(", ")}` : disk.used || "unused";
            const bay = el("article", { class: `diag-bay is-${tone}`, tabindex: 0 }, [
                el("header", { class: "diag-bay-heading" }, [lamp(tone, disk.health || "unknown"), el("span", { class: "diag-code", text: `BAY ${String(index + 1).padStart(2, "0")}` }), el("span", { class: "diag-bay-type", text: disk.type || "" })]),
                driveArt(disk),
                el("p", { class: "diag-bay-size", text: bytes(disk.size) }),
                el("p", { class: "diag-bay-role", text: role }),
                typeof disk.worn_percent === "number" ? meter({
                    total: 100, used: disk.worn_percent, label: `${disk.worn_percent}% worn`,
                    tone: disk.worn_percent >= WEAR_ALARM
                        ? "is-alarm"
                        : disk.worn_percent >= WEAR_WARN
                        ? "is-warning"
                        : ""
                }) : null,
                el("p", { class: "diag-bay-foot" }, [
                    el(
                        "span",
                        { text: typeof disk.worn_percent === "number"
                            ? `${disk.worn_percent}% worn`
                            : disk.rpm
                            ? `${disk.rpm} rpm`
                            : "" }
                    ),
                    el(
                        "span",
                        {
                            class: `diag-temp is-${tempTone(disk)}`,
                            text: typeof smart.temperature_c === "number" ? `${smart.temperature_c}°` : ""
                        }
                    ),
                ]),
            ]);
            tip(bay, [`${disk.model || "Disk"} (${disk.path})`, `${bytes(disk.size)} ${disk.type}${disk.rpm ? `, ${disk.rpm} rpm` : ""}`, `Health ${disk.health || "unknown"} · ${role}`,
                typeof disk.worn_percent === "number" ? `${disk.worn_percent}% of rated life used` : "",
                typeof smart.temperature_c === "number" ? `${smart.temperature_c} °C` : "",
                typeof smart.power_on_hours === "number"
                    ? `${smart.power_on_hours.toLocaleString()} hours powered`
                    : "",
                typeof smart.written_bytes === "number" ? `${bytes(smart.written_bytes)} written` : "",
                smart.reallocated_sectors ? `${smart.reallocated_sectors} reallocated sectors` : "",
                smart.pending_sectors ? `${smart.pending_sectors} pending sectors` : ""].filter(Boolean));
            return bay;
        }));
        const tones = disks.map(diskTone);
        return module("SYS-04", "Data drives", [bays], {
            tone: tones.includes("alarm") ? "alarm" : tones.includes("warn") ? "warn" : "on",
            aside: `${disks.length} · ${bytes(sum(disks, (disk) => disk.size))}`,
            className: "diag-module-drives",
        });
    }

    /**
     * Network: a jack per physical NIC with its link lamp and speed, then bonds.
     * @param {object} node Node view.
     * @returns {HTMLElement} The module.
     */
    function networkModule(node) {
        const nics = node.nics || [];
        const up = nics.filter((nic) => nic.link_up).length;
        const jacks = el("div", { class: "diag-jacks" }, nics.map((nic) => {
            const jack = el("div", { class: `diag-jack${nic.link_up ? " is-up" : ""}`, tabindex: 0 }, [
                svg2Jack(nic),
                el("span", { class: "diag-jack-name", text: nic.name }),
                el(
                    "span",
                    {
                        class: "diag-jack-speed",
                        text: nic.speed_mbps
                            ? (nic.speed_mbps >= 1000 ? `${nic.speed_mbps / 1000}G` : `${nic.speed_mbps}M`)
                            : nic.link_up
                            ? "up"
                            : "down"
                    }
                ),
            ]);
            tip(
                jack,
                [
                    nic.name, nic.link_up ? "Link up" : "No link", nic.speed_mbps ? `${nic.speed_mbps} Mb/s` : "",
                    nic.medium || nic.connector || ""
                ].filter(Boolean)
            );
            return jack;
        }));
        return module("SYS-05", "Network", [jacks,
            (node.bonds || []).length ? readout(node.bonds.map((bond) => [bond.name, `${bond.mode || "bond"} · ${bond.members.join(" + ")}`])) : null,
        ], {
            tone: !nics.length ? "off" : up === nics.length ? "on" : up ? "warn" : "alarm",
            aside: `${up} of ${nics.length} linked`
        });
    }

    /**
     * One port jack drawing, lit when linked.
     * @param {object} nic NIC record.
     * @returns {SVGElement} The jack.
     */
    function svg2Jack(nic) {
        const sfp = /sfp|fiber|dac/i.test(`${nic.connector || ""} ${nic.medium || ""}`);
        const art = svg("svg", { class: "diag-jack-art", viewBox: "0 0 40 32", "aria-hidden": "true" });
        if (sfp) {
            art.append(svg("rect", { class: "diag-jack-body", x: 3, y: 8, width: 34, height: 16, rx: 2 }));
            art.append(svg("rect", { class: "diag-jack-hole", x: 7, y: 12, width: 26, height: 8, rx: 1 }));
        } else {
            art.append(svg("path", { class: "diag-jack-body", d: "M4,6H36V26H26V30H14V26H4Z" }));
            art.append(svg("rect", { class: "diag-jack-hole", x: 9, y: 11, width: 22, height: 10, rx: 1 }));
        }
        art.append(svg("circle", { class: "diag-jack-led", cx: 8, cy: 3, r: 2 }));
        art.append(svg("circle", { class: "diag-jack-led is-activity", cx: 32, cy: 3, r: 2 }));
        return art;
    }

    /**
     * Accelerators and controllers from the PCI list.
     * @param {object} node Node view.
     * @returns {HTMLElement} The module.
     */
    function pciModule(node) {
        const pci = node.pci || [];
        const gpus = pci.filter((item) => item.kind === "gpu" && !/ASPEED|Matrox/i.test(item.name || ""));
        const net = pci.filter((item) => item.kind === "network");
        const storage = pci.filter((item) => item.kind === "storage");
        const group = (items) => {
            const counts = new Map();
            for (const item of items) {
                counts.set(item.name, (counts.get(item.name) || 0) + 1);
            }
            return [...counts.entries()];
        };
        return module("SYS-06", "Accelerators", [
            gpus.length
                ? el("ul", { class: "diag-cards" }, group(gpus).map(([name, count]) => el("li", { class: "diag-card" }, [
                el(
                    "span", { class: "diag-card-art", "aria-hidden": "true" }
                ), el(
                    "strong", { text: name }
                ), count > 1 ? el("span", { class: "diag-count", text: `×${count}` }) : null,
            ]))) : el("p", { class: "diag-muted", text: node.pci ? "No GPUs." : "PCI details not collected yet." }),
            readout([
                ["Network ctl", group(net).map(([name, count]) => `${count > 1 ? `${count}× ` : ""}${name}`).join("; ") || "—"],
                ["Storage ctl", group(storage).map(([name, count]) => `${count > 1 ? `${count}× ` : ""}${name}`).join("; ") || "—"],
            ]),
        ], { aside: `${gpus.length} GPU${gpus.length === 1 ? "" : "s"}` });
    }

    /**
     * Memory as a pie: each running guest's allocation, then what is left.
     * @param {object} node Node view.
     * @param {Array<object>} running Running guests on the node.
     * @returns {HTMLElement} The module.
     */
    function memoryMapModule(node, running) {
        const slices = running.map((guest) => ({
            label: guest.name || String(guest.vmid), value: guest.memory || 0
        })).sort((a, b) => b.value - a.value);
        const allocated = sum(slices, (slice) => slice.value);
        slices.push({ label: "Unallocated", value: Math.max(0, (node.maxmem || 0) - allocated), tone: "muted" });
        return module("SYS-08", "Memory map", [donut(
            slices, { label: `${node.name} memory by guest`, format: bytes, pie: true }
        )],
            { aside: `${running.length} guest${running.length === 1 ? "" : "s"}` });
    }

    /**
     * Maintenance: pending updates, the kernel, and whether a reboot or a
     * manager restart is needed (from apt/versions, Sys.Audit).
     * @param {object} node Node view.
     * @returns {HTMLElement} The module.
     */
    function maintenanceModule(node) {
        const versions = node.versions;
        if (!versions) {
            return module(
                "SYS-09", "Maintenance",
                [el("p", { class: "diag-muted", text: "Package versions not collected yet." })], { tone: "off" }
            );
        }
        const updates = versions.upgradable || [];
        const reboot = Boolean(versions.reboot_needed);
        const restart = Boolean(versions.manager_restart_needed);
        const tone = reboot || restart ? "warn" : updates.length ? "warn" : "on";
        const list = el("ul", { class: "diag-updates" }, updates.slice(0, 3).map((item) => el("li", {}, [
            el("span", { class: "diag-update-name", text: item.package }),
            el("span", { class: "diag-muted", text: `${item.installed} → ${item.available}` }),
        ])));
        const box = module("SYS-09", "Maintenance", [
            el("div", { class: "diag-flags" }, [
                el("span", { class: `diag-flag${reboot ? " is-warn" : ""}` }, [lamp(reboot ? "warn" : "on", reboot ? "Reboot needed" : "Running newest kernel"), el("span", { text: reboot ? "REBOOT PENDING" : "KERNEL CURRENT" })]),
                el("span", { class: `diag-flag${updates.length ? " is-warn" : ""}` }, [lamp(updates.length ? "warn" : "on", `${updates.length} updates`), el("span", { text: `${updates.length} UPDATE${updates.length === 1 ? "" : "S"}` })]),
                restart ? el(
                    "span", { class: "diag-flag is-warn" },
                    [lamp("warn", "Manager restart"), el("span", { text: "RESTART PVE" })]
                ) : null,
            ]),
            readout([
                ["Kernel", `${versions.running_kernel || "—"}${reboot ? ` (installed ${versions.kernel_installed})` : ""}`],
                ["Manager", `${versions.manager_running || "—"}${versions.manager_available && versions.manager_available !== versions.manager_installed ? ` → ${versions.manager_available}` : ""}`],
            ]),
            updates.length ? list : null,
            updates.length > 3 ? el("p", { class: "diag-muted", text: `and ${updates.length - 3} more` }) : null,
        ], { tone, aside: updates.length ? `${updates.length} pending` : "up to date" });
        tip(box.querySelector(".diag-flags"), [
            "Maintenance",
            reboot ? `A newer kernel (${versions.kernel_installed}) is installed than the one running (${versions.running_kernel}).` : "The running kernel is the newest installed.",
            updates.length
                ? `${updates.length} packages have newer versions available.`
                : "No package updates available."]);
        return box;
    }

    /**
     * Certificates and how long until each expires.
     * @param {object} node Node view.
     * @returns {HTMLElement} The module.
     */
    function certificatesModule(node) {
        const now = Date.now() / 1000;
        const certificates = (node.certificates || []).filter((cert) => cert.not_after);
        const rows = certificates.map((cert) => {
            const days = Math.floor((cert.not_after - now) / 86400);
            const tone = days < 14 ? "alarm" : days < 30 ? "warn" : "on";
            const row = el("div", { class: "diag-cert", tabindex: 0 }, [
                lamp(tone, `${days} days left`),
                el("span", { class: "diag-cert-name", text: cert.file }),
                meter({
                    total: 90, used: Math.max(0, Math.min(90, days)), label: `${days} days left`,
                    tone: tone === "alarm" ? "is-alarm" : tone === "warn" ? "is-warning" : ""
                }),
                el("span", { class: "diag-figure", text: days > 730 ? `${Math.round(days / 365)} y` : `${days} d` }),
            ]);
            tip(row, [cert.file, `Issued by ${cert.issuer || "unknown"}`, `Expires ${new Date(cert.not_after * 1000).toLocaleDateString()} (${days} days)`]);
            return row;
        });
        const worst = certificates.length
            ? Math.min(...certificates.map((cert) => cert.not_after - now)) / 86400
            : null;
        return module("SYS-10", "Certificates", rows.length ? [el(
            "div", { class: "diag-certs" }, rows
        )] : [el("p", { class: "diag-muted", text: "Certificates not collected yet." })],
            {
                tone: worst === null ? "off" : worst < 14 ? "alarm" : worst < 30 ? "warn" : "on",
                aside: "bar fills to 90 days"
            });
    }

    /**
     * Services: running, stopped though enabled, and inactive, as a donut.
     * @param {object} node Node view.
     * @returns {HTMLElement} The module.
     */
    function servicesModule(node) {
        const services = node.services || [];
        const running = services.filter((item) => item.state === "running");
        const stopped = services.filter((item) => item.state !== "running" && item.enabled);
        const idle = services.filter((item) => item.state !== "running" && !item.enabled);
        return module("SYS-11", "Services", services.length ? [donut([
            {
                label: "Running", value: running.length, tone: "ok", detail: running.map((item) => item.name).join(", ")
            },
            {
                label: "Stopped (enabled)", value: stopped.length, tone: "warning",
                detail: stopped.map((item) => item.name).join(", ")
            },
            {
                label: "Not enabled", value: idle.length, tone: "muted",
                detail: idle.map((item) => item.name).join(", ")
            },
        ], {
            label: `${node.name} services`, centre: String(running.length), caption: "running"
        })] : [el("p", { class: "diag-muted", text: "Services not collected yet." })],
        { tone: !services.length ? "off" : stopped.length ? "warn" : "on" });
    }

    /**
     * The day's traces as compact rows (owner: the node screen fits one
     * laptop screen): CPU, memory, network in and out, then pressure stall
     * (PSI) for CPU, IO, and memory, each a sparkline with its latest value.
     * @param {object} node Node view.
     * @returns {HTMLElement} The module.
     */
    function tracesModule(node) {
        const history = node.history || [];
        const fraction = (value) => `${(value * 100).toFixed(value < 0.1 ? 1 : 0)}%`;
        const rows = [
            ["cpu", "CPU", fraction, 1, 1], ["mem", "Memory", fraction, 1, 1],
            ["net_in", "Net in", rate, null, 1], ["net_out", "Net out", rate, null, 1],
            [
                "psi_cpu", "CPU wait", fraction, null, 100
            ], ["psi_io", "IO wait", fraction, null, 100], ["psi_mem", "Mem wait", fraction, null, 100],
        ].map(([field, label, format, max, scale]) => {
            const points = history.filter((point) => typeof point[field] === "number").map((point) => ({
                t: point.t, v: point[field] / scale
            }));
            if (points.length < 2) {
                return null;
            }
            const last = points[points.length - 1].v;
            const top = max || Math.max(scale === 100 ? 0.05 : 1, ...points.map((point) => point.v));
            return el("div", { class: "cluster-node-row" }, [
                el("span", { class: "cluster-node-key", text: label }),
                area(
                    points,
                    { field: "v", label: `${node.name} ${label} over 24 hours`, compact: true, max: top, format }
                ),
                el("strong", { class: "cluster-node-figure", text: format(last) }),
            ]);
        }).filter(Boolean);
        return module("TRC-01", "Last 24 hours", rows.length ? rows : [el(
            "p", { class: "diag-muted", text: "No history collected yet." }
        )],
            { aside: "waits: time stalled", className: "diag-module-traces" });
    }

    /**
     * Load averages against thread count, plus uptime and versions.
     * @param {object} node Node view.
     * @returns {HTMLElement} The module.
     */
    function loadModule(node) {
        const threads = node.maxcpu || 1;
        const rows = ["1 min", "5 min", "15 min"].map((label, index) => {
            const value = (node.loadavg || [])[index];
            return el("div", { class: "diag-capacity-row" }, [el("span", { class: "diag-key-label", text: label }),
                meter({ total: threads, used: Math.min(threads, value || 0), label: `${label} load ${value ?? "—"}`, tone: severity((value || 0) / threads) }),
                el("span", { class: "diag-figure", text: typeof value === "number" ? value.toFixed(2) : "—" })]);
        });
        return module("SYS-01", "Load", [el("div", { class: "diag-capacity" }, rows), readout([
            [
                "Uptime", uptime(node.uptime)
            ], ["Proxmox", (node.pve_version || "—").replace(/^pve-manager\//, "").split("/")[0]],
            ["Sockets", String(node.sockets ?? "—")],
        ])], { aside: `of ${threads} threads` });
    }

    // ------------------------------------------------------------------
    // The tab
    // ------------------------------------------------------------------

    /**
     * Draw one node's diagnostics screen and its node subtabs.
     * @param {HTMLElement} root The Nodes tab panel.
     * @param {object} source Source view from /api/v1/cluster.
     * @param {object} options Drawing options.
     * @param {string} options.node Remote ID or name of the node to show;
     *     the first node when unknown.
     * @param {Element} options.tab The view switch's Nodes tab, to spread from.
     * @param {Function} options.onSelect Called with a node's remote ID.
     * @param {number} [options.refreshMs] The page's refresh period, for
     *     the core gauge's sweep (one turn per refresh).
     * @param {number} [options.loadedAt] When the data last loaded.
     * @returns {string|null} The remote ID drawn, or null with no nodes.
     */
    function draw(root, source, { node: wanted, tab, onSelect, refreshMs = 0, loadedAt = 0 }) {
        if (!source.nodes.length) {
            root.replaceChildren(el(
                "p", { class: "cluster-empty", text: "The collector has not reported any Proxmox nodes yet." }
            ));
            return null;
        }
        const node = source.nodes.find((item) => item.remote_id === wanted || item.name === wanted) || source.nodes[0];
        const strip = window.LabratViewFrame.placeSubtabs({
            label: "Nodes", tab, current: node.remote_id, onSelect,
            items: source.nodes.map((item) => ({
                key: item.remote_id, label: item.name,
                tone: item.status === "online" ? null : "alarm",
            })),
        });
        const running = source.guests.filter((guest) => guest.node === node.remote_id
            && guest.status === "running"
            && !guest.template);
        const facts = { vcpus: sum(running, (guest) => guest.vcpus), allocated: sum(running, (guest) => guest.memory) };
        const guests = source.guests.filter((guest) => guest.node === node.remote_id && !guest.template);
        const status = el("div", { class: "diag-status" }, [
            el("span", { class: "diag-code", text: "NODE" }), el("strong", { text: node.name.toUpperCase() }),
            lamp(node.status === "online" ? "on" : "alarm", node.status || "unknown"),
            el("span", { text: node.status === "online" ? "ONLINE" : "OFFLINE" }),
            el(
                "span", { class: "diag-status-sep", text: "·" }
            ), el("span", { text: `UP ${uptime(node.uptime).toUpperCase()}` }),
            el(
                "span", { class: "diag-status-sep", text: "·" }
            ), el("span", { text: `${running.length}/${guests.length} GUESTS ACTIVE` }),
            node.versions && node.versions.reboot_needed ? el(
                "span", { class: "diag-status-flag" },
                [lamp("warn", "Reboot needed"), el("span", { text: "REBOOT PENDING" })]
            ) : null,
            node.versions && (node.versions.upgradable || []).length ? el(
                "span", { class: "diag-status-flag" },
                [lamp("warn", "Updates available"), el("span", { text: `${node.versions.upgradable.length} UPDATES` })]
            ) : null,
        ]);
        // A balanced deck (owner: the whole node on one laptop screen, with
        // the gauges sitting well between the sides): hardware on the left,
        // health, the data drives, and the day's traces on the right, and
        // in the centre the core gauge and the 24-hour CPU clock beside
        // services, over the boot drive beside the memory map and
        // accelerators (nodes.css).
        // Each box also names its grid area, where narrower screens place
        // it once the columns are flattened.
        const at = (name, item) => {
            item.classList.add(`is-area-${name}`);
            return item;
        };
        const deck = el("div", { class: "diag-deck" }, [
            el("div", { class: "diag-column is-left" }, [at(
                "load", loadModule(node)
            ), at("mem", memoryModule(node, facts)),
                at("net", networkModule(node))]),
            el("div", { class: "diag-column is-centre" }, [
                el("div", { class: "diag-centre-top" }, [at(
                    "core", core(node, facts, { refreshMs, loadedAt })
                ), at("clock", clock(node)),
                    at("svc", servicesModule(node))]),
                el("div", { class: "diag-centre-grid" }, [
                    el("div", { class: "diag-column" }, [at("boot", bootModule(node))]),
                    el(
                        "div", { class: "diag-column" },
                        [at("mmap", memoryMapModule(node, running)), at("pci", pciModule(node))]
                    ),
                ]),
            ]),
            el("div", { class: "diag-column is-right" }, [at(
                "maint", maintenanceModule(node)
            ), at("certs", certificatesModule(node)),
                at("drives", drivesModule(node, source)), at("traces", tracesModule(node))]),
        ]);
        const guestStrip = module("SYS-07", "Guests", [el("ul", { class: "diag-guests" }, guests.map((guest) => {
            const item = el("li", { class: `diag-guest${guest.status === "running" ? "" : " is-stopped"}`, tabindex: 0 }, [
                lamp(guest.status === "running" ? "on" : "off", guest.status || "unknown"),
                el("span", { text: guest.name || String(guest.vmid) }),
                el("span", { class: "diag-guest-spec", text: `${guest.vcpus ?? "?"}c · ${bytes(guest.memory)}` }),
                ...(((guest.agent && guest.agent.addresses) || []).filter((item) => item.family === "ipv4").slice(0, 1)
                    .map((item) => el("span", { class: "diag-guest-ip", text: item.address }))),
            ]);
            tip(item, [`${guest.name} (${guest.kind === "container" ? "CT" : "VM"} ${guest.vmid})`, guest.status, `${guest.vcpus} vCPU · ${bytes(guest.memory)}`,
                guest.status === "running" ? `CPU ${percent(guest.cpu)}` : "",
                guest.agent && guest.agent.os && guest.agent.os.name ? guest.agent.os.name : "",
                ...((guest.agent && guest.agent.addresses)
                    || []).map((address) => `${address.address} on ${address.interface}`)].filter(Boolean));
            return item;
        }))], { aside: `${running.length} running · ${facts.vcpus} vCPU · ${bytes(facts.allocated)}` });
        root.replaceChildren(
            ...(strip ? [strip] : []),
            el("div", { class: "diag-screen", "data-hue": "compute" }, [status, deck, guestStrip])
        );
        return node.remote_id;
    }

    window.LabratNodes = Object.freeze({ draw });
})();
