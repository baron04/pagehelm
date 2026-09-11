from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path

from web_operation_agent.traceview import (build_trace_case, render_trace_html,
                                            write_trace_bundle)


class TraceViewTests(unittest.TestCase):
    @staticmethod
    def _template() -> str:
        return files("web_operation_agent").joinpath(
            "web_runtime", "trace_replay.html").read_text(encoding="utf-8")

    def _files(self, root: Path, events=None):
        result = {
            "product_run_id": "run-1", "task": "Open the due invoices page",
            "product_success": True, "started_at": 100.0, "finished_at": 102.5,
            "agent": {
                "page_actions": 1, "terminal_reason": "agent_finished",
                "agent_said": "Done /x?access_token=answer-secret",
                "spend": {"provider_calls": 2, "cost_cny": 0.01},
                "control": {"permit_consumed": 0, "write_blocked": 0},
            },
        }
        events = events or [
            {"seq": 1, "t": 100.1, "run_id": "run-1", "event": "observation",
             "elements": [{"name": "api_key", "value": "page-secret"}]},
            {"seq": 2, "t": 100.2, "run_id": "run-1", "event": "tool_call",
             "tool": "goto", "args": {"url": "/x?externalLoginKey=handoff-secret"}},
            {"seq": 3, "t": 102.4, "run_id": "run-1", "event": "product_verification",
             "product_success": True},
        ]
        result_path = root / "result.json"
        ledger_path = root / "session.jsonl"
        result_path.write_text(json.dumps(result))
        ledger_path.write_text("".join(json.dumps(event) + "\n" for event in events))
        return result_path, ledger_path

    def test_projects_contiguous_matching_ledger_and_redacts_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, ledger = self._files(Path(tmp))
            case = build_trace_case(case_id="read", label="Read", mode="page-only",
                                    result_path=result, ledger_path=ledger)
            wire = json.dumps(case)
            self.assertTrue(case["success"])
            self.assertEqual(case["summary"]["model_calls"], 2)
            self.assertEqual([event["kind"] for event in case["events"]],
                             ["tool_call", "product_verification"])
            for secret in ("answer-secret", "page-secret", "handoff-secret"):
                self.assertNotIn(secret, wire)

    def test_refuses_sequence_gap_or_mixed_run_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, ledger = self._files(root, [
                {"seq": 2, "t": 100.1, "run_id": "run-1", "event": "tool_call"},
            ])
            with self.assertRaisesRegex(ValueError, "sequence"):
                build_trace_case(case_id="x", label="X", mode="test",
                                 result_path=result, ledger_path=ledger)
            result, ledger = self._files(root, [
                {"seq": 1, "t": 100.1, "run_id": "other", "event": "tool_call"},
            ])
            with self.assertRaisesRegex(ValueError, "different runs"):
                build_trace_case(case_id="x", label="X", mode="test",
                                 result_path=result, ledger_path=ledger)

    def test_writes_json_and_dependency_free_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, ledger = self._files(root)
            case = build_trace_case(case_id="read", label="Read", mode="page-only",
                                    result_path=result, ledger_path=ledger)
            bundle = write_trace_bundle([case], root / "trace.json")
            render_trace_html(bundle, root / "index.html")
            page = (root / "index.html").read_text()
            self.assertIn("每一步都留下证据", page)
            self.assertIn('id="trace-data"', page)
            self.assertNotIn("__WOA_TRACE_BUNDLE__", page)
            self.assertNotIn("__WOA_DESIGN_TOKENS__", page)
            # The replay shares the product's palette and none of its code: it renders a
            # past run from a static bundle, which is a different job from driving a session.
            self.assertIn("--woa-accent: #1d4ed8;", page)
            self.assertIn("prefers-color-scheme: dark", page)
            self.assertNotIn("createAssistantPanel", page)
            self.assertNotIn("<script src=", page)
            self.assertNotIn("import ", page)
            self.assertNotIn("answer-secret", page)
            self.assertNotIn("docs/demo", page)
            self.assertEqual(json.loads((root / "trace.json").read_text()), bundle)

    def test_source_template_opens_sanitized_standalone_example(self):
        template = self._template()
        self.assertEqual(template.count("__WOA_TRACE_BUNDLE__"), 1)
        self.assertIn("isSourceTemplate", template)
        self.assertIn('id="trace-example"', template)
        self.assertIn("default-src 'none'", template)
        self.assertNotIn("docs/demo", template)
        self.assertNotIn("window.location", template)
        self.assertNotIn("http://", template)
        self.assertNotIn("https://", template)
        self.assertNotIn("file://", template)
        self.assertNotIn("/Users/", template)
        self.assertNotRegex(template, r"<(?:script|img)\b[^>]*\ssrc=")
        self.assertNotRegex(template, r"<link\b")
        match = re.search(
            r'<script id="trace-example" type="application/json">\s*(.*?)\s*</script>',
            template, re.DOTALL)
        self.assertIsNotNone(match)
        example = json.loads(match.group(1))
        self.assertEqual(example["schema_version"], 1)
        self.assertEqual(len(example["cases"]), 1)
        self.assertIn("合成", example["cases"][0]["mode"])
        self.assertIn("不包含真实会话", example["cases"][0]["answer"])

    def test_renderer_uses_redacted_product_name_in_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "replay.html"
            render_trace_html({
                "schema_version": 1,
                "product": "Viewer access_token=title-secret",
                "cases": [],
            }, output)
            page = output.read_text(encoding="utf-8")
            self.assertNotIn("title-secret", page)
            self.assertIn("Viewer access_token=[redacted]", page)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to parse the viewer script")
    def test_inline_viewer_script_has_valid_javascript(self):
        scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", self._template(), re.DOTALL)
        self.assertGreaterEqual(len(scripts), 3)
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "trace-replay.js"
            script.write_text(scripts[-1], encoding="utf-8")
            proc = subprocess.run(
                ["node", "--check", str(script)], text=True, capture_output=True,
                check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_rendered_replay_runs_from_a_local_file_with_no_dependency(self):
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed")
        case = {
            "id": "read", "label": "Read companies", "mode": "typed API + page",
            "task": "List companies and open Acme", "success": True,
            "summary": {"duration_s": 1.2, "model_calls": 1, "page_actions": 1,
                        "cost_cny": 0.01, "permit_consumed": 0, "write_blocked": 0,
                        "terminal_reason": "verified"},
            "answer": "Acme opened.",
            "events": [
                {"seq": 1, "at_ms": 0, "kind": "tool_call", "tone": "decision",
                 "title": "query_companies", "detail": "Read current data"},
                {"seq": 2, "at_ms": 400, "kind": "action_receipt", "tone": "action",
                 "title": "Page opened", "detail": "/companies/acme"},
                {"seq": 3, "at_ms": 900, "kind": "product_verification", "tone": "verify",
                 "title": "Verified", "detail": "Route and record match"},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "replay.html"
            render_trace_html({"schema_version": 1, "product": "Web Operation Agent",
                               "cases": [case]}, output)
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=True)
                except PlaywrightError as error:
                    if "Executable doesn't exist" in str(error):
                        self.skipTest("Playwright Chromium is not installed")
                    raise
                context = browser.new_context(viewport={"width": 1280, "height": 800},
                                              reduced_motion="reduce")
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(output.as_uri(), wait_until="load")
                page.locator(".rail .event").first.wait_for()
                self.assertIn("query_companies", page.locator(".rail").inner_text())
                self.assertEqual(
                    page.locator(":root").evaluate(
                        "node => getComputedStyle(node).getPropertyValue('--woa-accent').trim()"),
                    "#1d4ed8")
                page.locator("#play").click()
                page.locator("#reset").click()
                self.assertEqual(errors, [])
                screenshot = Path(tmp) / "replay.png"
                page.screenshot(path=str(screenshot), full_page=True)
                self.assertGreater(screenshot.stat().st_size, 1000)
                context.close()
                browser.close()

    def test_bundle_requires_unique_case_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = {"id": "same"}
            with self.assertRaisesRegex(ValueError, "unique"):
                write_trace_bundle([case, case], Path(tmp) / "trace.json")


if __name__ == "__main__":
    unittest.main()
