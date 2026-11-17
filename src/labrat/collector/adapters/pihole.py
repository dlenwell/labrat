# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Read a Pi-hole's DNS statistics without changing it.

Pi-hole v6 (``/api``) authenticates with an application password: one
``POST /api/auth`` creates a session, reads use its ``X-FTL-SID`` header,
and ``DELETE /api/auth`` ends it so sessions do not pile up. Those two
calls only manage LabRat's own session; every other request is a ``GET``,
and blocking, lists, and settings are never touched. Pi-hole v5
(``/admin/api.php``) is read with its API token as a query parameter.

The adapter reports one observation per Pi-hole (``remote_id``
``pihole``) and :func:`pihole_snapshot` reads the statistics, which the
collector stores as the ``pihole`` telemetry key so frequent readings make
no audit events.

Used by: collector.__main__, integrations.
"""

from collections.abc import Mapping
from datetime import datetime, timezone
import ipaddress
from typing import Any
from urllib.parse import urlsplit

import requests

from labrat.coerce import number, text
from labrat.collector.contracts import DeviceObservation, DiscoveryBatch
from labrat.collector.http import controller_url, required_string, tls_verification

#: Seconds before one Pi-hole request gives up.
TIMEOUT_SECONDS = 10




class PiholeAdapter:
    """Read-only Pi-hole statistics over its HTTP API."""

    def __init__(self, session: requests.Session | None = None) -> None:
        """Create the adapter.

        Args:
            session: Optional HTTP session for tests or reuse.
        """
        self.session = session if session is not None else requests.Session()
        #: Readings from the last poll, by observation and telemetry key; the
        #: runner stores them with ``record_telemetry`` (see ``poll``).
        self.last_telemetry: dict[str, dict[str, Any]] = {}

    # -- transport ------------------------------------------------------

    def _request(self, method: str, url: str, settings: Mapping[str, Any], **kwargs: Any) -> Any:
        """Make one bounded request and decode its JSON.

        Args:
            method: HTTP method.
            url: Absolute URL.
            settings: The source's private settings (for TLS).
            **kwargs: Extra ``requests`` arguments.

        Returns:
            Any: The decoded JSON, or None for an empty body.

        Raises:
            requests.RequestException: The request failed or returned an error.
        """
        response = self.session.request(method, url, timeout=TIMEOUT_SECONDS, verify=tls_verification(settings),
                                        allow_redirects=False, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    def _sign_in(self, root: str, settings: Mapping[str, Any]) -> str:
        """Create a v6 API session with the application password.

        Args:
            root: The Pi-hole's root URL.
            settings: Private settings holding ``password``.

        Returns:
            str: The session ID.

        Raises:
            PermissionError: The password was refused.
        """
        payload = self._request("POST", f"{root}/api/auth", settings,
                                json={"password": required_string(settings, "password")})
        session = payload.get("session") if isinstance(payload, dict) else None
        if not isinstance(session, dict) or not session.get("valid") or not session.get("sid"):
            raise PermissionError("Pi-hole refused the application password")
        return str(session["sid"])

    def _sign_out(self, root: str, settings: Mapping[str, Any], sid: str) -> None:
        """End the session so Pi-hole's session slots are not used up.

        Args:
            root: The Pi-hole's root URL.
            settings: The private settings, for TLS verification.
            sid: The session ID to end.
        """
        try:
            self._request("DELETE", f"{root}/api/auth", settings, headers={"X-FTL-SID": sid})
        except requests.RequestException:
            pass  # The session expires on its own.

    # -- reads ------------------------------------------------------------

    def snapshot(self, settings: Mapping[str, Any]) -> dict:
        """Read the Pi-hole's statistics (v6, else v5).

        Args:
            settings: ``url``, ``password`` (v6 application password or v5
                API token), and TLS settings.

        Returns:
            dict: ``version``, ``api``, ``blocking``, ``queries`` (total,
                blocked, percent, cached, forwarded, clients, domains on
                lists), ``history`` (time, total, blocked), and top
                permitted and blocked domains and top clients.

        Raises:
            requests.RequestException: The Pi-hole cannot be reached.
            PermissionError: The credential was refused.
        """
        root = controller_url(settings)
        try:
            self._request("GET", f"{root}/api/auth", settings)
        except requests.RequestException:
            return self._snapshot_v5(root, settings)
        sid = self._sign_in(root, settings)
        try:
            headers = {"X-FTL-SID": sid, "Accept": "application/json"}

            def get(path: str) -> Any:
                return self._request("GET", f"{root}/api{path}", settings, headers=headers)

            summary = get("/stats/summary") or {}
            queries = summary.get("queries") if isinstance(summary.get("queries"), dict) else {}
            gravity = summary.get("gravity") if isinstance(summary.get("gravity"), dict) else {}
            clients = summary.get("clients") if isinstance(summary.get("clients"), dict) else {}
            history = (get("/history") or {}).get("history") or []
            top = lambda path, key: [  # noqa: E731 - small local reader
                {"name": text(row.get(key), 200), "count": number(row.get("count"))}
                for row in ((get(path) or {}).get("domains" if key == "domain" else "clients") or [])[:10]
                if isinstance(row, dict)]
            version = get("/info/version") or {}
            blocking = get("/dns/blocking") or {}
            core = ((version.get("version") or {}).get("core") or {}).get("local") or {}
            return {
                "api": "v6", "version": text(core.get("version"), 40),
                "blocking": text(blocking.get("blocking"), 20),
                "queries": {
                    "total": number(queries.get("total")), "blocked": number(queries.get("blocked")),
                    "percent_blocked": number(queries.get("percent_blocked")), "cached": number(queries.get("cached")),
                    "forwarded": number(queries.get("forwarded")), "unique_domains": number(queries.get("unique_domains")),
                    "clients": number(clients.get("active")), "domains_on_lists": number(gravity.get("domains_being_blocked")),
                },
                "history": [{"t": number(row.get("timestamp")), "total": number(row.get("total")), "blocked": number(row.get("blocked"))}
                            for row in history[-144:] if isinstance(row, dict)],
                "top_domains": top("/stats/top_domains?count=10", "domain"),
                "top_blocked": top("/stats/top_domains?blocked=true&count=10", "domain"),
                "top_clients": top("/stats/top_clients?count=10", "name"),
            }
        finally:
            self._sign_out(root, settings, sid)

    def _snapshot_v5(self, root: str, settings: Mapping[str, Any]) -> dict:
        """Read a v5 Pi-hole's summary with its API token.

        Args:
            root: The Pi-hole's root URL.
            settings: Private settings holding ``password`` (the API token).

        Returns:
            dict: The same shape as v6, with fewer fields filled.

        Raises:
            PermissionError: When Pi-hole refuses the API token.
        """
        token = required_string(settings, "password")
        summary = self._request("GET", f"{root}/admin/api.php", settings, params={"summaryRaw": "", "auth": token}) or {}
        if not isinstance(summary, dict) or "dns_queries_today" not in summary:
            raise PermissionError("Pi-hole refused the API token")
        return {
            "api": "v5", "version": None, "blocking": text(summary.get("status"), 20),
            "queries": {
                "total": number(summary.get("dns_queries_today")), "blocked": number(summary.get("ads_blocked_today")),
                "percent_blocked": number(summary.get("ads_percentage_today")), "cached": number(summary.get("queries_cached")),
                "forwarded": number(summary.get("queries_forwarded")), "unique_domains": number(summary.get("unique_domains")),
                "clients": number(summary.get("unique_clients")), "domains_on_lists": number(summary.get("domains_being_blocked")),
            },
            "history": [], "top_domains": [], "top_blocked": [], "top_clients": [],
        }

    def poll(self, settings: Mapping[str, Any]) -> DiscoveryBatch:
        """Report the Pi-hole as one observation after a successful read.

        Args:
            settings: The source's private settings.

        Returns:
            DiscoveryBatch: One ``server`` observation identified by address.
        """
        root = controller_url(settings)
        self.last_telemetry = {}
        snapshot = self.snapshot(settings)
        self.last_telemetry = {"pihole": {"pihole": {**snapshot, "sampled_at": datetime.now(timezone.utc).isoformat()}}}
        host = urlsplit(root).hostname or root
        identifiers: dict[str, Any] = {"hostname": host}
        try:
            identifiers["management_ips"] = [str(ipaddress.ip_address(host))]
        except ValueError:
            pass
        return DiscoveryBatch(devices=(DeviceObservation(
            remote_id="pihole", name=f"Pi-hole {host}", category="server", identifiers=identifiers,
            details={"platform": "pihole", "api": snapshot["api"], "version": snapshot["version"]},
        ),))

    def test_connection(self, settings: Mapping[str, Any]) -> tuple[bool, str]:
        """Check the address and credential with one read, for Settings.

        Args:
            settings: Submitted settings (never stored by this call).

        Returns:
            tuple[bool, str]: Success and a safe message.
        """
        try:
            snapshot = self.snapshot(settings)
        except PermissionError:
            return False, "Pi-hole refused the application password or API token."
        except requests.exceptions.SSLError:
            return False, "Certificate verification failed. Pi-hole's own certificate is self-signed: add its CA or adjust certificate checking."
        except requests.exceptions.RequestException:
            return False, "The Pi-hole could not be reached. Check its address, and that the firewall allows LabRat to reach it."
        except (ValueError, OSError):
            return False, "The address did not answer like a Pi-hole."
        return True, f"Connection successful. Pi-hole {snapshot['api']} answered with today's statistics."
