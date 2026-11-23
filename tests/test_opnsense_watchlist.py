# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""Check the OPNsense watchlist: gateway down, VPN peer stale, CARP change (build step 5).

The timeline in ``tests/fixtures/opnsense/timeline`` was recorded from the
test bed by ``scripts/opnsense_testbed.py timeline``: ``good`` (the
monitored gateway to the peer firewall up, the keepalive tunnel fresh,
CARP master), ``down`` (the peer paused: the gateway down, the handshake
over five minutes old), ``recovered`` (the peer resumed), then
``carp_backup`` and ``carp_master`` (CARP maintenance mode on and off).
Each step's time is the moment it was recorded, taken from the tunnel's
reported handshake and its age, since a peer's staleness is judged at the
sample's time.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest

from labrat.collector import watchlist
from labrat.collector.adapters.opnsense import watch_reading
import test_step4c as fixture
from test_opnsense import FIXTURES, SETTINGS, StoredFirewall, adapter, answers

TIMELINE = FIXTURES / "timeline"
PEER = "peer-firewall"


def step_answers(step: str) -> dict:
    """The recorded answers with one timeline step's on top.

    Args:
        step: ``good``, ``down``, ``recovered``, ``carp_backup``, or ``carp_master``.

    Returns:
        dict: Decoded JSON by path.
    """
    replies = answers()
    for path in (TIMELINE / step).glob("*.json"):
        replies[path.stem.replace("__", "/")] = json.loads(path.read_text(encoding="utf-8"))
    return replies


def recorded_at(replies: dict) -> datetime:
    """When a step was recorded: the tunnel's last handshake plus its age then.

    Args:
        replies: A step's answers.

    Returns:
        datetime: The recording moment (UTC).
    """
    row = next(row for row in replies["wireguard/service/show"]["rows"] if row.get("name") == PEER)
    return datetime.fromtimestamp(row["latest-handshake"] + (row.get("latest-handshake-age") or 0), timezone.utc)


def reading(step: str, at: datetime | None = None, replies: dict | None = None) -> dict:
    """One step's watch reading, sampled when it was recorded (or at ``at``).

    Args:
        step: The timeline step.
        at: The sample time instead of the recording moment.
        replies: Answers to read instead of the step's.

    Returns:
        dict: The reading for ``watchlist.advance``.
    """
    replies = replies if replies is not None else step_answers(step)
    reader, _session = adapter(replies)
    found = watch_reading(reader.read(SETTINGS))
    found["sampled_at"] = (at or recorded_at(replies)).isoformat()
    return found


def active(state: dict) -> dict[str, dict]:
    """The active conditions by item key.

    Args:
        state: A watchlist.

    Returns:
        dict[str, dict]: Each served condition by its item.
    """
    return {item["item"]: item for item in watchlist.conditions(state)}


def key(state: dict, kind: str, label: str) -> str:
    """An item's key by its kind and label.

    Args:
        state: A watchlist.
        kind: ``gateway``, ``wireguard``, or ``carp``.
        label: Its label (gateway or peer name, CARP address).

    Returns:
        str: Its key.
    """
    return next(name for name, item in state["items"].items() if item["kind"] == kind and item["label"] == label)


class TimelineTests(unittest.TestCase):
    """good → condition → no data and a poll gap (no change) → recovery."""

    def test_recorded_steps_read_as_recorded(self):
        """The recordings show what each step set out to: up, down, recovered; master, backup, master."""
        good = watchlist.advance(None, reading("good"))
        gateway, tunnel = key(good, "gateway", "PEER_GW"), key(good, "wireguard", PEER)
        self.assertEqual((good["items"][gateway]["state"], good["items"][gateway]["good"]), ("none", True))
        self.assertTrue(good["items"][tunnel]["good"])
        carp = next(name for name, item in good["items"].items() if item["kind"] == "carp")
        self.assertEqual(good["items"][carp]["baseline"], "master")
        self.assertEqual(active(good), {}, "a first sample only sets the baseline")

    def test_gateway_and_peer_timeline(self):
        """Good, then down: both enter. No data, then a gap: no change. Recovered: both clear."""
        good = watchlist.advance(None, reading("good"))
        gateway, tunnel = key(good, "gateway", "PEER_GW"), key(good, "wireguard", PEER)
        down = watchlist.advance(good, reading("down"))
        entered = active(down)
        self.assertEqual(entered[gateway]["condition"], "router-gateway-down")
        self.assertEqual(entered[tunnel]["condition"], "router-peer-stale")
        self.assertEqual({item["kind"] for item in entered.values()}, {"gateway", "wireguard"})
        # No data: the gateway and VPN reads fail; nothing enters or clears.
        failed = step_answers("recovered")
        failed["routes/gateway/status"] = ({}, 503)
        failed["wireguard/service/show"] = ({}, 403)
        at = recorded_at(step_answers("down")) + timedelta(minutes=2)
        blind = watchlist.advance(down, reading("recovered", at=at, replies=failed))
        self.assertEqual(active(blind).keys(), entered.keys())
        self.assertEqual(active(blind)[gateway]["sampled_at"], down["items"][gateway]["sampled_at"])
        # A gap: the next sample is long after; still down, so still active, not re-entered.
        later = at + timedelta(seconds=watchlist.GAP_SECONDS * 2)
        gap = watchlist.advance(blind, reading("down", at=later))
        self.assertEqual(active(gap)[gateway]["entered_at"], entered[gateway]["entered_at"])
        # Recovered, after the gap (the step read at a later time, keeping the
        # tunnel's handshake as fresh as it was recorded).
        replies = step_answers("recovered")
        shift = later + timedelta(minutes=1) - recorded_at(replies)
        for row in replies["wireguard/service/show"]["rows"]:
            if row.get("latest-handshake"):
                row["latest-handshake"] += int(shift.total_seconds())
        recovered = watchlist.advance(gap, reading("recovered", at=later + timedelta(minutes=1), replies=replies))
        self.assertEqual(active(recovered), {})

    def test_no_baseline_no_condition(self):
        """Down as a first sample, or after a gap, only sets the baseline."""
        self.assertEqual(active(watchlist.advance(None, reading("down"))), {})
        good = watchlist.advance(None, reading("good"))
        later = recorded_at(step_answers("down")) + timedelta(seconds=watchlist.GAP_SECONDS + 1)
        self.assertEqual(active(watchlist.advance(good, reading("down", at=later))), {})

    def test_unmonitored_gateways_and_idle_peers_never_enter(self):
        """WAN gateways without monitoring and the peer without keepalive stay out of it."""
        down = watchlist.advance(watchlist.advance(None, reading("good")), reading("down"))
        labels = {item["label"] for item in active(down).values()}
        self.assertNotIn("WAN_DHCP", labels)
        self.assertNotIn("road-warrior", labels)

    def test_opting_out_ends_a_condition_and_needs_a_new_baseline(self):
        """Monitoring or keepalive switched off ends the condition; on again, it needs a good sample first."""
        good = watchlist.advance(None, reading("good"))
        down = watchlist.advance(good, reading("down"))
        self.assertEqual(len(active(down)), 2)
        off = step_answers("down")
        for row in off["routes/gateway/status"]["items"]:
            row["monitor"] = "~"
        for row in off["wireguard/service/show"]["rows"]:
            if row.get("name") == PEER:
                row["persistent-keepalive"] = "off"
        at = recorded_at(step_answers("down")) + timedelta(minutes=1)
        quiet = watchlist.advance(down, reading("down", at=at, replies=off))
        self.assertEqual(active(quiet), {})
        again = watchlist.advance(quiet, reading("down", at=at + timedelta(minutes=1)))
        self.assertEqual(active(again), {}, "back on while down: a new baseline first")

    def test_missing_monitor_or_keepalive_is_not_opting_out(self):
        """Fields left out of the answer are unknown, not off: the conditions and their times hold."""
        down = watchlist.advance(watchlist.advance(None, reading("good")), reading("down"))
        missing = step_answers("down")
        for row in missing["routes/gateway/status"]["items"]:
            row.pop("monitor", None)
        for row in missing["wireguard/service/show"]["rows"]:
            row.pop("persistent-keepalive", None)
        found = reading("down", at=recorded_at(step_answers("down")) + timedelta(minutes=1), replies=missing)
        self.assertEqual({item["monitored"] for item in found["items"] if item["kind"] == "gateway"}, {None})
        self.assertEqual({item["keepalive"] for item in found["items"] if item["kind"] == "wireguard"}, {None})
        held = watchlist.advance(down, found)
        self.assertEqual({name: (item["condition"], item["entered_at"]) for name, item in active(held).items()},
                         {name: (item["condition"], item["entered_at"]) for name, item in active(down).items()})
        for name in active(down):
            self.assertEqual(held["items"][name]["sampled_at"], down["items"][name]["sampled_at"])

    def test_invalid_evidence_changes_nothing_and_breaks_the_baseline(self):
        """An unknown CARP state or a handshake in the future is missing evidence, not a transition."""
        good = watchlist.advance(None, reading("good"))
        carp = next(name for name, item in good["items"].items() if item["kind"] == "carp")
        tunnel = key(good, "wireguard", PEER)
        odd = step_answers("good")
        for row in odd["diagnostics/interface/get_vip_status"]["rows"]:
            row["status"] = ""
        for row in odd["wireguard/service/show"]["rows"]:
            if row.get("name") == PEER:
                row["latest-handshake"] += 86400
        at = recorded_at(step_answers("good")) + timedelta(minutes=1)
        blank = watchlist.advance(good, reading("good", at=at, replies=odd))
        self.assertEqual(active(blank), {})
        self.assertEqual(blank["items"][carp], good["items"][carp])
        self.assertEqual(blank["items"][tunnel], good["items"][tunnel])
        # The next real sample does not follow on from the blank one, so a change only sets the baseline.
        backup = watchlist.advance(blank, reading("carp_backup", at=at + timedelta(minutes=1)))
        self.assertNotIn(carp, active(backup))
        self.assertEqual(backup["items"][carp]["baseline"], "backup")

    def test_carp_change_enters_and_returns(self):
        """master → backup enters a change; back to master clears it."""
        start = watchlist.advance(None, reading("good"))
        carp = next(name for name, item in start["items"].items() if item["kind"] == "carp")
        recovered = watchlist.advance(start, reading("recovered"))
        backup = watchlist.advance(recovered, reading("carp_backup"))
        change = active(backup)[carp]
        self.assertEqual((change["condition"], change["from"], change["to"]), ("router-carp-changed", "master", "backup"))
        self.assertEqual(active(watchlist.advance(backup, reading("carp_master"))), {})


class StoredWatchTests(StoredFirewall):
    """Through the collector, the topology API, and an ignore rule."""

    post = fixture.Step4CTests.post

    def served(self):
        """The firewall's served watchlist conditions and its source ID.

        Returns:
            tuple[list[dict], int]: The conditions, and the source ID.
        """
        graph = self.client.get("/api/v1/topology").get_json()
        [observation] = [item for node in graph["nodes"] for item in node["observations"] if item["remote_id"] == "firewall"]
        return observation["watch_conditions"], observation["source_id"]

    def test_a_carp_change_is_acknowledged_once(self):
        """master → backup, acknowledged; master; backup again: a new occurrence the old acknowledgement misses."""
        self.configure()
        self.poll(step_answers("recovered"))
        self.poll(step_answers("carp_backup"))
        [first], source_id = self.served()
        self.assertEqual((first["condition"], first["from"], first["to"]), ("router-carp-changed", "master", "backup"))
        target = {"condition": "router-carp-changed", "source_id": source_id, "device_remote_id": "firewall",
                  "item": first["item"], "state": "backup", "since": first["entered_at"]}
        answer = self.post("/api/v1/observation-ignores", {"target": target})
        self.assertEqual(answer.status_code, 200, answer.get_data(as_text=True))
        self.assertEqual(answer.json["ignore"]["target"]["since"], first["entered_at"])
        for wrong in ({**target, "item": "carp:nowhere:9"}, {**target, "since": "2000-01-01T00:00:00+00:00"},
                      {key: value for key, value in target.items() if key != "since"}):
            self.assertEqual(self.post("/api/v1/observation-ignores", {"target": wrong}).status_code, 400)
        self.poll(step_answers("carp_master"))
        self.assertEqual(self.served()[0], [])
        self.poll(step_answers("carp_backup"))
        [second], _source = self.served()
        self.assertNotEqual(second["entered_at"], first["entered_at"])
        self.assertEqual(self.post("/api/v1/observation-ignores", {"target": target}).status_code, 400)

    def stored(self):
        """The stored watchlist.

        Returns:
            dict: It.
        """
        return json.loads(self.device().details_json)["watchlist"]

    def test_a_failed_poll_breaks_the_baseline(self):
        """good → failed core poll → down opens nothing; good → down then does."""
        self.configure()
        self.poll(step_answers("recovered"))
        before = self.stored()
        failed = step_answers("down")
        failed["diagnostics/system/system_information"] = ({}, 500)
        self.poll(failed)
        self.assertEqual(self.stored(), {**before, "interrupted": True})
        self.poll(step_answers("down"))
        self.assertFalse([item for item in watchlist.conditions(self.stored()) if item["kind"] == "gateway"])
        self.poll(step_answers("recovered"))
        self.poll(step_answers("down"))
        self.assertEqual([item["label"] for item in watchlist.conditions(self.stored()) if item["kind"] == "gateway"],
                         ["PEER_GW"])

    def test_a_held_poll_breaks_the_baseline(self):
        """A poll held for a different firewall marks the watchlist interrupted."""
        self.configure()
        self.poll(step_answers("recovered"))
        self.poll(self.other_firewall())
        self.assertTrue(self.stored()["interrupted"])


if __name__ == "__main__":
    unittest.main()
