"""Typed business-tool failures preserved across the server layers."""
from __future__ import annotations


class BusinessToolError(RuntimeError):
    """A safe, expected refusal or read failure that the Agent may reason about."""

    code = "business_tool"


class BusinessToolRequestError(BusinessToolError):
    """The requested tool call is invalid or is not permitted for this turn."""

    code = "business_request"


class BusinessToolReferenceError(BusinessToolRequestError):
    """A write target could not be resolved before any mutation was dispatched.

    The adapter supplies the actor-visible entity and current candidate displays so a
    host may offer a bounded clarification.  This remains a request error: it never
    authorizes a retry and is safe to expose only before approval/dispatch.
    """

    code = "business_reference"

    def __init__(self, message: str, *, entity: str, choices: tuple[str, ...] = ()):
        super().__init__(message)
        if not isinstance(entity, str) or not entity or len(entity) > 200:
            raise ValueError("reference entity is invalid")
        if (not isinstance(choices, tuple) or len(choices) > 6
                or any(not isinstance(c, str) or not c or len(c) > 500 for c in choices)
                or len(set(choices)) != len(choices)):
            raise ValueError("reference choices are invalid")
        self.entity = entity
        self.choices = choices


class BusinessToolIdentityError(BusinessToolError):
    """The application credential or bound principal cannot authorize the call."""

    code = "business_identity"


class BusinessToolAuthenticationError(BusinessToolIdentityError):
    """The delegated application credential is missing, expired, or rejected."""

    code = "business_authentication"


class BusinessToolPermissionError(BusinessToolError):
    """An authenticated actor is not permitted to perform this operation.

    Distinct from an identity or authentication failure: the credential works and names a
    real actor, and that actor's own authority refuses the operation. `layer` says who
    refused -- the framework's per-tool scope check before anything reached the application,
    or the application's own permission system after it did. A mutation adapter may raise
    this only when the application guarantees that the refused call made no business change;
    an uncertain or partially applied refusal is ``BusinessMutationIndeterminate`` instead.
    """

    code = "business_permission_denied"
    LAYERS = frozenset({"tool_authorization", "application"})

    def __init__(self, message: str, *, layer: str = "application",
                 required_scope: str = ""):
        super().__init__(message)
        if layer not in self.LAYERS:
            raise ValueError("permission refusal layer is invalid")
        self.layer = layer
        self.required_scope = (required_scope if isinstance(required_scope, str)
                               and len(required_scope) <= 100 else "")


class BusinessToolUnavailableError(BusinessToolError):
    """A valid read could not reach the application service."""

    code = "business_unavailable"


class BusinessToolDispatchError(BusinessToolUnavailableError):
    """A mutating request reached transport but returned no safe outcome."""

    code = "business_dispatch_unknown"


class BusinessToolContractError(BusinessToolError):
    """The application integration returned data outside its declared contract."""

    code = "business_contract"


class BusinessMutationBlockedError(BusinessToolError):
    """An unresolved or in-flight business key blocks another write."""

    code = "business_mutation_blocked"

    def __init__(self, message: str, *, reason: str, state: str = ""):
        super().__init__(message)
        self.reason = reason
        self.state = state


class BusinessMutationRefusedError(BusinessToolError):
    """A definite application refusal with proof of no persistent effect.

    This is distinct from ``BusinessMutationBlockedError``: a refusal such as a
    duplicate record is a settled business answer, while a blocked mutation points
    at an earlier operation that still needs reconciliation.
    """

    code = "business_mutation_refused"

    def __init__(self, message: str, *, reason: str, subject: str = ""):
        super().__init__(message)
        if (not isinstance(reason, str) or not reason.strip()
                or len(reason) > 100):
            raise ValueError("business mutation refusal reason is invalid")
        if not isinstance(subject, str) or len(subject) > 200:
            raise ValueError("business mutation refusal subject is invalid")
        self.reason = reason.strip()
        #: The record the rule refused, when there is one. "This order has nothing to invoice"
        #: does not tell a reader which order, and a reader who asked about one order needs to
        #: see that order named in the answer to know the refusal is about what they asked.
        self.subject = subject.strip()


class BusinessMutationIndeterminate(RuntimeError):
    """A dispatched mutation may have landed and therefore must never be retried.

    An application raising this usually knows more than the message says. When an upstream
    acknowledgement returned a record identifier and only the readback then failed, that
    identifier settles the row with one authorized read; without it an operator has to scan
    the whole collection. `phase` and `record_id` carry that forward to the durable journal
    and the redacted audit ledger, and nowhere else.
    """

    def __init__(self, message: str, *, phase: str = "", record_id: str = "",
                 reconciliation: dict | None = None):
        super().__init__(message)
        self.phase = phase if isinstance(phase, str) and len(phase) <= 64 else ""
        self.record_id = (record_id if isinstance(record_id, str) and len(record_id) <= 200
                          else "")
        import json
        from .tools.contracts import _has_sensitive_key
        if reconciliation is not None and (not isinstance(reconciliation, dict)
                                           or _has_sensitive_key(reconciliation)):
            raise ValueError("unsafe indeterminate reconciliation")
        raw = json.dumps(reconciliation or {}, ensure_ascii=False, allow_nan=False)
        if len(raw.encode()) > 8000:
            raise ValueError("indeterminate reconciliation exceeds 8 KiB")
        self._reconciliation_json = raw

    @property
    def reconciliation(self) -> dict:
        """The bounded post-dispatch facts, or an empty mapping when there are none."""
        import json
        return {**json.loads(self._reconciliation_json), **{key: value for key, value in
                (("phase", self.phase), ("record_id", self.record_id)) if value}
                }
