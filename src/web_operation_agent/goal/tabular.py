"""Local handles for complete read-only tables that have no record identifiers.

An adapter supplies a binding for its actor, credential generation and exact query.
The handles identify row contents within that binding, never application records or
write targets. Equal rows remain separate occurrences; neither names nor a missing
identifier may collapse them.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json

from .data import DataRefused


@dataclass(frozen=True)
class TableRow:
    reference: str
    values: dict


def table_rows(rows: list[dict], *, binding: str, max_rows: int) -> tuple[TableRow, ...]:
    """Validate a whole table before issuing any query-local references.

    Completeness and application authorization remain the adapter's responsibility.
    No reference is emitted for an invalid, oversized or unserializable response.
    """
    if not isinstance(binding, str) or not binding or len(binding) > 4096:
        raise ValueError("a table needs a bounded read binding")
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
        raise ValueError("max_rows must be a positive integer")
    if not isinstance(rows, list) or len(rows) > max_rows:
        raise DataRefused("read-only table is invalid or exceeds its row bound", read_failed=True)
    pending = []
    occurrences: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict) or any(not isinstance(key, str) for key in row):
            raise DataRefused("read-only table contains an invalid row", read_failed=True)
        try:
            encoded = json.dumps(row, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise DataRefused("read-only table contains an invalid value", read_failed=True) from None
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        occurrence = occurrences.get(digest, 0)
        occurrences[digest] = occurrence + 1
        reference = "table:" + hashlib.sha256(
            json.dumps([binding, digest, occurrence], separators=(",", ":")).encode()).hexdigest()
        pending.append(TableRow(reference, deepcopy(row)))
    return tuple(pending)
