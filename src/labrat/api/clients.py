# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Expose cached client browsing and explicitly permitted bounded TCP checks."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import ipaddress
import threading
import time

from flask import Blueprint, current_app, jsonify
from sqlalchemy import select
from collections import defaultdict
from labrat.api.common import is_virtual_guest, PlacementError, valid_name, write_payload
from labrat.api.topology import topology
from labrat.audit import record_audit_event
from labrat.auth.session import api_login_required, current_user, session_factory
from labrat.coerce import json_object, mac_address
from labrat.collector.scan import allowed_hosts, port_open
from labrat.collector.sources import configured_sources
from labrat.discovery.mac_ownership import EXACT, interface_owners
from labrat.discovery.models import DiscoveredDevice, DiscoveredPort, DiscoverySource
from labrat.discovery_settings import network_rows
from labrat.integrations import read_settings
from labrat.models import Device


clients_bp = Blueprint('clients_api', __name__, url_prefix='/api/v1')
COMMON_PORTS = {
    21: 'FTP', 22: 'SSH', 23: 'Telnet', 25: 'SMTP', 53: 'DNS', 80: 'HTTP',
    110: 'POP3', 111: 'RPC', 135: 'RPC', 139: 'NetBIOS', 143: 'IMAP',
    443: 'HTTPS', 445: 'SMB', 465: 'SMTP TLS', 548: 'AFP', 587: 'SMTP submission',
    631: 'IPP', 853: 'DNS TLS', 993: 'IMAP TLS', 995: 'POP3 TLS',
    1433: 'SQL Server', 1883: 'MQTT', 2049: 'NFS', 3306: 'MySQL',
    3389: 'RDP', 3493: 'NUT', 5000: 'Web service', 5432: 'PostgreSQL',
    5900: 'VNC', 8000: 'Web service', 8080: 'HTTP alternate', 8443: 'HTTPS alternate',
    8883: 'MQTT TLS', 9100: 'Printing',
}
_SCAN_SLOTS = threading.BoundedSemaphore(2)
_SCAN_LOCK = threading.Lock()
_SCAN_TIMES = {}


def _config():
    """Read the current private configuration so permission changes apply immediately.

    Returns:
        dict: The private configuration as read now.
    """
    return read_settings(current_app.extensions['labrat_config_path'])[1]


def scan_permission(client, source, db, config):
    """Return explicit address permission independently of cached firewall opinions.

    Args:
        client: Sanitized cached client metadata.
        source: Derived endpoint source.
        db: Read session for the configured parent source.
        config: Current private configuration.

    Returns:
        tuple[str | None, str]: Numeric permitted address and explanation.
    """
    parent = db.get(DiscoverySource, client.get('parent_source_id'))
    if (source is None or not source.enabled or parent is None or not parent.enabled
            or parent.config_name not in configured_sources(config)['unifi']):
        return None, 'The reporting connection is disabled or unavailable.'
    if source.last_error or parent.last_error:
        return None, 'The latest collection is unavailable. Refresh before scanning.'
    try:
        sample = datetime.fromisoformat(client['sampled_at'])
        age = (datetime.now(timezone.utc) - sample).total_seconds()
        address = ipaddress.ip_address(client['ip_address'])
    except (ValueError, TypeError, KeyError):
        return None, 'A fresh numeric address is required.'
    if client.get('state') != 'connected' or not 0 <= age <= 600:
        return None, 'The client observation is no longer current.'
    if address.is_multicast or address.is_unspecified or address.is_loopback or address.is_link_local:
        return None, 'This address is not eligible for a client port check.'
    if not config.get('discovery', {}).get('network', True):
        return None, 'Network scanning is switched off in Settings.'
    try:
        allowed = allowed_hosts(config.get('scan', {}).get('ranges', []))
    except (ValueError, TypeError):
        return None, 'Scan permission is unavailable. Check Settings.'
    if str(address) not in allowed:
        return None, 'Allow this address or its CIDR in Settings → Discovery first.'
    return str(address), 'Permitted by the saved scan allowlist.'


def client_identity_index(db):
    """Index exact server interface and optional guest MAC ownership for display.

    Returns:
        tuple[dict, dict]: MAC-to-host names and MAC-to-guest candidates. This
            never writes identity links, guesses from IPs, or creates guests.

    Args:
        db: An open database session.
    """
    hosts, guests = defaultdict(set), defaultdict(list)
    servers = {}
    for device in db.scalars(select(DiscoveredDevice).where(DiscoveredDevice.category == 'server')):
        details = json_object(device.details_json)
        if details.get('role') == 'client':
            continue
        source = db.get(DiscoverySource, device.source_id)
        inventory = db.get(Device, device.device_id) if device.device_id else None
        name = inventory.name if inventory else device.name
        servers[device.id] = name
        identifiers = json_object(device.identifiers_json)
        for item in [{'mac': identifiers.get('mac')}, *details.get('host_interfaces', [])]:
            if isinstance(item, dict) and (mac := mac_address(item.get('mac'))):
                hosts[mac].add(name)
        snapshot = details.get('proxmox_clients', {})
        try:
            fresh = 0 <= (datetime.now(timezone.utc) - datetime.fromisoformat(snapshot['sampled_at'])).total_seconds() <= 900
        except (ValueError, TypeError, KeyError):
            fresh = False
        for guest in snapshot.get('guests', []):
            if not isinstance(guest, dict):
                continue
            info = {key: guest.get(key) for key in ('vmid', 'kind', 'name', 'host', 'status')}
            info.update(source_id=device.source_id, source=source.name if source else None,
                        sampled_at=snapshot.get('sampled_at'), fresh=bool(fresh and snapshot.get('available') and source and source.enabled and not source.last_error))
            bridges = guest.get('bridges') if isinstance(guest.get('bridges'), dict) else {}
            for value in guest.get('macs', []):
                if (mac := mac_address(value)):
                    bridge = bridges.get(value)
                    guests[mac].append({**info, 'bridge': bridge if isinstance(bridge, str) else None})
    for port in db.scalars(select(DiscoveredPort)):
        if port.discovered_device_id in servers and (mac := mac_address(port.mac)):
            hosts[mac].add(servers[port.discovered_device_id])
    return hosts, guests


@clients_bp.get('/clients')
@api_login_required
def clients():
    """Return cached endpoint nodes, permission hints, and known subnet provenance.

    Returns:
        Response: JSON with the client nodes, their identity matches, scan
            permission hints, and the known networks they sit in.
    """
    graph = topology().get_json()
    with session_factory()() as db:
        hosts, guests = client_identity_index(db)
        try:
            config = _config()
            networks, _ = network_rows(config, db)
        except (OSError, ValueError):
            config, networks = {}, []
        for node in graph['nodes']:
            for observation in node['observations']:
                client = observation.get('client')
                if not client:
                    continue
                mac = mac_address(client.get('mac_address'))
                candidates = guests.get(mac, [])
                identities = {(g['source_id'], g['vmid']) for g in candidates}
                client['virtualization'] = candidates[0] if len(identities) == 1 else None
                owners, match = (set(), None) if client['virtualization'] else interface_owners(
                    mac, hosts, adapter_block=not client.get('ip_address') and not client.get('hostname'))
                # Derived matches must be unambiguous; an exact MAC on several
                # hosts is still a known server interface for the filter.
                if match != EXACT and len(owners) != 1:
                    owners, match = set(), None
                # The collector also folds addresses of non-server
                # infrastructure (extra switch-port MACs); honor that too.
                if not owners and not client['virtualization'] and client.get('represented_by'):
                    owner = db.get(DiscoveredDevice, client['represented_by'])
                    if owner is not None:
                        inventory = db.get(Device, owner.device_id) if owner.device_id else None
                        owners = {inventory.name if inventory else owner.name}
                        match = client.get('represented_by_match', EXACT)
                client['server_interfaces'] = sorted(owners)
                client['server_interface_match'] = match
                source = db.get(DiscoverySource, observation['source_id'])
                address, reason = scan_permission(client, source, db, config)
                client['scan_allowed'] = bool(address and current_user().is_admin)
                client['scan_reason'] = reason if current_user().is_admin else 'Administrator permission required.'
                client['collection_error'] = bool(source.last_error) if source else True
                client['source_enabled'] = bool(source and source.enabled)
                parent = db.get(DiscoverySource, client.get('parent_source_id'))
                client['source_enabled'] = bool(client['source_enabled'] and parent and parent.enabled
                    and parent.config_name in configured_sources(config)['unifi'])
                client['collection_error'] = bool(client['collection_error'] or parent and parent.last_error)
                client['networks'] = []
                for row in networks:
                    if row['source_id'] != client.get('parent_source_id') or row['site_id'] != client.get('site_id') or not row.get('cidr'):
                        continue
                    try:
                        matches = ipaddress.ip_address(client.get('ip_address')) in ipaddress.ip_network(row['cidr'])
                    except (ValueError, TypeError):
                        matches = False
                    if matches:
                        client['networks'].append({key: row.get(key) for key in ('name', 'cidr', 'fresh', 'observed_at')})
    graph['clients'] = [node for node in graph['nodes'] if any(o.get('client') for o in node['observations'])]
    return jsonify(graph)


@clients_bp.post('/clients/<int:observation_id>/inventory')
@api_login_required
def client_inventory(observation_id):
    """Explicitly add a client observation to inventory without requiring placement.

    Args:
        observation_id: Source-scoped client observation selected in the drawer.

    Returns:
        ResponseReturnValue: The canonical device ID or a safe validation error.
    """
    try:
        payload = write_payload()
        with session_factory().begin() as db:
            observed = db.get(DiscoveredDevice, observation_id)
            if observed is None or json_object(observed.details_json).get('role') != 'client':
                raise PlacementError('Client observation is unavailable', 404)
            if is_virtual_guest(observed):
                raise PlacementError('Virtual machines and containers are not physical inventory', 409)
            # The explicit action applies to the node displayed in the drawer.
            # Its source alias remains evidence, not a second inventory link.
            alias = json_object(observed.details_json).get('client', {}).get('represented_by')
            target = db.get(DiscoveredDevice, alias) if type(alias) is int and observed.device_id is None else None
            if target and json_object(target.details_json).get('role') != 'client':
                observed = target
            if observed.device_id is None:
                device = Device(name=valid_name(payload.get('name', observed.name[:120])), height_u=1)
                db.add(device)
                db.flush()
                observed.device_id = device.id
                actor = current_user()
                record_audit_event(db, actor_kind='user', actor_id=actor.id, actor_name=actor.username,
                                   action='inventory.device.created', subject_kind='device', subject_id=device.id,
                                   details={'discovery_id': observed.id})
            return jsonify(device_id=observed.device_id)
    except PlacementError as exc:
        return jsonify(error=str(exc)), exc.status


@clients_bp.post('/clients/<int:observation_id>/scan')
@api_login_required
def client_scan(observation_id):
    """Check a fixed common TCP port set on one fresh, explicitly permitted client.

    The target is derived from saved evidence, never from request parameters or
    DNS. Results describe this bounded connect-only sample, not a full port or
    service inventory. No UDP, banners, protocol payloads, or automatic scans.

    Args:
        observation_id: Saved client observation from which to derive the target.

    Returns:
        ResponseReturnValue: Ephemeral open-port results, or a safe permission,
            validation, cooldown, or availability error without probing.
    """
    acquired = False
    try:
        payload = write_payload()
        if payload:
            raise PlacementError('Port checks do not accept target or port overrides')
        with session_factory()() as db:
            observed = db.get(DiscoveredDevice, observation_id)
            if observed is None or json_object(observed.details_json).get('role') != 'client':
                raise PlacementError('Client observation is unavailable', 404)
            client = json_object(observed.details_json).get('client', {})
            source = db.get(DiscoverySource, observed.source_id)
            address, reason = scan_permission(client, source, db, _config())
            if not address:
                raise PlacementError(reason, 403)
        key = (str(current_app.extensions['labrat_config_path']), observation_id)
        with _SCAN_LOCK:
            now = time.monotonic()
            if now - _SCAN_TIMES.get(key, -100) < 20:
                raise PlacementError('Wait a moment before checking this device again.', 429)
            acquired = _SCAN_SLOTS.acquire(blocking=False)
            if not acquired:
                raise PlacementError('Other port checks are running. Try again shortly.', 429)
            _SCAN_TIMES[key] = now
            for old in list(_SCAN_TIMES):
                if now - _SCAN_TIMES[old] > 60:
                    del _SCAN_TIMES[old]
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda port: port_open(address, port, timeout=0.3), COMMON_PORTS))
        opened = [{'port': port, 'label': COMMON_PORTS[port]} for port, accepted in zip(COMMON_PORTS, results) if accepted]
        with session_factory().begin() as db:
            actor = current_user()
            record_audit_event(db, actor_kind='user', actor_id=actor.id, actor_name=actor.username,
                               action='discovery.client.port_check', subject_kind='discovered_device', subject_id=observation_id,
                               details={'tested_count': len(COMMON_PORTS), 'open_count': len(opened)})
        return jsonify(open_ports=opened, tested_count=len(COMMON_PORTS), sampled_at=datetime.now(timezone.utc).isoformat(),
                       elapsed_ms=round((time.monotonic()-started)*1000), scope='Common TCP ports; other ports and UDP were not checked. Labels are conventional, not service identification.')
    except PlacementError as exc:
        return jsonify(error=str(exc)), exc.status
    except (OSError, ValueError):
        return jsonify(error='Port check could not run. Check the Discovery settings.'), 503
    finally:
        if acquired:
            _SCAN_SLOTS.release()
