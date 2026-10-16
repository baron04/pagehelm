"""Qualified navigation/tab prototype, requiring an explicit handler-effect review.

This does not translate a legacy allow-click decision into effect authority. The
integration must supply a deployment review of its menu/tab handlers and ancillary
effects. Source/DOM recognition and the native HTTP fence are insufficient alone.
Optional list and FormPort opener qualifications remain deployment-owned.
"""
import json
import re
from urllib.parse import urlsplit

from web_operation_agent.execution.action_admission import (
    ActionEffect, ActionPermit, ActionAdmissionRefused, ActionResultPending)
from .bound_data_reads import BoundDataReads
from web_operation_agent.business.errors import BusinessToolIdentityError
from .native_tabs import NativeNavigationPolicy, NativeTabPolicy


class NativeReadActions:
    application_id = "yudao"
    required_scope = "yudao.read"
    actions = frozenset({"click", "goto"})

    def __init__(self, origin, data, record_routes, *, review, list_actions=None,
                 form_entries=None, list_tab_routes=()):
        if not isinstance(review, str) or not review.strip() or len(review) > 200:
            raise ValueError("native action effects require a deployment handler review")
        self.origin, self.data, self.review = origin.rstrip("/"), data, review
        self.record_routes = dict(record_routes)
        self.list_tab_routes = frozenset(list_tab_routes)
        self.list_actions = list_actions
        if list_actions is not None:
            self.actions = self.actions | list_actions.actions
        self._list_permits = {}
        self._list_turn = None
        from .form_open_actions import NativeFormOpenActions
        self.form_open_actions = NativeFormOpenActions(form_entries) if form_entries is not None else None
        self._form_permits = {}
        self._form_turn = None

    def _actor(self, context, checkpoint):
        context.require_live(application_id=self.application_id, required_scope=self.required_scope)
        return BoundDataReads(self.data, context, checkpoint)

    def _index(self, context, bound):
        return bound.index(context=context)

    def _form_read(self, checkpoint, call):
        client = self.data._client
        generation = client.read_generation
        def check():
            checkpoint()
            if client.read_generation != generation:
                raise BusinessToolIdentityError("the editor credential changed during verification")
        check()
        with client.read_window(check):
            result = call(check)
        check()
        return result

    def _path(self, url):
        parsed = urlsplit(url)
        if (f"{parsed.scheme}://{parsed.netloc}" != self.origin or parsed.query or parsed.fragment
                or not re.fullmatch(r"/(?!/)[^?#\\\s%]*", parsed.path)
                or any(p in {".", ".."} for p in parsed.path.split("/"))):
            raise ActionAdmissionRefused("invalid_target")
        return parsed.path

    def _record_target(self, target, index, context, bound):
        # Page/entity/record routes are in the outline. Compiling embedded line
        # tables reads unrelated details and is not needed to qualify navigation.
        _, schema = bound.outline_view(context=context)
        pages = {p.key:p for p in index.pages if p.entity
                 and (page := schema.page(p.key)) is not None and page.opens_records}
        matches = []
        for key, template in self.record_routes.items():
            if key not in pages or template.count("{id}") != 1:
                continue
            match = re.fullmatch(re.escape(template).replace(r"\{id\}", r"([1-9][0-9]*)"), target)
            if match:
                matches.append((key, pages[key].entity, match[1]))
        if not matches:
            return None
        if len(matches) != 1:
            raise ActionAdmissionRefused("invalid_target")
        key, entity, identifier = matches[0]
        spec = self.data._entities.get(entity)
        if spec is None or not spec.has_get:
            raise ActionAdmissionRefused("unreviewed")
        # A list may remain readable when detail access is denied. Navigation must
        # check the exact detail GET; the ordinary data read's list fallback is not proof.
        row = bound._invoke(self.data._client.get, spec.get_path, id=identifier)
        if (not isinstance(row, dict) or str(row.get("id")) != identifier
                or not self.data._display(entity, row).strip()):
            raise ActionAdmissionRefused("invalid_target")
        return ["record", target, key]

    def admit(self, intent, *, context, checkpoint):
        before = intent.observation
        if self.form_open_actions is not None and self.form_open_actions.matches(intent):
            self._actor(context, checkpoint)
            parsed = urlsplit(before.url)
            # The FormPort's reviewed entry binds its complete query location.
            # Retain the ordinary same-origin/path checks without weakening raw goto.
            path = self._path(parsed._replace(query="").geturl())
            if before.page_route != path:
                raise ActionAdmissionRefused("invalid_target")
            permit = self._form_read(checkpoint, lambda check: self.form_open_actions.admit(
                intent, context=context, checkpoint=check))
            turn = context.principal.binding_digest, context.session_id, context.turn_id
            if turn != self._form_turn:
                self._form_permits.clear(); self._form_turn = turn
            self._form_permits[permit.binding_digest] = self.data._client.read_generation
            return permit
        bound = self._actor(context, checkpoint)
        index = self._index(context, bound)
        path = self._path(before.url)
        if before.page_route != path and not (
                intent.action == "goto" and before.render_state == "blocked"
                and not before.page_route):
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
                expectation = self._record_target(target, index, context, bound)
        else:
            element = next(e for e in before.elements if e.ref == intent.arguments["ref"])
            attrs = element.attrs
            if self.list_actions is not None and (
                    intent.action in {"fill", "select"} or attrs.get("form_scope")
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
                if not NativeTabPolicy(routes.values(), pages & self.list_tab_routes).decide(before, element):
                    effect = ActionEffect.READ_ONLY
                    expectation = ["tab" if before.record_route == path else "list_tab",
                                   attrs["id"], attrs["aria-controls"], element.name]
            elif element.role == "link" and attrs.get("href"):
                from urllib.parse import urljoin
                target = self._path(urljoin(before.url, attrs["href"]))
                expectation = self._record_target(target, index, context, bound)
        if expectation is None:
            raise ActionAdmissionRefused("unreviewed")
        encoded = json.dumps([self.data._client.read_generation, expectation], separators=(",", ":"))
        if len(encoded) > 200:
            raise ActionAdmissionRefused("invalid_target")
        return ActionPermit(intent.binding_digest, effect, self.review, encoded)

    def verify(self, permit, intent, receipt, *, observation, context, checkpoint):
        if permit.binding_digest in self._form_permits:
            self._actor(context, checkpoint)
            if self._form_permits[permit.binding_digest] != self.data._client.read_generation:
                return False
            return self._form_read(checkpoint, lambda check: self.form_open_actions.verify(
                permit, intent, receipt, observation=observation, context=context, checkpoint=check))
        bound = self._actor(context, checkpoint)
        index = self._index(context, bound)
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
        if expected[0] in {"page", "record"} and path == expected[1] and not after.page_route:
            # The new document can report before Vue mounts its main route, with no
            # loading mask yet. Retain the consumed receipt and wait for render proof;
            # neither the URL nor this pending state can complete or replay navigation.
            raise ActionResultPending()
        if after.page_route != path:
            return False
        if expected[0] == "page":
            return path == expected[1] and path in {p.key for p in index.pages}
        if expected[0] == "record":
            return (path == expected[1] and after.record_route == path
                and self._record_target(path, index, context, bound) == expected)
        if after.url != before.url or after.record_route != before.record_route:
            return False
        if expected[0] == "branch":
            matches = [e for e in after.elements if e.attrs.get("navigation_kind") == "branch"
                       and e.attrs.get("navigation_path") == expected[1]]
            return len(matches) == 1 and matches[0].attrs.get("aria-expanded") == str(expected[2]).lower()
        if expected[0] == "list_tab":
            previous, current = before.page_read, after.page_read
            if previous is None:
                return False
            if current is None or current["revision"] <= previous["revision"]:
                # The tab can select before its list GET finishes. Wait on this
                # consumed receipt; never click the tab again to settle it.
                raise ActionResultPending()
            if (after.page_query is None or current["path"] != previous["path"]
                    or after.page_query["path"] != current["path"]
                    or current["query_digest"] == previous["query_digest"]):
                return False
        matches = [e for e in after.elements if e.role == "tab" and e.attrs.get("id") == expected[1]
                   and e.attrs.get("aria-controls") == expected[2] and e.name == expected[3]]
        # selected_controls is a bounded toolbar supplement. Native tab state is
        # already bound by its exact observed ID, owned panel and selected attribute.
        return (len(matches) == 1 and matches[0].attrs.get("aria-selected") == "true"
                and matches[0].attrs.get("panel_ready") == "true")
