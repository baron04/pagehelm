"""Release-bound raw source search, independent of the legacy rule compiler.

Only reviewed code and documentation locations enter the index. It is shared by the host's
sessions and built once at startup; neither complete repositories nor local paths enter
the model prompt. Every retrieved window retains its exact-byte citation.
"""
from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.planning.repository_source import RepositorySourceLookup
from web_operation_agent.planning.sourcetree import SourceTree


def repository_tree(root: Path, release: str) -> SourceTree:
    """The same release/allowlist boundary for search and on-demand compilation."""
    if not release.strip():
        raise ValueError("repository source requires the deployed release")
    policy = json.loads(files("web_operation_agent_yudao").joinpath(
        "policies", "repository.json").read_text(encoding="utf-8"))
    policy["release"] = release
    tree = SourceTree(policy, root.resolve())
    if not tree.available():
        raise ValueError("configured repository source is unavailable")
    return tree


def repository_source(root: Path | None, release: str, *,
                      commits: tuple[tuple[str, str], ...] = ()) -> SourceCapability:
    if root is None:
        if commits:
            raise ValueError("source commits require a configured repository source root")
        return SourceCapability.none()
    tree = repository_tree(root, release)
    lookup = RepositorySourceLookup(tree, line_limit=24, char_limit=1800, overlap=4)
    history = None
    if commits:
        from .repository_history import YudaoRepositoryHistory
        history = YudaoRepositoryHistory(tree, commits)
    return SourceCapability(
        runtime_source=True, lookup=lookup, history=history,
        history_unavailable_reason=(
            "this deployment supplies source snapshots, not Git history bound to a deployed commit; "
            "application operation logs do not identify code changes"))


__all__ = ["repository_source", "repository_tree"]
