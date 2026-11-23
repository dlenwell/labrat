# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Start the standalone LabRat collector process from ``python -m``.

The collector exposes no HTTP routes and needs no Flask application.
Configured Proxmox and UniFi sources are registered automatically, while
credentials remain in local TOML and never enter SQLite.

Before it polls, the collector waits until the database schema is at
Alembic's head (the web app may be migrating it), then takes the
``collector`` lease (``labrat/instance.py``) so two collectors never poll
into one database. When another collector holds a live lease,
``--on-conflict`` (or ``[collector].on_conflict``) decides: ``wait`` for it
to stop (the default), ``take-over`` (the other one stops at its next
renewal), or ``refuse`` to start. ``--once`` without ``--on-conflict`` runs
its one cycle beside a running collector, as a "scan now" does.
"""

import argparse
import logging
import sys
import threading
import time

from labrat.collector.adapters.mndp import MndpAdapter
from labrat.collector.adapters.nut import NutAdapter
from labrat.collector.adapters.opnsense import OpnsenseAdapter
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
from labrat import app_version, instance

CONFLICT_POLICIES = ("wait", "take-over", "refuse")
"""What a collector does when another one holds the lease."""

ONCE_POLICY = "alongside"
"""``--once`` without ``--on-conflict``: one cycle beside the holder, unleased.

A one-off scan ("scan now" beside a running collector) is short and was
always allowed; waiting for a lease the long-running collector never
gives up would hang it.
"""

SCHEMA_WAIT_SECONDS = 5
"""How often the collector checks whether a migration has finished."""

REFUSED_EXIT_STATUS = 3
"""Exit status when the collector refuses to start beside another one."""


LeaseLost = instance.LeaseLost
"""Another collector took this one's lease over (``labrat.instance``)."""


def wait_for_schema(engine) -> None:
    """Wait until the database schema is at Alembic's head.

    The web app migrates the database while it shows its holding page;
    polling before that finishes would write into old tables. An install
    without Alembic scripts runs as it is.

    Args:
        engine: The database engine.
    """
    told = 0.0
    while (state := instance.schema_state(engine)) is not None and state.behind:
        if time.monotonic() - told > 60:
            logging.info(
                "Waiting for the database migration (at %s, needs %s)",
                state.current or "no revision", state.head,
            )
            told = time.monotonic()
        time.sleep(SCHEMA_WAIT_SECONDS)


class CollectorLease:
    """Take the collector lease and keep it renewed from a background thread.

    Args:
        engine: The database engine.
        policy: ``wait``, ``take-over``, or ``refuse``.
    """

    def __init__(self, engine, policy: str) -> None:
        """Remember where the lease lives and how to handle a conflict.

        Args:
            engine: The database engine.
            policy: One of :data:`CONFLICT_POLICIES`.
        """
        self.engine = engine
        self.policy = policy
        self.holder = instance.holder_id()
        #: True when this run shares the database with the holder for one
        #: cycle (``--once``) instead of holding the lease.
        self.shared = False
        self.lost = threading.Event()
        self._stop = threading.Event()
        self.version = app_version()

    def acquire(self) -> bool:
        """Take the lease, waiting or taking over as the policy says.

        Returns:
            bool: True once held; False when the policy is ``refuse`` and
                another collector holds it.
        """
        told = 0.0
        while (other := instance.acquire(self.engine, "collector", self.holder, self.version)) is not None:
            where = f"{other.hostname} (process {other.pid}, version {other.version or 'unknown'})"
            if self.policy == ONCE_POLICY:
                logging.warning("Another collector is running on %s; running this one cycle beside it", where)
                self.shared = True
                return True
            if self.policy == "refuse":
                logging.error("Another collector is running on %s; refusing to start. "
                              "Use --on-conflict wait or take-over to change that.", where)
                return False
            if self.policy == "take-over":
                instance.take_over(self.engine, self.holder, self.version, role="collector", release=())
                logging.warning("Took the collector lease over from %s", where)
                break
            if time.monotonic() - told > 60:
                logging.warning("Another collector is running on %s; waiting for it to stop "
                                "(--on-conflict take-over replaces it)", where)
                told = time.monotonic()
            time.sleep(instance.RENEW_SECONDS)
        threading.Thread(target=self._renew, name="labrat-collector-lease", daemon=True).start()
        return True

    def _renew(self) -> None:
        """Renew every few seconds until stopped or replaced."""
        while not self._stop.wait(instance.RENEW_SECONDS):
            try:
                current = instance.renew(self.engine, "collector", self.holder)
            except Exception:  # A busy database; try at the next renewal.
                logging.warning("Could not renew the collector lease; trying again")
                continue
            if current is not None:
                self.lost.set()
                return

    def check(self) -> None:
        """Stop the collector if another one has taken over.

        Raises:
            LeaseLost: The lease now belongs to someone else.
        """
        if self.lost.is_set():
            raise LeaseLost

    def release(self) -> None:
        """Stop renewing and give the lease up, unless it was taken or never held."""
        self._stop.set()
        if not self.lost.is_set() and not self.shared:
            try:
                instance.release(self.engine, "collector", self.holder)
            except Exception:  # Exiting anyway.
                logging.warning("Could not release the collector lease")


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
    parser.add_argument(
        "--on-conflict", choices=CONFLICT_POLICIES, default=None,
        help="When another collector holds the lease: wait (default), take-over, or refuse",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_local_config()
    collector_settings = config.get("collector", {})
    if not isinstance(collector_settings, dict):
        parser.error("[collector] must be a TOML table")
    interval = args.interval if args.interval is not None else collector_settings.get("interval_seconds", 60)
    if type(interval) is not int or not 5 <= interval <= 86400:
        parser.error("Collector interval must be an integer from 5 to 86400 seconds")
    configured = collector_settings.get("on_conflict", "wait")
    if configured not in CONFLICT_POLICIES:
        parser.error("[collector].on_conflict must be wait, take-over, or refuse")
    policy = args.on_conflict or (ONCE_POLICY if args.once else configured)
    engine = make_engine(config)
    sessions = make_session_factory(engine)
    adapters = {"mndp": MndpAdapter(), "nut": NutAdapter(), "opnsense": OpnsenseAdapter(), "pihole": PiholeAdapter(),
                "proxmox": ProxmoxAdapter(), "unifi": UniFiAdapter()}
    access = None
    lease = None
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
        wait_for_schema(engine)
        if instance.has_lease_table(engine):
            lease = CollectorLease(engine, policy)
            if not lease.acquire():
                sys.exit(REFUSED_EXIT_STATUS)
            if not lease.shared:
                # A cycle in flight when another collector takes over must
                # not land its older observations after the newer ones.
                instance.fence_writes(sessions, "collector", lease.holder)
        access = AccessCollector()
        while True:
            if lease is not None:
                lease.check()
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
                if lease is not None:
                    lease.check()
                due = min(next_due, default=next_scan)
                # Wake at least once per renewal, so a takeover stops this
                # collector promptly.
                time.sleep(max(0.0, min(remaining, due - time.monotonic(), instance.RENEW_SECONDS)))
                for index, (source_id, settings, every, poll) in enumerate(fast):
                    if time.monotonic() >= next_due[index] and time.monotonic() < next_scan:
                        poll(source_id, settings)
                        next_due[index] = time.monotonic() + every
    except KeyboardInterrupt:
        logging.info("Collector stopped")
    except LeaseLost:
        logging.warning("Another collector took the lease over; this one is stopping")
        sys.exit(REFUSED_EXIT_STATUS)
    finally:
        if lease is not None:
            lease.release()
        if access is not None:
            access.close()
        engine.dispose()


if __name__ == "__main__":
    main()
