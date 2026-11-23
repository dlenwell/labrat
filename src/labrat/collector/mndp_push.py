# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Listen for MikroTik announcements here and report them to a LabRat elsewhere.

The standalone MNDP listener: run it on a host (or a host-network pod) on
the segment the MikroTiks announce on, and LabRat itself needs no layer-2
access. It listens like the collector's own MNDP adapter (never sending a
packet), then posts what it heard to ``/api/v1/machine/mndp`` with an API
token that has the ``observations:mndp`` permission (Settings → API
tokens). It needs no database and no LabRat config::

    LABRAT_API_TOKEN=labrat_… python -m labrat.collector.mndp_push --url https://labrat.example.lan

Options: ``--token-file`` instead of the environment variable,
``--listen-seconds`` (5 to 300, default 65), ``--interval`` between
reports (default 60, or the listen time when that is longer), ``--once``, and ``--ca-file`` for a private CA.
TLS certificates are always verified.
"""

import argparse
import json
import logging
import os
from pathlib import Path
import ssl
import sys
import time
from typing import Any, Callable
import urllib.error
import urllib.request

from labrat.collector.adapters.mndp import (
    DEFAULT_LISTEN_SECONDS, MAX_LISTEN_SECONDS, MIN_LISTEN_SECONDS, listen_for_packets, decode_packet,
)

ENDPOINT = "/api/v1/machine/mndp"
"""Where reports go, under the LabRat base URL."""

DEFAULT_INTERVAL = 60
"""Seconds between reports unless the listen window is longer."""

REPORT_FIELDS = ("identity", "board", "version", "platform", "software_id", "interface", "mac", "ipv4", "uptime_seconds")
"""The decoded fields the endpoint accepts."""


def announcements(packets: list[bytes]) -> list[dict[str, Any]]:
    """Decode heard packets into the fields the endpoint takes.

    Args:
        packets: Raw MNDP payloads.

    Returns:
        list[dict[str, Any]]: One object per packet that names its identity.
    """
    decoded = []
    for packet in packets:
        fields = decode_packet(packet)
        if fields.get("identity"):
            decoded.append({key: fields[key] for key in REPORT_FIELDS if fields.get(key) is not None})
    return decoded


def report(url: str, token: str, items: list[dict[str, Any]], interval: int, context: ssl.SSLContext | None) -> dict[str, Any]:
    """Post one listen window's announcements.

    Args:
        url: LabRat's base URL.
        token: The API token.
        items: Decoded announcements.
        interval: Seconds between reports, so LabRat can tell when one is late.
        context: TLS settings for HTTPS.

    Returns:
        dict[str, Any]: LabRat's answer (counts recorded).
    """
    body = json.dumps({"announcements": items, "interval_seconds": interval}).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + ENDPOINT, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        return json.loads(response.read().decode("utf-8"))


def read_token(token_file: str | None) -> str:
    """Read the API token from a file or ``LABRAT_API_TOKEN``.

    Args:
        token_file: A file holding only the token, or None.

    Returns:
        str: The token, stripped.
    """
    if token_file:
        return Path(token_file).read_text(encoding="utf-8").strip()
    return os.environ.get("LABRAT_API_TOKEN", "").strip()


def run(args: argparse.Namespace, listen: Callable[[float], list[bytes]] = listen_for_packets, post: Callable[..., dict[str, Any]] = report) -> int:
    """Listen and report until stopped (or once).

    Args:
        args: Parsed command-line options.
        listen: Collects raw packets for a number of seconds.
        post: Sends one report.

    Returns:
        int: The exit status: 0, or 1 when ``--once`` could not report.
    """
    token = read_token(args.token_file)
    if not token:
        logging.error("Set LABRAT_API_TOKEN or --token-file to an API token with the observations:mndp permission")
        return 2
    context = ssl.create_default_context(cafile=args.ca_file) if args.url.startswith("https:") else None
    while True:
        started = time.monotonic()
        items = announcements(listen(args.listen_seconds))
        try:
            answer = post(args.url, token, items, args.interval, context)
            logging.info("Reported %s announcement(s); LabRat recorded %s device(s)", len(items), answer.get("devices"))
            failed = False
        except urllib.error.HTTPError as exc:
            # Only the status: a body could echo request details.
            logging.warning("LabRat refused the report (HTTP %s)", exc.code)
            failed = True
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logging.warning("Could not reach LabRat (%s)", type(exc).__name__)
            failed = True
        if args.once:
            return 1 if failed else 0
        time.sleep(max(0.0, args.interval - (time.monotonic() - started)))


def main(argv: list[str] | None = None) -> int:
    """Parse options and run the listener.

    Args:
        argv: Arguments, or None for ``sys.argv``.

    Returns:
        int: The exit status.
    """
    parser = argparse.ArgumentParser(description="Report MikroTik MNDP announcements to LabRat over its API")
    parser.add_argument("--url", required=True, help="LabRat's base URL, such as https://labrat.example.lan")
    parser.add_argument("--token-file", help="File holding the API token (default: LABRAT_API_TOKEN)")
    parser.add_argument("--listen-seconds", type=int, default=DEFAULT_LISTEN_SECONDS, help="Seconds to listen per report (5 to 300)")
    parser.add_argument("--interval", type=int, default=None,
                        help="Seconds between reports, at least the listen time (default: 60, or the listen time if longer)")
    parser.add_argument("--ca-file", help="CA bundle for a LabRat with a private certificate")
    parser.add_argument("--once", action="store_true", help="Listen and report once, then exit")
    args = parser.parse_args(argv)
    if not args.url.startswith(("https://", "http://")):
        parser.error("--url must start with https:// (or http:// on a trusted network)")
    if not MIN_LISTEN_SECONDS <= args.listen_seconds <= MAX_LISTEN_SECONDS:
        parser.error("--listen-seconds must be from 5 to 300")
    if args.interval is None:
        args.interval = max(DEFAULT_INTERVAL, args.listen_seconds)
    if not args.listen_seconds <= args.interval <= 86400:
        parser.error("--interval must be at least --listen-seconds and at most 86400")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
