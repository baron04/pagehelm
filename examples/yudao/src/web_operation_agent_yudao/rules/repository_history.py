"""Bind the deployment's separate UI/server repositories to their actual commits."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from itertools import zip_longest

from web_operation_agent.planning.source_history import GitSourceHistory, SourceHistoryHit
from web_operation_agent.planning.sourcetree import SourceTree


class YudaoRepositoryHistory:
    def __init__(self, tree: SourceTree, commits: tuple[tuple[str, str], ...]):
        if (not isinstance(commits, tuple) or not commits
                or any(not isinstance(item, tuple) or len(item) != 2
                       or item[0] not in {"root", "ui", "server"}
                       or not isinstance(item[1], str) for item in commits)
                or len({name for name, _ in commits}) != len(commits)
                or any(name == "root" for name, _ in commits) and len(commits) != 1):
            raise ValueError("source commits require distinct UI/server repositories or one root repository")
        self.release = tree.release
        self._repositories = {}
        for name, commit in commits:
            prefix = "" if name == "root" else name + "/"
            root = (tree.root / prefix).resolve()
            if not root.is_relative_to(tree.root):
                raise ValueError("a repository root cannot leave the source allowlist boundary")
            policy = dict(tree.policy, repository_id=tree.repository_id + "/" + name,
                allow_globs=[pattern[len(prefix):] for pattern in tree.allow
                             if pattern.startswith(prefix)],
                deny_globs=[pattern[len(prefix):] if pattern.startswith(prefix) else pattern
                            for pattern in tree.deny])
            self._repositories[prefix] = GitSourceHistory(SourceTree(policy, root), commit)
        self.deployment_commits = tuple((provider.repository_id, provider.deployed_commit)
                                       for provider in self._repositories.values())
        self.deployed_commit = (self.deployment_commits[0][1]
                                if len(self.deployment_commits) == 1 else None)

    def search(self, kind: str, *, path: str = "", start_line: int = 0,
               end_line: int = 0, target_commit: str = "", limit: int = 8):
        if not isinstance(path, str) or path.startswith("/") or ".." in path.split("/"):
            raise ValueError("history paths must be repository-relative")
        repositories = [(prefix, provider) for prefix, provider in self._repositories.items()
                        if path.startswith(prefix) or not path and kind == "changes"]
        if not repositories:
            raise ValueError("choose a configured repository with a UI/server-prefixed source path")
        results = []
        for prefix, provider in repositories:
            # A comparison target belongs to one Git object database. Never apply
            # the same target to two unrelated repositories implicitly.
            if target_commit and len(repositories) != 1:
                raise ValueError("a comparison commit requires one exact repository path")
            hits = provider.search(kind, path=path[len(prefix):], start_line=start_line,
                end_line=end_line, target_commit=target_commit, limit=limit)
            for hit in hits:
                payload = asdict(hit)
                payload.pop("digest")
                payload["path"] = prefix + hit.path
                payload["statement"] = f"[{provider.repository_id}] {hit.statement}"
                digest = hashlib.sha256(json.dumps(payload, sort_keys=True,
                    separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
                results.append(SourceHistoryHit(**payload, digest=digest))
        # Interleave repositories so one bounded answer does not silently starve
        # the second repository. The result remains a bounded sample, not all history.
        groups = [[hit for hit in results if hit.repository_id == provider.repository_id]
                  for _, provider in repositories]
        return tuple(hit for row in zip_longest(*groups) for hit in row if hit is not None)[:limit]
