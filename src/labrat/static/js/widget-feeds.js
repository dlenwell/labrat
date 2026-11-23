// Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
// Used by: Home, Widgets.
/**
 * @file Widget feeds: named functions that turn a LabRat API response into
 * a widget's input, so a template can place a live widget with markup
 * alone:
 *
 *     <div data-widget-src="/api/v1/cluster" data-widget-feed="cluster.guest-states"
 *          data-widget-title="Guest states" data-widget-hue="compute"
 *          data-widget-refresh="300"></div>
 *
 * Each feed returns `{widget, data, options}` for LabratWidgets.render.
 * Feeds read the first source in the response; `data-widget-source` picks
 * another by index. Cluster feeds use cluster-model.js (and the shelf
 * feeds shelf-model.js) and network feeds use network-model.js, so load
 * those before this file. See planning/specs/widgets.md for the list.
 */
(() => {
    "use strict";

    const W = window.LabratWidgets;
    const M = window.LabratClusterModel;
    const N = window.LabratNetworkModel;
    const { bytes, percent, sum, tally } = W;

    /**
     * The source a widget element asks for.
     * @param {object} json API response with `sources`.
     * @param {HTMLElement} node The widget element.
     * @returns {object|null} The source.
     */
    function sourceOf(json, node) {
        const sources = (json && json.sources) || [];
        return sources[Number(node.dataset.widgetSource || 0)] || null;
    }

    /**
     * Register a feed that needs a source, drawing a note without one.
     * @param {string} name Feed name.
     * @param {Function} build `(source, node) => {widget, data, options}`.
     */
    function feed(name, build) {
        W.register(name, (json, node) => {
            const source = sourceOf(json, node);
            if (!source) {
                return {
                    widget: "attention", data: [], options: { title: null, clear: "No data from this source yet." }
                };
            }
            return build(source, node);
        });
    }

    // ------------------------------------------------------------------
    // Cluster (/api/v1/cluster)
    // ------------------------------------------------------------------

    feed("cluster.attention", (source, node) => ({
        widget: "attention", data: M.attentionItems(source, node.dataset.widgetLink || "/cluster"),
        // data-widget-bare draws the list alone, for a page that titles it.
        options: {
            title: node.hasAttribute("data-widget-bare")
                ? null
                : node.dataset.widgetTitle
                || "Cluster attention", hue: node.dataset.widgetHue
                || "compute",
            limit: Number(node.dataset.widgetLimit) || Infinity, more: node.dataset.widgetLink || "/cluster",
        },
    }));

    feed("cluster.tiles", (source) => {
        const online = source.nodes.filter((item) => item.status === "online");
        const guests = source.guests.filter((guest) => !guest.template);
        const running = M.runningGuests(source);
        const threads = sum(online, (item) => item.maxcpu);
        const cpu = threads ? sum(online, (item) => (item.cpu || 0) * (item.maxcpu || 0)) / threads : null;
        const installed = sum(online, (item) => item.maxmem);
        const used = sum(online, (item) => item.mem);
        return {
            widget: "stats", data: [
                { label: "Nodes online", value: `${online.length} of ${source.nodes.length}`,
                    meter: {
                        total: source.nodes.length, used: online.length,
                        label: `${online.length} of ${source.nodes.length} nodes online`,
                        tone: online.length < source.nodes.length ? "is-warning" : ""
                    } },
                { label: "Guests running", value: `${running.length} of ${guests.length}`,
                    meter: {
                        total: guests.length, used: running.length,
                        label: `${running.length} of ${guests.length} guests running`
                    } },
                { label: "CPU now", value: percent(cpu), caption: `of ${threads} threads`,
                    meter: { total: 1, used: cpu || 0, label: `Cluster CPU ${percent(cpu)}`, tone: "auto" } },
                {
                    label: "Memory in use", value: bytes(used),
                    caption: `${percent(installed ? used / installed : null)} of ${bytes(installed)}`,
                    meter: {
                        total: installed, used, label: `${bytes(used)} of ${bytes(installed)} in use`, tone: "auto"
                    } },
            ],
        };
    });

    feed("cluster.guest-states", (source) => {
        const guests = source.guests.filter((guest) => !guest.template);
        const running = M.runningGuests(source);
        return {
            widget: "donut", data: [
                { label: "Running VMs", value: running.filter((guest) => guest.kind === "vm").length },
                { label: "Running containers", value: running.filter((guest) => guest.kind === "container").length },
                { label: "Stopped", value: guests.filter((guest) => guest.status !== "running").length, tone: "muted" },
                { label: "Templates", value: source.guests.filter((guest) => guest.template).length, tone: "muted" },
            ],
            options: {
                label: "Guests by state", centre: String(running.length), caption: `of ${guests.length} running`
            },
        };
    });

    feed("cluster.memory-by-node", (source) => {
        const installed = sum(source.nodes.filter((item) => item.status === "online"), (item) => item.maxmem);
        const byNode = source.nodes.map((item) => ({
            label: item.name, value: sum(M.runningGuests(source, item.remote_id), (guest) => guest.memory)
        }));
        const allocated = sum(byNode, (item) => item.value);
        return {
            widget: "donut", data: [
                ...byNode, { label: "Unallocated", value: Math.max(0, installed - allocated), tone: "muted" }
            ],
            options: {
                label: "Allocated memory by node", format: bytes,
                centre: percent(installed ? allocated / installed : null), caption: "allocated"
            },
        };
    });

    feed("cluster.os-mix", (source) => ({
        widget: "pie", data: tally(source.guests.filter((guest) => !guest.template), M.guestOs),
        options: { label: "Guests by operating system" },
    }));

    feed(
        "cluster.cpu-24h",
        (source) => ({
            widget: "area", data: M.clusterHistory(source), options: { field: "cpu", label: "Cluster CPU" }
        })
    );
    feed(
        "cluster.memory-24h",
        (source) => ({
            widget: "area", data: M.clusterHistory(source), options: { field: "mem", label: "Cluster memory", max: 1 }
        })
    );
    feed(
        "cluster.cpu-clock",
        (source) => ({
            widget: "radialClock", data: M.clusterHistory(source),
            options: { field: "cpu", label: "Cluster CPU by time of day" }
        })
    );

    feed("cluster.storage", (source) => ({
        widget: "bars", data: M.mergeLocal(M.storagePools(source)).map((pool) => ({
            label: pool.storage, sub: pool.type, value: pool.used || 0, total: pool.total,
        })), options: { format: bytes, scale: "row" },
    }));

    feed("cluster.busiest-guests", (source) => ({
        widget: "bars", data: M.runningGuests(source).map((guest) => ({
            label: guest.name || String(guest.vmid), sub: guest.node, value: guest.cpu || 0
        })),
        options: { format: percent, limit: 8, empty: "Nothing to rank yet." },
    }));

    // Disk shelves (shelf-model.js, loaded before this file where used).
    // data-widget-masonry packs the chassis into columns; data-widget-bare
    // drops the health box.
    feed("cluster.drives", (source, node) => {
        const data = window.LabratShelfModel.clusterDrives(source);
        return { widget: "shelf", data: node.hasAttribute("data-widget-bare") ? { chassis: data.chassis } : data,
            options: { masonry: node.hasAttribute("data-widget-masonry") } };
    });

    feed("cluster.ceph-osds", (source) => {
        const ceph = source.cluster && source.cluster.ceph;
        return ceph ? { widget: "shelf", data: window.LabratShelfModel.cephClusterShelf(ceph, source.nodes) }
            : { widget: "attention", data: [], options: { title: null, clear: "No Ceph on this cluster." } };
    });

    // data-widget-pool names the pool (and data-widget-node its node);
    // without it, the first ZFS pool.
    feed("cluster.zfs", (source, node) => {
        const host = source.nodes.find((item) => (item.zfs
            || []).some((pool) => (!node.dataset.widgetPool || pool.name === node.dataset.widgetPool)
            && (!node.dataset.widgetNode || item.name === node.dataset.widgetNode)));
        const pool = host && host.zfs.find((item) => !node.dataset.widgetPool || item.name === node.dataset.widgetPool);
        return pool ? { widget: "shelf", data: window.LabratShelfModel.zfsShelf({ ...pool, node: host.name }, host) }
            : { widget: "attention", data: [], options: { title: null, clear: "No ZFS pool found." } };
    });

    // ------------------------------------------------------------------
    // Networks (/api/v1/network, each source's shared `network` form)
    // ------------------------------------------------------------------

    /**
     * Register a network feed that needs a source with a network, drawing
     * a note without one.
     * @param {string} name Feed name.
     * @param {Function} build `(network, node) => {widget, data, options}`.
     */
    function networkFeed(name, build) {
        W.register(name, (json, node) => {
            const source = sourceOf(json, node);
            if (!source || !source.network) {
                return {
                    widget: "attention", data: [], options: { title: null, clear: "No data from this source yet." }
                };
            }
            return build(source.network, node);
        });
    }

    networkFeed("network.clients-by-network", (network) => {
        const clients = network.clients || [];
        return { widget: "donut", data: tally(clients, (client) => client.network || "unknown"),
            options: { label: "Clients by network", centre: String(clients.length), caption: "clients" } };
    });

    networkFeed("network.connections", (network) => {
        const clients = network.clients || [];
        return { widget: "pie", data: tally(
            clients,
            (client) => (client.connection === "wired" ? "Wired" : client.connection === "wireless" ? "Wi-Fi" : "Other")
        ),
            options: { label: "Clients by connection" } };
    });

    networkFeed("network.wifi-channels", (network) => ({
        widget: "columnGroups", data: N.channelGroups(network),
        options: {
            label: "Neighbouring networks per channel", note: "Brighter columns are channels your own APs use."
        },
    }));

    // DNS sinkholes come with the source's network. data-widget-limit
    // keeps the first few; data-widget-bare drops each card's evidence
    // lines.
    W.register("network.sinkholes", (json, node) => {
        const source = sourceOf(json, node);
        const items = (source && source.network && source.network.sinkholes) || [];
        return {
            widget: "sinkholes", data: items.slice(0, Number(node.dataset.widgetLimit) || undefined),
            options: { evidence: !node.hasAttribute("data-widget-bare") },
        };
    });

    networkFeed("network.top-clients", (network) => ({
        widget: "splitBars", data: (network.clients || []).map((client) => ({
            label: client.name || client.mac, sub: client.network || "", values: [
                client.rx_bytes || 0, client.tx_bytes || 0
            ],
        })), options: { parts: ["received", "sent"], format: bytes, limit: 8 },
    }));
})();
