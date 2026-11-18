"""Conversation context is bounded, redacted, and never treated as current authority."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.business.runtime import BusinessToolResult
from web_operation_agent.state.conversation import (ConversationState, SQLiteConversationState,
                                              prune_sqlite_conversations)


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
            "choices": [
                {"choice_id": "choice-1", "label": "Acme — London",
                 "record_ref": "company/1"},
                {"choice_id": "choice-2", "label": "Acme — Paris",
                 "record_ref": "company/2"},
            ],
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

    def test_page_operation_consent_stays_out_of_model_context_and_resets_on_clear(self):
        state = ConversationState()
        self.assertFalse(state.page_operation_granted)
        state.grant_page_operation()
        self.assertTrue(state.page_operation_granted)
        self.assertIsNone(state.model_context())
        state.record(
            message_id="m1", user_text="Open orders", assistant_text="Opened.",
            status="finished")
        self.assertNotIn("page_operation_granted", state.model_context())
        self.assertEqual(state.clear(), 1)
        self.assertFalse(state.page_operation_granted)


class DurableConversationStateTests(unittest.TestCase):
    def test_restart_restores_bounded_references_without_execution_authority(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "conversation.sqlite3"
            state = SQLiteConversationState(path, "odoo-stable-session")
            state.record(
                message_id="m1", user_text="打开 Acme", assistant_text="已打开 Acme",
                status="finished")

            restored = SQLiteConversationState(path, "odoo-stable-session")

            self.assertEqual(restored.turn_count, 1)
            context = restored.model_context()
            self.assertEqual(context["prior_turns"][0]["user"], "打开 Acme")
            encoded = json.dumps(context)
            for forbidden in ("approval", "capability", "clientToken", "api_key"):
                self.assertNotIn(forbidden, encoded)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_new_conversation_is_durable_and_other_session_is_isolated(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "conversation.sqlite3"
            first = SQLiteConversationState(path, "session-one")
            first.record(message_id="m1", user_text="one", assistant_text="done",
                         status="finished")
            second = SQLiteConversationState(path, "session-two")
            second.record(message_id="m2", user_text="two", assistant_text="done",
                          status="finished")
            self.assertEqual(first.clear(), 1)

            self.assertIsNone(SQLiteConversationState(
                path, "session-one").model_context())
            self.assertEqual(
                SQLiteConversationState(path, "session-two").model_context()
                ["prior_turns"][0]["user"], "two")

    def test_corrupt_persisted_context_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "conversation.sqlite3"
            state = SQLiteConversationState(path, "session-one")
            state.record(message_id="m1", user_text="one", assistant_text="done",
                         status="finished")
            with sqlite3.connect(path) as database:
                database.execute(
                    "UPDATE conversation_contexts SET payload=? WHERE conversation_id=?",
                    ('{"schema_version":1,"prior_turns":"wrong"}', "session-one"))
            with self.assertRaisesRegex(ValueError, "turns"):
                SQLiteConversationState(path, "session-one")

    def test_retention_prunes_only_old_conversation_rows(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "conversation.sqlite3"
            old = SQLiteConversationState(path, "old-session")
            old.record(message_id="m1", user_text="old", assistant_text="done",
                       status="finished")
            current = SQLiteConversationState(path, "current-session")
            current.record(message_id="m2", user_text="new", assistant_text="done",
                           status="finished")
            with sqlite3.connect(path) as database:
                database.execute(
                    "UPDATE conversation_contexts SET updated=1000 "
                    "WHERE conversation_id='old-session'")
                database.execute(
                    "UPDATE conversation_contexts SET updated=200000000 "
                    "WHERE conversation_id='current-session'")

            self.assertEqual(prune_sqlite_conversations(
                path, older_than_ms=100_000_000, now_ms=200_000_000), 1)
            self.assertIsNone(SQLiteConversationState(
                path, "old-session").model_context())
            self.assertEqual(SQLiteConversationState(
                path, "current-session").turn_count, 1)

    def test_sqlite_persists_page_operation_granted_and_resets_on_clear(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "conversation.sqlite3"
            state = SQLiteConversationState(path, "op-session")
            self.assertFalse(state.page_operation_granted)
            state.grant_page_operation()
            self.assertTrue(state.page_operation_granted)

            reopened = SQLiteConversationState(path, "op-session")
            self.assertTrue(reopened.page_operation_granted)
            self.assertIsNone(reopened.model_context())

            reopened.clear()
            self.assertFalse(reopened.page_operation_granted)
            self.assertFalse(
                SQLiteConversationState(path, "op-session").page_operation_granted)

    def test_sqlite_upgrades_legacy_table_without_page_operation_column(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "legacy.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute(
                    "CREATE TABLE conversation_contexts ("
                    "conversation_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated INTEGER NOT NULL)")
                db.execute(
                    "INSERT INTO conversation_contexts VALUES (?, ?, ?)",
                    ("legacy-session", '{"schema_version":1,"prior_turns":[]}', 1000))
            state = SQLiteConversationState(path, "legacy-session")
            self.assertFalse(state.page_operation_granted)
            state.page_operation_granted = True
            self.assertTrue(
                SQLiteConversationState(path, "legacy-session").page_operation_granted)


if __name__ == "__main__":
    unittest.main()
