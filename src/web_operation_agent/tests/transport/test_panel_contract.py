"""The panel contract is one file, and these are the checks that keep it that way.

F8b was two sets of constants drifting apart until the panel enforced limits the server did
not have. The answer is not "be careful": it is that the text limits, the copy and the state
names live in `panel_contract.json`, the server reads them, the panel is handed them, and
the assertions below refuse anything only one side knows about. The palette is not here:
it is the frontend's, and `frontend/tests/tokens.unit.test.ts` checks it.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from web_operation_agent.transport.panel_view import (action_target, collapse_timeline,
                                                       describe_action, describe_step, label_keys,
                                                       panel_contract, panel_limits,
                                                       project_event, project_history_record,
                                                       step_facts)
from web_operation_agent.execution.product import AgentTurnStatus
from web_operation_agent.transport import RemoteSessionChannel
from web_operation_agent.schemas import (SCHEMA_VERSIONS, ActionCommand,
                                         ActionEnvelope, TaskEvent)



def _event(kind: str, payload: dict, *, sequence: int = 1, turn_id: str = "turn-1") -> TaskEvent:
    return TaskEvent(schema_version=SCHEMA_VERSIONS["TaskEvent"],
                     event_id=f"e{sequence:08d}", session_id="session",
                     sequence=sequence, kind=kind, turn_id=turn_id, payload=payload)


def _action(command: dict, *, sequence: int = 1, turn_id: str = "turn-1") -> TaskEvent:
    """A real envelope: TaskEvent validates action payloads, and so should a test."""
    action_command = ActionCommand.from_json(
        {"schema_version": SCHEMA_VERSIONS["ActionCommand"],
         "command_id": f"action-{sequence}", **command})
    envelope = ActionEnvelope(
        schema_version=SCHEMA_VERSIONS["ActionEnvelope"], session_id="session",
        turn_id=turn_id, action_id=f"action-{sequence}",
        based_on_observation_id="client:1", page_epoch=1,
        deadline=4_102_444_800_000, command_digest=action_command.content_digest,
        command=action_command)
    return TaskEvent.for_action(event_id=f"e{sequence:08d}", sequence=sequence,
                                action=envelope)


class PanelContractTests(unittest.TestCase):
    def test_the_contract_loads_and_every_reference_inside_it_resolves(self):
        contract = panel_contract()
        self.assertEqual(contract["protocol"], 2)
        for state, tone in contract["panel_states"].items():
            self.assertIn(tone, contract["tones"], state)
        for name, mapping in contract["progress_states"].items():
            self.assertIn(mapping["panel_state"], contract["panel_states"], name)

    def test_the_contract_holds_only_what_the_server_reads(self):
        """What only the panel reads is the frontend's (`frontend/src/presentation.json`)."""
        for key in ("layout", "approval_visibility", "approval_kinds", "start",
                    "entry_kinds", "suggestions"):
            self.assertNotIn(key, panel_contract())

    def test_session_lifecycle_metadata_is_not_rendered_as_an_empty_turn(self):
        payload = {"state": "session_recovered", "message": "会话已重新连接"}
        self.assertIsNone(project_event(_event("progress", payload), at=123))
        self.assertIsNone(project_history_record(
            {"kind": "progress", "payload": payload}, index=1))

    def test_every_panel_state_has_a_status_label(self):
        labels = label_keys()
        for state in panel_contract()["panel_states"]:
            key = "status" + re.sub(r"(^|_)([a-z])",
                                    lambda match: match.group(2).upper(), state)
            self.assertIn(key, labels, state)

    def test_the_server_emits_exactly_the_progress_states_the_panel_maps(self):
        """Anything the product can publish must have a projection, and nothing else may.

        The old panel carried entries for `verified`, `not_verified` and `knowledge_used` --
        states no code path has ever published. They were dead
        branches that looked like coverage.
        """
        mapped = set(panel_contract()["progress_states"])
        published = {status.value for status in AgentTurnStatus} | {
            "waiting_for_runtime", "session_recovered", "message_accepted",
            "new_conversation", "agent_started", "step", "approval_recorded", "cancelling",
            "task_failed",
        }
        self.assertEqual(mapped, published)

    def test_an_unmapped_progress_state_is_refused_rather_than_shown_generically(self):
        with self.assertRaisesRegex(ValueError, "no panel projection"):
            project_event(_event("progress", {"state": "invented"}), at=0)

    def test_an_expired_confirmation_explains_why_the_turn_stopped_live_and_after_restart(self):
        unanswered = {"state": "cancelled", "terminal_reason": "approval_expired",
                      "message": "已取消。"}
        for entry in (project_event(_event("progress", unanswered), at=0),
                      project_history_record({"kind": "progress", "payload": unanswered},
                                             index=1)):
            self.assertEqual(entry["kind"], "result")
            self.assertIn(panel_contract()["notices"]["approval_expired"], entry["text"])
            self.assertIn("已取消。", entry["text"])
            self.assertEqual(entry["panel_state"], "cancelled")
        stopped = {**unanswered, "terminal_reason": "user_cancelled"}
        self.assertEqual(project_event(_event("progress", stopped), at=0)["text"], "已取消。")

    def test_a_clarification_is_a_terminal_turn_that_keeps_the_session_ready(self):
        entry = project_event(_event("progress", {
            "state": "needs_input",
            "message": "Which Acme?\n1. London\n2. Paris",
        }), at=0)
        self.assertEqual(entry["panel_state"], "needs_input")
        self.assertEqual(entry["tone"], "warning")
        self.assertIn("1. London", entry["text"])

    def test_panel_capacity_accepts_the_largest_supported_product_answer(self):
        from web_operation_agent.execution.product import AgentTurnConfig
        config = AgentTurnConfig(supported_scope="read", ledger_path=Path("unused.jsonl"),
                                 max_user_answer_chars=panel_limits()["result_max_chars"])
        text = "x" * (config.max_user_answer_chars - 20) + "\nEND OF ANSWER"
        entry = project_event(_event("progress", {"state": "finished", "message": text}), at=0)
        self.assertEqual(entry["text"], text)

    def test_answer_navigation_is_a_separate_bounded_terminal_projection(self):
        target = {"kind": "record", "address": "/contacts/7", "label": "Mira",
                  "identity": "contacts:7", "read_ref": "read#1"}
        payload = {"state": "finished", "message": "Mira is contact 7",
                   "answer_targets": [target]}
        live = project_event(_event("progress", payload), at=0)
        recovered = project_history_record({"kind": "progress", "payload": payload}, index=1)
        self.assertEqual(live["answer_targets"], [target])
        self.assertEqual(recovered["answer_targets"], [target])
        for address in ("https://outside.test/", "//outside.test/", "/contacts/7?token=x"):
            with self.subTest(address=address):
                invalid = {**payload, "answer_targets": [{**target, "address": address}]}
                self.assertNotIn("answer_targets", project_event(_event("progress", invalid), at=0))
        self.assertNotIn("answer_targets", project_event(_event("progress", {
            **payload, "state": "needs_input"}), at=0))

    def test_the_projection_shows_prose_and_never_a_capability(self):
        action = project_event(
            _action({"action": "fill", "args": {"ref": "e12", "value": "private-value"}}), at=7)
        assert action is not None
        self.assertEqual(action["text"], "填写页面上的输入框")
        self.assertNotIn("private-value", json.dumps(action, ensure_ascii=False))

        approval = project_event(_event("approval_required", {
            "schema_version": 1, "approval_id": "a-1", "effect": "Create company",
            "targets": ["Acme"], "expires_at": 4_102_444_800_000,
        }), at=8)
        assert approval is not None
        # Presence only: the card's content travels on the timeline's own pending field.
        self.assertEqual(approval["kind"], "approval")
        self.assertNotIn("Acme", json.dumps(approval, ensure_ascii=False))

        self.assertIsNone(project_event(_event("session_started", {"state": "x"}), at=0))

    def test_action_prose_is_readable_and_never_echoes_a_filled_value(self):
        self.assertEqual(describe_action({"action": "goto", "args": {"url": "/orders"}}),
                         "打开页面")
        self.assertEqual(describe_action({"action": "submit", "args": {"ref": "e15"}}),
                         "提交页面表单")
        self.assertEqual(describe_action({"action": "observe"}), "重新检查当前页面")
        self.assertEqual(describe_action({"action": "reveal", "args": {"ref": "e13"}},
                                         {"name": "Actions", "role": "button"}),
                         "展开“Actions”菜单")
        self.assertEqual(describe_action({"action": "reveal", "args": {"ref": "e13"}}),
                         "展开页面上的菜单")
        self.assertNotIn("secret", describe_action(
            {"action": "fill", "args": {"ref": "e1", "value": "secret"}}))

    def test_a_control_is_named_as_its_page_names_it_never_by_ref_or_path(self):
        page = {"elements": [
            {"ref": "e3", "role": "button", "name": "确认", "tag": "button", "epoch": 1},
            {"ref": "e4", "role": "link", "name": "客户", "tag": "a", "epoch": 1,
             "attrs": {"href": "/odoo/customers"}},
            {"ref": "e5", "role": "textbox", "name": "邮箱", "tag": "input", "epoch": 1},
            {"ref": "e6", "role": "select", "name": "状态", "tag": "select", "epoch": 1},
            {"ref": "e7", "role": "row", "name": "S00002  Acme Corporation  2026-09-03  "
             "Mitchell Admin  1,231.00", "tag": "tr", "epoch": 1},
            {"ref": "e8", "role": "button", "name": "", "tag": "button", "epoch": 1},
            {"ref": "e9", "role": "td", "name": "S00007", "tag": "td", "epoch": 1},
            {"ref": "e10", "role": "article", "name": "Acme Corporation", "tag": "article",
             "epoch": 1},
        ]}

        def said(action: str, **args) -> str:
            command = {"action": action, "args": args}
            return describe_action(command, action_target(command, page))

        self.assertEqual(said("click", ref="e3"), "点击“确认”按钮")
        self.assertEqual(said("click", ref="e4"), "点击“客户”链接")
        self.assertEqual(said("fill", ref="e5", value="a@example.com"), "填写“邮箱”")
        self.assertEqual(said("select", ref="e6", value="sale"), "在“状态”中选择")
        self.assertEqual(said("submit", ref="e3"), "提交“确认”")
        self.assertEqual(said("goto", url="/odoo/customers/"), "打开“客户”")
        self.assertEqual(said("goto", url="/odoo/customers/13"), "打开页面")
        # A row is named by its first words, not the whole line of cells.
        self.assertEqual(said("click", ref="e7"), "点击“S00002 Acme Corporation…”所在行")
        self.assertEqual(said("click", ref="e9"), "点击“S00007”所在行")
        self.assertEqual(said("click", ref="e10"), "点击“Acme Corporation”卡片")
        self.assertEqual(said("click", ref="e8"), "点击页面上的按钮")
        self.assertEqual(said("click", ref="e99"), "点击页面上的控件")
        for text in (said("click", ref="e3"), said("fill", ref="e5", value="x")):
            self.assertNotRegex(text, r"\be\d+\b")

    def test_only_a_line_that_says_it_is_working_is_marked_transient(self):
        def step(facts: dict) -> dict:
            entry = project_event(_event("progress", {
                "state": "step", "message": "x", "step": facts}), at=0)
            assert entry is not None
            return entry
        self.assertTrue(step({"stage": "thinking"})["transient"])
        for facts in ({"stage": "read_done", "tool": "query_customers"},
                      {"stage": "tool", "tool": "query_customers"},
                      {"stage": "write_done"}):
            self.assertNotIn("transient", step(facts))

    def test_a_search_says_what_is_looked_for_and_for_which_word(self):
        self.assertEqual(describe_step({"stage": "search", "what": "page", "term": "订单"}), "正在查找页面：订单")
        self.assertEqual(describe_step({"stage": "search", "what": "source", "term": " 客户 \n合同 "}),
                         "正在查找数据来源：客户 合同")
        self.assertEqual(describe_step({"stage": "search", "what": "record", "term": "晨" * 40}),
                         "正在查找记录：" + "晨" * 24)
        self.assertEqual(describe_step({"stage": "search", "what": "page"}), "正在查找页面")
        # What is looked for is one of the three the panel has a word for, or nothing is said.
        self.assertEqual(describe_step({"stage": "search", "what": "secrets", "term": "x"}), "")
        self.assertEqual(describe_step({"stage": "search", "what": "page", "term": "a?token=abc123"}),
                         "正在查找页面：a?token=[redacted]")

    def test_the_closing_check_is_said_and_a_search_s_node_carries_what_was_asked(self):
        self.assertEqual(describe_step({"stage": "check"}), "正在核对结果")
        facts = {"stage": "search", "what": "page", "term": "订单", "tool": "search_menu",
                 "arguments": {"keyword": "订单", "nested": {"x": 1}}}
        self.assertEqual(step_facts(facts), {"stage": "search", "tool": "search_menu", "arguments": "keyword=订单"})
        entry = project_event(_event("progress", {
            "state": "step", "message": describe_step(facts), "step": step_facts(facts)}), at=0)
        self.assertEqual((entry["kind"], entry["text"], entry["panel_state"]), ("progress", "正在查找页面：订单", "acting"))
        self.assertNotIn("transient", entry)

    def test_repeats_collapse_without_anything_being_discarded(self):
        entries = [
            project_event(_event("progress", {"state": "message_accepted", "text": "列出订单"},
                                 sequence=1), at=1),
            project_event(_action({"action": "observe", "args": {}}, sequence=2), at=2),
            project_event(_action({"action": "observe", "args": {}}, sequence=3), at=3),
            project_event(_action({"action": "goto", "args": {"url": "/a"}}, sequence=4), at=4),
            project_event(_action({"action": "observe", "args": {}}, sequence=5), at=5),
            project_event(_action({"action": "observe", "args": {}},
                                  sequence=6, turn_id="turn-2"), at=6),
        ]
        collapsed = collapse_timeline([entry for entry in entries if entry is not None])
        texts = [entry["text"] for entry in collapsed]
        # Once per turn for the re-check line, and the goal and the goto both survive.
        self.assertEqual(texts, ["列出订单", "重新检查当前页面", "打开页面", "重新检查当前页面"])
        self.assertEqual(len([entry for entry in collapsed if entry["turn_id"] == "turn-2"]), 1)

    def test_the_timeline_no_longer_truncates_by_turn_or_by_entry(self):
        """Q6: the caps were truncation, so they went. Paging is the client's job now."""
        self.assertNotIn("timeline_max_turns", panel_limits())
        self.assertNotIn("timeline_max_entries_per_turn", panel_limits())
        wide = []
        for turn in range(20):
            for step in range(40):
                entry = project_event(_action(
                    {"action": "goto", "args": {"url": f"/{turn}/{step}"}},
                    sequence=turn * 40 + step + 1, turn_id=f"turn-{turn}"), at=0,
                    target={"name": f"{turn}-{step}", "role": "link"})
                assert entry is not None
                wide.append(entry)
        self.assertEqual(len(collapse_timeline(wide)), 800)

    def test_history_from_a_previous_incarnation_replays_progress_only(self):
        self.assertIsNone(project_history_record(
            {"kind": "action", "payload": {"state": "not_replayable"}}, index=1))
        goal = project_history_record({
            "kind": "progress", "turn_id": "turn-1", "at": 12,
            "payload": {"state": "message_accepted", "text": "列出订单"},
        }, index=1)
        assert goal is not None
        self.assertEqual((goal["kind"], goal["text"], goal["replayed"], goal["at"]),
                         ("goal", "列出订单", True, 12))

    def test_the_hello_reply_carries_every_bound_and_string_the_panel_renders(self):
        configuration = RemoteSessionChannel(
            "contract-probe", ("https://app.test",))._panel_configuration()
        contract = panel_contract()
        self.assertEqual(configuration["limits"], contract["limits"])
        self.assertEqual(configuration["labels"], contract["label_keys"])
        # The empty panel suggests nothing: fixed goals named records a deployment may not have.
        self.assertNotIn("suggestions", configuration)
        self.assertEqual(configuration["notices"], contract["notices"])
        self.assertEqual(configuration["progress_states"], contract["progress_states"])
        self.assertEqual(configuration["terminal_error_codes"],
                         contract["terminal_error_codes"])
        self.assertEqual(configuration["nonterminal_error_states"], contract["nonterminal_error_states"])



class ASessionEndIsSaidNotCoded(unittest.TestCase):
    """A closed session left "任务结束（delegated_authority_changed）" in the timeline (P7.5)."""

    def test_a_session_that_lapsed_leaves_no_line(self):
        """It comes back as it was on the next message; there is nothing to tell."""
        for reason in ("principal_expired", "session_expired", "browser_inactive",
                       "native_bootstrap_replaced", "host_stopped"):
            with self.subTest(reason=reason):
                self.assertIsNone(project_event(_event("terminal", {"reason": reason}), at=0))

    def test_every_reason_the_channel_closes_with_reads_as_a_sentence(self):
        reasons = ("principal_revoked", "principal_validation_failed",
                   "delegated_authority_changed", "identity_authority_mismatch",
                   "turn_capacity_exhausted", "session_closed",
                   "runner_setup_failed", "")
        for reason in reasons:
            with self.subTest(reason=reason):
                entry = project_event(_event("terminal", {"reason": reason}), at=0)
                self.assertEqual(entry["kind"], "result")
                self.assertIn("助手", entry["text"])
                self.assertIsNone(re.search(r"[A-Za-z_]{3,}", entry["text"]))


if __name__ == "__main__":
    unittest.main()
