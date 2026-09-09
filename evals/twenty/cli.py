"""Command-line process boundary for Twenty fixture evaluation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .evaluator import (ARTIFACT_VERSION, EvaluationInputError, evaluate,
                        make_unavailable_state)
from .fixture import (FixtureUnavailable, TwentyPostgresFixture, atomic_write_json,
                      load_json, prepare_snapshot, qualify_fixture, restore_snapshot,
                      utc_now)
from .protocol import freeze_protocol, verify_protocol
from .records import append_trial, initialize_batch, verify_batch
from .taskset import (instantiate_task, seal_task_set, verify_split_separation,
                      verify_task_set)


_EXIT = {
    "PASS": 0, "READY": 0, "FROZEN": 0, "OPEN": 0, "COMPLETE": 0,
    "FAIL": 1, "STOPPED_SAFETY": 1, "INVALID": 2, "SEALED_INVALID": 2,
    "INDETERMINATE": 3,
}


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


def _add_protocol_artifacts(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--protocol", required=True)
    parser.add_argument("--development-seal", required=True)
    parser.add_argument("--test-seal", required=True)
    parser.add_argument("--split-verification", required=True)


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


def _validate_distinct_output(args: argparse.Namespace, *input_names: str) -> None:
    if not getattr(args, "output", None):
        return
    output = Path(args.output).resolve()
    if any(output == Path(getattr(args, name)).resolve() for name in input_names):
        args.output = None
        raise EvaluationInputError("evaluation output must not overwrite an input artifact")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.twenty",
        description="Independent deterministic checks for a local synthetic Twenty fixture.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("baseline", "capture", "qualify"):
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

    command = commands.add_parser("seal-taskset")
    command.add_argument("--taskset", required=True)
    command.add_argument("--output", required=True)

    command = commands.add_parser("verify-taskset")
    command.add_argument("--taskset", required=True)
    command.add_argument("--seal", required=True)
    command.add_argument("--output")

    command = commands.add_parser("verify-splits")
    command.add_argument("--development-seal", required=True)
    command.add_argument("--test-seal", required=True)
    command.add_argument("--output")

    command = commands.add_parser("instantiate-task")
    command.add_argument("--taskset", required=True)
    command.add_argument("--task-id", required=True)
    command.add_argument("--baseline", required=True)
    command.add_argument("--output", required=True)

    for name in ("freeze-protocol", "verify-protocol"):
        command = commands.add_parser(name)
        _add_protocol_artifacts(command)
        command.add_argument("--output", required=name == "freeze-protocol")

    command = commands.add_parser("init-batch")
    _add_protocol_artifacts(command)
    command.add_argument("--ledger", required=True)
    command.add_argument("--batch-id", required=True)
    command.add_argument("--created-at", required=True)
    command.add_argument("--output")

    command = commands.add_parser("append-trial")
    _add_protocol_artifacts(command)
    command.add_argument("--ledger", required=True)
    command.add_argument("--record", required=True)
    command.add_argument("--output")

    command = commands.add_parser("verify-batch")
    _add_protocol_artifacts(command)
    command.add_argument("--ledger", required=True)
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
        if args.command == "qualify":
            return _emit(qualify_fixture(_fixture(args)), args.output)
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
        if args.command == "restore":
            _validate_recovery_paths(args)
            value = restore_snapshot(
                fixture=_fixture(args), expected_digest=args.expected_digest,
                snapshot_path=Path(args.snapshot), record_path=Path(args.record),
            )
            return _emit(value, args.output)
        if args.command == "seal-taskset":
            _validate_distinct_output(args, "taskset")
            return _emit(seal_task_set(load_json(Path(args.taskset))), args.output)
        if args.command == "verify-taskset":
            _validate_distinct_output(args, "taskset", "seal")
            value = verify_task_set(
                load_json(Path(args.taskset)), load_json(Path(args.seal)))
            return _emit(value, args.output)
        if args.command == "verify-splits":
            _validate_distinct_output(args, "development_seal", "test_seal")
            value = verify_split_separation(
                load_json(Path(args.development_seal)),
                load_json(Path(args.test_seal)),
            )
            return _emit(value, args.output)
        if args.command in {"freeze-protocol", "verify-protocol"}:
            _validate_distinct_output(
                args, "protocol", "development_seal", "test_seal",
                "split_verification")
            inputs = {
                "development_seal": load_json(Path(args.development_seal)),
                "test_seal": load_json(Path(args.test_seal)),
                "split_verification": load_json(Path(args.split_verification)),
            }
            if args.command == "freeze-protocol":
                value = freeze_protocol(load_json(Path(args.protocol)), **inputs)
            else:
                value = verify_protocol(load_json(Path(args.protocol)), **inputs)
            return _emit(value, args.output)
        if args.command in {"init-batch", "append-trial", "verify-batch"}:
            input_names = [
                "protocol", "development_seal", "test_seal", "split_verification",
                "ledger",
            ]
            if args.command == "append-trial":
                input_names.append("record")
            _validate_distinct_output(args, *input_names)
            inputs = {
                "protocol": load_json(Path(args.protocol)),
                "development_seal": load_json(Path(args.development_seal)),
                "test_seal": load_json(Path(args.test_seal)),
                "split_verification": load_json(Path(args.split_verification)),
            }
            if args.command == "init-batch":
                value = initialize_batch(
                    args.ledger, batch_id=args.batch_id, created_at=args.created_at,
                    **inputs)
            elif args.command == "append-trial":
                value = append_trial(
                    args.ledger, load_json(Path(args.record)), **inputs)
            else:
                value = verify_batch(args.ledger, **inputs)
            return _emit(value, args.output)
        _validate_distinct_output(args, "taskset", "baseline")
        value = instantiate_task(
            load_json(Path(args.taskset)), args.task_id,
            load_json(Path(args.baseline)),
        )
        return _emit(value, args.output)
    except (EvaluationInputError, FixtureUnavailable) as exc:
        if args.command in {
                "seal-taskset", "verify-taskset", "verify-splits", "instantiate-task",
                "freeze-protocol", "verify-protocol", "init-batch", "append-trial",
                "verify-batch"}:
            args.output = None
        return _emit({
            "artifact_version": ARTIFACT_VERSION,
            "kind": f"twenty_{args.command}",
            "status": "INVALID",
            "reason": str(exc),
        }, getattr(args, "output", None))


if __name__ == "__main__":
    raise SystemExit(main())
