"""CLI entry point for running the 5-family surface census."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .ledger import FAMILIES

ROOT = Path(__file__).resolve().parents[3]


def _activate_product_root(product_root: Path) -> None:
    resolved = product_root.resolve()
    product_paths = [
        str(resolved / "src"),
        str(resolved / "examples/yudao/src"),
        str(resolved / "examples/yudao/deploy/local"),
    ]
    for path in reversed(product_paths):
        if path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)
    if resolved != ROOT.resolve():
        for name in list(sys.modules):
            if name == "serve" or name.startswith("web_operation_agent"):
                sys.modules.pop(name, None)


def main(argv: list[str] | None = None) -> int:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--product-root", type=Path, default=ROOT)
    known, _ = pre.parse_known_args(argv)
    _activate_product_root(known.product_root)

    from web_operation_agent_yudao.host.accounts import ACCOUNTS_FILE  # noqa: E402
    from .runner import prepare_plan, run_census  # noqa: E402

    parser = argparse.ArgumentParser(description="Run Yudao 5-family surface census.")
    parser.add_argument("--product-root", type=Path, default=ROOT)
    parser.add_argument("--accounts", type=Path, default=ACCOUNTS_FILE)
    parser.add_argument("--account", default="admin")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / ".q0-cache/phase-53/oracle")
    parser.add_argument("--out-dir", type=Path, default=ROOT / ".q0-cache/phase-53/baseline")
    parser.add_argument("--families", default=",".join(FAMILIES))
    parser.add_argument("--envs", default="1")
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--lease-token", default=None)
    parser.add_argument("--only-cells-file", type=Path, default=None)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--no-retry-flakes", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--refresh-oracle", action="store_true")
    parser.add_argument("--refresh-plan", action="store_true")
    parser.add_argument("--max-cells", type=int, default=0)
    parser.add_argument("--port-base", type=int, default=48300)
    args = parser.parse_args(argv)

    families = tuple(f.strip() for f in args.families.split(",") if f.strip())
    envs = tuple(int(e.strip()) for e in args.envs.split(",") if e.strip())
    if args.prepare_only:
        plan = prepare_plan(
            product_root=args.product_root,
            accounts_file=args.accounts,
            cache_dir=args.cache_dir,
            envs=envs,
            account=args.account,
            refresh_oracle=args.refresh_oracle,
            refresh_plan=args.refresh_plan,
            lease_token=args.lease_token,
        )
        print(json.dumps({
            "inventory_summary": plan.get("inventory_summary"),
            "total_cells": len(plan["cells"]),
            "coverage_meta": {
                k: {kk: vv for kk, vv in v.items() if kk not in {"applicable_entity_keys", "inapplicable_reasons"}}
                for k, v in plan["coverage_meta"].items()
            },
        }, ensure_ascii=False, indent=2))
        return 0

    only_cells = None
    if args.only_cells_file is not None:
        only_cells = set(json.loads(args.only_cells_file.read_text(encoding="utf-8")))

    summary = run_census(
        product_root=args.product_root,
        accounts_file=args.accounts,
        cache_dir=args.cache_dir,
        out_dir=args.out_dir,
        families=families,
        envs=envs,
        account=args.account,
        resume=not args.no_resume,
        only_cells=only_cells,
        retry_flakes=not args.no_retry_flakes,
        max_cells=args.max_cells,
        port_base=args.port_base,
        workers=args.workers,
        lease_token=args.lease_token,
    )
    print(json.dumps({
        k: summary[k] for k in ("total_cells", "pass", "fail", "unsupported", "untested", "tested", "fail_rate", "total_elapsed_s")
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
