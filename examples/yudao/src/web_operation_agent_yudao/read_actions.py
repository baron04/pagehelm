"""Qualified navigation/tab prototype, requiring an explicit handler-effect review.

This does not translate a legacy allow-click decision into effect authority. The
integration must supply a deployment review of its menu/tab handlers and ancillary
effects. Source/DOM recognition and the native HTTP fence are insufficient alone.
Search, paging and form openers have not yet passed this effect qualification.
"""
import json
import re
from urllib.parse import urlsplit

from web_operation_agent.execution.action_admission import (
    ActionEffect, ActionPermit, ActionAdmissionRefused)
from .bound_data_reads import BoundDataReads
from .native_tabs import NativeNavigationPolicy, NativeTabPolicy


class NativeReadActions:
    application_id = "yudao"
    required_scope = "yudao.read"
    actions = frozenset({"click", "goto"})

    def __init__(self, origin, data, record_routes, *, review, list_actions=None):
        if not isinstance(review, str) or not review.strip() or len(review) > 200:
            raise ValueError("native action effects require a deployment handler review")
        self.origin, self.data, self.review = origin.rstrip("/"), data, review
        self.record_routes = dict(record_routes)
        self.list_actions = list_actions
        if list_actions is not None:
            self.actions = self.actions | list_actions.actions
        self._list_permits = {}
        self._list_turn = None

    def _index(self, context, checkpoint):
        context.require_live(application_id=self.application_id, required_scope=self.required_scope)
        bound = BoundDataReads(self.data, context, checkpoint)
        return bound.index(context=context)

    def _path(self, url):
        parsed = urlsplit(url)
        if (f"{parsed.scheme}://{parsed.netloc}" != self.origin or parsed.query or parsed.fragment
                or not re.fullmatch(r"/(?!/)[^?#\\\s%]*", parsed.path)
                or any(p in {".", ".."} for p in parsed.path.split("/"))):
            raise ActionAdmissionRefused("invalid_target")
        return parsed.path

    def admit(self, intent, *, context, checkpoint):
        index = self._index(context, checkpoint)
        before = intent.observation
        path = self._path(before.url)
        if before.page_route != path:
            raise ActionAdmissionRefused("invalid_target")
        pages = {p.key for p in index.pages}
        expectation = None
        effect = ActionEffect.NAVIGATION
        if intent.action == "goto":
            from urllib.parse import urljoin
            target = self._path(urljoin(before.url, intent.arguments["url"]))
            if target in pages:
                expectation = ["page", target]
        else:
            element = next(e for e in before.elements if e.ref == intent.arguments["ref"])
            attrs = element.attrs
            if self.list_actions is not None and (
                    intent.action == "fill" or attrs.get("form_scope")
                    or attrs.get("page_control") == "pagination"):
                permit = self.list_actions.admit(intent, index, context=context, checkpoint=checkpoint)
                turn = (context.principal.binding_digest, context.session_id, context.turn_id)
                if self._list_turn != turn:
                    self._list_turn = turn
                    self._list_permits.clear()
                self._list_permits[permit.binding_digest] = self.data._client.read_generation
                return permit
            if attrs.get("form_scope") or attrs.get("target", "") not in {"", "_self"}:
                raise ActionAdmissionRefused("invalid_target")
            policy = NativeNavigationPolicy(self.origin, lambda: index)
            if attrs.get("navigation_kind") in {"page", "branch"} and not policy.decide(before, element):
                target = attrs["navigation_path"]
                if attrs["navigation_kind"] == "page":
                    expectation = ["page", target]
                elif attrs.get("aria-expanded") in {"true", "false"}:
                    effect = ActionEffect.READ_ONLY
                    expectation = ["branch", target, attrs["aria-expanded"] == "false"]
            elif element.role == "tab" and not attrs.get("href"):
                routes = {k: v for k, v in self.record_routes.items() if k in pages}
                if not NativeTabPolicy(routes.values()).decide(before, element):
                    effect = ActionEffect.READ_ONLY
                    expectation = ["tab", attrs["id"], attrs["aria-controls"], element.name]
        if expectation is None:
            raise ActionAdmissionRefused("unreviewed")
        encoded = json.dumps([self.data._client.read_generation, expectation], separators=(",", ":"))
        if len(encoded) > 200:
            raise ActionAdmissionRefused("invalid_target")
        return ActionPermit(intent.binding_digest, effect, self.review, encoded)

    def verify(self, permit, intent, receipt, *, observation, context, checkpoint):
        index = self._index(context, checkpoint)
        if permit.binding_digest in self._list_permits:
            if self._list_permits[permit.binding_digest] != self.data._client.read_generation:
                return False
            return self.list_actions.verify(permit, intent, observation,
                                            index=index, context=context, checkpoint=checkpoint)
        generation, expected = json.loads(permit.postcondition)
        if generation != self.data._client.read_generation:
            return False
        after, before = observation, intent.observation
        path = self._path(after.url)
        if after.page_route != path:
            return False
        if expected[0] == "page":
            return path == expected[1] and path in {p.key for p in index.pages}
        if after.url != before.url or after.record_route != before.record_route:
            return False
        if expected[0] == "branch":
            matches = [e for e in after.elements if e.attrs.get("navigation_kind") == "branch"
                       and e.attrs.get("navigation_path") == expected[1]]
            return len(matches) == 1 and matches[0].attrs.get("aria-expanded") == str(expected[2]).lower()
        matches = [e for e in after.elements if e.role == "tab" and e.attrs.get("id") == expected[1]
                   and e.attrs.get("aria-controls") == expected[2] and e.name == expected[3]]
        return (len(matches) == 1 and matches[0].attrs.get("aria-selected") == "true"
                and expected[3] in after.selected_controls)
