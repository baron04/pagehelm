"""Translate actor-visible Odoo window actions into bounded generic page entries."""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET

from web_operation_agent.business.page_entries import (
    PageEntry, PageEntryCatalog, PagePart)
from web_operation_agent.business.errors import (
    BusinessToolAuthenticationError, BusinessToolContractError, BusinessToolError,
    BusinessToolUnavailableError)

from ..click_policy import OdooReviewedClickPolicy, load_review
from .descriptors import OdooRuntimeDescriptorReader
from .generic_reads import _FIELD_NAME, _SENSITIVE_FIELD, odoo_model_allowed


_ACTIVE_CONTEXT = re.compile(r"\bactive_(?:id|ids|model)\b")


class OdooPageEntryPort:
    """Catalog discovery is not permission to open an arbitrary Odoo action."""

    def __init__(self, client):
        self._client = client
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
                subject_reference=action.model,
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

    def parts(self, entry: PageEntry, *, context=None) -> tuple[PagePart, ...]:
        """Hints from this actor's default form, not a claim about the live action view."""
        model = entry.subject_reference
        if not entry.route or not model or not odoo_model_allowed(model):
            return ()
        try:
            view = self._client.call(model, "get_view", [], {"view_type": "form"})
        except (BusinessToolAuthenticationError, BusinessToolUnavailableError):
            raise
        except BusinessToolError:
            return ()
        if not isinstance(view, dict) or not isinstance(view.get("arch"), str):
            raise BusinessToolContractError("Odoo returned an invalid form view")
        arch = view["arch"]
        if len(arch) > 512_000 or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", arch, re.I):
            raise BusinessToolContractError("Odoo returned an unsafe form view")
        try:
            root = ET.fromstring(arch)
        except ET.ParseError as exc:
            raise BusinessToolContractError("Odoo returned an invalid form view") from exc
        if root.tag != "form":
            return ()
        tabs = {}
        fields = {}

        pending = [(root, False)]
        visited = 0
        while pending:
            node, inside_relation = pending.pop()
            visited += 1
            if visited > 10_000:
                raise BusinessToolContractError("Odoo returned an oversized form view")
            if node.tag == "page" and not inside_relation:
                label = node.get("string", "").strip()
                if label and len(label) <= 200 and not _SENSITIVE_FIELD.search(label):
                    tabs.setdefault(label.casefold(), PagePart(
                        reference=label.casefold(), label=label, kind="tab"))
            if node.tag == "field" and not inside_relation:
                name = node.get("name", "")
                if _FIELD_NAME.fullmatch(name) and not _SENSITIVE_FIELD.search(name):
                    fields.setdefault(name, node.get("string", ""))
                # Embedded one-to-many forms use another model's field namespace.
                if list(node):
                    inside_relation = True
            pending.extend((child, inside_relation) for child in reversed(node))
        if not fields:
            return tuple(tabs.values())
        try:
            metadata = self._client.call(model, "fields_get", [], {
                "attributes": ["string", "type"]})
        except (BusinessToolAuthenticationError, BusinessToolUnavailableError):
            raise
        except BusinessToolError:
            return tuple(tabs.values())
        if not isinstance(metadata, dict) or len(metadata) > 2_000:
            raise BusinessToolContractError("Odoo returned invalid form fields")
        parts = list(tabs.values())
        for name, override in fields.items():
            value = metadata.get(name)
            if not isinstance(value, dict):
                continue
            label = override.strip() if isinstance(override, str) else ""
            if not label:
                label = value.get("string", "")
            if (not isinstance(label, str) or not label.strip() or len(label) > 200
                    or _SENSITIVE_FIELD.search(label)):
                continue
            parts.append(PagePart(reference=name, label=label.strip(), kind="field",
                                  search_text=name.replace("_", " ")))
        return tuple(parts)


__all__ = ["OdooPageEntryPort"]
