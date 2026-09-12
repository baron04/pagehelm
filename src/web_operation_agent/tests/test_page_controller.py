"""The pinned PageController build: stated once, and checked wherever it is restated.

This exists because the project had the version written in prose in two places and both were
wrong. `THIRD_PARTY.md` and the Runtime's own name said 1.12.2; every qualified environment
served 1.12.4 byte for byte, and nothing noticed because a module URL does not attest what
answered it. The failure mode is not a broken build -- it is a correct-looking licence
inventory and version claim for software nobody is running.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.page_controller import (PINNED_PAGE_CONTROLLER,
                                                 PageControllerAssetError, pinned_version,
                                                 require_served_directory, runtime_name,
                                                 verify_served_directory)


_ROOT = Path(__file__).resolve().parents[3]
_EMBEDDED = Path(__file__).resolve().parents[1] / "web_runtime" / "embedded.js"


class ThePinIsWellFormed(unittest.TestCase):
    def test_every_served_file_carries_a_tarball_path_and_a_digest(self):
        served = PINNED_PAGE_CONTROLLER["served_files"]
        self.assertIn("page-controller.js", served)
        for name, entry in served.items():
            with self.subTest(name=name):
                self.assertTrue(entry["tarball_path"].startswith("package/"))
                self.assertRegex(entry["sha256"], r"\A[0-9a-f]{64}\Z")

    def test_the_licence_travels_with_the_asset(self):
        """Serving it redistributes it, and MIT requires the notice to go along."""
        self.assertIn("LICENSE", PINNED_PAGE_CONTROLLER["served_files"])
        self.assertEqual(PINNED_PAGE_CONTROLLER["license"], "MIT")

    def test_the_tarball_url_names_the_pinned_version(self):
        self.assertIn(pinned_version(), PINNED_PAGE_CONTROLLER["registry_tarball"])
        self.assertTrue(PINNED_PAGE_CONTROLLER["integrity"].startswith("sha512-"))


class NobodyRestatesTheVersionWithoutBeingChecked(unittest.TestCase):
    """Each of these used to be an independent copy of the same fact. Now they are compared."""

    def test_the_runtime_name_is_derived_from_the_pin(self):
        self.assertEqual(runtime_name(), f"woa-embedded/page-controller-{pinned_version()}")

    def test_the_shipped_runtime_reports_the_pinned_build(self):
        found = re.search(r"const RUNTIME_NAME = '([^']+)'", _EMBEDDED.read_text())
        self.assertIsNotNone(found, "embedded.js no longer declares a RUNTIME_NAME")
        self.assertEqual(found.group(1), runtime_name())

    def test_the_third_party_notice_states_the_pinned_version(self):
        notice = (_ROOT / "THIRD_PARTY.md").read_text()
        rows = [line for line in notice.splitlines()
                if "@page-agent/page-controller" in line and line.startswith("|")]
        self.assertTrue(rows, "THIRD_PARTY.md no longer inventories the PageController")
        for row in rows:
            with self.subTest(row=row[:60]):
                self.assertIn(pinned_version(), row,
                              "the notice names a build this Runtime is not qualified against")

    def test_the_evaluation_baseline_locks_the_same_build(self):
        """The baseline and the product must not drift apart unnoticed either.

        They are separate boundaries -- the baseline bundles PageController, the product
        serves it -- but a comparison run against a different DOM implementation than the
        product's would be measuring two applications, not two engines.
        """
        lock = json.loads((_ROOT / "evals" / "pageagent" / "package-lock.json").read_text())
        entries = [entry for name, entry in lock["packages"].items()
                   if name.endswith("@page-agent/page-controller")]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["version"], pinned_version())
        self.assertEqual(entries[0]["integrity"], PINNED_PAGE_CONTROLLER["integrity"])


class ADeploymentIsCheckedAgainstThePinRatherThanTrusted(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.served = Path(self._tmp.name)
        self.source = (_ROOT / "evals" / "pageagent" / "node_modules" / "@page-agent"
                       / "page-controller")

    def tearDown(self):
        self._tmp.cleanup()

    def _install(self) -> None:
        if not self.source.is_dir():
            self.skipTest("needs the pinned npm package; run npm ci in evals/pageagent")
        for name, entry in PINNED_PAGE_CONTROLLER["served_files"].items():
            origin = self.source / entry["tarball_path"].split("package/", 1)[1]
            shutil.copyfile(origin, self.served / name)

    def test_the_pinned_package_satisfies_the_pin(self):
        """The generator and the checker must agree, or one of them is wrong."""
        self._install()
        report = require_served_directory(self.served)
        self.assertTrue(report["verified"], report["problems"])
        self.assertEqual(report["version"], pinned_version())

    def test_a_missing_file_is_named_rather_than_ignored(self):
        self._install()
        (self.served / "SimulatorMask-Dq2GJqJc.js").unlink()
        report = verify_served_directory(self.served)
        self.assertFalse(report["verified"])
        self.assertEqual(report["problems"], ["SimulatorMask-Dq2GJqJc.js is not served"])

    def test_a_patched_asset_is_refused_even_though_it_is_present(self):
        """A served file that merely exists proves nothing; the digest is the claim."""
        self._install()
        target = self.served / "page-controller.js"
        target.write_bytes(target.read_bytes() + b"\n// locally patched\n")
        with self.assertRaisesRegex(PageControllerAssetError, "page-controller.js is served as"):
            require_served_directory(self.served)

    def test_an_empty_directory_names_every_file_it_owes(self):
        report = verify_served_directory(self.served)
        self.assertFalse(report["verified"])
        self.assertEqual(len(report["problems"]),
                         len(PINNED_PAGE_CONTROLLER["served_files"]))

    def test_the_report_carries_no_file_contents(self):
        """It is written into a deployment record, so it holds digests and names only."""
        self._install()
        text = json.dumps(verify_served_directory(self.served))
        self.assertNotIn("PageController", text)
        for entry in PINNED_PAGE_CONTROLLER["served_files"].values():
            self.assertIn(entry["sha256"], text)


if __name__ == "__main__":
    unittest.main()
