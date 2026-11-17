# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Read optional Proxmox guest NIC identity without creating physical devices."""

from datetime import datetime, timezone
import json
import re
import time
from urllib.parse import quote

from sqlalchemy import select

from labrat.coerce import json_object
from labrat.collector.http import controller_url, required_string, tls_verification
from labrat.discovery.models import DiscoveredDevice

_BRIDGE = re.compile(r'(?:^|,)bridge=([A-Za-z0-9_.-]{1,32})(?=,|$)')
_MAC = re.compile(r'(?:^|[,=])([0-9a-f]{2}(?::[0-9a-f]{2}){5})(?=,|$)', re.I)
_TAG = re.compile(r'(?:^|,)tag=(\d{1,4})(?=,|$)')
_TAGS = re.compile(r'[A-Za-z0-9_.+-]{1,64}')


def _whole(value):
    """Return a non-negative whole number from a resource field, or None.

    Args:
        value: A field from a Proxmox resource.

    Returns:
        int | None: The value as a whole number, or None when missing, not a
            number, or negative.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value < 0:
        return None
    return int(value)


def _guest_facts(row, payload):
    """Curate sizing and usage facts for the Cluster page from whitelisted fields.

    Args:
        row: The guest's ``/cluster/resources`` row.
        payload: The guest's configuration (only whitelisted keys are read).

    Returns:
        dict: Assigned vCPUs, memory and disk bytes, current CPU fraction and
            memory use, uptime, template and start-at-boot flags, and tags.
    """
    cpu = row.get('cpu')
    tags = row.get('tags') if isinstance(row.get('tags'), str) else payload.get('tags')
    return {
        'vcpus': _whole(row.get('maxcpu')), 'memory': _whole(row.get('maxmem')),
        'disk': _whole(row.get('maxdisk')), 'mem_used': _whole(row.get('mem')),
        'cpu': round(cpu, 4) if isinstance(cpu, (int, float)) and not isinstance(cpu, bool) and cpu >= 0 else None,
        'uptime': _whole(row.get('uptime')),
        'template': row.get('template') in (1, True, '1'),
        'onboot': str(payload.get('onboot')) == '1',
        'tags': sorted(set(_TAGS.findall(tags)))[:16] if isinstance(tags, str) else [],
        'ostype': payload['ostype'] if isinstance(payload.get('ostype'), str) and re.fullmatch(r'[a-z0-9]{1,16}', payload['ostype']) else None,
    }


def guest_snapshot(adapter, settings):
    """Read a bounded guest list and whitelist configured network MACs.

    Args:
        adapter: Existing Proxmox transport; no configuration writes are made.
        settings: Private connection/token settings.

    Returns:
        list[dict]: VM/container identities, hosting nodes, NIC MACs, and
            each MAC's host bridge name when configured.

    Raises:
        ValueError: Incomplete, excessive, or malformed guest metadata.
        requests.RequestException: Optional API permissions or collection failed.
    """
    base = controller_url(settings) + '/api2/json'
    headers = {'Authorization': 'PVEAPIToken=' + required_string(settings, 'token_id') + '=' + required_string(settings, 'token_secret')}
    verify = tls_verification(settings)
    deadline = time.monotonic() + 30
    rows = adapter.client.get_json(base + '/cluster/resources?type=vm', headers, verify).get('data')
    if not isinstance(rows, list) or len(rows) > 128:
        raise ValueError('Guest collection bound exceeded')
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get('type') not in {'qemu', 'lxc'}:
            continue
        vmid, node = row.get('vmid'), row.get('node')
        if type(vmid) is not int or vmid <= 0 or not isinstance(node, str) or not node or len(node) > 160:
            raise ValueError('Guest identity is invalid')
        if vmid in seen or time.monotonic() > deadline:
            raise ValueError('Guest snapshot is incomplete')
        seen.add(vmid)
        payload = adapter.client.get_json(f'{base}/nodes/{quote(node, safe="")}/{row["type"]}/{vmid}/config', headers, verify).get('data')
        if not isinstance(payload, dict):
            raise ValueError('Guest network configuration unavailable')
        macs, bridges, vlans = set(), {}, {}
        for key, value in payload.items():
            if re.fullmatch(r'net\d+', key) and isinstance(value, str) and len(value) <= 4096:
                found = [match.group(1).lower() for match in _MAC.finditer(value)]
                macs.update(found)
                # Keep only the bridge name, for a "host - bridge" label.
                if (bridge := _BRIDGE.search(value)):
                    bridges.update({mac: bridge.group(1) for mac in found})
                # A NIC's own VLAN tag; bridges that carry one VLAN are
                # resolved from host network configuration by the page.
                if (tag := _TAG.search(value)) and 1 <= int(tag.group(1)) <= 4094:
                    vlans.update({mac: int(tag.group(1)) for mac in found})
        name = row.get('name')
        result.append({'vmid': vmid, 'kind': 'vm' if row['type'] == 'qemu' else 'container',
                       'name': name[:160] if isinstance(name, str) else str(vmid),
                       'host': node, 'macs': sorted(macs), 'bridges': dict(sorted(bridges.items())),
                       'vlans': dict(sorted(vlans.items())), **_guest_facts(row, payload),
                       'status': row.get('status') if row.get('status') in {'running', 'stopped', 'paused'} else 'unknown'})
    return result


def collect_guest_identities(sessions, source, adapter, settings):
    """Refresh per-host guest identity telemetry at most once every five minutes.

    Failed optional reads preserve their original samples and mark them stale;
    they cannot fail physical infrastructure collection or create placements.

    Args:
        sessions: The session factory.
        source: The Proxmox discovery source.
        adapter: The Proxmox adapter, whose client reads the API.
        settings: The source's private settings.
    """
    attempts = getattr(adapter, '_client_guest_attempts', {})
    key = (source.id, controller_url(settings))
    now = time.monotonic()
    if now - attempts.get(key, -1000) < 300:
        return
    attempts[key] = now
    adapter._client_guest_attempts = attempts
    try:
        guests = guest_snapshot(adapter, settings)
    except Exception:
        guests = None
    with sessions.begin() as db:
        for host in db.scalars(select(DiscoveredDevice).where(DiscoveredDevice.source_id == source.id)):
            details = json_object(host.details_json)
            if details.get('platform') != 'proxmox':
                continue
            previous = details.get('proxmox_clients', {})
            details['proxmox_clients'] = ({'available': True, 'sampled_at': datetime.now(timezone.utc).isoformat(),
                'guests': [guest for guest in guests if guest['host'] == host.remote_id]}
                if guests is not None else {**previous, 'available': False})
            host.details_json = json.dumps(details)
