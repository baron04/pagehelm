"""Provider pricing, spend limits, and the append-only spend ledger.

Every dispatched model call produces exactly one ledger row.  Reported usage is costed from
an explicit per-model price; missing usage is recorded as unknown rather than silently zero.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
from pathlib import Path

from .provider import ProviderError


DEFAULT_LEDGER = os.path.expanduser("~/.cache/web-operation-agent/model-spend.jsonl")


def reported_model_string(value: object) -> str | None:
    """A provider's model string as this codebase compares it, or None if it is not one.

    One definition, because two of them is how the same string was read as two models: a
    provider padding what it returns passed one check under one spelling and failed the next
    under another. A model is the same model however it is padded, so the length bound is on
    the string that is kept; control characters are rejected in what arrived, so a model
    string can never carry a newline into a record whatever surrounds it.
    """
    if not isinstance(value, str):
        return None
    selected = value.strip()
    if not selected or len(selected) > 120 or re.search(r"[\x00-\x1f\x7f]", value):
        return None
    return selected


#: Per-model prices, per million tokens. Registered rather than assumed: a wrong price does
#: not fail, it quietly mis-states the running total. A model with no entry therefore raises
#: instead of guessing, and adding one requires an explicit dated price entry.
PRICES = json.loads((Path(__file__).parents[1] / "policies" / "prices.json").read_text())

SELF_HOSTED_PRICE: dict[str, object] = {
    "input_per_m": 0.0,
    "output_per_m": 0.0,
    "cache_read_per_m": 0.0,
    "currency": "CNY",
    "taken": "self-hosted (explicit zero-cost reference pricing)",
}


class UnpricedModel(ProviderError):
    """A model was used that has no registered price. Never silently mis-cost a run."""


def price_for(model: str, *, self_hosted: bool = False) -> dict:
    """The price table for a model, or an explicit refusal naming how to register or mark self-hosted."""
    if self_hosted:
        return dict(SELF_HOSTED_PRICE)
    if model in PRICES:
        return PRICES[model]
    raise UnpricedModel(
        f"no price registered for model {model!r}; add it to "
        f"src/web_operation_agent/policies/prices.json with the date it was taken, "
        f"or set AGENT_MODEL_SELF_HOSTED=true in .env "
        f"for local/self-hosted models so the ledger does not mis-state paid spend")


def cost_with(price: dict, i: int, o: int, c: int) -> float:
    """Cost cached prompt tokens, uncached input, and output at their registered rates."""
    if c < 0 or c > i or i < 0 or o < 0:
        raise ValueError(f"impossible usage: in={i} out={o} cached={c}")
    return ((i - c) / 1e6 * price["input_per_m"] + c / 1e6 * price["cache_read_per_m"]
            + o / 1e6 * price["output_per_m"])


def _display_path(path) -> str:
    """A path fit to store in a shared record: relative to home where it lives there."""
    text = str(path)
    home = os.path.expanduser("~")
    return "~" + text[len(home):] if home and text.startswith(home) else text


class BudgetExhausted(Exception):
    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind


class DailyAllowance:
    """What each key (a person, as the deployment names them) may spend on the model per day.

    Checked before a call is dispatched, so a key that has reached its limit sends nothing
    more until the day turns; calls already in flight finish and are counted, so the limit
    can be passed by at most what those calls cost. The tally is rebuilt from the ledger's
    rows for today when the process starts, so a restart does not hand everyone a new day.
    A call whose usage the provider did not report counts as nothing here; the ledger keeps
    it as unknown.
    """

    def __init__(self, limit_cny: float, *, utc_offset_minutes: int = 0):
        if (isinstance(limit_cny, bool) or not isinstance(limit_cny, (int, float))
                or not limit_cny > 0 or limit_cny != limit_cny):
            raise ValueError("limit_cny must be a positive number")
        self.limit_cny = float(limit_cny)
        self._offset_s = int(utc_offset_minutes) * 60
        self._lock = threading.Lock()
        self._day = self._today()
        self._spent: dict[str, float] = {}

    def _today(self, t: float | None = None) -> int:
        return int(((time.time() if t is None else t) + self._offset_s) // 86400)

    def _roll(self) -> None:
        today = self._today()
        if today != self._day:
            self._day, self._spent = today, {}

    def load(self, ledger: str | Path) -> None:
        """Count today's rows that carry an allowance key."""
        try:
            lines = Path(ledger).read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        with self._lock:
            self._roll()
            for line in lines:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                key, cost, t = row.get("allowance_key"), row.get("call_cost_cny"), row.get("t")
                if (isinstance(key, str) and isinstance(cost, (int, float))
                        and isinstance(t, (int, float)) and self._today(t) == self._day):
                    self._spent[key] = self._spent.get(key, 0.0) + float(cost)

    def spent(self, key: str) -> float:
        with self._lock:
            self._roll()
            return self._spent.get(key, 0.0)

    def allows(self, key: str) -> bool:
        return self.spent(key) < self.limit_cny

    def add(self, key: str, cost_cny: float) -> None:
        with self._lock:
            self._roll()
            self._spent[key] = self._spent.get(key, 0.0) + float(cost_cny)


@dataclass(frozen=True, slots=True)
class SpendCapReservation:
    """Outcome of an atomic process-wide spend reservation."""

    allowed: bool
    reason: str = ""
    reservation_id: str = ""
    reserved_cny: float = 0.0
    daily_used_cny: float = 0.0
    monthly_used_cny: float = 0.0


def _validate_optional_cap(value: object, name: str) -> float | None:
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{name} must be a positive finite number or None")
    return float(value)


class SQLiteSpendCap:
    """Atomic process-wide daily and monthly reference spend cap backed by SQLite."""

    def __init__(self, path: str | Path, *,
                 daily_cap_cny: float | None = None,
                 monthly_cap_cny: float | None = None,
                 turn_reserve_cny: float = 0.02,
                 utc_offset_minutes: int = 8 * 60,
                 stale_reservation_s: float = 3600.0,
                 timeout_s: float = 5.0):
        self.daily_cap_cny = _validate_optional_cap(daily_cap_cny, "daily_cap_cny")
        self.monthly_cap_cny = _validate_optional_cap(monthly_cap_cny, "monthly_cap_cny")
        if (isinstance(turn_reserve_cny, bool) or not isinstance(turn_reserve_cny, (int, float))
                or not math.isfinite(turn_reserve_cny) or turn_reserve_cny < 0):
            raise ValueError("turn_reserve_cny must be a non-negative finite number")
        if (isinstance(stale_reservation_s, bool) or not isinstance(stale_reservation_s, (int, float))
                or not math.isfinite(stale_reservation_s) or stale_reservation_s <= 0):
            raise ValueError("stale_reservation_s must be a positive finite number")
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or not 0 < timeout_s <= 120):
            raise ValueError("timeout_s must be in (0, 120]")
        self.turn_reserve_cny = float(turn_reserve_cny)
        self.stale_reservation_s = float(stale_reservation_s)
        self.timeout_s = float(timeout_s)
        self._tz = timezone(timedelta(minutes=int(utc_offset_minutes)))
        candidate = Path(path).expanduser()
        self.path = candidate if candidate.is_absolute() else Path.cwd() / candidate
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("spend cap database path must not be a symbolic link")
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            if not stat.S_ISREG(os.lstat(self.path).st_mode):
                raise ValueError("spend cap database path must be a regular file")
        else:
            os.close(descriptor)
        os.chmod(self.path, 0o600)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS spend_totals ("
                "period_kind TEXT NOT NULL, "
                "period_key TEXT NOT NULL, "
                "spent_cny REAL NOT NULL, "
                "PRIMARY KEY (period_kind, period_key))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS spend_reservations ("
                "reservation_id TEXT PRIMARY KEY, "
                "day_key TEXT NOT NULL, "
                "month_key TEXT NOT NULL, "
                "reserved_cny REAL NOT NULL, "
                "created_at REAL NOT NULL)"
            )

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=self.timeout_s, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def _keys(self, now: float | None = None) -> tuple[str, str, float]:
        ts = time.time() if now is None else float(now)
        dt = datetime.fromtimestamp(ts, tz=self._tz)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m"), ts

    def _usage(self, db, day_key: str, month_key: str, ts: float) -> tuple[float, float]:
        db.execute("DELETE FROM spend_reservations WHERE created_at <= ?",
                   (ts - self.stale_reservation_s,))
        day_spent = db.execute(
            "SELECT COALESCE(spent_cny, 0.0) FROM spend_totals WHERE period_kind='day' AND period_key=?",
            (day_key,),
        ).fetchone()
        month_spent = db.execute(
            "SELECT COALESCE(spent_cny, 0.0) FROM spend_totals WHERE period_kind='month' AND period_key=?",
            (month_key,),
        ).fetchone()
        day_res = db.execute(
            "SELECT COALESCE(SUM(reserved_cny), 0.0) FROM spend_reservations WHERE day_key=?",
            (day_key,),
        ).fetchone()
        month_res = db.execute(
            "SELECT COALESCE(SUM(reserved_cny), 0.0) FROM spend_reservations WHERE month_key=?",
            (month_key,),
        ).fetchone()
        day_used = float(day_spent[0] if day_spent else 0.0) + float(day_res[0] if day_res else 0.0)
        month_used = float(month_spent[0] if month_spent else 0.0) + float(month_res[0] if month_res else 0.0)
        return day_used, month_used

    def reserve(self, reservation_id: str, *, amount_cny: float | None = None,
                now: float | None = None) -> SpendCapReservation:
        if not isinstance(reservation_id, str) or not reservation_id.strip():
            raise ValueError("reservation_id must be a non-empty string")
        if amount_cny is not None and (
                isinstance(amount_cny, bool) or not isinstance(amount_cny, (int, float))
                or not math.isfinite(amount_cny) or amount_cny < 0):
            raise ValueError("amount_cny must be a non-negative finite number")
        day_key, month_key, ts = self._keys(now)
        caps = [c for c in (self.daily_cap_cny, self.monthly_cap_cny) if c is not None]
        if amount_cny is not None:
            req = float(amount_cny)
        elif caps:
            req = min(self.turn_reserve_cny, min(caps))
        else:
            req = 0.0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            day_used, month_used = self._usage(db, day_key, month_key, ts)
            if self.daily_cap_cny is not None and (
                    day_used >= self.daily_cap_cny
                    or day_used + req > self.daily_cap_cny + 1e-12):
                db.commit()
                return SpendCapReservation(
                    allowed=False, reason="daily_cap_exceeded",
                    daily_used_cny=day_used, monthly_used_cny=month_used)
            if self.monthly_cap_cny is not None and (
                    month_used >= self.monthly_cap_cny
                    or month_used + req > self.monthly_cap_cny + 1e-12):
                db.commit()
                return SpendCapReservation(
                    allowed=False, reason="monthly_cap_exceeded",
                    daily_used_cny=day_used, monthly_used_cny=month_used)
            db.execute(
                "INSERT OR REPLACE INTO spend_reservations "
                "(reservation_id, day_key, month_key, reserved_cny, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (reservation_id, day_key, month_key, req, ts),
            )
            db.commit()
            return SpendCapReservation(
                allowed=True, reason="", reservation_id=reservation_id,
                reserved_cny=req, daily_used_cny=day_used + req,
                monthly_used_cny=month_used + req)

    def release(self, reservation_id: str) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM spend_reservations WHERE reservation_id=?", (reservation_id,))
            db.commit()

    def record_spend(self, cost_cny: float, *, now: float | None = None) -> None:
        if (isinstance(cost_cny, bool) or not isinstance(cost_cny, (int, float))
                or not math.isfinite(cost_cny) or cost_cny < 0):
            raise ValueError("cost_cny must be a non-negative finite number")
        day_key, month_key, _ts = self._keys(now)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for kind, key in (("day", day_key), ("month", month_key)):
                db.execute(
                    "INSERT INTO spend_totals (period_kind, period_key, spent_cny) VALUES (?, ?, ?) "
                    "ON CONFLICT(period_kind, period_key) DO UPDATE SET spent_cny = spent_cny + excluded.spent_cny",
                    (kind, key, float(cost_cny)),
                )
            db.commit()

    def sync_from_ledger(self, ledger: str | Path, *, now: float | None = None) -> None:
        """Ensure persisted spend totals are at least the ledger's recorded day/month sums."""
        try:
            lines = Path(ledger).read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        day_key, month_key, _ts = self._keys(now)
        day_sum = 0.0
        month_sum = 0.0
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            cost, t = row.get("call_cost_cny"), row.get("t")
            if not isinstance(cost, (int, float)) or isinstance(cost, bool) or not isinstance(t, (int, float)):
                continue
            row_day, row_month, _ = self._keys(t)
            if row_month == month_key:
                month_sum += float(cost)
                if row_day == day_key:
                    day_sum += float(cost)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for kind, key, total in (("day", day_key, day_sum), ("month", month_key, month_sum)):
                db.execute(
                    "INSERT INTO spend_totals (period_kind, period_key, spent_cny) VALUES (?, ?, ?) "
                    "ON CONFLICT(period_kind, period_key) DO UPDATE SET spent_cny = MAX(spent_cny, excluded.spent_cny)",
                    (kind, key, total),
                )
            db.commit()

    def allows(self, *, now: float | None = None) -> bool:
        day_key, month_key, ts = self._keys(now)
        with self._connect() as db:
            day_used, month_used = self._usage(db, day_key, month_key, ts)
        if self.daily_cap_cny is not None and day_used >= self.daily_cap_cny:
            return False
        if self.monthly_cap_cny is not None and month_used >= self.monthly_cap_cny:
            return False
        return True

    def usage(self, *, now: float | None = None) -> tuple[float, float]:
        day_key, month_key, ts = self._keys(now)
        with self._connect() as db:
            return self._usage(db, day_key, month_key, ts)


class SpendMeter:
    """Cumulative usage, one ledger line per call, with the two stop lines."""

    def __init__(self, *, ledger: str | Path | None = None, batch_id: str = "",
                 run_input_token_abort: int = 220000, batch_cost_stop: float | None = None,
                 price: dict | None = None,
                 allowance: DailyAllowance | None = None, allowance_key: str = "",
                 global_cap: SQLiteSpendCap | None = None):
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
        self.unmetered_calls = 0
        self.total_in = self.total_out = self.total_cached = self.total_reasoning = 0
        self.batch_in = self.batch_out = self.batch_cached = 0
        self.run_in: dict[str, int] = {}
        self._run_summaries: dict[str, dict] = {}
        # No default rate. A meter costs nothing until `ChatModel` sets the price of the
        # model actually in use; a module-level fallback would book a run at some other
        # model's rates and the ledger would look right while being wrong.
        self.price = price
        if allowance is not None and not re.fullmatch(r"[0-9a-f]{8,64}", allowance_key):
            raise ValueError("allowance_key must be an opaque lowercase hex identifier")
        if global_cap is not None and not isinstance(global_cap, SQLiteSpendCap):
            raise TypeError("global_cap must be SQLiteSpendCap or None")
        self.allowance, self.allowance_key = allowance, allowance_key
        self.global_cap = global_cap

    def within_allowance(self) -> bool:
        """False once this meter's key or process-wide cap has spent its allowance."""
        if self.allowance is not None and not self.allowance.allows(self.allowance_key):
            return False
        if self.global_cap is not None and not self.global_cap.allows():
            return False
        return True

    def cost_of(self, i: int, o: int, c: int) -> float:
        if self.price is None:
            # Nothing used cannot be mis-stated, so a meter that has not metered still
            # reports. The moment there are tokens, an unknown rate has to stop: a wrong
            # rate does not fail, it makes the running total look right while being wrong.
            if not (i or o or c):
                return 0.0
            raise UnpricedModel(
                "this meter has no price: construct it through ChatModel, which registers "
                "the price of the model in use, or pass price= explicitly")
        return cost_with(self.price, i, o, c)

    def batch_cost(self) -> float:
        return self.cost_of(self.batch_in, self.batch_out, self.batch_cached)

    @staticmethod
    def _reported_model(value: str | None) -> str:
        if value is None:
            return ""
        selected = reported_model_string(value)
        if selected is None:
            raise ValueError("provider reported_model must be a bounded safe string")
        return selected

    @staticmethod
    def _empty_run_summary() -> dict:
        return {
            "model_calls": 0,
            "usage_unavailable_calls": 0,
            "known_input_tokens": 0,
            "known_output_tokens": 0,
            "known_cached_tokens": 0,
            "known_reasoning_tokens": 0,
            "known_cost_cny": 0.0,
            "reported_models": set(),
            "request_ids": [],
            "record_digests": [],
        }

    def _commit_run_record(self, *, run_id: str, rec: dict, reported_model: str,
                           request_id: str, unmetered: bool, known_cost: float = 0.0) -> None:
        summary = self._run_summaries.setdefault(run_id, self._empty_run_summary())
        summary["model_calls"] += 1
        summary["usage_unavailable_calls"] += int(unmetered)
        if not unmetered:
            summary["known_input_tokens"] += rec["in"]
            summary["known_output_tokens"] += rec["out"]
            summary["known_cached_tokens"] += rec["cached"]
            summary["known_reasoning_tokens"] += rec["reasoning"]
            summary["known_cost_cny"] += known_cost
        if reported_model:
            summary["reported_models"].add(reported_model)
        if request_id:
            summary["request_ids"].append(request_id)
        summary["record_digests"].append(hashlib.sha256(json.dumps(
            rec, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode("utf-8")).hexdigest())

    def record(self, *, provider: str, model: str, run_id: str, usage: dict,
               request_id: str = "", reported_model: str | None = None) -> dict:
        if not isinstance(usage, dict):
            raise ValueError("provider usage must be an object")
        if request_id and not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise ValueError("provider request_id must be a 32-character lowercase hex value")
        selected_reported_model = self._reported_model(reported_model)
        prompt_details = usage.get("prompt_tokens_details")
        completion_details = usage.get("completion_tokens_details")
        prompt_details = {} if prompt_details is None else prompt_details
        completion_details = {} if completion_details is None else completion_details
        if not isinstance(prompt_details, dict) or not isinstance(completion_details, dict):
            raise ValueError("provider usage detail must be an object")

        def token_count(value, name: str) -> int:
            value = 0 if value is None else value
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not 0 <= value <= 1_000_000_000):
                raise ValueError(f"{name} must be a non-negative bounded integer")
            return value

        i = token_count(usage.get("prompt_tokens"), "prompt_tokens")
        o = token_count(usage.get("completion_tokens"), "completion_tokens")
        c = token_count(prompt_details.get("cached_tokens"), "cached_tokens")
        # DeepSeek-compatible replies may expose cache accounting at top level.
        # Validate both forms rather than silently overwriting conflicting counts.
        if "prompt_cache_hit_tokens" in usage:
            hits = token_count(usage["prompt_cache_hit_tokens"], "prompt_cache_hit_tokens")
            if "cached_tokens" in prompt_details and c != hits:
                raise ValueError("conflicting provider cached token counts")
            c = hits
        if "prompt_cache_miss_tokens" in usage:
            misses = token_count(usage["prompt_cache_miss_tokens"], "prompt_cache_miss_tokens")
            if misses + c != i:
                raise ValueError("provider cache hits and misses must sum to prompt tokens")
        # Reasoning is billed inside the completion count, so the cost is already right, but
        # it is worth its own column: on a reasoning model most of a planning call can be
        # thought rather than answer, and the source arm makes an extra planning call the
        # control arm does not. Absent from providers that do not report it.
        r = token_count(completion_details.get("reasoning_tokens"), "reasoning_tokens")
        if c > i or r > o:
            raise ValueError("provider usage detail exceeds its containing token count")
        call_cost = self.cost_of(i, o, c)
        with self._lock:
            next_calls = self.calls + 1
            next_total_in = self.total_in + i
            next_total_out = self.total_out + o
            next_total_cached = self.total_cached + c
            next_batch_in = self.batch_in + i
            next_batch_out = self.batch_out + o
            next_batch_cached = self.batch_cached + c
            next_run_in = self.run_in.get(run_id, 0) + i
            next_batch_cost = self.cost_of(
                next_batch_in, next_batch_out, next_batch_cached)
            rec = {"t": time.time(), "provider": provider, "model": model,
                   "batch_id": self.batch_id, "logical_run_id": run_id,
                   "accounting_status": "reported",
                   "in": i, "out": o, "cached": c, "reasoning": r,
                   "cum_in": next_total_in, "cum_out": next_total_out,
                   "call_cost_cny": round(call_cost, 6),
                   "batch_cost_cny": round(next_batch_cost, 4),
                   "run_in_tokens": next_run_in, "calls": next_calls}
            if self.allowance is not None:
                rec["allowance_key"] = self.allowance_key
            if request_id:
                rec["provider_request_id"] = request_id
            if selected_reported_model:
                rec["provider_reported_model"] = selected_reported_model
            self._append_record(rec, batch_recount=True)
            next_batch_cost = rec["batch_cost_cny"]
            self.calls = next_calls
            self.total_in = next_total_in
            self.total_out = next_total_out
            self.total_cached = next_total_cached
            self.total_reasoning += r
            self.batch_in = next_batch_in
            self.batch_out = next_batch_out
            self.batch_cached = next_batch_cached
            self.run_in[run_id] = next_run_in
            if self.allowance is not None:
                self.allowance.add(self.allowance_key, call_cost)
            if self.global_cap is not None:
                self.global_cap.record_spend(call_cost, now=rec["t"])
            self._commit_run_record(
                run_id=run_id, rec=rec, reported_model=selected_reported_model,
                request_id=request_id, unmetered=False, known_cost=call_cost)
        if next_run_in > self.run_input_token_abort:
            raise BudgetExhausted("token_budget",
                                  f"run {run_id} consumed {next_run_in} input tokens, "
                                  f"over the {self.run_input_token_abort} ceiling")
        if self.batch_cost_stop is not None and next_batch_cost > self.batch_cost_stop:
            raise BudgetExhausted("batch_cost_stop",
                                  f"batch spend {next_batch_cost:.4f} passed the "
                                  f"{self.batch_cost_stop:.4f} stop line")
        return rec

    def record_unmetered(self, *, provider: str, model: str, run_id: str,
                         request_id: str, error_code: str,
                         reported_model: str | None = None) -> dict:
        """Record a dispatched call whose response supplied no trustworthy usage.

        A timeout or broken response can still be billed. Recording zero tokens or omitting
        the call would understate both traffic and uncertainty, so this row uses explicit null
        usage and cost fields for later provider-side reconciliation.
        """
        if not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise ValueError("provider request_id must be a 32-character lowercase hex value")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code):
            raise ValueError("provider error_code must be a bounded lowercase identifier")
        selected_reported_model = self._reported_model(reported_model)
        with self._lock:
            next_calls = self.calls + 1
            rec = {
                "t": time.time(), "provider": provider, "model": model,
                "batch_id": self.batch_id, "logical_run_id": run_id,
                "provider_request_id": request_id,
                "accounting_status": "usage_unavailable",
                "error_code": error_code,
                "in": None, "out": None, "cached": None, "reasoning": None,
                "call_cost_cny": None,
                "batch_cost_cny": round(self.batch_cost(), 4),
                "run_in_tokens": self.run_in.get(run_id, 0), "calls": next_calls,
            }
            if selected_reported_model:
                rec["provider_reported_model"] = selected_reported_model
            # A dispatched call with no usage adds no tokens, but its batch column must still
            # report the batch's spend rather than this process's share of it.
            self._append_record(rec, batch_recount=True)
            self.calls = next_calls
            self.unmetered_calls += 1
            self._commit_run_record(
                run_id=run_id, rec=rec, reported_model=selected_reported_model,
                request_id=request_id, unmetered=True)
        return rec

    def _batch_totals_from_ledger(self, fh) -> tuple[int, int, int] | None:
        """Sum this batch's reported tokens from the ledger. The caller holds the lock.

        Returns ``None`` when the batch has no rows yet, which a fresh ledger and a first
        call look alike, and lets the caller keep its own totals.
        """
        try:
            fh.seek(0)
            rows = fh.readlines()
        except OSError:
            return None
        totals, seen = [0, 0, 0], False
        for line in rows:
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if (not isinstance(row, dict) or row.get("batch_id") != self.batch_id
                    or row.get("accounting_status") != "reported"):
                continue
            seen = True
            for index, key in enumerate(("in", "out", "cached")):
                value = row.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    totals[index] += value
        return (totals[0], totals[1], totals[2]) if seen else None

    def _append_record(self, rec: dict, *, batch_recount: bool = False) -> dict:
        """Append one record, optionally recomputing its batch column under the lock.

        A batch sharded across processes accounts to one ledger, so process-local batch
        totals let every shard spend the stop line in full. With a stop line configured the
        batch column is recomputed from the ledger inside the same exclusive lock that
        serialises the write, which makes the figure the stop is checked against the batch's
        real spend rather than this process's share of it.
        """
        if not batch_recount or self.batch_cost_stop is None:
            self._locked_write(rec)
            return rec
        with open(self.ledger, "a+", encoding="utf-8") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass
            try:
                totals = self._batch_totals_from_ledger(fh)
                if totals is not None:
                    def contribution(key: str) -> int:
                        value = rec.get(key)
                        return value if isinstance(value, int) and not isinstance(value, bool) else 0

                    rec["batch_cost_cny"] = round(self.cost_of(
                        totals[0] + contribution("in"),
                        totals[1] + contribution("out"),
                        totals[2] + contribution("cached")), 4)
                fh.seek(0, os.SEEK_END)
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        return rec

    def _locked_write(self, rec: dict) -> None:
        with open(self.ledger, "a", encoding="utf-8") as fh:
            # The lock is between processes, not threads: sharding a batch across isolated
            # environments runs one harness process per worker and they all account to the one
            # ledger. A torn line there is a cost figure nobody can reconstruct.
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

    def report(self) -> dict:
        return {"provider_calls": self.calls,
                "unmetered_provider_calls": self.unmetered_calls,
                "input_tokens": self.total_in,
                "output_tokens": self.total_out, "cached_tokens": self.total_cached,
                "reasoning_tokens": self.total_reasoning,
                "cost_cny": round(self.cost_of(self.total_in, self.total_out, self.total_cached), 4),
                # Home-relative, so a run record can be read and compared without carrying
                # whose machine produced it. The file used is unchanged; only its displayed
                # name is normalized.
                "ledger": _display_path(self.ledger), "batch_id": self.batch_id}

    def run_report(self, run_id: str) -> dict:
        """Return a content-free accounting slice for one logical run.

        Request IDs and spend rows remain private. Their ordered digests bind a trial record
        to the exact provider calls and ledger rows without exposing correlation IDs.
        """
        with self._lock:
            summary = self._run_summaries.get(run_id, self._empty_run_summary())
            request_ids = list(summary["request_ids"])
            record_digests = list(summary["record_digests"])
            calls = summary["model_calls"]
            request_digest = None
            if calls and len(request_ids) == calls:
                request_digest = "sha256:" + hashlib.sha256(json.dumps(
                    request_ids, separators=(",", ":")).encode("utf-8")).hexdigest()
            slice_digest = None
            if record_digests:
                slice_digest = "sha256:" + hashlib.sha256(json.dumps(
                    record_digests, separators=(",", ":")).encode("utf-8")).hexdigest()
            known_cost = round(summary["known_cost_cny"], 6)
            unavailable = summary["usage_unavailable_calls"]
            return {
                "model_calls": calls,
                "usage_unavailable_calls": unavailable,
                "provider_reported_models": sorted(summary["reported_models"]),
                "provider_request_ids_digest": request_digest,
                "known_input_tokens": summary["known_input_tokens"],
                "known_output_tokens": summary["known_output_tokens"],
                "known_cached_tokens": summary["known_cached_tokens"],
                "known_reasoning_tokens": summary["known_reasoning_tokens"],
                "known_cost_cny": known_cost,
                "total_cost_cny": None if unavailable else known_cost,
                "spend_ledger_slice_digest": slice_digest,
            }
