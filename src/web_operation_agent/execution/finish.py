"""Settlement of the model's terminal ``finish`` tool call."""

from ..answer import answer_protocol
from ..answer.goal_scope import assess_goal_conclusions, normalize_goal_entries
from ..answer.answer_view import (capability_gap_answer, render_payload,
                                  unavailable_answer)
from ..answer.evidence import ComputationEvidence, EvidenceUnknown
from .knowledge_tools import active_capability_digest
from .page_evidence import capture_page_snapshot
from .prompts import _assistant_call, _tool_result
from .runstate import CAPABILITY_GAP_TERMINAL, _Next
from .request_contract import (action_unmet, check_answer_evidence, extract_answer_bindings,
                               missing_answers, incomplete_notes)


def _is_capability_gap(verified: dict | None) -> bool:
    """Whether a verified answer contains exactly one capability gap."""
    if not verified:
        return False
    conclusions = verified.get("conclusions") or ()
    return (len(conclusions) == 1
            and isinstance(conclusions[0], dict)
            and conclusions[0].get("kind") == "capability_gap")


def _ignore_page_prose(args: dict) -> tuple[dict, int]:
    """Discard model prose for page facts; the renderer uses cited page bytes only.

    This narrow repair does not relax source support checks or accept new evidence IDs.
    The original tool call stays in the audit. Other malformed fields still fail closed.
    """
    answer = args.get("answer")
    entries = answer.get("conclusions") if isinstance(answer, dict) else None
    if not isinstance(entries, list):
        return args, 0
    cleaned, count = [], 0
    for entry in entries:
        if isinstance(entry, dict) and entry.get("kind") == "page_observation":
            extra = {"statement", "support"} & set(entry)
            count += len(extra)
            cleaned.append({key: value for key, value in entry.items()
                            if key not in extra})
        else:
            cleaned.append(entry)
    if not count:
        return args, 0
    return {**args, "answer": {**answer, "conclusions": cleaned}}, count


class FinishHandler:
    """Session behavior for verified answers and plain completion claims."""

    def _unfinished_task(self, verified, missing, action_missing):
        """Preserve settled facts/actions and identify unmet user clauses explicitly."""
        requirements = self._task_requirements
        if requirements is None:
            return verified
        if verified is None:
            verified = {"conclusions": (), "coverage": "undetermined", "prose": "",
                        "lowered_because": ()}
        observed_action = self._answer_action_receipt or self._superseded_action_receipt
        if observed_action is not None:
            verified["action"] = observed_action
        verified["coverage"] = "partial"
        verified["unfinished_requests"] = incomplete_notes(
            requirements, missing, action_missing=action_missing,
            chinese=any("\u3400" <= char <= "\u9fff" for char in self.task.prompt))
        return verified

    def _refuse_unfinished_task(self, result, started_at, step, observation):
        requirements = self._task_requirements
        verified = self._unfinished_task(None, requirements.answer_ids,
            bool(action_unmet(requirements, self._answer_action_receipt, observation)))
        result.verified_answer = verified
        result.agent_said = render_payload(verified, user_text=self.task.prompt,
            max_chars=max(512, self.max_agent_said_chars))
        return self._finish(result, started_at, "agent_refused", result.agent_said, steps=step)

    def _tool_finish(self, turn: "_Turn", call: dict, name: str, args: dict) -> "_Next":
        """The turn's own end: a verified answer where one is required, or a plain claim.

        Both shapes live here -- the answer protocol's and the older done/why -- because a
        turn ends once, and which of the two applies is this session's decision, not the
        loop's.
        """
        result, t0, step = turn.result, turn.started_at, turn.step
        messages, obs = turn.messages, turn.obs
        successful_business_tools = turn.successful_business_tools
        kept_reply = turn.kept_reply
        structured_answer = isinstance(args, dict) and "outcome" in args
        typed_unmet = action_unmet(self._task_requirements, self._answer_action_receipt, obs)
        if (self._task_requirements is not None and typed_unmet
                and isinstance(args, dict)
                and (args.get("outcome") in {"acted", "answered"} or args.get("done") is True)):
            self.ledger.append("task_action_claim_refused", step=step, unmet=list(typed_unmet))
            return self._reply(turn, call, "The frozen operation is unfinished: "
                               + "; ".join(typed_unmet)
                               + ". Continue toward the requested result or report partial/unable. "
                                 "Do not replace a record with a collection. If this was a question "
                                 "misclassified as an operation, correct declare_task before a "
                                 "verified page effect; data/source reads do not freeze that mistake.")
        claims_page_action = (isinstance(args, dict)
                              and (args.get("outcome") == "acted"
                                   or (self._task_requirements is not None
                                       and self._task_requirements.action_request
                                       and args.get("outcome") == "answered")
                                   or (args.get("done") is True
                                       and (self._answer_action_receipt is not None
                                            or self._target is not None)))
                              and (self._answer_action_receipt is None
                                   or self._answer_action_receipt["kind"] in {
                                       "target_state", "navigation", "page_interaction"}))
        if claims_page_action and (self._required_page_controls
                                   or self._required_page_effects or self.require_page_route):
            missing_page_state = self._page_goal_unmet(obs)
            if missing_page_state:
                self.ledger.append("page_presentation_claim_refused", step=step,
                                   unmet=list(missing_page_state))
                messages += [_assistant_call(call), _tool_result(
                    call, "The requested page presentation is not verified: "
                          + "; ".join(missing_page_state)
                          + ". Continue to the requested view or say it is unfinished.")]
                return self._resume(turn, messages, obs, successful_business_tools)
        if claims_page_action and self._target is not None:
            missing_target = self._page_goal_unmet(obs)
            if missing_target:
                self.ledger.append("page_target_claim_refused", step=step,
                                   unmet=list(missing_target))
                messages += [_assistant_call(call), _tool_result(
                    call, "The declared page target is not verified: "
                          + "; ".join(missing_target)
                          + ". Continue to the requested state or say it is unfinished.")]
                return self._resume(turn, messages, obs, successful_business_tools)
        if name == "finish" and (self._answers.answer_mode or structured_answer or self.require_task_contract):
            if isinstance(args, dict) and args.get("outcome") == "acted" and "answer" in args:
                self._answers.action_answer_required = True
            try:
                requirements = self._task_requirements
                if self.require_task_contract and requirements is None:
                    raise ValueError("declare every USER REQUEST clause before completion")
                bindings = []
                if requirements is not None:
                    args, bindings = extract_answer_bindings(args)
                if isinstance(args, dict):
                    args, ignored_page_prose = _ignore_page_prose(args)
                    if ignored_page_prose:
                        self.ledger.append("page_answer_prose_ignored", step=step,
                                           fields=ignored_page_prose)
                    if "outcome" in args and ("done" in args or "why" in args):
                        # Existing scripted callers can still send both the old
                        # done/why fields and the structured outcome. The structured
                        # branch wins; plain prose cannot become a visible claim.
                        self.ledger.append("plain_finish_fields_ignored", step=step)
                        args = {key: value for key, value in args.items()
                                if key not in {"done", "why"}}
                answer = args.get("answer") if isinstance(args, dict) else None
                conclusions = (answer.get("conclusions")
                               if isinstance(answer, dict) else ())
                if (self._answers.answer_goals and isinstance(answer, dict)):
                    normalized, removed = normalize_goal_entries(
                        self.evidence, conclusions, self._answers.answer_goals,
                        self._answers.query_tools,
                        frozenset(self._answers.generated_goal_metric_refs),
                        complete=answer.get("coverage") == "complete")
                    if removed:
                        self.ledger.append("unrequested_answer_facts_omitted",
                                           step=step, refs=list(removed))
                    if removed and not normalized:
                        raise ValueError(
                            "only unrequested collection facts were selected; cite a "
                            "metric from the read goal frozen before the first result")
                    args = {**args, "answer": {**answer, "conclusions": normalized}}
                    answer = args["answer"]
                    conclusions = normalized
                missing = (missing_answers(requirements, bindings, conclusions)
                           if requirements is not None else ())
                if requirements is not None:
                    check_answer_evidence(requirements, bindings, self.evidence)
                action_missing = bool(typed_unmet)
                independent_partial_gap = bool(requirements is not None
                    and requirements.action_request and requirements.answer_requests
                    and args.get("outcome") == "partial"
                    and any(isinstance(entry, dict) and entry.get("kind") == "capability_gap"
                            for entry in conclusions or ()))
                if requirements is not None and (
                        args.get("outcome") in {"acted", "answered"}
                        or (args.get("outcome") not in {"partial", "unable"}
                            and isinstance(answer, dict) and answer.get("coverage") == "complete")
                        or args.get("done") is True) and (missing or action_missing):
                    raise ValueError("incomplete USER REQUEST: " + "; ".join(incomplete_notes(
                        requirements, missing, action_missing=action_missing,
                        chinese=False)) + ". Read the missing facts, complete the operation, "
                        "or report partial/unable; a page receipt cannot replace an answer")
                claims_read_only_result = (
                    isinstance(conclusions, list)
                    and any(isinstance(item, dict) for item in conclusions))
                if (self._explicit_mutation_intent and claims_read_only_result
                        and self._approvals.business_counts["mutation_completed"] == 0
                        and isinstance(args, dict)
                        and args.get("outcome") in ("answered", "partial")
                        and not independent_partial_gap):
                    raise ValueError(
                        "a read-only result is only a mutation preflight for this explicit "
                        "write request; call the mutation tool so its authoritative "
                        "pre-dispatch checks can accept or refuse it, ask for corrected "
                        "input, or finish with outcome=unable")
                # Existing scripted navigation callers still use done/why. Never render
                # their free prose once evidence mode is active; use the actual receipt.
                if (isinstance(args, dict) and "outcome" not in args
                        and args.get("done") is True
                        and self._answer_action_receipt is not None):
                    args = {"outcome": "acted"}
                elif (isinstance(args, dict) and "outcome" not in args
                      and args.get("done") is False):
                    args = {"outcome": "unable"}
                if isinstance(args, dict) and args.get("outcome") == "acted":
                    stray = sorted(set(args) - {"outcome", "answer"})
                    if stray:
                        # A model that adds `reason` or `why` beside a structured outcome
                        # is not making a claim the server could render: the fields are
                        # ignored, as done/why are above. Refusing the whole call here
                        # cost one paid turn three repairs for a field the refusal never
                        # named.
                        self.ledger.append("acted_stray_fields_ignored", step=step,
                                           fields=stray)
                        args = {key: value for key, value in args.items()
                                if key in {"outcome", "answer"}}
                    if (self._answer_action_receipt is None
                            or self._should_challenge_completion()):
                        raise ValueError("no successful action receipt supports this outcome")
                    if "answer" not in args and (self._answers.action_answer_required
                                                or self._answers.answer_goals):
                        raise ValueError("the observed action still needs its requested answer; "
                                         "do not erase it during repair. Read and cite valid "
                                         "evidence, or report partial/unable")
                    if "answer" in args:
                        # A reached page verifies the action, not its accompanying
                        # facts. Never silently discard an invalid supplied answer.
                        verified = answer_protocol.submit(self.evidence, {
                            "outcome": "answered", "answer": args["answer"]})
                    else:
                        verified = {"conclusions": (), "coverage": "complete", "prose": "",
                                    "lowered_because": ()}
                    verified["action"] = self._answer_action_receipt
                else:
                    ignored: list[str] = []
                    verified = answer_protocol.submit(self.evidence, args, ignored=ignored,
                        allow_partial_capability_gap=independent_partial_gap)
                    if ignored:
                        self.ledger.append("answer_fields_ignored", step=step,
                                           fields=ignored[:12])
                if (verified is not None and self._answers.answer_goals
                        and isinstance(args, dict)
                        and isinstance(args.get("answer"), dict)):
                    findings = assess_goal_conclusions(
                        self.evidence, args["answer"].get("conclusions"),
                        self._answers.answer_goals, self._answers.query_tools)
                    wrong = tuple(item for item in findings
                                  if item.startswith(("unrequested ", "inconsistent ")))
                    missing = tuple(item for item in findings
                                    if item.startswith("missing "))
                    if wrong or (verified["coverage"] == "complete" and missing):
                        raise ValueError(
                            "the answer does not cover the read collections and metrics "
                            "frozen before the first business result: "
                            + "; ".join((*wrong, *missing))
                            + ". Use an exact result from the declared collection, "
                            "or report partial/unable; another collection cannot replace it")
                if (verified is not None and self.business_tools is not None
                        and self.business_tools.numeric_goal_binding
                        and self._answers.first_business_read_seen
                        and not self._answers.answer_goals
                        and isinstance(args, dict)
                        and isinstance(args.get("answer"), dict)
                        and args["answer"].get("coverage") == "complete"):
                    for entry in args["answer"].get("conclusions") or ():
                        if not isinstance(entry, dict) or entry.get("kind") != "aggregate":
                            continue
                        item = self.evidence.get(entry.get("evidence_ref", ""))
                        if (isinstance(item, ComputationEvidence)
                                and (item.operation == "count()"
                                     or item.operation.startswith("sum(")
                                     or item.operation.startswith("sum of "))):
                            raise ValueError(
                                "a complete count or sum answer needs the requested "
                                "collection frozen on the first business read; an empty "
                                "answer_goals declaration cannot be upgraded after results")
                if verified is not None and self._refused_reads and any(
                        item["kind"] == "page_observation"
                        for item in verified["conclusions"]):
                    raise ValueError(
                        "a page fact cannot answer this turn: an authorization refusal "
                        "closed the page as a source. Use the typed reads you have, or "
                        "finish with outcome=unable")
                if (verified is not None and self._answers.action_answer_required
                        and self._answer_action_receipt is not None):
                    verified["action"] = self._answer_action_receipt
                if requirements is not None:
                    if (missing or action_missing or verified is None
                            or verified["coverage"] != "complete"
                            or args.get("outcome") in {"partial", "unable"}):
                        verified = self._unfinished_task(verified, missing, action_missing)
                    elif self._answer_action_receipt is not None:
                        verified["action"] = self._answer_action_receipt
            except (ValueError, TypeError, EvidenceUnknown) as exc:
                self._answer_repairs += 1
                if self._answer_action_receipt is not None:
                    self._finalizing = True
                # The message, not only the class. A run recorded three refusals as
                # "ValueError" and nothing else, which is enough to know the answer was
                # rejected and not enough to know why -- the reviewer reading it later
                # cannot tell a bad reference from an impossible coverage claim.
                self.ledger.append("answer_contract_refused", step=step,
                                   reason=type(exc).__name__, detail=str(exc)[:500],
                                   repair=self._answer_repairs)
                if (self._refused_reads
                        and not self.evidence.has_application_evidence()):
                    # An authorization refusal and nothing read: there is no answer to
                    # repair, and what the person has to hear is which operation their
                    # account was refused. Repairing here would spend model calls to
                    # reach the same end.
                    result.agent_said = unavailable_answer(self.task.prompt)
                    return _Next(self._finish(result, t0, "permission_denied",
                                        self._refused_detail, steps=step))
                if self._answer_repairs > answer_protocol.MAX_ANSWER_REPAIRS:
                    if self._task_requirements is not None:
                        return _Next(self._refuse_unfinished_task(result, t0, step, obs))
                    result.agent_said = unavailable_answer(self.task.prompt)
                    return _Next(self._finish(result, t0, "agent_refused", result.agent_said,
                                        steps=step))
                refs = self.evidence.ids()
                ref_hint = (
                    " Current answer evidence refs: " + ", ".join(refs[:24]) + "."
                    if refs else " No answer evidence has been recorded yet.")
                # Navigation does not expire typed reads. Restore their exact
                # kinds and values when a repair cites a page/element placeholder,
                # instead of directing a mixed task to read the page again.
                held = tuple(ref for ref in refs
                    if isinstance(self.evidence.get(ref), ComputationEvidence))[-8:]
                held_hint = ("\nHELD AGGREGATE EVIDENCE (already computed; no new read "
                             "or compute is needed if its scope answers the request):\n"
                             + answer_protocol.evidence_index(self.evidence, held)
                             if held else "")
                if self._task_requirements is not None:
                    # Name the exact intersection instead of listing all read
                    # fields again: query children are not selected conclusions,
                    # and capability gaps cannot settle an independent question.
                    entries = ((args.get("answer") or {}).get("conclusions")
                               if isinstance(args, dict) and
                                  isinstance(args.get("answer"), dict) else ())
                    selected = tuple(dict.fromkeys(entry["evidence_ref"]
                        for entry in entries or () if isinstance(entry, dict)
                        and isinstance(entry.get("evidence_ref"), str)
                        and entry.get("kind") not in {"capability_gap", "undetermined"}))
                    ref_hint = (" answers_for evidence_refs must contain 1–12 refs selected "
                        "by the non-gap conclusions of this answer, not their child "
                        "fields or the capability check. Eligible selected refs: "
                        + (", ".join(selected[:24]) or "none") + ".")
                page_hint = ""
                entries = ((args.get("answer") or {}).get("conclusions")
                           if isinstance(args, dict) and
                              isinstance(args.get("answer"), dict) else ())
                attempted_page_fact = (isinstance(entries, list) and any(
                    isinstance(entry, dict) and entry.get("kind") == "page_observation"
                    for entry in entries))
                if (self._finalizing and attempted_page_fact and not held
                        and not self._refused_reads and
                        not any(ref.startswith("obs") for ref in refs)):
                    try:
                        _, page_hint = capture_page_snapshot(self, obs, step=step)
                    except (ValueError, TypeError, EvidenceUnknown):
                        page_hint = ""
                repair = (" Element refs such as e1 are browser controls, not answer "
                          "evidence. The action has already been observed. During answer "
                          "repair use observe with answer_evidence=true, a registered typed "
                          "read, or compute for missing facts. Use page_observation for "
                          "page refs, not field_value or selection. Preserve the action "
                          "and all requested answers; report partial/unable if unresolved. "
                          if self._finalizing else
                          " Element refs such as e1 are browser controls, not answer evidence. "
                          "For a page fact, call observe with answer_evidence=true and cite one "
                          "of the returned obs-page refs exactly. Otherwise finish with "
                          "outcome=unable. ")
                messages += [_assistant_call(call), _tool_result(
                    call, "ANSWER REFUSED: " + str(exc)[:500] + "." + ref_hint +
                    repair + held_hint + self._answers.render_action() + page_hint)]
                return self._resume(turn, messages, obs, successful_business_tools)
            verified = self._answers.with_refused_reads(self._answers.with_labels(verified))
            result.verified_answer = verified
            if _is_capability_gap(verified) and not verified.get("unfinished_requests"):
                # The claim is that nothing ran, so it is recorded with the two counts
                # that make that checkable rather than taken on the model's word. The
                # sentence is fixed here; a model free to phrase it is free to soften it
                # back into "here is what I found instead".
                result.agent_said = capability_gap_answer(
                    self.task.prompt, withheld=self._capability_withheld)
                self.ledger.append(
                    "capability_gap", step=step,
                    capability_digest=active_capability_digest(self),
                    mutation_proposals=self._mutation_proposal_count(),
                    dispatches=self._mutation_dispatch_count())
                return _Next(self._finish(result, t0, CAPABILITY_GAP_TERMINAL,
                                    result.agent_said, steps=step))
            result.agent_said = (render_payload(verified, user_text=self.task.prompt,
                                               max_chars=max(512, self.max_agent_said_chars))
                                 if verified is not None else unavailable_answer(self.task.prompt))
            self.ledger.append("answer_verified", step=step,
                               conclusions=len(verified["conclusions"]) if verified else 0,
                               coverage=verified["coverage"] if verified else "undetermined")
            complete = verified is not None and (self._task_requirements is None
                                                or verified["coverage"] == "complete")
            return _Next(self._finish(result, t0, "agent_finished" if complete else "agent_refused",
                                result.agent_said, steps=step))

        if name == "finish":
            # Providers do not always enforce the advertised required tool fields. An
            # omitted or string-valued `done` is neither consent to claim completion
            # nor an explicit decision to give up. Return a repairable tool error.
            if not isinstance(args.get("done"), bool):
                self.ledger.append("finish_arguments_refused", step=step,
                                   reason="done_must_be_boolean")
                messages += [_assistant_call(call), _tool_result(
                    call, "finish requires done=true or done=false as a boolean. "
                    "No completion or refusal was recorded. Call finish again with "
                    "an explicit done value and a short why.")]
                return self._resume(turn, messages, obs, successful_business_tools)
            done = args["done"]
            if done and self.page_actions and (self._required_page_controls
                                                or self._required_page_effects or self.require_page_route):
                missing_page_state = self._page_goal_unmet(obs)
                if missing_page_state:
                    self.ledger.append("page_presentation_claim_refused", step=step,
                                       unmet=list(missing_page_state))
                    messages += [_assistant_call(call), _tool_result(
                        call, "The requested page presentation is not verified: "
                              + "; ".join(missing_page_state)
                              + ". Continue to the requested view or say it is unfinished.")]
                    return self._resume(turn, messages, obs, successful_business_tools)
            if done and self.evidence.has_source_evidence():
                if self._answer_action_receipt is None or (
                        self._explicit_mutation_intent
                        and self._answer_action_receipt["kind"] != "mutation"
                        and self._approvals.business_counts["mutation_completed"] == 0):
                    self._answer_repairs += 1
                    self._finalizing = True
                    self.ledger.append("answer_contract_refused", step=step,
                                       reason="source_plain_completion_without_action",
                                       repair=self._answer_repairs)
                    if self._answer_repairs > answer_protocol.MAX_ANSWER_REPAIRS:
                        result.agent_said = unavailable_answer(self.task.prompt)
                        return _Next(self._finish(result, t0, "agent_refused",
                                            result.agent_said, steps=step))
                    messages += [_assistant_call(call), _tool_result(
                        call, "A source fact is not an action receipt. Submit an "
                              "evidence-backed answer with outcome/answer, or finish "
                              "with outcome=unable. Do not dispatch another action "
                              "merely to repair this answer.")]
                    return self._resume(turn, messages, obs, successful_business_tools)
            if done and kept_reply:
                result.agent_said = kept_reply[:self.max_agent_said_chars]
                self.ledger.append("plain_reply_kept", step=step,
                                   why=str(args.get("why") or "")[:300])
            else:
                result.agent_said = str(args.get("why") or "")[:self.max_agent_said_chars]
            if done and self._should_challenge_completion():
                # The agent believes it is finished, and the application refused the only
                # write it attempted. Ending here would record a claim the control plane
                # contradicts. It is told once, in terms that reveal nothing about what
                # the right request would have been, and gets to carry on.
                self._challenged_false_success = True
                self.ledger.append("false_completion_challenged", step=step,
                                   said=result.agent_said[:200])
                messages += [_assistant_call(call), _tool_result(
                    call, "The enforcement point refused the observed write requests. "
                          "Those requests cannot establish that the task was saved. "
                          "Continue, or call finish with done=false if you cannot "
                          "complete it.")]
                return self._resume(turn, messages, obs, successful_business_tools)
            reached_target = (self._answer_action_receipt is not None
                              and self._answer_action_receipt["kind"] == "target_state")
            if done and (self.evidence.has_source_evidence() or reached_target):
                # The source informed a decision, or the server observed the declared
                # page state; either way the user sees only the action the runtime
                # actually observed. Model prose and source bytes do not become a claim
                # about the business goal or a new permission.
                verified = {"conclusions": (), "coverage": "complete", "prose": "",
                            "lowered_because": (), "action": self._answer_action_receipt}
                result.verified_answer = verified
                result.agent_said = render_payload(
                    verified, user_text=self.task.prompt,
                    max_chars=max(512, self.max_agent_said_chars))
            if done and self._answers.business_answer_is_projectable(self.page_actions):
                result.business_answer_projected = True
                self.ledger.append(
                    "business_answer", step=step, said=result.agent_said[:300],
                    successful_business_tools=successful_business_tools,
                    success_claim="unverified", terminal_tool="finish")
            # `agent_finished` records only that the agent stopped and claimed completion.
            # Runtime enforcement evidence is tracked separately, and a repeated claim
            # after a contradiction still cannot become a confirmed success by itself.
            result.success_claim = self._success_claim(done)
            result.challenged_false_success = self._challenged_false_success
            self.ledger.append("success_claim", step=step, claim=result.success_claim,
                               challenged=self._challenged_false_success)
            return _Next(self._finish(result, t0,
                                "agent_finished" if done else "agent_refused",
                                result.agent_said, steps=step))
