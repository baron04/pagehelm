"""Command-line process boundary for Twenty fixture evaluation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .evaluator import (ARTIFACT_VERSION, EvaluationInputError, evaluate,
                        make_unavailable_state)
from .fixture import (FixtureUnavailable, TwentyPostgresFixture, atomic_write_json,
                      load_json, prepare_snapshot, restore_snapshot, utc_now)


_EXIT = {"PASS": 0, "READY": 0, "FAIL": 1, "INVALID": 2, "INDETERMINATE": 3}


def _fixture(args: argparse.Namespace) -> TwentyPostgresFixture:
    return TwentyPostgresFixture(
        container=args.container, database=args.database, db_user=args.db_user,
        workspace_schema=args.workspace_schema,
    )


def _add_fixture(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--container", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--db-user", required=True)
    parser.add_argument("--workspace-schema", required=True)


def _emit(value: dict, output: str | None) -> int:
    if output:
        atomic_write_json(Path(output), value)
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return _EXIT.get(str(value.get("status")), 2)


def _validate_recovery_paths(args: argparse.Namespace) -> None:
    paths = [Path(args.snapshot).resolve(), Path(args.record).resolve()]
    if args.output:
        paths.append(Path(args.output).resolve())
    if len(paths) != len(set(paths)):
        args.output = None
        raise EvaluationInputError("snapshot, recovery record, and output paths must differ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.twenty",
        description="Independent deterministic checks for a local synthetic Twenty fixture.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("baseline", "capture"):
        command = commands.add_parser(name)
        _add_fixture(command)
        command.add_argument("--output", required=True)

    command = commands.add_parser("evaluate")
    command.add_argument("--task", required=True)
    command.add_argument("--before", required=True)
    command.add_argument("--after", required=True)
    command.add_argument("--evidence", required=True)
    command.add_argument("--output")

    command = commands.add_parser("snapshot")
    _add_fixture(command)
    command.add_argument("--baseline", required=True)
    command.add_argument("--expected-digest", required=True)
    command.add_argument("--snapshot", required=True)
    command.add_argument("--record", required=True)
    command.add_argument("--output")

    command = commands.add_parser("restore")
    _add_fixture(command)
    command.add_argument("--expected-digest", required=True)
    command.add_argument("--snapshot", required=True)
    command.add_argument("--record", required=True)
    command.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command in {"baseline", "capture"}:
            fixture = _fixture(args)
            role = "baseline" if args.command == "baseline" else "observation"
            try:
                value = fixture.capture(role=role)
            except FixtureUnavailable:
                value = make_unavailable_state(
                    fixture=fixture.identity, captured_at=utc_now(), role=role,
                )
            return _emit(value, args.output)
        if args.command == "evaluate":
            value = evaluate(
                task=load_json(Path(args.task)), before=load_json(Path(args.before)),
                after=load_json(Path(args.after)), evidence=load_json(Path(args.evidence)),
            )
            return _emit(value, args.output)
        if args.command == "snapshot":
            _validate_recovery_paths(args)
            value = prepare_snapshot(
                fixture=_fixture(args), baseline=load_json(Path(args.baseline)),
                expected_digest=args.expected_digest, snapshot_path=Path(args.snapshot),
                record_path=Path(args.record),
            )
            return _emit(value, args.output)
        _validate_recovery_paths(args)
        value = restore_snapshot(
            fixture=_fixture(args), expected_digest=args.expected_digest,
            snapshot_path=Path(args.snapshot), record_path=Path(args.record),
        )
        return _emit(value, args.output)
    except (EvaluationInputError, FixtureUnavailable) as exc:
        return _emit({
            "artifact_version": ARTIFACT_VERSION,
            "kind": f"twenty_{args.command}",
            "status": "INVALID",
            "reason": str(exc),
        }, getattr(args, "output", None))


if __name__ == "__main__":
    raise SystemExit(main())
