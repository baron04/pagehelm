# --------------------------------------------------------------------------------------
# Terminal reasons: ONE definition, shared by the runner that emits them and the analyser
# that scores them.
# --------------------------------------------------------------------------------------
from .common import SchemaError, _require

#: Reasons a run can end with a business verdict that counts towards the result.
SCORABLE_TERMINAL_REASONS = frozenset({
    "agent_finished",         # the agent declared itself done, whatever the evaluator says
    "step_budget",            # the agent used its whole step budget
    "wall_clock_budget",      # the run hit its wall-clock ceiling
    "token_budget",           # the run hit its input-token ceiling
    "action_policy_budget",   # repeated pre-dispatch policy refusals exhausted recovery
    "agent_refused",          # the agent stopped and said it could not proceed
    "needs_input",            # the task paused for one bounded user clarification
    "no_progress",            # an unchanged decision repeated after one recovery prompt
    "planning_failed",        # the source arm could not produce a valid contract
    "approval_denied",        # the authenticated user declined the proposed effect
    "approval_expired",       # the visible confirmation elapsed before dispatch
    "operation_not_confirmed",  # working the page was not allowed before the card expired
    "user_cancelled",         # the authenticated user stopped the active task
    "business_mutation_failed",  # a known-safe server write refusal was returned
    "business_mutation_blocked",  # an in-flight/unresolved business key refused dispatch
    "business_mutation_refused",  # an authoritative pre-dispatch business rule refused it
    "permission_denied",      # the actor's own authority refused the operation
})

#: Reasons that invalidate a run: apparatus, not behaviour. Never scored.
INVALIDATING_TERMINAL_REASONS = frozenset({
    "transport_error",
    "browser_crashed",
    "control_plane_error",
    "evaluator_error",
    "provider_error",
    "leak_detected",
    "approval_unavailable",   # the write-capable runner omitted its approval channel
    "approval_failed",        # the authenticated approval exchange failed before dispatch
    "observation_failed",     # a verified business effect survives a failed page follow-up
})

#: A write may or may not have landed. Never scored, never retried.
INDETERMINATE_TERMINAL_REASONS = frozenset({
    "indeterminate_write",
})

TERMINAL_REASONS = (
    SCORABLE_TERMINAL_REASONS | INVALIDATING_TERMINAL_REASONS | INDETERMINATE_TERMINAL_REASONS
)


def assert_terminal_reason_contract(emitted: set[str], scorable: set[str]) -> None:
    """Every reason the runner can emit must be classified by the analyser.

    Adding an emitted reason without adding it to the shared classification would make a
    completed run uninterpretable. This assertion rejects that protocol mismatch immediately.
    """
    unclassified = emitted - TERMINAL_REASONS
    _require(not unclassified,
             f"runner can emit terminal reasons the analyser does not classify: {sorted(unclassified)}")
    unreachable = scorable - emitted
    _require(not unreachable,
             f"analyser scores terminal reasons the runner can never emit: {sorted(unreachable)}")


def classify_terminal(reason: str) -> str:
    """-> 'scorable' | 'invalid' | 'indeterminate'"""
    if reason in SCORABLE_TERMINAL_REASONS:
        return "scorable"
    if reason in INVALIDATING_TERMINAL_REASONS:
        return "invalid"
    if reason in INDETERMINATE_TERMINAL_REASONS:
        return "indeterminate"
    raise SchemaError(f"unknown terminal reason {reason!r}")
