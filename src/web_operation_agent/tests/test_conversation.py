"""Conversation context is bounded, redacted, and never treated as current authority."""
from __future__ import annotations

import unittest

from web_operation_agent.business import BusinessToolResult
from web_operation_agent.conversation import ConversationState


def _result(content: dict | None = None) -> BusinessToolResult:
    return BusinessToolResult(
        content=content or {
            "companies": [{"name": "Acme", "domain": "acme.test",
                           "ui_path": "/object/company/acme"}],
            "completeness": {"source_complete": True},
        },
        audit={"operation": "query_companies"},
        allowed_routes=("/object/company/acme",) if content is None else (),
    )


class ConversationStateTests(unittest.TestCase):
    def test_clarification_is_bounded_and_survives_only_until_task_resolution(self):
        state = ConversationState()
        clarification = {
            "schema_version": 1,
            "clarification_id": "clarify-123",
            "task_id": "task-123",
            "question": "Which Acme did you mean?",
            "response_kind": "single_choice",
            "choices": ["Acme — London", "Acme — Paris"],
        }

        state.record(
            message_id="message-1", user_text="Open Acme",
            assistant_text=clarification["question"], status="needs_input",
            clarification=clarification,
        )

        self.assertEqual(state.pending_clarification, clarification)
        context = state.model_context()
        self.assertEqual(context["pending_clarification"], clarification)
        state.record(
            message_id="message-2", user_text="The London one",
            assistant_text="I could not refresh it.", status="failed",
        )
        self.assertEqual(state.pending_clarification, clarification)
        state.record(
            message_id="message-3", user_text="Try the London one again",
            assistant_text="Opened Acme — London.", status="finished",
        )
        self.assertIsNone(state.pending_clarification)

    def test_clarification_cannot_be_smuggled_into_another_terminal_state(self):
        state = ConversationState()
        with self.assertRaisesRegex(ValueError, "only valid"):
            state.record(
                message_id="message-1", user_text="Open Acme",
                assistant_text="Done", status="finished",
                clarification={"unexpected": True},
            )

    def test_finished_turn_retains_bounded_business_facts_and_redacts_handoffs(self):
        state = ConversationState(max_turns=2)
        state.record(
            message_id="m1",
            user_text="List companies from /app?token=private-value",
            assistant_text="Acme; password=hunter2",
            status="finished",
            business_result=_result(),
        )

        context = state.model_context()
        turn = context["prior_turns"][0]
        self.assertIn("token=[redacted]", turn["user"])
        self.assertIn("password=[redacted]", turn["assistant"])
        self.assertEqual(turn["prior_business_facts"]["companies"][0]["name"], "Acme")
        self.assertNotIn("private-value", str(context))
        self.assertNotIn("hunter2", str(context))

    def test_old_turns_and_large_business_payloads_are_omitted_deterministically(self):
        state = ConversationState(
            max_turns=2, max_bytes=2_000, max_business_bytes=100)
        for index in range(3):
            state.record(
                message_id=f"m{index}", user_text=f"Question {index}",
                assistant_text=f"Answer {index}", status="finished",
                business_result=_result({"rows": ["x" * 150]}),
            )

        context = state.model_context()
        self.assertEqual(
            [turn["message_id"] for turn in context["prior_turns"]], ["m1", "m2"])
        self.assertIn("omitted", context["prior_turns"][0]["prior_business_facts"])
        self.assertEqual(state.turn_count, 2)

    def test_nonfinished_turn_does_not_retain_business_facts(self):
        state = ConversationState()
        state.record(
            message_id="m1", user_text="Create Acme", assistant_text="Outcome unknown",
            status="indeterminate", business_result=_result())
        self.assertNotIn(
            "prior_business_facts", state.model_context()["prior_turns"][0])


if __name__ == "__main__":
    unittest.main()
