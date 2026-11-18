"""The production executor combines raw source and current reads in one turn."""
from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from web_operation_agent.composition.binding import bind_application
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnExecutor, AgentTurnStatus
from web_operation_agent.execution import turn
from web_operation_agent.modeling.egress import ProviderEgressPolicy
from web_operation_agent.state.identity import OperationContext, PrincipalContext
from web_operation_agent.tests.execution.product_support import _Channel, _RemoteBrowser
from web_operation_agent.tests.support.business import _Chat
from web_operation_agent_yudao.host.application import YudaoApplication
from web_operation_agent_yudao.rules.repository import repository_source
from .test_goal import DOCS, _Client


class _ActorPageBrowser(_RemoteBrowser):
    def __init__(self, channel, **kwargs):
        super().__init__(channel, **kwargs)
        self.url = channel.latest_observation_envelope.observation.url


class AgentTurnTests(unittest.TestCase):
    def test_source_question_and_dynamic_record_query_share_one_verified_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "ui/src/views/crm/customer/detail/index.vue"
            path.parent.mkdir(parents=True)
            path.write_text('<el-tab-pane label="回款" />\n')
            app = YudaoApplication("https://app.test", None, DOCS, client=_Client(), conventions=None)
            bound = bind_application(app)
            now = int(time.time() * 1000)
            principal = PrincipalContext(
                schema_version=1, application_id="yudao", tenant_id="1", workspace_id="synthetic",
                actor_id="257", delegation_id="synthetic", issued_at=now - 1000,
                expires_at=now + 60_000, scopes=("yudao.read",))
            context = OperationContext.create(principal, session_id="product-session", turn_id="mixed",
                                              operation_id="metadata", deadline_ms=now + 30_000)
            reads = app.business_tools._executor
            entity = reads.execute("discover_entities", {"query": "客户"}, context=context).content[
                "candidates"][0]["entity_id"]
            name = next(f["field_id"] for f in reads.execute(
                "describe_entity", {"entity_id": entity}, context=context).content["fields"]
                        if f["label"] == "客户名称")
            chat = _Chat([
                ("lookup_source", {"query": "客户详情 回款"}),
                ("resolve_goal_candidates", {"goals": [
                    {"delivery": "answer", "subject_query": "客户", "reference": "云帆",
                     "detail_query": "客户名称"}]}),
                ("query_entity_records", {"entity_id": entity, "field_ids": [name], "filters": [
                    {"field_id": name, "operator": "contains", "value": "云帆"}]}),
                ("finish", {"outcome": "answered", "answer": {
                    "version": 1, "coverage": "complete", "conclusions": [
                        {"kind": "source_excerpt", "evidence_ref": "source#1",
                         "statement": "客户详情提供回款页签。", "support": '<el-tab-pane label="回款" />'},
                        {"kind": "field_value", "evidence_ref": "call3-record#1"}]}}),
            ])
            chat.egress_policy = ProviderEgressPolicy(
                session_handoff_keys=bound.ports.routes.session_handoff_query_keys)
            channel = _Channel(page_url="https://app.test/crm/customer", principal=principal)
            executor = AgentTurnExecutor(
                channel=channel, chat=chat, application=bound, control=ReadOnlyControl(),
                config=AgentTurnConfig(
                    supported_scope="Read source knowledge and current actor records.",
                    ledger_path=root / "audit.jsonl", require_identity=True,
                    plain_business_answer_min_page_actions=0,
                    source=repository_source(root, "synthetic-1")),
                run_id_factory=lambda _m: "mixed-source-records")
            with patch.object(turn, "RemoteBrowserClient", _ActorPageBrowser):
                result = executor.execute({"message_id": "mixed", "page_url": "https://app.test/crm/customer",
                                           "text": "客户详情有没有回款页签？云帆客户的完整名称是什么？"})
            self.assertEqual(AgentTurnStatus.FINISHED, result.status)
            self.assertIn("回款页签", result.user_answer)
            self.assertIn("云帆科技有限公司", result.user_answer)
            self.assertEqual(2, len(result.agent.verified_answer["conclusions"]))
            self.assertEqual(0, result.agent.page_actions)
            self.assertIsNone(executor.config.source.source_map)
            browser = _ActorPageBrowser.instances[-1]
            self.assertEqual(["observe", "observe", "observe"], browser.actions)


if __name__ == "__main__":
    unittest.main()
