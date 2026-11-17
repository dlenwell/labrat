# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Exercise bounded scanning and read-only NUT discovery without a real lab."""

import json
import socket
import socketserver
import threading
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import labrat.models  # Register canonical models referenced by discovery tables.
from labrat.audit import AuditEvent
from labrat.collector.adapters.nut import NutAdapter, UpsReading, attachment_for, scan_settings, ups_telemetry
from labrat.collector.contracts import DiscoveryBatch
from labrat.collector.nut import NutError, read_server, split_line
from labrat.collector.runner import poll_ups_once
from labrat.collector.scan import MAX_SCAN_HOSTS, allowed_hosts, port_open, scan_tcp
from labrat.collector.sources import configured_sources
from labrat.collector.store import apply_batch, record_telemetry
from labrat.database import Base
from labrat.discovery.models import DiscoveredDevice, DiscoverySource

#: Canned answers for a NUT server with one USB UPS and one relayed unit.
NUT_ANSWERS = {
    "LIST UPS": [
        'UPS rack "Description unavailable"',
        'UPS station "Portable \\"power\\" station"',
    ],
    "LIST VAR rack": [
        'VAR rack device.mfr "Example"', 'VAR rack device.model "RackUPS 1500"',
        'VAR rack driver.name "usbhid-ups"', 'VAR rack ups.status "OL CHRG"',
        'VAR rack battery.charge "100"', 'VAR rack battery.runtime "6000"',
        'VAR rack ups.load "12"', 'VAR rack ups.serial "SN-TEST"',
    ],
    "LIST VAR station": [
        'VAR station driver.name "dummy-ups"', 'VAR station ups.status "ALARM OL"',
        'VAR station ups.realpower "577"', 'VAR station output.voltage "125.3"',
    ],
}


class FakeNutHandler(socketserver.StreamRequestHandler):
    """Answer LIST commands like upsd; record every command received."""

    def handle(self):
        """Serve one connection."""
        for raw in self.rfile:
            command = raw.decode().strip()
            self.server.commands.append(command)
            if command == "LOGOUT":
                self.wfile.write(b"OK Goodbye\n")
                return
            lines = self.server.answers.get(command)
            if lines is None:
                self.wfile.write(b"ERR UNKNOWN-UPS\n")
                continue
            query = command[len("LIST "):]
            body = "".join(f"{line}\n" for line in lines)
            self.wfile.write(f"BEGIN LIST {query}\n{body}END LIST {query}\n".encode())


class FakeNutServer:
    """A local upsd stand-in on an ephemeral port."""

    def __init__(self, answers=NUT_ANSWERS, handler=FakeNutHandler):
        """Start a NUT server on a free local port, in a background thread.

        Args:
            answers: Replies by command.
            handler: The request handler class.
        """
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.server.answers = answers
        self.server.commands = []
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        """Stop serving."""
        self.server.shutdown()
        self.server.server_close()


class ProtocolTests(unittest.TestCase):
    """Parse NUT's quoting and talk to a server read-only."""

    def test_split_line_handles_quotes_and_escapes(self):
        self.assertEqual(split_line('VAR ups x.y "a \\"b\\" \\\\c"'), ["VAR", "ups", "x.y", 'a "b" \\c'])
        with self.assertRaises(NutError):
            split_line('VAR ups x "open')

    def test_read_server_lists_units_and_sends_only_read_commands(self):
        server = FakeNutServer()
        self.addCleanup(server.close)
        units = read_server("127.0.0.1", server.port)
        self.assertEqual(set(units), {"rack", "station"})
        self.assertEqual(units["station"]["description"], 'Portable "power" station')
        self.assertEqual(units["rack"]["vars"]["battery.runtime"], "6000")
        commands = server.server.commands
        # The client says LOGOUT and closes without waiting; give the server a moment to log it.
        for _ in range(100):
            if commands and commands[-1] == "LOGOUT":
                break
            threading.Event().wait(0.01)
        self.assertTrue(all(command.startswith("LIST ") or command == "LOGOUT" for command in commands))
        self.assertEqual(commands[-1], "LOGOUT")

    def test_a_non_nut_service_is_rejected(self):
        class NotNut(socketserver.StreamRequestHandler):
            def handle(self):
                self.rfile.readline()
                self.wfile.write(b"HTTP/1.1 400 Bad Request\n")
        server = FakeNutServer(handler=NotNut)
        self.addCleanup(server.close)
        with self.assertRaises(NutError):
            read_server("127.0.0.1", server.port)

    def test_one_unreadable_ups_does_not_hide_the_others(self):
        answers = {key: value for key, value in NUT_ANSWERS.items() if key != "LIST VAR station"}
        server = FakeNutServer(answers=answers)
        self.addCleanup(server.close)
        units = read_server("127.0.0.1", server.port)
        self.assertIn("vars", units["rack"])
        self.assertIn("error", units["station"])


class ScanTests(unittest.TestCase):
    """Scan only the configured ranges, within fixed bounds."""

    def test_allowed_hosts(self):
        self.assertEqual(allowed_hosts(["192.0.2.0/30"]), ["192.0.2.1", "192.0.2.2"])
        self.assertEqual(allowed_hosts(["192.0.2.9", "192.0.2.9/32"]), ["192.0.2.9"])
        for bad in (["192.0.2.0/19"], ["not-a-network"], [5], [""]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                allowed_hosts(bad)
        with self.assertRaises(ValueError):
            allowed_hosts([f"10.{octet}.0.0/22" for octet in range(MAX_SCAN_HOSTS // 1022 + 1)])

    def test_scan_reports_only_accepting_hosts(self):
        server = FakeNutServer()
        self.addCleanup(server.close)
        self.assertTrue(port_open("127.0.0.1", server.port))
        tried = []
        found = scan_tcp(["192.0.2.1", "192.0.2.2"], 3493, check=lambda host, port: tried.append(host) or host.endswith(".2"))
        self.assertEqual(found, ["192.0.2.2"])
        self.assertEqual(sorted(tried), ["192.0.2.1", "192.0.2.2"])


class TelemetryTests(unittest.TestCase):
    """Turn NUT variables into readings and a summary state."""

    def test_states_and_numbers(self):
        cases = [("OL CHRG", "online"), ("OB DISCHRG", "on_battery"), ("OB LB", "low_battery"), ("OFF", "off"), ("", "unknown")]
        for status, state in cases:
            with self.subTest(status=status):
                self.assertEqual(ups_telemetry({"ups.status": status})["state"], state)
        reading = ups_telemetry({"ups.status": "ALARM OL", "battery.charge": "87.5", "ups.load": "n/a"})
        self.assertIn("alarm", reading["flags"])
        self.assertEqual(reading["battery_charge"], 87.5)
        self.assertIsNone(reading["load_percent"])


class AdapterTests(unittest.TestCase):
    """Confirm candidates by protocol and report servers and UPS units."""

    SETTINGS = {"ranges": ["192.0.2.0/29"]}

    def setUp(self):
        self.now = 1000.0
        self.scans = 0
        self.down = set()

        def scan(hosts, port):
            self.scans += 1
            return ["192.0.2.2", "192.0.2.3"]  # .3 has the port open but is not NUT.

        def read(host, port):
            if host == "192.0.2.3" or host in self.down:
                raise NutError("Not a NUT response")
            units = {}
            for line in NUT_ANSWERS["LIST UPS"]:
                words = split_line(line)
                units[words[1]] = {"description": words[2], "vars": {
                    split_line(item)[2]: split_line(item)[3] for item in NUT_ANSWERS[f"LIST VAR {words[1]}"]
                }}
            return units

        self.adapter = NutAdapter(scan=scan, read=read, resolve=lambda address: None, clock=lambda: self.now)

    def test_server_and_each_ups_are_separate_devices(self):
        batch = self.adapter.poll(self.SETTINGS)
        devices = {device.remote_id: device for device in batch.devices}
        server = devices["nut-server:192.0.2.2:3493"]
        rack = devices["nut:192.0.2.2:3493/rack"]
        station = devices["nut:192.0.2.2:3493/station"]
        self.assertEqual((server.category, server.name), ("server", "NUT server 192.0.2.2"))
        self.assertEqual(server.details["status"], "online")  # It just answered NUT.
        self.assertNotIn("nut-server:192.0.2.3:3493", devices)  # An open port alone records nothing.
        self.assertEqual((rack.category, rack.name), ("ups", "Example RackUPS 1500"))
        self.assertEqual(rack.details["attachment"], "usb")
        self.assertEqual(rack.identifiers["serial"], "SN-TEST")
        self.assertEqual(rack.details["ups"]["battery_runtime_s"], 6000)
        self.assertEqual(station.name, 'Portable "power" station')
        self.assertEqual(station.details["attachment"], "relayed")
        self.assertEqual(rack.details["server_remote_id"], server.remote_id)

    def test_rescans_only_when_due_and_fast_reads_known_servers(self):
        self.adapter.poll(self.SETTINGS)
        self.now += 3599
        self.adapter.poll(self.SETTINGS)
        self.assertEqual(self.scans, 1)
        readings = self.adapter.poll_ups(self.SETTINGS)
        self.assertEqual({reading.remote_id for reading in readings},
                         {"nut:192.0.2.2:3493/rack", "nut:192.0.2.2:3493/station"})
        self.assertEqual(self.scans, 1)  # Fast reads never scan.
        self.now += 2
        self.adapter.poll(self.SETTINGS)
        self.assertEqual(self.scans, 2)

    def test_unreachable_server_is_skipped(self):
        self.adapter.poll(self.SETTINGS)
        self.down.add("192.0.2.2")
        self.assertEqual(self.adapter.poll(self.SETTINGS).devices, ())
        self.assertEqual(self.adapter.poll_ups(self.SETTINGS), ())

    def test_settings_are_validated(self):
        for bad in ({}, {"ranges": []}, {"ranges": ["192.0.2.0/24"], "port": 0},
                    {"ranges": ["192.0.2.0/24"], "rescan_interval_seconds": 60}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                scan_settings(bad)


class ConfigTests(unittest.TestCase):
    """[scan] grants scanning: ranges plus explicitly enabled probes."""

    def test_scan_section_enables_probe_sources(self):
        config = {"scan": {"ranges": ["192.0.2.0/24"], "probes": ["nut"], "nut": {"ups_interval_seconds": 60}}}
        self.assertEqual(configured_sources(config)["nut"]["default"],
                         {"ranges": ["192.0.2.0/24"], "ups_interval_seconds": 60})
        self.assertEqual(configured_sources({"scan": {"ranges": ["192.0.2.0/24"]}})["nut"], {})
        self.assertEqual(configured_sources({})["nut"], {})
        for bad in ({"scan": {"probes": ["ssh"]}}, {"scan": {"probes": "nut"}}, {"scan": []}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                configured_sources(bad)


class StoreTests(unittest.TestCase):
    """UPS readings are telemetry: no change events, fast-poll updates."""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.source = DiscoverySource(name="nut:default", kind="nut", config_name="default")
        self.db.add(self.source)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_changing_readings_write_no_change_events(self):
        def batch(charge):
            return NutAdapter(
                scan=lambda hosts, port: ["192.0.2.2"],
                read=lambda host, port: {"rack": {"description": "Rack UPS", "vars": {
                    "ups.status": "OL", "battery.charge": str(charge), "driver.name": "usbhid-ups"}}},
                resolve=lambda address: "ups-host.example",
            ).poll({"ranges": ["192.0.2.0/30"]})
        apply_batch(self.db, self.source, batch(100))
        events = self.db.query(AuditEvent).count()
        apply_batch(self.db, self.source, batch(42))
        self.assertEqual(self.db.query(AuditEvent).count(), events)
        rack = self.db.scalar(select(DiscoveredDevice).where(DiscoveredDevice.category == "ups"))
        self.assertEqual(json.loads(rack.details_json)["ups"]["battery_charge"], 42)
        server = self.db.scalar(select(DiscoveredDevice).where(DiscoveredDevice.category == "server"))
        self.assertEqual(server.name, "ups-host.example")

    def test_fast_poll_updates_known_ups_only(self):
        apply_batch(self.db, self.source, DiscoveryBatch(devices=NutAdapter(
            scan=lambda hosts, port: ["192.0.2.2"],
            read=lambda host, port: {"rack": {"description": "Rack UPS", "vars": {"ups.status": "OL"}}},
            resolve=lambda address: None,
        ).poll({"ranges": ["192.0.2.0/30"]}).devices))
        self.assertTrue(record_telemetry(self.db, self.source, "nut:192.0.2.2:3493/rack", "ups", {"state": "on_battery"}))
        self.assertFalse(record_telemetry(self.db, self.source, "nut:192.0.2.9:3493/x", "ups", {}))
        with self.assertRaises(ValueError):
            record_telemetry(self.db, self.source, "nut:192.0.2.2:3493/rack", "name", {})

    def test_runner_fast_poll(self):
        class Adapter:
            def poll_ups(self, settings):
                return (UpsReading("nut:192.0.2.2:3493/rack", {"state": "low_battery"}),)
        apply_batch(self.db, self.source, NutAdapter(
            scan=lambda hosts, port: ["192.0.2.2"],
            read=lambda host, port: {"rack": {"description": "Rack UPS", "vars": {"ups.status": "OL"}}},
            resolve=lambda address: None,
        ).poll({"ranges": ["192.0.2.0/30"]}))
        self.db.commit()
        sessions = sessionmaker(bind=self.engine)
        self.assertEqual(poll_ups_once(sessions, self.source.id, {}, Adapter()), 1)
        self.db.expire_all()
        rack = self.db.scalar(select(DiscoveredDevice).where(DiscoveredDevice.category == "ups"))
        self.assertEqual(json.loads(rack.details_json)["ups"]["state"], "low_battery")


class AttachmentTests(unittest.TestCase):
    """Tell USB, relayed, and other UPS connections apart."""

    def test_usb_only_and_relay_drivers(self):
        """A USB-only driver is USB and a relay driver is relayed, whatever else is reported."""
        self.assertEqual(attachment_for("usbhid-ups", {}), "usb")
        self.assertEqual(attachment_for("dummy-ups", {"ups.vendorid": "0764"}), "relayed")

    def test_dual_drivers_need_usb_evidence(self):
        """A driver that can use USB or serial counts as USB only with USB evidence."""
        self.assertEqual(attachment_for("nutdrv_qx", {"driver.parameter.port": "auto"}), "usb")
        self.assertEqual(attachment_for("nutdrv_qx", {"ups.vendorid": "0665", "ups.productid": "5161"}), "usb")
        self.assertIsNone(attachment_for("nutdrv_qx", {"driver.parameter.port": "/dev/ttyS0"}))

    def test_network_and_unknown_drivers(self):
        """A network driver or no driver claims no physical connection."""
        self.assertIsNone(attachment_for("snmp-ups", {"driver.parameter.port": "ups.example.lan"}))
        self.assertIsNone(attachment_for(None, {}))


if __name__ == "__main__":
    unittest.main()
