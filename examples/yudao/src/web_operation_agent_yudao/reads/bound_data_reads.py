"""Bind the readonly data resolver to an exact Yudao delegated credential generation."""
from contextlib import contextmanager
import threading

from web_operation_agent.business.errors import (
    BusinessToolAuthenticationError,
    BusinessToolContractError,
    BusinessToolIdentityError,
    BusinessToolPermissionError,
    BusinessToolUnavailableError,
)

from .client import YudaoReadRefused

_TURN_ACTOR_LOCAL = threading.local()


def current_turn_read_scope() -> dict | None:
    """Return the active synchronous turn read scope on this thread, if any."""
    scope = getattr(_TURN_ACTOR_LOCAL, "scope", None)
    if isinstance(scope, dict) and scope.get("thread_id") == threading.get_ident():
        return scope
    return None


@contextmanager
def turn_actor_reads(client, turn_context=None, *, seed_scope=None, knowledge=None):
    """Reuse identity matching, never permissions, within one synchronous slim turn.

    The delegated token names one actor. Renewing it invalidates the whole turn;
    every native GET still authenticates and authorizes at the application.
    Thread-local state cannot cross another thread or survive this scope.
    """
    generation = client.read_generation
    binding = None
    if (isinstance(seed_scope, dict)
            and seed_scope.get("client") is client
            and seed_scope.get("generation") == generation
            and seed_scope.get("binding") is not None):
        binding = seed_scope["binding"]
    if knowledge is None and isinstance(seed_scope, dict):
        knowledge = seed_scope.get("knowledge")
    state = {
        "client": client,
        "generation": generation,
        "binding": binding,
        "thread_id": threading.get_ident(),
        "turn_context": turn_context,
        "knowledge": knowledge,
    }
    previous_local = getattr(_TURN_ACTOR_LOCAL, "scope", None)
    _TURN_ACTOR_LOCAL.scope = state
    previous_ctx = None
    if turn_context is not None:
        previous_ctx = getattr(turn_context, "actor_reads", None)
        turn_context.actor_reads = state
    try:
        yield
    finally:
        state.clear()
        _TURN_ACTOR_LOCAL.scope = previous_local
        if turn_context is not None:
            turn_context.actor_reads = previous_ctx


class BoundDataReads:
    def __init__(self, data, context, checkpoint, *, turn_context=None):
        self._data, self._context, self._checkpoint = data, context, checkpoint
        self._client = data._client
        self._generation = self._client.read_generation
        self._check()
        scope = None
        if turn_context is not None:
            candidate = getattr(turn_context, "actor_reads", None)
            if isinstance(candidate, dict) and candidate.get("thread_id") == threading.get_ident():
                scope = candidate
        if scope is None:
            scope = getattr(_TURN_ACTOR_LOCAL, "scope", None)
        binding = context.principal.binding_digest, context.session_id, context.turn_id
        if scope is not None:
            if (scope.get("client") is not self._client
                    or scope["generation"] != self._generation
                    or scope["binding"] not in (None, binding)):
                raise BusinessToolIdentityError("the delegated read binding changed within this turn")
            if scope["binding"] == binding:
                return
        info = self._invoke(self._client.get, "system/auth/get-permission-info")
        if not isinstance(info, dict):
            raise BusinessToolContractError("the application returned an invalid actor assertion")
        user = info.get("user")
        principal = context.principal
        if (not isinstance(user, dict) or str(user.get("id")) != principal.actor_id
                or str(user.get("deptId") or "none") != principal.workspace_id
                or str(self._client.tenant) != principal.tenant_id):
            raise BusinessToolIdentityError("the application data actor differs from the delegated principal")
        self._check()
        if scope is not None:
            scope["binding"] = binding

    def _check(self):
        self._checkpoint()
        self._context.require_live(application_id="yudao", required_scope="yudao.read")
        if self._client.read_generation != self._generation:
            raise BusinessToolIdentityError("the application credential changed during the data read")

    def __getattr__(self, name):
        method = getattr(self._data, name)
        if not callable(method):
            return method

        def call(*args, **kwargs):
            return self._invoke(method, *args, **kwargs)

        return call

    def _invoke(self, method, *args, **kwargs):
        self._check()
        try:
            with self._client.read_window(self._check):
                result = method(*args, **kwargs)
        except YudaoReadRefused as exc:
            if exc.code == 401:
                raise BusinessToolAuthenticationError("the application sign-in expired") from exc
            if exc.code == 403:
                raise BusinessToolPermissionError("the application denied this read") from exc
            raise BusinessToolUnavailableError("the application data read is unavailable") from exc
        self._check()
        return result
