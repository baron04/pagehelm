"""What an organisation means by some of its words, as its deployment declares.

The goal index shows a convention to the model when the request says one of its words.
Conventions are the organisation's, not the application's: two companies on the same
application say "订单" and mean different things, so they are written down by the
deployment and reviewed by its owner, never inferred from the application. Each adapter
ships its reference deployment's file; this module reads any of them.
"""
from __future__ import annotations

import json
from pathlib import Path

from .index import IndexConvention


def load_conventions(path: str | Path) -> tuple[IndexConvention, ...]:
    """The conventions a deployment's file declares; a malformed file is refused."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("a conventions file has schema_version 1")
    entries = document.get("conventions")
    if not isinstance(entries, list):
        raise ValueError("a conventions file lists its conventions")
    out = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("terms"), list):
            raise ValueError("a convention has terms and a meaning")
        records = entry.get("records")
        if records is not None and not (isinstance(records, dict)
                                        and isinstance(records.get("entity"), str)
                                        and isinstance(records.get("where", []), list)):
            raise ValueError("a convention's records name an entity and a list of conditions")
        where = (records or {}).get("where", [])
        if not all(isinstance(w, dict) and isinstance(w.get("field"), str)
                   and isinstance(w.get("values"), list) for w in where):
            raise ValueError("a condition is a field and its values")
        out.append(IndexConvention(
            terms=tuple(entry["terms"]), meaning=str(entry.get("meaning", "")),
            entity=(records or {}).get("entity", ""),
            conditions=tuple((w["field"], tuple(w["values"])) for w in where)))
    return tuple(out)


__all__ = ["load_conventions"]
