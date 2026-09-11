#!/usr/bin/env python3
"""Measure how much of an application executor is "call the API" vs "safely call it".

This produces the numbers in `docs/integrations/http-contract-feasibility.md`. The rule
is mechanical so the ratio can be re-derived rather than trusted: a code line belongs to
"safely" when it mentions one of the completeness, duplicate-check or readback
identifiers in `SAFETY` below. Blank lines, comments and docstrings are excluded from
every count.

    python scripts/measure_adapter_safety.py [--show]

`--show` prints every line counted as safety, so a reader who disagrees with an
identifier can see exactly which lines it pulled in.
"""
import ast, re, sys
from pathlib import Path

SAFETY = re.compile(
    r"source_complete|result_truncated|records_scanned|incomplete_reason|pages_scanned"
    r"|completeness|source_total|matched_count|search_count|totalCount|hasNextPage"
    r"|_woaPagination|pagination|endCursor|startingAfter|offset|_MAX_\w*PAGE|_PAGE_SIZE"
    r"|_MAX_COMPANY|_assert_no_duplicate|duplicate|business_text_key|readback"
    r"|Indeterminate|complete\b|scan\b|_scan\(")

def classify(path, class_name, label):
    source = Path(path).read_text()
    lines = source.splitlines()
    tree = ast.parse(source)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == class_name)
    total = safety = 0
    in_doc = False
    hits = []
    for index in range(node.lineno - 1, node.end_lineno):
        raw = lines[index]
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        quotes = line.count('"""') + line.count("'''")
        if in_doc:
            if quotes:
                in_doc = False
            continue
        if line.startswith(('"""', "'''")):
            if quotes == 1:
                in_doc = True
            continue
        total += 1
        if SAFETY.search(line):
            safety += 1
            hits.append((index + 1, line))
    print(f"\n== {label}: {class_name} ==")
    print(f"   code lines: {total}")
    print(f"   safety-of-calling lines: {safety}  ({safety / total:.0%})")
    if "--show" in sys.argv:
        for lineno, line in hits:
            print(f"     {lineno:>5}  {line[:110]}")
    return total, safety

classify("examples/twenty/src/web_operation_agent_twenty/adapter.py",
         "TwentyBusinessToolExecutor", "baseline (Twenty)")
classify("examples/odoo/src/web_operation_agent_odoo/adapter.py",
         "OdooBusinessToolExecutor", "main application (Odoo)")
