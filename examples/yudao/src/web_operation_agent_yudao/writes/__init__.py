"""Native submit, convention writes, form entries, prefill, and write definitions."""
from __future__ import annotations

import importlib

_EXPORTS = {
    "BACKEND_WRITE_ATOMICITY": ".write_definitions",
    "FenceEvidence": ".live_forms",
    "LiveFormPort": ".live_forms",
    "YUDAO_SUBMIT_CONVENTIONS": ".native_submit",
    "YudaoConventionWritePort": ".convention_writes",
    "YudaoMutationPrefill": ".mutation_prefill",
    "YudaoSubmitPort": ".native_submit",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_name, __name__)
    return getattr(module, name)
