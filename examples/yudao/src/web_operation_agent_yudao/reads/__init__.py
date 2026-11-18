"""Actor-bound reads, client, read tables, summaries, and filter/money review."""
from __future__ import annotations

import importlib

_EXPORTS = {
    "BoundDataReads": ".bound_data_reads",
    "TurnReadError": ".turn_tables",
    "TurnTables": ".turn_tables",
    "YudaoAccount": ".client",
    "YudaoClient": ".client",
    "YudaoReadRefused": ".client",
    "date_range": ".turn_tables",
    "turn_actor_reads": ".bound_data_reads",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_name, __name__)
    return getattr(module, name)
