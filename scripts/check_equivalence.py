#!/usr/bin/env python3
"""Deterministic scripted-model equivalence harness for Phase 48 (OPS-10).

Runs all 6 core scenario families against in-memory/synthetic HTTP, browser,
and ChatModel doubles (zero paid model calls, zero live ./local touches),
canonicalizes per-scenario captures, and verifies byte-for-byte equivalence
against a saved baseline with explicit, scoped `--allow-registered` overrides.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
from dataclasses import replace
import difflib
import importlib
import json
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Callable
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for _extra in (ROOT, ROOT / "src", ROOT / "examples" / "yudao" / "src"):
    _text = str(_extra)
    if _text not in sys.path:
        sys.path.insert(0, _text)


def _import_attr(symbol: str, *modules: str) -> Any:
    """Resolve `symbol` across flat and reorganized package layouts."""
    errors: list[str] = []
    for module_name in modules:
        try:
            mod = importlib.import_module(module_name)
        except ImportError as exc:
            errors.append(f"{module_name}: {exc}")
            continue
        if hasattr(mod, symbol):
            return getattr(mod, symbol)
    raise ImportError(f"Could not resolve {symbol!r} from {modules}: {'; '.join(errors)}")


# Canonical core imports
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.errors import (
    BusinessMutationIndeterminate,
    BusinessToolAuthenticationError,
)
from web_operation_agent.business.mutation import MemoryMutationJournal
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.execution.model_loop import ModelDrivenTurnExecutor
from web_operation_agent.execution.product import AgentTurnConfig
from web_operation_agent.goal import executor as goal_executor
from web_operation_agent.goal.conventions import load_conventions
from web_operation_agent.goal.data import DataRefused
from web_operation_agent.modeling.egress import ProviderEgressPolicy
from web_operation_agent.privacy import BASELINE_SESSION_HANDOFFS
from web_operation_agent.schemas import (
    ActionReceipt,
    ApprovalDecision,
    ElementRef,
    ObservationEnvelope,
    PageObservation,
)
from web_operation_agent.transport.panel_view import panel_limits
from web_operation_agent.transport.protocol import _remote_args_echo

# Resilient Yudao imports (supports both flat Wave 0 and reorganized Wave 1/2 layouts)
YudaoApplication = _import_attr(
    "YudaoApplication",
    "web_operation_agent_yudao",
    "web_operation_agent_yudao.application",
    "web_operation_agent_yudao.host.application",
)
SESSION_HANDOFF_KEYS = _import_attr(
    "SESSION_HANDOFF_KEYS",
    "web_operation_agent_yudao",
    "web_operation_agent_yudao.application",
    "web_operation_agent_yudao.host.application",
)
YudaoClient = _import_attr(
    "YudaoClient",
    "web_operation_agent_yudao",
    "web_operation_agent_yudao.client",
    "web_operation_agent_yudao.reads.client",
    "web_operation_agent_yudao.host.client",
)
YudaoReadRefused = _import_attr(
    "YudaoReadRefused",
    "web_operation_agent_yudao",
    "web_operation_agent_yudao.client",
    "web_operation_agent_yudao.reads.client",
)
REFERENCE_CONVENTIONS = _import_attr(
    "REFERENCE_CONVENTIONS",
    "web_operation_agent_yudao.conventions",
    "web_operation_agent_yudao.reads.conventions",
)
yudao_turn_config = _import_attr(
    "yudao_turn_config",
    "web_operation_agent_yudao.turn",
    "web_operation_agent_yudao.executor.turn",
    "web_operation_agent_yudao.host.turn",
)
TableReadTurnExecutor = _import_attr(
    "TableReadTurnExecutor",
    "web_operation_agent_yudao",
    "web_operation_agent_yudao.slim_loop",
    "web_operation_agent_yudao.executor",
    "web_operation_agent_yudao.executor.slim_loop",
    "web_operation_agent_yudao.executor.loop",
    "web_operation_agent_yudao.host",
)
ActorKnowledge = _import_attr(
    "ActorKnowledge",
    "web_operation_agent_yudao",
    "web_operation_agent_yudao.slim_loop",
    "web_operation_agent_yudao.executor",
    "web_operation_agent_yudao.executor.slim_loop",
    "web_operation_agent_yudao.reads.knowledge",
)
YudaoSubmitPort = _import_attr(
    "YudaoSubmitPort",
    "web_operation_agent_yudao",
    "web_operation_agent_yudao.native_submit",
    "web_operation_agent_yudao.writes.native_submit",
    "web_operation_agent_yudao.writes",
)
YUDAO_SUBMIT_CONVENTIONS = _import_attr(
    "YUDAO_SUBMIT_CONVENTIONS",
    "web_operation_agent_yudao.native_submit",
    "web_operation_agent_yudao.writes.native_submit",
)
YudaoMutationPrefill = _import_attr(
    "YudaoMutationPrefill",
    "web_operation_agent_yudao.mutation_prefill",
    "web_operation_agent_yudao.writes.mutation_prefill",
)
LiveFormPort = _import_attr(
    "LiveFormPort",
    "web_operation_agent_yudao.live_forms",
    "web_operation_agent_yudao.writes.live_forms",
)
FenceEvidence = _import_attr(
    "FenceEvidence",
    "web_operation_agent_yudao.live_forms",
    "web_operation_agent_yudao.writes.live_forms",
)
NativePageGuard = _import_attr(
    "NativePageGuard",
    "web_operation_agent_yudao.page_guard",
    "web_operation_agent_yudao.host.page_guard",
)
NativeGuardProxy = _import_attr(
    "NativeGuardProxy",
    "web_operation_agent_yudao.guard_proxy",
    "web_operation_agent_yudao.host.guard_proxy",
)
GUARD_PATH = _import_attr(
    "GUARD_PATH",
    "web_operation_agent_yudao.guard_proxy",
    "web_operation_agent_yudao.host.guard_proxy",
)
ModelPageGoalPort = _import_attr(
    "ModelPageGoalPort",
    "web_operation_agent_yudao.model_page_policy",
    "web_operation_agent_yudao.executor.model_page_policy",
)
NativePageReview = _import_attr(
    "NativePageReview",
    "web_operation_agent_yudao.model_page_policy",
    "web_operation_agent_yudao.executor.model_page_policy",
)
ReadOnlyWindowPageActions = _import_attr(
    "ReadOnlyWindowPageActions",
    "web_operation_agent_yudao.read_only_window_actions",
    "web_operation_agent_yudao.executor.read_only_window_actions",
    "web_operation_agent_yudao.reads.read_only_window_actions",
)
ReadOnlyWindowPageReview = _import_attr(
    "ReadOnlyWindowPageReview",
    "web_operation_agent_yudao.read_only_window_actions",
    "web_operation_agent_yudao.executor.read_only_window_actions",
    "web_operation_agent_yudao.reads.read_only_window_actions",
)
ReadOnlyWindowDraftSteps = _import_attr(
    "ReadOnlyWindowDraftSteps",
    "web_operation_agent_yudao.read_only_window_actions",
    "web_operation_agent_yudao.executor.read_only_window_actions",
    "web_operation_agent_yudao.reads.read_only_window_actions",
)
TurnTables = _import_attr(
    "TurnTables",
    "web_operation_agent_yudao.turn_tables",
    "web_operation_agent_yudao.reads.turn_tables",
    "web_operation_agent_yudao.executor.turn_tables",
)
BACKEND_WRITE_ATOMICITY = _import_attr(
    "BACKEND_WRITE_ATOMICITY",
    "web_operation_agent_yudao.write_definitions",
    "web_operation_agent_yudao.writes.write_definitions",
)

# Contacts second-application imports
from testapps.contacts.adapter import MinimalContactsAdapter
from testapps.contacts.app import (
    DEMO_ACTORS,
    DEMO_SESSION_COOKIE,
    MinimalContactsApplication,
)
from testapps.contacts.model_loop import ContactsModelGoalPort, contacts_profile


ORIGIN = "https://synthetic.test"
TODAY = "2026-10-13"
REGISTERED_ALLOWANCES = frozenset({
    "ops-03", "cln-08", "arc-08", "arc-09", "arc-12", "approval-refusal",
})
_CLN08_POLICY_MODULE_RE = re.compile(
    r"^web_operation_agent_yudao\.(?:host|executor|reads|writes|rules|goal_compat)\.([A-Za-z0-9_]+)$"
)


# ---------------------------------------------------------------------------
# Canonicalization & Comparison
# ---------------------------------------------------------------------------

_STRIP_TIMESTAMP_KEYS = frozenset({
    "t", "at", "timestamp", "created_at", "updated_at", "started_at", "finished_at",
    "decided_at", "expires_at", "issued_at", "observed_at",
    "elapsed_s", "elapsed_ms", "wall_s", "wall_seconds", "duration_s", "duration_ms", "seconds",
    "remaining_s", "waited_ms", "model_occupied_seconds", "occupied_seconds",
})

_EPHEMERAL_ID_KEYS = frozenset({
    "session_id", "run_id", "approval_id", "message_id", "turn_id", "operation_id",
    "permit", "permit_id", "command_id", "action_id", "observation_id",
    "principal_digest", "operation_context_digest", "operation_digest",
    "delegation_digest", "session_digest", "turn_digest",
    "proposal_digest", "control_plan_digest", "binding_digest",
})

_EPHEMERAL_STR_RE = re.compile(
    r"ev_[0-9a-f]{12}_\d+"
    r"|approval-[0-9a-f]{32}"
    r"|op-[0-9a-f]{32}"
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_ISO_WALL_CLOCK_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)
_LOCALHOST_PORT_RE = re.compile(r"http://127\.0\.0\.1:\d+")

_ARC08_EVENT_NAMES = frozenset({
    "turn_timing", "step_timing", "stage_timing", "first_route", "first_route_kind",
    "turn_stop_reason", "stop_reason", "turn_trace_summary", "route_timing",
})
_ARC08_KEYS = frozenset({
    "first_route_kind", "stop_reason", "terminal_stop_reason", "timing", "timings",
    "step_timings", "stage_timings", "phase_timings", "route_timing", "first_route",
})


class _ScenarioCanonicalizer:
    """Normalizes ephemeral IDs and strips wall-clock timestamps within one scenario."""

    def __init__(self) -> None:
        self._ids: dict[str, str] = {}

    def placeholder(self, raw: str) -> str:
        if raw not in self._ids:
            self._ids[raw] = f"id_{len(self._ids) + 1}"
        return self._ids[raw]

    def normalize_string(self, text: str) -> str:
        text = _ISO_WALL_CLOCK_RE.sub("<TIMESTAMP>", text)
        text = _LOCALHOST_PORT_RE.sub("http://127.0.0.1:<port>", text)
        return _EPHEMERAL_STR_RE.sub(lambda m: self.placeholder(m.group(0)), text)

    def normalize(self, value: Any, *, key: str = "", parent_key: str = "", event: str = "") -> Any:
        if isinstance(value, dict):
            ev = value.get("event", event) if isinstance(value.get("event"), str) else event
            out: dict[str, Any] = {}
            for k, v in value.items():
                if k in _STRIP_TIMESTAMP_KEYS:
                    continue
                if k in _EPHEMERAL_ID_KEYS and isinstance(v, str) and v:
                    out[k] = self.placeholder(v)
                elif k == "content_digest" and parent_key in {"control_plan", "proposal"} and isinstance(v, str) and v:
                    out[k] = self.placeholder(v)
                elif k == "digest" and parent_key in {"control_plan", "proposal"} and isinstance(v, str) and v:
                    out[k] = self.placeholder(v)
                elif k == "input_digest" and ev == "provider_egress" and isinstance(v, str) and v:
                    out[k] = self.placeholder(v)
                elif k == "context_digest" and ev == "conversation_state_updated" and isinstance(v, str) and v:
                    out[k] = self.placeholder(v)
                elif k == "digest" and ev in {"observation", "read_answer_selected"} and isinstance(v, str) and v:
                    # Digest includes URL/origin (which may carry ephemeral localhost port)
                    out[k] = self.placeholder(v)
                elif k == "state_sha256" and isinstance(v, str) and v:
                    out[k] = self.placeholder(v)
                elif k == "stage_timings" and isinstance(v, dict):
                    out[k] = {stage: "<DURATION>" for stage in sorted(v)}
                else:
                    out[k] = self.normalize(v, key=k, parent_key=k, event=ev)
            return out
        if isinstance(value, list):
            return [self.normalize(item, key=key, parent_key=parent_key, event=event) for item in value]
        if isinstance(value, tuple):
            return [self.normalize(item, key=key, parent_key=parent_key, event=event) for item in value]
        if isinstance(value, str):
            return self.normalize_string(value)
        return value


def canonicalize_scenario(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic canonical copy of one scenario capture."""
    canon = _ScenarioCanonicalizer()
    return canon.normalize(raw)


def canonicalize_capture(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    """Canonicalize each scenario independently and wrap in top-level capture envelope."""
    return {
        "schema_version": 1,
        "scenarios": [canonicalize_scenario(item) for item in scenarios],
    }


def parse_allow_registered(raw: str) -> set[str]:
    if not raw.strip():
        return set()
    items = {item.strip() for item in raw.split(",") if item.strip()}
    unknown = items - REGISTERED_ALLOWANCES
    if unknown:
        raise ValueError(f"Unknown --allow-registered flag(s): {sorted(unknown)}")
    return items


def apply_registered_allowances(capture: dict[str, Any], allow: set[str]) -> dict[str, Any]:
    """Project a canonicalized capture with explicit, strictly scoped Wave 1/2 allowances."""
    if not allow:
        return capture
    projected = copy.deepcopy(capture)
    arc09_batch_scenarios = {"yudao_table_read_multi_batch", "yudao_table_read_multi_early_exit"}
    for scenario in projected.get("scenarios", []):
        scen_name = scenario.get("scenario", "")
        audit = scenario.get("audit_records")
        if isinstance(audit, list):
            filtered_audit = []
            for rec in audit:
                if not isinstance(rec, dict):
                    filtered_audit.append(rec)
                    continue
                ev = rec.get("event", "")
                if "arc-12" in allow and ev == "check_fired":
                    continue
                if "approval-refusal" in allow and ev in {"approval_click_refused", "approval_gate_refusals"}:
                    continue
                if "arc-08" in allow and (ev in _ARC08_EVENT_NAMES or ev.endswith("_timing")):
                    continue
                if "ops-03" in allow and ev == "business_mutation_indeterminate":
                    rec = {k: v for k, v in rec.items() if k != "proposal_digest"}
                if "cln-08" in allow and ev == "action_effect_consumed":
                    pm = rec.get("policy_module")
                    if isinstance(pm, str):
                        match = _CLN08_POLICY_MODULE_RE.match(pm)
                        if match:
                            rec = {**rec, "policy_module": f"web_operation_agent_yudao.{match.group(1)}"}
                if "arc-09" in allow and scen_name in arc09_batch_scenarios and ev == "goal_run_receipt":
                    stages = rec.get("stages")
                    if isinstance(stages, dict):
                        new_stages = dict(stages)
                        for st_name in ("business", "ui"):
                            st_val = new_stages.get(st_name)
                            if isinstance(st_val, dict) and "operations" in st_val:
                                new_stages[st_name] = {**st_val, "operations": "<BATCHED_OPS>"}
                        rec = {**rec, "stages": new_stages}
                if "arc-08" in allow:
                    rec = {k: v for k, v in rec.items() if k not in _ARC08_KEYS}
                if "approval-refusal" in allow and "gate_refusals" in rec:
                    rec = {k: v for k, v in rec.items() if k != "gate_refusals"}
                filtered_audit.append(rec)
            if "arc-09" in allow:
                idx = 0
                while idx < len(filtered_audit):
                    rec = filtered_audit[idx]
                    if (isinstance(rec, dict) and rec.get("event") == "tool_call"
                            and rec.get("tool") == "observe"):
                        end = idx + 1
                        while (end < len(filtered_audit)
                               and isinstance(filtered_audit[end], dict)
                               and filtered_audit[end].get("event") == "write_outcome"):
                            end += 1
                        if (end + 1 < len(filtered_audit)
                                and isinstance(filtered_audit[end], dict)
                                and filtered_audit[end].get("event") == "action_receipt"
                                and filtered_audit[end].get("tool") == "observe"
                                and filtered_audit[end].get("step") == rec.get("step")
                                and isinstance(filtered_audit[end + 1], dict)
                                and filtered_audit[end + 1].get("event") == "yudao_api_read"):
                            api_rec = filtered_audit[end + 1]
                            observe_block = filtered_audit[idx : end + 1]
                            filtered_audit[idx : end + 2] = [api_rec, *observe_block]
                            idx = end + 2
                            continue
                    idx += 1
            if "arc-08" in allow or "arc-09" in allow or "arc-12" in allow or "approval-refusal" in allow:
                for idx, rec in enumerate(filtered_audit, 1):
                    if isinstance(rec, dict) and "seq" in rec:
                        rec["seq"] = idx
            scenario["audit_records"] = filtered_audit

        if "arc-09" in allow and scenario.get("early_exit_multi_read"):
            reads = scenario.get("backend_http_reads")
            if isinstance(reads, list):
                scenario["backend_http_reads"] = sorted(
                    reads, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True)
                )
    return projected


def compare_captures(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    allow: set[str] | None = None,
) -> tuple[bool, str]:
    """Compare canonicalized captures byte-for-byte; return (ok, unified_diff)."""
    allowed = allow or set()
    left = apply_registered_allowances(baseline, allowed)
    right = apply_registered_allowances(current, allowed)
    left_text = json.dumps(left, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    right_text = json.dumps(right, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if left_text == right_text:
        return True, ""
    diff = "".join(
        difflib.unified_diff(
            left_text.splitlines(keepends=True),
            right_text.splitlines(keepends=True),
            fromfile="baseline.json",
            tofile="current.json",
        )
    )
    return False, diff


# ---------------------------------------------------------------------------
# Deterministic In-Memory Test Doubles
# ---------------------------------------------------------------------------

def _tool_reply(*calls: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"call_{idx}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
            for idx, (name, args) in enumerate(calls, 1)
        ],
    }


def _extract_receipt(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "tool":
            matches = re.findall(r"ev_[0-9a-f]{12}_\d+", str(msg.get("content") or ""))
            if matches:
                return matches[-1]
    raise AssertionError("No ev_* receipt found in prior tool messages")


def _extract_all_receipts(messages: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for msg in messages:
        if msg.get("role") == "tool":
            for m in re.findall(r"ev_[0-9a-f]{12}_\d+", str(msg.get("content") or "")):
                if m not in found:
                    found.append(m)
    return found


class _ScriptedChat:
    """Zero-cost deterministic ChatModel double capturing exact messages and tools."""

    model = "scripted/no-model"
    provider = "synthetic"

    def __init__(self, steps: list[Any], *, egress_policy: ProviderEgressPolicy):
        self._steps = list(steps)
        self.egress_policy = egress_policy
        self.recorded_calls: list[dict[str, Any]] = []

    def complete(self, messages, tools, _run_id="", **_kwargs):
        self.recorded_calls.append({
            "messages": copy.deepcopy(list(messages)),
            "tools": copy.deepcopy(list(tools)) if tools is not None else None,
        })
        if not self._steps:
            raise AssertionError("ScriptedChat exhausted all replies")
        step = self._steps.pop(0)
        if callable(step):
            return step(messages, tools)
        return copy.deepcopy(step)

    def complete_cancellable(self, messages, tools, run_id="", *, cancelled=None, **kwargs):
        return self.complete(messages, tools, run_id, **kwargs)


class _RecordingChannel:
    """In-memory RemoteSessionChannel double capturing ApprovalRequest payloads."""

    def __init__(
        self,
        *,
        initial_obs: PageObservation,
        principal,
        decision: str = "approved",
        on_approval: Callable[[Any], None] | None = None,
        session_id: str = "equiv-session",
    ) -> None:
        self.session_id = session_id
        self.new_conversation_pending = False
        self.principal = principal
        self.decision = decision
        self.on_approval = on_approval
        self.published: list[tuple[str, dict, str]] = []
        self.approvals: list[Any] = []
        self.approval_payloads: list[dict[str, Any]] = []
        self.read_only_mode = True
        self.latest_observation_envelope = ObservationEnvelope(
            schema_version=1,
            session_id=session_id,
            observation_id="obs-1",
            page_epoch=initial_obs.epoch,
            observation=initial_obs,
        )

    @staticmethod
    def message_cancelled(_message_id: str) -> bool:
        return False

    def take_new_conversation(self) -> bool:
        pending = self.new_conversation_pending
        self.new_conversation_pending = False
        return pending

    def publish(self, kind: str, payload: dict, *, turn_id: str = "") -> None:
        self.published.append((kind, copy.deepcopy(payload), turn_id))

    def request_approval(self, request) -> None:
        self.approvals.append(request)
        self.approval_payloads.append({
            "kind": request.kind,
            "effect": request.effect,
            "targets": list(request.targets),
            "details": [
                {"kind": d.kind, "label": d.label, "text": d.text}
                for d in request.details
            ],
            "offer_prefill": bool(request.offer_prefill),
        })

    def wait_for_approval(self, approval_id: str, *, timeout_s: float) -> ApprovalDecision:
        request = next(item for item in self.approvals if item.approval_id == approval_id)
        if self.on_approval is not None:
            self.on_approval(request)
        dec = self.decision
        return ApprovalDecision(
            schema_version=1,
            session_id=self.session_id,
            approval_id=approval_id,
            decision=dec,
            principal_digest=request.principal_digest,
            turn_id=request.turn_id,
            operation_id=request.operation_id,
        )


def _make_yudao_obs(
    epoch: int,
    *,
    url: str = ORIGIN + "/crm/contact",
    title: str = "CRM 联系人",
    page_route: str = "/crm/contact",
    elements: list[ElementRef] | None = None,
    content: str = "",
) -> PageObservation:
    if elements is None:
        elements = [
            ElementRef("e1", "button", "新增", "button", epoch, {}),
        ]
    stamped = [replace(el, epoch=epoch) for el in elements]
    if not content:
        content = "\n".join(f"[{el.ref[1:]}]<{el.tag}>{el.name} />" for el in stamped)
    return PageObservation(
        schema_version=1,
        epoch=epoch,
        url=url,
        title=title,
        landmarks=[title],
        elements=stamped,
        content=content,
        render_state="ready",
        page_route=page_route,
    )


class _YudaoBrowserDouble:
    """Deterministic in-memory RemoteBrowserClient double supporting page, draft, and submit actions."""

    def __init__(
        self,
        channel: _RecordingChannel,
        *,
        timeout_s: float = 10.0,
        turn_id: str = "",
        form_state: dict[str, Any] | None = None,
        on_submit_click: Callable[[dict[str, Any]], None] | None = None,
        open_dialog_elements: Callable[[int], list[ElementRef]] | None = None,
        customer_select_elements: Callable[[int], list[ElementRef]] | None = None,
    ) -> None:
        self.channel = channel
        self.timeout_s = timeout_s
        self.turn_id = turn_id
        self.started = False
        self.closed = False
        self.seq = 0
        self.form_state = form_state
        self.on_submit_click = on_submit_click
        self.open_dialog_elements = open_dialog_elements
        self.customer_select_elements = customer_select_elements
        self.receipts: list[dict[str, Any]] = []

    def start(self) -> dict[str, Any]:
        self.started = True
        return {"ok": True, "embedded": True}

    def close(self) -> None:
        self.closed = True

    def observe(self) -> PageObservation:
        return self.channel.latest_observation_envelope.observation

    def execute_with_budget(self, action: str, args=None, *, timeout_s: float, expect_navigation: bool = False):
        return self.execute(action, args, expect_navigation=expect_navigation)

    def execute(self, action: str, args=None, expect_navigation: bool = False) -> ActionReceipt:
        args = dict(args or {})
        self.seq += 1
        before = self.channel.latest_observation_envelope.observation
        next_epoch = before.epoch + 1
        form_data = None
        new_obs = _make_yudao_obs(
            next_epoch,
            url=before.url,
            title=before.title,
            page_route=before.page_route,
            elements=list(before.elements),
            content=before.content,
        )

        if action == "goto":
            raw_url = str(args.get("url") or "/")
            path = "/" + raw_url.removeprefix(ORIGIN).lstrip("/")
            full_url = raw_url if raw_url.startswith("http") else ORIGIN + path
            title_map = {"/crm/customer": "客户管理", "/crm/contact": "CRM 联系人", "/orders": "订单"}
            new_obs = _make_yudao_obs(
                next_epoch,
                url=full_url,
                title=title_map.get(path, "列表页"),
                page_route=path,
                elements=[ElementRef("e1", "button", "新增", "button", next_epoch, {})],
            )
        elif action in {"fill", "input"}:
            ref = args.get("ref")
            val = str(args.get("value") or args.get("text") or "")
            updated = []
            for el in before.elements:
                if el.ref == ref:
                    updated.append(replace(el, epoch=next_epoch, attrs={**el.attrs, "value": val}))
                    if self.form_state is not None:
                        prop = el.attrs.get("prop") or (
                            "mobile" if el.attrs.get("field_label") == "手机" else
                            "name" if el.attrs.get("field_label") == "联系人姓名" else
                            "post" if el.attrs.get("field_label") == "职位" else ""
                        )
                        if prop:
                            self.form_state["model"][prop] = val
                else:
                    updated.append(replace(el, epoch=next_epoch))
            new_obs = _make_yudao_obs(
                next_epoch, url=before.url, title=before.title,
                page_route=before.page_route, elements=updated,
            )
        elif action == "select":
            ref = args.get("ref")
            val = str(args.get("option_label") or args.get("value") or "")
            if self.customer_select_elements is not None:
                updated = self.customer_select_elements(next_epoch)
            else:
                updated = [
                    replace(el, epoch=next_epoch, attrs={**el.attrs, "selected_option": val, "value": val})
                    if el.ref == ref else replace(el, epoch=next_epoch)
                    for el in before.elements
                ]
            new_obs = _make_yudao_obs(
                next_epoch, url=before.url, title=before.title,
                page_route=before.page_route, elements=updated,
            )
        elif action == "click":
            if self.open_dialog_elements is not None:
                updated = self.open_dialog_elements(next_epoch)
                new_obs = _make_yudao_obs(
                    next_epoch, url=before.url, title=before.title,
                    page_route=before.page_route, elements=updated,
                )
        elif action == "form_model":
            kind = args.get("kind")
            if self.form_state is None:
                form_data = {"model": {}, "mode": "create", "record_id": "", "items": [], "withheld": [], "invalid": []}
            elif kind == "form":
                model = copy.deepcopy(self.form_state["model"])
                form_data = {
                    "model": model,
                    "mode": self.form_state["mode"],
                    "record_id": self.form_state["record_id"],
                    "source": self.form_state.get("source", ""),
                    "items": copy.deepcopy(self.form_state["items"]),
                    "check": json.dumps(model, sort_keys=True),
                    "withheld": [],
                    "invalid": [],
                }
            elif kind == "validate":
                model = copy.deepcopy(self.form_state["model"])
                form_data = {
                    "model": model,
                    "mode": self.form_state["mode"],
                    "record_id": self.form_state["record_id"],
                    "source": self.form_state.get("source", ""),
                    "items": copy.deepcopy(self.form_state["items"]),
                    "check": json.dumps(model, sort_keys=True),
                    "withheld": [],
                    "invalid": [],
                    "validation": True,
                }
            elif kind == "confirm":
                form_data = {"open": False, "text": "", "buttons": []}
        elif action == "form_submit":
            if self.on_submit_click is not None:
                self.on_submit_click(args)

        self.channel.latest_observation_envelope = ObservationEnvelope(
            schema_version=1,
            session_id=self.channel.session_id,
            observation_id=f"obs-{next_epoch}",
            page_epoch=next_epoch,
            observation=new_obs,
        )
        echo = _remote_args_echo(args)
        receipt = ActionReceipt(
            schema_version=1,
            command_id=f"cmd-{self.seq}",
            action=action,
            args_echo=echo,
            ok=True,
            url_before=before.url,
            url_after=new_obs.url,
            navigated=new_obs.url != before.url,
            epoch_before=before.epoch,
            epoch_after=new_obs.epoch,
            observation=new_obs,
            write_guard_held=True,
            form_data=form_data,
        )
        self.receipts.append({
            "command_id": receipt.command_id,
            "action": receipt.action,
            "args_echo": receipt.args_echo,
            "ok": receipt.ok,
            "url_before": receipt.url_before,
            "url_after": receipt.url_after,
            "navigated": receipt.navigated,
            "write_guard_held": receipt.write_guard_held,
        })
        return receipt


def _synthetic_yudao_docs() -> dict[str, Any]:
    return {
        "paths": {
            "/admin-api/orders/page": {
                "get": {
                    "tags": ["管理后台 - 销售订单"],
                    "summary": "获得销售订单分页",
                    "parameters": [
                        {"name": "no", "in": "query", "schema": {"type": "string"}},
                        {"name": "customerId", "in": "query", "schema": {"type": "integer"}},
                        {"name": "pageNo", "in": "query", "schema": {"type": "integer"}},
                        {"name": "pageSize", "in": "query", "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/OrdersPage"}}}}},
                }
            },
            "/admin-api/orders/get": {
                "get": {
                    "tags": ["管理后台 - 销售订单"],
                    "parameters": [{"name": "id", "in": "query", "schema": {"type": "integer"}}],
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/OrdersGet"}}}}},
                }
            },
            "/admin-api/secret/page": {
                "get": {
                    "tags": ["管理后台 - 内部机密"],
                    "summary": "受限分页",
                    "parameters": [
                        {"name": "pageNo", "in": "query", "schema": {"type": "integer"}},
                        {"name": "pageSize", "in": "query", "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/OrdersPage"}}}}},
                }
            },
            "/admin-api/crm/customer/page": {
                "get": {
                    "tags": ["管理后台 - CRM 客户"],
                    "parameters": [
                        {"name": "name", "in": "query", "schema": {"type": "string"}},
                        {"name": "pageNo", "in": "query", "schema": {"type": "integer"}},
                        {"name": "pageSize", "in": "query", "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CustomerPage"}}}}},
                }
            },
            "/admin-api/crm/customer/get": {
                "get": {
                    "tags": ["管理后台 - CRM 客户"],
                    "parameters": [{"name": "id", "in": "query", "schema": {"type": "integer"}}],
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CustomerGet"}}}}},
                }
            },
            "/admin-api/crm/customer/simple-list": {"get": {}},
            "/admin-api/crm/customer/create": {"post": {}},
            "/admin-api/crm/customer/update": {"put": {}},
            "/admin-api/crm/contact/page": {
                "get": {
                    "tags": ["管理后台 - CRM 联系人"],
                    "parameters": [
                        {"name": "name", "in": "query", "schema": {"type": "string"}},
                        {"name": "pageNo", "in": "query", "schema": {"type": "integer"}},
                        {"name": "pageSize", "in": "query", "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ContactPage"}}}}},
                }
            },
            "/admin-api/crm/contact/get": {
                "get": {
                    "tags": ["管理后台 - CRM 联系人"],
                    "parameters": [{"name": "id", "in": "query", "schema": {"type": "integer"}}],
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/ContactGet"}}}}},
                }
            },
            "/admin-api/crm/contact/create": {"post": {}},
            "/admin-api/crm/contact/update": {"put": {}},
            "/admin-api/crm/follow-up-record/page": {"get": {}},
            "/admin-api/crm/follow-up-record/create": {"post": {}},
            "/admin-api/promotion/coupon-template/page": {
                "get": {
                    "tags": ["管理后台 - 优惠券模板"],
                    "parameters": [
                        {"name": "name", "in": "query", "schema": {"type": "string"}},
                        {"name": "pageNo", "in": "query", "schema": {"type": "integer"}},
                        {"name": "pageSize", "in": "query", "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CouponPage"}}}}},
                }
            },
            "/admin-api/promotion/coupon-template/get": {
                "get": {
                    "tags": ["管理后台 - 优惠券模板"],
                    "parameters": [{"name": "id", "in": "query", "schema": {"type": "integer"}}],
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CouponGet"}}}}},
                }
            },
            "/admin-api/promotion/coupon-template/update": {
                "put": {
                    "tags": ["管理后台 - 优惠券模板"],
                    "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/CouponUpdateReq"}}}},
                    "responses": {"200": {"content": {"*/*": {"schema": {"$ref": "#/components/schemas/CommonResultBoolean"}}}}},
                }
            },
            "/admin-api/system/auth/get-permission-info": {"get": {}},
            "/admin-api/system/dict-data/simple-list": {"get": {}},
            "/admin-api/system/dept/simple-list": {"get": {}},
            "/admin-api/system/area/tree": {"get": {}},
            "/admin-api/system/menu/list": {"get": {}},
            "/admin-api/crm/permission/list": {"get": {}},
        },
        "components": {
            "schemas": {
                "CommonResultBoolean": {"type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "boolean"}}},
                "OrdersResp": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "no": {"type": "string"},
                        "customerName": {"type": "string"},
                        "amount": {"type": "integer"},
                        "status": {"type": "integer"},
                    },
                },
                "OrdersGet": {"type": "object", "properties": {"data": {"$ref": "#/components/schemas/OrdersResp"}}},
                "OrdersPageData": {"type": "object", "properties": {"list": {"type": "array", "items": {"$ref": "#/components/schemas/OrdersResp"}}}},
                "OrdersPage": {"type": "object", "properties": {"data": {"$ref": "#/components/schemas/OrdersPageData"}}},
                "CustomerResp": {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}},
                "CustomerGet": {"type": "object", "properties": {"data": {"$ref": "#/components/schemas/CustomerResp"}}},
                "CustomerPageData": {"type": "object", "properties": {"list": {"type": "array", "items": {"$ref": "#/components/schemas/CustomerResp"}}}},
                "CustomerPage": {"type": "object", "properties": {"data": {"$ref": "#/components/schemas/CustomerPageData"}}},
                "ContactResp": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string"},
                        "mobile": {"type": "string"},
                        "email": {"type": "string"},
                        "remark": {"type": "string"},
                        "customerId": {"type": "integer"},
                        "ownerUserId": {"type": "integer"},
                    },
                },
                "ContactGet": {"type": "object", "properties": {"data": {"$ref": "#/components/schemas/ContactResp"}}},
                "ContactPageData": {"type": "object", "properties": {"list": {"type": "array", "items": {"$ref": "#/components/schemas/ContactResp"}}}},
                "ContactPage": {"type": "object", "properties": {"data": {"$ref": "#/components/schemas/ContactPageData"}}},
                "CouponUpdateReq": {
                    "type": "object",
                    "required": ["id", "name", "usePrice"],
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string", "description": "优惠券名称"},
                        "usePrice": {"type": "integer", "description": "满多少金额可用"},
                        "discountPrice": {"type": "integer", "description": "优惠金额"},
                        "description": {"type": "string", "description": "使用说明"},
                        "status": {"type": "integer", "description": "状态"},
                    },
                },
                "CouponResp": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string"},
                        "usePrice": {"type": "integer"},
                        "discountPrice": {"type": "integer"},
                        "description": {"type": "string"},
                        "status": {"type": "integer"},
                    },
                },
                "CouponGet": {"type": "object", "properties": {"data": {"$ref": "#/components/schemas/CouponResp"}}},
                "CouponPageData": {"type": "object", "properties": {"list": {"type": "array", "items": {"$ref": "#/components/schemas/CouponResp"}}}},
                "CouponPage": {"type": "object", "properties": {"data": {"$ref": "#/components/schemas/CouponPageData"}}},
            }
        },
    }


class _SyntheticYudaoClient(YudaoClient):
    """In-memory YudaoClient double recording HTTP reads and dispatched writes."""

    def __init__(self, *, fail_readback_after_write: bool = False, auth_error_on_secret: bool = False):
        super().__init__(ORIGIN, None, token="synthetic-token", tenant="1")
        self.fail_readback_after_write = fail_readback_after_write
        self.auth_error_on_secret = auth_error_on_secret
        self._write_happened = False
        self.http_reads: list[dict[str, Any]] = []
        self.dispatched_writes: list[dict[str, Any]] = []
        self.records: dict[tuple[str, int], dict[str, Any]] = {
            ("orders", 1): {"id": 1, "no": "SO-1001", "customerName": "合成星河", "amount": 500, "status": 10},
            ("orders", 2): {"id": 2, "no": "SO-1002", "customerName": "晨岚机电", "amount": 800, "status": 20},
            ("crm/customer", 20): {
                "id": 20, "name": "合成星河", "ownerUserId": 9,
                "contactLastTime": None, "contactNextTime": None, "contactLastContent": "之前", "followUpStatus": False,
            },
            ("crm/contact", 10): {
                "id": 10, "name": "林舟", "customerId": 20, "ownerUserId": 9,
                "mobile": "13800000001", "email": "lin@example.test", "remark": "保留备注",
                "contactLastTime": None, "contactNextTime": None, "contactLastContent": None,
            },
            ("promotion/coupon-template", 7): {
                "id": 7, "name": "合成满减券", "usePrice": 10000,
                "discountPrice": 1500, "description": "原说明", "status": 0,
            },
        }

    def _login(self) -> str:
        return "synthetic-token"

    def _request(self, method: str, path: str, *, params=None, token=None):
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        self.http_reads.append({"method": method, "path": path, "params": clean_params})
        if self.fail_readback_after_write and self._write_happened and path.endswith("/get"):
            raise TimeoutError("synthetic readback timeout")
        if path == "secret/page":
            if self.auth_error_on_secret:
                raise BusinessToolAuthenticationError("synthetic session expired on secret/page")
            return 403, {"code": 403, "msg": "permission denied"}
        if path == "system/auth/get-permission-info":
            return 0, {"code": 0, "data": {
                "user": {"id": 9, "deptId": 7, "nickname": "管理员"},
                "roles": [],
                "permissions": [
                    "crm:customer:query", "crm:customer:create", "crm:customer:update",
                    "crm:contact:query", "crm:contact:create", "crm:contact:update",
                    "promotion:coupon-template:query", "promotion:coupon-template:update",
                ],
                "menus": [
                    {"id": 1, "name": "CRM管理", "path": "/crm", "parentId": 0, "type": 1, "visible": True,
                     "children": [
                         {"id": 2, "name": "客户管理", "path": "customer", "component": "crm/customer/index", "parentId": 1, "type": 2, "visible": True},
                         {"id": 3, "name": "联系人", "path": "contact", "component": "crm/contact/index", "parentId": 1, "type": 2, "visible": True},
                     ]},
                    {"id": 5, "name": "优惠券模板", "path": "/promotion", "parentId": 0, "type": 1, "visible": True,
                     "children": [
                         {"id": 6, "name": "优惠券模板", "path": "coupon-template", "component": "promotion/couponTemplate/index", "parentId": 5, "type": 2, "visible": True},
                     ]},
                    {"id": 4, "name": "销售订单", "path": "/orders", "component": "orders/index", "parentId": 0, "type": 2, "visible": True},
                ],
            }}
        if path == "system/dict-type/simple-list":
            return 0, {"code": 0, "data": [{"type": "crm_follow_up_type", "name": "跟进方式"}]}
        if path == "system/dict-data/simple-list":
            return 0, {"code": 0, "data": [{"dictType": "crm_follow_up_type", "label": "电话", "value": "1"}]}
        if path == "system/dept/simple-list":
            return 0, {"code": 0, "data": [{"id": 7, "name": "研发部", "parentId": 0}]}
        if path == "system/area/tree":
            return 0, {"code": 0, "data": [{"id": 100, "name": "合成省", "children": [{"id": 101, "name": "合成市"}]}]}
        if path == "system/menu/list":
            return 0, {"code": 0, "data": [
                {"id": 1, "name": "CRM管理", "path": "/crm", "parentId": 0, "type": 1, "visible": True},
                {"id": 2, "name": "客户管理", "path": "customer", "parentId": 1, "type": 2, "visible": True},
                {"id": 3, "name": "联系人", "path": "contact", "parentId": 1, "type": 2, "visible": True},
                {"id": 4, "name": "销售订单", "path": "/orders", "parentId": 0, "type": 2, "visible": True},
            ]}
        if path == "crm/permission/list":
            return 0, {"code": 0, "data": [{"userId": 9, "level": 1}]}
        if path == "crm/customer/simple-list":
            return 0, {"code": 0, "data": [
                {"id": r["id"], "name": r["name"]}
                for (ent, _), r in sorted(self.records.items()) if ent == "crm/customer"
            ]}
        if path.endswith("/page"):
            entity = path[:-len("/page")]
            name_q = str(clean_params.get("name") or "").strip().casefold()
            rows = [
                copy.deepcopy(r)
                for (ent, _), r in sorted(self.records.items())
                if ent == entity and (not name_q or name_q in str(r.get("name") or "").casefold())
            ]
            return 0, {"code": 0, "data": {"total": len(rows), "list": rows}}
        if path.endswith("/get"):
            entity = path[:-len("/get")]
            rid = int(clean_params["id"])
            row = self.records.get((entity, rid))
            return 0, {"code": 0, "data": copy.deepcopy(row)}
        raise AssertionError(f"Unexpected synthetic GET {path!r} params={clean_params!r}")

    def write_once(self, method: str, path: str, *, body: dict, params=None, operation: str = ""):
        if operation and operation not in BACKEND_WRITE_ATOMICITY:
            raise ValueError(f"native write has no reviewed backend contract: {operation}")
        self.calls.append(f"{method} {path}")
        self.dispatched_writes.append({
            "method": method,
            "path": "/admin-api/" + path.lstrip("/"),
            "body": copy.deepcopy(body),
            "params": copy.deepcopy(params),
            "operation": operation,
        })
        self._write_happened = True
        if path.endswith("/update"):
            entity = path[:-len("/update")]
            self.records[(entity, int(body["id"]))].update(copy.deepcopy(body))
            return True
        if path.endswith("/create"):
            entity = path[:-len("/create")]
            new_id = 30
            self.records[(entity, new_id)] = {"id": new_id, **copy.deepcopy(body)}
            return new_id
        raise AssertionError(f"Unexpected synthetic write {method} {path}")


def _collect_turn_capture(
    *,
    scenario: str,
    chat: _ScriptedChat,
    channel: _RecordingChannel,
    browser_receipts: list[dict[str, Any]],
    dispatched_writes: list[dict[str, Any]],
    backend_http_reads: list[dict[str, Any]],
    executor,
    execution,
    audit_path: Path,
    turn_tables_snapshot: dict[str, Any] | None = None,
    early_exit_multi_read: bool = False,
) -> dict[str, Any]:
    tool_receipts: list[dict[str, Any]] = []
    for msg in getattr(executor, "_messages", ()):
        if msg.get("role") == "tool":
            tool_receipts.append({
                "tool_call_id": msg.get("tool_call_id"),
                "content": msg.get("content"),
            })
    audit_records = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    payload: dict[str, Any] = {
        "scenario": scenario,
        "early_exit_multi_read": early_exit_multi_read,
        "model_calls": chat.recorded_calls,
        "approval_requests": channel.approval_payloads,
        "dispatched_writes": dispatched_writes,
        "backend_http_reads": backend_http_reads,
        "tool_receipts": {
            "tool_messages": tool_receipts,
            "browser_receipts": browser_receipts,
        },
        "turn_tables": turn_tables_snapshot,
        "result": {
            "status": execution.status.value,
            "terminal_reason": execution.terminal_reason,
            "user_answer": execution.user_answer,
        },
        "audit_records": audit_records,
    }
    return payload


@contextmanager
def _yudao_guard_context(client: _SyntheticYudaoClient, window: str = "tab-1"):
    guard = NativePageGuard(frozenset(), writing_gets=frozenset())
    actor = "a" * 64
    proxy = NativeGuardProxy(
        upstream="http://127.0.0.1:9",
        origin=ORIGIN,
        guard=guard,
        resolve_actor=lambda _token, _tenant: actor,
    ).start()
    try:
        proxy.ingress_base = proxy.url
        proxy.verify_ingress()
        guard.begin(actor, window)
        yield guard, proxy, actor
    finally:
        proxy.close()


def _run_yudao_scenario(
    *,
    scenario: str,
    prompt: str,
    steps: list[Any],
    page_url: str = ORIGIN + "/crm/contact",
    initial_obs: PageObservation | None = None,
    native_writes: bool = False,
    native_submit_enabled: bool = False,
    confirm_page_operations: bool = False,
    approval_decision: str = "approved",
    on_approval: Callable[[Any, _SyntheticYudaoClient], None] | None = None,
    fail_readback_after_write: bool = False,
    auth_error_on_secret: bool = False,
    use_read_only_window: bool = False,
    enable_prefill: bool = False,
    form_state: dict[str, Any] | None = None,
    open_dialog_elements: Callable[[int], list[ElementRef]] | None = None,
    customer_select_elements: Callable[[int], list[ElementRef]] | None = None,
    early_exit_multi_read: bool = False,
) -> dict[str, Any]:
    docs = _synthetic_yudao_docs()
    client = _SyntheticYudaoClient(
        fail_readback_after_write=fail_readback_after_write,
        auth_error_on_secret=auth_error_on_secret,
    )
    obs = initial_obs or _make_yudao_obs(1, url=page_url, page_route=page_url.removeprefix(ORIGIN) or "/")
    with tempfile.TemporaryDirectory() as tmp, _yudao_guard_context(client, "tab-1") as (guard, proxy, actor):
        tmp_path = Path(tmp)
        audit_path = tmp_path / "audit.jsonl"
        forms = None
        if use_read_only_window or native_submit_enabled or enable_prefill:
            forms = LiveFormPort(FenceEvidence(guard, lambda: actor, "tab-1"))
        app = YudaoApplication(
            ORIGIN,
            None,
            lambda: docs,
            api_base_url=ORIGIN,
            client=client,
            native_tabs=use_read_only_window or native_submit_enabled or enable_prefill,
            page_write_guard=GUARD_PATH if (use_read_only_window or native_submit_enabled or enable_prefill) else "",
            forms=forms,
            native_writes=native_writes,
            mutation_journal=MemoryMutationJournal() if native_writes else None,
        )
        bound = bind_application(app)
        page_identity = app.identity._assertion({"id": "9", "deptId": "7"}, "page_session")
        principal = app.identity.bind(page_identity, delegation_id="equiv-delegation")
        channel = _RecordingChannel(
            initial_obs=obs,
            principal=principal,
            decision=approval_decision,
            on_approval=(lambda req: on_approval(req, client)) if on_approval is not None else None,
        )
        chat = _ScriptedChat(
            steps,
            egress_policy=ProviderEgressPolicy(session_handoff_keys=SESSION_HANDOFF_KEYS),
        )
        page_review = NativePageReview("v-synth", "synthetic-review", (
            ("form_entry", "/crm/contact", "", "", ("新增",)),
            ("fields", "/crm/contact", "", "", ()),
        ))
        action_policy = None
        draft_steps = None
        if use_read_only_window:
            rw_review = ReadOnlyWindowPageReview.from_review(page_review)
            model_goal = ModelPageGoalPort(app.goal_port, rw_review)
            action_policy = ReadOnlyWindowPageActions(
                ORIGIN, app.goal_data, {}, model_goal, rw_review,
                guard_proxy=proxy, window="tab-1",
            )
            draft_steps = ReadOnlyWindowDraftSteps(app.goal_data, app.ports.forms, rw_review)
        else:
            model_goal = ModelPageGoalPort(app.goal_port, page_review)
        mutation_prefill = YudaoMutationPrefill(client, forms, page_review) if enable_prefill else None
        submit_port = YudaoSubmitPort(client, proxy, lambda: docs, window="tab-1")
        if native_submit_enabled:
            guard.note_read(actor, "/admin-api/crm/contact/get?id=10")

        turn_config = yudao_turn_config(
            tmp_path,
            timeout_s=120,
            source=SourceCapability.none(),
            click_policy=None,
            form_prefill=enable_prefill,
            native_writes=native_writes,
        )
        turn_config = replace(
            turn_config,
            deployment_profile="test",
            ledger_path=audit_path,
            confirm_page_operations=confirm_page_operations,
            action_policy=action_policy,
            max_user_answer_chars=panel_limits()["result_max_chars"],
            step_budget=16,
        )
        knowledge = ActorKnowledge(
            client,
            lambda: docs,
            time_zone="Asia/Shanghai",
            codes=app.goal_data.code_names,
            columns=app.goal_data.column_names,
            query_choices=app.goal_data.query_choices,
        )

        class _InstrumentedTableReadTurnExecutor(TableReadTurnExecutor):
            pass

        captured_tables: dict[str, Any] = {}
        orig_close = TurnTables.close

        def spy_close(self_tables):
            if not captured_tables:
                captured_tables["tables"] = copy.deepcopy(self_tables.describe().get("tables", []))
            return orig_close(self_tables)

        executor = _InstrumentedTableReadTurnExecutor(
            channel=channel,
            config=turn_config,
            chat=chat,
            application=bound,
            goal_port=model_goal,
            control=ReadOnlyControl(),
            run_id_factory=lambda _m: "equiv-run",
            draft_steps=draft_steps,
            mutation_prefill=mutation_prefill,
            native_submit=submit_port if native_submit_enabled else None,
            page_reads=submit_port.recent_reads,
            disabled_page_actions=frozenset({"submit", "back", "apply_facet"}),
            knowledge=knowledge,
            conventions=load_conventions(REFERENCE_CONVENTIONS),
            today=TODAY,
            time_zone="Asia/Shanghai",
            skill_knowledge=True,
            turn_traces=False,
        )

        def on_submit_click(_args: dict[str, Any]) -> None:
            actor_state = guard._actor(actor)
            permit = actor_state.permit_pending or actor_state.permit
            body = copy.deepcopy(form_state["model"])
            decision = permit.admit("PUT", "/admin-api/crm/contact/update", "", json.dumps(body).encode()) if permit else None
            if decision is not None and decision.allowed:
                permit.settle(decision.sequence, 200, b'{"code":0,"data":true}')
                client.records[("crm/contact", int(body["id"]))].update(body)
                client.dispatched_writes.append({
                    "method": "PUT",
                    "path": "/admin-api/crm/contact/update",
                    "body": copy.deepcopy(body),
                    "params": None,
                    "operation": "native_submit",
                })

        browser = _YudaoBrowserDouble(
            channel,
            form_state=form_state,
            on_submit_click=on_submit_click if native_submit_enabled else None,
            open_dialog_elements=open_dialog_elements,
            customer_select_elements=customer_select_elements,
        )
        # Seed initial reads before turn starts so we only capture turn-scoped HTTP reads cleanly
        app.goal_port.index()
        client.http_reads.clear()

        with patch.object(goal_executor, "RemoteBrowserClient", return_value=browser), \
                patch.object(TurnTables, "close", spy_close):
            execution = executor.execute({
                "message_id": "msg-1",
                "text": prompt,
                "page_url": page_url,
            })

        return _collect_turn_capture(
            scenario=scenario,
            chat=chat,
            channel=channel,
            browser_receipts=browser.receipts,
            dispatched_writes=client.dispatched_writes,
            backend_http_reads=client.http_reads,
            executor=executor,
            execution=execution,
            audit_path=audit_path,
            turn_tables_snapshot=captured_tables or None,
            early_exit_multi_read=early_exit_multi_read,
        )


def _run_contacts_scenario(
    *,
    scenario: str,
    prompt: str,
    steps: list[Any],
    allow_mutations: bool = False,
    direct_skill_operations: bool = False,
) -> dict[str, Any]:
    with MinimalContactsApplication() as app, tempfile.TemporaryDirectory() as tmp:
        actor = DEMO_ACTORS[0]
        adapter = MinimalContactsAdapter(
            app.origin,
            api_token=actor.token,
            allow_mutations=allow_mutations,
            mutation_journal=MemoryMutationJournal() if allow_mutations else None,
            standard_skills=allow_mutations,
            convention_updates=allow_mutations,
        )
        principal = adapter.identity.bind(
            adapter.identity.page_identity(f"{DEMO_SESSION_COOKIE}={actor.session}"),
            delegation_id="contacts-equiv",
        )
        obs = PageObservation(
            schema_version=1,
            epoch=1,
            url=app.origin + "/contacts",
            title="Contacts",
            landmarks=["Contacts"],
            elements=[],
            content="Contacts list",
            render_state="ready",
            page_route="/contacts",
        )
        channel = _RecordingChannel(initial_obs=obs, principal=principal, decision="approved")
        chat = _ScriptedChat(
            steps,
            egress_policy=ProviderEgressPolicy(
                session_handoff_keys=adapter.ports.routes.session_handoff_query_keys
            ),
        )
        audit_path = Path(tmp) / "audit.jsonl"
        goal_port = ContactsModelGoalPort(adapter, updates=allow_mutations)

        class _ContactsDirectExecutor(ModelDrivenTurnExecutor):
            direct_skill_operations = True

        exec_cls = _ContactsDirectExecutor if direct_skill_operations else ModelDrivenTurnExecutor
        executor = exec_cls(
            channel=channel,
            config=AgentTurnConfig(
                supported_scope="Read and maintain actor-visible contacts.",
                ledger_path=audit_path,
                require_identity=True,
                deployment_profile="test",
                allow_business_mutations=allow_mutations,
                confirm_page_operations=False,
            ),
            application=bind_application(adapter),
            chat=chat,
            goal_port=goal_port,
            profile=contacts_profile(adapter, goal_port),
            control=ReadOnlyControl(),
            verify=False,
            run_id_factory=lambda _m: "contacts-equiv-run",
            turn_traces=False,
        )
        browser = _YudaoBrowserDouble(channel)
        dispatched_writes: list[dict[str, Any]] = []
        backend_http_reads: list[dict[str, Any]] = []

        if allow_mutations:
            update_def = adapter.ports.operations.tools.get("update_record")
            orig_dispatch = update_def.handler.port.dispatch_update

            def spy_dispatch_update(spec, record_id, payload):
                dispatched_writes.append({
                    "method": "PUT",
                    "path": f"/api/contacts/{record_id}",
                    "body": copy.deepcopy(payload),
                    "operation": "update_record",
                })
                return orig_dispatch(spec, record_id, payload)

            update_def.handler.port.dispatch_update = spy_dispatch_update

        orig_execute = adapter.business_tools._executor.execute

        def spy_read(name, args, *, context=None):
            backend_http_reads.append({"method": "GET", "operation": name, "args": copy.deepcopy(args)})
            return orig_execute(name, args, context=context)

        adapter.business_tools._executor.execute = spy_read

        with patch.object(goal_executor, "RemoteBrowserClient", return_value=browser):
            execution = executor.execute({
                "message_id": "contacts-msg-1",
                "text": prompt,
                "page_url": app.origin + "/contacts",
            })

        return _collect_turn_capture(
            scenario=scenario,
            chat=chat,
            channel=channel,
            browser_receipts=browser.receipts,
            dispatched_writes=dispatched_writes,
            backend_http_reads=backend_http_reads,
            executor=executor,
            execution=execution,
            audit_path=audit_path,
        )


# ---------------------------------------------------------------------------
# 6 Scenario Families
# ---------------------------------------------------------------------------

def run_all_scenarios() -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []

    # Family 1: Yudao TableReadTurnExecutor (single read+sql+finish, multi-read batch, early-exit batch)
    scenarios.append(_run_yudao_scenario(
        scenario="yudao_table_read_single",
        prompt="查一下销售订单一共有几条，总金额是多少？",
        steps=[
            _tool_reply(("call_api", {"path": "/admin-api/orders/page", "params": json.dumps({"pageNo": 1, "pageSize": 10})})),
            _tool_reply(("sql", {"query": "SELECT COUNT(*) AS cnt, SUM(amount) AS total_amount FROM t1"})),
            lambda msgs, _tools: _tool_reply(("finish", {
                "status": "done",
                "answer": "共 2 条销售订单，总金额 1300。",
                "items": [{
                    "item": "共 2 条销售订单，总金额 1300",
                    "kind": "data",
                    "status": "delivered",
                    "receipts": _extract_all_receipts(msgs),
                }],
            })),
        ],
    ))

    scenarios.append(_run_yudao_scenario(
        scenario="yudao_table_read_multi_batch",
        prompt="查一下销售订单数量，同时找一下优惠券相关接口。",
        steps=[
            _tool_reply(
                ("call_api", {"path": "/admin-api/orders/page", "params": json.dumps({"pageNo": 1, "pageSize": 10})}),
                ("search_api", {"keyword": "优惠券"}),
            ),
            _tool_reply(("sql", {"query": "SELECT COUNT(*) AS cnt FROM t1"})),
            lambda msgs, _tools: _tool_reply(("finish", {
                "status": "done",
                "answer": "共 2 条销售订单，已检索优惠券接口。",
                "items": [{
                    "item": "共 2 条销售订单",
                    "kind": "data",
                    "status": "delivered",
                    "receipts": _extract_all_receipts(msgs),
                }],
            })),
        ],
    ))

    scenarios.append(_run_yudao_scenario(
        scenario="yudao_table_read_multi_early_exit",
        prompt="读取销售订单和内部机密分页。",
        steps=[
            _tool_reply(
                ("call_api", {"path": "/admin-api/orders/page", "params": json.dumps({"pageNo": 1, "pageSize": 10})}),
                ("call_api", {"path": "/admin-api/secret/page", "params": json.dumps({"pageNo": 1, "pageSize": 10})}),
            ),
        ],
        auth_error_on_secret=True,
        early_exit_multi_read=True,
    ))

    # Family 2: Yudao page navigation (goto / ReadOnlyWindowPageActions)
    scenarios.append(_run_yudao_scenario(
        scenario="yudao_page_navigation",
        prompt="请打开客户管理页面",
        page_url=ORIGIN + "/orders",
        initial_obs=_make_yudao_obs(1, url=ORIGIN + "/orders", title="订单", page_route="/orders"),
        use_read_only_window=True,
        confirm_page_operations=True,
        steps=[
            _tool_reply(("goto", {"path": "/crm/customer"})),
            lambda msgs, _tools: _tool_reply(("finish", {
                "status": "done",
                "answer": "已打开客户管理页面。",
                "items": [{
                    "item": "打开客户管理页面",
                    "kind": "page",
                    "status": "delivered",
                    "receipts": [_extract_receipt(msgs)],
                }],
            })),
        ],
    ))

    # Family 3: Yudao SkillWrites mutation (approved, denied, readback timeout -> indeterminate)
    scenarios.append(_run_yudao_scenario(
        scenario="yudao_skill_write_approved",
        prompt="把联系人林舟的手机改成13800000002",
        native_writes=True,
        steps=[
            _tool_reply(("load_skill", {"skill": "crm-contact-update"})),
            _tool_reply(("crm_contact_update", {"target": "林舟", "changes": {"mobile": "13800000002"}})),
            lambda msgs, _tools: _tool_reply(("finish", {
                "status": "done",
                "answer": "已将林舟的手机改为 13800000002。",
                "items": [{
                    "item": "修改林舟手机号为 13800000002",
                    "kind": "write",
                    "status": "delivered",
                    "receipts": [_extract_receipt(msgs)],
                }],
            })),
        ],
    ))

    scenarios.append(_run_yudao_scenario(
        scenario="yudao_skill_write_denied",
        prompt="把联系人林舟的手机改成13800000002",
        native_writes=True,
        approval_decision="denied",
        steps=[
            _tool_reply(("load_skill", {"skill": "crm-contact-update"})),
            _tool_reply(("crm_contact_update", {"target": "林舟", "changes": {"mobile": "13800000002"}})),
        ],
    ))

    scenarios.append(_run_yudao_scenario(
        scenario="yudao_skill_write_readback_timeout",
        prompt="把联系人林舟的手机改成13800000002",
        native_writes=True,
        fail_readback_after_write=True,
        steps=[
            _tool_reply(("load_skill", {"skill": "crm-contact-update"})),
            _tool_reply(("crm_contact_update", {"target": "林舟", "changes": {"mobile": "13800000002"}})),
        ],
    ))

    # Family 4: YudaoSubmitPort (native_submit): approved, prefill handoff, denied, reread drift refusal
    def _contact_form_state() -> dict[str, Any]:
        return {
            "mode": "update",
            "record_id": "10",
            "source": "/admin-api/crm/contact/get",
            "model": {
                "id": 10, "name": "林舟", "customerId": 20, "ownerUserId": 9,
                "mobile": "13800000001", "email": "lin@example.test", "remark": "保留备注",
            },
            "items": [
                {"prop": "name", "label": "姓名"},
                {"prop": "mobile", "label": "手机"},
                {"prop": "remark", "label": "备注"},
            ],
        }

    def _contact_edit_obs(epoch: int = 1) -> PageObservation:
        return _make_yudao_obs(
            epoch,
            url=ORIGIN + "/crm/contact",
            title="CRM 联系人",
            page_route="/crm/contact",
            elements=[
                ElementRef("e1", "textbox", "手机", "input", epoch, {
                    "form_scope": "dialog:edit-contact", "form_instance": "i1",
                    "field_label": "手机", "value": "13800000001", "prop": "mobile",
                }),
                ElementRef("e2", "button", "保存", "button", epoch, {
                    "form_scope": "dialog:edit-contact", "form_instance": "i1", "native_submit": "true",
                }),
            ],
        )

    scenarios.append(_run_yudao_scenario(
        scenario="yudao_native_submit_approved",
        prompt="把联系人林舟的手机改成13800000002并保存",
        initial_obs=_contact_edit_obs(1),
        native_writes=True,
        native_submit_enabled=True,
        use_read_only_window=True,
        confirm_page_operations=True,
        form_state=_contact_form_state(),
        steps=[
            _tool_reply(("input", {"index": 1, "text": "13800000002"})),
            _tool_reply(("request_submit", {"index": 2})),
            lambda msgs, _tools: _tool_reply(("finish", {
                "status": "done",
                "answer": "已保存林舟的手机号为 13800000002。",
                "items": [{
                    "item": "保存林舟手机号为 13800000002",
                    "kind": "write",
                    "status": "delivered",
                    "receipts": [_extract_receipt(msgs)],
                }],
            })),
        ],
    ))

    # Prefill handoff (Skill proposal with YudaoMutationPrefill + decision="prefill" + handoff_form)
    def _prefill_dialog_elements(epoch: int) -> list[ElementRef]:
        return [
            ElementRef("e1", "combobox", "客户名称", "input", epoch, {
                "form_scope": "dialog:new-contact", "form_instance": "i1",
                "field_label": "客户名称", "selected_option": "", "aria-haspopup": "listbox", "aria-controls": "c-list",
            }),
            ElementRef("e2", "textbox", "联系人姓名", "input", epoch, {
                "form_scope": "dialog:new-contact", "form_instance": "i1", "field_label": "联系人姓名", "value": "",
            }),
            ElementRef("e3", "textbox", "手机", "input", epoch, {
                "form_scope": "dialog:new-contact", "form_instance": "i1", "field_label": "手机", "value": "",
            }),
            ElementRef("e4", "textbox", "职位", "input", epoch, {
                "form_scope": "dialog:new-contact", "form_instance": "i1", "field_label": "职位", "value": "",
            }),
            ElementRef("e5", "combobox", "负责人", "input", epoch, {
                "form_scope": "dialog:new-contact", "form_instance": "i1",
                "field_label": "负责人", "selected_option": "管理员",
            }),
            ElementRef("e6", "button", "确定", "button", epoch, {
                "form_scope": "dialog:new-contact", "form_instance": "i1",
            }),
        ]

    def _prefill_after_customer(epoch: int) -> list[ElementRef]:
        els = _prefill_dialog_elements(epoch)
        els[0] = replace(els[0], attrs={**els[0].attrs, "selected_option": "合成星河", "value": "合成星河"})
        return els

    scenarios.append(_run_yudao_scenario(
        scenario="yudao_native_submit_prefill",
        prompt="在合成星河名下新建联系人顾溪，手机13911113332，职位项目协调员",
        initial_obs=_make_yudao_obs(1, url=ORIGIN + "/crm/contact", title="CRM 联系人", page_route="/crm/contact"),
        native_writes=True,
        native_submit_enabled=True,
        use_read_only_window=True,
        enable_prefill=True,
        approval_decision="prefill",
        open_dialog_elements=_prefill_dialog_elements,
        customer_select_elements=_prefill_after_customer,
        steps=[
            _tool_reply(("propose_operation", {
                "operation": "crm_contact_create",
                "inputs": {
                    "customer": "合成星河",
                    "name": "顾溪",
                    "mobile": "13911113332",
                    "post": "项目协调员",
                },
            })),
        ],
    ))

    scenarios.append(_run_yudao_scenario(
        scenario="yudao_native_submit_denied",
        prompt="把联系人林舟的手机改成13800000002并保存",
        initial_obs=_contact_edit_obs(1),
        native_writes=True,
        native_submit_enabled=True,
        use_read_only_window=True,
        confirm_page_operations=False,
        approval_decision="denied",
        form_state=_contact_form_state(),
        steps=[
            _tool_reply(("input", {"index": 1, "text": "13800000002"})),
            _tool_reply(("request_submit", {"index": 2})),
        ],
    ))

    def _drift_contact_record(request, drift_client: _SyntheticYudaoClient) -> None:
        if request.kind == "write":
            # Mutate the underlying record after approval card is shown but before pre-submit reread
            drift_client.records[("crm/contact", 10)]["remark"] = "并发修改后的备注"

    scenarios.append(_run_yudao_scenario(
        scenario="yudao_native_submit_reread_drift",
        prompt="把联系人林舟的手机改成13800000002并保存",
        initial_obs=_contact_edit_obs(1),
        native_writes=True,
        native_submit_enabled=True,
        use_read_only_window=True,
        confirm_page_operations=False,
        approval_decision="approved",
        on_approval=_drift_contact_record,
        form_state=_contact_form_state(),
        steps=[
            _tool_reply(("input", {"index": 1, "text": "13800000002"})),
            _tool_reply(("request_submit", {"index": 2})),
        ],
    ))

    # Family 5: convention_writes / propose_operation flat record update (approved, denied, readback timeout)
    scenarios.append(_run_yudao_scenario(
        scenario="yudao_convention_write_approved",
        prompt="把优惠券模板合成满减券的说明改成新说明",
        native_writes=True,
        steps=[
            _tool_reply(("propose_operation", {
                "operation": "update_record",
                "inputs": {
                    "entity": "promotion/coupon-template",
                    "target": "合成满减券",
                    "changes": [{"field": "description", "value": "新说明"}],
                },
            })),
            lambda msgs, _tools: _tool_reply(("finish", {
                "status": "done",
                "answer": "已将合成满减券的使用说明改为新说明。",
                "items": [{
                    "item": "修改合成满减券使用说明为新说明",
                    "kind": "write",
                    "status": "delivered",
                    "receipts": [_extract_receipt(msgs)],
                }],
            })),
        ],
    ))

    scenarios.append(_run_yudao_scenario(
        scenario="yudao_convention_write_denied",
        prompt="把优惠券模板合成满减券的说明改成新说明",
        native_writes=True,
        approval_decision="denied",
        steps=[
            _tool_reply(("propose_operation", {
                "operation": "update_record",
                "inputs": {
                    "entity": "promotion/coupon-template",
                    "target": "合成满减券",
                    "changes": [{"field": "description", "value": "新说明"}],
                },
            })),
        ],
    ))

    scenarios.append(_run_yudao_scenario(
        scenario="yudao_convention_write_readback_timeout",
        prompt="把优惠券模板合成满减券的说明改成新说明",
        native_writes=True,
        fail_readback_after_write=True,
        steps=[
            _tool_reply(("propose_operation", {
                "operation": "update_record",
                "inputs": {
                    "entity": "promotion/coupon-template",
                    "target": "合成满减券",
                    "changes": [{"field": "description", "value": "新说明"}],
                },
            })),
        ],
    ))

    # Family 6: Second application testapps/contacts (ModelDrivenTurnExecutor, program_read_delivery=True)
    scenarios.append(_run_contacts_scenario(
        scenario="contacts_read",
        prompt="Show Grace Hopper's email and total matching count.",
        allow_mutations=False,
        steps=[
            _tool_reply(("list_contacts", {
                "name_contains": "Grace Hopper",
                "summary": {"columns": ["name", "email"], "measures": [{"op": "count", "as": "count"}]},
            })),
            _tool_reply(("finish", {"status": "done", "answer": "", "read_answer": "read-1"})),
        ],
    ))

    scenarios.append(_run_contacts_scenario(
        scenario="contacts_write_approved",
        prompt="Change Ada Lovelace's email to ada.new@example.test.",
        allow_mutations=True,
        direct_skill_operations=True,
        steps=[
            _tool_reply(("propose_operation", {
                "operation": "update_record",
                "inputs": {
                    "entity": "contacts",
                    "target": "Ada Lovelace",
                    "changes": {"email": "ada.new@example.test"},
                },
            })),
            _tool_reply(("finish", {
                "status": "done",
                "answer": "Updated Ada Lovelace's email to ada.new@example.test.",
            })),
        ],
    ))

    return scenarios


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OPS-10 scripted-model equivalence check")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--capture", type=Path, help="Run all scenarios and write canonicalized JSON baseline")
    group.add_argument("--verify", type=Path, help="Run all scenarios and verify against JSON baseline")
    parser.add_argument(
        "--allow-registered",
        default="",
        help="Comma-separated registered allowances for Wave 1/2 tasks (ops-03, arc-08, arc-09, arc-12)",
    )
    args = parser.parse_args(argv)

    if args.capture is not None and args.allow_registered.strip():
        parser.error("--allow-registered is only valid with --verify")

    try:
        allow = parse_allow_registered(args.allow_registered)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    current = canonicalize_capture(run_all_scenarios())

    if args.capture is not None:
        args.capture.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        args.capture.write_text(rendered, encoding="utf-8")
        print(f"Captured {len(current['scenarios'])} scenarios -> {args.capture}")
        return 0

    if not args.verify.exists():
        print(f"ERROR: baseline file does not exist: {args.verify}", file=sys.stderr)
        return 2

    baseline = json.loads(args.verify.read_text(encoding="utf-8"))
    ok, diff = compare_captures(baseline, current, allow=allow)
    if not ok:
        print("EQUIVALENCE CHECK FAILED (unregistered difference detected):", file=sys.stderr)
        print(diff, file=sys.stderr)
        return 1
    print(
        f"EQUIVALENCE CHECK PASSED ({len(current['scenarios'])} scenarios"
        + (f", allow_registered={sorted(allow)}" if allow else "")
        + ")"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
