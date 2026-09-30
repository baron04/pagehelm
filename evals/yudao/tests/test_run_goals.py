"""The yudao evaluation runner's scoring of cited rules and its report by task type."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from evals.odoo.goals.judge import score
from evals.odoo.goals.source_citations import bound_source
from evals.yudao.run_goals import check_run_contract, check_task_coverage, summarize
from web_operation_agent_yudao.client import YudaoAccount

_VUE = "ui/src/views/crm/contract/index.vue"
_LINES = "<el-button\n  v-if=\"scope.row.auditStatus === 0\"\n>提交审核</el-button>\n"


def _row(task, outcome, kind, category="answer"):
    return {"task": task, "outcome": outcome, "success": outcome in ("correct", "truthful_refusal"),
            "category": category, "type": kind, "answered_or_claimed": True, "asked": False,
            "provider_calls": 3, "cost_cny": 0.005, "input_tokens": 9000, "output_tokens": 800,
            "terminal_reason": "agent_finished", "write_detected": False}


class RunnerTests(unittest.TestCase):
    def test_preflight_requires_the_actor_index_to_cover_possible_tasks(self):
        doc = {"tasks": [
            {"type": "navigation", "entity": "product/spu",
             "truth": {"page": "/mall/product/spu"}},
            {"type": "composite", "truth": {"oracle": {"assertions": [
                ["pay/app", None, "应用", 1]]}}},
            {"type": "impossible", "entity": "not-installed"},
        ]}
        incomplete = SimpleNamespace(
            entities=[SimpleNamespace(key="pay/app")], pages=[])
        with self.assertRaisesRegex(RuntimeError, "product/spu"):
            check_task_coverage(doc, incomplete)
        complete = SimpleNamespace(
            entities=[SimpleNamespace(key=key) for key in ("pay/app", "product/spu")],
            pages=[SimpleNamespace(key="/mall/product/spu")])
        check_task_coverage(doc, complete)

    def test_a_sealed_holdout_refuses_another_origin_actor_or_snapshot(self):
        environment = {"base_url": "http://localhost:48088", "tenant_id": 1,
                       "actor": {"username": "reader"}}
        doc = {"environment": environment}
        seal = {"environment": environment, "snapshot": {"sha256": "sealed"}}
        account = YudaoAccount("reader", "synthetic", "1")
        with self.assertRaisesRegex(RuntimeError, "requires --snapshot"):
            check_run_contract(doc, seal, origin="http://localhost:48088",
                               account=account, snapshot=None)
        with self.assertRaisesRegex(RuntimeError, "browser origin differs"):
            check_run_contract(doc, seal, origin="http://localhost:48189",
                               account=account, snapshot=Path("synthetic.sql.gz"))
        with self.assertRaisesRegex(RuntimeError, "sealed actor"):
            check_run_contract(doc, seal, origin="http://localhost:48088",
                               account=YudaoAccount("other", "synthetic", "1"),
                               snapshot=Path("synthetic.sql.gz"))
        check_run_contract(doc, seal, origin="http://localhost:48088",
                           account=account, snapshot=Path("synthetic.sql.gz"))

    def test_the_report_counts_each_task_type_on_its_own(self):
        report = summarize([_row("a", "correct", "source_rule"), _row("b", "wrong", "source_rule"),
                            _row("c", "correct", "multi_condition")])
        self.assertEqual(report["success"], 2)
        self.assertEqual(report["by"]["type"]["source_rule"]["success"], 1)
        self.assertEqual(report["by"]["type"]["source_rule"]["attempts"], 2)
        self.assertEqual(report["by"]["type"]["multi_condition"]["outcomes"], {"correct": 1})

    def test_a_rule_answer_counts_only_with_a_citation_of_the_bound_lines(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / _VUE
            path.parent.mkdir(parents=True)
            path.write_bytes(_LINES.encode())
            doc = {"source_binding": {"deployment_release": "v2026.08", "citation_releases": ["v2026.08"],
                                      "files": {_VUE: hashlib.sha256(_LINES.encode()).hexdigest()}}}
            citations = bound_source(doc, Path(root), "v2026.08")
            task = {"rubric_version": 7, "prompt": "为什么看不到提交审核？", "parts": [{"check": {
                "answer_regex": "未提交", "source_regex": rf"(?m)^{_VUE}:2@v2026\.08$"}}]}
            lines = _LINES.encode().splitlines(keepends=True)
            right = f"{_VUE}:1-3@v2026.08 sha256:{hashlib.sha256(b''.join(lines)).hexdigest()}"
            forged = f"{_VUE}:1-3@v2026.08 sha256:{'0' * 64}"
            for citation, expected in ((right, True), (forged, False)):
                verdict = score(task, probe={}, visited=[], finished=True, write_detected=False,
                                answer=f"审批状态为未提交时才显示。\n{citation}", source=citations)
                self.assertEqual(verdict.success, expected)


if __name__ == "__main__":
    unittest.main()
