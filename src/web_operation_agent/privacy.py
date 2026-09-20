"""Privacy primitives shared by the browser boundary and server-side audits.

Session hand-off keys come from two places and both are needed. The baseline below is the
generic set this package knows about regardless of which application is mounted. It is not
complete for any particular deployment: an adapter declares its own application's session
parameters through ``ApplicationRoutePort.session_handoff_query_keys``, and a component that
holds an adapter must widen the baseline with them rather than assume the static list covers
its application. `SessionHandoffPolicy.extending` is how that widening is expressed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import unquote


# Apache OFBiz uses the first name for normal inter-app hand-offs. The two shorter
# spellings also occur in the bound public 24.09.07 source and carry the same secret value.
# This list is the floor, never the whole vocabulary -- see the module docstring.
SESSION_HANDOFF_KEYS = (
    "externalLoginKey",
    "externaLoginKey",
    "externLoginKey",
    "jsessionid",
)

# Generic browser credentials use a deliberately closed key vocabulary.  Matching every
# ``key=value`` pair would turn ordinary business identifiers into false privacy failures.
_SENSITIVE_CREDENTIAL_KEY_PATTERN = (
    r"(?:access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|"
    r"authorization|password|passwd|pwd|secret|token)"
)
SENSITIVE_CREDENTIAL_KEY = re.compile(
    rf"^{_SENSITIVE_CREDENTIAL_KEY_PATTERN}$", re.I)
SENSITIVE_CREDENTIAL_NAME = re.compile(_SENSITIVE_CREDENTIAL_KEY_PATTERN, re.I)
_ENCODED_URL_DELIMITER = r"%(?:25)*(?:26|23|3[bBfF])"
SENSITIVE_CREDENTIAL_ASSIGNMENT = re.compile(
    rf"(?P<prefix>(?:^|[?&#;\s\"'<>\\]|{_ENCODED_URL_DELIMITER})"
    rf"{_SENSITIVE_CREDENTIAL_KEY_PATTERN}(?:=|%(?:25)*3[dD]))"
    r"(?P<value>.+?)"
    rf"(?=(?:[&;/?#\s\"'<>\\]|{_ENCODED_URL_DELIMITER}|$))",
    re.I,
)


_HANDOFF_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{1,63}\Z")


def _handoff_assignment(keys: tuple[str, ...]) -> re.Pattern:
    """Match literal and percent-encoded `key=value` assignments for one key vocabulary.

    A backslash is a boundary so a URL embedded in a JSON string is handled without
    swallowing the suffix.
    """
    alternation = "(?:" + "|".join(re.escape(key) for key in keys) + ")"
    return re.compile(
        rf"({alternation}(?:=|%(?:25)*3[dD]))"
        r"(.+?)"
        r"(?=(?:[&;/?#\s\"<>\\]|%(?:25)*(?:26|3[bBfF]|2[fF]|23)|$))",
        re.I,
    )


@dataclass(frozen=True, slots=True)
class SessionHandoffPolicy:
    """One session-hand-off key vocabulary, and the detector/redactor built from it.

    The module-level functions use `BASELINE_SESSION_HANDOFFS`. A component that knows which
    application is mounted should use `extending(port.session_handoff_query_keys)` instead,
    so an application's own session parameter is redacted even though this package has never
    heard of it.
    """

    keys: tuple[str, ...]

    @staticmethod
    def extending(extra_keys) -> "SessionHandoffPolicy":
        """The baseline widened with one adapter's declared session parameters."""
        return _handoff_policy(frozenset(
            key for key in (extra_keys or ())
            if isinstance(key, str) and _HANDOFF_KEY.fullmatch(key)))

    @property
    def query_keys(self) -> frozenset[str]:
        return frozenset(key.lower() for key in self.keys)

    @property
    def assignment(self) -> re.Pattern:
        return _handoff_assignment(self.keys)

    def redact(self, value):
        return _redact_handoffs(value, self)

    def has_unredacted(self, value, *, url_decode_depth: int = 0) -> bool:
        return _has_unredacted_handoff(value, self, url_decode_depth=url_decode_depth)


@lru_cache(maxsize=64)
def _handoff_policy(extra: frozenset[str]) -> SessionHandoffPolicy:
    baseline = {key.lower(): key for key in SESSION_HANDOFF_KEYS}
    for key in sorted(extra):
        baseline.setdefault(key.lower(), key)
    return SessionHandoffPolicy(keys=tuple(baseline.values()))


BASELINE_SESSION_HANDOFFS = _handoff_policy(frozenset())
#: The baseline vocabulary as a lowercase key set, for callers that only compare key names.
SESSION_HANDOFF_QUERY_KEYS = BASELINE_SESSION_HANDOFFS.query_keys


def _contains_clear_value(value, *, url_decode_depth: int = 0) -> bool:
    if value is None or value == "":
        return False
    if not isinstance(value, str):
        return True
    candidate = value
    for _ in range(url_decode_depth + 1):
        if candidate.lower() == "[redacted]":
            return False
        decoded = unquote(candidate)
        if decoded == candidate:
            break
        candidate = decoded
    return candidate.lower() != "[redacted]"


def has_unredacted_sensitive_assignment(value, *, url_decode_depth: int = 0) -> bool:
    """Detect generic credentials recursively without treating business fields as secrets."""
    if isinstance(value, dict):
        for key, item in value.items():
            if (isinstance(key, str) and SENSITIVE_CREDENTIAL_KEY.fullmatch(key)
                    and _contains_clear_value(item, url_decode_depth=url_decode_depth)):
                return True
        marker = " ".join(str(value.get(key) or "")
                          for key in ("name", "id", "autocomplete", "type"))
        if (SENSITIVE_CREDENTIAL_NAME.search(marker) and "value" in value
                and _contains_clear_value(
                    value["value"], url_decode_depth=url_decode_depth)):
            return True
        return any(has_unredacted_sensitive_assignment(
            item, url_decode_depth=url_decode_depth) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(has_unredacted_sensitive_assignment(
            item, url_decode_depth=url_decode_depth) for item in value)
    if not isinstance(value, str):
        return False

    current = value
    for _ in range(url_decode_depth + 1):
        for match in SENSITIVE_CREDENTIAL_ASSIGNMENT.finditer(current):
            if _contains_clear_value(
                    match.group("value"), url_decode_depth=url_decode_depth):
                return True
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return False


def redact_sensitive_assignments(value, *, url_decode_depth: int = 3):
    """Deep-copy and redact the same narrow generic credential forms the detector rejects."""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if (isinstance(key, str) and SENSITIVE_CREDENTIAL_KEY.fullmatch(key)
                    and _contains_clear_value(
                        item, url_decode_depth=url_decode_depth)):
                cleaned[key] = "[redacted]"
            else:
                cleaned[key] = redact_sensitive_assignments(
                    item, url_decode_depth=url_decode_depth)
        marker = " ".join(str(cleaned.get(key) or "")
                          for key in ("name", "id", "autocomplete", "type"))
        if (SENSITIVE_CREDENTIAL_NAME.search(marker) and "value" in cleaned
                and _contains_clear_value(
                    cleaned["value"], url_decode_depth=url_decode_depth)):
            cleaned["value"] = "[redacted]"
        return cleaned
    if isinstance(value, (list, tuple)):
        return [redact_sensitive_assignments(
            item, url_decode_depth=url_decode_depth) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match) -> str:
        candidate = match.group("value")
        if not _contains_clear_value(candidate, url_decode_depth=url_decode_depth):
            return match.group(0)
        return match.group("prefix") + "[redacted]"

    cleaned = SENSITIVE_CREDENTIAL_ASSIGNMENT.sub(replace, value)
    # Percent-encoding can hide characters inside the key itself (for example the underscore
    # in ``access_token``).  If exact span replacement cannot safely locate what the detector
    # sees after bounded decoding, redact this entire string leaf rather than persist it.
    if has_unredacted_sensitive_assignment(
            cleaned, url_decode_depth=url_decode_depth):
        return "[redacted]"
    return cleaned


def _redact_handoffs(value, policy: SessionHandoffPolicy):
    keys = policy.query_keys
    if isinstance(value, dict):
        cleaned = {
            key: ("[redacted]" if isinstance(key, str) and key.lower() in keys
                  else _redact_handoffs(item, policy))
            for key, item in value.items()
        }
        # Structured browser controls may carry the secret key in `name` and its value in a
        # sibling field, so the value alone has no assignment string for the regex to find.
        name = str(cleaned.get("name") or "").lower()
        if name in keys and "value" in cleaned:
            cleaned["value"] = "[redacted]"
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_redact_handoffs(item, policy) for item in value]
    if isinstance(value, str):
        cleaned = policy.assignment.sub(r"\1[redacted]", value)
        if _has_unredacted_handoff(cleaned, policy, url_decode_depth=3):
            return "[redacted]"
        return cleaned
    return value


def redact_session_handoffs(value):
    """Deep-copy a value while replacing every baseline session hand-off value.

    Use `SessionHandoffPolicy.extending(...).redact` wherever the mounted application is
    known; this entry point can only see the keys this package ships with.
    """
    return _redact_handoffs(value, BASELINE_SESSION_HANDOFFS)


def _has_unredacted_handoff(value, policy: SessionHandoffPolicy, *,
                            url_decode_depth: int = 0) -> bool:
    keys = policy.query_keys
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in keys \
                    and str(item).lower() != "[redacted]":
                return True
        name = str(value.get("name") or "").lower()
        if name in keys and "value" in value \
                and str(value["value"]).lower() != "[redacted]":
            return True
        return any(_has_unredacted_handoff(
            item, policy, url_decode_depth=url_decode_depth) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_unredacted_handoff(
            item, policy, url_decode_depth=url_decode_depth) for item in value)
    if not isinstance(value, str):
        return False

    current = value
    assignment = policy.assignment
    for _ in range(url_decode_depth + 1):
        for match in assignment.finditer(current):
            candidate = match.group(2)
            for _ in range(url_decode_depth + 1):
                decoded = unquote(candidate)
                if decoded == candidate:
                    break
                candidate = decoded
            if candidate.lower() != "[redacted]":
                return True
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return False


def has_unredacted_session_handoff(value, *, url_decode_depth: int = 0) -> bool:
    """Whether a value still carries a baseline hand-off secret after optional decoding.

    Use `SessionHandoffPolicy.extending(...).has_unredacted` wherever the mounted application
    is known; this entry point can only see the keys this package ships with.
    """
    return _has_unredacted_handoff(
        value, BASELINE_SESSION_HANDOFFS, url_decode_depth=url_decode_depth)


def safe_tool_arguments(args: dict, handoffs: SessionHandoffPolicy | None = None) -> dict:
    """Project tool arguments for the audit ledger.

    `handoffs` is the mounted application's widened vocabulary; omitting it falls back to
    the package baseline, which cannot see an application's own session parameter.
    """
    policy = handoffs or SessionHandoffPolicy.extending(())
    cleaned = redact_sensitive_assignments(policy.redact(args))
    sensitive = re.compile(
        r"password|passwd|pwd|secret|token|api[_-]?key|authorization|jsessionid", re.I)

    def scrub(key: str, value):
        # Tool values can contain passwords as easily as ordinary form data.  The audit trail
        # needs the selected ref and action, not a replayable credential or personal value.
        if key == "value" or sensitive.search(key):
            return "[redacted]"
        if isinstance(value, dict):
            return {child: scrub(str(child), item) for child, item in value.items()}
        if isinstance(value, list):
            return [scrub("", item) for item in value]
        return value

    return {key: scrub(str(key), value) for key, value in cleaned.items()}
