import json
import os
import tempfile
import unittest
from importlib import resources
from pathlib import Path

from web_operation_agent.sourcetree import AccessDenied, SourceTree, _glob_match

PRODUCT_POLICY = json.loads(
    resources.files("web_operation_agent").joinpath("policies/ofbiz.json").read_text()
)

# This is the reviewed package contract. Keeping it in the product test makes widening the
# source surface an explicit security change instead of inheriting another tool's registry.
EXPECTED_ALLOW_GLOBS = {
    "applications/**/webapp/**/WEB-INF/*controller*.xml",
    "applications/**/widget/**/*.xml",
    "applications/**/servicedef/*.xml",
    "applications/**/config/*UiLabels.xml",
    "framework/**/webapp/**/WEB-INF/*controller*.xml",
    "framework/**/widget/**/*.xml",
    "framework/**/servicedef/*.xml",
    "framework/**/config/*UiLabels.xml",
    "plugins/*/webapp/**/WEB-INF/*controller*.xml",
    "plugins/*/widget/**/*.xml",
}
REQUIRED_DENY_GLOBS = {
    "**/.env*", "**/*.key", "**/*.pem", "**/*.p12", "**/*.jks",
    "**/secrets/**", "**/runtime/**", "**/upload/**", "**/uploads/**",
    "**/cache/**", "**/*.sql", "**/*.sqlite", "**/*.db", "**/.git/**",
    "**/node_modules/**", "**/build/**", "**/testdef/**", "**/data/demo/**",
    "**/data/seed/**", "**/security/data/**",
}


def _tree(tmp: Path, policy_overrides=None) -> SourceTree:
    policy = {"repository_id": "fixture/app", "release": "v1",
              "max_bytes_per_read": 4096,
              "allow_globs": ["app/**/config/*.xml", "app/*.xml"],
              "deny_globs": ["**/secrets/**", "**/*.key"]}
    policy.update(policy_overrides or {})
    (tmp / "src").mkdir(parents=True, exist_ok=True)
    return SourceTree(policy, (tmp / "src").resolve())


class GlobSemantics(unittest.TestCase):
    def test_double_star_spans_zero_or_more_directories(self):
        p = "applications/**/widget/**/*.xml"
        self.assertTrue(_glob_match("applications/accounting/widget/PaymentForms.xml", p),
                        "a top-level widget file must match; missing this dropped the form "
                        "that submits the payment application")
        self.assertTrue(_glob_match("applications/accounting/widget/ar/ArMenus.xml", p))
        self.assertFalse(_glob_match("applications/accounting/entitydef/x.xml", p))

    def test_single_star_does_not_cross_a_separator(self):
        self.assertTrue(_glob_match("a/b.xml", "a/*.xml"))
        self.assertFalse(_glob_match("a/b/c.xml", "a/*.xml"))

    def test_leading_double_star(self):
        self.assertTrue(_glob_match("x/y/z/.env", "**/.env*"))
        self.assertTrue(_glob_match(".env", "**/.env*"))


class Policy(unittest.TestCase):
    def test_deny_beats_allow(self):
        with tempfile.TemporaryDirectory() as d:
            t = _tree(Path(d), {"allow_globs": ["app/**/*.xml"],
                                "deny_globs": ["**/secrets/**"]})
            self.assertTrue(t.permits("app/config/a.xml"))
            self.assertFalse(t.permits("app/secrets/a.xml"))

    def test_default_is_deny(self):
        with tempfile.TemporaryDirectory() as d:
            t = _tree(Path(d))
            self.assertFalse(t.permits("app/README.md"))

    def test_traversal_is_refused_after_resolution(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            t = _tree(tmp)
            (tmp / "outside.xml").write_text("<a/>")
            with self.assertRaises(AccessDenied):
                t.resolve("app/../../outside.xml")

    def test_symlink_out_of_the_tree_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            t = _tree(tmp)
            (tmp / "outside.xml").write_text("<a/>")
            (tmp / "src/app").mkdir(parents=True, exist_ok=True)
            link = tmp / "src/app/escape.xml"
            os.symlink(tmp / "outside.xml", link)
            with self.assertRaises(AccessDenied):
                t.resolve("app/escape.xml")

    def test_packaged_allowlist_cannot_be_widened_silently(self):
        self.assertEqual(set(PRODUCT_POLICY["allow_globs"]), EXPECTED_ALLOW_GLOBS)

    def test_packaged_policy_keeps_every_required_deny_rule(self):
        missing = REQUIRED_DENY_GLOBS - set(PRODUCT_POLICY["deny_globs"])
        self.assertEqual(missing, set(), f"product policy dropped deny rules: {sorted(missing)}")

    def test_packaged_policy_contains_no_checkout_or_runtime_version(self):
        self.assertNotIn("root", PRODUCT_POLICY)
        self.assertNotIn("application_version", PRODUCT_POLICY)

    def test_source_root_is_required_and_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            tree = SourceTree.load("ofbiz", root)
        self.assertEqual(tree.root, root)
        with self.assertRaises(TypeError):
            SourceTree.load("ofbiz")  # type: ignore[call-arg]

    def test_a_policy_cannot_smuggle_in_a_local_source_root(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaisesRegex(
                ValueError, "must not contain a local root"):
            SourceTree({**PRODUCT_POLICY, "root": ".q0-cache/old-checkout"}, Path(d).resolve())


class Citations(unittest.TestCase):
    def _fixture(self, tmp: Path) -> SourceTree:
        t = _tree(tmp)
        (tmp / "src/app").mkdir(parents=True, exist_ok=True)
        (tmp / "src/app/thing.xml").write_text("<one/>\n<two/>\n<three/>\n")
        return t

    def test_a_citation_hashes_exactly_the_cited_lines(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            t = self._fixture(tmp)
            ref = t.cite("app/thing.xml", 2, 2, excerpt_chars=50)
            self.assertEqual(ref.key, "app/thing.xml:2-2")
            self.assertEqual(ref.excerpt, "<two/>")
            self.assertTrue(t.verify(ref))

    def test_a_changed_file_invalidates_its_citations(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            t = self._fixture(tmp)
            ref = t.cite("app/thing.xml", 2, 2)
            (tmp / "src/app/thing.xml").write_text("<one/>\n<CHANGED/>\n<three/>\n")
            self.assertFalse(t.verify(ref))

    def test_a_citation_from_another_release_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            t = self._fixture(tmp)
            ref = t.cite("app/thing.xml", 1, 1)
            other = SourceTree({**t.policy, "release": "v2"}, t.root)
            self.assertFalse(other.verify(ref))

    def test_out_of_range_line_span_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            t = self._fixture(tmp)
            with self.assertRaises(Exception):
                t.cite("app/thing.xml", 1, 99)

    def test_surface_digest_changes_when_a_compiled_file_changes(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            t = self._fixture(tmp)
            first = t.surface_digest()
            (tmp / "src/app/thing.xml").write_text("<one/>\n<two/>\n<four/>\n")
            t._files = None
            self.assertNotEqual(first, t.surface_digest())

    def test_reading_over_the_byte_limit_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            t = _tree(tmp, {"max_bytes_per_read": 8})
            (tmp / "src/app").mkdir(parents=True, exist_ok=True)
            (tmp / "src/app/big.xml").write_text("x" * 100)
            with self.assertRaises(AccessDenied):
                t.read_text("app/big.xml")


if __name__ == "__main__":
    unittest.main()
