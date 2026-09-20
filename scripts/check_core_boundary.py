#!/usr/bin/env python3
"""Assert that the core package carries no application-specific execution logic.

Three checks scoped to ``src/web_operation_agent``. The first two look at non-test ``.py``
files; the third deliberately includes the tests, because that is where the boundary broke
last time -- core test modules imported a reference adapter, so the core suite
could not run without an application distribution installed and every generic route consumer
was only ever exercised against one application's URL spelling.

1. No line of *executable code* spells an application's name (``ofbiz``, ``odoo``,
   ``twenty``) or an application's route shape (``/control/``, ``/odoo/``), case-insensitive. A comment or a docstring MAY still explain OFBiz as a
   historical example -- the review that set this gate was explicit that explanatory prose is
   good documentation and grep-zeroing names is not the acceptance bar; only application-owned
   execution logic has to be gone. This is why the check is AST/token-aware rather than a bare
   ``grep``: a naive line grep cannot tell a citation-matching regex from a comment about one.
2. Core never imports from ``evals`` -- the evaluation harness is a consumer of core, never
   the other way around.

Usage: python scripts/check_core_boundary.py
Exits 0 and prints a summary on success; exits 1 and lists every offending line otherwise.
"""
from __future__ import annotations

import ast
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "src" / "web_operation_agent"

#: Application names and the servlet/route shapes that belong to one application.
_PATTERN = re.compile(r"ofbiz|/control/|\bodoo\b|/odoo/|\btwenty\b", re.IGNORECASE)
#: Every in-repository application distribution. The core imports none of them, anywhere.
_ADAPTER_IMPORT = re.compile(r"web_operation_agent_[a-z0-9_]+")


def _docstring_spans(tree: ast.AST) -> list[tuple[int, int, int, int]]:
    """(start_line, start_col, end_line, end_col) of every module/class/function docstring."""
    spans = []
    candidates = [tree] + [n for n in ast.walk(tree)
                           if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
    for node in candidates:
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            spans.append((first.lineno, first.col_offset,
                         first.end_lineno, first.end_col_offset))
    return spans


def _comment_spans(source: str) -> list[tuple[int, int, int, int]]:
    spans = []
    try:
        for tok in tokenize.generate_tokens(iter(source.splitlines(keepends=True)).__next__):
            if tok.type == tokenize.COMMENT:
                spans.append((tok.start[0], tok.start[1], tok.end[0], tok.end[1]))
    except tokenize.TokenizeError:
        pass
    return spans


def _within(line: int, col: int, spans: list[tuple[int, int, int, int]]) -> bool:
    for start_line, start_col, end_line, end_col in spans:
        if start_line <= line <= end_line:
            if start_line == end_line and not (start_col <= col < end_col):
                continue
            if line == start_line and col < start_col:
                continue
            if line == end_line and col >= end_col:
                continue
            return True
    return False


def check_execution_logic() -> list[str]:
    violations = []
    for path in sorted(CORE.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        matches = list(_PATTERN.finditer(source))
        if not matches:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            violations.append(f"{rel}: could not parse ({exc})")
            continue
        allowed = _docstring_spans(tree) + _comment_spans(source)
        lines = source.splitlines()
        for match in matches:
            line = source.count("\n", 0, match.start()) + 1
            line_start = source.rfind("\n", 0, match.start()) + 1
            col = match.start() - line_start
            if _within(line, col, allowed):
                continue
            violations.append(f"{rel}:{line}: {lines[line - 1].strip()!r}")
    return violations


def check_no_evals_import() -> list[str]:
    violations = []
    pattern = re.compile(r"^\s*(?:from\s+evals(?:\.|import|\s)|import\s+evals\b)")
    for path in sorted(CORE.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.match(line):
                violations.append(f"{rel}:{lineno}: {line.strip()!r}")
    return violations


def check_no_adapter_import() -> list[str]:
    """Core, tests included, must not import any application distribution.

    A core test that imports an adapter is not a small convenience: it makes the core suite
    unrunnable without that distribution, and it silently narrows what the generic code is
    tested against to one application's conventions.
    """
    violations = []
    for path in sorted(CORE.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if "web_operation_agent_" not in source:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            violations.append(f"{rel}: could not parse ({exc})")
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if _ADAPTER_IMPORT.match(name.split(".")[0] or ""):
                    violations.append(f"{rel}:{node.lineno}: imports {name!r}")
    return violations


def main() -> int:
    execution_violations = check_execution_logic()
    adapter_violations = check_no_adapter_import()
    evals_violations = check_no_evals_import()
    ok = True
    if execution_violations:
        ok = False
        print("FAIL: application-specific execution logic in core (outside comments/docstrings):")
        for v in execution_violations:
            print(f"  {v}")
    else:
        print("PASS: no application name or route shape in core execution logic")
    if evals_violations:
        ok = False
        print("FAIL: core imports from evals:")
        for v in evals_violations:
            print(f"  {v}")
    else:
        print("PASS: core never imports from evals")
    if adapter_violations:
        ok = False
        print("FAIL: core imports an application distribution:")
        for v in adapter_violations:
            print(f"  {v}")
    else:
        print("PASS: core never imports an application distribution")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
