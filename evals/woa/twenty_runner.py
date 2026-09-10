"""One-process Web Operation Agent runner for Twenty evaluation cases.

The runner receives a public execution case and ordinary application capabilities. It runs
the real product path — identity binding, remote session channel, ``AgentTurnExecutor`` and
the shared Business Skills/runtime — and hands what that path produced to the engine-neutral
assembler. It never reads the database baseline, after-state, evaluator result, held-out task
set, or another run.
"""
from __future__ import annotations

import hmac
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from evals.twenty.evidence import EvidenceContractError, validate_execution_case
from evals.woa.assembler import (
    FinalPageSample, TurnEvidence, WoaRunAssemblyError, assemble_woa_run, utc_now,
)
from evals.woa.scripted import RecordingBusinessRuntime
from web_operation_agent.application import bind_application
from web_operation_agent.business import BusinessToolError
from web_operation_agent.embedded_agent import ReadOnlyControl
from web_operation_agent.identity import PrincipalError
from web_operation_agent.model import ChatPort, require_chat_port
from web_operation_agent.product import (
    AgentTurnConfig, AgentTurnError, AgentTurnExecution, AgentTurnExecutor,
)
from web_operation_agent.remote import (
    RemoteSessionChannel, RemoteSessionRegistry, SessionGrant,
)
from web_operation_agent.schemas import SourceMap
from web_operation_agent.twenty import TwentyApplicationAdapter


_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{2,159}\Z")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SKILL_DISABLED_BROWSER_ACTIONS = frozenset({"click", "fill", "select", "submit", "back"})
_BROWSER_DISABLED_ACTIONS = frozenset({"fill", "select", "submit", "back"})
_SKILL_READ_ONLY_SCOPE = (
    "Read current Twenty CRM company data and open only routes returned by the delegated "
    "application API. Do not modify records or submit browser forms."
)
_CREATE_SCOPE = (
    "Create exactly one Twenty CRM company with the user-supplied name and domain after a "
    "fresh exact-effect approval, then optionally open only its returned record route."
)
_BROWSER_READ_ONLY_SCOPE = (
    "Read only facts visible on the current Twenty CRM pages and open only exact links "
    "present on the current page. Do not use application APIs or modify records."
)
_SKILL_OUTPUT_CONTRACT = (
    "EVALUATION OUTPUT CONTRACT\n"
    "For a company-list goal, query once and return a brief explanation; the product projects "
    "the exact records, source and completeness from the typed ToolResult. Do not restate or "
    "extend the record set. For an open goal, finish only after the requested "
    "company name and domain are visible on the opened record page."
)
_BROWSER_OUTPUT_CONTRACT = (
    "EVALUATION OUTPUT CONTRACT\n"
    "Use only the current page and ordinary browser tools. For a company-list goal, answer "
    "with exactly one JSON object and no Markdown: "
    '{"artifact_version":1,"execution_status":"completed","complete":true,'
    '"companies":[{"name":"Company","domain":"company.example"}]}. Set complete true only '
    "when the visible page establishes the complete list. For an open goal, use an exact "
    "link visible on the current page and finish only after the requested company name and "
    "domain are visible on the opened record page."
)
_CREATE_OUTPUT_CONTRACT = (
    "EVALUATION OUTPUT CONTRACT\n"
    "For a create-company goal, load the create Skill and call create_company exactly once "
    "with the user-supplied name and domain. The server will pause for a fresh exact-effect "
    "approval. Never retry an uncertain or rejected write. If the user asked to open the "
    "record, use only the successful result's exact ui_path and re-observe it before finish."
)


class WoaTwentyRunnerError(WoaRunAssemblyError):
    """The Web Operation Agent runner configuration is invalid."""


class RuntimeHost(Protocol):
    """Drives the page for one run: posts observations and executes bounded actions."""

    def start(self) -> Any: ...

    def observe_now(self) -> Any: ...

    def stop(self) -> None: ...

    @property
    def current_url(self) -> str: ...


RuntimeFactory = Any


@dataclass(frozen=True, slots=True)
class WoaTwentyConfig:
    storage_state_path: Path
    entry_path: str = "/objects/companies"
    capability_profile: str = "skill_api"
    source_mode: str = "off"
    source_interface: str = "absent"
    allow_create: bool = False
    step_budget: int = 10
    max_page_actions: int = 10
    wall_time_s: int = 240
    transport_timeout_s: float = 60.0
    keep_observations: int = 3

    def __post_init__(self) -> None:
        object.__setattr__(self, "storage_state_path", Path(self.storage_state_path))
        parsed = urlsplit(self.entry_path)
        if (not self.entry_path.startswith("/") or self.entry_path.startswith("//")
                or parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
                or "\\" in self.entry_path or "%" in parsed.path
                or any(part == ".." for part in parsed.path.split("/"))
                or len(self.entry_path) > 1_000
                or re.search(r"[\x00-\x20\x7f]", self.entry_path)):
            raise WoaTwentyRunnerError("entry_path must be a relative application path")
        if self.capability_profile not in {"browser_only", "skill_api"}:
            raise WoaTwentyRunnerError("capability_profile is invalid")
        if self.source_mode not in {"off", "on"}:
            raise WoaTwentyRunnerError("source_mode is invalid")
        if self.source_interface not in {"absent", "paired"}:
            raise WoaTwentyRunnerError("source_interface is invalid")
        if self.capability_profile == "browser_only" and self.source_interface != "absent":
            raise WoaTwentyRunnerError("browser_only cannot expose a source interface")
        if self.source_mode == "on" and self.source_interface != "paired":
            raise WoaTwentyRunnerError("source_mode on requires the paired source interface")
        if not isinstance(self.allow_create, bool):
            raise WoaTwentyRunnerError("allow_create must be a boolean")
        if self.allow_create and self.capability_profile != "skill_api":
            raise WoaTwentyRunnerError("create requires the skill_api capability profile")
        for name, lower, upper in (
                ("step_budget", 1, 100), ("max_page_actions", 0, 200),
                ("wall_time_s", 1, 3_600), ("keep_observations", 1, 20)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise WoaTwentyRunnerError(f"{name} is invalid")
        timeout = self.transport_timeout_s
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 3_600):
            raise WoaTwentyRunnerError("transport_timeout_s is invalid")

    def verify_files(self) -> None:
        if not self.storage_state_path.is_file():
            raise WoaTwentyRunnerError("browser storage state is unavailable")


def turn_config(config: WoaTwentyConfig, ledger_path: Path) -> AgentTurnConfig:
    """Build the bounded turn policy this comparison arm runs under."""
    browser_only = config.capability_profile == "browser_only"
    return AgentTurnConfig(
        supported_scope=(
            _CREATE_SCOPE if config.allow_create else
            _BROWSER_READ_ONLY_SCOPE if browser_only else _SKILL_READ_ONLY_SCOPE),
        ledger_path=ledger_path,
        step_budget=config.step_budget,
        wall_budget_s=config.wall_time_s,
        transport_timeout_s=config.transport_timeout_s,
        disabled_actions=(
            _BROWSER_DISABLED_ACTIONS if browser_only else _SKILL_DISABLED_BROWSER_ACTIONS),
        scope_first_policy=True,
        policy_block_limit=1,
        plain_business_answer_min_page_actions=(None if browser_only else 0),
        keep_observations=config.keep_observations,
        allow_business_mutations=config.allow_create,
        require_identity=True,
        max_user_answer_chars=20_000,
        source_affordance_mode=(
            "auto" if config.source_mode == "on"
            else "empty" if config.source_interface == "paired" else "off"),
    )


def run_case(*, case: dict[str, Any], adapter: TwentyApplicationAdapter, client: ChatPort,
             config: WoaTwentyConfig, ledger_path: Path, runtime_factory: RuntimeFactory,
             page_sampler: Any = None, run_id: str | None = None,
             cancelled: threading.Event | None = None,
             source_map: SourceMap | None = None) -> dict[str, Any]:
    """Execute one query/open/create case through the product path and return a private artifact.

    ``runtime_factory(channel, grant, registry, origin, entry_path)`` supplies the page host
    for this run; ``page_sampler(runtime)`` returns the text a user can currently see on the
    final page. ``cancelled`` stops the run before the turn begins and is reported as the
    user's own scorable outcome. A task already in flight is stopped the way the assistant
    panel stops it, by cancelling that exact message through the authenticated channel.
    """
    selected = validate_execution_case(
        case, allowed_kinds=("query", "open", "create"))
    if selected["task_kind"] == "create" and not config.allow_create:
        raise WoaTwentyRunnerError("create case requires explicit allow_create configuration")
    if selected["task_kind"] != "create" and config.allow_create:
        raise WoaTwentyRunnerError("allow_create is restricted to a create case")
    config.verify_files()
    run_id = run_id or f"woa-{uuid.uuid4().hex}"
    if not isinstance(run_id, str) or not _ID.fullmatch(run_id) or not _RUN_ID.fullmatch(run_id):
        raise WoaTwentyRunnerError("run_id is invalid")
    if not isinstance(adapter, TwentyApplicationAdapter):
        raise WoaTwentyRunnerError("runner requires a TwentyApplicationAdapter")
    if config.source_mode == "on" and not isinstance(source_map, SourceMap):
        raise WoaTwentyRunnerError("source-on run requires a version-bound SourceMap")
    if config.source_mode == "off" and source_map is not None:
        raise WoaTwentyRunnerError("source-off run cannot receive a SourceMap")
    try:
        require_chat_port(client, role="runner client")
    except TypeError as exc:
        raise WoaTwentyRunnerError(str(exc)) from exc
    if not callable(getattr(getattr(client, "meter", None), "run_report", None)):
        raise WoaTwentyRunnerError("runner client meter must provide run_report")
    if cancelled is None:
        cancelled = threading.Event()
    elif not isinstance(cancelled, threading.Event):
        raise WoaTwentyRunnerError("cancelled must be a threading.Event or None")
    application = bind_application(adapter)
    skills = application.business_tools.relevant(selected["prompt"])
    if config.capability_profile == "skill_api":
        if not skills:
            raise WoaTwentyRunnerError("case did not select a Business Skill")
        if selected["task_kind"] == "create":
            if all(skill.read_only for skill in skills):
                raise WoaTwentyRunnerError("create case did not select a mutation Skill")
        elif any(not skill.read_only for skill in skills):
            raise WoaTwentyRunnerError("query/open case selected a mutation Skill")
    started_at = utc_now()
    started = time.monotonic()

    def artifact(evidence: TurnEvidence) -> dict[str, Any]:
        return assemble_woa_run(
            case=case, run_id=run_id, evidence=evidence,
            capability_profile=config.capability_profile, source_mode=config.source_mode,
            source_interface=config.source_interface,
            started_at=started_at, started_monotonic=started)

    if cancelled.is_set():
        return artifact(TurnEvidence(
            execution=None, cancelled=True, accounting=_accounting(client, run_id)))

    # Identity binding happens before any browser or model work. A binding failure is an
    # apparatus fault of the credential/identity class, not a result of the compared engine.
    identity_now = int(time.time() * 1_000)
    try:
        initial_binding = adapter.page_session_binding(
            config.storage_state_path, now_ms=identity_now)
        principal = adapter.bind_browser_session(
            config.storage_state_path, delegation_id=f"woa-{uuid.uuid4().hex}",
            lifetime_s=min(config.wall_time_s + 60, 3_600), now_ms=identity_now)
    except (BusinessToolError, PrincipalError, ValueError, OSError):
        return artifact(TurnEvidence(
            boundary_error_code="identity_binding",
            accounting=_accounting(client, run_id)))

    runtime_tools = (
        RecordingBusinessRuntime(application.business_tools)
        if config.capability_profile == "skill_api" else None)
    registry = RemoteSessionRegistry()
    grant, channel = registry.create(
        allowed_origins=[application.descriptor.origin],
        session_id=f"woa-eval-{uuid.uuid4().hex[:12]}", principal=principal)
    executor = AgentTurnExecutor(
        channel=channel, config=turn_config(config, ledger_path), chat=client,
        business_tools=runtime_tools, control=ReadOnlyControl(),
        source_map=source_map,
        run_id_factory=lambda _message: run_id)
    execution: AgentTurnExecution | None = None
    boundary_error_code: str | None = None
    final_page: FinalPageSample | None = None
    session_unchanged = False
    page_error_count = 0
    runtime: Any = None
    try:
        runtime = runtime_factory(
            channel=channel, grant=grant, registry=registry,
            origin=application.descriptor.origin, entry_path=config.entry_path)
        runtime.start()
        output_contract = (
            _CREATE_OUTPUT_CONTRACT if selected["task_kind"] == "create" else
            _BROWSER_OUTPUT_CONTRACT if config.capability_profile == "browser_only"
            else _SKILL_OUTPUT_CONTRACT)
        runtime.submit_task(selected["prompt"] + "\n\n" + output_contract)
        # The conversation host dequeues one message and hands it to the executor; the runner
        # does the same directly so a typed boundary failure is preserved for classification
        # instead of being flattened into display text.
        message = channel.wait_for_message(timeout_s=config.wall_time_s)
        try:
            execution = executor.execute(message)
        except AgentTurnError as exc:
            boundary_error_code = exc.code.value
        finally:
            channel.complete_message(message["message_id"])
        sample_final_page = (
            selected["task_kind"] == "open"
            or (selected["task_kind"] == "create"
                and selected["evaluator_task"]["require_open"] is True))
        if execution is not None and sample_final_page:
            try:
                observation = channel.latest_observation_envelope
                if observation is None:
                    raise EvidenceContractError("no final observation was recorded")
                final_page = FinalPageSample(
                    document_url=runtime.current_url,
                    observation_url=observation.observation.url,
                    observation_id=observation.observation_id,
                    observation_digest=observation.observation.content_digest,
                    visible_projection=(page_sampler(runtime) if page_sampler is not None
                                        else observation.observation.content))
            except EvidenceContractError:
                final_page = None
        page_error_count = len(getattr(runtime, "errors", ()) or ())
        try:
            current_binding = adapter.page_session_binding(runtime.storage_state())
            session_unchanged = hmac.compare_digest(initial_binding, current_binding)
        except AttributeError:
            # A runtime host that cannot re-read browser state cannot weaken the check; the
            # trusted host is responsible for supplying one that can.
            session_unchanged = False
        except Exception:
            session_unchanged = False
    except Exception:
        boundary_error_code = boundary_error_code or "runtime_unavailable"
    finally:
        if runtime is not None:
            try:
                runtime.stop()
            except Exception:
                pass
        registry.retire(grant.session_id, "run_finished")
    return artifact(TurnEvidence(
        execution=execution, boundary_error_code=boundary_error_code,
        ledger_events=tuple(_ledger_events(ledger_path, run_id)),
        accounting=_accounting(client, run_id),
        business_results=tuple(runtime_tools.results if runtime_tools is not None else ()),
        final_page=final_page, session_binding_unchanged=session_unchanged,
        principal_bound=True, cancelled=cancelled.is_set(),
        page_error_count=page_error_count))


def _accounting(client: ChatPort, run_id: str) -> dict[str, Any] | None:
    try:
        report = client.meter.run_report(run_id)
    except Exception:
        return None
    return report if isinstance(report, dict) else None


def _ledger_events(ledger_path: Path, run_id: str) -> list[dict[str, Any]]:
    """Read the private audit events this run appended, without failing the run on I/O."""
    import json

    try:
        lines = Path(ledger_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("run_id") == run_id:
            events.append(value)
    return events


__all__ = [
    "RuntimeHost", "WoaTwentyConfig", "WoaTwentyRunnerError", "run_case", "turn_config",
]
