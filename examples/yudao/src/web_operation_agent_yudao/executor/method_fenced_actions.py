"""Optional native page admission under the deployment's method fence."""
from dataclasses import fields
import re

from web_operation_agent.business.tools.contracts import form_control_unavailable
from web_operation_agent.execution.action_admission import ActionAdmissionRefused

from ..writes.live_forms import CREATE_WORDS, SUBMIT_WORDS, UPDATE_WORDS, plain
from .model_page_policy import HANDLERS, NativeModelReadActions, NativePageReview, _unsafe_button
from ..reads.read_actions import NativeReadActions


class MethodFencedPageReview(NativePageReview):
    """Missing handler registration does not override explicit deployment denials."""

    @classmethod
    def from_review(cls, review: NativePageReview):
        return cls(**{item.name: getattr(review, item.name) for item in fields(NativePageReview)})

    @property
    def capabilities(self):
        supported = {"tabs": "sections", "query": "facets", "paging": "paging",
                     "fields": "fill", "lines": "lines"}
        return super().capabilities | {name for family, name in supported.items()
                                       if family not in self.disabled_handlers}

    def permits(self, kind, path, control=None):
        if super().permits(kind, path, control):
            return True
        if kind == "form_entry" and control is not None:
            if not any(word in control for word in (*CREATE_WORDS, *UPDATE_WORDS)):
                return False
        return kind in HANDLERS and not self.denies(kind, path)

    def reviewed_control(self, kind, path, control):
        if super().reviewed_control(kind, path, control):
            return True
        # An application-owned tree query still needs a real node, field, value and new list read.
        return (kind == "query" and not self.denies(kind, path) and isinstance(control, str)
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", control) is not None
                and re.search(r"password|token|secret|credential", control, re.I) is None)


def _write_control(element):
    attrs = element.attrs
    if attrs.get("native_submit") == "true" or attrs.get("type") in {"submit", "image"} or element.role == "switch":
        return True
    if element.role in {"tab", "treeitem"} or attrs.get("navigation_kind") in {"page", "branch"}:
        return False  # These continue through the application's structural checks.
    return ((element.tag in {"button", "a"} or element.role in {"button", "link", "menuitem"})
            and (plain(element.name) in SUBMIT_WORDS or _unsafe_button(element)))


def _ordinary_control(action, element):
    attrs = element.attrs
    if (not element.name.strip() or attrs.get("href") or attrs.get("navigation_kind")
            or attrs.get("page_control") or attrs.get("form_scope")
            or attrs.get("target", "") not in {"", "_self"} or _write_control(element)):
        return False
    if action == "click":
        return element.role in {"button", "combobox", "select"}
    if action == "fill":
        return element.role in {"textbox", "spinbutton"} and element.tag in {"input", "textarea"}
    return action == "select" and element.role in {"combobox", "select"}


class MethodFencedPageActions(NativeModelReadActions):
    """Keep known action contracts and admit unregistered controls on an actor's live page."""

    def _tab_list_routes(self, path, actor_pages):
        # NativeTabPolicy still needs the actor's index, an owned enabled tab and a real list read.
        return {path} & actor_pages if not self.page_review.denies("tabs", path) else frozenset()

    def _unregistered_control(self, intent, element, path):
        # The caller already bound the current rendered page and the actual actor.
        return (not self.page_review.denies("navigation", path)
                and not form_control_unavailable(element.attrs)
                and _ordinary_control(intent.action, element))

    def admit(self, intent, *, context, checkpoint):
        if intent.action == "goto":
            return super().admit(intent, context=context, checkpoint=checkpoint)
        before = intent.observation
        element = next(item for item in before.elements if item.ref == intent.arguments["ref"])
        attrs = element.attrs
        if intent.action == "click" and _write_control(element):
            raise ActionAdmissionRefused("unsafe_effect")
        if intent.action == "click" and attrs.get("navigation_kind") == "branch":
            if not self.page_review.denies("navigation", attrs.get("navigation_path", "")):
                return NativeReadActions.admit(self, intent, context=context, checkpoint=checkpoint)
        return super().admit(intent, context=context, checkpoint=checkpoint)
