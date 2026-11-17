# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Collect bounded UniFi access evidence independently of device discovery.

Only read-only API requests and local kernel route lookups run here. Subnet
candidates never change scan permission. Persisted evidence contains normalized
selectors and whitelisted network fields, never credentials or raw responses.

Used by: collector.__main__, discovery_settings.
"""

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import subprocess
import tempfile
import os
import time
from ipaddress import ip_network
from urllib.parse import quote, urlencode

import requests
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from labrat.collector.access_policy import Flow, evaluate
from labrat.collector.access_route import route_context
from labrat.collector.http import JsonGetClient, required_string, tls_verification, unifi_network_root
from labrat.collector.sources import configured_sources
from labrat.collector.unifi_access import normalize_policy, subnet_candidates
from labrat.database import database_path
from labrat.discovery.models import DiscoverySource

_LOG = logging.getLogger(__name__)
PROFILES = {'nut': ('TCP', 3493), 'snmp': ('UDP', 161)}
DEFAULT_INTERVAL = 900
MAX_REQUESTS = 96
MAX_ROWS = 2000


def local_route(target: str) -> dict:
    """Read an exact-destination route in this process's namespace, sending no packets.

    Args:
        target: The destination address.

    Returns:
        dict: The kernel's route record (interface, gateway, source), or an
            empty mapping when it cannot be read.
    """
    try:
        result = subprocess.run(['ip', '-j', 'route', 'get', target], capture_output=True,
                                text=True, timeout=2, check=True)
        rows = json.loads(result.stdout)
        return rows[0] if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def _unknown(reason: str) -> dict:
    """Return independent unknown assessments for each supported discovery profile.

    Args:
        reason: Why access is unknown, shown with each assessment.

    Returns:
        dict: An ``unknown`` assessment per discovery profile.
    """
    return {name: {'access': 'unknown', 'reason': reason, 'rule_reference': None} for name in PROFILES}


def _unavailable(snapshot: dict) -> dict:
    """Invalidate old decisions while retaining timestamped candidates and evidence.

    Args:
        snapshot: The evidence snapshot, changed in place.

    Returns:
        dict: The same snapshot, marked unavailable with every decision
            unknown.
    """
    snapshot['available'] = False
    for site in snapshot.get('sites', []):
        for candidate in site.get('candidates', []):
            candidate['profiles'] = _unknown('Access evidence is unavailable or stale.')
    return snapshot


def read_snapshot(path: Path, *, now: datetime | None = None) -> dict:
    """Read private derived evidence, masking expired decisions even without a collector.

    Missing/corrupt files return an unavailable empty snapshot. This reader does
    not grant permission or infer that a retained source is still enabled; its
    future Settings caller must also check the current source/configuration.

    Args:
        path: The private cache file.
        now: The time to judge freshness by; defaults to now.

    Returns:
        dict: The snapshot, with expired decisions masked as unknown.
    """
    now = now or datetime.now(timezone.utc)
    try:
        snapshot = json.loads(path.read_text())
        if snapshot.get('version') != 1 or not isinstance(snapshot.get('sites'), list):
            raise ValueError('Invalid access cache')
        deadline = datetime.fromisoformat(snapshot['expires_at'])
        if deadline.tzinfo is None or now >= deadline or not snapshot['available']:
            return _unavailable(snapshot)
        return snapshot
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {'version': 1, 'available': False, 'sites': []}


def _write_snapshot(path: Path, snapshot: dict) -> None:
    """Atomically replace a private derived cache using an owner-only directory/file.

    Args:
        path: The cache file to replace.
        snapshot: The evidence to store.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor, filename = tempfile.mkstemp(prefix='.access-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(snapshot, handle, ensure_ascii=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(filename, path)
    finally:
        Path(filename).unlink(missing_ok=True)


class AccessCollector:
    """Cache bounded access refreshes separately from device/source health.

    Refresh cadence defaults to fifteen minutes and can be disabled with
    ``access_interval_seconds = 0`` on a UniFi connection. A configuration
    fingerprint invalidates caches after edits without storing settings.
    """

    def __init__(self, client: JsonGetClient | None = None, *, route_lookup=local_route):
        """Prepare injectable HTTP and route readers for deterministic testing.

        Args:
            client: The HTTP reader; a fresh ``JsonGetClient`` by default.
            route_lookup: Reads this host's route to an address (tests replace it).
        """
        self.client = client or JsonGetClient()
        self.route_lookup = route_lookup
        self.failed_attempts = {}

    def close(self) -> None:
        """Release the dedicated HTTP session owned by this collector."""
        self.client.session.close()

    def poll(self, sessions, config: dict, *, now: datetime | None = None) -> int:
        """Refresh enabled configured UniFi sources when their private cache is due.

        API, routing, and cache failures are isolated from normal discovery.
        Disabled/removed sources are skipped; an already-running read may finish.
        No database writes, scan probes, or audit events occur in this path.

        Args:
            sessions: The session factory, to read the registered sources.
            config: The private configuration.
            now: The time to judge due refreshes by; defaults to now.

        Returns:
            int: How many sources were refreshed.
        """
        now = now or datetime.now(timezone.utc)
        try:
            with sessions() as db:
                sources = [(row.id, row.config_name) for row in db.scalars(
                    select(DiscoverySource).where(DiscoverySource.kind == 'unifi', DiscoverySource.enabled.is_(True)))]
        except SQLAlchemyError:
            _LOG.warning('Access evidence source state could not be read')
            return 0
        settings_by_name = configured_sources(config)['unifi']
        refreshed = 0
        for source_id, name in sources:
            settings = settings_by_name.get(name)
            if settings is None:
                continue
            interval = settings.get('access_interval_seconds', DEFAULT_INTERVAL)
            if type(interval) is not int or interval == 0 or not 300 <= interval <= 86400:
                continue
            path = Path(str(database_path(config)) + '.access-evidence') / f'source-{source_id}.json'
            fingerprint = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
            prior = read_snapshot(path, now=now)
            failure_key = (source_id, fingerprint)
            last_failure = self.failed_attempts.get(failure_key)
            if last_failure is not None and (now.timestamp() - last_failure) < interval:
                continue
            if prior.get('fingerprint') == fingerprint and prior['available']:
                continue
            try:
                snapshot = self.collect(settings, source=str(source_id), now=now)
                snapshot.update(fingerprint=fingerprint, expires_at=datetime.fromtimestamp(now.timestamp() + interval, timezone.utc).isoformat())
                _write_snapshot(path, snapshot)
                self.failed_attempts.pop(failure_key, None)
                refreshed += 1
            except (requests.RequestException, OSError, ValueError, TypeError, KeyError):
                self.failed_attempts[failure_key] = now.timestamp()
                _LOG.warning('Access evidence refresh failed; prior decisions are unavailable')
                # Persist invalidation so a web process cannot show old allows as current.
                prior.update(last_attempt_at=now.isoformat())
                try:
                    _write_snapshot(path, _unavailable(prior))
                except OSError:
                    _LOG.warning('Access evidence cache could not be updated')
        return refreshed

    def collect(self, settings: dict, *, source: str, now: datetime) -> dict:
        """Fetch complete source/site evidence, with bounded calls and strict pagination.

        Network collection is required. Policy permission/API failures retain
        subnet candidates with unknown access. No raw response is persisted.

        Args:
            settings: The UniFi connection's private settings.
            source: The source's name, kept on each candidate.
            now: The collection time.

        Returns:
            dict: The new snapshot: each site's networks with their assessed
                access per discovery profile.

        Raises:
            ValueError: When the controller's replies are malformed, paginate
                inconsistently, or exceed the request or size budgets.
        """
        base = unifi_network_root(settings) + '/integration/v1'
        headers = {'X-API-Key': required_string(settings, 'api_key'), 'Accept': 'application/json'}
        verify = tls_verification(settings)
        calls = 0
        started = time.monotonic()

        def get(path):
            """Bound the total request count and refresh duration before each GET."""
            nonlocal calls
            calls += 1
            if calls > MAX_REQUESTS or time.monotonic() - started > 30:
                raise ValueError('Access evidence request budget exceeded')
            payload = self.client.get_json(base + path, headers, verify)
            if not isinstance(payload, dict):
                raise ValueError('Access evidence response must be an object')
            return payload

        def pages(path):
            """Require stable totals, exact offsets/counts, and distinct record IDs."""
            rows, ids, total = [], set(), None
            while True:
                response = get(f'{path}?offset={len(rows)}&limit=100')
                data = response['data']
                current_total = response['totalCount']
                if (not isinstance(data, list) or type(current_total) is not int or not 0 <= current_total <= MAX_ROWS
                        or type(response['offset']) is not int or type(response['count']) is not int
                        or response['offset'] != len(rows) or response['count'] != len(data)
                        or len(data) > 100 or (total is not None and current_total != total)):
                    raise ValueError('Inconsistent access evidence pagination')
                total = current_total
                for row in data:
                    key = row['id']
                    if not isinstance(key, str) or not key or key in ids:
                        raise ValueError('Duplicate or missing access evidence identity')
                    ids.add(key)
                rows.extend(data)
                if len(rows) == total:
                    return rows
                if not data or len(rows) > total:
                    raise ValueError('Incomplete access evidence pagination')

        info = get('/info')
        if not isinstance(info.get('applicationVersion'), str):
            raise ValueError('Application version must be a string')
        sites = pages('/sites')
        if len(sites) > 16:
            raise ValueError('Too many sites for one access refresh')
        result = {'version': 1, 'source': source, 'available': True,
                  'last_attempt_at': now.isoformat(), 'collected_at': now.isoformat(),
                  'application_version': info.get('applicationVersion'), 'sites': []}
        for site in sites:
            prefix = '/sites/' + quote(site['id'], safe='')
            summaries = pages(prefix + '/networks')
            if len(summaries) > 128:
                raise ValueError('Too many network details for one refresh')
            networks = []
            for summary in summaries:
                detail = get(prefix + '/networks/' + quote(summary['id'], safe=''))
                if detail.get('id') != summary['id']:
                    raise ValueError('Network detail identity mismatch')
                if any(key in detail and not isinstance(detail[key], str) for key in ('name', 'zoneId', 'management')):
                    raise ValueError('Network labels and references must be strings')
                if 'enabled' in detail and type(detail['enabled']) is not bool:
                    raise ValueError('Network enabled flag must be boolean')
                network = {key: detail[key] for key in ('id', 'name', 'zoneId', 'enabled', 'management') if key in detail}
                address = detail.get('ipv4Configuration')
                if isinstance(address, dict):
                    if (('hostIpAddress' in address and not isinstance(address['hostIpAddress'], str))
                            or ('prefixLength' in address and type(address['prefixLength']) is not int)):
                        raise ValueError('Network address configuration has invalid types')
                    network['ipv4Configuration'] = {key: address[key] for key in ('hostIpAddress', 'prefixLength') if key in address}
                networks.append(network)
            site_result = {'site_id': site['id'], 'networks': networks, 'rules': [],
                           'policy_complete': False, 'candidates': subnet_candidates(networks, source=source, site=site['id'])}
            try:
                zones = pages(prefix + '/firewall/zones')
                groups = {group['id']: group for group in pages(prefix + '/traffic-matching-lists')}
                policies = pages(prefix + '/firewall/policies')
                rules = [normalize_policy(policy, groups) for policy in policies]
                site_result['rules'] = [asdict(rule) for rule in rules]
                # Cross-check membership; conflicting sources cannot establish a zone.
                membership = {network['id']: [zone['id'] for zone in zones if network['id'] in zone.get('networkIds', [])]
                              for network in networks}
                ordered_pairs = {}
                for candidate in site_result['candidates']:
                    candidate['profiles'] = _unknown('Address or gateway context is unresolved.')
                    candidate['routable'] = 'unknown'
                    if candidate['cidr'] is None:
                        continue
                    subnet = ip_network(candidate['cidr'])
                    target = str(subnet.network_address + min(2, subnet.num_addresses - 1))
                    destination = next(network for network in networks if network['id'] == candidate['network_id'])
                    context = route_context(self.route_lookup(target), destination, networks)
                    candidate.update(route=asdict(context), route_target=target)
                    if not context.gateway_path:
                        continue
                    if (membership.get(context.source_network) != [context.source_zone]
                            or membership.get(candidate['network_id']) != [candidate['zone_id']]):
                        continue
                    pair = (context.source_zone, candidate['zone_id'])
                    if pair not in ordered_pairs:
                        query = urlencode({'sourceFirewallZoneId': pair[0], 'destinationFirewallZoneId': pair[1]})
                        ordering = get(prefix + '/firewall/policies/ordering?' + query)['orderedFirewallPolicyIds']
                        expected = ordering['beforeSystemDefined'] + ordering['afterSystemDefined']
                        applicable = [p for p in policies if p.get('source', {}).get('zoneId') == pair[0] and p.get('destination', {}).get('zoneId') == pair[1]]
                        actual = [p['id'] for p in sorted(applicable, key=lambda p: p['index']) if p.get('metadata', {}).get('origin') == 'USER_DEFINED']
                        ordered_pairs[pair] = expected == actual
                    candidate['profiles'] = {name: asdict(evaluate(rules, Flow(
                        context.source_address, candidate['cidr'], context.source_zone, candidate['zone_id'], protocol, port,
                        source_network=context.source_network, destination_network=candidate['network_id']),
                        complete=ordered_pairs[pair], fresh=True, gateway_path=True)) for name, (protocol, port) in PROFILES.items()}
                # Detect changing policies across a refresh before publishing decisions.
                if policies != pages(prefix + '/firewall/policies'):
                    raise ValueError('Policies changed during access collection')
                site_result['policy_complete'] = True
            except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError):
                for candidate in site_result['candidates']:
                    candidate['profiles'] = _unknown('Firewall evidence is incomplete or unsupported.')
                    candidate['routable'] = 'unknown'
            result['sites'].append(site_result)
        return result
