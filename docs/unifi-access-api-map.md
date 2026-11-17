# UniFi subnet and firewall API map

This focused reference owns the API shapes used by the planned Discovery access
assessment. Settings controls and permission editing are in
`../planning/specs/discovery-settings-ui.md`. It contains synthetic
examples, not a controller export. Private reconnaissance responses are cached
in ignored storage; consult local cache notes before repeating live requests.

## Read-only requests and completeness

Use the existing HTTPS/API-key client behavior: credential headers, bounded
GET requests, no redirects, and the integration's configured TLS verification.
Paths below are relative to `/proxy/network/integration/v1`.

| GET path | Relevant response | Purpose |
|---|---|---|
| `/info` | `applicationVersion` | Record API/application version with evidence. |
| `/sites` | Paged `data`: `id`, `internalReference`, `name` | Scope all later evidence to its integration and site. |
| `/sites/{siteId}/networks` | Paged summaries: `id`, `name`, `enabled`, `management`, `zoneId` | Enumerate network IDs; summaries do **not** supply CIDRs. |
| `/sites/{siteId}/networks/{networkId}` | Detail object; optional `ipv4Configuration.hostIpAddress` and `prefixLength` | Normalize the gateway's interface address/prefix into a subnet. |
| `/sites/{siteId}/firewall/zones` | Paged `id`, `name`, `networkIds`, `metadata` | Relate networks to zones; retain unresolved/contradictory relationships. |
| `/sites/{siteId}/firewall/policies` | Paged policies including `index`, selectors, action, state and metadata | Include user, system, and derived policies. |
| `/sites/{siteId}/firewall/policies/ordering` | `orderedFirewallPolicyIds.beforeSystemDefined` / `afterSystemDefined` | Check user policy order for a particular zone pair. |
| `/sites/{siteId}/traffic-matching-lists` | Paged `id`, `name`, `type`, `items` | Resolve address/port list references. |

Paged envelopes contain `offset`, `limit`, `count`, `totalCount`, and `data`.
Follow pages until complete, reject inconsistent/duplicate results, and bound
requests. Never treat a first page as the full policy set. Do not let a partial
or changing snapshot establish a current allow/block verdict.

The ordering request requires **both** `sourceFirewallZoneId` and
`destinationFirewallZoneId` query parameters. An unqualified request is not a
usable ordering lookup. Its arrays describe user rules around system-defined
rules; they are not a complete replacement for the full policy list.

Policy `index` values must be scoped to the source/destination zone pair;
indexes can repeat across different pairs. Reconnaissance compared ascending
indexes with the explicit user ordering and found agreement. Keep this
validation when fetching production snapshots; unsupported/inconsistent order
must remain unknown. Ordering behavior across other API versions still needs
verification rather than an assumed global sort.

Official references: [policy list](https://developer.ui.com/network/v10.6.106/getfirewallpolicies),
[zone list](https://developer.ui.com/network/v10.6.106/getfirewallzones), and
[policy ordering](https://developer.ui.com/network/v10.6.106/getfirewallpolicyordering).
The reference pages embed their OpenAPI schemas in their HTML; the private
reconnaissance cache includes a parsed copy for offline consultation.

## Networks and source context

Synthetic network detail fragment:

```json
{
  "id": "network-example", "name": "Example network", "enabled": true,
  "management": "GATEWAY", "zoneId": "zone-example",
  "ipv4Configuration": {"hostIpAddress": "192.0.2.1", "prefixLength": 24}
}
```

This describes `192.0.2.0/24`; it does not authorize scanning. `UNMANAGED`
networks may omit address configuration and zone IDs. Keep those visible with
unknown CIDR/access instead of inferring an address from their VLAN or name.
IPv6 candidate extraction is not implemented in this first slice.

A site-local route assessment consumes an exact-destination kernel route
lookup from the collector's runtime namespace. It matches the chosen source
address to a unique network and compares the next hop to that network's
reported gateway address. Missing/overlapping mappings, a different next hop,
unmanaged destination, or direct same-segment traffic cannot establish that
the assessed gateway firewall is on the path. A local route alone does not
prove remote routability, return routing, or a listening service. NAT, remote
routing, host firewalls, and switching ACLs are outside this first evaluator.

## Policy normalization

A policy contains `id`, `enabled`, `index`, `action`, `source`, `destination`,
`ipProtocolScope`, optional `connectionStateFilter`, and display/metadata fields.
Each endpoint contains `zoneId` and an optional `trafficFilter`.

| Condition | First-slice handling |
|---|---|
| Actions `ALLOW`, `BLOCK`, `REJECT` | Normalize to allowed/blocked for initial traffic. Unknown actions remain unknown. |
| `ipVersion`: `IPV4`, `IPV6`, `IPV4_AND_IPV6` | Match address family. |
| `NAMED_PROTOCOL` with `protocol.name` | TCP, UDP, ICMP, ICMPV6; honor `matchOpposite`. |
| `PRESET` with `preset.name = TCP_UDP` | Match TCP or UDP. |
| `connectionStateFilter` | Match the initial `NEW` flow separately from established/related traffic. |
| Endpoint `IP_ADDRESS` | `ipAddressFilter`, optional `portFilter`. |
| Endpoint `PORT` | `portFilter`. |
| Endpoint `NETWORK` | `networkFilter.networkIds`, optional `portFilter`. |
| Address filter `IP_ADDRESSES` | Inline `items`: `IP_ADDRESS.value`, `SUBNET.value`, `IP_ADDRESS_RANGE.start/stop`. |
| Port filter `PORTS` | Inline `items`: `PORT_NUMBER.value`, `PORT_NUMBER_RANGE.start/stop`. |
| Filter `TRAFFIC_MATCHING_LIST` | Resolve `trafficMatchingListId` to a typed list's `items`; missing/wrong-type lists are unknown. |
| `matchOpposite` | Explicit boolean negation for addresses, networks, ports, named protocols. |
| MAC, application, region, domain, VPN selectors; schedules/IPsec; new conditions | Unsupported; retain an unknown rule barrier if it could match before a decisive policy. |

Endpoint selectors are conjunctive. A known mismatch can exclude a rule even
when another condition is unresolved. Do not discard an unknown condition and
continue to a later allow. A matching explicit catch-all can express a default;
absence of a matching policy does not imply an undocumented default.

Whole-CIDR matching uses address intervals without enumerating hosts. An
exception for part of a subnet yields unknown for that subnet, even when a
specific host can be evaluated. Source-port filters also remain unknown when
the actual source port is unspecified. An allow describes configured initial
traffic only, not successful discovery or return-path access.

## Older API observations

The existing classic `GET /proxy/network/api/s/{site}/rest/networkconf`
contains `ip_subnet` and richer network configuration, but may include sensitive
configuration blobs. Prefer the integration detail endpoint for subnet evidence.
The classic `rest/firewallgroup` and `rest/firewallrule` responses must not be
assumed to represent the current zone policy engine. Use integration traffic
lists and policies for this evaluator; legacy firewall evaluation is not yet
supported. Classic site references differ from integration site IDs.

## Implementation entry points

- `collector/access_collection.py`: bounded scheduled refresh, private cache, and freshness.
- `collector/unifi_access.py`: extract subnet candidates and normalize policies.
- `collector/access_policy.py`: pure deterministic first-match evaluator.
- `collector/access_route.py`: pure interpretation of supplied local route facts.
- `tests/test_access_policy.py`: synthetic rule, subnet, route, and uncertainty cases.

Collection scheduling and private cache semantics are in
`../planning/specs/discovery-access-collection.md`. The Settings UI contract is
in `../planning/specs/discovery-settings-ui.md`. Callers must establish snapshot completeness/freshness and gateway
path before requesting a definitive result. Settings keeps explicit user permission separate from this derived evidence.
