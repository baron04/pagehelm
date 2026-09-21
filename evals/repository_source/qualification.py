"""Qualify generic repository retrieval and Git history without a model call.

The frozen fixture is a contract qualification, not evidence of natural-language answer
quality.  A separate invocation against a real application tree measures scale and recall
on application facts; keeping those claims separate prevents a purpose-built fixture from
being reported as product accuracy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import statistics
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from web_operation_agent.planning.repository_source import RepositorySourceLookup
from web_operation_agent.planning.source_history import GitSourceHistory
from web_operation_agent.planning.sourcetree import SourceTree


ROOT = Path(__file__).resolve().parent
TASK_SET = ROOT / "tasks-p8-v1.json"
FIXTURE = ROOT / "fixture"
CATEGORIES = frozenset({
    "permission", "error", "maintainer", "history", "rule", "implementation",
})


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    category: str
    query: str
    expected_path: str
    expected_marker: str
    line: int


@dataclass(frozen=True, slots=True)
class TaskSet:
    suite_id: str
    evidence_class: str
    minimum_recall_at_10: float
    tasks: tuple[Task, ...]
    sha256: str


def load_task_set(path: Path = TASK_SET) -> TaskSet:
    raw = path.read_bytes()
    body = json.loads(raw)
    expected = {"schema_version", "suite_id", "status", "evidence_class",
                "source_language", "minimum_recall_at_10", "tasks"}
    if set(body) != expected:
        raise ValueError("repository task set has unexpected fields")
    if body["schema_version"] != 1 or body["status"] != "frozen-before-first-run":
        raise ValueError("repository task set must be frozen schema 1")
    if body["source_language"] != "en":
        raise ValueError("the zero-model source query language is English")
    tasks = []
    for offset, value in enumerate(body["tasks"]):
        fields = {"id", "category", "query", "expected_path", "expected_marker", "line"}
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError(f"task {offset} has unexpected fields")
        task = Task(**value)
        if task.category not in CATEGORIES or not task.id or not task.query.strip():
            raise ValueError(f"task {task.id!r} is invalid")
        if (task.expected_path.startswith("/") or ".." in task.expected_path.split("/")
                or not task.expected_marker or not isinstance(task.line, int) or task.line < 1):
            raise ValueError(f"task {task.id!r} has an invalid target")
        tasks.append(task)
    counts = Counter(task.category for task in tasks)
    if len(tasks) != 60 or any(counts[name] != 10 for name in CATEGORIES):
        raise ValueError(f"the frozen set requires ten tasks in six categories: {dict(counts)}")
    if len({task.id for task in tasks}) != len(tasks):
        raise ValueError("task ids must be unique")
    threshold = body["minimum_recall_at_10"]
    if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
            or not 0 < threshold <= 1):
        raise ValueError("minimum_recall_at_10 must be in (0, 1]")
    return TaskSet(body["suite_id"], body["evidence_class"], float(threshold),
                   tuple(tasks), hashlib.sha256(raw).hexdigest())


def _git(root: Path, *args: str) -> str:
    run = subprocess.run(("git", "-C", str(root), *args), check=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env={"PATH": "/usr/bin:/bin:/usr/local/bin", "LC_ALL": "C"})
    return run.stdout.decode("utf-8", "replace").strip()


def _build_repository(root: Path) -> tuple[str, str]:
    for item in FIXTURE.iterdir():
        if item.name != "history.txt":
            shutil.copy2(item, root / item.name)
    (root / "history.txt").write_text("", encoding="utf-8")
    # Deliberately present but outside the allowlist.
    (root / "tests").mkdir()
    (root / "tests/secret.py").write_text("scope_leak_test_marker = True\n", encoding="utf-8")
    (root / "demo").mkdir()
    (root / "demo/records.xml").write_text("<secret>scope_leak_demo_marker</secret>\n",
                                            encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Repository Fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "add", "CODEOWNERS", "permissions.xml", "errors.py", "maintainers.toml",
         "rules.yaml", "implementation.js", "history.txt")
    _git(root, "commit", "-q", "-m", "install repository fixture")
    first = _git(root, "rev-parse", "HEAD")
    source_lines = (FIXTURE / "history.txt").read_text(encoding="utf-8").splitlines()
    target = root / "history.txt"
    for number, line in enumerate(source_lines, 1):
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        word = line.split("_", 1)[1].split()[0]
        _git(root, "config", "user.name", f"History {word.title()}")
        _git(root, "add", "history.txt")
        _git(root, "commit", "-q", "-m", f"history {word}")
    return first, _git(root, "rev-parse", "HEAD")


def _policy(release: str) -> dict:
    return {
        "repository_id": "fixture/application", "release": release,
        "allow_globs": ["*.py", "*.js", "*.xml", "*.yaml", "*.toml", "*.txt",
                        "CODEOWNERS"],
        "deny_globs": ["tests/**", "demo/**", "**/.env*", "**/*.key", "**/*.pem"],
        "max_bytes_per_read": 128 * 1024,
    }


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)] if ordered else 0.0


def qualify(task_path: Path = TASK_SET) -> dict[str, Any]:
    task_set = load_task_set(task_path)
    with tempfile.TemporaryDirectory(prefix="woa-repository-qualification-") as tmp:
        root = Path(tmp)
        first_commit, deployed_commit = _build_repository(root)
        tree = SourceTree(_policy("fixture-release:" + deployed_commit), root)

        started = time.perf_counter()
        lookup = RepositorySourceLookup(tree, line_limit=8, char_limit=1600, overlap=2)
        index_ms = (time.perf_counter() - started) * 1000
        history = GitSourceHistory(tree, deployed_commit)

        results = []
        latencies = []
        held_hits = []
        for task in task_set.tasks:
            before = time.perf_counter()
            if task.category == "history":
                hits = history.search("blame", path=task.expected_path,
                                      start_line=task.line, end_line=task.line, limit=10)
                ranks = [rank for rank, hit in enumerate(hits, 1)
                         if task.expected_marker in hit.statement.lower()]
                rank = ranks[0] if ranks else None
                target = None if rank is None else hits[rank - 1]
                citation = None if target is None else {
                    "kind": target.kind, "path": target.path,
                    "start_line": target.start_line, "end_line": target.end_line,
                    "commit": target.commit, "sha256": target.digest,
                }
            else:
                hits = lookup.search(task.query, limit=10)
                held_hits.extend(hits)
                ranks = [rank for rank, hit in enumerate(hits, 1)
                         if hit.evidence.path == task.expected_path
                         and hit.evidence.start_line <= task.line <= hit.evidence.end_line
                         and task.expected_marker in hit.evidence.excerpt]
                rank = ranks[0] if ranks else None
                target = None if rank is None else hits[rank - 1]
                citation = None if target is None else {
                    "kind": target.fact_type, "path": target.evidence.path,
                    "start_line": target.evidence.start_line,
                    "end_line": target.evidence.end_line,
                    "sha256": target.evidence.sha256,
                }
            latency = (time.perf_counter() - before) * 1000
            latencies.append(latency)
            results.append({"id": task.id, "category": task.category,
                            "query": task.query, "rank": rank,
                            "target": citation, "latency_ms": round(latency, 3)})

        unique_refs = {hit.evidence.key: hit.evidence for hit in held_hits}
        citation_failures = sorted(key for key, ref in unique_refs.items()
                                   if not tree.verify(ref))
        scope_violations = sorted(path for path in tree.files()
                                  if path.startswith(("tests/", "demo/")))
        leak_queries = {
            marker: sum(marker in hit.evidence.excerpt
                        for hit in lookup.search(marker.replace("_", " "), limit=10))
            for marker in ("scope_leak_test_marker", "scope_leak_demo_marker")
        }
        fact_type_violations = sorted({
            hit.fact_type for hit in held_hits if hit.fact_type != "implementation"
        })

        # A different checked-out commit must not be accepted as the deployment commit.
        binding_rejected = False
        try:
            GitSourceHistory(tree, first_commit)
        except ValueError:
            binding_rejected = True

        drift_reference = next(iter(unique_refs.values()))
        other = SourceTree(_policy("another-release"), root)
        release_drift_rejected = not other.verify(drift_reference)
        before_digest = tree.surface_digest()
        target_path = root / drift_reference.path
        lines = target_path.read_text(encoding="utf-8").splitlines(keepends=True)
        changed_line = drift_reference.start_line - 1
        ending = "\n" if lines[changed_line].endswith("\n") else ""
        body = lines[changed_line][:-1] if ending else lines[changed_line]
        lines[changed_line] = body + " " + ending
        target_path.write_text("".join(lines), encoding="utf-8")
        changed = SourceTree(_policy(tree.release), root)
        byte_drift_rejected = not changed.verify(drift_reference)
        surface_drift_detected = changed.surface_digest() != before_digest

        top5 = sum(item["rank"] is not None and item["rank"] <= 5 for item in results)
        top10 = sum(item["rank"] is not None for item in results)
        categories = {}
        for name in sorted(CATEGORIES):
            group = [item for item in results if item["category"] == name]
            categories[name] = {
                "tasks": len(group),
                "top_5": sum(item["rank"] is not None and item["rank"] <= 5
                             for item in group),
                "top_10": sum(item["rank"] is not None for item in group),
            }
        gates = {
            "recall_at_10": top10 / len(results) >= task_set.minimum_recall_at_10,
            "all_citations_verify": not citation_failures,
            "scope_leakage_zero": not scope_violations and not any(leak_queries.values()),
            "fact_types_not_elevated": not fact_type_violations,
            "deployment_commit_binding": binding_rejected,
            "byte_drift_rejected": byte_drift_rejected,
            "release_drift_rejected": release_drift_rejected,
            "surface_drift_detected": surface_drift_detected,
        }
        return {
            "suite_id": task_set.suite_id, "evidence_class": task_set.evidence_class,
            "task_set_sha256": task_set.sha256, "release": tree.release,
            "deployed_commit": deployed_commit, "tasks": len(results),
            "top_5": top5, "top_10": top10,
            "recall_at_5": round(top5 / len(results), 4),
            "recall_at_10": round(top10 / len(results), 4),
            "categories": categories,
            "index": {**lookup.stats, "build_ms": round(index_ms, 3),
                      "estimated_bytes": lookup.stats["estimated_index_bytes"]},
            "latency_ms": {"median": round(statistics.median(latencies), 3),
                           "p95": round(_percentile(latencies, .95), 3),
                           "maximum": round(max(latencies), 3)},
            "citation_failures": citation_failures,
            "scope_violations": scope_violations, "leak_queries": leak_queries,
            "fact_type_violations": fact_type_violations,
            "gates": gates, "passed": all(gates.values()), "results": results,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=Path, default=TASK_SET)
    args = parser.parse_args()
    report = qualify(args.tasks)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
