"""Where a navigation may go, and whether the page got there.

Both were static methods of the session class, which is how they read: they take what they
need and touch nothing else. `candidate_url` assembles one same-origin URL from a source
affordance and the model's bindings; `observation_is_at_route` answers whether an
observation is the page a route names.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import partial
from urllib.parse import quote, urlencode, urljoin, urlsplit

from ..affordance import SourceAffordance
from ..privacy import SessionHandoffPolicy, safe_tool_arguments
from ..schemas import PageObservation
from ..grounding import opaque_route_identity

def candidate_url(candidate: SourceAffordance, bindings, *,
                   route_identity=opaque_route_identity,
                   handoffs: "SessionHandoffPolicy | None" = None) -> tuple[str, str]:
    """Validate bindings and assemble one same-origin URL, without evaluating source.

    The second tuple member is an error message; an empty error means the URL is safe.
    Values are encoded by the standard library, so a binding can never replace the path
    or introduce a second query parameter.

    `handoffs` carries the mounted application's session-parameter vocabulary. Both the
    declared-parameter check and the binding-value check use it, so an application's own
    session parameter cannot be smuggled in as a candidate binding merely because this
    package's static baseline has never heard of its name.
    """
    policy = handoffs or SessionHandoffPolicy.extending(())
    session_handoff_query_keys = policy.query_keys
    if not isinstance(bindings, dict):
        return "", "bindings must be an object of declared string parameters"
    if len(bindings) > 16:
        return "", "bindings exceed the maximum of 16 parameters"
    declared: dict[str, list] = {}
    for parameter in candidate.parameters:
        declared.setdefault(parameter.name, []).append(parameter)
    if any(name.casefold() in session_handoff_query_keys for name in declared):
        return "", "the candidate declares a forbidden session parameter"
    if any(not isinstance(name, str) or name not in declared for name in bindings):
        return "", "bindings contain an undeclared parameter"
    if any(not isinstance(value, str) for value in bindings.values()):
        return "", "every binding value must be a string"
    for name, value in bindings.items():
        if (len(value) > 1000 or re.search(r"[\x00-\x1f\x7f]", value)
                or "${" in value or policy.has_unredacted(
                    {name: value}, url_decode_depth=3)):
            return "", "a binding contains a forbidden value"

    parsed = urlsplit(candidate.route_template)
    path_parts = parsed.path.split("/")
    safe_path = bool(candidate.route_template.startswith("/")
                     and not candidate.route_template.startswith("//")
                     and not parsed.scheme and not parsed.netloc
                     and not parsed.query and not parsed.fragment
                     and "%" not in parsed.path
                     and not ({".", ".."} & set(path_parts))
                     and "\\" not in parsed.path
                     and not re.search(r"[\x00-\x1f\x7f]", parsed.path))
    if not safe_path or route_identity(candidate.route_template) != (
            candidate.webapp.casefold(), candidate.request.rstrip("/").casefold()):
        return "", "the candidate route is not a safe relative control path"

    values: dict[str, str] = {}
    for name, alternatives in declared.items():
        literals = {parameter.value for parameter in alternatives
                    if parameter.value and "${" not in parameter.value}
        if len(literals) > 1:
            return "", "the candidate has conflicting fixed parameter values"
        # A single inert literal is safe to carry. from_field, implicit and expression
        # declarations remain unresolved unless the model supplies a concrete binding.
        if len(literals) == 1:
            literal = next(iter(literals))
            if name in bindings and bindings[name] != literal:
                return "", "a binding attempts to override a fixed parameter value"
            if (len(literal) > 1000 or re.search(r"[\x00-\x1f\x7f]", literal)
                    or policy.has_unredacted(
                        {name: literal}, url_decode_depth=3)):
                return "", "a declared literal contains a forbidden value"
            values[name] = literal
        elif name in bindings:
            values[name] = bindings[name]
    query = urlencode(sorted(values.items()))
    url = candidate.route_template + ("?" + query if query else "")
    if len(url) > 2000:
        return "", "the assembled route is too long"
    return url, ""


def observation_is_at_route(observation: PageObservation, route: str) -> bool:
    actual = urlsplit(observation.url)
    expected = urlsplit(urljoin(observation.url, route))
    return bool(
        actual.scheme == expected.scheme and actual.netloc == expected.netloc
        and (actual.path.rstrip("/") or "/") == (expected.path.rstrip("/") or "/")
        and actual.query == expected.query)



@dataclass(frozen=True)
class RouteResolution:
    """What a routing tool resolved to: a browser action, or a refusal.

    ``enter_application``, ``enter_skill_app``, ``open_skill_route`` and
    ``open_source_candidate`` are not browser actions. Each one decides *which* exact page
    request the agent is allowed to make, then hands that request to the same dispatch path
    every other action uses. Making that answer a value is what lets one copy of the refusal
    sequence serve all four.
    """

    name: str = ""
    args: dict = field(default_factory=dict)
    citations: tuple[str, ...] = ()
    skill: object | None = None
    rejection: str = ""

    @property
    def refused(self) -> bool:
        return bool(self.rejection)


@dataclass(frozen=True)
class RouteTool:
    """One routing tool: how it resolves, and what a refusal has to say."""

    resolve: object
    explanation: object
    no_fresh_detail: str
    # A refused source candidate re-observes, and the new page may carry different
    # candidates. Its plan message has to be refreshed before the model is told anything,
    # which the other three do not need.
    resync_plan_on_refusal: bool = False


class Router:
    """Which exact page request a navigation tool is allowed to make.

    Every one of these answers comes from something installed for this session -- the
    application's own entry contract, the Skill catalogue, the compiled source map -- and
    from nothing the model said beyond naming one of them. It was six methods of the
    session class reading nine of its attributes (P7.5 D-01).
    """

    def __init__(self, *, ledger, application_routes, knowledge_router, source_map,
                 session_handoffs, route_identity, allow_application_entry: bool,
                 source_affordance_mode: str):
        self.ledger = ledger
        self.application_routes = application_routes
        self.knowledge_router = knowledge_router
        self.source_map = source_map
        self.session_handoffs = session_handoffs
        self.route_identity = route_identity
        self.allow_application_entry = allow_application_entry
        self.source_affordance_mode = source_affordance_mode

    def tools(self, *, skill_ids: set[str],
              affordances: "dict[str, SourceAffordance]") -> dict[str, "RouteTool"]:
        """The routing tools this turn actually offers.

        Built from the same capabilities that decide the tool surface, so a tool the model
        cannot see can never be resolved here either. A name absent from this mapping is
        not a routing tool, which is the whole check -- there is no second condition buried
        in the branch to forget.
        """
        tools: dict[str, RouteTool] = {}
        if self.allow_application_entry:
            tools["enter_application"] = RouteTool(
                resolve=self._enter_application,
                explanation=lambda reason: (
                    "The server rejected enter_application before dispatch: "
                    + reason + "."),
                no_fresh_detail=("application-entry refusal did not obtain a fresh "
                                 "observation"))
        if self.knowledge_router is not None:
            tools["enter_skill_app"] = RouteTool(
                resolve=partial(self._enter_skill_app, skill_ids=skill_ids),
                explanation=lambda reason: (
                    "The server rejected enter_skill_app before dispatch: " + reason + "."),
                no_fresh_detail=("Skill app refusal could not obtain a successful "
                                 "fresh observation"))
            tools["open_skill_route"] = RouteTool(
                resolve=partial(self._open_skill_route, skill_ids=skill_ids),
                explanation=lambda reason: (
                    "The server rejected open_skill_route before dispatch: " + reason
                    + ". Use the current page to satisfy the Skill precondition, or choose "
                    "another action."),
                no_fresh_detail=("Skill refusal could not obtain a successful fresh "
                                 "observation"))
        if self.source_affordance_mode in ("auto", "empty"):
            tools["open_source_candidate"] = RouteTool(
                resolve=partial(self._open_source_candidate, affordances=affordances),
                explanation=lambda reason: (
                    "The server rejected open_source_candidate before dispatch: "
                    f"{reason}. The browser received only observe and returned a fresh "
                    "page. Choose from the newly delivered candidates or act on that page."),
                no_fresh_detail=("source-candidate refusal could not obtain a "
                                 "successful fresh observation"),
                resync_plan_on_refusal=True)
        return tools

    def _application_link_ref(self, ref: object, obs: PageObservation) -> tuple[str, str]:
        if self.application_routes is None:
            return "", "no application entry contract is installed"
        return self.application_routes.validate_application_entry(ref, obs)

    def _enter_application(self, args: dict, obs: PageObservation,
                                   step: int) -> "RouteResolution":
        ref, rejection = self._application_link_ref(args.get("ref"), obs)
        if rejection:
            self.ledger.append(
                "application_entry_rejected", step=step, reason=rejection,
                element_ref=str(args.get("ref") or "")[:100],
                requested_tool_dispatched=False, side_effect_possible=False)
            return RouteResolution(rejection=rejection)
        self.ledger.append("application_entered", step=step, element_ref=ref)
        return RouteResolution(name="click", args={"ref": ref, "expect_navigation": True})

    def _enter_skill_app(self, args: dict, obs: PageObservation,
                                 step: int, *, skill_ids: set[str]) -> "RouteResolution":
        ref, rejection, skill = self.knowledge_router.app_link(
            args.get("skill_id"), obs, skill_ids)
        if rejection:
            self.ledger.append(
                "skill_app_rejected", step=step,
                skill_id=str(args.get("skill_id") or "")[:200], reason=rejection,
                requested_tool_dispatched=False, side_effect_possible=False)
            return RouteResolution(rejection=rejection)
        assert skill is not None
        self.ledger.append(
            "skill_app_entered", step=step, skill_id=skill.skill_id,
            element_ref=ref, skill_sha256=skill.artifact_sha256,
            provenance=list(skill.provenance))
        return RouteResolution(name="click", args={"ref": ref, "expect_navigation": True},
                                citations=tuple(skill.provenance), skill=skill)

    def _open_skill_route(self, args: dict, obs: PageObservation,
                                  step: int, *, skill_ids: set[str]) -> "RouteResolution":
        url, rejection, skill = self.knowledge_router.resolve(
            args.get("skill_id"), args.get("bindings"), obs.url, skill_ids)
        if rejection:
            self.ledger.append(
                "skill_route_rejected", step=step,
                skill_id=str(args.get("skill_id") or "")[:200],
                reason=rejection, requested_tool_dispatched=False,
                side_effect_possible=False)
            return RouteResolution(rejection=rejection)
        assert skill is not None
        self.ledger.append(
            "skill_route_opened", step=step, skill_id=skill.skill_id, url=url,
            skill_sha256=skill.artifact_sha256,
            artifact_path=skill.artifact_path, provenance=list(skill.provenance))
        return RouteResolution(name="goto", args={"url": url},
                                citations=tuple(skill.provenance))

    def _open_source_candidate(self, args: dict, _obs: PageObservation,
                                       step: int, *,
                               affordances: "dict[str, SourceAffordance]") -> "RouteResolution":
        candidate_id = args.get("candidate_id")
        candidate = (affordances.get(candidate_id)
                     if isinstance(candidate_id, str) else None)
        bindings = args.get("bindings")
        rejection = ""
        url = ""
        if candidate is None:
            rejection = "candidate_id was not supplied on the current observation"
        elif (self.source_map is None
              or candidate.source_map_digest != self.source_map.content_digest):
            rejection = "candidate_id is stale for the current source binding"
        else:
            url, rejection = candidate_url(
                candidate, bindings,
                route_identity=self.route_identity,
                handoffs=self.session_handoffs,
            )
        if rejection:
            citations = list(candidate.citations) if candidate is not None else []
            binding_names = (sorted(str(key)[:100] for key in bindings)
                             if isinstance(bindings, dict) else [])
            self.ledger.append(
                "source_candidate_rejected", step=step,
                candidate_id=str(candidate_id or "")[:200], reason=rejection,
                binding_names=binding_names, citations=citations,
                requested_tool_dispatched=False, side_effect_possible=False)
            return RouteResolution(rejection=rejection)
        self.ledger.append(
            "source_candidate_opened", step=step, candidate_id=candidate.candidate_id,
            source_digest=candidate.source_map_digest, url=url,
            bindings=safe_tool_arguments(bindings, self.session_handoffs),
            citations=list(candidate.citations))
        return RouteResolution(name="goto", args={"url": url},
                                citations=tuple(candidate.citations))
