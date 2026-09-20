"""Which code reads each fallible observation, and what it decides with it.

The rule this apparatus turns on is that a source nobody could read is a different fact from a
source read as empty. The rule holds only where every consumer of a source applies it, so the
consumers are written down here rather than left to be found. The registry is data: the tests
check the code against it, and a reviewer reads it to follow a source to the decisions it
reaches without first having to find them.

A function that reads one of :data:`evals.odoo.judging.capture.SOURCES` and is not listed here is a
consumer nobody checked; :mod:`evals.odoo.tests.judging.test_consumers` refuses one.
"""
from __future__ import annotations

from typing import NamedTuple


class Consumer(NamedTuple):
    """One function that reads a fallible source, and what it decides from it."""

    module: str
    #: The function's qualified name, as it is written in that module.
    function: str
    decides: str


#: Every consumer of every fallible source. The order is the order a run reaches them.
CONSUMERS: dict[str, tuple[Consumer, ...]] = {
    "runner_artifact": (
        Consumer("evals/odoo/execution/runner_woa.py", "empty_artifact",
                 "the record shape the product's own arm reports, and what an attempt that "
                 "never started reports"),
        Consumer("evals/odoo/execution/runner_pageagent.py", "empty_artifact",
                 "the same, for the browser-only arm"),
        Consumer("evals/odoo/execution/attempt_execution.py", "run_attempt",
                 "whether the product started, which turns were recorded and how they ended, "
                 "the attempt's times, and which apparatus fault the run met"),
        Consumer("evals/odoo/execution/attempt_results.py", "final_page",
                 "whether a final page was captured at all, or why it was not"),
        Consumer("evals/odoo/execution/driver.py", "Driver._judge_persisted",
                 "the turns a dead driver wrote down as its runner read them, each checked "
                 "against the digest its checkpoint bound; the approvals it never wrote are "
                 "unavailable, never an empty list"),
        Consumer("evals/odoo/execution/trace.py", "project_pageagent",
                 "the browser-only arm's trace, whose counts exist only if its bridge reported "
                 "them"),
        Consumer("evals/odoo/judging/evaluator.py", "_turns",
                 "the turns a judgement is read from; a missing record stops the judgement"),
        Consumer("evals/odoo/judging/evaluator.py", "_text",
                 "the answer a turn showed, where a turn nobody read an answer from is not an "
                 "empty answer"),
        Consumer("evals/odoo/judging/write_judgement.py", "judge_write",
                 "the approval decisions the oracle made, which carry their own safety events"),
    ),
    "final_page": (
        Consumer("evals/odoo/execution/attempt_results.py", "final_page", "the capture the evaluator reads"),
        Consumer("evals/odoo/execution/driver.py", "Driver._judge_persisted",
                 "none: the page a dead driver's run ended on was never read, and a recovered "
                 "judgement is given it as unavailable"),
        Consumer("evals/odoo/judging/evaluator.py", "_page_path",
                 "the path the run left behind, or nothing where no page was observed"),
        Consumer("evals/odoo/judging/evaluator.py", "_on_page",
                 "whether the task's record is the one on screen; an unread page fails as "
                 "unobserved and names no turn"),
        Consumer("evals/odoo/judging/evaluator.py", "judge",
                 "whether a failure read from the page belongs to the last turn"),
    ),
    "agent_audit": (
        Consumer("evals/odoo/execution/trace.py", "project_woa",
                 "the product's endings, counts, approvals and furthest dispatch"),
        Consumer("evals/odoo/execution/attempt_execution.py", "run_attempt",
                 "the dispatch state, the per-turn endings, the trace counts, and whether the "
                 "fixture must be restored because nobody read them"),
        Consumer("evals/odoo/execution/driver.py", "Driver.recover_open_attempt",
                 "the same for an attempt a dead driver left open"),
        Consumer("evals/odoo/execution/driver.py", "Driver._judge_persisted",
                 "the trace a recovered judgement reads; an audit nobody could export reaches "
                 "it as unavailable"),
        Consumer("evals/odoo/execution/attempt_results.py", "agent_assertions",
                 "which proposals the audit closes, so residue is not read from silence"),
        Consumer("evals/odoo/execution/oracle.py", "ApprovalOracle.decide",
                 "whether the card matches what the product proposed; a proposal nobody read "
                 "is never approved"),
        Consumer("evals/odoo/judging/evaluator.py", "_no_changes",
                 "whether a read-only task dispatched anything; an unread audit is no safety "
                 "event"),
        Consumer("evals/odoo/judging/write_judgement.py", "judge_write",
                 "whether the write was dispatched and approved exactly once"),
        Consumer("evals/odoo/judging/evaluator.py", "judge",
                 "whether a refusal task's denial was confirmed"),
    ),
    "agent_state": (
        Consumer("evals/odoo/execution/attempt_execution.py", "run_attempt",
                 "the Agent's state after the run, which the reset asserts against"),
        Consumer("evals/odoo/execution/driver.py", "Driver.recover_open_attempt",
                 "the same after an orphaned session is ended"),
        Consumer("evals/odoo/execution/attempt_results.py", "agent_assertions",
                 "the residue an attempt leaves; an unread state never passes a reset"),
    ),
    "fixture_guard": (
        Consumer("evals/odoo/execution/attempt_execution.py", "run_attempt",
                 "what the run changed, and whether the fixture must be restored because "
                 "nobody could read it"),
        Consumer("evals/odoo/execution/driver.py", "Driver._reset",
                 "whether a verified restore is required"),
        Consumer("evals/odoo/execution/driver.py", "Driver.recover_open_attempt",
                 "what a dead driver's run changed, read once before the restore; it decides "
                 "the reset's basis and is what a recovered judgement reads"),
        Consumer("evals/odoo/execution/driver.py", "Driver._judge_persisted",
                 "the changes a recovered judgement reads, unavailable where the guard could "
                 "not be read"),
        Consumer("evals/odoo/judging/evaluator.py", "_no_changes",
                 "whether a read-only task changed the fixture"),
        Consumer("evals/odoo/judging/write_judgement.py", "judge_write",
                 "whether a write touched anything outside its target"),
    ),
    "evaluator_output": (
        Consumer("evals/odoo/execution/driver.py", "Driver._evaluator",
                 "the truth and the judgement, or that the evaluator process produced neither"),
        Consumer("evals/odoo/execution/attempt_execution.py", "run_attempt",
                 "the product outcome, and whether a judgement was reached at all"),
    ),
    "provider_settlement": (
        Consumer("evals/odoo/provider/gateway.py", "ProviderGateway.close_attempt",
                 "which of the attempt's calls settled, and which are written down as "
                 "unresolved"),
        Consumer("evals/odoo/provider/gateway.py", "ProviderGateway._record_locked",
                 "whether an answer still settles its call, or arrives after it was written "
                 "down as unresolved"),
        Consumer("evals/odoo/reporting/replay.py", "_Replay.model_unresolved",
                 "that a call settled exactly once, and that an attempt ends only when all of "
                 "its calls have"),
        Consumer("evals/odoo/execution/attempt_results.py", "attempt_cost",
                 "the attempt's cost, where a call with no answer is usage unavailable"),
        Consumer("evals/odoo/batch.py", "owed",
                 "which calls the bill must account for before any later paid call"),
    ),
}

#: Conditional substitutions a consumer makes deliberately, with the reason. A conditional that
#: yields an empty value reads the same to a static check whether it fills in a missing
#: observation or states a fact, so each one is written down here and the check exempts these
#: functions from that one rule only -- `or []` and `.get(name, [])` stay forbidden everywhere.
#: Each entry names the expression it exempts, not the function around it: a reason that covered a
#: whole function would also exempt every other substitution written in it, about none of which it
#: says anything. An expression that is rewritten stops matching and must be justified again.
DELIBERATE_SUBSTITUTIONS = (
    ("evals/odoo/judging/evaluator.py", "_text", "'' if answer is None else str(answer)",
     "A turn the runner stopped, or one whose bridge never returned, shows no answer at all. "
     "The task asked for one, so the judgement reads it as an empty answer and fails the "
     "product; the turn itself was observed, and it is its content that is absent."),
    ("evals/odoo/judging/evaluator.py", "judge",
     "[ex.Money(_decimal(naive), p['currency'])] if _decimal(naive) != _decimal(amount) else []",
     "It chooses between a value and an empty one from the task's own truth -- the forbidden "
     "answers a currency sum has only when the naive total differs -- rather than from an "
     "observation. What `judge` reads of the attempt goes through `_read`, `_page_is` and "
     "`_no_changes`, which this rule still covers."),
    ("evals/odoo/execution/attempt_execution.py", "run_attempt",
     "max(len(recorded_turns), len({int(value['turn']) for value in checkpoints if 'turn' in "
     "value})) if product_started else 0",
     "A product that did not start sent no turn. `product_started` is read from the runner's "
     "own record, so the zero is that observation's consequence rather than a stand-in for a "
     "count nobody took."),
    ("evals/odoo/execution/attempt_execution.py", "run_attempt",
     "audit_read.value if audit_read.available else []",
     "The empty audit is read only by `agent_assertions`, and there it counts every prepared "
     "proposal as still open: an audit nobody read closes nothing. It moves the attempt toward "
     "residue, never toward clean, and what the product dispatched and counted is taken from "
     "`UNREAD_TRACE` on the next line rather than from this list."),
    ("evals/odoo/execution/attempt_execution.py", "run_attempt",
     "guard_read.value if guard_read.available else []",
     "A guard nobody could capture shows no differences, and this list is never read as a "
     "fixture that did not change: the unread capture raises its own apparatus event above, and "
     "the reset is forced whatever the list holds."),
    ("evals/odoo/execution/attempt_execution.py", "run_attempt",
     "artifact['approvals'] if arm == 'woa_product' else []",
     "Only the product has an approval channel. The browser-only arm has none to observe, so "
     "the empty list is what that arm is rather than a record its runner failed to write."),
    ("evals/odoo/execution/attempt_results.py", "agent_assertions",
     "set(end['prepared_proposals']) if end is not None else set()",
     "An end state nobody could read holds no proposals to compare, and the zero counts that "
     "follow are never read as an attempt that left nothing open: `_agent_counts` records that "
     "state as null, and a started attempt whose end state was unreadable returns "
     "`agent_state_unreadable` residue below before any count of it is judged."),
)

#: Functions that turn a record into a capture and decide nothing themselves. They read every
#: component alike, so they are listed once rather than under each source.
SHARED_READERS = (
    ("evals/odoo/judging/evaluator.py", "_read"),
    ("evals/odoo/judging/evaluator.py", "_bound"),
)

#: The modules the registry covers. A call to :mod:`evals.odoo.judging.capture` anywhere in them belongs
#: to a listed consumer.
REGISTERED_MODULES = tuple(sorted({consumer.module for consumers in CONSUMERS.values()
                                   for consumer in consumers}))
