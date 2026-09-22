"""Model-free live check of the Odoo page write guard (woa_runtime 1.5).

Signs in with the fixture's stored browser session, opens the read-only window, and makes
the requests a page action could cause -- a form save, a create, a workflow button, a
delete, a settings write -- through the same JSON-RPC routes the web client uses. Every one
must be refused, every read must still work, the window must refuse cross-site and
non-JSON requests, must close on request and on expiry, and the business-table fingerprint
of the fixture must not change. If the guard failed, the writes below would really change
the synthetic fixture: restore it from the P8.7 snapshot.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from evals.odoo.p87_gate import fingerprint

ROOT = Path(__file__).resolve().parents[2]


class Session:
    def __init__(self, base_url: str, storage_state: Path):
        self.base = base_url.rstrip("/")
        cookies = json.loads(storage_state.read_text())["cookies"]
        sid = next(c["value"] for c in cookies if c["name"] == "session_id")
        self._cookie = f"session_id={sid}"
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(self, path: str, body, *, content_type="application/json", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method="POST", headers={
            "Content-Type": content_type, "Cookie": self._cookie, **(headers or {})})
        try:
            with self._opener.open(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"raw": raw[:200].decode("utf-8", "replace")}

    def call_kw(self, model: str, method: str, args, kwargs=None):
        status, body = self.post(f"/web/dataset/call_kw/{model}/{method}", {
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"model": model, "method": method, "args": args,
                       "kwargs": kwargs or {}}})
        return status, body


def _refused(body) -> bool:
    error = (body or {}).get("error") or {}
    data = error.get("data") or {}
    return (data.get("name") == "odoo.exceptions.UserError"
            and "changes are paused" in str(data.get("message", "")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-env", type=Path, default=ROOT / ".env")
    parser.add_argument("--container", default="woa-odoo-reference-db-1")
    parser.add_argument("--database", default="woa_reference")
    args = parser.parse_args()
    env = dict(line.split("=", 1) for line in args.model_env.read_text().splitlines()
               if "=" in line and not line.lstrip().startswith("#"))
    session = Session(env["WOA_ODOO_BASE_URL"].strip(),
                      Path(env["WOA_ODOO_STORAGE_STATE"].strip()))
    rows: list[dict] = []

    def record(case, passed, **detail):
        rows.append({"case": case, "passed": bool(passed), **detail})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

    before = fingerprint(args.container, args.database)
    _, partners = session.call_kw("res.partner", "search_read", [[["is_company", "=", True]]],
                                  {"fields": ["id", "name", "comment"], "limit": 1})
    partner = partners["result"][0]
    _, orders = session.call_kw("sale.order", "search_read", [[["state", "=", "draft"]]],
                                {"fields": ["id", "name"], "limit": 1})
    draft = orders["result"][0] if orders.get("result") else None
    _, tags = session.call_kw("crm.tag", "search_read", [[]], {"fields": ["id"], "limit": 1})
    tag = tags["result"][0] if tags.get("result") else None
    _, found = session.call_kw("res.users.settings", "search_read", [[]],
                               {"fields": ["id", "is_discuss_sidebar_category_channel_open"],
                                "limit": 1})
    settings = found["result"][0] if found.get("result") else None

    status, state = session.post("/woa/page-guard/state", {})
    record("closed-at-start", status == 200 and state == {"active": False, "expires_at": None},
           state=state)
    status, body = session.post("/woa/page-guard/begin", {"ttl_s": 1},
                                content_type="text/plain")
    record("begin-refuses-non-json", status == 400, status=status)
    status, body = session.post("/woa/page-guard/begin", {"ttl_s": 60},
                                headers={"Sec-Fetch-Site": "cross-site"})
    record("begin-refuses-cross-site", status == 400, status=status)
    status, body = session.post("/woa/page-guard/begin", {"ttl_s": 1000})
    record("begin-refuses-long-window", status == 400, status=status)

    status, opened = session.post("/woa/page-guard/begin", {"ttl_s": 60})
    record("begin-opens", status == 200 and opened.get("active") is True, opened=opened)
    try:
        attempts = [
            ("form-save", "res.partner", "web_save",
             [[partner["id"]], {"comment": (partner.get("comment") or "") + " guard"}],
             {"specification": {"comment": {}}}),
            ("write", "res.partner", "write", [[partner["id"]], {"name": partner["name"]}], {}),
            ("create", "res.partner", "create", [{"name": "WOA guard probe"}], {}),
        ]
        if settings:
            attempts.append((
                "settings-write", "res.users.settings", "set_res_users_settings",
                [[settings["id"]]], {"new_settings": {
                    "is_discuss_sidebar_category_channel_open":
                        not settings["is_discuss_sidebar_category_channel_open"]}}))
        if draft:
            attempts.append(("workflow-button", "sale.order", "action_confirm",
                             [[draft["id"]]], {}))
        if tag:
            attempts.append(("delete", "crm.tag", "unlink", [[tag["id"]]], {}))
        for case, model, method, call_args, kwargs in attempts:
            status, body = session.call_kw(model, method, call_args, kwargs)
            record(f"refuses-{case}", _refused(body), status=status,
                   error=((body or {}).get("error") or {}).get("data", {}).get("name"))
        status, body = session.call_kw("sale.order", "web_search_read", [[]], {
            "specification": {"name": {}, "state": {}}, "limit": 5})
        record("reads-still-work", status == 200 and "result" in body,
               records=len((body.get("result") or {}).get("records", [])))
        status, body = session.post("/woa/page-guard/state", {})
        record("open-while-acting", body.get("active") is True, state=body)
    finally:
        status, closed = session.post("/woa/page-guard/end", {})
    record("end-closes", status == 200 and closed == {"active": False}, closed=closed)
    status, state = session.post("/woa/page-guard/state", {})
    record("closed-after-end", state.get("active") is False, state=state)

    session.post("/woa/page-guard/begin", {"ttl_s": 1})
    time.sleep(2.2)
    status, state = session.post("/woa/page-guard/state", {})
    record("expires-on-its-own", state.get("active") is False, state=state)
    session.post("/woa/page-guard/end", {})

    after = fingerprint(args.container, args.database)
    record("no-business-write", before == after)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "page-guard-check.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1))
    passed = sum(row["passed"] for row in rows)
    print(json.dumps({"cases": len(rows), "passed": passed}), flush=True)
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
