"""A declared page state that completes the request, and the server's check of it.

A read-only page operation -- open a screen, switch a tab, arrive at a record -- had no
definition of "done" that the server could check. Questions end on an evidence-backed
answer and writes end on a settled receipt, but a navigation ended only when the model
decided to call ``finish``; a model that kept investigating after it had already reached
the page spent the whole step budget on the page it wanted (Phase 8, section 16).

The model declares a page state before acting: an exact same-application path,
selected controls and application-reported facets. A fresh observation can verify
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
_SELECTED_ATTRS = ("aria-selected", "aria-checked", "aria-pressed", "checked", "selected")
# A bounded guard for conventional named views, not a general intent parser. Unknown
# views and compound page goals still need an application-provided PageState/GoalSpec.
_VIEW_REQUESTS = (
    ("Kanban", re.compile(r"看板|(?<![a-z])kanban(?![a-z])", re.I)),
    ("List", re.compile(r"列表|(?<![a-z])(?:list|table)\s+view(?![a-z])", re.I)),
    ("Calendar", re.compile(r"日历|(?<![a-z])calendar\s+view(?![a-z])", re.I)),
    ("Pivot", re.compile(r"透视表?|(?<![a-z])pivot(?:\s+(?:table|view))?(?![a-z])", re.I)),
    ("Graph", re.compile(r"图表|(?<![a-z])(?:graph|chart)\s+view(?![a-z])", re.I)),
)
_VIEW_ALIASES = {
    "看板": "kanban", "列表": "list", "表格": "list", "table": "list",
    "日历": "calendar", "透视": "pivot", "透视表": "pivot",
    "图表": "graph", "chart": "graph",
}

# This is deliberately a small structural guard, not a semantic intent parser. It
# recognizes explicit presentation verbs, independent of any application's field names.
# Colloquial requests such as "按销售员看看" still need a separately validated goal spec.
_EXPLICIT_FILTER = re.compile(r"筛选|过滤|只(?:看|显示)|仅(?:看|显示)|"
                              r"(?<![a-z])filter(?:ed)?\s+by(?![a-z])", re.I)
_EXPLICIT_GROUP = re.compile(r"分组|归类|(?<![a-z])group(?:ed)?\s+by(?![a-z])", re.I)
_CLEAR_FILTER = re.compile(r"取消(?:[^，。]{0,12})?筛选|清除(?:[^，。]{0,12})?筛选|"
                           r"不只是|不止是|不限于|"
                           r"(?<![a-z])(?:remove|clear)\s+(?:the\s+)?filter|"
                           r"(?<![a-z])not\s+(?:just|only)(?![a-z])", re.I)


def required_page_controls(prompt: str) -> tuple[str, ...]:
    found = sorted((match.start(), name)
                   for name, pattern in _VIEW_REQUESTS
                   for match in pattern.finditer(prompt))
    # "从列表切换到看板" names the origin and the destination. The
    # destination alone is the page state that must be visible at completion.
    transition = re.search(r"(?:切换?|换|转)(?:到|为)\s*", prompt)
    if transition:
        destination = [name for start, name in found if start >= transition.end()]
        if destination:
            return (destination[-1],)
    return tuple(dict.fromkeys(name for _, name in found))


def required_page_effects(prompt: str) -> frozenset[str]:
    """A lower bound for explicit filter/group requests, never an inferred field name."""
    effects = set()
    clear_matches_removed = _CLEAR_FILTER.sub("", prompt)
    if _EXPLICIT_FILTER.search(clear_matches_removed):
        effects.add("filter")
    if _EXPLICIT_GROUP.search(prompt):
        effects.add("group")
    if _CLEAR_FILTER.search(prompt):
        effects.add("clear_filter")
    return frozenset(effects)


@dataclass(frozen=True)
class TargetState:
    """What a fresh observation must show for the request to be complete."""

    #: Exact same-application path, optionally with a query; "" when the path is not part
    #: of the request (a tab switch on the current page, say).
    path: str = ""
    #: Accessible names of controls that must be selected or checked, as the observation
    #: names them. Empty when arriving at the path is the whole request.
    selected: tuple[str, ...] = ()
    #: Active application-reported facets, never inferred from menu candidates or body text.
    filters: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    #: A filter to remove must first have been observed active in this turn.
    filters_absent: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {"path": self.path, "selected": list(self.selected),
                **({"filters": list(self.filters)} if self.filters else {}),
                **({"groups": list(self.groups)} if self.groups else {}),
                **({"filters_absent": list(self.filters_absent)}
                   if self.filters_absent else {})}


def _normalise_name(name: str) -> str:
    normalised = re.sub(r"\s+", " ", name).strip().casefold()
    return _VIEW_ALIASES.get(normalised, normalised)


def _normalise_path(path: str) -> str:
    return path.rstrip("/") or "/"


def parse_target(args: object) -> TargetState:
    """Validate the model's declaration, refusing anything the server could not check.

    The check is exact by construction: a path either equals the page's or it does not,
    and a control either shows as selected or it does not. There is no "contains", so a
    weak target that would be satisfied by an unrelated page cannot be expressed.
    """
    if (not isinstance(args, dict)
            or set(args) - {"path", "selected", "filters", "groups", "filters_absent"}):
        raise ValueError("set_target accepts only path, selected and facet conditions")
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
    def names(key: str) -> tuple[str, ...]:
        raw = args.get(key, ())
        if not isinstance(raw, list | tuple) or len(raw) > MAX_SELECTED_CONTROLS:
            raise ValueError(f"{key} must list at most {MAX_SELECTED_CONTROLS} names")
        values = []
        for value in raw:
            if (not isinstance(value, str) or not value.strip()
                    or len(value) > MAX_CONTROL_NAME_CHARS):
                raise ValueError(f"each {key} name must be a short non-empty string")
            clean = re.sub(r"\s+", " ", value).strip()
            if _normalise_name(clean) not in {_normalise_name(v) for v in values}:
                values.append(clean)
        return tuple(values)

    filters, groups = names("filters"), names("groups")
    filters_absent = names("filters_absent")
    if {_normalise_name(name) for name in filters} & {
            _normalise_name(name) for name in filters_absent}:
        raise ValueError("a filter cannot be both present and absent")
    if not path and not selected and not filters and not groups and not filters_absent:
        raise ValueError("a target needs a path, selected controls or facet conditions")
    return TargetState(path=path, selected=tuple(selected), filters=filters, groups=groups,
                       filters_absent=filters_absent)


def _is_selected(element) -> bool:
    attrs = element.attrs if isinstance(element.attrs, dict) else {}
    for key in _SELECTED_ATTRS:
        value = attrs.get(key)
        if value is True or (isinstance(value, str) and value.strip().lower() == "true"):
            return True
    return False


def unmet_conditions(target: TargetState, obs: PageObservation, *,
                     required_selected: tuple[str, ...] = (),
                     required_effects: frozenset[str] = frozenset(),
                     seen_active_filters: dict[str, set[str]] | None = None
                     ) -> tuple[str, ...]:
    """Which parts of the target the observation does not show, in the order declared.

    Only what the observation carries counts. A control below the viewport is not in it,
    so a target naming that control is unmet until a scroll brings it into a fresh
    observation; nothing here reaches into the page beyond what the browser reported.
    """
    unmet: list[str] = []
    if obs.render_state == "pending":
        unmet.append("page rendering pending")
    elif obs.render_state == "blocked":
        unmet.append("application reports a blocking page error")
    if target.path:
        wanted = urlsplit(target.path)
        current = urlsplit(obs.url)
        if (_normalise_path(wanted.path) != _normalise_path(current.path)
                or (wanted.query and wanted.query != current.query)):
            unmet.append(f"path {target.path} (current page: "
                         f"{current.path or '/'}"
                         f"{'?' + current.query if current.query else ''})")
    if target.selected or required_selected:
        selected_names = ({_normalise_name(element.name or "")
                           for element in obs.elements if _is_selected(element)}
                          | {_normalise_name(name) for name in obs.selected_controls})
        for name in dict.fromkeys((*target.selected, *required_selected)):
            if _normalise_name(name) not in selected_names:
                unmet.append(f"selected control \"{name}\"")
    if target.filters or target.groups:
        facets = {(_normalise_name(item["name"]), item["kind"])
                  for item in obs.page_facets or []}
        for kind, names in (("filter", target.filters), ("group", target.groups)):
            for name in names:
                if (_normalise_name(name), kind) not in facets:
                    unmet.append(f"active {kind} \"{name}\""
                                 + (" (application state unavailable)"
                                    if obs.page_facets is None else ""))
    active_filters = ({_normalise_name(item["name"])
                       for item in obs.page_facets or [] if item["kind"] == "filter"}
                      if obs.page_facets is not None else set())
    current_path = _normalise_path(urlsplit(obs.url).path)
    seen_filters = {(_normalise_name(name), path)
                    for name, paths in (seen_active_filters or {}).items()
                    for path in paths}
    for name in target.filters_absent:
        normalised = _normalise_name(name)
        if (normalised, current_path) not in seen_filters:
            unmet.append(f'filter removal "{name}" not grounded in an active filter '
                         'on this page')
        elif obs.page_facets is None:
            unmet.append(f'filter removal "{name}" (application state unavailable)')
        elif normalised in active_filters:
            unmet.append(f'filter "{name}" is still active')
    for effect, declared in (("filter", target.filters), ("group", target.groups),
                             ("clear_filter", target.filters_absent)):
        if effect in required_effects and not declared:
            unmet.append(f"{effect} condition not declared")
    return tuple(unmet)


def describe_target(target: TargetState) -> str:
    """One line for the model: what it declared, in the words it used."""
    parts = []
    if target.path:
        parts.append(f"path {target.path}")
    if target.selected:
        parts.append("selected: " + ", ".join(f'"{name}"' for name in target.selected))
    if target.filters:
        parts.append("filters: " + ", ".join(f'"{name}"' for name in target.filters))
    if target.groups:
        parts.append("groups: " + ", ".join(f'"{name}"' for name in target.groups))
    if target.filters_absent:
        parts.append("filters absent: " + ", ".join(
            f'"{name}"' for name in target.filters_absent))
    return "; ".join(parts)
