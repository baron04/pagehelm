"""Tests for ARC-12 ProgramCheckSpec, check_fired telemetry, and trace projection."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from web_operation_agent.execution.checks import (
    CHECK_OUTCOMES,
    ProgramCheckSpec,
    record_check_fired,
)
from web_operation_agent.execution.harness import HarnessTurnContext, _build_debug_trace
from web_operation_agent.execution.turn_trace import TurnTrace
from web_operation_agent.privacy import SessionHandoffPolicy
from web_operation_agent.state.ledger import SessionLedger


class ProgramCheckSpecTests(unittest.TestCase):
    def test_program_check_spec_validates_non_empty_fields(self):
        spec = ProgramCheckSpec(
            name="untraced_numbers",
            checks_what="stated numbers trace to turn sources",
            retry_guidance="replace untraced numbers with observed tool values",
            terminal_delivery="mark affected deliverable items undelivered or append note",
        )
        self.assertEqual(spec.name, "untraced_numbers")
        for bad in ("", "   ", 123):
            with self.assertRaises(ValueError):
                ProgramCheckSpec(
                    name=bad,  # type: ignore[arg-type]
                    checks_what="a",
                    retry_guidance="b",
                    terminal_delivery="c",
                )

    def test_record_check_fired_updates_ledger_context_trace_and_debug_trace(self):
        self.assertEqual(CHECK_OUTCOMES, frozenset({"returned", "annotated", "blocked"}))
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SessionLedger(Path(tmp) / "audit.jsonl", "run-chk", base={"session_id": "s1"})
            trace = TurnTrace(Path(tmp) / "turns", SessionHandoffPolicy(()))
            ctx = HarnessTurnContext(
                message=None,  # type: ignore[arg-type]
                run_id="run-chk",
                ledger=ledger,
                browser=None,
                writes=None,  # type: ignore[arg-type]
                approvals=None,  # type: ignore[arg-type]
                obs=None,
                started_at=0.0,
            )

            record_check_fired(
                ledger,
                s=ctx,
                trace=trace,
                check="untraced_numbers",
                outcome="returned",
                attempt=1,
                answer_preserved=False,
            )
            record_check_fired(
                ledger,
                s=ctx,
                trace=trace,
                check="untraced_numbers",
                outcome="annotated",
                attempt=2,
                answer_preserved=True,
            )

            events = [
                json.loads(line)
                for line in (Path(tmp) / "audit.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([e["event"] for e in events], ["check_fired", "check_fired"])
            self.assertEqual(
                events[0],
                {
                    "seq": 1,
                    "t": events[0]["t"],
                    "run_id": "run-chk",
                    "session_id": "s1",
                    "event": "check_fired",
                    "check": "untraced_numbers",
                    "outcome": "returned",
                    "attempt": 1,
                    "answer_preserved": False,
                },
            )
            self.assertTrue(events[1]["answer_preserved"])
            self.assertEqual(events[1]["outcome"], "annotated")
            self.assertEqual(events[1]["attempt"], 2)

            path = trace.write(
                run_id="run-chk",
                turn_id="t1",
                prompt="check",
                start_route="/",
                terminal_reason="agent_finished",
                wall_seconds=0.1,
                answer="ok",
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(saved["checks"]), 2)
            self.assertEqual(saved["checks"][1]["outcome"], "annotated")

            debug = _build_debug_trace(
                s=ctx,
                wall_seconds=0.25,
                stop_reason="agent_finished",
                stage_timings={"model": 0.1},
                receipt_report={},
            )
            self.assertEqual(debug["checks"], saved["checks"])

    def test_record_check_fired_rejects_invalid_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = SessionLedger(Path(tmp) / "audit.jsonl", "run-bad", base={"session_id": "s1"})
            with self.assertRaises(ValueError):
                record_check_fired(ledger, check="", outcome="returned", attempt=1, answer_preserved=False)
            with self.assertRaises(ValueError):
                record_check_fired(ledger, check="x", outcome="unknown", attempt=1, answer_preserved=False)
            with self.assertRaises(ValueError):
                record_check_fired(ledger, check="x", outcome="blocked", attempt=0, answer_preserved=False)
            with self.assertRaises(ValueError):
                record_check_fired(ledger, check="x", outcome="blocked", attempt=1, answer_preserved=1)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
