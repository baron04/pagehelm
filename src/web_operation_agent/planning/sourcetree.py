"""Read-only, allowlisted access to the deployed release's source tree.

Three rules, in this order, for every path:

  1. resolve first -- realpath the candidate before any comparison, or `a/../../secrets/x`
     walks straight through the glob checks;
  2. deny wins -- a denied path stays denied even when an allow glob also matches it;
  3. default deny -- anything not explicitly allowed is refused.

Every successful read returns bytes *and* the citation that proves where they came from, so
a relation in a SourceMap can always be replayed against the tree it was compiled from.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
from importlib import resources
from pathlib import Path

from ..schemas import EvidenceRef, SchemaError

class AccessDenied(Exception):
    pass


@functools.lru_cache(maxsize=512)
def _compile_glob(pattern: str) -> re.Pattern:
    """Path-aware glob semantics, because `fnmatch` has none.

    `fnmatch` translates `*` to `.*`, so it happily lets a single star cross directory
    separators, and it has no notion of "zero or more directories" at all. Under those rules
    `applications/**/widget/**/*.xml` silently fails to match
    `applications/accounting/widget/PaymentForms.xml` -- it demands a directory between
    `widget/` and the file -- which quietly dropped every top-level widget file, and with them
    the form that submits the payment application. Hence explicit translation:

        `**/`  zero or more directories        `*`  anything within one segment
        `**`   anything, separators included   `?`  one character within one segment
    """
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _glob_match(rel: str, pattern: str) -> bool:
    return _compile_glob(pattern).match(rel) is not None


class SourceTree:
    """One repository, pinned to one release, behind one allowlist."""

    def __init__(self, policy: dict, source_root: Path | str):
        if "root" in policy:
            raise ValueError(
                "source policy must not contain a local root; pass source_root explicitly")
        self.policy = policy
        self.repository_id = policy["repository_id"]
        self.release = policy["release"]
        root = Path(source_root).expanduser()
        if not root.is_absolute():
            raise ValueError("source_root must be an absolute path")
        self.root = root.resolve()
        self.allow = list(policy["allow_globs"])
        self.deny = list(policy["deny_globs"])
        self.max_bytes = int(policy.get("max_bytes_per_read", 262144))
        self._files: list[str] | None = None

    @staticmethod
    def load(name: str, source_root: Path | str, *,
             package: str = "web_operation_agent") -> "SourceTree":
        """Load one distribution's packaged allow/deny policy, bound to a caller-owned root.

        ``package`` is the importable package that ships ``policies/<name>.json`` -- the core
        package ships none of its own; an application distribution such as
        ``web_operation_agent_ofbiz`` ships its own policy under its own package name.
        """
        if (not isinstance(name, str) or not name
                or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)):
            raise ValueError("source policy name is invalid")
        if not isinstance(package, str) or not re.fullmatch(r"[A-Za-z0-9_.]+", package):
            raise ValueError("source policy package is invalid")
        policy_file = resources.files(package).joinpath("policies", f"{name}.json")
        try:
            policy = json.loads(policy_file.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"unknown source policy {name!r} in package {package!r}") from exc
        return SourceTree(policy, source_root)

    def available(self) -> bool:
        return self.root.is_dir()

    # -- policy ------------------------------------------------------------------------
    def permits(self, rel: str) -> bool:
        if any(_glob_match(rel, p) for p in self.deny):
            return False
        return any(_glob_match(rel, p) for p in self.allow)

    def resolve(self, rel: str) -> Path:
        candidate = (self.root / rel).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise AccessDenied(f"outside the source root: {rel!r}")
        if not self.permits(rel):
            raise AccessDenied(f"not permitted by the allowlist: {rel!r}")
        return candidate

    # -- enumeration and reading -------------------------------------------------------
    def files(self) -> list[str]:
        """Every allowlisted file, sorted. This set *is* the attested surface."""
        if self._files is None:
            out = []
            for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
                dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "build")]
                for fn in filenames:
                    rel = os.path.relpath(os.path.join(dirpath, fn), self.root)
                    if self.permits(rel):
                        out.append(rel)
            self._files = sorted(out)
        return list(self._files)

    def read_text(self, rel: str) -> str:
        path = self.resolve(rel)
        data = path.read_bytes()
        if len(data) > self.max_bytes:
            raise AccessDenied(f"{rel} is {len(data)} bytes, over the {self.max_bytes} limit")
        return data.decode("utf-8", errors="replace")

    def cite(self, rel: str, start_line: int, end_line: int, *, excerpt_chars: int = 0) -> EvidenceRef:
        """Build the citation for a line range, hashing the exact cited bytes."""
        lines = self.read_text(rel).splitlines(keepends=True)
        if start_line < 1 or end_line > len(lines) or end_line < start_line:
            raise SchemaError(f"{rel}:{start_line}-{end_line} is outside the file "
                              f"({len(lines)} lines)")
        body = "".join(lines[start_line - 1:end_line])
        excerpt = body[:excerpt_chars].rstrip() if excerpt_chars else ""
        return EvidenceRef(repository_id=self.repository_id, release=self.release, path=rel,
                           start_line=start_line, end_line=end_line,
                           sha256=hashlib.sha256(body.encode()).hexdigest(), excerpt=excerpt)

    def verify(self, ref: EvidenceRef) -> bool:
        """Does this citation still name exactly the bytes it claims?"""
        if ref.repository_id != self.repository_id or ref.release != self.release:
            return False
        try:
            fresh = self.cite(ref.path, ref.start_line, ref.end_line)
        except (AccessDenied, SchemaError):
            return False
        return fresh.sha256 == ref.sha256

    def surface_digest(self) -> str:
        """sha256 over 'relpath\\0filesha\\n' for every allowlisted file.

        This is the value the attestation binds. It changes if a compiled file changes, is
        added or is removed, which is exactly the drift the SourceMap must not survive.
        """
        h = hashlib.sha256()
        for rel in self.files():
            fh = hashlib.sha256()
            with open(self.root / rel, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    fh.update(chunk)
            h.update(f"{rel}\0{fh.hexdigest()}\n".encode())
        return h.hexdigest()
