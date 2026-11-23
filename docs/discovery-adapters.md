# Discovery adapters and source behavior

LabRat's **independent collector** gathers observations through
**adapters**: small, read-only readers, one per kind of source. The first
integrations are UniFi Network (its local Integration API) and Proxmox VE
(its JSON API), with Pi-hole beside them, and the discovery methods
(service discovery for NUT, SNMP interface lookups, and passive MikroTik
MNDP). They are a starting set, not the boundary: any system that can
describe hardware, ports, or links read-only can become another adapter
(see [Adding an adapter](#adding-an-adapter)).

Normal discovery runs outside the Flask web process. An administrator can
explicitly test a connection from Settings with one read-only API request;
that test does not run discovery. The collector stores physical hosts,
adopted network appliances, physical ports, logical network interfaces,
and evidence-backed candidate links as observations in the local database.

The Discovery map and Equipment view display source-discovered cable observations
with their evidence and confidence. Inferred links are trusted by default;
ambiguous links are tentative. Explicit port-down state overrides reported
speed. Where explicit state is missing, a positive negotiated speed may indicate
an active link. Observations do not silently create canonical devices, alter
placements, or change canonical cable records.

## Configuration

Administrators can add and edit integrations (UniFi Network, Proxmox VE,
and Pi-hole today) under **Settings → Integrations**. Saved secrets are never displayed; leave a
secret field blank to keep its current value. Changing the controller
address requires entering the secret again. **Test connection** checks a
read-only API collection without saving anything; **Save integration**
applies the fields and the explicit collection-enabled choice. Use the +
tool to find integrations by category or search; pencil tools open existing
connections in the shared drawer. Connection names can be changed without
changing source identities or losing observations. Compact rows show the
latest collector result with a lamp; open a connection for the complete
message and timestamps. Deleting a connection requires confirmation and
removes its saved credentials and everything it discovered (devices, ports,
links, and ignore rules), freeing its name for reuse. Curated inventory,
placements, and audit history are kept; adding the controller again
discovers its devices afresh, to be linked to inventory in Discovery.

The same private TOML file is used by the web UI and collector, including
an explicit `LABRAT_CONFIG` path. UI saves preserve comments and unrelated
settings and replace the file atomically with owner-only permissions.
The collector reloads valid source settings before its next full scan.
There is no integration credential table or separate secret store. A
whole-install export (**Settings → Export and import**) carries these
credentials with the database; certificate files named by `ca_bundle` are
not in it, so copy those separately when moving an installation.


Existing single-controller sections work directly (no database registration or
manual config migration required):

```toml
[unifi]
url = "https://unifi.example.lan"
api_key = "REPLACE_WITH_READ_ONLY_UNIFI_KEY"

[proxmox]
url = "https://pve.example.lan:8006"
token_id = "labrat@pve!readonly"
token_secret = "REPLACE_WITH_READ_ONLY_PVE_SECRET"
```

For multiple controllers use named sections **instead of** the corresponding
top-level section:

```toml
[integrations.unifi.home]
url = "https://unifi.home.example"
api_key = "REPLACE_ME"

[integrations.proxmox.primary]
url = "https://pve.home.example:8006"
token_id = "labrat@pve!readonly"
token_secret = "REPLACE_ME"
```

All values remain in the Git-ignored `local_config.toml`. The database stores
only adapter kind and configuration name. Source registration is automatic before
full scans. File reloads never re-enable, reset, or delete existing sources.
An administrator may explicitly enable or disable collection when saving
an integration in Settings. An in-flight request can finish after disabling.

**TLS:** HTTPS and certificate validation are required by default. For an
internal CA, add `ca_bundle = "/absolute/path/to/ca.pem"` inside the source's
section. A controller reached by IP address with a self-signed certificate
cannot pass validation, because the certificate does not name the address;
for that case `verify_tls = false` (or **Don't verify the certificate** under
**Certificate settings** in Settings) turns validation off. The connection
stays encrypted, but LabRat then sends the credentials to whatever answers
at that address, so prefer giving the controller a DNS name and a matching
certificate where you can.

## Subnet and firewall evidence

The collector also reads UniFi networks, zones, traffic lists, and firewall
policies into a private derived cache beside the database. This prepares
source-scoped subnet candidates for future Discovery settings; it never grants
scan permission or runs additional subnet probes. The default refresh is every
15 minutes. Set `access_interval_seconds = 0` on a UniFi connection to disable
it, or an integer from 300 through 86400 to change its interval.

Access is evaluated by deterministic code for NUT and SNMP traffic. Unsupported
conditions, partial subnet exceptions, missing context, and stale evidence stay
unknown. A local route or an allowed policy does not guarantee a responding
service. Missing firewall API access leaves ordinary device discovery working.
The [focused API map](unifi-access-api-map.md) describes supported evidence.
Administrators manage methods and scan permission under **Settings → Discovery**.
Its subnet drawer uses cached suggestions and per-protocol access details;
newly discovered networks are never selected automatically.

## Running

```bash
python -m pip install -e .
alembic -c alembic.ini upgrade head
python -m unittest discover -s tests -v
alembic -c alembic.ini check
python -m labrat.collector --once
python -m labrat.collector --list
```

The `--once` invocation performs one complete collection cycle and exits. Run
`python -m labrat.collector` to poll repeatedly at the `[collector]` interval
(default: 60 seconds, or set `--interval 60`). The `--list` command **only**
reads stored observations; it makes no controller requests.

For example, the collector may report:

```text
proxmox:default: 2 physical device(s)
  proxmox:default [server] node-a
  proxmox:default [server] node-b
unifi:default: 1 physical device(s)
  unifi:default [switch] lab-switch
```

These are illustrative outputs, not discoveries from your network.

## Supported APIs

### Proxmox VE

- `GET /api2/json/nodes`: physical cluster members, including offline nodes
- `GET /api2/json/cluster/status`: node management IPs used to identify a host
  announced by LLDP. If this optional call is denied, NIC discovery continues
  and cross-source server wiring remains unavailable.
- `GET /api2/json/nodes/{node}/network`: physical Ethernet NIC names and
  MAC addresses where exposed by Proxmox, plus separate bond, bridge, and VLAN
  observations when returned by the API
- Duplicate-MAC interface aliases and rows marked as not existing are excluded
  when the API provides those fields.
- Optional client identity enrichment reads `/cluster/resources?type=vm` and
  `/nodes/{node}/{qemu|lxc}/{vmid}/config`. It retains only guest names, IDs,
  hosting nodes, states, and configured NIC MACs; guests do not become physical
  infrastructure nodes. Reads are capped at 128 guests, refreshed at most every
  five minutes, and stop starting requests after 30 seconds. Failed reads retain
  prior samples with a cached/unavailable label.
- To identify guests, assign a role containing `VM.Audit` on `/vms`, with
  propagation enabled. With privilege separation, assign it to both the user
  and API token. Creating a role does not assign it. An empty guest list can
  mean insufficient visibility; check the token’s effective permissions.
- Logical interfaces are stored separately from physical ports. Bond member
  interfaces annotate physical NICs; logical interfaces are not cable endpoints.
  Bridge and VLAN configuration appears in separate device-inspector tabs.
- Does not infer negotiated NIC speeds from static configuration
- If an offline node's interfaces cannot be read, previously discovered
  interfaces remain stored with their older last-seen timestamps
- A UniFi LLDP management IP plus an exact physical NIC name or reported MAC
  produces a confirmed Proxmox link when both identify one endpoint. Configure
  `lldpd` to advertise interface names when the host otherwise advertises a
  MAC and the Proxmox API does not include NIC MACs. If no node IP matches,
  LabRat records an inferred link only when an exact NIC-name or MAC match is
  unique across all discovered Proxmox nodes; ambiguous names remain unresolved.

- A neighbor's LLDP chassis ID that equals exactly one node's physical NIC MAC
  identifies that node as exactly as its management IP (`lldpd` announces a
  host NIC MAC as its chassis ID). If the address and chassis ID name
  different nodes, the neighbor is skipped.

#### NIC MACs from the node report

`GET /nodes/{node}/network` describes interface configuration and often omits
hardware addresses. For nodes with NICs that lack a MAC, the adapter also reads
`GET /nodes/{node}/report` (the API form of `pvereport`, readable with the same
`Sys.Audit` token) and takes each physical NIC's address from its
`ip -details` output, preferring `permaddr`. Bond members share the bond's
current address, while LLDP and switches see each NIC's own permanent one.
Current runtime addresses are also retained for known host interfaces, so a
UniFi client using a bond/member MAC can be associated with its top-level
server even without an IP. They never replace permanent physical NIC identity
or turn logical interfaces into cable endpoints. Missing logical MACs also
trigger the same cached report read when physical NIC MACs are complete.
A MAC from the network API is never replaced. Two NICs on one node reporting
the same address get neither.

Producing a report runs many commands on the host and takes several seconds,
so each node's report is read at most once per `report_interval_seconds`
(default `3600`; `0` turns it off; otherwise 300 to 86400). Results are kept
in collector memory only, and a failed read keeps the previous result until the
next interval. The report also contains host configuration such as storage,
cluster, and hosts files. `collector/proxmox_report.py` parses it in memory and
discards it: never store, log, or display the report text.

```toml
[proxmox]
report_interval_seconds = 3600
```

The Proxmox API token needs read-only `Sys.Audit` access to each node for NIC
configuration and the node report, and to the cluster root path `/` for
`/cluster/status` node IPs.
For privilege-separated API tokens, the token ACL must be a subset of the
backing user's ACL; grant `Sys.Audit` to both at `/` and propagate it.

#### Optional SNMPv3 interface supplement

For nodes where neither the Proxmox API nor the node report gives physical
NIC MAC addresses, or to read live link state, the same `[proxmox]` settings
may include a nested SNMP configuration. The node report usually makes SNMP
unnecessary for MACs.
The node's management IP is taken from `/cluster/status`; no address is copied
into local config. Only names listed under `nodes` are queried. For example:

```toml
[proxmox.snmp]
nodes = ["pve1"]
username = "labrat"
auth_passphrase = "the first SNMPv3 passphrase"
priv_passphrase = "the second SNMPv3 passphrase"
# Optional; these are the defaults (Net-SNMP createUser names):
auth_protocol = "sha"     # sha, sha224, sha256, sha384, sha512
priv_protocol = "aes"     # aes (AES-128), aes192, aes256, aes192c, aes256c
port = 161
```

This uses SNMPv3 `authPriv`, SHA-1 and AES-128 unless the protocols are
set to match the agent's user. For AES-192 and AES-256, `aes192`/`aes256`
use Net-SNMP's key extension and `aes192c`/`aes256c` the one many network
vendors use; a mismatch logs the agent's answer (such as `wrongDigest`)
and which setting to check. It reads the standard IF-MIB interface name,
MAC, and operational status columns over UDP (161 by default). Results supplement matching Proxmox physical NIC rows by exact
interface name; they do not create another discovered device. Explicit SNMP
link state supersedes Proxmox's configured `active` value. A timeout or
unavailable SNMP agent leaves Proxmox API discovery intact. Keep both
passphrases only in Git-ignored `local_config.toml`.

### UniFi Network

Paths below start at the controller's Network application root. A UniFi OS
console or UniFi OS Server serves it under `/proxy/network` (the default);
a self-hosted Network application (usually on port 8443) serves it at its
own root. Choose **Controller type** in Settings, or set it in the
connection's section:

```toml
[unifi]
url = "https://controller.example:8443"
controller_type = "network_application"   # default: "unifi_os"
```

- `GET /proxy/network/integration/v1/sites`
- `GET /proxy/network/integration/v1/sites/{siteId}/devices` (paginated)
- `GET /proxy/network/integration/v1/sites/{siteId}/devices/{deviceId}`
- Uses `X-API-Key`; this infrastructure batch includes adopted physical network equipment
- Recognizes gateways, switches, and access points from the API's type,
  then known model prefixes, then documented features. An adopted device
  none of these classify is kept as other equipment (and its model logged)
  rather than dropped; one whose explicit type is not infrastructure is
  skipped.
- Physical ports use the API's stable `idx`/`portIdx` values; link speed is
  reported only when the API explicitly provides `speedMbps`.
- If per-port data contains **explicit** LLDP `deviceId` and `portIdx`
  neighbor identifiers for two adopted devices, it can produce candidate links.
  UniFi versions that expose only a generic `uplink.deviceId` without both
  physical port identifiers won't produce a connection from that alone.
- Connected clients are collected separately as observations for Discovery's
  Client devices view. DHCP leases, generic MAC-learning tables, and Nmap
  discoveries are not imported as inventory. Client inventory and placement
  require explicit user actions.

A separate endpoint collector reads the paginated Integration API
`sites/{siteId}/clients` list, with optional classic `stat/sta` telemetry and
local `v2/api/fingerprint_devices/0` labels. It retains a bounded whitelist,
uses its own source health, and never adds ordinary endpoints to the
infrastructure diagram. Its wired inferences require a unique eligible switch
port; Wi-Fi uses a separate AP association. User-owned names and placements
remain untouched. The in-app Discovery guide explains browsing and the
administrator-only, allowlist-gated common TCP port check.

The infrastructure adapter also reads one classic endpoint with the same API key:

- `GET /proxy/network/api/s/{site}/stat/device`, where `{site}` is the
  Integration API site's `internalReference` (for example `default`).

From it LabRat takes three things, and nothing else:

- **Cable medium per port** (`discovered_ports.medium`, revision 0011):
  `copper` for RJ45 media (`GE`, `2P5GE`, `10GE`), and for SFP cages with a
  module, `dac`, `fiber_multimode`, `fiber_singlemode`, or `fiber` from the
  module's `sfp_compliance` and `sfp_part`. Empty cages and unrecognized
  modules stay unknown.
- **Uplinks**: each device's own `uplink` record (local port, upstream device
  MAC, upstream port) becomes a *confirmed* link with evidence
  `unifi-uplink`. An access point with no ports in the Integration API gets
  its uplink port from this record. Equipment draws a same-room AP uplink from
  a rack-top or room-surface card to the switch's visible port pin, or to its
  card edge when the switch is on a shelf. It remains a physical cable in the
  normal link count, unlike the separate inferred Wi-Fi client path.
- **LLDP neighbors** that are not UniFi devices. UniFi exposes only a
  neighbor's chassis ID, port ID, and management IPs, not its name, so a
  neighbor becomes a device only when the LLDP vendor table identifies it:
  by network-OS port naming (MikroTik `ether3` or `bridge/sfp-sfpplus4`,
  Cisco `Gi1/0/24`, Juniper `ge-0/0/3`) or a MAC prefix. It is named by its
  vendor plus the end of its chassis ID, gets one port per announced port ID
  (with the medium of the UniFi port at the other end), and a *confirmed*
  `lldp` link. Other neighbors, such as hosts running `lldpd`, are retained
  as LLDP identity observations for cross-source matching rather than
  guessed as separate equipment.
- **Your own LLDP vendors.** Equipment the table does not know can be added
  to a UniFi connection's section; these entries are tried first. A
  `port_pattern` must match the whole announced port ID (case-insensitive)
  and must not match host interface names such as `eno1`; `ouis` are MAC
  prefixes of the chassis ID. An invalid entry fails the source's poll with
  a message naming it.

  ```toml
  [[unifi.lldp_vendors]]
  vendor = "Example Networks"
  port_pattern = 'port\d+'
  ouis = ["00:00:5e"]
  ``` The source strip reports LLDP neighbor
  count, Proxmox host matches, and resolved physical links.

If the classic endpoint is unavailable, the collector logs that and keeps
the Integration API data.

A device whose classic record has WAN interfaces (`wan1`, `wan2` ...) or a
gateway type (`udm`, `ugw`, `uxg`) is a **router**, even when the
Integration API also lists a switching feature, as a Dream Machine does.

#### VPN telemetry

Gateway telemetry includes an optional `wan.vpn` snapshot, refreshed with WAN
polls. Read-only `v2/api/site/<site>/vpn/connections` supplies explicit state,
association time, and receive/transmit bit/s rates. Classic `rest/networkconf`
supplies profile labels and enabled state; `stat/remoteuservpn` supplies only a
session count. Profiles absent from runtime remain unknown, not disconnected.
An idle `CONNECTED` tunnel stays connected. No synthetic traffic, pings, or
connection restarts are triggered. The v2 endpoint is also documented in the
[Unifly session implementation](https://github.com/hyperb1iss/unifly/blob/main/crates/unifly-api/src/session/vpn.rs).

The profile response may include secrets: `collector/vpn.py` copies only an
explicit whitelist and discards raw configuration, keys, passwords,
certificates, addresses, and session/user records. VPNs are logical telemetry,
never physical cable endpoints. Optional API failures retain previous sanitized
samples with their original timestamp and mark them unavailable; WAN collection
continues. Observations filters the panel by its owning gateway's room and
offers a read-only evidence drawer. No additional schema or credentials are
required, though unsupported controller versions may report VPN unavailable.

Each WAN reading also extends the gateway's `vpn_history` telemetry: per
tunnel ID, five-minute `[bucket, rx_bps, tx_bps, samples]` rows for one day
(at most 32 tunnels), drawn as each Observations tunnel card's traffic
chart. Only an available VPN reading adds points, so an endpoint outage
shows as a gap, not as zero traffic. `/api/v1/wan` returns it per gateway.

#### WAN status

Per-uplink ISP labels use interface provider metadata first, followed by the
gateway's `active_geo_info`, `geo_info`, and `last_geo_info` maps keyed `WAN`,
`WAN2`, etc. These stored controller results include standby circuits. The
site health ISP is an active-uplink fallback only; it must never be copied
onto a different WAN. Reading these fields does not start a routing test,
speed test, or external provider lookup.

For each gateway the adapter also reads, with the same key and GET only:

- `GET /proxy/network/api/s/{site}/stat/health` for the `wan` and `www`
  subsystems: ISP name, internet status, latency, drops, uptime, and the
  last speed test (left out when it never ran; the controller reports zeros);
- the gateway's `wan1`/`wan2` tables from its classic record: enabled, up,
  active, media, link speed, latency, availability, receive and transmit
  rates (stored in bit/s), uptime, and its IPv4 and IPv6 addresses. The
  public WAN address is stored and shown to signed-in users.

Full scans read these with the device list. Between scans the looping
collector polls only the WAN every `wan_interval_seconds` (per UniFi source;
default `30`, `0` turns it off, otherwise 10 to 300) using the site list,
`stat/health`, and `GET .../stat/device/{gateway MAC}`. WAN polls do not
change the source's attempt or success times. Readings are telemetry: they
are stored under the gateway observation's `wan` detail with a one-day
`wan_history` of five-minute averages (`collector.store.record_wan`), never
count as a device change, and write no audit events. The controller's
history reports need POST, so LabRat builds its own history from samples.
`GET /api/v1/wan` serves them; the header strip, Home, and the Equipment
view of the gateway's room show them.

Each enabled WAN interface with a reported physical port also creates a
source-scoped `modem` observation with one WAN port. Its reported name uses
that circuit's own ISP label when UniFi provides one, followed by the WAN
interface name; otherwise it falls back to the interface name. Discovery
shows this current reported name for the modem card while preserving any
user-owned canonical inventory name. The collector links that port to the
gateway's reported WAN port using
`unifi-wan-upstream` evidence and `inferred` confidence. It does not contact or
identify the upstream device directly. The observation follows the ordinary
Discovery placement flow; placing it creates the canonical inventory device.
Equipment draws its WAN link from the rack-top card to the reported gateway
port as a dashed path.

```toml
[unifi]
wan_interval_seconds = 30
```

#### Targeted client lookup for physical cables

After successful Proxmox and UniFi collections, LabRat queries the classic
`GET /proxy/network/api/s/{site}/stat/sta/{mac}` endpoint for each unique MAC
belonging to a discovered Proxmox physical NIC. A fresh wired client record
that names a known UniFi switch and port can produce an inferred physical link.
A server running lldpd announces one of its NIC MACs as its chassis ID, and the
controller sometimes files the server's other NICs as clients of that
"device" instead of the switch. When the named MAC belongs to the looked-up
NIC's own server, LabRat uses the switch whose LLDP table hears that chassis,
but only when exactly one port in the site hears it, on a switch in the latest
snapshot that is not offline, that port is the one the client record names, and
its LLDP remote port ID does not name a different NIC of that server. Shared or ambiguous NIC MACs, stale client observations, down ports, and
conflicts with existing links are excluded. LLDP evidence takes precedence.
Only the inferred cable observation is stored; ordinary client records are not
imported into inventory. The classic endpoint is undocumented and optional;
when requests fail, previous inferred observations retain their original
last-seen time. User cable overrides remain authoritative.

#### Unresolved neighbors and explicit identity matches

Switch inspector panels retain neighbor observations even when the remote port
is blank or the peer cannot be identified as inventory. Each observation shows
the local switch port, remote chassis and port identifiers, announced addresses,
and observation time. Discovery links to the corresponding Equipment inspector.

Choose **Match to server NIC** to select an existing inventory server and one
of its discovered physical NICs. The selection is an audited identity decision
stored as the device-owned `discovery.lldp_port_match` attribute. No migration
is required. A saved match resolves observed cables with `lldp-user-match`
evidence while the neighbor is observed. It is scoped to the UniFi source and
exact chassis/remote-port identity; duplicate identities and occupied endpoints
are rejected. A disappearing neighbor removes its observed cable while retaining
the operator's identity decision. **Remove neighbor match** is available from
the switch or target server inspector. Inventory placements and canonical cable
records are not changed by these actions.

Current client attachment parsing also accepts `uplink_mac`,
`uplink_device_mac`, `uplink_remote_port`, and `port_idx`, including equivalent
fields inside a current `uplink` object and numeric or `Port N` strings.
Contradictory aliases and historical `last_uplink` records do not establish a
current physical cable.

See the official, version-specific documentation under **UniFi Network →
Integrations**. API fields can vary by controller version.

### MikroTik MNDP (passive, optional)

Enabled by a `[mndp]` section in `local_config.toml`. Each collection listens
on UDP 5678 for `listen_seconds` (5 to 300, default 65) and **never sends**.
RouterOS broadcasts MikroTik Neighbor Discovery about once a minute on its
discovery interfaces (the "LAN" list by default), carrying the device's
identity, board model, RouterOS version, uptime, software ID, and the
announcing interface's name and MAC. Each device (keyed by software ID)
becomes an observation named by its identity; each announcing interface
becomes a port. No credentials are needed, but broadcasts do not cross VLANs
or routed firewalls, so the collector must share a segment with the
MikroTik's discovery interfaces (in containers or Kubernetes, see "Run on
Kubernetes" in `docs/setup.md`). A MikroTik also heard as a UniFi LLDP
neighbor appears once per source until linked to one inventory device.

#### Standalone MNDP listener

When LabRat cannot sit on the MikroTiks' segment (a Kubernetes pod, a
VM on another VLAN), run the listener on a machine that can, and let it
report over the API:

1. In **Settings → API tokens**, create a token with the "Submit MikroTik
   announcements" permission. Copy it: it is shown once.
2. On a host on that segment, with LabRat installed (`pip install .` from
   a checkout, or the container image):

   ```sh
   LABRAT_API_TOKEN=labrat_… python -m labrat.collector.mndp_push \
       --url https://labrat.example.lan
   ```

   Options: `--token-file` instead of the variable, `--listen-seconds`
   (5 to 300, default 65), `--interval` between reports (default 60, or
   the listen time when that is longer),
   `--ca-file` for a private certificate authority, and `--once`.

It listens exactly as the collector's own MNDP adapter does (it sends no
MNDP probes) and reports the decoded announcements over HTTPS to
`POST /api/v1/machine/mndp` with `Authorization: Bearer <token>`. Each
token reports into its own source, named `mndp:<token name>`, which the
collector never polls; devices and ports appear just as the built-in
listener records them. The listener needs no database or LabRat config,
and the token can do nothing but submit announcements. Revoke it in
Settings to stop it at once.

### Network UPS Tools (NUT), via service discovery (optional)

Service discovery only runs where `[scan]` allows it. `ranges` lists the only
networks or addresses LabRat may contact, and `probes` the protocols it may
try there; nothing else is scanned, and nothing is inferred or expanded from
what is found. NUT is the first probe.

```toml
[scan]
ranges = ["192.0.2.0/24"]
probes = ["nut"]

[scan.nut]                       # optional
rescan_interval_seconds = 3600   # how often to rescan ranges (300 to 86400)
ups_interval_seconds = 30        # UPS readings between scans (0 disables; 10 to 300)
```

- `collector/scan.py` is the reusable stage: a bounded TCP connect scan (at
  most 4096 addresses, 64 concurrent attempts, 0.5 s each) that sends no
  data. An open port is only a candidate.
- `collector/nut.py` confirms a candidate with NUT's own read-only protocol and
  reads it: `LIST UPS`, `LIST VAR <ups>`, and `LOGOUT` only. It never logs in,
  sets variables, or sends instant commands (no shutdown, test, or beeper).
  Responses are limited to 256 KiB and 3 s per read.
- `collector/adapters/nut.py` records the computer running each confirmed NUT
  server as a `server` device (named by reverse DNS when available) and each
  UPS it reports as its own `ups` device. A UPS on a USB driver
  (`usbhid-ups` and similar) is marked USB-attached to that computer, because
  NUT drivers run on the server they report to. A driver that can use USB or
  a serial line (such as `nutdrv_qx`) counts as USB when NUT reports the
  unit's USB IDs (`ups.vendorid`, `ups.productid`) or `port = auto`; a
  `dummy-ups` unit relays
  another source's data and is marked `relayed`, with no claim about its
  physical connection. The NUT server's address is not treated as a UPS
  network port, and no inventory device is created or placed.
- UPS readings (status tokens and a summary state, battery charge and
  runtime, load, power, voltages, temperature, alarm text) are telemetry under
  the observation's `ups` detail: they never count as device changes or write
  audit events. Between scans the collector re-reads known servers every
  `ups_interval_seconds` without scanning. Confirmed servers are remembered in
  memory between rescans; a restarted collector rescans once.
- mDNS (`_upsd._tcp`) was not used: multicast may not cross the VLAN, while a
  bounded scan of an allowed range is reliable.

### Inferred NUT server Wi-Fi attachment

After a successful UniFi collection, the collector reads the local Integration
API's connected clients and matches a NUT server's exact management IP. It
accepts only one current `WIRELESS` client with a valid MAC; its
`uplinkDeviceId` must resolve to an observed access point to draw the AP link.
The observation stores a small, time stamped `wireless` telemetry detail with
its source and evidence. An ambiguous match is not drawn. If the client lookup
fails, the previous reading is retained and becomes stale; a successful lookup
with no match records `not_seen`. The Equipment inspector shows the inferred
interface, IP, client MAC, AP and sample age. A fresh match draws a dashed
wireless path between visible placements. This evidence never creates a
physical port or cable, changes placement, or adds ordinary UniFi clients to
inventory.

### Inferred NUT server wired NIC

The same client walk (`collector/nut_wired.py`) also looks for a `WIRED`
client with exactly one NUT server's IP. For it, the collector reads the
classic per-MAC client record (`stat/sta/{mac}`, as for Proxmox NIC MACs)
and requires a fresh (five-minute) wired record whose switch MAC and port
name one switch and one known port that this UniFi source observed, and
that port must not be reported down. A match gives the NUT server one
inferred physical port, "Wired NIC" (remote ID `unifi-wired-nic`, the
client's MAC, the switch port's speed and medium), and an inferred
`unifi-client-ip` link to that switch port, owned by the UniFi source.
Unlike Wi-Fi, this is a physical cable: it counts as one and appears in
Equipment, Discovery, and the inspector. Any other link already on the
switch port wins, except the clients feed's own link to the same NIC (a
client port with this MAC): that is the same cable seen twice, so it is
replaced, and the client defers to the server from then on. When every UniFi lookup succeeds without a match, or the
match is ambiguous, the port and link are removed; a failed lookup keeps
them. Creation, changes, and removal are audited; an unchanged cable only
refreshes its last-seen time. The NUT adapter never marks its port list
complete, so its scans keep the inferred port.

### Pi-hole (optional)

The Networks page already spots likely Pi-holes without any key: DNS
servers that UniFi's DHCP hands out, and clients or Proxmox guests named
like Pi-hole. To see their statistics, add a **Pi-hole** integration in
Settings with its HTTPS address and an application password (Pi-hole v6:
Settings, Web interface / API) or, for Pi-hole v5, its API token. LabRat
reads today's query counts, blocking state, the day's history, and the top
allowed and blocked domains and clients. It never turns blocking on or off
or edits lists. It first asks `GET /api/auth`, without a credential,
whether this is a v6 Pi-hole (v6 answers with a session object, HTTP 401
when a password is set). Only when that is not v6 does it ask
`GET /admin/api.php?versions`, again without a credential, and only a
v5 versions answer lets it send the v5 API token (which v5 takes in the
query string). Anything else (a proxy error, a redirect, another server)
fails as "did not answer like a Pi-hole", and no credential is sent.
The password is optional in Settings: leave it blank for a Pi-hole
without one, and Test connection says so if the Pi-hole needs it. With v6 it then signs in with one `POST /api/auth`, reads
with GETs, and signs out with `DELETE /api/auth`, which only opens and
closes its own session; a Pi-hole with no password is read without
signing in. A refused password shows as refused in **Test connection**.
Pi-hole v6 serves HTTPS with its own self-signed certificate for
`pi.hole`: add that certificate as the CA bundle and use a matching name,
or turn certificate checking off for it. Every Pi-hole set up in
Settings appears among the Networks page's DNS sinkholes with its
statistics, even when UniFi's DHCP and client names give no sign of it,
and even before (or without) any UniFi network snapshot. The machine running the collector must be allowed
through your firewall to the Pi-hole's HTTPS port.

### OPNsense (optional)

Add an **OPNsense** integration in Settings with the firewall's HTTPS
address and an API key and secret. LabRat shows the firewall as one
device with its network cards as ports (link state and speed), and keeps
its health: version, uptime, load, memory, disks, and per-card counters.
The Networks page gets the firewall's networks and VLANs with their DHCP
ranges (dnsmasq or Kea), its WAN gateways, its firewall rules, its
interface groups as zones, its WireGuard peers, which services run, CARP
state, and its DNS blocklist (the firewall then counts as a DNS sinkhole
beside any Pi-hole); Discovery, Clients gets the devices it has seen.

Rules are shown as OPNsense reports them, in evaluation order: floating
rules, then interface groups, then interfaces, then the automatic system
rules. LabRat does not say what can reach what: a rule that is not
"quick" can be overridden by a later one, so it marks those, and rules
still in the old format ("not yet migrated"). Alias contents and
credentials, and blocklist URLs and domains, are never kept: only names
and counts.

The Observations watchlist shows three OPNsense conditions: a monitored
gateway down, a WireGuard peer with keepalive and no handshake for five
minutes, and a CARP virtual IP that left its usual state. Each needs a
good reading on the poll before, so the first poll, a new item, or a
return after a gap in collection never raises one, and a part LabRat
could not read raises or clears nothing. Unmonitored gateways and peers
without keepalive never raise one. Tested on OPNsense 26.7; **Test connection**
says so when another release answers.

A router knows that a device *used an address* on one of its networks
(ARP, NDP, a DHCP lease), not where it is plugged in. So an OPNsense
client is **Seen by router** while the firewall still lists it in ARP or
NDP, **Lease only** when it holds a lease but has not been seen, and
never "connected": switches and access points (UniFi) still say where a
device attaches. A device seen by both shows as one row, its switch or
access point first. Neighbours on a WAN (an interface with a gateway)
are upstream equipment and are left out.

**OPNsense has no read-only API role.** A privilege grants a whole page,
and each page's privilege covers its actions as well as its reads. So:

1. Create a group (System, Access, Groups) with only the privileges
   below, and a user in it with a long random password you never use.
   Never give it Firmware, Backup, System: Settings, or User Manager.
2. On that user, add an API key. The key and secret go in Settings; LabRat
   only ever sends `GET` requests to a fixed list of endpoints, without
   parameters, refuses redirects, and keeps only the fields it shows.
3. Limit the web interface (and so the API) to the address LabRat
   collects from, with a firewall rule or the web interface's listen
   interfaces.

Privileges, by what they let LabRat show (those marked * also allow
changes on that page, so leave them out unless you want that part):

| Privilege | Shows |
|---|---|
| System: Status | Test connection |
| Lobby: Dashboard (every user has it) | name, version, uptime, load, memory, disks |
| Status: Interfaces* | the network cards and their link |
| Diagnostics: Netstat | per-card counters |
| Diagnostics: ARP Table*, NDP Table* | clients seen on each network |
| Diagnostics: Routing tables* | which gateway is the default |
| System: Gateways* | gateway status, latency, and loss |
| Services: Dnsmasq DNS/DHCP: Settings* | dnsmasq's ranges, reservations, and leases |
| Services: DHCP: Kea(v4)* | Kea's subnets, reservations, and leases |
| Services: Unbound* | the DNS resolver's settings and blocklists |
| Status: DNS Overview | whether the DNS blocklist is on |
| Firewall: Rules [new]* | the firewall rules |
| Firewall: Aliases*, Groups* | alias names and sizes, interface groups (zones) |
| VPN: WireGuard: Status | WireGuard peers, handshakes, transfer |
| Status: Services* | which services run |
| Interfaces: Virtual IPs: Status* | CARP state |

Lobby: Dashboard is required: a poll cannot complete without the system
information it gives. System: Status is needed for **Test connection**.
Every other privilege is optional: a missing one shows its part as "not
available" and nothing else.

One integration is one firewall. Changing its address asks whether it is
the same firewall at a new address or a different one. If a poll finds
a different hostname *and* none of the network cards LabRat knew, it
pauses that integration and asks in Settings, so the known firewall
keeps its inventory link, label, and placement; a different firewall
then gets its own integration and the old one is kept as "(replaced)".
A new key, a new hostname alone, or a replaced card never asks.

OPNsense serves its web interface with a self-signed certificate: add
its CA as the CA bundle, or turn certificate checking off for it.

## Adding an adapter

Every adapter follows one contract, so the web app, the Discovery map,
and Equipment need no changes to show a new source:

1. **Read.** Implement `DiscoveryAdapter.poll(settings)` from
   `collector/contracts.py` in a module under `collector/adapters/`. It
   reads the source (read-only credentials, TLS verification on by
   default) and returns one `DiscoveryBatch`: devices, physical ports,
   logical network interfaces, and links, each with the source's own
   stable IDs. It never writes the database; the store applies the batch.
   Report what the source says, and leave out what it does not: missing
   data is not proof that something is gone or down.
2. **Describe.** Add the integration to `integration_catalog.toml`: its
   label, category, form fields (secret fields are never shown again), and
   the one read-only request **Test connection** makes. The catalog's
   kinds are the kinds the collector reads from `local_config.toml`
   (`[integrations.<kind>.<name>]`).
3. **Register.** Add the adapter to the collector's adapter map in
   `collector/__main__.py`.
4. **Test.** Drive the adapter with a fake client and canned responses
   (see `tests/test_step5_links.py`); tests never contact a real source.
5. **Document.** Add its section under [Supported APIs](#supported-apis):
   what it reads, the least privilege it needs, and what it never does.

Links carry evidence and a confidence (`confirmed`, `inferred`, or
`ambiguous`), so an adapter can offer a cable it only infers without it
being mistaken for one it saw.

## Safety and provenance

- HTTPS calls are GET-only, use finite timeouts, and disallow redirects. The one
  exception is Pi-hole v6, whose adapter opens and closes its own API session
  (`POST` and `DELETE /api/auth`) around its reads.
- The collector stores source-specific IDs and observation timestamps.
- A failed poll records an error **type**, never a raw exception containing a
  potential credential, URL, or private device payload.
- Missing observations are retained and become stale; absence isn't deletion.
- No automatic merge of discoveries from different sources. Cross-source
  identity requires an explicit operator link in Discovery.
- Source-observed links retain evidence and confidence and do not silently
  replace canonical cable records or manual link policies.

## Reference documentation

- [Official UniFi local API setup](https://help.ui.com/hc/en-us/articles/30076656117655-Getting-Started-with-the-Official-UniFi-API)
- [UniFi Network developer reference](https://developer.ui.com/network/v10.6.106/gettingstarted)
- [Proxmox API viewer](https://pve.proxmox.com/pve-docs/api-viewer/)
