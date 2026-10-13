"""Selected task knowledge retains ordinary reads, repair and mixed source answers."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.tools.registry import ToolRegistry
from web_operation_agent.composition import bind_application
from web_operation_agent.composition.business_operations import RegisteredOperations
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnStatus
from web_operation_agent.goal import executor as goal_executor
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.goal.ports import Aggregate, Fact, Resolved
from web_operation_agent.skills import SkillRegistry
from web_operation_agent.tests.execution.product_support import _Channel
from web_operation_agent.tests.goal.test_goal_turn import _Adapter, _AgreeingChat, _Browser, _Port
from web_operation_agent.tests.goal.test_repository_answers import Lookup, scope, source_reply


class SkillReadCompatibilityTests(unittest.TestCase):
    def run_turn(self, scripts, resolved, *, then=None, readings=1, source=False, request=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "workflow"
            package.mkdir()
            package.joinpath("SKILL.md").write_text(
                "---\nname: workflow\ndescription: Query synthetic orders and their workflow.\n---\n"
                "Preserve the full requested record scope; do not add an owner condition.\n")
            package.joinpath("agents").mkdir()
            package.joinpath("agents/woa.toml").write_text(
                'schema_version = 1\nscope_entities = ["order"]\n')
            registry = SkillRegistry.discover(root, namespace="fixture")
            adapter = _Adapter()
            adapter.business_tools.skill_registry = registry
            adapter.ports = replace(adapter.ports, operations=RegisteredOperations(
                ToolRegistry(()), registry, lambda entities: None))
            chat = _AgreeingChat(scripts)
            lookup = Lookup()
            ledger = root / "audit.jsonl"
            channel = _Channel(page_url=adapter.descriptor.origin + "/home")
            executor = GoalTurnExecutor(channel=channel,
                config=AgentTurnConfig(supported_scope="synthetic orders", ledger_path=ledger,
                    source=SourceCapability(runtime_source=True, lookup=lookup) if source else SourceCapability.none()),
                chat=chat, application=bind_application(adapter), goal_port=_Port(resolved, then),
                control=ReadOnlyControl(), run_id_factory=lambda message: "skill-read",
                readings=readings, repository_answers=source)
            with patch.object(goal_executor, "RemoteBrowserClient", _Browser):
                result = executor.execute({"message_id": "test", "text":
                    request or ("Count orders." if not source else "Read the order Total and explain the interface engineering owner."),
                    "page_url": channel.latest_observation_envelope.observation.url})
            events = [json.loads(line) for line in ledger.read_text().splitlines()]
            return result, chat, events

    def test_selected_skill_keeps_two_independent_reads_and_runtime_answer(self):
        result, chat, events = self.run_turn([
            ("declare_scope", {"parts": [{"kind": "aggregate", "entity": "order"}],
                }),
            ("declare_goal", {"deliverables": [{"kind": "aggregate", "entity": "order",
                "metric": {"op": "count"}}]})],
            [Resolved(kind="aggregate", aggregate=Aggregate(subject="orders", count=17))], readings=2)
        self.assertEqual(result.status, AgentTurnStatus.FINISHED, result.user_answer)
        self.assertIn("17", result.user_answer)
        self.assertEqual(len(chat.messages), 3)
        for messages in chat.messages[1:]:
            self.assertIn("Preserve the full requested record scope", json.dumps(messages))
        kinds = [event["event"] for event in events]
        self.assertEqual(kinds.count("business_skill_loaded"), 1)
        self.assertIn("goal_skill_knowledge_applied", kinds)
        self.assertNotIn("skill_task_declared", kinds)

    def test_named_record_rule_request_keeps_runtime_interpreter_instead_of_static_excerpt(self):
        result, chat, events = self.run_turn([
            ("declare_scope", {"parts": [{"kind": "fact", "entity": "order", "rules": True,
                "names": ["Synthetic order"]}]}),
            ("declare_goal", {"deliverables": [{"kind": "fact", "entity": "order",
                "record": "Synthetic order", "fields": ["Total"]}]})],
            [Resolved(kind="fact", facts=(Fact("Synthetic order", "Total", "17"),))],
            request="Read Synthetic order's Total to check its workflow.")
        self.assertEqual(result.status, AgentTurnStatus.FINISHED, result.user_answer)
        self.assertIn("17", result.user_answer)
        self.assertIn("goal_skill_knowledge_applied", {event["event"] for event in events})
        self.assertNotIn("skill_task_declared", {event["event"] for event in events})

    def test_selected_skill_does_not_drop_bounded_resolution_repair(self):
        result, chat, events = self.run_turn([
            ("declare_scope", {"parts": [{"kind": "aggregate", "entity": "order"}],
                }),
            ("declare_goal", {"deliverables": [{"kind": "aggregate", "entity": "order",
                "metric": {"op": "count"}}]}),
            ("declare_goal", {"deliverables": [{"kind": "aggregate", "entity": "order",
                "metric": {"op": "count"}}]})],
            [Resolved(kind="aggregate", problem="Synthetic unresolved scope", repair="Resolve the complete order scope.")],
            then=[Resolved(kind="aggregate", aggregate=Aggregate(subject="orders", count=17))])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED, result.user_answer)
        self.assertIn("17", result.user_answer)
        self.assertTrue(any(event["event"] == "goal_repaired" and event["accepted"] for event in events))

    def test_selected_skill_preserves_business_and_repository_obligations(self):
        declaration = scope([{"kind": "fact", "entity": "order"}])
        result, chat, events = self.run_turn([declaration,
            ("declare_goal", {"deliverables": [{"kind": "fact", "entity": "order", "fields": ["Total"]}]}),
            ("declare_source_queries", {"queries_0": []}), source_reply()],
            [Resolved(kind="fact", facts=(Fact("Synthetic order", "Total", "17"),))], source=True)
        self.assertEqual(result.status, AgentTurnStatus.FINISHED, result.user_answer)
        self.assertIn("17", result.user_answer)
        self.assertIn("Aurora team", result.user_answer)
        self.assertIn("docs/owners.md", result.user_answer)
        kinds = [event["event"] for event in events]
        self.assertIn("goal_source_answer_verified", kinds)
        self.assertNotIn("skill_task_declared", kinds)
