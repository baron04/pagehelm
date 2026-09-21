"""A deployment that disables a page action must not lose every turn that reaches for it.

Two defaults combined into one failure: `tool_schemas()` offered every browser tool whatever
the action policy said, and `policy_block_limit` defaulted to 1, which ended the turn on the
first refusal -- before the recovery message the product already writes could be read. A
Skill/API deployment that disables clicks therefore lost any turn in which the model tried
one, and the model had no way to know clicks were unavailable.
"""
import unittest

import tempfile
from pathlib import Path

from web_operation_agent.execution.product import AgentTurnConfig
from web_operation_agent.schemas import POTENTIALLY_WRITING_ACTIONS
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.execution.session import LabApparatus, TOOL_SCHEMAS, ServerAgentSession, TaskSpec
from web_operation_agent.tests.support.execution import _Browser, _Chat, _Control, _obs


def _names(tools):
    return {tool["function"]["name"] for tool in tools}


class TheToolSurfaceFollowsTheActionPolicy(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.directory.cleanup()

    def _session(self, disabled):
        return ServerAgentSession(
            task=TaskSpec("T", "inspect"), lab=LabApparatus(arm="S"), chat=_Chat([]),
            browser=_Browser([_obs(1)]), control=_Control([[], []]),
            ledger=SessionLedger(Path(self.directory.name) / "audit.jsonl", "t"),
            disabled_actions=frozenset(disabled))

    def test_a_disabled_action_is_absent_from_the_tools_the_model_sees(self):
        offered = _names(self._session({"click", "fill"}).tool_schemas())

        self.assertNotIn("click", offered)
        self.assertNotIn("fill", offered)
        self.assertIn("goto", offered)

    def test_no_policy_leaves_every_tool_offered(self):
        self.assertEqual(_names(self._session(()).tool_schemas()), _names(TOOL_SCHEMAS))

    def test_a_run_with_every_page_action_disabled_can_still_observe_and_finish(self):
        offered = _names(self._session(
            {"click", "fill", "select", "submit", "back", "goto", "scroll",
             "wait_for"}).tool_schemas())

        self.assertIn("finish", offered)
        self.assertIn("observe", offered)


class TheRecoveryBudgetLetsTheRefusalReachTheModel(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.directory.cleanup()

    def _config(self, **changes):
        return AgentTurnConfig(
            supported_scope="read", ledger_path=Path(self.directory.name) / "audit.jsonl",
            **changes)

    def test_product_policy_fails_closed_for_every_potentially_writing_action(self):
        self.assertEqual(
            self._config().disabled_actions, POTENTIALLY_WRITING_ACTIONS)
        self.assertEqual(self._config(disabled_actions=frozenset()).disabled_actions,
                         frozenset())

    def test_the_default_survives_a_refusal(self):
        self.assertGreater(self._config().policy_block_limit, 1)

    def test_an_explicit_limit_is_still_honoured(self):
        self.assertEqual(
            self._config(policy_block_limit=1).policy_block_limit, 1)
        self.assertIsNone(
            self._config(policy_block_limit=None).policy_block_limit)

    def test_a_non_positive_limit_is_still_refused(self):
        with self.assertRaisesRegex(ValueError, "policy_block_limit"):
            self._config(policy_block_limit=0)


if __name__ == "__main__":
    unittest.main()
