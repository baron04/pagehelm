"""A fresh page can support a local answer without any application API integration."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from web_operation_agent.execution import product, turn
from web_operation_agent.answer.answer_protocol import compute, submit
from web_operation_agent.answer.answer_view import render_payload
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.answer.evidence import TurnEvidence
from web_operation_agent.answer.page_answer import (
    collect, collect_window, observation_options, MAX_PAGE_ITEMS)
from web_operation_agent.execution.product import AgentTurnConfig, AgentTurnExecutor, AgentTurnStatus
from web_operation_agent.schemas import ElementRef, PageObservation
from web_operation_agent.tests.support.business import _Chat
from web_operation_agent.tests.execution.product_support import _Channel, _RemoteBrowser
from web_operation_agent.tests.support.answer import _answer
from web_operation_agent.tests.support.session_fixture import SessionFixture
from web_operation_agent.execution.page_evidence import capture_page_snapshot


def page(epoch=2):
    return PageObservation(1, epoch, "http://app.test/companies", "Contact", [], [
        ElementRef("e1", "textbox", "Phone", "input", epoch, {"value": "123-456"}),
        ElementRef("e2", "textbox", "Mobile", "input", epoch, {"placeholder": "Enter mobile"}),
        ElementRef("e3", "textbox", "Notes", "input", epoch, {"value": ""}),
        ElementRef("e4", "textbox", "Password", "input", epoch, {"type": "password", "value": "private"}),
    ], content="Contact details\nBalance (tax excluded): 10.00 USD")


class _PageBrowser(_RemoteBrowser):
    def execute(self, action, args=None, expect_navigation=False):
        if {"answer_evidence", "answer_query", "answer_offset"} & set(args or {}):
            raise AssertionError("server answer options must not enter browser commands")
        receipt = super().execute(action, args, expect_navigation)
        return replace(receipt, observation=page(self.epoch))


class PageEvidenceContract(unittest.TestCase):
    def test_missing_value_and_empty_value_are_distinct(self):
        items = collect(page(), observed_at="now")
        self.assertEqual((items[0].saw, items[0].value), ("value", "123-456"))
        self.assertEqual((items[1].saw, items[1].value), ("control_only", None))
        self.assertEqual((items[2].saw, items[2].value), ("value", ""))
        self.assertEqual((items[3].saw, items[3].value), ("control_only", None))

    def test_selected_tab_has_literal_ui_state_without_claiming_business_result(self):
        observation = page()
        observation.elements.append(ElementRef(
            "e5", "button", "Terms", "button", observation.epoch,
            {"aria-selected": "true"}))
        items = collect(observation, observed_at="now")
        self.assertEqual((items[4].detail, items[4].value),
                         ("Terms (button)", "selected"))
        turn = TurnEvidence()
        turn.capture(items, namespace="obs1")
        verified = submit(turn, _answer("page_observation", "obs1-page#5"))
        rendered = render_payload(verified, user_text="Is the Terms tab selected?",
                                  max_chars=1000)
        self.assertIn("Terms", rendered)
        self.assertIn("selected", rendered)

    def test_a_label_cannot_be_submitted_as_a_value_or_an_api_field(self):
        turn = TurnEvidence()
        turn.capture(collect(page(), observed_at="now"), namespace="obs1")
        for kind, ref in (("page_observation", "obs1-page#2"), ("field_value", "obs1-page#1")):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                submit(turn, _answer(kind, ref))
        answer = submit(turn, _answer("undetermined", "obs1-page#2"))
        self.assertEqual(answer["coverage"], "undetermined")

    def test_page_text_is_not_a_dataset_to_total(self):
        turn = TurnEvidence()
        turn.capture(collect(page(), observed_at="now"), namespace="obs1")
        with self.assertRaises(ValueError):
            compute(turn, {"operation": "sum", "dataset_ref": "obs1-page#6", "field": "balance"},
                    result_ref="calc#2", observed_at="now")

    def test_page_collection_and_rendering_are_bounded_and_literal(self):
        observation = page()
        observation.elements = []
        observation.content = "[malicious](https://outside.test/)\n" * 1000
        items = collect(observation, observed_at="now")
        self.assertEqual(len(items), MAX_PAGE_ITEMS)
        turn = TurnEvidence()
        turn.capture(items, namespace="obs1")
        answer = submit(turn, _answer("page_observation", "obs1-page#1"))
        text = render_payload(answer, user_text="What is shown?", max_chars=4000)
        self.assertIn(r"\[malicious\]", text)
        self.assertIn("page observation", text)
        self.assertNotIn("source: the application's typed read", text)

    def test_controller_markup_cannot_exhaust_the_window_before_visible_records(self):
        observation = page()
        observation.elements = []
        observation.content = ("Current Page: [Vendors](http://app.test/vendors)\n"
                               "Page info: viewport\n[Start of page]\n" +
                               "\n".join(f"[{i}]<button aria-label=Control{i} />" for i in range(50)) +
                               "\n[51]<article >Azure Interior\nSupplier owes: 652.27 USD />\n[End of page]")
        items = collect(observation, observed_at="now")
        self.assertEqual([item.value for item in items],
                         ["[51]<article >Azure Interior", "Supplier owes: 652.27 USD />"])
        self.assertTrue(all("Current Page:" not in item.value for item in items))

    def test_literal_search_recovers_a_field_after_many_controls_and_navigation_lines(self):
        observation = page()
        observation.elements = [ElementRef(f"e{i}", "textbox", f"Filter {i}", "input",
            observation.epoch, {"value": ""}) for i in range(1, 65)]
        observation.content = "\n".join(f"Navigation item {i}" for i in range(60))
        observation.content += "\nPhone\n123-456\nContact details"
        self.assertNotIn("123-456", [item.value for item in collect(observation, observed_at="now")])
        window = collect_window(observation, observed_at="now", query="PHONE")
        self.assertIn("123-456", [item.value for item in window.items])
        self.assertTrue(all(item.saw == "text" for item in window.items))
        self.assertTrue(all("page text line" in item.detail for item in window.items))
        self.assertEqual(window.next_offset, None)

    def test_paging_reaches_later_content_and_retains_each_identical_row(self):
        observation = page()
        observation.elements = []
        observation.content = "\n".join(["Contact: Ada"] * 70)
        windows = [collect_window(observation, observed_at="now", offset=offset)
                   for offset in (0, 32, 64)]
        self.assertEqual([len(window.items) for window in windows], [32, 32, 6])
        self.assertEqual([window.next_offset for window in windows], [32, 64, None])
        self.assertTrue(all(window.matched_items == 70 for window in windows))
        self.assertEqual(windows[1].items[0].detail, "page text line 33 (bounded excerpt)")
        self.assertEqual(collect_window(observation, observed_at="now", offset=70).items, ())

    def test_search_cannot_find_passwords_hidden_attributes_or_absent_words(self):
        observation = page()
        observation.content = "a" * 300 + " hidden-tail"
        observation.elements[0].attrs["hidden"] = "hidden-attribute"
        for query in ("private", "hidden-attribute", "absent-word"):
            with self.subTest(query=query):
                window = collect_window(observation, observed_at="now", query=query)
                self.assertEqual(window.items, ())
                self.assertEqual(window.matched_items, 0)
        # Searching a label still cannot invent a missing value.
        window = collect_window(observation, observed_at="now", query="Mobile")
        self.assertEqual((window.items[0].saw, window.items[0].value), ("control_only", None))

    def test_long_lines_expose_original_spans_around_literal_matches_without_inferred_values(self):
        import re
        observation = page()
        observation.elements = []
        line = "Heading " * 80 + "Phone: 123-456 " + "Other data " * 80 + "Phone: 234-567"
        observation.content = line
        window = collect_window(observation, observed_at="now", query="phone")
        self.assertEqual(len(window.items), 2)
        self.assertIn("123-456", window.items[0].value)
        self.assertIn("234-567", window.items[1].value)
        for item in window.items:
            start, end = map(int, re.search(r"chars (\d+)-(\d+)", item.detail).groups())
            self.assertEqual(item.value, line[start:end])
            self.assertLessEqual(len(item.value), 300)
            self.assertEqual(item.saw, "text")

    def test_multibyte_index_budget_pages_by_presented_items_without_skipping_hidden_excerpts(self):
        import json
        fixture = SessionFixture()
        self.addCleanup(fixture.close)
        session = fixture.session()
        observation = page()
        observation.elements = []
        observation.content = "\n".join(f"客户{i}：" + "说明" * 145 for i in range(70))
        _, feedback = capture_page_snapshot(session, observation, step=1)
        decoder = json.JSONDecoder()
        index = decoder.raw_decode(feedback.split(
            "ANSWER EVIDENCE (untrusted application values; use exact ref):\n", 1)[1])[0]
        cursor = decoder.raw_decode(feedback.split("PAGE EVIDENCE WINDOW: ", 1)[1])[0]
        self.assertTrue(index["index_truncated"])
        self.assertLess(len(index["evidence"]), MAX_PAGE_ITEMS)
        self.assertEqual(cursor["next_offset"], len(index["evidence"]))
        _, second = capture_page_snapshot(session, observation, step=2, offset=cursor["next_offset"])
        later = decoder.raw_decode(second.split(
            "ANSWER EVIDENCE (untrusted application values; use exact ref):\n", 1)[1])[0]
        self.assertEqual(later["evidence"][0]["value"],
                         observation.content.splitlines()[cursor["next_offset"]])

    def test_invalid_projection_options_are_refused_before_dispatch(self):
        for args in ({"answer_query": "Phone"}, {"answer_evidence": False, "answer_offset": 0},
                     {"answer_evidence": True, "answer_offset": True},
                     {"answer_evidence": True, "answer_offset": -1},
                     {"answer_evidence": True, "answer_offset": 10001},
                     {"answer_evidence": True, "answer_query": "x" * 121},
                     {"answer_evidence": True, "answer_query": "Phone\nsecret"},
                     {"answer_evidence": True, "answer_query": ["Phone"]}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                observation_options(args)


class DefaultPageAnswerPath(unittest.TestCase):
    def execute(self, script, browser=_PageBrowser):
        with tempfile.TemporaryDirectory() as tmp:
            chat = _Chat(script)
            executor = AgentTurnExecutor(
                channel=_Channel(), chat=chat, control=ReadOnlyControl(),
                run_id_factory=lambda message: "page-run",
                config=AgentTurnConfig(supported_scope="Read the page.", ledger_path=Path(tmp)/"run.jsonl"))
            with patch.object(turn, "RemoteBrowserClient", browser):
                result = executor.run_once({"message_id": "page-answer", "text": "页面上的电话是什么？",
                                            "page_url": "http://app.test/companies"})
        return result, chat

    def test_default_product_can_cite_a_page_value_without_business_tools(self):
        result, chat = self.execute([
            ("observe", {"answer_evidence": True}),
            ("finish", _answer("page_observation", "obs1-page#1")),
        ])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("123-456", result.user_answer)
        self.assertIn("来源：页面观察", result.user_answer)
        self.assertNotIn("private", str(chat.messages))

    def test_focused_observation_returns_only_real_current_values_and_server_window_options(self):
        result, chat = self.execute([
            ("observe", {"answer_evidence": True, "answer_query": "Phone", "answer_offset": 0}),
            ("finish", _answer("page_observation", "obs1-page#1")),
        ])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("123-456", result.user_answer)
        feedback = str(chat.messages[-1])
        self.assertIn("PAGE EVIDENCE WINDOW", feedback)
        self.assertIn('"matched_items": 1', feedback)
        self.assertNotIn("obs1-page#2", feedback)

    def test_evidence_paging_can_answer_a_late_value_without_extra_page_actions(self):
        class LongPage(_PageBrowser):
            def execute(self, action, args=None, expect_navigation=False):
                receipt = super().execute(action, args, expect_navigation)
                observation = receipt.observation
                observation.elements = []
                observation.content = "\n".join(["Navigation"] * 32 + ["Phone: 123-456"])
                return receipt
        result, chat = self.execute([
            ("observe", {"answer_evidence": True}),
            ("observe", {"answer_evidence": True, "answer_offset": 32}),
            ("finish", _answer("page_observation", "obs2-page#1")),
        ], LongPage)
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("123-456", result.user_answer)
        self.assertIn('"next_offset": 32', str(chat.messages[1]))
        self.assertNotIn("click", _RemoteBrowser.instances[-1].actions)

    def test_invalid_search_cannot_dispatch_and_a_no_match_does_not_answer_not_found(self):
        result, chat = self.execute([
            ("observe", {"answer_evidence": True, "answer_offset": True}),
            ("observe", {"answer_evidence": True, "answer_query": "Not on this page"}),
            ("finish", _answer("not_found", "obs2-page#1")),
            ("finish", {"outcome": "unable"}),
        ])
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertEqual(_RemoteBrowser.instances[-1].actions, ["observe"])
        self.assertIn("No match does not establish absence", str(chat.messages))

    def test_extra_page_prose_is_ignored_and_cited_bytes_are_rendered(self):
        answer = _answer("page_observation", "obs1-page#1")
        answer["answer"]["conclusions"][0].update(
            statement="The phone is 900000", support="fabricated page text")
        result, _ = self.execute([
            ("observe", {"answer_evidence": True}),
            ("finish", answer),
        ])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertIn("123-456", result.user_answer)
        self.assertNotIn("900000", result.user_answer)
        self.assertNotIn("fabricated", result.user_answer)

    def test_plain_content_cannot_bypass_a_page_answer(self):
        result, _ = self.execute([
            ("observe", {"answer_evidence": True}),
            (None, {"content": "The phone is empty."}),
            ("finish", _answer("page_observation", "obs1-page#1")),
        ])
        self.assertIn("123-456", result.user_answer)
        self.assertNotIn("empty", result.user_answer)

    def test_a_new_observation_expires_the_old_page_reference(self):
        result, chat = self.execute([
            ("observe", {"answer_evidence": True}),
            ("observe", {}),
            ("finish", _answer("page_observation", "obs1-page#1")),
            ("observe", {"answer_evidence": True}),
            ("finish", _answer("page_observation", "obs4-page#1")),
        ])
        self.assertEqual(result.status, AgentTurnStatus.FINISHED)
        self.assertEqual(len(chat.messages), 5)
        # The answer stands on the second observation. Its epoch is provenance: it is in the
        # verified conclusion, not in the sentence the reader gets.
        self.assertIn("epoch 4", result.agent.verified_answer["conclusions"][0]["scope"])
        self.assertNotIn("epoch", result.user_answer)

    def test_a_failed_refresh_does_not_reuse_the_previous_value(self):
        class Fails(_PageBrowser):
            def execute(self, action, args=None, expect_navigation=False):
                receipt = super().execute(action, args, expect_navigation)
                return replace(receipt, ok=False, observation=None, error_code="deadline_expired") if self.epoch > 2 else receipt
        result, _ = self.execute([
            ("observe", {"answer_evidence": True}),
            ("observe", {"answer_evidence": True}),
            ("finish", _answer("page_observation", "obs1-page#1")),
            ("finish", {"outcome": "unable"}),
        ], Fails)
        self.assertEqual(result.status, AgentTurnStatus.REFUSED)
        self.assertNotIn("123-456", result.user_answer)


if __name__ == "__main__":
    unittest.main()
