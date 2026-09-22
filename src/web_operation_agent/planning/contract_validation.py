"""Normalize, index, validate, and conservatively prune operation contracts."""
from __future__ import annotations

from ..schemas import ContractStep, MutationSignature, OperationContract, SourceMap
from .retrieval import EvidenceBundle


class PlanningFailed(Exception):
    pass


def _segment_from_template(template: str) -> str:
    """The literal path segment an adapter's `contract_route_template` wraps around routes.

    A template shaped `/{scope}/<segment>/{route}` yields `<segment>`. Deriving it from the
    one template an adapter already declares avoids a second field that could disagree with it.
    """
    if not template:
        return ""
    _before, _, rest = template.partition("{scope}")
    middle, _, _after = rest.partition("{route}")
    return middle.strip("/")


def normalise_request(webapp: str, request: str, *, segment: str) -> str:
    """Accept the request name in either spelling the page presents it in.

    A form's action reads a full path such as `/billing/<segment>/findInvoices`, and a
    model that copies it verbatim is being reasonable -- it is quoting the deployment.
    Stripping the servlet prefix is syntax, not task knowledge: it says nothing about *which*
    request is right, it is applied identically to every planning mode, and multi-segment
    request names such as `Schedule/create` come through untouched. A request
    must not be rejected merely because the model copied the deployment's servlet prefix.
    `segment` is the adapter-declared path segment that wraps a route; an adapter with no such
    convention passes an empty `segment` and only the bare-name fallback below ever applies.
    """
    u = (request or "").strip().strip("'\"")
    if u.startswith("http://") or u.startswith("https://"):
        u = u.split("://", 1)[1]
        u = u.split("/", 1)[1] if "/" in u else ""
    u = u.lstrip("/")
    if segment:
        prefix = f"{webapp}/{segment}/"
        wrapped = f"/{segment}/"
        if u.startswith(prefix):
            u = u[len(prefix):]
        elif wrapped in u:
            u = u.split(wrapped, 1)[1]
        elif u.startswith(f"{webapp}/"):
            # `accounting/createPaymentApplication` -- the webapp named twice, once by the
            # field and once in the value. Same concession, one segment shorter.
            u = u[len(webapp) + 1:]
    elif u.startswith(f"{webapp}/"):
        u = u[len(webapp) + 1:]
    return u.split("?", 1)[0].rstrip("/")


def normalise_citation(key: str) -> str:
    """Accept a citation key however the model copied it out of the facts.

    The facts are rendered as `[path:start-end] text`, so a model that quotes its source
    verbatim writes `[path:start-end]`. Rejecting that is the same mistake `normalise_request`
    avoids: punctuation the model copied from our own presentation is not a claim about the
    application.
    """
    return (key or "").strip().strip("[]").strip().strip("`").strip()


def split_handler_subject(subject: str) -> tuple[str, str]:
    """`webapp/request` as the compiler writes it for a controller entry.

    The request part may itself contain slashes, as in `beacon/LampSchedule/create`,
    so only the first segment is the application.
    """
    webapp, _, request = (subject or "").partition("/")
    return webapp.lower(), request.lower()


def qualified_request(uri: str, *, segment: str, default_webapp: str = "") -> str:
    """`webapp/request`, lowercased, for comparing one deployment's requests with each other.

    A target that carries its own application keeps it: inter-application menu links are real
    and re-homing them to the linking application would lose the very fact they record. A bare
    name takes the application of whatever declared it. `segment` is the same adapter-declared
    wrapper `normalise_request` takes; without one, nothing proves an application prefix and
    every uri is treated as a bare request.
    """
    u = (uri or "").strip().strip("'\"")
    if "://" in u:
        u = u.split("://", 1)[1]
        u = u.split("/", 1)[1] if "/" in u else ""
    cleaned = u.strip("/")
    wrapped = f"/{segment}/" if segment else ""
    if wrapped and wrapped in cleaned:
        head, _, rest = cleaned.partition(wrapped)
        webapp = head.rsplit("/", 1)[-1]
    else:
        # Only the adapter's own wrapper proves an application prefix. A bare `a/b` is
        # ambiguous and may be a request name that contains a slash, such as
        # `LampSchedule/create`, rather than an application. Reading the first segment as the
        # application turned that request into an application called LampSchedule and dropped
        # the step that named it.
        webapp, rest = "", cleaned
    return f"{(webapp or default_webapp or '').lower()}/{bare_request(rest, segment=segment).lower()}"


def bare_request(uri: str, *, segment: str) -> str:
    """A request name with any servlet path stripped, without knowing the webapp.

    `normalise_request` needs the webapp to strip `<webapp>/<segment>/`; the source map holds
    targets written by whoever authored the descriptor -- inter-app menu links carry the full
    `/ap/<segment>/findPayments`, ordinary ones carry the bare name. Both must compare equal
    or a citation that is genuinely about a request is read as being about nothing.
    """
    u = (uri or "").strip().strip("'\"")
    if "://" in u:
        u = u.split("://", 1)[1]
        u = u.split("/", 1)[1] if "/" in u else ""
    wrapped = f"/{segment}/" if segment else ""
    if wrapped and wrapped in u:
        u = u.split(wrapped, 1)[1]
    return u.lstrip("/").split("?", 1)[0].rstrip("/")


def normalise_contract(contract: OperationContract, *, segment: str) -> OperationContract:
    def cites(seq):
        return tuple(normalise_citation(c) for c in seq)

    def fix(step):
        return ContractStep(intent=step.intent, webapp=step.webapp,
                            request=normalise_request(step.webapp, step.request,
                                                      segment=segment),
                            landmark=step.landmark, note=step.note,
                            citations=cites(step.citations))
    m = contract.mutation
    return OperationContract(
        schema_version=contract.schema_version, task_ref=contract.task_ref,
        summary=contract.summary,
        entry_candidates=[fix(s) for s in contract.entry_candidates],
        preconditions=list(contract.preconditions),
        steps=[fix(s) for s in contract.steps],
        mutation=(MutationSignature(webapp=m.webapp,
                                    request=normalise_request(m.webapp, m.request,
                                                              segment=segment),
                                    required_params=m.required_params, service=m.service,
                                    citations=cites(m.citations)) if m else None),
        unknowns=list(contract.unknowns),
        citations=list(cites(contract.citations)))


class SupportIndex:
    """Which requests each piece of evidence is actually about.

    Checking that a citation key exists in the bundle only proves the model copied a real key.
    It does not prove the key has anything to do with the claim it was attached to -- a
    contract could name one request and cite the evidence for another, and every check would
    pass. This index closes that gap by asking the map what each key is evidence *for*.

    It is a structural check, not a semantic one. It catches a citation that is about the
    wrong request. It cannot catch a step that names the right request and describes it
    wrongly in prose, and it is not claimed to.
    """

    def __init__(self, smap: SourceMap, *, segment: str):
        self._segment = segment
        # Key by the full `webapp/request`: request names such as `edit` and `view` can occur
        # in more than one application, and evidence from one must not support another.
        self.requests: dict[str, set[str]] = {}
        self.services: dict[str, set[str]] = {}
        service_requests: dict[str, set[str]] = {}
        # Which applications declare a controller entry of each bare name. A form or menu
        # names its target the way its author wrote it -- sometimes `/ap/<segment>/findPayments`,
        # usually just `findPayments` -- and a form descriptor's subject is a file path, not an
        # application, so the owning application has to come from the declarations themselves.
        by_bare: dict[str, set[str]] = {}
        for r in smap.by_kind("request_handler"):
            webapp, request = split_handler_subject(r.subject)
            by_bare.setdefault(request, set()).add(f"{webapp}/{request}")
            if r.object:
                service_requests.setdefault(r.object, set()).add(f"{webapp}/{request}")
        for kind in ("request_handler", "request_response"):
            for r in smap.by_kind(kind):
                webapp, request = split_handler_subject(r.subject)
                for e in r.evidence:
                    self.requests.setdefault(e.key, set()).add(f"{webapp}/{request}")
        for kind in ("form_target", "menu_link"):
            for r in smap.by_kind(kind):
                if not r.object:
                    continue
                for uri in self._targets_of(r.object, by_bare):
                    for e in r.evidence:
                        self.requests.setdefault(e.key, set()).add(uri)
        for kind in ("service_parameter", "service_entity"):
            for r in smap.by_kind(kind):
                for e in r.evidence:
                    self.services.setdefault(e.key, set()).add(r.subject)
        # A fact about a service is a fact about every request that runs it.
        for key, services in self.services.items():
            for svc in services:
                self.requests.setdefault(key, set()).update(service_requests.get(svc, set()))

    def _targets_of(self, obj: str, by_bare: dict[str, set[str]]) -> set[str]:
        """Which `webapp/request` a form or menu target refers to.

        A reference that carries its own application keeps it: inter-application menu links
        are real and re-homing them would lose the fact they record. A bare name is resolved
        against the applications that actually declare it. An ambiguous bare name -- one
        declared by multiple applications -- resolves to neither, because its evidence cannot
        be attributed safely to a single application.
        """
        qualified = qualified_request(obj, segment=self._segment)
        if not qualified.startswith("/"):
            return {qualified}
        bare = qualified.lstrip("/")
        owners = by_bare.get(bare, set())
        return set(owners) if len(owners) == 1 else set()

    def about(self, key: str) -> set[str]:
        return self.requests.get(key, set())

    def supports(self, citations, request: str, webapp: str = "") -> bool:
        """Is any of these citations evidence about this application's request?"""
        if not request:
            return True
        want = qualified_request(request, segment=self._segment, default_webapp=webapp)
        return any(want in self.about(key) for key in citations)


def prune_contract(contract: OperationContract, smap: SourceMap, *, segment: str
                   ) -> tuple[OperationContract, list[str]]:
    """Drop the moves this deployment does not have, or that nothing cited supports.

    A contract with three checkable steps and two unfounded ones is more useful than no
    contract, provided the unfounded ones never reach the agent. So they are removed and
    recorded as explicit unknowns rather than voiding the whole answer -- the same rule the
    compiler follows for descriptors it cannot interpret.

    Two ways to be unfounded: naming a request this deployment does not have, and naming one
    the step's own citations say nothing about.
    """
    handlers = {r.subject for r in smap.by_kind("request_handler")}
    targets = {r.object for r in smap.by_kind("form_target")}
    support = SupportIndex(smap, segment=segment)
    dropped: list[str] = []

    def keep(step, label):
        if not step.request:
            return True
        if not (f"{step.webapp}/{step.request}" in handlers or step.request in targets):
            dropped.append(f"the source does not show a request {step.webapp}/{step.request} "
                           f"for {label} {step.intent!r}; that move is unverified")
            return False
        if not support.supports(step.citations, step.request, step.webapp):
            dropped.append(f"nothing cited for {label} {step.intent!r} is evidence about "
                           f"{step.webapp}/{step.request}; that move is unsupported")
            return False
        return True

    entries = [s for s in contract.entry_candidates if keep(s, "entry")]
    steps = [s for s in contract.steps if keep(s, "step")]
    mutation = contract.mutation
    if mutation:
        subject = f"{mutation.webapp}/{mutation.request}"
        if subject not in handlers and mutation.request not in targets:
            dropped.append(f"the source does not show a request {subject}; the write endpoint "
                           f"is unknown")
            mutation = None
        elif not support.supports(mutation.citations, mutation.request, mutation.webapp):
            dropped.append(f"nothing cited for the write endpoint is evidence about {subject}; "
                           f"the write endpoint is unsupported")
            mutation = None
    return OperationContract(
        schema_version=contract.schema_version, task_ref=contract.task_ref,
        summary=contract.summary, entry_candidates=entries,
        preconditions=list(contract.preconditions), steps=steps, mutation=mutation,
        unknowns=list(contract.unknowns) + dropped,
        citations=list(contract.citations)), dropped


def validate_contract(contract: OperationContract, bundle: EvidenceBundle,
                      smap: SourceMap | None = None) -> list[str]:
    """The reasons this contract cannot be accepted at all.

    Only two things are fatal: citing evidence that was never shown, and citing nothing.
    Both mean the answer cannot be checked, which is the one thing this layer exists to
    prevent. Naming a request the deployment does not have is not fatal -- see
    `prune_contract`, which removes those moves and records them as unknowns.

    A planner given no evidence is held to neither rule, because neither can mean anything
    to it. That is the one asymmetry the comparison cannot avoid: an arm with nothing to cite
    cannot be asked to cite. Everything else -- schema, budget, repair, withdrawal, recovery
    -- is the same code for both.
    """
    problems: list[str] = []
    allowed = bundle.citation_keys
    if not allowed:
        return [f"citation {key!r} was invented; you were given no facts to cite"
                for key in sorted(contract.cited_keys())]
    for key in sorted(contract.cited_keys()):
        if key not in allowed:
            problems.append(f"citation {key!r} was not among the facts you were given; "
                            f"remove it or replace it with a key you were actually shown")
    if not contract.cited_keys():
        problems.append("the contract cites nothing; every assertion must carry a citation key")
    return problems
