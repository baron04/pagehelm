"""The runtime version guard and distribution requirements share one range."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from web_operation_agent import (SUPPORTED_PYTHON, python_is_supported,
                                 supported_python_text)

ROOT = Path(__file__).resolve().parents[3]
PROJECTS = ("pyproject.toml", "examples/odoo/pyproject.toml", "examples/yudao/pyproject.toml")


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



    def test_the_boundaries_are_what_they_say(self):
        (major, low), (_high_major, high) = SUPPORTED_PYTHON
        self.assertTrue(python_is_supported((major, low)))
        self.assertTrue(python_is_supported((major, high - 1)))
        self.assertFalse(python_is_supported((major, low - 1)))
        self.assertFalse(python_is_supported((major, high)))
        self.assertIn(f"{major}.{low}", supported_python_text())


if __name__ == "__main__":
    unittest.main()
