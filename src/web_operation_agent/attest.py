"""Bind source evidence to the exact deployment that will execute an agent run.

The running application, source tree, enforcing proxy, control gateway, and browser build
must agree. A single ``attest()`` collects those facts, one ``binding_id`` covers them, and
``require_binding()`` checks later use against that original state.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .controlplane import ControlGatewayConfig, ControlPlaneError
from .schemas import digest
from .sourcetree import SourceTree


class AttestationError(Exception):
    """Raised for any drift. Callers turn this into terminal reason `attestation_failed`."""


def _object(value: object, name: str, required: set[str],
            optional: frozenset[str] = frozenset()) -> dict:
    if not isinstance(value, dict):
        raise AttestationError(f"deployment {name} must be an object")
    missing = required - set(value)
    unknown = set(value) - required - optional
    if missing:
        raise AttestationError(
            f"deployment {name} is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise AttestationError(
            f"deployment {name} has unknown fields: {', '.join(sorted(unknown))}")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AttestationError(f"deployment {name} must be a non-empty string")
    return value.strip()


def _deployment_path(value: object, name: str, base_dir: Path) -> Path:
    raw = _text(value, name)
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _application_url(value: object) -> str:
    url = _text(value, "application.base_url").rstrip("/")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise AttestationError(
            "deployment application.base_url must be an http(s) URL without credentials, "
            "query or fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise AttestationError(
            "plain HTTP application URLs are allowed only on loopback")
    return url


@dataclass(frozen=True, slots=True)
class DeploymentConfig:
    """Validated, deployment-owned inputs required before attestation may inspect a runtime."""

    deployment_id: str
    source_policy: str
    source_root: Path
    application_base_url: str
    application_container: str
    expected_application_version: str
    application_version_probe: tuple[str, ...]
    proxy_container: str
    proxy_source: Path
    gateway: ControlGatewayConfig

    def __post_init__(self) -> None:
        for name in ("deployment_id", "source_policy", "application_container",
                     "expected_application_version", "proxy_container"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if any(char in self.source_policy for char in ("/", "\\")) \
                or self.source_policy in {".", ".."}:
            raise ValueError("source_policy must be a package policy name, not a path")
        for name in ("source_root", "proxy_source"):
            path = Path(getattr(self, name))
            if not path.is_absolute():
                raise ValueError(f"{name} must be an absolute path")
            object.__setattr__(self, name, path.resolve())
        object.__setattr__(self, "application_base_url",
                           _application_url(self.application_base_url))
        if (not isinstance(self.application_version_probe, tuple)
                or not self.application_version_probe
                or any(not isinstance(part, str) or not part or len(part) > 512
                       for part in self.application_version_probe)):
            raise ValueError("application_version_probe must be a non-empty tuple of argv strings")
        if not isinstance(self.gateway, ControlGatewayConfig):
            raise TypeError("gateway must be a ControlGatewayConfig")

    @classmethod
    def from_json(cls, value: object, *, base_dir: str | Path) -> "DeploymentConfig":
        """Parse the strict deployment JSON shape, resolving paths beside that JSON file."""
        root = _object(value, "document", {
            "schema_version", "deployment_id", "source", "application", "enforcement"})
        if root["schema_version"] != 1:
            raise AttestationError("deployment schema_version must be 1")
        source = _object(root["source"], "source", {"policy", "root"})
        application = _object(root["application"], "application", {
            "base_url", "container", "expected_version", "version_probe"})
        enforcement = _object(root["enforcement"], "enforcement", {
            "proxy_container", "proxy_source", "gateway"})
        gateway = _object(enforcement["gateway"], "enforcement.gateway", {
            "base_url", "key_file", "key_header"}, {"timeout_s"})
        base = Path(base_dir).expanduser().resolve()
        probe = application["version_probe"]
        if not isinstance(probe, list):
            raise AttestationError("deployment application.version_probe must be an argv array")
        try:
            gateway_config = ControlGatewayConfig(
                base_url=_text(gateway["base_url"], "enforcement.gateway.base_url"),
                key_file=_deployment_path(
                    gateway["key_file"], "enforcement.gateway.key_file", base),
                key_header=_text(gateway["key_header"], "enforcement.gateway.key_header"),
                timeout_s=gateway.get("timeout_s", 15.0),
            )
            return cls(
                deployment_id=_text(root["deployment_id"], "deployment_id"),
                source_policy=_text(source["policy"], "source.policy"),
                source_root=_deployment_path(source["root"], "source.root", base),
                application_base_url=_application_url(application["base_url"]),
                application_container=_text(application["container"], "application.container"),
                expected_application_version=_text(
                    application["expected_version"], "application.expected_version"),
                application_version_probe=tuple(probe),
                proxy_container=_text(
                    enforcement["proxy_container"], "enforcement.proxy_container"),
                proxy_source=_deployment_path(
                    enforcement["proxy_source"], "enforcement.proxy_source", base),
                gateway=gateway_config,
            )
        except (TypeError, ValueError) as exc:
            raise AttestationError(f"invalid deployment configuration: {exc}") from exc

    @classmethod
    def from_file(cls, path: str | Path) -> "DeploymentConfig":
        config_path = Path(path).expanduser().resolve()
        try:
            value = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AttestationError(
                f"deployment configuration {config_path} is unreadable: {exc}") from exc
        return cls.from_json(value, base_dir=config_path.parent)


@dataclass
class EnvironmentAttestation:
    deployment_id: str
    source: dict
    application: dict
    enforcement: dict
    browser: dict
    binding_id: str = ""
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.binding_id:
            self.binding_id = digest({"deployment_id": self.deployment_id,
                                      "source": self.source, "application": self.application,
                                      "enforcement": self.enforcement, "browser": self.browser})

    @property
    def source_binding(self) -> str:
        """Identity of the source evidence alone.

        The full `binding_id` also covers the running container and the browser build, which
        is right for a run but wrong for a SourceMap: recompiling 37k relations because a
        Chromium revision moved would be pure waste, and keying the cache on the wider value
        made an offline `compile` and an online `run` disagree about the same tree.
        """
        return digest(self.source)

    def to_json(self) -> dict:
        return {"deployment_id": self.deployment_id,
                "source": self.source, "application": self.application,
                "enforcement": self.enforcement, "browser": self.browser,
                "binding_id": self.binding_id, "warnings": list(self.warnings)}

    @staticmethod
    def from_json(d: dict) -> "EnvironmentAttestation":
        return EnvironmentAttestation(deployment_id=d["deployment_id"],
                                      source=d["source"], application=d["application"],
                                      enforcement=d["enforcement"], browser=d["browser"],
                                      binding_id=d.get("binding_id", ""),
                                      warnings=list(d.get("warnings", [])))


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _docker(*args: str, timeout: int = 30) -> str:
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise AttestationError(f"docker {' '.join(args)} failed: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def _chromium_identity() -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:                                  # pragma: no cover - env specific
        raise AttestationError(f"playwright is not importable: {e}")
    with sync_playwright() as pw:
        path = pw.chromium.executable_path
    # The build revision is the directory playwright installed it under; the executable
    # itself is hundreds of MB and hashing it on every run buys nothing.
    revision = ""
    for part in Path(path).parts:
        if part.startswith("chromium"):
            revision = part
            break
    return {"executable": path, "revision": revision, "present": os.path.exists(path)}


def attest(*, deployment: DeploymentConfig, include_browser: bool = True,
           surface_digest_cache: str | None = None) -> EnvironmentAttestation:
    """Collect the whole binding. Any component that cannot be read is an error, not a warning.

    Every deployment-specific value is already present in ``deployment``. Local source,
    proxy and key evidence is validated before Docker or network inspection begins.
    """
    if not isinstance(deployment, DeploymentConfig):
        raise TypeError("attest requires an explicit DeploymentConfig")
    app_container = deployment.application_container
    proxy_container = deployment.proxy_container
    px = deployment.proxy_source
    if not px.is_file():
        raise AttestationError(f"control proxy evidence {px} is not a readable file")
    try:
        proxy_sha256 = _sha_file(px)
    except OSError as e:
        raise AttestationError(f"control proxy evidence {px} is unreadable: {e}") from e
    if not deployment.source_root.is_dir():
        raise AttestationError(
            f"source tree {deployment.source_root} is not present")
    try:
        key = deployment.gateway.read_key()
    except ControlPlaneError as exc:
        raise AttestationError(str(exc)) from exc

    tree = SourceTree.load(deployment.source_policy, deployment.source_root)
    if not tree.available():
        raise AttestationError(f"source tree {tree.root} is not present")
    files = tree.files()
    if not files:
        raise AttestationError("the allowlist matched no files; the policy or the tree is wrong")
    sd = _cached_surface_digest(tree, surface_digest_cache)
    source = {"repository_id": tree.repository_id, "release": tree.release,
              "root": str(tree.root), "file_count": len(files), "surface_digest": sd,
              "policy_digest": digest(tree.policy)}

    version = _docker(
        "exec", app_container, *deployment.application_version_probe).strip()
    running = _docker("inspect", "-f", "{{.State.Running}}", app_container)
    if running != "true":
        raise AttestationError(f"application container {app_container} is not running")
    application = {"container": app_container, "version": version,
                   "base_url": deployment.application_base_url,
                   "version_probe": list(deployment.application_version_probe),
                   "image": _docker("inspect", "-f", "{{.Config.Image}}", app_container)}
    if deployment.expected_application_version != version:
        raise AttestationError(
            f"deployed application reports {version!r}, the deployment expects "
            f"{deployment.expected_application_version!r}")

    proxy_running = _docker("inspect", "-f", "{{.State.Running}}", proxy_container)
    if proxy_running != "true":
        raise AttestationError(f"enforcing proxy {proxy_container} is not running")
    mounts = _docker("inspect", "-f", "{{range .Mounts}}{{.Source}}:{{.Destination}}:{{.RW}} {{end}}",
                     proxy_container)
    proxy_mounts = [m for m in mounts.split() if m.startswith(f"{px}:")]
    if not proxy_mounts:
        raise AttestationError(f"enforcing proxy does not have {px} mounted: {mounts}")
    if any(m.endswith(":true") for m in proxy_mounts):
        raise AttestationError("enforcing proxy script is mounted read-write; it must be read-only")

    gateway_health = deployment.gateway.base_url + "/admin/health"
    req = urllib.request.Request(
        gateway_health, headers={deployment.gateway.key_header: key})
    try:
        with urllib.request.urlopen(req, timeout=deployment.gateway.timeout_s) as r:
            health = json.load(r)
    except Exception as e:
        raise AttestationError(f"control gateway is unreachable at {gateway_health}: {e}")
    if not health.get("ok"):
        raise AttestationError(f"control gateway is unhealthy: {health}")
    loaded_sha256 = health.get("gateway_loaded_sha256")
    if (not isinstance(loaded_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", loaded_sha256) is None):
        raise AttestationError(
            "control gateway health gateway_loaded_sha256 must be 64 lowercase hex characters")
    if loaded_sha256 != proxy_sha256:
        raise AttestationError(
            "control gateway loaded proxy digest does not match deployment proxy evidence: "
            f"{loaded_sha256} != {proxy_sha256}")
    enforcement_state = {"proxy_container": proxy_container, "proxy_sha256": proxy_sha256,
                         "gateway_base_url": deployment.gateway.base_url,
                         "gateway_key_header": deployment.gateway.key_header,
                         "gateway_loaded_sha256": loaded_sha256,
                         "gateway_webapps": health.get("webapps")}

    browser = _chromium_identity() if include_browser else {"skipped": True}
    return EnvironmentAttestation(deployment_id=deployment.deployment_id,
                                  source=source, application=application,
                                  enforcement=enforcement_state, browser=browser)


def _cached_surface_digest(tree: SourceTree, cache_path: str | None) -> str:
    """Hashing ~1.5k descriptors takes a couple of seconds; a run does it once.

    The cache is keyed by the file list and their mtimes+sizes, so an edited descriptor
    invalidates it. A stale cache would defeat the whole point of the attestation.
    """
    files = tree.files()
    stamp = digest([[f, os.stat(tree.root / f).st_mtime_ns, os.stat(tree.root / f).st_size]
                    for f in files])
    if cache_path:
        p = Path(os.path.expanduser(cache_path))
        if p.exists():
            try:
                blob = json.loads(p.read_text())
                if blob.get("stamp") == stamp:
                    return blob["surface_digest"]
            except Exception:
                pass
    sd = tree.surface_digest()
    if cache_path:
        p = Path(os.path.expanduser(cache_path))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"stamp": stamp, "surface_digest": sd}))
    return sd


def require_binding(current: EnvironmentAttestation, captured: dict | None) -> None:
    """Reject drift from a previously captured deployment attestation."""
    if captured is None:
        return
    if current.binding_id == captured.get("binding_id"):
        return
    diffs = []
    if current.deployment_id != captured.get("deployment_id", "legacy"):
        diffs.append(
            f"deployment_id: {captured.get('deployment_id')!r} -> {current.deployment_id!r}")
    for section in ("source", "application", "enforcement", "browser"):
        was, now = captured.get(section, {}), getattr(current, section)
        for k in sorted(set(was) | set(now)):
            if was.get(k) != now.get(k):
                diffs.append(f"{section}.{k}: {was.get(k)!r} -> {now.get(k)!r}")
    raise AttestationError("environment binding drifted:\n  " + "\n  ".join(diffs))
