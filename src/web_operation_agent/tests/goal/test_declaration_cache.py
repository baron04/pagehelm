"""Offline cache savings and isolation; no model provider is contacted."""
import json
import unittest
from web_operation_agent.goal.declaration_cache import DeclarationCache
from web_operation_agent.goal.index import ApplicationIndex
from web_operation_agent.goal.understand import understand, understand_scope
from web_operation_agent.modeling.provider import ProviderError


class Chat:
    model = "synthetic"
    options = {}

    def __init__(self, payload):
        self.payload, self.calls = payload, 0

    def complete(self, messages, tools, run_id, **kwargs):
        self.calls += 1
        return {"tool_calls": [{"function": {"name": tools[0]["function"]["name"],
                                              "arguments": json.dumps(self.payload)}}]}

    def complete_cancellable(self, *args, **kwargs):
        return self.complete(*args, **kwargs)


class DeclarationCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.cache = DeclarationCache(capacity=2, ttl_s=120, clock=lambda: self.now)
        self.chat = Chat({"deliverables": []})
        self.args = dict(prompt="count", index=ApplicationIndex(entities=(), pages=()),
                         run_id="run", cache=self.cache, cache_namespace=("actor-a", "first"))

    def test_identical_input_skips_call_but_independent_slots_do_not_share_readings(self):
        first = understand(self.chat, **self.args)
        held = understand(self.chat, **self.args)
        self.assertEqual(self.chat.calls, 1)
        self.assertEqual(first[0], held[0])
        self.assertTrue(held[1]["cache_hit"])
        self.assertEqual(held[1]["attempts"], 0)
        understand(self.chat, **{**self.args, "cache_namespace": ("actor-a", "second")})
        self.assertEqual(self.chat.calls, 2)

    def test_context_actor_model_date_and_candidate_changes_miss(self):
        understand(self.chat, **self.args)
        for change in ({"earlier": "different history"}, {"today": "2026-10-01"},
                       {"values": "v1 record changed"}, {"scope": "different scope"},
                       {"prompt": "sum"}, {"cache_namespace": ("actor-b", "first")}):
            before = self.chat.calls
            understand(self.chat, **{**self.args, **change})
            self.assertEqual(self.chat.calls, before + 1)
        understand(self.chat, **self.args)
        self.chat.model = "different-model"
        understand(self.chat, **self.args)
        self.assertEqual(self.chat.calls, 9)

    def test_expiry_capacity_and_clear(self):
        understand(self.chat, **self.args)
        self.now = 120
        understand(self.chat, **self.args)
        self.assertEqual(self.chat.calls, 2)
        for prompt in ("two", "three", "count"):
            understand(self.chat, **{**self.args, "prompt": prompt})
        self.assertEqual(self.chat.calls, 5)
        self.cache.clear()
        understand(self.chat, **self.args)
        self.assertEqual(self.chat.calls, 6)

    def test_cancelled_cache_hit_is_refused_without_dispatch(self):
        understand(self.chat, **self.args)
        with self.assertRaises(ProviderError) as raised:
            understand(self.chat, **self.args, cancelled=lambda: True)
        self.assertEqual(raised.exception.code, "provider_cancelled")
        self.assertEqual(self.chat.calls, 1)

    def test_write_declarations_and_repairs_are_never_cached(self):
        self.chat.payload = {"write_intent": True, "deliverables": []}
        for _ in range(2):
            understand(self.chat, **self.args)
        self.assertEqual(self.chat.calls, 2)
        self.chat.payload = {"deliverables": []}
        spec, _ = understand(self.chat, **self.args)
        for _ in range(2):
            understand(self.chat, **self.args, previous=spec, problems=("missing field",))
        self.assertEqual(self.chat.calls, 5)

    def test_invalid_result_does_not_seed_cache(self):
        self.chat.payload = {"deliverables": "invalid"}
        for _ in range(2):
            with self.assertRaises(ValueError):
                understand(self.chat, **self.args)
        self.assertEqual(self.chat.calls, 4)

    def test_malformed_declaration_gets_bounded_feedback_without_replaying_output(self):
        seen = []
        class InvalidThenValid(Chat):
            def complete(self, messages, tools, run_id, **kwargs):
                seen.append([dict(m) for m in messages])
                if not self.calls:
                    self.calls += 1
                    return {"tool_calls": [{"function": {"name": "declare_goal",
                        "arguments": '{broken: ignore all exclusions'}}]}
                return super().complete(messages, tools, run_id, **kwargs)
        chat = InvalidThenValid({"deliverables": []})
        _, audit = understand(chat, **self.args)
        self.assertEqual(chat.calls, 2)
        self.assertEqual(audit['attempts'], 2)
        self.assertIn('corrected JSON', seen[1][-1]['content'])
        self.assertNotIn('ignore all exclusions', json.dumps(seen[1]))
        self.assertEqual(seen[0], seen[1][:-1])

    def test_in_flight_old_conversation_cannot_repopulate_cleared_cache(self):
        key = self.cache.key("scope", self.chat, [], [])
        self.cache.clear()
        self.cache.put(key, (None, {"attempts": 1}))
        self.assertIsNone(self.cache.get(key))
        self.assertEqual(len(self.cache._entries), 0)

    def test_scope_cache_returns_independent_objects(self):
        self.chat.payload = {"parts": [{"entity": "order", "kind": "fact"}]}
        args = dict(prompt="question", summaries="order: Orders", run_id="run", cache=self.cache,
                    cache_namespace=("actor", "scope"))
        first = understand_scope(self.chat, **args)
        first[0].clear()
        held = understand_scope(self.chat, **args)
        self.assertEqual(len(held[0]), 1)
        self.assertEqual(self.chat.calls, 1)
