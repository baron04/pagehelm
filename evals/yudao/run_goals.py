"""Run a sealed yudao goal set on the local deployment, one fresh session per task, and score it.

The task file stays where its author sealed it; this runner reads it only after checking its
digest and the judge's against the seal, hands the model nothing but each task's prompt, and
scores every attempt with the same judge the Odoo goal sets use (rubric 7): the reply the person
was shown, the final page's address, the addresses passed on the way, and whether the database
changed. Lanes split the tasks between processes; each attempt is written as it finishes, so a
lane that stops resumes where it left off.

    PYTHONPATH=src:examples/yudao/src .venv/bin/python -m evals.yudao.run_goals \\
        --taskset <path>/yudao-goals-zh-v1-holdout.json --output .artifacts/yudao-v1 \\
        --account .private/yudao-local-evaluator.json --model-env .env --lane 1/4
"""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import multiprocessing
import os
import signal
import subprocess
import time
from datetime import date
from pathlib import Path

from evals.odoo.goals.judge import score
from evals.odoo.goals.source_citations import bound_source
from evals.odoo.goals.run_goals import sheet, summarize_v7
from web_operation_agent_yudao.client import YudaoAccount, YudaoClient
from web_operation_agent_yudao.conventions import REFERENCE_CONVENTIONS
from web_operation_agent_yudao.demo import (YudaoSession, model_env, read_api_docs, run_session,
                                            source_rules, ui_source, verify_browser_ready)
from web_operation_agent_yudao.goal import YudaoGoalData
from web_operation_agent_yudao.repository import repository_source

ROOT = Path(__file__).resolve().parents[2]
JUDGE = ROOT / "evals" / "odoo" / "goals" / "judge.py"
#: Tables a read changes by itself: sign-ins, tokens, access and error logs, job runs.
_NOISE = ("%\\_log", "system\\_oauth2\\_%", "infra\\_api\\_%", "infra\\_job%",
          "system\\_login\\_log", "system\\_operate\\_log")
#: Columns a sign-in writes on its user's row.
_SIGN_IN_STAMPS = {"system_users": ("login_ip", "login_date", "update_time")}


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _seal(taskset: Path) -> dict:
    return json.loads(taskset.with_name(taskset.name.replace(".json", ".seal.json")).read_text())


def _prefixed(digest: str) -> str:
    return digest if digest.startswith("sha256:") else "sha256:" + digest


def load_sealed(taskset: Path) -> dict:
    """The task set, after its digest and the judge's match the seal beside it.

    Seals come in two layouts: v1/v2 carry `status` and `tasks`, v3 carries `task_count` and
    leaves the status to the sealed task set itself. Digests may or may not be prefixed.
    """
    seal = _seal(taskset)
    if _digest(taskset) != _prefixed(seal["task_set_sha256"]):
        raise RuntimeError("the task set does not match its seal")
    if _digest(JUDGE).removeprefix("sha256:") != seal["judge"]["sha256"].removeprefix("sha256:"):
        raise RuntimeError("the judge differs from the one the task set was sealed with")
    doc = json.loads(taskset.read_text())
    if seal.get("status", doc.get("status")) not in ("FROZEN", "DEVELOPMENT"):
        raise RuntimeError("the task set is not sealed for running")
    if len(doc["tasks"]) != seal.get("tasks", seal.get("task_count")):
        raise RuntimeError("the task set's size differs from its seal")
    return doc


def fingerprint(container: str, database: str) -> str:
    """A checksum of every table a read-only session must leave alone. A sign-in stamps
    the user's row with when and where, so those columns are left out of the users table."""
    def mysql(query: str) -> str:
        return subprocess.run(
            ["docker", "exec", container, "sh", "-c",
             f'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N -B -e "{query}" 2>/dev/null'],
            check=True, capture_output=True, text=True, timeout=120).stdout
    noise = " AND ".join(f"table_name NOT LIKE '{p}'" for p in _NOISE)
    tables = mysql(f"SELECT table_name FROM information_schema.tables WHERE table_schema="
                   f"'{database}' AND {noise} ORDER BY table_name").split()
    if not tables:
        raise RuntimeError("the database has no tables to fingerprint")
    stamped = [t for t in tables if t in _SIGN_IN_STAMPS]
    sums = mysql("CHECKSUM TABLE " + ", ".join(f"\\`{database}\\`.\\`{t}\\`" for t in tables
                                             if t not in _SIGN_IN_STAMPS)
                 ).replace(f"{database}.", "")
    for table in stamped:
        columns = [c for c in mysql(
            f"SELECT column_name FROM information_schema.columns WHERE table_schema="
            f"'{database}' AND table_name='{table}' ORDER BY ordinal_position").split()
            if c not in _SIGN_IN_STAMPS[table]]
        sums += mysql(f"SELECT {', '.join(columns)} FROM \\`{database}\\`.\\`{table}\\` "
                      "ORDER BY id")
    return hashlib.sha256(sums.encode()).hexdigest()


#: One turn's own budget; the attempt as a whole (sign-in, browser, turn, teardown) gets
#: `--attempt-deadline-s`, after which its process group is killed.
_TURN_TIMEOUT_S = 180


def _session_child(session: YudaoSession, failure: Path) -> None:
    os.setsid()  # the browser and its driver join this group, and die with it
    try:
        run_session(session)
    except BaseException as exc:  # noqa: BLE001 -- reported to the parent as the attempt's error
        failure.write_text(f"{type(exc).__name__}: {exc}"[:500])
        raise SystemExit(1)


def _bounded(session: YudaoSession, *, deadline_s: float) -> str:
    """Run the session in its own process group; kill the whole group at the deadline.
    Returns the attempt's error, or "" when the session finished."""
    session.run_dir.mkdir(parents=True, exist_ok=True)
    failure = session.run_dir / "failure.txt"
    failure.unlink(missing_ok=True)
    child = multiprocessing.get_context("spawn").Process(
        target=_session_child, args=(session, failure))
    child.start()
    child.join(deadline_s)
    if child.is_alive():
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.join(10)
        return f"AttemptDeadline: the attempt did not finish within {deadline_s:.0f} s"
    if child.exitcode != 0:
        return failure.read_text() if failure.is_file() else f"exit code {child.exitcode}"
    return ""


def check_snapshot(snapshot: Path, seal: dict, *, container: str, database: str) -> None:
    """The snapshot file is the one the task set was sealed against, and the live
    database holds what it holds, sign-in stamps and logs aside."""
    sealed = seal.get("snapshot_sha256") or (seal.get("snapshot") or {}).get("sha256")
    if sealed and _digest(snapshot) != _prefixed(sealed):
        raise RuntimeError("the snapshot is not the one the task set was sealed against")
    # One scratch database per process: lanes split a run between processes and start
    # together, so a fixed name would have one lane load while another drops it.
    scratch = f"woa_snapshot_check_{os.getpid()}"
    dump = gzip.decompress(snapshot.read_bytes()).replace(
        f"`{database}`".encode(), f"`{scratch}`".encode())
    login = ["docker", "exec", "-i", container, "sh", "-c", 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" 2>/dev/null']
    subprocess.run(login, input=dump, check=True, capture_output=True, timeout=600)
    try:
        if fingerprint(container, database) != fingerprint(container, scratch):
            raise RuntimeError("the database differs from the snapshot the task set needs")
    finally:
        subprocess.run(login, input=f"DROP DATABASE IF EXISTS `{scratch}`;".encode(),
                       check=True, capture_output=True, timeout=120)


def check_run_contract(doc: dict, seal: dict, *, origin: str, account: YudaoAccount,
                       snapshot: Path | None) -> None:
    """Reject a changed holdout environment before a browser or model is started."""
    environment = doc.get("environment") or {}
    if seal.get("environment") and environment != seal["environment"]:
        raise RuntimeError("the task set's environment differs from its seal")
    if environment.get("base_url") and origin.rstrip("/") != environment["base_url"].rstrip("/"):
        raise RuntimeError("the browser origin differs from the sealed environment")
    actor = environment.get("actor") or {}
    if actor.get("username") and account.username != actor["username"]:
        raise RuntimeError("the account differs from the sealed actor")
    if environment.get("tenant_id") is not None and account.tenant != str(environment["tenant_id"]):
        raise RuntimeError("the tenant differs from the sealed environment")
    sealed_snapshot = seal.get("snapshot_sha256") or (seal.get("snapshot") or {}).get("sha256")
    if sealed_snapshot and snapshot is None:
        raise RuntimeError("the sealed holdout requires --snapshot and an unchanged live database")


def check_task_coverage(doc: dict, index) -> None:
    """A feasible task cannot be scored against an index missing its target."""
    entities = {entity.key for entity in index.entities}
    pages = {page.key for page in index.pages}
    required_entities = set()
    required_pages = set()
    for task in doc["tasks"]:
        if task.get("type") == "impossible":
            continue
        if task.get("entity"):
            required_entities.add(task["entity"])
        oracle = (task.get("truth") or {}).get("oracle") or {}
        for assertion in oracle.get("assertions") or ():
            required_entities.add(assertion[0])
        if task.get("type") == "navigation":
            page = (task.get("truth") or {}).get("page")
            if page:
                required_pages.add(page)
    missing_entities = sorted(required_entities - entities)
    missing_pages = sorted(required_pages - pages)
    if missing_entities or missing_pages:
        raise RuntimeError("the actor's current index misses sealed task targets: "
                           f"entities={missing_entities}, pages={missing_pages}")


def summarize(rows: list[dict]) -> dict:
    """Outcomes over all attempts, then by category and by task type."""
    def part(selected):
        counts = collections.Counter(r["outcome"] for r in selected)
        return {"attempts": len(selected), "success": sum(bool(r["success"]) for r in selected),
                "outcomes": dict(counts)}
    by = {}
    for key in ("category", "type"):
        groups = collections.defaultdict(list)
        for row in rows:
            if row.get(key):
                groups[row[key]].append(row)
        if groups:
            by[key] = {name: part(group) for name, group in sorted(groups.items())}
    return {**summarize_v7(rows), **part(rows), "by": by,
            "provider_calls": sum(r["provider_calls"] for r in rows),
            "input_tokens": sum(r.get("input_tokens", 0) for r in rows),
            "output_tokens": sum(r.get("output_tokens", 0) for r in rows),
            "cost_cny": round(sum(r["cost_cny"] for r in rows), 6),
            "harness_errors": [r["task"] for r in rows if r["terminal_reason"] == "harness_error"],
            "writes": [r["task"] for r in rows if r["write_detected"]]}


def run_attempt(task: dict, doc: dict, *, args, api_docs: dict, ui, env: dict,
                run_name: str, source=None, citations=None) -> dict:
    """One task in a fresh session, scored. `source` offers the deployment's rules to the
    agent; `citations` is the evaluator's own sealed copy the judge checks the reply's
    citations against, never shown to the agent."""
    before = fingerprint(args.container, args.database)
    started = time.monotonic()
    run_dir = args.output / "runs" / task["id"]
    session = YudaoSession(
        origin=args.origin, account=YudaoAccount.from_file(args.account), api_docs=api_docs,
        tasks=(task["prompt"],), run_dir=run_dir, ui=ui, model_env=env,
        start_path=doc["entry_path"], turn_timeout_s=_TURN_TIMEOUT_S,
        today=date.fromisoformat(doc["reference_date"]), batch_id=f"{run_name}-{task['id']}",
        source=source, conventions=None if args.no_conventions else REFERENCE_CONVENTIONS)
    if getattr(args, "executor", "goal") != "goal":
        from dataclasses import replace
        session = replace(session, executor_mode=args.executor)
    error, summary = _bounded(session, deadline_s=args.attempt_deadline_s), {}
    if not error:
        summary = json.loads((run_dir / "records.json").read_text())
    wall = round(time.monotonic() - started, 3)
    write_detected = fingerprint(args.container, args.database) != before
    records = summary.get("records") or []
    turn = records[-1] if records else {}
    error = error or turn.get("error", "")
    answer = turn.get("answer", "")
    terminal = turn.get("terminal_reason") or ("harness_error" if error else "")
    probe = {"path": summary.get("final_path", ""),
             "breadcrumb_last": summary.get("breadcrumb_last", ""),
             "active_tabs": summary.get("active_tabs") or []}
    visited = summary.get("visited") or []
    verdict = score(task, probe=probe, visited=visited, answer=answer,
                    finished=terminal == "agent_finished", refused=terminal == "agent_refused",
                    write_detected=write_detected, source=citations,
                    clarifying=terminal in ("needs_clarification", "needs_input"))
    spend = summary.get("spend") or {}
    return {"task": task["id"], "category": task.get("category", ""), "type": task.get("type", ""),
            "prompt": task["prompt"],
            "terminal_reason": terminal, "answer": answer, "probe": probe, "visited": visited,
            "write_detected": write_detected, "error": error, "wall_s": wall,
            "provider_calls": spend.get("model_calls", 0), "cost_cny": spend.get("cost", 0.0),
            "input_tokens": spend.get("input_tokens", 0),
            "output_tokens": spend.get("output_tokens", 0),
            **verdict.to_json(), "run_dir": summary.get("run_dir", "")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--taskset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--account", type=Path, required=True)
    parser.add_argument("--model-env", type=Path, required=True)
    parser.add_argument("--origin", default="http://localhost:48088")
    parser.add_argument("--ui-source", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--source-release", default="")
    parser.add_argument("--executor", choices=("goal", "agent", "hybrid"), default="goal",
                        help="goal baseline, generic agent loop, or native staged business/source composition")
    parser.add_argument("--no-conventions", action="store_true",
                        help="run without the deployment's reviewed conventions (control arm)")
    parser.add_argument("--container", default="woa-yudao-mysql-1")
    parser.add_argument("--database", default="ruoyi-vue-pro")
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--lane", default="1/1")
    parser.add_argument("--attempt-deadline-s", type=float, default=_TURN_TIMEOUT_S + 120,
                        help="kill an attempt, browser and all, that runs longer than this")
    parser.add_argument("--snapshot", type=Path,
                        help="the snapshot the task set's truth was read from: checked against "
                             "the seal and the live database before any model call")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--summarize-only", action="store_true",
                        help="report every attempt already in --output, from all lanes")
    args = parser.parse_args()
    if args.summarize_only:
        rows = [json.loads(p.read_text()) for p in sorted((args.output / "attempts").glob("*.json"))]
        print(json.dumps(summarize(rows), ensure_ascii=False, indent=1))
        return 0
    doc = load_sealed(args.taskset)
    seal = _seal(args.taskset)
    account = YudaoAccount.from_file(args.account)
    check_run_contract(doc, seal, origin=args.origin, account=account, snapshot=args.snapshot)
    citations = None
    if "source_binding" in doc:
        # The rules are offered to the agent from the same release the truth cites; the
        # judge reads its own sealed copy of the cited files.
        if args.source_root is None or not args.source_release:
            raise RuntimeError("this task set cites source: give --source-root and --source-release")
        citations = bound_source(doc, args.source_root, args.source_release)
    env = model_env(args.model_env)
    if not env.get("AGENT_OPENAI_MODEL"):
        raise RuntimeError("the model env names no model")
    fingerprint(args.container, args.database)
    if args.snapshot is not None:
        check_snapshot(args.snapshot, seal, container=args.container,
                       database=args.database)
    api_docs = read_api_docs(args.origin)
    ui = ui_source(args.ui_source, args.source_root)
    check_task_coverage(doc, YudaoGoalData(
        YudaoClient(args.origin, account), api_docs, ui=ui).index())
    verify_browser_ready(args.origin, account, doc["entry_path"])
    if args.preflight_only:
        print(json.dumps({"suite": doc["suite_id"], "tasks": len(doc["tasks"]),
                          "reference_date": doc["reference_date"],
                          "model": env["AGENT_OPENAI_MODEL"]}, ensure_ascii=False))
        return 0
    source = (repository_source(args.source_root, args.source_release) if args.executor != "goal"
              else source_rules(args.source_root, args.source_release))
    run_name = f"{doc['suite_id']}-{args.output.name}"
    lane, lanes = (int(part) for part in args.lane.split("/"))
    (args.output / "attempts").mkdir(parents=True, exist_ok=True)
    rows = []
    selected = [t for t in doc["tasks"] if not args.task or t["id"] in args.task]
    for position, task in enumerate(selected):
        if position % lanes != lane - 1:
            continue
        path = args.output / "attempts" / f"{task['id']}.json"
        if path.exists():
            row = json.loads(path.read_text())
        else:
            row = run_attempt(task, doc, args=args, api_docs=api_docs, ui=ui, env=env,
                              run_name=run_name, source=source, citations=citations)
            path.write_text(json.dumps(row, ensure_ascii=False, indent=1) + "\n")
        rows.append(row)
        print(json.dumps({key: row[key] for key in (
            "task", "outcome", "success", "terminal_reason", "provider_calls", "cost_cny",
            "wall_s", "write_detected", "error")}, ensure_ascii=False), flush=True)
        if row["write_detected"]:
            print(json.dumps({"stopped": "write_detected", "task": row["task"]}), flush=True)
            return 3
    (args.output / f"projection-lane{lane}of{lanes}.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1) + "\n")
    sheet(rows, doc, args.output / f"grading-lane{lane}of{lanes}.html")
    print(json.dumps(summarize(rows), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
