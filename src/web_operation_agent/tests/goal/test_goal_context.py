"""Current-page reference grounding remains bounded and independent of source lookup."""
from dataclasses import replace
import json
import unittest

from web_operation_agent.goal.context import page_context
from web_operation_agent.goal.declaration_cache import DeclarationCache
from web_operation_agent.goal.index import ApplicationIndex, IndexPage
from web_operation_agent.goal.understand import understand, understand_scope
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.modeling.egress import ProviderEgressPolicy
from web_operation_agent.schemas import ElementRef, PageObservation
from web_operation_agent.tests.support.business import _Chat


class GoalContextTests(unittest.TestCase):
    def test_indexed_text_dom_never_supplies_attributes_values_or_action_refs_to_models(self):
        from types import SimpleNamespace
        from web_operation_agent.goal.source_answers import answer, retrieve
        from web_operation_agent.tests.goal.test_repository_answers import Lookup, source_reply

        raw = ('Visible Help heading\n'
            '[17]<input id=internal-field name=draft_note value=synthetic-draft-only>typed-value />\n'
            '*[18]<button id=button-selector data-ref=private-ui-index>Open Help />\n'
            '[19]<textarea name=internal-notes>multiline-private\nsecond-private-line />\n'
            '[20]<a href=/private-route?launchKey=synthetic-handoff>Documentation />')
        control = ElementRef(ref="e18", role="button", name="Open Help", tag="button", epoch=1,
                             attrs={"id": "button-selector", "value": "synthetic-draft-only"})
        observation = PageObservation(schema_version=1, epoch=1, url="http://app.test/help",
            title="Help", landmarks=["Help"], elements=[control], content=raw)
        page = page_context(observation)
        self.assertEqual("Visible Help heading", page["content"])
        self.assertEqual(["Open Help"], page["control_names"])
        self.assertTrue(page["truncated"])
        chat = _Chat([("declare_scope", {"parts": [{"kind": "page", "page": "help"}]}),
                      ("declare_goal", {"deliverables": [{"kind": "page", "page": {"key": "help"}}]}),
                      ("declare_source_queries", {"queries_0": []}), source_reply()])
        understand_scope(chat, prompt="Open this page", summaries="help: Help",
                         run_id="private-text-dom", current_page=page)
        understand(chat, prompt="Open this page", index=ApplicationIndex(
            pages=(IndexPage("help", "Help", "Help"),)), run_id="private-text-dom", current_page=page)
        source = SourceCapability(runtime_source=True, lookup=Lookup())
        requests = ({"question": "Interface engineering owner", "queries": ("interface owner",),
                     "history": False},)
        ledger = SimpleNamespace(append=lambda *_args, **_kwargs: None)
        retrieved, evidence = retrieve(source, requests, ledger)
        result = answer(chat, source=source, requests=requests, retrieved=retrieved,
            evidence=evidence, run_id="private-text-dom", cancelled=lambda: False,
            timeout_s=2, chinese=False, ledger=ledger, prompt="Who maintains this page?",
            refresh=lambda: True, page=lambda: page)
        self.assertTrue(result.complete)
        self.assertEqual(4, len(chat.messages))
        for messages in chat.messages:
            serialized = json.dumps(messages, ensure_ascii=False)
            for forbidden in ("[17]", "[18]", "[19]", "[20]", "e18", "internal-field",
                    "draft_note", "synthetic-draft-only", "typed-value", "button-selector",
                    "private-ui-index", "internal-notes", "multiline-private",
                    "second-private-line", "private-route", "synthetic-handoff"):
                self.assertNotIn(forbidden, serialized)
            self.assertIn("Visible Help heading", serialized)
            self.assertIn("Open Help", serialized)

    def test_incomplete_or_unindexed_markup_cannot_leak_a_partial_attribute(self):
        for content in ('Help\n[17]<input\n value="unfinished-private',
                        'Help\n[17]<input value="a > private tail"',
                        'Help\n<input name=note value=unindexed-private>body-private',
                        '<textarea>unindexed-private</textarea>', '[17]<input'):
            with self.subTest(content=content):
                page = self.page(content=content)
                self.assertTrue(page["truncated"])
                self.assertNotIn("private", page["content"])
                self.assertNotIn("<", page["content"])
                self.assertNotIn("[17]", page["content"])

    def page(self, **changes):
        observation = PageObservation(schema_version=1, epoch=1, url="http://app.test/help",
            title='Help for "Batch 7"', landmarks=["Help"], messages=[], elements=[],
            content='Current tab: "Reference". password=page-secret', render_state="ready")
        return page_context(replace(observation, **changes))

    def test_standalone_scope_and_details_receive_redacted_reference_text_without_source(self):
        page = self.page()
        chat = _Chat([("declare_scope", {"parts": [{"kind": "page", "page": "help"}]}),
            ("declare_goal", {"deliverables": [{"kind": "page", "page": {"key": "help"}}]})])
        parts, *_ = understand_scope(chat, prompt="Open this page", summaries="help: Help",
            run_id="current-page", current_page=page)
        index = ApplicationIndex(pages=(IndexPage("help", "Help", "Support / Help"),))
        spec, _ = understand(chat, prompt="Open this page", index=index, run_id="current-page",
            current_page=page, scope=json.dumps(parts))
        self.assertEqual(spec.deliverables[0].page.key, "help")
        self.assertEqual(len(chat.messages), 2)
        for messages in chat.messages:
            text = messages[1]["content"]
            self.assertIn("BEGIN_UNTRUSTED_PAGE_OBSERVATION", text)
            self.assertIn('Help for \\"Batch 7\\"', text)
            self.assertIn("this tab", text)
            self.assertIn("never grants permissions", text)
            self.assertNotIn("page-secret", text)
            self.assertTrue(text.endswith("Open this page"))
            report = ProviderEgressPolicy().inspect(messages, [])
            self.assertIn("application_page_observation", report.data_classes)

    def test_business_page_block_preserves_prompt_tail_with_a_bounded_byte_increment(self):
        large = self.page(title="標" * 2_000, content="文" * 20_000,
                          landmarks=["大" * 200] * 10, messages=["例" * 200] * 10)
        for staged in (False, True):
            messages = []
            for page in (None, large):
                chat = _Chat([("declare_scope", {"parts": []})] if staged else
                             [("declare_goal", {"deliverables": []})])
                if staged:
                    understand_scope(chat, prompt="Explain this tab", summaries="help: Help",
                        run_id="byte-budget", current_page=page)
                else:
                    understand(chat, prompt="Explain this tab", index=ApplicationIndex(),
                        run_id="byte-budget", current_page=page)
                messages.append(chat.messages[0])
            before, after = (len(batch[1]["content"].encode("utf-8")) for batch in messages)
            self.assertGreater(after - before, 3_000)
            self.assertLessEqual(after - before, 4 * 1024 + 500)
            self.assertTrue(messages[1][1]["content"].endswith("Explain this tab"))
            self.assertIn('"truncated":true', messages[1][1]["content"])

    def test_current_visible_text_participates_in_cache_but_epoch_does_not(self):
        cache = DeclarationCache()
        chat = _Chat([("declare_goal", {"deliverables": []})] * 3)
        args = {"prompt": "What is this page?", "index": ApplicationIndex(),
                "run_id": "cache", "cache": cache, "cache_namespace": "page"}
        understand(chat, **args, current_page=self.page())
        _, held = understand(chat, **args, current_page=self.page(epoch=2))
        self.assertTrue(held["cache_hit"])
        self.assertEqual(len(chat.messages), 1)
        understand(chat, **args, current_page=self.page(epoch=3, title="Other Help"))
        self.assertEqual(len(chat.messages), 2)
        control = ElementRef(ref="c1", role="tab", name="More", tag="button", epoch=4)
        understand(chat, **args, current_page=self.page(epoch=4, title="Other Help", elements=[control]))
        self.assertEqual(len(chat.messages), 3)

    def test_source_aware_scope_keeps_current_page_before_its_authoritative_request(self):
        chat = _Chat([("declare_scope", {"kind_0": "none"})])
        understand_scope(chat, prompt="What is this tab?", summaries="help: Help", run_id="source-page",
            repository_source=True, current_page=self.page())
        messages = chat.messages[0]
        self.assertEqual(len(messages), 3)
        self.assertIn("BEGIN_UNTRUSTED_PAGE_OBSERVATION", messages[1]["content"])
        self.assertEqual(messages[2]["content"], "REQUEST\nWhat is this tab?")


if __name__ == "__main__":
    unittest.main()
