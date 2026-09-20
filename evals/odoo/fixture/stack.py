"""Lifecycle of the isolated ``p74_odoo_v1`` Compose project on an evaluation host.

Every destructive command first proves that the container it touches carries the expected
Compose project label, so a mistyped project name cannot reset a developer's fixture or a
pilot deployment. The Agent gets a new, verified-empty state directory and a new provider
token before each attempt; it is never assumed that a container restart cleared anything,
because the state lives on a named volume that survives recreation.

Images are built only from a clean checkout and are labelled with its full commit, and the
running Agent image is checked against that clean checkout before its results can describe a
candidate.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.odoo.fixture.revision import RevisionError, repository_state, require_clean_head, require_commit

REPOSITORY = Path(__file__).resolve().parents[3]
BASE_COMPOSE = REPOSITORY / "examples" / "odoo" / "deploy" / "single-node" / "docker-compose.yml"
OVERLAY_COMPOSE = REPOSITORY / "evals" / "odoo" / "fixture" / "compose.p74.yml"
DATABASE = "woa_trial"
_PROJECT = re.compile(r"woa-p74-[a-z0-9][a-z0-9-]{1,40}\Z")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
REVISION_LABEL = "org.opencontainers.image.revision"
#: Runs inside the Agent container: session and unresolved-mutation counts from the internal
#: metrics endpoint, the digests of prepared journal reservations and the number of retained
#: conversation contexts.
AGENT_STATE_PROGRAM = (
    "import json,sqlite3,urllib.request,pathlib\n"
    "m=json.loads(urllib.request.urlopen('http://127.0.0.1:8767/internal/metrics',"
    "timeout=5).read())\n"
    "def rows(db,sql):\n"
    " p=pathlib.Path('/var/lib/woa')/db\n"
    " if not p.exists(): return []\n"
    " c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)\n"
    " try:\n"
    "  return [r[0] for r in c.execute(sql).fetchall()]\n"
    " except sqlite3.OperationalError as e:\n"
    "  if 'no such table' in str(e): return []\n"
    "  raise\n"
    " finally:\n"
    "  c.close()\n"
    "print(json.dumps({'active_sessions':m['active_sessions'],"
    "'unresolved_mutations':m['unresolved_mutations'],"
    "'prepared_proposals':rows('mutation-journal.sqlite3',"
    "\"select proposal_digest from mutation_intents where state='prepared' "
    "order by proposal_digest\"),"
    "'conversation_rows':(rows('conversation-state.sqlite3',"
    "'select count(*) from conversation_contexts') or [0])[0]}))")


class StackError(RuntimeError):
    """A stack command failed or targeted something outside the evaluation project."""


@dataclass(frozen=True)
class Stack:
    private_dir: Path
    project: str = "woa-p74-odoo-v1"
    #: The full commit images are built from and labelled with; the checkout's HEAD by default.
    build_revision: str | None = None
    https_port: int = 19443
    rpc_port: int = 19069
    gateway_port: int = 18090
    requested_model: str = "qwen3.8-flash"
    #: Qualification-only overlays, such as the domain-mutation probe's adapter mount.
    extra_compose: tuple[Path, ...] = ()
    runner: Any = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not _PROJECT.fullmatch(self.project):
            raise StackError("evaluation stacks must use an explicit woa-p74-* project name")
        object.__setattr__(self, "private_dir", Path(self.private_dir).resolve())
        try:
            revision = (repository_state()["commit"] if self.build_revision is None
                        else require_commit(self.build_revision, "build_revision"))
        except RevisionError as exc:
            raise StackError(str(exc)) from exc
        object.__setattr__(self, "build_revision", revision)

    # -- configuration ---------------------------------------------------------------------
    @property
    def stack_inputs(self) -> Path:
        return self.private_dir / "stack"

    @property
    def base_url(self) -> str:
        return f"https://localhost:{self.https_port}"

    @property
    def rpc_url(self) -> str:
        return f"http://127.0.0.1:{self.rpc_port}"

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("COMPOSE_PROJECT_NAME", None)
        env.pop("COMPOSE_PROFILES", None)
        env.update({
            "WOA_PROJECT_NAME": self.project,
            "WOA_PRIVATE_DIR": str(self.stack_inputs),
            "WOA_PUBLIC_ORIGIN": self.base_url,
            "WOA_HTTPS_PORT": str(self.https_port),
            "WOA_BUILD_REVISION": self.build_revision,
            "P74_EVALS_DIR": str(REPOSITORY / "evals" / "odoo"),
            "P74_GATEWAY_PORT": str(self.gateway_port),
            "P74_ODOO_RPC_PORT": str(self.rpc_port),
        })
        return env

    def run(self, argv: list[str], *, input_bytes: bytes | None = None,
            timeout: int = 900, check: bool = True) -> subprocess.CompletedProcess:
        try:
            completed = subprocess.run(argv, input=input_bytes, capture_output=True,
                                       timeout=timeout, env=self.env(), cwd=REPOSITORY,
                                       check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StackError(f"{' '.join(argv[:3])} could not complete") from exc
        if check and completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip()[-600:]
            raise StackError(f"{' '.join(argv[:5])} exited {completed.returncode}: {detail}")
        return completed

    def compose(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess:
        overlays = [item for path in self.extra_compose for item in ("-f", str(path))]
        return self.run(["docker", "compose", "-p", self.project, "-f", str(BASE_COMPOSE),
                         "-f", str(OVERLAY_COMPOSE), *overlays, *args], **kwargs)

    def container(self, service: str, *, running: bool = True) -> str:
        """The single container of ``service``, proven to belong to this project.

        Destructive callers use the default and get only a running container; read-only
        inspection may name a stopped one.
        """
        listed = self.compose("ps", "-q", *(() if running else ("-a",)),
                              service).stdout.decode().split()
        if len(listed) != 1:
            raise StackError(f"service {service} is not one {'running ' if running else ''}"
                             "container")
        label = self.run(["docker", "inspect", "--format",
                          '{{ index .Config.Labels "com.docker.compose.project" }}',
                          listed[0]]).stdout.decode().strip()
        if label != self.project:
            raise StackError(f"service {service} belongs to project {label!r}")
        return listed[0]

    def psql(self, sql: str, *, database: str = DATABASE, timeout: int = 300) -> str:
        return self.run(["docker", "exec", "-i", self.container("db"), "psql", "-U", "odoo",
                         "-d", database, "-v", "ON_ERROR_STOP=1", "-tAc", sql],
                        timeout=timeout).stdout.decode("utf-8", "replace")

    # -- the candidate image -------------------------------------------------------------------
    def build(self, *services: str) -> dict[str, Any]:
        """Build images from a clean checkout at this stack's revision, labelled with it."""
        try:
            state = require_clean_head(commit=self.build_revision)
        except RevisionError as exc:
            raise StackError(f"refusing to build: {exc}") from exc
        self.compose("build", *(services or ("odoo", "agent")), timeout=3600)
        return state

    def candidate(self) -> dict[str, Any]:
        """The running Agent image and the commit its revision label names."""
        container = self.container("agent", running=False)
        image = self.run(["docker", "inspect", "--format", "{{.Image}}",
                          container]).stdout.decode().strip()
        if not _IMAGE_ID.fullmatch(image):
            raise StackError("the Agent image id is not a content digest")
        label = self.run(["docker", "image", "inspect", "--format",
                          f'{{{{index .Config.Labels "{REVISION_LABEL}"}}}}',
                          image]).stdout.decode().strip()
        return {"commit": self.build_revision, "agent_image_id": image,
                "agent_image_revision": label}

    def verify_candidate(self) -> dict[str, Any]:
        """The candidate, provided the running Agent image was built from this clean HEAD."""
        record = self.candidate()
        try:
            require_clean_head(commit=self.build_revision)
        except RevisionError as exc:
            raise StackError(f"the candidate cannot be verified: {exc}") from exc
        if record["agent_image_revision"] != self.build_revision:
            raise StackError("the running Agent image was not built from this checkout's HEAD")
        return record

    # -- the Agent -------------------------------------------------------------------------
    def write_model_env(self, token: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{24,200}", token):
            raise StackError("provider token has an unexpected shape")
        path = self.stack_inputs / "model.env"
        body = (f"AGENT_OPENAI_BASE_URL=http://127.0.0.1:18080/v1\n"
                f"AGENT_OPENAI_API_KEY={token}\n"
                f"AGENT_OPENAI_MODEL={self.requested_model}\n"
                "AGENT_PROVIDER=p74-gateway\n")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(body)
        os.chmod(path, 0o600)

    def _agent_python(self, code: str) -> str:
        return self.compose("exec", "-T", "agent", "python", "-c", code,
                            timeout=60).stdout.decode().strip()

    def fresh_agent(self, token: str, *, timeout_s: float = 90) -> dict[str, Any]:
        """Stop, wipe and verify an empty state directory, then start with a new token."""
        self.compose("stop", "agent", "provider-relay", timeout=120)
        self.compose("run", "--rm", "--no-deps", "--entrypoint", "sh", "agent", "-c",
                     "find /var/lib/woa -mindepth 1 -maxdepth 1 -exec rm -rf {} + && "
                     "test -z \"$(find /var/lib/woa -mindepth 1 -print -quit)\"",
                     timeout=120)
        self.write_model_env(token)
        self.compose("up", "-d", "--no-deps", "--force-recreate", "agent", "provider-relay",
                     timeout=300)
        self._wait_healthy(timeout_s)
        self.container("provider-relay")
        state = self.agent_state()
        if (state["active_sessions"] or state["unresolved_mutations"]
                or state["prepared_proposals"] or state["conversation_rows"]):
            raise StackError("the fresh Agent state is not empty")
        return state

    def end_orphaned_sessions(self, *, timeout_s: float = 90) -> dict[str, Any]:
        """Restart the Agent on its current state directory and return that state.

        A driver that died mid-attempt never logged out. Sessions live only in the Agent
        process, so the restart ends them and nothing can act for the dead attempt afterwards,
        while the durable journal and conversation state stay on the volume to be asserted.
        The provider relay is not started: recovery makes no model request.
        """
        self.compose("restart", "agent", timeout=300)
        self._wait_healthy(timeout_s)
        return self.agent_state()

    def _wait_healthy(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            probe = self.compose(
                "exec", "-T", "agent", "python", "-c",
                "import urllib.request;print(urllib.request.urlopen("
                "'http://127.0.0.1:8766/healthz',timeout=2).status)", check=False, timeout=30)
            if probe.returncode == 0 and probe.stdout.decode().strip() == "200":
                break
            time.sleep(1)
        else:
            raise StackError("the Agent did not become healthy")
        self.container("agent")

    def agent_state(self) -> dict[str, Any]:
        """Content-free session, mutation and conversation state from inside the Agent.

        Counts, plus the digests of journal reservations still ``prepared``: whether such a
        reservation is residue depends on whether the audit closed its proposal. A database
        that cannot be read fails the call; only a table that does not exist yet reads as empty.
        """
        return json.loads(self._agent_python(AGENT_STATE_PROGRAM).splitlines()[-1])

    def export_audit(self) -> list[dict[str, Any]]:
        """Every Agent audit event of the current state directory, in file order."""
        raw = self.compose("exec", "-T", "agent", "sh", "-c",
                           "for f in /var/lib/woa/runs/*/audit.jsonl; do "
                           "[ -f \"$f\" ] && cat \"$f\"; done; true",
                           timeout=60).stdout.decode("utf-8", "replace")
        events = []
        for line in raw.splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events
