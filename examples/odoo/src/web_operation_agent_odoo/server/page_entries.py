"""Translate actor-visible Odoo window actions into bounded generic page entries."""
from __future__ import annotations

import hashlib
import json
import re

from web_operation_agent.business.page_entries import PageEntry, PageEntryCatalog

from ..click_policy import OdooReviewedClickPolicy, load_review
from .descriptors import OdooRuntimeDescriptorReader


_ACTIVE_CONTEXT = re.compile(r"\bactive_(?:id|ids|model)\b")


class OdooPageEntryPort:
    """Catalog discovery is not permission to open an arbitrary Odoo action."""

    def __init__(self, client):
        self._reader = OdooRuntimeDescriptorReader(client)

    def catalog(self, *, context=None) -> PageEntryCatalog:
        catalog = self._reader.catalog()
        review = load_review()
        reviewed = OdooReviewedClickPolicy.from_review(
            review,
            {action.xml_id: action.action_id for action in catalog.actions
             if action.xml_id},
            {action.xml_id: action.route for action in catalog.actions
             if action.xml_id and not action.route.startswith("action-")},
        )
        model_labels = {model.model: model.label for model in catalog.models}
        entries = []
        for action in catalog.actions:
            if not action.readable:
                continue
            requires_record = bool(_ACTIVE_CONTEXT.search(
                action.context + " " + action.domain))
            path = "/odoo/" + action.route
            openable = not requires_record and reviewed.allows_destination(path)
            entries.append(PageEntry(
                reference=str(action.action_id),
                label=action.name or model_labels.get(action.model, action.model),
                search_text=" ".join((
                    action.xml_id.replace(".", " ").replace("_", " "),
                    model_labels.get(action.model, ""))),
                breadcrumbs=action.breadcrumbs,
                views=action.view_modes,
                route=path if openable else "",
                unavailable_reason=("requires_record_context" if requires_record
                                    else "not_reviewed_for_navigation")
                if not openable else "",
            ))
        binding = hashlib.sha256(json.dumps({
            "descriptor": catalog.descriptor_digest,
            "review": review,
            "qualification": "actor-window-action-v1",
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        return PageEntryCatalog(
            binding_digest=binding,
            entries=tuple(entries),
            complete=catalog.complete,
            scope="current actor's readable window actions; only reviewed independent "
                  "destinations have an openable route",
        )


__all__ = ["OdooPageEntryPort"]
