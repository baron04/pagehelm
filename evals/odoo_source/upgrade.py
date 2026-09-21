"""Compare source facts, citations, and retrieval across two real Odoo builds."""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from web_operation_agent.planning.source_facts import SourceFactIndex
from web_operation_agent.planning.sourcetree import SourceTree
from web_operation_agent.schemas import SourceMap, SourceRelation, canonical_json
from web_operation_agent_odoo.sourcemap import OdooCompiler, odoo_source_tree

from .qualification import TASK_SET, TaskSet, load_task_set, rank_task


def _semantic(relation: SourceRelation) -> str:
    """A fact independent of line numbers and citation hashes."""
    return canonical_json({
        "kind": relation.kind,
        "subject": relation.subject,
        "object": relation.object,
        "attrs": relation.attrs,
    })


def _identity(relation: SourceRelation) -> str:
    """A deliberately coarse identity used to flag facts whose attributes changed."""
    extra = ""
    if relation.kind == "form_target":
        extra = canonical_json({
            "label": relation.attrs.get("label", ""),
            "target_type": relation.attrs.get("target_type", ""),
            "button_id": relation.attrs.get("button_id", ""),
        })
    return canonical_json([relation.kind, relation.subject, relation.object, extra])


def _evidence_shape(relation: SourceRelation) -> tuple[tuple[str, int, int, str], ...]:
    return tuple(sorted((item.path, item.start_line, item.end_line, item.sha256)
                        for item in relation.evidence))


def _examples(values: Counter[str], *, limit: int = 12) -> list[dict[str, Any]]:
    examples = []
    for encoded, count in sorted(values.items()):
        body = json.loads(encoded)
        examples.append({
            "count": count,
            "kind": body["kind"],
            "subject": body["subject"],
            "object": body["object"],
            "attrs": body["attrs"],
        })
        if len(examples) == limit:
            break
    return examples


def diff_maps(old: SourceMap, new: SourceMap) -> dict[str, Any]:
    old_facts = Counter(_semantic(relation) for relation in old.relations)
    new_facts = Counter(_semantic(relation) for relation in new.relations)
    added = new_facts - old_facts
    removed = old_facts - new_facts

    old_by_identity: dict[str, Counter[str]] = defaultdict(Counter)
    new_by_identity: dict[str, Counter[str]] = defaultdict(Counter)
    for relation in old.relations:
        old_by_identity[_identity(relation)][_semantic(relation)] += 1
    for relation in new.relations:
        new_by_identity[_identity(relation)][_semantic(relation)] += 1
    changed_identities = sorted(
        identity for identity in old_by_identity.keys() & new_by_identity.keys()
        if old_by_identity[identity] != new_by_identity[identity]
    )

    old_evidence: dict[str, Counter[tuple]] = defaultdict(Counter)
    new_evidence: dict[str, Counter[tuple]] = defaultdict(Counter)
    for relation in old.relations:
        old_evidence[_semantic(relation)][_evidence_shape(relation)] += 1
    for relation in new.relations:
        new_evidence[_semantic(relation)][_evidence_shape(relation)] += 1
    citation_rebased = sum(
        old_evidence[fact] != new_evidence[fact]
        for fact in old_evidence.keys() & new_evidence.keys()
    )

    by_kind: dict[str, dict[str, int]] = defaultdict(lambda: {"added": 0, "removed": 0})
    for encoded, count in added.items():
        by_kind[json.loads(encoded)["kind"]]["added"] += count
    for encoded, count in removed.items():
        by_kind[json.loads(encoded)["kind"]]["removed"] += count
    return {
        "old_relations": len(old.relations),
        "new_relations": len(new.relations),
        "semantic_unchanged": sum((old_facts & new_facts).values()),
        "semantic_added": sum(added.values()),
        "semantic_removed": sum(removed.values()),
        "stable_identities_changed": len(changed_identities),
        "unchanged_facts_with_rebased_citations": citation_rebased,
        "by_kind": dict(sorted(by_kind.items())),
        "added_examples": _examples(added),
        "removed_examples": _examples(removed),
    }


def _compile(root: Path, release: str, task_set: TaskSet) -> tuple[SourceTree, SourceMap, float]:
    tree = odoo_source_tree(root.resolve(), release=release)
    started = time.perf_counter()
    source_map = OdooCompiler(
        tree, f"upgrade:{task_set.sha256}:{release}", modules=task_set.modules,
    ).compile()
    return tree, source_map, (time.perf_counter() - started) * 1000


def _retrieval(source_map: SourceMap, task_set: TaskSet) -> tuple[
        dict[str, int | None], dict[str, SourceRelation]]:
    index = SourceFactIndex(source_map)
    ranks: dict[str, int | None] = {}
    targets: dict[str, SourceRelation] = {}
    for task in task_set.tasks:
        hits = index.search(task.query, limit=task_set.top_k)
        rank = rank_task(task, hits)
        ranks[task.id] = rank
        if rank is not None:
            targets[task.id] = hits[rank - 1].relation
    return ranks, targets


def compare_builds(*, old_root: Path, old_release: str, new_root: Path,
                   new_release: str, task_path: Path = TASK_SET) -> dict[str, Any]:
    if old_release == new_release:
        raise ValueError("upgrade comparison requires two distinct releases")
    task_set = load_task_set(task_path)
    old_tree, old_map, old_compile_ms = _compile(old_root, old_release, task_set)
    new_tree, new_map, new_compile_ms = _compile(new_root, new_release, task_set)
    old_ranks, old_targets = _retrieval(old_map, task_set)
    new_ranks, new_targets = _retrieval(new_map, task_set)

    old_refs = old_map.evidence_index().values()
    release_rejected = sum(not new_tree.verify(reference) for reference in old_refs)
    old_ref_count = len(old_map.evidence_index())
    old_citation_failures = sum(not old_tree.verify(reference)
                                for reference in old_map.evidence_index().values())
    new_citation_failures = sum(not new_tree.verify(reference)
                                for reference in new_map.evidence_index().values())
    same_release_policy = dict(new_tree.policy)
    same_release_policy["release"] = old_release
    new_bytes_as_old_release = SourceTree(same_release_policy, new_root.resolve())
    exact_bytes_survived = sum(new_bytes_as_old_release.verify(reference)
                               for reference in old_map.evidence_index().values())

    lost = sorted(task_id for task_id, rank in old_ranks.items()
                  if rank is not None and new_ranks[task_id] is None)
    gained = sorted(task_id for task_id, rank in old_ranks.items()
                    if rank is None and new_ranks[task_id] is not None)
    degraded = sorted(task_id for task_id, rank in old_ranks.items()
                      if rank is not None and new_ranks[task_id] is not None
                      and new_ranks[task_id] > rank)
    improved = sorted(task_id for task_id, rank in old_ranks.items()
                      if rank is not None and new_ranks[task_id] is not None
                      and new_ranks[task_id] < rank)
    shared_targets = old_targets.keys() & new_targets.keys()
    changed_target_facts = sorted(
        task_id for task_id in shared_targets
        if _semantic(old_targets[task_id]) != _semantic(new_targets[task_id])
    )
    rebased_target_citations = sorted(
        task_id for task_id in shared_targets
        if _evidence_shape(old_targets[task_id]) != _evidence_shape(new_targets[task_id])
    )

    map_diff = diff_maps(old_map, new_map)
    old_top_10 = sum(rank is not None for rank in old_ranks.values())
    new_top_10 = sum(rank is not None for rank in new_ranks.values())
    gates = {
        "old_recall_at_10": old_top_10 / len(task_set.tasks) >= task_set.minimum_recall_at_10,
        "new_recall_at_10": new_top_10 / len(task_set.tasks) >= task_set.minimum_recall_at_10,
        "old_citations_verify": old_citation_failures == 0,
        "new_citations_verify": new_citation_failures == 0,
        "all_old_citations_rejected_by_new_release": release_rejected == old_ref_count,
    }
    return {
        "schema_version": 1,
        "suite_id": task_set.suite_id,
        "task_set_sha256": task_set.sha256,
        "old": {
            "release": old_release, "surface_sha256": old_tree.surface_digest(),
            "map_sha256": old_map.content_digest, "compile_ms": round(old_compile_ms, 3),
            "top_10": old_top_10,
        },
        "new": {
            "release": new_release, "surface_sha256": new_tree.surface_digest(),
            "map_sha256": new_map.content_digest, "compile_ms": round(new_compile_ms, 3),
            "top_10": new_top_10,
        },
        "fact_diff": map_diff,
        "citation_drift": {
            "old_unique": old_ref_count,
            "old_verification_failures": old_citation_failures,
            "new_verification_failures": new_citation_failures,
            "rejected_by_new_release": release_rejected,
            "exact_path_line_bytes_survived": exact_bytes_survived,
            "changed_or_moved": old_ref_count - exact_bytes_survived,
        },
        "retrieval_drift": {
            "lost": lost, "gained": gained, "degraded": degraded, "improved": improved,
            "target_facts_changed": changed_target_facts,
            "target_citations_rebased": rebased_target_citations,
            "old_ranks": old_ranks, "new_ranks": new_ranks,
        },
        "gates": gates,
        "passed": all(gates.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-root", type=Path, required=True)
    parser.add_argument("--old-release", required=True)
    parser.add_argument("--new-root", type=Path, required=True)
    parser.add_argument("--new-release", required=True)
    parser.add_argument("--tasks", type=Path, default=TASK_SET)
    args = parser.parse_args(argv)
    result = compare_builds(
        old_root=args.old_root, old_release=args.old_release,
        new_root=args.new_root, new_release=args.new_release, task_path=args.tasks,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
