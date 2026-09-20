"""Which commit an evaluation image describes, and whether the checkout proves it.

A batch has to show that the Agent it ran is the candidate it names. The Agent image is built
only from a clean checkout and labels itself with that checkout's full commit
(``org.opencontainers.image.revision``); every later check compares the label with the clean
checkout it runs from.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[3]
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


def changed_between(repository: Path, commit: str, descendant: str, prefix: str) -> list[str]:
    """The paths under ``prefix`` that differ between two commits."""
    for value in (commit, descendant):
        if not commit_exists(repository, value):
            raise RevisionError(f"commit {value[:12]} does not exist in {repository}")
    return _git(repository, "diff", "--name-only", commit, descendant, "--",
                prefix).decode().split()
