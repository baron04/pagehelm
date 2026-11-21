"""Re-export core untrusted API redaction and fencing helpers for the Yudao adapter."""
from __future__ import annotations

from web_operation_agent.execution.turn_tables import (
    BEGIN_UNTRUSTED as BEGIN,
    END_UNTRUSTED as END,
    REDACTED,
    compose_fenced_lines as composed,
    fence_untrusted_data as fenced,
    program_lines,
    redact_api_content as redacted,
)

__all__ = [
    "BEGIN",
    "END",
    "REDACTED",
    "composed",
    "fenced",
    "program_lines",
    "redacted",
]
