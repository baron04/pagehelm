# --------------------------------------------------------------------------------------
# Browser wire types
# --------------------------------------------------------------------------------------
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from importlib.resources import files

from .common import (SCHEMA_VERSIONS, _bool, _dict_copy, _non_negative_int,
                     _require, _schema_version, _str, _strict_object, digest)

#: The fixed, generic tool surface. The browser exposes exactly these and nothing else;
#: no task-specific or application-specific action ever appears here.
BROWSER_ACTIONS = frozenset({
    "observe", "goto", "click", "fill", "select", "submit", "back", "scroll", "wait_for",
    "apply_facet",
})

# Only these actions are intrinsically unable to initiate an application request that
# changes state. Navigation is deliberately included in the complement: a GET, route
# transition or onchange handler can write in legacy applications. Product policy and the
# write witness import this one set so they cannot silently disagree.
INTRINSICALLY_READ_ONLY_ACTIONS = frozenset({"observe", "scroll", "wait_for"})

_BROWSER_CONTRACT = json.loads(
    files("web_operation_agent").joinpath(
        "web_runtime", "browser_contract.json").read_text(encoding="utf-8"))
MAX_OBSERVATION_ELEMENTS = int(_BROWSER_CONTRACT["observation"]["max_elements"])
MAX_SELECTED_CONTROLS = int(_BROWSER_CONTRACT["observation"]["max_selected_controls"])
MAX_SELECTED_CONTROL_CHARS = int(_BROWSER_CONTRACT["observation"]["max_selected_control_chars"])
MAX_PAGE_FACETS = int(_BROWSER_CONTRACT["observation"]["max_page_facets"])
MAX_FACET_CANDIDATES = int(_BROWSER_CONTRACT["observation"]["max_facet_candidates"])
MAX_OBSERVATION_CONTENT_CHARS = int(
    _BROWSER_CONTRACT["observation"]["max_content_chars"])
MAX_SCROLL_PAGES = int(_BROWSER_CONTRACT["scroll"]["max_pages"])
if min(MAX_OBSERVATION_ELEMENTS, MAX_OBSERVATION_CONTENT_CHARS, MAX_SCROLL_PAGES,
       MAX_PAGE_FACETS, MAX_FACET_CANDIDATES) < 1:
    raise ValueError("browser contract bounds must be positive integers")
POTENTIALLY_WRITING_ACTIONS = BROWSER_ACTIONS - INTRINSICALLY_READ_ONLY_ACTIONS


@dataclass(frozen=True)
class ActionCommand:
    command_id: str
    action: str
    args: dict = field(default_factory=dict)
    expect_navigation: bool = False

    def __post_init__(self) -> None:
        _str(self.command_id, "command_id")
        _str(self.action, "action")
        _require(self.action in BROWSER_ACTIONS,
                 f"unknown action {self.action!r}; the browser exposes {sorted(BROWSER_ACTIONS)}")
        object.__setattr__(self, "args", _dict_copy(self.args, "args"))
        _bool(self.expect_navigation, "expect_navigation")

    def to_json(self) -> dict:
        return {"schema_version": SCHEMA_VERSIONS["ActionCommand"], "command_id": self.command_id,
                "action": self.action, "args": copy.deepcopy(self.args),
                "expect_navigation": self.expect_navigation}

    @property
    def content_digest(self) -> str:
        return digest(self.to_json())

    @staticmethod
    def from_json(d: dict) -> "ActionCommand":
        allowed = {"schema_version", "command_id", "action", "args", "expect_navigation"}
        required = {"schema_version", "command_id", "action"}
        d = _strict_object(d, "ActionCommand", allowed=allowed, required=required)
        _schema_version(d["schema_version"], "ActionCommand")
        # Keep the action name in its dedicated envelope field. Arguments are untrusted
        # payload and must never select or overwrite the operation being dispatched.
        return ActionCommand(command_id=d["command_id"], action=d["action"],
                             args=d.get("args", {}),
                             expect_navigation=d.get("expect_navigation", False))


@dataclass(frozen=True)
class ElementRef:
    """A stable handle the model can name without seeing the DOM.

    `epoch` is the observation generation the ref was minted in. Using a ref from an older
    epoch is refused rather than resolved against a changed page.
    """
    ref: str
    role: str
    name: str
    tag: str
    epoch: int
    attrs: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        _str(self.ref, "ref")
        _str(self.role, "role")
        _str(self.name, "name", allow_empty=True)
        _str(self.tag, "tag")
        _non_negative_int(self.epoch, "epoch")
        object.__setattr__(self, "attrs", _dict_copy(self.attrs, "attrs"))

    def to_json(self) -> dict:
        return {"ref": self.ref, "role": self.role, "name": self.name, "tag": self.tag,
                "epoch": self.epoch, "attrs": copy.deepcopy(self.attrs)}

    @staticmethod
    def from_json(d: dict) -> "ElementRef":
        allowed = {"ref", "role", "name", "tag", "epoch", "attrs"}
        required = allowed - {"attrs"}
        d = _strict_object(d, "ElementRef", allowed=allowed, required=required)
        return ElementRef(ref=d["ref"], role=d["role"], name=d["name"], tag=d["tag"],
                          epoch=d["epoch"], attrs=d.get("attrs", {}))


@dataclass
class PageObservation:
    schema_version: int
    epoch: int
    url: str
    title: str
    landmarks: list[str]
    elements: list[ElementRef]
    # Optional text-DOM supplied by an embedded PageController.  The local Playwright
    # adapter keeps using structured elements only, so omitting this field preserves the
    # byte shape and digest of every historical observation.
    content: str = ""
    messages: list[str] = field(default_factory=list)
    truncated: bool = False
    # The embedded Runtime marks a URL transition pending while its page projection
    # still belongs to the preceding route. Older browser ports omit this signal.
    render_state: str = "unknown"
    # Bounded, viewport-visible selected controls from exclusive groups. Unlike elements,
    # these facts need no action ref and survive PageController omitting a large view's toolbar.
    selected_controls: list[str] = field(default_factory=list)
    # None means the application has not supplied typed facet state; [] means it did
    # inspect the page and found no active filters or groups.
    page_facets: list[dict[str, str]] | None = None
    # Application-owned choices on this page. A ref is valid only with this observation;
    # a candidate is not an active facet and grants no action permission on its own.
    facet_candidates: list[dict] | None = None
    # Application-owned identity of the form record actually rendered. This is an
    # observation, never authority to navigate to that path.
    record_route: str = ""

    def __post_init__(self) -> None:
        _schema_version(self.schema_version, "PageObservation")
        _non_negative_int(self.epoch, "epoch")
        _str(self.url, "url")
        _str(self.title, "title", allow_empty=True)
        _require(isinstance(self.landmarks, list), "landmarks must be an array")
        _require(all(isinstance(item, str) for item in self.landmarks),
                 "landmarks entries must be strings")
        _require(isinstance(self.elements, list), "elements must be an array")
        _require(len(self.elements) <= MAX_OBSERVATION_ELEMENTS,
                 f"elements must contain at most {MAX_OBSERVATION_ELEMENTS} entries")
        _require(all(isinstance(item, ElementRef) for item in self.elements),
                 "elements entries must be ElementRef values")
        _require(all(item.epoch == self.epoch for item in self.elements),
                 "every element epoch must equal the page epoch")
        refs = [item.ref for item in self.elements]
        _require(len(refs) == len(set(refs)), "element refs must be unique within an observation")
        _str(self.content, "content", allow_empty=True)
        _require(len(self.content) <= MAX_OBSERVATION_CONTENT_CHARS,
                 f"content must contain at most {MAX_OBSERVATION_CONTENT_CHARS} characters")
        _require(isinstance(self.messages, list), "messages must be an array")
        _require(all(isinstance(item, str) for item in self.messages),
                 "messages entries must be strings")
        _bool(self.truncated, "truncated")
        _require(self.render_state in {"unknown", "pending", "ready"},
                 "render_state must be unknown, pending or ready")
        _require(isinstance(self.selected_controls, list),
                 "selected_controls must be an array")
        _require(len(self.selected_controls) <= MAX_SELECTED_CONTROLS,
                 f"selected_controls must contain at most {MAX_SELECTED_CONTROLS} entries")
        _require(all(isinstance(name, str) and 0 < len(name) <= MAX_SELECTED_CONTROL_CHARS
                     for name in self.selected_controls),
                 "selected_controls entries must be short non-empty strings")
        if self.page_facets is not None:
            _require(isinstance(self.page_facets, list), "page_facets must be an array")
            _require(len(self.page_facets) <= MAX_PAGE_FACETS,
                     f"page_facets must contain at most {MAX_PAGE_FACETS} entries")
            _require(all(isinstance(item, dict) and set(item) == {"kind", "name"}
                         and item.get("kind") in {"filter", "group"}
                         and isinstance(item.get("name"), str)
                         and 0 < len(item["name"]) <= MAX_SELECTED_CONTROL_CHARS
                         for item in self.page_facets),
                     "page_facets entries must have a kind and short non-empty name")
        if self.facet_candidates is not None:
            _require(isinstance(self.facet_candidates, list),
                     "facet_candidates must be an array")
            _require(len(self.facet_candidates) <= MAX_FACET_CANDIDATES,
                     "too many facet_candidates")
            _require(all(isinstance(item, dict)
                         and set(item) == {"ref", "kind", "name", "active"}
                         and isinstance(item.get("ref"), str)
                         and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item["ref"]) is not None
                         and item.get("kind") in {"filter", "group"}
                         and isinstance(item.get("name"), str)
                         and 0 < len(item["name"]) <= MAX_SELECTED_CONTROL_CHARS
                         and isinstance(item.get("active"), bool)
                         for item in self.facet_candidates),
                     "invalid facet candidate")
            refs = [item["ref"] for item in self.facet_candidates]
            _require(len(refs) == len(set(refs)), "facet candidate refs must be unique")
        _require(isinstance(self.record_route, str) and len(self.record_route) <= 1000,
                 "record_route must be a bounded application path")
        if self.record_route:
            _require(self.record_route.startswith("/") and not self.record_route.startswith("//")
                     and not any(char in self.record_route for char in "?#\\")
                     and not re.search(r"[\x00-\x20\x7f]", self.record_route)
                     and not any(part in {".", ".."} for part in self.record_route.split("/")),
                     "record_route must be a plain application path")
        self.landmarks = copy.deepcopy(self.landmarks)
        self.elements = copy.deepcopy(self.elements)
        self.messages = copy.deepcopy(self.messages)
        self.selected_controls = copy.deepcopy(self.selected_controls)
        self.page_facets = copy.deepcopy(self.page_facets)
        self.facet_candidates = copy.deepcopy(self.facet_candidates)

    def to_json(self) -> dict:
        out = {"schema_version": self.schema_version, "epoch": self.epoch, "url": self.url,
                "title": self.title, "landmarks": copy.deepcopy(self.landmarks),
                "elements": [e.to_json() for e in self.elements],
                "messages": copy.deepcopy(self.messages), "truncated": self.truncated}
        if self.content:
            out["content"] = self.content
        if self.render_state != "unknown":
            out["render_state"] = self.render_state
        if self.selected_controls:
            out["selected_controls"] = copy.deepcopy(self.selected_controls)
        if self.page_facets is not None:
            out["page_facets"] = copy.deepcopy(self.page_facets)
        if self.facet_candidates is not None:
            out["facet_candidates"] = copy.deepcopy(self.facet_candidates)
        if self.record_route:
            out["record_route"] = self.record_route
        return out

    @staticmethod
    def from_json(d: dict) -> "PageObservation":
        allowed = {"schema_version", "epoch", "url", "title", "landmarks", "elements",
                   "content", "messages", "truncated", "render_state", "selected_controls",
                   "page_facets", "facet_candidates", "record_route"}
        required = {"schema_version", "epoch", "url", "title", "landmarks", "elements"}
        d = _strict_object(d, "PageObservation", allowed=allowed, required=required)
        _require(isinstance(d["landmarks"], list), "landmarks must be an array")
        _require(isinstance(d["elements"], list), "elements must be an array")
        _require(isinstance(d.get("messages", []), list), "messages must be an array")
        _require(isinstance(d.get("selected_controls", []), list),
                 "selected_controls must be an array")
        return PageObservation(schema_version=d["schema_version"], epoch=d["epoch"], url=d["url"],
                               title=d["title"], landmarks=copy.deepcopy(d["landmarks"]),
                               elements=[ElementRef.from_json(e) for e in d.get("elements", [])],
                               content=_str(d.get("content", ""), "content", allow_empty=True),
                               messages=copy.deepcopy(d.get("messages", [])),
                               truncated=d.get("truncated", False),
                               render_state=d.get("render_state", "unknown"),
                               selected_controls=copy.deepcopy(d.get("selected_controls", [])),
                               page_facets=copy.deepcopy(d.get("page_facets")),
                               facet_candidates=copy.deepcopy(d.get("facet_candidates")),
                               record_route=d.get("record_route", ""))

    @property
    def content_digest(self) -> str:
        return digest(self.to_json())


@dataclass
class ActionReceipt:
    """What the browser did, echoed back in full.

    `action` and `args_echo` are echoed rather than reconstructed so transport loss or renaming
    is caught directly by `tests/test_transport.py`.
    """
    schema_version: int
    command_id: str
    action: str
    args_echo: dict
    ok: bool
    error_code: str = ""
    error_detail: str = ""
    url_before: str = ""
    url_after: str = ""
    navigated: bool = False
    epoch_before: int = 0
    epoch_after: int = 0
    elapsed_ms: int = 0
    #: True when the browser re-authenticated during this action. Some applications carry a
    #: session between webapps in a URL parameter, so navigation without it lands on
    #: a login form. Recovering is session lifecycle, which is the browser's job, but it is
    #: reported rather than hidden: it is applied identically for every arm and it must be
    #: visible when reading a trace.
    relogin: bool = False
    #: True when the application held this browser session read-only for the whole action,
    #: through the page write guard it declares on its descriptor. It is what lets a turn
    #: tell "the click ran and changed nothing" from "the click ran and may have written":
    #: without it, an action that lost its result has to be settled as uncertain.
    write_guard_held: bool = False
    observation: PageObservation | None = None

    def __post_init__(self) -> None:
        _schema_version(self.schema_version, "ActionReceipt")
        _str(self.command_id, "command_id")
        _str(self.action, "action")
        _require(self.action in BROWSER_ACTIONS,
                 f"unknown receipt action {self.action!r}")
        self.args_echo = _dict_copy(self.args_echo, "args_echo")
        _bool(self.ok, "ok")
        _str(self.error_code, "error_code", allow_empty=True)
        _require(self.error_code in RECEIPT_ERRORS,
                 f"unknown receipt error code {self.error_code!r}")
        _str(self.error_detail, "error_detail", allow_empty=True)
        _str(self.url_before, "url_before", allow_empty=True)
        _str(self.url_after, "url_after", allow_empty=True)
        _bool(self.navigated, "navigated")
        _non_negative_int(self.epoch_before, "epoch_before")
        _non_negative_int(self.epoch_after, "epoch_after")
        _non_negative_int(self.elapsed_ms, "elapsed_ms")
        _bool(self.relogin, "relogin")
        _bool(self.write_guard_held, "write_guard_held")
        _require(self.observation is None or isinstance(self.observation, PageObservation),
                 "observation must be a PageObservation or null")
        self.observation = copy.deepcopy(self.observation)

    def to_json(self) -> dict:
        return {"schema_version": self.schema_version, "command_id": self.command_id,
                "action": self.action, "args_echo": copy.deepcopy(self.args_echo), "ok": self.ok,
                "error_code": self.error_code, "error_detail": self.error_detail,
                "url_before": self.url_before, "url_after": self.url_after,
                "navigated": self.navigated, "epoch_before": self.epoch_before,
                "epoch_after": self.epoch_after, "elapsed_ms": self.elapsed_ms,
                "relogin": self.relogin, "write_guard_held": self.write_guard_held,
                "observation": self.observation.to_json() if self.observation else None}

    @staticmethod
    def from_json(d: dict) -> "ActionReceipt":
        allowed = {"schema_version", "command_id", "action", "args_echo", "ok",
                   "error_code", "error_detail", "url_before", "url_after", "navigated",
                   "epoch_before", "epoch_after", "elapsed_ms", "relogin",
                   "write_guard_held", "observation"}
        required = {"schema_version", "command_id", "action", "args_echo", "ok"}
        d = _strict_object(d, "ActionReceipt", allowed=allowed, required=required)
        obs = d.get("observation")
        return ActionReceipt(schema_version=d["schema_version"], command_id=d["command_id"],
                             action=d["action"], args_echo=d.get("args_echo", {}),
                             ok=d["ok"], error_code=d.get("error_code", ""),
                             error_detail=d.get("error_detail", ""),
                             url_before=d.get("url_before", ""), url_after=d.get("url_after", ""),
                             navigated=d.get("navigated", False),
                             epoch_before=d.get("epoch_before", 0),
                             epoch_after=d.get("epoch_after", 0),
                             elapsed_ms=d.get("elapsed_ms", 0),
                             relogin=d.get("relogin", False),
                             write_guard_held=d.get("write_guard_held", False),
                             observation=PageObservation.from_json(obs)
                             if obs is not None else None)


#: Receipt error codes. Closed set, for the same reason terminal reasons are.
RECEIPT_ERRORS = frozenset({
    "", "unknown_ref", "stale_ref", "no_such_action", "bad_arguments", "element_not_actionable",
    "navigation_timeout", "origin_refused", "page_error", "write_refused",
    "deadline_expired", "stale_observation", "capability_refused", "runtime_replaced",
})
