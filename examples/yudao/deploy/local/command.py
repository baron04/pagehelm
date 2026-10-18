"""One local acceptance command, with bounded startup and read-only diagnostics."""
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
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import configuration as cfg

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def read_json(url: str, *, timeout=3):
    with OPENER.open(url, timeout=timeout) as response:
        if "application/json" not in response.headers.get("Content-Type", ""):
            raise cfg.LocalError("健康检查返回网页，未连接到预期的本地服务。")
        return json.load(response)


def health():
    try:
        value = read_json(cfg.ORIGIN + "/__local__/health")
        return value if (isinstance(value, dict) and value.get("schema_version") == 1
                         and value.get("checkout_id") == cfg.checkout_id()) else None
    except (OSError, ValueError, cfg.LocalError):
        return None


def application_ready() -> bool:
    try:
        value = read_json(cfg.UPSTREAM + "/admin-api/system/tenant/get-id-by-name?name=x")
        return isinstance(value, dict) and type(value.get("code")) is int and value["code"] == 0
    except (OSError, ValueError, cfg.LocalError):
        return False


def owned_pid() -> int | None:
    path = cfg.STATE / "server.pid"
    if not path.is_file() or not path.read_text().strip().isdigit():
        return None
    pid = int(path.read_text().strip())
    if pid <= 1:
        return None
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return pid if result.returncode == 0 and str(cfg.HERE / "serve.py") in result.stdout else None


def check() -> cfg.Configuration:
    missing = [tool for tool in ("docker", "npm", "node") if not shutil.which(tool)]
    if missing:
        raise cfg.LocalError("缺少命令：" + ", ".join(missing))
    config = cfg.configuration()
    for file in (cfg.CACHE / "ui/dist-prod/index.html", cfg.CACHE / "server/yudao-server/target/yudao-server.jar",
                 cfg.CACHE / "business-schema.sql", cfg.POLICIES / "fence.json", cfg.POLICIES / "pages.json",
                 cfg.POLICIES / "filters.json", cfg.POLICIES / "verified-money.json"):
        if not file.is_file():
            raise cfg.LocalError(f"缺少 {file.relative_to(cfg.ROOT)}；见本地部署指南的首次准备。")
    cfg.load_model_environment()  # Validates required names and price only; no model call.
    sys.path[:0] = [str(cfg.ROOT / "src"), str(cfg.ROOT / "examples/yudao/src")]
    from web_operation_agent.page_controller import PageControllerAssetError, require_served_directory
    from web_operation_agent_yudao.deployment_validation import validate_native_deployment
    from web_operation_agent_yudao.native_server import YudaoHostConfig
    try:
        require_served_directory(cfg.CACHE / "woa")
    except PageControllerAssetError as exc:
        raise cfg.LocalError("页面控制器不符合锁定版本；按部署指南准备缓存中的 woa/ 资源。") from exc
    validate_native_deployment(YudaoHostConfig(api_base=cfg.UPSTREAM, public_origin=cfg.ORIGIN,
        state_dir=cfg.STATE / "state", source_release=cfg.RELEASE, ui_source=cfg.CACHE / "ui",
        native_read_policy=cfg.POLICIES / "fence.json", native_page_policy=cfg.POLICIES / "pages.json",
        native_filter_semantics=cfg.POLICIES / "filters.json",
        native_money_policy=cfg.POLICIES / "verified-money.json"))
    subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=15)
    subprocess.run(["docker", "compose", "version"], check=True, capture_output=True, timeout=15)
    print("前置检查通过：Docker、模型配置、固定缓存、页面控制器和准入策略。", flush=True)
    return config


def build_assets() -> None:
    marker = cfg.STATE / "build.json"
    try:
        before = json.loads(marker.read_text()) if marker.is_file() else {}
    except (ValueError, OSError):
        before = {}
    if not isinstance(before, dict):
        before = {}
    source = cfg.source_digest(frontend_only=True)
    runtime_hash = hashlib.sha256(cfg.RUNTIME.read_bytes()).hexdigest() if cfg.RUNTIME.is_file() else ""
    if before.get("frontend") == source and before.get("runtime") == runtime_hash:
        print("前端未变化，复用已核验的构建。", flush=True)
        return
    installed = cfg.ROOT / "frontend/node_modules/.package-lock.json"
    dependency_hash = hashlib.sha256((cfg.ROOT / "frontend/package-lock.json").read_bytes() + b"\0" +
                                    (installed.read_bytes() if installed.is_file() else b"")).hexdigest()
    install = [] if installed.is_file() and before.get("dependencies") == dependency_hash else ["--install"]
    print("构建当前助手前端。", flush=True)
    for script, options in (("build_panel.py", install), ("build_runtime.py", ["--skip-build"])):
        subprocess.run([sys.executable, str(cfg.ROOT / "scripts" / script), *options],
                       cwd=cfg.ROOT, env=cfg.process_environment(), check=True)
    dependency_hash = hashlib.sha256((cfg.ROOT / "frontend/package-lock.json").read_bytes() + b"\0" +
                                    (installed.read_bytes() if installed.is_file() else b"")).hexdigest()
    marker.write_text(json.dumps({"frontend": source, "runtime": hashlib.sha256(cfg.RUNTIME.read_bytes()).hexdigest(),
                                  "dependencies": dependency_hash}))
    marker.chmod(0o600)


def ensure_application(config: cfg.Configuration) -> None:
    envfile = cfg.STATE / "application.env"
    if not envfile.exists():
        # A fresh synthetic volume gets a local password. Existing stacks must be adopted
        # with their original env first; no account, SQL or volume is reset here.
        result = subprocess.run(["docker", "volume", "ls", "--filter",
                                 f"label=com.docker.compose.project={config.project}", "--quiet"],
                                capture_output=True, text=True, check=True, timeout=15)
        if result.stdout.strip():
            raise cfg.LocalError("基础应用已有数据卷，但 application.env 缺失；请恢复原配置，不会生成新密码或重置数据。")
        envfile.write_text("YUDAO_DB_PASSWORD=" + secrets.token_urlsafe(32) + "\n")
        envfile.chmod(0o600)
    running = set(cfg.compose(config, "ps", "--services", "--status", "running", capture=True).split())
    if running == {"mysql", "redis", "server", "web"} and application_ready():
        print("芋道基础服务已就绪，复用当前实例。", flush=True)
        return
    if application_ready() and "web" not in running:
        raise cfg.LocalError("48161 已被其他应用实例占用；请检查 local.local.toml 的 compose_project。")
    cfg.compose(config, "up", "-d", "mysql", "redis", "server", "web")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if application_ready():
            return
        time.sleep(2)
    raise cfg.LocalError("芋道基础服务五分钟内未就绪；运行 ./local logs app 查看基础服务日志。")


def stop() -> None:
    pid = owned_pid()
    if pid is None:
        print("当前检出的本地助手未运行。")
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if owned_pid() is None:
            print("本地助手已停止，基础应用和数据保留。", flush=True)
            return
        time.sleep(.2)
    raise cfg.LocalError("助手仍在停止；运行 ./local logs 查看日志，不会强制终止其他进程。")


def start(config: cfg.Configuration, *, restart=False) -> None:
    ensure_application(config)
    build_assets()  # Build failure leaves the running assistant intact.
    current = health()
    expected = cfg.source_digest()
    if (not restart and current and current.get("ready") and current.get("source_digest") == expected
            and current.get("runtime_sha256") == hashlib.sha256(cfg.RUNTIME.read_bytes()).hexdigest()):
        try:
            status()
            return
        except (cfg.LocalError, OSError, ValueError):
            pass  # One controlled restart if an already running host is unhealthy.
    if owned_pid() is not None:
        stop()
    for port in cfg.PORTS:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise cfg.LocalError(f"端口 {port} 已被其他进程占用；未终止该进程。")
    logfile = cfg.STATE / "server.log"
    logfile.touch(mode=0o600, exist_ok=True)
    logfile.chmod(0o600)
    with logfile.open("ab") as log:
        child = subprocess.Popen([sys.executable, str(cfg.HERE / "serve.py")], cwd=cfg.ROOT,
            env=cfg.process_environment(), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise cfg.LocalError("助手启动失败；运行 ./local logs 查看日志。")
        ready = health()
        if ready and ready.get("ready") and ready.get("pid") == child.pid:
            status()
            return
        time.sleep(.2)
    child.terminate()
    raise cfg.LocalError("助手 45 秒内未就绪，已停止本次启动；运行 ./local logs 排查。")


def status() -> None:
    value = health()
    if not value or not value.get("ready") or not application_ready():
        raise cfg.LocalError("本地环境未就绪；先运行 ./local，失败时运行 ./local check 或 ./local logs。")
    with OPENER.open(cfg.ORIGIN + "/woa-agent/readyz", timeout=3) as response:
        ready = json.load(response)
    with OPENER.open(cfg.ORIGIN + "/woa-agent/v1/runtime/assistant.js", timeout=3) as response:
        served = hashlib.sha256(response.read()).hexdigest()
    if (not ready.get("ready") or value.get("source_digest") != cfg.source_digest()
            or served != hashlib.sha256(cfg.RUNTIME.read_bytes()).hexdigest()):
        raise cfg.LocalError("服务正在运行，但代码或 Runtime 未同步；运行 ./local 更新。")
    print(f"本地环境已就绪：{cfg.ORIGIN}\n当前 checkout 与 Runtime 一致；基础服务及助手健康检查通过。")


def log_text(text: str) -> str:
    sys.path[:0] = [str(cfg.ROOT / "src")]
    from web_operation_agent.modeling.client import load_env
    from web_operation_agent.privacy import (SENSITIVE_CREDENTIAL_NAME,
        redact_sensitive_assignments, redact_session_handoffs)
    for file in (cfg.ROOT / ".env", cfg.STATE / "application.env"):
        for key, value in load_env(file).items():
            if value and SENSITIVE_CREDENTIAL_NAME.search(key):
                text = text.replace(value, "[redacted]")
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text)
    return redact_session_handoffs(redact_sensitive_assignments(text))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="./local", description="本地合成数据只读验收；无参数启动或更新。")
    parser.add_argument("action", nargs="?", default="start", choices=("start", "restart", "stop", "status", "check", "logs"))
    parser.add_argument("component", nargs="?", choices=("app",), help="仅 logs 可选 app，查看基础应用日志")
    args = parser.parse_args(argv)
    if args.component and args.action != "logs":
        parser.error("app 仅用于 ./local logs app")
    try:
        if args.action == "check":
            check()
        elif args.action == "status":
            status()
        elif args.action == "logs":
            if args.component == "app":
                print(log_text(cfg.compose(cfg.configuration(), "logs", "--tail", "80", "server", "web", capture=True)))
            elif (cfg.STATE / "server.log").is_file():
                print(log_text("".join((cfg.STATE / "server.log").read_text(errors="replace").splitlines(keepends=True)[-80:])))
            else:
                print("尚无本地助手日志；运行 ./local 启动。")
        else:
            cfg.STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
            cfg.STATE.chmod(0o700)
            with (cfg.STATE / "command.lock").open("w") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise cfg.LocalError("另一个本地启动或停止命令正在执行。") from exc
                if args.action == "stop":
                    stop()
                else:
                    start(check(), restart=args.action == "restart")
        return 0
    except ImportError:
        print("本地 Python 依赖尚未安装；按 docs/guides/local-yudao.md 完成首次准备。", file=sys.stderr)
        return 1
    except (cfg.LocalError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"本地环境：{exc}\n操作指南：docs/guides/local-yudao.md", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
