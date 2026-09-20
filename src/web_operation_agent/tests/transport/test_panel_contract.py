"""The panel contract is one file, and these are the checks that keep it that way.

F8b was two sets of constants drifting apart until the panel enforced bounds the server did
not have. The answer is not "be careful": it is that the bounds, the copy, the state names
and the palette live in `panel_contract.json` and `design_tokens.json`, the server reads
them, the panel is handed them, and the assertions below refuse anything only one side
knows about.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from web_operation_agent.transport.panel_view import (collapse_timeline, describe_action, design_tokens,
                                            label_keys, panel_contract, panel_limits,
                                            project_event, project_history_record,
                                            token_custom_properties)
from web_operation_agent.execution.product import AgentTurnStatus
from web_operation_agent.transport import RemoteSessionChannel
from web_operation_agent.schemas import (SCHEMA_VERSIONS, ActionCommand,
                                         ActionEnvelope, TaskEvent)

WEB_RUNTIME = Path(__file__).resolve().parents[2] / "web_runtime"
FRONTEND = Path(__file__).resolve().parents[4] / "frontend"


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


def _luminance(colour: str) -> float:
    value = colour.lstrip("#")
    channels = (int(value[index:index + 2], 16) for index in (0, 2, 4))

    def linear(channel: int) -> float:
        scaled = channel / 255
        return scaled / 12.92 if scaled <= 0.04045 else ((scaled + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(foreground: str, background: str) -> float:
    first, second = _luminance(foreground), _luminance(background)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


class PanelContractTests(unittest.TestCase):
    def test_the_contract_loads_and_every_reference_inside_it_resolves(self):
        contract = panel_contract()
        self.assertEqual(contract["protocol"], 1)
        for state, tone in contract["panel_states"].items():
            self.assertIn(tone, contract["tones"], state)
        for name, mapping in contract["progress_states"].items():
            self.assertIn(mapping["panel_state"], contract["panel_states"], name)
        self.assertGreater(contract["layout"]["overlay_z_index"], 2_147_483_640)

    def test_the_empty_state_offers_only_promises_the_server_owns(self):
        """The suggestion chips are capability disclosure; they live here, not in a component.

        Each text must also fit the bound a typed goal is clipped to -- picking a chip
        should never land in the composer already truncated.
        """
        contract = panel_contract()
        suggestions = contract["suggestions"]
        self.assertTrue(suggestions)
        maximum = contract["limits"]["goal_max_chars"]
        for item in suggestions:
            self.assertTrue(str(item.get("text", "")).strip(), item)
            self.assertTrue(str(item.get("kind", "")).strip(), item)
            self.assertLessEqual(len(item["text"]), maximum, item["text"])

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

    def test_source_details_are_redacted_bounded_and_survive_history_projection(self):
        payload = {"state": "finished", "message": "12.50 USD",
                   "source_details": "token=secret-value\n" + "source " * panel_limits()["result_max_chars"]}
        live = project_event(_event("progress", payload), at=0)
        replay = project_history_record({"kind": "progress", "payload": payload}, index=1)
        self.assertEqual(live["source_details"], replay["source_details"])
        self.assertNotIn("secret-value", live["source_details"])
        self.assertLessEqual(len(live["source_details"]), panel_limits()["result_max_chars"])
        payload["state"] = "agent_started"
        self.assertNotIn("source_details", project_event(_event("progress", payload), at=0))

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

    def test_the_projection_shows_prose_and_never_a_capability(self):
        action = project_event(
            _action({"action": "fill", "args": {"ref": "e12", "value": "private-value"}}), at=7)
        assert action is not None
        self.assertEqual(action["text"], "填写页面字段：e12")
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
                         "打开页面：/orders")
        self.assertEqual(describe_action({"action": "submit", "args": {"ref": "e15"}}),
                         "提交页面表单：e15")
        self.assertEqual(describe_action({"action": "observe"}), "重新检查当前页面")
        self.assertNotIn("secret", describe_action(
            {"action": "fill", "args": {"ref": "e1", "value": "secret"}}))

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
        self.assertEqual(texts, ["列出订单", "重新检查当前页面", "打开页面：/a", "重新检查当前页面"])
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
                    sequence=turn * 40 + step + 1, turn_id=f"turn-{turn}"), at=0)
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
        self.assertEqual(configuration["suggestions"], contract["suggestions"])
        self.assertEqual(configuration["notices"], contract["notices"])
        self.assertEqual(configuration["progress_states"], contract["progress_states"])
        self.assertEqual(configuration["layout"], contract["layout"])
        self.assertEqual(configuration["terminal_error_codes"],
                         contract["terminal_error_codes"])

    def test_the_frontend_reads_the_same_file_rather_than_copying_it(self):
        """The panel's types are a view over the contract, not a second declaration."""
        if not FRONTEND.is_dir():
            self.skipTest("the frontend project is not present in this checkout")
        source = (FRONTEND / "src" / "contract" / "panel.ts").read_text(encoding="utf-8")
        self.assertIn("import raw from '@contract/panel_contract.json'", source)
        alias = (FRONTEND / "vite.config.ts").read_text(encoding="utf-8")
        self.assertIn("src/web_operation_agent/web_runtime", alias)
        for literal in ('"awaiting_confirmation"', "'awaiting_confirmation'"):
            self.assertNotIn(literal, source)


class DesignTokenTests(unittest.TestCase):
    def test_both_themes_meet_their_declared_contrast(self):
        """The palette is checkable, so a colour cannot be made inaccessible unnoticed."""
        tokens = design_tokens()
        failures = []
        for theme, palette in tokens["themes"].items():
            for requirement in tokens["contrast_requirements"]:
                ratio = _contrast(palette[requirement["foreground"]],
                                  palette[requirement["background"]])
                if ratio < requirement["minimum"]:
                    failures.append(
                        f"{theme}: {requirement['foreground']} on {requirement['background']}"
                        f" is {ratio:.2f}, needs {requirement['minimum']}")
        self.assertEqual(failures, [])

    def test_the_two_themes_declare_the_same_tokens(self):
        light, dark = design_tokens()["themes"]["light"], design_tokens()["themes"]["dark"]
        self.assertEqual(set(light), set(dark))

    def test_the_brand_disc_is_the_same_object_in_both_themes(self):
        """The launcher disc is one brand moment; a theme must not quietly fork it."""
        themes = design_tokens()["themes"]
        for name in ("brand-top", "brand-mid", "brand-bottom", "brand-ink"):
            self.assertEqual(themes["light"][name], themes["dark"][name], name)

    def test_the_generated_properties_carry_both_themes(self):
        css = token_custom_properties()
        self.assertIn("--woa-accent: #1d4ed8;", css)
        self.assertIn("prefers-color-scheme: dark", css)
        self.assertIn("--woa-accent: #7aa2ff;", css)

    def test_the_built_stylesheet_is_generated_from_the_same_palette(self):
        generated = FRONTEND / "src" / "styles" / "_tokens.generated.scss"
        if not generated.is_file():
            self.skipTest("the frontend tokens are not built in this checkout")
        text = generated.read_text(encoding="utf-8")
        for name, value in design_tokens()["themes"]["light"].items():
            self.assertIn(f"--woa-{name}: {value};", text)


class ASessionEndIsSaidNotCoded(unittest.TestCase):
    """A closed session left "任务结束（delegated_authority_changed）" in the timeline (P7.5)."""

    def test_every_reason_the_channel_closes_with_reads_as_a_sentence(self):
        reasons = ("principal_revoked", "principal_expired", "principal_validation_failed",
                   "session_expired", "browser_inactive", "native_bootstrap_replaced",
                   "delegated_authority_changed", "identity_authority_mismatch",
                   "host_stopped", "turn_capacity_exhausted", "session_closed",
                   "runner_setup_failed", "")
        for reason in reasons:
            with self.subTest(reason=reason):
                entry = project_event(_event("terminal", {"reason": reason}), at=0)
                self.assertEqual(entry["kind"], "result")
                self.assertIn("助手", entry["text"])
                self.assertIsNone(re.search(r"[A-Za-z_]{3,}", entry["text"]))


if __name__ == "__main__":
    unittest.main()
