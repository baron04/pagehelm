"""Offline read-catalog generator retained for experiments.

The native host does not consume this catalog or accept an auto-catalog switch.
Deployments supply a reviewed method fence explicitly.
"""
from __future__ import annotations

import re
from pathlib import Path

from .ui_source import UiSource

#: Endpoints the generated catalog never admits, whatever a page's source says.
#: Mutations, writes, exports and approval actions stay out of a read fence.
_DENY = re.compile(
    r"/(create|update|delete|approve|cancel|submit|export|export-\w+|import|copy|transfer|"
    r"receive|send|reset|enable|disable|sync|refund|pay|confirm|login|logout|revoke|grant"
    r"|assign)(?:-[\w-]*)?$", re.I)
_ALLOWED = re.compile(r"^/admin-api/[a-z0-9][a-z0-9/-]*$")


def generated_read_paths(source: UiSource, api_docs: dict | None = None) -> tuple[str, ...]:
    """Exact admin API paths the deployment itself declares as safe reads, sorted.

    Two sources, both deployment-owned and release-bound:

    1. The compiled UI source: every list page's own endpoint (what a rendered page
       actually reads).
    2. When ``api_docs`` is given, every endpoint OpenAPI documents as GET whose name
       is not mutation-shaped. The document is the deployment's own declared read
       surface; the guard keeps exact-path matching, the actor fence and its journal,
       so admitting a read path changes who may read what, never how writes happen.

    Everything mutation-shaped, session-affecting (login/logout/revoke/grant) or
    bulk-exporting is excluded by the deny pattern even before the guard validates.
    """
    paths: set[str] = set()
    for view in source.views.values():
        endpoint = view.endpoint
        if not endpoint or _DENY.search(endpoint):
            continue
        full = f"/admin-api{endpoint}"
        if not _ALLOWED.match(full):
            continue
        paths.add(full)
    if api_docs:
        documented = api_docs.get("paths") or {}
        for path, node in documented.items():
            if (isinstance(node, dict) and "get" in node
                    and _ALLOWED.match(path) and not _DENY.search(path)):
                paths.add(path)
    return tuple(sorted(paths))


def write_catalog(source: UiSource, release: str, output: Path) -> Path:
    """Write a reviewed-policy-shaped file from the compiled index."""
    paths = generated_read_paths(source)
    if not paths:
        raise ValueError("the compiled UI source yielded no readable list endpoints")
    output.write_text(_json({"schema_version": 1, "release": release,
                             "read_paths": list(paths),
                             "generated_from": "ui-source-compile"}) + "\n",
                      encoding="utf-8")
    return output


def _json(payload) -> str:
    import json
    return json.dumps(payload, ensure_ascii=False, indent=2)


__all__ = ["generated_read_paths", "write_catalog"]
