"""Leak check: nothing from a task population or the evaluation harness reaches the product.

Every distinctive synthetic value of both populations -- names, e-mails, references, tags -- is
searched for in the product code, packaged Skills and policies, the Odoo addon, the panel
source, the deployment files and, when present, the extracted Odoo source tree the optional
source map is compiled from. Product code must also never name the evaluation package or its
task sets. Results carry paths and value digests only, so a report cannot itself leak a
held-out value.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from evals.odoo.fixture.stack import REPOSITORY

PRODUCT_ROOTS = (
    "src/web_operation_agent", "examples/odoo/src/web_operation_agent_odoo",
    "examples/odoo/deploy/addons/woa_runtime", "frontend/src", "examples/odoo/deploy/single-node",
)
OPTIONAL_ROOTS = (".fixture/odoo-source",)
HARNESS_MARKERS = ("evals.odoo", "evals/odoo", "p74-odoo-development", "p74-odoo-heldout",
                   "p74_odoo_task_set")
_SKIP_SUFFIXES = {".png", ".jpg", ".svg", ".woff", ".woff2", ".pgdump", ".whl", ".sqlite3",
                  ".tar", ".gz", ".ico"}


def _files(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if (path.is_file() and path.suffix not in _SKIP_SUFFIXES
                    and not {"__pycache__", "node_modules", "dist", "build"} & set(path.parts)):
                yield path


def scan(values: Iterable[str], *, repository: Path = REPOSITORY) -> dict[str, Any]:
    needles = sorted({value.casefold() for value in values if len(value) >= 5},
                     key=len, reverse=True)
    roots = [repository / item for item in PRODUCT_ROOTS]
    optional = [repository / item for item in OPTIONAL_ROOTS]
    hits, markers, scanned = [], [], 0
    for path in _files([*roots, *optional]):
        try:
            text = path.read_text(encoding="utf-8").casefold()
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        relative = path.relative_to(repository).as_posix()
        for needle in needles:
            if needle in text:
                hits.append({"path": relative,
                             "value_digest": "sha256:" + hashlib.sha256(
                                 needle.encode()).hexdigest()[:16]})
        if any(path.is_relative_to(root) for root in roots):
            markers += [{"path": relative, "marker": marker}
                        for marker in HARNESS_MARKERS if marker in text]
    return {"kind": "p74_leak_check", "scanned_files": scanned,
            "optional_roots_present": [item for item in OPTIONAL_ROOTS
                                       if (repository / item).exists()],
            "population_value_hits": hits, "harness_references_in_product": markers,
            "status": "READY" if not hits and not markers and scanned else "FAILED"}
