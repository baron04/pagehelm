"""Compatibility imports for server-owned approval controls."""
from .business.control import ApprovalDenied, ApprovalGate, ApprovalGrant, ReadOnlyControl

__all__ = ["ApprovalDenied", "ApprovalGate", "ApprovalGrant", "ReadOnlyControl"]
