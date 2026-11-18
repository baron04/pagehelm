"""Goal-port compatibility adapters for Yudao menus, pages, and forms."""
from __future__ import annotations

import importlib

_EXPORTS = {
    "LiveFormsGoalPort": ".live_goals",
    "NativeFormsGoalPort": ".form_goals",
    "NativePageGoalPort": ".page_goals",
    "YudaoGoalData": ".goal",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_name, __name__)
    return getattr(module, name)
