"""Qualify Odoo source retrieval without a model or a running Odoo database.

The task manifest is frozen before its first run.  This prevents tuning the ranker against
observed misses during this phase, but it is deliberately described as a post-implementation
challenge set rather than an independently authored blind benchmark.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import statistics
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from web_operation_agent.planning.source_facts import SourceFact, SourceFactIndex
from web_operation_agent.planning.sourcetree import SourceTree
from web_operation_agent.schemas import EvidenceRef, SourceMap, SourceRelation, canonical_json
from web_operation_agent_odoo.sourcemap import OdooCompiler, module_of, odoo_source_tree


TASK_SET = Path(__file__).with_name("tasks-p8-v1.json")
_CATEGORIES = frozenset({
    "hidden_entry", "field_semantics", "refusal_precondition",
    "current_release_behavior",
})


@dataclass(frozen=True)
class ExpectedRelation:
    kind: str
    subject: str
    object: str | None = None
    object_contains: str | None = None

    def matches(self, relation: SourceRelation) -> bool:
        return (
            relation.kind == self.kind
            and relation.subject == self.subject
            and (self.object is None or relation.object == self.object)
            and (self.object_contains is None or self.object_contains in relation.object)
        )


@dataclass(frozen=True)
class RetrievalTask:
    id: str
    category: str
    query: str
    expected: ExpectedRelation


@dataclass(frozen=True)
class TaskSet:
    suite_id: str
    evidence_class: str
    top_k: int
    minimum_recall_at_10: float
    modules: frozenset[str]
    tasks: tuple[RetrievalTask, ...]
    sha256: str


def _exact_keys(value: dict[str, Any], allowed: set[str], where: str) -> None:
    if set(value) != allowed:
        raise ValueError(
            f"{where} keys must be {sorted(allowed)}, got {sorted(value)}")


def load_task_set(path: Path = TASK_SET) -> TaskSet:
    raw = path.read_bytes()
    body = json.loads(raw)
    _exact_keys(body, {
        "schema_version", "suite_id", "status", "evidence_class", "source_language",
        "top_k", "minimum_recall_at_10", "modules", "tasks",
    }, "task set")
    if body["schema_version"] != 1 or body["status"] != "frozen-before-first-run":
        raise ValueError("task set must be schema 1 and frozen before its first run")
    if body["source_language"] != "en":
        raise ValueError("this source tree and task set must use English retrieval terms")
    if body["top_k"] != 10:
        raise ValueError("the P8 gate is fixed at recall@10")
    threshold = body["minimum_recall_at_10"]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ValueError("minimum_recall_at_10 must be numeric")
    if not 0 < threshold <= 1:
        raise ValueError("minimum_recall_at_10 must be in (0, 1]")
    modules = frozenset(body["modules"])
    if len(modules) != len(body["modules"]) or not modules:
        raise ValueError("modules must be a non-empty unique list")

    tasks: list[RetrievalTask] = []
    for offset, value in enumerate(body["tasks"]):
        _exact_keys(value, {"id", "category", "query", "expected"}, f"task[{offset}]")
        if value["category"] not in _CATEGORIES:
            raise ValueError(f"task {value['id']!r} has an unknown category")
        expected = value["expected"]
        allowed = {"kind", "subject", "object", "object_contains"}
        if not isinstance(expected, dict) or set(expected) - allowed:
            raise ValueError(f"task {value['id']!r} has an invalid expected relation")
        if not {"kind", "subject"} <= set(expected):
            raise ValueError(f"task {value['id']!r} expected relation is incomplete")
        if "object" in expected and "object_contains" in expected:
            raise ValueError(f"task {value['id']!r} cannot use two object matchers")
        task = RetrievalTask(
            id=value["id"],
            category=value["category"],
            query=value["query"],
            expected=ExpectedRelation(**expected),
        )
        if not task.id or not task.query.strip():
            raise ValueError("task ids and queries must be non-empty")
        tasks.append(task)
    ids = [task.id for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("task ids must be unique")
    counts = Counter(task.category for task in tasks)
    if len(tasks) != 40 or any(counts[category] != 10 for category in _CATEGORIES):
        raise ValueError(f"the frozen set must have 10 tasks per category, got {dict(counts)}")
    return TaskSet(
        suite_id=body["suite_id"], evidence_class=body["evidence_class"],
        top_k=body["top_k"], minimum_recall_at_10=float(threshold), modules=modules,
        tasks=tuple(tasks), sha256=hashlib.sha256(raw).hexdigest(),
    )


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)] if ordered else 0.0


def rank_task(task: RetrievalTask, hits: tuple[SourceFact, ...]) -> int | None:
    return next((rank for rank, hit in enumerate(hits, 1)
                 if task.expected.matches(hit.relation)), None)


def _drift_probe(tree: SourceTree, reference: EvidenceRef) -> dict[str, Any]:
    """Prove that byte drift and release drift both invalidate an old citation."""
    with tempfile.TemporaryDirectory(prefix="woa-source-drift-") as tmp:
        root = Path(tmp)
        target = root / reference.path
        target.parent.mkdir(parents=True)
        shutil.copy2(tree.resolve(reference.path), target)

        same = SourceTree(dict(tree.policy), root)
        before = same.surface_digest()
        before_verifies = same.verify(reference)

        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        index = reference.start_line - 1
        ending = "\n" if lines[index].endswith("\n") else ""
        line = lines[index][:-1] if ending else lines[index]
        lines[index] = line + " " + ending
        target.write_text("".join(lines), encoding="utf-8")
        changed = SourceTree(dict(tree.policy), root)
        after = changed.surface_digest()

        other_policy = dict(tree.policy)
        other_policy["release"] = tree.release + "+different"
        other_release = SourceTree(other_policy, root)
        return {
            "same_bytes_verified": before_verifies,
            "changed_bytes_rejected": not changed.verify(reference),
            "changed_surface_digest": before != after,
            "different_release_rejected": not other_release.verify(reference),
        }


def qualify(source_root: Path, *, release: str, task_path: Path = TASK_SET) -> dict[str, Any]:
    task_set = load_task_set(task_path)
    tree = odoo_source_tree(source_root.resolve(), release=release)
    if not tree.available():
        raise ValueError(f"source root does not exist: {source_root}")

    compile_started = time.perf_counter()
    source_map = OdooCompiler(
        tree, f"qualification:{task_set.sha256}", modules=task_set.modules,
    ).compile()
    compile_ms = (time.perf_counter() - compile_started) * 1000

    index_started = time.perf_counter()
    index = SourceFactIndex(source_map)
    index_ms = (time.perf_counter() - index_started) * 1000

    results = []
    latencies = []
    for task in task_set.tasks:
        started = time.perf_counter()
        hits = index.search(task.query, limit=task_set.top_k)
        latency = (time.perf_counter() - started) * 1000
        latencies.append(latency)
        rank = rank_task(task, hits)
        target = hits[rank - 1] if rank is not None else None
        results.append({
            "id": task.id,
            "category": task.category,
            "query": task.query,
            "rank": rank,
            "target": None if target is None else {
                "kind": target.relation.kind,
                "subject": target.relation.subject,
                "object": target.relation.object,
                "path": target.evidence.path,
                "start_line": target.evidence.start_line,
                "end_line": target.evidence.end_line,
                "sha256": target.evidence.sha256,
            },
            "latency_ms": round(latency, 3),
        })

    evidence = source_map.evidence_index()
    citation_failures = sorted(reference.key for reference in evidence.values()
                               if not tree.verify(reference))
    scope_violations = sorted({
        reference.path for reference in evidence.values()
        if module_of(reference.path) not in task_set.modules or not tree.permits(reference.path)
    })
    # Authored access-control declarations are intentionally in the P8.5 source surface;
    # they are static declarations, never proof of this actor's effective permission.
    forbidden_segments = {"tests", "demo", "data", "i18n", "secrets"}
    excluded_path_violations = sorted({
        reference.path for reference in evidence.values()
        if forbidden_segments.intersection(Path(reference.path).parts)
        or Path(reference.path).suffix in {".key", ".pem", ".p12", ".sql", ".db", ".sqlite"}
        or Path(reference.path).name.startswith(".env")
        or Path(reference.path).name.endswith("_demo.xml")
    })

    first_target = next((item["target"] for item in results if item["target"] is not None), None)
    if first_target is not None:
        drift_ref = evidence[
            f"{first_target['path']}:{first_target['start_line']}-{first_target['end_line']}"]
    else:
        drift_ref = next(iter(evidence.values()), None)
    drift = (_drift_probe(tree, drift_ref) if drift_ref is not None else {
        "same_bytes_verified": False,
        "changed_bytes_rejected": False,
        "changed_surface_digest": False,
        "different_release_rejected": False,
    })
    category_metrics = {}
    for category in sorted(_CATEGORIES):
        group = [item for item in results if item["category"] == category]
        category_metrics[category] = {
            "tasks": len(group),
            "top_5": sum(item["rank"] is not None and item["rank"] <= 5 for item in group),
            "top_10": sum(item["rank"] is not None for item in group),
        }

    top_5 = sum(item["rank"] is not None and item["rank"] <= 5 for item in results)
    top_10 = sum(item["rank"] is not None for item in results)
    gates = {
        "recall_at_10": top_10 / len(results) >= task_set.minimum_recall_at_10,
        "all_citations_verify": not citation_failures,
        "installed_module_scope_only": not scope_violations,
        "excluded_paths_absent": not excluded_path_violations,
        "byte_and_release_drift_detected": all(drift.values()),
    }
    encoded_map = canonical_json(source_map.to_json()).encode()
    return {
        "schema_version": 1,
        "suite_id": task_set.suite_id,
        "task_set_sha256": task_set.sha256,
        "evidence_class": task_set.evidence_class,
        "release": release,
        "source_surface_sha256": tree.surface_digest(),
        "source_map_sha256": source_map.content_digest,
        "modules": sorted(task_set.modules),
        "relations": len(source_map.relations),
        "facts": len(index),
        "unsupported": len(source_map.unsupported),
        "source_map_bytes": len(encoded_map),
        "compile_ms": round(compile_ms, 3),
        "index_ms": round(index_ms, 3),
        "query_ms": {
            "min": round(min(latencies), 3),
            "median": round(statistics.median(latencies), 3),
            "p95": round(_percentile(latencies, 0.95), 3),
            "max": round(max(latencies), 3),
        },
        "retrieval": {
            "tasks": len(results), "top_5": top_5, "top_10": top_10,
            "minimum_recall_at_10": task_set.minimum_recall_at_10,
            "by_category": category_metrics,
        },
        "citations": {"unique": len(evidence), "failures": citation_failures},
        "scope": {
            "violations": scope_violations,
            "excluded_path_violations": excluded_path_violations,
        },
        "drift_probe": drift,
        "gates": gates,
        "passed": all(gates.values()),
        "tasks": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--tasks", type=Path, default=TASK_SET)
    args = parser.parse_args(argv)
    result = qualify(args.source_root, release=args.release, task_path=args.tasks)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
