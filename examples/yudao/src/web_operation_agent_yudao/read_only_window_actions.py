"""Explicit isolated page actions, protected by the live method fence.

This integration profile does not qualify event handlers. It is unavailable without
the verified HTTP guard; the ordinary deployment keeps its reviewed action policy.
"""
from dataclasses import fields
import hashlib
import json
import re
from urllib.parse import urljoin, urlsplit

from web_operation_agent.business.tools.contracts import form_control_unavailable
from web_operation_agent.execution.action_admission import (
    ActionAdmissionRefused, ActionEffect, ActionPermit, ActionResultPending)

from .guard_proxy import NativeGuardProxy
from .method_fenced_actions import _write_control
from .model_page_policy import HANDLERS, NativeDraftSteps, NativePageReview
from .read_actions import NativeReadActions


PROFILE = "read-only-window"
PROFILE_VERSION = 1


class ReadOnlyWindowDraftSteps(NativeDraftSteps):
    """Keep business-value preparation, without treating every dialog click as a draft."""

    def handles(self, intent):
        if intent.action not in self.actions:
            return False
        element = self._target(intent)
        if element.attrs.get("page_control") == "query":
            return False
        if intent.action == "click" and element.role not in {"option", "treeitem", "radio"}:
            return False
        return super().handles(intent)


class ReadOnlyWindowPageReview(NativePageReview):
    """Tool visibility in the isolated window is independent of handler catalogs."""

    @classmethod
    def from_review(cls, review):
        values = {item.name: getattr(review, item.name)
                  for item in fields(NativePageReview)}
        # Shared tool schemas also consult this set before action admission.
        # Do not silently retain that handler gate in the isolated profile.
        return cls(**{**values, "disabled_handlers": frozenset()})

    @property
    def capabilities(self):
        return super().capabilities | {"sections", "facets", "paging", "fill", "lines"}

    def permits(self, kind, path, control=None):
        return kind in HANDLERS

    def denies(self, kind, path):
        return False

    def reviewed_control(self, kind, path, control):
        return kind in HANDLERS


class ReadOnlyWindowPageActions(NativeReadActions):
    """Keep identity, fresh targets, consent and the fence; omit handler matching."""

    actions = frozenset({"goto", "click", "fill", "select"})

    def __init__(self, origin, data, record_routes, goal_port, review, *, guard_proxy,
                 window=""):
        if not isinstance(guard_proxy, NativeGuardProxy) or not guard_proxy.method_fence_ready:
            raise ValueError("read-only-window requires the running, ingress-verified method fence")
        super().__init__(origin, data, record_routes, review="read-only-window/v1")
        self.guard_proxy = guard_proxy
        #: Which guard window the page the current turn runs on works through; page-load
        #: passes are armed and cleared for it alone, so two open pages never borrow each
        #: other's. A callable resolves the window when the guard is asked -- shared
        #: conversations name the page the turn is running on, which can change per turn.
        self._window = window

    def _window_name(self) -> str:
        return str(self._window() if callable(self._window) else self._window or "")

    def _guard(self):
        if not self.guard_proxy.method_fence_ready:
            raise ActionAdmissionRefused("unsafe_effect")

    def _same_origin(self, url):
        parsed, origin = urlsplit(url), urlsplit(self.origin)
        if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                or (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc)
                or "\\" in url or any(ord(char) < 32 for char in url)):
            raise ActionAdmissionRefused("invalid_target")
        return parsed.geturl()

    def admit(self, intent, *, context, checkpoint):
        self._guard()
        self._actor(context, checkpoint)
        before, args = intent.observation, intent.arguments
        self._same_origin(before.url)
        target = ""
        if intent.action == "goto":
            target = self._same_origin(urljoin(before.url, args["url"]))
        else:
            element = next(e for e in before.elements if e.ref == args["ref"])
            if (form_control_unavailable(element.attrs)
                    or element.attrs.get("target", "") not in {"", "_self"}):
                raise ActionAdmissionRefused("invalid_target")
            if (intent.action in {"fill","select"} and element.attrs.get("form_mode") in {"create","edit"}
                    and element.attrs.get("page_control") != "query"):
                # The common draft port must issue business values; this profile
                # does not turn their entry into an unqualified ordinary action.
                raise ActionAdmissionRefused("unsafe_effect")
            if intent.action == "click":
                if _write_control(element):
                    raise ActionAdmissionRefused("unsafe_effect")
                href = element.attrs.get("href", "")
                inert = re.fullmatch(r"javascript:\s*(?:void\s*\(\s*0\s*\)|;?)\s*;?", href, re.I)
                if href and not inert:
                    self._same_origin(urljoin(before.url, href))
        actor = self.guard_proxy.resolve_actor(self.data._client._login(), self.data._client.tenant)
        if intent.action == "goto" or (intent.action == "click" and (
                element.attrs.get("href") or element.attrs.get("navigation_kind"))):
            # The page this opens loads itself; its own GETs pass, even scan-listed ones.
            self.guard_proxy.guard.arm_page_load(actor, 10.0, window=self._window_name())
        else:
            # Any other action is not that page's loading: nothing earlier carries over to it.
            self.guard_proxy.guard.clear_page_load(actor, window=self._window_name())
        postcondition = json.dumps([self.data._client.read_generation,
            hashlib.sha256(target.encode()).hexdigest() if target else ""], separators=(",", ":"))
        return ActionPermit(intent.binding_digest, ActionEffect.READ_ONLY,
                            self.review, postcondition)

    def verify(self, permit, intent, receipt, *, observation, context, checkpoint):
        self._guard()
        self._actor(context, checkpoint)
        generation, target = json.loads(permit.postcondition)
        if generation != self.data._client.read_generation or not receipt.write_guard_held:
            return False
        actor = self.guard_proxy.resolve_actor(self.data._client._login(), self.data._client.tenant)
        window = self._window_name()
        held = (self.guard_proxy.guard.active(actor, window) if window
                else self.guard_proxy.guard.window_held(actor))
        if not held:
            return False
        self._same_origin(observation.url)
        if observation.render_state == "pending":
            raise ActionResultPending()
        return (observation.render_state == "ready" and
                (not target or hashlib.sha256(observation.url.encode()).hexdigest() == target))
