// Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
/**
 * @file The Proxmox cluster view (templates/private/cluster.html).
 *
 * Reads /api/v1/cluster once and draws one of four tabs: Summary (stat
 * tiles and charts), Compute (guest cards grouped by VLAN or node),
 * Networks (which nodes carry which bridges and VLANs), and Storage
 * (engines, Ceph OSDs, ZFS pools). Tabs switch in place with
 * history.pushState, so each has its own address.
 *
 * The content security policy forbids inline style attributes, so every
 * chart is SVG whose geometry is attributes, and positions set from script
 * go through the CSSOM (element.style), which the policy allows.
 */
(() => {
    "use strict";

    const { readPreference, writePreference } = window.LabratProxmox;
    const {
        el, svg, bytes, percent, uptime, ago, sum, tally, lamp, tip, meter, severity, statTile: widgetTile, stats, row, panel,
        area, sparkline, multiples, radialClock, timeline, donut, pie, sunburst, segments,
        bars, allocation, splitBars, columns, treemap, attention,
    } = window.LabratWidgets;
    const {
        runningGuests, storagePools, clusterHistory, mergeLocal, guestOs, fullestDisk, attentionItems, ruleClass,
    } = window.LabratClusterModel;
    // Disk shelves: plain data from the shelf model, drawn by the widget.
    const Shelves = window.LabratShelfModel;
    const { shelf } = window.LabratWidgets;
    /** Cluster tiles take the compute hue unless they say otherwise. */
    const statTile = (tile) => widgetTile({ hue: "compute", ...tile });
    const app = document.getElementById("cluster-app");
    if (!app) {
        return;
    }
    const summaryLine = document.getElementById("cluster-summary");
    const notice = document.getElementById("cluster-notice");
    const sourcePicker = document.getElementById("cluster-sources");
    const groupButton = document.getElementById("cluster-group");
    const styleButton = document.getElementById("cluster-network-style");
    const computeButton = document.getElementById("cluster-compute-style");
    const zfsButton = document.getElementById("cluster-zfs-style");
    const COMPUTE_STYLES = ["cards", "list", "map"];
    /** ZFS and Ceph styles: the drawn shelf or the plain view (a toggle). */
    const ZFS_STYLES = ["shelf", "cards"];
    /** How often a node screen redraws while visible. */
    const LIVE_REFRESH_MS = 30000;
    /** When the cluster data was last loaded (ms since the epoch). */
    let loadedAt = 0;

    /** @type {{sources: Array<object>}|null} */
    let data = null;
    let sourceIndex = 0;
    const state = {
        view: app.dataset.view || "summary",
        groupBy: readPreference("labrat.cluster.groupBy", "vlan"),
        kinds: { vm: true, container: true },
        runningOnly: false,
        templates: false,
        networkStyle: readPreference("labrat.cluster.networkStyle", "grid"),
        computeStyle: readPreference("labrat.cluster.computeStyle", "cards"),
        // Drawn-hardware style for ZFS and Ceph pools (the key kept its
        // first name, from when only ZFS had it).
        // A remembered "tower" (since dropped) falls back to the shelf.
        zfsStyle: readPreference("labrat.cluster.zfsStyle", "shelf") === "cards" ? "cards" : "shelf",
        listSort: { key: "name", dir: 1 },
        node: app.dataset.node || "",
        storageSub: new URLSearchParams(window.location.search).get("sub") || "overview",
        cephPool: new URLSearchParams(window.location.search).get("pool") || "overview",
    };

    /** Keep the Storage subtabs in the address, so a drill-in can be linked. */
    function syncQuery() {
        const params = new URLSearchParams();
        if (state.view === "storage" && state.storageSub !== "overview") {
            params.set("sub", state.storageSub);
            if (state.storageSub === "ceph" && state.cephPool !== "overview") {
                params.set("pool", state.cephPool);
            }
        }
        const query = params.toString();
        window.history.replaceState(window.history.state, "", `${window.location.pathname}${query ? `?${query}` : ""}`);
    }
    if (!["cards", "list", "map"].includes(state.computeStyle)) {
        state.computeStyle = "cards";
    }


    // ------------------------------------------------------------------
    // Derived facts
    // ------------------------------------------------------------------

    /**
     * The currently selected Proxmox source.
     * @returns {object|null} Source view from the API.
     */
    function current() {
        return data && data.sources[sourceIndex] ? data.sources[sourceIndex] : null;
    }

    // ------------------------------------------------------------------
    // Summary
    // ------------------------------------------------------------------

    /**
     * Draw the Summary tab.
     * @param {HTMLElement} root Tab panel.
     * @param {object} source Source view.
     */
    function drawSummary(root, source) {
        const nodes = source.nodes;
        const online = nodes.filter((node) => node.status === "online");
        const guests = source.guests.filter((guest) => !guest.template);
        const running = runningGuests(source);
        const threads = sum(online, (node) => node.maxcpu);
        const vcpus = sum(running, (guest) => guest.vcpus);
        const installed = sum(online, (node) => node.maxmem);
        const memUsed = sum(online, (node) => node.mem);
        const allocated = sum(running, (guest) => guest.memory);
        const pools = storagePools(source);
        const storeTotal = sum(pools, (pool) => pool.total);
        const storeUsed = sum(pools, (pool) => pool.used);
        const cpuNow = threads ? sum(online, (node) => (node.cpu || 0) * (node.maxcpu || 0)) / threads : null;
        const vms = guests.filter((guest) => guest.kind === "vm");
        const containers = guests.filter((guest) => guest.kind === "container");

        const tiles = stats([
            statTile({
                label: "Nodes online", value: `${online.length} of ${nodes.length}`,
                caption: source.cluster
                    && source.cluster.quorate === false
                    ? "Cluster not quorate"
                    : source.cluster
                    && source.cluster.quorate
                    ? "Quorate"
                    : "",
                meter: {
                    total: nodes.length, used: online.length, label: `${online.length} of ${nodes.length} nodes online`,
                    tone: online.length < nodes.length ? "is-warning" : ""
                },
            }),
            statTile({
                label: "Guests running", value: `${running.length} of ${guests.length}`,
                caption: `${vms.length} VM${vms.length === 1 ? "" : "s"} · ${containers.length} container${containers.length === 1 ? "" : "s"}`,
                meter: {
                    total: guests.length, used: running.length,
                    label: `${running.length} of ${guests.length} guests running`
                },
            }),
            statTile({
                label: "CPU now", value: percent(cpuNow), caption: `of ${threads} threads`,
                meter: {
                    total: 1, used: cpuNow || 0, label: `Cluster CPU ${percent(cpuNow)}`, tone: severity(cpuNow || 0)
                },
            }),
            statTile({
                label: "vCPUs assigned", value: `${vcpus}`,
                caption: threads ? `${(vcpus / threads).toFixed(2)}× of ${threads} threads` : "",
                meter: {
                    total: threads, used: Math.min(vcpus, threads),
                    label: `${vcpus} vCPUs assigned to running guests, ${threads} threads`
                },
                tip: [
                    "vCPUs assigned",
                    "Running guests' vCPUs against the online nodes' threads; over 1× is overcommitted."
                ],
            }),
            statTile({
                label: "Memory in use", value: bytes(memUsed), caption: `${percent(installed ? memUsed / installed : null)} of ${bytes(installed)}`,
                meter: {
                    total: installed, used: memUsed, label: `${bytes(memUsed)} of ${bytes(installed)} in use`,
                    tone: severity(installed ? memUsed / installed : 0)
                },
            }),
            statTile({
                label: "Memory allocated", value: bytes(allocated), caption: `${percent(installed ? allocated / installed : null)} of installed`,
                meter: {
                    total: installed, used: allocated,
                    label: `${bytes(allocated)} allocated to running guests of ${bytes(installed)}`
                },
                tip: ["Memory allocated", "The memory configured on running guests."],
            }),
            statTile({
                label: "Storage used", value: bytes(storeUsed), caption: `${percent(storeTotal ? storeUsed / storeTotal : null)} of ${bytes(storeTotal)}`,
                meter: {
                    total: storeTotal, used: storeUsed,
                    label: `${bytes(storeUsed)} of ${bytes(storeTotal)} storage used`,
                    tone: severity(storeTotal ? storeUsed / storeTotal : 0)
                },
            }),
        ]);
        const ceph = source.cluster && source.cluster.ceph;
        if (ceph && ceph.health) {
            const tone = ceph.health === "HEALTH_OK" ? "on" : ceph.health === "HEALTH_WARN" ? "warn" : "alarm";
            tiles.append(statTile({
                label: "Ceph", value: ({
                    HEALTH_OK: "Healthy", HEALTH_WARN: "Warning", HEALTH_ERR: "Error"
                })[ceph.health] || ceph.health,
                caption: `${ceph.osds.up ?? "?"} of ${ceph.osds.total ?? "?"} OSDs up · ${ceph.osds.in ?? "?"} in`,
                lamp: { tone, label: ceph.health },
            }));
        }

        const history = clusterHistory(source);
        const charts = row([
            panel(
                "CPU · 24 h", [area(history, { field: "cpu",  label: "Cluster CPU" })],
                { hue: "compute", aside: "average of all node threads" }
            ),
            panel(
                "Memory · 24 h", [area(history, { field: "mem",  label: "Cluster memory", max: 1 })],
                { hue: "compute", aside: `of ${bytes(installed)}` }
            ),
        ]);

        const donuts = summaryDonuts(source, { guests, running, installed, allocated });
        const radial = row([
            panel("Memory · cluster › node › guest", [sunburst(nodes.map((node) => ({
                label: node.name,
                children: runningGuests(
                    source, node.remote_id
                ).map((guest) => ({ label: guest.name || String(guest.vmid), value: guest.memory || 0 })),
            })), {
                label: "Running guests' memory by node and guest", format: bytes, centre: bytes(allocated),
                caption: "allocated"
            })], { hue: "compute", aside: "inner ring nodes, outer ring guests" }),
            panel(
                "CPU · 24-hour clock", [radialClock(history, { field: "cpu",  label: "Cluster CPU by time of day" })],
                { hue: "compute", aside: "midnight at the top; dashed line is now" }
            ),
        ]);
        const dayRow = row([
            panel("Busiest guests · 24 h", [busiestDay(running)], { hue: "compute", aside: "average CPU, one scale" }),
            panel("Top talkers · 24 h", [topTalkers(running)], { hue: "network", aside: "data moved by each guest" }),
        ]);
        const nodeCards = el("div", { class: "cluster-node-grid" }, nodes.map((node) => nodeCard(source, node)));
        const summaryPools = mergeLocal(pools);
        const storage = storageBars(summaryPools);
        const tasks = panel(
            "Tasks · timeline", [tasksTimeline(source)],
            { hue: "compute", aside: "last 24 hours by node; bars ran over two minutes" }
        );
        root.replaceChildren(attention(
            attentionItems(source, app.dataset.base), { hue: "compute" }
        ), tiles, donuts, tasks, charts, dayRow, radial,
            panel(
                "Nodes", [nodeCards],
                { hue: "compute", aside: "memory: in use · allocated to running guests · installed" }
            ),
            row([
                panel("vCPUs per node", [allocation(nodes.map((node) => ({
                    label: node.name, total: node.maxcpu || 0, used: sum(
                        runningGuests(source, node.remote_id), (g) => g.vcpus
                    ),
                })), { unit: "threads" })], {
                    hue: "compute", aside: "assigned to running guests · line at 100% of threads"
                }),
                panel("Memory per node", [allocation(nodes.map((node) => ({
                    label: node.name, total: node.maxmem || 0, used: sum(
                        runningGuests(source, node.remote_id), (g) => g.memory
                    ),
                })), {
                    format: bytes, unit: "installed"
                })], { hue: "compute", aside: "allocated to running guests · line at installed" }),
            ]),
            row([
                panel(
                    "Busiest guests", [guestBars(running, (g) => g.cpu || 0, percent)],
                    { hue: "compute", aside: "CPU now" }
                ),
                panel(
                    "Largest guests", [guestBars(guests, (g) => g.memory || 0, bytes)],
                    { hue: "compute", aside: "configured memory" }
                ),
                panel("Guests per VLAN", [vlanBars(source)], { hue: "network", aside: "running guests' memory" }),
            ]),
            panel("Storage", [storage], { hue: "compute", aside: `${summaryPools.length} pools` }));
    }

    /**
     * One node's small-multiple card for the Summary.
     * @param {object} source Source view.
     * @param {object} node Node view.
     * @returns {HTMLElement} The card.
     */
    function nodeCard(source, node) {
        const running = runningGuests(source, node.remote_id);
        const all = source.guests.filter((guest) => guest.node === node.remote_id && !guest.template);
        const vcpus = sum(running, (guest) => guest.vcpus);
        const allocated = sum(running, (guest) => guest.memory);
        const isOnline = node.status === "online";
        const memFraction = node.maxmem ? node.mem / node.maxmem : 0;
        const card = el("article", { class: `cluster-card cluster-node${isOnline ? "" : " is-offline"}` }, [
            el("header", { class: "cluster-node-heading" }, [
                lamp(isOnline ? "on" : "alarm", isOnline ? "Online" : "Offline"),
                el("h3", { text: node.name }),
                el("span", { class: "cluster-muted", text: isOnline ? `up ${uptime(node.uptime)}` : "offline" }),
            ]),
            el("p", { class: "cluster-node-model", title: node.cpu_model || "", text: node.cpu_model ? `${node.cpu_model} · ${node.cores ?? "?"} cores / ${node.threads ?? node.maxcpu ?? "?"} threads` : `${node.maxcpu ?? "?"} CPUs` }),
            el("div", { class: "cluster-node-row" }, [
                el("span", { class: "cluster-node-key", text: "CPU" }),
                area(node.history || [], { field: "cpu",  label: `${node.name} CPU`, compact: true }),
                el("strong", { class: "cluster-node-figure", text: percent(node.cpu) }),
            ]),
            el("div", { class: "cluster-node-row" }, [
                el("span", { class: "cluster-node-key", text: "Memory" }),
                meter({
                    total: node.maxmem || 0, used: node.mem || 0, reserved: Math.max(0, allocated - (node.mem || 0)),
                    label: `${bytes(node.mem)} in use, ${bytes(allocated)} allocated, ${bytes(node.maxmem)} installed`, tone: severity(memFraction),
                    tip: [
                        `${node.name} memory`, `In use ${bytes(node.mem)}`,
                        `Allocated to running guests ${bytes(allocated)}`, `Installed ${bytes(node.maxmem)}`
                    ],
                }),
                el("strong", { class: "cluster-node-figure", text: percent(memFraction) }),
            ]),
            el("div", { class: "cluster-node-row" }, [
                el("span", { class: "cluster-node-key", text: "vCPUs" }),
                meter({
                    total: node.maxcpu || 0, used: vcpus, label: `${vcpus} vCPUs assigned of ${node.maxcpu} threads`,
                    tone: vcpus > (node.maxcpu || 0) ? "is-warning" : "",
                    tip: [`${node.name} vCPUs`, `${vcpus} assigned to running guests`, `${node.maxcpu} threads`] }),
                el("strong", { class: "cluster-node-figure", text: `${vcpus}/${node.maxcpu ?? "?"}` }),
            ]),
            el(
                "p",
                {
                    class: "cluster-node-foot",
                    text: `${running.length} of ${all.length} guests running · ${bytes(allocated)} allocated of ${bytes(node.maxmem)}`
                }
            ),
            node.versions
                && (node.versions.reboot_needed || (node.versions.upgradable || []).length)
                ? el("p", { class: "cluster-node-flags" }, [
                node.versions.reboot_needed ? el(
                    "span", { class: "cluster-pill is-warning", text: "reboot pending" }
                ) : null,
                (node.versions.upgradable || []).length ? el(
                    "span", { class: "cluster-pill", text: `${node.versions.upgradable.length} updates` }
                ) : null,
            ]) : null,
        ]);
        return card;
    }

    /**
     * A guest's main address: its first IPv4 address, else its first one.
     * @param {object} guest Guest view.
     * @param {object|null} [nic] Prefer addresses on this NIC (matched by MAC).
     * @returns {string} Address, or "".
     */
    function guestAddress(guest, nic = null) {
        const all = (guest.agent && guest.agent.addresses) || [];
        // In a VLAN group, prefer the address on that VLAN's NIC (by MAC).
        const onNic = nic
            && nic.mac
            ? all.filter((item) => (item.mac || "").toLowerCase() === nic.mac.toLowerCase())
            : [];
        const addresses = onNic.length ? onNic : all;
        const first = addresses.find((item) => item.family === "ipv4") || addresses[0];
        return first ? first.address : "";
    }

    /**
     * Tooltip lines for a guest's agent data: addresses and filesystems.
     * @param {object} guest Guest view.
     * @returns {Array<string>} Lines.
     */
    function agentLines(guest) {
        const agent = guest.agent;
        if (!agent) {
            return guest.kind === "vm" && guest.status === "running" ? ["No guest agent data"] : [];
        }
        return [
            agent.os && agent.os.name ? `${agent.os.name}${agent.os.kernel ? ` · ${agent.os.kernel}` : ""}` : "",
            ...(agent.addresses || []).map((item) => `${item.address}${item.prefix ? `/${item.prefix}` : ""} on ${item.interface}`),
            ...(agent.filesystems
                || []).map((fs) => `${fs.mount} ${percent(fs.total ? (fs.used || 0) / fs.total : null)} of ${bytes(fs.total)}`),
        ].filter(Boolean);
    }

    /**
     * Average of one field over history points.
     * @param {Array<object>|null} points History points.
     * @param {string} field Field name.
     * @returns {number} The average, or -1 with no points.
     */
    function averageOf(points, field) {
        const values = (points || []).map((point) => point[field]).filter((value) => typeof value === "number");
        return values.length ? values.reduce((a, b) => a + b, 0) / values.length : -1;
    }

    /**
     * The top guests by one measure, as ranked bars (widgets.bars).
     * @param {Array<object>} guests Candidate guests.
     * @param {Function} pick The measure.
     * @param {Function} format Value formatter.
     * @returns {HTMLElement} The bar list.
     */
    function guestBars(guests, pick, format) {
        return bars(guests.map((guest) => ({
            label: guest.name || String(guest.vmid), sub: guest.node, value: pick(guest),
            tip: [`${guest.name} (${guest.kind === "container" ? "CT" : "VM"} ${guest.vmid})`, `${format(pick(guest))} on ${guest.node}`],
        })), { format, limit: 8, empty: "Nothing to rank yet." });
    }

    /**
     * Running guests and their memory per VLAN (a guest on two VLANs counts in both).
     * @param {object} source Source view.
     * @returns {HTMLElement} The bar list.
     */
    function vlanBars(source) {
        const totals = new Map();
        for (const guest of runningGuests(source)) {
            const keys = new Set((guest.nics || []).map((nic) => (nic.vlan ? `VLAN ${nic.vlan}` : `untagged ${nic.bridge || ""}`.trim())));
            for (const key of keys.size ? keys : ["no network"]) {
                const item = totals.get(key) || { label: key, guests: 0, value: 0 };
                item.guests += 1;
                item.value += guest.memory || 0;
                totals.set(key, item);
            }
        }
        return bars([...totals.values()].map((item) => ({
            ...item, figure: `${item.guests} · ${bytes(item.value)}`,
            tip: [item.label, `${item.guests} running guest${item.guests === 1 ? "" : "s"}`, `${bytes(item.value)} memory configured`],
        })), { format: bytes });
    }

    /**
     * The day's busiest guests by average CPU, as small multiples.
     * @param {Array<object>} guests Running guests.
     * @returns {HTMLElement} The grid.
     */
    function busiestDay(guests) {
        const ranked = guests.filter((guest) => (guest.history || []).length > 1)
            .map((guest) => ({
                guest, average: averageOf(guest.history, "cpu")
            })).sort((a, b) => b.average - a.average).slice(0, 8);
        return multiples(ranked.map(({ guest, average }) => ({
            label: guest.name, caption: `avg ${percent(average)}`, points: guest.history
        })),
            { field: "cpu", label: "CPU over 24 hours" });
    }

    /**
     * Guests that moved the most data over the day, in and out.
     * @param {Array<object>} guests Running guests.
     * @returns {HTMLElement} The bar list.
     */
    function topTalkers(guests) {
        const seconds = 1800;
        return splitBars(guests.filter((guest) => (guest.history || []).length).map((guest) => {
            const inBytes = sum(guest.history, (point) => (point.net_in || 0) * seconds);
            const outBytes = sum(guest.history, (point) => (point.net_out || 0) * seconds);
            const peak = Math.max(...guest.history.map((point) => Math.max(point.net_in || 0, point.net_out || 0)));
            return { label: guest.name, sub: guest.node, values: [inBytes, outBytes],
                tip: [
                    guest.name, `In ${bytes(inBytes)} · out ${bytes(outBytes)} over 24 h`,
                    `Peak ${window.LabratWidgets.rate(peak)}`
                ] };
        }), { format: bytes, limit: 8 });
    }

    /**
     * The last day's tasks on a time axis, one lane per node: a dot per
     * task (a bar when it ran over two minutes), coloured by outcome.
     * @param {object} source Source view.
     * @returns {HTMLElement} The chart with its key.
     */
    function tasksTimeline(source) {
        const tasks = ((source.cluster && source.cluster.tasks && source.cluster.tasks.recent)
            || []).filter((task) => task.start);
        const tone = { ok: "ok", warning: "warning", error: "alarm", running: "muted" };
        return timeline({
            lanes: source.nodes.map((node) => ({ id: node.remote_id, label: node.name })),
            events: tasks.map((task) => ({
                lane: task.node, start: task.start, end: task.end, tone: tone[task.outcome] || "muted",
                tip: [`${task.type}${task.id ? ` ${task.id}` : ""}`, `${task.node} · ${window.LabratWidgets.clock(task.start)}`,
                    task.end
                        ? `took ${Math.max(1, Math.round((task.end - task.start) / 60))} min`
                        : "still running", task.outcome],
            })),
        }, { label: "Tasks in the last 24 hours by node" });
    }

    /**
     * Usage bars for storage pools, largest first, each against its own size.
     * @param {Array<object>} pools Unique storage pools.
     * @returns {HTMLElement} The bar list.
     */
    function storageBars(pools) {
        return bars([...pools].sort((a, b) => b.total - a.total).map((pool) => {
            const where = pool.shared
                ? "shared"
                : pool.nodes.length > 1
                ? `local on ${pool.nodes.length} nodes`
                : pool.nodes[0];
            return {
                label: pool.storage, sub: `${pool.type} · ${where}`, value: pool.used || 0, total: pool.total,
                tip: [pool.storage, `${pool.type}, ${pool.shared ? "shared" : "local: " + pool.nodes.join(", ")}`,
                    `${bytes(pool.used)} of ${bytes(pool.total)} (${percent(pool.total ? pool.used / pool.total : 0)})`],
            };
        }), { format: bytes, scale: "row", sort: false });
    }

    /**
     * Placement groups by state: one segmented bar, clean in the fine tone.
     * @param {object} ceph Ceph status.
     * @returns {HTMLElement} The bar with its heading and key.
     */
    function pgBar(ceph) {
        return el("div", { class: "cluster-pg" }, [
            el("h3", { class: "cluster-subheading", text: "Placement groups" }),
            segments((ceph.pg_states || []).map((item) => ({
                label: item.state, value: item.count, tone: item.state === "active+clean" ? "ok" : "warning"
            })),
                { label: "Placement groups by state" }),
        ]);
    }

    /**
     * Every OSD's fill as a column, sorted fullest first, with Ceph's
     * near-full (85%) line, so an unbalanced OSD stands out.
     * @param {Array<object>} osds OSD rows.
     * @returns {HTMLElement} The chart with its heading.
     */
    function osdFill(osds) {
        const sorted = [...osds].sort((a, b) => (b.percent_used || 0) - (a.percent_used || 0));
        return el("div", { class: "cluster-osd-fill" }, [
            el("h3", { class: "cluster-subheading", text: "OSD fill, fullest first" }),
            columns(sorted.map((osd) => ({
                label: String(osd.id), value: osd.percent_used
                    || 0, tone: osd.status === "up"
                    && osd.in
                    ? null
                    : "alarm",
                tip: [`${osd.name} on ${osd.host}`, `${(osd.percent_used || 0).toFixed(1)}% used of ${bytes(osd.total)}`, `${osd.device_class || "?"} · ${osd.status}, ${osd.in ? "in" : "out"}`],
            })), {
                label: "OSD fill, fullest first", max: 100, format: (value) => `${Math.round(value)}%`,
                lines: [{ value: 85, warn: true }], warnAt: 85
            }),
        ]);
    }

    /**
     * The Summary's row of donuts: guest states, memory by node, OS mix,
     * backup coverage, and the last day's tasks.
     * @param {object} source Source view.
     * @param {object} facts Guests, running guests, installed and allocated memory.
     * @returns {HTMLElement} The row.
     */
    function summaryDonuts(source, { guests, running, installed, allocated }) {
        const all = source.guests;
        const cluster = source.cluster || {};
        const states = [
            { label: "Running VMs", value: running.filter((g) => g.kind === "vm").length },
            { label: "Running containers", value: running.filter((g) => g.kind === "container").length },
            { label: "Stopped", value: guests.filter((g) => g.status !== "running").length, tone: "muted" },
            { label: "Templates", value: all.filter((g) => g.template).length, tone: "muted" },
        ];
        const byNode = source.nodes.map((node) => ({
            label: node.name, value: sum(runningGuests(source, node.remote_id), (g) => g.memory)
        }));
        const parts = [
            panel(
                "Guest states",
                [donut(
                    states,
                    { label: "Guests by state", centre: String(running.length), caption: `of ${guests.length} running` }
                )],
                { hue: "compute" }
            ),
            panel("Memory by node", [donut([
                ...byNode, { label: "Unallocated", value: Math.max(0, installed - allocated), tone: "muted" }
            ],
                {
                    label: "Allocated memory by node", format: bytes,
                    centre: percent(installed ? allocated / installed : null), caption: "allocated"
                })], { hue: "compute" }),
            panel(
                "Operating systems",
                [donut(tally(guests, guestOs), { label: "Guests by operating system", pie: true })],
                { hue: "compute", aside: "guest agent, else OS type" }
            ),
        ];
        if (Array.isArray(cluster.not_backed_up)) {
            const unprotected = new Set(cluster.not_backed_up);
            const missing = guests.filter((g) => unprotected.has(g.vmid)).length;
            parts.push(panel("Backup coverage", [donut([
                { label: "In a backup job", value: guests.length - missing, tone: "ok" },
                { label: "Not in any backup job", value: missing, tone: missing ? "warning" : "muted",
                  detail: guests.filter((g) => unprotected.has(g.vmid)).map((g) => g.name).slice(0, 12).join(", ") },
            ], {
                label: "Guests covered by a backup job", centre: `${guests.length - missing}/${guests.length}`,
                caption: "covered"
            })],
            { hue: "compute", aside: "templates not counted" }));
        }
        if (cluster.tasks) {
            const tasks = cluster.tasks;
            const total = tasks.ok + tasks.warning + tasks.error + tasks.running;
            parts.push(panel("Tasks · 24 h", [donut([
                { label: "OK", value: tasks.ok, tone: "ok" },
                { label: "Warnings", value: tasks.warning, tone: "warning" },
                { label: "Failed", value: tasks.error, tone: "alarm", detail: (tasks.failures || []).map((f) => `${f.type}${f.id ? ` ${f.id}` : ""} on ${f.node}: ${f.status}`).slice(0, 5).join(" · ") },
                { label: "Running", value: tasks.running, tone: "muted" },
            ], { label: "Cluster tasks in the last 24 hours by outcome", centre: String(total), caption: "tasks" }),
            donut(Object.entries(tasks.by_type || {}).map(([type, counts]) => ({
                label: type, value: counts.ok + counts.warning + counts.error + counts.running
            })),
                { label: "Tasks by type", pie: true })], { hue: "compute", aside: "outcome, then type" }));
        }
        const disks = guests.flatMap((guest) => ((guest.agent && guest.agent.filesystems)
            || []).filter((fs) => fs.total)
            .map((fs) => ({ guest, fs, fraction: (fs.used || 0) / fs.total })))
            .sort((a, b) => b.fraction - a.fraction).slice(0, 8);
        if (disks.length) {
            parts.push(panel("Fullest guest disks", [bars(disks.map(({ guest, fs, fraction }) => ({
                label: guest.name, sub: fs.mount, value: fraction, total: 1, figure: percent(fraction),
                tip: [
                    `${guest.name}: ${fs.mount}`, `${bytes(fs.used)} of ${bytes(fs.total)} (${percent(fraction)})`,
                    fs.type || ""
                ],
            })), { scale: "row", sort: false })], { hue: "compute", aside: "from guest agents" }));
        }
        return row(parts);
    }

    // ------------------------------------------------------------------
    // Compute
    // ------------------------------------------------------------------

    /**
     * Guests that pass the Compute filters.
     * @param {object} source Source view.
     * @returns {Array<object>} Guests.
     */
    function filteredGuests(source) {
        return source.guests.filter((guest) => state.kinds[guest.kind] !== false
            && (!state.runningOnly || guest.status === "running")
            && (state.templates || !guest.template));
    }

    /**
     * Draw the Compute tab: guest cards grouped by VLAN or node.
     * @param {HTMLElement} root Tab panel.
     * @param {object} source Source view.
     */
    function drawCompute(root, source) {
        const guests = filteredGuests(source);
        const groups = new Map();
        const add = (key, label, sort, guest, note, nic = null) => {
            const group = groups.get(key) || { label, sort, guests: [] };
            group.guests.push({ guest, note, nic });
            groups.set(key, group);
        };
        for (const guest of guests) {
            if (state.groupBy === "node") {
                add(`node:${guest.node}`, guest.node, guest.node, guest);
                continue;
            }
            const vlans = new Map();
            for (const nic of guest.nics || []) {
                vlans.set(nic.vlan ?? "untagged", nic);
            }
            if (!vlans.size) {
                add("none", "No network", "zzz", guest);
            }
            for (const [vlan, nic] of vlans) {
                if (vlan === "untagged") {
                    add(`bridge:${nic.bridge}`, `Untagged · ${nic.bridge || "?"}`, `y${nic.bridge}`, guest, null, nic);
                } else {
                    add(`vlan:${vlan}`, `VLAN ${vlan}`, String(vlan).padStart(5, "0"), guest, vlans.size > 1 ? `also on ${[...vlans.keys()].filter((v) => v !== vlan).map((v) => (v === "untagged" ? "untagged" : `VLAN ${v}`)).join(", ")}` : null, nic);
                }
            }
        }
        const sorted = [...groups.values()].sort((a, b) => a.sort.localeCompare(b.sort));
        if (!sorted.length) {
            root.replaceChildren(el("p", { class: "cluster-empty", text: "No guests match these filters." }));
            return;
        }
        if (state.computeStyle === "list") {
            root.replaceChildren(guestList(sorted));
            return;
        }
        if (state.computeStyle === "map") {
            root.replaceChildren(guestMap(sorted));
            return;
        }
        root.replaceChildren(el("div", { class: "cluster-groups" }, sorted.map((group) => {
            const members = group.guests.map((item) => item.guest);
            const running = members.filter((guest) => guest.status === "running");
            const aside = `${members.length} guest${members.length === 1 ? "" : "s"} · ${sum(running, (g) => g.vcpus)} vCPU · ${bytes(sum(running, (g) => g.memory))} running`;
            return panel(
                group.label,
                [el(
                    "div", { class: "cluster-guest-grid" },
                    group.guests.map((item) => guestCard(item.guest, item.note, item.nic))
                )],
                { hue: "compute", aside, className: "cluster-group" }
            );
        })));
    }

    /**
     * Guests as a ledger list: one sortable table, a heading row per group.
     * @param {Array<object>} groups Sorted groups from drawCompute.
     * @returns {HTMLElement} The list panel.
     */
    function guestList(groups) {
        const columns = [
            ["name", "Guest"], ["vmid", "ID"], ["where", state.groupBy === "node" ? "Network" : "Node"],
            ["address", "Address"], ["os", "OS"], ["diskuse", "Disk use"],
            ["vcpus", "vCPU"], ["memory", "Memory"], ["disk", "Disk"], ["cpu", "CPU"], [
                "cpuday", "CPU · 24 h"
            ], ["mem", "Mem use"], ["uptime", "Up"],
        ];
        const value = (guest, key) => ({
            name: (guest.name || "").toLowerCase(), vmid: guest.vmid, vcpus: guest.vcpus
                || 0, memory: guest.memory
                || 0, disk: guest.disk
                || 0,
            cpu: guest.status === "running"
                ? guest.cpu
                || 0
                : -1, mem: guest.memory
                && guest.mem_used
                ? guest.mem_used / guest.memory
                : -1,
            uptime: guest.uptime || 0, where: whereText(guest).toLowerCase(),
            cpuday: averageOf(guest.history, "cpu"),
            address: guestAddress(guest).split(".").map((part) => part.padStart(3, "0")).join("."),
            os: guestOs(guest).toLowerCase(), diskuse: fullestDisk(guest) ? fullestDisk(guest).fraction : -1,
        })[key];
        const { key: sortKey, dir } = state.listSort;
        const table = el("table", { class: "cluster-list" });
        const headRow = el("tr");
        for (const [key, label] of columns) {
            const sorted = key === sortKey;
            const th = el(
                "th", { scope: "col", "aria-sort": sorted ? (dir > 0 ? "ascending" : "descending") : "none" }
            );
            const button = el(
                "button", { type: "button", text: `${label} ` },
                [el("span", { "aria-hidden": "true", text: sorted ? (dir > 0 ? "↑" : "↓") : "↕" })]
            );
            button.addEventListener("click", () => {
                state.listSort = {
                    key, dir: sorted ? -dir : key === "name" || key === "where" || key === "vmid" ? 1 : -1
                };
                render();
            });
            th.append(button);
            headRow.append(th);
        }
        table.append(el("thead", {}, [headRow]));
        for (const group of groups) {
            const body = el("tbody");
            body.append(el(
                "tr", { class: "cluster-list-group" },
                [el(
                    "th",
                    { scope: "rowgroup", colspan: columns.length, text: `${group.label} · ${group.guests.length}` }
                )]
            ));
            const rows = [...group.guests].sort((a, b) => {
                const x = value(a.guest, sortKey);
                const y = value(b.guest, sortKey);
                return (x < y ? -1 : x > y ? 1 : 0) * dir;
            });
            for (const { guest, nic } of rows) {
                const running = guest.status === "running";
                const tone = running ? "on" : guest.status === "paused" ? "warn" : "off";
                const memFraction = guest.memory && guest.mem_used ? guest.mem_used / guest.memory : 0;
                const row = el("tr", { class: running ? "" : "is-stopped", tabindex: 0 }, [
                    el(
                        "td", { class: "cluster-list-name" },
                        [
                            lamp(tone, guest.template ? "Template" : guest.status || "unknown"),
                            el("strong", { text: guest.name || String(guest.vmid) })
                        ]
                    ),
                    el("td", { text: `${guest.kind === "container" ? "CT" : "VM"} ${guest.vmid}` }),
                    el("td", { text: whereText(guest) }),
                    el(
                        "td",
                        {
                            class: "cluster-list-ip", text: guestAddress(guest, nic) || "—",
                            title: agentLines(guest).join("\n")
                        }
                    ),
                    el("td", { class: "cluster-list-os", text: guestOs(guest), title: guestOs(guest) }),
                    el(
                        "td", { class: "cluster-list-meter" },
                        fullestDisk(guest) ? [
                            meter({
                                total: 1, used: fullestDisk(guest).fraction,
                                label: `Disk ${percent(fullestDisk(guest).fraction)}`,
                                tone: severity(fullestDisk(guest).fraction)
                            }),
                            el("span", { text: percent(fullestDisk(guest).fraction) })
                        ] : []
                    ),
                    el("td", { class: "is-number", text: String(guest.vcpus ?? "—") }),
                    el("td", { class: "is-number", text: bytes(guest.memory) }),
                    el("td", { class: "is-number", text: bytes(guest.disk) }),
                    el(
                        "td", { class: "cluster-list-meter" },
                        running ? [
                            meter({
                                total: 1, used: guest.cpu || 0, label: `CPU ${percent(guest.cpu)}`,
                                tone: severity(guest.cpu || 0)
                            }),
                            el("span", { text: percent(guest.cpu) })
                        ] : []
                    ),
                    el(
                        "td", { class: "cluster-list-spark" },
                        running && (guest.history || []).length > 1 ? [area(
                            guest.history,
                            {
                                field: "cpu", label: `${guest.name} CPU over 24 hours`, compact: true,
                                max: Math.max(0.05, ...guest.history.map((point) => point.cpu || 0))
                            }
                        )] : []
                    ),
                    el(
                        "td", { class: "cluster-list-meter" },
                        running ? [
                            meter({
                                total: guest.memory || 0, used: guest.mem_used || 0,
                                label: `Memory ${percent(memFraction)}`
                            }),
                            el("span", { text: percent(memFraction) })
                        ] : []
                    ),
                    el("td", { class: "is-number", text: running ? uptime(guest.uptime) : "—" }),
                ]);
                body.append(row);
            }
            table.append(body);
        }
        return panel(
            "Guests", [el("div", { class: "cluster-list-scroll", tabindex: 0 }, [table])],
            { hue: "compute", aside: state.groupBy === "node" ? "grouped by node" : "grouped by VLAN" }
        );
    }

    /**
     * Where a guest is, for the list's third column: its node when grouped
     * by VLAN, its networks when grouped by node.
     * @param {object} guest Guest view.
     * @returns {string} Display text.
     */
    function whereText(guest) {
        if (state.groupBy !== "node") {
            return guest.node;
        }
        const nets = [...new Set((guest.nics
            || []).map((nic) => (nic.vlan ? `VLAN ${nic.vlan}` : nic.bridge || null)).filter(Boolean))];
        return nets.join(", ") || "no network";
    }

    /**
     * Guests as a treemap: each group a block sized by its guests' memory,
     * each guest a tile sized by its configured memory and shaded by state.
     * @param {Array<object>} groups Sorted groups from drawCompute.
     * @returns {HTMLElement} The map panel.
     */
    function guestMap(groups) {
        const map = treemap(groups.map((group) => ({
            label: group.label,
            children: group.guests.map(({ guest }) => {
                const running = guest.status === "running";
                const memFraction = guest.memory && guest.mem_used ? guest.mem_used / guest.memory : 0;
                return {
                    label: guest.name || String(guest.vmid), value: guest.memory || 0, dim: !running,
                    fill: running ? memFraction : null, detail: `${guest.vcpus ?? "?"} vCPU · ${bytes(guest.memory)}`,
                    tip: [`${guest.name} (${guest.kind === "container" ? "CT" : "VM"} ${guest.vmid})`, `${guest.status} on ${guest.node}`,
                        `${guest.vcpus ?? "?"} vCPU · ${bytes(guest.memory)} memory`, running ? `${bytes(guest.mem_used)} in use (${percent(memFraction)})` : ""],
                };
            }),
        })), { label: "Guests sized by memory", format: bytes });
        return panel("Guests by memory", [map,
            el(
                "p", { class: "cluster-legend" },
                [el(
                    "span",
                    { text: "Tile area is configured memory; the bar along a running guest's foot is memory in use. Dim tiles are stopped." }
                )]
            )],
        { hue: "compute", aside: state.groupBy === "node" ? "blocks are nodes" : "blocks are VLANs" });
    }

    /**
     * One guest card.
     * @param {object} guest Guest view.
     * @param {string|null} note Extra line (other VLANs it is on).
     * @param {object|null} [nic] The NIC that put it in this group, if any.
     * @returns {HTMLElement} The card.
     */
    function guestCard(guest, note, nic = null) {
        const tone = guest.status === "running" ? "on" : guest.status === "paused" ? "warn" : "off";
        const vlanText = [...new Set((guest.nics
            || []).map((nic) => (nic.vlan ? `VLAN ${nic.vlan}` : nic.bridge ? nic.bridge : null)).filter(Boolean))];
        const memFraction = guest.memory && guest.mem_used ? guest.mem_used / guest.memory : 0;
        const where = state.groupBy === "node" ? vlanText.join(", ") || "no network" : guest.node;
        const card = el("article", { class: `cluster-card cluster-guest${guest.status === "running" ? "" : " is-stopped"}${guest.template ? " is-template" : ""}`, tabindex: 0 }, [
            el("header", { class: "cluster-guest-heading" }, [
                lamp(tone, guest.template ? "Template" : guest.status || "unknown"),
                el("h3", { text: guest.name || String(guest.vmid) }),
                el("span", { class: "cluster-guest-id", text: `${guest.kind === "container" ? "CT" : "VM"} ${guest.vmid}` }),
            ]),
            el("p", { class: "cluster-guest-spec", text: `${guest.vcpus ?? "?"} vCPU · ${bytes(guest.memory)} · ${bytes(guest.disk)}` }),
            guest.status === "running" ? el("div", { class: "cluster-guest-meters" }, [
                el(
                    "span", { class: "cluster-node-key", text: "CPU" }
                ), meter({
                    total: 1, used: guest.cpu || 0, label: `CPU ${percent(guest.cpu)}`, tone: severity(guest.cpu || 0)
                }),
                el(
                    "span", { class: "cluster-node-key", text: "Mem" }
                ), // No severity: a VM's reported use includes its page cache.
                meter({ total: guest.memory || 0, used: guest.mem_used || 0, label: `Memory ${percent(memFraction)}` }),
            ]) : null,
            guest.status === "running"
                && (guest.history || []).length > 1
                ? el("div", { class: "cluster-guest-spark" }, [
                el("span", { class: "cluster-node-key", text: "24 h" }),
                area(
                    guest.history,
                    {
                        field: "cpu", label: `${guest.name} CPU over 24 hours`, compact: true,
                        max: Math.max(0.05, ...guest.history.map((point) => point.cpu || 0))
                    }
                ),
                el(
                    "span",
                    {
                        class: "cluster-node-figure",
                        text: `↑${percent(Math.max(...guest.history.map((point) => point.cpu || 0)))}`
                    }
                ),
            ]) : null,
            guestAddress(guest, nic) || guest.agent ? el("p", { class: "cluster-guest-agent" }, [
                el("span", { class: "cluster-guest-ip", text: guestAddress(guest, nic) || "no address" }),
                el("span", { class: "cluster-muted", text: ` · ${guestOs(guest)}` }),
            ]) : null,
            fullestDisk(guest) ? el("div", { class: "cluster-guest-meters is-disk" }, [
                el("span", { class: "cluster-node-key", text: "Disk" }),
                meter({
                    total: 1, used: fullestDisk(guest).fraction,
                    label: `Fullest filesystem ${fullestDisk(guest).mount} ${percent(fullestDisk(guest).fraction)}`,
                    tone: severity(fullestDisk(guest).fraction)
                }),
                el("span", { class: "cluster-node-figure", text: percent(fullestDisk(guest).fraction) }),
            ]) : null,
            el(
                "p", { class: "cluster-guest-where" },
                [el("span", { text: where }), note ? el("span", { class: "cluster-muted", text: ` · ${note}` }) : null]
            ),
        ]);
        tip(card, [
            `${guest.name} (${guest.kind === "container" ? "container" : "VM"} ${guest.vmid})`,
            `${guest.template ? "Template" : guest.status} on ${guest.node}${guest.uptime ? `, up ${uptime(guest.uptime)}` : ""}`,
            `${guest.vcpus ?? "?"} vCPU, ${bytes(guest.memory)} memory, ${bytes(guest.disk)} disk`,
            guest.status === "running" ? `CPU ${percent(guest.cpu)}, memory ${bytes(guest.mem_used)} in use` : "",
            ...(guest.nics || []).map((nic) => `${nic.bridge || "?"}${nic.vlan ? ` · VLAN ${nic.vlan} (${nic.vlan_source === "tag" ? "tag" : "bridge"})` : " · untagged"}`),
            guest.onboot ? "Starts at boot" : "",
            guest.tags && guest.tags.length ? `Tags: ${guest.tags.join(", ")}` : "",
            ...agentLines(guest),
        ].filter(Boolean));
        return card;
    }

    // ------------------------------------------------------------------
    // Networks
    // ------------------------------------------------------------------

    /**
     * Draw the Networks tab: a grid of bridges (rows) by nodes (columns).
     * @param {HTMLElement} root Tab panel.
     * @param {object} source Source view.
     */
    function drawNetworks(root, source) {
        if (state.networkStyle === "diagram") {
            drawNetworkDiagram(root, source);
            return;
        }
        const nodes = source.nodes;
        const table = el("table", { class: "cluster-matrix" });
        table.append(el("thead", {}, [el("tr", {}, [
            el("th", { scope: "col", text: "Bridge" }),
            el("th", { scope: "col", text: "VLAN" }),
            el("th", { scope: "col", text: "Guests" }),
            ...nodes.map((node) => el("th", { scope: "col", class: "cluster-matrix-node", text: node.name })),
        ])]));
        const body = el("tbody");
        for (const network of source.networks) {
            const present = nodes.filter((node) => network.nodes[node.remote_id]);
            const missing = nodes.length - present.length;
            const risky = missing > 0 && network.guests > 0;
            const row = el("tr", { class: risky ? "is-partial" : "" }, [
                el("th", { scope: "row" }, [el("strong", { text: network.bridge })]),
                el(
                    "td",
                    {
                        class: "cluster-matrix-vlan",
                        text: network.vlan ? String(network.vlan) : network.vlan_aware ? "trunk" : "untagged"
                    }
                ),
                el("td", { class: "cluster-matrix-count", text: String(network.guests) }),
            ]);
            for (const node of nodes) {
                const bridge = network.nodes[node.remote_id];
                const cell = el("td", { class: `cluster-matrix-cell${bridge ? " is-present" : " is-missing"}`, tabindex: 0 });
                cell.append(el("span", { class: "cluster-matrix-dot", "aria-hidden": "true" }));
                cell.append(el(
                    "span",
                    {
                        class: "visually-hidden",
                        text: bridge ? `${network.bridge} on ${node.name}` : `${network.bridge} missing on ${node.name}`
                    }
                ));
                if (bridge && bridge.cidr) {
                    cell.append(el("span", { class: "cluster-matrix-ip", text: bridge.cidr.split("/")[0] }));
                }
                tip(cell, bridge ? [
                    `${network.bridge} on ${node.name}`,
                    bridge.vlan ? `VLAN ${bridge.vlan}` : bridge.vlan_aware ? `VLAN-aware${bridge.vids ? ` (${bridge.vids})` : ""}` : "Untagged",
                    bridge.uplink ? `Uplink ${bridge.uplink}` : "No uplink (internal)",
                    bridge.cidr ? `Host address ${bridge.cidr}` : "",
                ].filter(Boolean) : [
                    `${network.bridge} is not on ${node.name}`,
                    risky ? "Guests on this bridge cannot migrate here." : ""
                ].filter(Boolean));
                row.append(cell);
            }
            body.append(row);
        }
        table.append(body);
        const partial = source.networks.filter((network) => network.guests > 0
            && Object.keys(network.nodes).length < nodes.length).length;
        root.replaceChildren(panel("Bridges by node", [
            el("div", { class: "cluster-matrix-scroll", tabindex: 0 }, [table]),
            el("p", { class: "cluster-legend" }, [
                el("span", { class: "cluster-legend-dot is-partial", "aria-hidden": "true" }),
                el("span", { text: "Missing on a node while guests use it: those guests cannot migrate there." }),
            ]),
        ], {
            hue: "network",
            aside: partial ? `${partial} bridge${partial === 1 ? "" : "s"} with guests missing on some nodes` : "every bridge in use is on every node",
        }));
    }

    /**
     * Draw Networks as a diagram: nodes, bridges, and VLANs in three
     * columns, linked where a node carries a bridge and a bridge carries a
     * VLAN. Hovering or focusing any box lights everything connected to it.
     * @param {HTMLElement} root Tab panel.
     * @param {object} source Source view.
     */
    function drawNetworkDiagram(root, source) {
        const nodes = source.nodes;
        const networks = source.networks;
        const vlanKey = (network) => (network.vlan
            ? `vlan:${network.vlan}`
            : network.vlan_aware
            ? "trunk"
            : "untagged");
        const vlanLabel = (key) => (key.startsWith("vlan:")
            ? `VLAN ${key.slice(5)}`
            : key === "trunk"
            ? "VLAN-aware"
            : "Untagged");
        const vlans = [...new Set(networks.map(vlanKey))];
        const row = 44;
        const width = 900;
        const height = Math.max(nodes.length, networks.length, vlans.length) * row + 40;
        const columns = { node: 110, bridge: 450, vlan: 790 };
        const boxWidth = { node: 170, bridge: 180, vlan: 150 };
        // Spread each column over the full height so short columns centre.
        const place = (count, index) => 20 + (height - 40) * ((index + 0.5) / count);
        const chart = svg(
            "svg",
            {
                class: "cluster-diagram-svg", viewBox: `0 0 ${width} ${height}`, role: "group",
                "aria-label": "Bridges on each node and the VLANs they carry"
            }
        );
        const links = svg("g", { class: "cluster-diagram-links" });
        const boxes = svg("g", { class: "cluster-diagram-boxes" });
        chart.append(links, boxes);
        const positions = new Map();
        const items = [];
        const neighbours = new Map();
        const connect = (a, b) => {
            for (const [from, to] of [[a, b], [b, a]]) {
                const set = neighbours.get(from) || new Set();
                set.add(to);
                neighbours.set(from, set);
            }
        };
        nodes.forEach((node, index) => positions.set(
            `node:${node.remote_id}`, { x: columns.node, y: place(nodes.length, index) }
        ));
        networks.forEach((network, index) => positions.set(
            `bridge:${network.bridge}`, { x: columns.bridge, y: place(networks.length, index) }
        ));
        vlans.forEach((key, index) => positions.set(`v:${key}`, { x: columns.vlan, y: place(vlans.length, index) }));

        /**
         * Draw one curved link between two boxes.
         * @param {string} a Left item ID.
         * @param {string} b Right item ID.
         * @param {string} kind Link class modifier.
         */
        const link = (a, b, kind) => {
            const p = positions.get(a);
            const q = positions.get(b);
            const x1 = p.x + boxWidth[a.startsWith("node") ? "node" : "bridge"] / 2;
            const x2 = q.x - boxWidth[b.startsWith("bridge") ? "bridge" : "vlan"] / 2;
            const mid = (x1 + x2) / 2;
            const path = svg(
                "path",
                {
                    class: `cluster-diagram-link ${kind}`, d: `M${x1},${p.y}C${mid},${p.y} ${mid},${q.y} ${x2},${q.y}`,
                    "data-a": a, "data-b": b
                }
            );
            links.append(path);
            connect(a, b);
        };
        for (const network of networks) {
            for (const node of nodes) {
                if (network.nodes[node.remote_id]) {
                    link(`node:${node.remote_id}`, `bridge:${network.bridge}`, "");
                }
            }
            link(`bridge:${network.bridge}`, `v:${vlanKey(network)}`, "is-vlan");
        }

        /**
         * Draw one labelled box.
         * @param {string} id Item ID.
         * @param {string} column Column name.
         * @param {string} title Main label.
         * @param {string} detail Second label.
         * @param {Array<string>} tipLines Tooltip lines.
         * @param {string} [extra] Extra class names.
         */
        const box = (id, column, title, detail, tipLines, extra = "") => {
            const { x, y } = positions.get(id);
            const w = boxWidth[column];
            const group = svg(
                "g",
                {
                    class: `cluster-diagram-item is-${column} ${extra}`, tabindex: 0, "data-id": id, role: "img",
                    "aria-label": tipLines.join(". ")
                }
            );
            group.append(svg(
                "rect", { x: x - w / 2, y: y - 16, width: w, height: 32, rx: column === "node" ? 6 : 16 }
            ));
            const label = svg("text", { class: "cluster-diagram-title", x: x - w / 2 + 12, y: y + (detail ? -1 : 4) });
            label.textContent = title;
            group.append(label);
            if (detail) {
                const small = svg("text", { class: "cluster-diagram-detail", x: x - w / 2 + 12, y: y + 11 });
                small.textContent = detail;
                group.append(small);
            }
            boxes.append(group);
            items.push(group);
            tip(group, tipLines);
        };
        for (const node of nodes) {
            const count = networks.filter((network) => network.nodes[node.remote_id]).length;
            box(`node:${node.remote_id}`, "node", node.name, `${count} bridge${count === 1 ? "" : "s"}`,
                [
                    node.name, `${count} of ${networks.length} bridges configured`
                ], node.status === "online" ? "" : "is-offline");
        }
        for (const network of networks) {
            const present = nodes.filter((node) => network.nodes[node.remote_id]);
            const missing = nodes.filter((node) => !network.nodes[node.remote_id]);
            const risky = missing.length > 0 && network.guests > 0;
            box(`bridge:${network.bridge}`, "bridge", network.bridge,
                `${network.guests} guest${network.guests === 1 ? "" : "s"}${missing.length ? ` · ${present.length}/${nodes.length} nodes` : ""}`,
                [network.bridge, `${network.guests} guest NIC${network.guests === 1 ? "" : "s"}`, `On ${present.map((node) => node.name).join(", ") || "no nodes"}`,
                    missing.length ? `Missing on ${missing.map((node) => node.name).join(", ")}${risky ? ": its guests cannot migrate there" : ""}` : "On every node"],
                risky ? "is-partial" : "");
        }
        for (const key of vlans) {
            const bridges = networks.filter((network) => vlanKey(network) === key).map((network) => network.bridge);
            box(`v:${key}`, "vlan", vlanLabel(key), `${bridges.length} bridge${bridges.length === 1 ? "" : "s"}`, [vlanLabel(key), `Carried by ${bridges.join(", ")}`]);
        }
        // Column headings.
        for (const [column, text] of [["node", "Nodes"], ["bridge", "Bridges"], ["vlan", "VLANs"]]) {
            const heading = svg(
                "text", { class: "cluster-diagram-heading", x: columns[column], y: 12, "text-anchor": "middle" }
            );
            heading.textContent = text;
            chart.append(heading);
        }

        /**
         * Light one item's connections across all three columns.
         * @param {string|null} id Item ID, or null to clear.
         */
        const focus = (id) => {
            if (!id) {
                chart.classList.remove("is-focused");
                return;
            }
            // A node lights its bridges and their VLANs; a VLAN its bridges
            // and their nodes; a bridge both sides.
            const lit = new Set([id]);
            const walk = (from, direction) => {
                for (const next of neighbours.get(from) || []) {
                    const order = { node: 0, bridge: 1, v: 2 };
                    const step = Math.sign(order[next.split(":")[0]] - order[from.split(":")[0]]);
                    if (step === direction && !lit.has(next)) {
                        lit.add(next);
                        walk(next, direction);
                    }
                }
            };
            walk(id, 1);
            walk(id, -1);
            chart.classList.add("is-focused");
            for (const item of items) {
                item.classList.toggle("is-lit", lit.has(item.dataset.id));
            }
            for (const path of links.children) {
                path.classList.toggle("is-lit", lit.has(path.dataset.a) && lit.has(path.dataset.b));
            }
        };
        for (const item of items) {
            item.addEventListener("pointerenter", () => focus(item.dataset.id));
            item.addEventListener("focus", () => focus(item.dataset.id));
            item.addEventListener("pointerleave", () => focus(null));
            item.addEventListener("blur", () => focus(null));
        }
        const partial = networks.filter((network) => network.guests > 0
            && Object.keys(network.nodes).length < nodes.length).length;
        root.replaceChildren(panel("Bridges by node", [
            el("div", { class: "cluster-diagram", tabindex: -1 }, [chart]),
            el("p", { class: "cluster-legend" }, [
                el("span", { class: "cluster-legend-key is-partial", "aria-hidden": "true" }),
                el(
                    "span",
                    { text: "A bridge in use that some nodes lack; its guests cannot migrate there. Hover a box to trace it." }
                ),
            ]),
        ], {
            hue: "network",
            aside: partial ? `${partial} bridge${partial === 1 ? "" : "s"} with guests missing on some nodes` : "every bridge in use is on every node",
        }));
    }

    // ------------------------------------------------------------------
    // Storage
    // ------------------------------------------------------------------

    /**
     * Draw the Storage tab: engines, Ceph, and ZFS.
     * @param {HTMLElement} root Tab panel.
     * @param {object} source Source view.
     */
    function drawStorage(root, source) {
        const config = (source.cluster && source.cluster.storage_config) || [];
        const pools = storagePools(source);
        const engines = new Map();
        for (const item of config) {
            const engine = engines.get(item.type) || [];
            engine.push(item);
            engines.set(item.type, engine);
        }
        const nodes = source.nodes;
        const engineCards = el("div", { class: "cluster-engine-grid" }, [...engines.entries()].sort().map(([type, items]) =>
            el("article", { class: "cluster-card cluster-engine" }, [
                el(
                    "header", { class: "cluster-engine-heading" },
                    [el("h3", { text: engineName(type) }), el("span", { class: "cluster-muted", text: type })]
                ),
                el("ul", { class: "cluster-engine-list" }, items.map((item) => {
                    const used = pools.filter((pool) => pool.storage === item.storage);
                    const total = sum(used, (pool) => pool.total);
                    const usedBytes = sum(used, (pool) => pool.used);
                    const where = item.nodes.length ? item.nodes : nodes.map((node) => node.remote_id);
                    const row = el("li", { class: item.disabled ? "is-disabled" : "", tabindex: 0 }, [
                        el(
                            "span", { class: "cluster-engine-name" },
                            [
                                el("strong", { text: item.storage }),
                                el(
                                    "span",
                                    {
                                        class: "cluster-muted",
                                        text: item.disabled ? " disabled" : item.shared ? " shared" : ""
                                    }
                                )
                            ]
                        ),
                        el("span", { class: "cluster-engine-nodes" }, nodes.map((node) => el("span", {
                            class: `cluster-node-pip${where.includes(node.remote_id) ? " is-on" : ""}`, title: node.name, "aria-label": `${node.name}: ${where.includes(node.remote_id) ? "enabled" : "not enabled"}`, role: "img",
                        }))),
                        total ? meter({
                            total, used: usedBytes, label: `${bytes(usedBytes)} of ${bytes(total)}`,
                            tone: severity(usedBytes / total)
                        }) : el("span", { class: "cluster-muted", text: "no usage reported" }),
                    ]);
                    tip(row, [item.storage, `${engineName(type)}${item.shared ? ", shared" : ""}${item.disabled ? ", disabled" : ""}`,
                        `Enabled on ${item.nodes.length ? item.nodes.join(", ") : "all nodes"}`, `Content: ${item.content.join(", ") || "—"}`,
                        total ? `${bytes(usedBytes)} of ${bytes(total)} used` : ""].filter(Boolean));
                    return row;
                })),
            ])));
        const ceph = source.cluster && source.cluster.ceph;
        const zfs = nodes.flatMap((node) => (node.zfs || []).map((pool) => ({
            ...pool, node: node.name, nodeRecord: node
        })));
        const zfsBad = zfs.some((pool) => (pool.state || pool.health) !== "ONLINE");
        const subs = [{ key: "overview", label: "Overview" }];
        if (ceph || (source.cluster && source.cluster.uses_ceph)) {
            subs.push({
                key: "ceph", label: "Ceph",
                detail: ceph && Array.isArray(ceph.pool_list) ? String(ceph.pool_list.length) : null,
                tone: ceph
                    && ceph.health === "HEALTH_ERR"
                    ? "alarm"
                    : ceph
                    && ceph.health === "HEALTH_WARN"
                    ? "warning"
                    : null });
        }
        if (zfs.length) {
            subs.push({ key: "zfs", label: "ZFS", detail: String(zfs.length), tone: zfsBad ? "warning" : null });
        }
        if (!subs.some((item) => item.key === state.storageSub)) {
            state.storageSub = "overview";
        }
        const strip = window.LabratViewFrame.placeSubtabs({
            label: "Storage sections", items: subs, current: state.storageSub,
            tab: app.querySelector('[data-tab="storage"]'),
            onSelect: (key) => { state.storageSub = key; state.cephPool = "overview"; syncQuery(); render(); },
        });
        const rows = strip ? [strip] : [];
        if (state.storageSub === "overview") {
            const merged = mergeLocal(pools);
            // Every drive in the cluster as a shelf on top (owner), the
            // engine cards, donuts, and pool bars kept below it. No health
            // box here; each node one flat grid, packed as masonry, so the
            // whole cluster fits a 1600px screen (owner).
            const drives = Shelves.clusterDrives(source);
            rows.push(panel("Every drive", [shelf({ chassis: drives.chassis }, { masonry: true, flat: true })],
                { hue: "compute", aside: `${drives.health.subtitle} · by node and role` }));
            rows.push(panel("Storage engines", [engineCards], { hue: "compute", aside: `${config.length} storage${config.length === 1 ? "" : "s"} configured` }),
                row([
                    panel("Capacity by engine", [donut(tally(
                        merged, (pool) => engineName(pool.type), (pool) => pool.total
                    ),
                        {
                            label: "Storage capacity by engine", format: bytes,
                            centre: bytes(sum(merged, (pool) => pool.total)), caption: "total"
                        })], { hue: "compute", aside: "shared pools once" }),
                    panel("Used by pool", [donut([...merged.map((pool) => ({
                        label: pool.storage, value: pool.used || 0
                    })).sort((a, b) => b.value - a.value),
                        {
                            label: "Free", value: sum(merged, (pool) => Math.max(0, pool.total - (pool.used || 0))),
                            tone: "muted"
                        }],
                    {
                        label: "Storage use by pool", format: bytes,
                        centre: percent(sum(merged, (p) => p.used || 0) / Math.max(1, sum(merged, (p) => p.total))),
                        caption: "used"
                    })], { hue: "compute" }),
                ]),
                panel("Pools", [storageBars(merged)], { hue: "compute", aside: "used of capacity" }));
        } else if (state.storageSub === "zfs") {
            const drawn = state.zfsStyle === "cards"
                ? el("div", { class: "cluster-zfs-grid" }, zfs.map(zfsCard))
                : el(
                    "div", { class: "shelf-pools" }, zfs.map((pool) => shelf(Shelves.zfsShelf(pool, pool.nodeRecord)))
                );
            rows.push(panel("ZFS pools", [drawn], { hue: "compute", aside: `${zfs.length} pool${zfs.length === 1 ? "" : "s"}` }));
        } else if (!ceph) {
            rows.push(panel(
                "Ceph",
                [el("p", { class: "cluster-muted", text: "Ceph is configured, but its status could not be read." })]
            ));
        } else {
            const cephPools = (Array.isArray(ceph.pool_list)
                ? ceph.pool_list
                : []).slice().sort((a, b) => (b.raw_used || 0) - (a.raw_used || 0));
            if (!cephPools.some((pool) => pool.name === state.cephPool)) {
                state.cephPool = "overview";
            }
            const poolRow = window.LabratViewFrame.subtabs({
                label: "Ceph pools", level: 2, current: state.cephPool,
                items: [
                    { key: "overview", label: "Cluster" },
                    ...cephPools.map((pool) => ({ key: pool.name, label: pool.name, detail: bytes(pool.stored) }))
                ],
                onSelect: (key) => { state.cephPool = key; syncQuery(); render(); },
            });
            // Under an open strip, the row hangs from the switch on the right.
            poolRow.classList.toggle("is-under-switch", !strip);
            rows.push(poolRow);
            const pool = cephPools.find((item) => item.name === state.cephPool);
            rows.push(pool ? cephPoolView(ceph, pool, config, nodes) : cephPanel(ceph, nodes));
        }
        root.replaceChildren(...rows);
    }


    /**
     * One Ceph pool: what it stores, how it is replicated, which storages
     * use it, and the OSDs its rule places data on.
     * @param {object} ceph Curated Ceph status.
     * @param {object} pool Curated pool.
     * @param {Array<object>} config Storage configuration.
     * @param {Array<object>} nodes The cluster's nodes, for each OSD's drive.
     * @returns {HTMLElement} The pool view.
     */
    function cephPoolView(ceph, pool, config, nodes) {
        const osds = ceph.osd_list || [];
        const deviceClass = ruleClass(pool.rule, osds);
        const serving = deviceClass ? osds.filter((osd) => osd.device_class === deviceClass) : osds;
        const users = config.filter((item) => item.pool === pool.name
            || (pool.applications.includes("cephfs") && item.type === "cephfs" && pool.name.startsWith(`${item.pool}_`)));
        const usable = pool.class_capacity && pool.size ? pool.class_capacity / pool.size : null;
        const fraction = pool.class_capacity ? (pool.raw_used || 0) / pool.class_capacity : 0;
        const down = serving.filter((osd) => osd.status !== "up" || !osd.in);
        const tiles = stats([
            statTile({
                label: "Stored", value: bytes(pool.stored), caption: `${bytes(pool.raw_used)} raw with replicas`
            }),
            statTile({
                label: "Of its OSDs", value: percent(fraction), caption: `${bytes(pool.raw_used)} of ${bytes(pool.class_capacity)} raw`,
                meter: {
                    total: pool.class_capacity || 0, used: pool.raw_used || 0,
                    label: `${percent(fraction)} of the rule's raw capacity`, tone: severity(fraction)
                },
                tip: [
                    "Of its OSDs",
                    "Raw use against every OSD this pool's rule can place data on; other pools share that space."
                ],
            }),
            statTile({
                label: "Usable left", value: usable ? bytes(Math.max(0, usable - (pool.stored || 0))) : "—",
                caption: usable ? `about, at ${pool.size}× replication` : ""
            }),
            statTile({ label: "Replicas", value: `${pool.size ?? "?"} / ${pool.min_size ?? "?"}`, caption: "size / min size" }),
            statTile({ label: "Placement groups", value: String(pool.pg_num ?? "?"), caption: pool.pg_target && pool.pg_target !== pool.pg_num ? `autoscale target ${pool.pg_target}` : `autoscale ${pool.autoscale || "off"}` }),
            statTile({
                label: "OSDs", value: `${serving.length - down.length} of ${serving.length}`, caption: deviceClass
                    ? `${deviceClass} only (${pool.rule})`
                    : pool.rule
                    || "",
                meter: {
                    total: serving.length, used: serving.length - down.length,
                    label: `${serving.length - down.length} of ${serving.length} OSDs up and in`,
                    tone: down.length ? "is-warning" : ""
                },
            }),
        ]);
        const facts = el("dl", { class: "cluster-facts" }, [
            el("dt", { text: "Used for" }), el("dd", { text: pool.applications.join(", ") || "—" }),
            el(
                "dt", { text: "Proxmox storage" }
            ), el("dd", { text: users.map((item) => item.storage).join(", ") || "none" }),
            el("dt", { text: "CRUSH rule" }), el("dd", { text: pool.rule || "—" }),
        ]);
        if (state.zfsStyle !== "cards") {
            return panel(pool.name, [shelf(Shelves.cephPoolShelf(ceph, pool, { serving, deviceClass, users, nodes }))],
                { hue: "compute", aside: `${serving.length} OSDs` });
        }
        const hosts = new Map();
        for (const osd of serving) {
            hosts.set(osd.host, (hosts.get(osd.host) || []).concat(osd));
        }
        return el("div", { class: "cluster-view" }, [
            tiles,
            panel(
                pool.name, [facts, osdFill(serving)],
                { hue: "compute", aside: `${serving.length} OSDs on ${hosts.size} hosts` }
            ),
        ]);
    }

    /**
     * A readable name for a Proxmox storage type.
     * @param {string} type Storage plugin type.
     * @returns {string} Display name.
     */
    function engineName(type) {
        return ({
            rbd: "Ceph RBD", cephfs: "CephFS", zfspool: "ZFS", zfs: "ZFS over iSCSI", lvmthin: "LVM-thin", lvm: "LVM",
            dir: "Directory", nfs: "NFS", cifs: "SMB/CIFS", pbs: "Proxmox Backup Server", iscsi: "iSCSI", btrfs: "Btrfs", glusterfs: "GlusterFS",
        })[type] || type;
    }

    /**
     * The Ceph panel: health, capacity, placement groups, and OSDs by host.
     * @param {object} ceph Curated Ceph status.
     * @param {Array<object>} nodes The cluster's nodes, for each OSD's drive.
     * @returns {HTMLElement} The panel.
     */
    function cephPanel(ceph, nodes) {
        const tone = ceph.health === "HEALTH_OK" ? "on" : ceph.health === "HEALTH_WARN" ? "warn" : "alarm";
        const osds = ceph.osd_list || [];
        const hosts = new Map();
        for (const osd of osds) {
            const list = hosts.get(osd.host) || [];
            list.push(osd);
            hosts.set(osd.host, list);
        }
        const fraction = ceph.bytes_total ? ceph.bytes_used / ceph.bytes_total : 0;
        const clean = (ceph.pg_states || []).filter((row) => row.state === "active+clean").reduce(
            (total, row) => total + (row.count || 0), 0
        );
        const head = el("div", { class: "cluster-ceph-head" }, [
            el(
                "div", { class: `cluster-health cluster-health-${tone}` },
                [lamp(tone, ceph.health), el("strong", { text: ceph.health })]
            ),
            el("dl", { class: "cluster-facts" }, [
                el("dt", { text: "OSDs" }), el("dd", { text: `${ceph.osds.up ?? "?"} up · ${ceph.osds.in ?? "?"} in · ${ceph.osds.total ?? "?"} total` }),
                el(
                    "dt", { text: "Monitors" }
                ), el("dd", { text: `${ceph.monitors.in_quorum} of ${ceph.monitors.total} in quorum` }),
                el("dt", { text: "Placement groups" }), el("dd", { text: `${clean} of ${ceph.pgs ?? "?"} active+clean` }),
                el(
                    "dt", { text: "Raw capacity" }
                ), el("dd", { text: `${bytes(ceph.bytes_used)} of ${bytes(ceph.bytes_total)} (${percent(fraction)})` }),
            ]),
            meter({
                total: ceph.bytes_total || 0, used: ceph.bytes_used || 0,
                label: `Ceph raw capacity ${percent(fraction)} used`, tone: severity(fraction)
            }),
        ]);
        const checks = (ceph.checks || []).length ? el("ul", { class: "cluster-checks" }, ceph.checks.map((check) =>
            el("li", { class: check.severity === "HEALTH_ERR" ? "is-alarm" : "is-warning" }, [lamp(check.severity === "HEALTH_ERR" ? "alarm" : "warn", check.severity || ""), el("strong", { text: check.code }), el("span", { text: ` ${check.summary || ""}` })]))) : null;
        const hostCards = el("div", { class: "cluster-osd-hosts" }, [...hosts.entries()].map(([host, list]) =>
            el("article", { class: "cluster-card cluster-osd-host" }, [
                el("h3", { text: host || "unknown host" }),
                el("ul", { class: "cluster-osds" }, list.map((osd) => {
                    const up = osd.status === "up";
                    const osdTone = up && osd.in ? "on" : up || osd.in ? "warn" : "alarm";
                    const used = (osd.percent_used || 0) / 100;
                    const item = el("li", { class: `cluster-osd is-${osdTone}`, tabindex: 0 }, [
                        lamp(osdTone, `${osd.status || "unknown"}, ${osd.in ? "in" : "out"}`),
                        el("strong", { text: osd.name || `osd.${osd.id}` }),
                        el("span", { class: "cluster-osd-class", text: osd.device_class || "" }),
                        meter({ total: 1, used, label: `${percent(used)} used`, tone: severity(used) }),
                        el("span", { class: "cluster-osd-size", text: bytes(osd.total) }),
                    ]);
                    tip(item, [`${osd.name} on ${osd.host}`, `${osd.status}, ${osd.in ? "in" : "out"}`, `${osd.device_class || "?"} · ${bytes(osd.used)} of ${bytes(osd.total)} (${percent(used)})`,
                        `${osd.pgs ?? "?"} PGs · latency ${osd.apply_latency_ms ?? "?"} ms`]);
                    return item;
                })),
            ])));
        const pools = Array.isArray(ceph.pool_list) ? ceph.pool_list : [];
        const cephDonuts = row([
            panel("Raw use by pool", [donut([...pools.map((pool) => ({
                label: pool.name, value: pool.raw_used || 0
            })).sort((a, b) => b.value - a.value),
                { label: "Free", value: ceph.bytes_avail || 0, tone: "muted" }],
            {
                label: "Ceph raw capacity by pool", format: bytes,
                centre: percent(ceph.bytes_total ? ceph.bytes_used / ceph.bytes_total : null), caption: "raw used"
            })]),
            panel("Raw capacity by class", [donut(tally(
                osds, (osd) => osd.device_class || "unknown", (osd) => osd.total || 0
            ),
                { label: "OSD raw capacity by device class", format: bytes, pie: true })]),
        ]);
        // The shelf draws the OSDs as hardware with a health box in
        // place of the head, checks, and host cards; Cards keeps those.
        const parts = state.zfsStyle === "cards"
            ? [head, checks, cephDonuts, pgBar(ceph), osdFill(osds), hostCards]
            : [shelf(Shelves.cephClusterShelf(ceph, nodes)), cephDonuts, pgBar(ceph)];
        return panel("Ceph", parts, { hue: "compute", aside: `${osds.length} OSDs on ${hosts.size} hosts` });
    }

    /**
     * One ZFS pool's health card.
     * @param {object} pool Curated pool record with its node name.
     * @returns {HTMLElement} The card.
     */
    function zfsCard(pool) {
        const health = pool.state || pool.health || "UNKNOWN";
        const tone = health === "ONLINE" ? "on" : health === "DEGRADED" ? "warn" : "alarm";
        const fraction = pool.size ? pool.alloc / pool.size : 0;
        return el("article", { class: "cluster-card cluster-zfs" }, [
            el("header", { class: "cluster-zfs-heading" }, [
                el(
                    "div", { class: `cluster-health cluster-health-${tone}` },
                    [lamp(tone, health), el("strong", { text: health })]
                ),
                el("h3", { text: pool.name }), el("span", { class: "cluster-muted", text: pool.node }),
            ]),
            meter({
                total: pool.size || 0, used: pool.alloc || 0, label: `${percent(fraction)} allocated`,
                tone: severity(fraction)
            }),
            el("dl", { class: "cluster-facts" }, [
                el(
                    "dt", { text: "Allocated" }
                ), el("dd", { text: `${bytes(pool.alloc)} of ${bytes(pool.size)} (${percent(fraction)})` }),
                el(
                    "dt", { text: "Fragmentation" }
                ), el("dd", { text: pool.frag !== null && pool.frag !== undefined ? `${pool.frag}%` : "—" }),
                el("dt", { text: "Errors" }), el("dd", { text: pool.errors || "—" }),
                el("dt", { text: "Last scan" }), el("dd", { text: pool.scan || "—" }),
            ]),
            pool.vdevs && pool.vdevs.length ? el("ul", { class: "cluster-vdevs" }, pool.vdevs.map((vdev) => {
                const errors = (vdev.read || 0) + (vdev.write || 0) + (vdev.cksum || 0);
                const vtone = vdev.state === "ONLINE"
                    && !errors
                    ? "on"
                    : vdev.state === "ONLINE"
                    || vdev.state === "DEGRADED"
                    ? "warn"
                    : "alarm";
                const row = el("li", { class: `cluster-vdev depth-${Math.min(vdev.depth, 3)}`, tabindex: 0 }, [
                    lamp(vtone, vdev.state || "unknown"), el("span", { class: "cluster-vdev-name", text: vdev.name }),
                    el("span", { class: "cluster-muted", text: errors ? `${errors} errors` : vdev.state || "" }),
                ]);
                tip(
                    row,
                    [
                        vdev.name, vdev.state || "",
                        `read ${vdev.read ?? 0} · write ${vdev.write ?? 0} · checksum ${vdev.cksum ?? 0}`
                    ]
                );
                return row;
            })) : null,
            pool.status ? el("p", { class: "cluster-zfs-status", text: pool.status }) : null,
        ]);
    }

    // ------------------------------------------------------------------
    // Page wiring
    // ------------------------------------------------------------------

    const drawers = {
        summary: drawSummary, compute: drawCompute, networks: drawNetworks, storage: drawStorage,
        nodes: (root, source) => {
            state.node = window.LabratNodes.draw(root, source, {
                node: state.node, tab: app.querySelector('[data-tab="nodes"]'),
                // The gauge's sweep turns once per refresh, from this load.
                refreshMs: LIVE_REFRESH_MS, loadedAt,
                onSelect: (key) => {
                    state.node = key;
                    window.history.pushState(
                        { view: "nodes", node: key }, "", `${app.dataset.base}/nodes/${encodeURIComponent(key)}`
                    );
                    render();
                },
            }) || "";
        },
    };

    /** Draw the selected tab and update the heading and tools. */
    function render() {
        for (const panelNode of app.querySelectorAll("[data-view-panel]")) {
            panelNode.hidden = panelNode.dataset.viewPanel !== state.view;
        }
        for (const tab of app.querySelectorAll("[data-tab]")) {
            if (tab.dataset.tab === state.view) {
                tab.setAttribute("aria-current", "page");
            } else {
                tab.removeAttribute("aria-current");
            }
        }
        for (const tools of app.querySelectorAll("[data-tools]")) {
            tools.hidden = tools.dataset.tools !== state.view
                || (tools.dataset.tools === "storage" && state.storageSub !== "zfs" && state.storageSub !== "ceph");
        }
        const zfsLabel = state.zfsStyle === "shelf"
            ? "Showing drive bays; show cards"
            : "Showing cards; show drive bays";
        zfsButton.setAttribute("aria-pressed", String(state.zfsStyle === "shelf"));
        for (const name of ["aria-label", "title", "data-tooltip"]) {
            zfsButton.setAttribute(name, zfsLabel);
        }
        zfsButton.dataset.style = state.zfsStyle;
        const byNode = state.groupBy === "node";
        const groupLabel = byNode ? "Grouped by node; group by VLAN" : "Grouped by VLAN; group by node";
        groupButton.setAttribute("aria-pressed", String(byNode));
        for (const name of ["aria-label", "title", "data-tooltip"]) {
            groupButton.setAttribute(name, groupLabel);
        }
        groupButton.dataset.group = state.groupBy;
        const asDiagram = state.networkStyle === "diagram";
        const styleLabel = asDiagram ? "Showing diagram; show grid" : "Showing grid; show diagram";
        styleButton.setAttribute("aria-pressed", String(asDiagram));
        for (const name of ["aria-label", "title", "data-tooltip"]) {
            styleButton.setAttribute(name, styleLabel);
        }
        styleButton.dataset.style = state.networkStyle;
        const nextStyle = COMPUTE_STYLES[(COMPUTE_STYLES.indexOf(state.computeStyle) + 1) % COMPUTE_STYLES.length];
        const computeLabel = `Showing ${state.computeStyle}; show ${nextStyle}`;
        for (const name of ["aria-label", "title", "data-tooltip"]) {
            computeButton.setAttribute(name, computeLabel);
        }
        computeButton.dataset.style = state.computeStyle;
        const source = current();
        if (!source) {
            summaryLine.textContent = data ? "No Proxmox connection" : "Loading…";
            return;
        }
        const cluster = source.cluster || {};
        // Live pulls between full scans stamp live_at; show the freshest.
        const sampled = source.nodes.flatMap((node) => [node.sampled_at, node.live_at]).filter(Boolean).sort().pop();
        const live = source.nodes.some((node) => node.live_at && node.live_at === sampled);
        summaryLine.textContent = `${cluster.name || source.name} · ${source.nodes.length} node${source.nodes.length === 1 ? "" : "s"} · ${live ? "live" : "sampled"} ${ago(sampled)}`;
        const counts = { vm: 0, container: 0 };
        for (const guest of source.guests) {
            counts[guest.kind] = (counts[guest.kind] || 0) + (guest.template ? 0 : 1);
        }
        for (const input of app.querySelectorAll("[data-kind]")) {
            const label = input.closest("label");
            const text = `Show ${label.dataset.kindLabel} (${counts[input.dataset.kind] || 0})`;
            input.setAttribute("aria-label", text);
            label.title = text;
            label.dataset.tooltip = text;
        }
        const root = document.getElementById(`cluster-view-${state.view}`);
        if (!source.nodes.length) {
            root.replaceChildren(el(
                "p", { class: "cluster-empty", text: "The collector has not reported any Proxmox nodes yet." }
            ));
            return;
        }
        if (state.view !== "storage" && state.view !== "nodes") {
            window.LabratViewFrame.placeSubtabs({ label: "Subtabs", items: [] });
        }
        drawers[state.view](root, source);
    }

    /**
     * Fetch the cluster data and redraw.
     * @returns {Promise<void>} Resolves after drawing.
     */
    async function load() {
        notice.textContent = "";
        try {
            data = await labratJson(app.dataset.api);
            loadedAt = Date.now();
        } catch (error) {
            notice.textContent = "The cluster data could not be loaded. Try Refresh.";
            return;
        }
        sourceIndex = Math.min(sourceIndex, Math.max(0, data.sources.length - 1));
        sourcePicker.hidden = data.sources.length < 2;
        sourcePicker.replaceChildren(...data.sources.map((source, index) => {
            const button = el(
                "button",
                {
                    class: "button button-quiet cluster-source", type: "button",
                    "aria-pressed": String(index === sourceIndex),
                    text: (source.cluster && source.cluster.name) || source.name
                }
            );
            button.addEventListener("click", () => {
                sourceIndex = index;
                for (const other of sourcePicker.children) {
                    other.setAttribute("aria-pressed", String(other === button));
                }
                render();
            });
            return button;
        }));
        render();
    }

    for (const tab of app.querySelectorAll("[data-tab]")) {
        tab.addEventListener("click", (event) => {
            if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) {
                return;
            }
            event.preventDefault();
            state.view = tab.dataset.tab;
            window.history.pushState({ view: state.view }, "", tab.href);
            render();
        });
    }
    window.addEventListener("popstate", () => {
        const parts = window.location.pathname.replace(/\/$/, "").split("/");
        const at = parts.indexOf("cluster");
        const view = parts[at + 1];
        state.view = view && view in drawers ? view : "summary";
        state.node = state.view === "nodes" ? decodeURIComponent(parts[at + 2] || "") : state.node;
        render();
    });
    for (const input of app.querySelectorAll("[data-kind]")) {
        input.addEventListener("change", () => {
            state.kinds[input.dataset.kind] = input.checked;
            render();
        });
    }
    document.getElementById("cluster-running").addEventListener("change", (event) => {
        state.runningOnly = event.target.checked;
        render();
    });
    document.getElementById("cluster-templates").addEventListener("change", (event) => {
        state.templates = event.target.checked;
        render();
    });
    groupButton.addEventListener("click", () => {
        state.groupBy = state.groupBy === "node" ? "vlan" : "node";
        writePreference("labrat.cluster.groupBy", state.groupBy);
        render();
    });
    computeButton.addEventListener("click", () => {
        state.computeStyle = COMPUTE_STYLES[(COMPUTE_STYLES.indexOf(state.computeStyle) + 1) % COMPUTE_STYLES.length];
        writePreference("labrat.cluster.computeStyle", state.computeStyle);
        render();
    });
    zfsButton.addEventListener("click", () => {
        state.zfsStyle = ZFS_STYLES[(ZFS_STYLES.indexOf(state.zfsStyle) + 1) % ZFS_STYLES.length];
        writePreference("labrat.cluster.zfsStyle", state.zfsStyle);
        render();
    });
    styleButton.addEventListener("click", () => {
        state.networkStyle = state.networkStyle === "diagram" ? "grid" : "diagram";
        writePreference("labrat.cluster.networkStyle", state.networkStyle);
        render();
    });
    document.getElementById("cluster-refresh").addEventListener("click", load);
    // Sitting on a node screen, redraw every 30 s while the page is visible
    // (the collector pulls node and guest state about every 20 s between
    // scans), and at once on returning to a tab left longer than that.
    let reloading = false;
    const liveReload = async () => {
        if (state.view !== "nodes"
            || document.visibilityState !== "visible"
            || reloading
            || Date.now() - loadedAt < LIVE_REFRESH_MS - 1000) {
            return;
        }
        reloading = true;
        try {
            await load();
        } finally {
            reloading = false;
        }
    };
    window.setInterval(liveReload, LIVE_REFRESH_MS);
    document.addEventListener("visibilitychange", liveReload);
    // Subtabs spread inside the switch on wide screens and sit in a row on
    // phones; redraw when the window crosses between the two.
    let wide = window.innerWidth > 640;
    window.addEventListener("resize", () => {
        if ((window.innerWidth > 640) !== wide) {
            wide = window.innerWidth > 640;
            render();
        }
    });
    render();
    load();
})();
