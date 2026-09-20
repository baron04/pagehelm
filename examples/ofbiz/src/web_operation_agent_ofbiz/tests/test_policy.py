"""The packaged OFBiz source policy: what it is allowed to widen, and what it must deny.

Keeping this in the product test makes widening the source surface an explicit security
change instead of inheriting another tool's registry.
"""
import json
import tempfile
import unittest
from importlib import resources
from pathlib import Path

from web_operation_agent.planning.sourcetree import SourceTree

PRODUCT_POLICY = json.loads(
    resources.files("web_operation_agent_ofbiz").joinpath("policies/ofbiz.json").read_text()
)

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


class PackagedPolicy(unittest.TestCase):
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
            tree = SourceTree.load("ofbiz", root, package="web_operation_agent_ofbiz")
        self.assertEqual(tree.root, root)
        with self.assertRaises(TypeError):
            SourceTree.load("ofbiz")  # type: ignore[call-arg]


if __name__ == "__main__":
    unittest.main()
