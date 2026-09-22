from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from evals.source_operations.contacts_semantic_form import source_capability


class FixedFactGateTests(unittest.TestCase):
    def test_oracle_returns_the_exact_release_fact_without_ranking(self):
        with tempfile.TemporaryDirectory() as directory:
            source = source_capability(Path(directory), "release-supplier",
                                       "Orion Supplier", oracle=True)
            lookup = source.fact_lookup()
            self.assertEqual(lookup.release, source.source_map.release)
            for query in ("vendor Orion", "unrelated terms"):
                hits = lookup.search(query, limit=5)
                self.assertEqual(len(hits), 1)
                self.assertIn("Orion Supplier", hits[0].line)
                self.assertEqual(hits[0].evidence.release, "release-supplier")


if __name__ == "__main__":
    unittest.main()
