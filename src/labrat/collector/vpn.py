# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Extract VPN telemetry without retaining configuration secrets or user sessions.

The controller's VPN configuration includes private keys, certificates, and
passwords. Only the explicit display fields below may leave this parser.
An enabled profile is not proof that a tunnel is connected.
"""
from collections.abc import Mapping
from datetime import datetime, timezone
from functools import partial
from typing import Any

from labrat.coerce import number, text


# VPN counters are never negative; names keep their spacing (labrat.coerce).
_number = partial(number, minimum=0)
_text = partial(text, strip=False)


def vpn_snapshot(
    connections: list[dict], networks: list[dict], remote_sessions: int | None,
    sampled_at: datetime | None = None,
) -> dict:
    """Build a whitelist-only snapshot of client and site-to-site VPN tunnels.

    Args:
        connections: Runtime rows from the v2 VPN connections endpoint.
        networks: In-memory classic network configuration; never persist raw.
        remote_sessions: Count of remote-access sessions, or None if unavailable.
        sampled_at: Test clock or current UTC sampling time.

    Returns:
        dict: Explicit connection state, start time, rates in bit/s, configured
        name/protocol, and remote-access count. Missing runtime rows remain
        unknown, including enabled profiles. Idle traffic never implies down.
    """
    now = sampled_at or datetime.now(timezone.utc)
    profiles = {
        row['_id']: row for row in networks if isinstance(row, Mapping)
        and isinstance(row.get('_id'), str) and row.get('purpose') in {'vpn-client', 'site-vpn'}
    }
    runtime = {row['network_id']: row for row in connections if isinstance(row, Mapping)
               and isinstance(row.get('network_id'), str)}
    result = []
    for identity in sorted(set(profiles) | set(runtime)):
        profile, row = profiles.get(identity, {}), runtime.get(identity, {})
        enabled = profile.get('enabled') if type(profile.get('enabled')) is bool else None
        reported = _text(row.get('status'), 40)
        reported = reported.upper() if reported else None
        state = ('disabled' if enabled is False else 'connected' if reported in {'CONNECTED', 'ESTABLISHED', 'UP'}
                 else 'disconnected' if reported in {'DISCONNECTED', 'DOWN', 'FAILED', 'ERROR'}
                 else 'connecting' if reported in {'CONNECTING', 'RECONNECTING', 'PENDING'} else 'unknown')
        associated = _number(row.get('assoc_time'))
        if associated is not None and (associated == 0 or associated > now.timestamp()):
            associated = None
        result.append({
            'id': identity, 'name': _text(profile.get('name')) or 'VPN connection',
            'protocol': _text(row.get('type')) or _text(profile.get('vpn_type')) or 'Not reported',
            'enabled': enabled, 'state': state, 'reported_status': reported,
            'connected_at': associated,
            'rx_bps': _number(row.get('rx_rate_bps')), 'tx_bps': _number(row.get('tx_rate_bps')),
        })
    return {'available': True, 'sampled_at': now.isoformat(), 'connections': result,
            'remote_access_sessions': remote_sessions}
