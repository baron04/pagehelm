"""Reviewed page identities used only as knowledge/index context."""

def scope_page_keys(parts):
    """Knowledge/index context only; consumers must not dispatch from these keys."""
    return tuple(dict.fromkeys(part[key] for part in parts
        for key in ("page", "page_context") if part.get(key)))
