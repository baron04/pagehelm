"""Bind the readonly data resolver to an exact Yudao delegated credential generation."""
from web_operation_agent.business.errors import (BusinessToolAuthenticationError,
    BusinessToolContractError, BusinessToolIdentityError, BusinessToolPermissionError, BusinessToolUnavailableError)
from contextlib import contextmanager
from contextvars import ContextVar

from .client import YudaoReadRefused


_TURN_ACTOR = ContextVar("yudao_turn_actor", default=None)


@contextmanager
def turn_actor_reads(client):
    """Reuse identity matching, never permissions, within one synchronous slim turn.

    The delegated token names one actor. Renewing it invalidates the whole turn;
    every native GET still authenticates and authorizes at the application.
    Context-local state cannot cross another thread or survive this scope.
    """
    state = {"client": client, "generation": client.read_generation, "binding": None}
    marker = _TURN_ACTOR.set(state)
    try:
        yield
    finally:
        state.clear()
        _TURN_ACTOR.reset(marker)


class BoundDataReads:
    def __init__(self, data, context, checkpoint):
        self._data, self._context, self._checkpoint = data, context, checkpoint
        self._client = data._client
        self._generation = self._client.read_generation
        self._check()
        scope = _TURN_ACTOR.get()
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
