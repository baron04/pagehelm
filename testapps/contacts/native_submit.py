"""Contacts' side of the native submit contract: deliberately different declarations.

The port is the part of the contract an application owns: which interfaces belong to the
record the page is working on, how to read that record as the signed-in person, what its
record conventions are (this app's differ from Yudao's on purpose), and which of the
person's own writes the application accepted. Nothing here names the agent.
"""
from __future__ import annotations

import re

from web_operation_agent.execution.submit_contract import (ActionRequest,
                                                           SubmitConventions)

from datetime import timezone

_PREFIX = "/api/contacts"
_CREATE = ("POST", _PREFIX)
_UPDATE = ("PUT", _PREFIX)
_DANGER = re.compile(r"删除|移除|清除|作废|delete|remove|trash|clear", re.IGNORECASE)


def _decode_write(status, body):
    """Contacts' responses: errors are always non-2xx; a create answers with the new
    record under ``contact`` and its ``id`` — no status envelope or `code` field."""
    import json
    answer = None
    if body and status is not None and 200 <= status < 300:
        try:
            envelope = json.loads(body.decode("utf-8"))
            record = envelope.get("contact") if isinstance(envelope, dict) else None
            if isinstance(record, dict) and record.get("id") not in (None, ""):
                answer = record["id"]
        except (UnicodeDecodeError, ValueError):
            pass
    return status, answer


CONTACTS_SUBMIT_CONVENTIONS = SubmitConventions(
    # The application stamps its own bookkeeping fields; the identity fields are not
    # "id" in requests or in lines, and epoch-millisecond values are UTC instants.
    volatile_keys=frozenset({"modified", "modifiedBy"}),
    record_key="contactId", line_key="lineId",
    epoch_zone=timezone.utc,
    decode_write=_decode_write)


class ContactsSubmitPort:
    """`SubmitPort` for one signed-in person's session over the contacts REST surface.

    `client` is the application's read interface (``.get(path, **params)``); `journal`
    is the application's accepted user-write journal: dicts of ``method``, ``path``,
    ``record_id`` for the targeted record, and ``created_id`` for a created record.
    """

    def __init__(self, client, journal=()):
        self.client, self._journal = client, journal
        self.armed, self._reads = None, ()

    def contract(self):
        return CONTACTS_SUBMIT_CONVENTIONS

    # -- what the page itself read -----------------------------------------------------
    def recent_reads(self):
        return self._reads

    def note_read(self, path, query=""):
        """Record one read the page made; the form's entity comes from its own source."""
        self._reads = (*self._reads, (path, query))

    def prefixes(self, reads, *, mode, record_id, source=""):
        """The contacts collection is the only write surface; an edit needs the list
        read that carries the record, a create needs the same observed list."""
        if not isinstance(source, str) or source != _PREFIX:
            return ()
        if not any(path == source for path, _ in reads):
            return ()
        if mode == "update" and not record_id:
            return ()
        return (_PREFIX,)

    def row_prefixes(self, reads, row):
        source = row.get("source")
        if source != _PREFIX or not any(path == source for path, _ in reads):
            return ()
        return (_PREFIX,)

    def row_request(self, prefixes, row, element, record):
        """A row's documented action: the application's own delete endpoint."""
        record_id = str((record or {}).get("id") or row.get("record_id") or "")
        if not record_id or _PREFIX not in prefixes:
            return None
        return ActionRequest("POST", f"/api/browser/contacts/{record_id}/delete", {})

    def catalog(self, prefixes):
        """The application's documented writes under the contacts surface."""
        if _PREFIX not in prefixes:
            return frozenset()
        found = {_CREATE, _UPDATE}
        for item in self._records():
            if item.get("id"):
                found.add(("POST", f"/api/browser/contacts/{item['id']}/delete"))
        return frozenset(found)

    # -- the record, as the same person -------------------------------------------------
    def _records(self):
        data = self.client.get(_PREFIX)
        return data.get("contacts") or () if isinstance(data, dict) else ()

    def read_record(self, prefixes, record_id):
        if not record_id or _PREFIX not in prefixes:
            return None
        for item in self._records():
            if str(item.get("id")) == str(record_id):
                return item
        return None

    # -- what the person may not be asked to approve ------------------------------------
    def dangerous(self, element):
        labels = " ".join(str(value) for value in (element.name,
                          element.attrs.get("title"), element.attrs.get("aria-label")) if value)
        return bool(_DANGER.search(labels) or not labels.strip())

    # -- the armed permit ---------------------------------------------------------------
    def arm(self, permit):
        self.armed = permit

    def disarm(self):
        self.armed = None

    def settled(self):
        pass

    # -- the person's own press after a handoff -------------------------------------------
    def user_write_mark(self):
        return len(self._journal)

    def user_write_event(self, prefixes, record_id, mark, create=False):
        """The first accepted user write under `prefixes` after `mark`, or None."""
        for seq, entry in enumerate(self._journal):
            if seq < mark:      # entry at index i is the (i + 1)-th accepted write
                continue
            path, method = (str(entry.get("path") or ""),
                            str(entry.get("method") or "").upper())
            if not any(path.startswith(prefix) for prefix in prefixes):
                continue
            wanted, created = (str(entry.get("record_id") or ""),
                               str(entry.get("created_id") or ""))
            if record_id and record_id not in {wanted, created}:
                continue
            if create and (method, path.split("?", 1)[0]) != _CREATE:
                continue
            return {"seq": seq, "method": method, "path": path,
                    "record_id": created or wanted or record_id}
        return None
