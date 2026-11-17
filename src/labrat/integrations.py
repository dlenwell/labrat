# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Edit private integration TOML without putting credentials in database rows.

Integration forms edit UniFi and Proxmox connection fields. Discovery controls
share the atomic configuration writer. Advanced settings and comments survive
round trips. Writes use an optimistic revision,
a per-file lock, and a private atomic replacement. Audit entries describe the
source and intent, never submitted connection settings or credentials.

Used by: api.clients, discovery_settings, pages.private,
pages.settings_discovery, pages.settings_integrations.
"""

import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import os
import re
import tempfile
import tomllib
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

import requests
import tomlkit
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from labrat.audit import record_audit_event
from labrat.collector.http import JsonGetClient, controller_url, tls_verification, unifi_network_root
from labrat.collector.sources import configured_sources
from labrat.discovery.models import DiscoverySource, DiscoveredDevice
from labrat.placement import DevicePlacement
from labrat.models import User

from labrat.integration_catalog import CATALOG, CATEGORIES, DEFINITIONS, KINDS
from labrat.settings_forms import field_views


def _secret_fields(kind: str) -> list[str]:
    """Return only credential fields declared secret by the public catalog.

    Args:
        kind: The integration kind, a key of the catalog.

    Returns:
        list[str]: The keys of its password fields.
    """
    return [field["key"] for field in DEFINITIONS[kind]["fields"] if field["type"] == "password"]


class IntegrationError(ValueError):
    """Describe a safe form error without including secret values or raw failures."""


def read_settings(path: Path) -> tuple[bytes, dict, str]:
    """Read private configuration and its optimistic revision without caching.

    Args:
        path: The app's configured TOML file, resolved at startup.

    Returns:
        tuple: Original bytes, parsed plain settings, and a revision digest.

    Raises:
        IntegrationError: Configuration is missing, unreadable, or malformed.
    """
    try:
        raw = path.read_bytes()
        config = tomllib.loads(raw.decode("utf-8"))
        configured_sources(config)
    except (OSError, ValueError, UnicodeError) as exc:
        raise IntegrationError("The configuration file could not be read. Check it before editing integrations.") from exc
    return raw, config, hashlib.sha256(raw).hexdigest()


def _text(form: object, field: str, *, maximum: int = 2048, required: bool = False) -> str:
    """Validate one bounded form field while keeping its value out of errors.

    Args:
        form: The submitted form.
        field: The field's name.
        maximum: The longest value accepted.
        required: Refuse a blank value.

    Returns:
        str: The trimmed value.

    Raises:
        IntegrationError: When the value is not text, is too long, holds
            control characters, or is blank but required.
    """
    value = form.get(field, "")
    if not isinstance(value, str) or len(value) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise IntegrationError("A connection field contains unsupported characters or is too long.")
    value = value.strip()
    if required and not value:
        raise IntegrationError("Complete the required connection fields.")
    return value


def candidate(form: object, config: dict) -> tuple[str, str, dict, bool]:
    """Validate a submitted connection, preserving blank saved secrets and extras.

    Saved secrets may be reused only at the same normalized controller root.
    New configurations use named sections; existing legacy sections stay put.

    Args:
        form: Untrusted form fields; values must never be logged or echoed.
        config: Freshly parsed private TOML.

    Returns:
        tuple: Adapter kind, immutable config name, private settings, and new flag.

    Raises:
        IntegrationError: When the kind, name, address, or a field is
            invalid, or a new address comes without its secret.
    """
    kind = _text(form, "kind", maximum=20)
    if kind not in KINDS:
        raise IntegrationError("Choose a supported integration.")
    is_new = form.get("is_new") == "1"
    family = configured_sources(config)[kind]
    name = _text(form, "name", maximum=80, required=not is_new)
    display_name = _text(form, "display_name", maximum=80, required="display_name" in form)
    if is_new and not name:
        if not display_name:
            raise IntegrationError("Give this connection a name.")
        normalized = unicodedata.normalize("NFKD", display_name).encode("ascii", "ignore").decode()
        stem = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")[:70] or "connection"
        name = stem
        suffix = 2
        while name in family:
            name = f"{stem}-{suffix}"
            suffix += 1
    current = family.get(name)
    if is_new:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name):
            raise IntegrationError("Use letters, numbers, hyphens, and underscores for the integration name.")
        if current is not None:
            raise IntegrationError("That integration name already exists. Edit the existing connection instead.")
    elif current is None:
        raise IntegrationError("This integration no longer exists. Reload the Integrations panel.")
    settings = copy.deepcopy(current or {})
    if display_name:
        settings["display_name"] = display_name
    label = settings.get("display_name") or name
    if any(peer_name != name and (peer.get("display_name") or peer_name) == label for peer_name, peer in family.items()):
        raise IntegrationError("Another connection of this type already uses that name.")
    raw_url = _text(form, "url", required=True)
    try:
        settings["url"] = controller_url({"url": raw_url})
        parts = urlsplit(settings["url"])
        if not parts.netloc or parts.port == 0 or any(char.isspace() for char in parts.netloc):
            raise ValueError()
    except ValueError as exc:
        raise IntegrationError("Enter an HTTPS controller address without a path, query, or embedded credentials.") from exc
    previous_url = current.get("url") if current is not None else None
    same_url = isinstance(previous_url, str) and previous_url.strip().rstrip("/") == settings["url"]
    for field in DEFINITIONS[kind]["fields"]:
        key = field["key"]
        if key == "url":
            continue
        if field["type"] == "checkbox":
            settings[key] = form.get(key) == "1"
            if field.get("required") and not settings[key]:
                raise IntegrationError("Complete the required connection fields.")
            continue
        value = _text(form, key, maximum=field.get("max_length", 2048))
        if field["type"] == "password" and not value:
            saved = settings.get(key)
            if same_url and isinstance(saved, str) and saved.strip():
                continue
            if field.get("required"):
                raise IntegrationError("Enter the integration secret. A new controller address requires the secret again.")
        if field.get("required") and not value:
            raise IntegrationError("Complete the required connection fields.")
        if not value:
            settings.pop(key, None)
            continue
        if field.get("pattern") and not re.fullmatch(field["pattern"], value):
            raise IntegrationError(field.get("validation_message", "A connection field has an invalid format."))
        if field["type"] == "number":
            try:
                number = int(value)
            except ValueError as exc:
                raise IntegrationError("Enter a whole number for the numeric connection field.") from exc
            if number < field.get("min", -(2 ** 31)) or number > field.get("max", 2 ** 31 - 1):
                raise IntegrationError("A numeric connection field is outside its allowed range.")
            settings[key] = number
        elif field["type"] == "select":
            if value not in {choice["value"] for choice in field["choices"]}:
                raise IntegrationError("Choose an available connection option.")
            settings[key] = value
        else:
            settings[key] = value
    mode = form.get("tls_mode", "verify")
    if mode == "existing" and current is not None and current.get("verify_tls") is False and same_url:
        settings["verify_tls"] = False
        settings.pop("ca_bundle", None)
    elif mode == "verify":
        settings["verify_tls"] = True
        ca_bundle = _text(form, "ca_bundle")
        if ca_bundle:
            ca_path = Path(ca_bundle).expanduser()
            if not ca_path.is_absolute() or not ca_path.is_file():
                raise IntegrationError("The CA bundle must be an existing absolute file path on the LabRat server.")
            settings["ca_bundle"] = str(ca_path)
        else:
            settings.pop("ca_bundle", None)
    else:
        raise IntegrationError("Choose certificate verification for this controller.")
    tls_verification(settings)
    return kind, name, settings, is_new


def _display_url(value: object) -> str:
    """Return a controller root only, withholding malformed credential-bearing URLs.

    Args:
        value: A saved controller address.

    Returns:
        str: The controller's root URL, or an empty string when the address
            is malformed.
    """
    try:
        if not isinstance(value, str) or any(ord(char) < 32 for char in value):
            return ""
        return controller_url({"url": value})
    except ValueError:
        return ""


def connection_health(settings: dict, source: DiscoverySource | None, now: datetime | None = None) -> dict:
    """Describe cached collection health using safe, deliberately selected text.

    Green requires a recent successful collection. A recorded failure or overdue
    collection is red; disabled and never-collected connections remain neutral.
    Raw exception text is never returned, including in the full detail drawer.

    Args:
        settings: Private connection configuration; only validity is inspected.
        source: Existing non-secret discovery source metadata, if registered.
        now: Optional UTC time for deterministic checks.

    Returns:
        dict: Lamp state, accessible label, safe message, and collection times.
    """
    now = now or datetime.now(timezone.utc)
    def utc(value):
        """Normalize SQLite timestamps without inventing local timezone offsets."""
        return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value
    success = utc(source.last_success_at) if source else None
    attempt = utc(source.last_attempt_at) if source else None
    state, label, message = 'waiting', 'Awaiting first collection', 'No collection result is available yet.'
    if source is not None and not source.enabled:
        state, label, message = 'waiting', 'Collection disabled', 'Collection is disabled for this connection.'
    elif not _display_url(settings.get('url')):
        state, label, message = 'error', 'Invalid connection settings', 'The controller address is missing or invalid. Edit this connection to correct it.'
    elif source is not None and source.last_error:
        state, label = 'error', 'Collection failed'
        messages = {
            'SSLError': 'Certificate verification failed. Check the controller certificate and CA bundle.',
            'HTTPError': 'The controller rejected a collection request. Check the API credentials, API support, and read permissions.',
            'ConnectionError': 'The collector could not reach the controller. Check its address, DNS, and network connectivity.',
            'Timeout': 'The controller did not respond in time. Check connectivity and controller availability.',
            'ConnectTimeout': 'The connection timed out. Check the controller address and network connectivity.',
            'ReadTimeout': 'The controller response timed out. Check controller availability and network connectivity.',
            'ValueError': 'The controller returned an unexpected response or its settings are invalid. Review this connection and API support.',
        }
        error_type = source.last_error.removeprefix('Collection failed: ')
        message = messages.get(error_type, 'Collection failed. Check the controller, connection settings, credentials, and read permissions.')
    elif success:
        interval = source.poll_interval_seconds
        threshold = timedelta(seconds=max(180, interval * 2 + 60)) if type(interval) is int and interval > 0 else timedelta(minutes=30)
        if now - success > threshold:
            state, label, message = 'error', 'Collection overdue', 'No recent successful collection. Check that the collector is running and the controller is reachable.'
        else:
            state, label, message = 'ready', 'Active', 'The latest full collection completed successfully.'
    return {'state': state, 'label': label, 'message': message,
            'last_success': success.isoformat() if success else None,
            'last_attempt': attempt.isoformat() if attempt else None}


def removal_preview(db: Session, source: DiscoverySource | None) -> dict:
    """Count retained observations and curated inventory without changing rows.

    Args:
        db: An open database session.
        source: The connection's registered source, or None when it has none.

    Returns:
        dict: How many observations, inventory devices, and placements would
            stay after the connection is deleted.
    """
    if source is None:
        return {'observations': 0, 'inventory': 0, 'placements': 0}
    devices = select(DiscoveredDevice.device_id).where(
        DiscoveredDevice.source_id == source.id, DiscoveredDevice.device_id.is_not(None),
    ).distinct()
    return {
        'observations': db.scalar(select(func.count()).select_from(DiscoveredDevice).where(DiscoveredDevice.source_id == source.id)),
        'inventory': db.scalar(select(func.count(func.distinct(DiscoveredDevice.device_id))).where(DiscoveredDevice.source_id == source.id)),
        'placements': db.scalar(select(func.count()).select_from(DevicePlacement).where(DevicePlacement.device_id.in_(devices))),
    }


def panel_context(path: Path, db: Session, *, kind: str | None = None, name: str | None = None, is_new: bool = False, removing: bool = False, submitted: object | None = None) -> dict:
    """Build explicitly whitelisted display data; secret inputs are always empty.

    Args:
        path: Active private TOML configuration path.
        db: Open read session for source status.
        kind: Optional editor's adapter type.
        name: Optional existing config name.
        is_new: Open the add-connection form rather than an existing entry.
        removing: Show an explicit deletion preview instead of connection fields.
        submitted: Optional failed/tested submission, with safe fields only echoed.

    Returns:
        dict: Safe overview, editor, revision, and integration labels for templates.

    Raises:
        IntegrationError: When the editor names an unknown kind or a
            connection that no longer exists.
    """
    _raw, config, revision = read_settings(path)
    sources = {(row.kind, row.config_name): row for row in db.scalars(select(DiscoverySource))}
    entries = []
    configured = configured_sources(config)
    for family in KINDS:
        for config_name, settings in sorted(configured[family].items()):
            source = sources.get((family, config_name))
            entries.append({"kind": family, "label": KINDS[family], "name": config_name,
                            "display_name": settings.get("display_name") or config_name,
                            "icon": next(entry["icon"] for entry in CATALOG if entry["kind"] == family),
                            "url": _display_url(settings.get("url")), "enabled": source.enabled if source else True,
                            "has_secret": all(bool(settings.get(key)) for key in _secret_fields(family)),
                            "last_success": source.last_success_at if source else None,
                            "last_error": bool(source.last_error) if source else False,
                            "health": connection_health(settings, source)})
    editor = None
    if kind is not None:
        if kind not in KINDS:
            raise IntegrationError("Choose a supported integration.")
        settings = {} if is_new else configured[kind].get(name)
        if settings is None:
            raise IntegrationError("This integration no longer exists. Reload the Integrations panel.")
        source = sources.get((kind, name))
        editor = {"kind": kind, "label": KINDS[kind], "name": name or "", "is_new": is_new,
                  "display_name": settings.get("display_name") or name or "",
                  "removing": removing and not is_new,
                  "health": connection_health(settings, source),
                  "removal": removal_preview(db, source) if removing and not is_new else None,
                  "ca_bundle": settings.get("ca_bundle", ""),
                  "tls_mode": "existing" if settings.get("verify_tls") is False else "verify",
                  "legacy_tls": settings.get("verify_tls") is False,
                  "has_secret": all(bool(settings.get(key)) for key in _secret_fields(kind)),
                  "enabled": source.enabled if source else True}
        if submitted is not None:
            for field in ("name", "display_name", "ca_bundle", "tls_mode"):
                if field not in submitted:
                    continue
                value = submitted.get(field, "")
                editor[field] = value[:2048] if isinstance(value, str) else ""
            editor["enabled"] = submitted.get("enabled") == "1"
            revision = submitted.get("revision", revision)
        editor["category"] = DEFINITIONS[kind]["category"]
        editor["fields"] = field_views(DEFINITIONS[kind]["fields"], settings, submitted)
        for field in editor["fields"]:
            if field["definition"]["key"] == "url":
                field["value"] = _display_url(field["value"])
    return {"integrations": entries, "integration_editor": editor,
            "integration_revision": revision, "integration_kinds": KINDS,
            "integration_catalog": CATALOG, "integration_categories": CATEGORIES}


def _atomic_write(path: Path, raw: bytes) -> None:
    """Fsync and atomically replace TOML with owner-only permissions, no backup copy.

    Args:
        path: The configuration file to replace.
        raw: Its new contents.
    """
    descriptor, filename = tempfile.mkstemp(prefix=".labrat-config-", suffix=".tmp", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def configuration_edit(path: Path, expected_revision: str, sessions: sessionmaker):
    """Share locked, revision-checked TOML/audit commit and rollback across writes.

    Yields:
        tuple: Parsed private settings, comment-preserving TOML, and database session.

    Args:
        path: The private configuration file.
        expected_revision: The revision the form was opened at.
        sessions: The session factory for the audit transaction.

    Raises:
        IntegrationError: When the file changed since the form was opened or
            while saving.
    """
    descriptor = os.open(path.with_name(path.name + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        original, config, revision = read_settings(path)
        if expected_revision != revision:
            raise IntegrationError('Configuration changed since this form was opened. Reload Settings before saving.')
        document = tomlkit.parse(original.decode('utf-8'))
        written = False
        updated = None
        try:
            with sessions.begin() as db:
                yield config, document, db
                db.flush()
                updated = tomlkit.dumps(document).encode('utf-8')
                configured_sources(tomllib.loads(updated.decode('utf-8')))
                if path.read_bytes() != original:
                    raise IntegrationError('Configuration changed while saving. Reload Settings and try again.')
                _atomic_write(path, updated)
                written = True
        except Exception:
            if written and path.read_bytes() == updated:
                _atomic_write(path, original)
            raise


def save_integration(path: Path, form: object, sessions: sessionmaker, actor: User) -> tuple[str, str]:
    """Validate and save connection fields, source metadata, and a curated audit.

    Args:
        path: Active private TOML file.
        form: Untrusted submitted fields and file revision.
        sessions: Source/audit transaction factory.
        actor: Authorized administrator making the explicit change.

    Returns:
        tuple[str, str]: Adapter kind and unchanged internal configuration key.

    Raises:
        IntegrationError: When the submission is invalid, the name is taken,
            or the file changed since the form was opened.
    """
    with configuration_edit(path, form.get('revision'), sessions) as (config, document, db):
        kind, name, settings, is_new = candidate(form, config)
        if not is_new and name == "default" and kind in config:
            target = document[kind]
        else:
            if "integrations" not in document:
                document["integrations"] = tomlkit.table()
            if kind not in document["integrations"]:
                document["integrations"][kind] = tomlkit.table()
            family = document["integrations"][kind]
            if name not in family:
                family[name] = tomlkit.table()
            target = family[name]
        editable = {field["key"] for field in DEFINITIONS[kind]["fields"]} | {"verify_tls", "ca_bundle", "display_name"}
        for field in editable:
            if field in settings:
                target[field] = settings[field]
            elif field in target:
                del target[field]
        source = db.scalar(select(DiscoverySource).where(
            DiscoverySource.kind == kind, DiscoverySource.config_name == name,
        ))
        if source is None:
            source = DiscoverySource(kind=kind, config_name=name, name=f"{kind}:{name}")
            db.add(source)
        source.name = f"{kind}:{settings.get('display_name') or name}"
        with db.no_autoflush:
            query = select(DiscoverySource.id).where(DiscoverySource.name == source.name)
            if source.id is not None:
                query = query.where(DiscoverySource.id != source.id)
            duplicate = db.scalar(query)
        if duplicate is not None:
            raise IntegrationError("Another connection of this type already uses that name.")
        source.enabled = form.get("enabled") == "1"
        db.flush()
        record_audit_event(db, actor_kind="user", actor_id=actor.id, actor_name=actor.username,
                           action="integration.created" if is_new else "integration.updated",
                           subject_kind="discovery_source", subject_id=source.id,
                           details={"kind": kind, "config_name": name, "enabled": source.enabled})
    return kind, name


def delete_integration(path: Path, form: object, sessions: sessionmaker, actor: User) -> None:
    """Remove credentials and disable collection while preserving all discovery data.

    Args:
        path: Active private TOML file.
        form: Source identity, current revision, and explicit deletion confirmation.
        sessions: Source/audit transaction factory.
        actor: Authorized administrator confirming the operation.

    Raises:
        IntegrationError: Missing confirmation, stale form, or absent connection.
    """
    if form.get('confirm') != 'delete':
        raise IntegrationError('Confirm that you understand retained devices may remain visible before deleting this integration.')
    kind = _text(form, 'kind', maximum=40, required=True)
    name = _text(form, 'name', maximum=80, required=True)
    if kind not in KINDS:
        raise IntegrationError('Choose a supported integration.')
    with configuration_edit(path, form.get('revision'), sessions) as (config, document, db):
        if name not in configured_sources(config)[kind]:
            raise IntegrationError('This integration no longer exists. Reload the Integrations panel.')
        if name == 'default' and kind in config:
            del document[kind]
        else:
            del document['integrations'][kind][name]
            if not document['integrations'][kind]:
                del document['integrations'][kind]
            if not document['integrations']:
                del document['integrations']
        source = db.scalar(select(DiscoverySource).where(
            DiscoverySource.kind == kind, DiscoverySource.config_name == name,
        ))
        retained = removal_preview(db, source)
        if source is not None:
            source.enabled = False
        record_audit_event(db, actor_kind='user', actor_id=actor.id, actor_name=actor.username,
                           action='integration.deleted', subject_kind='discovery_source',
                           subject_id=source.id if source else None,
                           details={'kind': kind, 'config_name': name, 'retained': retained})


def test_connection(kind: str, settings: dict) -> tuple[bool, str]:
    """Make one bounded read-only API request, returning only a safe result.

    This checks the collection entry point, not every permission a full scan
    may need. It never registers a source or persists the submitted settings.

    Args:
        kind: The integration kind.
        settings: The submitted, validated settings.

    Returns:
        tuple[bool, str]: Whether the request succeeded, and a safe message
            to show.
    """
    definition = DEFINITIONS[kind]["test"]
    if definition.get("style") == "adapter":
        # Integrations whose sign-in is not one header (Pi-hole v6 sessions)
        # test through their adapter, which makes only reads after signing in.
        from labrat.collector.adapters.pihole import PiholeAdapter
        try:
            return PiholeAdapter().test_connection(settings)
        except ValueError:
            return False, "Fill in the address and credential first."
    # UniFi paths are relative to the Network application root.
    root = unifi_network_root(settings) if kind == "unifi" else controller_url(settings)
    verify = tls_verification(settings)
    endpoint = root + definition["path"]
    headers = {"Accept": "application/json", definition["auth_header"]: definition["auth_template"].format_map(settings)}
    success = f'Connection successful. The API credential can read {definition["collection_label"]}.'
    try:
        with requests.Session() as session:
            payload = JsonGetClient(session).get_json(endpoint, headers, verify)
        if not isinstance(payload, dict) or not isinstance(payload.get(definition["response_key"]), list) or not all(isinstance(row, dict) for row in payload[definition["response_key"]]):
            raise ValueError()
        return True, success
    except requests.exceptions.SSLError:
        return False, "Certificate verification failed. Check the controller certificate or configure its CA bundle."
    except requests.exceptions.Timeout:
        return False, "The controller did not respond in time. Check its address and connectivity."
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in {401, 403}:
            return False, "Authentication or access was denied. Check the credentials and read permissions."
        return False, "The controller returned an unexpected HTTP response. Check the address and API support."
    except requests.exceptions.RequestException:
        return False, "The controller could not be reached. Check its address and connectivity."
    except (ValueError, OSError):
        return False, "The controller did not return the expected API response. Check API support and certificate settings."
