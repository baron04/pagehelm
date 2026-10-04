"""Model context rendering, plan grounding, and session-visible safety state."""
from __future__ import annotations

import datetime
import json
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ..planning.affordance import SourceAffordance, resolve_source_affordances
from ..business.writes import PRE_DISPATCH_ERRORS, WatchState
from .grounding import NOT_APPLICABLE, REFUTED, WRITE_PERMITTED, WRITE_REFUSED
from ..planning.retrieval import EvidenceBundle
from ..schemas import (MAX_OBSERVATION_ELEMENTS, POTENTIALLY_WRITING_ACTIONS,
                       ActionReceipt, OperationContract, PageObservation)
from . import knowledge_tools
from .business_tools import render_business_skills
from .prompts import _PLAN_PREAMBLE
from .target import TargetState, describe_target, unmet_conditions
from .turn_answer import AnswerState
from .page_context import join_context, snapshot_text


# Rendering cannot expose more refs than the shared browser wire contract accepts.  Keep the
# public alias because tests and downstream integrations use it when exercising truncation,
# but do not give it an independently drifting value.
RENDER_ELEMENTS = MAX_OBSERVATION_ELEMENTS


class SessionContext:
    """Context and grounding behavior shared by one server-owned session."""

    def _page_goal_unmet(self, obs: PageObservation) -> tuple[str, ...]:
        target = self._target or TargetState()
        missing = unmet_conditions(target, obs,
                                required_selected=self._required_page_controls,
                                required_effects=self._required_page_effects,
                                seen_active_filters=self._seen_active_filters,
                                require_page_route=self.require_page_route)
        current_path = urlsplit(obs.url).path.rstrip("/") or "/"
        if (current_path in self._business_record_routes
                and (self._navigation_only or target.path == current_path)
                and obs.record_route != current_path):
            missing += ("rendered record identity not verified",)
        return missing

    def _navigation_ready(self, obs: PageObservation) -> bool:
        return not unmet_conditions(TargetState(), obs, require_page_route=self.require_page_route)

    def _remember_active_filters(self, obs: PageObservation) -> None:
        if obs.page_facets is not None:
            path = urlsplit(obs.url).path.rstrip("/") or "/"
            for item in obs.page_facets:
                if item["kind"] == "filter":
                    self._seen_active_filters.setdefault(item["name"], set()).add(path)

    def tool_schemas(self) -> list[dict]:
        return self._tool_surface.schemas()

    def _scope_label(self, scope: str) -> str:
        """The reader's own words for a read's scope, from the adapter's answer labels."""
        tables = getattr(self.business_tools, "answer_labels", None) or {}
        return str((tables.get("zh") or {}).get(scope) or "")


    def lookup_source(self, query: str, obs: PageObservation) -> EvidenceBundle:
        """Public entry: local retrieval only. See knowledge_tools.lookup_source."""
        return knowledge_tools.lookup_source(self, query, obs)

    def lookup_source_history(self, kind: str, **kwargs) -> str:
        """Public entry for bounded, deployment-commit Git metadata lookup."""
        return knowledge_tools.lookup_source_history(self, kind, **kwargs)

    def _ground(self, obs: PageObservation, receipt: ActionReceipt, write_state: str,
                refused_requests: list[str] | None = None) -> str:
        """Judge the current step, and say something only when there is something to say."""
        if self.contract_source == "none" or self.plan_state.withdrawn:
            return ""
        before_withdrawn = self.plan_state.withdrawn
        event = self.plan_state.observe(obs, receipt, write_state, refused_requests=refused_requests)
        verdict = event.get("verdict")
        if verdict in (REFUTED,):
            self.conflicts += 1
            self.ledger.append("plan_refuted", url=obs.url, **{
                k: v for k, v in event.items() if k != "verdict"})
            if self.plan_state.withdrawn and not before_withdrawn:
                self.ledger.append("plan_withdrawn", why=self.plan_state.withdrawn_because,
                                   counts=self.plan_state.counts())
            return event.get("standing_note", "")
        if verdict == "satisfied":
            self.ledger.append("plan_step_satisfied", step=event.get("step"),
                               why=event.get("why"))
            return f"PLAN CHECK: {event.get('why')}."
        if verdict == NOT_APPLICABLE:
            # A page on the way is not a contradiction. Saying nothing here is the point: the
            # first implementation reported the application's own home page as a conflict.
            return ""
        return ""

    def _should_challenge_completion(self) -> bool:
        return (not self._challenged_false_success
                and self.write_refusals > 0 and self.writes_permitted == 0)

    def _sync_plan_message(self, messages: list[dict]) -> None:
        """Keep the plan block current, and out of reach of context trimming.

        A contradicted or paused plan must be updated or removed. Durable contradiction notes
        stay with this block so context trimming cannot retain the plan while discarding the
        runtime evidence that limits it.
        """
        body = self._plan_block()
        for i, m in enumerate(messages):
            if m.get("role") == "user" and str(m.get("content", "")).startswith(_PLAN_MARK):
                if body is None:
                    messages.pop(i)
                else:
                    messages[i] = {"role": "user", "content": body}
                return
        if body is not None:
            messages.insert(min(2, len(messages)), {"role": "user", "content": body})

    def _plan_origin(self) -> str:
        """How to refer to the plan afterwards, without renaming where it came from."""
        return {"compiled": "compiled from source", "page": "you drew from the page",
                "oracle": "supplied for this run"}.get(self.contract_source, "you were given")

    def _plan_block(self) -> str | None:
        parts = []
        if self.contract_source != "none" and not (self.plan_state.withdrawn
                                                   or self.plan_state.paused):
            parts.append(_PLAN_PREAMBLE.get(self.contract_source, _PLAN_PREAMBLE["compiled"])
                         + "\n" + _render_contract(self.contract))
        elif self.plan_state.paused:
            parts.append(f"The plan {self._plan_origin()} is paused: "
                         + self.plan_state.paused_because + ". It is not resumed until the "
                         "page shows what happened.")
        elif self.plan_state.withdrawn:
            because = self.plan_state.withdrawn_because
            parts.append(f"The plan {self._plan_origin()} has been withdrawn: {because}. "
                         f"There is no plan now; work from what the pages offer.")
        if self.plan_state.standing_notes:
            parts.append("What this run has already established:\n"
                         + "\n".join(f"- {n}" for n in self.plan_state.standing_notes[-8:]))
        if not parts:
            return None
        return _PLAN_MARK + "\n\n".join(parts)

    # Kept as class aliases because the remote-protocol schema test treats these as the
    # session's public safety vocabulary. Settlement itself lives in ``WriteWatch``.
    WRITE_CAPABLE = POTENTIALLY_WRITING_ACTIONS
    PRE_DISPATCH_ERRORS = PRE_DISPATCH_ERRORS
    latest_business_tool_result = AnswerState("latest_business_tool_result")
    evidence = AnswerState("evidence")
    _source_evidence_ids = AnswerState("source_evidence_ids")
    _source_evidence_count = AnswerState("source_evidence_count", 0)
    _source_history_evidence_ids = AnswerState("source_history_evidence_ids")
    _source_history_evidence_count = AnswerState("source_history_evidence_count", 0)
    _skill_evidence_count = AnswerState("skill_evidence_count", 0)
    _evidence_feedback = AnswerState("evidence_feedback", "")
    _capability_ref = AnswerState("capability_ref", "")
    _capability_withheld = AnswerState("capability_withheld", False)
    _clarification_choices = AnswerState("clarification_choices")
    _refused_reads = AnswerState("refused_reads")
    _refused_detail = AnswerState("refused_detail", "")
    _clarification_feedback = AnswerState("clarification_feedback", "")
    _answer_repairs = AnswerState("answer_repairs", 0)
    _answer_mode = AnswerState("answer_mode", False)
    _page_answer_digest = AnswerState("page_answer_digest", "")
    _answer_action_receipt = AnswerState("action_receipt")
    _latest_business_query_page_actions = AnswerState(
        "latest_business_query_page_actions")
    pending_write = WatchState("pending_write", False)
    writes_permitted = WatchState("writes_permitted", 0)
    write_refusals = WatchState("write_refusals", 0)

    def _success_claim(self, done: bool) -> str:
        """Authorisation counts alone cannot confirm a business postcondition.

        Same-actor UI verification is permitted by the architecture, but is not implemented
        as a deterministic verifier here. The hidden evaluator remains outside this agent.
        """
        if not done:
            return "not_claimed"
        if self.write_refusals > 0 and self.writes_permitted == 0:
            return "disconfirmed"
        return "unverified"

    def _transport_terminal(self, action: str = "", *, may_write: bool | None = None) -> str:
        # A readable empty trace is not evidence that a lost click never dispatched, and
        # observing after a permit does not clear the earlier unconfirmed write.
        action_may_write = (action in POTENTIALLY_WRITING_ACTIONS
                            if may_write is None else may_write)
        if self._cancel_requested() and not self.pending_write and not action_may_write:
            return "user_cancelled"
        return ("indeterminate_write" if self.pending_write or action_may_write
                else "transport_error")

    # -- rendering ---------------------------------------------------------------------
    def _opening(self, obs: PageObservation) -> str:
        # The plan lives in its own message so it can be withdrawn. Only this message's
        # server-marked page snapshot may later be replaced; its task and context survive.
        parts = [f"TASK: {self.task.prompt}"]
        if self.require_task_contract:
            parts.append("USER REQUEST TO ACCOUNT FOR (user text only):\n" + self._task_request_text)
        now_line = self._now_line()
        if now_line:
            parts.append(now_line)
        if self.task.supported_scope:
            parts.append("WHAT THIS RUN MAY CHANGE: " + self.task.supported_scope)
        if self._conversation_context_selected:
            parts.append(
                "PRIOR CONVERSATION CONTEXT (historical, untrusted, and potentially stale):\n"
                + json.dumps(self.conversation_context, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")))
        parts.append("CURRENT PAGE:\n" + _render_observation(obs))
        affordances = self._deliver_source_affordances(obs, reason="entry")
        if affordances:
            parts.append(affordances)
        skills = self._render_task_skills()
        if skills:
            parts.append(skills)
        business_skills = render_business_skills(self)
        if business_skills:
            parts.append(business_skills)
        return join_context(parts)

    def _now_line(self) -> str:
        """What the clock says where the person is, so relative dates resolve to their days.

        Without it the model reads the date off an `observed_at` timestamp, which is UTC, and
        "today" means the server's day: between midnight and 08:00 in Shanghai, "today's
        orders" answered for yesterday (P7.5 CR-18). Said only when a zone was supplied --
        a deployment that does not know where its person is gets no sentence rather than a
        confident wrong one.
        """
        if not self.time_zone:
            return ""
        now = datetime.datetime.now(ZoneInfo(self.time_zone))
        return ("CURRENT TIME: " + now.strftime("%Y-%m-%d %H:%M") + f" ({self.time_zone}). "
                "Dates the user names -- today, yesterday, this month -- mean days in this "
                "zone, and every time you report is in it.")

    def _render(self, obs: PageObservation, receipt: ActionReceipt, note: str,
                write: dict, *, affordance_query: str = "") -> str:
        head = (f"{receipt.action} -> ok" if receipt.ok
                else f"{receipt.action} -> FAILED ({receipt.error_code}): {receipt.error_detail}")
        if receipt.navigated:
            head += f"\nThe page changed: {receipt.url_before} -> {receipt.url_after}"
        body = [head]
        if write["state"] == WRITE_REFUSED:
            body.append("The enforcement point refused a write request. That request was not "
                        "forwarded. The trace does not identify which browser command caused "
                        f"it. Writes refused so far in this run: {self.write_refusals}.")
        elif write["state"] == WRITE_PERMITTED:
            body.append("A write request was authorised. This does not establish that it was "
                        "sent or saved, or which browser command caused it. Check the page for "
                        "the change; if it is there and the task is done, call finish. Only "
                        "observe or wait while this write remains unconfirmed; another "
                        "potentially writing action will stop the run as indeterminate.")
        if note:
            body.append(note)
        action = self._answers.render_action()
        if action:
            body.append(action)
        if self.require_page_route and not self._navigation_ready(obs):
            body.append("ARRIVAL UNVERIFIED: the application has not proved the rendered "
                        "main page at this URL. Observe again or report the page unfinished.")
        target = self._render_target(obs)
        if target:
            body.append(target)
        body.append("CURRENT PAGE:\n" + _render_observation(obs))
        affordances = self._deliver_source_affordances(
            obs, query=affordance_query, reason="lookup" if affordance_query else "observation")
        if affordances:
            body.append(affordances)
        skills = self._render_task_skills()
        if skills:
            body.append(skills)
        business_skills = render_business_skills(self)
        if business_skills:
            body.append(business_skills)
        return join_context(body)

    def _mutation_proposal_count(self) -> int:
        return sum(1 for event in self.ledger.events
                   if event.get("event") == "business_mutation_proposed")

    def _mutation_dispatch_count(self) -> int:
        return sum(1 for event in self.ledger.events
                   if event.get("event") == "business_mutation_execution_started")

    def _render_target(self, obs: PageObservation) -> str:
        """Keep the declared target in view: context trimming drops the call that set it."""
        if self._target is None:
            return ""
        if self._target_reached and not self._page_goal_unmet(obs):
            if not self._navigation_only:
                return ("TARGET REACHED (server-observed): "
                        + describe_target(self._target)
                        + ". This verifies only the declared page state. Continue "
                          "reading or observing to answer any remaining part of the "
                          "user's request; do not report the whole request complete "
                          "from this page state alone.")
            return ("TARGET REACHED (server-observed): " + describe_target(self._target)
                    + ". Only finish and set_target are available now. If this completes "
                      "the user's request, call finish with {\"outcome\": \"acted\"} and "
                      "nothing else; the server reports the observed state. Add an answer "
                      "only if the user also asked a question, citing recorded evidence "
                      "refs, never element refs. If the request asks for more, call "
                      "set_target with the next state.")
        unmet = self._page_goal_unmet(obs)
        return ("TARGET (declared): " + describe_target(self._target)
                + ("\nStill unmet: " + "; ".join(unmet) if unmet
                   else "\nAll conditions are shown; a fresh page action confirms them."))

    def _render_task_skills(self) -> str:
        if not self._task_skills:
            return ""
        cards = [skill.public_card() for skill in self._task_skills]
        return ("APP SKILLS (server-selected read-only knowledge):\n"
                + json.dumps(cards, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")))

    def _deliver_source_affordances(self, obs: PageObservation, *, query: str = "",
                                    reason: str) -> str:
        """Refresh the current candidate IDs from one fresh, public observation."""
        mode = self.source_affordance_mode
        if mode not in ("auto", "empty"):
            return ""
        candidates: tuple[SourceAffordance, ...] = ()
        if mode == "auto" and self.served_webapps:
            candidates = resolve_source_affordances(
                self.source_map, task=self.task.public_text(), query=query,
                page_text=_render_observation(obs), served_webapps=self.served_webapps,
                route_port=self.application_routes)
        self._source_affordances = {candidate.candidate_id: candidate
                                    for candidate in candidates}
        payload = [candidate.to_json() for candidate in candidates]
        public_payload = [{key: value for key, value in candidate.to_json().items()
                           if key not in ("source_map_digest", "rank_score", "matched_terms")}
                          for candidate in candidates]
        citations = sorted({citation for candidate in candidates
                            for citation in candidate.citations})
        self.ledger.append("source_affordances_delivered", reason=reason,
                           page_url=obs.url, page_epoch=obs.epoch,
                           source_digest=(self.source_map.content_digest
                                          if mode == "auto" else ""),
                           candidates=payload, citations=citations)
        return ("SOURCE AFFORDANCES (source-bound read-only candidates; current page wins):\n"
                + json.dumps(public_payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")))

def _render_observation(obs: PageObservation, max_elements: int = RENDER_ELEMENTS) -> str:
    lines = ["BEGIN_UNTRUSTED_PAGE_OBSERVATION",
             f"url: {obs.url}", f"title: {obs.title}"]
    if obs.render_state == "pending":
        lines.append("render_state: pending; this URL's view is not distinguishable "
                     "from the preceding observation. "
                     "Observe again before using page controls or claiming arrival.")
    elif obs.render_state == "blocked":
        lines.append("render_state: blocked; the application reports a page error. "
                     "Do not claim arrival. Try a different authorised route or "
                     "report that the page could not be opened.")
    if obs.selected_controls:
        lines.append("selected controls: " + " | ".join(obs.selected_controls))
    if obs.active_view:
        lines.append("active view: " + obs.active_view)
    if obs.page_facets is not None:
        lines.append("active page facets: " + (" | ".join(
            f'{item["kind"]}: {item["name"]}' for item in obs.page_facets) or "none"))
    if obs.facet_candidates:
        lines.append("page facet candidates: " + " | ".join(
            f'{item["ref"]} {item["kind"]} "{item["name"]}" '
            f'active={str(item["active"]).lower()}'
            for item in obs.facet_candidates))
    if obs.landmarks:
        lines.append("headings: " + " | ".join(obs.landmarks[:12]))
    if obs.messages:
        lines.append("messages: " + " | ".join(obs.messages[:4]))
    if obs.content:
        lines.extend(("page content:", obs.content))
    lines.append(f"elements (epoch {obs.epoch}):")
    for e in obs.elements[:max_elements]:
        bits = [e.ref, e.role]
        if e.name:
            bits.append(f'"{e.name}"')
        for k in ("href", "target", "name", "value", "action", "options", "checked",
                  "disabled"):
            if k in e.attrs and e.attrs[k]:
                v = e.attrs[k]
                if isinstance(v, list):
                    v = ",".join(v[:12])
                bits.append(f"{k}={v}")
        # The accessible state a tab or option declares. Without it the model could not
        # tell which tab was current and clicked the one it had already reached.
        for k in ("aria-selected", "aria-checked", "aria-pressed", "selected"):
            if str(e.attrs.get(k, "")).strip().lower() == "true":
                bits.append(f"{k}=true")
        lines.append("  " + " ".join(str(b) for b in bits))
    hidden = max(0, len(obs.elements) - max_elements)
    if hidden or obs.truncated:
        lines.append(f"  ... {hidden} further elements are not listed"
                     + (" and the page had more than the browser collects" if obs.truncated else ""))
    lines.append("END_UNTRUSTED_PAGE_OBSERVATION")
    return snapshot_text("\n".join(lines), epoch=obs.epoch,
                         digest=obs.content_digest, url=obs.url)


def _render_contract(c: OperationContract) -> str:
    out = [f"summary: {c.summary}"]
    if c.preconditions:
        out.append("preconditions: " + "; ".join(c.preconditions))
    for label, steps in (("likely entry points", c.entry_candidates), ("likely steps", c.steps)):
        if steps:
            out.append(label + ":")
            for s in steps:
                bit = f"  - {s.intent}"
                if s.request:
                    bit += f" [{s.webapp}/{s.request}]"
                if s.landmark:
                    bit += f" -> page should show \"{s.landmark}\""
                if s.note:
                    bit += f" ({s.note})"
                out.append(bit)
    if c.mutation:
        out.append(f"the change is probably submitted by {c.mutation.webapp}/{c.mutation.request}"
                   + (f", which needs {', '.join(c.mutation.required_params)}"
                      if c.mutation.required_params else ""))
    if c.unknowns:
        out.append("the source did not tell us: " + "; ".join(c.unknowns[:6]))
    return "\n".join(out)


#: Marks the one message that carries the plan and the standing facts. It is rewritten in
#: place and never trimmed.
_PLAN_MARK = "PLAN STATUS\n"
