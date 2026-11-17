// Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
/**
 * @file The widget gallery (templates/private/widgets.html): every widget
 * drawn from made-up data beside the call that draws it, so widgets can be
 * tried in each theme and hue before a page stitches them in. The hue
 * buttons recolour the whole gallery. Sample names are generic on purpose
 * (no lab's real devices).
 */
(() => {
    "use strict";

    const W = window.LabratWidgets;
    const { el, bytes, percent, rate, panel, row } = W;
    const gallery = document.getElementById("widget-gallery");
    if (!gallery) {
        return;
    }

    // ------------------------------------------------------------------
    // Hue buttons
    // ------------------------------------------------------------------

    for (const button of gallery.querySelectorAll("[data-pick-hue]")) {
        button.addEventListener("click", () => {
            gallery.dataset.hue = button.dataset.pickHue;
            for (const other of gallery.querySelectorAll("[data-pick-hue]")) {
                other.setAttribute("aria-pressed", String(other === button));
            }
        });
    }

    const items = document.getElementById("widget-gallery-items");
    const index = document.getElementById("widget-gallery-index");
    if (!items) {
        return;
    }

    // ------------------------------------------------------------------
    // Sample data (deterministic, so the gallery looks the same each visit)
    // ------------------------------------------------------------------

    let seed = 7;
    /** A repeatable pseudo-random number in 0..1. */
    const random = () => {
        seed = (seed * 16807) % 2147483647;
        return (seed - 1) / 2147483646;
    };

    const now = Math.floor(Date.now() / 1000 / 1800) * 1800;
    /**
     * A day of half-hour points: a daily curve plus noise.
     * @param {number} base Typical value (0..1).
     * @param {number} swing Daily swing.
     * @returns {Array<{t: number, value: number, in: number, out: number}>} Points.
     */
    const day = (base, swing) => Array.from({ length: 49 }, (_, i) => {
        const t = now - (48 - i) * 1800;
        const hour = new Date(t * 1000).getHours();
        const curve = Math.sin(((hour - 6) / 24) * 2 * Math.PI);
        return {
            t, value: Math.max(0.01, Math.min(1, base + swing * curve + (random() - 0.5) * swing)),
            in: Math.max(0, 4e6 + 3e6 * curve + random() * 2e6), out: Math.max(0, 1.5e6 + 1e6 * curve + random() * 1e6),
        };
    });

    const hosts = ["host-a", "host-b", "host-c", "host-d"];
    const services = [
        "web", "db", "cache", "media", "build", "dns", "backup", "mail", "git", "proxy", "metrics", "queue"
    ];
    const GiB = 1024 ** 3;

    // ------------------------------------------------------------------
    // Entries: title, what it is for, the call, and how to draw it
    // ------------------------------------------------------------------

    const entries = [
        {
            id: "stats", title: "Stat tiles", about: "A labelled figure with a caption and an optional meter; stats() lays several out as a grid.",
            code: 'W.stats([{ label: "CPU now", value: "34%", caption: "of 64 threads",\n  meter: { total: 1, used: 0.34, label: "CPU 34%", tone: "auto" } }])',
            draw: () => [W.stats([
                {
                    label: "Hosts online", value: "4 of 4", caption: "Quorate",
                    meter: { total: 4, used: 4, label: "4 of 4 online" }
                },
                {
                    label: "CPU now", value: "34%", caption: "of 64 threads",
                    meter: { total: 1, used: 0.34, label: "CPU 34%", tone: "auto" }
                },
                {
                    label: "Memory in use", value: "212 GiB", caption: "83% of 256 GiB",
                    meter: { total: 256, used: 212, label: "83%", tone: "auto" }
                },
                { label: "Disk", value: "Healthy", caption: "12 of 12 up", lamp: { tone: "on", label: "Healthy" } },
                { label: "Alarm", value: "Degraded", caption: "1 of 3 down", lamp: { tone: "alarm", label: "Degraded" },
                    meter: {
                        total: 3, used: 2, label: "2 of 3 up", tone: "is-alarm"
                    }, tip: ["Alarm", "A tile can carry a tooltip too."] },
            ])],
        },
        {
            id: "panel", title: "Panel", about: "A titled panel with an aside; sunk inside a view frame, raised elsewhere. Every other widget can sit in one.",
            code: 'W.panel("Title", [widget], { hue: "network", aside: "a note" })',
            draw: () => [row([
                panel(
                    "A panel", [el("p", { class: "widget-muted", text: "Content goes here." })],
                    { aside: "with an aside" }
                ),
                panel(
                    "Network hue", [W.meter({ total: 10, used: 6, label: "6 of 10" })],
                    { hue: "network", aside: "data-hue=network" }
                ),
            ])],
        },
        {
            id: "meter", title: "Meter and lamp", about: "A meter fills to used, with an optional lighter reserved step; tone recolours it. Lamps show state.",
            code: 'W.meter({ total: 100, used: 40, reserved: 30, label: "40 used, 30 reserved" })\nW.lamp("warn", "Caution")',
            draw: () => [el("div", { class: "widget-gallery-stack" }, [
                W.meter({
                    total: 100, used: 40, reserved: 30, label: "40 used, 30 reserved",
                    tip: ["Memory", "40 in use", "30 more reserved"]
                }),
                W.meter({ total: 100, used: 84, label: "84%", tone: "auto" }),
                W.meter({ total: 100, used: 95, label: "95%", tone: "auto" }),
                el(
                    "p", { class: "widget-gallery-lamps" },
                    ["on", "warn", "alarm", "off"].map((tone) => el("span", {}, [W.lamp(tone, tone), ` ${tone}`]))
                ),
            ])],
        },
        {
            id: "area", title: "Area chart and sparkline", about: "One series over time with a hover crosshair. Values are 0..1 by default; pass format for other units.",
            code: 'W.area(points, { field: "value", label: "CPU" })\nW.sparkline(points, { label: "CPU" })',
            draw: () => {
                const points = day(0.35, 0.2);
                return [row([
                    panel("Area", [W.area(points, { label: "CPU" })], { aside: "24 hours" }),
                    panel(
                        "Other units",
                        [W.area(points.map((p) => ({ t: p.t, value: p.in })), { label: "Traffic", format: rate })],
                        { aside: "format: rate" }
                    ),
                    panel("Sparklines", [el("div", { class: "widget-gallery-stack" }, hosts.map((host, i) =>
                        el(
                            "div", { class: "widget-gallery-spark-row" },
                            [
                                el("span", { class: "widget-muted", text: host }),
                                W.sparkline(day(0.2 + i * 0.1, 0.15), { label: `${host} CPU` })
                            ]
                        )))], { aside: "compact" }),
                ])];
            },
        },
        {
            id: "mirror", title: "Mirror chart", about: "Two series on one scale, one above the axis and one below, such as traffic in and out.",
            code: 'W.mirror(points, { label: "Traffic", up: { field: "in", label: "In" }, down: { field: "out", label: "Out" } })',
            draw: () => [panel(
                "Traffic · 24 h", [W.mirror(day(0.3, 0.1), { label: "Traffic in and out" })],
                { aside: "in above, out below" }
            )],
        },
        {
            id: "multiples", title: "Small multiples", about: "One sparkline per item on one shared scale, so the busiest reads at a glance.",
            code: 'W.multiples([{ label: "web", caption: "avg 31%", points }], { field: "value" })',
            draw: () => [panel("Busiest · 24 h", [W.multiples(services.slice(0, 8).map((name, i) => {
                const points = day(0.5 - i * 0.05, 0.15);
                return {
                    label: name, caption: `avg ${percent(points.reduce((a, p) => a + p.value, 0) / points.length)}`,
                    points
                };
            }))], { aside: "one scale" })],
        },
        {
            id: "clock", title: "Radial clock", about: "A day around a dial: one petal per reading, midnight at the top, now dashed.",
            code: 'W.radialClock(points, { field: "value", label: "CPU by time of day" })',
            draw: () => [row([panel("CPU clock", [W.radialClock(day(0.35, 0.25), { label: "CPU by time of day" })])])],
        },
        {
            id: "donut", title: "Donut and pie", about: "Parts of a whole in fixed slot colours (five, then Other); a status tone overrides a slot.",
            code: 'W.donut([{ label: "Running", value: 18 }, { label: "Stopped", value: 3, tone: "muted" }],\n  { label: "Guests", centre: "18", caption: "running" })\nW.pie(slices, { label: "OS" })',
            draw: () => [row([
                panel("Donut", [W.donut([
                    { label: "VMs", value: 14 }, { label: "Containers", value: 6 },
                    { label: "Stopped", value: 3, tone: "muted" }
                ],
                    { label: "Guests", centre: "20", caption: "running" })]),
                panel(
                    "Folds into Other",
                    [W.donut(services.map((name, i) => ({ label: name, value: 30 - i * 2 })), { label: "Services" })]
                ),
                panel("Status tones", [W.donut([
                    { label: "OK", value: 40, tone: "ok" }, { label: "Warnings", value: 4, tone: "warning" },
                    { label: "Failed", value: 2, tone: "alarm" }
                ],
                    { label: "Outcomes", centre: "46", caption: "tasks" })]),
                panel(
                    "Pie",
                    [W.pie(
                        [{ label: "Linux", value: 15 }, { label: "Windows", value: 3 }, { label: "BSD", value: 2 }],
                        { label: "Operating systems" }
                    )]
                ),
            ])],
        },
        {
            id: "sunburst", title: "Sunburst", about: "Groups on the inner ring, their members on the outer, each in a step of the group's colour.",
            code: 'W.sunburst([{ label: "host-a", children: [{ label: "web", value: 8 }] }], { label: "Memory", format: W.bytes })',
            draw: () => [row([panel("Memory · host › guest", [W.sunburst(hosts.map((host, i) => ({
                label: host, children: services.slice(i * 3, i * 3 + 3).map((name, j) => ({
                    label: name, value: (4 + j * 6 + i * 2) * GiB
                })),
            })), { label: "Memory by host and guest", format: bytes, centre: "128 GiB", caption: "allocated" })])])],
        },
        {
            id: "segments", title: "Segmented bar", about: "One bar split into parts with a key, such as placement groups by state.",
            code: 'W.segments([{ label: "clean", value: 120, tone: "ok" }, { label: "remapped", value: 8, tone: "warning" }])',
            draw: () => [el("div", { class: "widget-gallery-stack" }, [
                W.segments(
                    [
                        { label: "clean", value: 120, tone: "ok" }, { label: "remapped", value: 8, tone: "warning" },
                        { label: "degraded", value: 2, tone: "alarm" }
                    ],
                    { label: "States" }
                ),
                W.segments(hosts.map((host, i) => ({ label: host, value: 4 + i * 3 })), { label: "By host" }),
            ])],
        },
        {
            id: "bars", title: "Bars", about: "Ranked bars on one scale, or usage bars each against its own total (scale: \"row\"), coloured past 80% and 90%.",
            code: 'W.bars(rows, { format: W.percent, limit: 8 })\nW.bars(rows, { format: W.bytes, scale: "row" })',
            draw: () => [row([
                panel(
                    "Ranked",
                    [W.bars(
                        services.slice(0, 8).map((name, i) => ({
                            label: name, sub: hosts[i % 4], value: 0.8 / (i + 1)
                        })),
                        { format: percent }
                    )],
                    { aside: "shared scale" }
                ),
                panel("Usage", [W.bars([
                    ["fast", 0.42], ["bulk", 0.83], ["archive", 0.94], ["scratch", 0.12]
                ].map(([label, share]) => ({
                    label, sub: "pool", value: share * 8 * 1024 ** 4, total: 8 * 1024 ** 4,
                })), { format: bytes, scale: "row" })], { aside: "own scale" }),
            ])],
        },
        {
            id: "allocation", title: "Allocation bars", about: "Use against capacity on one scale for every row, with a tick at each capacity, so overcommit reads past it.",
            code: 'W.allocation([{ label: "host-a", used: 40, total: 32 }], { unit: "threads" })',
            draw: () => [panel("vCPUs per host", [W.allocation(
                hosts.map((host, i) => ({ label: host, used: [20, 40, 12, 30][i], total: [32, 32, 16, 24][i] })),
                { unit: "threads" }
            )],
                { aside: "tick at capacity" })],
        },
        {
            id: "split", title: "Split bars", about: "Two or three parts of each row end to end, such as data received and sent.",
            code: 'W.splitBars([{ label: "web", values: [inBytes, outBytes] }], { parts: ["in", "out"], format: W.bytes })',
            draw: () => [panel("Top talkers", [W.splitBars(services.slice(0, 6).map((name, i) => ({
                label: name, sub: hosts[i % 4], values: [(9 - i) * 3 * GiB, (i + 1) * 1.5 * GiB],
            })), { format: bytes })])],
        },
        {
            id: "columns", title: "Columns", about: "Vertical columns with reference lines; warnAt turns columns amber past a threshold.",
            code: 'W.columns(rows, { max: 100, lines: [{ value: 85, warn: true }], warnAt: 85, format: (v) => `${v}%` })',
            draw: () => [panel("Disk fill", [W.columns(Array.from(
                { length: 16 }, (_, i) => ({ label: String(i), value: 92 - i * 4 - random() * 6 })
            ),
                {
                    label: "Disk fill", max: 100, lines: [{ value: 85, warn: true }], warnAt: 85,
                    format: (v) => `${Math.round(v)}%`
                })], { aside: "fullest first" })],
        },
        {
            id: "column-groups", title: "Column groups", about: "Small column charts on one scale, each only as wide as it needs; marked columns are lit and the rest dim. The Networks page uses it for Wi-Fi channels.",
            code: 'W.columnGroups([{ label: "2.4 GHz", rows: [{ label: "1", value: 4, mark: true }] }], { note: "Lit columns are yours." })',
            draw: () => [panel("Neighbours per channel", [W.columnGroups([
                {
                    label: "2.4 GHz",
                    rows: [1, 6, 11].map((channel, i) => ({
                        label: String(channel), value: [7, 4, 9][i], mark: channel === 6
                    }))
                },
                {
                    label: "5 GHz",
                    rows: [
                        36, 44, 52, 100, 149, 157
                    ].map((channel, i) => ({
                        label: String(channel), value: [3, 2, 1, 1, 5, 2][i], mark: channel === 100
                    }))
                },
            ], {
                label: "Neighbours per channel", note: "Brighter columns are channels your own access points use."
            })])],
        },
        {
            id: "timeline", title: "Timeline", about: "Events on a time axis, one lane each: a dot, or a bar when an event ran long.",
            code: 'W.timeline({ lanes: [{ id: "a", label: "host-a" }],\n  events: [{ lane: "a", start, end, tone: "ok", tip: ["backup", "ok"] }] })',
            draw: () => {
                const events = [];
                for (let i = 0; i < 40; i += 1) {
                    const start = now - Math.floor(random() * 86000);
                    const long = random() > 0.7;
                    const tone = random() > 0.92 ? "alarm" : random() > 0.85 ? "warning" : "ok";
                    events.push({
                        lane: hosts[i % 4], start, end: start + (long ? 3600 * random() : 30), tone,
                        tip: [services[i % services.length], tone]
                    });
                }
                return [panel(
                    "Jobs · 24 h",
                    [W.timeline(
                        { lanes: hosts.map((host) => ({ id: host, label: host })), events }, { label: "Jobs by host" }
                    )]
                )];
            },
        },
        {
            id: "treemap", title: "Treemap", about: "Groups as blocks, members as tiles sized by value, with an optional fill bar and dimmed members.",
            code: 'W.treemap([{ label: "host-a", children: [{ label: "web", value: 8, fill: 0.6 }] }], { format: W.bytes })',
            draw: () => [panel("Guests by memory", [W.treemap(hosts.map((host, i) => ({
                label: host, children: services.slice(i * 3, i * 3 + 3).map((name, j) => ({
                    label: name, value: (2 + j * 5 + i) * GiB, fill: 0.3 + random() * 0.6, dim: j === 2
                        && i === 1, detail: `${2 + j} vCPU`,
                })),
            })), { label: "Guests by memory", format: bytes })])],
        },
        {
            id: "shelf", title: "Disk shelf", about: "Drives as 3.5\" hot-swap bays in rack chassis, beside an optional health box. Bays carry status and (simulated) activity lamps, gauges, a readout, a pool stripe, and an M.2 board for NVMe; rows and columns balance themselves.",
            code: 'W.shelf({ health: { state: "ONLINE", tone: "on", title: "tank", facts: [["Free", "2 TiB"]], found: [] }, chassis: [{ name: "host-a", groups: [{ name: "raidz1-0", bays }] }] })',
            draw: () => {
                const fill = (value, tone = "on") => ({
                    kind: "fill", value, tone, text: `${Math.round(value * 100)}% full`
                });
                const dial = (ms) => ({
                    kind: "dial", value: (ms - 10) / 140, tone: ms > 100 ? "alarm" : ms > 80 ? "warn" : "on",
                    reading: `${ms}ms`, text: `Latency ${ms} ms`,
                    zones: [{ from: 0.5, to: 0.643, tone: "warn" }, { from: 0.643, to: 1, tone: "alarm" }] });
                const bay = (index, extra = {}) => ({
                    label: `BAY 0${index}`, device: `sd${"abcdefgh"[index - 1]}`, issues: [], gauges: [fill(0.42)],
                    lines: [["12 TiB", "HDD 7200"], [{ text: `${30 + index}°C` }, `${(2 + index / 3).toFixed(1)} y`]],
                    activity: {
                        level: "light", source: "a sample reading"
                    }, pools: [{ slot: "1", name: "tank" }], ...extra,
                });
                return [panel("Disk shelf", [W.shelf({
                    health: { state: "ONLINE", tone: "on", title: "tank", subtitle: "host-a · 6 drives · raidz1-0",
                        meter: {
                            total: 100, used: 42, label: "42% allocated"
                        }, facts: [["Allocated", "42%"], ["Free", "28 TiB"]],
                        key: [{ slot: "1", label: "tank" }, { slot: "2", label: "fast" }],
                        found: [
                            { tone: "warn", text: "Bay 03 · sdc: 120 pending: plan a replacement" },
                            { tone: "info", text: "Bay 05 · sde: 2 reallocated, below replacement levels" }
                        ] },
                    chassis: [
                        { name: "host-a · tank", aside: "I/O light", groups: [{ name: "raidz1-0", bays: [bay(1), bay(2),
                            bay(3, { issues: [{ tone: "warn", text: "120 pending: plan a replacement" }] }), bay(4),
                            bay(
                                5, { issues: [{ tone: "info", text: "2 reallocated, below replacement levels" }] }
                            ), bay(6)] }] },
                        { name: "host-b · fast", aside: "I/O busy", groups: [{ name: "nvme OSDs", bays: [
                            bay(
                                1,
                                {
                                    device: "nvme0n1 · NVME", m2: true, pools: [{ slot: "2", name: "fast" }],
                                    gauges: [fill(0.12), dial(7)],
                                    lines: [["932 GiB", "35 PGs"], ["12% full", { text: "41°C" }]],
                                    activity: { level: "busy", source: "a sample reading" }
                                }
                            ),
                            bay(
                                2,
                                {
                                    device: "nvme1n1 · NVME", m2: true, pools: [{ slot: "2", name: "fast" }],
                                    gauges: [fill(0.11), dial(124)],
                                    lines: [["932 GiB", "33 PGs"], ["11% full", { text: "44°C" }]],
                                    activity: { level: "busy", source: "a sample reading" }
                                }
                            ),
                        ] }] },
                    ],
                })], { aside: "made-up drives" })];
            },
        },
        {
            id: "sinkholes", title: "DNS sinkholes", about: "A card per custom DNS server or likely Pi-hole, with its evidence and, once an integration reads it, the day's queries.",
            code: "W.sinkholes(items, { evidence: false })",
            draw: () => [panel("DNS sinkholes", [W.sinkholes([
                {
                    ip: "192.0.2.53", names: ["dns-a"], likely_pihole: true, evidence: [
                        "Handed out as DNS by DHCP on two networks.", "Named like a Pi-hole."
                    ],
                    integration: { stats: {
                        api: "v6", version: "6.1", blocking: "enabled",
                        queries: { total: 48210, blocked: 6120, clients: 31, domains_on_lists: 182400 },
                        history: Array.from(
                            { length: 24 },
                            (_, i) => ({ t: 1700000000 + i * 3600, total: 2000, blocked: 200 + random() * 160 })
                        ),
                        top_blocked: [
                            { name: "ads.example.com", count: 1210 }, { name: "telemetry.example.net", count: 640 },
                            { name: "tracker.example.org", count: 310 }
                        ],
                    } },
                },
                {
                    ip: "192.0.2.54", names: ["dns-b"], likely_pihole: true,
                    evidence: ["Handed out as DNS by DHCP on one network."], integration: null
                },
            ])], { aside: "2 likely Pi-hole" })],
        },
        {
            id: "attention", title: "Attention", about: "Worst first: a lamp and a linked sentence per item, counted in the heading.",
            code: 'W.attention([{ tone: "alarm", text: "A disk is failing.", href: "/cluster/nodes" }])',
            draw: () => [row([
                W.attention([
                    { tone: "info", text: "12 package updates waiting.", href: "#attention" },
                    { tone: "alarm", text: "host-c disk 2 SMART health is FAILED.", href: "#attention" },
                    { tone: "warn", text: "host-a memory is 84% used.", href: "#attention" },
                ]),
                W.attention([], { title: "All clear" }),
            ])],
        },
    ];

    for (const entry of entries) {
        index.append(el("a", { href: `#${entry.id}`, text: entry.title }));
        const section = el("section", {
            class: "widget-gallery-item", id: entry.id, "aria-labelledby": `${entry.id}-title`
        }, [
            el("header", { class: "widget-gallery-item-heading" }, [
                el("h2", { id: `${entry.id}-title`, text: entry.title }),
                el("p", { text: entry.about }),
            ]),
            el("div", { class: "widget-gallery-demo" }, entry.draw()),
            el("pre", { class: "widget-gallery-code" }, [el("code", { text: entry.code })]),
        ]);
        items.append(section);
    }
})();
