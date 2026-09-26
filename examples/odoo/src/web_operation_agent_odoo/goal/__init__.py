"""Odoo's goal port: index compilation and deterministic resolution (see ``port``)."""
from .port import OdooGoalPort
from .text import Ambiguous

__all__ = ["Ambiguous", "OdooGoalPort"]
