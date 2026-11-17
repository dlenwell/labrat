# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Normalize UniFi access evidence for the pure firewall evaluator.

Only documented selectors are interpreted. Unknown fields or missing group
references remain unsupported, rather than being discarded to produce an allow.
These helpers have no credentials, HTTP requests, database writes, or scan I/O.
"""

from ipaddress import ip_address, ip_network, summarize_address_range

from labrat.collector.access_policy import Endpoint, Rule


def subnet_candidates(details: list[dict], *, source: str, site: str) -> list[dict]:
    """Extract source-scoped IPv4 subnet observations without granting scan permission.

    Network list records do not contain CIDRs; pass network detail responses.
    Third-party networks without address configuration remain visible with no
    inferred CIDR. The user-owned allowlist is deliberately absent from results.

    Args:
        details: Network detail responses from the integration API.
        source: The discovery source's name, kept on each candidate.
        site: The controller site's ID.

    Returns:
        list[dict]: One candidate per network: its ID, name, and CIDR when
            the network has an IPv4 configuration.

    Raises:
        ValueError: When a detail is not an object.
    """
    result = []
    for detail in details:
        if not isinstance(detail, dict):
            raise ValueError('Network detail must be an object')
        cidr = None
        config = detail.get('ipv4Configuration', {})
        try:
            address, prefix = config['hostIpAddress'], config['prefixLength']
            if type(prefix) is int and ip_address(address).version == 4:
                cidr = str(ip_network(f'{address}/{prefix}', strict=False))
        except (KeyError, TypeError, ValueError):
            pass
        result.append({'source': source, 'site': site, 'network_id': detail.get('id'),
                       'name': detail.get('name'), 'zone_id': detail.get('zoneId'),
                       'cidr': cidr, 'enabled': detail.get('enabled'),
                       'management': detail.get('management')})
    return result


def _items(value: dict, lists: dict, expected: str) -> list:
    """Resolve inline values or a typed traffic-matching list, refusing missing data.

    Args:
        value: A policy's address or port selector.
        lists: Traffic-matching lists keyed by ID.
        expected: The list type the selector must use (addresses or ports).

    Returns:
        list: The selector's items.

    Raises:
        ValueError: When the selector is malformed, of another type, or names
            a list of the wrong kind.
        KeyError: When it names a list that is missing.
    """
    if not isinstance(value, dict):
        raise ValueError('Selector must be an object')
    if value.get('type') == 'TRAFFIC_MATCHING_LIST':
        group = lists[value['trafficMatchingListId']]
        valid = {'IPV4_ADDRESSES', 'IPV6_ADDRESSES'} if expected == 'IP_ADDRESSES' else {'PORTS'}
        if group['type'] not in valid:
            raise ValueError('Mismatched traffic list type')
        items = group['items']
    else:
        if value.get('type') != expected:
            raise ValueError('Unsupported selector type')
        items = value['items']
    if not isinstance(items, list):
        raise ValueError('Selector items must be a list')
    return items


def _address_values(items: list) -> tuple[str, ...]:
    """Normalize literal addresses, CIDRs, and inclusive ranges into CIDRs.

    Args:
        items: Address items: single addresses, subnets, or ranges.

    Returns:
        tuple[str, ...]: Every item as one or more CIDRs.

    Raises:
        ValueError: When an item is malformed or not an address.
    """
    result = []
    for item in items:
        kind = item['type']
        keys = {'type', 'start', 'stop'} if kind == 'IP_ADDRESS_RANGE' else {'type', 'value'}
        if set(item) != keys:
            raise ValueError('Unsupported address item fields')
        if kind == 'IP_ADDRESS':
            result.append(str(ip_network(ip_address(item['value']).exploded)))
        elif kind == 'SUBNET':
            result.append(str(ip_network(item['value'], strict=False)))
        elif kind == 'IP_ADDRESS_RANGE':
            result.extend(str(net) for net in summarize_address_range(ip_address(item['start']), ip_address(item['stop'])))
        else:
            raise ValueError('Unsupported address selector')
    return tuple(result)


def _port_values(items: list) -> tuple[tuple[int, int], ...]:
    """Normalize exact ports and inclusive ranges with strict integer validation.

    Args:
        items: Port items: single ports or ranges.

    Returns:
        tuple[tuple[int, int], ...]: Each item as an inclusive range.

    Raises:
        ValueError: When an item is malformed or a port is out of range.
    """
    result = []
    for item in items:
        keys = {'type', 'start', 'stop'} if item['type'] == 'PORT_NUMBER_RANGE' else {'type', 'value'}
        if set(item) != keys:
            raise ValueError('Unsupported port item fields')
        if item['type'] == 'PORT_NUMBER':
            lower = upper = item['value']
        elif item['type'] == 'PORT_NUMBER_RANGE':
            lower, upper = item['start'], item['stop']
        else:
            raise ValueError('Unsupported port selector')
        if type(lower) is not int or type(upper) is not int or not 1 <= lower <= upper <= 65535:
            raise ValueError('Invalid port selector')
        result.append((lower, upper))
    return tuple(result)


def _opposite(value: dict) -> bool:
    """Require explicit boolean inversion semantics for a selector.

    Args:
        value: A policy selector.

    Returns:
        bool: Whether the selector matches everything except its items.

    Raises:
        ValueError: When ``matchOpposite`` is not a boolean.
    """
    if type(value['matchOpposite']) is not bool:
        raise ValueError('Invalid inversion flag')
    return value['matchOpposite']


def _endpoint(value: dict, lists: dict) -> Endpoint:
    """Normalize endpoint selectors, retaining safe zone narrowing on failure.

    Args:
        value: A policy's source or destination selector.
        lists: Traffic-matching lists keyed by ID.

    Returns:
        Endpoint: The zone and normalized addresses and ports, marked
            unsupported (keeping only the zone) when they cannot be read.
    """
    if not isinstance(value, dict):
        return Endpoint(supported=False)
    zone = value.get('zoneId')
    try:
        if not isinstance(zone, str) or set(value) - {'zoneId', 'trafficFilter'}:
            raise ValueError('Unsupported endpoint')
        traffic = value.get('trafficFilter')
        if traffic is None:
            return Endpoint(zone=zone)
        kind = traffic['type']
        fields = {'IP_ADDRESS': {'ipAddressFilter', 'portFilter'},
                  'PORT': {'portFilter'}, 'NETWORK': {'networkFilter', 'portFilter'}}
        if kind not in fields or set(traffic) - (fields[kind] | {'type'}):
            raise ValueError('Unsupported endpoint filter')
        required = {'IP_ADDRESS': 'ipAddressFilter', 'PORT': 'portFilter', 'NETWORK': 'networkFilter'}[kind]
        if required not in traffic:
            raise ValueError('Missing endpoint filter')
        addresses = ports = networks = None
        invert_addresses = invert_ports = invert_networks = False
        if 'ipAddressFilter' in traffic:
            field = traffic['ipAddressFilter']
            if set(field) - {'type', 'matchOpposite', 'items', 'trafficMatchingListId'}:
                raise ValueError('Unsupported address condition')
            addresses = _address_values(_items(field, lists, 'IP_ADDRESSES'))
            invert_addresses = _opposite(field)
        if 'portFilter' in traffic:
            field = traffic['portFilter']
            if set(field) - {'type', 'matchOpposite', 'items', 'trafficMatchingListId'}:
                raise ValueError('Unsupported port condition')
            ports = _port_values(_items(field, lists, 'PORTS'))
            invert_ports = _opposite(field)
        if 'networkFilter' in traffic:
            field = traffic['networkFilter']
            if set(field) - {'networkIds', 'matchOpposite'}:
                raise ValueError('Unsupported network condition')
            if not isinstance(field['networkIds'], list):
                raise ValueError('Network IDs must be a list')
            networks = tuple(field['networkIds'])
            if not networks or not all(isinstance(key, str) for key in networks):
                raise ValueError('Invalid network IDs')
            invert_networks = _opposite(field)
        return Endpoint(zone, addresses, ports, networks, invert_addresses, invert_ports, invert_networks)
    except (KeyError, TypeError, ValueError):
        return Endpoint(zone=zone if isinstance(zone, str) else None, supported=False)


def normalize_policy(value: dict, lists: dict | None = None) -> Rule:
    """Convert a documented UniFi policy; unsupported constraints stay unknown.

    Args:
        value: One policy response object from a complete paginated snapshot.
        lists: Traffic-matching list objects keyed by their site-scoped IDs.

    Returns:
        Rule: Order, selectors, action, and whether every condition is supported.

    Raises:
        ValueError: When the policy is not an object, or uses an IP scope or
            protocol filter LabRat cannot read.
    """
    if not isinstance(value, dict):
        raise ValueError('Policy must be an object')
    lists = lists or {}
    source = _endpoint(value.get('source') or {}, lists)
    destination = _endpoint(value.get('destination') or {}, lists)
    supported = not bool(set(value) - {'id', 'name', 'description', 'index', 'enabled', 'action',
                                      'source', 'destination', 'ipProtocolScope', 'connectionStateFilter',
                                      'loggingEnabled', 'metadata', 'reportFlowEnabled'})
    versions, protocols, inverted = (), None, False
    try:
        scope = value['ipProtocolScope']
        if set(scope) - {'ipVersion', 'protocolFilter'}:
            raise ValueError('Unsupported IP scope')
        versions = {'IPV4': (4,), 'IPV6': (6,), 'IPV4_AND_IPV6': (4, 6)}[scope['ipVersion']]
        protocol = scope.get('protocolFilter')
        if protocol is not None:
            if not isinstance(protocol, dict) or not protocol:
                raise ValueError('Invalid protocol filter')
            if protocol['type'] == 'NAMED_PROTOCOL' and not set(protocol) - {'type', 'protocol', 'matchOpposite'}:
                name = protocol['protocol']['name']
                if name not in {'TCP', 'UDP', 'ICMP', 'ICMPV6'} or set(protocol['protocol']) != {'name'}:
                    raise ValueError('Unsupported protocol')
                protocols = (name,)
                inverted = _opposite(protocol)
            elif protocol == {'type': 'PRESET', 'preset': {'name': 'TCP_UDP'}}:
                protocols = ('TCP', 'UDP')
            else:
                raise ValueError('Unsupported protocol filter')
    except (KeyError, TypeError, ValueError):
        supported = False
        versions = (4, 6)
    states = value.get('connectionStateFilter')
    if states is not None and (not isinstance(states, list) or not states or not all(isinstance(s, str) and s in {'NEW', 'ESTABLISHED', 'RELATED', 'INVALID'} for s in states)):
        supported = False
        states = None
    action = value.get('action') or {}
    if (not isinstance(action, dict) or set(action) - {'type', 'allowReturnTraffic'}
            or not isinstance(action.get('type'), str)):
        action = {}
    index = value.get('index')
    index = index if type(index) is int and index >= 0 else None
    enabled = value.get('enabled')
    enabled = enabled if type(enabled) is bool else None
    return Rule(str(value.get('id', 'unidentified')), index, source, destination,
                {'ALLOW': 'allowed', 'BLOCK': 'blocked', 'REJECT': 'blocked'}.get(action.get('type'), 'unknown'),
                enabled, versions, protocols, inverted,
                tuple(states) if states else None, supported)
