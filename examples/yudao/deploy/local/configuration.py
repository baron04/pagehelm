"""Local-only deployment settings, shared by its launcher and HTTP front."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent
CACHE = ROOT / ".q0-cache/yudao"
STATE_ROOT = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "web-operation-agent/yudao"
PORTS = (48163, 48164, 48165, 48166)  # Same inside both assistant containers.
POLICIES = ROOT / "examples/yudao/deploy/cloud/native-policy"
RUNTIME = ROOT / "src/web_operation_agent/web_runtime/assistant.js"
RELEASE = "v2026.09"


class LocalError(RuntimeError):
    pass


@dataclass(frozen=True)
class Configuration:
    number: int = 1
    test_clock: str | None = None
    test_clock_library: str | None = None

    def __post_init__(self):
        if type(self.number) is not int or self.number not in (1, 2):
            raise LocalError("环境编号只接受1或2。")

    @property
    def project(self) -> str:
        return f"woa-yudao-env{self.number}"

    @property
    def port(self) -> int:
        return 40000 + self.number

    @property
    def origin(self) -> str:
        return f"http://localhost:{self.port}"

    @property
    def state(self) -> Path:
        return STATE_ROOT / f"env{self.number}"

    @property
    def command(self) -> str:
        return f"./local --env {self.number}"


def dependency_digest() -> str:
    digest = hashlib.sha256()
    for path in (ROOT / "requirements.lock", HERE / "agent.Dockerfile", HERE / "agent.Dockerfile.dockerignore"):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:20]


def image_name() -> str:
    return "woa-yudao-agent-dev:" + dependency_digest()


def process_environment(config: Configuration | None = None) -> dict[str, str]:
    config = config or Configuration()
    env = dict(os.environ, YUDAO_CACHE=str(CACHE), LOCAL_CHECKOUT=str(ROOT),
               LOCAL_PUBLIC_PORT=str(config.port), LOCAL_PUBLIC_ORIGIN=config.origin, LOCAL_AGENT_IMAGE=image_name(),
               LOCAL_AGENT_ENV_FILE=str(config.state / "agent.env"),
               LOCAL_ENVIRONMENT=str(config.number), LOCAL_CHECKOUT_ID=checkout_id(),
               LOCAL_CGROUP_PARENT=cgroup_parent(config),
               LOCAL_UID=str(os.getuid()), LOCAL_GID=str(os.getgid()),
               PYTHONPATH=os.pathsep.join((str(ROOT / "src"), str(ROOT / "examples/yudao/src"))),
               NO_PROXY="localhost,127.0.0.1,::1", no_proxy="localhost,127.0.0.1,::1")
    env.pop("COMPOSE_PROFILES", None)
    return env


#: One parent cgroup per environment holds every container of it, including a
#: measurement host. The services share these totals instead of fixed per-service
#: caps; the two environments have equal CPU weight.
ENVIRONMENT_CPUS = 7
ENVIRONMENT_MEMORY_BYTES = 4 * 1024 ** 3
_CGROUP_HELPER = "redis:latest"  # an image the deployment already pulls; only its shell is used


def cgroup_parent(config: Configuration) -> str:
    return config.project


def _cgroup(script: str) -> str:
    result = subprocess.run(["docker", "run", "--rm", "--privileged", "--cgroupns=host", "--network", "none",
                             "-v", "/sys/fs/cgroup:/sys/fs/cgroup", "--entrypoint", "sh", _CGROUP_HELPER,
                             "-c", script], capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise LocalError("无法在 Docker 虚拟机中设置该环境的资源组。")
    return result.stdout


def environment_resources(config: Configuration) -> dict:
    """The effective totals of this environment's parent group, read from the Linux VM."""
    group = "/sys/fs/cgroup/" + cgroup_parent(config)
    out = _cgroup(f'cd {group} && cat cpu.max memory.max cpu.weight && '
                  f'(cat memory.swap.max 2>/dev/null || echo none)').split("\n")
    quota, period = out[0].split()
    return {"group": cgroup_parent(config), "cpus": int(quota) / int(period), "memory_bytes": int(out[1]),
            "cpu_weight": int(out[2]), "swap": out[3]}


def ensure_resources(config: Configuration) -> dict:
    """Create or reset this environment's parent group; never touches the other one.

    The VM loses the group when Docker restarts, so every start runs this first and
    refuses to start services if the read-back totals differ."""
    group = "/sys/fs/cgroup/" + cgroup_parent(config)
    _cgroup(f'set -e; mkdir -p {group}; '
            f'echo "+cpu +memory +pids" > {group}/cgroup.subtree_control; '
            f'echo "{ENVIRONMENT_CPUS * 100000} 100000" > {group}/cpu.max; '
            f'echo {ENVIRONMENT_MEMORY_BYTES} > {group}/memory.max; '
            f'echo 100 > {group}/cpu.weight; '
            f'if [ -f {group}/memory.swap.max ]; then echo 0 > {group}/memory.swap.max; fi')
    actual = environment_resources(config)
    if (actual["cpus"] != ENVIRONMENT_CPUS or actual["memory_bytes"] != ENVIRONMENT_MEMORY_BYTES
            or actual["cpu_weight"] != 100 or actual["swap"] not in {"0", "none"}):
        raise LocalError("该环境资源组的实际配额与配置不一致。")
    return actual


def compose(config: Configuration, *args: str, capture: bool = False, quiet: bool = False) -> str:
    envfile = config.state / "application.env"
    files = ["-f", str(ROOT / "examples/yudao/deploy/compose.yaml"), "-f", str(HERE / "compose.yaml")]
    source = config.state / "source.compose.json"
    if source.is_file():
        files += ["-f", str(source)]
    result = subprocess.run(["docker", "compose", "-p", config.project,
                             "--env-file", str(envfile) if envfile.is_file() else os.devnull,
                             *files, *args], cwd=ROOT,
                            env=process_environment(config), check=True, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if quiet else None, timeout=300)
    return result.stdout.strip() if capture else ""


def checkout_id() -> str:
    return hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]


def source_mounts(config: Configuration) -> list[dict]:
    """Mount code and reviewed assets, excluding credentials, caches and browser state."""
    paths = [ROOT / part for part in (
        "src", "examples/yudao/src", "examples/yudao/deploy/local",
        "examples/yudao/deploy/cloud/native-policy", "examples/yudao/deploy/cloud/yudao-loader.js",
        "frontend/src", "frontend/scripts", "frontend/trace", "frontend/dist/integrations/yudao",
        "scripts/build_panel.py", "scripts/build_runtime.py", "requirements.lock")]
    paths += sorted(p for p in (ROOT / "frontend").iterdir() if p.is_file() and p.suffix in {".json", ".ts"})
    paths += [CACHE / "ui/src", CACHE / "woa"]
    if (CACHE / "server").is_dir():
        paths += sorted(
            p / "src/main/java" for p in (CACHE / "server").rglob("yudao-module-*")
            if p.is_dir() and (p / "src/main/java").is_dir()
        )
    mounts = [{"type": "bind", "source": str(path), "target": "/workspace/" + str(path.relative_to(ROOT)),
               "read_only": True, "bind": {"create_host_path": False}} for path in paths]
    mounts.append({"type": "bind", "source": str(config.state / "state"), "target": "/var/lib/woa-yudao",
                   "bind": {"create_host_path": False}})
    return mounts


def source_composition(config: Configuration) -> str:
    deployment = hashlib.sha256()
    for path in (ROOT / "examples/yudao/deploy/compose.yaml", HERE / "compose.yaml"):
        deployment.update(path.read_bytes() + b"\0")
    deployment.update(json.dumps({"port": config.port, "origin": config.origin, "image": image_name(),
                                 "uid": os.getuid(), "gid": os.getgid()}, sort_keys=True).encode())
    # This digest stays in the private Compose metadata, never the public health
    # response. It identifies what was applied even after a failed update attempt.
    deployment.update((config.state / "agent.env").read_bytes())
    agent = {"volumes": source_mounts(config),
             "labels": {"org.web-operation-agent.deployment": deployment.hexdigest()}}
    from test_clock import composition
    clockfile = config.state / "test-clock.json"
    clock = json.loads(clockfile.read_text()) if clockfile.is_file() else None
    return json.dumps({"services": {"agent": agent, **composition(clock)}}, sort_keys=True, indent=2) + "\n"


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
    if not frontend_only:
        files.extend((ROOT / "requirements.lock", HERE / "agent.Dockerfile", HERE / "agent.Dockerfile.dockerignore"))
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
    from web_operation_agent_yudao.host.native_server import native_write_options
    if not str(values.get("WOA_NATIVE_WRITES") or "").strip():
        values = {**values, "WOA_NATIVE_WRITES": "true"}
    native_write_options(values)
    return {key: values[key] for key in (*required, "AGENT_PROVIDER", "WOA_NATIVE_WRITES") if values.get(key)}
