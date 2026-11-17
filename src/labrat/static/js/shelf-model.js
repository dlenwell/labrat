// Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
// Used by: Discovery, Equipment and Diagram, Discovery clients, Cluster, Floor plan, Widgets.
/**
 * @file Disk shelf data from the cluster API (one source view): turns ZFS
 * pools, Ceph pools, the Ceph cluster, and every node's drives into the
 * plain `{health, chassis}` data the shelf widget draws
 * (widget-shelf.js). Exposed as window.LabratShelfModel; used by the
 * Cluster page, the rack view's server faceplates, the device drawer's
 * Drives section, and the widget feeds. Needs widgets.js and
 * cluster-model.js first.
 *
 * Tones (owner): green fine, amber plan a replacement or act, red
 * failing; notes never colour a bay. Activity lamps are simulated:
 * their rate follows a real reading (a node's I/O pressure, Ceph's
 * client throughput).
 */
(() => {
    "use strict";

    const { bytes, percent, rate, ago, severity } = window.LabratWidgets;
    const { diskIssues, tempTone, cephPoolSlots, osdPools } = window.LabratClusterModel;

    /** ZFS states for a device that is not serving data. */
    const DOWN = new Set(["FAULTED", "UNAVAIL", "REMOVED", "OFFLINE"]);
    /** Ceph's default near-full and full ratios, as percent of an OSD. */
    const OSD_NEARFULL = 85;
    const OSD_FULL = 95;
    /** Commit latency, in ms, worth a note on an OSD. */
    const OSD_SLOW_MS = 100;
    /** Latency dial (owner): 10 ms at the left stop, 150 at the right; yellow over 80, red over 100. */
    const LATENCY_FLOOR_MS = 10;
    const LATENCY_CEILING_MS = 150;
    const LATENCY_WARN_MS = 80;
    const LATENCY_ALARM_MS = 100;

    // ------------------------------------------------------------------
    // Shared readings
    // ------------------------------------------------------------------

    /**
     * Years (or days) of power-on time, short.
     * @param {number|null} hours Power-on hours.
     * @returns {string} Such as "6.3 y" or "40 d".
     */
    function powerOn(hours) {
        if (typeof hours !== "number") {
            return "—";
        }
        return hours >= 24 * 365 ? `${(hours / 24 / 365).toFixed(1)} y` : `${Math.round(hours / 24)} d`;
    }

    /**
     * A drive's temperature as a readout cell, toned by its thresholds.
     * @param {object|null} disk SMART record.
     * @returns {{text: string, tone: string}} The cell.
     */
    function tempCell(disk) {
        const temp = disk && disk.smart && disk.smart.temperature_c;
        return { tone: disk ? tempTone(disk) : "off", text: typeof temp === "number" ? `${temp}°C` : "—°C" };
    }

    /**
     * The SMART summary line for a bay's tooltip.
     * @param {object|null} disk SMART record.
     * @returns {string} The line, or "".
     */
    function smartTip(disk) {
        if (!disk) {
            return "";
        }
        const smart = disk.smart || {};
        return `SMART ${disk.health || "unknown"} · ${typeof smart.temperature_c === "number" ? `${smart.temperature_c} °C · ` : ""}powered on ${powerOn(smart.power_on_hours)}`;
    }

    /**
     * A drive's size and type, for a readout line.
     * @param {object|null} disk SMART record.
     * @returns {Array<string>} One readout pair.
     */
    function sizeLine(disk) {
        return [disk ? bytes(disk.size) : "—", disk ? `${(disk.type || "").toUpperCase()}${disk.rpm ? ` ${disk.rpm}` : ""}` : "no SMART"];
    }

    /**
     * A fill gauge: share full, amber from `warnAt`.
     * @param {number|null} value Share full, 0..1.
     * @param {number} warnAt Share at which it turns amber.
     * @param {string} label Tooltip detail.
     * @returns {object} Gauge spec for the shelf widget.
     */
    function fillSpec(value, warnAt, label) {
        const known = typeof value === "number";
        return {
            kind: "fill", value: known ? value : null, tone: known && value >= warnAt ? "warn" : "on",
            text: known ? `${percent(value)} full` : "Fill unknown",
            tip: known ? [`${percent(value)} full`, label].filter(Boolean) : ["Fill unknown"],
        };
    }

    /**
     * A latency dial (owner): 10 ms at the left stop, 150 ms at the right
     * (values beyond pin the needle there), yellow over 80 ms, red over
     * 100, with those zones drawn faintly.
     * @param {number|null} ms Latency in milliseconds.
     * @param {string} detail Tooltip detail (such as commit and apply).
     * @returns {object} Dial spec for the shelf widget.
     */
    function latencySpec(ms, detail) {
        const known = typeof ms === "number";
        const scale = (value) => (value - LATENCY_FLOOR_MS) / (LATENCY_CEILING_MS - LATENCY_FLOOR_MS);
        return {
            kind: "dial", ms,
            value: known ? scale(ms) : null,
            tone: !known ? "off" : ms > LATENCY_ALARM_MS ? "alarm" : ms > LATENCY_WARN_MS ? "warn" : "on",
            zones: [
                { from: scale(LATENCY_WARN_MS), to: scale(LATENCY_ALARM_MS), tone: "warn" },
                { from: scale(LATENCY_ALARM_MS), to: 1, tone: "alarm" }
            ],
            reading: known ? `${Math.round(ms)}ms` : "—",
            text: known ? `Latency ${ms} ms` : "Latency unknown",
            tip: known ? [
                `Latency ${ms} ms`, detail,
                `Scale ${LATENCY_FLOOR_MS}–${LATENCY_CEILING_MS} ms; yellow over ${LATENCY_WARN_MS}, red over ${LATENCY_ALARM_MS}`
            ]
                : ["Latency unknown"],
        };
    }

    /**
     * How busy a node's disks are, from its latest history point: I/O
     * pressure (PSI, percent of time stalled) or I/O wait.
     * @param {object} node Node record with `history`.
     * @returns {{level: string, source: string}} Level and its source.
     */
    function nodeBusyness(node) {
        const last = (node.history || []).slice(-1)[0] || {};
        const psi = typeof last.psi_io === "number" ? last.psi_io : null;
        const wait = typeof last.iowait === "number" ? last.iowait : null;
        const level = psi !== null ? (psi >= 5 ? "busy" : psi >= 0.5 ? "light" : "idle")
            : wait !== null ? (wait >= 0.05 ? "busy" : wait >= 0.005 ? "light" : "idle") : "idle";
        return {
            level,
            source: psi !== null
                ? `the node's I/O pressure (${psi.toFixed(2)}%)`
                : wait !== null
                ? `the node's I/O wait (${percent(wait)})`
                : "nothing (no I/O reading)"
        };
    }

    /**
     * How busy Ceph is, from its client throughput.
     * @param {object} ceph Curated Ceph status.
     * @returns {{level: string, source: string}} Level and its source.
     */
    function cephBusyness(ceph) {
        const total = (ceph.read_bytes_sec || 0) + (ceph.write_bytes_sec || 0);
        return {
            level: total >= 50e6 ? "busy" : total >= 1e6 ? "light" : "idle",
            source: `Ceph's client throughput (${rate(total)}, all pools)`
        };
    }

    /**
     * A chassis per host, in the cluster's node order (unknown hosts last).
     * @param {Map<string, Array<object>>} hosts Host → groups.
     * @param {Array<object>} nodes Node records.
     * @returns {Array<[string, Array<object>]>} Ordered entries.
     */
    function byNodeOrder(hosts, nodes) {
        const order = (host) => {
            const index = nodes.findIndex((node) => node.name === host);
            return index < 0 ? nodes.length : index;
        };
        return [...hosts.entries()].sort(([a], [b]) => order(a) - order(b));
    }

    /**
     * Ceph health as a health-box state and tone.
     * @param {string|undefined} health Such as "HEALTH_OK".
     * @returns {{state: string, tone: string}} State and tone.
     */
    function cephState(health) {
        const value = health || "UNKNOWN";
        return {
            state: value.replace("HEALTH_", ""),
            tone: value === "HEALTH_OK" ? "on" : value === "HEALTH_WARN" ? "warn" : "alarm"
        };
    }

    /**
     * Ceph's cluster-wide checks as findings (they name no pool).
     * @param {object} ceph Curated Ceph status.
     * @returns {Array<{tone: string, text: string}>} Findings.
     */
    function cephChecks(ceph) {
        return (ceph.checks || []).map((check) => ({
            tone: check.severity === "HEALTH_ERR" ? "alarm" : "warn", text: `Ceph: ${check.summary || check.code}`
        }));
    }

    // ------------------------------------------------------------------
    // ZFS
    // ------------------------------------------------------------------

    /**
     * The pool's member drives in order, each with its vdev group, ZFS
     * state and error counters, and the matching SMART record (by the
     * serial in the ZFS device ID, else by device name).
     * @param {object} pool Curated pool record (`vdevs`).
     * @param {object} node Its node (`disks`).
     * @returns {Array<object>} Members: `group`, `vdev`, `disk` (or null).
     */
    function members(pool, node) {
        const disks = node.disks || [];
        const found = [];
        const groups = [];
        for (const vdev of pool.vdevs || []) {
            groups[vdev.depth] = vdev.name;
            groups.length = vdev.depth + 1;
            if (!vdev.leaf) {
                continue;
            }
            const name = vdev.name.replace(/-part\d+$/, "");
            const disk = disks.find((item) => item.serial && name.includes(item.serial))
                || disks.find((item) => item.path && item.path.replace("/dev/", "") === name) || null;
            found.push({ group: vdev.depth ? groups[vdev.depth - 1] : pool.name, vdev, disk });
        }
        return found;
    }

    /**
     * A ZFS member's issues: device state and error counters (a note while
     * ZFS keeps it ONLINE), then the drive's own SMART issues.
     * @param {object} member Member from {@link members}.
     * @returns {Array<{tone: string, text: string}>} Issues.
     */
    function zfsIssues(member) {
        const { vdev, disk } = member;
        const issues = [];
        if (vdev.state && vdev.state !== "ONLINE") {
            issues.push({ tone: DOWN.has(vdev.state) ? "alarm" : "warn", text: `ZFS ${vdev.state}` });
        }
        const errors = (vdev.read || 0) + (vdev.write || 0) + (vdev.cksum || 0);
        if (errors) {
            issues.push({
                tone: vdev.state === "ONLINE" ? "info" : "warn",
                text: `ZFS errors: ${vdev.read || 0} read, ${vdev.write || 0} write, ${vdev.cksum || 0} checksum`
            });
        }
        return disk ? issues.concat(diskIssues(disk)) : issues;
    }

    /**
     * The last scrub or resilver, from ZFS's scan line, short.
     * @param {string|null} scan Such as "scrub repaired 0B in 13:44:07 with 0 errors on Sun Nov  8 14:08:09 2026".
     * @returns {{text: string, tone: "on"|"warn"|"off"}} Summary.
     */
    function scrub(scan) {
        if (!scan) {
            return { text: "No scrub on record", tone: "off" };
        }
        const match = /^(scrub|resilver)\w* repaired (\S+) in (\S+) with (\d+) errors on (.+)$/.exec(scan.trim());
        if (!match) {
            return { text: scan, tone: /in progress/.test(scan) ? "warn" : "off" };
        }
        const when = Date.parse(match[5].replace(/\s+/g, " "));
        const errors = Number(match[4]);
        return {
            text: `${match[1] === "scrub" ? "Scrub" : "Resilver"} ${Number.isNaN(when) ? match[5] : ago(new Date(when).toISOString())}: ${match[2]} repaired, ${errors} error${errors === 1 ? "" : "s"}, took ${match[3]}`,
            tone: errors ? "warn" : "on",
        };
    }

    /**
     * A ZFS member as a bay.
     * @param {object} member Member from {@link members}.
     * @param {object} pool Its pool.
     * @param {string} label Bay label (such as "BAY 01").
     * @param {object} busy Node busyness.
     * @param {Array<object>} [pools] Colour stripe entries.
     * @returns {object} Bay data.
     */
    function zfsBay(member, pool, label, busy, pools) {
        const { vdev, disk } = member;
        return {
            label, device: disk ? disk.path.replace("/dev/", "") : vdev.name.slice(0, 14), issues: zfsIssues(member),
            state: vdev.state, down: DOWN.has(vdev.state), verdict: DOWN.has(vdev.state) ? vdev.state : null,
            m2: Boolean(disk && disk.type === "nvme"), pools,
            // ZFS reports no per-drive fill; a vdev's members fill alike.
            gauges: [fillSpec(
                pool.size ? (pool.alloc || 0) / pool.size : null, 0.8,
                `${pool.name}'s allocation (ZFS does not report each drive's fill)`
            )],
            lines: [sizeLine(disk), [tempCell(disk), powerOn(disk && disk.smart && disk.smart.power_on_hours)]],
            activity: busy,
            tip: [
                `${label} · ${disk ? `${disk.model || "Drive"} (${disk.path})` : vdev.name}`,
                disk ? `${bytes(disk.size)} ${disk.type || ""}${disk.rpm ? `, ${disk.rpm} rpm` : ""} · serial ${disk.serial || "—"}` : "No SMART record matched this device.",
                `ZFS ${vdev.state || "unknown"} in ${member.group} · read ${vdev.read ?? 0} · write ${vdev.write ?? 0} · checksum ${vdev.cksum ?? 0}`,
                smartTip(disk),
            ],
        };
    }

    /**
     * One ZFS pool as shelf data: a health box beside its node's chassis,
     * one row of bays per vdev.
     * @param {object} pool Pool record with `node` (the node's name).
     * @param {object} node The node record (disks and history).
     * @returns {{health: object, chassis: Array<object>}} Shelf data.
     */
    function zfsShelf(pool, node) {
        const busy = nodeBusyness(node);
        const found = [];
        if (pool.errors && !/^No known data errors$/i.test(pool.errors)) {
            found.push({ tone: "alarm", text: pool.errors });
        }
        const groups = new Map();
        members(pool, node).forEach((member, index) => {
            const number = String(index + 1).padStart(2, "0");
            const bay = zfsBay(member, pool, `BAY ${number}`, busy);
            found.push(...bay.issues.map((item) => ({
                tone: item.tone, text: `Bay ${number} · ${bay.device}: ${item.text}`
            })));
            groups.set(member.group, (groups.get(member.group) || []).concat(bay));
        });
        const health = pool.state || pool.health || "UNKNOWN";
        const fraction = pool.size ? pool.alloc / pool.size : 0;
        const last = scrub(pool.scan);
        const named = [...groups.keys()].filter((group) => group !== pool.name);
        const count = [...groups.values()].reduce((total, list) => total + list.length, 0);
        return {
            health: {
                state: health, tone: health === "ONLINE" ? "on" : health === "DEGRADED" ? "warn" : "alarm",
                title: pool.name, subtitle: `${pool.node || node.name} · ${count} drive${count === 1 ? "" : "s"}${named.length ? ` · ${named.join(", ")}` : ""}`,
                meter: {
                    total: pool.size || 0, used: pool.alloc || 0, label: `${percent(fraction)} allocated`,
                    tone: severity(fraction)
                },
                facts: [
                    [
                        "Allocated", `${bytes(pool.alloc)} of ${bytes(pool.size)} (${percent(fraction)})`
                    ], ["Free", bytes(pool.free)],
                    [
                        "Fragmentation", pool.frag !== null && pool.frag !== undefined ? `${pool.frag}%` : "—"
                    ], ["Dedup", pool.dedup ? `${pool.dedup}×` : "—"],
                ],
                lines: [{ tone: last.tone, label: "Last scrub", text: last.text }],
                found, note: pool.status,
            },
            chassis: [{
                name: `${node.name} · ${pool.name}`, aside: `I/O ${busy.level}`,
                groups: [...groups.entries()].map(([name, bays]) => ({ name, bays }))
            }],
        };
    }

    // ------------------------------------------------------------------
    // Ceph
    // ------------------------------------------------------------------

    /**
     * An OSD's issues: down or out, near full or full, slow commits (a
     * note), then its drive's SMART issues.
     * @param {object} osd OSD row.
     * @param {object|null} disk Its drive's SMART record.
     * @returns {Array<{tone: string, text: string}>} Issues.
     */
    function osdIssues(osd, disk) {
        const issues = [];
        if (osd.status !== "up") {
            issues.push({ tone: "alarm", text: `${osd.name} is ${osd.status || "down"}` });
        } else if (!osd.in) {
            issues.push({ tone: "warn", text: `${osd.name} is out of the cluster` });
        }
        const used = typeof osd.percent_used === "number" ? osd.percent_used : null;
        if (used !== null && used >= OSD_NEARFULL) {
            issues.push({
                tone: used >= OSD_FULL ? "alarm" : "warn",
                text: `${percent(used / 100)} full (Ceph near-full is ${OSD_NEARFULL}%)`
            });
        }
        if (typeof osd.commit_latency_ms === "number" && osd.commit_latency_ms >= OSD_SLOW_MS) {
            issues.push({ tone: "info", text: `slow commits (${osd.commit_latency_ms} ms)` });
        }
        return disk ? issues.concat(diskIssues(disk)) : issues;
    }

    /**
     * An OSD's latency for its dial: the worse of commit and apply.
     * @param {object} osd OSD row.
     * @returns {number|null} Milliseconds, or null when neither is known.
     */
    function osdLatency(osd) {
        const known = [osd.commit_latency_ms, osd.apply_latency_ms].filter((value) => typeof value === "number");
        return known.length ? Math.max(...known) : null;
    }

    /**
     * The drive behind each OSD, from the nodes' disk lists.
     * @param {Array<object>} nodes Node records (`disks` with `osds`).
     * @returns {Map<string, object>} OSD ID → disk (with `node`).
     */
    function osdDisks(nodes) {
        const found = new Map();
        for (const node of nodes) {
            for (const disk of node.disks || []) {
                for (const id of disk.osds || []) {
                    found.set(String(id), { ...disk, node: node.name });
                }
            }
        }
        return found;
    }

    /**
     * One OSD as a bay, and its issues named for a health box.
     * @param {object} osd OSD row.
     * @param {object|null} disk Its drive's SMART record.
     * @param {object} busy Ceph busyness.
     * @param {Array<object>} slots Pool colours from cephPoolSlots.
     * @returns {{bay: object, issues: Array<{tone: string, text: string}>}} Bay data and named issues.
     */
    function osdBay(osd, disk, busy, slots) {
        const pools = osdPools(osd, slots).map((entry) => ({ slot: entry.slot, name: entry.pool.name }));
        const issues = osdIssues(osd, disk);
        const device = disk ? disk.path.replace("/dev/", "") : "—";
        const used = typeof osd.percent_used === "number" ? osd.percent_used / 100 : null;
        const kind = osd.device_class || (disk && disk.type) || "";
        return {
            issues: issues.map((item) => ({ tone: item.tone, text: `${osd.name} on ${osd.host}${disk ? ` (${device})` : ""}: ${item.text}` })),
            bay: {
                label: `OSD ${osd.id}`, device: `${device} · ${kind.toUpperCase()}`, issues,
                state: osd.status === "up" ? (osd.in ? "UP · IN" : "UP · OUT") : "DOWN",
                down: osd.status !== "up", verdict: osd.status !== "up" ? "DOWN" : !osd.in ? "OUT" : null,
                deviceClass: kind, pools, m2: kind === "nvme",
                // Fill, then the latency dial (owner): the worse of commit and apply.
                gauges: [
                    fillSpec(used, OSD_NEARFULL / 100, `${bytes(osd.used)} of ${bytes(osd.total)} on this OSD`),
                    latencySpec(osdLatency(osd), `Commit ${osd.commit_latency_ms ?? "?"} ms, apply ${osd.apply_latency_ms ?? "?"} ms`),
                ],
                // Four lines, like a ZFS bay, so the readout fits a 3.5" caddy.
                lines: [
                    [bytes(osd.total), `${osd.pgs ?? "?"} PGs`],
                    [
                        {
                            tone: used === null ? "off" : used * 100 >= OSD_NEARFULL ? "warn" : "",
                            text: used === null ? "—" : `${percent(used)} full`
                        },
                        tempCell(disk)
                    ],
                ],
                activity: busy,
                tip: [
                    `${osd.name} on ${osd.host}${disk ? ` · ${disk.model || "Drive"} (${disk.path})` : ""}`,
                    `${bytes(osd.used)} of ${bytes(osd.total)} used · ${osd.pgs ?? "?"} placement groups · ${osd.status}${osd.in ? ", in" : ", out"}`,
                    `Latency: commit ${osd.commit_latency_ms ?? "?"} ms, apply ${osd.apply_latency_ms ?? "?"} ms`,
                    pools.length ? `Pools: ${pools.map((pool) => pool.name).join(", ")}` : "",
                    disk ? `Serial ${disk.serial || "—"}` : "No SMART record matched this OSD.",
                    smartTip(disk),
                ],
            },
        };
    }

    /**
     * One Ceph pool as shelf data: a health box beside a chassis per host,
     * each holding the OSDs the pool's rule places data on.
     * @param {object} ceph Curated Ceph status.
     * @param {object} pool Curated pool.
     * @param {object} context `serving` (the pool's OSDs), `deviceClass`,
     *     `users` (Proxmox storages using it), and `nodes`.
     * @returns {{health: object, chassis: Array<object>}} Shelf data.
     */
    function cephPoolShelf(ceph, pool, context) {
        const { serving, deviceClass, users, nodes } = context;
        const busy = cephBusyness(ceph);
        const slots = cephPoolSlots(ceph);
        const drives = osdDisks(nodes);
        const hosts = new Map();
        const found = [];
        for (const osd of serving.slice().sort((a, b) => (a.id ?? 0) - (b.id ?? 0))) {
            const { bay, issues } = osdBay(osd, drives.get(String(osd.id)) || null, busy, slots);
            found.push(...issues);
            hosts.set(osd.host, (hosts.get(osd.host) || []).concat(bay));
        }
        const fraction = pool.class_capacity ? (pool.raw_used || 0) / pool.class_capacity : 0;
        const usable = pool.class_capacity && pool.size ? pool.class_capacity / pool.size : null;
        const down = serving.filter((osd) => osd.status !== "up" || !osd.in);
        if (fraction >= 0.8) {
            found.push({
                tone: fraction >= 0.9 ? "alarm" : "warn", text: `The pool's OSDs are ${percent(fraction)} used`
            });
        }
        found.push(...cephChecks(ceph));
        return {
            health: {
                ...cephState(ceph.health), title: pool.name,
                subtitle: `${pool.type || "replicated"} ${pool.size ?? "?"}/${pool.min_size ?? "?"} · ${serving.length - down.length} of ${serving.length} OSDs up · ${hosts.size} host${hosts.size === 1 ? "" : "s"}`,
                meter: {
                    total: pool.class_capacity || 0, used: pool.raw_used || 0,
                    label: `${percent(fraction)} of its OSDs' raw capacity`, tone: severity(fraction)
                },
                facts: [
                    ["Stored", `${bytes(pool.stored)} (${bytes(pool.raw_used)} raw)`],
                    ["Usable left", usable ? `about ${bytes(Math.max(0, usable - (pool.stored || 0)))}` : "—"],
                    ["Placement groups", `${pool.pg_num ?? "?"}${pool.pg_target && pool.pg_target !== pool.pg_num ? ` (target ${pool.pg_target})` : ""} · autoscale ${pool.autoscale || "off"}`],
                    ["CRUSH rule", `${pool.rule || "—"}${deviceClass ? ` (${deviceClass} only)` : ""}`],
                    ["Used for", (pool.applications || []).join(", ") || "—"],
                    ["Proxmox storage", (users || []).map((item) => item.storage).join(", ") || "none"],
                ],
                key: slots.filter((entry) => serving.some((osd) => osdPools(osd, [entry]).length)).map((entry) => ({ slot: entry.slot, label: entry.pool.name })),
                found,
            },
            chassis: byNodeOrder(hosts, nodes).map(([host, bays]) => ({
                name: `${host} · ${pool.name}`, aside: `I/O ${busy.level}`, groups: [{
                    name: deviceClass ? `${deviceClass} OSDs` : "OSDs", bays
                }],
            })),
        };
    }

    /**
     * The whole Ceph cluster as shelf data: a health box beside one
     * chassis per host, its OSDs grouped by device class.
     * @param {object} ceph Curated Ceph status.
     * @param {Array<object>} nodes Node records (for drives and order).
     * @returns {{health: object, chassis: Array<object>}} Shelf data.
     */
    function cephClusterShelf(ceph, nodes) {
        const busy = cephBusyness(ceph);
        const slots = cephPoolSlots(ceph);
        const drives = osdDisks(nodes);
        const osds = (ceph.osd_list || []).slice().sort((a, b) => (a.id ?? 0) - (b.id ?? 0));
        const hosts = new Map();
        const found = [];
        for (const osd of osds) {
            const { bay, issues } = osdBay(osd, drives.get(String(osd.id)) || null, busy, slots);
            found.push(...issues);
            hosts.set(osd.host, (hosts.get(osd.host) || []).concat(bay));
        }
        const fraction = ceph.bytes_total ? (ceph.bytes_used || 0) / ceph.bytes_total : 0;
        if (fraction >= 0.8) {
            found.push({
                tone: fraction >= 0.9 ? "alarm" : "warn", text: `Ceph raw capacity is ${percent(fraction)} used`
            });
        }
        found.push(...cephChecks(ceph));
        const counts = ceph.osds || {};
        const monitors = ceph.monitors || {};
        const clean = (ceph.pg_states || []).filter((row) => row.state === "active+clean").reduce(
            (total, row) => total + (row.count || 0), 0
        );
        return {
            health: {
                ...cephState(ceph.health), title: "Ceph cluster",
                subtitle: `${osds.length} OSDs on ${hosts.size} host${hosts.size === 1 ? "" : "s"} · ${ceph.pool_count ?? (ceph.pool_list || []).length} pools`,
                meter: {
                    total: ceph.bytes_total || 0, used: ceph.bytes_used || 0,
                    label: `Raw capacity ${percent(fraction)} used`, tone: severity(fraction)
                },
                facts: [
                    ["Raw capacity", `${bytes(ceph.bytes_used)} of ${bytes(ceph.bytes_total)} (${percent(fraction)})`],
                    ["OSDs", `${counts.up ?? "?"} up · ${counts.in ?? "?"} in · ${counts.total ?? osds.length} total`],
                    ["Monitors", `${monitors.in_quorum ?? "?"} of ${monitors.total ?? "?"} in quorum`],
                    ["Placement groups", `${clean} of ${ceph.pgs ?? "?"} active+clean`],
                    ["Client I/O", `${rate(ceph.read_bytes_sec || 0)} read · ${rate(ceph.write_bytes_sec || 0)} write`],
                    ["Flags", (ceph.flags || []).join(", ") || "none"],
                ],
                key: slots.map((entry) => ({ slot: entry.slot, label: entry.pool.name })),
                found,
            },
            chassis: byNodeOrder(hosts, nodes).map(([host, bays]) => {
                const classes = new Map();
                for (const bay of bays) {
                    classes.set(
                        bay.deviceClass || "other", (classes.get(bay.deviceClass || "other") || []).concat(bay)
                    );
                }
                return {
                    name: host || "unknown host", aside: `I/O ${busy.level}`,
                    groups: [...classes.entries()].map(([name, list]) => ({ name: `${name} OSDs`, bays: list }))
                };
            }),
        };
    }

    // ------------------------------------------------------------------
    // Every drive in the cluster
    // ------------------------------------------------------------------

    /**
     * Pool colours for the whole cluster: Ceph pools first, in the Ceph
     * views' order (so they keep the same colours there), then each ZFS
     * pool; five categorical slots, then "other".
     * @param {object} source Cluster source view.
     * @returns {Array<{name: string, slot: string, kind: string}>} Pools in order.
     */
    function storageSlots(source) {
        const ceph = source.cluster && source.cluster.ceph;
        const names = (ceph ? cephPoolSlots(ceph).map((entry) => ({ name: entry.pool.name, kind: "ceph" })) : [])
            .concat(source.nodes.flatMap((node) => (node.zfs || []).map((pool) => ({
                name: `${pool.name} (${node.name})`, kind: "zfs"
            }))));
        return names.map((entry, index) => ({ ...entry, slot: index < 5 ? String(index + 1) : "other" }));
    }

    /**
     * Every node's drives as shelf data (owner: a cluster-wide shelf on
     * the Storage overview): one chassis per node, grouped by role (boot,
     * each ZFS pool, Ceph OSDs by class, other drives), each bay with its
     * gauges, pool stripe, and issues, beside a health box for them all.
     * @param {object} source Cluster source view (`nodes`, `cluster.ceph`).
     * @returns {{health: object, chassis: Array<object>}} Shelf data.
     */
    function clusterDrives(source) {
        const ceph = (source.cluster && source.cluster.ceph) || null;
        const osdList = (ceph && ceph.osd_list) || [];
        const cephSlots = ceph ? cephPoolSlots(ceph) : [];
        const slots = storageSlots(source);
        const cephBusy = ceph ? cephBusyness(ceph) : null;
        const found = [];
        const counts = { boot: 0, zfs: 0, ceph: 0, other: 0 };
        const types = new Map();
        let raw = 0;
        let hottest = null;
        const chassis = source.nodes.filter((node) => (node.disks || []).length).map((node) => {
            const busy = nodeBusyness(node);
            const used = new Set();
            const groups = [];
            const add = (name, bays) => bays.length && groups.push({ name, bays });
            const named = (bay, disk) => found.push(...bay.issues.map((item) => ({ tone: item.tone, text: `${node.name} ${disk ? disk.path.replace("/dev/", "") : bay.device}: ${item.text}` })));
            const plain = (disk, role, gauges, pools) => ({
                label: disk.path.replace("/dev/", ""), device: role, issues: diskIssues(disk), pools,
                m2: disk.type === "nvme", gauges, activity: busy,
                lines: [sizeLine(disk), [tempCell(disk), powerOn(disk.smart && disk.smart.power_on_hours)]],
                tip: [`${node.name} ${disk.path} · ${disk.model || "Drive"}`, `${bytes(disk.size)} ${disk.type || ""}${disk.rpm ? `, ${disk.rpm} rpm` : ""} · serial ${disk.serial || "—"}`,
                    `Role: ${role}${disk.used ? ` (${disk.used})` : ""}`, smartTip(disk)],
            });
            // Boot drives: the root filesystem's use is the closest fill there is.
            const boot = (node.disks || []).filter((disk) => disk.boot);
            add("Boot", boot.map((disk) => {
                used.add(disk.path);
                counts.boot += 1;
                const bay = plain(
                    disk, "boot",
                    [fillSpec(
                        node.rootfs_total ? (node.rootfs_used || 0) / node.rootfs_total : null, 0.85,
                        "The node's root filesystem"
                    )]
                );
                named(bay, disk);
                return bay;
            }));
            for (const pool of node.zfs || []) {
                const slot = slots.find((entry) => entry.kind === "zfs"
                    && entry.name === `${pool.name} (${node.name})`);
                add(`ZFS ${pool.name}`, members(pool, node).filter((member) => member.disk
                    && !used.has(member.disk.path)).map((member) => {
                    used.add(member.disk.path);
                    counts.zfs += 1;
                    const bay = zfsBay(
                        member, pool, member.disk.path.replace("/dev/", ""), busy,
                        slot ? [{ slot: slot.slot, name: pool.name }] : []
                    );
                    bay.device = `ZFS · ${(member.disk.type || "").toUpperCase()}`;
                    named(bay, member.disk);
                    return bay;
                }));
            }
            const osdByClass = new Map();
            for (const disk of (node.disks || []).filter((item) => (item.osds || []).length && !used.has(item.path))) {
                used.add(disk.path);
                counts.ceph += 1;
                const osd = osdList.find((item) => String(item.id) === String(disk.osds[0]));
                let bay;
                if (osd && cephBusy) {
                    // Short labels so the readout fits beside the gauges:
                    // the OSD, then its device (the class shows in the bay).
                    bay = osdBay(osd, disk, cephBusy, cephSlots).bay;
                    bay.label = `OSD ${osd.id}`;
                    bay.device = disk.path.replace("/dev/", "");
                } else {
                    bay = plain(disk, `OSD ${disk.osds.join(", ")}`, []);
                }
                named(bay, disk);
                const kind = (osd && osd.device_class) || disk.type || "other";
                osdByClass.set(kind, (osdByClass.get(kind) || []).concat(bay));
            }
            for (const [kind, bays] of osdByClass) {
                add(`Ceph ${kind} OSDs`, bays);
            }
            add("Other drives", (node.disks || []).filter((disk) => !used.has(disk.path)).map((disk) => {
                counts.other += 1;
                const bay = plain(disk, disk.used ? String(disk.used).slice(0, 18) : "unused", []);
                named(bay, disk);
                return bay;
            }));
            for (const disk of node.disks || []) {
                raw += disk.size || 0;
                types.set(
                    (disk.type || "other").toUpperCase(), (types.get((disk.type || "other").toUpperCase()) || 0) + 1
                );
                const temp = disk.smart && disk.smart.temperature_c;
                if (typeof temp === "number" && (!hottest || temp > hottest.temp)) {
                    hottest = { temp, where: `${node.name} ${disk.path.replace("/dev/", "")}` };
                }
            }
            return { name: node.name, aside: `I/O ${busy.level}`, groups };
        });
        const total = counts.boot + counts.zfs + counts.ceph + counts.other;
        const tone = found.some((item) => item.tone === "alarm")
            ? "alarm"
            : found.some((item) => item.tone === "warn")
            ? "warn"
            : "on";
        const zfsPools = source.nodes.flatMap((node) => (node.zfs || []).map((pool) => `${pool.name} ${pool.state || pool.health || "?"}`));
        return {
            health: {
                state: tone === "on" ? "HEALTHY" : tone === "warn" ? "ATTENTION" : "FAILING", tone,
                title: "Every drive", subtitle: `${total} drive${total === 1 ? "" : "s"} on ${chassis.length} node${chassis.length === 1 ? "" : "s"}`,
                facts: [
                    ["Raw capacity", bytes(raw)],
                    ["By type", [...types.entries()].map(([type, count]) => `${count} ${type}`).join(" · ") || "—"],
                    [
                        "By role",
                        [
                            ["boot", counts.boot], ["ZFS", counts.zfs], ["Ceph", counts.ceph], ["other", counts.other]
                        ].filter(([, count]) => count).map(([role, count]) => `${count} ${role}`).join(" · ") || "—"
                    ],
                    ["Ceph", ceph ? `${(ceph.health || "?").replace("HEALTH_", "")} · ${(ceph.osds || {}).up ?? "?"} of ${(ceph.osds || {}).total ?? osdList.length} OSDs up` : "none"],
                    ["ZFS pools", zfsPools.join(" · ") || "none"],
                    ["Hottest", hottest ? `${hottest.temp} °C (${hottest.where})` : "—"],
                ],
                key: slots.map((entry) => ({ slot: entry.slot, label: entry.name })),
                keyTitle: "Pools (stripe on each bay)",
                found,
            },
            chassis,
        };
    }

    /**
     * Each node's drives as one flat list of bays, in the same order and
     * with the same grading as {@link clusterDrives} (for a server's
     * faceplate on the rack view, owner).
     * @param {object} source Cluster source view.
     * @returns {Map<string, Array<object>>} Node name → bays.
     */
    function drivesByNode(source) {
        return new Map(clusterDrives(source).chassis.map((box) => [
            box.name, box.groups.flatMap((group) => group.bays)
        ]));
    }

    /**
     * Each node's chassis from {@link clusterDrives} (its drives grouped
     * boot, ZFS, Ceph, other), for one node's shelf (the device drawer).
     * @param {object} source Cluster source view.
     * @returns {Map<string, object>} Node name → chassis.
     */
    function chassisByNode(source) {
        return new Map(clusterDrives(source).chassis.map((box) => [box.name, box]));
    }

    window.LabratShelfModel = Object.freeze({
        zfsShelf, cephPoolShelf, cephClusterShelf, clusterDrives, drivesByNode, chassisByNode,
        // For tests and other models.
        members, scrub, osdIssues, osdDisks, osdLatency, latencySpec, fillSpec, storageSlots,
    });
})();
