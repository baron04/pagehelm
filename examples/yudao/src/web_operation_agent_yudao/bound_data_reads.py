"""Bind the readonly data resolver to an exact Yudao delegated credential generation."""
from web_operation_agent.business.errors import (BusinessToolAuthenticationError,
    BusinessToolContractError, BusinessToolIdentityError, BusinessToolPermissionError, BusinessToolUnavailableError)
from .client import YudaoReadRefused


class BoundDataReads:
    def __init__(self, data, context, checkpoint):
        self._data, self._context, self._checkpoint = data, context, checkpoint
        self._client = data._client
        self._generation = self._client.read_generation
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

    def _check(self):
        self._checkpoint()
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
