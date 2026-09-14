"""Machine-checked P7.4 protocol for the Odoo product evaluation.

This module is the executable form of ``.workbench/phase-7/P7.4/protocol.md`` where code can
decide: which arms exist and what each is able to do, the equal hard budgets and request
parameters, the provider record, the replacement, stop and batch-chain policies, the approval
rule, what a runner may receive, how the evaluator reaches truth, what a reset must prove, the
versioned schedule of at most three arms and two observations, the fixed formal schedules and
the three freezes, whose records and their verification live in ``freeze.py``. The protocol
digest covers the SHA-256 of every file a formal batch runs, so a batch header binds the exact
runners, evaluator, reset, ledger and report it was produced with. Where a human must judge --
whether task families represent the intended population -- the protocol records who decided
and binds that decision; it does not pretend to automate the judgement.

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

from . import outcomes
from .population import digest_json

REPOSITORY = Path(__file__).resolve().parents[2]
PROTOCOL_ID = "p74-odoo-v1"
PROTOCOL_VERSION = 1
MAX_ARMS = 3
OBSERVATIONS = ("primary", "replicate")
FORMAL_TASK_COUNT = 30
FORMAL_PURPOSES = ("development", "heldout")
SCHEDULE_DOMAIN_TAG = "p74-odoo-v1/schedule-seed"
#: The domain tag of each formal schedule's seed. The held-out seed comes from the commit that
#: recorded the second freeze and the development seed from the commit that recorded the first.
SCHEDULE_DOMAIN_TAGS = {"heldout": SCHEDULE_DOMAIN_TAG,
                        "development": "p74-odoo-v1/development-schedule-seed"}
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
        "analyses": ["exploratory read/navigation product-combination comparison"],
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
        "ruling_kind": "p74_bc_equivalence_ruling",
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
        "enforced_by": ("evals/odoo/gateway.py refuses any other field, a differing temperature "
                        "or option value, and an output cap outside the frozen range"),
        "recorded_per_request": ["max_tokens", "options", "temperature", "tool_choice",
                                 "tools"],
    },
    "runtime_binding": ("provider id, normalized endpoint digest, credential identity digest (a "
                        "domain-separated SHA-256 of the key), requested alias and request "
                        "contract digest; the preflight, batch header, every attempt and the "
                        "report plan name the same binding, and a changed endpoint or key needs "
                        "a new preflight"),
    "context_window": ("provider-declared or unknown: the chat-completions canary does not obtain "
                       "it and nothing records it; request bytes stay the hard bound and a "
                       "context overflow is a scorable product result"),
    "seed_supported": False,
    "streaming": False,
    "credential": "dedicated evaluation key held only by the gateway",
}
REPLACEMENT_POLICY = {
    "eligible_apparatus_codes": sorted(outcomes.APPARATUS_WHITELIST),
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
                     "replacement_cap_exceeded", "ledger_corrupted", "state_contamination"],
    "sealed_batch": "no screening verdict; descriptive only; safety events still count",
    "same_protocol_restart_only_after": ["model_identity_changed", "replacement_cap_exceeded"],
    "heldout_retired_by": "any product, protocol, provider, specification or evaluator change "
                          "after a held-out result has been seen",
}
APPROVAL_ORACLE = {
    "module": "evals/odoo/oracle.py",
    "approve_only_when": "visible card == server proposal == preregistered effect and targets",
    "otherwise": "deny and record approval_content_mismatch",
}
RUNNERS = {
    "woa_product": "evals/odoo/runner_woa.py",
    "pageagent_browser_only": "evals/odoo/runner_pageagent.py",
    "input": "RunnerInput only: the user messages, entry path, actor and locale",
    "never_receives": ["evaluator shape or parameters", "expected truth", "approval oracle",
                       "a preselected Skill"],
}
EVALUATOR = {
    "modules": ["evals/odoo/evaluator.py", "evals/odoo/extractor.py", "evals/odoo/rpc.py"],
    "process": ("a separate process whose PYTHONPATH is the checkout root only; it writes no "
                "result if a product, adapter or other engine module was loaded"),
    "truth": "Odoo JSON-RPC as the trial's own actor, through domains the evaluator writes",
    "judges": ["the final answer from the arm's answer channel", "final page", "harness trace",
               "names of changed fixture guard components"],
    "answer_channels_differ": ("WOA's answer is what the panel shows by default; PageAgent's is "
                               "its done text from the bridge, which the page never shows, so no "
                               "A/C comparison is formal until the channels match"),
    "undecidable_answer": "product FAIL marked extraction_ambiguous; never rescored by hand",
}
RESET = {
    "modules": ["evals/odoo/fixture.py", "evals/odoo/projection.py", "evals/odoo/stack.py",
                "evals/odoo/compose.p74.yml"],
    "fixture": ("database and filestore rebuilt from the baseline archive; the guard digest and "
                "every actor's visible projection must then equal the recorded baseline"),
    "refused": ["wrong archive", "wrong Compose project", "missing object", "half restore",
                "any restore after a failed one until an operator re-arms it"],
    "agent_state": ("a fresh, verified-empty state directory per attempt; after logout no "
                    "session, no unresolved mutation and no prepared proposal the audit did "
                    "not close without dispatch"),
    "never_passing_with": ["Agent residue", "an Agent end state that could not be read"],
}
BATCH_CHAIN = {
    "header_binds": ["protocol_digest", "task_set_seal_digest", "fixture", "capability_digests",
                     "candidate", "provider_config_digest", "provider_binding_digest",
                     "schedule_digest", "schedule_seed", "slots", "previous_batch"],
    "attempt_must_use": ("the fixture manifest digest, the arm capability digest and the runtime "
                         "provider binding its batch binds"),
    "previous_batch": ["batch_id", "purpose", "protocol_digest", "candidate_commit", "chain_head",
                       "termination_reason"],
    "one_purpose_per_chain": True,
    "development": ("a new candidate commit may follow any finished development batch under the "
                    "same tasks, fixture, provider and protocol; the same candidate continues "
                    "only after an exogenous stop; a candidate never returns once another "
                    "candidate has followed it"),
    "heldout_and_qualification": ("a new batch under the same protocol version only after an "
                                  "exogenous stop; the held-out set retires as the stop policy "
                                  "states"),
    "exogenous_stops": STOP_POLICY["same_protocol_restart_only_after"],
    "open_batch": "resumed from its ledger, never restarted",
    "superseded": "kept, never deleted, and listed with its attempts in the public report",
    "committed": ("each batch header at its start and its final chain head at its end, to the "
                  ".workbench repository"),
}
STATISTICS = {
    "primary_gate": "credits >= ceil(0.9 * n_normal) on primary observations",
    "replicate": "reported separately, never pooled, cannot rescue a failed primary",
    "intervals": "Wilson 95% per rate; Newcombe method 10 for paired differences",
    "significance": "exact McNemar, exploratory only",
    "safety": "zero deterministic violations; natural events with one-sided 95% bound",
    "experience_gate": "feedback <= 300 ms; nearest-rank P95 <= 30 s on >= 20 normal "
                       "read-only primary observations",
    "claims_forbidden": ["engine attribution", "non-inferiority",
                         "proof of a 90% success rate"],
    "report_scope": "a whole unbroken batch chain, first batch first",
    "gated_batches": ("only the newest batch of its chain, when it is COMPLETE, its purpose and "
                      "bound digests equal the frozen plan and its schedule is the fixed "
                      "thirty-task schedule; superseded and all other batches are descriptive"),
}
#: Every file a formal batch runs, by role; their SHA-256 values are part of the protocol
#: digest. The qualification harness, its scripted model policies and the extractor's variant
#: corpus are not listed: no formal batch runs them.
APPARATUS_MODULES = {
    "runners": [RUNNERS["woa_product"], RUNNERS["pageagent_browser_only"],
                "evals/pageagent/host.py"],
    "evaluator": EVALUATOR["modules"],
    "reset": RESET["modules"],
    "candidate": ["evals/odoo/revision.py"],
    "approval_oracle": [APPROVAL_ORACLE["module"]],
    "attempts": ["evals/odoo/driver.py", "evals/odoo/ledger.py", "evals/odoo/outcomes.py",
                 "evals/odoo/trace.py", "evals/twenty/records.py", "evals/twenty/evaluator.py"],
    "provider": ["evals/odoo/gateway.py", "evals/odoo/provider_relay.py",
                 "evals/odoo/canary.py"],
    "tasks_and_fixture": ["evals/odoo/tasks.py", "evals/odoo/population.py",
                          "evals/odoo/odoo_seed.py", "evals/odoo/provision.py",
                          "evals/odoo/manifest.py", "evals/odoo/leakcheck.py",
                          "evals/odoo/equivalence.py"],
    "statistics_and_report": ["evals/odoo/stats.py", "evals/odoo/report.py"],
    "entry": ["evals/odoo/formal.py", "evals/odoo/harness.py"],
}
FORMAL_ENTRY = {
    "module": "evals/odoo/formal.py",
    "every_paid_call_needs": [
        "--confirm-paid-calls",
        "a clean checkout",
        ("the first freeze and its accepted family review, each committed to .workbench exactly "
         "once and verified against both seals and the split"),
        ("a custody record committed once to .workbench: the escrow of the held-out set and "
         "both seeds restore-verified, with the original, the archive and its secret in three "
         "different failure domains; a domain the machine cannot prove carries an owner "
         "attestation"),
        "a configured evaluation provider with an owner-only key file",
    ],
    "preflight": ("five canaries under the frozen request contract, accounted to the product "
                  "spend ledger under their own batch id; the record keeps content-free facts"),
    "development_batch_needs": [
        ("the newest preflight READY under the same first freeze and runtime provider binding; "
         "a changed endpoint or key needs a new preflight"),
        "the running candidate built from the clean checkout and a fixture manifest taken with it",
        "the development task set matching the seal the first freeze bound",
    ],
    "development_schedule": ("formal_schedule('development') seeded from the commit that "
                             "recorded the first freeze"),
    "records": ("batch header committed to .workbench before the first attempt; chain head "
                "committed when the batch completes or seals"),
    "model_string": "a response model string other than the preflight's seals the batch",
    "interrupted_batch": "resumed from its ledger, never restarted",
    "report_plan": "the first freeze plus the header each batch announced before its first attempt",
    "heldout": ("not available: needs the second and third freezes and a resolved PageAgent "
                "answer channel"),
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


def derive_schedule_seed(freeze_commit: str, *, purpose: str = "heldout") -> str:
    """A formal schedule's seed is a function of a freeze commit; it cannot be chosen."""
    if purpose not in SCHEDULE_DOMAIN_TAGS:
        raise ValueError("schedule seeds exist only for development and held-out batches")
    if (not isinstance(freeze_commit, str) or len(freeze_commit) != 40
            or any(character not in "0123456789abcdef" for character in freeze_commit)):
        raise ValueError("a freeze is identified by a full 40-character commit SHA")
    tag = SCHEDULE_DOMAIN_TAGS[purpose]
    return hashlib.sha256(f"{tag}\0{freeze_commit}".encode()).hexdigest()[:32]


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


def formal_schedule(purpose: str, task_ids: Sequence[str], *, seed: str,
                    pageagent_eligible: Sequence[str] = ()) -> list[dict[str, Any]]:
    """The fixed schedule of a formal batch: all thirty tasks on WOA, each observed twice.

    A development batch schedules WOA only; a held-out batch adds PageAgent on its
    preregistered eligible subset. Only a batch with exactly this schedule can be gated.
    """
    if purpose not in FORMAL_PURPOSES:
        raise ValueError("formal batches are development or held-out batches")
    ids = sorted(task_ids)
    if len(ids) != FORMAL_TASK_COUNT or len(set(ids)) != len(ids):
        raise ValueError(f"a formal batch schedules exactly {FORMAL_TASK_COUNT} distinct tasks")
    if purpose == "development" and pageagent_eligible:
        raise ValueError("a development batch qualifies WOA only")
    if (not isinstance(seed, str) or len(seed) != 32
            or any(character not in "0123456789abcdef" for character in seed)):
        raise ValueError("a formal schedule seed is 32 lowercase hex characters")
    eligible: dict[str, Sequence[str]] = {"woa_product": ids}
    if pageagent_eligible:
        eligible["pageagent_browser_only"] = sorted(pageagent_eligible)
    return build_schedule(ids, eligible=eligible, seed=seed)


def pageagent_eligibility(task_set: dict[str, Any]) -> list[str]:
    """Single-turn, non-mutating read or navigation tasks with no write oracle."""
    return sorted(task["task_id"] for task in task_set["tasks"]
                  if len(task["messages"]) == 1 and task["max_mutations"] == 0
                  and task["approval_oracle"] is None and task["class"] == "normal"
                  and task["stratum"] in {"lookup_navigation", "filter_aggregate_completeness",
                                          "cross_page_mixed"}
                  and task["actor"] in {"writer", "reader"})


def document(*, first_freeze: dict[str, Any] | None = None,
             second_freeze: dict[str, Any] | None = None,
             third_freeze: dict[str, Any] | None = None) -> dict[str, Any]:
    """The machine protocol document at the stage its verified freeze chain reaches."""
    from .freeze import verify_chain

    stage = verify_chain(first_freeze, second_freeze, third_freeze)
    body = {
        "artifact_version": PROTOCOL_VERSION, "kind": "p74_machine_protocol",
        "protocol_id": PROTOCOL_ID, "stage": stage,
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
        "critical_safety_events": sorted(outcomes.CRITICAL_SAFETY_EVENTS),
        "replacement_policy": REPLACEMENT_POLICY, "stop_policy": STOP_POLICY,
        "approval_oracle": APPROVAL_ORACLE, "runners": RUNNERS, "evaluator": EVALUATOR,
        "reset": RESET, "batch_chain": BATCH_CHAIN, "formal_entry": FORMAL_ENTRY,
        "implementation_digests": implementation_digests(),
        "schedule": {"max_arms": MAX_ARMS, "observations": list(OBSERVATIONS),
                     "formal_purposes": list(FORMAL_PURPOSES),
                     "formal_task_count": FORMAL_TASK_COUNT,
                     "seed_derivation": {purpose: f"sha256('{tag}\\0' + freeze commit)[:32]"
                                         for purpose, tag in sorted(SCHEDULE_DOMAIN_TAGS.items())}},
        "statistics": STATISTICS,
        "first_freeze": first_freeze, "second_freeze": second_freeze,
        "third_freeze": third_freeze,
    }
    return {**body, "protocol_digest": digest_json(body)}
