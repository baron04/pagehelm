"""Reviewed editor navigation; business-value entry remains owned by FormPort."""
import hashlib

from web_operation_agent.business.errors import BusinessToolRequestError
from web_operation_agent.execution.action_admission import (
    ActionAdmissionRefused, ActionEffect, ActionPermit, ActionResultPending)
from .form_entries import NativeEntryFormPort, NativeCreateEntryVerifier, _location, _TARGET


class NativeFormOpenActions:
    def __init__(self, port):
        if not isinstance(port, NativeEntryFormPort):
            raise TypeError("editor navigation requires the reviewed native FormPort")
        self.port = port
        self._issued = {}
        self._turn = None

    def _verifier(self):
        return getattr(self.port._verifier, "active", self.port._verifier)

    def matches(self, intent):
        verifier = self._verifier()
        planned = verifier._planned if verifier is not None else None
        return (intent.action == "click" and planned is not None
                and intent.arguments["ref"] == planned[0].control.ref)

    @staticmethod
    def _authorize(verifier, entry, context, checkpoint):
        checkpoint()
        record = "" if isinstance(verifier, NativeCreateEntryVerifier) else _TARGET.fullmatch(entry.reference)[1]
        verifier._authorize(record, context)
        checkpoint()

    def admit(self, intent, *, context, checkpoint):
        verifier = self._verifier()
        planned = verifier._planned if verifier is not None else None
        before = intent.observation
        if (not self.matches(intent) or planned[:4] != (planned[0], context.principal.binding_digest,
                context.session_id, context.turn_id) or planned[0].control not in before.elements
                or before.record_route != planned[5] or _location(before.url) != planned[4]):
            raise ActionAdmissionRefused("stale_binding")
        self._authorize(verifier, planned[0], context, checkpoint)
        turn = context.principal.binding_digest, context.session_id, context.turn_id
        if turn != self._turn:
            self._issued.clear(); self._turn = turn
        self._issued[intent.binding_digest] = verifier, planned
        review = hashlib.sha256(repr(verifier.review).encode()).hexdigest()
        return ActionPermit(intent.binding_digest, ActionEffect.NAVIGATION,
                            "reviewed-transient-editor:" + review, "exact-guarded-form-entry")

    def verify(self, permit, intent, receipt, *, observation, context, checkpoint):
        issued = self._issued.get(permit.binding_digest)
        if issued is None:
            return False
        verifier, planned = issued
        if (self._verifier() is not verifier or verifier._planned != planned
                or observation.record_route != planned[5]
                or _location(observation.url) != planned[4]):
            return False
        self._authorize(verifier, planned[0], context, checkpoint)
        # A synchronous click may precede mounting the reviewed dialog. Retain the
        # original receipt; the core allows only guarded observation until it settles.
        if not any(str(e.attrs.get("form_scope", "")).startswith("dialog")
                   for e in observation.elements):
            raise ActionResultPending()
        try:
            return verifier.verify_entry(planned[0], intent.observation, receipt,
                                         observation, context=context)
        except BusinessToolRequestError:
            return False
