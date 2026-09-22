from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from evals.source_operations.workflow_gate import _source, _tasks
from testapps.contacts.app import (DEMO_SESSION_COOKIE, DEMO_SESSION_VALUE,
                                   MinimalContactsApplication)
from testapps.contacts.demo import DemoConfig


class WorkflowGateTests(unittest.TestCase):
    def test_tab_state_is_visible_to_the_generic_page_observer(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed")
        with MinimalContactsApplication() as application, sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                self.skipTest(f"Chromium is unavailable: {type(exc).__name__}")
            try:
                context = browser.new_context()
                context.add_cookies([{"name": DEMO_SESSION_COOKIE,
                                      "value": DEMO_SESSION_VALUE,
                                      "url": application.origin}])
                page = context.new_page()
                page.goto(application.origin + "/contacts/workflows/partner",
                          wait_until="domcontentloaded")
                page.get_by_role("tab", name="Terms").click()
                state = page.evaluate("""async () => {
                  const {PageController} = await import('/woa/page-controller.js');
                  return (await new PageController().getBrowserState()).content;
                }""")
                self.assertIn('aria-selected="true">Terms', state)
                self.assertIn('aria-selected="false">Overview', state)
                self.assertEqual(application.write_requests, 0)
            finally:
                browser.close()

    def test_frozen_taskset_and_release_bound_raw_source(self):
        tasks = _tasks()
        self.assertEqual({item["desk"] for item in tasks["releases"]},
                         {"partner", "supplier"})
        with tempfile.TemporaryDirectory() as directory:
            for item in tasks["releases"]:
                source = _source(Path(directory) / item["release"],
                                 item["release"], item["desk"])
                hits = source.fact_lookup().search(
                    "vendor Orion current operational desk", limit=5)
                self.assertIn(f"{item['desk'].title()} desk", hits[0].line)
                self.assertEqual(hits[0].evidence.release, item["release"])

    def test_click_opt_in_is_limited_to_read_only_test_pages(self):
        for path in ("/contacts/workflows", "/contacts/workflows/partner"):
            self.assertTrue(DemoConfig(start_path=path, allow_page_click=True)
                            .allow_page_click)
        with self.assertRaisesRegex(ValueError, "click"):
            DemoConfig(start_path="/contacts/new", allow_page_click=True)
        self.assertEqual(DemoConfig(step_budget=10).step_budget, 10)
        with self.assertRaisesRegex(ValueError, "step_budget"):
            DemoConfig(step_budget=0)


if __name__ == "__main__":
    unittest.main()
