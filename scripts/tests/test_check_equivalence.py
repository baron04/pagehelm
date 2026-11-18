"""Unit and roundtrip tests for scripts/check_equivalence.py (OPS-10)."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import check_equivalence as ce


class CanonicalizerTests(unittest.TestCase):
    def test_strips_wall_clock_and_normalizes_ephemeral_ids_sequentially(self):
        run_a = {
            "scenario": "canon_test",
            "session_id": "sess-aaaa-1111",
            "run_id": "run-bbbb-2222",
            "message_id": "11111111-2222-3333-4444-555555555555",
            "approval_id": "approval-00000000000000000000000000000001",
            "at": "2026-11-18T01:05:05.123Z",
            "elapsed_s": 0.42,
            "elapsed_ms": 420,
            "business_date": "2026-10-20 10:00:00",
            "model_calls": [
                {
                    "messages": [
                        {"role": "tool", "content": "receipt ev_abcdef123456_1 at 2026-11-18T01:05:05Z"},
                        {"role": "assistant", "content": "repeat ev_abcdef123456_1"},
                    ],
                    "tools": [],
                }
            ],
        }
        run_b = {
            "scenario": "canon_test",
            "session_id": "sess-zzzz-9999",
            "run_id": "run-yyyy-8888",
            "message_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "approval_id": "approval-ffffffffffffffffffffffffffffffff",
            "at": "2026-11-19T09:10:11+09:00",
            "elapsed_s": 1.99,
            "elapsed_ms": 1990,
            "business_date": "2026-10-20 10:00:00",
            "model_calls": [
                {
                    "messages": [
                        {"role": "tool", "content": "receipt ev_999999888888_1 at 2026-11-19T09:10:11Z"},
                        {"role": "assistant", "content": "repeat ev_999999888888_1"},
                    ],
                    "tools": [],
                }
            ],
        }
        canon_a = ce.canonicalize_scenario(run_a)
        canon_b = ce.canonicalize_scenario(run_b)
        self.assertEqual(canon_a, canon_b)
        self.assertNotIn("at", canon_a)
        self.assertNotIn("elapsed_s", canon_a)
        self.assertNotIn("elapsed_ms", canon_a)
        self.assertEqual(canon_a["business_date"], "2026-10-20 10:00:00")
        self.assertEqual(canon_a["session_id"], "id_1")
        self.assertEqual(canon_a["run_id"], "id_2")
        self.assertEqual(canon_a["message_id"], "id_3")
        self.assertEqual(canon_a["approval_id"], "id_4")
        self.assertEqual(
            canon_a["model_calls"][0]["messages"][0]["content"],
            "receipt id_5 at <TIMESTAMP>",
        )
        self.assertEqual(
            canon_a["model_calls"][0]["messages"][1]["content"],
            "repeat id_5",
        )


class EquivalenceHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capture_a = ce.canonicalize_capture(ce.run_all_scenarios())
        cls.capture_b = ce.canonicalize_capture(ce.run_all_scenarios())

    def test_self_capture_equals_verify_across_independent_runs(self):
        ok, diff = ce.compare_captures(self.capture_a, self.capture_b)
        self.assertTrue(ok, diff)

    def test_cli_capture_and_verify_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = Path(tmp) / "baseline.json"
            self.assertEqual(ce.main(["--capture", str(baseline)]), 0)
            self.assertTrue(baseline.exists())
            self.assertEqual(ce.main(["--verify", str(baseline)]), 0)
            # Unknown --allow-registered must exit non-zero
            self.assertEqual(ce.main(["--verify", str(baseline), "--allow-registered", "arc-999"]), 2)

    def test_all_six_scenario_families_cover_expected_outcomes(self):
        by_name = {s["scenario"]: s for s in self.capture_a["scenarios"]}
        expected = {
            "yudao_table_read_single": ("finished", "agent_finished"),
            "yudao_table_read_multi_batch": ("finished", "agent_finished"),
            "yudao_table_read_multi_early_exit": ("refused", "permission_denied"),
            "yudao_page_navigation": ("finished", "agent_finished"),
            "yudao_skill_write_approved": ("finished", "agent_finished"),
            "yudao_skill_write_denied": ("cancelled", "approval_denied"),
            "yudao_skill_write_readback_timeout": ("indeterminate", "indeterminate_write"),
            "yudao_native_submit_approved": ("finished", "agent_finished"),
            "yudao_native_submit_prefill": ("finished", "agent_finished"),
            "yudao_native_submit_denied": ("cancelled", "approval_denied"),
            "yudao_native_submit_reread_drift": ("refused", "business_mutation_refused"),
            "yudao_convention_write_approved": ("finished", "agent_finished"),
            "yudao_convention_write_denied": ("cancelled", "approval_denied"),
            "yudao_convention_write_readback_timeout": ("indeterminate", "indeterminate_write"),
            "contacts_read": ("finished", "agent_finished"),
            "contacts_write_approved": ("finished", "agent_finished"),
        }
        self.assertEqual(set(by_name), set(expected))
        for name, (status, reason) in expected.items():
            with self.subTest(scenario=name):
                self.assertEqual(by_name[name]["result"]["status"], status)
                self.assertEqual(by_name[name]["result"]["terminal_reason"], reason)

        # Verify write dispatch counts and approval request shapes
        self.assertEqual(len(by_name["yudao_skill_write_approved"]["dispatched_writes"]), 1)
        self.assertEqual(len(by_name["yudao_skill_write_denied"]["dispatched_writes"]), 0)
        self.assertEqual(len(by_name["yudao_skill_write_readback_timeout"]["dispatched_writes"]), 1)
        self.assertEqual(len(by_name["yudao_native_submit_approved"]["dispatched_writes"]), 1)
        self.assertEqual(len(by_name["yudao_native_submit_prefill"]["dispatched_writes"]), 0)
        self.assertTrue(by_name["yudao_native_submit_prefill"]["approval_requests"][0]["offer_prefill"])
        self.assertEqual(len(by_name["yudao_native_submit_denied"]["dispatched_writes"]), 0)
        self.assertEqual(len(by_name["yudao_native_submit_reread_drift"]["dispatched_writes"]), 0)
        self.assertEqual(len(by_name["yudao_convention_write_approved"]["dispatched_writes"]), 1)
        self.assertEqual(len(by_name["yudao_convention_write_denied"]["dispatched_writes"]), 0)
        self.assertEqual(len(by_name["yudao_convention_write_readback_timeout"]["dispatched_writes"]), 1)
        self.assertEqual(len(by_name["contacts_write_approved"]["dispatched_writes"]), 1)
        self.assertEqual(by_name["yudao_page_navigation"]["approval_requests"][0]["kind"], "operate")

    def test_drift_detection_fails_on_messages_tools_approval_dispatch_or_receipt(self):
        mutations = [
            ("messages", lambda s: s["model_calls"][0]["messages"][1].__setitem__("content", "drifted")),
            ("tools", lambda s: s["model_calls"][0]["tools"].append({"type": "function", "function": {"name": "extra"}})),
            ("approval_effect", lambda s: s["approval_requests"][0].__setitem__("effect", "mutated effect")),
            ("approval_targets", lambda s: s["approval_requests"][0]["targets"].append("extra")),
            ("approval_kind", lambda s: s["approval_requests"][0].__setitem__("kind", "operate")),
            ("approval_offer_prefill", lambda s: s["approval_requests"][0].__setitem__("offer_prefill", True)),
            ("dispatch", lambda s: s["dispatched_writes"][0]["body"].__setitem__("mobile", "13999999999")),
            ("receipt", lambda s: s["tool_receipts"]["tool_messages"][0].__setitem__("content", "altered receipt")),
        ]
        for label, mutator in mutations:
            with self.subTest(mutation=label):
                drifted = copy.deepcopy(self.capture_a)
                target = next(s for s in drifted["scenarios"] if s["scenario"] == "yudao_skill_write_approved")
                mutator(target)
                ok, diff = ce.compare_captures(self.capture_a, drifted)
                self.assertFalse(ok, f"Expected drift failure for {label}")
                self.assertIn("--- baseline.json", diff)

    def test_allow_registered_arc08_scoping(self):
        modified = copy.deepcopy(self.capture_a)
        scen = next(s for s in modified["scenarios"] if s["scenario"] == "yudao_table_read_single")
        scen["audit_records"].insert(1, {
            "seq": 2, "event": "turn_timing", "first_route_kind": "table_read", "stop_reason": "finish",
        })
        scen["audit_records"][-1]["first_route_kind"] = "table_read"
        scen["audit_records"][-1]["stop_reason"] = "finish"
        for idx, rec in enumerate(scen["audit_records"], 1):
            rec["seq"] = idx

        # Strict verify fails without arc-08
        ok_strict, _ = ce.compare_captures(self.capture_a, modified)
        self.assertFalse(ok_strict)
        # Passes with arc-08
        ok_allowed, diff = ce.compare_captures(self.capture_a, modified, allow={"arc-08"})
        self.assertTrue(ok_allowed, diff)

        # arc-08 NEVER ignores model messages/tools, approvals, dispatches, receipts, or answers
        for mutator in (
            lambda s: s["model_calls"][0]["messages"][0].__setitem__("content", "changed"),
            lambda s: s["result"].__setitem__("user_answer", "changed answer"),
            lambda s: s["tool_receipts"]["tool_messages"][0].__setitem__("content", "changed receipt"),
        ):
            bad = copy.deepcopy(modified)
            mutator(next(s for s in bad["scenarios"] if s["scenario"] == "yudao_table_read_single"))
            ok_bad, _ = ce.compare_captures(self.capture_a, bad, allow={"arc-08"})
            self.assertFalse(ok_bad)

    def test_allow_registered_arc12_scoping(self):
        modified = copy.deepcopy(self.capture_a)
        scen = next(s for s in modified["scenarios"] if s["scenario"] == "yudao_skill_write_approved")
        scen["audit_records"].insert(2, {"seq": 3, "event": "check_fired", "check": "post_write"})
        for idx, rec in enumerate(scen["audit_records"], 1):
            rec["seq"] = idx

        self.assertFalse(ce.compare_captures(self.capture_a, modified)[0])
        ok_allowed, diff = ce.compare_captures(self.capture_a, modified, allow={"arc-12"})
        self.assertTrue(ok_allowed, diff)

        # Other audit events must still fail even with arc-12
        scen["audit_records"].insert(2, {"seq": 3, "event": "unexpected_audit_event"})
        for idx, rec in enumerate(scen["audit_records"], 1):
            rec["seq"] = idx
        self.assertFalse(ce.compare_captures(self.capture_a, modified, allow={"arc-12"})[0])

    def test_allow_registered_arc09_scoping(self):
        modified = copy.deepcopy(self.capture_a)
        early = next(s for s in modified["scenarios"] if s["scenario"] == "yudao_table_read_multi_early_exit")
        self.assertGreaterEqual(len(early["backend_http_reads"]), 2)
        early["backend_http_reads"] = list(reversed(early["backend_http_reads"]))

        # Fails without arc-09
        self.assertFalse(ce.compare_captures(self.capture_a, modified)[0])
        # Passes with arc-09
        ok_allowed, diff = ce.compare_captures(self.capture_a, modified, allow={"arc-09"})
        self.assertTrue(ok_allowed, diff)

        # Changing receipts, TurnTables, or answer on early-exit STILL fails with arc-09
        bad_answer = copy.deepcopy(modified)
        next(s for s in bad_answer["scenarios"] if s["scenario"] == "yudao_table_read_multi_early_exit")["result"]["user_answer"] = "tampered"
        self.assertFalse(ce.compare_captures(self.capture_a, bad_answer, allow={"arc-09"})[0])

        # Reordering backend_http_reads on a non-early-exit scenario STILL fails with arc-09
        bad_other = copy.deepcopy(modified)
        single = next(s for s in bad_other["scenarios"] if s["scenario"] == "yudao_table_read_single")
        single["backend_http_reads"] = list(reversed(single["backend_http_reads"]))
        if single["backend_http_reads"] != next(s for s in self.capture_a["scenarios"] if s["scenario"] == "yudao_table_read_single")["backend_http_reads"]:
            self.assertFalse(ce.compare_captures(self.capture_a, bad_other, allow={"arc-09"})[0])

    def test_allow_registered_ops03_scoping(self):
        modified = copy.deepcopy(self.capture_a)
        scen = next(
            s for s in modified["scenarios"] if s["scenario"] == "yudao_convention_write_readback_timeout"
        )
        indet = next(
            r for r in scen["audit_records"] if r.get("event") == "business_mutation_indeterminate"
        )
        indet["proposal_digest"] = "mutated_digest"

        # Fails without ops-03
        self.assertFalse(ce.compare_captures(self.capture_a, modified)[0])
        # Passes with ops-03
        ok_allowed, diff = ce.compare_captures(self.capture_a, modified, allow={"ops-03"})
        self.assertTrue(ok_allowed, diff)

        # Changing proposal_digest on any other audit event STILL fails even with ops-03
        other_event_mod = copy.deepcopy(modified)
        other_scen = next(
            s for s in other_event_mod["scenarios"] if s["scenario"] == "yudao_convention_write_readback_timeout"
        )
        non_indet = next(
            r for r in other_scen["audit_records"]
            if r.get("event") != "business_mutation_indeterminate" and isinstance(r, dict)
        )
        non_indet["proposal_digest"] = "unexpected_digest"
        self.assertFalse(ce.compare_captures(self.capture_a, other_event_mod, allow={"ops-03"})[0])

        # Changing model calls, receipts, or answer STILL fails even with ops-03
        bad_answer = copy.deepcopy(modified)
        next(
            s for s in bad_answer["scenarios"] if s["scenario"] == "yudao_convention_write_readback_timeout"
        )["result"]["user_answer"] = "tampered"
        self.assertFalse(ce.compare_captures(self.capture_a, bad_answer, allow={"ops-03"})[0])


if __name__ == "__main__":
    unittest.main()
