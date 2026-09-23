"""Which clicks the Odoo assistant may make: reviewed read-only pages and controls only.

Odoo is driven through its typed API and every write goes through a confirmed typed tool,
so the production turn disables generic click. This policy re-allows the part a reviewer
checked, element by element:

* anywhere -- a notebook tab (it only switches what is shown) and the Home Menu button (it
  only opens the application switcher);
* anywhere -- a same-application link whose destination is a reviewed collection page, or
  a record opened from one (never `/new`); leaving the page still asks the person first;
* on a reviewed collection page -- the view switches, pager, search and menu controls
  named in `policies/reviewed_click_pages.json`, and, where the review marks the page
  `rows: open`, a list row or kanban card, which opens its record;
* on a record opened from a reviewed collection -- the record pager, top menus, and a
  related-record link. One exact customer-invoice button may open the Pay dialog after
  checking its live method name; opening this wizard does not submit a payment.

Everything else is refused before dispatch: unnamed buttons, row checkboxes, cells of lists
not marked `rows: open` (editable lists start inline editing), New, Upload, the Actions
menu, other header workflow buttons, module activation, and every control on a page nobody
reviewed. Odoo action numbers and URL paths differ between databases, so the review names
actions by XML ID and the adapter resolves them against the deployment; an action it cannot
resolve is simply not reviewed.

This is the first line. Since woa_runtime 1.5 the second is inside Odoo: every page action
runs with the person's session read-only, so a write a reviewed control could still cause
is refused there.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit

from web_operation_agent.schemas import ElementRef, PageObservation

REVIEW = Path(__file__).with_name("policies") / "reviewed_click_pages.json"
_ACTION = re.compile(r"^/odoo/action-([A-Za-z0-9_.]+)$")
_RECORD = re.compile(r"^(?P<collection>/odoo/[^/]+)/(?P<id>\d+)$")
#: A related record as a record's many2one link names it: a technical model name, which
#: always has a dot, and an id. `/odoo/res.partner/11`, never `/odoo/res.partner/new`.
_RELATED = re.compile(r"^/odoo/[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+/\d+$")
_ACTION_PATH = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
#: The one query an address the assistant opens directly may carry: which view to show.
_VIEW_TYPES = frozenset({"list", "kanban", "form", "pivot", "graph", "calendar", "activity",
                         "map"})
#: What a row or card of a collection is in the Runtime's observation.
_ROW_ROLES = frozenset({"td", "article"})


def load_review() -> dict:
    review = json.loads(REVIEW.read_text(encoding="utf-8"))
    if review.get("schema_version") != 1:
        raise ValueError("unsupported click review schema")
    return review


def _normal(path: str) -> str:
    return path.rstrip("/") or "/"


@dataclass(frozen=True)
class OdooReviewedClickPolicy:
    """Decides one click from the reviewed page set and the element the model named."""

    paths: frozenset[str]
    actions: frozenset[str]
    row_pages: frozenset[str]
    page_controls: frozenset[str]
    record_controls: frozenset[str]
    anywhere_controls: frozenset[str]
    action_target_paths: tuple[tuple[str, str], ...] = ()
    safe_dialog_openers: tuple[tuple[str, str, str], ...] = ()

    @classmethod
    def from_review(cls, review: dict, action_ids: dict[str, int],
                    action_paths: dict[str, str] | None = None) -> "OdooReviewedClickPolicy":
        """Build from the review and what this database resolved.

        `action_ids` maps a reviewed XML ID to its action id; `action_paths` maps it to the
        action's URL path (`bills` for `/odoo/bills`) where the action has one.
        """
        action_paths = action_paths or {}
        paths: set[str] = set()
        actions: set[str] = set()
        rows: set[str] = set()
        target_paths: list[tuple[str, str]] = []
        for item in review["named_routes"]:
            paths.add(item["path"])
            if item.get("rows") == "open":
                rows.add(item["path"])
        for item in review["actions"]:
            xmlid = item["xmlid"]
            if xmlid not in action_ids:
                continue
            keys = {xmlid, str(action_ids[xmlid])}
            target_paths.append((f"/odoo/action-{xmlid}",
                                 f"/odoo/action-{action_ids[xmlid]}"))
            path = action_paths.get(xmlid)
            if isinstance(path, str) and _ACTION_PATH.fullmatch(path):
                paths.add("/odoo/" + path)
                keys_with_path = keys | {"/odoo/" + path}
            else:
                keys_with_path = keys
            actions.update(keys)
            if item.get("rows") == "open":
                rows.update(keys_with_path)
        dialog_openers: set[tuple[str, str, str]] = set()
        for item in review.get("safe_dialog_openers", ()):
            if (not isinstance(item, dict)
                    or not all(isinstance(item.get(key), str) for key in (
                        "action_xmlid", "record_collection", "button_name", "method"))):
                raise ValueError("invalid reviewed dialog opener")
            xmlid = item["action_xmlid"]
            if xmlid not in action_ids or item["record_collection"] not in paths:
                continue
            collections = {item["record_collection"], f"/odoo/action-{xmlid}",
                           f"/odoo/action-{action_ids[xmlid]}"}
            action_path = action_paths.get(xmlid)
            if isinstance(action_path, str) and _ACTION_PATH.fullmatch(action_path):
                collections.add("/odoo/" + action_path)
            dialog_openers.update((collection, item["button_name"], item["method"])
                                  for collection in collections)
        return cls(paths=frozenset(paths), actions=frozenset(actions),
                   row_pages=frozenset(rows),
                   page_controls=frozenset(review["controls_on_reviewed_pages"]),
                   record_controls=frozenset(review.get("controls_on_reviewed_records", ())),
                   anywhere_controls=frozenset(review["controls_anywhere"]),
                   action_target_paths=tuple(target_paths),
                   safe_dialog_openers=tuple(sorted(dialog_openers)))

    def canonical_target_path(self, path: str) -> str:
        """Bind a reviewed XML-ID target to this deployment's observed action number.

        Only resolved reviewed actions are aliases. The query, if any, is kept so the
        generic target checker can still demand the exact requested view.
        """
        parts = urlsplit(path)
        mapped = dict(self.action_target_paths).get(parts.path)
        if mapped is None:
            return path
        return mapped + (f"?{parts.query}" if parts.query else "")

    def _collection(self, path: str) -> str | None:
        """The reviewed collection `path` is, as the key rows are marked under, or None."""
        if path in self.paths:
            return path
        match = _ACTION.match(path)
        if match and match.group(1) in self.actions:
            return match.group(1)
        return None

    def _record_of_reviewed(self, path: str) -> bool:
        record = _RECORD.match(path)
        return bool(record and self._collection(record.group("collection")) is not None)

    def reviewed(self, path: str) -> bool:
        """A reviewed collection page."""
        return self._collection(_normal(path)) is not None

    def destination_allowed(self, path: str) -> bool:
        """A reviewed collection page or a record opened from one."""
        path = _normal(path)
        return self.reviewed(path) or self._record_of_reviewed(path)

    def allows_destination(self, url: str) -> bool:
        """A reviewed page, or a record opened from one, to open by address.

        Only a same-application path, and at most `view_type` in its query: `debug=1` and
        the like change how the web client behaves for the person, not which page it shows.
        """
        if not isinstance(url, str):
            return False
        parts = urlsplit(url)
        if parts.scheme or parts.netloc or parts.fragment or not parts.path.startswith("/"):
            return False
        if parts.query:
            try:
                query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
            except ValueError:
                return False
            if len(query) != 1 or query[0][0] != "view_type" or query[0][1] not in _VIEW_TYPES:
                return False
        return self.destination_allowed(parts.path)

    def decide(self, observation: PageObservation, element: ElementRef) -> str:
        name = (element.name or "").strip()
        if element.role == "tab" or name in self.anywhere_controls:
            return ""
        attrs = element.attrs if isinstance(element.attrs, dict) else {}
        href = attrs.get("href")
        if isinstance(href, str) and href and not href.startswith("#"):
            if href.lower().startswith("javascript:"):
                return "a scripted link is not a reviewed control"
            here = urlsplit(observation.url)
            there = urlsplit(urljoin(observation.url, href))
            if (there.scheme, there.netloc) != (here.scheme, here.netloc):
                return "a link that leaves the application is not a reviewed control"
            target = attrs.get("target", "")
            if target and str(target).lower() not in {"_self", ""}:
                return "a link that opens another window is not a reviewed control"
            if self.destination_allowed(there.path):
                return ""
            if (_RELATED.match(there.path)
                    and self._record_of_reviewed(_normal(here.path))):
                return ""
            return "that link leads to a page nobody reviewed for the assistant"
        page = _normal(urlsplit(observation.url).path)
        collection = self._collection(page)
        if collection is None:
            if not self._record_of_reviewed(page):
                return "this page has not been reviewed for assistant clicks"
            record = _RECORD.match(page)
            if (record is not None and element.role == "button"
                    and (record.group("collection"), name, attrs.get("action_name"))
                    in self.safe_dialog_openers):
                return ""
            if element.role == "button" and name in self.record_controls:
                return ""
            label = f"{name!r} " if name else "an unnamed "
            return f"{label}{element.role} is not a reviewed read-only control on a record"
        if element.role == "button" and name in self.page_controls:
            return ""
        if (element.role in _ROW_ROLES and collection in self.row_pages
                and (element.role == "article" or attrs.get("name"))):
            return ""
        label = f"{name!r} " if name else "an unnamed "
        return f"{label}{element.role} is not a reviewed read-only control on this page"
