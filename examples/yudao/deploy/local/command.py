"""Operate two local Compose environments without touching the legacy deployment."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit
import urllib.request

import assets
import configuration as cfg

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def read_json(url: str, *, timeout=3):
    with OPENER.open(url, timeout=timeout) as response:
        if "application/json" not in response.headers.get("Content-Type", ""):
            raise cfg.LocalError("健康检查返回网页，未连接到预期的本地服务。")
        return json.load(response)


def health(config: cfg.Configuration):
    try:
        value = read_json(config.origin + "/__local__/health")
        return value if (isinstance(value, dict) and value.get("schema_version") == 2
                         and value.get("environment") == config.number
                         and value.get("checkout_id") == cfg.checkout_id()) else None
    except (OSError, ValueError, cfg.LocalError):
        return None


def container(config: cfg.Configuration):
    ids = subprocess.run(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + config.project,
                          "--filter", "label=com.docker.compose.service=agent"],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    if not ids:
        return None
    if len(ids) != 1:
        raise cfg.LocalError("环境中存在多个助手容器，未自动接管。")
    template = ('{"running":{{.State.Running}},"image":{{json .Config.Image}},'
                '"labels":{{json .Config.Labels}},"ports":{{json .HostConfig.PortBindings}}}')
    result = subprocess.run(["docker", "inspect", "--format", template, ids[0]],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def require_owner(config: cfg.Configuration, current):
    if current and current["running"]:
        labels = current["labels"] or {}
        if (labels.get("org.web-operation-agent.checkout") != cfg.checkout_id()
                or labels.get("org.web-operation-agent.environment") != str(config.number)):
            raise cfg.LocalError("该环境正在由其他源码目录运行；先从原目录停止助手，再在此目录启动。")


def application_ready(config: cfg.Configuration, *, internal=False) -> bool:
    try:
        if internal:
            value = json.loads(cfg.compose(config, "exec", "-T", "web", "wget", "-qO-",
                "http://server:48080/admin-api/system/tenant/get-id-by-name?name=x", capture=True, quiet=True))
        else:
            value = read_json(config.origin + "/admin-api/system/tenant/get-id-by-name?name=x")
        return isinstance(value, dict) and type(value.get("code")) is int and value["code"] == 0
    except (OSError, ValueError, cfg.LocalError, subprocess.SubprocessError):
        return False


def check(config: cfg.Configuration):
    missing = [tool for tool in ("docker", "npm", "node") if not shutil.which(tool)]
    if missing:
        raise cfg.LocalError("缺少命令：" + ", ".join(missing))
    for file in (cfg.CACHE / "ui/dist-prod/index.html", cfg.CACHE / "server/yudao-server/target/yudao-server.jar",
                 cfg.CACHE / "business-schema.sql", cfg.POLICIES / "fence.json", cfg.POLICIES / "pages.json",
                 cfg.POLICIES / "filters.json", cfg.POLICIES / "verified-money.json"):
        if not file.is_file():
            raise cfg.LocalError(f"缺少 {file.relative_to(cfg.ROOT)}；见本地部署指南的首次准备。")
    cfg.load_model_environment()
    sys.path[:0] = [str(cfg.ROOT / "src"), str(cfg.ROOT / "examples/yudao/src")]
    from web_operation_agent.page_controller import PageControllerAssetError, require_served_directory
    from web_operation_agent_yudao.deployment_validation import validate_native_deployment
    from web_operation_agent_yudao.native_server import YudaoHostConfig
    try:
        require_served_directory(cfg.CACHE / "woa")
    except PageControllerAssetError as exc:
        raise cfg.LocalError("页面控制器不符合锁定版本；按部署指南准备缓存中的 woa/ 资源。") from exc
    validate_native_deployment(YudaoHostConfig(api_base="http://web:80", public_origin=config.origin,
        state_dir=config.state / "state", source_release=cfg.RELEASE, ui_source=cfg.CACHE / "ui",
        native_read_policy=cfg.POLICIES / "fence.json", native_page_policy=cfg.POLICIES / "pages.json",
        native_filter_semantics=cfg.POLICIES / "filters.json",
        native_money_policy=cfg.POLICIES / "verified-money.json"))
    subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=15)
    version = subprocess.run(["docker", "compose", "version", "--short"], check=True,
                             capture_output=True, text=True, timeout=15).stdout.strip().lstrip("v")
    if not re.match(r"^\d+\.\d+", version) or tuple(map(int, version.split(".")[:2])) < (2, 30):
        raise cfg.LocalError("需要 Docker Compose 2.30 或更新版本。")
    print(f"环境{config.number}前置检查通过：{config.origin}；源码、固定缓存、模型配置和准入策略已核验。", flush=True)


def write_private(path: Path, value: str) -> bool:
    if path.is_file() and path.read_text() == value:
        return False
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as temporary:
        temporary.write(value)
        source = Path(temporary.name)
    try:
        source.chmod(0o600)
        source.replace(path)
    finally:
        source.unlink(missing_ok=True)
    return True


def prepare(config: cfg.Configuration, current=None) -> bool:
    """Generate only the selected project's private config; never adopt legacy volumes."""
    envfile = config.state / "application.env"
    if not envfile.exists():
        result = subprocess.run(["docker", "volume", "ls", "--filter",
            "label=com.docker.compose.project=" + config.project, "--quiet"],
            capture_output=True, text=True, check=True)
        if result.stdout.strip():
            raise cfg.LocalError("该环境已有数据卷但 application.env 缺失；请恢复原配置，不会重置数据库或密码。")
        write_private(envfile, "YUDAO_DB_PASSWORD=" + secrets.token_urlsafe(32) + "\n")
    (config.state / "state").mkdir(parents=True, exist_ok=True, mode=0o700)
    model = cfg.load_model_environment()
    base = urlsplit(model["AGENT_OPENAI_BASE_URL"])
    if base.hostname in {"localhost", "127.0.0.1", "::1"}:
        if base.username or base.password:
            raise cfg.LocalError("本机模型服务地址不能包含用户凭据。")
        model["AGENT_OPENAI_BASE_URL"] = urlunsplit(base._replace(
            netloc="host.docker.internal" + (f":{base.port}" if base.port else "")))
    if any("\n" in value or "\r" in value or "\0" in value for value in model.values()):
        raise cfg.LocalError("模型环境配置必须是单行值。")
    changed = write_private(config.state / "agent.env", "".join(f"{key}={value}\n" for key, value in sorted(model.items())))
    source = cfg.source_composition(config)
    changed = write_private(config.state / "source.compose.json", source) or changed
    expected = json.loads(source)["services"]["agent"]["labels"]["org.web-operation-agent.deployment"]
    pending = current and current["running"] and (current.get("labels") or {}).get("org.web-operation-agent.deployment") != expected
    return bool(changed or pending)


def ensure_application(config: cfg.Configuration):
    services = set(cfg.compose(config, "ps", "--services", "--status", "running", capture=True).split())
    if {"mysql", "redis", "server", "web"} <= services and application_ready(config, internal=True):
        print("复用本环境的业务服务和数据。", flush=True)
        return
    cfg.compose(config, "up", "-d", "--no-build", "mysql", "redis", "server", "web")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if application_ready(config, internal=True):
            return
        time.sleep(2)
    raise cfg.LocalError(f"业务服务五分钟内未就绪；运行 {config.command} logs app 查看日志。")


def stop(config: cfg.Configuration):
    current = container(config)
    require_owner(config, current)
    if current and current["running"]:
        cfg.compose(config, "stop", "agent")
    print(f"环境{config.number}助手已停止；业务服务和数据保留。", flush=True)


def start(config: cfg.Configuration, *, restart=False):
    current = container(config)
    require_owner(config, current)
    owns_port = (current and current["running"] and any(
        binding.get("HostPort") == str(config.port)
        for bindings in (current.get("ports") or {}).values() for binding in (bindings or [])))
    if not owns_port:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", config.port)) == 0:
                raise cfg.LocalError(f"端口 {config.port} 已被其他服务占用，未终止该服务。")
    assets.build_frontend()
    changed = prepare(config, current)
    assets.ensure_image(config)
    ensure_application(config)
    value = health(config)
    if (not restart and not changed and value and value.get("ready")
            and value.get("source_digest") == cfg.source_digest()
            and value.get("runtime_sha256") == hashlib.sha256(cfg.RUNTIME.read_bytes()).hexdigest()
            and current and current["image"] == cfg.image_name()):
        status(config)
        return
    if current and current["running"] and not changed and current["image"] == cfg.image_name():
        cfg.compose(config, "restart", "agent")
    else:
        cfg.compose(config, "up", "-d", "--no-deps", "--no-build", "--force-recreate", "agent")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        value = health(config)
        if value and value.get("ready"):
            status(config)
            return
        current = container(config)
        if not current or not current["running"]:
            raise cfg.LocalError(f"助手启动失败；运行 {config.command} logs 查看日志。")
        time.sleep(.3)
    cfg.compose(config, "stop", "agent")
    raise cfg.LocalError(f"助手45秒内未就绪，已停止本次启动；运行 {config.command} logs 排查。")


def status(config: cfg.Configuration):
    value = health(config)
    if not value or not value.get("ready") or not application_ready(config):
        raise cfg.LocalError(f"未就绪或由其他源码目录运行；先运行 {config.command}，失败时查看 check 或 logs。")
    ready = read_json(config.origin + "/woa-agent/readyz")
    with OPENER.open(config.origin + "/woa-agent/v1/runtime/assistant.js", timeout=3) as response:
        served = hashlib.sha256(response.read()).hexdigest()
    if (not ready.get("ready") or value.get("source_digest") != cfg.source_digest()
            or served != hashlib.sha256(cfg.RUNTIME.read_bytes()).hexdigest()):
        raise cfg.LocalError(f"代码或Runtime尚未同步；运行 {config.command} 更新。")
    print(f"环境{config.number}已就绪：{config.origin}\n当前源码目录与Runtime一致，业务服务及助手健康检查通过。", flush=True)


def log_text(text: str, config: cfg.Configuration) -> str:
    sys.path[:0] = [str(cfg.ROOT / "src")]
    from web_operation_agent.modeling.client import load_env
    from web_operation_agent.privacy import SENSITIVE_CREDENTIAL_NAME, redact_sensitive_assignments, redact_session_handoffs
    for file in (cfg.ROOT / ".env", config.state / "application.env", config.state / "agent.env"):
        for key, value in load_env(file).items():
            if value and SENSITIVE_CREDENTIAL_NAME.search(key):
                text = text.replace(value, "[redacted]")
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text)
    return redact_session_handoffs(redact_sensitive_assignments(text))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="./local", description="本地双实例合成数据验收；默认环境1。")
    parser.add_argument("--env", type=int, choices=(1, 2), default=1, dest="environment")
    parser.add_argument("action", nargs="?", default="start", choices=("start", "restart", "stop", "status", "check", "logs"))
    parser.add_argument("component", nargs="?", choices=("app",), help="仅 logs 可选 app")
    args = parser.parse_args(argv)
    if args.component and args.action != "logs":
        parser.error("app仅用于logs")
    config = cfg.Configuration(args.environment)
    try:
        if args.action == "check":
            check(config)
        elif args.action == "status":
            status(config)
        elif args.action == "logs":
            services = ["mysql", "redis", "server", "web"] if args.component else ["agent"]
            print(log_text(cfg.compose(config, "logs", "--tail", "80", *services, capture=True), config))
        else:
            if args.action != "stop":
                check(config)
            config.state.mkdir(parents=True, exist_ok=True, mode=0o700)
            config.state.chmod(0o700)
            with (config.state / "command.lock").open("w") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise cfg.LocalError("该环境另一个启动或停止命令正在执行。") from exc
                if args.action == "stop":
                    stop(config)
                else:
                    start(config, restart=args.action == "restart")
        return 0
    except ImportError:
        print("本地Python依赖尚未安装；见 docs/guides/local-yudao.md。", file=sys.stderr)
        return 1
    except (cfg.LocalError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"环境{config.number}：{exc}\n操作指南：docs/guides/local-yudao.md", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
