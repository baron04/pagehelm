"""Configuration and live capability preflight for PageHelm model endpoints."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import tempfile
import time
from typing import Mapping

from .client import ChatModel, load_env, resolve_pagehelm_environment
from .provider import ProviderError
from .spend import SpendMeter, price_for

_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "preflight_ping",
        "description": "Diagnostic read-only health check. Call this tool with status set to 'ok'.",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Must be set to 'ok'.",
                }
            },
            "required": ["status"],
            "additionalProperties": False,
        },
    },
}

_PROBE_MESSAGES = [
    {
        "role": "system",
        "content": "You are running a configuration preflight check. Invoke preflight_ping with status set to ok.",
    },
    {
        "role": "user",
        "content": "Run preflight_ping now.",
    },
]

_ERROR_HINTS = {
    "provider_usage": (
        "The endpoint omitted OpenAI-compatible usage.prompt_tokens / usage.completion_tokens. "
        "Enable usage reporting (e.g. Ollama >= 0.5 / vLLM OpenAI server) before running."
    ),
    "provider_authentication": (
        "Authentication failed (HTTP 401/403). Check the API key in .env or gateway auth settings."
    ),
    "provider_tool_response": (
        "The model responded without calling the required tool. Verify that the selected model "
        "supports native OpenAI-style tool calling."
    ),
    "provider_transport": (
        "Could not connect to the model endpoint. Verify base_url and ensure the local server is running."
    ),
    "provider_deadline": (
        "The probe timed out. Check model load status or raise --timeout."
    ),
}


def _inspect_tool_call(message: dict) -> tuple[bool, str]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        return False, "model returned no tool_calls"
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        func = call.get("function")
        if not isinstance(func, dict) or func.get("name") != "preflight_ping":
            continue
        raw_args = func.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except ValueError:
            return False, "preflight_ping arguments were not valid JSON"
        if not isinstance(args, dict):
            return False, "preflight_ping arguments were not a JSON object"
        status = args.get("status")
        if status == "ok":
            return True, "preflight_ping called with status='ok'"
        return True, f"preflight_ping called with JSON arguments (status={repr(status)[:60]})"
    return False, "model did not invoke preflight_ping"


def run_preflight(
    *,
    root: str | Path | None = None,
    toml_path: str | Path | None = None,
    env_path: str | Path | None = None,
    env_values: Mapping[str, str] | None = None,
    probe: bool = False,
    timeout_s: float = 30.0,
) -> dict[str, object]:
    """Validate PageHelm model config and optionally run one synthetic tool-call probe."""
    if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s) or timeout_s <= 0):
        raise ValueError("timeout_s must be a positive finite number")

    checks: dict[str, dict[str, object]] = {}

    if env_values is None and env_path is not None and not Path(env_path).is_file():
        checks["config"] = {"ok": False, "detail": f"env file not found: {env_path}"}
        return {
            "ok": False,
            "provider": "",
            "base_url": "",
            "model": "",
            "self_hosted": False,
            "auth_configured": False,
            "read_only": False,
            "pricing_source": "",
            "checks": checks,
            "probe": None,
        }

    loaded_env = dict(env_values) if env_values is not None else (
        load_env(env_path) if env_path is not None else None
    )

    try:
        resolved = resolve_pagehelm_environment(
            root=root,
            env_values=loaded_env,
            toml_path=toml_path,
        )
    except Exception as exc:
        checks["config"] = {"ok": False, "detail": str(exc)}
        return {
            "ok": False,
            "provider": "",
            "base_url": "",
            "model": "",
            "self_hosted": False,
            "auth_configured": False,
            "read_only": False,
            "pricing_source": "",
            "checks": checks,
            "probe": None,
        }

    checks["config"] = {"ok": True, "detail": "environment configuration resolved"}
    provider = str(resolved.get("AGENT_PROVIDER") or "")
    base_url = str(resolved["AGENT_OPENAI_BASE_URL"])
    model = str(resolved["AGENT_OPENAI_MODEL"])
    self_hosted = resolved.get("AGENT_MODEL_SELF_HOSTED") == "true"
    auth_configured = bool(str(resolved.get("AGENT_OPENAI_API_KEY") or ""))
    read_only = resolved.get("WOA_READ_ONLY") == "true"

    try:
        price = price_for(model, self_hosted=self_hosted)
    except Exception as exc:
        checks["pricing"] = {"ok": False, "detail": str(exc)}
        return {
            "ok": False,
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "self_hosted": self_hosted,
            "auth_configured": auth_configured,
            "read_only": read_only,
            "pricing_source": "",
            "checks": checks,
            "probe": None,
        }

    pricing_source = str(price.get("taken") or "")
    checks["pricing"] = {
        "ok": True,
        "detail": "self-hosted zero-cost reference pricing" if self_hosted else "registered in prices.json",
    }

    if not probe:
        return {
            "ok": True,
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "self_hosted": self_hosted,
            "auth_configured": auth_configured,
            "read_only": read_only,
            "pricing_source": pricing_source,
            "checks": checks,
            "probe": None,
        }

    started = time.monotonic()
    probe_report: dict[str, object] = {
        "latency_ms": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_cny": 0.0,
        "error_code": "",
        "hint": "",
    }
    overall_ok = False
    with tempfile.TemporaryDirectory() as tmpdir:
        meter = SpendMeter(
            ledger=Path(tmpdir) / "preflight-spend.jsonl",
            batch_id="preflight",
        )
        try:
            chat = ChatModel(
                meter,
                env=dict(resolved),
                max_completion_tokens=128,
                timeout=float(timeout_s),
            )
            message = chat.complete(
                _PROBE_MESSAGES,
                [_PROBE_TOOL],
                "preflight-probe",
                options={"tool_choice": "required"},
                timeout_s=float(timeout_s),
            )
        except ProviderError as exc:
            report_snap = meter.report()
            probe_report["latency_ms"] = round((time.monotonic() - started) * 1000)
            probe_report["input_tokens"] = int(report_snap["input_tokens"])
            probe_report["output_tokens"] = int(report_snap["output_tokens"])
            probe_report["estimated_cost_cny"] = float(report_snap["cost_cny"])
            probe_report["error_code"] = exc.code
            probe_report["hint"] = _ERROR_HINTS.get(exc.code, str(exc))
            if exc.code == "provider_tool_response":
                checks["endpoint_and_usage"] = {
                    "ok": True,
                    "detail": "endpoint responded with valid token usage",
                }
                checks["native_tool_calling"] = {
                    "ok": False,
                    "detail": str(exc),
                }
            else:
                checks["endpoint_and_usage"] = {
                    "ok": False,
                    "detail": f"{exc.code}: {exc}",
                }
                checks["native_tool_calling"] = {
                    "ok": False,
                    "detail": "skipped because endpoint or usage check failed",
                }
        else:
            report_snap = meter.report()
            probe_report["latency_ms"] = round((time.monotonic() - started) * 1000)
            probe_report["input_tokens"] = int(report_snap["input_tokens"])
            probe_report["output_tokens"] = int(report_snap["output_tokens"])
            probe_report["estimated_cost_cny"] = float(report_snap["cost_cny"])
            checks["endpoint_and_usage"] = {
                "ok": True,
                "detail": "endpoint responded with valid token usage",
            }
            tool_ok, tool_detail = _inspect_tool_call(message)
            checks["native_tool_calling"] = {
                "ok": tool_ok,
                "detail": tool_detail,
            }
            if tool_ok:
                overall_ok = True
            else:
                probe_report["error_code"] = "provider_tool_response"
                probe_report["hint"] = _ERROR_HINTS["provider_tool_response"]

    return {
        "ok": overall_ok,
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "self_hosted": self_hosted,
        "auth_configured": auth_configured,
        "read_only": read_only,
        "pricing_source": pricing_source,
        "checks": checks,
        "probe": probe_report,
    }


def _format_text(report: dict[str, object]) -> str:
    lines = [
        f"PageHelm Model Preflight: {'PASS' if report['ok'] else 'FAIL'}",
        f"  provider: {report['provider'] or '(none)'}",
        f"  base_url: {report['base_url'] or '(unset)'}",
        f"  model: {report['model'] or '(unset)'}",
        f"  self_hosted: {report['self_hosted']}",
        f"  auth_configured: {report['auth_configured']}",
        f"  read_only: {report['read_only']}",
    ]
    checks = report.get("checks") or {}
    if isinstance(checks, dict):
        for name, entry in checks.items():
            if isinstance(entry, dict):
                status = "OK" if entry.get("ok") else "FAIL"
                lines.append(f"  [{status}] {name}: {entry.get('detail', '')}")
    probe = report.get("probe")
    if isinstance(probe, dict):
        lines.append(
            f"  probe: latency={probe.get('latency_ms', 0)}ms, "
            f"tokens={probe.get('input_tokens', 0)}/{probe.get('output_tokens', 0)}, "
            f"cost_cny={probe.get('estimated_cost_cny', 0.0)}"
        )
        if probe.get("hint"):
            lines.append(f"  hint: {probe['hint']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m web_operation_agent.modeling.preflight",
        description="Validate PageHelm model config and optionally probe tool-calling compatibility.",
    )
    parser.add_argument("--env", type=Path, default=None, help="Path to .env file")
    parser.add_argument("--config", type=Path, default=None, help="Optional path to legacy pagehelm.toml")
    parser.add_argument("--probe", action="store_true", help="Send one synthetic tool-call probe")
    parser.add_argument("--timeout", type=float, default=30.0, help="Probe request timeout in seconds")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON output")
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a positive finite number")

    root = (
        args.config.parent if args.config is not None
        else (args.env.parent if args.env is not None else None)
    )
    report = run_preflight(
        root=root,
        toml_path=args.config,
        env_path=args.env,
        probe=args.probe,
        timeout_s=args.timeout,
    )
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_format_text(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
