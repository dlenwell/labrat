# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Interpret local route evidence without claiming remote service reachability.

The caller obtains an exact-destination route lookup in the collector's runtime
network namespace. A local route is distinct from end-to-end routability and
from the configured gateway firewall policy. No network probes run here.
"""

from dataclasses import dataclass
from ipaddress import ip_address, ip_network


@dataclass(frozen=True)
class RouteContext:
    """Describe whether the observed route traverses the assessed UniFi gateway."""

    local_route: str = 'unknown'
    gateway_path: bool = False
    source_address: str | None = None
    source_network: str | None = None
    source_zone: str | None = None
    reason: str = 'Route or source context is unavailable.'


def route_context(route: dict, destination: dict, networks: list[dict]) -> RouteContext:
    """Match a kernel route lookup to one site's IPv4 gateway network details.

    Args:
        route: One ``ip -j route get`` result for the exact destination address.
        destination: Destination network detail in the same site.
        networks: Complete network details from that site, with provenance kept
            by the caller. Do not combine unrelated controllers or sites.

    Returns:
        RouteContext: Local route availability and a conservative gateway-path
            prerequisite for firewall evaluation. This does not model NAT,
            policy routing on remote routers, or a remote host firewall.
    """
    if route.get('type') in {'unreachable', 'blackhole', 'prohibit', 'throw'}:
        return RouteContext(local_route='no', reason='The local route explicitly rejects this destination.')
    if not route.get('dev') or route.get('type', 'unicast') != 'unicast':
        return RouteContext()
    source = route.get('prefsrc') or route.get('src')
    try:
        address = ip_address(source)
    except ValueError:
        return RouteContext(local_route='yes', reason='The local route has no usable source address.')
    matches = []
    for network in networks:
        config = network.get('ipv4Configuration') or {}
        try:
            subnet = ip_network(f"{config['hostIpAddress']}/{config['prefixLength']}", strict=False)
        except (KeyError, TypeError, ValueError):
            continue
        if address in subnet:
            matches.append(network)
    if len(matches) != 1:
        return RouteContext(local_route='yes', source_address=str(address), reason='The collector source network is absent or ambiguous.')
    origin = matches[0]
    common = {'local_route': 'yes', 'source_address': str(address),
              'source_network': origin.get('id'), 'source_zone': origin.get('zoneId')}
    if not route.get('gateway'):
        return RouteContext(**common, reason='The local route is direct; the assessed gateway firewall is not on this path.')
    known_path = (origin.get('enabled') is True and destination.get('enabled') is True
                  and origin.get('management') == destination.get('management') == 'GATEWAY'
                  and origin.get('zoneId') is not None and destination.get('zoneId') is not None
                  and route['gateway'] == origin['ipv4Configuration']['hostIpAddress'])
    return RouteContext(**common, gateway_path=known_path,
                        reason='The next hop matches the site gateway and both networks are gateway-managed.' if known_path
                        else 'The next hop or destination does not establish traversal of the assessed gateway.')
