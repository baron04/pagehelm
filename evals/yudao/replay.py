"""Replay a yudao goal run through the current executor with its recorded model outputs (¥0).

Each model call is answered with what the model answered in the recorded run -- the scope,
the first reading (the turn's thread), the second (the pool's), then later declarations in
order. The resolver reads the live local yudao as the evaluation account on the task set's
reference day; the browser is the test double, and a page task's final address is where
the replay's `goto` sent it. A replay measures what a code change does to the same
declarations; it is not a run, and a task whose calls diverge from the recording is marked.

    PYTHONPATH=src:examples/yudao/src:examples/odoo/src .venv/bin/python -m evals.yudao.replay \\
        <taskset.json> <run output> --account <name> --ui-source <ui checkout> [--all]
"""
from __future__ import annotations

import argparse
import glob
import json
import tempfile
import threading
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from evals.odoo.goals.judge import score
from web_operation_agent.modeling.provider import ProviderError
from evals.odoo.goals.source_citations import bound_source
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.execution.product import AgentTurnConfig
from web_operation_agent.goal import executor as executor_module
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.goal.resolve.service import DataGoalPort
from web_operation_agent.tests.execution.product_support import _Channel
from web_operation_agent.tests.goal.test_goal_turn import _Adapter, _Browser
from web_operation_agent.tests.support.chat_port_contract import CancellableChatDouble
from web_operation_agent_yudao.accounts import add_account_arguments
from web_operation_agent_yudao.client import YudaoAccount, YudaoClient
from web_operation_agent.goal.conventions import load_conventions
from web_operation_agent_yudao.conventions import REFERENCE_CONVENTIONS
from web_operation_agent_yudao.demo import read_api_docs, source_rules
from web_operation_agent_yudao.goal import YudaoGoalData
from web_operation_agent_yudao.ui_source import UiSource


class ReplayChat(CancellableChatDouble):
    def __init__(self, events):
        self.owner = threading.get_ident()
        self.scope = next((e for e in events if e.get("event") == "goal_scope_declared"), None)
        declared = [e for e in events if e.get("event") == "goal_declared"]
        self.first = declared[0]["spec"] if declared else None
        second = next((e for e in events if e.get("event") == "goal_second_reading"), None)
        self.second = second["spec"] if second else self.first
        self.later = [e["spec"] for e in declared[1:]]
        self.calls = 0
        self.pool_calls = 0
        self.diverged = False
        self.lock = threading.Lock()

        class _Meter:
            @staticmethod
            def report():
                return {"provider_calls": 0, "cost_cny": 0.0}
        self.meter = _Meter()

    def complete(self, messages, tools, _run_id, **_kwargs):
        name = next(iter({t["function"]["name"] for t in tools}))
        with self.lock:
            self.calls += 1
            if name == "declare_scope" and self.scope is None:
                args = {"parts": "unreadable"}
            elif name == "declare_scope":
                s = self.scope
                args = {key: s.get(key, default) for key, default in (
                    ("parts", []), ("write_intent", False), ("unsupported", ""),
                    ("about_assistant", False))}
            elif threading.get_ident() != self.owner:
                # The one recorded pool call is the second reading; further ones (votes
                # taken when readings disagree) were not recorded and cast no vote.
                self.pool_calls += 1
                if self.pool_calls > 1:
                    raise ProviderError("the vote was not recorded", code="provider_unrecorded",
                                        retryable=False)
                args = self.second
            elif self.first is not None:
                args, self.first = self.first, None
            elif self.later:
                args = self.later.pop(0)
            else:
                self.diverged = True
                args = self.second
        return {"tool_calls": [{"id": str(self.calls), "function": {
            "name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("taskset", type=Path)
    p.add_argument("run", type=Path)
    add_account_arguments(p)
    p.add_argument("--ui-source", type=Path)
    p.add_argument("--source-root", type=Path)
    p.add_argument("--source-release", default="")
    p.add_argument("--no-conventions", action="store_true")
    p.add_argument("--origin", default="http://localhost:48088")
    p.add_argument("--all", action="store_true")
    p.add_argument("--task", action="append", default=[])
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--dump", type=Path)
    p.add_argument("--ledgers", type=Path, help="keep each replayed turn's audit ledger here")
    a = p.parse_args()
    doc = json.loads(a.taskset.read_text())
    tasks = {t["id"]: {**t, "truth": t.get("expected_outcome")} for t in doc["questions"]}
    data = YudaoGoalData(YudaoClient(a.origin, YudaoAccount.load(a.account, a.accounts)),
                         read_api_docs(a.origin),
                         ui=UiSource.read(a.ui_source) if a.ui_source else None)
    if not a.no_conventions:
        data.attach_conventions(load_conventions(REFERENCE_CONVENTIONS))
    source = source_rules(a.source_root, a.source_release)
    citations = (bound_source(doc, a.source_root, a.source_release)
                 if "source_binding" in doc and a.source_root else None)
    if source is not None:
        data.attach_source(source)
    port = DataGoalPort(data, today=date.fromisoformat(doc["reference_date"]))
    port.index()
    # The browser double's application lives at its own origin; only the reads go to yudao.
    entry = "http://127.0.0.1:8080" + doc["entry_path"]
    before = after = n = 0
    rows = []
    with patch.object(executor_module, "RemoteBrowserClient", _Browser), \
            tempfile.TemporaryDirectory() as tmp:
        for f in sorted(glob.glob(str(a.run / "attempts" / "*.json"))):
            row = json.load(open(f))
            task = tasks.get(row["task"])
            if task is None or (a.task and row["task"] not in a.task):
                continue
            if not a.task and not a.all and row["success"]:
                continue
            audit = a.run / "runs" / row["task"] / "audit.jsonl"
            if not audit.is_file():
                continue  # the attempt never reached the agent (a harness error)
            events = [json.loads(line) for line in audit.read_text().splitlines()]
            chat = ReplayChat(events)
            ledger = Path(a.ledgers or tmp) / f"{row['task']}.jsonl"
            ledger.unlink(missing_ok=True)
            _Browser.instances.clear()
            executor = GoalTurnExecutor(
                channel=_Channel(page_url=entry, decision="approved"),
                config=AgentTurnConfig(supported_scope="read", ledger_path=ledger),
                chat=chat, application=bind_application(_Adapter()), goal_port=port,
                control=ReadOnlyControl(), run_id_factory=lambda _m: "replay-run")
            try:
                execution = executor.execute({"message_id": "m1", "text": row["prompt"],
                                              "page_url": entry})
                answer = execution.user_answer or ""
                terminal = next((json.loads(line).get("terminal_reason")
                                 for line in ledger.read_text().splitlines()
                                 if json.loads(line).get("event") == "run_finished"), "")
            except Exception as exc:  # noqa: BLE001 -- a replay's failure is its result
                answer, terminal = f"ERROR {type(exc).__name__}: {exc}", "error"
            path = doc["entry_path"]
            gone = [args["url"] for b in _Browser.instances for action, args in b.actions
                    if action == "goto"]
            if gone:
                parts = urlsplit(gone[-1])
                path = parts.path + (f"?{parts.query}" if parts.query else "")
            v = score(task, probe={"path": path}, visited=[], answer=answer,
                      finished=terminal == "agent_finished", refused=terminal == "agent_refused",
                      write_detected=False, source=citations,
                      clarifying=terminal in ("needs_clarification", "needs_input"))
            n += 1
            before += bool(row["success"])
            after += bool(v.success)
            mark = ("FIXED" if v.success and not row["success"] else
                    "BROKE" if row["success"] and not v.success else
                    "ok" if v.success else "still")
            rows.append({"task": row["task"], "mark": mark, "outcome": v.outcome,
                         "success": bool(v.success), "answer": answer, "terminal": terminal,
                         "path": path, "diverged": chat.diverged})
            if not a.quiet or mark in ("FIXED", "BROKE"):
                flag = " (diverged)" if chat.diverged else ""
                print(f"{row['task']:7} {mark:6} {v.outcome:26}{flag} {path} | "
                      f"{answer[:160]!r}", flush=True)
    print(f"replayed {n}: recorded success {before} -> replay success {after}")
    if a.dump:
        a.dump.write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
