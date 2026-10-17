"""Local-only deployment settings, shared by its launcher and HTTP front."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent
CACHE = ROOT / ".q0-cache/yudao"
STATE = ROOT / ".q0-cache/yudao-local"
ORIGIN = "http://localhost:48163"
UPSTREAM = "http://127.0.0.1:48161"
PORTS = (48163, 48164, 48165, 48166)  # front, gateway, bootstrap, guard
POLICIES = ROOT / "examples/yudao/deploy/cloud/native-policy"
RUNTIME = ROOT / "src/web_operation_agent/web_runtime/assistant.js"
RELEASE = "v2026.09"


class LocalError(RuntimeError):
    pass


@dataclass(frozen=True)
class Configuration:
    project: str = "woa-yudao-local"


def configuration() -> Configuration:
    path = ROOT / "local.local.toml"
    if not path.is_file():
        return Configuration()
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    if set(data) != {"compose_project"} or not isinstance(data["compose_project"], str):
        raise LocalError("local.local.toml 只接受 compose_project。")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", data["compose_project"]):
        raise LocalError("compose_project 只能包含小写字母、数字、下划线和连字符。")
    return Configuration(data["compose_project"])


def process_environment() -> dict[str, str]:
    env = dict(os.environ, YUDAO_CACHE=str(CACHE),
               PYTHONPATH=os.pathsep.join((str(ROOT / "src"), str(ROOT / "examples/yudao/src"))),
               NO_PROXY="localhost,127.0.0.1,::1", no_proxy="localhost,127.0.0.1,::1")
    env.pop("COMPOSE_PROFILES", None)
    return env


def compose(config: Configuration, *args: str, capture: bool = False) -> str:
    result = subprocess.run(["docker", "compose", "-p", config.project,
                             "--env-file", str(STATE / "application.env"),
                             "-f", str(ROOT / "examples/yudao/deploy/compose.yaml"),
                             "-f", str(HERE / "compose.yaml"), *args], cwd=ROOT,
                            env=process_environment(), check=True, text=True,
                            stdout=subprocess.PIPE if capture else None, timeout=300)
    return result.stdout.strip() if capture else ""


def checkout_id() -> str:
    return hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]


def source_digest(*, frontend_only: bool = False) -> str:
    """Hash maintained inputs only; no credentials, caches, user content or evaluators."""
    digest = hashlib.sha256()
    areas = [(ROOT / "frontend/src", {".ts", ".tsx", ".js", ".json", ".scss", ".css"}),
             (ROOT / "frontend/scripts", {".mjs"}), (ROOT / "frontend/trace", {".html", ".js", ".css"}),
             (ROOT / "src/web_operation_agent/web_runtime", {".json"})]
    if not frontend_only:
        areas.extend(((ROOT / "src/web_operation_agent", {".py", ".json", ".md", ".toml"}),
                      (ROOT / "examples/yudao/src/web_operation_agent_yudao", {".py", ".json", ".md", ".toml"}),
                      (HERE, {".py", ".yaml"}), (POLICIES, {".json"})))
    files = []
    for directory, suffixes in areas:
        files.extend(p for p in directory.rglob("*") if p.is_file() and p.suffix in suffixes
                     and not {"tests", "__pycache__"}.intersection(p.relative_to(directory).parts)
                     and not p.name.endswith((".test.ts", ".test.tsx", ".generated.ts", ".generated.scss")))
    files.extend(p for p in (ROOT / "frontend").glob("*")
                 if p.is_file() and p.suffix in {".json", ".ts"})
    files.extend(ROOT / "scripts" / name for name in ("build_panel.py", "build_runtime.py"))
    files.append(ROOT / "examples/yudao/deploy/cloud/yudao-loader.js")
    for path in sorted(set(files)):
        digest.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def load_model_environment() -> dict:
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "examples/yudao/src")]
    from web_operation_agent.modeling.client import load_env
    from web_operation_agent.modeling.spend import UnpricedModel, price_for
    values = load_env(ROOT / ".env")
    required = ("AGENT_OPENAI_BASE_URL", "AGENT_OPENAI_API_KEY", "AGENT_OPENAI_MODEL")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise LocalError(".env 缺少：" + ", ".join(missing) + "。只需填写本机 .env，无需上传凭据。")
    try:
        price_for(values["AGENT_OPENAI_MODEL"])
    except UnpricedModel as exc:
        raise LocalError("本机模型没有登记价格，无法启动费用核算；请使用已配置计价的模型。") from exc
    return {key: values[key] for key in (*required, "AGENT_PROVIDER") if values.get(key)}
