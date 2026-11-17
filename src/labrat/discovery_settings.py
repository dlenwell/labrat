# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Edit discovery controls and scan permission in private TOML with curated audits.

Used by: api.clients, pages.settings_discovery.
"""

import copy
import hashlib
from importlib.resources import files
from ipaddress import ip_network
import json
from pathlib import Path
import tomllib

import tomlkit
from sqlalchemy import select

from labrat.audit import record_audit_event
from labrat.collector.access_collection import read_snapshot
from labrat.collector.scan import allowed_hosts
from labrat.collector.sources import configured_sources
from labrat.database import database_path
from labrat.discovery.models import DiscoverySource
from labrat.integrations import IntegrationError, configuration_edit, read_settings
from labrat.settings_forms import field_views

CATALOG = tomllib.loads(files('labrat').joinpath('discovery_catalog.toml').read_text())['methods']


def _states(config: dict, sources: dict) -> dict:
    """Preserve legacy source enablement until explicit method flags are saved.

    Args:
        config: The private configuration.
        sources: Registered discovery sources, keyed by kind and config name.

    Returns:
        dict: Whether each discovery activity (``network``, ``nut``, ``snmp``,
            ``mndp``) is on.
    """
    raw = configured_sources(config)
    flags = config.get('discovery', {})
    def enabled(kind):
        """Find an enabled legacy/configured source without inventing one."""
        return any(sources.get((kind, name)) is None or sources[(kind, name)].enabled for name in raw.get(kind, {}))
    return {'network': flags.get('network', bool(config.get('scan', {}).get('ranges'))),
            'nut': flags.get('nut', bool(raw['nut']) and enabled('nut')),
            'snmp': flags.get('snmp', any(isinstance(settings.get('snmp'), dict) and settings['snmp'].get('nodes') for settings in raw['proxmox'].values())),
            'mndp': flags.get('mndp', bool(raw['mndp']) and enabled('mndp'))}


def _section(document, kind: str, name: str):
    """Locate/create a comment-preserving source section, retaining legacy defaults.

    Args:
        document: The parsed TOML document, edited in place.
        kind: The source kind, such as ``nut``.
        name: The connection name; ``default`` may use the legacy top-level
            table.

    Returns:
        The TOML table that holds this source's settings.
    """
    if name == 'default' and (kind in document or kind not in document.get('integrations', {})):
        if kind not in document:
            document[kind] = tomlkit.table()
        return document[kind]
    for key in ['integrations', kind, name]:
        if key not in document:
            document[key] = tomlkit.table()
        document = document[key]
    return document


def _fields(method: str, form, existing: dict) -> dict:
    """Validate trusted catalog fields, retaining blank credentials without reflecting them.

    Args:
        method: The discovery activity, a key of ``CATALOG``.
        form: The submitted form.
        existing: The activity's current settings.

    Returns:
        dict: The new settings: submitted values, with blank secrets kept.

    Raises:
        IntegrationError: When the saved settings are malformed or a
            submitted value is invalid.
    """
    if not isinstance(existing, dict):
        raise IntegrationError('The discovery configuration is malformed. Correct the private configuration file first.')
    result = copy.deepcopy(existing)
    for definition in CATALOG[method].get('fields', []):
        key = definition['key']
        value = form.get(key, '')
        if not isinstance(value, str) or len(value) > 8192 or '\x00' in value:
            raise IntegrationError('A discovery setting is invalid or too long.')
        value = value.strip()
        if definition['type'] == 'password' and not value and existing.get(key):
            continue
        if definition.get('required') and not value:
            raise IntegrationError('Complete the required discovery settings.')
        if definition['type'] == 'number' and not value and 'default' in definition:
            result[key] = definition['default']  # An optional number left blank.
            continue
        if definition['type'] == 'number':
            try:
                number = int(value)
            except ValueError as exc:
                raise IntegrationError('Enter whole numbers for discovery intervals and ports.') from exc
            if not (definition['min'] <= number <= definition['max'] or definition.get('allow_zero') and number == 0):
                raise IntegrationError('A discovery interval or port is outside its allowed range.')
            result[key] = number
        elif definition['type'] == 'select':
            choice = value or definition.get('default', '')
            if choice not in {item['value'] for item in definition['choices']}:
                raise IntegrationError('Choose an available discovery option.')
            result[key] = choice
        elif definition['type'] == 'lines':
            rows = list(dict.fromkeys(line.strip() for line in value.splitlines() if line.strip()))
            if len(rows) > 128 or any(len(row) > 255 or any(ord(c) < 32 for c in row) for row in rows):
                raise IntegrationError('Use one valid node name per line, up to 128 nodes.')
            result[key] = rows
        else:
            if len(value) > 2048 or any(ord(c) < 32 for c in value) or len(value) < definition.get('min_length', 0):
                raise IntegrationError('A discovery field is invalid or a passphrase is too short.')
            result[key] = value
    return result


def _range_list(values: list) -> list[str]:
    """Canonicalize and bound explicit scan permission without expanding from discoveries.

    Args:
        values: Submitted or saved subnets in CIDR form.

    Returns:
        list[str]: The subnets, normalized, without duplicates.

    Raises:
        IntegrationError: When a value is not a valid subnet or address, or
            together they cover more than 4096 scan addresses.
    """
    result = []
    try:
        for value in values:
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError()
            normalized = str(ip_network(value.strip(), strict=False))
            if normalized not in result:
                result.append(normalized)
        allowed_hosts(result)
    except ValueError as exc:
        raise IntegrationError('Enter valid CIDRs or addresses, covering at most 4096 scan addresses in total.') from exc
    return result


def save_settings(path: Path, form, sessions, actor) -> None:
    """Save one method/configuration and matching source flags in the shared atomic writer.

    Args:
        path: The private configuration file.
        form: The submitted form: ``method``, ``operation``, and fields.
        sessions: The session factory, for the audit event.
        actor: The administrator saving.

    Raises:
        IntegrationError: When the activity, operation, or a field is
            invalid, or the file changed since it was read.
    """
    method = form.get('method')
    operation = form.get('operation')
    if method not in CATALOG or operation not in {'toggle', 'configure'}:
        raise IntegrationError('Choose a discovery activity to configure.')
    with configuration_edit(path, form.get('revision'), sessions) as (config, document, db):
        sources = {(source.kind, source.config_name): source for source in db.scalars(select(DiscoverySource))}
        states = _states(config, sources)
        raw = configured_sources(config)
        target = form.get('target', 'default')
        if operation == 'toggle':
            states[method] = form.get('enabled') == '1'
        elif method == 'network':
            manual = form.get('manual_ranges', '')
            if not isinstance(manual, str) or len(manual) > 16384:
                raise IntegrationError('The manual subnet list is too long.')
            ranges = _range_list(form.getlist('allowed_cidrs') + [line for line in manual.splitlines() if line.strip()])
            if 'scan' not in document:
                document['scan'] = tomlkit.table()
            document['scan']['ranges'] = ranges
        elif method == 'nut':
            if 'scan' not in document:
                document['scan'] = tomlkit.table()
            if 'nut' not in document['scan']:
                document['scan']['nut'] = tomlkit.table()
            settings = _fields(method, form, config.get('scan', {}).get('nut', {}))
            for definition in CATALOG[method]['fields']:
                document['scan']['nut'][definition['key']] = settings[definition['key']]
        else:
            kind = 'proxmox' if method == 'snmp' else 'mndp'
            family = raw[kind]
            if target not in family and not (method == 'mndp' and target == 'default' and not family):
                raise IntegrationError('This discovery connection no longer exists. Reload Settings.')
            existing = family.get(target, {})
            existing = existing.get('snmp', {}) if method == 'snmp' else existing
            settings = _fields(method, form, existing)
            section = _section(document, kind, target)
            if method == 'snmp':
                if 'snmp' not in section:
                    section['snmp'] = tomlkit.table()
                section = section['snmp']
            for definition in CATALOG[method]['fields']:
                section[definition['key']] = settings[definition['key']]
        if 'discovery' not in document:
            document['discovery'] = tomlkit.table()
        for key, value in states.items():
            document['discovery'][key] = bool(value)
        if 'scan' not in document:
            document['scan'] = tomlkit.table()
        probes = list(config.get('scan', {}).get('probes', []))
        probes = list(dict.fromkeys([*probes, 'nut'])) if states['nut'] else [probe for probe in probes if probe != 'nut']
        document['scan']['probes'] = probes
        if states['mndp'] and not raw['mndp']:
            _section(document, 'mndp', 'default')
        updated = tomllib.loads(tomlkit.dumps(document))
        updated_sources = configured_sources(updated)
        for kind in ('nut', 'mndp'):
            for name in updated_sources[kind]:
                source = sources.get((kind, name))
                if source is None:
                    source = DiscoverySource(kind=kind, config_name=name, name=f'{kind}:{name}', enabled=False)
                    db.add(source)
                source.enabled = bool(states[kind] and (kind != 'nut' or states['network'] and updated.get('scan', {}).get('ranges')))
            if kind == 'nut' and not states['nut']:
                for (source_kind, _name), source in sources.items():
                    if source_kind == 'nut':
                        source.enabled = False
        record_audit_event(db, actor_kind='user', actor_id=actor.id, actor_name=actor.username,
                           action='discovery.settings.updated', subject_kind='discovery_settings',
                           details={'method': method, 'operation': operation, 'enabled': bool(states[method])})


def network_rows(config: dict, db) -> tuple[list, list]:
    """Read current configured source caches; never present expired/stale policy verdicts.

    Args:
        config: The private configuration.
        db: An open database session.

    Returns:
        tuple[list, list]: The networks the gateways report, each with its
            reachability verdicts and whether scanning covers it, and the
            saved scan ranges no gateway reports.
    """
    raw = configured_sources(config)
    ranges = _range_list(config.get('scan', {}).get('ranges', []))
    rows = []
    for source in db.scalars(select(DiscoverySource).where(DiscoverySource.kind == 'unifi')):
        settings = raw['unifi'].get(source.config_name)
        if settings is None:
            continue
        cache = read_snapshot(Path(str(database_path(config)) + '.access-evidence') / f'source-{source.id}.json')
        fingerprint = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
        fresh = source.enabled and cache['available'] and cache.get('fingerprint') == fingerprint
        for site in cache.get('sites', []):
            for candidate in site.get('candidates', []):
                row = {key: candidate.get(key) for key in ('network_id', 'name', 'cidr', 'route_target', 'route')}
                row.update(connection=settings.get('display_name') or source.config_name,
                           observed_at=cache.get('collected_at'), fresh=bool(fresh), source_id=source.id,
                           site_id=site['site_id'], routable='unknown')
                row['key'] = hashlib.sha256(f'{source.id}:{site["site_id"]}:{candidate["network_id"]}'.encode()).hexdigest()[:20]
                row['profiles'] = candidate.get('profiles', {}) if fresh else {}
                verdicts = [row['profiles'].get(profile, {}).get('access', 'unknown') for profile in ('nut', 'snmp')]
                row['status'] = 'blocked' if all(v == 'blocked' for v in verdicts) else 'allowed' if all(v == 'allowed' for v in verdicts) else 'mixed' if 'blocked' in verdicts or 'allowed' in verdicts else 'unknown'
                row['checked'] = row['cidr'] in ranges
                row['covered'] = False
                if row['cidr']:
                    net = ip_network(row['cidr'])
                    row['covered'] = not row['checked'] and any(net.version == (parent := ip_network(value)).version and net.subnet_of(parent) for value in ranges)
                rows.append(row)
    known = {row['cidr'] for row in rows if row['cidr']}
    return rows, [value for value in ranges if value not in known]


def panel_context(path: Path, db, *, method=None, target=None, subnet=None, submitted=None) -> dict:
    """Return safe activity rows, explicit subnet permission, and optional drawer content.

    Args:
        path: The private configuration file.
        db: An open database session.
        method: The activity whose drawer is open, if any.
        target: The connection the drawer edits, if any.
        subnet: The network row the drawer shows, if any.
        submitted: The posted form when re-rendering after a failed save.

    Returns:
        dict: The panel's template context.

    Raises:
        IntegrationError: When the drawer names an unknown activity,
            network, or connection, or the configuration is malformed.
    """
    _raw, config, revision = read_settings(path)
    sources = {(source.kind, source.config_name): source for source in db.scalars(select(DiscoverySource))}
    states = _states(config, sources)
    networks, manual = network_rows(config, db)
    methods = [{'key': key, **definition, 'enabled': bool(states[key])} for key, definition in CATALOG.items()]
    editor = None
    if method is not None:
        if method not in CATALOG:
            raise IntegrationError('Choose a discovery activity.')
        editor = {'method': method, 'label': CATALOG[method]['label'], 'view': 'editor', 'target': target or 'default'}
        family = configured_sources(config)
        if subnet is not None:
            selected = next((row for row in networks if row['key'] == subnet), None)
            if selected is None:
                raise IntegrationError('This subnet observation is unavailable. Reload Settings.')
            editor.update(view='detail', label=selected['name'] or 'Subnet details', subnet=selected)
        elif method == 'network':
            editor.update(networks=networks, manual_ranges='\n'.join(manual))
            if submitted is not None:
                chosen = set(submitted.getlist('allowed_cidrs'))
                editor['networks'] = [{**row, 'checked': row['cidr'] in chosen} for row in networks]
                editor['manual_ranges'] = submitted.get('manual_ranges', '')
        else:
            kind = 'proxmox' if method == 'snmp' else 'mndp'
            choices = family[kind] if method in {'snmp', 'mndp'} else {}
            if method == 'snmp' and target is None or method == 'mndp' and target is None and len(choices) > 1:
                editor.update(view='menu', choices=[{'name': name, 'label': settings.get('display_name') or name} for name, settings in choices.items()])
            else:
                if method == 'nut':
                    settings = config.get('scan', {}).get('nut', {})
                else:
                    if target is not None and target not in choices and not (method == 'mndp' and target == 'default' and not choices):
                        raise IntegrationError('This discovery connection is unavailable.')
                    settings = choices.get(target or 'default', {})
                    if method == 'snmp':
                        settings = settings.get('snmp', {})
                if not isinstance(settings, dict):
                    raise IntegrationError('The discovery configuration is malformed. Correct the private configuration file first.')
                editor['fields'] = field_views(CATALOG[method].get('fields', []), settings, submitted)
    return {'discovery_methods': methods, 'discovery_editor': editor, 'discovery_revision': revision}
