from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path

from web_operation_agent.model import ProviderError, require_chat_port
from examples.contacts.app import MinimalContactsApplication
from examples.contacts.adapter import MinimalContactsAdapter
from examples.contacts.demo import DemoConfig, ScriptedDemoChat, run_demo
from web_operation_agent.tests.adapter_contract import assert_adapter_contract


def _chromium_launches() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


class MinimalDemoUnitTests(unittest.TestCase):
    def test_second_adapter_binds_and_reads_its_live_typed_api(self):
        with MinimalContactsApplication() as application:
            adapter = MinimalContactsAdapter(application.origin)
            bound = assert_adapter_contract(self, adapter)
            self.assertTrue(adapter.health()["passed"])
            self.assertEqual(bound.descriptor.application_id, "minimal.contacts")
            self.assertEqual(
                [item.skill_id for item in bound.business_tools.relevant(
                    "Open Grace Hopper's contact")],
                ["minimal.contacts.query"],
            )
            result = bound.business_tools.execute(
                "list_contacts", {"name_contains": "Grace"},
                {"minimal.contacts.query"})
            self.assertEqual(result.content["matched_count"], 1)
            self.assertTrue(result.content["completeness"]["source_complete"])
            self.assertEqual(result.allowed_routes, ("/contacts/grace-hopper",))

    def test_application_serves_the_same_origin_controller(self):
        with MinimalContactsApplication() as application:
            with urllib.request.urlopen(
                    application.origin + "/woa/page-controller.js", timeout=2) as response:
                source = response.read().decode("utf-8")
            self.assertIn("export class PageController", source)
            self.assertIn("export function getElementByIndex", source)

    def test_scripted_apparatus_implements_the_chat_contract_and_cancels(self):
        chat = require_chat_port(ScriptedDemoChat(), role="minimal demo chat")
        reply = chat.complete([], [], "plain")
        self.assertEqual(reply["tool_calls"][0]["function"]["name"], "load_skill")
        with self.assertRaises(ProviderError) as raised:
            chat.complete_cancellable([], [], "cancelled", cancelled=lambda: True)
        self.assertEqual(raised.exception.code, "provider_cancelled")


class MinimalDemoBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = os.environ.get("WOA_REQUIRE_MINIMAL_DEMO_BROWSER") == "1"
        if not _chromium_launches():
            if required:
                raise RuntimeError("Chromium is required for minimal-demo qualification")
            raise unittest.SkipTest("Chromium is unavailable")

    def test_scripted_walkthrough_uses_the_real_gateway_and_opens_api_route(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = run_demo(DemoConfig(
                scripted=True,
                headless=True,
                output_dir=Path(directory),
                keep_open_seconds=0,
                turn_timeout_s=30,
            ))
            self.assertEqual(summary["mode"], "scripted_apparatus")
            self.assertEqual(summary["model_usage"]["provider_calls"], 0)
            self.assertEqual(summary["model_usage"]["scripted_decisions"], 4)
            self.assertEqual(len(summary["turns"]), 1)
            self.assertEqual(summary["turns"][0]["status"], "finished")
            self.assertEqual(summary["turns"][0]["terminal_reason"], "agent_finished")
            self.assertGreaterEqual(summary["turns"][0]["page_actions"], 1)
            self.assertTrue(summary["last_url"].endswith("/contacts/grace-hopper"))
            self.assertIn("Grace Hopper", summary["last_page_text"])
            self.assertIn("grace@example.test", summary["last_page_text"])
            audit = next(Path(directory).glob("*/audit.jsonl"))
            events = [json.loads(line)["event"] for line in audit.read_text().splitlines()]
            self.assertIn("business_skill_loaded", events)
            self.assertIn("business_tool_result", events)
            self.assertIn("action_receipt", events)


if __name__ == "__main__":
    unittest.main()
