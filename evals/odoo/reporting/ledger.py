"""Append-only, hash-chained attempt ledger for Odoo evaluation batches.

Every attempt is written down before its runner starts, every model request before it is
forwarded, and every result afterwards, so a failed or interrupted trial cannot be rerun
without leaving a trace. The file reuses the Twenty batch ledger's discipline -- a private
``0600`` regular file, a POSIX lock held across read-validate-append, SHA-256 entry digests
linking each line to the previous one. It records attempts rather than completed trials:
an attempt may be interrupted, replaced within a closed policy, or end a batch.

Validation replays the whole chain on every append and on every read. The rules it enforces
are the protocol's, not conveniences: one open attempt at a time, a fixed record order per
attempt, legal combinations of the three outcome axes, bounded replacements that follow their
original immediately, and mandatory seals after safety events, reset failures, unclassified
results, provider model-string changes, requests that belong to no attempt, a refused provider
credential, a call the spend ledger could not record and a request outside the frozen request
contract.

Every entry carries the ledger version it was written under, and a ledger of any other version
is refused whole rather than read by rules it was not written under.
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from evals.odoo.domain.population import digest_json
from evals.odoo.reporting.replay import (
    DISPOSITIONS, LEDGER_VERSION, EvaluationInputError, LedgerError,
    _MAX_LEDGER_BYTES, _MAX_LINE_BYTES, _SEAL_REASONS, _canonical_model_string,
    _entry_digest, _time, records_request_parameters, replay as _replay, utc_now,
)

# Validation and replay live in replay.py; this module owns durable file I/O.


def replay(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate entries under the version currently exposed by this public module."""
    return _replay(entries, version=LEDGER_VERSION)


def _json_line(value: dict[str, Any]) -> bytes:
    try:
        encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvaluationInputError("ledger entry is not canonical JSON") from exc
    if len(encoded) > _MAX_LINE_BYTES:
        raise EvaluationInputError("ledger entry exceeds its size limit")
    return encoded


@contextmanager
def _locked_ledger(path: Path) -> Iterator[Any]:
    handle = None
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
        handle = os.fdopen(descriptor, "r+b", closefd=True)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise EvaluationInputError("ledger must be a private regular file")
        yield handle
        handle.flush()
        os.fsync(handle.fileno())
    except EvaluationInputError:
        raise
    except OSError as exc:
        raise EvaluationInputError("ledger is unavailable") from exc
    finally:
        if handle is not None and not handle.closed:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _decode(handle: Any) -> list[dict[str, Any]]:
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    if not 1 <= size <= _MAX_LEDGER_BYTES:
        raise EvaluationInputError("ledger is empty or exceeds its size limit")
    handle.seek(0)
    raw_lines = handle.read().splitlines()
    if not raw_lines or any(not line or len(line) > _MAX_LINE_BYTES for line in raw_lines):
        raise EvaluationInputError("ledger contains an empty or oversized entry")
    try:
        values = [json.loads(line) for line in raw_lines]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationInputError("ledger contains invalid JSON") from exc
    if any(not isinstance(value, dict) for value in values):
        raise EvaluationInputError("ledger entries must be objects")
    return values


def _new_entry(kind: str, body: dict[str, Any], seq: int, prev: str | None,
               at: str | None) -> dict[str, Any]:
    entry = {"v": LEDGER_VERSION, "kind": kind, "seq": seq, "prev": prev,
             "at": at or utc_now(), "body": body}
    entry["digest"] = _entry_digest(entry)
    return entry


def create(path: str | Path, header: dict[str, Any], *, at: str | None = None) -> dict[str, Any]:
    """Create a new private ledger holding only its validated batch header."""
    entry = _new_entry("batch_header", header, 0, None, at)
    summary = replay([entry])
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError as exc:
        raise LedgerError("attempt ledger already exists") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(_json_line(entry))
        handle.flush()
        os.fsync(handle.fileno())
    return summary


def append(path: str | Path, kind: str, body: dict[str, Any], *,
           at: str | None = None) -> dict[str, Any]:
    """Validate the chain plus one prospective entry under the lock, then write it."""
    if kind == "batch_header":
        raise LedgerError("a batch header is written only by create()")
    with _locked_ledger(Path(path)) as handle:
        entries = _decode(handle)
        replay(entries)
        entry = _new_entry(kind, body, len(entries), entries[-1]["digest"], at)
        if _time(entry["at"]) < _time(entries[-1]["at"]):
            entry = _new_entry(kind, body, len(entries), entries[-1]["digest"],
                               entries[-1]["at"])
        summary = replay([*entries, entry])
        handle.write(_json_line(entry))
        return summary


def read(path: str | Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with _locked_ledger(Path(path)) as handle:
        entries = _decode(handle)
        return entries, replay(entries)


def schedule_digest(slots: list[dict[str, Any]]) -> str:
    return digest_json(slots)
