"""What an organisation using Odoo means by some of its words, as its deployment declares.

The goal index shows a convention to the model when the request says one of its words
("订单" -> every sales order, quotations included). Conventions are the organisation's,
not Odoo's: two companies on the same Odoo say "订单" and mean different things, so they
are written down by the deployment and reviewed by its owner, never inferred from the
application. `reference.json` is the reference deployment's.
"""
from __future__ import annotations

import json
from pathlib import Path

from web_operation_agent.goal.index import IndexConvention

#: The reference deployment's conventions.
REFERENCE_CONVENTIONS = Path(__file__).with_name("reference.json")


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
        out.append(IndexConvention(terms=tuple(entry["terms"]), meaning=str(entry.get("meaning", ""))))
    return tuple(out)


__all__ = ["REFERENCE_CONVENTIONS", "load_conventions"]
