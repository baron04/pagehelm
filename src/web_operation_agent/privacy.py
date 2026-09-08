"""Privacy primitives shared by the browser boundary and server-side audits."""
from __future__ import annotations

import re
from urllib.parse import unquote


# Apache OFBiz uses the first name for normal inter-app hand-offs. The two shorter
# spellings also occur in the bound public 24.09.07 source and carry the same secret value.
SESSION_HANDOFF_KEYS = (
    "externalLoginKey",
    "externaLoginKey",
    "externLoginKey",
    "jsessionid",
)
SESSION_HANDOFF_QUERY_KEYS = frozenset(key.lower() for key in SESSION_HANDOFF_KEYS)
_KEY_PATTERN = "(?:" + "|".join(re.escape(key) for key in SESSION_HANDOFF_KEYS) + ")"

# Match literal assignments and one-or-more URL-encoded assignments. A backslash is a
# boundary so a URL embedded in a JSON string is handled without swallowing the suffix.
SESSION_HANDOFF_ASSIGNMENT = re.compile(
    rf"({_KEY_PATTERN}(?:=|%(?:25)*3[dD]))"
    r"(.+?)"
    r"(?=(?:[&;/?#\s\"<>\\]|%(?:25)*(?:26|3[bBfF]|2[fF]|23)|$))",
    re.I,
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


def redact_session_handoffs(value):
    """Deep-copy a value while replacing every known session hand-off value."""
    if isinstance(value, dict):
        cleaned = {
            key: ("[redacted]" if isinstance(key, str)
                  and key.lower() in SESSION_HANDOFF_QUERY_KEYS
                  else redact_session_handoffs(item))
            for key, item in value.items()
        }
        # Structured browser controls may carry the secret key in `name` and its value in a
        # sibling field, so the value alone has no assignment string for the regex to find.
        name = str(cleaned.get("name") or "").lower()
        if name in SESSION_HANDOFF_QUERY_KEYS and "value" in cleaned:
            cleaned["value"] = "[redacted]"
        return cleaned
    if isinstance(value, list):
        return [redact_session_handoffs(item) for item in value]
    if isinstance(value, tuple):
        return [redact_session_handoffs(item) for item in value]
    if isinstance(value, str):
        cleaned = SESSION_HANDOFF_ASSIGNMENT.sub(r"\1[redacted]", value)
        if has_unredacted_session_handoff(cleaned, url_decode_depth=3):
            return "[redacted]"
        return cleaned
    return value


def has_unredacted_session_handoff(value, *, url_decode_depth: int = 0) -> bool:
    """Return whether a string leaf contains a known hand-off value after optional decoding."""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in SESSION_HANDOFF_QUERY_KEYS \
                    and str(item).lower() != "[redacted]":
                return True
        name = str(value.get("name") or "").lower()
        if name in SESSION_HANDOFF_QUERY_KEYS and "value" in value \
                and str(value["value"]).lower() != "[redacted]":
            return True
        return any(has_unredacted_session_handoff(
            item, url_decode_depth=url_decode_depth) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(has_unredacted_session_handoff(
            item, url_decode_depth=url_decode_depth) for item in value)
    if not isinstance(value, str):
        return False

    current = value
    for _ in range(url_decode_depth + 1):
        for match in SESSION_HANDOFF_ASSIGNMENT.finditer(current):
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
