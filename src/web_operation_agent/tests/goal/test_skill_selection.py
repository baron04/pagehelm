"""Knowledge routing never declares values or authorizes execution."""
import json
import unittest
from copy import deepcopy

from web_operation_agent.goal.understand import understand_scope
from web_operation_agent.goal.spec import DECLARE_SCOPE_TOOL
from web_operation_agent.goal.source_answers import source_scope_tool
from web_operation_agent.tests.support.business import _Chat


CARDS = ({"skill_id": "fixture.record-maintenance",
          "name": "record-maintenance", "description": "Create and update records."},)


class SkillSelectionTests(unittest.TestCase):
    def route(self, script, *, source=False, cards=CARDS, prompt="Create Synthetic Birch, then read its email."):
        class Chat(_Chat):
            def complete(self, messages, tools, run_id, **kwargs):
                self.offered = deepcopy(tools)
                return super().complete(messages, tools, run_id, **kwargs)
        chat = Chat(script)
        result = understand_scope(chat, prompt=prompt, summaries="records: Records",
            run_id="selection", skill_candidates=cards, repository_source=source)
        return result, chat

    def test_selection_has_only_identities_and_preserves_original_request(self):
        result, chat = self.route([("select_skills", {"skill_ids": [CARDS[0]["skill_id"]]})])
        self.assertEqual(result[:4], ([], False, "", False))
        self.assertEqual(result[4]["selected_skill_ids"], (CARDS[0]["skill_id"],))
        self.assertEqual(result[4]["attempts"], 1)
        self.assertTrue(chat.messages[0][-1]["content"].endswith("Create Synthetic Birch, then read its email."))
        self.assertEqual(chat.tool_names[0], {"select_skills", "declare_scope"})

    def test_ordinary_scope_retains_schema_and_parser_with_installed_skills(self):
        payload = {"parts": [{"kind": "fact", "entity": "records"}]}
        result, chat = self.route([("declare_scope", payload)])
        ordinary, plain = self.route([("declare_scope", payload)], cards=())
        self.assertEqual(result, ordinary)
        offered = next(t for t in chat.offered if t["function"]["name"] == "declare_scope")
        self.assertEqual(offered, DECLARE_SCOPE_TOOL)
        self.assertEqual(len(chat.messages), len(plain.messages))

    def test_selection_and_live_scope_are_both_validated_in_either_order(self):
        selection = ("select_skills", {"skill_ids": [CARDS[0]["skill_id"]]})
        scope = ("declare_scope", {"parts": [{"kind": "handoff", "entity": "records",
                                              "names": ["Synthetic Birch"]}]})
        for calls in ([selection, scope], [scope, selection]):
            with self.subTest(calls=calls):
                result, chat = self.route([calls])
                self.assertEqual(result[0][0]["names"], ["Synthetic Birch"])
                self.assertEqual(result[4]["selected_skill_ids"], (CARDS[0]["skill_id"],))
                self.assertEqual(result[4]["attempts"], 1)
                self.assertEqual(len(chat.messages), 1)

    def test_valid_selection_cannot_bypass_invalid_or_duplicate_scope(self):
        selection = ("select_skills", {"skill_ids": [CARDS[0]["skill_id"]]})
        invalid = ("declare_scope", {"parts": "invalid"})
        duplicate = ("declare_scope", {"parts": [{"kind": "fact", "entity": "records"}]})
        for calls in ([selection, invalid], [selection, duplicate, duplicate]):
            with self.subTest(calls=calls), self.assertRaises(ValueError):
                self.route([calls, calls])

    def test_unknown_empty_duplicate_or_extra_selection_corrects_once(self):
        for invalid in ({"skill_ids": []}, {"skill_ids": ["private invented identity"]},
                        {"skill_ids": [CARDS[0]["skill_id"]] * 2},
                        {"skill_ids": [CARDS[0]["skill_id"]], "write_intent": True}):
            with self.subTest(invalid=invalid):
                result, chat = self.route([("select_skills", invalid),
                    ("select_skills", {"skill_ids": [CARDS[0]["skill_id"]]})])
                self.assertEqual(result[4]["attempts"], 2)
                self.assertIn("structural validation", chat.messages[1][-1]["content"])
                self.assertNotIn("private invented identity", json.dumps(chat.messages))
        with self.assertRaises(ValueError):
            self.route([("select_skills", {"skill_ids": []})] * 2)

    def test_selection_does_not_require_source_metadata_when_source_is_enabled(self):
        result, _ = self.route([("select_skills", {"skill_ids": [CARDS[0]["skill_id"]]})], source=True)
        self.assertEqual(result[0], [])
        self.assertEqual(result[4]["source_requests"], ())

    def test_source_mixture_retains_existing_live_dependency_and_source_contract(self):
        source = {"kind_0": "fact", "entity_0": "records",
            "kind_1": "source", "about_1": "Record rule", "terms_1": ["record rule"], "current_1": True}
        result, _ = self.route([[("select_skills", {"skill_ids": [CARDS[0]["skill_id"]]}),
            ("declare_scope", source)]], source=True)
        self.assertEqual(result[0][0]["kind"], "fact")
        self.assertTrue(result[4]["source_requests"][0]["current"])
        source.pop("kind_0"); source["kind_0"] = "source"
        source = {"kind_0": "source", "about_0": "Record rule", "terms_0": ["record rule"], "current_0": True}
        with self.assertRaises(ValueError):
            self.route([[("select_skills", {"skill_ids": [CARDS[0]["skill_id"]]}),
                ("declare_scope", source)]] * 2, source=True)

    def test_malformed_or_duplicate_routing_calls_fail_without_an_implicit_route(self):
        for reply in ({"content": '{"skill_ids": ["fixture.record-maintenance"]}'},
                      {"tool_calls": ["invalid"]}, {"tool_calls": {"invalid": True}}):
            class Chat(_Chat):
                def complete(self, *args, **kwargs):
                    return reply
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                understand_scope(Chat([]), prompt="Create Birch", summaries="Records",
                    run_id="malformed", skill_candidates=CARDS)

    def test_no_selection_keeps_original_source_tool(self):
        class Chat(_Chat):
            def complete(self, messages, tools, run_id, **kwargs):
                self_test.assertEqual(tools, [source_scope_tool(deepcopy(DECLARE_SCOPE_TOOL))])
                self_test.assertNotIn("options", kwargs)
                return super().complete(messages, tools, run_id, **kwargs)
        self_test = self
        result = understand_scope(Chat([("declare_scope", {"kind_0": "source",
            "about_0": "Record rule", "terms_0": ["record rule"]})]), prompt="Explain the rule",
            summaries="Records", run_id="ordinary-source", repository_source=True)
        self.assertEqual(result[4]["part_count"], 0)
