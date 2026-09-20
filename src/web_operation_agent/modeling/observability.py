"""Content-free operational telemetry with an optional OTLP/HTTP JSON exporter."""
from __future__ import annotations

import hashlib
import json
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .chat import require_chat_port


_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_BROWSER_TOOLS = frozenset({"observe", "open_route", "click", "fill", "select",
                            "submit", "back", "open_source_route"})
_TOOL_END = frozenset({"action_receipt", "business_tool_result",
                       "business_tool_rejected", "business_tool_permission_denied"})


def _attribute(key: str, value: str | int | float | bool) -> dict:
    if isinstance(value, bool):
        encoded = {"boolValue": value}
    elif isinstance(value, int):
        encoded = {"intValue": str(value)}
    elif isinstance(value, float):
        encoded = {"doubleValue": value}
    else:
        encoded = {"stringValue": str(value)}
    return {"key": key, "value": encoded}


@dataclass
class _Run:
    started_ns: int
    spans: list[dict] = field(default_factory=list)
    model_started_ns: int | None = None
    tools: dict[int, tuple[int, str]] = field(default_factory=dict)
    answer_started_ns: int | None = None
    model_wait_ms: float = 0.0
    model_calls: int = 0
    alerts: set[str] = field(default_factory=set)


class RuntimeObservability:
    """Translate safe ledger state into OTLP without retaining business content.

    Export is best effort and asynchronous. A collector outage updates the internal health
    projection but can never fail a user task or a business write.
    """

    def __init__(self, *, service_name: str = "web-operation-agent",
                 otlp_endpoint: str = "", alert_webhook: str = "",
                 cost_alert_cny: float | None = None, timeout_s: float = 2.0):
        if not _LABEL.fullmatch(service_name):
            raise ValueError("telemetry service_name is invalid")
        for name, value in (("otlp_endpoint", otlp_endpoint),
                            ("alert_webhook", alert_webhook)):
            if not isinstance(value, str) or len(value) > 2_000:
                raise ValueError(f"{name} is invalid")
        if cost_alert_cny is not None and (
                isinstance(cost_alert_cny, bool) or not isinstance(cost_alert_cny, (int, float))
                or not 0 < cost_alert_cny <= 1_000_000):
            raise ValueError("cost_alert_cny must be a positive number")
        self.service_name = service_name
        self.otlp_endpoint = otlp_endpoint.rstrip("/")
        self.alert_webhook = alert_webhook
        self.cost_alert_cny = float(cost_alert_cny) if cost_alert_cny is not None else None
        self.timeout_s = float(timeout_s)
        self._runs: dict[str, _Run] = {}
        self._lock = threading.RLock()
        self._queue: queue.Queue[tuple[str, dict] | None] = queue.Queue(maxsize=256)
        self._counters = {
            "turns": 0, "failed_turns": 0, "provider_failures": 0,
            "provider_deadlines": 0, "indeterminate_writes": 0,
            "stale_runs_reaped": 0, "maintenance_failures": 0,
            "exports_failed": 0, "alerts_failed": 0,
        }
        self._provider_failure_streak = 0
        self._provider_deadline_streak = 0
        self._worker = threading.Thread(
            target=self._send_loop, name="woa-otel-export", daemon=True)
        self._worker.start()

    @staticmethod
    def _ns(event: dict) -> int:
        value = event.get("t")
        return int(float(value) * 1_000_000_000) if isinstance(value, (int, float)) \
            else time.time_ns()

    @staticmethod
    def _ids(run_id: str, name: str, index: int = 0) -> tuple[str, str]:
        trace = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]
        span = hashlib.sha256(f"{run_id}\0{name}\0{index}".encode("utf-8")).hexdigest()[:16]
        return trace, span

    def _span(self, run_id: str, name: str, started_ns: int, ended_ns: int,
              *, outcome: str = "ok", index: int = 0, attributes=()) -> dict:
        trace, span = self._ids(run_id, name, index)
        return {
            "traceId": trace, "spanId": span, "name": name, "kind": 1,
            "startTimeUnixNano": str(max(0, started_ns)),
            "endTimeUnixNano": str(max(started_ns, ended_ns)),
            "attributes": list(attributes),
            "status": {"code": 1 if outcome == "ok" else 2},
        }

    def record(self, event: dict) -> None:
        """Consume one already-redacted SessionLedger record."""
        if not isinstance(event, dict):
            return
        run_id = event.get("run_id")
        kind = event.get("event")
        if not isinstance(run_id, str) or not _LABEL.fullmatch(run_id or "") \
                or not isinstance(kind, str):
            return
        now = self._ns(event)
        alert = ""
        export: tuple[dict, dict] | None = None
        with self._lock:
            run = self._runs.setdefault(run_id, _Run(now))
            if kind == "provider_egress":
                run.model_started_ns = now
            elif kind in {"provider_failed", "provider_cancelled", "provider_deadline"}:
                if run.model_started_ns is not None:
                    run.spans.append(self._span(
                        run_id, "woa.model", run.model_started_ns, now,
                        outcome=kind, index=len(run.spans),
                        attributes=(_attribute("woa.outcome", kind),)))
                    run.model_started_ns = None
                if kind == "provider_failed":
                    self._counters["provider_failures"] += 1
                    self._provider_failure_streak += 1
                    if self._provider_failure_streak == 3:
                        alert = "provider_sustained_failure"
                elif kind == "provider_deadline":
                    self._counters["provider_deadlines"] += 1
                    self._provider_deadline_streak += 1
                    if self._provider_deadline_streak == 3:
                        alert = "provider_deadline_sustained"
            elif kind == "tool_call":
                if run.model_started_ns is not None:
                    run.spans.append(self._span(
                        run_id, "woa.model", run.model_started_ns, now,
                        index=len(run.spans)))
                    run.model_started_ns = None
                step = event.get("step")
                tool = event.get("tool")
                if isinstance(step, int) and isinstance(tool, str) and _LABEL.fullmatch(tool):
                    run.tools[step] = (now, tool)
            elif kind in _TOOL_END:
                step = event.get("step")
                started = run.tools.pop(step, None) if isinstance(step, int) else None
                if started is not None:
                    at, tool = started
                    name = "woa.browser" if tool in _BROWSER_TOOLS else "woa.tool"
                    run.spans.append(self._span(
                        run_id, name, at, now, outcome=("ok" if kind in {
                            "action_receipt", "business_tool_result"} else kind),
                        index=len(run.spans),
                        attributes=(_attribute("woa.tool", tool),
                                    _attribute("woa.outcome", kind))))
            elif kind == "answer_computed":
                run.answer_started_ns = now
            elif kind in {"answer_verified", "answer_contract_refused"}:
                if run.answer_started_ns is not None:
                    run.spans.append(self._span(
                        run_id, "woa.answer.verify", run.answer_started_ns, now,
                        outcome=("ok" if kind == "answer_verified" else kind),
                        index=len(run.spans)))
                    run.answer_started_ns = None
            elif ((kind == "write_outcome" and event.get("state") == "unknown")
                  or kind == "business_mutation_indeterminate"
                  or (kind == "product_turn_finished"
                      and event.get("terminal_reason") == "indeterminate_write")):
                if "indeterminate_write" not in run.alerts:
                    run.alerts.add("indeterminate_write")
                    self._counters["indeterminate_writes"] += 1
                    alert = "indeterminate_write"
            if kind == "product_turn_finished":
                outcome = str(event.get("status") or "unknown")[:64]
                if run.model_started_ns is not None:
                    run.spans.append(self._span(
                        run_id, "woa.model", run.model_started_ns, now,
                        outcome="incomplete", index=len(run.spans)))
                run.spans.append(self._span(
                    run_id, "woa.turn", run.started_ns, now,
                    outcome="ok" if outcome in {"finished", "needs_input", "refused"}
                    else outcome,
                    index=len(run.spans),
                    attributes=(_attribute("woa.status", outcome),)))
                self._counters["turns"] += 1
                if outcome not in {"finished", "needs_input", "refused"}:
                    self._counters["failed_turns"] += 1
                export = self._payloads(run_id, run, now, outcome)
                self._runs.pop(run_id, None)
        if alert:
            self.alert(alert)
        if export is not None and self.otlp_endpoint:
            self._enqueue("traces", export[0])
            self._enqueue("metrics", export[1])

    def model_wait(self, run_id: str, duration_s: float) -> None:
        if not isinstance(run_id, str) or not _LABEL.fullmatch(run_id):
            return
        with self._lock:
            run = self._runs.setdefault(run_id, _Run(time.time_ns()))
            run.model_wait_ms += max(0.0, float(duration_s) * 1000)

    def model_call(self, run_id: str, duration_s: float, outcome: str,
                   known_cost_cny: float | None = None) -> None:
        if not isinstance(run_id, str) or not _LABEL.fullmatch(run_id):
            return
        with self._lock:
            run = self._runs.setdefault(run_id, _Run(time.time_ns()))
            run.model_calls += 1
            if outcome == "success":
                self._provider_failure_streak = 0
                self._provider_deadline_streak = 0
        if (known_cost_cny is not None and self.cost_alert_cny is not None
                and known_cost_cny >= self.cost_alert_cny):
            self.alert("cost_threshold_exceeded")

    def _payloads(self, run_id: str, run: _Run, now: int,
                  outcome: str) -> tuple[dict, dict]:
        resource = {"attributes": [_attribute("service.name", self.service_name)]}
        traces = {"resourceSpans": [{"resource": resource, "scopeSpans": [{
            "scope": {"name": "web_operation_agent"}, "spans": run.spans,
        }]}]}
        points = [
            ("woa.turn.duration", max(0.0, (now - run.started_ns) / 1e9), "s"),
            ("woa.model.wait", run.model_wait_ms, "ms"),
            ("woa.model.calls", float(run.model_calls), "{call}"),
        ]
        metrics = []
        for name, value, unit in points:
            metrics.append({
                "name": name, "unit": unit,
                "gauge": {"dataPoints": [{
                    "timeUnixNano": str(now), "asDouble": value,
                    "attributes": [_attribute("woa.status", outcome)],
                }]},
            })
        return traces, {"resourceMetrics": [{"resource": resource, "scopeMetrics": [{
            "scope": {"name": "web_operation_agent"}, "metrics": metrics,
        }]}]}

    def _enqueue(self, kind: str, payload: dict) -> None:
        try:
            self._queue.put_nowait((kind, payload))
        except queue.Full:
            with self._lock:
                self._counters["exports_failed"] += 1

    def alert(self, kind: str) -> None:
        if not _LABEL.fullmatch(kind or "") or not self.alert_webhook:
            return
        self._enqueue("alert", {
            "schema_version": 1, "service": self.service_name,
            "kind": kind, "observed_at_ms": int(time.time() * 1000),
        })

    def maintenance_failed(self) -> None:
        with self._lock:
            self._counters["maintenance_failures"] += 1
        self.alert("retention_maintenance_failure")

    def reap_stale_runs(self, *, max_age_s: int = 900,
                        now_ns: int | None = None) -> int:
        """Close telemetry-only runs whose product terminal event never arrived."""
        if (isinstance(max_age_s, bool) or not isinstance(max_age_s, int)
                or not 60 <= max_age_s <= 86_400):
            raise ValueError("max_age_s must be from 60 to 86400")
        now = time.time_ns() if now_ns is None else now_ns
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("now_ns must be a non-negative integer")
        with self._lock:
            stale = [run_id for run_id, run in self._runs.items()
                     if now - run.started_ns >= max_age_s * 1_000_000_000]
        for run_id in stale:
            self.record({
                "run_id": run_id,
                "event": "product_turn_finished",
                "status": "failed",
                "terminal_reason": "telemetry_terminal_missing",
                "t": now / 1_000_000_000,
            })
        with self._lock:
            self._counters["stale_runs_reaped"] += len(stale)
        return len(stale)

    def _send_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            kind, payload = item
            url = (self.alert_webhook if kind == "alert" else
                   f"{self.otlp_endpoint}/v1/{kind}")
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            try:
                request = urllib.request.Request(
                    url, data=body, method="POST",
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                    response.read(1024)
                    if not 200 <= response.status < 300:
                        raise urllib.error.URLError("telemetry endpoint refused the signal")
            except Exception:
                with self._lock:
                    key = "alerts_failed" if kind == "alert" else "exports_failed"
                    self._counters[key] += 1
            finally:
                self._queue.task_done()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "schema_version": 1,
                "otlp_configured": bool(self.otlp_endpoint),
                "alerts_configured": bool(self.alert_webhook),
                "active_runs": len(self._runs),
                "provider_failure_streak": self._provider_failure_streak,
                "provider_deadline_streak": self._provider_deadline_streak,
                **self._counters,
            }

    def flush(self, timeout_s: float = 5) -> bool:
        deadline = time.monotonic() + timeout_s
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(.01)
        return self._queue.unfinished_tasks == 0

    def close(self) -> None:
        self.flush()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            return


class ObservedChat:
    """ChatPort decorator that emits only timing, outcome, and accounted cost."""

    def __init__(self, delegate, telemetry: RuntimeObservability):
        self._delegate = require_chat_port(delegate, role="observed chat delegate")
        if not isinstance(telemetry, RuntimeObservability):
            raise TypeError("telemetry must be RuntimeObservability")
        self._telemetry = telemetry
        self.model = delegate.model
        self.meter = getattr(delegate, "meter", None)
        self.options = getattr(delegate, "options", None)
        self.egress_policy = getattr(delegate, "egress_policy", None)

    def _finish(self, run_id: str, started: float, outcome: str) -> None:
        try:
            known = None
            if self.meter is not None:
                report = self.meter.run_report(run_id)
                value = report.get("known_cost_cny")
                known = float(value) if isinstance(value, (int, float)) else None
            self._telemetry.model_call(
                run_id, time.monotonic() - started, outcome, known_cost_cny=known)
        except Exception:
            pass

    def complete(self, messages, tools, run_id, max_tokens=None, options=None,
                 timeout_s=None):
        started = time.monotonic()
        try:
            result = self._delegate.complete(
                messages, tools, run_id, max_tokens=max_tokens, options=options,
                timeout_s=timeout_s)
        except BaseException as exc:
            self._finish(run_id, started, getattr(exc, "code", type(exc).__name__))
            raise
        self._finish(run_id, started, "success")
        return result

    def complete_cancellable(self, messages, tools, run_id, *, cancelled,
                             max_tokens=None, options=None, timeout_s=None):
        started = time.monotonic()
        try:
            result = self._delegate.complete_cancellable(
                messages, tools, run_id, cancelled=cancelled, max_tokens=max_tokens,
                options=options, timeout_s=timeout_s)
        except BaseException as exc:
            self._finish(run_id, started, getattr(exc, "code", type(exc).__name__))
            raise
        self._finish(run_id, started, "success")
        return result
