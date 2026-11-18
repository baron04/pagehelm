"""The whole product over one local environment, with a scripted model instead of a provider.

The page-facing front, the guard proxy, the Yudao host (read-only-window profile, typed Skill
writes and native submit on), the loader and the embedded Runtime are the product's own. Only the
model is replaced: a local endpoint answers each request from a script, so every turn is
constructed and no model is called. All state goes to a directory this run owns; the spend
ledger it writes is a private temporary file.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import threading

from . import stack as _stack
from .stack import ROOT, login, serve, native  # noqa: F401  (serve and native come from the stack module)
from web_operation_agent_yudao.reads.client import YudaoClient

POLICIES = ROOT / "examples/yudao/deploy/cloud/native-policy"


class ScriptedModel:
    """An OpenAI-compatible endpoint that answers from a script and records every request."""

    def __init__(self, script):
        self.script, self.requests, self.errors = script, [], []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                owner.requests.append(body)
                try:
                    message = owner.script(body)
                except Exception as exc:  # noqa: BLE001 -- a script that cannot answer ends the turn visibly
                    owner.errors.append(f"{type(exc).__name__}: {exc}")
                    message = {"role": "assistant", "content": "脚本没有对应的回复。"}
                finish = "tool_calls" if message.get("tool_calls") else "stop"
                payload = json.dumps({"id": "scripted", "object": "chat.completion", "model": body.get("model", "scripted"),
                                      "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                                      "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.http.server_port}"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def close(self):
        self.http.shutdown()
        self.http.server_close()


def call(name: str, **arguments) -> dict:
    return {"id": f"call-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def reply(*calls: dict) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def observation_text(request: dict) -> str:
    """The latest page the model was shown: the last message that holds numbered controls."""
    for message in reversed(request.get("messages", [])):
        content = message.get("content")
        if isinstance(content, str) and re.search(r"^\s*\*?\[\d+\]<", content, re.M):
            return content
    return ""


def index_of(request: dict, label: str, tag: str = "", *, first: bool = False) -> int:
    """The number of the control the model would name for `label`, from the page text it sees."""
    spaced = r"\s*".join(map(re.escape, label))
    lines = observation_text(request).splitlines()
    for line in (lines if first else reversed(lines)):
        head = re.match(r"^\s*\*?\[(\d+)\]<(\w+)", line)
        if head and re.search(spaced, line) and (not tag or head.group(2) == tag):
            return int(head.group(1))
    raise LookupError(f"no control labelled {label!r} on the page the model was shown")


def index_matching(request: dict, pattern: str, *, first: bool = False) -> int:
    """The number of the first (or last) numbered control whose line matches `pattern`."""
    lines = observation_text(request).splitlines()
    for line in (lines if first else reversed(lines)):
        head = re.match(r"^\s*\*?\[(\d+)\]<", line)
        if head and re.search(pattern, line):
            return int(head.group(1))
    raise LookupError(f"no control matching {pattern!r} on the page the model was shown")


def last_tool_text(request: dict) -> str:
    for message in reversed(request.get("messages", [])):
        if message.get("role") == "tool":
            return str(message.get("content") or "")
    return ""


def receipt_of(request: dict) -> str:
    """The turn receipt a tool result quoted, which a delivered item must cite."""
    for message in reversed(request.get("messages", [])):
        found = re.search(r"本轮(?:工具)?回执[：:]\s*(\S+?)[。，\s]", str(message.get("content") or "") + " ")
        if found:
            return found.group(1)
    return ""


class FullStack:
    def __init__(self, environment: int, state: Path, accounts: Path, script, *, account="admin", front_port=48200,
                 model_name="deepseek-flash"):
        self.environment, self.state, self.accounts, self.account = environment, Path(state), accounts, account
        self.upstream = f"http://localhost:{40000 + environment}"
        self.ports = (front_port, front_port + 1, front_port + 2, front_port + 3)
        self.origin = f"http://localhost:{front_port}"
        self.model = ScriptedModel(script)
        self.model_name = model_name
        self.front = self.host = self.boot = None

    def start(self):
        self.state.mkdir(parents=True, exist_ok=True)
        settings = serve.Settings(self.environment, "phase46-write-contract", self.origin, upstream=self.upstream,
                                  state=self.state, ports=self.ports, listen_host="127.0.0.1")
        self.front = serve.Front(settings)
        threading.Thread(target=self.front.serve_forever, daemon=True).start()
        env = {"AGENT_OPENAI_BASE_URL": self.model.url, "AGENT_OPENAI_API_KEY": "scripted", "AGENT_OPENAI_MODEL": self.model_name}
        policy = POLICIES
        self.host = native.YudaoHost(native.YudaoHostConfig(
            api_base=self.upstream, public_origin=self.origin, public_agent_url=self.origin + "/woa-agent",
            state_dir=self.state, gateway_host="127.0.0.1", gateway_port=self.ports[1],
            bootstrap_host="127.0.0.1", bootstrap_port=self.ports[2], native_guard_port=self.ports[3],
            native_ingress_base=self.origin, ui_source=ROOT / ".q0-cache/yudao/ui", source_release="v2026.09",
            native_read_policy=policy / "fence.json", native_page_policy=policy / "pages.json",
            native_money_policy=policy / "verified-money.json", native_filter_semantics=policy / "filters.json",
            native_page_admission="read-only-window", native_writes=True, native_submit=True,
            native_edit_prefill=True, model=self.model_name, model_env=env, daily_allowance_cny=20.0))
        self.host.server.start()
        self.boot = ThreadingHTTPServer(("127.0.0.1", self.ports[2]), native._handler(self.host))
        threading.Thread(target=self.boot.serve_forever, daemon=True).start()
        threading.Thread(target=self.host._warm_api_docs, daemon=True).start()
        self.front.ready = True
        self.front.api_docs_loaded = True
        self.token = login(self.upstream, self.accounts, self.account)
        self.client = YudaoClient.for_token(self.upstream, self.token["access"], self.token["tenant"])
        return self

    def close(self):
        for server in (self.boot, ):
            if server is not None:
                server.shutdown()
                server.server_close()
        if self.host is not None:
            self.host.close()
        if self.front is not None:
            self.front.shutdown()
            self.front.server_close()
        self.model.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()

    def sign_in_script(self) -> str:
        record = lambda value: json.dumps({"c": 0, "e": 1.7976931348623157e308, "v": json.dumps(value)})
        return ("(() => { try {"
                f"localStorage.setItem('ACCESS_TOKEN', {json.dumps(record(self.token['access']))});"
                f"localStorage.setItem('REFRESH_TOKEN', {json.dumps(record(self.token['refresh']))});"
                f"localStorage.setItem('tenantId', {json.dumps(record(self.token['tenant']))});"
                "} catch (e) {} })()")
