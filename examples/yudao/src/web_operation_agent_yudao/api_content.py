"""What the application's API returns, on its way into a model's context.

Two things happen to it. Credentials are blanked: the fields this adapter never reads
(`goal._SECRET` -- a channel's keys, an account's password, a token), a named setting whose
name says it is one, and the core's own credential and session hand-off forms inside any
string. And the text is fenced, so the model is told where the application's words begin
and end: a record's name or remark is written by whoever could edit it, and nothing in it
is an instruction.

Business fields the signed-in person can read in the application -- a phone number, a bank
account, an address -- are not credentials and pass unchanged; answering with them is what
the person asked for.
"""
from __future__ import annotations

import re

from web_operation_agent.privacy import BASELINE_SESSION_HANDOFFS, redact_sensitive_assignments

from .goal import _SECRET

BEGIN, END = "BEGIN_UNTRUSTED_APPLICATION_DATA", "END_UNTRUSTED_APPLICATION_DATA"
REDACTED = "[redacted]"
#: A row that is one named setting (the application's configuration list): the fields that
#: name it, and what a name says when the value beside it is a credential.
_SETTING_NAMES = ("key", "configKey", "name")
_SECRET_WORD = re.compile(r"密码|密钥|口令")
_SECRET_PROSE = re.compile(
    r"((?:初始|默认|账号)密码(?:\s*(?:配置|项|值|为|是|[：:=]))+\s*|(?:init[-_]?password|default[-_]?password)(?:\s*[|｜：:=\s])+\s*)"
    r"(?!\[redacted\])([A-Za-z0-9!@#$%^&*._\-]{4,64})",
    re.IGNORECASE,
)
_MARKER = re.compile(r"(BEGIN|END)_UNTRUSTED_")


def _blank(value):
    if isinstance(value, str):
        return _SECRET_PROSE.sub(r"\1" + REDACTED, value)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            # A number or a flag under such a name is a lifetime or a count, not a secret.
            held = isinstance(item, (str, dict, list)) and bool(item)
            out[key] = REDACTED if held and _SECRET.search(str(key)) else _blank(item)
        if isinstance(out.get("value"), (str, dict, list)) and out["value"] and any(
                isinstance(out.get(name), str) and (_SECRET.search(out[name]) or _SECRET_WORD.search(out[name]))
                for name in _SETTING_NAMES):
            out["value"] = REDACTED
        return out
    if isinstance(value, (list, tuple)):
        return [_blank(item) for item in value]
    return value


def redacted(value, handoffs=BASELINE_SESSION_HANDOFFS):
    """A deep copy of an API value (or one string of it) with every credential blanked."""
    return redact_sensitive_assignments(handoffs.redact(_blank(value)))


def fenced(text: str) -> str:
    """Application data between the markers. Marker text inside the data is defused, so
    the data cannot end its own fence."""
    return f"{BEGIN}\n{_MARKER.sub(lambda m: m.group(1) + '-UNTRUSTED-', text)}\n{END}"


def composed(lines) -> str:
    """Lines of a tool result, each `(is_data, text)`: every run of data lines is fenced,
    what the program itself says stays outside."""
    out, run = [], []
    for is_data, text in lines:
        if is_data:
            run.append(text)
            continue
        if run:
            out.append(fenced("\n".join(run)))
            run = []
        out.append(text)
    if run:
        out.append(fenced("\n".join(run)))
    return "\n".join(out)


def program_lines(text: str) -> str:
    """What the program said before the first fence of a composed result, for a place that
    keeps a result's summary and drops its data."""
    return text.split(BEGIN, 1)[0].rstrip("\n")
