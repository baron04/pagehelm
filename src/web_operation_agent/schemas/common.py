"""Validation and hashing primitives shared by the versioned schemas."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from ..origins import OriginError, normalize_origin

SCHEMA_VERSIONS = {
    "EvidenceRef": 1,
    "SourceRelation": 1,
    "SourceMap": 1,
    "OperationContract": 1,
    "PageObservation": 1,
    "ActionCommand": 1,
    "ActionReceipt": 1,
    "ClientHello": 1,
    "ObservationEnvelope": 1,
    "ActionEnvelope": 1,
    "ActionResultEnvelope": 1,
    "TaskEvent": 1,
    "ApprovalRequest": 1,
    "ApprovalDecision": 1,
    "ApprovalRefusalReport": 1,
    "CancellationRequest": 1,
    "ReconciliationRequest": 1,
    "ControlPlan": 1,
}


class SchemaError(ValueError):
    """A value did not satisfy its schema. Always fatal: nothing is coerced."""


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SchemaError(msg)


def _str(v: Any, name: str, *, allow_empty: bool = False) -> str:
    _require(isinstance(v, str), f"{name} must be a string, got {type(v).__name__}")
    _require(allow_empty or v != "", f"{name} must not be empty")
    return v


def _non_negative_int(v: Any, name: str) -> int:
    _require(isinstance(v, int) and not isinstance(v, bool) and v >= 0,
             f"{name} must be a non-negative integer")
    return v


def _bool(v: Any, name: str) -> bool:
    _require(type(v) is bool, f"{name} must be a boolean")
    return v


def _dict_copy(v: Any, name: str) -> dict:
    _require(isinstance(v, dict), f"{name} must be an object")
    _require(all(isinstance(key, str) for key in v), f"{name} keys must be strings")
    return copy.deepcopy(v)


def _sha256(v: Any, name: str) -> str:
    value = _str(v, name)
    _require(bool(re.fullmatch(r"[0-9a-f]{64}", value)),
             f"{name} must be 64 lowercase hex chars")
    return value


def _schema_version(v: Any, schema: str) -> int:
    expected = SCHEMA_VERSIONS[schema]
    _require(type(v) is int and v == expected,
             f"{schema} schema_version must be {expected}")
    return v


def parse_event_id(value: Any, *, name: str = "event_id", allow_empty: bool = False) -> int:
    """Return the sequence encoded by the one canonical SSE cursor format."""
    cursor = _str(value, name, allow_empty=allow_empty)
    if cursor == "" and allow_empty:
        return 0
    match = re.fullmatch(r"e([0-9]{8})", cursor)
    _require(match is not None, f"{name} must use the e######## cursor format")
    sequence = int(match.group(1))
    _require(sequence >= 1, f"{name} sequence must be at least 1")
    return sequence


def _strict_object(d: Any, name: str, *, allowed: set[str], required: set[str]) -> dict:
    """Validate a public wire object without silently accepting protocol drift."""
    _require(isinstance(d, dict), f"{name} must be an object")
    extra = set(d) - allowed
    missing = required - set(d)
    _require(not extra, f"{name} has unknown fields {sorted(extra)}")
    _require(not missing, f"{name} is missing fields {sorted(missing)}")
    return d


def _string_sequence(v: Any, name: str) -> tuple[str, ...]:
    _require(isinstance(v, (list, tuple)), f"{name} must be an array")
    out = tuple(_str(item, f"{name} entry") for item in v)
    _require(len(out) == len(set(out)), f"{name} entries must be unique")
    return out


def _page_origin(v: Any) -> str:
    """The page's own origin, canonical: whoever receives it compares it with the browser's
    Origin header and with the session's allowed origins."""
    try:
        return normalize_origin(_str(v, "page_origin"), name="page_origin")
    except OriginError as exc:
        raise SchemaError(str(exc)) from exc
