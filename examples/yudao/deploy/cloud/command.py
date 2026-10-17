#!/usr/bin/env python3
"""The repository's routine release entry point; no application or model calls."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[4]
CLOUD = Path(__file__).resolve().parent
CACHE = ROOT / ".q0-cache" / "yudao"
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=3"]


class DeploymentError(RuntimeError):
    pass


def run(args: list[str], *, capture: bool = False, **kwargs) -> str:
    result = subprocess.run(args, cwd=ROOT, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None, **kwargs)
    return result.stdout.strip() if capture else ""


def configuration() -> tuple[str, str]:
    path = ROOT / "deploy.local.toml"
    if not path.is_file():
        raise DeploymentError("缺少 deploy.local.toml。复制 deploy.example.toml 并填写服务器和域名；见 docs/guides/deploy-yudao.md。")
    with path.open("rb") as handle:
        values = tomllib.load(handle)
    if set(values) != {"ssh_target", "public_origin"}:
        raise DeploymentError("deploy.local.toml 只接受 ssh_target 和 public_origin。缓存固定在 .q0-cache/yudao。")
    target, origin = values["ssh_target"], values["public_origin"]
    if not isinstance(target, str) or not re.fullmatch(r"(?:[a-zA-Z0-9_][a-zA-Z0-9_.-]*@)?[a-zA-Z0-9][a-zA-Z0-9.-]*", target):
        raise DeploymentError("ssh_target 应为 SSH 别名、主机名或 user@host。")
    if not isinstance(origin, str):
        raise DeploymentError("public_origin 应为 HTTPS 站点地址。")
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or not re.fullmatch(r"https://[a-zA-Z0-9.-]+(?::[0-9]+)?/?", origin)):
        raise DeploymentError("public_origin 应为 HTTPS 站点地址，不含路径、账号或查询参数。")
    return target, origin.rstrip("/")


def require_tools() -> None:
    missing = [name for name in ("git", "ssh", "rsync", "uv", "npm", "node", "unzip", "openssl", "python3")
               if not shutil.which(name)]
    if missing:
        raise DeploymentError("缺少本地命令：" + ", ".join(missing) + "；见 docs/guides/deploy-yudao.md。")


def check_cache() -> None:
    # One location and one active release. Never fall back to a dated sibling cache.
    for part, expected in (("server", "d3d00639c0"), ("ui", "9323ffa2ca")):
        if not (CACHE / part / ".git").exists():
            raise DeploymentError(f"缺少 .q0-cache/yudao/{part} 源码；按部署指南完成首次构建。")
        head = run(["git", "-C", str(CACHE / part), "rev-parse", "HEAD"], capture=True)
        if not head.startswith(expected):
            raise DeploymentError(f"{part} 版本不匹配（需要 {expected}）；停止发布，请按部署指南升级。")
    required = ["server/yudao-server/target/yudao-server.jar", "ui/dist-cloud/index.html",
                "server/sql/mysql/ruoyi-vue-pro.sql", "server/sql/mysql/quartz.sql", "business-schema.sql"]
    policy = json.loads((ROOT / "src/web_operation_agent/policies/page_controller.json").read_text())
    for name, entry in policy["served_files"].items():
        path = CACHE / "woa" / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise DeploymentError(f"PageController 文件缺失或版本不符：woa/{name}；见部署指南。")
    for name in required:
        if not (CACHE / name).is_file():
            raise DeploymentError(f"缺少构建产物：.q0-cache/yudao/{name}；按部署指南完成首次构建。")


def remote_check(target: str, origin: str) -> None:
    # Check existence and public configuration only. Never print server env files.
    domain = urlsplit(origin).hostname
    command = ("set -eu; test -d /opt/yudao/releases; test -w /opt/yudao/releases; "
               "test -f /opt/yudao/.env; test -f /opt/yudao/agent.env; "
               f"grep -qx 'YUDAO_DOMAIN={domain}' /opt/yudao/.env; "
               "command -v rsync >/dev/null; command -v flock >/dev/null; "
               "docker compose version >/dev/null; docker info >/dev/null; "
               "echo '服务器部署前置检查通过'")
    run(["ssh", *SSH_OPTIONS, target, command])


def status(target: str, origin: str) -> None:
    info = run(["ssh", *SSH_OPTIONS, target,
         "set -eu; cd /opt/yudao; readlink current; "
         "if test -f current/release.json; then cat current/release.json; "
         "else echo '当前版本尚无发布清单'; fi; "
         "curl --fail --silent --show-error --max-time 10 http://127.0.0.1:48091/readyz"], capture=True)
    print(info)
    with urlopen(origin + "/woa-agent/v1/runtime/assistant.js", timeout=20) as response:
        digest = hashlib.sha256(response.read()).hexdigest()
    print("线上助手脚本 SHA256:", digest)
    expected = re.search(r'"runtime_sha256"\s*:\s*"([a-f0-9]{64})"', info)
    if expected and expected.group(1) != digest:
        raise DeploymentError("线上助手脚本与发布清单不一致，请检查当前部署。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="./deploy", description="无参数发布；缓存固定为 .q0-cache/yudao。详见 docs/guides/deploy-yudao.md。")
    parser.add_argument("action", nargs="?", choices=("deploy", "check", "status"), default="deploy",
                        help="deploy 发布（默认）；check 只检查；status 查看线上版本")
    args = parser.parse_args(argv)
    try:
        target, origin = configuration()
        print(f"服务器：{target}  站点：{origin}", flush=True)
        if args.action == "status":
            status(target, origin)
            return 0
        require_tools()
        check_cache()
        remote_check(target, origin)
        if args.action == "check":
            print("检查通过；未构建或发布。缓存：.q0-cache/yudao")
            return 0
        with (ROOT / ".q0-cache/deploy.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DeploymentError("本机已有部署正在执行，请等待它完成。") from exc
            print("[1/2] 构建当前助手前端", flush=True)
            dependency_lock = ROOT / "frontend/package-lock.json"
            installed_lock = ROOT / "frontend/node_modules/.package-lock.json"
            marker = ROOT / ".q0-cache/deploy-frontend.sha256"

            def dependency_digest() -> str:
                return hashlib.sha256(dependency_lock.read_bytes() + b"\0" +
                                      (installed_lock.read_bytes() if installed_lock.is_file() else b"")).hexdigest()

            install = [] if (installed_lock.is_file() and marker.is_file()
                             and marker.read_text() == dependency_digest()) else ["--install"]
            run([sys.executable, str(ROOT / "scripts/build_panel.py"), *install])
            run([sys.executable, str(ROOT / "scripts/build_runtime.py"), "--skip-build"])
            marker.write_text(dependency_digest())
            print("[2/2] 打包、上传、切换并核验服务", flush=True)
            env = dict(os.environ, YUDAO_CACHE=str(CACHE), WOA_PUBLIC_ORIGIN=origin)
            # Do not inherit a profile or SSH option that changes the reviewed release path.
            env.pop("COMPOSE_PROFILES", None)
            run(["sh", str(CLOUD / "deploy.sh"), target], env=env)
        print(f"部署完成：{origin}；可运行 ./deploy status 核对线上版本。")
        return 0
    except (DeploymentError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"部署失败：{exc}", file=sys.stderr)
        print("排查入口：docs/guides/deploy-yudao.md", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
