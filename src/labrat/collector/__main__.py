# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Start the standalone LabRat collector process from ``python -m``.

The collector exposes no HTTP routes and needs no Flask application.
Configured Proxmox and UniFi sources are registered automatically, while
credentials remain in local TOML and never enter SQLite.
"""

import argparse
import logging
import time

from labrat.collector.adapters.mndp import MndpAdapter
from labrat.collector.adapters.nut import NutAdapter
from labrat.collector.adapters.pihole import PiholeAdapter
from labrat.collector.adapters.proxmox import ProxmoxAdapter
from labrat.collector.adapters.unifi import UniFiAdapter
from labrat.collector.access_collection import AccessCollector
from labrat.collector.proxmox_live import live_poll_sources, poll_proxmox_live_once
from labrat.collector.runner import poll_once, poll_ups_once, poll_wan_once, ups_poll_sources, wan_poll_sources
from labrat.collector.sources import configured_sources, sync_configured_sources
from labrat.discovery.models import DiscoveredDevice, DiscoverySource
from labrat import models as inventory_models
from sqlalchemy import func, select
from labrat.config import load_local_config
from labrat.database import make_engine, make_session_factory


def refreshed_config(previous: dict) -> dict:
    """Reload integration settings before a full scan, keeping valid prior state.

    Invalid or unreadable edits are reported generically; no exception or
    configuration content is logged. The running process keeps its original
    database destination and scan interval until explicitly restarted.

    Args:
        previous: Last valid process configuration, containing private settings.

    Returns:
        dict: New source settings, or the previous mapping if reload fails.
    """
    try:
        config = load_local_config()
        configured_sources(config)
        if config.get("database", {}) != previous.get("database", {}):
            logging.warning("Database configuration changed; restart the collector to apply it")
            return previous
        return config
    except (OSError, ValueError):
        logging.warning("Configuration reload failed; keeping the last valid settings")
        return previous


def main() -> None:
    """Run one discovery cycle or repeat at a configured interval.

    The ``--once`` switch is useful for a developer verifying the daemon
    separately from the API process. Press Ctrl-C to stop continuous polling.
    """
    parser = argparse.ArgumentParser(description="LabRat infrastructure collector")
    parser.add_argument("--once", action="store_true", help="Run exactly one collection cycle")
    parser.add_argument("--interval", type=int, default=None, help="Seconds between cycles")
    parser.add_argument("--list", action="store_true", help="List stored physical discoveries without polling")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_local_config()
    collector_settings = config.get("collector", {})
    if not isinstance(collector_settings, dict):
        parser.error("[collector] must be a TOML table")
    interval = args.interval if args.interval is not None else collector_settings.get("interval_seconds", 60)
    if type(interval) is not int or not 5 <= interval <= 86400:
        parser.error("Collector interval must be an integer from 5 to 86400 seconds")
    engine = make_engine(config)
    sessions = make_session_factory(engine)
    adapters = {"mndp": MndpAdapter(), "nut": NutAdapter(), "pihole": PiholeAdapter(), "proxmox": ProxmoxAdapter(), "unifi": UniFiAdapter()}
    access = None
    try:
        if args.list:
            with sessions() as db:
                summary = db.execute(
                    select(DiscoverySource.name, func.count(DiscoveredDevice.id))
                    .outerjoin(DiscoveredDevice, DiscoveredDevice.source_id == DiscoverySource.id)
                    .group_by(DiscoverySource.id)
                    .order_by(DiscoverySource.name)
                ).all()
                devices = db.execute(
                    select(DiscoverySource.name, DiscoveredDevice.category, DiscoveredDevice.name)
                    .join(DiscoverySource, DiscoveredDevice.source_id == DiscoverySource.id)
                    .order_by(DiscoverySource.name, DiscoveredDevice.name)
                ).all()
            for name, count in summary:
                print(f"{name}: {count} physical device(s)")
            for source_name, category, device_name in devices:
                print(f"  {source_name} [{category}] {device_name}")
            return
        access = AccessCollector()
        while True:
            config = refreshed_config(config)
            created = sync_configured_sources(sessions, config)
            if created:
                logging.info("Registered %s discovery source(s) from local configuration", created)
            completed = poll_once(sessions, config, adapters, None if args.once else interval)
            logging.info("Collector cycle complete: %s source(s) polled", completed)
            access.poll(sessions, config)
            if args.once:
                break
            # Between full scans, read only fast-changing telemetry on shorter
            # intervals: gateway uplinks (UniFi), UPS readings (NUT), and
            # node, guest, and task state (Proxmox).
            next_scan = time.monotonic() + interval
            fast = [
                (source_id, settings, every, lambda s_id, s: poll_wan_once(sessions, s_id, s, adapters["unifi"]))
                for source_id, settings, every in wan_poll_sources(sessions, config)
            ] + [
                (source_id, settings, every, lambda s_id, s: poll_ups_once(sessions, s_id, s, adapters["nut"]))
                for source_id, settings, every in ups_poll_sources(sessions, config)
            ] + [
                (source_id, settings, every, lambda s_id, s: poll_proxmox_live_once(sessions, s_id, s, adapters["proxmox"]))
                for source_id, settings, every in live_poll_sources(sessions, config)
            ]
            next_due = [time.monotonic() + every for _, _, every, _ in fast]
            while (remaining := next_scan - time.monotonic()) > 0:
                due = min(next_due, default=next_scan)
                time.sleep(max(0.0, min(remaining, due - time.monotonic())))
                for index, (source_id, settings, every, poll) in enumerate(fast):
                    if time.monotonic() >= next_due[index] and time.monotonic() < next_scan:
                        poll(source_id, settings)
                        next_due[index] = time.monotonic() + every
    except KeyboardInterrupt:
        logging.info("Collector stopped")
    finally:
        if access is not None:
            access.close()
        engine.dispose()


if __name__ == "__main__":
    main()
