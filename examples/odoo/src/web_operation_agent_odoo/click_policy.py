"""Which clicks the Odoo assistant may make: reviewed read-only pages and controls only.

Odoo is driven through its typed API and every write goes through a confirmed typed tool,
so the production turn disables generic click. This policy re-allows the part a reviewer
checked, element by element:

* anywhere -- a notebook tab (it only switches what is shown) and the Home Menu button (it
  only opens the application switcher);
* anywhere -- a same-application link whose destination is a reviewed collection page, or
  a record opened from one (never `/new`); leaving the page still asks the person first;
* on a reviewed page -- the view switches, pager, search and menu controls named in
  `policies/reviewed_click_pages.json`.

Everything else is refused before dispatch: unnamed buttons, row checkboxes and cells
(which start inline editing), New, Upload, the Actions menu, module activation, and every
control on a page nobody reviewed. Odoo action numbers differ between databases, so the
review names actions by XML ID and the adapter resolves them against the deployment; an
action it cannot resolve is simply not reviewed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from web_operation_agent.schemas import ElementRef, PageObservation

REVIEW = Path(__file__).with_name("policies") / "reviewed_click_pages.json"
_ACTION = re.compile(r"^/odoo/action-([A-Za-z0-9_.]+)$")
_RECORD = re.compile(r"^(?P<collection>/odoo/[^/]+)/(?P<id>\d+)$")


def load_review() -> dict:
    review = json.loads(REVIEW.read_text(encoding="utf-8"))
    if review.get("schema_version") != 1:
        raise ValueError("unsupported click review schema")
    return review


@dataclass(frozen=True)
class OdooReviewedClickPolicy:
    """Decides one click from the reviewed page set and the element the model named."""

    paths: frozenset[str]
    actions: frozenset[str]
    page_controls: frozenset[str]
    anywhere_controls: frozenset[str]

    @classmethod
    def from_review(cls, review: dict, action_ids: dict[str, int]) -> "OdooReviewedClickPolicy":
        """Build from the review and the XML ID -> action id map resolved on this database."""
        actions = set()
        for item in review["actions"]:
            xmlid = item["xmlid"]
            if xmlid in action_ids:
                actions.update({xmlid, str(action_ids[xmlid])})
        return cls(paths=frozenset(item["path"] for item in review["named_routes"]),
                   actions=frozenset(actions),
                   page_controls=frozenset(review["controls_on_reviewed_pages"]),
                   anywhere_controls=frozenset(review["controls_anywhere"]))

    def reviewed(self, path: str) -> bool:
        path = path.rstrip("/") or "/"
        if path in self.paths:
            return True
        match = _ACTION.match(path)
        return bool(match and match.group(1) in self.actions)

    def _destination_allowed(self, path: str) -> bool:
        path = path.rstrip("/") or "/"
        if self.reviewed(path):
            return True
        record = _RECORD.match(path)
        return bool(record and self.reviewed(record.group("collection")))

    def decide(self, observation: PageObservation, element: ElementRef) -> str:
        name = (element.name or "").strip()
        if element.role == "tab" or name in self.anywhere_controls:
            return ""
        href = element.attrs.get("href") if isinstance(element.attrs, dict) else None
        if isinstance(href, str) and href and not href.startswith("#"):
            if href.lower().startswith("javascript:"):
                return "a scripted link is not a reviewed control"
            here = urlsplit(observation.url)
            there = urlsplit(urljoin(observation.url, href))
            if (there.scheme, there.netloc) != (here.scheme, here.netloc):
                return "a link that leaves the application is not a reviewed control"
            target = element.attrs.get("target") if isinstance(element.attrs, dict) else ""
            if target and str(target).lower() not in {"_self", ""}:
                return "a link that opens another window is not a reviewed control"
            if self._destination_allowed(there.path):
                return ""
            return "that link leads to a page nobody reviewed for the assistant"
        if not self.reviewed(urlsplit(observation.url).path):
            return "this page has not been reviewed for assistant clicks"
        if element.role == "button" and name in self.page_controls:
            return ""
        label = f"{name!r} " if name else "an unnamed "
        return (f"{label}{element.role} is not a reviewed read-only control on this page")
