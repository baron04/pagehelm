"""Fixture validity checks; no model calls and no live application data."""
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.tests.execution.product_support import _Channel
from web_operation_agent_yudao.application import YudaoApplication
from web_operation_agent_yudao.repository import repository_source
from web_operation_agent_yudao.ui_source import UiSource, ListView
from . import fixture
from .run import Browser


class FixtureTests(unittest.TestCase):
    def test_api_and_menu_metadata_bind_every_synthetic_collection(self):
        app = YudaoApplication(
            "http://localhost", None, fixture.documents(), client=fixture.Client(),
            ui=UiSource(views={"bpm/processInstance/index": ListView(
                columns=("name",), endpoint="/bpm/process-instance/my-page")}),
            conventions=None)
        tools = app.business_tools
        for query, count in (("客户", 3), ("合同", 6), ("供应商", 4), ("我的流程", 3)):
            with self.subTest(query=query):
                entity = tools.execute("discover_entities", {"query": query}, ()).content["candidates"][0]["entity_id"]
                name = tools.execute("describe_entity", {"entity_id": entity}, ()).content["fields"][0]["field_id"]
                result = tools.execute("query_entity_records", {
                    "entity_id": entity, "field_ids": [name]}, ())
                self.assertEqual(count, result.content["completeness"]["matched_count"])
        self.assertEqual(745000, sum(r["totalPrice"] for r in fixture.CONTRACTS))
        self.assertEqual(450000, sum(r["totalPrice"] for r in fixture.CONTRACTS
                                    if r["auditStatus"] == 20))

    def test_transport_double_preserves_application_origin_and_ready_route(self):
        browser = Browser(_Channel(page_url="http://localhost/index"), timeout_s=1)
        receipt = browser.execute("goto", {"url": "/crm/contract"}, expect_navigation=True)
        self.assertEqual("http://localhost/crm/contract", receipt.url_after)
        self.assertEqual(receipt.url_after, receipt.observation.url)
        self.assertEqual("ready", receipt.observation.render_state)

    def test_source_fixture_is_allowlisted_and_retains_cited_team_declaration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative, text in fixture.SOURCE.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            source = repository_source(root, "synthetic-dev-1")
            hits = source.lookup.search("仓储前端维护团队")
            self.assertTrue(any("北极星界面组" in hit.text for hit in hits))
            self.assertTrue(all(source.lookup.verify(hit.evidence) for hit in hits))
            self.assertTrue(source.history_unavailable_reason)


if __name__ == "__main__":
    unittest.main()
