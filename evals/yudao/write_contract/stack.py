"""A same-origin front, the native guard proxy and a signed-in browser over one local environment.

This is the page-facing half of the product stack with no assistant behind it: the front
serves the application and the page-fact modules, `/admin-api` and the page-guard endpoint go
through the guard proxy exactly as in the deployment, and the browser is signed in as one
named synthetic account. It calls no model.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "examples/yudao/src"), str(ROOT / "examples/yudao/deploy/local")]

import serve  # noqa: E402  (the local front)
from web_operation_agent_yudao.host import native_server as native  # noqa: E402
from web_operation_agent_yudao.reads.client import YudaoClient  # noqa: E402
from web_operation_agent_yudao.host.guard_identity import GuardIdentity  # noqa: E402
from web_operation_agent_yudao.host.guard_proxy import GUARD_PATH, NativeGuardProxy  # noqa: E402
from web_operation_agent_yudao.writes.native_submit import YudaoSubmitPort  # noqa: E402
from web_operation_agent_yudao.host.page_guard import NativePageGuard  # noqa: E402

POLICY = ROOT / "examples/yudao/deploy/cloud/native-policy/fence.json"


def login(origin: str, accounts: Path, name: str) -> dict:
    """The application's own sign-in as a named synthetic account; nothing is printed or kept."""
    account = json.loads(Path(accounts).read_text(encoding="utf-8"))[name]
    tenant = str(account.get("tenant", "1"))
    request = urllib.request.Request(
        origin + "/admin-api/system/auth/login", method="POST", headers={"tenant-id": tenant, "Content-Type": "application/json"},
        data=json.dumps({k: account[k] for k in ("username", "password")}).encode())
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.load(response)
    if body.get("code") != 0:
        raise RuntimeError("sign-in refused")
    data = body["data"]
    return {"access": data["accessToken"], "refresh": data["refreshToken"], "tenant": tenant}


class Stack:
    def __init__(self, environment: int, state: Path, accounts: Path, account: str = "admin", *, front_port: int = 48200):
        self.environment, self.state, self.accounts, self.account = environment, Path(state), accounts, account
        # The environment's published entry is its own same-origin front, which only answers a
        # request that names `localhost`; this stack stands in front of it and leaves it as found.
        self.upstream = f"http://localhost:{40000 + environment}"
        self.ports = (front_port, front_port + 1, front_port + 2, front_port + 3)
        self.origin = f"http://localhost:{front_port}"
        self.front = self.proxy = self.guard = self.port = None
        self.token = None

    def start(self):
        self.state.mkdir(parents=True, exist_ok=True)
        settings = serve.Settings(self.environment, "phase46-write-contract", self.origin, upstream=self.upstream,
                                  state=self.state, ports=self.ports, listen_host="127.0.0.1")
        self.front = serve.Front(settings)
        self.front.ready = True
        threading.Thread(target=self.front.serve_forever, daemon=True).start()
        fence = json.loads(POLICY.read_text(encoding="utf-8"))
        self.guard = NativePageGuard(frozenset(), writing_gets=frozenset(fence["writing_gets"]),
                                     journal=self.state / "guard.sqlite3")
        self.proxy = NativeGuardProxy(upstream=self.upstream, origin=self.origin, guard=self.guard,
                                      resolve_actor=GuardIdentity(self.upstream), host="127.0.0.1",
                                      port=self.ports[3], application_window=True, ingress_base=self.origin).start()
        self.proxy.verify_ingress()
        self.token = login(self.upstream, self.accounts, self.account)
        self.client = YudaoClient.for_token(self.upstream, self.token["access"], self.token["tenant"])
        self._docs = None
        self.port = YudaoSubmitPort(self.client, self.proxy, self.api_docs)
        return self

    def api_docs(self):
        if self._docs is None:
            self._docs = native._api_docs(self.upstream, wait_s=0, timeout_s=300)
        return self._docs

    def actor(self) -> str:
        return self.proxy.resolve_actor(self.token["access"], self.token["tenant"])

    def close(self):
        if self.proxy is not None:
            self.proxy.close()
        if self.front is not None:
            self.front.shutdown()
            self.front.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()

    # -- the browser -----------------------------------------------------------------------
    def sign_in_script(self) -> str:
        """Runs before the application: the person's tokens where yudao keeps them."""
        # web-storage-cache: an item without an expiry carries the largest number as its time.
        record = lambda value: json.dumps({"c": 0, "e": 1.7976931348623157e308, "v": json.dumps(value)})
        return ("(() => { try {"
                f"localStorage.setItem('ACCESS_TOKEN', {json.dumps(record(self.token['access']))});"
                f"localStorage.setItem('REFRESH_TOKEN', {json.dumps(record(self.token['refresh']))});"
                f"localStorage.setItem('tenantId', {json.dumps(record(self.token['tenant']))});"
                "} catch (e) {} })()")


__all__ = ["Stack", "GUARD_PATH", "login"]
