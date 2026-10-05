"""Read-only Git history for the commit behind a deployed source release.

History is deliberately separate from line evidence.  A commit author, CODEOWNERS match or
diff summary is Git metadata, not bytes at ``file:line``; representing it as an
``EvidenceRef`` would make a citation look stronger than it is.  The records below carry
their own replay digest and always name the deployment commit they were derived from.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .sourcetree import SourceTree, _glob_match


_COMMIT = re.compile(r"[0-9a-f]{40,64}")
HISTORY_KINDS = frozenset({"owners", "blame", "commits", "changes"})
_MAX_OUTPUT = 4 * 1024 * 1024


class HistoryUnavailable(ValueError):
    """This deployment does not expose a locally replayable repository history."""


@dataclass(frozen=True, slots=True)
class SourceHistoryHit:
    repository_id: str
    release: str
    deployed_commit: str
    kind: str
    statement: str
    digest: str
    path: str = ""
    start_line: int = 0
    end_line: int = 0
    commit: str = ""

    def __post_init__(self) -> None:
        if self.kind not in HISTORY_KINDS:
            raise ValueError(f"unknown source history kind {self.kind!r}")
        if not _COMMIT.fullmatch(self.deployed_commit):
            raise ValueError("deployed_commit must be a full commit id")
        if self.commit and not _COMMIT.fullmatch(self.commit):
            raise ValueError("commit must be a full commit id")
        if not re.fullmatch(r"[0-9a-f]{64}", self.digest):
            raise ValueError("history digest must be sha256")
        if not self.statement.strip():
            raise ValueError("a source history hit must state what Git reported")
        if self.path.startswith("/") or ".." in self.path.split("/"):
            raise ValueError("history paths must be repository-relative")
        if self.start_line or self.end_line:
            if not 1 <= self.start_line <= self.end_line:
                raise ValueError("history line range is invalid")

    @property
    def key(self) -> str:
        return ":".join((self.kind, self.path, str(self.start_line), str(self.end_line),
                         self.commit, self.digest))


@runtime_checkable
class SourceHistoryLookup(Protocol):
    release: str
    # A provider may compose several repositories. Each hit binds its own real
    # commit; None means there is no single deployment commit for the composition.
    deployed_commit: str | None

    def search(self, kind: str, *, path: str = "", start_line: int = 0,
               end_line: int = 0, target_commit: str = "",
               limit: int = 8) -> tuple[SourceHistoryHit, ...]: ...


def _digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def _safe_text(value: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()[:1000]


class GitSourceHistory:
    """Bounded Git queries whose paths remain inside a ``SourceTree`` policy."""

    def __init__(self, tree: SourceTree, deployed_commit: str, *, timeout_s: float = 10.0,
                 path_filter: Callable[[str], bool] | None = None):
        if not isinstance(tree, SourceTree):
            raise TypeError("tree must be a SourceTree")
        if not 0.1 <= timeout_s <= 60:
            raise ValueError("timeout_s must be from 0.1 through 60")
        self._tree = tree
        self._root = Path(tree.root)
        self._timeout_s = timeout_s
        if path_filter is not None and not callable(path_filter):
            raise TypeError("path_filter must be callable or None")
        self._path_filter = path_filter or (lambda _path: True)
        try:
            root = self._git("rev-parse", "--show-toplevel").strip()
        except HistoryUnavailable:
            raise
        if Path(root).resolve() != self._root:
            raise HistoryUnavailable("the source root is not the Git worktree root")
        self.deployed_commit = self._resolve_commit(deployed_commit)
        self.release = tree.release
        self.repository_id = tree.repository_id
        self._assert_clean_surface()

    @classmethod
    def optional(cls, tree: SourceTree, deployed_commit: str = "", *,
                 path_filter: Callable[[str], bool] | None = None) -> tuple[
            "GitSourceHistory | None", str]:
        if not deployed_commit:
            return None, "deployment commit is not configured"
        try:
            return cls(tree, deployed_commit, path_filter=path_filter), ""
        except HistoryUnavailable as exc:
            return None, str(exc)

    def _git(self, *args: str, check: bool = True) -> str:
        try:
            run = subprocess.run(
                ("git", "-C", str(self._root), *args), check=False,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self._timeout_s,
                env={"PATH": "/usr/bin:/bin:/usr/local/bin", "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HistoryUnavailable(f"local Git history is unavailable: {exc}") from None
        if len(run.stdout) > _MAX_OUTPUT or len(run.stderr) > _MAX_OUTPUT:
            raise HistoryUnavailable("local Git output exceeded its safety bound")
        if check and run.returncode:
            detail = run.stderr.decode("utf-8", "replace").strip().splitlines()
            message = detail[0][:300] if detail else "Git command failed"
            raise HistoryUnavailable(message)
        return run.stdout.decode("utf-8", "replace")

    def _resolve_commit(self, value: str) -> str:
        if not isinstance(value, str) or not _COMMIT.fullmatch(value.strip()):
            raise HistoryUnavailable("a full deployment commit id is required")
        resolved = self._git("rev-parse", "--verify", f"{value.strip()}^{{commit}}").strip()
        if not _COMMIT.fullmatch(resolved):
            raise HistoryUnavailable("deployment commit did not resolve to a full commit id")
        return resolved

    def _assert_clean_surface(self) -> None:
        changed = set(self._git(
            "diff", "--name-only", "--no-renames", self.deployed_commit, "--",
        ).splitlines())
        changed.update(self._git(
            "ls-files", "--others", "--exclude-standard", "--",
        ).splitlines())
        changed.update(self._git(
            "ls-files", "--others", "--ignored", "--exclude-standard", "--",
        ).splitlines())
        drift = sorted(path for path in changed
                       if self._tree.permits(path) and self._path_filter(path))
        if drift:
            shown = ", ".join(drift[:5])
            raise HistoryUnavailable(
                f"allowlisted source bytes do not match deployment commit: {shown}")

    def _path(self, path: str, *, required: bool = True) -> str:
        if not isinstance(path, str):
            raise TypeError("path must be a string")
        clean = path.strip().replace("\\", "/")
        if len(clean) > 500 or re.search(r"[\x00-\x1f\x7f]", clean):
            raise ValueError("history path is invalid")
        if required and not clean:
            raise ValueError("this history query requires a path")
        if clean and (clean.startswith("/") or ".." in clean.split("/")
                      or not self._tree.permits(clean) or not self._path_filter(clean)):
            raise ValueError("history path is outside the source allowlist")
        return clean

    def _hit(self, kind: str, statement: str, *, path: str = "", start_line: int = 0,
             end_line: int = 0, commit: str = "") -> SourceHistoryHit:
        statement = _safe_text(statement)
        payload = {"repository_id": self.repository_id, "release": self.release,
                   "deployed_commit": self.deployed_commit, "kind": kind,
                   "statement": statement, "path": path, "start_line": start_line,
                   "end_line": end_line, "commit": commit}
        return SourceHistoryHit(**payload, digest=_digest(payload))

    def search(self, kind: str, *, path: str = "", start_line: int = 0,
               end_line: int = 0, target_commit: str = "",
               limit: int = 8) -> tuple[SourceHistoryHit, ...]:
        if kind not in HISTORY_KINDS:
            raise ValueError(f"kind must be one of {sorted(HISTORY_KINDS)}")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be an integer from 1 through 20")
        if kind == "owners":
            return self._owners(self._path(path), limit)
        if kind == "blame":
            return self._blame(self._path(path), start_line, end_line, limit)
        if kind == "commits":
            return self._commits(self._path(path), limit)
        return self._changes(self._path(path, required=False), target_commit, limit)

    def _owners(self, path: str, limit: int) -> tuple[SourceHistoryHit, ...]:
        # GitHub reads the first CODEOWNERS file in this order; lower-priority files do not
        # add owners.  Preserve that behavior so one repository cannot appear to declare
        # several competing owners merely because it contains stale copies.
        candidates = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
        matches: list[tuple[str, str, str]] = []
        for owner_file in candidates:
            # Ownership metadata must itself be in the deployment's source allowlist.
            if not self._tree.permits(owner_file) or not self._path_filter(owner_file):
                continue
            content = self._git(
                "show", f"{self.deployed_commit}:{owner_file}", check=False)
            if not content:
                continue
            selected = None
            for number, raw in enumerate(content.splitlines(), 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                fields = line.split()
                if len(fields) < 2:
                    continue
                pattern = fields[0].lstrip("/")
                # CODEOWNERS uses gitignore-like rules.  This bounded matcher handles the
                # common anchored, directory and wildcard forms; the output names it a
                # declared match, not proof of current organizational responsibility.
                selected_path = path
                matched = (_glob_match(selected_path, pattern)
                           or ("/" not in pattern and _glob_match(
                               selected_path.rsplit("/", 1)[-1], pattern))
                           or (pattern.endswith("/") and selected_path.startswith(pattern)))
                if matched:
                    selected = (str(number), _safe_text(" ".join(fields[1:])))
            if selected:
                number, owners = selected
                matches.append((owner_file, number, owners))
            break
        return tuple(self._hit(
            "owners", f"{owner_file}:{line} declares {owners} for {path}",
            path=path, commit=self.deployed_commit,
        ) for owner_file, line, owners in matches[-limit:])

    def _blame(self, path: str, start_line: int, end_line: int,
               limit: int) -> tuple[SourceHistoryHit, ...]:
        if not start_line:
            start_line = end_line = 1
        if not end_line:
            end_line = start_line
        if (isinstance(start_line, bool) or isinstance(end_line, bool)
                or not 1 <= start_line <= end_line or end_line - start_line > 200):
            raise ValueError("blame requires a line range of at most 201 lines")
        output = self._git(
            "blame", "--line-porcelain", f"-L{start_line},{end_line}",
            self.deployed_commit, "--", path,
        )
        rows: list[tuple[str, str, str, int]] = []
        commit = author = summary = ""
        current_line = start_line
        for line in output.splitlines():
            header = re.match(r"^([0-9a-f]{40,64}) \d+ (\d+)(?: \d+)?$", line)
            if header:
                commit, current = header.group(1), int(header.group(2))
                current_line = current
                author = summary = ""
            elif line.startswith("author "):
                author = _safe_text(line[7:])
            elif line.startswith("summary "):
                summary = _safe_text(line[8:])
            elif line.startswith("\t") and commit:
                rows.append((commit, author or "unknown author", summary, current_line))
                current_line += 1
        out = []
        seen = set()
        for commit, author, summary, line in rows:
            key = (commit, author, summary)
            if key in seen:
                continue
            seen.add(key)
            statement = f"{path}:{line} is attributed to {author} in {commit[:12]}"
            if summary:
                statement += f" ({summary})"
            out.append(self._hit("blame", statement, path=path, start_line=line,
                                 end_line=line, commit=commit))
            if len(out) == limit:
                break
        return tuple(out)

    def _commits(self, path: str, limit: int) -> tuple[SourceHistoryHit, ...]:
        output = self._git(
            "log", f"--max-count={limit}", "--format=%H%x00%an%x00%aI%x00%s",
            self.deployed_commit, "--", path,
        )
        out = []
        for row in output.splitlines():
            fields = row.split("\0", 3)
            if len(fields) != 4 or not _COMMIT.fullmatch(fields[0]):
                continue
            commit, author, when, summary = (
                fields[0], _safe_text(fields[1]), _safe_text(fields[2]), _safe_text(fields[3]))
            statement = f"{commit[:12]} by {author} at {when}: {summary}"
            out.append(self._hit("commits", statement, path=path, commit=commit))
        return tuple(out)

    def _changes(self, path: str, target_commit: str,
                 limit: int) -> tuple[SourceHistoryHit, ...]:
        if not target_commit:
            raise ValueError("changes requires target_commit")
        target = self._resolve_commit(target_commit)
        args = ["diff", "--name-status", "--no-renames", self.deployed_commit, target, "--"]
        if path:
            args.append(path)
        output = self._git(*args)
        bounded_diff = ""
        if path:
            diff_lines = self._git(
                "diff", "--no-ext-diff", "--no-color", "--unified=2",
                self.deployed_commit, target, "--", path,
            ).splitlines()
            informative = [line for line in diff_lines
                           if not line.startswith(("diff --git ", "index ", "--- ", "+++ "))]
            bounded_diff = _safe_text(" | ".join(informative[:24]))[:800]
        out = []
        for row in output.splitlines():
            status, separator, changed = row.partition("\t")
            if (not separator or not self._tree.permits(changed)
                    or not self._path_filter(changed)):
                continue
            statement = (f"{changed} has status {status} from deployed "
                         f"{self.deployed_commit[:12]} to {target[:12]}")
            if bounded_diff and changed == path:
                statement += f"; bounded diff: {bounded_diff}"
            out.append(self._hit("changes", statement, path=changed, commit=target))
            if len(out) == limit:
                break
        return tuple(out)
