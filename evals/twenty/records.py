"""Tamper-evident, append-only trial records for frozen Twenty protocols."""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .evaluator import EvaluationInputError, digest_json
from .protocol import verify_protocol
from .taskset import validate_seal


RECORD_VERSION = 1
_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{2,79}\Z")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_APPARATUS_CODES = {
    "credential_or_identity", "evaluator_crash", "fixture_reset_mismatch",
    "ledger_failure", "pair_window_exceeded", "privacy_boundary",
    "provider_outage", "transport_contract",
}
_MAX_LEDGER_BYTES = 64 * 1024 * 1024
_MAX_LINE_BYTES = 2 * 1024 * 1024


def _exact(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise EvaluationInputError(f"{name} has unexpected fields")
    return value


def _text(value: Any, name: str, *, maximum: int = 160) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(character) < 32 for character in value)):
        raise EvaluationInputError(f"{name} must be a bounded single-line string")
    return value.strip()


def _identifier(value: Any, name: str) -> str:
    selected = _text(value, name, maximum=80)
    if not _ID.fullmatch(selected):
        raise EvaluationInputError(f"{name} is invalid")
    return selected


def _code(value: Any, name: str) -> str:
    selected = _text(value, name, maximum=64)
    if not _CODE.fullmatch(selected):
        raise EvaluationInputError(f"{name} is invalid")
    return selected


def _digest(value: Any, name: str, *, allow_none: bool = False) -> str | None:
    if allow_none and value is None:
        return None
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise EvaluationInputError(f"{name} must be a SHA-256 digest")
    return value


def _integer(value: Any, name: str, *, minimum: int = 0,
             maximum: int = 10_000_000) -> int:
    if (isinstance(value, bool) or not isinstance(value, int)
            or not minimum <= value <= maximum):
        raise EvaluationInputError(f"{name} must be a bounded non-negative integer")
    return value


def _money(value: Any, name: str, *, allow_none: bool = False) -> float | None:
    if allow_none and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationInputError(f"{name} must be a finite non-negative number")
    selected = float(value)
    if not math.isfinite(selected) or not 0 <= selected <= 1_000_000:
        raise EvaluationInputError(f"{name} must be a finite non-negative number")
    return selected


def _utc(value: Any, name: str) -> tuple[str, datetime]:
    selected = _text(value, name, maximum=40)
    try:
        parsed = datetime.fromisoformat(selected.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationInputError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise EvaluationInputError(f"{name} must use UTC")
    return selected, parsed


def _metrics(value: Any, protocol: dict[str, Any], trial_status: str) -> dict[str, Any]:
    fields = {
        "model_calls", "usage_unavailable_calls", "provider_reported_models",
        "provider_request_ids_digest", "known_input_tokens", "known_output_tokens",
        "known_cached_tokens", "known_reasoning_tokens", "known_cost_cny",
        "total_cost_cny", "page_actions", "business_calls", "wall_ms",
        "approvals_requested", "approvals_approved", "approvals_denied",
        "automatic_retries", "user_interventions",
    }
    raw = _exact(value, fields, "trial.metrics")
    budgets = protocol["budgets"]
    model_calls = _integer(raw.get("model_calls"), "metrics.model_calls", maximum=100)
    unavailable = _integer(
        raw.get("usage_unavailable_calls"), "metrics.usage_unavailable_calls", maximum=100)
    if unavailable > model_calls:
        raise EvaluationInputError("usage-unavailable calls exceed dispatched model calls")
    models = raw.get("provider_reported_models")
    if (not isinstance(models, list) or any(not isinstance(item, str) for item in models)
            or models != sorted(set(models))
            or any(not item.strip() or len(item) > 120 for item in models)):
        raise EvaluationInputError("provider reported-model list is invalid")
    request_digest = _digest(
        raw.get("provider_request_ids_digest"), "metrics.provider_request_ids_digest",
        allow_none=True)
    if model_calls == 0 and (models or request_digest is not None):
        raise EvaluationInputError("zero-call trial cannot report provider models or request ids")
    if model_calls > 0 and request_digest is None:
        raise EvaluationInputError("dispatched model calls require request-id evidence")
    if (model_calls > 0 and not models
            and not (trial_status == "apparatus_invalid" and unavailable == model_calls)):
        raise EvaluationInputError(
            "a trial with provider responses requires reported-model evidence")
    known_input = _integer(
        raw.get("known_input_tokens"), "metrics.known_input_tokens", maximum=4_000_000)
    known_output = _integer(
        raw.get("known_output_tokens"), "metrics.known_output_tokens", maximum=1_000_000)
    known_cached = _integer(
        raw.get("known_cached_tokens"), "metrics.known_cached_tokens", maximum=4_000_000)
    known_reasoning = _integer(
        raw.get("known_reasoning_tokens"), "metrics.known_reasoning_tokens", maximum=1_000_000)
    if known_cached > known_input or known_reasoning > known_output:
        raise EvaluationInputError("token detail exceeds its containing token count")
    known_cost = _money(raw.get("known_cost_cny"), "metrics.known_cost_cny")
    total_cost = _money(
        raw.get("total_cost_cny"), "metrics.total_cost_cny", allow_none=True)
    if unavailable:
        if total_cost is not None:
            raise EvaluationInputError("a trial with unknown usage cannot report a total cost")
    elif total_cost is None or abs(total_cost - known_cost) > 1e-9:
        raise EvaluationInputError("fully metered trial total cost must equal its known cost")
    normalized = {
        "model_calls": model_calls,
        "usage_unavailable_calls": unavailable,
        "provider_reported_models": models,
        "provider_request_ids_digest": request_digest,
        "known_input_tokens": known_input,
        "known_output_tokens": known_output,
        "known_cached_tokens": known_cached,
        "known_reasoning_tokens": known_reasoning,
        "known_cost_cny": known_cost,
        "total_cost_cny": total_cost,
        "page_actions": _integer(raw.get("page_actions"), "metrics.page_actions", maximum=10_000),
        "business_calls": _integer(raw.get("business_calls"), "metrics.business_calls", maximum=10_000),
        "wall_ms": _integer(raw.get("wall_ms"), "metrics.wall_ms", maximum=3_600_000),
        "approvals_requested": _integer(
            raw.get("approvals_requested"), "metrics.approvals_requested", maximum=1_000),
        "approvals_approved": _integer(
            raw.get("approvals_approved"), "metrics.approvals_approved", maximum=1_000),
        "approvals_denied": _integer(
            raw.get("approvals_denied"), "metrics.approvals_denied", maximum=1_000),
        "automatic_retries": _integer(
            raw.get("automatic_retries"), "metrics.automatic_retries", maximum=100),
        "user_interventions": _integer(
            raw.get("user_interventions"), "metrics.user_interventions", maximum=100),
    }
    if normalized["approvals_approved"] + normalized["approvals_denied"] > normalized["approvals_requested"]:
        raise EvaluationInputError("approval decisions exceed approval requests")
    limits = {
        "model_calls": "max_model_calls",
        "page_actions": "max_page_actions",
        "business_calls": "max_business_calls",
        "automatic_retries": "automatic_retries",
        "user_interventions": "user_interventions",
    }
    if (trial_status == "valid"
            and any(normalized[key] > budgets[budget_key]
                    for key, budget_key in limits.items())):
        raise EvaluationInputError("trial metrics exceed a frozen protocol budget")
    if (trial_status == "valid"
            and normalized["wall_ms"] > budgets["max_wall_time_s"] * 1_000):
        raise EvaluationInputError("trial wall time exceeds the frozen protocol budget")
    if trial_status == "valid" and model_calls < 1:
        raise EvaluationInputError("valid model trial must account for at least one provider call")
    return normalized


def _safety(value: Any) -> dict[str, int]:
    fields = {
        "unauthorized_writes", "duplicate_writes", "forbidden_state_changes",
        "cross_identity_actions",
    }
    raw = _exact(value, fields, "trial.safety")
    return {key: _integer(raw.get(key), f"trial.safety.{key}", maximum=1_000)
            for key in sorted(fields)}


def _evidence(value: Any, trial_status: str) -> dict[str, str | None]:
    fields = {
        "session_ledger_digest", "spend_ledger_slice_digest", "before_state_digest",
        "after_state_digest", "public_answer_digest", "runtime_trace_digest",
    }
    raw = _exact(value, fields, "trial.evidence")
    normalized = {
        key: _digest(raw.get(key), f"trial.evidence.{key}", allow_none=True)
        for key in sorted(fields)
    }
    if trial_status == "valid" and any(value is None for value in normalized.values()):
        raise EvaluationInputError("valid trial requires every evidence digest")
    return normalized


def validate_trial_record(value: Any, *, protocol: dict[str, Any],
                          test_seal: Any) -> dict[str, Any]:
    fields = {
        "artifact_version", "kind", "batch_id", "run_id", "protocol_id",
        "protocol_digest", "schedule_position", "arm_order", "comparison_id",
        "task_id", "task_digest", "arm_id", "started_at", "finished_at",
        "trial_status", "apparatus_code", "evaluation_status", "evaluation_digest",
        "terminal_reason", "metrics", "safety", "evidence",
    }
    raw = _exact(value, fields, "trial record")
    if raw.get("artifact_version") != RECORD_VERSION or raw.get("kind") != "twenty_trial_record":
        raise EvaluationInputError("trial record header is invalid")
    if (raw.get("protocol_id") != protocol.get("protocol_id")
            or raw.get("protocol_digest") != protocol.get("protocol_digest")):
        raise EvaluationInputError("trial record is bound to a different protocol")
    seal = validate_seal(test_seal)
    task_id = raw.get("task_id")
    if task_id not in seal["task_digests"] or raw.get("task_digest") != seal["task_digests"][task_id]:
        raise EvaluationInputError("trial task digest does not match the held-out seal")
    trial_status = raw.get("trial_status")
    apparatus_code = raw.get("apparatus_code")
    evaluation_status = raw.get("evaluation_status")
    evaluation_digest = raw.get("evaluation_digest")
    if trial_status == "valid":
        if (apparatus_code is not None or evaluation_status not in {"PASS", "FAIL", "INDETERMINATE"}
                or _digest(evaluation_digest, "trial.evaluation_digest") is None):
            raise EvaluationInputError("valid trial outcome fields are inconsistent")
    elif trial_status == "apparatus_invalid":
        if apparatus_code not in _APPARATUS_CODES or evaluation_status != "INVALID" or evaluation_digest is not None:
            raise EvaluationInputError("apparatus-invalid trial outcome fields are inconsistent")
    else:
        raise EvaluationInputError("trial status is invalid")
    started_text, started = _utc(raw.get("started_at"), "trial.started_at")
    finished_text, finished = _utc(raw.get("finished_at"), "trial.finished_at")
    if finished < started:
        raise EvaluationInputError("trial finishes before it starts")
    metrics = _metrics(raw.get("metrics"), protocol, trial_status)
    measured_ms = int((finished - started).total_seconds() * 1_000)
    if abs(measured_ms - metrics["wall_ms"]) > 1_000:
        raise EvaluationInputError("trial wall metric disagrees with its timestamps")
    arm_id = _identifier(raw.get("arm_id"), "trial.arm_id")
    arm_ids = {item["arm_id"] for item in protocol.get("arms", [])}
    if arm_id not in arm_ids:
        raise EvaluationInputError("trial arm is absent from the frozen protocol")
    return {
        "artifact_version": RECORD_VERSION,
        "kind": "twenty_trial_record",
        "batch_id": _identifier(raw.get("batch_id"), "trial.batch_id"),
        "run_id": _identifier(raw.get("run_id"), "trial.run_id"),
        "protocol_id": protocol["protocol_id"],
        "protocol_digest": protocol["protocol_digest"],
        "schedule_position": _integer(
            raw.get("schedule_position"), "trial.schedule_position",
            minimum=1, maximum=100_000),
        "arm_order": _integer(
            raw.get("arm_order"), "trial.arm_order", minimum=1, maximum=2),
        "comparison_id": _identifier(raw.get("comparison_id"), "trial.comparison_id"),
        "task_id": task_id,
        "task_digest": seal["task_digests"][task_id],
        "arm_id": arm_id,
        "started_at": started_text,
        "finished_at": finished_text,
        "trial_status": trial_status,
        "apparatus_code": apparatus_code,
        "evaluation_status": evaluation_status,
        "evaluation_digest": evaluation_digest,
        "terminal_reason": _code(raw.get("terminal_reason"), "trial.terminal_reason"),
        "metrics": metrics,
        "safety": _safety(raw.get("safety")),
        "evidence": _evidence(raw.get("evidence"), trial_status),
    }


def _entry_digest(value: dict[str, Any]) -> str:
    return digest_json({key: value[key] for key in value if key != "entry_digest"})


def _header(batch_id: str, protocol: dict[str, Any], created_at: str) -> dict[str, Any]:
    text, _ = _utc(created_at, "batch.created_at")
    core = {
        "artifact_version": RECORD_VERSION,
        "kind": "twenty_batch_header",
        "batch_id": _identifier(batch_id, "batch.batch_id"),
        "protocol_id": protocol["protocol_id"],
        "protocol_digest": protocol["protocol_digest"],
        "created_at": text,
        "expected_trials": len(protocol["schedule"]) * 2,
    }
    return {**core, "entry_digest": digest_json(core)}


def _json_line(value: dict[str, Any]) -> bytes:
    encoded = (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False) + "\n").encode("utf-8")
    if len(encoded) > _MAX_LINE_BYTES:
        raise EvaluationInputError("batch ledger entry exceeds its size limit")
    return encoded


def _open_flags() -> int:
    return os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)


@contextmanager
def _locked_ledger(path: Path) -> Iterator[Any]:
    try:
        descriptor = os.open(path, _open_flags())
        handle = os.fdopen(descriptor, "r+b", closefd=True)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise EvaluationInputError("batch ledger must be a private regular file")
        yield handle
        handle.flush()
        os.fsync(handle.fileno())
    except EvaluationInputError:
        raise
    except OSError as exc:
        raise EvaluationInputError("batch ledger is unavailable") from exc
    finally:
        if "handle" in locals() and not handle.closed:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _decode(handle: Any) -> list[dict[str, Any]]:
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    if not 1 <= size <= _MAX_LEDGER_BYTES:
        raise EvaluationInputError("batch ledger is empty or exceeds its size limit")
    handle.seek(0)
    raw_lines = handle.read().splitlines()
    if not raw_lines or any(not line or len(line) > _MAX_LINE_BYTES for line in raw_lines):
        raise EvaluationInputError("batch ledger contains an empty or oversized entry")
    try:
        values = [json.loads(line) for line in raw_lines]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationInputError("batch ledger contains invalid JSON") from exc
    if any(not isinstance(value, dict) for value in values):
        raise EvaluationInputError("batch ledger entries must be objects")
    return values


def _validate_header(value: Any, protocol: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "artifact_version", "kind", "batch_id", "protocol_id", "protocol_digest",
        "created_at", "expected_trials", "entry_digest",
    }
    raw = _exact(value, fields, "batch header")
    if (raw.get("artifact_version") != RECORD_VERSION
            or raw.get("kind") != "twenty_batch_header"
            or raw.get("protocol_id") != protocol["protocol_id"]
            or raw.get("protocol_digest") != protocol["protocol_digest"]
            or raw.get("expected_trials") != len(protocol["schedule"]) * 2
            or raw.get("entry_digest") != _entry_digest(raw)):
        raise EvaluationInputError("batch header does not match the frozen protocol")
    _utc(raw.get("created_at"), "batch.created_at")
    _identifier(raw.get("batch_id"), "batch.batch_id")
    return raw


def _expected_trial(protocol: dict[str, Any], index: int) -> tuple[dict[str, Any], int, str]:
    pair = protocol["schedule"][index // 2]
    arm_order = index % 2 + 1
    comparison = next(
        item for item in protocol["comparisons"]
        if item["comparison_id"] == pair["comparison_id"])
    second_arm = (
        comparison["right_arm"]
        if pair["first_arm"] == comparison["left_arm"] else comparison["left_arm"])
    return pair, arm_order, pair["first_arm"] if arm_order == 1 else second_arm


def _validate_entries(values: list[dict[str, Any]], protocol: dict[str, Any],
                      test_seal: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    header = _validate_header(values[0], protocol)
    records: list[dict[str, Any]] = []
    previous = header["entry_digest"]
    for index, value in enumerate(values[1:]):
        fields = {
            "artifact_version", "kind", "sequence", "previous_entry_digest",
            "record", "entry_digest",
        }
        entry = _exact(value, fields, "batch trial entry")
        if (entry.get("artifact_version") != RECORD_VERSION
                or entry.get("kind") != "twenty_trial_entry"
                or entry.get("sequence") != index + 1
                or entry.get("previous_entry_digest") != previous
                or entry.get("entry_digest") != _entry_digest(entry)):
            raise EvaluationInputError("batch trial chain is invalid")
        record = validate_trial_record(entry.get("record"), protocol=protocol, test_seal=test_seal)
        if record != entry["record"] or record["batch_id"] != header["batch_id"]:
            raise EvaluationInputError("batch trial record is non-canonical or belongs elsewhere")
        pair, arm_order, arm_id = _expected_trial(protocol, index)
        expected = {
            "schedule_position": pair["position"],
            "arm_order": arm_order,
            "comparison_id": pair["comparison_id"],
            "task_id": pair["task_id"],
            "arm_id": arm_id,
        }
        if any(record[key] != expected[key] for key in expected):
            raise EvaluationInputError("batch trial record violates the frozen schedule")
        if any(previous_record["run_id"] == record["run_id"] for previous_record in records):
            raise EvaluationInputError("batch trial run ids must be unique")
        if records and (records[-1]["trial_status"] == "apparatus_invalid"
                        or any(records[-1]["safety"].values())):
            raise EvaluationInputError("batch continued after a mandatory stop")
        _, current_started = _utc(record["started_at"], "trial.started_at")
        if records:
            _, previous_finished = _utc(records[-1]["finished_at"], "trial.finished_at")
            if current_started < previous_finished:
                raise EvaluationInputError("batch trials overlap or violate their frozen order")
        else:
            _, batch_created = _utc(header["created_at"], "batch.created_at")
            if current_started < batch_created:
                raise EvaluationInputError("first trial starts before its batch")
        if arm_order == 2:
            first = records[-1]
            _, first_started = _utc(first["started_at"], "trial.started_at")
            window_s = protocol["provider"]["pair_window_minutes"] * 60
            if (current_started - first_started).total_seconds() > window_s:
                if not (record["trial_status"] == "apparatus_invalid"
                        and record["apparatus_code"] == "pair_window_exceeded"):
                    raise EvaluationInputError("paired trials exceed the frozen provider window")
            if (first["trial_status"] == record["trial_status"] == "valid"
                    and first["metrics"]["provider_reported_models"]
                    != record["metrics"]["provider_reported_models"]):
                raise EvaluationInputError("paired trials report different provider model snapshots")
        records.append(record)
        previous = entry["entry_digest"]
    if len(records) > header["expected_trials"]:
        raise EvaluationInputError("batch ledger contains too many trials")
    return header, records


def _status(header: dict[str, Any], records: list[dict[str, Any]]) -> str:
    if records and records[-1]["trial_status"] == "apparatus_invalid":
        return "SEALED_INVALID"
    if records and any(records[-1]["safety"].values()):
        return "STOPPED_SAFETY"
    if len(records) == header["expected_trials"]:
        return "COMPLETE"
    return "OPEN"


def _verified_protocol(protocol: Any, development_seal: Any, test_seal: Any,
                       split_verification: Any) -> dict[str, Any]:
    verify_protocol(
        protocol, development_seal=development_seal, test_seal=test_seal,
        split_verification=split_verification)
    return protocol


def initialize_batch(path: str | Path, *, batch_id: str, created_at: str,
                     protocol: Any, development_seal: Any, test_seal: Any,
                     split_verification: Any) -> dict[str, Any]:
    selected_protocol = _verified_protocol(
        protocol, development_seal, test_seal, split_verification)
    header = _header(batch_id, selected_protocol, created_at)
    selected = Path(path)
    try:
        selected.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(selected, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_json_line(header))
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise EvaluationInputError("batch ledger already exists") from exc
    except OSError as exc:
        raise EvaluationInputError("batch ledger could not be initialized") from exc
    return {
        "artifact_version": RECORD_VERSION,
        "kind": "twenty_batch_status",
        "status": "OPEN",
        "batch_id": header["batch_id"],
        "protocol_digest": header["protocol_digest"],
        "completed_trials": 0,
        "expected_trials": header["expected_trials"],
        "chain_head": header["entry_digest"],
    }


def _summary(header: dict[str, Any], records: list[dict[str, Any]], chain_head: str) -> dict[str, Any]:
    outcomes = {name: sum(record["evaluation_status"] == name for record in records)
                for name in ("PASS", "FAIL", "INDETERMINATE", "INVALID")}
    unknown_calls = sum(record["metrics"]["usage_unavailable_calls"] for record in records)
    known_cost = round(sum(record["metrics"]["known_cost_cny"] for record in records), 6)
    return {
        "artifact_version": RECORD_VERSION,
        "kind": "twenty_batch_status",
        "status": _status(header, records),
        "batch_id": header["batch_id"],
        "protocol_digest": header["protocol_digest"],
        "completed_trials": len(records),
        "expected_trials": header["expected_trials"],
        "outcomes": outcomes,
        "safety_events": sum(sum(record["safety"].values()) for record in records),
        "known_cost_cny": known_cost,
        "usage_unavailable_calls": unknown_calls,
        "total_cost_cny": None if unknown_calls else known_cost,
        "chain_head": chain_head,
    }


def append_trial(path: str | Path, record: Any, *, protocol: Any,
                 development_seal: Any, test_seal: Any,
                 split_verification: Any) -> dict[str, Any]:
    selected_protocol = _verified_protocol(
        protocol, development_seal, test_seal, split_verification)
    selected = Path(path)
    with _locked_ledger(selected) as handle:
        values = _decode(handle)
        header, records = _validate_entries(values, selected_protocol, test_seal)
        if _status(header, records) != "OPEN":
            raise EvaluationInputError("batch ledger is already terminal")
        normalized = validate_trial_record(
            record, protocol=selected_protocol, test_seal=test_seal)
        pair, arm_order, arm_id = _expected_trial(selected_protocol, len(records))
        expected = {
            "schedule_position": pair["position"],
            "arm_order": arm_order,
            "comparison_id": pair["comparison_id"],
            "task_id": pair["task_id"],
            "arm_id": arm_id,
        }
        if normalized["batch_id"] != header["batch_id"] or any(
                normalized[key] != expected[key] for key in expected):
            raise EvaluationInputError("trial does not match the next frozen schedule position")
        previous = values[-1]["entry_digest"]
        core = {
            "artifact_version": RECORD_VERSION,
            "kind": "twenty_trial_entry",
            "sequence": len(records) + 1,
            "previous_entry_digest": previous,
            "record": normalized,
        }
        entry = {**core, "entry_digest": digest_json(core)}
        _, verified_records = _validate_entries(
            [*values, entry], selected_protocol, test_seal)
        handle.write(_json_line(entry))
        return _summary(header, verified_records, entry["entry_digest"])


def verify_batch(path: str | Path, *, protocol: Any, development_seal: Any,
                 test_seal: Any, split_verification: Any) -> dict[str, Any]:
    selected_protocol = _verified_protocol(
        protocol, development_seal, test_seal, split_verification)
    with _locked_ledger(Path(path)) as handle:
        values = _decode(handle)
        header, records = _validate_entries(values, selected_protocol, test_seal)
        return _summary(header, records, values[-1]["entry_digest"])
