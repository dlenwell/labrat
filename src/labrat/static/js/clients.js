/**
 * @fileoverview Discovery endpoint browser, using shared details and placement.
 * @copyright 2026 LabRat contributors
 * @license Apache-2.0
 * SPDX-License-Identifier: Apache-2.0
 */
"use strict";

/**
 * Return a node's client observations, the primary one first: a currently
 * reported one before any Not seen, then the newest sample.
 * @param {object} node - Cached graph node with source observations.
 * @returns {object[]} Client observations, primary first.
 */
function clientObservations(node) {
    return [...(node.observations || [])].filter((o) => o.client).sort((a, b) =>
        (a.client.state === "not_seen") - (b.client.state === "not_seen")
        || String(b.client.sampled_at).localeCompare(String(a.client.sampled_at)));
}

/**
 * Return the primary client observation attached to a graph node.
 * @param {object} node - Cached graph node with source observations.
 * @returns {object|undefined} Primary client observation, if any.
 */
function clientObservation(node) {
    return clientObservations(node)[0];
}

/**
 * Fold client nodes that are known to be one device into one row.
 *
 * A Proxmox guest with several network cards appears to UniFi as several
 * clients (one per MAC, each with its own address); an exact guest match
 * (same source and VM ID) is the same device. Nodes are never merged on
 * names or addresses. The merged row keeps every observation, so details
 * list each card; an inventory-linked node supplies identity and placement.
 * @param {object[]} nodes - Client graph nodes.
 * @returns {object[]} Rows: original nodes, or merged copies.
 */
function mergeClientNodes(nodes) {
    const groups = new Map();
    for (const node of nodes) {
        const guest = clientObservation(node)?.client?.virtualization;
        const key = guest && guest.vmid != null ? `guest:${guest.source_id}:${guest.vmid}` : `node:${node.id}`;
        groups.set(key, [...(groups.get(key) || []), node]);
    }
    return [...groups.values()].map((group) => {
        if (group.length === 1) return group[0];
        const base = group.find((node) => node.device_id) || group[0];
        return {
            ...base, observations: group.flatMap((node) => node.observations), merged_ids: group.map((node) => node.id)
        };
    });
}

/**
 * List a row's addresses: each card's IP, or its MAC when it has none. IPs
 * come first (primary card first), then bare MACs. Duplicates are dropped.
 * @param {object} node - Client node or merged row.
 * @returns {string[]} Addresses.
 */
function clientAddresses(node) {
    const cards = clientObservations(node).map((o) => o.client);
    return [...new Set([
        ...cards.map((c) => c.ip_address), ...cards.filter((c) => !c.ip_address).map((c) => c.mac_address)
    ].filter(Boolean))];
}

/**
 * Choose an offline icon from reported device classification, never its name.
 * @param {object} client - Whitelisted controller client metadata.
 * @returns {string} Local icon template key.
 */
function clientIcon(client) {
    if (client.virtualization?.kind === "vm") return "vm";
    if (client.virtualization?.kind === "container") return "container";
    const kind = [
        client.device_category, client.device_family, client.device_model, client.operating_system
    ].filter(Boolean).join(" ").toLowerCase();
    const choices = [
        [/iphone|android.*phone|smartphone|mobile phone|phone/, "phone"],
        [/ipad|tablet/, "tablet"], [/desktop\/laptop/, "computer"], [/laptop|notebook|macbook/, "laptop"],
        [/print/, "printer"], [/camera|surveillance/, "camera"],
        [/television|smart.?tv|streaming|roku|chromecast/, "television"],
        [/speaker|audio|sonos|homepod/, "speaker"], [/console|playstation|xbox|nintendo/, "gamepad"],
        [/server|nas|storage/, "server"], [/iot|sensor|plug|thermostat|lighting/, "iot"],
        [/computer|desktop|windows|macos|linux/, "computer"],
    ];
    return choices.find(([pattern]) => pattern.test(kind))?.[1] || "client";
}

/**
 * Describe observation health without treating retained clients as online.
 * @param {object} client - Client metadata with source health flags.
 * @param {number} [now] - Current epoch milliseconds, injectable in tests.
 * @returns {{key:string,label:string,lamp:string}} Text and lamp state.
 */
function clientCondition(client, now = Date.now()) {
    if (!client.source_enabled || client.collection_error) return {key: "stale", label: "Unavailable", lamp: "warning"};
    if (client.state === "not_seen") return {key: "not_seen", label: "Not seen", lamp: "waiting"};
    const age = now - Date.parse(client.sampled_at);
    if (!Number.isFinite(age) || age < 0 || age > 600000) return {key: "stale", label: "Stale", lamp: "warning"};
    if (client.is_blocked) return {key: "connected", label: "Blocked", lamp: "warning"};
    return {key: "connected", label: "Connected", lamp: "ready"};
}

/**
 * Sort a client into exactly one toolbar kind: a matched Proxmox guest (VM
 * or container), else its reported Wi-Fi or wired attachment.
 * @param {object} client - Client metadata.
 * @returns {string} "vm", "wireless", or "wired".
 */
function clientKind(client) {
    if (client.virtualization) return "vm";
    return client.connection === "wireless" ? "wireless" : "wired";
}

/**
 * Search and filter cached nodes without modifying their inventory identity.
 * @param {object[]} nodes - Cached client graph nodes.
 * @param {object} [filters] - Toolbar choices: search text; kinds shown, any
 *     of "vm", "wireless", "wired" (default all three); unplaced only; not
 *     connected only (stale, unavailable, or not seen); and whether known
 *     server interfaces are shown.
 * @returns {object[]} Matching original node objects.
 */
function filterClients(
    nodes,
    {query = "", kinds = ["vm", "wireless", "wired"], unplaced = false, attention = false, showInterfaces = false} = {}
) {
    const search = query.trim().toLowerCase();
    return nodes.filter((node) => {
        const record = clientObservation(node)?.client;
        if (!record || !showInterfaces && record.server_interfaces?.length) return false;
        if (!kinds.includes(clientKind(record))) return false;
        // Unplaced means physical equipment awaiting a place, never a guest.
        if (unplaced && (node.placement || record.virtualization)) return false;
        if (attention && clientCondition(record).key === "connected") return false;
        const haystack = [node.name, ...node.observations.map((o) => o.reported_name),
            ...clientObservations(node).flatMap((o) => [o.client.ip_address, o.client.mac_address]),
            record.manufacturer, record.device_category,
            record.device_model, record.hostname, record.ssid, record.network_name,
            record.virtualization?.name, record.virtualization?.host, ...(record.server_interfaces || []),
            ...(record.networks || []).flatMap((n) => [n.name, n.cidr])].filter(Boolean).join(" ").toLowerCase();
        return !search || haystack.includes(search);
    });
}

/**
 * Name a client row: a matched guest's own name, unless inventory renamed it.
 *
 * UniFi names unnamed clients from a hostname plus MAC octets; a Proxmox
 * guest name is the better label, and an inventory name is the owner's.
 * @param {object} node - Client node.
 * @returns {string} Display name.
 */
function clientDisplayName(node) {
    const guest = clientObservation(node)?.client?.virtualization;
    return guest?.name && !node.device_id ? guest.name : node.name;
}

/**
 * Describe how a client attaches: its link kind, and the access point,
 * switch, or hypervisor it attaches through (when that node is known).
 * @param {object} node - Client node.
 * @param {object[]} nodes - Graph nodes, to find the attaching device.
 * @returns {{kind:string,device:object|null,label:string|null,detail:string|null,cached:boolean}}
 *     kind is "vm", "wireless", "wired", or "unknown"; label names the
 *     attaching device; detail is the bridges, Wi-Fi network, or switch
 *     port; cached marks guest metadata that is no longer fresh.
 */
function clientAttachment(node, nodes = []) {
    const client = clientObservation(node).client;
    const guest = client.virtualization;
    if (guest) {
        const hypervisor = nodes.find((n) => n.observations.some((o) => o.source_id === guest.source_id
            && o.remote_id === guest.host));
        // Every card's bridge, for a guest merged from several clients.
        const bridges = [...new Set(clientObservations(node).map((o) => o.client.virtualization?.bridge).filter(Boolean))];
        return {kind: "vm", device: hypervisor || null, label: hypervisor?.name || guest.host || null,
            detail: bridges.join(", ") || null, cached: !guest.fresh};
    }
    const uplink = nodes.find((n) => n.observations.some((o) => o.source_id === client.parent_source_id
        && o.remote_id === client.uplink_remote_id));
    const kind = ["wireless", "wired"].includes(client.connection) ? client.connection : "unknown";
    return {kind, device: uplink || null, label: uplink?.name || null, cached: false,
        detail: kind === "wireless"
            ? client.ssid
            || null
            : kind === "wired"
            && client.switch_port
            ? `Port ${client.switch_port}`
            : null};
}

/**
 * Build the same visible column values used for sorting and rendering.
 * @param {object} node - Client node.
 * @param {object[]} nodes - Graph nodes for attachment names.
 * @param {Function} placementLabel - Shared placement formatter.
 * @returns {object} Text by sortable column key.
 */
function clientColumns(node, nodes = [], placementLabel = () => "Placed") {
    const client = clientObservation(node).client;
    const guest = client.virtualization;
    const link = clientAttachment(node, nodes);
    return {
        type: guest ? guest.kind === "vm" ? "VM" : "Container" : client.device_category || "Client device",
        name: clientDisplayName(node),
        address: clientAddresses(node).join(" "),
        // A guest's attachment is its hypervisor and bridge, not its own name.
        connection: guest ? `${[link.label, link.detail].filter(Boolean).join(" - ")}${link.cached ? " · cached" : ""}` : [
            {wireless: "Wi-Fi", wired: "Wired"}[link.kind] || "Connection unknown", link.label, link.detail,
        ].filter(Boolean).join(" · "),
        status: clientCondition(client).label,
        // Guests are never placed (not physical equipment).
        placement: guest
            ? "Virtual"
            : node.placement
            ? placementLabel(node)
            : node.device_id
            ? "Inventory · unplaced"
            : "Unplaced",
    };
}

/**
 * Sort all matching rows, using natural numeric text ordering.
 * @param {object[]} nodes - Filtered nodes, never mutated.
 * @param {string} column - Visible column key.
 * @param {string} direction - asc or desc.
 * @param {Function} values - Column-value getter.
 * @returns {object[]} Stable sorted copy with missing values last.
 */
function sortClients(nodes, column, direction, values = clientColumns) {
    return [...nodes].sort((a, b) => {
        const left = String(values(a)[column] ?? ""), right = String(values(b)[column] ?? "");
        if (!left || !right) return left ? -1 : right ? 1 : 0;
        return (left.localeCompare(
            right, undefined, {numeric: true, sensitivity: "base"}
        ) * (direction === "desc" ? -1 : 1))
            || a.name.localeCompare(b.name, undefined, {numeric: true});
    });
}

/**
 * The Clients page: the client list, its toolbar, and the shared drawer
 * (details, placement, Add to inventory, and the port check). Reads and
 * writes go through the authenticated, same-origin API.
 */
class ClientsPage {
    /**
     * Build the drawer and bind every control; nothing loads until start().
     *
     * @param {HTMLElement} host - The page's #clients-app element.
     */
    constructor(host) {
        this.host = host;
        this.state = {
            nodes: [], clients: [], links: [], locations: {}, selected: null, scanVersion: 0, busy: false, sort: "name",
            direction: "asc", detailTab: "observation", portsFor: null
        };
        this.admin = this.host.dataset.admin === "true";
        this.drawer = new DeviceDrawer({host: this.host, rootView: "shelf", closeAtRoot: true,
            keepsOpen: (target) => Boolean(target.closest(".client-row")),
            onClose: () => {
                const opener = this.state.selected;
                labratShowDeviceInUrl("/topology/clients", null);
                this.state.scanVersion++; this.state.selected = null; this.drawer.reset("shelf"); this.renderList();
                this.element(`client-row-${opener}`)?.focus({preventScroll: true});
            },
        });
        this.details = new DeviceDetails({drawer: this.drawer, csrfToken: this.host.dataset.csrfToken,
            nodes: () => this.state.nodes, links: () => this.state.links, neighborMatches: () => [],
            locations: () => this.state.locations, reload: (...args) => this.refresh(...args), notify: (...args) => this.notice(...args),
            onPlace: (node) => this.placement.open(node), ignorable: true,
        });
        this.placement = new DevicePlacement({drawer: this.drawer, csrfToken: this.host.dataset.csrfToken,
            locations: () => this.state.locations, reload: (...args) => this.refresh(...args), notify: (...args) => this.notice(...args),
        });
        this.bindAddToInventory();
        this.bindPortCheck();
        this.bindToolbar();
    }

    /**
     * Look up one of the page's elements.
     *
     * @param {string} id - The element's ID.
     * @returns {?HTMLElement} The element.
     */
    element(id) {
        return document.getElementById(id);
    }

    /**
     * Read or write the API with this page's CSRF token (main.js labratJson).
     *
     * @param {string} path - The API path.
     * @param {?object} [payload=null] - A JSON body; given, the request is a POST.
     * @returns {Promise<object>} The parsed reply.
     */
    request(path, payload = null) {
        return labratJson(path, { payload, csrfToken: this.host.dataset.csrfToken });
    }

    /**
     * Show a status or error line above the list (empty hides it).
     *
     * @param {string} message - The message.
     * @param {boolean} [ok=false] - Style it as a success.
     */
    notice(message, ok = false) {
        this.element("clients-notice").textContent = message;
        this.element("clients-notice").hidden = !message;
        this.element("clients-notice").dataset.state = ok ? "ok" : "error";
    }

    /**
     * The node whose details are open: a client row, or the AP, switch, or
     * hypervisor named in its Connection / host column.
     *
     * @returns {object|undefined} The node, if any is selected.
     */
    selected() {
        return [
            ...this.state.clients, ...this.state.nodes
        ].find((node) => node.observations.some((o) => o.id === this.state.selected));
    }

    /**
     * Look up one part of the port check inside the Ports tab (it moves into
     * each freshly rendered details panel).
     *
     * @param {string} id - The part's ID.
     * @returns {?HTMLElement} The element.
     */
    ports(id) {
        return this.portsPanel.querySelector(`#${id}`);
    }

    /**
     * Add the Add to inventory tool to the drawer bar.
     */
    bindAddToInventory() {
        const footer = this.element("device-details-place").parentElement;
        const add = this.addButton = document.createElement("button");
        add.className = "button button-quiet room-icon-button"; add.type = "button";
        deviceToolLabel(add, "Add to inventory");
        const addIcon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        addIcon.setAttribute("viewBox", "0 0 24 24"); addIcon.setAttribute("aria-hidden", "true");
        const addPath = document.createElementNS(addIcon.namespaceURI, "path");
        addPath.setAttribute("d", "M3 5h18v14H3zM12 9v6M9 12h6");
        addIcon.append(addPath); add.append(addIcon);
        add.addEventListener("click", async () => {
            const observation = clientObservation(this.selected() || {});
            if (!observation || this.state.busy) return;
            this.state.busy = true; add.disabled = true;
            try {await this.request(`${LABRAT_API.client(observation.id)}/inventory`, {}); await this.refresh();}
            catch (error) {this.notice(error.message);}
            finally {this.state.busy = false; add.disabled = false;}
        });
        footer.prepend(add);
    }

    /**
     * Bind the Ports tab's check: one explicit, bounded TCP port check of the
     * selected client.
     */
    bindPortCheck() {
        this.portsPanel = this.element("client-port-check");
        this.ports("client-scan-run").addEventListener("click", async () => {
            const observation = clientObservation(this.selected() || {});
            if (!observation || this.state.busy) return;
            const version = ++this.state.scanVersion;
            this.state.busy = true; this.ports("client-scan-run").disabled = true;
            this.ports("client-scan-status").textContent = "Checking common TCP ports…";
            this.ports("client-scan-results").replaceChildren();
            try {
                const result = await this.request(`${LABRAT_API.client(observation.id)}/scan`, {});
                if (version !== this.state.scanVersion) return;
                this.ports("client-scan-status").textContent = `${result.open_ports.length} open ports · ${result.tested_count} common TCP ports checked at ${new Date(result.sampled_at).toLocaleTimeString()}. Service labels are hints, not verified identities.`;
                for (const port of result.open_ports) this.ports("client-scan-results").append(deviceText(
                    "span", "", `${port.port} · ${port.label}`
                ));
                if (!result.open_ports.length) this.ports("client-scan-results").append(deviceText(
                    "p", "device-muted",
                    "No tested port accepted a connection. Ports may be closed, filtered, or unreachable."
                ));
            } catch (error) {if (version === this.state.scanVersion) this.ports("client-scan-status").textContent = error.message;}
            finally {this.state.busy = false; if (version === this.state.scanVersion) this.ports("client-scan-run").disabled = false;}
        });
    }

    /**
     * Bind search, the filter toggles, sorting, Refresh, and fitting the list
     * to the window.
     */
    bindToolbar() {
        this.element("clients-search").addEventListener("input", (...args) => this.renderList(...args));
        // Phones: the Search button opens the search row (clients.css); the ☰
        // tool menu is shared with Infrastructure (view-frame.js).
        const toolbar = this.host.querySelector(".clients-toolbar");
        const searchButton = this.element("clients-search-toggle");
        searchButton.addEventListener("click", () => {
            // Keep a search with text open, so an active filter never hides.
            const open = !toolbar.hasAttribute("data-search-open") || Boolean(this.element("clients-search").value);
            toolbar.toggleAttribute("data-search-open", open);
            searchButton.setAttribute("aria-expanded", String(open));
            if (open) this.element("clients-search").focus();
        });
        for (const toggle of this.host.querySelectorAll(".clients-toolbar input[type=checkbox]")) toggle.addEventListener("change", (...args) => this.renderList(...args));
        for (const button of this.host.querySelectorAll("[data-sort]")) button.addEventListener("click", () => {
            this.state.direction = this.state.sort === button.dataset.sort
                && this.state.direction === "asc"
                ? "desc"
                : "asc";
            this.state.sort = button.dataset.sort; this.renderList();
        });
        this.fitList();
        window.addEventListener("resize", (...args) => this.fitList(...args));
        this.element("clients-refresh").addEventListener(
            "click", async () => {try {await this.refresh();} catch (error) {this.notice(error.message);}}
        );
    }

    /**
     * Open the shared details for any node: a client, or the device a
     * client attaches through.
     * @param {object} node - Graph node with at least one observation.
     */
    openNode(node) {
        const observation = clientObservation(node) || node.observations[0];
        if (!observation) return;
        this.state.scanVersion++;
        this.state.selected = observation.id;
        this.state.detailTab = "observation";
        this.renderDetails(); this.drawer.reset("details"); this.drawer.open("device-drawer-title"); this.renderList();
        // The address names the open device, so a refresh or a link reopens it.
        labratShowDeviceInUrl("/topology/clients", node.id);
    }

    /**
     * Split a client's details into tabs: its client observation, the shared
     * overview (identity, placement, interfaces), and the port check.
     * @param {HTMLElement} body - Rendered details body.
     * @param {HTMLElement} hero - Portrait kept above the tabs.
     */
    tabDetails(body, hero) {
        // The Network tab: how the network sees the client, plus its Network
        // box (a Wi-Fi client's link is a card there now).
        const observation = [...body.querySelectorAll(":scope > .device-client-details, :scope > .device-network-tabs-wrap")];
        // One section per card: label them when a merged row has several.
        // Same order as the shared renderer: the row's own observation order.
        const cards = (this.selected()?.observations || []).filter((o) => o.client);
        observation.filter((section) => section.matches(".device-client-details")).forEach((section, index) => {
            const heading = section.querySelector(":scope > h4");
            const record = cards[index]?.client;
            if (observation.length < 2 || !heading || !record) {heading?.remove(); return;}
            heading.textContent = [
                `Network card ${index + 1}`, record.ip_address || record.mac_address, record.virtualization?.bridge
            ].filter(Boolean).join(" · ");
            heading.classList.add("client-card-heading");
        });
        const overview = [...body.children].filter((child) => child !== hero && !observation.includes(child));
        const views = [
            {id: "observation", label: "Network", parts: observation},
            {id: "overview", label: "Overview", parts: overview},
        ];
        if (this.admin) views.push({id: "ports", label: "Ports", parts: [this.portsPanel]});
        if (!views.some((view) => view.id === this.state.detailTab)) this.state.detailTab = "observation";
        const wrapper = deviceText("div", "device-network-tabs-wrap client-detail-tabs", "");
        const tabs = deviceText("div", "device-network-tabs", "");
        tabs.setAttribute("role", "tablist");
        tabs.setAttribute("aria-label", "Client details");
        wrapper.append(tabs);
        const select = (id) => {
            this.state.detailTab = id;
            for (const button of tabs.children) {
                const active = button.dataset.tab === id;
                button.setAttribute("aria-selected", String(active));
                button.tabIndex = active ? 0 : -1;
            }
            for (const panel of wrapper.querySelectorAll(":scope > [role=tabpanel]")) panel.hidden = panel.dataset.tab !== id;
        };
        for (const view of views) {
            const button = deviceText("button", "device-network-tab", view.label);
            button.type = "button"; button.dataset.tab = view.id;
            button.id = `client-detail-tab-${view.id}`;
            button.setAttribute("role", "tab");
            button.setAttribute("aria-controls", `client-detail-panel-${view.id}`);
            button.addEventListener("click", () => select(view.id));
            button.addEventListener("keydown", (event) => {
                if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
                event.preventDefault();
                const buttons = [...tabs.children], index = buttons.indexOf(button);
                const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
                    : (index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
                buttons[next].focus(); buttons[next].click();
            });
            tabs.append(button);
            const panel = deviceText("div", "client-detail-panel", "");
            panel.id = `client-detail-panel-${view.id}`; panel.dataset.tab = view.id;
            panel.setAttribute("role", "tabpanel");
            panel.setAttribute("aria-labelledby", button.id);
            panel.tabIndex = 0;
            panel.append(...view.parts);
            wrapper.append(panel);
        }
        this.portsPanel.hidden = false;
        body.append(wrapper);
        select(this.state.detailTab);
    }

    /** Render shared details while preserving nested form drafts. */
    renderDetails() {
        const node = this.selected();
        this.details.render(node || null);
        const client = clientObservation(node || {})?.client;
        if (client) {
            const body = this.element("device-details-body");
            const hero = this.element("client-detail-hero").content.firstElementChild.cloneNode(true);
            hero.querySelector(".client-avatar").append(this.element(`client-icon-${clientIcon(client)}`).content.cloneNode(true));
            hero.querySelector("h3").textContent = clientDisplayName(node);
            // The shared panel titles itself from the node; use the row's name.
            const title = this.host.querySelector('[data-view="details"] .device-drawer-panel-title');
            if (title) {title.textContent = clientDisplayName(node); this.drawer.updateBar();}
            const guest = client.virtualization;
            hero.querySelector("p").textContent = guest
                ? `${guest.kind === "vm" ? "VM" : "Container"} ${guest.vmid} - ${[clientAttachment(node, this.state.nodes).label, clientAttachment(node, this.state.nodes).detail].filter(Boolean).join(" - ")}${guest.fresh ? "" : " (cached)"}`
                : [
                    client.device_model || client.device_family || client.device_category || "Client device",
                    client.ip_address
                ].filter(Boolean).join(" · ");
            const condition = clientCondition(client);
            hero.querySelector(".led").classList.add(`led-${condition.lamp}`);
            hero.querySelector(".client-condition > span:last-child").textContent = condition.label;
            body.prepend(hero);
            // A different client starts with a fresh port check.
            const observationId = clientObservation(node).id;
            if (this.state.portsFor !== observationId) {
                this.state.portsFor = observationId;
                this.ports("client-scan-status").textContent = "";
                this.ports("client-scan-results").replaceChildren();
            }
            this.ports("client-scan-permission").textContent = client.scan_reason || "";
            this.ports("client-scan-run").disabled = !client.scan_allowed || this.state.busy;
            this.tabDetails(body, hero);
        } else {
            this.portsPanel.hidden = true;
            this.host.append(this.portsPanel);
        }
        // VMs and containers are not physical equipment: no inventory or
        // placement from here (owner; the Cluster page shows them).
        const guest = Boolean(client?.virtualization);
        this.addButton.hidden = !this.admin || !client || guest || Boolean(node.device_id);
        this.element("device-details-place").hidden = !this.admin || guest;
        this.element("device-details-rename").hidden = !this.admin || !node?.device_id;
    }

    /**
     * Fit the list to the window, like the Infrastructure canvas: the page
     * keeps still and the rows scroll under their sticky headings, leaving
     * the shared footer in view. Short windows keep a usable minimum.
     */
    fitList() {
        const scroller = this.element("clients-scroll");
        const frame = this.element("clients-frame");
        const top = scroller.getBoundingClientRect().top + window.scrollY;
        const below = frame.getBoundingClientRect().bottom - scroller.getBoundingClientRect().bottom;
        const footer = document.querySelector(".site-footer")?.getBoundingClientRect().height || 0;
        scroller.style.height = `${Math.max(240, Math.floor(window.innerHeight - top - below - footer - 16))}px`;
    }

    /**
     * Fill a Connection / host cell: a Wi-Fi, wired, or host icon, then the
     * access point, switch, or hypervisor as a link that opens its details,
     * then the Wi-Fi network, switch port, or bridge.
     * @param {HTMLElement} cell - Table cell to fill.
     * @param {object} node - Client node.
     */
    renderAttachment(cell, node) {
        const link = clientAttachment(node, this.state.nodes);
        const label = {wireless: "Wi-Fi", wired: "Wired", vm: "Hosted on"}[link.kind] || "Connection unknown";
        const icon = deviceText("span", "client-link-icon", "");
        icon.title = label;
        icon.append(this.element(`client-icon-${{wireless: "wifi", wired: "wired", vm: "server"}[link.kind] || "client"}`).content.cloneNode(true));
        icon.append(deviceText("span", "visually-hidden", label));
        cell.replaceChildren(icon);
        if (link.device) {
            const button = deviceText("button", "client-uplink", link.label);
            button.type = "button";
            button.title = `Show ${link.label}`;
            button.addEventListener("click", (event) => {event.stopPropagation(); this.openNode(link.device);});
            cell.append(button);
        } else if (link.label) {
            cell.append(deviceText("span", "", link.label));
        } else if (link.kind === "unknown") {
            cell.append(deviceText("span", "", "Unknown"));
        }
        if (link.detail) cell.append(deviceText("span", "client-link-detail", `${link.kind === "vm" ? " - " : " · "}${link.detail}`));
        if (link.cached) cell.append(deviceText("span", "client-link-detail", " · cached"));
    }

    /** Display every matching row as a keyboard-selectable ledger line. */
    renderList() {
        const filters = {query: this.element("clients-search").value,
            kinds: [...this.host.querySelectorAll("[data-kind]:checked")].map((input) => input.dataset.kind),
            unplaced: this.element("clients-unplaced").checked, attention: this.element("clients-attention").checked,
            showInterfaces: this.element("clients-show-interfaces").checked};
        const columns = (node) => clientColumns(node, this.state.nodes, (n) => this.details.placementLabel(n));
        const filtered = sortClients(
            filterClients(this.state.clients, filters), this.state.sort, this.state.direction, columns
        );
        for (const heading of this.host.querySelectorAll("th[data-column]")) {
            const active = heading.dataset.column === this.state.sort;
            heading.setAttribute(
                "aria-sort", active ? this.state.direction === "asc" ? "ascending" : "descending" : "none"
            );
            heading.querySelector("span").textContent = active ? this.state.direction === "asc" ? "↑" : "↓" : "↕";
        }
        const rows = filtered;
        const list = this.element("clients-list"); list.replaceChildren();
        const visible = filterClients(this.state.clients, {showInterfaces: filters.showInterfaces});
        for (const node of rows) {
            const observation = clientObservation(node), client = observation.client;
            const row = this.element("client-row-template").content.firstElementChild.cloneNode(true);
            const opener = row.querySelector(".client-open");
            opener.id = `client-row-${observation.id}`;
            const values = columns(node);
            row.querySelector(".client-avatar").append(this.element(`client-icon-${clientIcon(client)}`).content.cloneNode(true));
            row.querySelector(".client-identity strong").textContent = values.name;
            // One truncated line: maker and family, never the model.
            const subtitle = client.virtualization
                ? `${values.type} - ${client.virtualization.host}`
                : [
                    client.manufacturer, client.device_family
                ].filter(Boolean).join(" - ") || client.device_category || "Client device";
            row.querySelector(".client-identity small").textContent = subtitle;
            row.querySelector(".client-identity small").title = subtitle;
            row.querySelector(".client-avatar").title = values.type;
            row.querySelector(".client-avatar").append(deviceText("span", "visually-hidden", values.type));
            // One line: the primary address, "+N" for a merged VM's other
            // cards, and every address in the tooltip.
            const addresses = clientAddresses(node), cell = row.querySelector(".client-address");
            cell.replaceChildren(deviceText("span", "client-address-line", addresses[0] || "No address reported"));
            cell.title = addresses.join("\n");
            if (addresses.length > 1) {
                cell.append(deviceText("span", "client-address-more", `+${addresses.length - 1}`));
                cell.append(deviceText("span", "visually-hidden", `, also ${addresses.slice(1).join(", ")}`));
            }
            this.renderAttachment(row.querySelector(".client-attachment"), node);
            const condition = clientCondition(client);
            row.querySelector(".led").classList.add(`led-${condition.lamp}`);
            row.querySelector(".client-condition > span:last-child").textContent = condition.label;
            row.querySelector(".client-place").textContent = values.placement;
            opener.setAttribute("aria-expanded", String(node.observations.some((o) => o.id === this.state.selected)));
            opener.setAttribute("aria-controls", "device-drawer");
            row.addEventListener("click", () => this.openNode(node));
            list.append(row);
        }
        list.parentElement.hidden = !rows.length; this.element("clients-empty").hidden = Boolean(rows.length);
        this.element("clients-empty-message").textContent = this.state.clients.length
            ? "Try another search or clear the filters."
            : "The collector will list clients reported by enabled UniFi connections.";
        // Mark the folded phone menu when any toggle is off its default.
        this.host.querySelector(".view-menu-dot").hidden = filters.kinds.length === 3
            && !filters.unplaced && !filters.attention && !filters.showInterfaces;
        // Header readout beside the title; each kind's count is in its
        // toggle's tooltip (and accessible name) instead.
        this.element("clients-summary").textContent = `${rows.length} of ${visible.length} shown`;
        for (const toggle of this.host.querySelectorAll("[data-kind-label]")) {
            const input = toggle.querySelector("[data-kind]");
            const total = visible.filter((n) => clientKind(clientObservation(n).client) === input.dataset.kind).length;
            const label = `Show ${toggle.dataset.kindLabel} (${total})`;
            toggle.title = label; toggle.dataset.tooltip = label;
            input.setAttribute("aria-label", label);
        }
    }

    /** Refresh cached data, keeping selection by its stable observation row. */
    async refresh() {
        const [graph, locations] = await Promise.all([
            this.request(LABRAT_API.clients), this.request(LABRAT_API.locations)
        ]);
        this.state.nodes = graph.nodes; this.state.clients = mergeClientNodes(graph.clients).sort((a, b) => a.name.localeCompare(b.name));
        this.state.links = graph.links; this.state.locations = locations; this.details.index();
        this.renderList(); this.renderDetails();
        const failed = graph.sources.filter((source) => source.kind === "unifi_clients" && source.last_error);
        this.notice(
            failed.length
                ? "Some client collections are unavailable. Retained observations are marked accordingly."
                : "",
            !failed.length
        );
    }

    /**
     * Load the cached data, reopen a device named in the address, and refresh
     * every 15 seconds while the page is idle.
     *
     * @returns {Promise<void>} Resolves after the first load.
     */
    async start() {
        try {
            await this.refresh();
            // A device link (/topology/clients/observed-110) opens its details.
            const requested = this.host.dataset.openDevice;
            const node = requested && [
                ...this.state.clients, ...this.state.nodes
            ].find((item) => item.id === requested);
            if (node) this.openNode(node);
            else if (requested) labratShowDeviceInUrl("/topology/clients", null);
        } catch (error) {this.notice(error.message);}
        window.setInterval(async () => {
            if (document.hidden || this.state.busy || this.drawer.isOpen) return;
            try {await this.refresh();} catch (error) {this.notice(`Client refresh unavailable: ${error.message}`);}
        }, 15000);
    }
}

/**
 * Start the Clients page when its template is present.
 * @returns {Promise<void>} Resolves after the initial cached data load.
 */
async function startClients() {
    const host = document.getElementById("clients-app");
    if (!host) return;
    await new ClientsPage(host).start();
}

void startClients();
