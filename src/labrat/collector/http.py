# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Read-only HTTPS helpers shared by local infrastructure adapters.

The collector uses GET requests with bounded timeouts and TLS verification by
default. API credentials are placed in HTTP headers, never URLs or persisted
observation data. Redirects are disabled so a controller cannot forward those
headers to a different host. Exceptions are sanitized by the collector runner.

Used by: collector.access_collection, collector.adapters.opnsense,
collector.adapters.pihole, collector.adapters.proxmox,
collector.adapters.unifi, collector.client_links, collector.clients,
collector.nut_wired, collector.proxmox_clients, collector.proxmox_cluster,
collector.unifi_network, collector.wireless_clients, integrations.
"""

from collections.abc import Callable, Mapping
import logging
from typing import Any
from urllib.parse import urlsplit

import requests


def controller_url(settings: Mapping[str, Any]) -> str:
    """Validate and normalize one configured controller URL.

    Args:
        settings: Integration settings containing an HTTPS ``url``.

    Returns:
        str: Root URL without a trailing slash.

    Raises:
        ValueError: The controller URL is missing, non-HTTPS, or contains
            credentials, a query, fragment, or non-root path.
    """
    raw = settings.get("url")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Integration requires a nonempty HTTPS url")
    parts = urlsplit(raw.strip())
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise ValueError("Integration url must be an HTTPS controller root")
    return raw.strip().rstrip("/")


#: Where each kind of UniFi controller serves the Network application's
#: APIs: a UniFi OS console or UniFi OS Server proxies them under
#: ``/proxy/network``; a self-hosted Network application serves them at its
#: root (usually on port 8443).
UNIFI_NETWORK_PREFIXES = {"unifi_os": "/proxy/network", "network_application": ""}


def unifi_network_root(settings: Mapping[str, Any]) -> str:
    """Return the root URL of a UniFi controller's Network application APIs.

    Args:
        settings: UniFi integration settings with ``url`` and an optional
            ``controller_type`` (``unifi_os``, the default, or
            ``network_application``).

    Returns:
        str: The controller root plus the Network application prefix, with
            no trailing slash. Append ``/integration/v1``, ``/api/s/<site>``,
            or ``/v2/api`` paths to it.

    Raises:
        ValueError: The controller URL is invalid or the controller type is
            not one of the known kinds.
    """
    kind = settings.get("controller_type", "unifi_os")
    if kind not in UNIFI_NETWORK_PREFIXES:
        raise ValueError("Integration controller_type must be unifi_os or network_application")
    return controller_url(settings) + UNIFI_NETWORK_PREFIXES[kind]


def required_string(settings: Mapping[str, Any], field: str) -> str:
    """Retrieve a required configuration string without revealing its value.

    Args:
        settings: One integration configuration section.
        field: Required TOML key name.

    Returns:
        str: Configured nonempty value, with surrounding whitespace removed.

    Raises:
        ValueError: The selected key is absent or not a nonempty string.
    """
    value = settings.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Integration requires a nonempty {field}")
    return value.strip()


def optional_read(errors: dict, source: str, part: str, read: Callable[[], Any], default: Any = None) -> Any:
    """Run one optional read, recording a generic failure instead of raising.

    Telemetry collectors read many independent parts (a node's ZFS pools,
    a site's Wi-Fi); one part failing must not fail the rest, and the
    failure is kept as a label, never the exception's text.

    Args:
        errors: Map of part name to a short failure label, updated in place.
        source: The source's name for the log line, such as ``Proxmox``.
        part: Name of the part being read.
        read: Callable that performs the read.
        default: Value returned when the read fails.

    Returns:
        Any: The read's result, or ``default``.
    """
    try:
        return read()
    except Exception as exc:  # Optional telemetry must never fail collection.
        logging.getLogger(__name__).info("%s %s unavailable (%s)", source, part, type(exc).__name__)
        errors[part] = "unavailable"
        return default


def tls_verification(settings: Mapping[str, Any]) -> bool | str:
    """Select TLS certificate validation or a custom CA bundle.

    Args:
        settings: Configuration with optional ``verify_tls`` and ``ca_bundle``.

    Returns:
        bool | str: Requests-compatible verification setting.

    Raises:
        ValueError: Conflicting or unsupported TLS settings are supplied.

    Note:
        Disabling verification is intended only for temporary local diagnosis.
        Prefer a CA bundle or properly issued controller certificates.
    """
    verify = settings.get("verify_tls", True)
    bundle = settings.get("ca_bundle")
    if type(verify) is not bool or (bundle is not None and (
        not isinstance(bundle, str) or not bundle.strip() or not verify
    )):
        raise ValueError("Invalid TLS verification options")
    return bundle.strip() if bundle is not None else verify


class JsonGetClient:
    """Issue credentialed JSON GETs with reusable HTTP connections.

    Instances own a requests session, allowing a long-running collector to
    reuse sockets across polling cycles. The default TLS behavior is secure.
    Test suites may inject a minimal fake session to inspect requests without
    contacting live controllers.
    """

    def __init__(self, session: requests.Session | None = None) -> None:
        """Prepare the HTTP client and optionally accept a test session.

        Args:
            session: Requests-compatible session, or None to create one.
        """
        self.session = session if session is not None else requests.Session()

    def get_json(
        self,
        url: str,
        headers: Mapping[str, str],
        verify: bool | str,
    ) -> Any:
        """Fetch and decode a single JSON response.

        Args:
            url: Complete HTTPS request URL, without embedded credentials.
            headers: HTTP request headers carrying the integration API token.
            verify: Enable TLS verification or select a CA bundle.

        Returns:
            Any: JSON object returned by the external integration.

        Raises:
            requests.RequestException: The request fails or times out.
            ValueError: The response body is not valid JSON.

        Note:
            All callers use GET only. Never log the response or exception
            text, because they might contain private infrastructure details.
        """
        response = self.session.get(
            url, headers=dict(headers), timeout=(3.0, 15.0),
            allow_redirects=False, verify=verify,
        )
        response.raise_for_status()
        return response.json()
