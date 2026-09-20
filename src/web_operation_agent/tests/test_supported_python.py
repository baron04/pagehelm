"""One statement of which interpreters this runs on (P7.5, owner ruling D15).

Five modules carried "3.12" as a literal and the four distributions repeated it in their
metadata. Widening the range meant finding all nine, and missing one turned into a demo that
refuses to start on an interpreter the suite passes on.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from web_operation_agent import (SUPPORTED_PYTHON, python_is_supported,
                                 supported_python_text)

ROOT = Path(__file__).resolve().parents[3]
PROJECTS = ("pyproject.toml", "examples/odoo/pyproject.toml")


class TheRangeIsStatedOnce(unittest.TestCase):
    def test_every_distribution_declares_the_tested_range(self):
        (low_major, low_minor), (high_major, high_minor) = SUPPORTED_PYTHON
        expected = f'">={low_major}.{low_minor},<{high_major}.{high_minor}"'
        for name in PROJECTS:
            path = ROOT / name
            if not path.exists():  # a distribution may be absent from a wheel-only checkout
                continue
            with self.subTest(project=name):
                declared = re.search(r"^requires-python = (.+)$", path.read_text(),
                                     re.MULTILINE)
                self.assertIsNotNone(declared, name)
                self.assertEqual(declared.group(1), expected)

    def test_every_tested_minor_version_is_a_declared_classifier(self):
        (major, low), (_high_major, high) = SUPPORTED_PYTHON
        for name in PROJECTS:
            path = ROOT / name
            if not path.exists():
                continue
            text = path.read_text()
            for minor in range(low, high):
                with self.subTest(project=name, version=f"{major}.{minor}"):
                    self.assertIn(
                        f'"Programming Language :: Python :: {major}.{minor}"', text)

    def test_no_module_carries_its_own_interpreter_check(self):
        offenders = []
        for path in sorted((ROOT / "src").rglob("*.py")) + sorted(
                (ROOT / "examples").rglob("*.py")):
            if "/tests/" in str(path) or "/build/" in str(path):
                continue
            if re.search(r"version_info\[:2\]\s*[!=]=\s*\(", path.read_text()):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])

    def test_the_boundaries_are_what_they_say(self):
        (major, low), (_high_major, high) = SUPPORTED_PYTHON
        self.assertTrue(python_is_supported((major, low)))
        self.assertTrue(python_is_supported((major, high - 1)))
        self.assertFalse(python_is_supported((major, low - 1)))
        self.assertFalse(python_is_supported((major, high)))
        self.assertIn(f"{major}.{low}", supported_python_text())


if __name__ == "__main__":
    unittest.main()
