# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Collect bounded, whitelisted UniFi endpoints separately from infrastructure.

A derived source owns client observations and inferred cables. Controller
identity and source provenance remain distinct from explicit inventory links.
Optional classic telemetry augments the supported connected-client list.
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone
import ipaddress
import math
import logging
import json
import time
from urllib.parse import quote

import requests
from sqlalchemy import or_, select

from labrat.coerce import json_object, mac_address
from labrat.collector.client_links import client_attachment, MAX_AGE_SECONDS
from labrat.collector.contracts import DeviceObservation, DiscoveryBatch, PortObservation
from labrat.collector.http import required_string, tls_verification, unifi_network_root
from labrat.collector.store import apply_batch
from labrat.discovery.mac_ownership import EXACT, interface_owners
from labrat.discovery.models import (
    DiscoveredDevice, DiscoveredLink, DiscoveredPort, DiscoverySource,
)

_LOGGER = logging.getLogger(__name__)

EVIDENCE = 'unifi-client-attachment'
MAX_CLIENTS = 2000
# Explicitly selected controller fields; never store raw client/config responses.
TEXT_FIELDS = {
    'hostname': 'hostname', 'oui': 'manufacturer', 'os_name': 'operating_system',
    'dev_cat': 'device_category', 'dev_vendor': 'device_vendor',
    'dev_family': 'device_family', 'dev_model': 'device_model',
    'essid': 'ssid', 'radio': 'radio', 'radio_proto': 'wifi_standard',
    'network': 'network_name', 'network_id': 'network_id',
}
NUMBER_FIELDS = {
    'signal': 'signal_dbm', 'noise': 'noise_dbm', 'channel': 'channel',
    'rssi': 'rssi', 'satisfaction': 'experience', 'uptime': 'uptime_seconds',
    'rx_bytes': 'received_bytes', 'tx_bytes': 'sent_bytes',
    'rx_rate': 'receive_rate_kbps', 'tx_rate': 'transmit_rate_kbps',
    'rx_bytes-r': 'receive_bytes_per_second', 'tx_bytes-r': 'transmit_bytes_per_second',
    'vlan': 'vlan', 'last_seen': 'controller_last_seen',
}


def text(value, limit=160):
    """Return bounded printable controller text, retaining no structured blobs.

    Args:
        value: A value from the controller.
        limit: Most characters kept.

    Returns:
        str | None: The text cut to ``limit``, or None when it is not text,
            is blank, or holds control characters.
    """
    return value[:limit] if isinstance(value, str) and value.strip() and not any(ord(c) < 32 for c in value) else None


def fingerprint_catalog(adapter, base, headers, verify):
    """Cache optional controller fingerprint labels in memory for one hour.

    Only the matching public labels are copied into client snapshots. There
    are no remote images, cloud lookups, or vendored controller catalogues.

    Args:
        adapter: UniFi adapter holding the optional memory cache and transport.
        base: Configured controller root URL.
        headers: Existing authenticated request headers.
        verify: Existing TLS verification configuration.

    Returns:
        dict: Optional catalogue, or an empty mapping when unsupported.
    """
    previous = getattr(adapter, '_client_fingerprint_cache', None)
    if isinstance(previous, tuple) and previous[0] == base and previous[1] > time.monotonic():
        return previous[2]
    try:
        result = adapter.client.get_json(base + '/v2/api/fingerprint_devices/0', headers, verify)
        if not isinstance(result, dict) or not isinstance(result.get('dev_ids'), dict) or len(result['dev_ids']) > 20000:
            raise ValueError('Invalid fingerprint catalogue')
    except (requests.RequestException, ValueError):
        result = {}
    adapter._client_fingerprint_cache = (base, time.monotonic() + (3600 if result else 300), result)
    return result


def fingerprint_labels(raw, catalogue):
    """Resolve numeric controller fingerprint IDs to bounded descriptive labels.

    Args:
        raw: Fresh classic client metadata, used only in memory.
        catalogue: Optional controller fingerprint dictionaries.

    Returns:
        dict: Matched descriptive labels and bounded identification codes.
    """
    labels = {}
    identifier = raw.get('dev_id_override', raw.get('dev_id'))
    device = catalogue.get('dev_ids', {}).get(str(identifier), {})
    if not isinstance(device, dict):
        device = {}
    if (name := text(device.get('name'))):
        labels['device_model'] = name
    lookups = (
        ('dev_cat', 'dev_type_id', 'dev_type_ids', 'device_category'),
        ('dev_family', 'family_id', 'family_ids', 'device_family'),
        ('dev_vendor', 'vendor_id', 'vendor_ids', 'device_vendor'),
        ('os_name', 'os_name_id', 'os_name_ids', 'operating_system'),
    )
    for field, device_key, table, label in lookups:
        value = raw.get(field + '_override', raw.get(field))
        if value is None:
            value = device.get(device_key)
        if type(value) is int or isinstance(value, str) and value.isdecimal():
            labels[label + '_id'] = str(value)[:20]
            mapping = catalogue.get(table, {})
            if isinstance(mapping, dict) and (name := text(mapping.get(str(value)))):
                labels[label] = name
    if type(identifier) is int:
        labels['fingerprint_id'] = identifier
    return labels


# Fingerprint labels dropped when UniFi's own match is a catch-all.
FINGERPRINT_LABELS = ('device_model', 'device_vendor', 'device_family', 'device_category')
# One fingerprint on devices from this many MAC vendors is a catch-all.
CATCH_ALL_VENDORS = 3


def _vendor_word(value):
    """Return the first significant lowercase word of a vendor name, if any.

    Args:
        value: A vendor or maker name.

    Returns:
        str | None: Its first word of two or more letters or digits, lowercase.
    """
    words = [w for w in ''.join(c if c.isalnum() else ' ' for c in str(value or '')).lower().split() if len(w) > 1]
    return words[0] if words else None


def distrust_catch_all_fingerprints(records):
    """Drop fingerprint labels UniFi applied to unrelated manufacturers' devices.

    The controller assigns some fingerprints broadly (a UPS card to phones,
    single-board computers, and VMs alike) while still reporting full
    confidence. A fingerprint seen on clients whose MAC vendors (OUI) name at
    least ``CATCH_ALL_VENDORS`` different manufacturers is treated as
    unknown for all of them; the bounded ID stays for diagnosis.

    Args:
        records: Sanitized client records from one complete snapshot.
    """
    vendors = defaultdict(set)
    for record in records:
        if record.get('fingerprint_id') is not None and (word := _vendor_word(record.get('manufacturer'))):
            vendors[record['fingerprint_id']].add(word)
    for record in records:
        if len(vendors.get(record.get('fingerprint_id'), ())) >= CATCH_ALL_VENDORS:
            for label in FINGERPRINT_LABELS:
                record.pop(label, None)
                record.pop(label + '_id', None)
            record['fingerprint_ambiguous'] = True


def client_name(record, alias, reported, vendor_names):
    """Choose a client's reported name without UniFi's MAC-octet suffix.

    UniFi names an unaliased client "<hostname or model> <last two MAC
    octets>". Prefer the owner's controller alias, then the hostname, then
    a trusted fingerprint model, the MAC vendor, and finally the MAC. A
    hostname that is just another company's name (a game console announcing
    itself by a phone maker's name) yields to a trusted model from another vendor.

    Args:
        record: Sanitized client record after fingerprint checks.
        alias: The owner's controller alias, if set.
        reported: The integration API's display name, used only when the
            classic record is unavailable.
        vendor_names: Leading lowercase words of the catalogue's vendor names.

    Returns:
        str: Display name for the observation.
    """
    if alias:
        return alias
    hostname, model = record.get('hostname'), record.get('device_model')
    if hostname:
        # A one-word hostname equal to a catalogue vendor's leading word.
        foreign = hostname.strip().lower() in vendor_names and _vendor_word(hostname) != _vendor_word(record.get('device_vendor'))
        return model if foreign and model else hostname
    if model:
        return model
    if reported and not record.get('classic_available'):
        mac = record.get('mac_address') or ''
        suffix = ' ' + mac[-5:] if mac else None
        stripped = reported[:-len(suffix)] if suffix and reported.lower().endswith(suffix.lower()) else reported
        if stripped.strip():
            return stripped.strip()
    return record.get('manufacturer') or record.get('mac_address') or 'Unknown client'


def unifi_product_labels(raw):
    """Label UniFi-made clients from the controller's own product record.

    UniFi devices adopted by another UniFi application (Protect cameras, for
    example) carry ``unifi_device_info_from_ucore`` instead of a fingerprint.
    Only bounded names are kept; icon URLs and firmware are not.

    Args:
        raw: Fresh classic client metadata, used only in memory.

    Returns:
        tuple[dict, str | None]: Labels to merge, and the product's own name
            (set in its UniFi application), used like a controller alias.
    """
    info = raw.get('unifi_device_info_from_ucore')
    if not isinstance(info, dict):
        return {}, None
    labels = {}
    if (line := text(info.get('product_line'), 40)):
        labels['product_line'] = line
    if (model := text(info.get('product_model'))):
        labels['device_model'] = model
    shortname = text(info.get('product_shortname')) or ''
    # "UVC" is UniFi Video Camera: every Protect camera model carries it.
    if shortname.startswith('UVC '):
        labels.update(device_category='Camera', device_family='Camera', device_vendor='Ubiquiti')
    return labels, text(info.get('name'))


def client_snapshot(adapter, settings):
    """Read a complete bounded endpoint snapshot, with optional classic detail.

    Args:
        adapter: Existing UniFi adapter and its authenticated HTTP transport.
        settings: Private connection settings, never returned or persisted.

    Returns:
        list[dict]: Sanitized endpoint records scoped to their controller site.

    Raises:
        ValueError: Incomplete, duplicated, or excessive connected-client data.
        requests.RequestException: Required controller collection failed.
    """
    base = unifi_network_root(settings)
    headers = {'Accept': 'application/json', 'X-API-Key': required_string(settings, 'api_key')}
    verify = tls_verification(settings)
    catalogue = fingerprint_catalog(adapter, base, headers, verify)
    root = base + '/integration/v1'
    sites = adapter._page(root, 'sites', headers, verify, max_records=16)
    if len(sites) > 16:
        raise ValueError('Too many client sites')
    result, identities = [], set()
    now = datetime.now(timezone.utc).timestamp()
    for site in sites:
        site_id = text(site.get('id'))
        if not site_id:
            raise ValueError('Client site has no identity')
        clients = adapter._page(root, f'sites/{quote(site_id, safe="")}/clients', headers, verify, max_records=MAX_CLIENTS - len(result))
        if len(result) + len(clients) > MAX_CLIENTS:
            raise ValueError('Too many clients')
        classic = {}
        classic_failed = False
        reference = text(site.get('internalReference'))
        if reference:
            try:
                payload = adapter.client.get_json(base + '/api/s/' + quote(reference, safe='') + '/stat/sta', headers, verify)
                data = payload.get('data') if isinstance(payload, dict) else None
                meta = payload.get('meta', {}) if isinstance(payload, dict) else {}
                if (isinstance(data, list) and len(data) <= MAX_CLIENTS
                        and isinstance(meta, dict) and meta.get('rc', 'ok') == 'ok'):
                    by_mac = defaultdict(list)
                    for item in data:
                        if isinstance(item, dict) and mac_address(item.get('mac')):
                            by_mac[mac_address(item['mac'])].append(item)
                    classic = {mac: rows[0] for mac, rows in by_mac.items() if len(rows) == 1}
                else:
                    classic_failed = True
            except (requests.RequestException, ValueError):
                classic_failed = True  # Keep prior attachment evidence ageing.
        for item in clients:
            mac = mac_address(item.get('macAddress'))
            remote = text(item.get('id')) or mac
            if not remote:
                continue
            identity = f'{site_id}:client:{remote}'
            if identity in identities:
                raise ValueError('Duplicate client identity')
            identities.add(identity)
            raw = classic.get(mac, {})
            last_seen = raw.get('last_seen')
            fresh = type(last_seen) in (int, float) and 0 <= now - last_seen <= MAX_AGE_SECONDS
            raw = raw if fresh else {}
            try:
                address = str(ipaddress.ip_address(item.get('ipAddress')))
            except (ValueError, TypeError):
                address = None
            kind = item.get('type')
            connection = {'WIRED': 'wired', 'WIRELESS': 'wireless'}.get(kind, 'unknown')
            record = {
                'remote_id': identity, 'site_id': site_id,
                'site_name': text(site.get('name')), 'controller_id': text(item.get('id')),
                # Final name chosen below, after catch-all fingerprint checks.
                '_alias': text(raw.get('name')), '_reported': text(item.get('name')),
                'ip_address': address,
                'mac_address': ':'.join(mac[i:i+2] for i in range(0, 12, 2)) if mac else None,
                'connection': connection, 'state': 'connected',
                'connected_at': text(item.get('connectedAt'), 40),
                'uplink_remote_id': f'{site_id}:{item["uplinkDeviceId"]}' if text(item.get('uplinkDeviceId')) else None,
                'classic_available': bool(raw), 'classic_failed': classic_failed,
            }
            record.update(fingerprint_labels(raw, catalogue))
            product, product_name = unifi_product_labels(raw)
            record.update(product)
            record['_alias'] = record['_alias'] or product_name
            for field, label in TEXT_FIELDS.items():
                if (value := text(raw.get(field))) is not None:
                    record[label] = value
            for field, label in NUMBER_FIELDS.items():
                value = raw.get(field)
                if type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1e18:
                    record[label] = value
            for field in ('is_guest', 'is_blocked', 'is_iot'):
                if type(raw.get(field)) is bool:
                    record[field] = raw[field]
            attachment = client_attachment(raw) if connection == 'wired' and raw.get('is_wired') is True else None
            if attachment:
                record['switch_mac'], record['switch_port'] = attachment
            result.append(record)
    distrust_catch_all_fingerprints(result)
    vendor_names = {_vendor_word(name) for name in catalogue.get('vendor_ids', {}).values()} - {None}
    for record in result:
        record['name'] = client_name(record, record.pop('_alias'), record.pop('_reported'), vendor_names)
    return result


def reconcile_clients(db, source, parent, records, sampled_at=None):
    """Apply one complete client snapshot without moving or renaming inventory.

    Args:
        db: Caller-owned transaction.
        source: Derived endpoint discovery source.
        parent: Owning UniFi infrastructure source.
        records: Complete sanitized connected-client snapshot.
        sampled_at: Optional UTC observation time for deterministic tests.
    """
    now = sampled_at or datetime.now(timezone.utc)
    existing = list(db.scalars(select(DiscoveredDevice).where(DiscoveredDevice.source_id != source.id)))
    ports = list(db.scalars(select(DiscoveredPort)))
    by_mac = defaultdict(set)
    by_id = {d.id: d for d in existing if json_object(d.details_json).get('role') != 'client'}
    for device in existing:
        if json_object(device.details_json).get('role') == 'client':
            continue
        mac = mac_address(json_object(device.identifiers_json).get('mac'))
        if mac:
            by_mac[mac].add(device.id)
        if device.category == 'server':
            for interface in json_object(device.details_json).get('host_interfaces', []):
                if isinstance(interface, dict) and (mac := mac_address(interface.get('mac'))):
                    by_mac[mac].add(device.id)
        wireless = json_object(device.details_json).get('wireless', {})
        if isinstance(wireless, dict) and (mac := mac_address(wireless.get('mac_address'))):
            by_mac[mac].add(device.id)
    for port in ports:
        if port.discovered_device_id in by_id and (mac := mac_address(port.mac)):
            by_mac[mac].add(port.discovered_device_id)
    infrastructure = {d.remote_id: d for d in existing if d.source_id == parent.id}
    switch_by_mac = {
        (json_object(d.details_json).get('site_id'), mac_address(json_object(d.identifiers_json).get('mac'))): d
        for d in infrastructure.values()
    }
    guest_macs = {mac_address(mac) for device in existing
                  for guest in json_object(device.details_json).get('proxmox_clients', {}).get('guests', [])
                  for mac in guest.get('macs', []) if mac_address(mac)}
    previous_clients = {d.remote_id: json_object(d.details_json).get('client', {}) for d in db.scalars(select(DiscoveredDevice).where(DiscoveredDevice.source_id == source.id))}
    devices, observed_ports = [], []
    for record in records:
        client = {**record, 'parent_source_id': parent.id, 'sampled_at': now.isoformat(), 'evidence': EVIDENCE}
        previous = previous_clients.get(client['remote_id'], {})
        if (client.get('classic_failed') and client['connection'] == 'wired'
                and previous.get('connection') == 'wired'
                and previous.get('uplink_remote_id') == client.get('uplink_remote_id')
                and previous.get('mac_address') == client.get('mac_address')):
            for key in ('switch_mac', 'switch_port'):
                if key in previous:
                    client[key] = previous[key]
            client['attachment_sampled_at'] = previous.get('attachment_sampled_at', previous.get('sampled_at'))
        else:
            client['attachment_sampled_at'] = now.isoformat()
        client['virtual_endpoint'] = mac_address(client.get('mac_address')) in guest_macs
        possible, match = set(), None
        if not client['virtual_endpoint']:
            # An address with no IP or hostname of its own may be a sibling
            # function of a known host adapter; see discovery.mac_ownership.
            possible, match = interface_owners(
                mac_address(client.get('mac_address')), by_mac,
                adapter_block=not client.get('ip_address') and not client.get('hostname'))
        # This is display deduplication evidence, never an inventory identity write.
        if len(possible) == 1:
            client['represented_by'] = next(iter(possible))
            if match != EXACT:
                client['represented_by_match'] = match
        details = {'role': 'client', 'client': client, 'status': 'online', 'ports_complete': True}
        if client['connection'] == 'wireless':
            details['wireless'] = {
                'state': 'connected', 'evidence': EVIDENCE,
                'source_id': parent.id, 'source': parent.name,
                'mac_address': client['mac_address'], 'ip_address': client['ip_address'],
                'ap_remote_id': client['uplink_remote_id'],
                'connected_at': client['connected_at'], 'sampled_at': now.isoformat(),
            }
        else:
            details['wireless'] = {'state': 'not_seen', 'sampled_at': now.isoformat()}
        devices.append(DeviceObservation(
            remote_id=client['remote_id'], name=client['name'], category='other_physical',
            identifiers={'mac': client['mac_address'], 'management_ips': [client['ip_address']] if client['ip_address'] else []}, details=details,
        ))
        if client['connection'] == 'wired' and not client.get('represented_by') and not client['virtual_endpoint']:
            observed_ports.append(PortObservation(client['remote_id'], 'wired', 'Wired NIC', mac=client['mac_address'], link_up=True, medium='copper'))
    apply_batch(db, source, DiscoveryBatch(devices=tuple(devices), ports=tuple(observed_ports)), seen_at=now)
    saved = list(db.scalars(select(DiscoveredDevice).where(DiscoveredDevice.source_id == source.id)))
    current = {r['remote_id'] for r in records}
    for device in saved:
        if device.remote_id not in current:
            details = json_object(device.details_json)
            if isinstance(details.get('client'), dict):
                details['client']['state'] = 'not_seen'
                details['status'] = None
                details['wireless'] = {'state': 'not_seen', 'sampled_at': now.isoformat()}
                device.details_json = json.dumps(details)
            for port in db.scalars(select(DiscoveredPort).where(DiscoveredPort.discovered_device_id == device.id)):
                port.link_up = False
    # apply_batch reconciles this derived source's prior links before rebuilding.
    current_clients = [json_object(d.details_json).get('client', {}) for d in saved if d.remote_id in current]
    attachment_counts = Counter(
        (r['site_id'], r.get('switch_mac'), r.get('switch_port'))
        for r in current_clients if r.get('connection') == 'wired' and 'switch_mac' in r
    )
    for device in saved:
        client = json_object(device.details_json).get('client', {})
        if device.remote_id not in current or client.get('represented_by') or client.get('virtual_endpoint') or 'switch_mac' not in client:
            continue
        if attachment_counts[(client['site_id'], client['switch_mac'], client['switch_port'])] != 1:
            continue
        switch = switch_by_mac.get((client['site_id'], client['switch_mac']))
        if switch is None:
            continue
        upstream = db.scalar(select(DiscoveredPort).where(DiscoveredPort.discovered_device_id == switch.id, DiscoveredPort.remote_id == str(client['switch_port'])))
        local = db.scalar(select(DiscoveredPort).where(DiscoveredPort.discovered_device_id == device.id, DiscoveredPort.remote_id == 'wired'))
        if upstream is None or local is None or upstream.link_up is False:
            continue
        if db.scalar(select(DiscoveredLink.id).where(or_(DiscoveredLink.a_port_id == upstream.id, DiscoveredLink.b_port_id == upstream.id))) is not None:
            continue
        a, b = sorted((local.id, upstream.id))
        db.add(DiscoveredLink(source_id=source.id, a_port_id=a, b_port_id=b, evidence=EVIDENCE, confidence='inferred', last_seen_at=datetime.fromisoformat(client['attachment_sampled_at'])))
        local.last_seen_at = datetime.fromisoformat(client['attachment_sampled_at'])
    db.flush()


def poll_clients(sessions, parent, adapter, settings, interval_seconds=None):
    """Refresh the derived client source; optional failures retain old evidence.

    Args:
        sessions: Application session factory; each write uses its own transaction.
        parent: Successfully polled UniFi infrastructure source.
        adapter: Its authenticated adapter and optional fingerprint memory cache.
        settings: Private parent connection configuration.
        interval_seconds: Collector cadence, or None to retain the prior value.
    """
    with sessions.begin() as db:
        source = db.scalar(select(DiscoverySource).where(DiscoverySource.kind == 'unifi_clients', DiscoverySource.config_name == parent.config_name))
        if source is None:
            source = DiscoverySource(kind='unifi_clients', config_name=parent.config_name, name=f'Clients {parent.id} · {parent.name[:100]}')
            db.add(source)
            db.flush()
        source_id = source.id
        source.name = f'Clients {parent.id} · {parent.name[:100]}'
        source.enabled = True
        source.last_attempt_at = datetime.now(timezone.utc)
        if interval_seconds is not None:
            source.poll_interval_seconds = interval_seconds
    try:
        records = client_snapshot(adapter, settings)
        with sessions.begin() as db:
            reconcile_clients(db, db.get(DiscoverySource, source_id), db.get(DiscoverySource, parent.id), records)
    except Exception as exc:
        _LOGGER.warning("Client collection unavailable (%s)", type(exc).__name__)
        with sessions.begin() as db:
            db.get(DiscoverySource, source_id).last_error = 'Client collection unavailable'
