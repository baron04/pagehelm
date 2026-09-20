"""The hidden control plane: what the user authorised, installed before the agent runs.

Authorisation is derived from the public task specification by the operator and installed
before execution. The agent cannot declare or broaden its own write permission, and the
installed plan is never shown to the model, prompt, contract or browser.

The enforcement point itself is unchanged: the out-of-process gateway in front of the
application, which fails closed on anything it was not given a permit for. This module only
installs permits and reads back the decision trace.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..schemas import ControlPlan, ControlTicket

try:
    import fcntl
except ImportError:  # pragma: no cover - the supported runtime is POSIX.
    fcntl = None


class ControlPlaneError(Exception):
    """Callers map this to terminal reason `control_plane_error`. Never fail open."""


_HEADER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MAX_CONTROL_RESPONSE_BYTES = 1024 * 1024


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True, slots=True)
class ControlGatewayConfig:
    """Explicit connection material for one deployment's control gateway.

    The secret itself stays in a deployment-owned file.  Requiring the endpoint, file and
    header together prevents an installed package from silently targeting a developer fixture.
    """

    base_url: str
    key_file: Path
    key_header: str
    timeout_s: float = 15.0

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str):
            raise ValueError("control gateway base_url must be a string")
        parsed = urllib.parse.urlsplit(self.base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise ValueError("control gateway base_url must be an http(s) URL without credentials, query or fragment")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("plain HTTP control gateways are allowed only on loopback")
        path = Path(os.path.expanduser(os.fspath(self.key_file)))
        if not path.is_absolute():
            raise ValueError("control gateway key_file must be an absolute path")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        # Normalise `..` without resolving the final component. `read_key()` uses
        # O_NOFOLLOW, which would be defeated if configuration eagerly dereferenced a
        # caller-supplied symlink here.
        object.__setattr__(self, "key_file", Path(os.path.abspath(path)))
        if (not isinstance(self.key_header, str)
                or not _HEADER_NAME.fullmatch(self.key_header)
                or self.key_header.casefold() in {"content-type", "content-length", "host"}):
            raise ValueError("control gateway key_header is invalid")
        if (isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, (int, float))
                or not math.isfinite(self.timeout_s)
                or self.timeout_s <= 0 or self.timeout_s > 120):
            raise ValueError("control gateway timeout_s must be in (0, 120]")

    def read_key(self) -> str:
        descriptor = -1
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.key_file, flags)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
                raise OSError("not a regular key file of at most 4096 bytes")
            if metadata.st_mode & 0o077:
                raise ControlPlaneError(
                    "control key file must not be group/world accessible")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                raw = handle.read(4097)
            if len(raw) > 4096:
                raise OSError("control key file exceeds 4096 bytes")
            key = raw.decode("utf-8").strip()
        except ControlPlaneError:
            raise
        except (OSError, UnicodeError) as exc:
            raise ControlPlaneError("control key file is unreadable") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not key:
            raise ControlPlaneError("control key file is empty")
        if "\r" in key or "\n" in key:
            raise ControlPlaneError("control key contains a line break")
        return key


class ControlPlaneClient:
    _leases_guard = threading.Lock()
    _active_gateways: set[str] = set()

    def __init__(self, config: ControlGatewayConfig):
        if not isinstance(config, ControlGatewayConfig):
            raise TypeError("ControlPlaneClient requires an explicit ControlGatewayConfig")
        self.config = config
        self.base_url = config.base_url
        self._key = config.read_key()
        self._opener = urllib.request.build_opener(_RejectRedirects())
        self._active_run_id = ""
        self._lease_descriptor = -1
        gateway_digest = hashlib.sha256(self.base_url.encode("utf-8")).hexdigest()[:24]
        self._lease_key = self.base_url
        self._lease_dir = (Path(tempfile.gettempdir())
                           / f"web-operation-agent-{os.geteuid()}")
        self._lease_path = self._lease_dir / f"control-{gateway_digest}.lock"

    def _prepare_lease_dir(self) -> None:
        try:
            self._lease_dir.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ControlPlaneError("control lease directory is unavailable") from exc
        try:
            metadata = self._lease_dir.lstat()
        except OSError as exc:
            raise ControlPlaneError("control lease directory is unavailable") from exc
        if (not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077):
            raise ControlPlaneError(
                "control lease directory must be private and owned by the current user")

    def _call(self, path: str, payload: dict | None = None, method: str = "POST") -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload or {}).encode() if method == "POST" else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"content-type": "application/json",
                                              self.config.key_header: self._key})
        try:
            with self._opener.open(req, timeout=self.config.timeout_s) as response:
                raw = response.read(_MAX_CONTROL_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_CONTROL_RESPONSE_BYTES:
                raise ControlPlaneError("control gateway response exceeds 1 MiB")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ControlPlaneError("control gateway response must be an object")
            return payload
        except urllib.error.HTTPError as e:
            raise ControlPlaneError(f"{method} {path} returned HTTP {e.code}") from e
        except ControlPlaneError:
            raise
        except Exception as e:
            raise ControlPlaneError(f"{method} {path} failed: {type(e).__name__}: {e}")

    def _require_active(self) -> str:
        if not self._active_run_id or self._lease_descriptor < 0:
            raise ControlPlaneError("control gateway has no active run lease")
        return self._active_run_id

    # -- run lifecycle -----------------------------------------------------------------
    def begin_run(self, run_id: str) -> None:
        """Exclusively lease and reset a legacy global gateway for one bounded run."""
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ControlPlaneError("control run_id is invalid")
        if self._active_run_id:
            raise ControlPlaneError("this control client already owns an active run")
        if fcntl is None:
            raise ControlPlaneError("control run isolation requires a POSIX file lock")
        with self._leases_guard:
            if self._lease_key in self._active_gateways:
                raise ControlPlaneError("control gateway is already leased by another run")
            self._active_gateways.add(self._lease_key)
        descriptor = -1
        try:
            self._prepare_lease_dir()
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self._lease_path, flags, 0o600)
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()):
                raise ControlPlaneError(
                    "control lease file must be regular and owned by the current user")
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ControlPlaneError(
                    "control gateway is already leased by another process") from exc
            self._active_run_id = run_id
            self._lease_descriptor = descriptor
            descriptor = -1
            response = self._call("/admin/reset", {"run_id": run_id})
            if not response.get("ok"):
                raise ControlPlaneError("control plane refused to start the run")
        except Exception:
            self._release_lease()
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _release_lease(self) -> None:
        descriptor = self._lease_descriptor
        self._lease_descriptor = -1
        self._active_run_id = ""
        if descriptor >= 0:
            try:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        with self._leases_guard:
            self._active_gateways.discard(self._lease_key)

    def end_run(self, run_id: str) -> None:
        """Clear all permits while exclusively leased, then release the gateway."""
        if not self._active_run_id or self._lease_descriptor < 0:
            raise ControlPlaneError("control gateway has no active run lease")
        if run_id != self._active_run_id:
            raise ControlPlaneError("cannot release another control run")
        response = self._call("/admin/reset", {
            "run_id": run_id,
            "reason": "run_ended",
        })
        if not response.get("ok"):
            raise ControlPlaneError("control plane refused to clear the completed run")
        self._release_lease()

    def install(self, plan: ControlPlan) -> list[dict]:
        """Register every ticket as a single-use permit. All or nothing.

        A partially installed plan is worse than none: the agent could complete step one of a
        sequence and then be refused at step two for a reason indistinguishable from a real
        authorisation boundary. Any failure here aborts the run.
        """
        run_id = self._require_active()
        installed = []
        for i, ticket in enumerate(plan.tickets):
            r = self._call("/admin/declare", {
                "run_id": run_id,
                "operation_id": f"{plan.plan_id}:{i}", "intent": "update",
                "webapp": ticket.webapp, "uri": ticket.request,
                "targets": [dict(t) for t in ticket.targets], "changes": dict(ticket.changes)})
            if not r.get("ok"):
                raise ControlPlaneError(f"ticket {i} of plan {plan.plan_id} was refused: {r}")
            installed.append({"operation_id": r.get("operation_id"), "permits": r.get("permits"),
                              "signature_checked": bool(r.get("service"))})
        return installed

    def trace(self) -> list[dict]:
        self._require_active()
        trace = self._call("/admin/trace", method="GET").get("trace")
        if not isinstance(trace, list) or any(not isinstance(event, dict) for event in trace):
            raise ControlPlaneError("control gateway trace is invalid")
        return trace

    def since(self, seen: int) -> tuple[list[dict], int]:
        """Decisions taken since the caller last looked, and the new watermark.

        This is how the agent learns that a write was refused. The alternative -- reading the
        page and hoping the application says so -- let a run submit a refused write and then
        report the task complete. Nothing about the plan or the permitted request leaves this
        method; the caller only ever tells the model that its own action did not execute.
        """
        tr = self.trace()
        if len(tr) < seen:
            raise ControlPlaneError("control trace rewound within the run")
        return tr[seen:], len(tr)

    @staticmethod
    def outcome_of(events: list[dict]) -> dict:
        """Classify an observed window of control decisions, without command attribution.

        One permitted request says nothing about a different refused request. A window that
        contains both decisions is `mixed`: part of a change may have been made and part
        certainly was not, so the outcome is indeterminate rather than successful.

        Each decision also keeps its own identity here. `since()` is a slice of a shared
        trace, not a correlation to a command id, so this cannot claim every event belongs to
        the action that just ran -- but it can at least report what was allowed and what was
        refused instead of a single word.
        """
        consumed = [e for e in events if e.get("event") == "permit_consumed"]
        blocked = [e for e in events if e.get("event") in ("write_blocked", "blocked")]
        if consumed and blocked:
            state = "mixed"
        elif consumed:
            state = "permitted"
        elif blocked:
            state = "refused"
        else:
            state = "none"
        return {"state": state, "permitted": len(consumed), "refused": len(blocked),
                "permitted_requests": [_where(e) for e in consumed],
                "refused_requests": [_where(e) for e in blocked]}

    # -- read-back ---------------------------------------------------------------------
    def outcome(self) -> dict:
        """What the enforcement point actually saw, independent of what the agent believes."""
        tr = self.trace()
        consumed = [t for t in tr if t.get("event") == "permit_consumed"]
        blocked = [t for t in tr if t.get("event") in ("write_blocked", "blocked")]
        return {"permit_consumed": len(consumed), "write_blocked": len(blocked),
                "consumed_requests": sorted({f"{t.get('webapp')}/{t.get('uri')}" for t in consumed}),
                "blocked_requests": sorted({f"{t.get('webapp')}/{t.get('uri')}" for t in blocked}),
                "trace_events": len(tr)}


def assert_scope_is_not_the_answer(scope: str, plan: ControlPlan, prompt: str) -> None:
    """A public scope constraint may state a boundary; it may not hand over the answer.

    Applied consistently, the constraint removes a confound. Written carelessly, it becomes the
    request name the agent was supposed to find. So it may not contain any permitted request
    name, nor any target value the task itself does not already mention.
    """
    text = (scope or "").lower()
    if not text:
        return
    for ticket in plan.tickets:
        if ticket.request.lower() in text:
            raise ControlPlaneError(
                f"the supported-scope text names the permitted request {ticket.request!r}; "
                f"that is the answer, not a boundary")
        for target in ticket.targets:
            for value in target.values():
                v = str(value).lower()
                if len(v) > 2 and v in text and v not in prompt.lower():
                    raise ControlPlaneError(
                        f"the supported-scope text introduces target value {value!r}, which "
                        f"the task itself does not mention")


def plan_from_spec(plan_id: str, task_ref: str, operations: list[dict]) -> ControlPlan:
    """Build a plan from the operator's authorisation entries.

    An entry is `{webapp, request|uri, targets: [{...}], changes: {...}}`. Nothing here reads
    a task id, a prompt or an evaluator: the caller supplies exactly what the user authorised
    for this run, and the same shape works for a single write or an ordered multi-stage one.
    """
    tickets = []
    for op in operations:
        request = op.get("request") or op.get("uri")
        if not op.get("webapp") or not request:
            raise ControlPlaneError(f"authorisation entry needs webapp and request: {op}")
        targets = op.get("targets") or []
        if not targets:
            raise ControlPlaneError(f"authorisation entry needs at least one target: {op}")
        tickets.append(ControlTicket(webapp=op["webapp"], request=request,
                                     targets=tuple(dict(t) for t in targets),
                                     changes=dict(op.get("changes") or {})))
    return ControlPlan(plan_id=plan_id, task_ref=task_ref, tickets=tuple(tickets))


def _where(event: dict) -> str:
    """`webapp/request` for one decision, for reports that must name what happened."""
    return f"{event.get('webapp', '?')}/{event.get('uri', '?')}"
