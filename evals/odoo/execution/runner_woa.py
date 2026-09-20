"""Real-Chromium runner for the WOA product arm on the ordinary Odoo entry.

The runner does what a person does: signs in on Odoo's own login page, lets the addon mount
the assistant, opens the panel from its launcher, types each message and presses Enter,
reads the confirmation card and presses a button, and finally logs out through Odoo's user
menu. A card that asks before the assistant leaves the page is confirmed, as the person who
asked for that page would; it is recorded apart from write approvals and never reaches the
write oracle. It never injects a Runtime, never calls the Agent directly and never reads panel state
from JavaScript objects: every answer it records is the text a person sees in the default,
unexpanded view of the newest turn.

It receives a ``RunnerInput`` -- messages, entry path, actor -- and an approval hook, never
the task's evaluator specification. Capabilities are the actor's delegation, fixed by the
deployment before the run.

A turn runs at most ``turn_timeout_s`` and the whole attempt at most ``attempt_wall_s`` of
automatic time; time spent on a confirmation card is charged to neither. At a limit the runner
presses the panel's stop button, as a person would, times how long the panel takes to show that
it is stopping, and sends no further message. A turn the product did not finish in time is the
product's result to score, never a failure of the control channel.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.odoo.domain.tasks import RunnerInput

_ROOT = "#woa-assistant"
_TURNS = "li.woa-turn"
_SETTLED_JS = """([before]) => {
  const host = document.querySelector('#woa-assistant');
  const s = host && host.shadowRoot;
  if (!s) return null;
  if (s.querySelector('section.woa-approval')) return 'approval';
  const turns = s.querySelectorAll('li.woa-turn');
  if (turns.length <= before) return null;
  const last = turns[turns.length - 1];
  const outcome = last.querySelector(':scope .woa-agent > .woa-md, :scope .woa-agent > .woa-outcome');
  if (last.dataset.settled === 'true' && outcome && !s.querySelector('.woa-md__caret')) return 'settled';
  return null;
}"""
_ANSWER_JS = """() => {
  const s = document.querySelector('#woa-assistant').shadowRoot;
  const turns = s.querySelectorAll('li.woa-turn');
  const agent = turns[turns.length - 1].querySelector('.woa-agent');
  const plain = agent.querySelector(':scope > .woa-md');
  if (plain) return {kind: 'plain', text: plain.innerText};
  const card = agent.querySelector(':scope > .woa-outcome');
  return {kind: card ? card.dataset.kind : 'missing', text: card ? card.innerText : ''};
}"""
_APPROVAL_JS = """() => {
  const s = document.querySelector('#woa-assistant').shadowRoot;
  const card = s.querySelector('section.woa-approval');
  if (!card) return null;
  const effect = card.querySelector('.woa-approval__effect');
  return {kind: card.dataset.kind || 'write', effect: effect ? effect.innerText : '',
          targets: Array.from(card.querySelectorAll('.woa-approval__targets li')).map(x => x.innerText)};
}"""
_CARD_GONE_JS = ("() => !document.querySelector('#woa-assistant').shadowRoot"
                 ".querySelector('section.woa-approval')")
_FEEDBACK_JS = """([before]) => {
  const s = document.querySelector('#woa-assistant').shadowRoot;
  return Boolean(s.querySelector('.woa-pending')) || s.querySelectorAll('li.woa-turn').length > before;
}"""
_STOP_BUTTON = "button.woa-send[data-stop]"
_STOP_FEEDBACK_JS = """() => {
  const s = document.querySelector('#woa-assistant').shadowRoot;
  const stop = s.querySelector('button.woa-send[data-stop]');
  const panel = s.querySelector('section.woa-window');
  return !stop || stop.disabled || Boolean(panel && panel.dataset.state === 'stopping');
}"""
_STOPPED_JS = """() => {
  const s = document.querySelector('#woa-assistant').shadowRoot;
  return !s.querySelector('button.woa-send[data-stop]');
}"""


class RunnerPreStartError(RuntimeError):
    """The product could not be reached for reasons outside it, before it started."""

    def __init__(self, code: str, evidence: str):
        super().__init__(code)
        self.code = code
        self.evidence = evidence


@dataclass(frozen=True)
class ActorCredential:
    login: str
    password_file: Path


@dataclass(frozen=True)
class WoaRunnerConfig:
    base_url: str
    actors: dict[str, ActorCredential]
    evidence_dir: Path
    turn_timeout_s: float = 150
    #: Automatic time the whole attempt may take; confirmation waits are not charged to it.
    attempt_wall_s: float = 300
    approval_visible_wait_ms: int = 700
    #: How long a navigated page may take to render its view after the turn ends. A page
    #: still showing the old view afterwards is recorded as it is, for the evaluator to judge.
    view_settle_s: float = 15
    #: How long a turn the runner stopped may take to wind down before the attempt ends.
    stop_settle_s: float = 30
    viewport: tuple[int, int] = (1440, 1000)
    headless: bool = True
    chromium: Any = field(default=None, compare=False)


def login(page: Any, config: Any, actor: str) -> None:
    credential = config.actors[actor]
    password = Path(credential.password_file).read_text(encoding="utf-8").strip()
    page.goto(config.base_url + "/web/login?db=woa_trial", wait_until="domcontentloaded",
              timeout=60_000)
    page.locator("input[name=login]").fill(credential.login)
    page.locator("input[name=password]").fill(password)
    page.locator("button[type=submit]").click()
    page.wait_for_url("**/odoo**", timeout=60_000)


def logout(page: Any) -> bool:
    """Log out through Odoo's own user menu, as a person would."""
    try:
        page.locator(".o_user_menu button, .o_user_menu .dropdown-toggle").first.click(
            timeout=15_000)
        page.locator("[data-menu='logout']").first.click(timeout=15_000)
        page.wait_for_url("**/web/login**", timeout=30_000)
        return True
    except Exception:
        return False


def breadcrumb(page: Any) -> dict[str, str]:
    return page.evaluate("""() => ({
        path: location.pathname,
        breadcrumb: (document.querySelector('.o_breadcrumb') || {}).innerText || '',
        title: document.title || ''})""")


def settle_view(page: Any, before: dict[str, str], timeout_s: float) -> int | None:
    """Wait, within a bound, for a navigated Odoo page to render its new view.

    Returns the milliseconds it took, 0 when the path did not change, or None when the view
    still showed its previous breadcrumb and title at the bound. Nothing is retried or
    clicked: the page is observed exactly as a person would find it.
    """
    started = time.monotonic()
    if page.evaluate("() => location.pathname") == before["path"]:
        return 0
    deadline = started + timeout_s
    while time.monotonic() < deadline:
        current = breadcrumb(page)
        if (current["breadcrumb"] and current["breadcrumb"] != before["breadcrumb"]) or (
                current["title"] and current["title"] != before["title"]):
            try:
                page.wait_for_load_state("networkidle", timeout=3_000)
            except Exception:
                pass
            return int((time.monotonic() - started) * 1000)
        page.wait_for_timeout(250)
    return None


def turn_budget(*, turn_timeout_s: float, attempt_wall_s: float,
                automatic_used_s: float) -> tuple[float, str]:
    """How much automatic time the next turn may use, and which limit that is."""
    remaining = attempt_wall_s - automatic_used_s
    if remaining < turn_timeout_s:
        return max(0.0, remaining), "attempt_automatic_wall"
    return float(turn_timeout_s), "turn_wall"


def stop_turn(page: Any, config: WoaRunnerConfig) -> dict[str, Any]:
    """Stop the running turn with the panel's stop button and time the panel's feedback.

    The time runs from just before the click to the first frame in which the panel shows that
    it is stopping or has stopped, so it is never shorter than what a person would see.
    """
    from playwright.sync_api import Error as PlaywrightError

    feedback_ms = None
    try:
        clicked = time.monotonic()
        page.locator(_ROOT).locator(_STOP_BUTTON).click(timeout=5_000)
        page.wait_for_function(_STOP_FEEDBACK_JS, polling="raf", timeout=5_000)
        feedback_ms = int((time.monotonic() - clicked) * 1000)
    except PlaywrightError:
        pass
    try:
        page.wait_for_function(_STOPPED_JS, polling=100,
                               timeout=int(config.stop_settle_s * 1000))
        stopped = True
    except PlaywrightError:
        stopped = False
    return {"cancel_feedback_ms": feedback_ms, "stopped": stopped}


def run(runner_input: RunnerInput, config: WoaRunnerConfig, *,
        approval_hook: Callable[[dict[str, Any]], dict[str, Any]] | None,
        checkpoint: Callable[[dict[str, Any]], None], playwright: Any) -> dict[str, Any]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    artifact: dict[str, Any] = empty_artifact(runner_input)
    # A fault while the product runs costs the runner its sight of the product. Once every turn has
    # ended, reading the final page and tearing down are recorded apart: they show nothing about
    # how the product ended.
    stage = ["run"]
    browser = playwright.chromium.launch(headless=config.headless)
    try:
        context = browser.new_context(ignore_https_errors=True,
                                      viewport={"width": config.viewport[0],
                                                "height": config.viewport[1]})
        page = context.new_page()
        page.on("pageerror", lambda error: artifact["page_errors"].append(type(error).__name__))
        page.on("crash", lambda _page: stage[0] == "run" and artifact.update(
            browser_exit="page_crashed"))
        try:
            login(page, config, runner_input.actor)
            if runner_input.entry_path:
                page.goto(config.base_url + runner_input.entry_path,
                          wait_until="domcontentloaded", timeout=60_000)
            root = page.locator(_ROOT)
            root.locator(".woa-launcher").click(timeout=60_000)
            root.locator("textarea").wait_for(timeout=30_000)
        except Exception as exc:
            raise RunnerPreStartError("credential_or_identity_before_start",
                                      "pre_start_check") from exc
        automatic_used = 0.0
        for index, message in enumerate(runner_input.messages):
            budget_s, limit = turn_budget(turn_timeout_s=config.turn_timeout_s,
                                          attempt_wall_s=config.attempt_wall_s,
                                          automatic_used_s=automatic_used)
            if budget_s <= 0:
                artifact["budget_exhausted"] = limit
                break
            before = page.locator(_ROOT).locator(_TURNS).count()
            view_before = breadcrumb(page)
            box = page.locator(_ROOT).locator("textarea")
            box.fill(message)
            submitted = time.monotonic()
            box.press("Enter")
            artifact["product_started"] = True
            checkpoint({"product_started": True, "turn": index})
            feedback_ms = None
            try:
                page.wait_for_function(_FEEDBACK_JS, arg=[before], polling="raf", timeout=5_000)
                feedback_ms = int((time.monotonic() - submitted) * 1000)
            except Exception:
                feedback_ms = None
            approval_wait = 0.0
            state = None
            while True:
                # A confirmation wait is not automatic time, so it moves the deadline.
                left = submitted + budget_s + approval_wait - time.monotonic()
                if left <= 0:
                    break
                try:
                    handle = page.wait_for_function(_SETTLED_JS, arg=[before], polling=100,
                                                    timeout=max(1, int(left * 1000)))
                except PlaywrightTimeout:
                    break
                state = handle.json_value()
                if state != "approval":
                    break
                seen = time.monotonic()
                card = page.evaluate(_APPROVAL_JS)
                if card.get("kind") == "navigation":
                    # The product asks before it leaves the current page (P7.5 D7): a person who
                    # asked for another page opens it. Not a write, so not the oracle's decision.
                    page.wait_for_timeout(config.approval_visible_wait_ms)
                    page.locator(_ROOT).get_by_test_id("woa-approve").click(timeout=10_000)
                    page.wait_for_function(_CARD_GONE_JS, timeout=15_000)
                    approval_wait += time.monotonic() - seen
                    artifact["navigation_confirmations"].append({"turn": index, "card": card})
                    checkpoint({"product_started": True, "turn": index, "navigation": True})
                    continue
                verdict = (approval_hook(card) if approval_hook is not None
                           else {"decision": "deny", "reasons": ["no_approval_expected"],
                                 "safety_events": []})
                page.wait_for_timeout(config.approval_visible_wait_ms)
                testid = "woa-approve" if verdict["decision"] == "approve" else "woa-deny"
                page.locator(_ROOT).get_by_test_id(testid).click(timeout=10_000)
                page.wait_for_function(_CARD_GONE_JS, timeout=15_000)
                approval_wait += time.monotonic() - seen
                artifact["approvals"].append({"turn": index, "card": card, "verdict": verdict})
                checkpoint({"product_started": True, "turn": index, "approval": True})
            automatic = max(0.0, time.monotonic() - submitted - approval_wait)
            automatic_used += automatic
            if state != "settled":
                stopped = stop_turn(page, config)
                artifact["budget_exhausted"] = limit
                # The stopped turn reports the same fields as one that answered. Nobody read an
                # answer from it, which is recorded as null rather than left out.
                artifact["turns"].append({
                    "index": index, "timed_out": True, "limit": limit,
                    "answer_kind": None, "visible_answer": None, "path": None,
                    "view_settle_ms": None,
                    "first_feedback_ms": feedback_ms, **stopped,
                    "automatic_ms": int(automatic * 1000),
                    "approval_wait_ms": int(approval_wait * 1000)})
                checkpoint({"product_started": True, "turn": index,
                            "record": dict(artifact["turns"][-1])})
                break
            answer = page.evaluate(_ANSWER_JS)
            turn = {
                "index": index, "timed_out": False, "answer_kind": answer["kind"],
                "visible_answer": answer["text"], "path": page.evaluate("() => location.pathname"),
                "view_settle_ms": None, "first_feedback_ms": feedback_ms,
                "cancel_feedback_ms": None, "automatic_ms": int(automatic * 1000),
                "approval_wait_ms": int(approval_wait * 1000),
            }
            # The visible answer is the turn's result. It is recorded, and the turn counts as ended,
            # before waiting on the page, so a later fault cannot lose what the user already saw.
            # The driver writes it down at once: this artifact reaches the evidence only when the
            # run returns, and a driver killed before then would lose every answer it had read.
            artifact["turns"].append(turn)
            checkpoint({"product_started": True, "turn": index, "settled": True,
                        "record": dict(turn)})
            if index == len(runner_input.messages) - 1:
                # The last message's turn has ended and no further one will be sent: waiting on
                # the page is already reading the final view, not running the product.
                stage[0] = "final_page"
            turn["view_settle_ms"] = settle_view(page, view_before, config.view_settle_s)
            turn["path"] = page.evaluate("() => location.pathname")
        stage[0] = "final_page"
        artifact["final"] = breadcrumb(page)
        stage[0] = "teardown"
        config.evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        page.screenshot(path=str(config.evidence_dir / "final.png"))
        artifact["logged_out"] = logout(page)
        context.close()
    except RunnerPreStartError:
        raise
    except Exception as exc:
        record_fault(artifact, stage[0], type(exc).__name__)
    finally:
        try:
            browser.close()
        except Exception:
            record_fault(artifact, stage[0], "close_failed")
    return artifact


def empty_artifact(runner_input: RunnerInput) -> dict[str, Any]:
    """The record of a run that has not happened yet, with every field this arm reports.

    One definition serves the runner and the driver, so a run that never started and one that
    ended is read the same way: a field missing from an artifact is a broken contract, never a
    value for a reader to fill in.
    """
    return {
        "runner": "woa_product", "task_id": runner_input.task_id, "actor": runner_input.actor,
        "product_started": False, "turns": [], "page_errors": [], "final": None,
        "logged_out": False, "browser_exit": None, "control_channel_error": None,
        "final_page_error": None, "teardown_error": None, "approvals": [],
        "navigation_confirmations": [], "budget_exhausted": None,
    }


def record_fault(artifact: dict[str, Any], stage: str, kind: str) -> None:
    """Record a fault at the stage the run had reached, keeping the first fault of a stage.

    During the run a failed browser close is the browser's exit and any other fault the control
    channel's, unless the browser had already exited. After it, a fault reading the final page or
    tearing down is recorded as that, never as losing sight of the product.
    """
    fault = {"at_monotonic": round(time.monotonic(), 3), "type": kind}
    if stage == "run":
        if kind == "close_failed":
            artifact["browser_exit"] = artifact["browser_exit"] or "close_failed"
        elif artifact["browser_exit"] is None:
            artifact["control_channel_error"] = fault
    elif not (artifact["final_page_error"] or artifact["teardown_error"]):
        artifact[f"{stage}_error"] = fault
