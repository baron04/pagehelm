"""The reference yudao deployment's conventions: what its organisation means by some words.

Reviewed by the owner; loaded by `YudaoApplication` unless another file, or none, is given.
"""
from __future__ import annotations

from pathlib import Path

#: The reference deployment's conventions.
REFERENCE_CONVENTIONS = Path(__file__).with_name("reference.json")

__all__ = ["REFERENCE_CONVENTIONS"]
