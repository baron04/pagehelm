"""Real-Chromium runner for the official PageAgent browser-only arm on Odoo.

PageAgent signs in as the same synthetic actor on the same Odoo, but receives no Skill, no
business API and no WOA assistant: the addon's assertion and assistant endpoints are refused
in this browser context, and the runner verifies that no assistant mounted before PageAgent
starts. The pinned bundle is injected after the page loads, its provider calls go through the
same evaluation gateway as WOA's, and its visible final answer is ``done.text``.

The arm is single-turn and read-only by construction; the schedule admits it only to the
preregistered read and navigation subset.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.pageagent.host import PageAgentProviderContract, PageAgentProviderProxy

from .runner_woa import ActorCredential, RunnerPreStartError, breadcrumb, login, logout
from .tasks import RunnerInput

BLOCKED_ASSISTANT_ROUTES = ("**/woa/session-assertion", "**/assistant/**")


@dataclass(frozen=True)
class PageAgentRunnerConfig:
    base_url: str
    actors: dict[str, ActorCredential]
    evidence_dir: Path
    bundle_path: Path
    bundle_sha256: str
    max_steps: int = 12
    max_page_actions: int = 20
    wall_time_s: int = 120
    viewport: tuple[int, int] = (1440, 1000)
    headless: bool = True


def run(runner_input: RunnerInput, config: PageAgentRunnerConfig, *, client: Any,
        contract: PageAgentProviderContract, run_id: str, playwright: Any) -> dict[str, Any]:
    if len(runner_input.messages) != 1:
        raise ValueError("the browser-only arm runs single-turn tasks only")
    bundle = config.bundle_path.read_bytes()
    if "sha256:" + hashlib.sha256(bundle).hexdigest() != config.bundle_sha256:
        raise RunnerPreStartError("credential_or_identity_before_start", "pre_start_check")
    artifact: dict[str, Any] = {
        "runner": "pageagent_browser_only", "task_id": runner_input.task_id,
        "actor": runner_input.actor, "product_started": False, "bridge": None,
        "host_errors": [], "page_errors": [], "dialogs": 0, "blocked_assistant_requests": 0,
        "final": None, "logged_out": False, "browser_exit": None,
        "control_channel_error": None, "document_replaced": False, "turns": [],
    }
    token = secrets.token_urlsafe(32)
    proxy = PageAgentProviderProxy(run_token=token, run_id=run_id, client=client,
                                   contract=contract)
    browser = playwright.chromium.launch(headless=config.headless)
    try:
        context = browser.new_context(ignore_https_errors=True,
                                      viewport={"width": config.viewport[0],
                                                "height": config.viewport[1]})

        def refuse(route: Any) -> None:
            artifact["blocked_assistant_requests"] += 1
            route.abort()

        for pattern in BLOCKED_ASSISTANT_ROUTES:
            context.route(pattern, refuse)
        page = context.new_page()
        page.on("pageerror", lambda error: artifact["page_errors"].append(type(error).__name__))
        page.on("crash", lambda _page: artifact.update(browser_exit="page_crashed"))

        def dialog(value: Any) -> None:
            artifact["dialogs"] += 1
            value.dismiss()

        page.on("dialog", dialog)

        def provider_fetch(payload: Any) -> dict[str, Any]:
            try:
                return proxy.handle(payload)
            except Exception as exc:
                code = getattr(exc, "code", "host_exception")
                if code == "provider_call":
                    code = proxy.transport_report().get("last_error_code") or code
                artifact["host_errors"].append(("provider", str(code)))
                raise

        try:
            login(page, config, runner_input.actor)
            if runner_input.entry_path:
                page.goto(config.base_url + runner_input.entry_path,
                          wait_until="domcontentloaded", timeout=60_000)
            page.locator("body").wait_for(state="visible", timeout=60_000)
            page.wait_for_timeout(1_500)
            if page.evaluate("() => document.querySelector('#woa-assistant') !== null"):
                raise RunnerPreStartError("credential_or_identity_before_start",
                                          "pre_start_check")
            page.expose_function("__WOA_PAGEAGENT_PROVIDER_FETCH__", provider_fetch)
            page.add_script_tag(content=bundle.decode("utf-8"))
        except RunnerPreStartError:
            raise
        except Exception as exc:
            raise RunnerPreStartError("credential_or_identity_before_start",
                                      "pre_start_check") from exc
        started = time.monotonic()
        artifact["product_started"] = True
        bridge_config = {
            "prompt": runner_input.messages[0], "model": contract.model,
            "language": runner_input.locale if runner_input.locale in {"en-US", "zh-CN"}
            else "en-US",
            "max_steps": config.max_steps, "max_page_actions": config.max_page_actions,
            "wall_ms": config.wall_time_s * 1000, "navigation_mode": "spa",
            "capability_profile": "browser_only", "instructions": "", "tools": [],
            "run_token": token, "request_options": contract.bridge_request_options(),
        }
        try:
            value = page.evaluate(
                "config => globalThis.__WOA_PAGEAGENT_BASELINE__.execute(config)",
                bridge_config)
            artifact["bridge"] = value if isinstance(value, dict) else None
        except Exception:
            installed = page.evaluate(
                "() => Object.hasOwn(globalThis, '__WOA_PAGEAGENT_BASELINE__')")
            artifact["document_replaced"] = installed is False
        elapsed = time.monotonic() - started
        answer = (artifact["bridge"] or {}).get("answer", "")
        artifact["turns"].append({"index": 0, "timed_out": artifact["bridge"] is None,
                                  "answer_kind": "plain", "visible_answer": answer,
                                  "first_feedback_ms": None,
                                  "automatic_ms": int(elapsed * 1000), "approval_wait_ms": 0})
        artifact["final"] = breadcrumb(page)
        config.evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        page.screenshot(path=str(config.evidence_dir / "final.png"))
        artifact["logged_out"] = logout(page)
        artifact["provider"] = proxy.transport_report()
        context.close()
    except RunnerPreStartError:
        raise
    except Exception as exc:
        if artifact["browser_exit"] is None:
            artifact["control_channel_error"] = {
                "at_monotonic": round(time.monotonic(), 3), "type": type(exc).__name__}
    finally:
        try:
            browser.close()
        except Exception:
            artifact["browser_exit"] = artifact["browser_exit"] or "close_failed"
    return artifact
