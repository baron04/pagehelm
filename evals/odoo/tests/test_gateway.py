import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from evals.odoo import gateway, ledger
from evals.odoo.tests.test_ledger import DIGEST, header, slots

TOKEN = "t" * 32
OPTIONS = {"thinking_budget": 1024, "parallel_tool_calls": False}


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
            "provider_config_digest": DIGEST, "replacement_of": None})

    def post(self, body, token=TOKEN):
        request = urllib.request.Request(
            self.gateway.base_url + "/chat/completions",
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

    def body(self, **changes):
        """The request the product sends for this model."""
        return {"model": "qwen3.8-flash", "messages": [], "temperature": 0.0,
                "max_tokens": 2048, **OPTIONS, **changes}

    def recording(self, body, index):
        self.seen.append(body)
        return completion(body, index)

    def test_owned_request_is_recorded_before_and_after_dispatch(self):
        self.gateway.open_attempt("attempt-001", TOKEN)
        self.backend.install("attempt-001", completion)
        status, payload = self.post(self.body())
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], gateway.SCRIPTED_MODEL)
        kinds, summary = self.kinds()
        self.assertEqual(kinds, ["model_request_dispatched", "model_response_received"])
        self.assertEqual(summary["provider_model"], gateway.SCRIPTED_MODEL)

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

    def test_request_parameters_must_be_the_shared_set(self):
        self.gateway.open_attempt("attempt-001", TOKEN)
        self.backend.install("attempt-001", completion)
        missing_temperature = self.body()
        del missing_temperature["temperature"]
        missing_option = self.body()
        del missing_option["thinking_budget"]
        cases = {
            "top_p": self.body(top_p=1.0),
            "max_completion_tokens": self.body(max_completion_tokens=2048),
            "other_temperature": self.body(temperature=0.7),
            "missing_temperature": missing_temperature,
            "other_option_value": self.body(thinking_budget=4096),
            "option_of_another_type": self.body(parallel_tool_calls=0),
            "missing_option": missing_option,
        }
        for label, body in cases.items():
            with self.subTest(label=label):
                self.assertEqual(self.post(body)[1]["error"]["code"], "request_contract")
        kinds, _ = self.kinds()
        self.assertEqual(kinds, ["model_request_refused"] * len(cases))

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
                         ["provider_http_5xx", "provider_connection_lost"])

    def test_completion_client_sends_pageagent_requests_with_the_product_parameters(self):
        self.gateway.open_attempt("attempt-001", TOKEN)
        self.backend.install("attempt-001", self.recording)
        client = gateway.GatewayCompletionClient(base_url=self.gateway.base_url, token=TOKEN,
                                                 model="qwen3.8-flash", model_options=OPTIONS)
        pageagent = {"model": "qwen3.8-flash", "messages": [], "temperature": 0.0,
                     "top_p": 1.0, "max_completion_tokens": 2048, "parallel_tool_calls": False}
        response = client.request_completion(pageagent, "run-001")
        self.assertEqual(response["model"], gateway.SCRIPTED_MODEL)
        self.assertEqual(client.meter.run_report("run-001"), {"model_calls": 1})
        self.assertEqual(self.seen, [self.body()])


if __name__ == "__main__":
    unittest.main()
