#!/usr/bin/env python3
"""Run a unittest package across processes, sharded by test id.

The offline apparatus suite is the longest single step of every verification cycle, and most of
it is one module's tests waiting on loopback servers rather than using a core. Running them in
shards is only safe if the shards together run exactly the tests the sequential run would, so
each shard reports the ids it actually started and this runner refuses the result unless those
ids are the discovered ids, each of them once. Counting alone would let a shard that skipped a
test and one that repeated another cancel out.

Sharding is round-robin over the sorted ids, so an id lands in the same shard on every run and a
failure is reproduced by running that shard's log line again.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import unittest
from collections import Counter
from pathlib import Path


def test_ids(start_dir: str, top_level: str) -> list[str]:
    """Every test id the sequential run would execute, in a stable order."""
    suite = unittest.TestLoader().discover(start_dir, top_level_dir=top_level)
    found: list[str] = []

    def walk(item: object) -> None:
        if isinstance(item, unittest.TestSuite):
            for child in item:
                walk(child)
        else:
            found.append(item.id())  # type: ignore[attr-defined]

    walk(suite)
    return sorted(found)


class _Recording(unittest.TextTestResult):
    """A result that writes down what each test did, so the parent can account for them.

    A skipped test starts and unittest calls the run successful, so counting the tests that
    started would report a prerequisite nobody had as a test that ran. Skips are recorded apart
    from the tests that executed a body.
    """

    started: list[str] = []
    skipped: list[str] = []

    def startTest(self, test: unittest.TestCase) -> None:  # noqa: N802 - unittest naming
        type(self).started.append(test.id())
        super().startTest(test)

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:  # noqa: N802
        type(self).skipped.append(test.id())
        super().addSkip(test, reason)


def run_shard(ids_file: str, executed_file: str) -> int:
    """One shard: run the ids in ``ids_file`` and write down what each of them did."""
    ids = json.loads(Path(ids_file).read_text(encoding="utf-8"))
    _Recording.started, _Recording.skipped = [], []
    suite = unittest.TestLoader().loadTestsFromNames(ids)
    runner = unittest.TextTestRunner(verbosity=1, resultclass=_Recording)
    result = runner.run(suite)
    Path(executed_file).write_text(
        json.dumps({"started": _Recording.started, "skipped": _Recording.skipped}),
        encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


def _shard_environment() -> dict[str, str]:
    """The parent's own import path, handed to the shard.

    A shard runs this file by path, so its own `sys.path[0]` is this directory rather than the
    checkout. Passing what the parent imports from keeps `--shards 1` and the sharded run alike,
    whether the caller exported PYTHONPATH or not.
    """
    inherited = [entry for entry in sys.path if entry and entry != str(Path(__file__).parent)]
    return {**os.environ, "PYTHONPATH": os.pathsep.join(dict.fromkeys(inherited))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-dir", default="evals/odoo/tests")
    parser.add_argument("--top-level", default=".")
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--log-dir", default=".artifacts/offline-suite")
    parser.add_argument("--run-ids", help=argparse.SUPPRESS)
    parser.add_argument("--executed", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.run_ids:
        return run_shard(args.run_ids, args.executed)

    ids = test_ids(args.start_dir, args.top_level)
    if not ids:
        print("no tests discovered", file=sys.stderr)
        return 2
    shards = max(1, args.shards)
    groups = [group for group in (ids[index::shards] for index in range(shards)) if group]
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    running = []
    for index, group in enumerate(groups):
        assigned = log_dir / f"shard-{index}.ids.json"
        assigned.write_text(json.dumps(group), encoding="utf-8")
        executed = log_dir / f"shard-{index}.executed.json"
        executed.unlink(missing_ok=True)
        log = log_dir / f"shard-{index}.log"
        handle = log.open("wb")
        running.append((index, group, executed, log, handle, subprocess.Popen(
            [sys.executable, __file__, "--run-ids", str(assigned), "--executed", str(executed)],
            stdout=handle, stderr=subprocess.STDOUT, env=_shard_environment())))
    results = []
    for index, group, executed, log, handle, process in running:
        code = process.wait()
        handle.close()
        try:
            reported = json.loads(executed.read_text(encoding="utf-8"))
            ran = (reported["started"], reported["skipped"])
        except (OSError, ValueError, KeyError, TypeError):
            ran = None
        results.append((index, code, ran, group, log))
    elapsed = time.monotonic() - started

    print(f"{'shard':>5} {'exit':>5} {'ran':>5} {'skipped':>8} {'assigned':>9}  log")
    for index, code, ran, group, log in results:
        counts = ("-", "-") if ran is None else (len(ran[0]) - len(ran[1]), len(ran[1]))
        print(f"{index:>5} {code:>5} {counts[0]:>5} {counts[1]:>8} {len(group):>9}  {log}")
    print(f"in {elapsed:.1f}s across {len(results)} shards")
    return account(ids, [item[2] for item in results], [item[1] for item in results],
                   logs=[item[4] for item in results])


def account(discovered: list[str], executed: list[tuple[list[str], list[str]] | None],
            codes: list[int], *, logs: list[Path] | None = None) -> int:
    """The run's exit code: 2 unless every discovered test ran exactly once and none was skipped,
    then 1 if a shard failed, else 0.

    Counting alone would let a shard that skipped a test and one that repeated another cancel
    out, so what is compared is the ids the shards reported. A skipped test is refused rather
    than counted: unittest calls a run with skips successful, so a prerequisite nobody had would
    otherwise be reported as a suite that passed.
    """
    ran: Counter[str] = Counter()
    skipped: list[str] = []
    for reported in executed:
        started, missed = reported if reported is not None else ([], [])
        ran.update(started)
        skipped.extend(missed)
    print(f"discovered {len(discovered)}, ran {sum(ran.values()) - len(skipped)}, "
          f"skipped {len(skipped)}")
    missing = sorted(set(discovered) - set(ran))
    repeated = sorted(name for name, count in ran.items() if count > 1)
    foreign = sorted(set(ran) - set(discovered))
    silent = [index for index, reported in enumerate(executed) if reported is None]
    if missing or repeated or foreign or silent or skipped:
        for label, names in (("never ran", missing), ("ran more than once", repeated),
                             ("are not in this suite", foreign),
                             ("were skipped, so their prerequisite is absent",
                              sorted(skipped))):
            if names:
                print(f"REFUSED: {len(names)} tests {label}: {', '.join(map(str, names[:5]))}",
                      file=sys.stderr)
        for index in silent:
            print(f"REFUSED: shard {index} reported no tests at all", file=sys.stderr)
        return 2
    failed = [index for index, code in enumerate(codes) if code != 0]
    for index in failed:
        where = f"; read {logs[index]}" if logs else ""
        print(f"shard {index} failed (exit {codes[index]}){where}", file=sys.stderr)
    if failed:
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
