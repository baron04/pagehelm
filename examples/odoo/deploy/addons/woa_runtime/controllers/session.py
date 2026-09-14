"""Resolve an Odoo login into a signed, short-lived Agent session event."""
from __future__ import annotations

import json
import logging
import os
import urllib.request

from odoo import http
from odoo.addons.web.controllers.session import Session
from odoo.http import request

from ..claims import session_event, signed_envelope


_logger = logging.getLogger(__name__)
_SECRET_FILE_ENV = "WOA_SESSION_SECRET_FILE"
_REVOKE_URL_ENV = "WOA_AGENT_REVOKE_URL"


def _secret():
    path = os.environ.get(_SECRET_FILE_ENV, "")
    if not path:
        raise RuntimeError("the WOA session secret is not configured")
    with open(path, "rb") as source:
        value = source.read(4097)
    if len(value) < 32 or len(value) > 4096:
        raise RuntimeError("the WOA session secret has an invalid size")
    return value


def _event(kind, runtime_instance_id=""):
    uid = request.session.uid
    if not uid:
        raise RuntimeError("an authenticated Odoo session is required")
    return session_event(
        event=kind,
        database=request.db,
        company_id=request.env.company.id,
        actor_id=uid,
        runtime_instance_id=runtime_instance_id,
    )


def _response(payload, status=200):
    body = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    headers = [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Cache-Control", "no-store"),
        ("Content-Length", str(len(body))),
    ]
    return request.make_response(body, headers=headers, status=status)


class WoaSessionAssertion(http.Controller):
    @http.route(
        "/woa/session-assertion", type="http", auth="user",
        methods=["POST"], csrf=False, readonly=True,
    )
    def session_assertion(self):
        try:
            value = request.httprequest.get_json(silent=True)
            instance = value.get("runtime_instance_id") if isinstance(value, dict) else None
            envelope = signed_envelope(
                _event("bootstrap", runtime_instance_id=instance), _secret())
        except (OSError, RuntimeError, ValueError, TypeError):
            _logger.exception("WOA session assertion failed")
            return _response({"error": "assistant_identity_unavailable"}, status=503)
        return _response(envelope)


def _notify_revocation(envelope):
    url = os.environ.get(_REVOKE_URL_ENV, "")
    if not url:
        return
    body = json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    outgoing = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(outgoing, timeout=1) as response:
            response.read(1024)
    except Exception:  # Logout must still succeed when the optional assistant is down.
        _logger.warning("WOA revocation notification failed", exc_info=True)


class WoaSessionLifecycle(Session):
    @http.route()
    def destroy(self):
        try:
            envelope = signed_envelope(_event("revoke"), _secret())
        except Exception:
            envelope = None
        if envelope is not None:
            _notify_revocation(envelope)
        return super().destroy()

    @http.route()
    def logout(self, redirect="/odoo"):
        try:
            envelope = signed_envelope(_event("revoke"), _secret())
        except Exception:
            envelope = None
        if envelope is not None:
            _notify_revocation(envelope)
        return super().logout(redirect=redirect)
