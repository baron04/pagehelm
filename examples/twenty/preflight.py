#!/usr/bin/env python3
"""Check whether the public Twenty demo can start, without launching it.

The check may make read-only calls to the configured synthetic Twenty instance. It never
opens a browser, dispatches a model request, writes business data, or prints configured
paths and credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from examples.twenty import demo


_REQUIRED_LOCATIONS = (
    ("base_url", "WOA_TWENTY_BASE_URL"),
    ("storage_state", "WOA_TWENTY_STORAGE_STATE"),
    ("oauth_token", "WOA_TWENTY_OAUTH_TOKEN"),
    ("oauth_registration", "WOA_TWENTY_OAUTH_REGISTRATION"),
    ("page_controller_module", "WOA_TWENTY_PAGE_CONTROLLER_MODULE"),
    ("audit_ledger", "WOA_AUDIT_LEDGER"),
    ("spend_ledger", "WOA_SPEND_LEDGER"),
    ("output_dir", "WOA_OUTPUT_DIR"),
)
_FILE_FIELDS = (
    ("storage_state", "browser session state"),
    ("oauth_token", "delegated OAuth token"),
    ("oauth_registration", "OAuth client registration"),
)
_MODEL_KEYS = (
    "AGENT_OPENAI_BASE_URL",
    "AGENT_OPENAI_API_KEY",
    "AGENT_OPENAI_MODEL_PRO",
)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": "PASS" if self.passed else "FAIL",
            "detail": self.detail,
        }


def _value(namespace: argparse.Namespace, env: Mapping[str, str], field: str,
           env_name: str) -> str:
    direct = getattr(namespace, field, None)
    return str(direct or env.get(env_name) or "").strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="demo.sh --check",
        description=(
            "Validate the Twenty demo configuration and delegated identity without "
            "opening a browser or calling the model provider."),
    )
    for field, env_name in _REQUIRED_LOCATIONS:
        parser.add_argument(
            "--" + field.replace("_", "-"),
            help=f"override {env_name} for this check",
        )
    parser.add_argument("--model-env", help="override WOA_MODEL_ENV for this check")
    parser.add_argument("--mutation-journal",
                        help="override WOA_MUTATION_JOURNAL for this check")
    parser.add_argument("--allow-mutations", action="store_true")
    parser.add_argument(
        "--offline", action="store_true",
        help="validate local configuration only; do not contact Twenty",
    )
    return parser


def _model_values(namespace: argparse.Namespace, env: Mapping[str, str]) -> dict[str, str]:
    values = {key: str(env.get(key) or "").strip() for key in _MODEL_KEYS}
    model_env = str(namespace.model_env or env.get("WOA_MODEL_ENV") or "").strip()
    if model_env:
        path = Path(model_env).expanduser()
        if path.is_file():
            loaded = demo.load_env(path)
            for key in _MODEL_KEYS:
                if not values[key]:
                    values[key] = str(loaded.get(key) or "").strip()
    return values


def inspect_local(
        namespace: argparse.Namespace,
        environ: Mapping[str, str] | None = None) -> tuple[list[Check], dict[str, str]]:
    env = demo.configuration_environment(environ)
    values = {
        field: _value(namespace, env, field, env_name)
        for field, env_name in _REQUIRED_LOCATIONS
    }
    missing = [env_name for field, env_name in _REQUIRED_LOCATIONS if not values[field]]
    checks = [Check(
        "required_configuration", not missing,
        "all required WOA_* settings are configured" if not missing else
        "missing: " + ", ".join(missing),
    )]
    checks.append(Check(
        "python_runtime", sys.version_info[:2] == (3, 12),
        f"running Python {sys.version_info.major}.{sys.version_info.minor}; required 3.12",
    ))

    for field, label in _FILE_FIELDS:
        configured = values[field]
        exists = bool(configured) and Path(configured).expanduser().is_file()
        checks.append(Check(
            field, exists,
            f"{label} is readable" if exists else f"{label} file is unavailable",
        ))

    model_values = _model_values(namespace, env)
    missing_model = [key for key in _MODEL_KEYS if not model_values[key]]
    checks.append(Check(
        "model_configuration", not missing_model,
        "provider endpoint, credential, and model are configured" if not missing_model else
        "missing: " + ", ".join(missing_model),
    ))

    base = urlsplit(values["base_url"])
    controller = urlsplit(values["page_controller_module"])
    valid_origin = (
        base.scheme in {"http", "https"} and bool(base.netloc)
        and controller.scheme in {"http", "https"} and bool(controller.netloc)
        and demo._origin(base) == demo._origin(controller)
    )
    checks.append(Check(
        "controller_origin", valid_origin,
        "PageController is configured on the Twenty origin" if valid_origin else
        "Twenty and PageController must use the same valid HTTP(S) origin",
    ))

    mutation_journal = str(
        namespace.mutation_journal or env.get("WOA_MUTATION_JOURNAL") or "").strip()
    checks.append(Check(
        "mutation_mode", not namespace.allow_mutations or bool(mutation_journal),
        "read-only mode needs no mutation journal" if not namespace.allow_mutations else
        ("durable mutation journal is configured" if mutation_journal else
         "WOA_MUTATION_JOURNAL is required with --allow-mutations"),
    ))
    values["mutation_journal"] = mutation_journal
    values["model_env"] = str(
        namespace.model_env or env.get("WOA_MODEL_ENV") or "").strip()
    return checks, values


def _config_from(values: Mapping[str, str], *, allow_mutations: bool) -> demo.TwentyDemoConfig:
    return demo.TwentyDemoConfig(
        base_url=values["base_url"],
        entry_path="/objects/companies",
        storage_state_path=Path(values["storage_state"]).expanduser(),
        oauth_token_path=Path(values["oauth_token"]).expanduser(),
        oauth_registration_path=Path(values["oauth_registration"]).expanduser(),
        page_controller_module=values["page_controller_module"],
        audit_ledger_path=Path(values["audit_ledger"]).expanduser(),
        spend_ledger_path=Path(values["spend_ledger"]).expanduser(),
        output_root=Path(values["output_dir"]).expanduser(),
        mutation_journal_path=(
            Path(values["mutation_journal"]).expanduser()
            if values["mutation_journal"] else None),
        model_env_path=(
            Path(values["model_env"]).expanduser() if values["model_env"] else None),
        allow_mutations=allow_mutations,
    )


def inspect_twenty(config: demo.TwentyDemoConfig) -> list[Check]:
    adapter = demo.build_adapter(config)
    try:
        health = adapter.health()
        healthy = bool(health.get("passed"))
    except Exception:
        healthy = False
    checks = [Check(
        "twenty_health", healthy,
        "Twenty health endpoint reports ok" if healthy else
        "Twenty health endpoint is unavailable or unhealthy",
    )]
    if not healthy:
        checks.append(Check(
            "identity_binding", False,
            "identity cannot be checked until Twenty health passes",
        ))
        return checks
    now_ms = int(__import__("time").time() * 1000)
    try:
        adapter.page_session_binding(config.storage_state_path, now_ms=now_ms)
    except Exception:
        checks.append(Check(
            "identity_binding", False,
            "browser session is missing or expired; run ./demo.sh --login, then check again",
        ))
        return checks
    try:
        adapter.bind_browser_session(
            config.storage_state_path,
            delegation_id="twenty-demo-preflight",
            lifetime_s=60,
            now_ms=now_ms,
        )
        bound = True
    except Exception:
        bound = False
    checks.append(Check(
        "identity_binding", bound,
        "browser and OAuth identities match" if bound else
        "browser and OAuth actor/workspace do not match, or Twenty rejected identity resolution",
    ))
    return checks


def run(
        argv: Sequence[str] | None = None,
        environ: Mapping[str, str] | None = None) -> tuple[int, dict[str, object]]:
    namespace = build_parser().parse_args(argv)
    checks, values = inspect_local(namespace, environ)
    network_prerequisites = {
        "required_configuration", "python_runtime", "storage_state",
        "oauth_token", "oauth_registration", "controller_origin",
    }
    can_check_identity = all(
        check.passed for check in checks if check.name in network_prerequisites)
    if not namespace.offline and can_check_identity:
        try:
            config = _config_from(values, allow_mutations=namespace.allow_mutations)
            checks.extend(inspect_twenty(config))
        except Exception:
            checks.append(Check(
                "demo_configuration", False,
                "the assembled demo configuration is invalid",
            ))
    passed = all(check.passed for check in checks)
    report = {
        "schema_version": 1,
        "status": "READY" if passed else "BLOCKED",
        "checks": [check.as_dict() for check in checks],
        "browser_launched": False,
        "model_calls": 0,
        "business_writes": 0,
    }
    return (0 if passed else 2), report


def main(argv: Sequence[str] | None = None) -> int:
    code, report = run(argv)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
