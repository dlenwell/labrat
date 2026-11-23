# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Accept observations from machines that hold a scoped API token.

A standalone MNDP listener (``python -m labrat.collector.mndp_push``)
runs on the network segment its MikroTiks announce on and posts what it
hears here, so LabRat itself needs no layer-2 access. Each token feeds its
own discovery source (kind ``mndp_push``), which the collector never
polls; the announcements go through the same batch builder as the
collector's own listener, so a MikroTik looks the same whichever heard it.

These routes take only ``Authorization: Bearer <token>``: no session
cookie, so no CSRF token either. A token is checked for the route's scope
(``labrat/api_tokens.py``).
"""

from collections.abc import Callable
from datetime import datetime, timezone
from functools import wraps
import ipaddress
import re
from typing import Any

from flask import Blueprint, current_app, g, jsonify, request
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from labrat.api_tokens import ApiToken, authenticate
from labrat.collector.adapters.mndp import batch_from_announcements
from labrat.collector.store import apply_batch
from labrat.discovery.models import DiscoverySource

machine_bp = Blueprint("machine", __name__, url_prefix="/api/v1/machine")

PUSH_KIND = "mndp_push"
"""Source kind for announcements a standalone listener reports."""

MAX_ANNOUNCEMENTS = 500
"""Most announcements one request may carry."""

TEXT_FIELDS = ("identity", "board", "version", "platform", "software_id", "interface")
"""Optional text fields of an announcement, as ``decode_packet`` names them."""

TEXT_MAX = 255
"""Longest text field accepted."""

MAC_PATTERN = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
"""A MAC address in the lowercase colon form ``decode_packet`` produces."""


class MachineRequestError(ValueError):
    """A machine sent a body LabRat cannot accept; the message says which field."""


def _error(status: int, code: str, detail: str | None = None) -> ResponseReturnValue:
    """Answer a machine with a JSON error.

    Args:
        status: The HTTP status.
        code: A stable error code.
        detail: What to fix, when it helps.

    Returns:
        ResponseReturnValue: The JSON error response.
    """
    body: dict[str, str] = {"error": code}
    if detail:
        body["detail"] = detail
    response = jsonify(body)
    response.status_code = status
    if status == 401:
        response.headers["WWW-Authenticate"] = 'Bearer realm="labrat"'
    elif status == 403:
        response.headers["WWW-Authenticate"] = 'Bearer realm="labrat", error="insufficient_scope"'
    return response


def token_required(scope: str) -> Callable[[Callable[..., ResponseReturnValue]], Callable[..., ResponseReturnValue]]:
    """Require a bearer token holding one scope; put it in ``g.api_token``.

    Args:
        scope: The scope the route needs.

    Returns:
        Callable: A decorator for the route.
    """
    def decorate(view: Callable[..., ResponseReturnValue]) -> Callable[..., ResponseReturnValue]:
        """Wrap a route with the token check."""

        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> ResponseReturnValue:
            """Check the token, then run the route."""
            header = request.headers.get("Authorization", "")
            kind, _, presented = header.partition(" ")
            if kind.lower() != "bearer" or not presented.strip():
                return _error(401, "token_required")
            with current_app.extensions["labrat_session_factory"]() as db:
                token, outcome = authenticate(db, presented.strip(), scope)
                if token is not None:
                    db.commit()
                    g.api_token = {"id": token.id, "name": token.name}
            if outcome == "forbidden":
                return _error(403, "insufficient_scope", f"This token lacks the {scope} permission.")
            if token is None:
                return _error(401, "invalid_token")
            return view(*args, **kwargs)

        wrapped.token_scope = scope  # type: ignore[attr-defined]  # read by the API reference
        return wrapped

    return decorate


def read_announcements(payload: Any) -> tuple[list[dict[str, Any]], int | None]:
    """Validate a listener's report.

    Args:
        payload: The decoded JSON body: ``announcements`` (a list of
            objects with ``identity`` and any of ``board``, ``version``,
            ``platform``, ``software_id``, ``interface``, ``mac``, ``ipv4``,
            ``uptime_seconds``) and an optional ``interval_seconds``, how
            often the listener reports.

    Returns:
        tuple[list[dict[str, Any]], int | None]: Clean announcements and the
            interval.

    Raises:
        MachineRequestError: A field is missing, of the wrong type, or too long.
    """
    if not isinstance(payload, dict):
        raise MachineRequestError("Send a JSON object.")
    items = payload.get("announcements")
    if not isinstance(items, list) or len(items) > MAX_ANNOUNCEMENTS:
        raise MachineRequestError(f"announcements must be a list of at most {MAX_ANNOUNCEMENTS} objects.")
    interval = payload.get("interval_seconds")
    if interval is not None and (type(interval) is not int or not 5 <= interval <= 86400):
        raise MachineRequestError("interval_seconds must be an integer from 5 to 86400.")
    clean: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise MachineRequestError(f"announcements[{index}] must be an object.")
        fields: dict[str, Any] = {}
        for name in TEXT_FIELDS:
            value = item.get(name)
            if value is None:
                continue
            if not isinstance(value, str) or not 0 < len(value.strip()) <= TEXT_MAX:
                raise MachineRequestError(f"announcements[{index}].{name} must be text of 1 to {TEXT_MAX} characters.")
            fields[name] = value.strip()
        if "identity" not in fields:
            raise MachineRequestError(f"announcements[{index}].identity is required.")
        mac = item.get("mac")
        if mac is not None:
            if not isinstance(mac, str) or not MAC_PATTERN.match(mac.lower()):
                raise MachineRequestError(f"announcements[{index}].mac must look like aa:bb:cc:dd:ee:ff.")
            fields["mac"] = mac.lower()
        address = item.get("ipv4")
        if address is not None:
            try:
                fields["ipv4"] = str(ipaddress.IPv4Address(address))
            except (ipaddress.AddressValueError, ValueError, TypeError):
                raise MachineRequestError(f"announcements[{index}].ipv4 must be an IPv4 address.") from None
        uptime = item.get("uptime_seconds")
        if uptime is not None:
            if type(uptime) is not int or uptime < 0:
                raise MachineRequestError(f"announcements[{index}].uptime_seconds must be a whole number of seconds.")
            fields["uptime_seconds"] = uptime
        clean.append(fields)
    return clean, interval


def push_source(db: Any, token: ApiToken | dict[str, Any]) -> DiscoverySource:
    """Find or create the discovery source a token reports into.

    Args:
        db: The open session.
        token: The token (row, or ``g.api_token``'s id and name).

    Returns:
        DiscoverySource: The token's own ``mndp_push`` source.
    """
    token_id = token["id"] if isinstance(token, dict) else token.id
    token_name = token["name"] if isinstance(token, dict) else token.name
    config_name = f"token-{token_id}"
    source = db.scalar(select(DiscoverySource).where(
        DiscoverySource.kind == PUSH_KIND, DiscoverySource.config_name == config_name,
    ))
    if source is not None:
        return source
    name = f"mndp:{token_name}"
    if db.scalar(select(DiscoverySource.id).where(DiscoverySource.name == name)) is not None:
        name = f"mndp:{token_name} ({token_id})"
    source = DiscoverySource(name=name[:120], kind=PUSH_KIND, config_name=config_name, enabled=True)
    db.add(source)
    db.flush()
    from labrat.audit import record_audit_event

    record_audit_event(
        db, actor_kind="system", actor_name="api-token",
        action="discovery.source.created", subject_kind="discovery_source", subject_id=source.id,
        details={"kind": PUSH_KIND, "token_id": token_id},
    )
    return source


@machine_bp.post("/mndp")
@token_required("observations:mndp")
def report_mndp() -> ResponseReturnValue:
    """Record MikroTik announcements a standalone listener heard.

    Needs a token with the ``observations:mndp`` permission. The body is
    ``{"announcements": [{"identity": ..., "board": ..., "mac": ...}, ...],
    "interval_seconds": 60}``. Each report is a snapshot from one listen
    window; MikroTiks it leaves out are kept and go stale, as with every
    source.

    Returns:
        ResponseReturnValue: HTTP 202 with the counts recorded, 400 for a
            malformed body, 401 for a missing or invalid token, or 403 for
            a token without this permission.
    """
    if not request.is_json:
        return _error(400, "invalid_request", "Send Content-Type: application/json.")
    try:
        announcements, interval = read_announcements(request.get_json(silent=True))
    except MachineRequestError as exc:
        return _error(400, "invalid_request", str(exc))
    batch = batch_from_announcements(announcements)
    now = datetime.now(timezone.utc)
    with current_app.extensions["labrat_session_factory"].begin() as db:
        source = push_source(db, g.api_token)
        source.last_attempt_at = now
        source.last_error = None
        if interval is not None:
            source.poll_interval_seconds = interval
        apply_batch(db, source, batch, seen_at=now)
    return jsonify(accepted=len(announcements), devices=len(batch.devices), ports=len(batch.ports)), 202
