"""Actor-visible projection used to verify a reset from the application's side.

The fixture guard proves the database and filestore are byte-for-byte back; this projection
proves each synthetic actor again sees exactly its baseline -- same records, same key fields --
through ordinary JSON-RPC under its own API key. Only digests leave this module.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .population import digest_json
from .rpc import OdooActorClient

COLLECTIONS = (
    ("res.partner", [["customer_rank", ">", 0]], ["name", "email", "phone", "city", "vat"]),
    ("product.template", [["sale_ok", "=", True]], ["name", "default_code", "list_price"]),
    ("sale.order", [], ["name", "state", "partner_id", "amount_total", "currency_id",
                        "invoice_status", "date_order", "client_order_ref"]),
    ("account.move", [["move_type", "=", "out_invoice"]],
     ["name", "state", "partner_id", "amount_total", "amount_residual", "invoice_origin"]),
)


def actor_projection(rpc_url: str, key_files: dict[str, Path]) -> dict[str, Any]:
    projection: dict[str, Any] = {}
    for actor, key_file in sorted(key_files.items()):
        client = OdooActorClient(base_url=rpc_url, api_key_file=key_file)
        views = {}
        for model, domain, fields in COLLECTIONS:
            rows = client.search_read(model, domain, fields)
            views[model] = {"count": len(rows), "digest": digest_json(rows)}
        projection[actor] = views
    return projection
