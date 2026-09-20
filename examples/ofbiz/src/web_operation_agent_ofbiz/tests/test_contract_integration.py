"""The OFBiz compiler's output, read by the core retriever and planner.

These two cases used to live in the core suite, where they forced the core package's tests
to install this distribution. They belong here: each one writes OFBiz descriptor XML,
compiles it, and asserts what a *compiled* OFBiz tree makes available downstream. The core
suite now tests the same downstream behaviour against a compiler-free relation fixture with
a different route grammar, so neither side is a proxy for the other.
"""
import json
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.planning.contract import ContractPlanner, _segment_from_template
from web_operation_agent.planning.retrieval import SourceRetriever
from web_operation_agent.planning.sourcetree import SourceTree
from web_operation_agent.tests.support.chat_port_contract import CancellableChatDouble

from web_operation_agent_ofbiz.route import OFBIZ_ROUTE_PORT
from web_operation_agent_ofbiz.sourcemap import OFBizCompiler
from web_operation_agent_ofbiz.tests.test_sourcemap import POLICY, build

TASK = "The tray in bay four needs a new cultivar sprouted into it."
SEGMENT = _segment_from_template(OFBIZ_ROUTE_PORT.contract_route_template)


class _StubModel(CancellableChatDouble):
    """Answers with whatever the test queued. Records what it was asked."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.budgets = []

        class _Meter:
            def report(self):
                return {"provider_calls": 0}
        self.meter = _Meter()

    def complete(self, messages, tools, run_id, max_tokens=None, options=None,
                 timeout_s=None):
        self.calls.append([dict(m) for m in messages])
        self.max_tokens = max_tokens
        self.budgets.append(max_tokens)
        reply = self.replies.pop(0)
        if isinstance(reply, dict):
            return {"content": "", "finish_reason": "stop", "reasoning": "", **reply}
        return {"content": reply, "finish_reason": "stop", "reasoning": ""}


class CompiledRetrieval(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        build(Path(self.tmp.name), ("greenhouse", "lighthouse"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_alternate_create_target_reaches_the_bundle_with_its_condition(self):
        path = Path(self.tmp.name) / "applications/greenhouse/widget/SeedlingEditor.xml"
        path.write_text('''<forms>
          <form name="SeedlingEditor" target="Seedling/update">
            <alt-target target="Seedling/create" use-when="seedling==null"/>
            <field name="trayId"><text/></field>
          </form>
        </forms>''')
        smap = OFBizCompiler(SourceTree(POLICY, Path(self.tmp.name)), "fixture").compile()
        bundle = SourceRetriever(smap).retrieve(
            "T", "create a seedling using the SeedlingEditor form")
        self.assertIn("uses alternate request `Seedling/create` when `seedling==null`",
                      bundle.render())
        alternate = next(r for r in smap.by_kind("form_target")
                         if r.object == "Seedling/create")
        self.assertIn(alternate.evidence[0].key, bundle.citation_keys)

    def test_controller_continuations_reach_planner_with_condition_and_identity(self):
        """A form submit may continue to a second handler without another browser action.

        Use invented names and an error branch so retrieval cannot learn one task's answer
        or silently present every continuation as a successful response.
        """
        path = (Path(self.tmp.name)
                / "applications/greenhouse/webapp/glasshouse/WEB-INF/controller.xml")
        path.write_text('''<site-conf>
          <request-map uri="sproutSeedling"><event type="service" invoke="sproutSeedling"/>
            <response name="success" type="request" value="attachSeedlingToTray"/>
            <response name="error" type="request" value="restoreTrayForm"/>
            <response name="other" type="view" value="NotARequest"/>
            <response name="redirect" type="request-redirect" value="BrowserRedirect"/>
          </request-map>
        </site-conf>''')
        tree = SourceTree(POLICY, Path(self.tmp.name))
        smap = OFBizCompiler(tree, "fixture").compile()
        bundle = SourceRetriever(smap).retrieve("T", TASK)
        lines = [line for line in bundle.lines if "declares type=request," in line]
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("`success`" in line and "`attachSeedlingToTray`" in line
                            for line in lines))
        self.assertTrue(any("`error`" in line and "`restoreTrayForm`" in line
                            for line in lines))
        self.assertTrue(all("`glasshouse/sproutSeedling`" in line
                            and "not a second browser submission" in line for line in lines))
        self.assertTrue(all("NotARequest" not in line and "BrowserRedirect" not in line
                            for line in lines))
        for ref in bundle.refs.values():
            self.assertTrue(tree.verify(ref))
        key = next(r.evidence[0].key for r in smap.by_kind("request_handler")
                   if r.subject == "glasshouse/sproutSeedling")
        model = _StubModel([json.dumps(
            {"task_ref": "T", "summary": "fixture",
             "mutation": {"webapp": "glasshouse", "request": "sproutSeedling",
                          "citations": [key]},
             "citations": [key]})])
        ContractPlanner(model, smap, segment=SEGMENT).plan(
            "T", TASK, bundle, "fixture-chain")
        planner_input = model.calls[0][1]["content"]
        self.assertTrue(all(line in planner_input for line in lines),
                        "compiled facts must reach the actual planner call, not only the bundle")


if __name__ == "__main__":
    unittest.main()
