"""The values one run carries: its public ends, its lab settings, its step state.

Turn state was `_run`'s local-variable list; a step handler takes these values instead
of re-declaring the locals, so a handler can live outside the loop (P7.5 D-01).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..schemas import OperationContract, PageObservation


#: What a verified capability gap ends the turn as. The claim itself lives in the
#: `capability_gap` conclusion and its CapabilityEvidence, which is what a reader checks; this
#: is only the terminal name, and it stays inside the evaluation protocol's frozen product
#: vocabulary. A distinct name would be better English and would currently be scored as
#: classification drift, so changing it means changing the apparatus first -- one line, here.
CAPABILITY_GAP_TERMINAL = "agent_refused"


@dataclass(frozen=True)
class _StepEnd:
    """How one refused step ended: a terminal reason, or a fresh page and what to say.

    Exactly one of the two is set. Making that a value rather than a tuple of four is what
    lets `_run` treat every pre-dispatch refusal identically: terminate, or continue with
    the page the refusal just obtained.
    """

    reason: str = ""
    detail: str = ""
    observation: "PageObservation | None" = None
    feedback: str = ""

    @property
    def terminal(self) -> bool:
        return bool(self.reason)


@dataclass(frozen=True)
class LabApparatus:
    """Settings that exist only so a comparison harness can define its arms.

    Keeping them as four independent keywords on a constructor that a deployment also calls
    was the problem: `oracle_contract` injects a known-good plan, which is hidden expected
    state AGENTS.md forbids in an agent's context, and it sat in the signature between a
    budget and an approval callback with nothing to mark it apart. As one value the boundary
    is in the signature rather than in a docstring, and `product.py` passing no `lab=` at all
    is a structural fact a test can assert.

    `arm` is only a label: the session does not branch on it. It rides here because naming
    an arm is a lab's business, and a product turn should not have to invent one.
    """

    arm: str = "U"
    #: Plan from the task and the page, with no source evidence -- the control arm.
    plan_from_page: bool = False
    #: A known-good contract handed to the run. Hidden expected state; never a product path.
    oracle_contract: "OperationContract | None" = None
    #: Query-expansion cache shared across the trials of one source-contract arm.
    terms_cache: object = None

    def __post_init__(self) -> None:
        if not isinstance(self.arm, str) or not self.arm.strip() or len(self.arm) > 64:
            raise ValueError("arm must be a short non-empty label")
        if not isinstance(self.plan_from_page, bool):
            raise ValueError("plan_from_page must be a boolean")
        if self.oracle_contract is not None and not isinstance(
                self.oracle_contract, OperationContract):
            raise TypeError("oracle_contract must be an OperationContract or None")


#: What a session gets when nobody is running an experiment.
NO_LAB = LabApparatus()


@dataclass
class TaskSpec:
    """The public description of the work. Everything here may be shown to the model.

    `supported_scope` states which part of the application this run is authorised to change.
    The boundary is part of the public task and must be identical across planning modes. It
    describes permitted effects rather than revealing a route; `assert_scope_is_not_the_answer`
    enforces that distinction.
    """
    task_ref: str
    prompt: str
    entry_path: str = "/"
    supported_scope: str = ""

    def public_text(self) -> str:
        """Everything the task says, for any component that reasons about it."""
        if self.supported_scope:
            return f"{self.prompt}\n\nWhat this run may change: {self.supported_scope}"
        return self.prompt


@dataclass
class RunResult:
    run_id: str
    task_ref: str
    arm: str
    terminal_reason: str
    steps_used: int = 0
    wall_seconds: float = 0.0
    agent_said: str = ""
    contract_digest: str = ""
    contract_source: str = "none"
    citations: int = 0
    contract_conflicts: int = 0
    page_actions: int = 0
    control: dict = field(default_factory=dict)
    business_control: dict = field(default_factory=dict)
    spend: dict = field(default_factory=dict)
    plan_status: dict = field(default_factory=dict)
    plan_withdrawn: bool = False
    write_refusals: int = 0
    #: Whether this run asked the person to let it work the page, and how that ended. Kept
    #: apart from `business_control`, whose approval counts mean writes. None where no person
    #: could be asked, so such a run serialises as it always did.
    operation_control: dict | None = None
    #: What the run can say about the agent's own claim to have finished: `not_claimed`,
    #: `unverified` (no runtime evidence either way) or `disconfirmed` (the enforcement point
    #: refused every write and permitted none). A deterministic same-actor business verifier
    #: is not implemented yet; permission counts cannot establish `confirmed`.
    success_claim: str = ""
    challenged_false_success: bool = False
    #: Model-selected actions refused by a server policy before the browser saw a command.
    #: This is separate from gateway write refusals and from actions the browser executed.
    policy_blocks: int = 0
    #: True only when the loop ended immediately after a successful typed business read,
    #: either with plain content or ``finish(done=true)``. Product rendering may then replace
    #: factual prose with the application-supplied structured query projection. A later page
    #: action does not qualify.
    business_answer_projected: bool = False
    verified_answer: dict | None = None
    #: A bounded user-visible question that pauses this task without keeping a worker or
    #: approval alive. It contains no capability and the next ordinary message may answer
    #: or replace it.
    clarification: dict | None = None
    #: Bounded machine classification for a provider terminal. Provider response bodies and
    #: exception prose remain outside the product result.
    provider_error_code: str | None = None
    detail: str = ""
    #: The record a pre-dispatch business rule refused, for the sentence that reports it.
    #: None when nothing was refused, so a run without a refusal serialises as it always did.
    refusal_subject: str | None = None
    #: What the application refused this actor, in the reader's language, when the adapter
    #: names its tools. "Your account is not permitted to perform this operation" leaves the
    #: reader guessing which half of "the phone and the orders" was refused.
    refused_operation: str | None = None
    #: Content-free per-turn request timing and accounting, when measured by the host.
    model_receipt: dict | None = None
    #: Structured clarification quick-options for `needs_input` turns (UX-02).
    input_options: list | None = None
    #: Structured form-handoff outcome payload for terminal progress events (UX-05).
    handoff_payload: dict | None = None

    def to_json(self) -> dict:
        optional = {"verified_answer", "clarification", "provider_error_code",
                    "refusal_subject", "refused_operation", "operation_control", "model_receipt",
                    "input_options", "handoff_payload"}
        value = {k: getattr(self, k) for k in self.__dataclass_fields__
                 if k not in optional or getattr(self, k) is not None}
        if self.clarification is not None:
            value["clarification"] = {
                key: self.clarification[key] for key in (
                    "schema_version", "clarification_id", "task_id", "response_kind")
            }
            value["clarification"]["choice_count"] = len(
                self.clarification.get("choices") or ())
        return value


@dataclass
class _Turn:
    """One run of the step loop, in the state a step may read and change.

    These were local variables of `_run`, which is why every tool had to be handled inside
    it. A step handler takes this instead, so each tool can live next to what it means
    rather than in a 1400-line method with the others (P7.5 D-01).
    """

    result: RunResult
    started_at: float
    messages: list[dict]
    obs: PageObservation | None
    step: int = 0
    #: Business tools that answered. A turn that read through the typed API is held to a
    #: different standard when it claims to be done.
    successful_business_tools: int = 0
    #: A plain reply the model gave before this call, which a `finish` turns into the answer.
    kept_reply: str = ""


@dataclass
class _Next:
    """What the loop does after a step: keep going, or finish with this result.

    A handler answers `None` when the call was not its tool.
    """

    finished: RunResult | None = None
