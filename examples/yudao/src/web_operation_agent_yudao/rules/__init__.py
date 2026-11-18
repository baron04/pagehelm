"""Compiled Java/UI source rules, repository source, playbooks, and Skill packages."""
from __future__ import annotations

import importlib

_EXPORTS = {
    "EntitySource": ".source",
    "ListView": ".ui_source",
    "UiSource": ".ui_source",
    "YudaoSource": ".source",
    "domain_playbooks": ".skill_packages",
    "packaged_skills": ".skill_packages",
    "repository_source": ".repository",
    "repository_tree": ".repository",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_name, __name__)
    return getattr(module, name)
