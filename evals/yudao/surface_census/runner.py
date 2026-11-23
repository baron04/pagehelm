"""Dual-environment runner for the 5-family surface census using FullStack, ScriptedModel, and Panel."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import parse_qsl, urlsplit

from playwright.sync_api import sync_playwright

from evals.yudao.env_lease import (
    DEFAULT_LEASE_LOCK_FILE,
    DEFAULT_LEASE_STATE_FILE,
    EnvLeaseGuard,
)
from evals.yudao.write_contract.fullstack import ScriptedModel
from evals.yudao.write_contract.panel import Panel
from evals.yudao.write_contract.stack import login

from .inventory import build_inventory
from .ledger import FAMILIES, CellResult, Ledger
from .oracle import IndependentApiOracle, build_census_plan, inspect_all_routes
from .templates import TurnExecution, evaluate_cell, make_scripted_turn

_NAV_SEQ_SCRIPT = """
(() => {
  try {
    const seq = Number(sessionStorage.getItem('__woa_census_seq') || '0') + 1;
    sessionStorage.setItem('__woa_census_seq', String(seq));
    window.__woa_nav_seq = seq;
  } catch (e) {
    window.__woa_nav_seq = Math.random();
  }
})();
"""

_SESSION_AUTH_INIT_SCRIPT = """
(() => {
  try {
    if (localStorage.getItem('ACCESS_TOKEN')) return;
    const raw = sessionStorage.getItem('__woa_auth_bundle');
    if (!raw) return;
    const b = JSON.parse(raw);
    if (b.a) localStorage.setItem('ACCESS_TOKEN', b.a);
    if (b.r) localStorage.setItem('REFRESH_TOKEN', b.r);
    if (b.t) localStorage.setItem('tenantId', b.t);
  } catch (e) {}
})();
"""


def _safe_id(cell_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", cell_id)


def _load_product_modules(product_root: Path):
    """Ensure product packages and local `serve` load from `product_root`."""
    root = Path(product_root).resolve()
    paths = [str(root / "src"), str(root / "examples/yudao/src"), str(root / "examples/yudao/deploy/local")]
    for p in reversed(paths):
        if p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)
    import serve  # noqa: E402
    from web_operation_agent_yudao.host import native_server as native  # noqa: E402
    from web_operation_agent_yudao.reads.client import YudaoClient  # noqa: E402
    return root, serve, native, YudaoClient


class CensusStack:
    """Product stack matching local deployment (`serve.py`) with a local ScriptedModel."""

    def __init__(
        self,
        environment: int,
        state: Path,
        accounts: Path,
        script_dispatch,
        *,
        product_root: Path,
        account: str = "admin",
        front_port: int = 48300,
        model_name: str = "deepseek-flash",
    ):
        self.environment = environment
        self.state = Path(state)
        self.accounts = Path(accounts)
        self.account = account
        self.upstream = f"http://localhost:{40000 + environment}"
        self.ports = (front_port, front_port + 1, front_port + 2, front_port + 3)
        self.origin = f"http://localhost:{front_port}"
        self.model = ScriptedModel(script_dispatch)
        self.model_name = model_name
        self.product_root, self._serve, self._native, self._YudaoClient = _load_product_modules(product_root)
        self.front = self.host = self.boot = None
        self.token = None
        self.client = None
        self._auth_lock = threading.Lock()
        self._last_auth_ts = 0.0

    def refresh_auth(self):
        with self._auth_lock:
            self.token = login(self.upstream, self.accounts, self.account)
            self.client = self._YudaoClient.for_token(self.upstream, self.token["access"], self.token["tenant"])
            self._last_auth_ts = time.monotonic()
            return self.client

    def start(self) -> "CensusStack":
        self.state.mkdir(parents=True, exist_ok=True)
        serve = self._serve
        native = self._native
        root = self.product_root
        settings = serve.Settings(
            self.environment,
            "phase53-census",
            self.origin,
            upstream=self.upstream,
            state=self.state,
            ports=self.ports,
            listen_host="127.0.0.1",
        )
        self.front = serve.Front(settings)
        threading.Thread(target=self.front.serve_forever, daemon=True).start()
        env = {
            "AGENT_OPENAI_BASE_URL": self.model.url,
            "AGENT_OPENAI_API_KEY": "scripted",
            "AGENT_OPENAI_MODEL": self.model_name,
        }
        policy = root / "examples/yudao/deploy/cloud/native-policy"
        cache_dir = root / ".q0-cache/yudao"
        self.host = native.YudaoHost(native.YudaoHostConfig(
            api_base=self.upstream,
            public_origin=self.origin,
            public_agent_url=self.origin + "/woa-agent",
            state_dir=self.state,
            gateway_host="127.0.0.1",
            gateway_port=self.ports[1],
            bootstrap_host="127.0.0.1",
            bootstrap_port=self.ports[2],
            native_guard_port=self.ports[3],
            native_ingress_base=self.origin,
            ui_source=cache_dir / "ui",
            source_root=cache_dir if (cache_dir / "server").is_dir() else None,
            source_release="v2026.09",
            native_read_policy=policy / "fence.json",
            native_page_policy=policy / "pages.json",
            native_money_policy=policy / "verified-money.json",
            native_filter_semantics=policy / "filters.json",
            native_page_admission="read-only-window",
            native_writes=True,
            native_submit=True,
            native_edit_prefill=True,
            model=self.model_name,
            model_env=env,
            daily_allowance_cny=50.0,
            turn_trace_days=0,
        ))
        self.host.server.start()
        self.boot = ThreadingHTTPServer(("127.0.0.1", self.ports[2]), native._handler(self.host))
        threading.Thread(target=self.boot.serve_forever, daemon=True).start()
        threading.Thread(target=self.host._warm_api_docs, daemon=True).start()
        self.front.ready = True
        self.front.api_docs_loaded = True
        self.refresh_auth()
        return self

    def close(self) -> None:
        if self.boot is not None:
            self.boot.shutdown()
            self.boot.server_close()
        if self.host is not None:
            self.host.close()
        if self.front is not None:
            self.front.shutdown()
            self.front.server_close()
        self.model.close()

    def __enter__(self) -> "CensusStack":
        return self.start()

    def __exit__(self, *_) -> None:
        self.close()

    def sign_in_script(self, *, only_if_empty: bool = False) -> str:
        record = lambda value: json.dumps({"c": 0, "e": 1.7976931348623157e308, "v": json.dumps(value)})
        a = record(self.token["access"])
        r = record(self.token["refresh"])
        t = record(self.token["tenant"])
        guard = "if (localStorage.getItem('ACCESS_TOKEN')) return;" if only_if_empty else ""
        return (
            "(() => { try {"
            + f"sessionStorage.setItem('__woa_auth_bundle', {json.dumps(json.dumps({'a': a, 'r': r, 't': t}))});"
            + guard
            + f"localStorage.setItem('ACCESS_TOKEN', {json.dumps(a)});"
            + f"localStorage.setItem('REFRESH_TOKEN', {json.dumps(r)});"
            + f"localStorage.setItem('tenantId', {json.dumps(t)});"
            + "} catch (e) {} })()"
        )


def prepare_plan(
    *,
    product_root: Path,
    accounts_file: Path,
    cache_dir: Path,
    envs: tuple[int, ...] = (1,),
    account: str = "admin",
    refresh_oracle: bool = False,
    refresh_plan: bool = False,
    lease_token: str | None = None,
) -> dict:
    """Build or load the shared deterministic 5-family census plan & independent oracle."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    plan_file = cache_dir / "census-plan.json"
    if not refresh_plan and not refresh_oracle and plan_file.is_file():
        return json.loads(plan_file.read_text(encoding="utf-8"))

    with EnvLeaseGuard(envs, experiment="surface-census-prepare", lease_token=lease_token):
        root, _serve, native, YudaoClient = _load_product_modules(product_root)
        from web_operation_agent_yudao.goal_compat.goal import YudaoGoalData
        from web_operation_agent_yudao.rules.ui_source import UiSource

        env_id = envs[0]
        upstream = f"http://localhost:{40000 + env_id}"
        tok = login(upstream, accounts_file, account)
        client = YudaoClient.for_token(upstream, tok["access"], tok["tenant"])
        perm = client.get("system/auth/get-permission-info")
        api_docs = native._api_docs(upstream, wait_s=0, timeout_s=120)
        ui = UiSource.read(root / ".q0-cache/yudao/ui")
        gd = YudaoGoalData(client, api_docs, ui=ui)
        outline = gd.outline()

        inv = build_inventory(
            menus=perm.get("menus") or [],
            api_docs=api_docs,
            compiled_outline=outline,
            compiled_entities_meta=gd._entities,
        )
        (cache_dir / "inventory.json").write_text(json.dumps(inv, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        routes = [p["route"] for p in inv["core_pages"]]
        route_snaps = inspect_all_routes(
            routes,
            accounts_file=accounts_file,
            cache_dir=cache_dir,
            envs=envs,
            account=account,
            refresh=refresh_oracle,
        )

        from datetime import date
        plan = build_census_plan(inv, route_snaps, client, today_str=date.today().isoformat(), goal_data=gd)
        plan["inventory_summary"] = {
            "core_pages": len(inv["core_pages"]),
            "core_entities": len(inv["core_entities"]),
            "excluded_pages": len(inv["excluded_pages"]),
        }
        plan_file.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return plan


def _run_one_attempt(
    cell: dict,
    *,
    stack: CensusStack,
    page,
    panel: Panel,
    active_script_holder: dict,
    oracle: IndependentApiOracle,
    route_snapshot: dict | None,
    today_str: str,
) -> tuple[CellResult, dict]:
    prompt, script_fn, bag = make_scripted_turn(cell, today_str=today_str)
    active_script_holder["fn"] = script_fn
    stack.model.requests.clear()
    stack.model.errors.clear()

    # Reset browser to portal root for browser families or if not already on root/index or /login.
    cur_path = urlsplit(page.url).path
    if cell["family"] in {"open_page", "filter_page"} or cur_path not in {"/", "/index"}:
        page.evaluate(stack.sign_in_script(only_if_empty=False))
        page.goto(stack.origin + "/", wait_until="domcontentloaded", timeout=20000)
        try:
            page.wait_for_selector(".el-menu, .layout-container, aside", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        if urlsplit(page.url).path.startswith("/login") or not page.locator(".el-menu, .layout-container, aside").count():
            stack.refresh_auth()
            page.evaluate(stack.sign_in_script(only_if_empty=False))
            page.goto(stack.origin + "/", wait_until="domcontentloaded", timeout=20000)
            try:
                page.wait_for_selector(".el-menu, .layout-container, aside", timeout=15000)
            except Exception:  # noqa: BLE001
                pass

    page.evaluate("() => window.__WOA_ASSISTANT__?.newConversation?.()")
    page.wait_for_timeout(120)
    panel.open()

    nav_before = int(page.evaluate("() => window.__woa_nav_seq || 0") or 0)
    captured_gets: list[dict] = []

    def on_resp(resp) -> None:
        if resp.request.method != "GET":
            return
        parsed = urlsplit(resp.url)
        if not parsed.path.startswith("/admin-api/"):
            return
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            payload = None
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        data = payload.get("data") if isinstance(payload, dict) else None
        row_ids = []
        total = None
        row_count = None
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            total = data.get("total")
            row_count = len(data["list"])
            row_ids = [str(r.get("id")) for r in data["list"][:20] if isinstance(r, dict) and r.get("id") is not None]
        elif isinstance(data, list):
            total = len(data)
            row_count = len(data)
            row_ids = [str(r.get("id")) for r in data[:20] if isinstance(r, dict) and r.get("id") is not None]
        captured_gets.append({
            "t": time.time(),
            "path": parsed.path,
            "params": params,
            "status": resp.status,
            "code": payload.get("code") if isinstance(payload, dict) else None,
            "total": total,
            "row_count": row_count,
            "row_ids": row_ids,
        })

    page.on("response", on_resp)
    t0 = time.time()
    try:
        done_fn = lambda: page.locator(".woa-turn").last.locator(".woa-work__summary, .woa-took").count() > 0

        def decide(kind: str, _text: str) -> str:
            return "approve" if kind == "operate" else "deny"

        panel.ask(prompt)
        res = panel.run(decide, timeout_s=45, quiet_s=0.15, done=done_fn)
    finally:
        page.remove_listener("response", on_resp)
    elapsed_s = time.time() - t0
    nav_after = int(page.evaluate("() => window.__woa_nav_seq || 0") or 0)

    exe = TurnExecution(
        prompt=prompt,
        reply=res["reply"],
        timed_out=res["timed_out"],
        final_url=page.url,
        requests=list(stack.model.requests),
        model_errors=list(stack.model.errors),
        cards=[{k: v for k, v in c.items() if k != "node"} for c in res["cards"]],
        api_gets_during_turn=captured_gets,
        nav_seq_before=nav_before,
        nav_seq_after=nav_after,
    )
    verdict = evaluate_cell(cell, exe, bag, oracle=oracle, route_snapshot=route_snapshot, elapsed_s=elapsed_s)
    tool_msgs_dedup: list[str] = []
    seen_msgs: set[str] = set()
    for req in exe.requests:
        for m in req.get("messages", ()):
            if m.get("role") == "tool":
                txt = str(m.get("content") or "")[:1200]
                if txt not in seen_msgs:
                    seen_msgs.add(txt)
                    tool_msgs_dedup.append(txt)
    evidence = {
        "cell": cell,
        "verdict": verdict.to_dict(),
        "prompt": prompt,
        "reply": exe.reply,
        "timed_out": exe.timed_out,
        "final_url": exe.final_url,
        "nav_seq_before": nav_before,
        "nav_seq_after": nav_after,
        "cards": exe.cards,
        "model_errors": exe.model_errors,
        "bag": {k: v for k, v in bag.items() if k not in {"table_info"}},
        "api_gets_during_turn": captured_gets[-10:],
        "tool_messages": tool_msgs_dedup,
    }
    return verdict, evidence


def run_census(
    *,
    product_root: Path,
    accounts_file: Path,
    cache_dir: Path,
    out_dir: Path,
    families: tuple[str, ...] = FAMILIES,
    envs: tuple[int, ...] = (1,),
    account: str = "admin",
    resume: bool = True,
    only_cells: set[str] | None = None,
    retry_flakes: bool = True,
    max_cells: int = 0,
    port_base: int = 48300,
    workers: int = 0,
    lease_token: str | None = None,
) -> dict:
    """Execute the census plan over live local environments and write ledger + summary."""
    cache_dir = Path(cache_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ev_dir = out_dir / "evidence"
    ev_dir.mkdir(parents=True, exist_ok=True)

    with EnvLeaseGuard(envs, experiment="surface-census", lease_token=lease_token) as guard:
        plan = prepare_plan(
            product_root=product_root,
            accounts_file=accounts_file,
            cache_dir=cache_dir,
            envs=envs,
            account=account,
            lease_token=guard.active_token,
        )
        today_str = plan["today"]
        routes_dir = cache_dir / "oracle-routes"

        def load_route_snap(route: str) -> dict | None:
            if not route:
                return None
            from .oracle import _route_slug
            p = routes_dir / f"{_route_slug(route)}.json"
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8"))
            return None

        ledger = Ledger(out_dir / "ledger.jsonl")
        selected = [c for c in plan["cells"] if c["family"] in families]
        if only_cells is not None:
            selected = [c for c in selected if c["cell_id"] in only_cells]
        if max_cells > 0:
            selected = selected[:max_cells]

        # Record preflight untested cells immediately without launching a turn.
        todo: list[dict] = []
        for c in selected:
            if resume and ledger.has(c["cell_id"]):
                continue
            if c.get("preflight_status") == "untested":
                res = CellResult(
                    cell_id=c["cell_id"],
                    family=c["family"],
                    entity=c["entity"],
                    domain=c["domain"],
                    operation=c["operation"],
                    field_or_control=c["field_or_control"],
                    field_or_control_type=c["field_or_control_type"],
                    status="untested",
                    reason_code=c.get("preflight_reason", "untested"),
                    detail=c.get("preflight_reason", "untested"),
                )
                ledger.append(res)
            else:
                todo.append(c)

        if todo:
            lock = threading.Lock()
            n_workers = max(1, int(workers)) if workers > 0 else len(envs)
            buckets: list[list[dict]] = [[] for _ in range(n_workers)]
            for i, c in enumerate(todo):
                buckets[i % n_workers].append(c)
            active = [(i, envs[i % len(envs)], b) for i, b in enumerate(buckets) if b]
            ready_barrier = threading.Barrier(len(active)) if len(active) > 1 else None

            def worker(worker_idx: int, env_id: int, batch: list[dict]) -> None:
                if not batch:
                    return
                state_dir = out_dir / f"stack-env{env_id}-w{worker_idx}"
                holder = {"fn": lambda body: {"role": "assistant", "content": "ok"}}
                stack = CensusStack(
                    env_id,
                    state_dir,
                    accounts_file,
                    lambda body: holder["fn"](body),
                    product_root=product_root,
                    account=account,
                    front_port=port_base + worker_idx * 20,
                )
                try:
                    stack.__enter__()
                except Exception:
                    if ready_barrier is not None:
                        ready_barrier.abort()
                    raise
                try:
                    if ready_barrier is not None:
                        ready_barrier.wait(timeout=120)
                    oracle = IndependentApiOracle(stack.client, refresh_client=stack.refresh_auth)
                    with sync_playwright() as pw:
                        browser = pw.chromium.launch(headless=True)
                        ctx = browser.new_context(viewport={"width": 1500, "height": 1000})
                        ctx.add_init_script(_SESSION_AUTH_INIT_SCRIPT)
                        ctx.add_init_script(_NAV_SEQ_SCRIPT)
                        page = ctx.new_page()
                        page.goto(stack.origin + "/login", wait_until="domcontentloaded", timeout=25000)
                        page.evaluate(stack.sign_in_script(only_if_empty=False))
                        page.goto(stack.origin + "/", wait_until="domcontentloaded", timeout=25000)
                        page.wait_for_selector(".el-menu, .layout-container, aside", timeout=20000)
                        panel = Panel(page)
                        panel.open()

                        for idx, cell in enumerate(batch, start=1):
                            if time.monotonic() - stack._last_auth_ts >= 600.0:
                                stack.refresh_auth()
                                page.evaluate(stack.sign_in_script(only_if_empty=False))
                            snap = load_route_snap(cell.get("route", ""))
                            verdict, ev = _run_one_attempt(
                                cell,
                                stack=stack,
                                page=page,
                                panel=panel,
                                active_script_holder=holder,
                                oracle=oracle,
                                route_snapshot=snap,
                                today_str=today_str,
                            )
                            attempts = 1
                            flaky = False
                            # Section 3.7 flake rule for browser families: rerun failed cells up to 2 more times;
                            # fail only if >= 2/3 attempts fail.
                            if retry_flakes and cell["family"] in {"open_page", "filter_page"} and verdict.status == "fail":
                                verdict2, ev2 = _run_one_attempt(
                                    cell, stack=stack, page=page, panel=panel,
                                    active_script_holder=holder, oracle=oracle,
                                    route_snapshot=snap, today_str=today_str,
                                )
                                attempts = 2
                                if verdict2.status == "pass":
                                    verdict3, ev3 = _run_one_attempt(
                                        cell, stack=stack, page=page, panel=panel,
                                        active_script_holder=holder, oracle=oracle,
                                        route_snapshot=snap, today_str=today_str,
                                    )
                                    attempts = 3
                                    if verdict3.status == "pass":
                                        verdict = CellResult(
                                            **{**verdict3.to_dict(), "flaky": True, "attempts": 3},
                                        )
                                        ev = ev3
                                        flaky = True
                                    else:
                                        verdict = CellResult(
                                            **{**verdict3.to_dict(), "flaky": True, "attempts": 3},
                                        )
                                        ev = ev3
                                        flaky = True
                                else:
                                    verdict = CellResult(
                                        **{**verdict2.to_dict(), "attempts": 2},
                                    )
                                    ev = ev2
                            del flaky
                            ev_path = ev_dir / f"{_safe_id(cell['cell_id'])}.json"
                            ev_path.write_text(json.dumps(ev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                            final_cell = CellResult(**{**verdict.to_dict(), "evidence_path": str(ev_path)})
                            with lock:
                                ledger.append(final_cell)
                                if idx % 25 == 0 or idx == len(batch):
                                    print(
                                        f"[env{env_id}:w{worker_idx}] {idx}/{len(batch)} last={cell['cell_id']} -> {final_cell.status}"
                                        + (f" ({final_cell.reason_code})" if final_cell.reason_code else ""),
                                        flush=True,
                                    )
                        browser.close()
                finally:
                    stack.close()

            with ThreadPoolExecutor(max_workers=len(active)) as pool:
                futs = [pool.submit(worker, i, env_id, b) for i, env_id, b in active]
                for fut in futs:
                    fut.result()

        summary = ledger.write_summary(out_dir / "summary.json", inventory_meta=plan["coverage_meta"])
        return summary
