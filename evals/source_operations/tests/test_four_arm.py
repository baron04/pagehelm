from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from evals.source_operations.four_arm import _skill, _source, _tasks
from testapps.contacts.adapter import MinimalContactsAdapter
from testapps.contacts.demo import DemoConfig


class FourArmApparatusTests(unittest.TestCase):
    def test_frozen_taskset_and_source_arm_boundaries(self):
        tasks = _tasks()
        self.assertEqual(len(tasks["semantic"]), 2)
        self.assertEqual(tasks["semantic"][0]["prompt"],
                         tasks["semantic"][1]["prompt"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for arm in ("A", "B", "C", "D"):
                source, top5 = _source(root, "release-partner", "Orion Partner", arm)
                self.assertEqual(source.runtime_source, arm in {"B", "C"})
                self.assertEqual(top5, arm in {"B", "C"})
                if arm in {"B", "C"}:
                    hits = source.fact_lookup().search(
                        "official display name for vendor Orion", limit=5)
                    self.assertIn("Orion Partner", hits[0].line)
                    self.assertEqual(hits[0].verbatim, arm == "B")

    def test_manual_skill_is_isolated_and_synthetic_click_requires_opt_in(self):
        skill = _skill("release-supplier", "Orion Supplier")
        self.assertTrue(skill.read_only)
        self.assertIn("Orion Supplier", " ".join(skill.instructions))
        adapter = MinimalContactsAdapter("http://127.0.0.1:1", extra_skills=(skill,))
        self.assertEqual(adapter.business_tools.catalog_sha256 is not None, True)
        self.assertEqual(DemoConfig(extra_skills=(skill,)).extra_skills, (skill,))
        with self.assertRaisesRegex(ValueError, "click"):
            DemoConfig(start_path="/contacts/new", allow_page_click=True)


if __name__ == "__main__":
    unittest.main()
