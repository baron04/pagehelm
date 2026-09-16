import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from evals.odoo import gateway, ledger
from evals.odoo.tests.test_ledger import CLEAN_AGENT, DIGEST, PASS, header, metrics, slots

TOKEN = "t" * 32
OPTIONS = {"thinking_budget": 1024, "parallel_tool_calls": False}
MESSAGES = [{"role": "user", "content": "Which customers are in Lyon?"}]
TOOL = {"type": "function", "function": {"name": "click", "parameters": {"type": "object"}}}


def completion(_body, index):
    return {"choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": f"call {index}"}}]}


class GatewayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "attempts.jsonl"
        slot_list = slots(1)
        ledger.create(self.path, header(slot_list))
        self.backend = gateway.ScriptedBackend()
        self.gateway = gateway.ProviderGateway(
            ledger_path=self.path, backend=self.backend,
            budgets=gateway.GatewayBudgets("qwen3.8-flash", max_model_calls=2,
                                           max_output_tokens=2048,
                                           model_options=OPTIONS)).start()
        self.addCleanup(self.gateway.close)
        self.seen = []
        ledger.append(self.path, "attempt_started", {
            "attempt_id": "attempt-001", "slot_id": "s000", "arm": "woa_product",
            "task_id": "task-000", "replicate": "primary", "run_id": "run-001",
            "fixture_digest": DIGEST, "capability_digest": DIGEST,
            "provider_config_digest": DIGEST, "provider_binding_digest": DIGEST,
            "replacement_of": None})

    def post(self, body, token=TOKEN, service=None):
        request = urllib.request.Request(
            (service or self.gateway).base_url + "/chat/completions",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def kinds(self):
        entries, summary = ledger.read(self.path)
        return [entry["kind"] for entry in entries[2:]], summary

    def dispatched(self):
        entries, _ = ledger.read(self.path)
        return [entry["body"] for entry in entries if entry["kind"] == "model_request_dispatched"]

    def body(self, **changes):
        """The request the product sends for this model."""
        return {"model": "qwen3.8-flash", "messages": MESSAGES, "temperature": 0.0,
                "max_tokens": 2048, **OPTIONS, **changes}

    def recording(self, body, index):
        self.seen.append(body)
        return completion(body, index)

    def test_owned_request_is_recorded_with_its_parameters_before_dispatch(self):
        self.gateway.open_attempt("attempt-001", TOKEN)
        self.backend.install("attempt-001", completion)
        status, payload = self.post(self.body(tools=[TOOL], tool_choice="auto"))
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], gateway.SCRIPTED_MODEL)
        kinds, summary = self.kinds()
        self.assertEqual(kinds, ["model_request_dispatched", "model_response_received"])
        self.assertEqual(summary["provider_model"], gateway.SCRIPTED_MODEL)
        self.assertEqual(self.dispatched()[0]["parameters"], {
            "temperature": 0.0, "max_tokens": 2048, "tool_choice": "auto", "tools": 1,
            "options": OPTIONS})

    def test_request_without_open_attempt_is_unowned_and_never_forwarded(self):
        status, payload = self.post(self.body())
        self.assertEqual((status, payload["error"]["code"]), (401, "unowned_request"))
        kinds, summary = self.kinds()
        self.assertEqual(kinds, ["unowned_model_request"])
        self.assertEqual(summary["pending_seal"], "unowned_model_request")

    def test_retired_token_cannot_be_reused(self):
        self.gateway.open_attempt("attempt-001", TOKEN)
        self.backend.install("attempt-001", completion)
        self.gateway.close_attempt("attempt-001")
        self.assertEqual(self.post(self.body())[0], 401)
        with self.assertRaises(RuntimeError):
            self.gateway.open_attempt("attempt-002", TOKEN)

    def test_budgets_and_contract_are_refused_with_typed_codes(self):
        self.gateway.open_attempt("attempt-001", TOKEN)
        self.backend.install("attempt-001", completion)
        self.assertEqual(self.post(self.body(max_tokens=99999))[1]["error"]["code"],
                         "output_token_budget")
        self.assertEqual(self.post(self.body(model="other"))[1]["error"]["code"],
                         "request_contract")
        self.assertEqual(self.post(self.body())[0], 200)
        self.assertEqual(self.post(self.body())[0], 200)
        status, payload = self.post(self.body())
        self.assertEqual((status, payload["error"]["code"]), (429, "model_call_budget"))
        kinds, _ = self.kinds()
        self.assertEqual(kinds.count("model_request_refused"), 3)
        self.assertEqual(kinds.count("model_request_dispatched"), 2)

    def test_any_field_or_value_outside_the_frozen_contract_is_refused(self):
        self.gateway.open_attempt("attempt-001", TOKEN)
        self.backend.install("attempt-001", completion)
        missing_temperature = self.body()
        del missing_temperature["temperature"]
        missing_option = self.body()
        del missing_option["thinking_budget"]
        cases = {
            "top_p": self.body(top_p=1.0),
            "max_completion_tokens": self.body(max_completion_tokens=2048),
            "enable_thinking": self.body(enable_thinking=False),
            "seed": self.body(seed=7),
            "presence_penalty": self.body(presence_penalty=0.5),
            "other_temperature": self.body(temperature=0.7),
            "missing_temperature": missing_temperature,
            "other_option_value": self.body(thinking_budget=4096),
            "option_of_another_type": self.body(parallel_tool_calls=0),
            "missing_option": missing_option,
            "empty_messages": self.body(messages=[]),
            "tools_not_a_list": self.body(tools="all"),
            "tool_choice_number": self.body(tool_choice=3),
            "streaming": self.body(stream=True),
        }
        for label, body in cases.items():
            with self.subTest(label=label):
                self.assertEqual(self.post(body)[1]["error"]["code"], "request_contract")
        kinds, _ = self.kinds()
        self.assertEqual(kinds, ["model_request_refused"] * len(cases))

    def test_output_cap_must_be_an_integer_from_one_to_the_frozen_maximum(self):
        self.gateway.open_attempt("attempt-001", TOKEN)
        self.backend.install("attempt-001", completion)
        missing = self.body()
        del missing["max_tokens"]
        for label, body in {"zero": self.body(max_tokens=0), "negative": self.body(max_tokens=-1),
                            "boolean": self.body(max_tokens=True),
                            "above_cap": self.body(max_tokens=2049),
                            "text": self.body(max_tokens="2048"), "missing": missing}.items():
            with self.subTest(label=label):
                self.assertEqual(self.post(body)[1]["error"]["code"], "output_token_budget")
        self.assertEqual(self.post(self.body(max_tokens=1))[0], 200)
        self.assertEqual(self.post(self.body(max_tokens=2048))[0], 200)
        self.assertEqual([item["parameters"]["max_tokens"] for item in self.dispatched()],
                         [1, 2048])

    def test_injected_outage_and_connection_loss_leave_request_evidence(self):
        self.gateway.open_attempt("attempt-001", TOKEN)
        self.backend.install("attempt-001", completion,
                             faults={1: "http_503", 2: "connection_lost"})
        self.assertEqual(self.post(self.body())[0], 503)
        with self.assertRaises((urllib.error.URLError, ConnectionError, OSError)):
            self.post(self.body())
        entries, _ = ledger.read(self.path)
        responses = [entry["body"] for entry in entries
                     if entry["kind"] == "model_response_received"]
        self.assertEqual([item["status"] for item in responses],
                         ["http_error", "transport_error"])
        self.assertEqual([item["error_code"] for item in responses],
                         ["provider_unavailable", "provider_connection_lost"])

    def service(self, max_model_calls):
        service = gateway.ProviderGateway(
            ledger_path=self.path, backend=self.backend,
            budgets=gateway.GatewayBudgets("qwen3.8-flash", max_model_calls=max_model_calls,
                                           max_output_tokens=2048,
                                           model_options=OPTIONS)).start()
        self.addCleanup(service.close)
        return service

    def test_every_client_receives_the_same_typed_error_for_the_same_provider_fact(self):
        service = self.service(max_model_calls=6)
        service.open_attempt("attempt-001", TOKEN)
        self.backend.install("attempt-001", completion, faults={
            1: "http_429", 2: "http_401", 3: "context_length", 4: "malformed_response",
            5: "http_408", 6: "http_429"})
        replies = [self.post(self.body(), service=service) for _ in range(5)]
        self.assertEqual([(status, payload["error"]["code"]) for status, payload in replies], [
            (429, "provider_rate_limited"), (401, "provider_authentication"),
            (400, "provider_context_length"), (502, "provider_response_contract"),
            (408, "provider_deadline")])
        with self.assertRaises(RuntimeError) as raised:
            self.client(service).request_completion(self.body(), "run-001")
        self.assertEqual(raised.exception.code, "provider_rate_limited")
        entries, _ = ledger.read(self.path)
        responses = [entry["body"] for entry in entries
                     if entry["kind"] == "model_response_received"]
        self.assertEqual([(item["status"], item["error_code"]) for item in responses], [
            ("http_error", "provider_rate_limited"), ("http_error", "provider_authentication"),
            ("http_error", "provider_context_length"),
            ("contract_error", "provider_response_contract"),
            ("http_error", "provider_deadline"), ("http_error", "provider_rate_limited")])

    def test_an_unaccounted_call_closes_the_gateway_to_every_later_request(self):
        self.gateway.open_attempt("attempt-001", TOKEN)
        self.backend.install("attempt-001", completion, faults={1: "accounting_failure"})
        self.assertEqual(self.post(self.body()), (502, {"error": {"code": "accounting_failure"}}))
        self.assertEqual(self.post(self.body()), (503, {"error": {"code": "accounting_failure"}}))
        kinds, _ = self.kinds()
        self.assertEqual(kinds, ["model_request_dispatched", "model_response_received",
                                 "model_request_refused"])

    def test_an_attempt_closes_only_once_the_calls_it_made_have_settled(self):
        # The design review of 102c2f4: the attempt closed on the gateway while a call was still
        # being answered, and the driver read its records in that window.
        entered, release = threading.Event(), threading.Event()

        def slow(body, index):
            entered.set()
            release.wait(10)
            return completion(body, index)

        self.gateway.open_attempt("attempt-001", TOKEN)
        self.backend.install("attempt-001", slow)
        worker = threading.Thread(target=self.post, args=(self.body(),), daemon=True)
        worker.start()
        self.assertTrue(entered.wait(10))
        settled: list[dict] = []
        closing = threading.Thread(
            target=lambda: settled.append(self.gateway.close_attempt("attempt-001")), daemon=True)
        closing.start()
        # A request that arrives while the attempt settles belongs to no open attempt.
        self.assertEqual(self.post(self.body())[0], 401)
        release.set()
        closing.join(10)
        worker.join(10)
        kinds, summary = self.kinds()
        self.assertEqual(kinds, ["model_request_dispatched", "unowned_model_request",
                                 "model_response_received"])
        self.assertEqual((settled[0]["unresolved"], self.gateway.late_responses), (0, 0))
        self.assertEqual(summary["pending_seal"], "unowned_model_request")

    def test_a_call_that_outlives_the_provider_deadline_settles_as_unresolved(self):
        """The design review of 102c2f4: a lease expires with the request it belongs to, not on
        a timeout of the apparatus's own, and the call settles exactly once."""
        entered, release = threading.Event(), threading.Event()

        def slow(body, index):
            entered.set()
            release.wait(10)
            return completion(body, index)

        self.gateway.backend.request_deadline_s = 0.2
        self.gateway.open_attempt("attempt-001", TOKEN)
        self.backend.install("attempt-001", slow)
        worker = threading.Thread(target=self.post, args=(self.body(),), daemon=True)
        worker.start()
        self.assertTrue(entered.wait(10))
        settled = self.gateway.close_attempt("attempt-001")
        self.assertEqual(settled["unresolved"], 1)
        entries, _ = ledger.read(self.path)
        (record,) = [entry["body"] for entry in entries
                     if entry["kind"] == "model_response_unresolved"]
        self.assertEqual((record["attempt_id"], record["request_seq"], record["reason"]),
                         ("attempt-001", 1, "provider_deadline_elapsed"))
        # The answer that arrives afterwards settles nothing a second time.
        release.set()
        worker.join(10)
        kinds, _ = self.kinds()
        self.assertEqual(kinds, ["model_request_dispatched", "model_response_unresolved"])
        self.assertEqual(self.gateway.late_responses, 1)
        # The attempt can end: its unsettled call is written down, and costs as usage unavailable.
        for kind, body in (
                ("attempt_outcome", {"attempt_id": "attempt-001", **PASS.as_record(),
                                     "evaluation_digest": None, "truth_digest": None}),
                ("reset_outcome", {"attempt_id": "attempt-001", "outcome": "NOT_REQUIRED",
                                   "basis": [], "guard_digest": None, "agent": CLEAN_AGENT}),
                ("attempt_cost", {"attempt_id": "attempt-001", "model_calls": 1,
                                  "usage_unavailable_calls": 1, "input_tokens": 0,
                                  "output_tokens": 0, "cached_tokens": 0,
                                  "reasoning_tokens": 0, "known_cost_cny": 0.0,
                                  "total_cost_cny": None}),
                ("attempt_metrics", metrics("attempt-001")),
                ("attempt_terminal", {"attempt_id": "attempt-001"})):
            ledger.append(self.path, kind, body)

    def test_an_answer_that_could_not_be_accounted_still_closes_the_gateway_when_it_is_late(self):
        # The review of 48cf127: an accounting failure was lost with the attempt that closed
        # before its answer arrived. The call is now written down as unresolved before that, and
        # the bill reconciliation reads it there.
        self.gateway.open_attempt("attempt-001", TOKEN)
        self.backend.install("attempt-001", completion)
        self.assertEqual(self.post(self.body())[0], 200)
        self.gateway.close_attempt("attempt-001")
        unaccounted = gateway.BackendResult("accounting_error", 502, None, "req-2", None, None,
                                            None, "accounting_failure")
        self.gateway._record_response("attempt-001", 2, unaccounted)
        kinds, summary = self.kinds()
        self.assertEqual(kinds, ["model_request_dispatched", "model_response_received"])
        self.assertEqual((summary["pending_seal"], self.gateway.late_responses), (None, 1))

    def test_a_slow_scripted_answer_keeps_the_client_waiting_and_then_answers_whole(self):
        slow = gateway.ScriptedBackend(slow_answer_s=0.3)
        slow.install("attempt-001", completion, faults={1: "slow_answer"})
        started = time.monotonic()
        result = slow.complete(self.body(), attempt_id="attempt-001", call_index=1)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertEqual((result.status, result.response_model), ("ok", gateway.SCRIPTED_MODEL))
        with self.assertRaises(ValueError):
            slow.install("attempt-002", completion, faults={1: "slow"})

    def test_the_runtime_binding_names_the_backend_and_the_request_contract(self):
        binding = self.gateway.binding()
        self.assertEqual((binding["provider_id"], binding["credential_identity"],
                          binding["requested_model"]), ("p74-scripted", None, "qwen3.8-flash"))
        identity = self.backend.identity()
        fewer_calls = gateway.GatewayBudgets("qwen3.8-flash", max_model_calls=1,
                                             max_output_tokens=2048, model_options=OPTIONS)
        self.assertEqual(gateway.provider_binding(identity, fewer_calls), binding)
        smaller_cap = gateway.GatewayBudgets("qwen3.8-flash", max_model_calls=2,
                                             max_output_tokens=1024, model_options=OPTIONS)
        self.assertNotEqual(gateway.provider_binding(identity, smaller_cap), binding)
        self.assertEqual(self.gateway.binding_digest(), gateway.digest_binding(binding))

    def client(self, service=None):
        return gateway.GatewayCompletionClient(base_url=(service or self.gateway).base_url,
                                               token=TOKEN, model="qwen3.8-flash",
                                               model_options=OPTIONS)

    def test_completion_client_sends_pageagent_requests_with_the_product_parameters(self):
        self.gateway.open_attempt("attempt-001", TOKEN)
        self.backend.install("attempt-001", self.recording)
        # What the pinned PageAgent bundle sends for a Qwen model through its bridge.
        pageagent = {"model": "qwen3.8-flash", "messages": MESSAGES, "tools": [TOOL],
                     "parallel_tool_calls": False, "tool_choice": "required",
                     "temperature": 0.0, "enable_thinking": False, "top_p": 1.0,
                     "max_completion_tokens": 2048}
        client = self.client()
        response = client.request_completion(pageagent, "run-001")
        self.assertEqual(response["model"], gateway.SCRIPTED_MODEL)
        self.assertEqual(client.meter.run_report("run-001"), {"model_calls": 1})
        self.assertEqual(self.seen, [{
            "model": "qwen3.8-flash", "messages": MESSAGES, "tools": [TOOL],
            "parallel_tool_calls": False, "tool_choice": "required", "temperature": 0.0,
            "max_tokens": 2048, "thinking_budget": 1024}])
        self.assertEqual(self.dispatched()[0]["parameters"]["tool_choice"], "required")

    def test_completion_client_leaves_unknown_fields_for_the_gateway_to_refuse(self):
        self.gateway.open_attempt("attempt-001", TOKEN)
        self.backend.install("attempt-001", self.recording)
        with self.assertRaises(RuntimeError) as raised:
            self.client().request_completion(
                {"model": "qwen3.8-flash", "messages": MESSAGES, "temperature": 0.0,
                 "max_completion_tokens": 2048, "reasoning_effort": "low"}, "run-001")
        self.assertEqual(raised.exception.code, "request_contract")
        self.assertEqual(self.seen, [])


if __name__ == "__main__":
    unittest.main()
