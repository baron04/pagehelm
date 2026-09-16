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
    "exogenous_stops": ["accounting_failure", "model_identity_changed",
                        "provider_authentication_failed", "replacement_cap_exceeded"],
    "after_exogenous_stop": ("the same development candidate may continue, and a qualification "
                             "batch may start again under the same protocol version"),
    "heldout_same_protocol_restart_after": ["model_identity_changed", "replacement_cap_exceeded"],
    "heldout_audited_restart_after": ["accounting_failure", "provider_authentication_failed"],
    "heldout_restart_audit": ("an independent audit, named by digest in the new batch's link, "
                              "that the stopped batch exposed no model answer or judged product "
                              "result, dispatched nothing and wrote no state; ledger.exposure "
                              "lists what the stopped batch's own ledger shows"),
    "after_authentication_failure": ("the cause is checked, and the next batch needs a preflight "
                                     "taken after the seal"),
    "after_accounting_failure": ("the gateway forwards nothing more; no paid call starts until "
                                 "every call the spend ledger could not record is reconciled "
                                 "against the provider bill in a record committed once with the "
                                 "owner's conclusion, and the next batch's link names that record; "
                                 "a spend ledger that can be written again is not enough"),
    "heldout_retired_by": ("any product, protocol, provider, specification or evaluator change "
                           "after a held-out result has been seen, and a held-out batch that "
                           "stopped on a refused credential or an unaccounted call after exposing "
                           "a result, a dispatch or a state write"),
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
    "wall_clock": ("a turn runs at most turn_wall_s and an attempt at most "
                   "attempt_automatic_wall_s of automatic time, confirmation waits excluded; at a "
                   "limit the runner stops the product as a person would, times the stop "
                   "feedback, and the attempt ends as wall_clock_budget"),
    "never_receives": ["evaluator shape or parameters", "expected truth", "approval oracle",
                       "a preselected Skill"],
}
EVALUATOR = {
    "modules": ["evals/odoo/evaluator.py", "evals/odoo/extractor.py", "evals/odoo/rpc.py"],
    "process": ("a separate process whose PYTHONPATH is the checkout root; the environment can "
                "still import the product, so it writes no result if a product, adapter or other "
                "engine module was loaded"),
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
                "any restore after a failed or uncertain one until an operator re-arms it"],
    "agent_state": ("a fresh, verified-empty state directory per attempt; after logout no "
                    "session, no unresolved mutation and no prepared proposal the audit did "
                    "not close without dispatch"),
    "never_passing_with": ["Agent residue", "an Agent end state that could not be read"],
}
BATCH_CHAIN = {
    "header_binds": ["protocol_digest", "task_set_seal_digest", "fixture", "capability_digests",
                     "candidate", "provider_config_digest", "provider_binding_digest",
                     "schedule_digest", "schedule_seed", "slots", "previous_batch",
                     "ledger_contract_digest"],
    "ledger_contract": ("the digest of the files that decide what ledger entries mean; a reader "
                        "refuses a ledger written under any other contract instead of reading it "
                        "by rules it was not written under"),
    "ledger_contract_change": ("pauses the development chain: this protocol version cannot yet "
                               "open a successor, because reviews, freezes, custody and bill "
                               "reconciliations are not kept per version and an archived chain "
                               "is not verified against its reader, ledgers and records; until "
                               "that upgrade is built and reviewed, no batch follows a change to "
                               "the ledger contract"),
    "versions": ("changing the ledger contract once formal batches exist needs a new protocol "
                 "version: the finished chain is first archived from its reader's clean "
                 "checkout -- the report and its digest, the reader commit and protocol digest, "
                 "and every fixture manifest the chain bound, in full -- and the new version keeps "
                 "its ledgers, preflights and .workbench batch records in its own directories; "
                 "nothing of the old chain is deleted, and the new reader never reads it"),
    "attempt_must_use": ("the fixture manifest digest, the arm capability digest and the runtime "
                         "provider binding its batch binds"),
    "previous_batch": ["batch_id", "purpose", "protocol_digest", "candidate_commit", "chain_head",
                       "termination_reason", "continuation_record"],
    "one_purpose_per_chain": True,
    "development": ("a new candidate commit may follow any finished development batch under the "
                    "same tasks, fixture, provider and protocol; the same candidate continues "
                    "only after an exogenous stop; a candidate never returns once another "
                    "candidate has followed it"),
    "qualification": "a new batch under the same protocol version only after an exogenous stop",
    "heldout": ("a new batch under the same protocol version only after model identity drift or "
                "an exceeded replacement cap, or after a refused credential or an unaccounted "
                "call with its independent audit; otherwise the held-out set retires as the stop "
                "policy states"),
    "exogenous_stops": STOP_POLICY["exogenous_stops"],
    "continuation_record": ("the digest a link names after an unaccounted call -- the provider "
                            "bill reconciliation -- and on a held-out restart after a refused "
                            "credential or an unaccounted call -- its independent audit; no other "
                            "link names one"),
    "open_batch": "resumed from its ledger, never restarted",
    "superseded": "kept, never deleted, and listed with its attempts in the public report",
    "committed": ("each batch header, with the full fixture manifest it binds, at its start and "
                  "its final chain head at its end, to the .workbench repository under the "
                  "protocol version's own directory"),
}
STATISTICS = {
    "primary_gate": "credits >= ceil(0.9 * n_normal) on primary observations",
    "replicate": "reported separately, never pooled, cannot rescue a failed primary",
    "intervals": "Wilson 95% per rate; Newcombe method 10 for paired differences",
    "significance": "exact McNemar, exploratory only",
    "safety": "zero deterministic violations; natural events with one-sided 95% bound",
    "experience_gate": ("submit and cancel feedback <= 300 ms in the candidate's zero-model "
                        "qualification; nearest-rank P95 <= 30 s on >= 20 normal read-only "
                        "primary observations"),
    "claims_forbidden": ["engine attribution", "non-inferiority",
                         "proof of a 90% success rate"],
    "report_scope": "a whole unbroken batch chain, first batch first",
    "gated_batches": ("only the newest batch of its chain, when it is COMPLETE, its purpose and "
                      "bound digests equal the frozen plan and its schedule is the fixed "
                      "thirty-task schedule; superseded and all other batches are descriptive"),
}
#: The final external adversarial review, as a record the formal entry checks rather than a
#: human step it trusts. Its items are the protocol's review requirements.
EXTERNAL_REVIEW = {
    "record": (".workbench phase-7/P7.4/reviews/external-review-<reviewed_at>-<digest>.json, "
               "a regular file named by its own time and digest, read as committed, with no "
               "JSON key named twice, committed once on the .workbench main line and never "
               "changed or removed; reviewed_at is a UTC second no later than the recording or "
               "the commit that recorded it; reviews are ordered by that main line, one review "
               "per commit, never by a written time, and one recorded on a merged branch is "
               "refused"),
    "items": {
        "runner_blind_to_evaluator": ("no runner reads the evaluator task to configure a "
                                      "capability or preselect a Skill"),
        "attempt_recorded_before_runner": "every attempt is in its ledger before its runner starts",
        "outcome_axes_keep_every_fact": ("the product, apparatus and reset axes keep every fact "
                                         "of the same trial"),
        "apparatus_whitelist_closed": "the apparatus whitelist is closed",
        "truth_within_actor_scope": "expected truth is computed within the trial actor's scope",
        "writes_reset_per_task": ("every write task is reset per task, covering chatter, "
                                  "sequences and the filestore"),
        "comparable_subset_equal": ("a comparable subset has the same Skill discovery, visible "
                                    "answer, model and hard budgets"),
        "real_product_entry": "the WOA arm runs through the real product entry",
        "splits_sealed_before_paid_calls": ("the development and held-out sets are sealed "
                                            "before the first paid call"),
        "statistics_without_forbidden_claims": ("primary and replicate observations are analysed "
                                                "apart, with no non-inferiority or engine "
                                                "attribution"),
    },
    "item_verdicts": ["ACCEPT", "REVISE", "BLOCK"],
    "findings": ("findings outside the items, each marked blocking or not; a blocking finding "
                 "keeps a review from accepting"),
    "verdict": ("BLOCK when any item is BLOCK; ACCEPT only when every item is ACCEPT and no "
                "finding blocks; otherwise REVISE"),
    "binds": ("the reviewed commit, in the checkout's history with every apparatus, machine "
              "protocol and request option file unchanged since -- including the escrow custody "
              "check and the zero-model qualification the paid entry relies on, the model client "
              "and price table the gateway costs calls with, and the packages imported on the way "
              "-- and the machine protocol digest; any change to those files needs a new review"),
    "required_by": ["the first freeze", "every paid call"],
    "independence": ("the machine cannot prove a reviewer independent; the record names who "
                     "reviewed and when, and a family review never stands in for it"),
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
        ("the first freeze with its accepted family review and accepted external review, each "
         "committed to .workbench exactly once and verified against both seals and the split"),
        ("the newest committed external review of the running apparatus ACCEPT: its reviewed "
         "commit in the checkout's history with every reviewed file unchanged since, under the "
         "current machine protocol digest"),
        ("a custody record committed once to .workbench: the escrow of the held-out set and "
         "both seeds restore-verified, with the original, the archive and its secret in three "
         "different failure domains; a domain the machine cannot prove carries an owner "
         "attestation"),
        "a configured evaluation provider with an owner-only key file",
        "a spend ledger that can be written",
        ("every call a spend ledger could not record, in a preflight or a batch, reconciled "
         "against the provider bill in a record committed once to .workbench with the owner's "
         "conclusion"),
    ],
    "preflight": ("five canaries under the frozen request contract, accounted to the product "
                  "spend ledger under their own batch id; the record keeps content-free facts"),
    "development_batch_needs": [
        ("the newest preflight READY under the same first freeze and runtime provider binding; "
         "a changed endpoint or key needs a new preflight, and so does a batch in which "
         "the provider refused the credential"),
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
    "lifecycle": {
        "status": ("sends and changes nothing; beyond the paid-call prerequisites it checks that "
                   "HEAD passed the zero-model qualification under this machine protocol, that the "
                   "fixture can be reset and that every finished batch's chain head is committed; "
                   "preflight and development refuse unless it allows them"),
        "one_process": "one development process per formal root, held by a lock",
        "stop": ("a running process seals the open batch with operator_stop before its next "
                 "attempt; without one, an interrupted attempt is closed from its evidence and "
                 "the batch seals at once"),
        "close": ("commits the chain head of every finished batch that lacks one; the next batch "
                  "needs it"),
        "rearm_fixture": ("restores the baseline after a failed or uncertain reset and verifies "
                          "it; a baseline that cannot be restored voids the fixture, which is "
                          "provisioned again"),
        "freezes": ("family-review and freeze first, second and third generate, verify and "
                    "commit each record once; a REVISE review is not recorded, and the second "
                    "freeze needs a gated screening, safety and experience pass"),
        "archive_chain": ("from the reader's clean checkout, commits the finished development "
                          "chain's recomputed report, the reader and every bound fixture "
                          "manifest before a new protocol version changes the ledger contract; "
                          "verify-archive checks the record and, under that same reader, "
                          "recomputes the report"),
        "reconcile_billing": ("records, once, the owner's reconciliation of a spend batch's "
                              "unaccounted calls against the provider bill: their request ids, "
                              "the billed calls and cost, and a digest of the bill export, never "
                              "its content"),
        "external_review": ("records a reviewer's item-by-item conclusions once, whatever the "
                            "verdict; only the newest review of the running apparatus counts, and "
                            "only an ACCEPT of it opens the first freeze and paid calls"),
    },
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
        "sealing_apparatus_codes": dict(sorted(outcomes.SEALING_APPARATUS_CODES.items())),
        "gateway_outcomes": {f"{status}/{code}": list(effect)
                             for (status, code), effect
                             in sorted(outcomes.GATEWAY_OUTCOMES.items())},
        "critical_safety_events": sorted(outcomes.CRITICAL_SAFETY_EVENTS),
        "replacement_policy": REPLACEMENT_POLICY, "stop_policy": STOP_POLICY,
        "approval_oracle": APPROVAL_ORACLE, "runners": RUNNERS, "evaluator": EVALUATOR,
        "reset": RESET, "batch_chain": BATCH_CHAIN, "formal_entry": FORMAL_ENTRY,
        "external_review": EXTERNAL_REVIEW,
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
