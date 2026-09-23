"""A declared page state that completes the request, and the server's check of it.

A read-only page operation -- open a screen, switch a tab, arrive at a record -- had no
definition of "done" that the server could check. Questions end on an evidence-backed
answer and writes end on a settled receipt, but a navigation ended only when the model
decided to call ``finish``; a model that kept investigating after it had already reached
the page spent the whole step budget on the page it wanted (Phase 8, section 16).

The model declares a page state before acting: an exact same-application path and
accessible names of controls that must show as selected. A fresh observation can verify
that declared state, but only a pure navigation request may then be settled. A question
or multi-part request may still need another read or action. Declaring a target grants
nothing: every click and navigation is routed, confirmed and settled as before.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..schemas import PageObservation

#: How often one turn may restate its target. New evidence can change a goal once or twice;
#: a turn that keeps redefining "done" is flailing, and the ordinary budgets end it.
MAX_TARGET_DECLARATIONS = 3
MAX_SELECTED_CONTROLS = 4
MAX_PATH_CHARS = 500
MAX_CONTROL_NAME_CHARS = 120

#: `selected` is the Runtime's own derived fact for a tab the page marks current only by
#: class (Bootstrap's `.active`, which Odoo 18 notebooks use); see `hydrateSelectionFacts`.
_SELECTED_ATTRS = ("aria-selected", "aria-checked", "checked", "selected")


@dataclass(frozen=True)
class TargetState:
    """What a fresh observation must show for the request to be complete."""

    #: Exact same-application path, optionally with a query; "" when the path is not part
    #: of the request (a tab switch on the current page, say).
    path: str = ""
    #: Accessible names of controls that must be selected or checked, as the observation
    #: names them. Empty when arriving at the path is the whole request.
    selected: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {"path": self.path, "selected": list(self.selected)}


def _normalise_name(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().casefold()


def _normalise_path(path: str) -> str:
    return path.rstrip("/") or "/"


def parse_target(args: object) -> TargetState:
    """Validate the model's declaration, refusing anything the server could not check.

    The check is exact by construction: a path either equals the page's or it does not,
    and a control either shows as selected or it does not. There is no "contains", so a
    weak target that would be satisfied by an unrelated page cannot be expressed.
    """
    if not isinstance(args, dict) or set(args) - {"path", "selected"}:
        raise ValueError("set_target accepts only path and selected")
    path = args.get("path", "")
    if not isinstance(path, str):
        raise ValueError("path must be a string")
    path = path.strip()
    if path:
        if len(path) > MAX_PATH_CHARS:
            raise ValueError("path is too long")
        parts = urlsplit(path)
        if (not path.startswith("/") or path.startswith("//") or parts.scheme
                or parts.netloc or parts.fragment or "%" in parts.path
                or ".." in parts.path.split("/") or re.search(r"\s", path)):
            raise ValueError(
                "path must be an exact same-application path such as /orders/42")
    raw_selected = args.get("selected", ())
    if raw_selected is None:
        raw_selected = ()
    if not isinstance(raw_selected, list | tuple):
        raise ValueError("selected must be a list of control names")
    if len(raw_selected) > MAX_SELECTED_CONTROLS:
        raise ValueError(f"selected may name at most {MAX_SELECTED_CONTROLS} controls")
    selected = []
    for name in raw_selected:
        if (not isinstance(name, str) or not name.strip()
                or len(name) > MAX_CONTROL_NAME_CHARS):
            raise ValueError("each selected control name must be a short non-empty string")
        cleaned = re.sub(r"\s+", " ", name).strip()
        if _normalise_name(cleaned) not in {_normalise_name(item) for item in selected}:
            selected.append(cleaned)
    if not path and not selected:
        raise ValueError("a target needs a path, selected controls, or both")
    return TargetState(path=path, selected=tuple(selected))


def _is_selected(element) -> bool:
    attrs = element.attrs if isinstance(element.attrs, dict) else {}
    for key in _SELECTED_ATTRS:
        value = attrs.get(key)
        if value is True or (isinstance(value, str) and value.strip().lower() == "true"):
            return True
    return False


def unmet_conditions(target: TargetState, obs: PageObservation) -> tuple[str, ...]:
    """Which parts of the target the observation does not show, in the order declared.

    Only what the observation carries counts. A control below the viewport is not in it,
    so a target naming that control is unmet until a scroll brings it into a fresh
    observation; nothing here reaches into the page beyond what the browser reported.
    """
    unmet: list[str] = []
    if obs.render_state == "pending":
        unmet.append("page rendering pending")
    if target.path:
        wanted = urlsplit(target.path)
        current = urlsplit(obs.url)
        if (_normalise_path(wanted.path) != _normalise_path(current.path)
                or (wanted.query and wanted.query != current.query)):
            unmet.append(f"path {target.path} (current page: "
                         f"{current.path or '/'}"
                         f"{'?' + current.query if current.query else ''})")
    if target.selected:
        selected_names = {_normalise_name(element.name or "")
                          for element in obs.elements if _is_selected(element)}
        for name in target.selected:
            if _normalise_name(name) not in selected_names:
                unmet.append(f"selected control \"{name}\"")
    return tuple(unmet)


def describe_target(target: TargetState) -> str:
    """One line for the model: what it declared, in the words it used."""
    parts = []
    if target.path:
        parts.append(f"path {target.path}")
    if target.selected:
        parts.append("selected: " + ", ".join(f'"{name}"' for name in target.selected))
    return "; ".join(parts)
