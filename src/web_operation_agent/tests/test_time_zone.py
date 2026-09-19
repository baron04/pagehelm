"""The turn is told what day it is where the person is (P7.5 CR-18).

The model read the date off an `observed_at` timestamp, which is UTC, so "today's orders"
meant the server's today. Between midnight and 08:00 in Shanghai that is yesterday, and the
J1 relative-date questions answered differently depending on the hour they were asked.
"""
from __future__ import annotations

import datetime
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from web_operation_agent.embedded_agent import ReadOnlyControl
from web_operation_agent.ledger import SessionLedger
from web_operation_agent.session import ServerAgentSession, TaskSpec, _validated_time_zone
from web_operation_agent.tests.test_business import _Browser, _Chat, _observation


def _opening(time_zone: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        session = ServerAgentSession(
            task=TaskSpec(task_ref="T", prompt="今天有几张订单？",
                          supported_scope="Read the current application only."),
            chat=_Chat([]), browser=_Browser(), control=ReadOnlyControl(),
            ledger=SessionLedger(Path(tmp) / "session.jsonl", "tz"),
            time_zone=time_zone)
        session.entry_observation = _observation(1)
        return session._opening(session.entry_observation)


class TheTurnKnowsTheReadersDay(unittest.TestCase):
    def test_the_opening_states_the_local_time_and_the_zone(self):
        text = _opening("Asia/Shanghai")
        local = datetime.datetime.now(ZoneInfo("Asia/Shanghai"))

        self.assertIn("CURRENT TIME: " + local.strftime("%Y-%m-%d"), text)
        self.assertIn("(Asia/Shanghai)", text)
        self.assertIn("mean days in this zone", text)

    def test_a_turn_with_no_zone_says_nothing_about_the_clock(self):
        """A deployment that does not know where its person is gets no sentence, rather than
        a confident wrong one -- and the recorded golden session stays valid."""
        self.assertNotIn("CURRENT TIME", _opening(""))

    def test_the_stated_day_is_the_readers_day_not_the_servers(self):
        """The two differ for eight hours of every day, which is the whole defect."""
        zone = "Asia/Shanghai"
        text = _opening(zone)
        theirs = datetime.datetime.now(ZoneInfo(zone)).strftime("%Y-%m-%d")
        self.assertIn(theirs, text)


class AZoneIsCheckedBeforeItIsBelieved(unittest.TestCase):
    def test_a_zone_this_machine_cannot_resolve_is_refused(self):
        with self.assertRaises(ValueError):
            _validated_time_zone("Mars/Olympus")
        with self.assertRaises(ValueError):
            _validated_time_zone(17)

    def test_nothing_is_a_valid_answer(self):
        self.assertEqual(_validated_time_zone(""), "")
        self.assertEqual(_validated_time_zone(None), "")


if __name__ == "__main__":
    unittest.main()
