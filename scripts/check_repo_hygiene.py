#!/usr/bin/env python3
"""Scan tracked repository files for leaked credentials, private keys, and local paths.

Usage: python scripts/check_repo_hygiene.py
Exits 0 when clean; exits 1 and prints every violation otherwise.
"""
from __future__ import annotations

import math
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Binary extensions that carry no readable text secrets/paths.
_BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".bmp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pdf", ".zip", ".gz", ".tar", ".tgz", ".whl", ".jar", ".sqlite3",
})

#: Container and CI runner home directories explicitly permitted under `/home/`.
ALLOWED_HOME_USERS = frozenset({"ubuntu", "runner"})

#: Narrow allowlist for synthetic negative-test inputs inside test files only.
_ALLOWED_TEST_FILE_URIS = frozenset({"tmp/probe", "tmp/provider", "x"})

_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_KNOWN_TOKEN_RES = (
    ("OpenAI/provider key prefix", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token prefix", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("AWS access key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token prefix", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key prefix", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
)
_SECRET_ASSIGN_RE = re.compile(
    r"""(?i)\b(?:api_?key|secret_?key|access_?token|refresh_?token|private_?key|client_?secret|auth_?token|password)\b"""
    r"""\s*(?:=|:)\s*(?:["']([A-Za-z0-9_+/=-]{20,})["']|([A-Za-z0-9_+/=-]{20,})(?=\s|$|#))"""
)
_PLACEHOLDER_MARKERS = (
    "example", "placeholder", "changeme", "redacted", "synthetic", "dummy",
    "your_", "your-", "test_", "test-", "fixture", "sample", "xxxx",
)

# Built without contiguous forbidden path literals so this checker does not trip naive grep.
_USER_PREFIX = "/" + "Us" + "ers/"
_USER_HOME_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])(?:"
    + _USER_PREFIX
    + r"([A-Za-z0-9_][A-Za-z0-9._-]*|)|/home/([A-Za-z0-9_][A-Za-z0-9._-]*))(?:/[^\s\"'`)]*)?"
)
_FILE_URI_RE = re.compile(r"\b" + "fi" + r"le:///([^\s\"'`)]+)")


def _shannon_entropy(value: str) -> float:
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    length = len(value)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def _char_classes(value: str) -> int:
    return sum((
        any(c.islower() for c in value),
        any(c.isupper() for c in value),
        any(c.isdigit() for c in value),
        any(not c.isalnum() for c in value),
    ))


def _is_high_entropy_secret(value: str) -> bool:
    lowered = value.casefold()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        return False
    if value.startswith(("http://", "https://", "sha256:", "sha512-")):
        return False
    return _char_classes(value) >= 3 and _shannon_entropy(value) >= 3.3


def _is_test_file(rel: str) -> bool:
    path = Path(rel)
    parts = path.parts
    return (
        "tests" in parts
        or path.name.startswith("test_")
        or ".test." in path.name
        or ".spec." in path.name
    )


def _tracked_files(root: Path) -> list[str]:
    if (root / ".git").exists():
        raw = subprocess.check_output(
            ["git", "-C", str(root), "ls-files", "-z"],
            stderr=subprocess.DEVNULL,
        )
        return sorted(os.fsdecode(item) for item in raw.split(b"\0") if item)
    skip_dirs = {".git", "__pycache__", "node_modules", "dist", ".venv"}
    found = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in skip_dirs for part in rel.parts):
            continue
        found.append(rel.as_posix())
    return sorted(found)


def scan_repository(root: Path = ROOT) -> list[str]:
    """Return human-readable hygiene violation lines for ``root``."""
    violations: list[str] = []
    for rel in _tracked_files(root):
        path = root / rel
        if not path.is_file() or path.suffix.lower() in _BINARY_SUFFIXES:
            continue
        raw = path.read_bytes()
        if b"\x00" in raw[:8192]:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        is_test = _is_test_file(rel)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _PRIVATE_KEY_RE.search(line):
                violations.append(f"{rel}:{lineno}: private key block header")
            for label, pattern in _KNOWN_TOKEN_RES:
                if pattern.search(line):
                    violations.append(f"{rel}:{lineno}: {label}")
            for match in _SECRET_ASSIGN_RE.finditer(line):
                candidate = match.group(1) or match.group(2)
                if candidate and _is_high_entropy_secret(candidate):
                    violations.append(f"{rel}:{lineno}: high-entropy secret assignment")
            for match in _USER_HOME_PATH_RE.finditer(line):
                full_match = match.group(0)
                home_user = match.group(2)
                if home_user and home_user in ALLOWED_HOME_USERS:
                    continue
                if is_test and f'assertNotIn("{_USER_PREFIX}"' in line:
                    continue
                violations.append(f"{rel}:{lineno}: local machine absolute path {full_match!r}")
            for match in _FILE_URI_RE.finditer(line):
                target = match.group(1)
                if is_test and target in _ALLOWED_TEST_FILE_URIS:
                    continue
                violations.append(f"{rel}:{lineno}: local file URI {match.group(0)!r}")
    return violations


def main() -> int:
    violations = scan_repository(ROOT)
    if violations:
        print("FAIL: repository hygiene check found violations:")
        for item in violations:
            print(f"  {item}")
        return 1
    print(f"PASS: repository hygiene clean ({len(_tracked_files(ROOT))} files scanned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
