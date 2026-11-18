"""Yudao application host, server, guards, accounts, and runtime turn entrypoints."""
from __future__ import annotations

import importlib

_EXPORTS = {
    "GuardIdentity": ".guard_identity",
    "GuardRefused": ".page_guard",
    "NativeGuardProxy": ".guard_proxy",
    "NativePageGuard": ".page_guard",
    "ValidatedNativeDeployment": ".deployment_validation",
    "YudaoApplication": ".application",
    "YudaoHost": ".native_server",
    "YudaoHostConfig": ".native_server",
    "YudaoMutationReconciler": ".reconciliation",
    "validate_native_deployment": ".deployment_validation",
    "yudao_turn_config": ".turn",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_name, __name__)
    return getattr(module, name)
