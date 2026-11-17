# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Discover NUT servers and the UPS units they report, read-only.

Enabled by listing ``"nut"`` in ``[scan] probes``; only hosts inside
``[scan] ranges`` are ever contacted (see ``collector/scan.py``). Each scan:

1. finds hosts accepting TCP 3493 in the allowed ranges (at most hourly by
   default; confirmed servers are remembered in memory between scans);
2. confirms each candidate with NUT's own read-only ``LIST UPS``; an open
   port alone records nothing;
3. records the computer running the NUT server as a ``server`` device, and
   each UPS it reports as its own ``ups`` device.

UPS readings (status, battery, runtime, load, power, voltages) are telemetry
under the UPS observation's ``ups`` detail, so frequent readings never count
as device changes. ``poll_ups`` re-reads known servers between scans.

A UPS on a USB driver is recorded as USB-attached to the NUT server's computer,
because NUT drivers run on the server they report to. Drivers that can use
USB or a serial line (such as ``nutdrv_qx``) count as USB when NUT reports
USB identity (``ups.vendorid``/``ups.productid``) or the USB ``port = auto``.
A ``dummy-ups`` unit relays data from elsewhere and is marked ``relayed``,
with no claim about its physical connection.
"""

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
import logging
import socket
import time
from typing import Any

from labrat.coerce import number
from labrat.collector.contracts import DeviceObservation, DiscoveryBatch
from labrat.collector.nut import NUT_PORT, NutError, read_server
from labrat.collector.scan import allowed_hosts, scan_tcp

_LOGGER = logging.getLogger(__name__)

#: Default and allowed seconds between rescans of the allowed ranges.
DEFAULT_RESCAN_SECONDS = 3600
MIN_RESCAN_SECONDS = 300
MAX_RESCAN_SECONDS = 86400

#: Seconds allowed for a reverse-DNS name lookup of a NUT server.
REVERSE_DNS_TIMEOUT_SECONDS = 1.0

#: NUT drivers that talk to their UPS over USB on the server's own computer.
USB_DRIVERS = frozenset({
    "usbhid-ups", "blazer_usb", "tripplite_usb", "bcmxcp_usb", "richcomm_usb",
    "riello_usb", "nutdrv_atcl_usb",
})

#: Drivers that relay another source's data rather than talk to hardware.
RELAY_DRIVERS = frozenset({"dummy-ups", "clone", "clone-outlet"})

#: NUT variables only a USB connection reports (the device's USB IDs).
_USB_IDENTITY_VARS = ("ups.vendorid", "ups.productid")


def attachment_for(driver: str | None, variables: Mapping[str, str]) -> str | None:
    """Tell how a UPS reaches its NUT server from its driver and variables.

    Args:
        driver: The ``driver.name`` value, if reported.
        variables: The UPS's NUT variables.

    Returns:
        str | None: ``relayed`` for a relay driver, ``usb`` for a USB-only
            driver or any other hardware driver with USB evidence, else None
            (serial, network, or unknown).
    """
    if driver in RELAY_DRIVERS:
        return "relayed"
    if driver in USB_DRIVERS:
        return "usb"
    port = variables.get("driver.parameter.port")
    if isinstance(port, str) and port.strip().lower() == "auto":
        return "usb"
    if _first(variables, _USB_IDENTITY_VARS):
        return "usb"
    return None

#: Descriptions NUT servers use when none was configured.
_PLACEHOLDER_DESCRIPTIONS = {"", "description unavailable", "unavailable"}

#: UPS identity details: our name -> NUT variables, first present wins.
_IDENTITY_VARS = {
    "manufacturer": ("device.mfr", "ups.mfr"),
    "model": ("device.model", "ups.model"),
    "serial": ("device.serial", "ups.serial"),
    "driver": ("driver.name",),
    "firmware": ("ups.firmware",),
}

#: Numeric readings kept as telemetry: our name -> NUT variables.
_NUMBER_VARS = {
    "battery_charge": ("battery.charge",),
    "battery_charge_low": ("battery.charge.low",),
    "battery_runtime_s": ("battery.runtime",),
    "battery_runtime_low_s": ("battery.runtime.low",),
    "battery_voltage": ("battery.voltage",),
    "load_percent": ("ups.load",),
    "real_power_w": ("ups.realpower",),
    "apparent_power_va": ("ups.power",),
    "real_power_nominal_w": ("ups.realpower.nominal",),
    "apparent_power_nominal_va": ("ups.power.nominal",),
    "input_voltage": ("input.voltage",),
    "input_frequency_hz": ("input.frequency",),
    "output_voltage": ("output.voltage",),
    "output_frequency_hz": ("output.frequency",),
    "temperature_c": ("ups.temperature", "battery.temperature"),
}

#: Text readings kept as telemetry (bounded in length).
_TEXT_VARS = {
    "status_raw": ("ups.status",),
    "alarm": ("ups.alarm",),
    "test_result": ("ups.test.result",),
}

#: ``ups.status`` tokens LabRat understands, with readable names.
STATUS_TOKENS = {
    "OL": "online", "OB": "on_battery", "LB": "low_battery", "HB": "high_battery",
    "RB": "replace_battery", "CHRG": "charging", "DISCHRG": "discharging",
    "BYPASS": "bypass", "CAL": "calibrating", "OFF": "off", "OVER": "overloaded",
    "TRIM": "trimming", "BOOST": "boosting", "FSD": "forced_shutdown", "ALARM": "alarm",
}


@dataclass(frozen=True)
class UpsReading:
    """Current readings for one UPS, from a fast poll between scans.

    Attributes:
        remote_id: The UPS observation's remote ID.
        telemetry: Value for the observation's ``ups`` detail.
    """

    remote_id: str
    telemetry: Mapping[str, Any]


def _first(variables: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    """Return the first present, non-empty variable among names.

    Args:
        variables: The UPS's NUT variables.
        names: Variable names to try, in order.

    Returns:
        str | None: The first value found, trimmed, or None.
    """
    for name in names:
        value = variables.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# NUT variables are text (labrat.coerce).
_number = partial(number, strings=True)


def ups_telemetry(variables: Mapping[str, str], sampled_at: datetime | None = None) -> dict[str, Any]:
    """Build the telemetry record for one UPS from its NUT variables.

    Args:
        variables: ``LIST VAR`` values for the UPS.
        sampled_at: Reading time; defaults to now in UTC.

    Returns:
        dict[str, Any]: Numbers (``battery_charge`` %, ``battery_runtime_s``,
            ``load_percent``, power, voltages, ...), the raw status and its
            recognized ``flags``, a summary ``state`` (``low_battery``,
            ``on_battery``, ``online``, ``off``, or ``unknown``), any alarm
            text, and ``sampled_at``.
    """
    record: dict[str, Any] = {key: _number(_first(variables, names)) for key, names in _NUMBER_VARS.items()}
    for key, names in _TEXT_VARS.items():
        value = _first(variables, names)
        record[key] = value[:200] if value else None
    tokens = (record["status_raw"] or "").split()
    record["flags"] = [STATUS_TOKENS[token] for token in tokens if token in STATUS_TOKENS]
    flags = set(record["flags"])
    record["state"] = ("low_battery" if "low_battery" in flags else "on_battery" if "on_battery" in flags
                       else "online" if "online" in flags else "off" if "off" in flags else "unknown")
    record["sampled_at"] = (sampled_at or datetime.now(timezone.utc)).isoformat()
    return record


def _ups_name(nut_name: str, description: str, identity: Mapping[str, str | None]) -> str:
    """Pick a readable device name: the description, else maker and model.

    Args:
        nut_name: The UPS's name on its NUT server.
        description: Its NUT description, which may be a placeholder.
        identity: Its manufacturer and model, when known.

    Returns:
        str: The name, at most 160 characters.
    """
    if description.strip().lower() not in _PLACEHOLDER_DESCRIPTIONS:
        return description.strip()[:160]
    model = " ".join(part for part in (identity.get("manufacturer"), identity.get("model")) if part)
    return (model or nut_name)[:160]


def scan_settings(settings: Mapping[str, Any]) -> tuple[list[str], int, int]:
    """Validate the NUT probe's settings.

    Args:
        settings: ``ranges`` (from ``[scan]``), optional ``port`` and
            ``rescan_interval_seconds``.

    Returns:
        tuple[list[str], int, int]: Allowed hosts, port, and rescan interval.

    Raises:
        ValueError: Missing ranges, bad port, or bad interval.
    """
    ranges = settings.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        raise ValueError("[scan] ranges must list at least one network to scan")
    port = settings.get("port", NUT_PORT)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("NUT port must be an integer from 1 to 65535")
    rescan = settings.get("rescan_interval_seconds", DEFAULT_RESCAN_SECONDS)
    if type(rescan) is not int or not MIN_RESCAN_SECONDS <= rescan <= MAX_RESCAN_SECONDS:
        raise ValueError("NUT rescan_interval_seconds must be an integer from 300 to 86400")
    return allowed_hosts(ranges), port, rescan


class NutAdapter:
    """Find NUT servers in the allowed ranges and report their UPS units."""

    def __init__(
        self,
        scan: Callable[..., list[str]] = scan_tcp,
        read: Callable[..., dict[str, dict]] = read_server,
        resolve: Callable[[str], str | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Use the real scanner, NUT client, and DNS unless tests supply them.

        Args:
            scan: TCP candidate scanner (``scan_tcp``).
            read: NUT server reader (``read_server``).
            resolve: Reverse-DNS lookup returning a host name or None.
            clock: Monotonic clock in seconds.
        """
        self.scan = scan
        self.read = read
        self.resolve = resolve or self._reverse_dns
        self.clock = clock
        # (hosts, port) -> (last scan time, confirmed server addresses).
        self._servers: dict[tuple[tuple[str, ...], int], tuple[float, list[str]]] = {}
        self._names: dict[str, str | None] = {}
        self._dns = ThreadPoolExecutor(max_workers=2, thread_name_prefix="labrat-dns")

    def _reverse_dns(self, address: str) -> str | None:
        """Look up a server's name, giving up after a second.

        Args:
            address: The server's IP address.

        Returns:
            str | None: Its host name, or None when the lookup fails or times out.
        """
        future = self._dns.submit(socket.gethostbyaddr, address)
        try:
            return future.result(timeout=REVERSE_DNS_TIMEOUT_SECONDS)[0]
        except (OSError, FutureTimeout):
            return None

    def servers(self, settings: Mapping[str, Any]) -> tuple[list[str], int]:
        """Return confirmed NUT servers, rescanning the ranges when due.

        A candidate counts only after ``LIST UPS`` succeeds. Between rescans
        the remembered servers are used without scanning.

        Args:
            settings: The probe's settings.

        Returns:
            tuple[list[str], int]: Server addresses and the port.
        """
        hosts, port, rescan = scan_settings(settings)
        key = (tuple(hosts), port)
        cached = self._servers.get(key)
        if cached is not None and self.clock() - cached[0] < rescan:
            return cached[1], port
        confirmed = []
        for host in self.scan(hosts, port):
            try:
                self.read(host, port)
            except (OSError, NutError):
                continue
            confirmed.append(host)
        _LOGGER.info("NUT scan of %s address(es) found %s server(s)", len(hosts), len(confirmed))
        self._servers[key] = (self.clock(), confirmed)
        return confirmed, port

    def poll(self, settings: Mapping[str, Any]) -> DiscoveryBatch:
        """Report each NUT server's computer and each UPS it serves.

        Args:
            settings: The probe's settings.

        Returns:
            DiscoveryBatch: One ``server`` device per reachable NUT server and
                one ``ups`` device per UPS, with readings as telemetry.

        Raises:
            ValueError: Invalid settings.
        """
        servers, port = self.servers(settings)
        devices: list[DeviceObservation] = []
        for host in servers:
            try:
                units = self.read(host, port)
            except (OSError, NutError) as exc:
                _LOGGER.warning("NUT server unreadable (%s)", type(exc).__name__)
                continue
            if host not in self._names:
                self._names[host] = self.resolve(host)
            server_id = f"nut-server:{host}:{port}"
            devices.append(DeviceObservation(
                remote_id=server_id,
                name=(self._names[host] or f"NUT server {host}")[:160],
                category="server",
                identifiers={"management_ips": [host]},
                # It just answered NUT, so it is running; a scan where it does
                # not answer leaves it out, and it ages into "not seen".
                details={"platform": "nut", "role": "nut_server", "status": "online",
                         "address": host, "port": port, "ups_count": len(units)},
            ))
            for name, unit in sorted(units.items()):
                variables = unit.get("vars") or {}
                identity = {key: _first(variables, names) for key, names in _IDENTITY_VARS.items()}
                driver = identity["driver"]
                attachment = attachment_for(driver, variables)
                identifiers = {"nut_ups": name}
                if identity["serial"]:
                    identifiers["serial"] = identity["serial"][:120]
                details: dict[str, Any] = {
                    "platform": "nut", "nut_name": name, "server_remote_id": server_id,
                    "server_address": host, "attachment": attachment,
                    **{key: value[:160] if value else None for key, value in identity.items() if key != "serial"},
                }
                if "vars" in unit:
                    details["ups"] = ups_telemetry(variables)
                devices.append(DeviceObservation(
                    remote_id=f"nut:{host}:{port}/{name}",
                    name=_ups_name(name, unit.get("description", ""), identity),
                    category="ups",
                    identifiers=identifiers,
                    details=details,
                ))
        return DiscoveryBatch(devices=tuple(devices))

    def poll_ups(self, settings: Mapping[str, Any]) -> tuple[UpsReading, ...]:
        """Re-read UPS variables from known servers only, without scanning.

        Args:
            settings: The probe's settings.

        Returns:
            tuple[UpsReading, ...]: Readings for every UPS that answered.
        """
        hosts, port, _ = scan_settings(settings)
        cached = self._servers.get((tuple(hosts), port))
        readings = []
        for host in cached[1] if cached else []:
            try:
                units = self.read(host, port)
            except (OSError, NutError) as exc:
                _LOGGER.warning("NUT server unreadable (%s)", type(exc).__name__)
                continue
            for name, unit in units.items():
                if "vars" in unit:
                    readings.append(UpsReading(f"nut:{host}:{port}/{name}", ups_telemetry(unit["vars"])))
        return tuple(readings)
