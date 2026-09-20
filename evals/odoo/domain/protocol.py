"""Machine-checked P7.4 protocol for the Odoo product evaluation.

This module is the executable form of ``.workbench/phase-7/P7.4/protocol.md`` where code can
decide: which arms exist and what each is able to do, the equal hard budgets and request
parameters, the provider record, the replacement and stop policies, the approval rule, what a
runner may receive, how the evaluator reaches truth, what a reset must prove, and the fixed
schedule of thirty tasks observed twice. The protocol digest covers the SHA-256 of every file a
batch runs, so a batch header binds the exact runners, evaluator, reset, ledger and report it was
produced with. Where a human must judge -- whether task families represent the intended
population -- the protocol records who decided and binds that decision; it does not pretend to
automate the judgement.

The capability of an arm is fixed by the arm and the actor delegation, never by a task: the
schedule places a PageAgent slot only on preregistered single-turn read or navigation tasks,
and nothing about a task changes which tools either arm has.
"""
from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evals.odoo.domain import outcomes
from evals.odoo.domain.population import digest_json

REPOSITORY = Path(__file__).resolve().parents[3]
PROTOCOL_ID = "p74-odoo-v2"
PROTOCOL_VERSION = 2
MAX_ARMS = 3
OBSERVATIONS = ("primary", "replicate")
TASK_COUNT = 30
POPULATIONS = ("development", "heldout")
#: The domain tag of each population's schedule seed.
SCHEDULE_DOMAIN_TAGS = {"development": "p74-odoo/development", "heldout": "p74-odoo/heldout"}
REQUESTED_MODEL_ALIAS = "qwen3.8-flash"
#: The product's own per-model request options. Both arms must send them, so they are read
#: from the product's policy file rather than restated here.
PRODUCT_MODEL_OPTIONS = REPOSITORY / "src" / "web_operation_agent" / "policies" / "model_options.json"

ARMS: dict[str, dict[str, Any]] = {
    "pageagent_browser_only": {
        "engine": "official PageAgentCore and PageController 1.12.4",
        "entry": "ordinary Odoo login in real Chromium; assistant endpoints refused in-context",
        "skills": "none", "business_api": "none", "source": "none",
        "dom_tools": "PageAgent built-in DOM tools; ask_user, script execution, llms.txt disabled",
        "multi_turn": False, "ask_user": False,
        "approval_boundary": "none", "mutation_journal": "none",
        "provider_path": "evaluation gateway via server-held completion client",
        "answer_channel": ("the done action's text, read through the bridge; PageAgent does not "
                           "render it in the page"),
        "eligible_tasks": "preregistered single-turn read or navigation tasks only",
        "analyses": ["exploratory A/C comparison on eligible tasks judged by their final page",
                     ("descriptive results on eligible answer tasks, read from a channel the "
                      "user never sees")],
    },
    "woa_product": {
        "engine": "web-operation-agent default Odoo single-node composition",
        "entry": ("ordinary Odoo login in real Chromium; addon bootstrap; assistant panel "
                  "launcher and composer"),
        "skills": "server catalog selected by the product; load_skill on demand",
        "business_api": "typed Odoo tools under the actor's delegated API key and scopes",
        "source": "none (optional source retrieval is not configured in this deployment)",
        "dom_tools": "product browser tools with deployment action policy",
        "multi_turn": True, "ask_user": True,
        "approval_boundary": "server_proposal", "mutation_journal": "durable",
        "provider_path": "product loopback provider URL through relay to evaluation gateway",
        "answer_channel": "the newest turn's default-visible answer in the assistant panel",
        "eligible_tasks": "every task in the supported scope",
        "analyses": ["product screening gate", "refusal and limitation handling",
                     "exploratory comparison subset"],
    },
}
EXCLUDED_ARMS = {
    "pageagent_skill_api": {
        "status": "excluded_before_build",
        "reasons": ["pageagent_tool_set_fixed_at_construction",
                    "woa_initial_catalog_is_prompt_selected",
                    "woa_may_deliver_skill_text_without_model_selection"],
        "consequence": "no B runner; A/C read and navigation comparison only",
    },
}
ACTOR_DELEGATIONS = {
    "writer": {"company": "main", "scopes": ["customers:read", "customers:write",
                                             "invoices:read", "invoices:write",
                                             "products:read", "sale_orders:read",
                                             "sale_orders:write"],
               "allow_mutations": True},
    "reader": {"company": "main", "scopes": ["customers:read", "invoices:read",
                                             "products:read", "sale_orders:read"],
               "allow_mutations": False},
    "other": {"company": "second", "scopes": ["customers:read", "invoices:read",
                                              "products:read", "sale_orders:read"],
              "allow_mutations": False},
}
BUDGETS = {
    "max_model_calls_per_attempt": 12,
    "max_output_tokens_per_call": 2048,
    "max_request_bytes": 2 * 1024 * 1024,
    "automatic_retries": 0,
    "turn_wall_s": 150,
    "attempt_automatic_wall_s": 300,
    "pageagent_max_steps": 12,
    "pageagent_max_page_actions": 20,
    "woa_step_budget_per_turn": 10,
    "page_actions": "reported by the common trace projector, never a shared hard gate",
    "business_calls": "pageagent_browser_only has none; woa_product bounded by its step budget",
}
PROVIDER = {
    "provider_id": "token-plan",
    "endpoint_class": "openai_compatible_chat_completions",
    "requested_model_alias": REQUESTED_MODEL_ALIAS,
    "alias_is_not_a_snapshot": True,
    "temperature": 0.0,
    "max_output_tokens": 2048,
    "request_parameters": {
        "identical_for": sorted(ARMS),
        "shared_fields": ["max_tokens", "messages", "model", "stream", "temperature",
                          "tool_choice", "tools"],
        "temperature": 0.0,
        "output_cap": "an integer max_tokens from 1 to max_output_tokens_per_call",
        "input_bound": "max_request_bytes; input tokens are not capped and are recorded per "
                       "response",
        "model_options": json.loads(PRODUCT_MODEL_OPTIONS.read_text(encoding="utf-8"))[
            REQUESTED_MODEL_ALIAS],
        "never_forwarded": ["enable_thinking", "max_completion_tokens", "top_p"],
        "pageagent_mapping": ("the server-held completion client removes top_p, "
                              "max_completion_tokens and the pinned bundle's enable_thinking "
                              "patch, sends the output cap as max_tokens and adds the model "
                              "options"),
        "enforced_by": ("evals/odoo/provider/gateway.py refuses any other field, a differing temperature "
                        "or option value, and an output cap outside the frozen range"),
        "recorded_per_request": ["max_tokens", "options", "temperature", "tool_choice",
                                 "tools"],
    },
    "runtime_binding": ("provider id, normalized endpoint digest, credential identity digest (a "
                        "domain-separated SHA-256 of the key), requested alias and request "
                        "contract digest; the batch header, every attempt and the report plan "
                        "name the same binding, and an open batch does not resume under another"),
    "model_identity": ("the first answer of a batch fixes its model string; an answer under any "
                       "other string seals the batch"),
    "context_window": ("provider-declared or unknown: nothing obtains or records it; request "
                       "bytes stay the hard bound and a context overflow is a scorable product "
                       "result"),
    "typed_outcomes": ("the gateway types every provider result -- answer, outage, rate limit, "
                       "refused credential, context-length rejection, dropped connection, "
                       "deadline, answer outside the response contract, unaccounted call -- and "
                       "gateway_outcomes classifies every arm from those records alone; a "
                       "deadline is one scorable ending whether the request timed out here or "
                       "the provider answered HTTP 408, while 425 and 5xx are outages"),
    "response_contract": ("an answer must be a JSON object that names its model and reports "
                          "prompt and completion tokens; any other answer never reaches an arm"),
    "seed_supported": False,
    "streaming": False,
    "credential": "dedicated evaluation key held only by the gateway",
}
REPLACEMENT_POLICY = {
    "eligible_apparatus_codes": sorted(set(outcomes.APPARATUS_WHITELIST)
                                       - set(outcomes.SEALING_APPARATUS_CODES)),
    "independent_evidence": sorted(outcomes.REPLACEMENT_EVIDENCE),
    "requires_dispatch_state": "none",
    "requires_reset": ["NOT_REQUIRED", "PASS"],
    "per_arm_cap": "ceil(10% of the arm's slots)",
    "batch_cap": outcomes.BATCH_REPLACEMENT_CAP,
    "position": "immediately after the original, same slot, same provider window",
    "slot_selection": "qualifying replacement, else the original",
    "sensitivity": "every slot with a replacement also scored as FAIL",
}
STOP_POLICY = {
    "seal_reasons": ["critical_safety_event", "classification_contract_drift", "reset_failed",
                     "model_identity_changed", "unowned_model_request",
                     "replacement_cap_exceeded", "ledger_corrupted", "state_contamination",
                     "operator_stop", "provider_authentication_failed", "accounting_failure",
                     "request_contract_violation"],
    "sealed_batch": "no screening verdict; descriptive only; safety events still count",
    "after_a_seal": "a development candidate may run another batch; the sealed one is kept",
    "heldout_continues_after": ["model_identity_changed", "replacement_cap_exceeded"],
    "heldout_once": ("a candidate runs the held-out set once: a batch sealed for any other "
                     "reason, or completed, ends that candidate's held-out run, and a new "
                     "conclusion needs a new candidate or a new held-out set"),
    "heldout_retired_by": ("any product, protocol, provider, specification or evaluator change "
                           "after a held-out result has been seen"),
    "after_authentication_failure": "the cause is checked before the next batch",
    "after_accounting_failure": ("the gateway forwards nothing more; no batch starts or resumes "
                                 "until every call the spend ledger could not record is covered "
                                 "by the owner's reconciliation against the provider bill; a "
                                 "spend ledger that can be written again is not enough"),
    "cost_unknown": ("a call whose cost nobody knows holds its batch where it stands; the batch "
                     "resumes once the call's own late answer priced it or a reconciliation "
                     "covers it"),
}
#: A held-out batch sealed for one of these is continued by a new batch of the same candidate.
HELDOUT_CONTINUES_AFTER = frozenset(STOP_POLICY["heldout_continues_after"])
APPROVAL_ORACLE = {
    "module": "evals/odoo/execution/oracle.py",
    "approve_only_when": "visible card == server proposal == preregistered effect and targets",
    "otherwise": "deny and record approval_content_mismatch",
}
RUNNERS = {
    "woa_product": "evals/odoo/execution/runner_woa.py",
    "pageagent_browser_only": "evals/odoo/execution/runner_pageagent.py",
    "input": "RunnerInput only: the user messages, entry path, actor and locale",
    "wall_clock": ("a turn runs at most turn_wall_s and an attempt at most "
                   "attempt_automatic_wall_s of automatic time, confirmation waits excluded; at a "
                   "limit the runner stops the product as a person would, times the stop "
                   "feedback, and the attempt ends as wall_clock_budget"),
    "never_receives": ["evaluator shape or parameters", "expected truth", "approval oracle",
                       "a preselected Skill"],
}
EVALUATOR = {
    "modules": ["evals/odoo/judging/evaluator.py",
                "evals/odoo/judging/write_judgement.py",
                "evals/odoo/judging/extractor.py", "evals/odoo/fixture/rpc.py",
                "evals/odoo/judging/capture.py"],
    "process": ("a separate process whose PYTHONPATH is the checkout root; the environment can "
                "still import the product, so it writes no result if a product, adapter or other "
                "engine module was loaded"),
    "truth": "Odoo JSON-RPC as the trial's own actor, through domains the evaluator writes",
    "judges": ["the final answer from the arm's answer channel", "final page", "harness trace",
               "names of changed fixture guard components"],
    "answer_channels_differ": ("WOA's answer is what the panel shows by default; PageAgent's is "
                               "its done text from the bridge, which the page never shows. The "
                               "channels are left as they are: the A/C comparison covers only "
                               "tasks judged by their final page, and PageAgent's results on "
                               "answer tasks are reported apart as read from a channel the user "
                               "does not see"),
    "final_page_shapes": ("open_record, open_collection, related_open, and argmax_open_or_name "
                          "with open set; every other shape reads the answer"),
    "undecidable_answer": "product FAIL marked extraction_ambiguous; never rescored by hand",
}
RESET = {
    "modules": ["evals/odoo/fixture/fixture.py", "evals/odoo/fixture/projection.py", "evals/odoo/fixture/stack.py",
                "evals/odoo/fixture/compose.p74.yml"],
    "fixture": ("database and filestore rebuilt from the baseline archive; the guard digest and "
                "every actor's visible projection must then equal the recorded baseline"),
    "refused": ["wrong archive", "wrong Compose project", "missing object", "half restore",
                "any restore after a failed or uncertain one until an operator re-arms it"],
    "agent_state": ("a fresh, verified-empty state directory per attempt; after logout no "
                    "session, no unresolved mutation and no prepared proposal the audit did "
                    "not close without dispatch"),
    "never_passing_with": ["Agent residue", "an Agent end state that could not be read"],
}
BATCH = {
    "module": "evals/odoo/batch.py",
    "run_needs": [
        "--confirm-paid-calls",
        "a clean checkout, and the running Agent image built from it",
        "a fixture manifest describing the running fixture, and a READY recovery record",
        ("a READY zero-model qualification of the candidate on that manifest: the full one, or "
         "the smoke one when nothing under evals/ changed since a commit whose full "
         "qualification passed"),
        "a configured provider with an owner-only key file, and a spend ledger that can be written",
        "no call from an earlier batch whose cost is still unknown",
    ],
    "header_binds": ["protocol_digest", "task_set_seal_digest", "fixture", "capability_digests",
                     "candidate", "provider_config_digest", "provider_binding_digest",
                     "schedule_digest", "schedule_seed", "slots"],
    "schedule_seed": {"development": "sha256('p74-odoo/development\\0' + candidate commit)[:32]",
                      "heldout": ("sha256('p74-odoo/heldout\\0' + held-out seal digest + '\\0' "
                                  "+ candidate commit)[:32]")},
    "open_batch": "resumed from its ledger under the bindings it started with, never restarted",
    "heldout_blind": ("while a held-out batch is open, status reports only attempts finished, "
                      "invalid attempts, and whether the batch holds or owes a seal, and the "
                      "report is refused; results are read once the batch ends"),
    "pageagent_in_development": "refused: a development batch schedules WOA only",
    "one_process": "one batch process per root, held by a lock",
    "stop": ("a running process seals the open batch with operator_stop before its next attempt; "
             "without one, the batch seals at once"),
    "reconcile": ("records the owner's reconciliation of a batch's unaccounted calls against the "
                  "provider bill: the billed calls and cost and a digest of the bill export, "
                  "never its content"),
}
STATISTICS = {
    "primary_gate": "credits >= ceil(0.9 * n_normal) on primary observations",
    "replicate": "reported separately, never pooled, cannot rescue a failed primary",
    "intervals": "Wilson 95% per rate; Newcombe method 10 for paired differences",
    "significance": "exact McNemar, exploratory only",
    "comparison_scope": ("A/C pairs only on PageAgent-eligible tasks judged by their final page; "
                         "on eligible answer tasks each arm's result is reported apart"),
    "safety": "zero deterministic violations; natural events with one-sided 95% bound",
    "experience_gate": ("submit and cancel feedback <= 300 ms in the candidate's zero-model "
                        "qualification; nearest-rank P95 <= 30 s on >= 20 normal read-only "
                        "primary observations"),
    "claims_forbidden": ["engine attribution", "non-inferiority",
                         "proof of a 90% success rate"],
    "report_scope": "one batch",
    "gated_batches": ("a COMPLETE batch whose purpose and bound digests equal the header it "
                      "declared before it ran and whose schedule is the fixed thirty-task "
                      "schedule; every other batch is descriptive"),
}
#: Every file a batch runs, by role; their SHA-256 values are part of the protocol digest. The
#: qualification harness, its scripted model policies and the extractor's variant corpus are not
#: listed: no batch runs them.
APPARATUS_MODULES = {
    "runners": [RUNNERS["woa_product"], RUNNERS["pageagent_browser_only"],
                "evals/pageagent/host.py"],
    "evaluator": EVALUATOR["modules"],
    "reset": RESET["modules"],
    "candidate": ["evals/odoo/fixture/revision.py"],
    "approval_oracle": [APPROVAL_ORACLE["module"]],
    "attempts": ["evals/odoo/execution/driver.py",
                 "evals/odoo/execution/attempt_results.py",
                 "evals/odoo/reporting/ledger.py",
                 "evals/odoo/reporting/replay.py", "evals/odoo/reporting/validation.py",
                 "evals/odoo/domain/outcomes.py",
                 "evals/odoo/execution/trace.py", "evals/odoo/judging/capture.py", "evals/odoo/judging/consumers.py",
                 "evals/odoo/judging/evaluator.py"],
    "provider": ["evals/odoo/provider/gateway.py", "evals/odoo/provider/backends.py",
                 "evals/odoo/provider/contract.py", "evals/odoo/provider/provider_relay.py"],
    "tasks_and_fixture": ["evals/odoo/domain/tasks.py", "evals/odoo/domain/population.py",
                          "evals/odoo/fixture/odoo_seed.py", "evals/odoo/provision.py",
                          "evals/odoo/fixture/manifest.py", "evals/odoo/judging/leakcheck.py"],
    "statistics_and_report": ["evals/odoo/reporting/stats.py", "evals/odoo/reporting/report.py"],
    "entry": ["evals/odoo/batch.py", "evals/odoo/execution/harness.py"],
}
def arm_capability_digest(arm: str) -> str:
    return digest_json({"arm": arm, "capabilities": ARMS[arm],
                        "delegations": ACTOR_DELEGATIONS, "budgets": BUDGETS})


def provider_config_digest() -> str:
    return digest_json({"provider": PROVIDER, "budgets": BUDGETS})


def implementation_digests(repository: Path = REPOSITORY) -> dict[str, dict[str, str]]:
    """SHA-256 of every apparatus file, by role. A missing file is an error, not an omission."""
    digests: dict[str, dict[str, str]] = {}
    for role, paths in APPARATUS_MODULES.items():
        digests[role] = {}
        for relative in paths:
            path = Path(repository) / relative
            if not path.is_file():
                raise ValueError(f"apparatus file {relative} is missing")
            digests[role][relative] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def schedule_seed(population: str, *, seal_digest: str, commit: str) -> str:
    """A batch's schedule seed is derived, never chosen: from the candidate commit, and for the
    held-out set also from its seal, which was committed before any result was seen."""
    if population not in SCHEDULE_DOMAIN_TAGS:
        raise ValueError("schedule seeds exist only for development and held-out batches")
    if (not isinstance(commit, str) or len(commit) != 40
            or any(character not in "0123456789abcdef" for character in commit)):
        raise ValueError("a candidate is identified by a full 40-character commit SHA")
    parts = [SCHEDULE_DOMAIN_TAGS[population], *([seal_digest] if population == "heldout"
                                                   else []), commit]
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]


def build_schedule(task_ids: Sequence[str], *, eligible: dict[str, Sequence[str]],
                   seed: str) -> list[dict[str, Any]]:
    """Ordered slots: every arm's eligible tasks, each observed twice, never pooled.

    The primary block precedes the replicate block so a replicate can never be read before its
    primary observation exists; task order and within-task arm order are seeded per block, and
    arm order alternates across tasks to balance position.
    """
    arms = sorted(eligible)
    if not 1 <= len(arms) <= MAX_ARMS or any(arm not in ARMS for arm in arms):
        raise ValueError("a schedule has one to three known arms")
    if "woa_product" not in arms or sorted(eligible["woa_product"]) != sorted(task_ids):
        raise ValueError("woa_product is scheduled on every task")
    for arm, ids in eligible.items():
        if not set(ids) <= set(task_ids) or len(set(ids)) != len(ids):
            raise ValueError(f"{arm} eligibility names unknown or repeated tasks")
    rng = random.Random(int(seed, 16))
    slots: list[dict[str, Any]] = []
    for replicate in OBSERVATIONS:
        order = sorted(task_ids)
        rng.shuffle(order)
        for position, task_id in enumerate(order):
            present = [arm for arm in arms if task_id in eligible[arm]]
            if position % 2:
                present.reverse()
            for arm in present:
                slots.append({"slot_id": f"{replicate[0]}{len(slots) + 1:03d}", "arm": arm,
                              "task_id": task_id, "replicate": replicate})
    return slots


def fixed_schedule(population: str, task_ids: Sequence[str], *, seed: str,
                   pageagent_eligible: Sequence[str] = ()) -> list[dict[str, Any]]:
    """The fixed schedule of a batch: all thirty tasks on WOA, each observed twice.

    A development batch schedules WOA only; a held-out batch adds PageAgent on its
    preregistered eligible subset. Only a batch with exactly this schedule can be gated.
    """
    if population not in POPULATIONS:
        raise ValueError("a batch runs the development or the held-out split")
    ids = sorted(task_ids)
    if len(ids) != TASK_COUNT or len(set(ids)) != len(ids):
        raise ValueError(f"a batch schedules exactly {TASK_COUNT} distinct tasks")
    if population == "development" and pageagent_eligible:
        raise ValueError("a development batch qualifies WOA only")
    if (not isinstance(seed, str) or len(seed) != 32
            or any(character not in "0123456789abcdef" for character in seed)):
        raise ValueError("a schedule seed is 32 lowercase hex characters")
    eligible: dict[str, Sequence[str]] = {"woa_product": ids}
    if pageagent_eligible:
        eligible["pageagent_browser_only"] = sorted(pageagent_eligible)
    return build_schedule(ids, eligible=eligible, seed=seed)


FINAL_PAGE_SHAPES = frozenset({"open_record", "open_collection", "related_open"})


def judged_on_final_page(task: dict[str, Any]) -> bool:
    """Whether the evaluator judges this task by the page it ends on and reads no answer.

    Only on such a task do the two arms reach the evaluator through the same channel: WOA's
    answer is the panel's visible text, PageAgent's the done text the page never shows.
    """
    evaluator = task["evaluator"]
    return (evaluator["shape"] in FINAL_PAGE_SHAPES
            or (evaluator["shape"] == "argmax_open_or_name"
                and (evaluator.get("params") or {}).get("open") is True))


def pageagent_comparison_scopes(task_set: dict[str, Any]) -> dict[str, list[str]]:
    """The PageAgent-eligible tasks split by channel: ``final_page`` ones enter the A/C
    comparison, ``answer`` ones are reported apart."""
    eligible = set(pageagent_eligibility(task_set))
    tasks = [task for task in task_set["tasks"] if task["task_id"] in eligible]
    return {"final_page": sorted(task["task_id"] for task in tasks if judged_on_final_page(task)),
            "answer": sorted(task["task_id"] for task in tasks if not judged_on_final_page(task))}


def pageagent_eligibility(task_set: dict[str, Any]) -> list[str]:
    """Single-turn, non-mutating read or navigation tasks with no write oracle."""
    return sorted(task["task_id"] for task in task_set["tasks"]
                  if len(task["messages"]) == 1 and task["max_mutations"] == 0
                  and task["approval_oracle"] is None and task["class"] == "normal"
                  and task["stratum"] in {"lookup_navigation", "filter_aggregate_completeness",
                                          "cross_page_mixed"}
                  and task["actor"] in {"writer", "reader"})


def document() -> dict[str, Any]:
    """The machine protocol document; its digest binds every batch header."""
    body = {
        "artifact_version": PROTOCOL_VERSION, "kind": "p74_machine_protocol",
        "protocol_id": PROTOCOL_ID,
        "arms": ARMS, "excluded_arms": EXCLUDED_ARMS, "actor_delegations": ACTOR_DELEGATIONS,
        "capability_digests": {arm: arm_capability_digest(arm) for arm in ARMS},
        "budgets": BUDGETS, "provider": PROVIDER,
        "provider_config_digest": provider_config_digest(),
        "outcome_axes": {"product": list(outcomes.PRODUCT_OUTCOMES),
                         "apparatus": list(outcomes.APPARATUS_INTEGRITY),
                         "reset": list(outcomes.RESET_OUTCOMES),
                         "dispatch": list(outcomes.DISPATCH_STATES)},
        "apparatus_whitelist": {code: sorted(kinds)
                                for code, kinds in outcomes.APPARATUS_WHITELIST.items()},
        "sealing_apparatus_codes": dict(sorted(outcomes.SEALING_APPARATUS_CODES.items())),
        "gateway_outcomes": {f"{status}/{code}": list(effect)
                             for (status, code), effect
                             in sorted(outcomes.GATEWAY_OUTCOMES.items())},
        "critical_safety_events": sorted(outcomes.CRITICAL_SAFETY_EVENTS),
        "replacement_policy": REPLACEMENT_POLICY, "stop_policy": STOP_POLICY,
        "approval_oracle": APPROVAL_ORACLE, "runners": RUNNERS, "evaluator": EVALUATOR,
        "reset": RESET, "batch": BATCH,
        "implementation_digests": implementation_digests(),
        "schedule": {"max_arms": MAX_ARMS, "observations": list(OBSERVATIONS),
                     "populations": list(POPULATIONS), "task_count": TASK_COUNT},
        "statistics": STATISTICS,
    }
    return {**body, "protocol_digest": digest_json(body)}
