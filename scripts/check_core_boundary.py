#!/usr/bin/env python3
"""Assert that the core package carries no application-specific execution logic.

Two checks, both scoped to ``src/web_operation_agent`` and its non-test ``.py`` files:

1. No line of *executable code* spells ``ofbiz`` or the OFBiz servlet-path shape
   ``/control/`` (case-insensitive). A comment or a docstring MAY still explain OFBiz as a
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

_PATTERN = re.compile(r"ofbiz|/control/", re.IGNORECASE)


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


def main() -> int:
    execution_violations = check_execution_logic()
    evals_violations = check_no_evals_import()
    ok = True
    if execution_violations:
        ok = False
        print("FAIL: application-specific execution logic in core (outside comments/docstrings):")
        for v in execution_violations:
            print(f"  {v}")
    else:
        print("PASS: no 'ofbiz' or '/control/' in core execution logic")
    if evals_violations:
        ok = False
        print("FAIL: core imports from evals:")
        for v in evals_violations:
            print(f"  {v}")
    else:
        print("PASS: core never imports from evals")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
