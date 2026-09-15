"""Which commit an evaluation image or a freeze describes, and whether the checkout proves it.

A formal batch has to show that the Agent it ran is the frozen candidate. The Agent image is
built only from a clean checkout and labels itself with that checkout's full commit
(``org.opencontainers.image.revision``); every later check compares the label with the clean
checkout it runs from. Freeze records read apparatus files from the commit itself rather than
from the working tree, so an edit made after a freeze cannot change what the freeze bound.
"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[2]
COMMIT = re.compile(r"[0-9a-f]{40}\Z")


class RevisionError(RuntimeError):
    """The checkout or a commit cannot support the claim made about it."""


def _git(repository: Path, *args: str) -> bytes:
    completed = subprocess.run(["git", "-C", str(repository), *args], capture_output=True,
                               check=False, timeout=60)
    if completed.returncode != 0:
        raise RevisionError(f"git {args[0]} failed in {repository}")
    return completed.stdout


def require_commit(value: Any, name: str = "commit") -> str:
    if not isinstance(value, str) or not COMMIT.fullmatch(value):
        raise RevisionError(f"{name} must be a full 40-character lowercase commit")
    return value


def repository_state(repository: Path = REPOSITORY) -> dict[str, Any]:
    """HEAD's full commit and whether anything outside ignored paths differs from it."""
    commit = _git(repository, "rev-parse", "HEAD").decode().strip()
    changes = _git(repository, "status", "--porcelain", "--untracked-files=normal").decode()
    return {"commit": require_commit(commit, "HEAD"), "clean": not changes.strip()}


def require_clean_head(repository: Path = REPOSITORY, *,
                       commit: str | None = None) -> dict[str, Any]:
    """The checkout's state, provided it is clean and, if given, at ``commit``."""
    state = repository_state(repository)
    if not state["clean"]:
        raise RevisionError("the checkout has uncommitted changes, so HEAD does not describe it")
    if commit is not None and state["commit"] != require_commit(commit):
        raise RevisionError("the checkout's HEAD is not the expected commit")
    return state


def commit_exists(repository: Path, commit: str) -> bool:
    require_commit(commit)
    completed = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True, check=False, timeout=60)
    return completed.returncode == 0


def file_at(repository: Path, commit: str, path: str) -> bytes:
    """The bytes of ``path`` as committed in ``commit``; a path absent there is an error."""
    if not commit_exists(repository, commit):
        raise RevisionError(f"commit {commit[:12]} does not exist in {repository}")
    try:
        return _git(repository, "show", f"{commit}:{path}")
    except RevisionError as exc:
        raise RevisionError(f"{path} is not in commit {commit[:12]}") from exc


def committed_once(repository: Path, path: str) -> str:
    """The one commit that recorded ``path``, provided nothing has changed it since.

    A record edited by a later commit or in the working tree no longer shows what was decided
    at the commit that recorded it, so both are refused.
    """
    commits = _git(repository, "log", "--format=%H", "--", path).decode().split()
    if len(commits) != 1:
        raise RevisionError(f"{path} must be committed exactly once and never changed")
    if _git(repository, "status", "--porcelain", "--untracked-files=normal", "--", path).strip():
        raise RevisionError(f"{path} differs from the commit that recorded it")
    return require_commit(commits[0], path)


def commit_path(repository: Path, path: str, message: str) -> str:
    """Commit exactly ``path``, leaving anything else staged untouched, and return the commit."""
    _git(repository, "add", "--", path)
    _git(repository, "commit", "-q", "--only", "-m", message, "--", path)
    return committed_once(repository, path)


def commit_time(repository: Path, commit: str) -> datetime:
    """When ``commit`` was committed, as an aware timestamp."""
    value = _git(repository, "show", "-s", "--format=%cI", require_commit(commit)).decode()
    return datetime.fromisoformat(value.strip())


def is_ancestor(repository: Path, commit: str, descendant: str) -> bool:
    """Whether ``commit`` is ``descendant`` itself or in its history."""
    for value in (commit, descendant):
        if not commit_exists(repository, value):
            raise RevisionError(f"commit {value[:12]} does not exist in {repository}")
    completed = subprocess.run(
        ["git", "-C", str(repository), "merge-base", "--is-ancestor", commit, descendant],
        capture_output=True, check=False, timeout=60)
    if completed.returncode not in (0, 1):
        raise RevisionError(f"commit {commit[:12]} cannot be compared with {descendant[:12]}")
    return completed.returncode == 0


def blob_ids_at(repository: Path, commit: str, paths: list[str]) -> dict[str, str | None]:
    """The git blob id of each path as committed in ``commit``, or None where it is absent."""
    if not commit_exists(repository, commit):
        raise RevisionError(f"commit {commit[:12]} does not exist in {repository}")
    found: dict[str, str] = {}
    for line in _git(repository, "ls-tree", "-r", commit, "--", *paths).decode().splitlines():
        meta, _, path = line.partition("\t")
        found[path] = meta.split()[2]
    return {path: found.get(path) for path in paths}


def content_blob_ids(repository: Path, paths: list[str]) -> dict[str, str | None]:
    """The git blob id each path's current content has, or None where the file is missing."""
    present = [path for path in paths if (Path(repository) / path).is_file()]
    ids = (_git(repository, "hash-object", "--no-filters", "--", *present).decode().split()
           if present else [])
    values = dict(zip(present, ids))
    return {path: values.get(path) for path in paths}
