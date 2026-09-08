"""The hidden control plane: what the user authorised, installed before the agent runs.

Authorisation is derived from the public task specification by the operator and installed
before execution. The agent cannot declare or broaden its own write permission, and the
installed plan is never shown to the model, prompt, contract or browser.

The enforcement point itself is unchanged: the out-of-process gateway in front of the
application, which fails closed on anything it was not given a permit for. This module only
installs permits and reads back the decision trace.
"""
from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .schemas import ControlPlan, ControlTicket


class ControlPlaneError(Exception):
    """Callers map this to terminal reason `control_plane_error`. Never fail open."""


_HEADER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")


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
        object.__setattr__(self, "key_file", path.resolve())
        if (not isinstance(self.key_header, str)
                or not _HEADER_NAME.fullmatch(self.key_header)
                or self.key_header.casefold() in {"content-type", "content-length", "host"}):
            raise ValueError("control gateway key_header is invalid")
        if (isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, (int, float))
                or not math.isfinite(self.timeout_s)
                or self.timeout_s <= 0 or self.timeout_s > 120):
            raise ValueError("control gateway timeout_s must be in (0, 120]")

    def read_key(self) -> str:
        try:
            if not self.key_file.is_file() or self.key_file.stat().st_size > 4096:
                raise OSError("not a regular key file of at most 4096 bytes")
            key = self.key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ControlPlaneError(
                f"control key {self.key_file} is unreadable: {exc}") from exc
        if not key:
            raise ControlPlaneError(f"control key {self.key_file} is empty")
        if "\r" in key or "\n" in key:
            raise ControlPlaneError("control key contains a line break")
        return key


class ControlPlaneClient:
    def __init__(self, config: ControlGatewayConfig):
        if not isinstance(config, ControlGatewayConfig):
            raise TypeError("ControlPlaneClient requires an explicit ControlGatewayConfig")
        self.config = config
        self.base_url = config.base_url
        self._key = config.read_key()

    def _call(self, path: str, payload: dict | None = None, method: str = "POST") -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload or {}).encode() if method == "POST" else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"content-type": "application/json",
                                              self.config.key_header: self._key})
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_s) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise ControlPlaneError(f"{method} {path} -> {e.code}: {e.read()[:300]!r}")
        except Exception as e:
            raise ControlPlaneError(f"{method} {path} failed: {type(e).__name__}: {e}")

    # -- run lifecycle -----------------------------------------------------------------
    def begin_run(self, run_id: str) -> None:
        """Clear the previous run's declarations and trace. Permits never span runs."""
        r = self._call("/admin/reset", {"run_id": run_id})
        if not r.get("ok"):
            raise ControlPlaneError(f"control plane refused to start run {run_id}: {r}")

    def install(self, plan: ControlPlan) -> list[dict]:
        """Register every ticket as a single-use permit. All or nothing.

        A partially installed plan is worse than none: the agent could complete step one of a
        sequence and then be refused at step two for a reason indistinguishable from a real
        authorisation boundary. Any failure here aborts the run.
        """
        installed = []
        for i, ticket in enumerate(plan.tickets):
            r = self._call("/admin/declare", {
                "operation_id": f"{plan.plan_id}:{i}", "intent": "update",
                "webapp": ticket.webapp, "uri": ticket.request,
                "targets": [dict(t) for t in ticket.targets], "changes": dict(ticket.changes)})
            if not r.get("ok"):
                raise ControlPlaneError(f"ticket {i} of plan {plan.plan_id} was refused: {r}")
            installed.append({"operation_id": r.get("operation_id"), "permits": r.get("permits"),
                              "signature_checked": bool(r.get("service"))})
        return installed

    def trace(self) -> list[dict]:
        return self._call("/admin/trace", method="GET").get("trace", [])

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
