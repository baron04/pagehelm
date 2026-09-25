"""Odoo's goal port: index compilation and deterministic resolution (see ``port``)."""
from .port import OdooGoalPort
from .text import ALIASES, Ambiguous

__all__ = ["ALIASES", "Ambiguous", "OdooGoalPort"]
