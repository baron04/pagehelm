"""Open and close the read-only window around the Agent's page actions.

Plain JSON over POST, same origin only: a cross-site page can neither send
`application/json` without a preflight nor pass the `Sec-Fetch-Site` check, so it cannot
close the window while the Agent is acting.
"""
from __future__ import annotations

import json
import math

from odoo import http
from odoo.http import request

from ..models.page_guard import MAX_TTL_S


def _json(payload, status=200):
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return request.make_response(body, status=status, headers=[
        ("Content-Type", "application/json; charset=utf-8"),
        ("Cache-Control", "no-store"), ("Content-Length", str(len(body)))])


def _same_origin_json():
    site = request.httprequest.headers.get("Sec-Fetch-Site")
    if site not in (None, "same-origin"):
        return None
    if request.httprequest.mimetype != "application/json":
        return None
    value = request.httprequest.get_json(silent=True)
    return value if isinstance(value, dict) else None


class WoaPageGuard(http.Controller):
    @http.route("/woa/page-guard/begin", type="http", auth="user", methods=["POST"],
                csrf=False)
    def begin(self):
        body = _same_origin_json()
        ttl = body.get("ttl_s") if body is not None else None
        if (isinstance(ttl, bool) or not isinstance(ttl, (int, float))
                or not math.isfinite(ttl) or not 1 <= ttl <= MAX_TTL_S):
            return _json({"error": "invalid_page_guard_request"}, status=400)
        expires_at = request.env["woa.page.guard"]._open(float(ttl))
        return _json({"active": True, "expires_at": expires_at,
                      "guard": "read_only_transaction"})

    @http.route("/woa/page-guard/end", type="http", auth="user", methods=["POST"],
                csrf=False)
    def end(self):
        if _same_origin_json() is None:
            return _json({"error": "invalid_page_guard_request"}, status=400)
        request.env["woa.page.guard"]._close()
        return _json({"active": False})

    @http.route("/woa/page-guard/state", type="http", auth="user", methods=["POST"],
                csrf=False, readonly=True)
    def state(self):
        if _same_origin_json() is None:
            return _json({"error": "invalid_page_guard_request"}, status=400)
        return _json(request.env["woa.page.guard"]._state())
