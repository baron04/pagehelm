"""Twenty CRM distribution: the application adapter and its Business Skills that used to
live inside the core package.

The core Agent has no Twenty-specific code; this package is a concrete
``ApplicationAdapter`` built against Twenty's own delegated-OAuth REST API.
"""
from __future__ import annotations

__version__ = "0.1.0"

from .adapter import (CREATE_COMPANY_SCHEMA, QUERY_COMPANIES_SCHEMA,
                      QUERY_OPPORTUNITIES_SCHEMA, QUERY_PEOPLE_SCHEMA,
                      TwentyApplicationAdapter, TwentyBusinessToolExecutor,
                      TwentyPageSessionIdentityResolver, normalize_twenty_domain)

__all__ = [
    "CREATE_COMPANY_SCHEMA",
    "QUERY_COMPANIES_SCHEMA",
    "QUERY_OPPORTUNITIES_SCHEMA",
    "QUERY_PEOPLE_SCHEMA",
    "TwentyApplicationAdapter",
    "TwentyBusinessToolExecutor",
    "TwentyPageSessionIdentityResolver",
    "normalize_twenty_domain",
]
