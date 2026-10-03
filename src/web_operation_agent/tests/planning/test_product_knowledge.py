from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from web_operation_agent.planning.product_knowledge import (
    KnowledgeGuide, KnowledgeSection, ProductKnowledgeLookup,
)
from web_operation_agent.planning.repository_source import RepositorySourceLookup
from web_operation_agent.planning.sourcetree import SourceTree
from web_operation_agent.composition.sources import SourceCapability
from web_operation_agent.goal.source_answers import retrieve, focus_preview, _verify_answers
from web_operation_agent.state.ledger import SessionLedger


class ProductKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tree = SourceTree({"repository_id": "contacts/rest", "release": "contacts-2",
                               "allow_globs": ["src/**"], "deny_globs": ["**/.env*"]}, self.root)
        (self.root / "src").mkdir()
        (self.root / "src/new.py").write_text(
            "def new_contact(name):\n    if not name: raise ValueError('name required')\n")
        (self.root / "src/retention.toml").write_text("archive_after_days = 45\n")
        self.ref = self.tree.cite("src/new.py", 1, 2, excerpt_chars=4000)
        self.guide = KnowledgeGuide("contact-create", "Create a contact", (
            KnowledgeSection("Creation rejects an empty name.", (self.ref,)),))

    def test_python_rest_guide_and_uncompiled_config_remain_cited(self):
        lookup = ProductKnowledgeLookup(self.tree, (self.guide,),
                                        fallback=RepositorySourceLookup(self.tree))
        self.assertEqual("src/new.py", lookup.search("create contact")[0].evidence.path)
        self.assertTrue(all(lookup.verify(h.evidence) for h in lookup.search("create contact")))
        self.assertEqual("src/retention.toml", lookup.search("archive after days")[0].evidence.path)

    def test_guide_text_is_part_of_the_source_capability_digest(self):
        other = replace(self.guide, title="Contact creation")
        left = ProductKnowledgeLookup(self.tree, (self.guide,))
        right = ProductKnowledgeLookup(self.tree, (other,))
        self.assertNotEqual(SourceCapability(runtime_source=True, lookup=left).content_digest,
                            SourceCapability(runtime_source=True, lookup=right).content_digest)

    def test_mutation_or_deletion_invalidates_whole_selected_guide(self):
        for change in ("modify", "delete"):
            with self.subTest(change=change):
                file = self.root / "src/new.py"
                file.write_text(self.ref.excerpt + "\n")
                lookup = ProductKnowledgeLookup(self.tree, (self.guide,))
                if change == "modify":
                    file.write_text("def new_contact(name): pass\n")
                else:
                    file.unlink()
                with self.assertRaises(ValueError):
                    lookup.search("contact")

    def test_forged_excerpt_or_other_release_cannot_be_loaded(self):
        for ref in (replace(self.ref, excerpt="invented proof"),
                    replace(self.ref, release="other"),
                    replace(self.ref, path="src/.env")):
            with self.subTest(ref=ref):
                guide = replace(self.guide, sections=(KnowledgeSection("draft", (ref,)),))
                with self.assertRaises(ValueError):
                    ProductKnowledgeLookup(self.tree, (guide,))

    def test_summary_reaches_focus_but_is_not_a_citable_support(self):
        guide = replace(self.guide, sections=(KnowledgeSection(
            "Untrusted draft says arbitrary invented permissions", (self.ref,)),))
        lookup = ProductKnowledgeLookup(self.tree, (guide,))
        source = SourceCapability(runtime_source=True, lookup=lookup)
        requests = ({"question": "contact creation", "queries": ("contact",),
                     "history": False},)
        body, evidence = retrieve(source, requests, SessionLedger(self.root / "audit.jsonl", "test"))
        question = body["questions"][0]
        preview = focus_preview(question, ("contact",))
        self.assertIn("invented permissions", preview["matches"][0]["derived_summary"])
        supports = question["evidence"][0]["supports"]
        self.assertNotIn("invented permissions", " ".join(s["text"] for s in supports))
        with self.assertRaises(ValueError):
            _verify_answers({"complete_0": True, "statement_0": "invented permission",
                             "references_0": ["derived_summary"]}, [question], evidence)

    def test_bounds_unique_keys_and_same_release_fallback(self):
        with self.assertRaises(ValueError):
            ProductKnowledgeLookup(self.tree, (self.guide, self.guide))
        for limit in (True, 0, 21):
            with self.assertRaises(ValueError):
                ProductKnowledgeLookup(self.tree, (self.guide,)).search("contact", limit=limit)
        with self.assertRaises(ValueError):
            KnowledgeSection("draft", ())

    def test_explicit_source_refinement_can_replace_the_initial_topic(self):
        other = replace(self.guide, key="src/other.py", title="Unrelated workflow")
        exact = replace(self.guide, key="src/new.py")
        lookup = ProductKnowledgeLookup(self.tree, (other, exact))
        self.assertEqual("src/new.py", lookup.rank("new.py actual validation")[0].key)


if __name__ == "__main__":
    unittest.main()
