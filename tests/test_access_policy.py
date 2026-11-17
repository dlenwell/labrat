# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Check conservative firewall decisions using synthetic, source-scoped evidence."""

from dataclasses import replace
import unittest

from labrat.collector.access_policy import Endpoint, Flow, Rule, evaluate
from labrat.collector.unifi_access import normalize_policy, subnet_candidates


def policy(**changes):
    """Build a synthetic UniFi allow policy with explicit zone and IP scope.

    Args:
        **changes: Fields to override.

    Returns:
        dict: The policy.
    """
    value = {'id': 'policy-a', 'enabled': True, 'index': 10,
             'source': {'zoneId': 'zone-a'}, 'destination': {'zoneId': 'zone-b'},
             'ipProtocolScope': {'ipVersion': 'IPV4_AND_IPV6'}, 'action': {'type': 'ALLOW'}}
    value.update(changes)
    return value


class AccessPolicyTests(unittest.TestCase):
    """Exercise rule order, uncertainty, partial coverage, and evidence prerequisites."""

    def setUp(self):
        """Use documentation addresses and an initial NUT connection."""
        self.flow = Flow('192.0.2.10', '198.51.100.0/24', 'zone-a', 'zone-b', 'TCP', 3493,
                         source_network='net-a', destination_network='net-b')
        self.allow = normalize_policy(policy())
        self.block = replace(self.allow, reference='policy-b', index=20, action='blocked')

    def check(self, rules, access, *, flow=None, **flags):
        """Assert a decision with explicitly complete, fresh, gateway-scoped evidence.

        Args:
            rules: The normalized policies.
            access: The expected decision.
            flow: The traffic to judge; a default NUT flow when None.
            **flags: Evidence flags to override (complete, fresh, gateway_path).

        Returns:
            The decision.
        """
        options = {'complete': True, 'fresh': True, 'gateway_path': True, **flags}
        result = evaluate(rules, flow or self.flow, **options)
        self.assertEqual(result.access, access, result)
        return result

    def test_first_matching_rule_and_explicit_default(self):
        """Input order cannot override indexes or a decisive earlier rule."""
        self.assertEqual(self.check([self.block, self.allow], 'allowed').rule_reference, 'policy-a')
        self.check([replace(self.block, index=1), self.allow], 'blocked')
        self.check([], 'unknown')
        self.check([replace(self.allow, enabled=False), self.block], 'blocked')

    def test_missing_evidence_and_context(self):
        """An allow is unavailable without completeness, freshness, and actual path."""
        for flag in ['complete', 'fresh', 'gateway_path']:
            self.check([self.allow], 'unknown', **{flag: False})
        for flow in [replace(self.flow, source='192.0.2.0/24'),
                     replace(self.flow, destination='2001:db8::/64'),
                     replace(self.flow, source_zone='')]:
            self.check([self.allow], 'unknown', flow=flow)

    def test_missing_duplicate_indexes_and_disabled_unknown_rules(self):
        """Unknown order blocks conclusions only for potentially applicable rules."""
        for index in [None, True, '10', 10]:
            self.check([self.allow, replace(self.block, index=index)], 'unknown')
        self.check([self.allow, replace(self.block, index=None, enabled=False)], 'allowed')
        self.check([replace(self.allow, enabled=None), self.block], 'unknown')
        self.check([replace(self.allow, source=Endpoint('other'), index=None), self.block], 'blocked')

    def test_unsupported_earlier_condition_and_action(self):
        """Do not skip a possibly matching unknown rule to reach a later allow."""
        self.check([replace(self.allow, supported=False), self.block], 'unknown')
        self.check([replace(self.allow, action='unknown'), self.block], 'unknown')
        self.check([self.allow, replace(self.block, supported=False)], 'allowed')
        self.check([replace(self.allow, source=Endpoint(None, supported=False)), self.block], 'unknown')

    def test_address_union_partial_negated_and_nonmatching(self):
        """Whole-subnet conclusions require full coverage of its address interval."""
        for addresses, access in [
            (('198.51.100.0/25', '198.51.100.128/25'), 'allowed'),
            (('198.51.100.0/25',), 'unknown'),
            (('203.0.113.0/24',), 'blocked'),
            (('2001:db8::/64',), 'blocked'),
            ((), 'blocked'),
        ]:
            rule = replace(self.allow, destination=Endpoint('zone-b', addresses=addresses))
            self.check([rule, self.block], access)
        negated = replace(self.allow, destination=Endpoint('zone-b', addresses=('198.51.100.0/25',), invert_addresses=True))
        self.check([negated, self.block], 'unknown')
        self.check([negated, self.block], 'blocked', flow=replace(self.flow, destination='198.51.100.10'))
        self.check([negated, self.block], 'allowed', flow=replace(self.flow, destination='198.51.100.200'))

    def test_network_ids_and_port_selectors(self):
        """Unknown source ports and missing network IDs remain conservative."""
        for selector, access in [
            (Endpoint('zone-a', ports=((1, 1024),)), 'unknown'),
            (Endpoint('zone-a', networks=('net-a',)), 'allowed'),
            (Endpoint('zone-a', networks=('net-a',), invert_networks=True), 'blocked'),
        ]:
            self.check([replace(self.allow, source=selector), self.block], access)
        self.check([replace(self.allow, source=Endpoint('zone-a', ports=((1000, 2000),))), self.block],
                   'allowed', flow=replace(self.flow, source_port=1500))
        self.check([replace(self.allow, source=Endpoint('zone-a', networks=('net-a',)))],
                   'unknown', flow=replace(self.flow, source_network=None))
        self.check([replace(self.allow, destination=Endpoint('zone-b', ports=((3493, 3493),), invert_ports=True)), self.block], 'blocked')

    def test_protocol_state_and_address_family(self):
        """Evaluate initial TCP separately from UDP, IPv6, and return traffic."""
        for rule in [replace(self.allow, protocols=('UDP',)), replace(self.allow, states=('ESTABLISHED', 'RELATED')),
                     replace(self.allow, versions=(6,)), replace(self.allow, protocols=('TCP',), invert_protocols=True)]:
            self.check([rule, self.block], 'blocked')
        self.check([replace(self.allow, versions=(6,))], 'allowed',
                   flow=replace(self.flow, source='2001:db8::1', destination='2001:db8:1::/64'))


class UniFiAccessNormalizationTests(unittest.TestCase):
    """Check normalization from documented API shapes without real topology data."""

    def test_subnet_candidates_preserve_provenance_without_permission(self):
        """CIDRs come from details, and missing address data stays explicit."""
        rows = subnet_candidates([
            {'id': 'n', 'name': 'Example', 'zoneId': 'z', 'enabled': True,
             'ipv4Configuration': {'hostIpAddress': '192.0.2.1', 'prefixLength': 24}},
            {'id': 'unmanaged'}, {'id': 'bad', 'ipv4Configuration': {'hostIpAddress': 'bad', 'prefixLength': 24}},
            {'id': 'v6', 'ipv4Configuration': {'hostIpAddress': '2001:db8::1', 'prefixLength': 64}},
            {'id': 'bool', 'ipv4Configuration': {'hostIpAddress': '192.0.2.1', 'prefixLength': True}},
        ], source='source-a', site='site-a')
        self.assertEqual(rows[0]['cidr'], '192.0.2.0/24')
        self.assertEqual(rows[0]['source'], 'source-a')
        self.assertEqual(rows[0]['site'], 'site-a')
        self.assertTrue(all(row['cidr'] is None for row in rows[1:]))
        self.assertTrue(all('scan_enabled' not in row for row in rows))

    def test_literal_and_group_address_selectors(self):
        """Expand known typed groups and inclusive ranges without vendor decisions."""
        items = [{'type': 'IP_ADDRESS', 'value': '192.0.2.5'},
                 {'type': 'SUBNET', 'value': '198.51.100.1/24'},
                 {'type': 'IP_ADDRESS_RANGE', 'start': '203.0.113.0', 'stop': '203.0.113.3'}]
        for field, groups in [({'type': 'IP_ADDRESSES', 'items': items}, {}),
                              ({'type': 'TRAFFIC_MATCHING_LIST', 'trafficMatchingListId': 'g'}, {'g': {'type': 'IPV4_ADDRESSES', 'items': items}})]:
            value = policy(source={'zoneId': 'zone-a', 'trafficFilter': {'type': 'IP_ADDRESS', 'ipAddressFilter': {**field, 'matchOpposite': False}}})
            endpoint = normalize_policy(value, groups).source
            self.assertTrue(endpoint.supported)
            self.assertEqual(endpoint.addresses, ('192.0.2.5/32', '198.51.100.0/24', '203.0.113.0/30'))

    def test_ports_networks_and_group_resolution(self):
        """Support exact/ranged ports and explicit network IDs, including negation."""
        ports = [{'type': 'PORT_NUMBER', 'value': 443}, {'type': 'PORT_NUMBER_RANGE', 'start': 3000, 'stop': 4000}]
        for field, groups in [({'type': 'PORTS', 'items': ports}, {}),
                              ({'type': 'TRAFFIC_MATCHING_LIST', 'trafficMatchingListId': 'g'}, {'g': {'type': 'PORTS', 'items': ports}})]:
            endpoint = normalize_policy(policy(destination={'zoneId': 'zone-b', 'trafficFilter': {
                'type': 'PORT', 'portFilter': {**field, 'matchOpposite': True}}}), groups).destination
            self.assertEqual(endpoint.ports, ((443, 443), (3000, 4000)))
            self.assertTrue(endpoint.invert_ports)
        endpoint = normalize_policy(policy(source={'zoneId': 'zone-a', 'trafficFilter': {
            'type': 'NETWORK', 'networkFilter': {'networkIds': ['n'], 'matchOpposite': True}}})).source
        self.assertEqual(endpoint.networks, ('n',))
        self.assertTrue(endpoint.invert_networks)

    def test_unsupported_or_malformed_selectors_are_unknown(self):
        """Missing groups, unknown fields, and bad types cannot become an allow."""
        filters = [
            {'type': 'MAC_ADDRESS', 'macAddressFilter': {'macAddresses': ['02:00:00:00:00:01']}},
            {'type': 'IP_ADDRESS'},
            {'type': 'IP_ADDRESS', 'ipAddressFilter': {'type': 'TRAFFIC_MATCHING_LIST', 'trafficMatchingListId': 'missing', 'matchOpposite': False}},
            {'type': 'IP_ADDRESS', 'ipAddressFilter': {'type': 'UNKNOWN', 'items': [], 'matchOpposite': False}},
            {'type': 'IP_ADDRESS', 'ipAddressFilter': {'type': 'IP_ADDRESSES', 'items': [{'type': 'UNKNOWN'}], 'matchOpposite': False}},
            {'type': 'IP_ADDRESS', 'ipAddressFilter': {'type': 'IP_ADDRESSES', 'items': [], 'matchOpposite': 'false'}},
            {'type': 'IP_ADDRESS', 'ipAddressFilter': {'type': 'IP_ADDRESSES', 'items': [], 'matchOpposite': False, 'future': True}},
            {'type': 'PORT', 'portFilter': {'type': 'PORTS', 'items': [{'type': 'UNKNOWN'}], 'matchOpposite': False}},
            {'type': 'PORT', 'portFilter': {'type': 'PORTS', 'items': [{'type': 'PORT_NUMBER', 'value': True}], 'matchOpposite': False}},
            {'type': 'PORT', 'portFilter': {'type': 'PORTS', 'items': [], 'matchOpposite': False, 'future': True}},
            {'type': 'NETWORK', 'networkFilter': {'networkIds': [], 'matchOpposite': False}},
            {'type': 'NETWORK', 'networkFilter': {'networkIds': ['n'], 'matchOpposite': False, 'future': True}},
            {'type': 'PORT', 'future': True},
        ]
        for traffic in filters:
            with self.subTest(traffic=traffic):
                endpoint = normalize_policy(policy(source={'zoneId': 'zone-a', 'trafficFilter': traffic})).source
                self.assertFalse(endpoint.supported)
                self.assertEqual(endpoint.zone, 'zone-a')
        self.assertFalse(normalize_policy(policy(source={'zoneId': 'zone-a', 'future': True})).source.supported)
        field = {'type': 'TRAFFIC_MATCHING_LIST', 'trafficMatchingListId': 'g', 'matchOpposite': False}
        value = policy(source={'zoneId': 'zone-a', 'trafficFilter': {'type': 'IP_ADDRESS', 'ipAddressFilter': field}})
        self.assertFalse(normalize_policy(value, {'g': {'type': 'PORTS', 'items': []}}).source.supported)

    def test_protocol_normalization_and_unknown_conditions(self):
        """Unknown schedules/scopes stay unsupported; known protocols stay exact."""
        named = {'type': 'NAMED_PROTOCOL', 'protocol': {'name': 'TCP'}, 'matchOpposite': True}
        rule = normalize_policy(policy(ipProtocolScope={'ipVersion': 'IPV4', 'protocolFilter': named}))
        self.assertTrue(rule.supported)
        self.assertEqual(rule.protocols, ('TCP',))
        self.assertTrue(rule.invert_protocols)
        rule = normalize_policy(policy(ipProtocolScope={'ipVersion': 'IPV4_AND_IPV6', 'protocolFilter': {'type': 'PRESET', 'preset': {'name': 'TCP_UDP'}}}))
        self.assertEqual(rule.protocols, ('TCP', 'UDP'))
        for changes in [
            {'schedule': {'mode': 'ALWAYS'}}, {'ipProtocolScope': {}},
            {'ipProtocolScope': {'ipVersion': 'IPV4', 'future': True}},
            {'ipProtocolScope': {'ipVersion': 'IPV4', 'protocolFilter': {'type': 'OTHER'}}},
            {'ipProtocolScope': {'ipVersion': 'IPV4', 'protocolFilter': {**named, 'protocol': {'name': 'OTHER'}}}},
            {'connectionStateFilter': ['FUTURE']}, {'connectionStateFilter': []},
        ]:
            self.assertFalse(normalize_policy(policy(**changes)).supported)
        self.assertEqual(normalize_policy(policy(action={'type': 'OTHER'})).action, 'unknown')
        self.assertEqual(normalize_policy(policy(action={'type': 'ALLOW', 'future': True})).action, 'unknown')
        self.assertEqual(normalize_policy(policy(action='ALLOW')).action, 'unknown')
        self.assertEqual(normalize_policy(policy(connectionStateFilter=['NEW'])).states, ('NEW',))

class RouteContextTests(unittest.TestCase):
    """Keep local route evidence separate from the gateway policy path."""

    def setUp(self):
        """Create two synthetic gateway-managed networks in one site."""
        self.origin = {'id': 'n-a', 'zoneId': 'z-a', 'management': 'GATEWAY', 'enabled': True,
                       'ipv4Configuration': {'hostIpAddress': '192.0.2.1', 'prefixLength': 24}}
        self.dest = {'id': 'n-b', 'zoneId': 'z-b', 'management': 'GATEWAY', 'enabled': True,
                     'ipv4Configuration': {'hostIpAddress': '198.51.100.1', 'prefixLength': 24}}
        self.route = {'dev': 'example0', 'prefsrc': '192.0.2.10', 'gateway': '192.0.2.1'}

    def test_gateway_path_direct_and_alternative_next_hop(self):
        """Recognize a known gateway, but never infer it from a default route alone."""
        from labrat.collector.access_route import route_context
        networks = [self.origin, self.dest]
        result = route_context(self.route, self.dest, networks)
        self.assertTrue(result.gateway_path)
        self.assertEqual(result.source_network, 'n-a')
        self.assertEqual(result.local_route, 'yes')
        for route in [{k: v for k, v in self.route.items() if k != 'gateway'},
                      {**self.route, 'gateway': '192.0.2.2'}]:
            self.assertFalse(route_context(route, self.dest, networks).gateway_path)
        self.assertFalse(route_context(self.route, {**self.dest, 'management': 'UNMANAGED'}, networks).gateway_path)
        self.assertFalse(route_context(self.route, {**self.dest, 'enabled': False}, networks).gateway_path)

    def test_missing_rejected_and_ambiguous_context(self):
        """Absent or overlapping source mappings cannot establish a gateway path."""
        from labrat.collector.access_route import route_context
        self.assertEqual(route_context({}, self.dest, []).local_route, 'unknown')
        self.assertEqual(route_context({'type': 'local', 'dev': 'lo'}, self.dest, []).local_route, 'unknown')
        for kind in ['unreachable', 'prohibit', 'blackhole', 'throw']:
            self.assertEqual(route_context({'type': kind}, self.dest, []).local_route, 'no')
        self.assertEqual(route_context({'dev': 'example0'}, self.dest, []).local_route, 'yes')
        for networks in [[], [self.origin, dict(self.origin)], [self.dest, {'id': 'invalid'}]]:
            self.assertFalse(route_context(self.route, self.dest, networks).gateway_path)
        self.assertTrue(route_context({'dev': 'example0', 'src': '192.0.2.10', 'gateway': '192.0.2.1'}, self.dest, [self.origin]).gateway_path)


class MalformedEvidenceTests(unittest.TestCase):
    """Future or malformed API fields must not silently broaden a rule."""

    def test_invalid_records_and_unknown_item_fields(self):
        """Reject invalid record boundaries and retain unknown selectors."""
        for value in [None, [], 'invalid']:
            with self.assertRaises(ValueError):
                normalize_policy(value)
            with self.assertRaises(ValueError):
                subnet_candidates([value], source='s', site='t')
        self.assertFalse(normalize_policy(policy(source='invalid')).source.supported)
        for traffic in [
            {'type': 'IP_ADDRESS', 'ipAddressFilter': []},
            {'type': 'IP_ADDRESS', 'ipAddressFilter': {'type': 'IP_ADDRESSES', 'items': 'invalid', 'matchOpposite': False}},
            {'type': 'IP_ADDRESS', 'ipAddressFilter': {'type': 'IP_ADDRESSES', 'items': [{'type': 'IP_ADDRESS', 'value': '192.0.2.1', 'future': True}], 'matchOpposite': False}},
            {'type': 'PORT', 'portFilter': {'type': 'PORTS', 'items': [{'type': 'PORT_NUMBER', 'value': 443, 'future': True}], 'matchOpposite': False}},
            {'type': 'NETWORK', 'networkFilter': {'networkIds': 'not-a-list', 'matchOpposite': False}},
        ]:
            self.assertFalse(normalize_policy(policy(source={'zoneId': 'z', 'trafficFilter': traffic})).source.supported)
        for scope in [[], {}, {'ipVersion': 'IPV4', 'protocolFilter': []}, {'ipVersion': 'IPV4', 'protocolFilter': {}}]:
            self.assertFalse(normalize_policy(policy(ipProtocolScope=scope)).supported)
        self.assertFalse(normalize_policy(policy(connectionStateFilter=[{}])).supported)
        self.assertEqual(normalize_policy(policy(action={'type': []})).action, 'unknown')

    def test_invalid_flow_and_negative_order(self):
        """Bad flow inputs and policy indexes cannot produce an allow."""
        flow = Flow('192.0.2.10', '198.51.100.10', 'zone-a', 'zone-b', 'TCP', 3493)
        for candidate in [replace(flow, protocol='FUTURE'), replace(flow, destination_port=True),
                          replace(flow, destination_port=0), replace(flow, source_port=70000)]:
            self.assertEqual(evaluate([normalize_policy(policy())], candidate,
                                     complete=True, fresh=True, gateway_path=True).access, 'unknown')
        self.assertEqual(evaluate([normalize_policy(policy(index=-1))], flow,
                                 complete=True, fresh=True, gateway_path=True).access, 'unknown')


if __name__ == '__main__':
    unittest.main()
