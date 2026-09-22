"""What the session knows from outside the page: source retrieval, capability, asking.

These are the tools that answer about the deployment itself rather than about the
business application: bounded source evidence for a query, the whole-catalog capability
search a decline stands on, and the one question back to the person a turn may hold
(P7.5 D-01).
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from ..answer.evidence import (CapabilityEvidence, Origin, SourceEvidence,
                               SourceHistoryEvidence)
from ..transport.browser import TransportError
from ..planning.retrieval import EvidenceBundle
from ..planning.source_facts import terms
from ..privacy import redact_sensitive_assignments
from ..schemas import BROWSER_ACTIONS, PageObservation, digest
from .prompts import CLARIFICATION_CHOICE_LIMIT, _assistant_call, _tool_result
from .runstate import RunResult, _Next, _Turn


# -- routing tools ---------------------------------------------------------------


def _source_route_path(session, identity: str) -> str:
    """Render a source request identity in the application's configured route shape."""
    scope, _, route = identity.partition("/")
    if not scope or not route or session.application_routes is None:
        return identity
    try:
        return session.application_routes.compose_contract_route(scope, route)
    except ValueError:
        return identity


def _source_line(session, hit, query: str) -> str:
    """Give quotable source bytes a turn-owned evidence reference."""
    reference = hit.evidence
    if not reference.excerpt.strip():
        return hit.line
    ref = session._source_evidence_ids.get(reference.key, "")
    if not ref or ref not in session.evidence:
        session._source_evidence_count += 1
        ref = f"source#{session._source_evidence_count}"
        session.evidence.record(SourceEvidence(
            evidence_id=ref,
            origin=Origin(kind="source", reference=f"lookup_source:{query[:120]}",
                          observed_at=str(time.time()),
                          scope=f"the deployed release {reference.release}"),
            path=reference.path, start_line=reference.start_line,
            end_line=reference.end_line, release=reference.release,
            sha256=reference.sha256, excerpt=reference.excerpt, summary=hit.text,
            fact_type=hit.fact_type))
        session._source_evidence_ids[reference.key] = ref
    session._answers.answer_mode = True
    return f"{ref} {hit.line}"


def lookup_source(session, query: str, obs: PageObservation) -> EvidenceBundle:
    """Local retrieval only: no planner/model call, filesystem expansion or hidden data.

    The query originates in the live loop, and the current page provides its context.
    The exact bounded evidence is persisted for a decision-level audit.
    """
    if not session.runtime_source:
        raise ValueError("runtime source lookup is disabled")
    if session.source_affordance_mode == "empty":
        bundle = EvidenceBundle(task_ref=session.task.task_ref)
        session.ledger.append("runtime_source_retrieved", query=query, page_url=obs.url,
                           page_epoch=obs.epoch, source_digest="",
                           evidence=[], licensed_view_routes=[], **bundle.stats)
        return bundle
    if session._runtime_retriever is None:
        raise ValueError("runtime source lookup has no retriever")
    started = time.monotonic()
    lookup = session.source.fact_lookup()
    hits = (lookup.search(query, limit=10,
                          route=lambda identity: _source_route_path(session, identity))
            if lookup is not None else ())
    bundle = session._runtime_retriever.retrieve(
        session.task.task_ref, query,
        webapps=session.served_webapps, max_requests=12, max_menu=4, max_forms=4, max_lines=70)
    if hits:
        bundle.lines[:0] = [_source_line(session, hit, query) for hit in hits]
        for hit in hits:
            bundle.refs.setdefault(hit.evidence.key, hit.evidence)
    bundle.stats = {**bundle.stats, "facts_shown": len(hits),
                    "lines": len(bundle.lines), "citations": len(bundle.refs),
                    "latency_ms": round((time.monotonic() - started) * 1000, 1)}
    cited = bundle.citation_keys
    for relation in session.source_map.by_kind("request_handler"):
        if relation.attrs.get("event_type") != "view-only":
            continue
        keys = {ref.key for ref in relation.evidence} & cited
        if keys:
            webapp, _, request = relation.subject.partition("/")
            session._source_view_routes.setdefault(
                (webapp.lower(), request.rstrip("/").lower()), set()).update(keys)
    session.ledger.append("runtime_source_retrieved", query=query, page_url=obs.url,
                       page_epoch=obs.epoch, source_digest=session.source_map.content_digest,
                       evidence=list(bundle.lines),
                       licensed_view_routes=[f"{a}/{r}" for a, r in session._source_view_routes],
                       **bundle.stats)
    return bundle


def lookup_source_tool(session, turn: "_Turn", call: dict, name: str, args: dict) -> "_Next":
    """Cited source evidence, and then a fresh look at the page it is about."""
    query = args.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        return session._reply(turn, call,
                           "query must be a nonempty string of at most 1000 characters")
    bundle = lookup_source(session, query, turn.obs)
    if session._cancel_requested():
        return _Next(session._finish(
            turn.result, turn.started_at, "user_cancelled",
            "the user stopped this task after source retrieval", steps=turn.step))
    try:
        receipt = session.browser.execute("observe", {})
        session._browser_attempts += 1
    except TransportError as e:
        return _Next(session._finish(turn.result, turn.started_at,
                                  session._transport_terminal("observe"), str(e),
                                  steps=turn.step))
    write = session._writes.write_outcome(receipt, turn.step)
    session.ledger.append("action_receipt", step=turn.step, tool="observe",
                       command_id=receipt.command_id, after_tool="lookup_source",
                       ok=receipt.ok, error_code=receipt.error_code,
                       write=write["state"], write_attribution="unattributed")
    reason, detail = session._writes.settle_write(write)
    if reason:
        return _Next(session._finish(turn.result, turn.started_at, reason, detail,
                                  steps=turn.step))
    if receipt.observation is not None:
        turn.obs = receipt.observation
    note = session._ground(turn.obs, receipt, write["state"], write["refused_requests"])
    empty_note = (
        "No searchable source terms in the query. Try a concrete label, identifier, or "
        "source phrase. Do not invent an answer."
        if not terms(query) else "No matching source evidence. Do not invent an answer."
    )
    return session._reply(turn, call,
                       "SOURCE EVIDENCE (prior; current page wins):\n" +
                       (bundle.render() or empty_note) +
                       "\n\n" + session._render(turn.obs, receipt, note, write,
                                              affordance_query=query))


def _source_history_line(session, hit) -> str:
    ref = session._source_history_evidence_ids.get(hit.key, "")
    if not ref or ref not in session.evidence:
        session._source_history_evidence_count += 1
        ref = f"history#{session._source_history_evidence_count}"
        session.evidence.record(SourceHistoryEvidence(
            evidence_id=ref,
            origin=Origin(kind="source", reference=f"lookup_source_history:{hit.kind}",
                          observed_at=str(time.time()),
                          scope=(f"local Git metadata at deployed commit "
                                 f"{hit.deployed_commit}")),
            release=hit.release, deployed_commit=hit.deployed_commit,
            history_kind=hit.kind, statement=hit.statement, sha256=hit.digest,
            path=hit.path, start_line=hit.start_line, end_line=hit.end_line,
            commit=hit.commit))
        session._source_history_evidence_ids[hit.key] = ref
    session._answers.answer_mode = True
    return f"{ref} [{hit.kind}] {hit.statement}"


def lookup_source_history(session, kind: str, *, path: str = "", start_line: int = 0,
                          end_line: int = 0, target_commit: str = "",
                          limit: int = 8) -> str:
    """Return bounded Git metadata or an explicit deployment-level unavailability."""
    if not session.runtime_source:
        raise ValueError("runtime source lookup is disabled")
    history = session.source.history_lookup()
    if history is None:
        reason = session.source.history_unavailable_reason or "repository history is unavailable"
        session.ledger.append("runtime_source_history_unavailable", reason=reason)
        return "Source history unavailable: " + reason
    started = time.monotonic()
    hits = history.search(kind, path=path, start_line=start_line, end_line=end_line,
                          target_commit=target_commit, limit=limit)
    lines = [_source_history_line(session, hit) for hit in hits]
    session.ledger.append(
        "runtime_source_history_retrieved", kind=kind, path=path,
        start_line=start_line, end_line=end_line, target_commit=target_commit,
        deployed_commit=history.deployed_commit,
        evidence=[hit.digest for hit in hits], facts_shown=len(hits),
        latency_ms=round((time.monotonic() - started) * 1000, 1))
    return "\n".join(lines) or "No matching source history. Do not invent an answer."


def lookup_source_history_tool(session, turn: "_Turn", call: dict, name: str,
                               args: dict) -> "_Next":
    del name
    try:
        kind = args.get("kind")
        if not isinstance(kind, str):
            raise ValueError("kind is required")
        path = args.get("path", "")
        target = args.get("target_commit", "")
        if not isinstance(path, str) or len(path) > 500:
            raise ValueError("path must be a string of at most 500 characters")
        if not isinstance(target, str) or len(target) > 64:
            raise ValueError("target_commit must be a string of at most 64 characters")
        start = args.get("start_line", 0)
        end = args.get("end_line", 0)
        if (isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, int) or not isinstance(end, int)
                or start < 0 or end < 0):
            raise ValueError("line numbers must be positive integers")
        result = lookup_source_history(
            session, kind, path=path, start_line=start, end_line=end,
            target_commit=target)
    except ValueError as exc:
        return session._reply(turn, call, str(exc))
    if session._cancel_requested():
        return _Next(session._finish(
            turn.result, turn.started_at, "user_cancelled",
            "the user stopped this task after source history retrieval", steps=turn.step))
    try:
        receipt = session.browser.execute("observe", {})
        session._browser_attempts += 1
    except TransportError as exc:
        return _Next(session._finish(turn.result, turn.started_at,
                                  session._transport_terminal("observe"), str(exc),
                                  steps=turn.step))
    write = session._writes.write_outcome(receipt, turn.step)
    session.ledger.append("action_receipt", step=turn.step, tool="observe",
                       command_id=receipt.command_id, after_tool="lookup_source_history",
                       ok=receipt.ok, error_code=receipt.error_code,
                       write=write["state"], write_attribution="unattributed")
    reason, detail = session._writes.settle_write(write)
    if reason:
        return _Next(session._finish(turn.result, turn.started_at, reason, detail,
                                  steps=turn.step))
    if receipt.observation is not None:
        turn.obs = receipt.observation
    note = session._ground(turn.obs, receipt, write["state"], write["refused_requests"])
    return session._reply(
        turn, call, "SOURCE HISTORY (prior; current runtime facts win):\n" + result
        + "\n\n" + session._render(turn.obs, receipt, note, write))


# -- capability check ------------------------------------------------------------


def register_capability_evidence(session) -> str:
    """Search the whole installed catalog for this turn's goal and record what it found.

    Run once, before the loop, so a decline always has something to stand on and never
    has to be inferred from the turn simply stopping. The search covers the typed Skill
    catalog plus the non-typed surfaces this deployment actually enables, because a gap
    is a statement about the whole executable surface.
    """
    if not capability_check_available(session):
        return ""
    if session._capability_ref:
        return session._capability_ref
    # Only the surfaces that can make the change themselves. Page actions that write can,
    # so while they are enabled no gap may be claimed: the session might do it through the
    # page. A form handoff cannot -- it prepares a form and the user submits it, or does
    # not -- and it answers every goal alike, so counting it made every search non-empty
    # and the gap impossible in the one deployment that enables it.
    writing_actions = {"fill", "select", "submit", "click"} - set(session.disabled_actions)
    extra = [f"page:{name}" for name in sorted(writing_actions & BROWSER_ACTIONS)]
    allowed = session._approvals.mutations_enabled
    try:
        matches = session.business_tools.resolve_capability(
            session.task.prompt, allow_mutations=allowed, extra=tuple(extra))
        # The same search with changes allowed. What it finds and this one does not is
        # what this actor's delegation (or this deployment) withholds -- the difference
        # between a capability nobody installed and one nobody granted them.
        withheld = () if allowed else tuple(
            name for name in (
                tuple(session.business_tools.resolve_capability(
                    session.task.prompt, allow_mutations=True))
                + tuple(session.business_tools.withheld_capability(session.task.prompt)))
            if name not in matches)
    except (ValueError, TypeError) as exc:
        # A capability check that cannot run leaves the turn without the reference, so no
        # gap can be claimed. That is the safe direction: the model falls back to ordinary
        # work rather than declining on a search that never happened.
        session.ledger.append("capability_check_unavailable", reason=str(exc)[:200])
        return ""
    item = CapabilityEvidence(
        evidence_id="capability#1",
        origin=Origin(kind="capability", reference=f"capability-check:{session.run_id}",
                      observed_at=str(time.time()),
                      scope=session.task.supported_scope or session.arm),
        application=session._approvals.principal.application_id,
        application_version=session.contract.content_digest,
        actor=session._approvals.principal.actor_authority_digest,
        catalog_digest=session.business_tools.catalog_sha256,
        goal=session.task.prompt[:500],
        matches=tuple(matches), withheld=tuple(withheld))
    session.evidence.record(item)
    session._capability_ref = item.evidence_id
    session._capability_withheld = bool(item.withheld)
    session.ledger.append("capability_check", goal_chars=len(session.task.prompt),
                       catalog_digest=item.catalog_digest,
                       matches=list(item.matches), withheld=list(item.withheld))
    return item.evidence_id


def capability_check_available(session) -> bool:
    return (session.business_tools is not None and session._approvals.principal is not None
            and not session._capability_question)


def capability_check_feedback(session) -> str:
    ref = register_capability_evidence(session)
    if not ref:
        return ("CAPABILITY CHECK UNAVAILABLE: no search could run, so no capability gap "
                "can be claimed.")
    # The structured finish is what can carry the conclusion, and until now it opened only
    # after a business read -- the one thing a turn declining a change must not do first.
    session._answer_mode = True
    held = session.evidence.get(ref)
    if held.matches:
        return (f"CAPABILITY CHECK {ref}: this session can perform it with "
                + ", ".join(held.matches) + ". Load that Skill and use it; a "
                "capability_gap citing this check is refused.")
    return (f"CAPABILITY CHECK {ref}: nothing installed for this actor performs the change "
            "the user asked for. Finish now with outcome=unable and answer "
            '{"version": 1, "coverage": "undetermined", "conclusions": '
            f'[{{"kind": "capability_gap", "evidence_ref": "{ref}"}}]}}. '
            "Do not look the record up, do not goto it, do not answer with a related query, "
            "and do not substitute a different change.")


def active_capability_digest(session) -> str:
    """A digest of the business tools this turn could actually call.

    An unsupported-operation refusal is a claim about the toolset, so the toolset is what
    is recorded. Names alone, sorted: the digest answers "which capabilities were live",
    and a reader comparing two runs can see the set change without the ledger carrying
    the tool definitions themselves.
    """
    names = sorted(session.business_tools.tool_names) if session.business_tools is not None else []
    return digest({"business_tools": names})


def build_clarification(session, args: object, *, step: int) -> dict:
    if not isinstance(args, dict) or set(args) != {
            "question", "response_kind", "choice_ids"}:
        raise ValueError(
            "ask_user requires exactly question, response_kind, and choice_ids")
    question = args.get("question")
    kind = args.get("response_kind")
    choice_ids = args.get("choice_ids")
    if (not isinstance(question, str) or not question.strip()
            or len(question.strip()) > 500
            or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", question)):
        raise ValueError("question must be 1-500 visible characters")
    if kind not in {"free_text", "single_choice"}:
        raise ValueError("response_kind must be free_text or single_choice")
    if (not isinstance(choice_ids, list) or len(choice_ids) > CLARIFICATION_CHOICE_LIMIT
            or any(not isinstance(item, str) or item not in session._clarification_choices
                   for item in choice_ids)
            or len(set(choice_ids)) != len(choice_ids)):
        raise ValueError(
            "choice_ids must be distinct current business-tool clarification candidates")
    choices = [dict(session._clarification_choices[item]) for item in choice_ids]
    if kind == "single_choice" and len(choices) < 1:
        raise ValueError("single_choice requires at least one current choice")
    if kind == "free_text" and choices:
        raise ValueError("free_text does not accept choice_ids")
    if len({item["label"] for item in choices}) != len(choices):
        raise ValueError("current choices do not have distinct user-visible labels")
    prior = ((session.conversation_context or {}).get("pending_clarification") or {})
    task_id = str(prior.get("task_id") or "")
    if not re.fullmatch(r"task-[a-f0-9]{24}", task_id):
        task_id = "task-" + hashlib.sha256(session.run_id.encode("utf-8")).hexdigest()[:24]
    safe_question = redact_sensitive_assignments(question.strip())
    seed = json.dumps({"run_id": session.run_id, "step": step, "question": safe_question,
                       "kind": kind, "choices": choices},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": 1,
        "clarification_id": "clarify-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24],
        "task_id": task_id,
        "question": safe_question,
        "response_kind": kind,
        "choices": choices,
    }


def finish_with_clarification(session, result: RunResult, t0: float, *,
                               step: int, clarification: dict | None = None,
                               question: str = "",
                               choice_ids: list[str] | None = None,
                               deterministic: str = "") -> RunResult:
    if clarification is None:
        clarification = build_clarification(session, {
            "question": question,
            "response_kind": "single_choice",
            "choice_ids": list(choice_ids or ()),
        }, step=step)
    result.clarification = clarification
    result.agent_said = clarification["question"]
    session.ledger.append(
        "clarification_requested", step=step,
        clarification_id=clarification["clarification_id"],
        task_id=clarification["task_id"],
        response_kind=clarification["response_kind"],
        choice_count=len(clarification["choices"]),
        choice_ids=[item["choice_id"] for item in clarification["choices"]],
        question_digest=hashlib.sha256(
            clarification["question"].encode("utf-8")).hexdigest(),
        **({"deterministic": deterministic} if deterministic else {}),
    )
    return session._finish(result, t0, "needs_input", result.agent_said, steps=step)


def ask_user_tool(session, turn: "_Turn", call: dict, name: str, args: dict) -> "_Next":
    """One question back to the person, which ends the turn holding what it knows."""
    try:
        clarification = build_clarification(session, args, step=turn.step)
    except (TypeError, ValueError) as exc:
        session.ledger.append("clarification_refused", step=turn.step,
                           error_type=type(exc).__name__)
        turn.messages += [_assistant_call(call), _tool_result(
            call, "CLARIFICATION REFUSED: " + str(exc)[:500])]
        return _Next()
    return _Next(finish_with_clarification(
        session, turn.result, turn.started_at, step=turn.step, clarification=clarification))
