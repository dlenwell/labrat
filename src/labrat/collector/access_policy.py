# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Evaluate normalized first-match firewall policies without network I/O.

Results describe one initial discovery flow or an entire destination subnet.
A partially matching subnet, unsupported condition, incomplete policy set, or
unconfirmed gateway path remains unknown. No result grants scan permission.

Used by: collector.access_collection, collector.unifi_access.
"""

from dataclasses import dataclass
from ipaddress import ip_network


@dataclass(frozen=True)
class Endpoint:
    """Represent conjunctive selectors; None means any for optional selectors."""

    zone: str | None = None
    addresses: tuple[str, ...] | None = None
    ports: tuple[tuple[int, int], ...] | None = None
    networks: tuple[str, ...] | None = None
    invert_addresses: bool = False
    invert_ports: bool = False
    invert_networks: bool = False
    supported: bool = True


@dataclass(frozen=True)
class Rule:
    """Represent a policy with explicit order and conservative support metadata."""

    reference: str
    index: int | None
    source: Endpoint
    destination: Endpoint
    action: str = 'unknown'
    enabled: bool | None = True
    versions: tuple[int, ...] = (4, 6)
    protocols: tuple[str, ...] | None = None
    invert_protocols: bool = False
    states: tuple[str, ...] | None = None
    supported: bool = True


@dataclass(frozen=True)
class Flow:
    """Describe initial traffic from the collector to an address or subnet.

    Ports are exact when known. An unspecified source port is not assumed to
    match a restricted rule. Network IDs and zones are scoped to one site.
    """

    source: str
    destination: str
    source_zone: str
    destination_zone: str
    protocol: str
    destination_port: int
    source_port: int | None = None
    source_network: str | None = None
    destination_network: str | None = None
    state: str = 'NEW'


@dataclass(frozen=True)
class Assessment:
    """Explain a configured policy result, without claiming service reachability."""

    access: str
    reason: str
    rule_reference: str | None = None


def _and(values: list[bool | None]) -> bool | None:
    """Combine three-valued conditions; a known mismatch settles a conjunction.

    Args:
        values: Each condition: True, False, or None when unknown.

    Returns:
        bool | None: False if any is False, else None if any is unknown,
            else True.
    """
    return False if False in values else None if None in values else True


def _invert(value: bool | None, invert: bool) -> bool | None:
    """Negate a known predicate, preserving uncertainty.

    Args:
        value: The predicate: True, False, or None when unknown.
        invert: Whether the selector matches the opposite.

    Returns:
        bool | None: The predicate, negated when asked, still None if unknown.
    """
    return None if value is None else not value if invert else value


def _addresses(target: str, selected: tuple[str, ...] | None) -> bool | None:
    """Match a whole destination interval against a union without enumerating hosts.

    Args:
        target: The destination address or subnet.
        selected: The selector's CIDRs, or None for any address.

    Returns:
        bool | None: True when they cover the whole target, False when they
            miss it entirely, None when they cover only part.
    """
    if selected is None:
        return True
    network = ip_network(target, strict=False)
    intervals = sorted((int(n.network_address), int(n.broadcast_address)) for value in selected
                       if (n := ip_network(value, strict=False)).version == network.version)
    start, stop = int(network.network_address), int(network.broadcast_address)
    cursor = start
    overlaps = False
    for lower, upper in intervals:
        overlaps |= lower <= stop and upper >= start
        if lower <= cursor <= upper:
            cursor = upper + 1
    return True if cursor > stop else None if overlaps else False


def _endpoint(selector: Endpoint, address: str, port: int | None,
              network: str | None, zone: str) -> bool | None:
    """Match an endpoint while preserving unsupported or partial conditions.

    Args:
        selector: The policy's source or destination.
        address: The address being checked.
        port: The destination port, or None when unknown.
        network: The address's network ID, or None when unknown.
        zone: The address's firewall zone ID.

    Returns:
        bool | None: Whether the endpoint matches, or None when it cannot be
            decided.
    """
    port_match = True if selector.ports is None else None if port is None else any(a <= port <= b for a, b in selector.ports)
    network_match = True if selector.networks is None else None if network is None else network in selector.networks
    return _and([
        selector.zone == zone if selector.zone else None,
        _invert(_addresses(address, selector.addresses), selector.invert_addresses),
        _invert(port_match, selector.invert_ports),
        _invert(network_match, selector.invert_networks),
        True if selector.supported else None,
    ])


def evaluate(rules: list[Rule], flow: Flow, *, complete: bool,
             fresh: bool, gateway_path: bool) -> Assessment:
    """Evaluate initial traffic in ascending policy order with explicit evidence gates.

    Args:
        rules: Normalized policies for one site, including system/derived rules.
        flow: Source, destination scope, protocol, and initial connection state.
        complete: All policy pages and required dependencies were retrieved.
        fresh: Evidence is current; stale snapshots cannot establish current access.
        gateway_path: The caller established traversal of the assessed gateway.

    Returns:
        Assessment: Allowed, blocked, or unknown, with a safe reason and rule ID.
            No matching rule yields unknown; no implicit default is invented.
    """
    if not complete or not fresh or not gateway_path:
        return Assessment('unknown', 'Policy evidence is incomplete, stale, or the gateway path is unconfirmed.')
    source, destination = ip_network(flow.source, strict=False), ip_network(flow.destination, strict=False)
    if (source.num_addresses != 1 or source.version != destination.version
            or not flow.source_zone or not flow.destination_zone
            or flow.protocol not in {'TCP', 'UDP', 'ICMP', 'ICMPV6'}
            or type(flow.destination_port) is not int or not 1 <= flow.destination_port <= 65535
            or (flow.source_port is not None and (type(flow.source_port) is not int or not 1 <= flow.source_port <= 65535))):
        return Assessment('unknown', 'The source address, address family, or zone context is unresolved.')
    candidates = []
    for rule in rules:
        if rule.enabled is False:
            continue
        match = _and([
            _endpoint(rule.source, flow.source, flow.source_port, flow.source_network, flow.source_zone),
            _endpoint(rule.destination, flow.destination, flow.destination_port, flow.destination_network, flow.destination_zone),
            source.version in rule.versions,
            _invert(True if rule.protocols is None else flow.protocol in rule.protocols, rule.invert_protocols),
            True if rule.states is None else flow.state in rule.states,
            True if rule.supported and rule.enabled is True else None,
        ])
        if match is not False:
            candidates.append((rule, match))
    indexes = [rule.index for rule, _match in candidates]
    if any(type(index) is not int or index < 0 for index in indexes) or len(indexes) != len(set(indexes)):
        return Assessment('unknown', 'Applicable policy order is missing or ambiguous.')
    for rule, match in sorted(candidates, key=lambda item: item[0].index):
        if match is None:
            return Assessment('unknown', 'An earlier policy has unsupported, unresolved, or partially matching conditions.', rule.reference)
        if rule.action not in {'allowed', 'blocked'}:
            return Assessment('unknown', 'The matching policy action is unsupported.', rule.reference)
        return Assessment(rule.action, 'The first matching configured gateway policy determines initial traffic.', rule.reference)
    return Assessment('unknown', 'No explicit matching policy or default was established.')
