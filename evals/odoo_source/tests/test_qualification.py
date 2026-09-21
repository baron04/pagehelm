import tempfile
import unittest
from collections import Counter
from pathlib import Path

from web_operation_agent.planning.sourcetree import SourceTree

from evals.odoo_source.qualification import (
    ExpectedRelation,
    _drift_probe,
    load_task_set,
)
from web_operation_agent.schemas import EvidenceRef, SourceRelation
from web_operation_agent.schemas import SCHEMA_VERSIONS, SourceMap
from evals.odoo_source.upgrade import diff_maps


class FrozenTaskSetTests(unittest.TestCase):
    def test_manifest_is_balanced_and_strict(self):
        task_set = load_task_set()
        self.assertEqual(40, len(task_set.tasks))
        self.assertEqual(
            {"hidden_entry": 10, "field_semantics": 10,
             "refusal_precondition": 10, "current_release_behavior": 10},
            dict(Counter(task.category for task in task_set.tasks)),
        )
        self.assertRegex(task_set.sha256, r"^[0-9a-f]{64}$")

    def test_expected_relation_can_require_an_exact_or_partial_object(self):
        reference = EvidenceRef(
            repository_id="example", release="r1", path="addons/x/models/x.py",
            start_line=1, end_line=1, sha256="0" * 64,
        )
        relation = SourceRelation(
            kind="service_guard", subject="x.write", object="Cannot change a posted x",
            evidence=(reference,),
        )
        self.assertTrue(ExpectedRelation(
            kind="service_guard", subject="x.write", object_contains="posted").matches(relation))
        self.assertFalse(ExpectedRelation(
            kind="service_guard", subject="x.write", object="other").matches(relation))


class DriftProbeTests(unittest.TestCase):
    def test_byte_and_release_changes_invalidate_a_citation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "addons/greenhouse/models/tray.py"
            path.parent.mkdir(parents=True)
            path.write_text("first\nsecond\n", encoding="utf-8")
            policy = {
                "repository_id": "example", "release": "r1",
                "allow_globs": ["addons/*/models/**/*.py"], "deny_globs": [],
            }
            tree = SourceTree(policy, root)
            reference = tree.cite("addons/greenhouse/models/tray.py", 2, 2)
            result = _drift_probe(tree, reference)
            self.assertTrue(all(result.values()), result)


class UpgradeDiffTests(unittest.TestCase):
    def test_semantic_and_citation_only_changes_are_separate(self):
        old_ref = EvidenceRef(
            repository_id="example", release="old", path="addons/x/models/x.py",
            start_line=1, end_line=1, sha256="1" * 64,
        )
        new_ref = EvidenceRef(
            repository_id="example", release="new", path="addons/x/models/x.py",
            start_line=2, end_line=2, sha256="2" * 64,
        )
        old = SourceMap(
            schema_version=SCHEMA_VERSIONS["SourceMap"], attestation_id="old",
            repository_id="example", release="old", relations=[
                SourceRelation(kind="service_entity", subject="x", object="state",
                               attrs={"selection": ["a"]}, evidence=(old_ref,)),
                SourceRelation(kind="service_guard", subject="x.write", object="no",
                               attrs={"condition": "old"}, evidence=(old_ref,)),
            ],
        )
        new = SourceMap(
            schema_version=SCHEMA_VERSIONS["SourceMap"], attestation_id="new",
            repository_id="example", release="new", relations=[
                SourceRelation(kind="service_entity", subject="x", object="state",
                               attrs={"selection": ["a"]}, evidence=(new_ref,)),
                SourceRelation(kind="service_guard", subject="x.write", object="no",
                               attrs={"condition": "new"}, evidence=(new_ref,)),
            ],
        )
        result = diff_maps(old, new)
        self.assertEqual(1, result["semantic_unchanged"])
        self.assertEqual(1, result["semantic_added"])
        self.assertEqual(1, result["semantic_removed"])
        self.assertEqual(1, result["stable_identities_changed"])
        self.assertEqual(1, result["unchanged_facts_with_rebased_citations"])


if __name__ == "__main__":
    unittest.main()
