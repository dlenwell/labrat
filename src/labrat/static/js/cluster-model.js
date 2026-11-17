// Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
// Used by: Home, Discovery, Equipment and Diagram, Discovery clients, Cluster, Floor plan, Widgets.
/**
 * @file Facts derived from /api/v1/cluster (one source view): running
 * guests, unique storage pools, cluster-wide history, operating systems,
 * and the Attention list. Pure functions, shared by the Cluster page
 * (cluster.js) and the widget feeds (widget-feeds.js), and exposed as
 * window.LabratClusterModel.
 */
(() => {
    "use strict";

    const { percent, sum } = window.LabratWidgets;

    /**
     * Running, non-template guests on one node (or all nodes).
     * @param {object} source Source view.
     * @param {string} [node] Node remote ID.
     * @returns {Array<object>} Guests.
     */
    function runningGuests(source, node) {
        return source.guests.filter((guest) => guest.status === "running" && !guest.template
            && (node === undefined || guest.node === node));
    }

    /**
     * Unique storage pools: a shared pool once, local pools once per node.
     * @param {object} source Source view.
     * @returns {Array<object>} Pools with `nodes` (the nodes that report it).
     */
    function storagePools(source) {
        const pools = new Map();
        for (const node of source.nodes) {
            for (const item of node.storage || []) {
                const key = item.shared ? item.storage : `${item.storage}@${node.remote_id}`;
                const pool = pools.get(key) || { ...item, nodes: [] };
                pool.nodes.push(node.remote_id);
                pools.set(key, pool);
            }
        }
        return [...pools.values()].filter((pool) => pool.total > 0);
    }

    /**
     * Combine nodes' histories into cluster-wide CPU and memory series.
     * CPU is weighted by each node's CPU count; memory is total used over
     * total installed.
     * @param {object} source Source view.
     * @returns {Array<{t: number, cpu: number|null, mem: number|null}>} Points.
     */
    function clusterHistory(source) {
        const buckets = new Map();
        for (const node of source.nodes) {
            const cpus = node.maxcpu || 0;
            for (const point of node.history || []) {
                const bucket = buckets.get(point.t) || { cpu: 0, cpus: 0, used: 0, total: 0 };
                if (typeof point.cpu === "number" && cpus) {
                    bucket.cpu += point.cpu * cpus;
                    bucket.cpus += cpus;
                }
                if (typeof point.mem === "number" && node.maxmem) {
                    bucket.used += point.mem * node.maxmem;
                    bucket.total += node.maxmem;
                }
                buckets.set(point.t, bucket);
            }
        }
        return [...buckets.entries()].sort((a, b) => a[0] - b[0]).map(([t, bucket]) => ({
            t,
            cpu: bucket.cpus ? bucket.cpu / bucket.cpus : null,
            mem: bucket.total ? bucket.used / bucket.total : null,
        }));
    }

    /**
     * Fold local pools that share a name (one per node) into one row.
     * @param {Array<object>} pools Unique storage pools.
     * @returns {Array<object>} Pools with local namesakes summed.
     */
    function mergeLocal(pools) {
        const merged = new Map();
        for (const pool of pools) {
            const key = pool.shared ? `shared:${pool.storage}` : `local:${pool.storage}`;
            const row = merged.get(key);
            if (!row) {
                merged.set(key, { ...pool, nodes: [...pool.nodes] });
            } else {
                row.used += pool.used || 0;
                row.total += pool.total || 0;
                row.nodes.push(...pool.nodes);
            }
        }
        return [...merged.values()];
    }

    /**
     * OS family for a guest from its Proxmox ostype.
     * @param {object} guest Guest view.
     * @returns {string} Display family.
     */
    function osFamily(guest) {
        const type = guest.ostype || "";
        if (type === "l26" || type === "l24") {
            return "Linux";
        }
        if (/^(win|w2k|wxp|wvista)/.test(type)) {
            return "Windows";
        }
        if (type === "solaris" || type === "other" || !type) {
            return type === "solaris" ? "Solaris" : "Unknown";
        }
        return type.charAt(0).toUpperCase() + type.slice(1);
    }

    /**
     * A guest's operating system: the agent's name when it reported one,
     * else the family from its Proxmox OS type.
     * @param {object} guest Guest view.
     * @returns {string} OS name.
     */
    function guestOs(guest) {
        return (guest.agent && guest.agent.os && guest.agent.os.name) || osFamily(guest);
    }

    /**
     * A guest's fullest filesystem, from its agent.
     * @param {object} guest Guest view.
     * @returns {object|null} Filesystem with a `fraction`, or null.
     */
    function fullestDisk(guest) {
        const filesystems = ((guest.agent && guest.agent.filesystems) || []).filter((fs) => fs.total);
        return filesystems.map((fs) => ({
            ...fs, fraction: (fs.used || 0) / fs.total
        })).sort((a, b) => b.fraction - a.fraction)[0] || null;
    }

    /**
     * Everything worth a look across the cluster, worst first: each item a
     * severity, a sentence, and the tab that explains it.
     * @param {object} source Source view.
     * @param {string} base The Cluster page's address, for each item's link.
     * @returns {Array<{tone: string, text: string, href: string}>} Items.
     */
    function attentionItems(source, base) {
        const items = [];
        const add = (tone, text, href) => items.push({ tone, text, href });
        const cluster = source.cluster || {};
        if (cluster.quorate === false) {
            add("alarm", "The cluster is not quorate.", base);
        }
        for (const node of source.nodes) {
            const nodeHref = `${base}/nodes/${encodeURIComponent(node.remote_id)}`;
            if (node.status !== "online") {
                add("alarm", `${node.name} is ${node.status || "not reporting"}.`, nodeHref);
                continue;
            }
            const mem = node.maxmem ? node.mem / node.maxmem : 0;
            if (mem >= 0.8) {
                add(mem >= 0.9 ? "alarm" : "warn", `${node.name} memory is ${percent(mem)} used.`, nodeHref);
            }
            const vcpus = sum(runningGuests(source, node.remote_id), (guest) => guest.vcpus);
            if (node.maxcpu && vcpus > node.maxcpu) {
                add(
                    "info",
                    `${node.name} has ${vcpus} vCPUs assigned on ${node.maxcpu} threads (${(vcpus / node.maxcpu).toFixed(2)}×).`,
                    nodeHref
                );
            }
            for (const disk of node.disks || []) {
                const where = `${node.name} ${disk.path.replace("/dev/", "")}${disk.boot ? " (boot)" : ""}`;
                if (disk.health && !/^(PASSED|OK)$/i.test(disk.health)) {
                    add("alarm", `${where} SMART health is ${disk.health}.`, nodeHref);
                }
                if (typeof disk.worn_percent === "number" && disk.worn_percent >= 50) {
                    add(
                        disk.worn_percent >= 80 ? "alarm" : "warn", `${where} is ${disk.worn_percent}% worn.`, nodeHref
                    );
                }
                const sectors = sectorIssue(disk);
                if (sectors) {
                    add(sectors.tone, `${where} has ${sectors.text}.`, nodeHref);
                }
            }
            for (const pool of node.zfs || []) {
                const state = pool.state || pool.health;
                if (state && state !== "ONLINE") {
                    add("alarm", `ZFS pool ${pool.name} on ${node.name} is ${state}.`, `${base}/storage?sub=zfs`);
                }
            }
            const versions = node.versions || {};
            if (versions.reboot_needed) {
                add("warn", `${node.name} needs a reboot into kernel ${versions.kernel_installed}.`, nodeHref);
            }
            const now = Date.now() / 1000;
            for (const cert of node.certificates || []) {
                const days = cert.not_after ? Math.floor((cert.not_after - now) / 86400) : null;
                if (days !== null && days < 30) {
                    add(days < 14 ? "alarm" : "warn", `${node.name} ${cert.file} expires in ${days} days.`, nodeHref);
                }
            }
            const stopped = (node.services || []).filter((item) => item.enabled && item.state !== "running");
            if (stopped.length) {
                add("warn", `${node.name}: ${stopped.map((item) => item.name).join(", ")} stopped though enabled.`, nodeHref);
            }
        }
        const updating = source.nodes.filter((node) => node.versions && (node.versions.upgradable || []).length);
        if (updating.length) {
            add("info", `${sum(updating, (node) => node.versions.upgradable.length)} package updates waiting on ${updating.map((node) => node.name).join(", ")}.`, `${base}/nodes/${encodeURIComponent(updating[0].remote_id)}`);
        }
        const ceph = cluster.ceph;
        if (ceph && ceph.health && ceph.health !== "HEALTH_OK") {
            add(ceph.health === "HEALTH_ERR" ? "alarm" : "warn", `Ceph is ${ceph.health}${(ceph.checks || []).length ? `: ${ceph.checks.map((check) => check.summary || check.code).join("; ")}` : ""}.`, `${base}/storage?sub=ceph`);
        }
        const down = ((ceph && ceph.osd_list) || []).filter((osd) => osd.status !== "up" || !osd.in);
        if (down.length) {
            add("alarm", `${down.length} Ceph OSD${down.length === 1 ? " is" : "s are"} down or out: ${down.map((osd) => osd.name).join(", ")}.`, `${base}/storage?sub=ceph`);
        }
        for (const pool of mergeLocal(storagePools(source))) {
            const used = pool.total ? (pool.used || 0) / pool.total : 0;
            if (used >= 0.8) {
                add(
                    used >= 0.9 ? "alarm" : "warn", `Storage ${pool.storage} is ${percent(used)} full.`,
                    `${base}/storage`
                );
            }
        }
        const guests = source.guests.filter((guest) => !guest.template);
        for (const guest of guests) {
            const disk = fullestDisk(guest);
            if (disk && disk.fraction >= 0.8) {
                add(
                    disk.fraction >= 0.9 ? "alarm" : "warn",
                    `${guest.name} ${disk.mount} is ${percent(disk.fraction)} full.`, `${base}/compute`
                );
            }
        }
        if (Array.isArray(cluster.not_backed_up)) {
            const missing = guests.filter((guest) => cluster.not_backed_up.includes(guest.vmid));
            if (missing.length) {
                add("warn", `${missing.length} of ${guests.length} guests are in no backup job.`, base);
            }
        }
        if (cluster.tasks && cluster.tasks.error) {
            const kinds = [...new Set((cluster.tasks.failures || []).map((failure) => failure.type))];
            add("warn", `${cluster.tasks.error} task${cluster.tasks.error === 1 ? "" : "s"} failed in the last day${kinds.length ? ` (${kinds.join(", ")})` : ""}.`, base);
        }
        const risky = source.networks.filter((network) => network.guests > 0
            && Object.keys(network.nodes).length < source.nodes.length);
        if (risky.length) {
            add("info", `${risky.map((network) => network.bridge).join(", ")} ${risky.length === 1 ? "is" : "are"} missing on some nodes; guests on ${risky.length === 1 ? "it" : "them"} cannot migrate there.`, `${base}/networks`);
        }
        const noAgent = source.guests.filter((guest) => guest.kind === "vm"
            && guest.status === "running"
            && !guest.agent);
        if (noAgent.length) {
            add("info", `${noAgent.length} running VM${noAgent.length === 1 ? " has" : "s have"} no guest agent data: ${noAgent.map((guest) => guest.name).join(", ")}.`, `${base}/compute`);
        }
        const order = { alarm: 0, warn: 1, info: 2 };
        return items.sort((a, b) => order[a.tone] - order[b.tone]);
    }

    // Wear: share of an SSD's rated life used. Temperatures in Celsius.
    const WEAR_WARN = 50;
    const WEAR_ALARM = 80;
    const TEMP_WARN = { nvme: 60, ssd: 55, hdd: 50 };
    const TEMP_ALARM = { nvme: 70, ssd: 65, hdd: 58 };

    /**
     * Severity tone for a disk's temperature.
     * @param {object} disk Disk record.
     * @returns {"on"|"warn"|"alarm"|"off"} Lamp tone.
     */
    function tempTone(disk) {
        const temp = disk.smart && disk.smart.temperature_c;
        if (typeof temp !== "number") {
            return "off";
        }
        const kind = disk.type === "nvme" ? "nvme" : disk.type === "ssd" ? "ssd" : "hdd";
        return temp >= TEMP_ALARM[kind] ? "alarm" : temp >= TEMP_WARN[kind] ? "warn" : "on";
    }

    /**
     * Severity tone for SSD wear.
     * @param {number|null} worn Percent of rated life used.
     * @returns {"on"|"warn"|"alarm"|"off"} Lamp tone.
     */
    function wearTone(worn) {
        return typeof worn !== "number" ? "off" : worn >= WEAR_ALARM ? "alarm" : worn >= WEAR_WARN ? "warn" : "on";
    }

    /**
     * A disk's overall tone: SMART health first, then wear, then heat.
     * @param {object} disk Disk record.
     * @returns {"on"|"warn"|"alarm"|"off"} Lamp tone.
     */
    function diskTone(disk) {
        if (disk.health && !/^(PASSED|OK)$/i.test(disk.health)) {
            return "alarm";
        }
        const tones = [wearTone(disk.worn_percent), tempTone(disk)];
        return tones.includes("alarm") ? "alarm" : tones.includes("warn") ? "warn" : "on";
    }

    /**
     * Bad-sector counts at which a drive is worth replacing (owner: amber
     * only when a replacement is warranted). Below these a non-zero
     * count is a note: a few remapped or unreadable sectors are common on
     * older drives and say little on their own.
     */
    const SECTOR_LIMITS = {
        pending_sectors: 50, uncorrectable_sectors: 50, reallocated_sectors: 50, media_errors: 10,
    };
    const SECTOR_LABELS = {
        media_errors: "media errors", reallocated_sectors: "reallocated", pending_sectors: "pending", uncorrectable_sectors: "uncorrectable",
    };

    /**
     * A disk's bad-sector counters as one finding: a warning to plan a
     * replacement when any reaches {@link SECTOR_LIMITS}, otherwise a
     * note, or nothing when all are zero.
     * @param {object} disk Disk record with `smart`.
     * @returns {{tone: "warn"|"info", text: string}|null} The finding.
     */
    function sectorIssue(disk) {
        const smart = disk.smart || {};
        const counts = Object.keys(SECTOR_LABELS).filter((key) => typeof smart[key] === "number" && smart[key] > 0);
        if (!counts.length) {
            return null;
        }
        const text = counts.map((key) => `${smart[key].toLocaleString()} ${SECTOR_LABELS[key]}`).join(", ");
        const over = counts.some((key) => smart[key] >= SECTOR_LIMITS[key]);
        return over ? {
            tone: "warn", text: `${text}: plan a replacement`
        } : { tone: "info", text: `${text}, below replacement levels` };
    }

    /**
     * Everything worth noting on one disk, worst first: SMART health,
     * wear, heat, and bad-sector counters (the same rules the Attention
     * list uses). Tones: alarm (failing), warn (act: replace, or cool it),
     * info (a note only).
     * @param {object} disk Disk record with `smart`.
     * @returns {Array<{tone: "alarm"|"warn"|"info", text: string}>} Issues.
     */
    function diskIssues(disk) {
        const issues = [];
        const smart = disk.smart || {};
        if (disk.health && !/^(PASSED|OK)$/i.test(disk.health)) {
            issues.push({ tone: "alarm", text: `SMART health ${disk.health}` });
        }
        const wear = wearTone(disk.worn_percent);
        if (wear === "warn" || wear === "alarm") {
            issues.push({ tone: wear, text: `${disk.worn_percent}% worn` });
        }
        const heat = tempTone(disk);
        if (heat === "warn" || heat === "alarm") {
            issues.push({ tone: heat, text: `${smart.temperature_c} °C` });
        }
        const sectors = sectorIssue(disk);
        if (sectors) {
            issues.push(sectors);
        }
        const rank = { alarm: 0, warn: 1, info: 2 };
        return issues.sort((a, b) => rank[a.tone] - rank[b.tone]);
    }

    /**
     * The device class a CRUSH rule selects, by Proxmox's naming
     * (`replicated_hdd` keeps to hdd OSDs); null when it names none.
     * @param {string|null} rule CRUSH rule name.
     * @param {Array<object>} osds OSD rows, for the classes present.
     * @returns {string|null} Device class.
     */
    function ruleClass(rule, osds) {
        const classes = new Set(osds.map((osd) => osd.device_class).filter(Boolean));
        const match = /[_-]([a-z0-9]+)$/.exec(rule || "");
        return match && classes.has(match[1]) ? match[1] : null;
    }

    /**
     * Each Ceph pool's colour slot, fixed by its place in the pool list
     * ordered by raw use (the Ceph subtabs' order): the five categorical
     * slots, then "other".
     * @param {object} ceph Curated Ceph status.
     * @returns {Array<{pool: object, slot: string, deviceClass: string|null}>} Pools in order.
     */
    function cephPoolSlots(ceph) {
        const osds = ceph.osd_list || [];
        return (Array.isArray(ceph.pool_list) ? ceph.pool_list : []).slice()
            .sort((a, b) => (b.raw_used || 0) - (a.raw_used || 0))
            .map((pool, index) => ({
                pool, slot: index < 5 ? String(index + 1) : "other", deviceClass: ruleClass(pool.rule, osds)
            }));
    }

    /**
     * The pools that can place data on an OSD: those whose rule keeps to
     * its device class, or names none.
     * @param {object} osd OSD row.
     * @param {Array<object>} slots From {@link cephPoolSlots}.
     * @returns {Array<object>} The matching slot entries, in order.
     */
    function osdPools(osd, slots) {
        return slots.filter((entry) => !entry.deviceClass || entry.deviceClass === osd.device_class);
    }

    window.LabratClusterModel = {
        runningGuests, storagePools, clusterHistory, mergeLocal, osFamily, guestOs, fullestDisk, attentionItems,
        tempTone, wearTone, diskTone, diskIssues, sectorIssue, SECTOR_LIMITS, ruleClass, cephPoolSlots, osdPools, WEAR_WARN, WEAR_ALARM, TEMP_WARN, TEMP_ALARM,
    };
})();
