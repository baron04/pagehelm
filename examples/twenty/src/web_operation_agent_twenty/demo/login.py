#!/usr/bin/env python3
"""Renew the browser state for an isolated Twenty demo through visible sign-in."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from web_operation_agent import python_is_supported, supported_python_text
from web_operation_agent_twenty.demo import demo
from web_operation_agent_twenty.adapter import TwentyPageSessionIdentityResolver, _validated_origin


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def build_parser(environ: Mapping[str, str] | None = None) -> argparse.ArgumentParser:
    env = demo.configuration_environment(environ)
    parser = argparse.ArgumentParser(
        prog="demo.sh --login",
        description=(
            "Open Twenty for manual sign-in and replace the configured browser "
            "storage state after a valid HttpOnly session appears."),
    )
    parser.add_argument(
        "--base-url", default=env.get("WOA_TWENTY_BASE_URL"),
        required=not bool(env.get("WOA_TWENTY_BASE_URL")),
        help="Twenty origin (or WOA_TWENTY_BASE_URL)",
    )
    parser.add_argument(
        "--storage-state", default=env.get("WOA_TWENTY_STORAGE_STATE"),
        required=not bool(env.get("WOA_TWENTY_STORAGE_STATE")),
        help="destination Playwright storage-state JSON (or WOA_TWENTY_STORAGE_STATE)",
    )
    parser.add_argument(
        "--browser-executable", default=env.get("WOA_BROWSER_EXECUTABLE"),
        help="optional Chromium executable (or WOA_BROWSER_EXECUTABLE)",
    )
    parser.add_argument(
        "--timeout", type=_positive_float, default=300.0,
        help="seconds to wait for manual sign-in (default: 300)",
    )
    return parser


def _write_storage_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(state, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def run(
        argv: Sequence[str] | None = None,
        environ: Mapping[str, str] | None = None) -> dict[str, object]:
    namespace = build_parser(environ).parse_args(argv)
    if not python_is_supported():
        raise RuntimeError(f"the Twenty demo requires Python {supported_python_text()}")
    origin = _validated_origin(namespace.base_url)
    destination = Path(namespace.storage_state).expanduser().resolve()
    executable = (
        Path(namespace.browser_executable).expanduser().resolve()
        if namespace.browser_executable else None)

    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    with sync_playwright() as playwright:
        launch_options: dict[str, object] = {"headless": False, "args": ["--start-maximized"]}
        if executable is not None:
            launch_options["executable_path"] = str(executable)
        browser = playwright.chromium.launch(**launch_options)
        try:
            context = browser.new_context(no_viewport=True)
            try:
                page = context.new_page()
                page.goto(origin, wait_until="domcontentloaded", timeout=60_000)
                print(json.dumps({
                    "schema_version": 1,
                    "event": "login_required",
                    "message": "Complete sign-in in the opened Twenty window.",
                }, sort_keys=True), flush=True)
                deadline = time.monotonic() + namespace.timeout
                state: dict | None = None
                while time.monotonic() < deadline:
                    if page.is_closed():
                        raise RuntimeError("the login window closed before sign-in completed")
                    candidate = context.storage_state()
                    try:
                        TwentyPageSessionIdentityResolver(
                            base_url=origin, storage_state=candidate).binding_digest()
                        state = candidate
                        break
                    except Exception:
                        page.wait_for_timeout(250)
                if state is None:
                    raise TimeoutError("timed out waiting for a valid Twenty browser session")
                _write_storage_state(destination, state)
                TwentyPageSessionIdentityResolver(
                    base_url=origin, storage_state_path=destination).binding_digest()
            finally:
                context.close()
        except PlaywrightError as exc:
            raise RuntimeError("the browser could not complete Twenty sign-in") from exc
        finally:
            browser.close()
    result = {
        "schema_version": 1,
        "status": "READY",
        "browser_session": "renewed",
        "model_calls": 0,
        "business_writes": 0,
    }
    print(json.dumps(result, sort_keys=True), flush=True)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    try:
        run(argv)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(json.dumps({
            "schema_version": 1,
            "status": "BLOCKED",
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }, sort_keys=True), file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
