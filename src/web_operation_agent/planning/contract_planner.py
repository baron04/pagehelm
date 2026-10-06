"""Bounded model loop that produces a checked operation contract."""
from __future__ import annotations

from ..schemas import (SCHEMA_VERSIONS, OperationContract, PageObservation, SchemaError,
                       SourceMap)
from .retrieval import EvidenceBundle
from .contract_inputs import PLANNER_SYSTEM, PLANNER_SYSTEM_PAGE_ONLY, render_page_for_planner
from .contract_response import (PLANNER_MAX_GROWTHS, PLANNER_START_TOKENS,
                                _extract_json, split_reasoning)
from ..modeling.reasoning import ReasoningScenario, scenario_options
from .contract_validation import (PlanningFailed, normalise_contract, prune_contract,
                                  validate_contract)


class ContractPlanner:
    """One planner, used by both arms of the comparison.

    `smap` is the manipulated variable. With it, the planner is shown facts from the deployed
    release and its answer is checked against them; without it, the same loop plans from the
    task and the page alone. Budget growth, repair, schema, and everything downstream of the
    contract are one implementation, so a difference in results cannot come from a difference
    in machinery.
    """

    def __init__(self, chat, smap: SourceMap | None = None, *, segment: str = "",
                 start_tokens: int = PLANNER_START_TOKENS,
                 max_growths: int = PLANNER_MAX_GROWTHS):
        self.chat = chat
        self.segment = segment
        self.map = smap
        self.start_tokens = start_tokens
        self.max_growths = max_growths

    def plan(self, task_ref: str, prompt: str, bundle: EvidenceBundle, run_id: str,
             *, repairs: int = 1, page: PageObservation | None = None
             ) -> tuple[OperationContract, list[dict]]:
        """Returns the contract and the attempt log (kept for the ledger).

        `page` is where the agent is standing. Planning without it produced routes into
        screens the entry page does not lead to; the source says what exists, the page says
        what is in front of the agent, and a plan needs both.
        """
        has_evidence = bool(bundle.lines)
        user = [f"TASK ({task_ref}):\n{prompt}"]
        if page is not None:
            user.append("THE PAGE THE AGENT IS ON NOW:\n" + render_page_for_planner(page))
        if has_evidence:
            user.append(
                "FACTS FROM THE DEPLOYED SOURCE:\n"
                "BEGIN_UNTRUSTED_SOURCE_EVIDENCE\n" + bundle.render()
                + "\nEND_UNTRUSTED_SOURCE_EVIDENCE")
        messages = [
            {"role": "system",
             "content": PLANNER_SYSTEM if has_evidence else PLANNER_SYSTEM_PAGE_ONLY},
            {"role": "user", "content": "\n\n".join(user)},
        ]
        attempts: list[dict] = []
        budget, growths, repaired = self.start_tokens, 0, 0
        while True:
            # Full operation-contract synthesis is the explicit complex scenario.
            # Scope/goal declarations and ordinary browser decisions remain routine.
            think = scenario_options(ReasoningScenario.COMPLEX_PLAN)
            reply = self.chat.complete(messages, tools=None, run_id=run_id, max_tokens=budget,
                                       options=think)
            raw = reply.get("content") or ""
            thought, answer = split_reasoning(raw, reply.get("reasoning") or "")
            # Two provider-neutral ways of saying "the budget, not the answer, was the
            # problem": the endpoint reports it stopped on length, or a reasoning model spent
            # the whole allowance thinking and produced no answer at all.
            cut_off = (reply.get("finish_reason") or "").lower() == "length"
            starved = bool(thought) and not answer
            dropped: list[str] = []
            contract, problems = None, []

            if cut_off or starved:
                problems = ["the answer was cut off by the completion budget" if cut_off
                            else "the whole completion budget went on reasoning; "
                                 "no answer followed it"]
            else:
                try:
                    data = _extract_json(raw)
                    data.setdefault("schema_version", SCHEMA_VERSIONS["OperationContract"])
                    data.setdefault("task_ref", task_ref)
                    contract = normalise_contract(
                        OperationContract.from_json(data), segment=self.segment)
                    problems = validate_contract(contract, bundle, self.map)
                    if not problems and self.map is not None:
                        # Checking a plan against the deployment's descriptors is itself a use
                        # of source evidence, so it belongs to the arm that has it. Applying it
                        # to the page-only arm would quietly hand that arm the very thing the
                        # comparison is manipulating.
                        contract, dropped = prune_contract(
                            contract, self.map, segment=self.segment)
                        if not (contract.steps or contract.entry_candidates or contract.mutation):
                            problems = ["none of the moves in that contract are supported by "
                                        "the facts you cited for them"]
                except (PlanningFailed, SchemaError) as e:
                    contract, problems = None, [str(e)]

            attempts.append({"attempt": len(attempts) + 1, "problems": problems,
                             "dropped": dropped, "chars": len(raw),
                             # How the budget was spent, and whether the provider said it ran
                             # out. A reasoning model that thinks for the whole allowance and
                             # answers with nothing looks, from outside, like a malformed
                             # answer; these tell them apart without knowing the vendor.
                             "reasoning_chars": len(thought), "answer_chars": len(answer),
                             "finish_reason": reply.get("finish_reason") or "",
                             "max_tokens": budget,
                             "reasoning_scenario": ReasoningScenario.COMPLEX_PLAN.value,
                             "raw_head": (answer or raw)[:400] if problems else ""})

            if contract is not None and not problems:
                return contract, attempts

            if (cut_off or starved) and growths < self.max_growths:
                # Not a repair: the model was never given room to answer. Ask again with more,
                # and do not spend the repair allowance or feed its own reasoning back to it.
                growths += 1
                budget *= 2
                if starved:
                    messages = messages[:2] + [
                        {"role": "user",
                         "content": "Your previous response was entirely reasoning and "
                                    "contained no answer. Reason briefly, then output the "
                                    "JSON object and nothing else."}]
                continue

            if repaired >= repairs:
                raise PlanningFailed("; ".join(problems)[:600]
                                     + f" | last answer began: {(answer or raw)[:300]!r}")
            repaired += 1
            messages += [{"role": "assistant", "content": answer or "(no answer)"},
                         {"role": "user", "content":
                             "That contract was rejected:\n- " + "\n- ".join(problems) +
                             "\nReturn a corrected JSON object only."}]
        raise PlanningFailed("unreachable")
