"""Which code reads each fallible observation, and what it decides with it.

The rule this apparatus turns on is that a source nobody could read is a different fact from a
source read as empty. The rule holds only where every consumer of a source applies it, so the
consumers are written down here rather than left to be found. The registry is data: the tests
check the code against it, and a reviewer reads it to follow a source to the decisions it
reaches without first having to find them.

A function that reads one of :data:`evals.odoo.capture.SOURCES` and is not listed here is a
consumer nobody checked; :mod:`evals.odoo.tests.test_consumers` refuses one.
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
        Consumer("evals/odoo/runner_woa.py", "empty_artifact",
                 "the record shape the product's own arm reports, and what an attempt that "
                 "never started reports"),
        Consumer("evals/odoo/runner_pageagent.py", "empty_artifact",
                 "the same, for the browser-only arm"),
        Consumer("evals/odoo/driver.py", "Driver.run_attempt",
                 "whether the product started, which turns were recorded and how they ended, "
                 "the attempt's times, and which apparatus fault the run met"),
        Consumer("evals/odoo/driver.py", "_final_page",
                 "whether a final page was captured at all, or why it was not"),
        Consumer("evals/odoo/trace.py", "project_pageagent",
                 "the browser-only arm's trace, whose counts exist only if its bridge reported "
                 "them"),
        Consumer("evals/odoo/evaluator.py", "_turns",
                 "the turns a judgement is read from; a missing record stops the judgement"),
        Consumer("evals/odoo/evaluator.py", "_text",
                 "the answer a turn showed, where a turn nobody read an answer from is not an "
                 "empty answer"),
        Consumer("evals/odoo/evaluator.py", "_judge_write",
                 "the approval decisions the oracle made, which carry their own safety events"),
    ),
    "final_page": (
        Consumer("evals/odoo/driver.py", "_final_page", "the capture the evaluator reads"),
        Consumer("evals/odoo/evaluator.py", "_page_path",
                 "the path the run left behind, or nothing where no page was observed"),
        Consumer("evals/odoo/evaluator.py", "_on_page",
                 "whether the task's record is the one on screen; an unread page fails as "
                 "unobserved and names no turn"),
        Consumer("evals/odoo/evaluator.py", "judge",
                 "whether a failure read from the page belongs to the last turn"),
    ),
    "agent_audit": (
        Consumer("evals/odoo/trace.py", "project_woa",
                 "the product's endings, counts, approvals and furthest dispatch"),
        Consumer("evals/odoo/driver.py", "Driver.run_attempt",
                 "the dispatch state, the per-turn endings, the trace counts, and whether the "
                 "fixture must be restored because nobody read them"),
        Consumer("evals/odoo/driver.py", "Driver.recover_open_attempt",
                 "the same for an attempt a dead driver left open"),
        Consumer("evals/odoo/driver.py", "agent_assertions",
                 "which proposals the audit closes, so residue is not read from silence"),
        Consumer("evals/odoo/oracle.py", "ApprovalOracle.decide",
                 "whether the card matches what the product proposed; a proposal nobody read "
                 "is never approved"),
        Consumer("evals/odoo/evaluator.py", "_no_changes",
                 "whether a read-only task dispatched anything; an unread audit is no safety "
                 "event"),
        Consumer("evals/odoo/evaluator.py", "_judge_write",
                 "whether the write was dispatched and approved exactly once"),
        Consumer("evals/odoo/evaluator.py", "judge",
                 "whether a refusal task's denial was confirmed"),
    ),
    "agent_state": (
        Consumer("evals/odoo/driver.py", "Driver.run_attempt",
                 "the Agent's state after the run, which the reset asserts against"),
        Consumer("evals/odoo/driver.py", "Driver.recover_open_attempt",
                 "the same after an orphaned session is ended"),
        Consumer("evals/odoo/driver.py", "agent_assertions",
                 "the residue an attempt leaves; an unread state never passes a reset"),
    ),
    "fixture_guard": (
        Consumer("evals/odoo/driver.py", "Driver.run_attempt",
                 "what the run changed, and whether the fixture must be restored because "
                 "nobody could read it"),
        Consumer("evals/odoo/driver.py", "Driver._reset",
                 "whether a verified restore is required"),
        Consumer("evals/odoo/evaluator.py", "_no_changes",
                 "whether a read-only task changed the fixture"),
        Consumer("evals/odoo/evaluator.py", "_judge_write",
                 "whether a write touched anything outside its target"),
    ),
    "evaluator_output": (
        Consumer("evals/odoo/driver.py", "Driver._evaluator",
                 "the truth and the judgement, or that the evaluator process produced neither"),
        Consumer("evals/odoo/driver.py", "Driver.run_attempt",
                 "the product outcome, and whether a judgement was reached at all"),
    ),
    "provider_settlement": (
        Consumer("evals/odoo/gateway.py", "ProviderGateway.close_attempt",
                 "which of the attempt's calls settled, and which are written down as "
                 "unresolved"),
        Consumer("evals/odoo/gateway.py", "ProviderGateway._record_locked",
                 "whether an answer still settles its call, or arrives after it was written "
                 "down as unresolved"),
        Consumer("evals/odoo/ledger.py", "_Replay.model_unresolved",
                 "that a call settled exactly once, and that an attempt ends only when all of "
                 "its calls have"),
        Consumer("evals/odoo/driver.py", "_cost",
                 "the attempt's cost, where a call with no answer is usage unavailable"),
        Consumer("evals/odoo/formal.py", "unaccounted_calls",
                 "which calls the bill must account for before any later paid call"),
    ),
}

#: Functions that turn a record into a capture and decide nothing themselves. They read every
#: component alike, so they are listed once rather than under each source.
SHARED_READERS = (
    ("evals/odoo/evaluator.py", "_read"),
    ("evals/odoo/evaluator.py", "_bound"),
)

#: The modules the registry covers. A call to :mod:`evals.odoo.capture` anywhere in them belongs
#: to a listed consumer.
REGISTERED_MODULES = tuple(sorted({consumer.module for consumers in CONSUMERS.values()
                                   for consumer in consumers}))
