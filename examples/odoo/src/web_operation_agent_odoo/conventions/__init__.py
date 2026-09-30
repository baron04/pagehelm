"""What an organisation using Odoo means by some of its words, as its deployment declares.

The goal index shows a convention to the model when the request says one of its words
("订单" -> every sales order, quotations included). Conventions are the organisation's,
not Odoo's: two companies on the same Odoo say "订单" and mean different things, so they
are written down by the deployment and reviewed by its owner, never inferred from the
application. `reference.json` is the reference deployment's.
"""
from __future__ import annotations

from pathlib import Path

from web_operation_agent.goal.conventions import load_conventions

#: The reference deployment's conventions.
REFERENCE_CONVENTIONS = Path(__file__).with_name("reference.json")


__all__ = ["REFERENCE_CONVENTIONS", "load_conventions"]
