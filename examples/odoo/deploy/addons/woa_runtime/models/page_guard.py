"""A read-only window the WOA Runtime opens on the signed-in session while the Agent acts.

The Agent operates the page through the person's own Odoo session, so a click the reviewed
click list should never have allowed -- or an autosave the web client makes on its own --
would write with that person's rights. Before every page action the Runtime opens this
window (`/woa/page-guard/begin`); while it is open, every request that session makes runs
in a PostgreSQL read-only transaction, and a request that tries to change data is refused
with nothing saved. The Runtime closes the window when the turn ends, and it closes itself
after at most `MAX_TTL_S` seconds.

What this guards: database writes from requests carrying this browser session. What it
does not guard: the typed business tools, which run through their own API credentials and
the product's proposal-and-confirmation protocol; other sessions of the same person; and
server code that causes an external effect without writing to the database first.
"""
from __future__ import annotations

import hashlib
import logging
import time

import psycopg2

from odoo import models
from odoo.exceptions import UserError
from odoo.http import request


_logger = logging.getLogger(__name__)

MAX_TTL_S = 120
#: Requests the window never applies to: the window's own routes, derived asset bundles
#: (a cache the web client needs to render any page), and the bus socket, whose messages
#: only subscribe and report presence.
EXEMPT_PREFIXES = ("/woa/page-guard/", "/web/assets/", "/websocket")
REFUSAL = ("The assistant is operating this page, so changes are paused and nothing was "
           "saved. Try again after the assistant finishes.")


def session_digest(sid: str) -> str:
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


_TABLE_READY: set[str] = set()


def _table_ready(cr) -> bool:
    """True once this database has the window table.

    Code of a newer module version is loaded before the module is upgraded; until then no
    window can have been opened, and querying the absent table would fail every request.
    """
    if cr.dbname not in _TABLE_READY:
        cr.execute("SELECT to_regclass('woa_page_guard') IS NOT NULL")
        if not cr.fetchone()[0]:
            return False
        _TABLE_READY.add(cr.dbname)
    return True


def _guarded_request() -> bool:
    if not request or not request.db or not request.session.uid:
        return False
    if request.httprequest.path.startswith(EXEMPT_PREFIXES):
        return False
    cr = request.env.cr
    if not _table_ready(cr):
        return False
    cr.execute("SELECT expires_at FROM woa_page_guard WHERE sid_digest = %s",
               [session_digest(request.session.sid)])
    row = cr.fetchone()
    return bool(row and row[0] > time.time())


class WoaPageGuard(models.AbstractModel):
    _name = "woa.page.guard"
    _description = "WOA page write guard"

    def init(self):
        self.env.cr.execute("""
            CREATE TABLE IF NOT EXISTS woa_page_guard (
                sid_digest varchar(64) PRIMARY KEY,
                uid integer NOT NULL,
                expires_at double precision NOT NULL
            )
        """)

    def _open(self, ttl_s: float) -> float:
        expires_at = time.time() + ttl_s
        self.env.cr.execute("""
            INSERT INTO woa_page_guard (sid_digest, uid, expires_at) VALUES (%s, %s, %s)
            ON CONFLICT (sid_digest) DO UPDATE
                SET uid = EXCLUDED.uid, expires_at = EXCLUDED.expires_at
        """, [session_digest(request.session.sid), request.session.uid, expires_at])
        # Windows left behind by closed tabs are swept here rather than by a cron.
        self.env.cr.execute("DELETE FROM woa_page_guard WHERE expires_at < %s",
                            [time.time() - MAX_TTL_S])
        return expires_at

    def _close(self) -> None:
        self.env.cr.execute("DELETE FROM woa_page_guard WHERE sid_digest = %s",
                            [session_digest(request.session.sid)])

    def _state(self) -> dict:
        self.env.cr.execute("SELECT expires_at FROM woa_page_guard WHERE sid_digest = %s",
                            [session_digest(request.session.sid)])
        row = self.env.cr.fetchone()
        active = bool(row and row[0] > time.time())
        return {"active": active, "expires_at": row[0] if active else None}


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _dispatch(cls, endpoint):
        if not _guarded_request():
            return super()._dispatch(endpoint)
        cr = request.env.cr
        cr.execute("SET TRANSACTION READ ONLY")
        try:
            result = super()._dispatch(endpoint)
            # Pending ORM writes reach the database here, inside the window, rather than
            # at the commit after it.
            request.env.flush_all()
            return result
        except psycopg2.errors.ReadOnlySqlTransaction:
            cr.rollback()
            cr.execute("SET TRANSACTION READ ONLY")
            _logger.info("WOA page guard refused a write: %s", request.httprequest.path)
            raise UserError(REFUSAL) from None
