import copy
import unittest

from evals.odoo import canary


def completion(content="391", model="provider-model-x"):
    return {"id": "r", "model": model, "choices": [{"index": 0, "message": {
        "role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 2}}


class CanaryTests(unittest.TestCase):
    def test_request_is_fixed(self):
        request = canary.canary_request("qwen3.8-flash")
        self.assertEqual(request["messages"][0]["content"], canary.CANARY_PROMPT)
        self.assertEqual(request["temperature"], 0.0)

    def test_stable_scripted_provider_passes(self):
        calls = []

        def send(request):
            calls.append(request)
            return completion("<think>17*23</think>391")
        result = canary.run_preflight(send, model="qwen3.8-flash")
        self.assertEqual(result["status"], "READY")
        self.assertEqual(len(calls), 5)

    def test_each_drift_fails_the_preflight(self):
        drifted_model = [completion() for _ in range(4)] + [completion(model="provider-model-y")]
        missing_usage = [completion() for _ in range(5)]
        missing_usage[2] = copy.deepcopy(missing_usage[2])
        del missing_usage[2]["usage"]
        wrong_answer = [completion() for _ in range(4)] + [completion("390")]
        ambiguous = [completion() for _ in range(4)] + [completion("391 or 17 23")]
        for label, responses in (("model", drifted_model), ("schema", missing_usage),
                                 ("answer", wrong_answer), ("ambiguous", ambiguous),
                                 ("short", [completion() for _ in range(4)])):
            with self.subTest(label=label):
                self.assertEqual(canary.evaluate(responses)["status"], "FAILED")

    def test_a_failed_call_fails_without_retry(self):
        count = [0]

        def send(_request):
            count[0] += 1
            if count[0] == 3:
                raise ConnectionError()
            return completion()
        result = canary.run_preflight(send, model="qwen3.8-flash")
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(count[0], 5)


if __name__ == "__main__":
    unittest.main()
