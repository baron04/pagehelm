"""Settlement of the model's terminal ``finish`` tool call."""

from ..answer import answer_protocol
from ..answer.answer_view import (capability_gap_answer, render_payload,
                                  unavailable_answer)
from ..answer.evidence import EvidenceUnknown
from .knowledge_tools import active_capability_digest
from .page_evidence import capture_page_snapshot
from .prompts import _assistant_call, _tool_result
from .runstate import CAPABILITY_GAP_TERMINAL, _Next


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
        if name == "finish" and (self._answers.answer_mode or structured_answer):
            try:
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
                claims_read_only_result = (
                    isinstance(conclusions, list)
                    and any(isinstance(item, dict) for item in conclusions))
                if (self._explicit_mutation_intent and claims_read_only_result
                        and self._approvals.business_counts["mutation_completed"] == 0
                        and isinstance(args, dict)
                        and args.get("outcome") in ("answered", "partial")):
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
                    if "answer" in args:
                        try:
                            verified = answer_protocol.submit(self.evidence, {
                                "outcome": "answered", "answer": args["answer"]})
                        except (ValueError, TypeError, EvidenceUnknown) as exc:
                            if not self._target_reached:
                                raise
                            # At a target checkpoint the action is server-verified and the
                            # answer is optional. An answer that cites what the turn never
                            # recorded is dropped rather than sent back for repair; the
                            # person reads the observed state and nothing the model wrote.
                            self.ledger.append("acted_answer_dropped", step=step,
                                               reason=type(exc).__name__,
                                               detail=str(exc)[:300])
                            verified = {"conclusions": (), "coverage": "complete",
                                        "prose": "", "lowered_because": ()}
                    else:
                        verified = {"conclusions": (), "coverage": "complete", "prose": "",
                                    "lowered_because": ()}
                    verified["action"] = self._answer_action_receipt
                else:
                    verified = answer_protocol.submit(self.evidence, args)
                if self._refused_reads and any(
                        item["kind"] == "page_observation"
                        for item in verified["conclusions"]):
                    raise ValueError(
                        "a page fact cannot answer this turn: an authorization refusal "
                        "closed the page as a source. Use the typed reads you have, or "
                        "finish with outcome=unable")
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
                    result.agent_said = unavailable_answer(self.task.prompt)
                    return _Next(self._finish(result, t0, "agent_refused", result.agent_said,
                                        steps=step))
                refs = self.evidence.ids()
                ref_hint = (
                    " Current answer evidence refs: " + ", ".join(refs[:24]) + "."
                    if refs else " No answer evidence has been recorded yet.")
                page_hint = ""
                entries = ((args.get("answer") or {}).get("conclusions")
                           if isinstance(args, dict) and
                              isinstance(args.get("answer"), dict) else ())
                attempted_page_fact = (isinstance(entries, list) and any(
                    isinstance(entry, dict) and entry.get("kind") == "page_observation"
                    for entry in entries))
                if (self._finalizing and attempted_page_fact
                        and not self._refused_reads and
                        not any(ref.startswith("obs") for ref in refs)):
                    try:
                        _, page_hint = capture_page_snapshot(self, obs, step=step)
                    except (ValueError, TypeError, EvidenceUnknown):
                        page_hint = ""
                repair = (" Element refs such as e1 are browser controls, not answer "
                          "evidence. The action has already been observed. Only finish is available "
                          "during answer repair; use an existing evidence ref, report the "
                          "action without an answer, or finish with outcome=unable. "
                          if self._finalizing else
                          " Element refs such as e1 are browser controls, not answer evidence. "
                          "For a page fact, call observe with answer_evidence=true and cite one "
                          "of the returned obs-page refs exactly. Otherwise finish with "
                          "outcome=unable. ")
                messages += [_assistant_call(call), _tool_result(
                    call, "ANSWER REFUSED: " + str(exc)[:500] + "." + ref_hint +
                    repair + self._answers.render_action() + page_hint)]
                return self._resume(turn, messages, obs, successful_business_tools)
            verified = self._answers.with_refused_reads(self._answers.with_labels(verified))
            result.verified_answer = verified
            if _is_capability_gap(verified):
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
            return _Next(self._finish(result, t0, "agent_finished" if verified else "agent_refused",
                                result.agent_said, steps=step))

        if name == "finish":
            done = bool(args.get("done"))
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
