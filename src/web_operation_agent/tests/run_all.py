"""Run the product test suite with a spend-ledger integrity guard.

Synthetic usage is indistinguishable from real usage once written to the production ledger.
The whole suite therefore runs between two fingerprints of that file and fails if any test
changes it. Individual meter tests must always use a temporary ledger path.

    python -m web_operation_agent.tests.run_all [-k pattern]
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[2]))

from web_operation_agent.modeling.client import DEFAULT_LEDGER      # noqa: E402


def fingerprint(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return "absent"
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return f"{h.hexdigest()}:{p.stat().st_size}"


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def is_browser(test):
    return bool(getattr(type(test), "__woa_browser_test__", False)
                or getattr(getattr(test, test._testMethodName), "__woa_browser_test__", False))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", "--pattern", default="test_*.py")
    ap.add_argument("-v", "--verbose", action="count", default=1)
    ap.add_argument("--group", action="append", default=[], help="core test subdirectory (repeatable)")
    ap.add_argument("--module", action="append", default=[], help="explicit unittest module or test ID")
    ap.add_argument("--start-dir", type=Path, default=HERE)
    ap.add_argument("--top-level", type=Path, default=HERE.parents[1])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--without-browser", action="store_true")
    mode.add_argument("--only-browser", action="store_true")
    ap.add_argument("--exclude", action="append", default=[], help="exclude test-ID glob")
    ap.add_argument("--strict-skips", action="store_true", help="refuse missing integration prerequisites")
    ap.add_argument("--list", action="store_true", help="collect without running tests")
    ap.add_argument("--jobs", type=int, default=1, help="run test modules in this many parallel processes")
    ap.add_argument("--ids-file", type=Path, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    inherited = os.environ.get("WOA_SPEND_LEDGER")
    paths = {DEFAULT_LEDGER} | ({inherited} if inherited else set())
    protected = {name: fingerprint(name) for name in paths}
    with tempfile.TemporaryDirectory(prefix="woa-test-ledger-") as directory:
        scratch = Path(directory) / "spend.jsonl"
        os.environ["WOA_SPEND_LEDGER"] = str(scratch)
        try:
            code = run_selected(a)
            untouched = all(fingerprint(name) == original for name, original in protected.items())
            if not a.list or not untouched:
                print(f"\nspend ledger untouched: {untouched}")
            if not untouched:
                print("FAILED: the test suite modified a protected spend ledger")
                return 2
            return code
        finally:
            if inherited is None:
                os.environ.pop("WOA_SPEND_LEDGER", None)
            else:
                os.environ["WOA_SPEND_LEDGER"] = inherited


def run_selected(a) -> int:
    loader = unittest.TestLoader()
    if a.ids_file:
        suite = loader.loadTestsFromNames(a.ids_file.read_text().split())
    elif a.module:
        suite = loader.loadTestsFromNames(a.module)
    elif a.group:
        suite = unittest.TestSuite()
        for group in sorted(set(a.group)):
            directory = HERE / group
            if directory.parent != HERE or not directory.is_dir():
                ap.error(f"unknown core test group: {group}")
            suite.addTests(loader.discover(str(directory), pattern=a.pattern,
                                          top_level_dir=str(HERE.parents[1])))
    else:
        suite = loader.discover(str(a.start_dir), pattern=a.pattern, top_level_dir=str(a.top_level))
    selected = {test.id(): test for test in cases(suite)
                if not (a.without_browser and is_browser(test))
                and not (a.only_browser and not is_browser(test))
                and not any(fnmatch.fnmatchcase(test.id(), pattern) for pattern in a.exclude)}
    if not selected or loader.errors:
        for error in loader.errors:
            print(error, file=sys.stderr)
        print("REFUSED: empty or incomplete test collection", file=sys.stderr)
        return 2
    if a.list:
        print("\n".join(sorted(selected)))
        return 0

    # Module is the process unit, keeping setUpModule/setUpClass together. Browser tests,
    # not imports, bound the wall clock: each gets its own process unless its class shares fixtures.
    modules = {}
    for name in sorted(selected):
        test = selected[name]
        kind = type(test)
        shared = any(getattr(kind, f).__func__ is not getattr(unittest.TestCase, f).__func__
                     for f in ("setUpClass", "tearDownClass"))
        unit = kind.__module__ if not is_browser(test) else name if not shared else f"{kind.__module__}.{kind.__qualname__}"
        modules.setdefault(unit, []).append(name)
    if a.jobs > 1 and len(modules) > 1:
        return run_parallel(a, modules)

    result = unittest.TextTestRunner(verbosity=a.verbose).run(
        unittest.TestSuite(selected[name] for name in sorted(selected)))

    if a.strict_skips and result.skipped:
        print(f"REFUSED: {len(result.skipped)} selected integration tests were skipped")
        return 2
    return 0 if result.wasSuccessful() else 1


def run_parallel(a, modules) -> int:
    """Run each unit in its own process; the largest start first."""
    with tempfile.TemporaryDirectory(prefix="woa-test-ids-") as directory:
        def run(item):
            module, names = item
            ids = Path(directory) / f"{module}.txt"
            ids.write_text("\n".join(names))
            command = [sys.executable, __file__, "--ids-file", str(ids)]
            command += ["--strict-skips"] if a.strict_skips else []
            started = time.monotonic()
            done = subprocess.run(command, capture_output=True, text=True)
            return module, len(names), done.returncode, done.stdout + done.stderr, time.monotonic() - started

        order = sorted(modules.items(), key=lambda item: -len(item[1]))
        failed, skipped = [], 0
        with ThreadPoolExecutor(a.jobs) as pool:
            for module, count, code, output, seconds in pool.map(run, order):
                if code:
                    failed.append(module)
                    print(output, file=sys.stderr)
                skipped += sum(int(n) for n in re.findall(r"skipped=(\d+)", output))
                print(f"{'FAILED' if code else 'ok':6} {module} ({count} tests, {seconds:.1f}s)", flush=True)
    total = sum(len(names) for names in modules.values())
    print(f"\nRan {total} tests in {len(modules)} modules with {a.jobs} jobs: "
          + (f"FAILED ({', '.join(failed)})" if failed else "OK") + f" (skipped={skipped})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
