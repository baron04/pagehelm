"""Model client and spend accounting.

Every actual model call must correspond to exactly one line in the spend ledger, and every
ledger line must correspond to an actual call. The ledger file, price table and cost formula
therefore form one accounting contract rather than independent estimates.

The ledger path is overridable so tests never need to touch the real ledger; the test runner
also fingerprints the configured real file before and after the suite as a final guard.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = os.path.expanduser("~/.cache/web-operation-agent/model-spend.jsonl")

def load_env(path: str | Path | None = None) -> dict:
    """Read `.env` without echoing anything. Values are returned, never logged."""
    p = Path(path or REPO_ROOT / ".env")
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class ProviderError(Exception):
    """Callers map this to terminal reason `provider_error`."""


#: Per-model prices, per million tokens. Registered rather than assumed: a wrong price does
#: not fail, it quietly mis-states the running total. A model with no entry therefore raises
#: instead of guessing, and adding one requires an explicit dated price entry.
PRICES = json.loads((Path(__file__).parent / "policies" / "prices.json").read_text())

#: The model used by existing ledger entries. Keeping it as the module-level default preserves
#: a comparable cumulative figure; the accounting tests verify the shared price contract.
DEFAULT_PRICE_MODEL = "MiniMax-M3"
PRICE = PRICES[DEFAULT_PRICE_MODEL]


#: Extra request-body fields per model, keyed by the model's own name. See the file's own
#: `_why_thinking_budget` note for the rationale. Keyed by model rather than branched on in
#: code so changing model is a policy edit and an unknown model receives no unsupported fields.
MODEL_OPTIONS = json.loads((Path(__file__).parent / "policies" / "model_options.json").read_text())

#: Fields the caller owns; a policy may never reach in and rewrite the request itself.
_RESERVED_BODY = ("model", "messages", "tools", "tool_choice", "max_tokens")


def options_for(model: str) -> dict:
    """Extra body fields for a model. Unknown models get none, deliberately."""
    opt = MODEL_OPTIONS.get(model)
    return {k: v for k, v in opt.items() if k not in _RESERVED_BODY} \
        if isinstance(opt, dict) else {}


class UnpricedModel(ProviderError):
    """A model was used that has no registered price. Never silently mis-cost a run."""


def price_for(model: str) -> dict:
    """The price table for a model, or a refusal naming what to add."""
    if model in PRICES:
        return PRICES[model]
    raise UnpricedModel(
        f"no price registered for model {model!r}; add it to "
        f"src/web_operation_agent/policies/prices.json with the date it was taken, or the "
        f"ledger will under- or over-state what this run cost")


def cost_with(price: dict, i: int, o: int, c: int) -> float:
    """The frozen formula: cached prompt tokens at the cache rate, the rest at the input rate,
    completions at the output rate. An impossible figure is an accounting error, never clamped."""
    if c < 0 or c > i or i < 0 or o < 0:
        raise ValueError(f"impossible usage: in={i} out={o} cached={c}")
    return ((i - c) / 1e6 * price["input_per_m"] + c / 1e6 * price["cache_read_per_m"]
            + o / 1e6 * price["output_per_m"])


def _host_label(base_url: str) -> str:
    """A short, stable label from an endpoint's host, for the ledger only."""
    try:
        host = urllib.parse.urlsplit(base_url).hostname or ""
    except Exception:
        return "openai-compatible"
    parts = [p for p in host.split(".") if p not in ("www", "api", "com", "cn", "io", "ai", "co")]
    return parts[0] if parts else (host or "openai-compatible")


def _display_path(path) -> str:
    """A path fit to store in a shared record: relative to home where it lives there."""
    text = str(path)
    home = os.path.expanduser("~")
    return "~" + text[len(home):] if home and text.startswith(home) else text


class BudgetExhausted(Exception):
    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind


class SpendMeter:
    """Cumulative usage, one ledger line per call, with the two stop lines."""

    def __init__(self, *, ledger: str | Path | None = None, batch_id: str = "",
                 run_input_token_abort: int = 220000, batch_cost_stop: float | None = None,
                 price: dict | None = None):
        # Precedence: an explicit argument, then the environment override, then the
        # project ledger. The environment is how a test run redirects everything at once;
        # a caller that names a file means that file.
        self.ledger = Path(ledger or os.environ.get("WOA_SPEND_LEDGER") or DEFAULT_LEDGER)
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.batch_id = batch_id
        self.run_input_token_abort = run_input_token_abort
        self.batch_cost_stop = batch_cost_stop
        self._lock = threading.Lock()
        self.calls = 0
        self.total_in = self.total_out = self.total_cached = self.total_reasoning = 0
        self.batch_in = self.batch_out = self.batch_cached = 0
        self.run_in: dict[str, int] = {}
        self.price = price or PRICE

    def cost_of(self, i: int, o: int, c: int) -> float:
        return cost_with(self.price, i, o, c)

    def batch_cost(self) -> float:
        return self.cost_of(self.batch_in, self.batch_out, self.batch_cached)

    def record(self, *, provider: str, model: str, run_id: str, usage: dict) -> dict:
        i = int(usage.get("prompt_tokens") or 0)
        o = int(usage.get("completion_tokens") or 0)
        c = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        # Reasoning is billed inside the completion count, so the cost is already right, but
        # it is worth its own column: on a reasoning model most of a planning call can be
        # thought rather than answer, and the source arm makes an extra planning call the
        # control arm does not. Absent from providers that do not report it.
        r = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        with self._lock:
            self.calls += 1
            self.total_in += i
            self.total_out += o
            self.total_cached += c
            self.total_reasoning += r
            self.batch_in += i
            self.batch_out += o
            self.batch_cached += c
            self.run_in[run_id] = self.run_in.get(run_id, 0) + i
            rec = {"t": time.time(), "provider": provider, "model": model,
                   "batch_id": self.batch_id, "logical_run_id": run_id,
                   "in": i, "out": o, "cached": c, "reasoning": r,
                   "cum_in": self.total_in, "cum_out": self.total_out,
                   "call_cost_cny": round(self.cost_of(i, o, c), 6),
                   "batch_cost_cny": round(self.batch_cost(), 4),
                   "run_in_tokens": self.run_in[run_id], "calls": self.calls}
            with open(self.ledger, "a", encoding="utf-8") as fh:
                # The lock is between processes, not threads: sharding a batch across
                # isolated environments runs one harness process per worker and they all
                # account to the one ledger. A torn line there is not a slow ledger, it is a
                # cost figure nobody can reconstruct.
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                except OSError:
                    pass                      # no locking on this filesystem; single writer
                try:
                    fh.write(json.dumps(rec) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                finally:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
        if self.run_in[run_id] > self.run_input_token_abort:
            raise BudgetExhausted("token_budget",
                                  f"run {run_id} consumed {self.run_in[run_id]} input tokens, "
                                  f"over the {self.run_input_token_abort} ceiling")
        if self.batch_cost_stop is not None and self.batch_cost() > self.batch_cost_stop:
            raise BudgetExhausted("batch_cost_stop",
                                  f"batch spend {self.batch_cost():.4f} passed the "
                                  f"{self.batch_cost_stop:.4f} stop line")
        return rec

    def report(self) -> dict:
        return {"provider_calls": self.calls, "input_tokens": self.total_in,
                "output_tokens": self.total_out, "cached_tokens": self.total_cached,
                "reasoning_tokens": self.total_reasoning,
                "cost_cny": round(self.cost_of(self.total_in, self.total_out, self.total_cached), 4),
                # Home-relative, so a run record can be read and compared without carrying
                # whose machine produced it. The file used is unchanged; only its displayed
                # name is normalized.
                "ledger": _display_path(self.ledger), "batch_id": self.batch_id}


class ChatModel:
    """A single OpenAI-compatible chat endpoint with native tool calling."""

    def __init__(self, meter: SpendMeter, *, env: dict | None = None, model: str | None = None,
                 max_completion_tokens: int = 2048, temperature: float = 0.0,
                 timeout: int = 180, provider: str | None = None,
                 options: dict | None = None):
        env = env if env is not None else load_env()
        self.base = (env.get("AGENT_OPENAI_BASE_URL") or "").rstrip("/")
        self._key = env.get("AGENT_OPENAI_API_KEY") or ""
        self.model = model or env.get("AGENT_OPENAI_MODEL_PRO") or ""
        # A label for the ledger, nothing more. It has no behavioural meaning -- the code path
        # is the same for every OpenAI-compatible endpoint -- and defaults to the host in the
        # base URL so switching providers needs no code change, only .env.
        provider = provider or env.get("AGENT_PROVIDER") or _host_label(self.base)
        if not (self.base and self._key and self.model):
            raise ProviderError("AGENT_OPENAI_BASE_URL / _API_KEY / _MODEL_PRO are not all set")
        self.meter = meter
        # Fail now, loudly, rather than book a run at another model's rates.
        meter.price = price_for(self.model)
        self.provider = provider
        self.max_completion_tokens = max_completion_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.options = ({k: v for k, v in options.items() if k not in _RESERVED_BODY}
                        if options is not None else options_for(self.model))

    def complete(self, messages: list[dict], tools: list[dict] | None, run_id: str,
                 max_tokens: int | None = None, options: dict | None = None,
                 timeout_s: float | None = None) -> dict:
        """`max_tokens` overrides the per-call ceiling for this one request.

        The action loop's 2,048 is frozen: one structured tool call never needs more, and a
        model that spends thousands of reasoning tokens per click buys nothing. A contract
        document is a different shape of answer, and the shared ceiling truncated it mid-JSON
        in four of six source-arm runs, which measured the ceiling rather than the contract.

        `max_tokens` turns out to bound only the answer on at least one endpoint we use, so
        it cannot by itself stop a model thinking for two minutes about a click. `options`
        carries the per-model fields that can -- see `policies/model_options.json`; a caller
        passing its own merges over the model's, and neither may touch the fields above.
        """
        body = {"model": self.model, "messages": messages, "temperature": self.temperature,
                "max_tokens": max_tokens or self.max_completion_tokens}
        body.update(self.options)
        if options:
            body.update({k: v for k, v in options.items() if k not in _RESERVED_BODY})
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if timeout_s is not None and (
                isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or timeout_s <= 0):
            raise ValueError("timeout_s must be a positive finite number")
        request_timeout = self.timeout if timeout_s is None else min(self.timeout, timeout_s)
        payload = json.dumps(body).encode()
        req = urllib.request.Request(f"{self.base}/chat/completions", data=payload, method="POST",
                                     headers={"content-type": "application/json",
                                              "authorization": f"Bearer {self._key}"})
        try:
            with urllib.request.urlopen(req, timeout=request_timeout) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            raise ProviderError(f"provider returned {e.code}: {e.read()[:400]!r}")
        except Exception as e:
            raise ProviderError(f"provider call failed: {type(e).__name__}: {e}")
        usage = data.get("usage") or {}
        # Record before inspecting the answer: a malformed reply still cost money.
        self.meter.record(provider=self.provider, model=self.model, run_id=run_id, usage=usage)
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"provider returned no choices: {str(data)[:300]}")
        choice = choices[0]
        message = choice.get("message") or {}
        # Two provider-neutral signals lifted to the top of the message so callers never have
        # to know which vendor produced it:
        #  * finish_reason distinguishes "the answer was cut off" (a budget problem the caller
        #    can fix) from "the model stopped on its own" (an answer problem it cannot);
        #  * reasoning is whatever the provider returned in a separate reasoning channel, under
        #    any of the several field names vendors use for it. Inline <think> text is handled
        #    later, in split_reasoning; this only catches the out-of-band form.
        message["finish_reason"] = choice.get("finish_reason") or ""
        message.setdefault("reasoning",
                           message.get("reasoning_content") or message.get("reasoning") or "")
        return message
