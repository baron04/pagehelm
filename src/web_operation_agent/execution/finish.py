"""Settlement of the model's terminal ``finish`` tool call."""

from ..answer import answer_protocol
from ..answer.answer_view import (capability_gap_answer, render_payload,
                                  unavailable_answer)
from ..answer.evidence import EvidenceUnknown
from .knowledge_tools import active_capability_digest
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
        if name == "finish" and self._answers.answer_mode:
            try:
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
                if (isinstance(args, dict) and args.get("done") is True
                        and self._answer_action_receipt is not None):
                    args = {"outcome": "acted"}
                elif isinstance(args, dict) and args.get("done") is False:
                    args = {"outcome": "unable"}
                if isinstance(args, dict) and args.get("outcome") == "acted":
                    if (set(args) - {"outcome", "answer"} or self._answer_action_receipt is None
                            or self._should_challenge_completion()):
                        raise ValueError("no successful action receipt supports this outcome")
                    verified = (answer_protocol.submit(self.evidence, {
                        "outcome": "answered", "answer": args["answer"]}) if "answer" in args else
                        {"conclusions": (), "coverage": "complete", "prose": "", "lowered_because": ()})
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
                # The message, not only the class. A run recorded three refusals as
                # "ValueError" and nothing else, which is enough to know the answer was
                # rejected and not enough to know why -- the reviewer reading it later
                # cannot tell a bad reference from an impossible coverage claim.
                self.ledger.append("answer_contract_refused", step=step,
                                   reason=type(exc).__name__, detail=str(exc)[:500],
                                   repair=self._answer_repairs)
                if self._refused_reads and not len(self.evidence):
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
                messages += [_assistant_call(call), _tool_result(
                    call, "ANSWER REFUSED: " + str(exc)[:500] +
                    ". Correct the evidence reference or perform a supported read/compute; "
                    "otherwise finish with outcome=unable. " + self._answers.render_action())]
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
