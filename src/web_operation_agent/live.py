"""Product composition for one live, first-party embedded Agent session.

Application adapters supply a runner and decide how their page includes the returned
bootstrap script.  This module owns the common registry, capability grant, loopback gateway,
server worker lifecycle and optional replay projection.  It has no knowledge of OFBiz, lab
tasks, hidden evaluator data or browser automation.
"""
from __future__ import annotations

import queue
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, cast

from .embedded_gateway import EmbeddedGateway
from .identity import PrincipalContext
from .privacy import redact_sensitive_assignments, redact_session_handoffs
from .product import AgentTurnError
from .remote import RemoteSessionChannel, RemoteSessionClosed, RemoteSessionRegistry
from .traceview import build_trace_case, render_trace_html, write_trace_bundle


class ProductRunner(Protocol):
    """Compatibility contract for a complete product-session worker."""

    def run_once(self): ...


class AgentTurnExecutor(Protocol):
    """Execute one already-dequeued wire message without owning the session loop."""

    def execute(self, message: dict, /) -> object: ...


class _LegacyTurnRunner(Protocol):
    def run_once(self, *, message: dict) -> object: ...


LegacyTurnRunnerFactory = Callable[[dict, int], _LegacyTurnRunner]


@dataclass(frozen=True)
class LiveTurnOutcome:
    turn: int
    result: object | None
    task: str
    error: str = ""


class LiveConversationRunner:
    """Host sequential panel turns and isolate one turn's failure from the next.

    New product integrations supply one ``AgentTurnExecutor`` instance for the whole
    conversation.  The optional factory path is a compatibility shim for older demos
    that still construct a runner per turn.
    """

    def __init__(self, channel: RemoteSessionChannel,
                 executor: AgentTurnExecutor | LegacyTurnRunnerFactory | None = None,
                 *, runner_factory: LegacyTurnRunnerFactory | None = None):
        if executor is not None and runner_factory is not None:
            raise TypeError("supply executor or runner_factory, not both")
        selected = executor if executor is not None else runner_factory
        execute = getattr(selected, "execute", None)
        if callable(execute):
            self.executor: AgentTurnExecutor | None = cast(AgentTurnExecutor, selected)
            self._legacy_runner_factory: LegacyTurnRunnerFactory | None = None
        elif callable(selected):
            # Preserve the former positional ``runner_factory`` API while keeping the
            # turn counter and message dequeue in this host.
            self.executor = None
            self._legacy_runner_factory = cast(LegacyTurnRunnerFactory, selected)
        else:
            raise TypeError("an AgentTurnExecutor is required")
        self.channel = channel
        self._outcomes: queue.Queue[LiveTurnOutcome] = queue.Queue()
        self.turns_completed = 0

    def _execute_turn(self, message: dict, turn: int) -> object:
        if self.executor is not None:
            return self.executor.execute(message)
        assert self._legacy_runner_factory is not None
        runner = self._legacy_runner_factory(message, turn)
        return runner.run_once(message=message)

    def _record(self, outcome: LiveTurnOutcome) -> None:
        self.turns_completed = outcome.turn
        self._outcomes.put(outcome)

    def run(self) -> tuple[LiveTurnOutcome, ...]:
        """Run until the remote session closes; never retry an individual turn."""
        outcomes: list[LiveTurnOutcome] = []
        while True:
            try:
                message = self.channel.wait_for_message(timeout_s=None)
            except RemoteSessionClosed:
                return tuple(outcomes)
            turn = self.turns_completed + 1
            message_id = str(message.get("message_id") or "")
            try:
                try:
                    result = self._execute_turn(message, turn)
                    outcome = LiveTurnOutcome(
                        turn=turn, result=result, task=str(message.get("text") or ""))
                except RemoteSessionClosed:
                    # Closing the panel/session is normal lifecycle completion, including
                    # when it interrupts an in-flight executor.
                    return tuple(outcomes)
                except Exception as exc:
                    detail = redact_sensitive_assignments(redact_session_handoffs(
                        f"{type(exc).__name__}: {exc}"))[:800]
                    outcome = LiveTurnOutcome(
                        turn=turn, result=None, task=str(message.get("text") or ""),
                        error=detail)
                    outcomes.append(outcome)
                    self._record(outcome)
                    if isinstance(exc, AgentTurnError):
                        error_code = exc.code.value
                        retryable = exc.retryable
                        user_message = {
                            "observation_unavailable": "页面状态暂不可用，请稍后重新提交。",
                            "stale_message": "页面已发生变化，请检查当前页面后重新提交。",
                            "runtime_unavailable": "页面连接暂不可用，请稍后重试。",
                            "invalid_message": "请求格式无效，请重新输入。",
                        }.get(error_code, "本次任务执行失败，请查看运行记录。")
                    else:
                        error_code = "unexpected_error"
                        retryable = False
                        user_message = "本次任务执行失败，请查看运行记录。"
                    try:
                        self.channel.publish("progress", {
                            "state": "task_failed",
                            "message": user_message,
                            "error_code": error_code,
                            "retryable": retryable,
                        })
                    except RemoteSessionClosed:
                        # The outcome is already durable to this host.  The next dequeue
                        # observes closure after any messages already accepted by the channel.
                        pass
                    continue
                outcomes.append(outcome)
                self._record(outcome)
            finally:
                self.channel.complete_message(message_id)

    def run_once(self) -> tuple[LiveTurnOutcome, ...]:
        """Compatibility alias for callers that treated the session loop as one run."""
        return self.run()

    def close(self, reason: str = "host_stopped") -> None:
        """Close the channel owned by this conversation host."""
        self.channel.close(reason)

    def next_outcome(self, timeout_s: float | None = None) -> LiveTurnOutcome:
        try:
            return self._outcomes.get(timeout=timeout_s)
        except queue.Empty as exc:
            raise TimeoutError("timed out waiting for a completed conversation turn") from exc

    def outcomes_nowait(self) -> tuple[LiveTurnOutcome, ...]:
        found: list[LiveTurnOutcome] = []
        while True:
            try:
                found.append(self._outcomes.get_nowait())
            except queue.Empty:
                return tuple(found)


@dataclass(frozen=True)
class LiveSessionStatus:
    session_id: str
    gateway_url: str
    running: bool
    finished: bool
    failed: bool


class LiveProductSession:
    """Own one browser capability and one server-side product runner lifecycle."""

    def __init__(self, *, allowed_origin: str, page_controller_module: str,
                 runner_factory: Callable[[RemoteSessionChannel], ProductRunner],
                 session_id: str | None = None, sse_wait_s: float = .2,
                 navigation_mode: str = "document",
                 principal: PrincipalContext | None = None):
        self.registry = RemoteSessionRegistry()
        self.gateway = EmbeddedGateway(self.registry, sse_wait_s=sse_wait_s)
        self.grant, self.channel = self.registry.create(
            allowed_origins=[allowed_origin],
            session_id=session_id or f"live-{uuid.uuid4().hex}",
            principal=principal,
        )
        self.page_controller_module = page_controller_module
        if navigation_mode not in {"document", "spa"}:
            raise ValueError("navigation_mode must be document or spa")
        self.navigation_mode = navigation_mode
        self.runner = runner_factory(self.channel)
        self._thread: threading.Thread | None = None
        self._result = None
        self._error: BaseException | None = None

    @property
    def bootstrap_script(self) -> str:
        if self._thread is None:
            raise RuntimeError("live session must be started before requesting bootstrap")
        return self.gateway.bootstrap_script(
            self.grant, page_controller_module=self.page_controller_module,
            navigation_mode=self.navigation_mode)

    def start(self) -> "LiveProductSession":
        if self._thread is not None:
            return self
        self.gateway.start()

        def target() -> None:
            try:
                self._result = self.runner.run_once()
            except BaseException as exc:
                self._error = exc

        self._thread = threading.Thread(
            target=target, name=f"{self.grant.session_id}-server-agent", daemon=True)
        self._thread.start()
        return self

    def wait(self, timeout_s: float | None = None):
        if self._thread is None:
            raise RuntimeError("live session has not been started")
        self._thread.join(timeout=timeout_s)
        if self._thread.is_alive():
            raise TimeoutError("live product session did not finish before its outer deadline")
        if self._error is not None:
            raise RuntimeError(
                f"live product worker failed: {type(self._error).__name__}: {self._error}") \
                from self._error
        return self._result

    def status(self) -> LiveSessionStatus:
        thread = self._thread
        return LiveSessionStatus(
            session_id=self.grant.session_id,
            gateway_url=self.gateway.base_url,
            running=bool(thread and thread.is_alive()),
            finished=bool(thread and not thread.is_alive() and self._error is None),
            failed=self._error is not None,
        )

    def close(self) -> None:
        self.channel.close("host_stopped")
        self.gateway.close()

    def __enter__(self) -> "LiveProductSession":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()


def render_live_replay(*, result_path: str | Path, ledger_path: str | Path,
                       output_path: str | Path, label: str, mode: str,
                       case_id: str = "live") -> dict:
    """Project one completed live run into the existing privacy-safe replay."""
    case = build_trace_case(
        case_id=case_id, label=label, mode=mode,
        result_path=Path(result_path), ledger_path=Path(ledger_path))
    output = Path(output_path)
    bundle_path = output.with_name("trace-data.json")
    bundle = write_trace_bundle([case], bundle_path)
    render_trace_html(bundle, output)
    return bundle
