"""Run the generic repository index against the frozen Odoo 18 P8.2 questions."""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from pathlib import Path

from evals.odoo_source.qualification import load_task_set, rank_task
from web_operation_agent.planning.repository_source import RepositorySourceLookup
from web_operation_agent_odoo.sourcemap import OdooCompiler, module_of, odoo_source_tree


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)] if ordered else 0.0


def qualify(source_root: Path, *, release: str) -> dict:
    tasks = load_task_set()
    tree = odoo_source_tree(source_root.resolve(), release=release)
    if not tree.available():
        raise ValueError(f"source root does not exist: {source_root}")
    source_map = OdooCompiler(
        tree, f"repository-qualification:{tasks.sha256}", modules=tasks.modules,
    ).compile()
    relation_targets = {}
    from web_operation_agent.planning.source_facts import SourceFactIndex
    structured = SourceFactIndex(source_map)
    for task in tasks.tasks:
        facts = structured.search(task.query, limit=tasks.top_k)
        rank = rank_task(task, facts)
        if rank is None:
            raise ValueError(f"structured baseline no longer locates {task.id}")
        relation_targets[task.id] = facts[rank - 1].evidence

    started = time.perf_counter()
    lookup = RepositorySourceLookup(
        tree, path_filter=lambda path: module_of(path) in tasks.modules)
    build_ms = (time.perf_counter() - started) * 1000
    results = []
    latencies = []
    citations = {}
    for task in tasks.tasks:
        before = time.perf_counter()
        hits = lookup.search(task.query, limit=10)
        latency = (time.perf_counter() - before) * 1000
        latencies.append(latency)
        expected = relation_targets[task.id]
        rank = next((offset for offset, hit in enumerate(hits, 1)
                     if hit.evidence.path == expected.path
                     and hit.evidence.start_line <= expected.start_line
                     and hit.evidence.end_line >= expected.start_line), None)
        for hit in hits:
            citations[hit.evidence.key] = hit.evidence
        results.append({
            "id": task.id, "category": task.category, "query": task.query,
            "rank": rank, "expected": {
                "path": expected.path, "start_line": expected.start_line,
                "end_line": expected.end_line,
            }, "latency_ms": round(latency, 3),
        })
    failures = sorted(key for key, ref in citations.items() if not tree.verify(ref))
    scope = sorted({ref.path for ref in citations.values()
                    if module_of(ref.path) not in tasks.modules or not tree.permits(ref.path)})
    top5 = sum(item["rank"] is not None and item["rank"] <= 5 for item in results)
    top10 = sum(item["rank"] is not None for item in results)
    return {
        "suite_id": "p8-generic-index-on-frozen-odoo-v1",
        "evidence_class": ("existing frozen Odoo challenge targets projected to raw code "
                           "blocks; not an external blind set"),
        "task_set_sha256": tasks.sha256, "release": release, "tasks": len(results),
        "top_5": top5, "top_10": top10,
        "recall_at_5": round(top5 / len(results), 4),
        "recall_at_10": round(top10 / len(results), 4),
        "index": {**lookup.stats, "build_ms": round(build_ms, 3)},
        "latency_ms": {"median": round(statistics.median(latencies), 3),
                       "p95": round(_percentile(latencies, .95), 3),
                       "maximum": round(max(latencies), 3)},
        "citation_failures": failures, "scope_violations": scope,
        "passed": top10 / len(results) >= .8 and not failures and not scope,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--release", required=True)
    args = parser.parse_args()
    report = qualify(args.source_root, release=args.release)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
