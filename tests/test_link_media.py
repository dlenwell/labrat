# Copyright 2026 LabRat contributors. SPDX-License-Identifier: Apache-2.0
"""A cable with no reported medium takes its identical twin's (topology API)."""

import unittest

from labrat.api.topology import _infer_twin_media


def cable(link_id, medium, speed, ends=("server", "switch")):
    """Build a link record as the topology API assembles it.

    Args:
        link_id: The link's ID.
        medium: Its reported medium, or None.
        speed: Its speed in Mb/s.
        ends: The two device IDs.

    Returns:
        dict: The link record.
    """
    return {"id": link_id, "from": ends[0], "to": ends[1], "medium": medium,
            "medium_inferred_from": None, "_speed": speed}


class TwinMediaTests(unittest.TestCase):
    """The rules for borrowing a twin cable's medium."""

    def test_unknown_cable_takes_its_twins_medium(self):
        records = [cable(1, "dac", 10000), cable(2, None, 10000, ("switch", "server"))]
        _infer_twin_media(records)
        self.assertEqual(records[1]["medium"], "dac")
        self.assertEqual(records[1]["medium_inferred_from"], 1)
        self.assertIsNone(records[0]["medium_inferred_from"], "A reported medium is never marked inferred")
        self.assertNotIn("_speed", records[0], "The private speed is removed")

    def test_twins_that_disagree_or_differ_leave_it_unknown(self):
        disagree = [cable(1, "dac", 10000), cable(2, "fiber_multimode", 10000), cable(3, None, 10000)]
        _infer_twin_media(disagree)
        self.assertIsNone(disagree[2]["medium"])
        other_speed = [cable(1, "dac", 10000), cable(2, None, 1000)]
        _infer_twin_media(other_speed)
        self.assertIsNone(other_speed[1]["medium"], "A slower cable is not a twin")
        alone = [cable(1, None, 10000), cable(2, "dac", 10000, ("server", "other"))]
        _infer_twin_media(alone)
        self.assertIsNone(alone[0]["medium"], "Only cables between the same two devices are twins")

    def test_inferred_media_do_not_chain(self):
        records = [cable(1, "dac", 10000), cable(2, None, 10000), cable(3, None, 10000)]
        _infer_twin_media(records)
        self.assertEqual([r["medium_inferred_from"] for r in records], [None, 1, 1])


if __name__ == "__main__":
    unittest.main()
