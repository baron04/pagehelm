"""The packaged price registry and accounting contract are tested without a lab relay."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from web_operation_agent.modeling.client import (PRICES, BudgetExhausted, SpendMeter, UnpricedModel,
                                       cost_with, load_env)

#: The rate these tests meter at. Named here rather than defaulted inside `SpendMeter`, so a
#: test that books spend says which price it books at.
PRICE = PRICES["qwen3.8-flash"]


class PriceAgreement(unittest.TestCase):
    def test_a_meter_with_no_price_refuses_to_cost_anything(self):
        """There is no module-level default rate, deliberately.

        There used to be one, naming a model no ledger entry has ever used, so a meter that
        nobody had given a price to still produced numbers -- at that model's rates. A wrong
        rate does not fail; it makes the running total look right while being wrong, and the
        ledger is this phase's only cost control. `ChatModel` sets the price of the model
        actually in use, and a meter that never went through it now says so.
        """
        with tempfile.TemporaryDirectory() as directory:
            meter = SpendMeter(ledger=Path(directory) / "spend.jsonl")
            self.assertIsNone(meter.price)
            with self.assertRaisesRegex(UnpricedModel, "no price"):
                meter.cost_of(1_000, 1_000, 0)

    def test_a_price_given_explicitly_is_honoured(self):
        with tempfile.TemporaryDirectory() as directory:
            meter = SpendMeter(ledger=Path(directory) / "spend.jsonl",
                               price=PRICES["qwen3.8-flash"])
            self.assertEqual(meter.cost_of(1_000_000, 0, 0),
                             PRICES["qwen3.8-flash"]["input_per_m"])

    def test_every_registered_price_records_where_it_came_from(self):
        from web_operation_agent.modeling.client import PRICES
        for model, entry in PRICES.items():
            if model.startswith("_"):
                continue
            self.assertTrue(entry.get("taken"), f"{model} has no provenance for its rates")
            for field in ("input_per_m", "output_per_m", "cache_read_per_m", "currency"):
                self.assertIn(field, entry, f"{model} is missing {field}")

    def test_an_unregistered_model_refuses_rather_than_guessing(self):
        from web_operation_agent.modeling.client import UnpricedModel, price_for
        with self.assertRaises(UnpricedModel) as caught:
            price_for("some-model-nobody-registered")
        self.assertIn("prices.json", str(caught.exception),
                      "the refusal must say what to do about it")

    def test_the_cost_formula_uses_registered_price_fields(self):
        i, o, c = 12345, 678, 900
        expected = ((i - c) / 1e6 * PRICE["input_per_m"] + c / 1e6 * PRICE["cache_read_per_m"]
                    + o / 1e6 * PRICE["output_per_m"])
        self.assertAlmostEqual(cost_with(PRICE, i, o, c), expected, places=12)

    def test_impossible_usage_is_an_error_not_a_clamp(self):
        for bad in ((10, 0, 11), (-1, 0, 0), (10, -1, 0)):
            with self.assertRaises(ValueError):
                cost_with(PRICE, *bad)


class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "spend.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _meter(self, **kw):
        kw.setdefault("price", PRICE)
        return SpendMeter(ledger=self.path, batch_id="test", **kw)

    def test_one_line_per_call_with_everything_the_ledger_must_carry(self):
        m = self._meter()
        m.record(provider="p", model="m", run_id="r1",
                 usage={"prompt_tokens": 100, "completion_tokens": 10})
        rec = json.loads(self.path.read_text().strip())
        for key in ("provider", "model", "batch_id", "logical_run_id", "in", "out",
                    "call_cost_cny", "calls"):
            self.assertIn(key, rec)
        self.assertEqual(rec["provider"], "p")
        self.assertEqual(rec["in"], 100)

    def test_provider_request_id_is_optional_but_strict_when_present(self):
        m = self._meter()
        request_id = "a" * 32
        rec = m.record(provider="p", model="m", run_id="r1", usage={},
                       request_id=request_id)
        self.assertEqual(rec["provider_request_id"], request_id)
        with self.assertRaisesRegex(ValueError, "request_id"):
            m.record(provider="p", model="m", run_id="r1", usage={}, request_id="unsafe")

    def test_a_dispatched_call_without_usage_is_recorded_as_unknown_not_zero(self):
        m = self._meter()
        rec = m.record_unmetered(
            provider="p", model="m", run_id="r1", request_id="b" * 32,
            error_code="provider_transport")
        self.assertEqual(rec["accounting_status"], "usage_unavailable")
        self.assertIsNone(rec["call_cost_cny"])
        self.assertIsNone(rec["in"])
        self.assertEqual(m.report()["provider_calls"], 1)
        self.assertEqual(m.report()["unmetered_provider_calls"], 1)

    def test_run_report_is_content_free_and_scoped_to_one_logical_run(self):
        m = self._meter()
        first_request = "a" * 32
        second_request = "b" * 32
        m.record(
            provider="p", model="configured", run_id="r1",
            usage={"prompt_tokens": 100, "completion_tokens": 12,
                   "prompt_tokens_details": {"cached_tokens": 20},
                   "completion_tokens_details": {"reasoning_tokens": 4}},
            request_id=first_request, reported_model="snapshot-a",
        )
        m.record_unmetered(
            provider="p", model="configured", run_id="r1",
            request_id=second_request, error_code="provider_transport",
        )
        m.record(
            provider="p", model="configured", run_id="r2",
            usage={"prompt_tokens": 7, "completion_tokens": 2},
            request_id="c" * 32, reported_model="snapshot-b",
        )

        report = m.run_report("r1")
        self.assertEqual(report["model_calls"], 2)
        self.assertEqual(report["usage_unavailable_calls"], 1)
        self.assertEqual(report["provider_reported_models"], ["snapshot-a"])
        self.assertEqual(report["known_input_tokens"], 100)
        self.assertEqual(report["known_output_tokens"], 12)
        self.assertEqual(report["known_cached_tokens"], 20)
        self.assertEqual(report["known_reasoning_tokens"], 4)
        self.assertIsNone(report["total_cost_cny"])
        encoded = json.dumps(report)
        self.assertNotIn(first_request, encoded)
        self.assertNotIn(second_request, encoded)
        self.assertEqual(m.run_report("missing")["model_calls"], 0)

    def test_failed_ledger_append_does_not_advance_in_memory_accounting(self):
        m = self._meter()
        with mock.patch.object(m, "_append_record", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                m.record(
                    provider="p", model="m", run_id="r1",
                    usage={"prompt_tokens": 100, "completion_tokens": 10},
                    request_id="a" * 32,
                )
        self.assertEqual(m.report()["provider_calls"], 0)
        self.assertEqual(m.run_report("r1")["model_calls"], 0)

    def test_reported_model_is_validated_before_writing(self):
        m = self._meter()
        with self.assertRaisesRegex(ValueError, "reported_model"):
            m.record(
                provider="p", model="m", run_id="r1", usage={},
                request_id="a" * 32, reported_model="bad\nmodel",
            )
        self.assertFalse(self.path.exists())

    def test_the_bound_is_on_the_model_string_not_on_the_padding_around_it(self):
        """A model is the same model however the provider pads what it returns. The bound was
        read off the padded string, so a name of the maximum length arrived one character over
        and the call it belonged to could not be spent against -- while the apparatus measuring
        this product had already recorded the same answer as good."""
        m = self._meter()
        padded = "x" * 120 + " "
        m.record(
            provider="p", model="configured", run_id="r1",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            request_id="a" * 32, reported_model=padded,
        )
        self.assertEqual(m.run_report("r1")["provider_reported_models"], ["x" * 120])
        # What is over the bound once the padding is gone is still refused.
        with self.assertRaisesRegex(ValueError, "reported_model"):
            m.record(
                provider="p", model="configured", run_id="r1",
                usage={"prompt_tokens": 1, "completion_tokens": 1},
                request_id="b" * 32, reported_model="x" * 121,
            )

    def test_cached_tokens_are_read_from_the_provider_reply(self):
        m = self._meter()
        m.record(provider="p", model="m", run_id="r1",
                 usage={"prompt_tokens": 100, "completion_tokens": 0,
                        "prompt_tokens_details": {"cached_tokens": 40}})
        self.assertEqual(m.total_cached, 40)

    def test_the_per_run_token_ceiling_stops_that_run(self):
        m = self._meter(run_input_token_abort=150)
        m.record(provider="p", model="m", run_id="r1",
                 usage={"prompt_tokens": 100, "completion_tokens": 1})
        with self.assertRaises(BudgetExhausted) as cm:
            m.record(provider="p", model="m", run_id="r1",
                     usage={"prompt_tokens": 100, "completion_tokens": 1})
        self.assertEqual(cm.exception.kind, "token_budget")
        self.assertEqual(len(self.path.read_text().strip().splitlines()), 2,
                         "the call that tripped the ceiling still cost money and must be booked")

    def test_the_run_ceiling_is_per_run_not_shared(self):
        m = self._meter(run_input_token_abort=150)
        m.record(provider="p", model="m", run_id="r1",
                 usage={"prompt_tokens": 140, "completion_tokens": 1})
        m.record(provider="p", model="m", run_id="r2",
                 usage={"prompt_tokens": 140, "completion_tokens": 1})
        self.assertEqual(m.calls, 2)

    def test_a_batch_cost_stop_line_halts_the_batch(self):
        m = self._meter(batch_cost_stop=0.0001, run_input_token_abort=10_000_000)
        with self.assertRaises(BudgetExhausted) as cm:
            m.record(provider="p", model="m", run_id="r1",
                     usage={"prompt_tokens": 1_000_000, "completion_tokens": 0})
        self.assertEqual(cm.exception.kind, "batch_cost_stop")

    def test_the_environment_redirects_a_meter_that_names_no_file(self):
        os.environ["WOA_SPEND_LEDGER"] = str(self.path)
        try:
            self.assertEqual(SpendMeter().ledger, self.path)
            self.assertEqual(SpendMeter(ledger=self.path / "explicit").ledger,
                             self.path / "explicit",
                             "an explicit path must win; otherwise a caller cannot direct "
                             "its own accounting")
        finally:
            os.environ.pop("WOA_SPEND_LEDGER")

    def test_the_default_ledger_has_a_product_owned_name(self):
        from web_operation_agent.modeling.client import DEFAULT_LEDGER
        self.assertTrue(DEFAULT_LEDGER.endswith("web-operation-agent/model-spend.jsonl"))


class Environment(unittest.TestCase):
    def test_the_env_reader_never_returns_a_value_it_was_not_asked_for(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text('# comment\nA=1\nB="two"\n\nBROKEN\n')
            env = load_env(p)
            self.assertEqual(env, {"A": "1", "B": "two"})

    def test_a_missing_env_file_is_empty_not_an_error(self):
        self.assertEqual(load_env(Path("/no/such/.env")), {})


if __name__ == "__main__":
    unittest.main()


class TheClientIsNotTiedToOneProvider(unittest.TestCase):
    """Swapping models must be a .env change, not a code change."""

    ENV = {"AGENT_OPENAI_BASE_URL": "https://api.example-vendor.com/v1",
           "AGENT_OPENAI_API_KEY": "k", "AGENT_OPENAI_MODEL": "qwen3.8-flash"}

    def _chat(self, **over):
        from web_operation_agent.modeling.client import ChatModel
        with tempfile.TemporaryDirectory() as d:
            meter = SpendMeter(ledger=Path(d) / "s.jsonl")
            return ChatModel(meter, env={**self.ENV, **over})

    def test_no_vendor_name_is_hardcoded_as_a_default(self):
        import inspect
        from web_operation_agent.modeling import client as m
        sig = inspect.signature(m.ChatModel.__init__)
        self.assertIsNone(sig.parameters["provider"].default,
                          "a vendor name as a default is a coupling waiting to go stale")

    def test_the_ledger_label_is_derived_from_the_endpoint(self):
        self.assertEqual(self._chat().provider, "example-vendor")

    def test_the_label_can_be_set_explicitly_by_environment(self):
        self.assertEqual(self._chat(AGENT_PROVIDER="whoever").provider, "whoever")

    def test_an_odd_base_url_still_yields_some_label(self):
        self.assertTrue(self._chat(AGENT_OPENAI_BASE_URL="http://localhost:8080/v1").provider)

    def test_remote_provider_requires_https_and_rejects_url_credentials(self):
        from web_operation_agent.modeling.client import ProviderError
        for value in (
                "http://provider.example/v1",
                "https://user:secret@provider.example/v1",
                "file:///tmp/provider"):
            with self.subTest(value=value), self.assertRaises(ProviderError):
                self._chat(AGENT_OPENAI_BASE_URL=value)

    def test_the_model_name_comes_from_the_environment(self):
        self.assertEqual(self._chat().model, "qwen3.8-flash")

    def test_an_unpriced_model_is_refused_at_construction(self):
        """Better to stop before the first call than to book the run at the wrong rates."""
        from web_operation_agent.modeling.client import UnpricedModel
        with self.assertRaises(UnpricedModel):
            self._chat(AGENT_OPENAI_MODEL="a-model-with-no-price")

    def test_the_old_tier_name_is_refused_rather_than_quietly_honoured(self):
        """`AGENT_OPENAI_MODEL_PRO` was renamed; a fallback would pick the wrong model.

        Nothing ever ran two tiers -- every construction site takes exactly one model -- and
        the old name meant a flash model could only be selected through a key called PRO.
        Reading it as a fallback is the failure this project keeps removing: an environment
        nobody updated would keep working while naming something else.
        """
        from web_operation_agent.modeling.client import ProviderError
        env = {name: value for name, value in self.ENV.items()
               if name != "AGENT_OPENAI_MODEL"}
        env["AGENT_OPENAI_MODEL_PRO"] = "qwen3.8-flash"
        with self.assertRaisesRegex(ProviderError, "AGENT_OPENAI_MODEL"):
            self._chat(**env, AGENT_OPENAI_MODEL="")

    def test_the_meter_is_given_the_price_of_the_model_actually_in_use(self):
        from web_operation_agent.modeling.client import PRICES
        chat = self._chat()
        self.assertEqual(chat.meter.price, PRICES["qwen3.8-flash"])


class ProviderSignalsReachTheCaller(unittest.TestCase):
    """finish_reason and out-of-band reasoning are how the caller tells a budget problem from
    an answer problem, without knowing which vendor it is talking to."""

    def _reply(self, payload):
        import io
        import urllib.request
        from unittest import mock
        from web_operation_agent.modeling.client import ChatModel
        with tempfile.TemporaryDirectory() as d:
            chat = ChatModel(SpendMeter(ledger=Path(d) / "s.jsonl"),
                             env=TheClientIsNotTiedToOneProvider.ENV)
            body = io.BytesIO(json.dumps(payload).encode())
            body.__enter__ = lambda s=body: s
            body.__exit__ = lambda s=body, *a: None
            with mock.patch.object(urllib.request, "urlopen", return_value=body):
                return chat.complete([{"role": "user", "content": "hi"}], None, "r1")

    def test_finish_reason_is_lifted_onto_the_message(self):
        msg = self._reply({"usage": {"prompt_tokens": 1, "completion_tokens": 1},
                           "choices": [{"finish_reason": "length",
                                        "message": {"content": "cut"}}]})
        self.assertEqual(msg["finish_reason"], "length")

    def test_reasoning_content_is_normalised_onto_reasoning(self):
        msg = self._reply({"usage": {"prompt_tokens": 1, "completion_tokens": 1},
                           "choices": [{"finish_reason": "stop",
                                        "message": {"content": "{}",
                                                    "reasoning_content": "thought"}}]})
        self.assertEqual(msg["reasoning"], "thought")

    def test_a_provider_that_reports_neither_is_still_usable(self):
        msg = self._reply({"usage": {"prompt_tokens": 1, "completion_tokens": 1},
                           "choices": [{"message": {"content": "{}"}}]})
        self.assertEqual(msg["finish_reason"], "")
        self.assertEqual(msg["reasoning"], "")

    def test_oversized_provider_response_is_rejected_before_json_parsing(self):
        import io
        import urllib.request
        from unittest import mock
        from web_operation_agent.modeling.client import (MAX_PROVIDER_RESPONSE_BYTES, ChatModel,
                                                ProviderError)
        with tempfile.TemporaryDirectory() as directory:
            chat = ChatModel(
                SpendMeter(ledger=Path(directory) / "spend.jsonl"),
                env=TheClientIsNotTiedToOneProvider.ENV)
            body = io.BytesIO(b"x" * (MAX_PROVIDER_RESPONSE_BYTES + 1))
            body.__enter__ = lambda stream=body: stream
            body.__exit__ = lambda stream=body, *_args: None
            with mock.patch.object(urllib.request, "urlopen", return_value=body):
                with self.assertRaisesRegex(ProviderError, "4 MiB"):
                    chat.complete([{"role": "user", "content": "hi"}], None, "r1")

    def test_http_error_body_is_not_copied_into_the_exception(self):
        import io
        import urllib.error
        import urllib.request
        from unittest import mock
        from web_operation_agent.modeling.client import ChatModel, ProviderError
        with tempfile.TemporaryDirectory() as directory:
            chat = ChatModel(
                SpendMeter(ledger=Path(directory) / "spend.jsonl"),
                env=TheClientIsNotTiedToOneProvider.ENV)
            error = urllib.error.HTTPError(
                chat.base, 401, "unauthorized", {},
                io.BytesIO(b"access_token=PROVIDER_SECRET"))
            with mock.patch.object(urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(ProviderError) as caught:
                    chat.complete([{"role": "user", "content": "hi"}], None, "r1")
        self.assertEqual(str(caught.exception), "provider returned HTTP 401")
        self.assertNotIn("PROVIDER_SECRET", str(caught.exception))


class ReasoningTokensAreAccountedSeparately(unittest.TestCase):
    """Billed inside the completion count, but worth their own column: on a reasoning model
    most of a planning call can be thought, and different planning modes can use different
    numbers of model calls."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "spend.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_provider_that_reports_them_has_them_recorded(self):
        m = SpendMeter(ledger=self.path, batch_id="t", price=PRICE)
        m.record(provider="p", model="m", run_id="r1",
                 usage={"prompt_tokens": 100, "completion_tokens": 55,
                        "completion_tokens_details": {"reasoning_tokens": 26}})
        rec = json.loads(self.path.read_text().strip())
        self.assertEqual(rec["reasoning"], 26)
        self.assertEqual(m.report()["reasoning_tokens"], 26)

    def test_the_cost_still_comes_from_the_completion_count(self):
        """Reasoning is inside completion_tokens; counting it again would double-charge."""
        m = SpendMeter(ledger=self.path, batch_id="t", price=PRICE)
        rec = m.record(provider="p", model="m", run_id="r1",
                       usage={"prompt_tokens": 0, "completion_tokens": 55,
                              "completion_tokens_details": {"reasoning_tokens": 26}})
        self.assertAlmostEqual(rec["call_cost_cny"],
                               round(cost_with(PRICE, 0, 55, 0), 6), places=9)

    def test_a_provider_that_does_not_report_them_is_fine(self):
        m = SpendMeter(ledger=self.path, batch_id="t", price=PRICE)
        m.record(provider="p", model="m", run_id="r1",
                 usage={"prompt_tokens": 10, "completion_tokens": 5})
        self.assertEqual(m.report()["reasoning_tokens"], 0)

    def test_malformed_usage_cannot_corrupt_in_memory_or_disk_totals(self):
        m = SpendMeter(ledger=self.path, batch_id="t", price=PRICE)
        invalid = (
            {"prompt_tokens": -1, "completion_tokens": 0},
            {"prompt_tokens": 1, "completion_tokens": 0,
             "prompt_tokens_details": {"cached_tokens": 2}},
            {"prompt_tokens": "10", "completion_tokens": 0},
            {"prompt_tokens": 1, "completion_tokens": 0,
             "completion_tokens_details": []},
        )
        for usage in invalid:
            with self.subTest(usage=usage), self.assertRaises(ValueError):
                m.record(provider="p", model="m", run_id="r", usage=usage)
        self.assertEqual(m.calls, 0)
        self.assertEqual(m.report()["input_tokens"], 0)
        self.assertFalse(self.path.exists())


class TheReportedLedgerPathCarriesNoMachine(unittest.TestCase):
    """A shared run record identifies its ledger without exposing a user's home directory."""

    def test_a_path_under_home_is_reported_relative_to_it(self):
        import os
        from web_operation_agent.modeling.client import SpendMeter
        m = SpendMeter(ledger=os.path.expanduser("~/.cache/woa-test-display/x.jsonl"))
        reported = m.report()["ledger"]
        self.assertTrue(reported.startswith("~/"), reported)
        self.assertNotIn(os.path.expanduser("~"), reported)

    def test_a_path_outside_home_is_left_alone(self):
        import tempfile
        from web_operation_agent.modeling.client import SpendMeter
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            m = SpendMeter(ledger=f"{d}/x.jsonl")
            self.assertEqual(m.report()["ledger"], f"{d}/x.jsonl")

    def test_the_file_actually_used_is_unchanged(self):
        import os
        from web_operation_agent.modeling.client import SpendMeter
        p = os.path.expanduser("~/.cache/woa-test-display/y.jsonl")
        m = SpendMeter(ledger=p)
        self.assertEqual(str(m.ledger), p, "only the reported name changes, not the target")
