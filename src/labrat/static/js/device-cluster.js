// Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
// Used by: Discovery, Equipment and Diagram, Discovery clients, Floor plan.
/**
 * @file Proxmox details in the device drawer, drawn with widgets.
 *
 * A device whose observation is a Proxmox node gets a "Proxmox node"
 * section: what needs attention on it, tiles for CPU, memory, vCPUs, and
 * guests, the last day's CPU, memory, and network, its guests' memory, its
 * drives' wear, and its storage. A Proxmox guest (a client record matched
 * to a VM or container) gets a "Proxmox guest" section: state, CPU,
 * memory, the day's CPU and network, and its filesystems. Both link to the
 * Cluster page.
 *
 * device-details.js calls `LabratDeviceCluster.section(node)` while
 * rendering; it returns an empty, hidden section at once and fills it when
 * /api/v1/cluster answers (one request, reused for a minute). Without a
 * match the section stays hidden. Needs widgets.js and cluster-model.js.
 * Exposed as window.LabratDeviceCluster.
 */
(() => {
    "use strict";

    const W = window.LabratWidgets;
    const M = window.LabratClusterModel;
    if (!W || !M) {
        return;
    }
    const { el, bytes, percent, rate, sum, uptime } = W;

    /** How long one cluster response is reused, in milliseconds. */
    const REUSE_MS = 60000;
    let cached = null;
    let cachedAt = 0;

    /**
     * The cluster API response, fetched at most once a minute.
     * @returns {Promise<object|null>} The response, or null when unavailable.
     */
    function cluster() {
        if (!cached || Date.now() - cachedAt > REUSE_MS) {
            cachedAt = Date.now();
            cached = labratJson(LABRAT_API.cluster).catch(() => null);
        }
        return cached;
    }

    /**
     * Find the Proxmox node or guest a drawer device is.
     * @param {object} data Cluster API response.
     * @param {object} node Drawer device record (with `observations`).
     * @returns {{kind: "node"|"guest", source: object, node?: object, guest?: object}|null} The match.
     */
    function match(data, node) {
        const observations = node.observations || [];
        for (const source of (data && data.sources) || []) {
            const host = source.nodes.find((item) => observations.some((observation) => observation.id === item.id));
            if (host) {
                return { kind: "node", source, node: host };
            }
        }
        const guests = [node.client, ...observations.map((observation) => observation.client)]
            .map((client) => client && client.virtualization).filter(Boolean);
        for (const found of guests) {
            const source = ((data && data.sources) || []).find((item) => item.id === found.source_id);
            const guest = source && source.guests.find((item) => item.vmid === found.vmid);
            if (guest) {
                return { kind: "guest", source, guest };
            }
        }
        return null;
    }

    /**
     * A small heading with a link to the Cluster page.
     * @param {string} title Heading.
     * @param {string} href Link address.
     * @param {string} label Link text.
     * @returns {HTMLElement} The heading row.
     */
    function heading(title, href, label) {
        return el("header", { class: "device-cluster-heading" }, [
            el("h4", { text: title }),
            el("a", { class: "device-cluster-link", href, text: label }),
        ]);
    }

    /**
     * A labelled sparkline row with the latest figure.
     * @param {string} label Row name.
     * @param {Array<object>} points History points.
     * @param {string} field Field to draw.
     * @param {Function} format Value formatter.
     * @param {number} [max] Top of the scale.
     * @returns {HTMLElement|null} The row, or null without history.
     */
    function trace(label, points, field, format, max) {
        const series = (points || []).filter((point) => typeof point[field] === "number");
        if (series.length < 2) {
            return null;
        }
        return el("div", { class: "device-cluster-trace" }, [
            el("span", { class: "device-cluster-trace-label", text: label }),
            W.sparkline(series, { field, format, max, label: `${label} over 24 hours` }),
            el("strong", { class: "device-cluster-trace-figure", text: format(series[series.length - 1][field]) }),
        ]);
    }

    /**
     * A titled block inside the section.
     * @param {string} title Block title.
     * @param {Array<Node|null>} children Content.
     * @returns {HTMLElement|null} The block, or null when empty.
     */
    function block(title, children) {
        const kept = children.filter(Boolean);
        return kept.length ? el("div", { class: "device-cluster-block" }, [el("h5", { text: title }), ...kept]) : null;
    }

    /**
     * The Proxmox node section's content.
     * @param {object} source Cluster source view.
     * @param {object} host Node view.
     * @returns {Array<Node>} Content.
     */
    function nodeContent(source, host) {
        const base = "/cluster";
        const href = `${base}/nodes/${encodeURIComponent(host.remote_id)}`;
        const running = M.runningGuests(source, host.remote_id);
        const guests = source.guests.filter((guest) => guest.node === host.remote_id && !guest.template);
        const vcpus = sum(running, (guest) => guest.vcpus);
        const allocated = sum(running, (guest) => guest.memory);
        const online = host.status === "online";
        const items = M.attentionItems({ ...source, nodes: [host] }, base)
            .filter((item) => item.href === href);
        const history = host.history || [];
        const memFraction = host.maxmem ? (host.mem || 0) / host.maxmem : 0;
        const tiles = W.stats([
            {
                label: "CPU now", value: online ? percent(host.cpu) : "Offline",
                caption: `${host.threads ?? host.maxcpu ?? "?"} threads · up ${uptime(host.uptime)}`,
                meter: { total: 1, used: host.cpu || 0, label: `CPU ${percent(host.cpu)}`, tone: "auto" },
            },
            {
                label: "Memory", value: bytes(host.mem), caption: `${percent(memFraction)} of ${bytes(host.maxmem)}`,
                meter: { total: host.maxmem || 0, used: host.mem || 0, reserved: Math.max(
                    0, allocated - (host.mem || 0)
                ), tone: "auto",
                    label: `${bytes(host.mem)} in use, ${bytes(allocated)} allocated, ${bytes(host.maxmem)} installed`,
                    tip: [
                        "Memory", `In use ${bytes(host.mem)}`, `Allocated to running guests ${bytes(allocated)}`,
                        `Installed ${bytes(host.maxmem)}`
                    ] },
            },
            {
                label: "vCPUs assigned", value: `${vcpus}`, caption: host.maxcpu
                    ? `${(vcpus / host.maxcpu).toFixed(2)}× of threads`
                    : "",
                meter: {
                    total: host.maxcpu || 0, used: Math.min(vcpus, host.maxcpu || 0),
                    label: `${vcpus} vCPUs of ${host.maxcpu} threads`,
                    tone: vcpus > (host.maxcpu || 0) ? "is-warning" : ""
                },
            },
            {
                label: "Guests running", value: `${running.length} of ${guests.length}`,
                caption: host.versions
                    && host.versions.reboot_needed
                    ? "reboot pending"
                    : (host.pve_version ? `PVE ${(String(host.pve_version).match(/\d+(\.\d+)+/) || [host.pve_version])[0]}` : ""),
                meter: {
                    total: guests.length, used: running.length,
                    label: `${running.length} of ${guests.length} guests running`
                },
            },
        ], { hue: "compute" });
        const traces = block("Last 24 hours", [
            trace("CPU", history, "cpu", percent),
            trace("Memory", history, "mem", percent, 1),
            trace("IO wait", history, "iowait", percent),
            trace("Net in", history, "net_in", rate),
            trace("Net out", history, "net_out", rate),
        ]);
        const memory = running.length ? block("Guest memory", [W.donut(
            [...running].sort((a, b) => (b.memory || 0) - (a.memory || 0)).map((guest) => ({
                label: guest.name || String(guest.vmid), value: guest.memory || 0
            }))
                .concat([{ label: "Unallocated", value: Math.max(0, (host.maxmem || 0) - allocated), tone: "muted" }]),
            {
                label: `Memory on ${host.name} by guest`, format: bytes,
                centre: percent(host.maxmem ? allocated / host.maxmem : null), caption: "allocated"
            },
        )]) : null;
        const drives = (host.disks || []).filter((disk) => typeof disk.worn_percent === "number");
        const wear = drives.length ? block("Drive wear", [W.bars(drives.map((disk) => ({
            label: disk.path.replace("/dev/", ""), sub: disk.boot
                ? "boot"
                : disk.type
                || "", value: disk.worn_percent, total: 100,
            figure: `${disk.worn_percent}%`,
            tip: [`${disk.path}${disk.boot ? " (boot)" : ""}`, disk.model || "", `${disk.worn_percent}% of rated life used`, disk.health ? `SMART ${disk.health}` : ""],
        })), { scale: "row", format: (value) => `${value}%` })]) : null;
        const pools = (host.storage || []).filter((pool) => pool.total > 0);
        const storage = pools.length ? block("Storage", [W.bars(pools.map((pool) => ({
            label: pool.storage, sub: `${pool.type}${pool.shared ? " · shared" : ""}`, value: pool.used || 0, total: pool.total,
        })), { scale: "row", format: bytes })]) : null;
        return [
            heading("Proxmox node", href, "Open in Cluster ›"),
            items.length ? W.attention(items, { title: null }) : null,
            tiles, traces, memory, wear, storage,
        ].filter(Boolean);
    }

    /**
     * The Proxmox guest section's content.
     * @param {object} source Cluster source view.
     * @param {object} guest Guest view.
     * @returns {Array<Node>} Content.
     */
    function guestContent(source, guest) {
        const host = source.nodes.find((item) => item.remote_id === guest.node);
        const running = guest.status === "running";
        const memFraction = guest.memory && guest.mem_used ? guest.mem_used / guest.memory : 0;
        const kind = guest.kind === "container" ? "Container" : "VM";
        const fullest = M.fullestDisk(guest);
        const perSecond = (value) => `${bytes(value)}/s`;
        const tiles = W.stats([
            {
                label: `${kind} ${guest.vmid}`, value: running ? "Running" : guest.status || "Unknown",
                lamp: { tone: running ? "on" : "off", label: guest.status || "unknown" },
                caption: `on ${host ? host.name : guest.node}${running ? ` · up ${uptime(guest.uptime)}` : ""}`,
            },
            {
                label: "CPU now", value: running ? percent(guest.cpu) : "—", caption: `${guest.vcpus ?? "?"} vCPU`,
                meter: running ? { total: 1, used: guest.cpu || 0, label: `CPU ${percent(guest.cpu)}` } : null,
            },
            {
                label: "Memory", value: running && guest.mem_used ? bytes(guest.mem_used) : bytes(guest.memory),
                caption: running && guest.mem_used ? `${percent(memFraction)} of ${bytes(guest.memory)}` : "configured",
                meter: running && guest.mem_used ? {
                    total: guest.memory, used: guest.mem_used,
                    label: `${bytes(guest.mem_used)} of ${bytes(guest.memory)}`
                } : null,
            },
            fullest ? {
                label: "Fullest disk", value: percent(fullest.fraction), caption: `${fullest.mount} · ${bytes(fullest.total)}`,
                meter: {
                    total: 1, used: fullest.fraction, label: `${fullest.mount} ${percent(fullest.fraction)} full`,
                    tone: "auto"
                },
            } : { label: "Disk", value: bytes(guest.disk), caption: "configured" },
        ], { hue: "compute" });
        const history = guest.history || [];
        const traces = running ? block("Last 24 hours", [
            trace("CPU", history, "cpu", percent),
            trace("Net in", history, "net_in", rate),
            trace("Net out", history, "net_out", rate),
            trace("Disk read", history, "disk_read", perSecond),
            trace("Disk write", history, "disk_write", perSecond),
        ]) : null;
        const filesystems = ((guest.agent && guest.agent.filesystems) || []).filter((fs) => fs.total);
        const disks = filesystems.length ? block("Filesystems", [W.bars(filesystems.map((fs) => ({
            label: fs.mount, sub: fs.type || "", value: fs.used || 0, total: fs.total,
        })), { scale: "row", format: bytes })]) : null;
        return [
            heading("Proxmox guest", "/cluster/compute", "Open in Cluster ›"), tiles, traces, disks
        ].filter(Boolean);
    }

    /**
     * The drawer section for a device: hidden at first, filled and shown
     * when the device turns out to be a Proxmox node or guest.
     * @param {object} node Drawer device record.
     * @returns {HTMLElement} The section (append it; it fills itself).
     */
    function section(node) {
        const target = el(
            "section", { class: "device-cluster-details", "data-hue": "compute", hidden: true, "aria-live": "polite" }
        );
        cluster().then((data) => {
            const found = data && node ? match(data, node) : null;
            if (!found) {
                return;
            }
            target.replaceChildren(...(found.kind === "node" ? nodeContent(
                found.source, found.node
            ) : guestContent(found.source, found.guest)));
            target.hidden = false;
        });
        return target;
    }

    /**
     * The drawer's Drives section for a Proxmox node (owner: under
     * networking, styled like the Storage overview's shelf, one drive per
     * row): its chassis from the same data as the overview
     * (shelf-model.js `chassisByNode`), each bay with its lamps, gauges,
     * and readout. Hidden for anything else, or when the page does not
     * load the shelf model.
     * @param {object} node Drawer device record.
     * @returns {HTMLElement} The section (filled in when data arrives).
     */
    function drives(node) {
        const target = el(
            "section",
            {
                class: "device-cluster-details device-drives", "data-hue": "compute", hidden: true,
                "aria-live": "polite"
            }
        );
        const shelves = window.LabratShelfModel;
        if (!shelves || !W.shelf) {
            return target;
        }
        cluster().then((data) => {
            const found = data && node ? match(data, node) : null;
            const box = found
                && found.kind === "node"
                ? shelves.chassisByNode(found.source).get(found.node.name)
                : null;
            const count = box ? box.groups.reduce((total, group) => total + group.bays.length, 0) : 0;
            if (!count) {
                return;
            }
            target.replaceChildren(
                heading(`Drives (${count})`, "/cluster/storage", "Storage ›"),
                W.shelf({ chassis: [box] }, { columns: 1 })
            );
            target.hidden = false;
        });
        return target;
    }

    window.LabratDeviceCluster = { section, drives, match, cluster };
})();
