"""Host operation tests for BUG-09, ARC-02, OPS-01, OPS-02, and OPS-05."""
from __future__ import annotations

import http.client
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from web_operation_agent.business.mutation import SQLiteMutationJournal
from web_operation_agent.execution.product import AgentTurnError, AgentTurnStatus
from web_operation_agent.modeling.spend import DailyAllowance
from web_operation_agent.state.identity import PrincipalError
from web_operation_agent_yudao.host import native_server as native
from web_operation_agent_yudao.host.application import YudaoIdentityPort
from web_operation_agent_yudao.host.native_server import YudaoHost, YudaoHostConfig


class _HeldModelServer:
    def __init__(self):
        outer = self
        self.lock = threading.Lock()
        self.connections = 0
        self.in_flight = 0
        self.max_seen_in_flight = 0
        self.received: dict[str, threading.Event] = {}
        self.arrived = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def setup(self):
                super().setup()
                with outer.lock:
                    outer.connections += 1

            def do_POST(self):
                length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(length))
                marker = body["messages"][-1]["content"]
                with outer.lock:
                    outer.in_flight += 1
                    outer.max_seen_in_flight = max(outer.max_seen_in_flight, outer.in_flight)
                    ev = outer.received.setdefault(marker, threading.Event())
                    outer.arrived.set()
                try:
                    ev.wait(5)
                finally:
                    with outer.lock:
                        outer.in_flight -= 1
                reply = json.dumps({
                    "id": "cmp-1",
                    "object": "chat.completion",
                    "choices": [{"message": {"role": "assistant", "content": f"ok:{marker}"}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.block_on_close = False
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()

    def release(self, marker: str):
        with self.lock:
            self.received.setdefault(marker, threading.Event()).set()

    def close(self):
        with self.lock:
            for ev in self.received.values():
                ev.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class _BaseHostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.hosts: list[YudaoHost] = []
        self.addCleanup(lambda: [h.close() for h in self.hosts])
        self.addCleanup(patch.stopall)
        patch("web_operation_agent_yudao.host.guard_proxy.NativeGuardProxy").start()
        patch("web_operation_agent_yudao.host.native_server._api_docs", return_value={"paths": {}, "components": {}}).start()

        def actor(token):
            if token not in ("tok-1", "tok-2"):
                raise PrincipalError("not signed in")
            return {"id": "1" if token == "tok-1" else "2", "deptId": "10"}

        def page_identity(port, token):
            return port._assertion(actor(token), "page_session")

        def request(client, method, path, **kwargs):
            if method == "GET" and path == "system/dict-data/simple-list":
                return 0, {"code": 0, "data": []}
            if method == "GET" and path == "system/auth/get-permission-info":
                return 0, {"code": 0, "data": {"user": actor(kwargs["token"]), "menus": [], "permissions": []}}
            raise AssertionError((method, path))

        patch.object(YudaoIdentityPort, "page_identity", page_identity).start()
        self._request_patcher = patch("web_operation_agent_yudao.reads.client.YudaoClient._request", request)
        self._request_patcher.start()

    def _policies(self, writing_gets=()):
        fence = self.root / "fence.json"
        fence.write_text(json.dumps({
            "schema_version": 2,
            "release": "synthetic-rel",
            "mode": "method-fence",
            "writing_gets": list(writing_gets),
            "scan": {},
        }))
        pages = self.root / "pages.json"
        pages.write_text(json.dumps({
            "schema_version": 1,
            "release": "synthetic-rel",
            "review": "synthetic",
            "handlers": [{"kind": "navigation", "route": "/crm/customer", "evidence": "s", "sha256": "a" * 64, "controls": []}],
        }))
        return fence, pages

    def _config(self, **overrides) -> YudaoHostConfig:
        fence, pages = self._policies(overrides.pop("writing_gets", ()))
        state_dir = overrides.pop("state_dir", self.root / "state")
        return YudaoHostConfig(
            api_base="http://yudao.invalid",
            public_origin="https://yudao.invalid",
            state_dir=state_dir,
            gateway_host="127.0.0.1",
            gateway_port=0,
            source_release="synthetic-rel",
            native_read_policy=fence,
            native_page_policy=pages,
            native_page_admission="reviewed",
            warm_index=False,
            **overrides,
        )

    def _make_host(self, **overrides) -> YudaoHost:
        host = YudaoHost(self._config(**overrides))
        self.hosts.append(host)
        return host


class HostConcurrencyTests(_BaseHostTest):
    def test_bug09_sessions_share_process_guard_and_connection_pool(self):
        from web_operation_agent.modeling.provider import ProviderError

        server = _HeldModelServer()
        self.addCleanup(server.close)
        env = {"no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost"}
        with patch.dict("os.environ", env):
            host = self._make_host(
                max_concurrent_model_requests=1,
                daily_allowance_cny=1.0,
                model_env={
                    "AGENT_OPENAI_BASE_URL": server.url,
                    "AGENT_OPENAI_API_KEY": "k",
                    "AGENT_OPENAI_MODEL": "qwen3.8-flash",
                },
            )
            host.bootstrap("tok-1", "1", "tab-a")
            host.bootstrap("tok-2", "1", "tab-b")
            chat_a = host._sessions[("1", "1", "tab-a")].session.runner.executor.chat
            chat_b = host._sessions[("1", "2", "tab-b")].session.runner.executor.chat
            self.assertIs(chat_a.runtime_guard, host.runtime_guard)
            self.assertIs(chat_b.runtime_guard, host.runtime_guard)
            self.assertIs(chat_a._connection_cache(), chat_b._connection_cache())

            # Sequential calls across two sessions reuse the single pooled connection.
            server.release("seq1")
            r1 = chat_a.complete_cancellable([{"role": "user", "content": "seq1"}], None, "r1", cancelled=lambda: False, timeout_s=5)
            server.release("seq2")
            r2 = chat_b.complete_cancellable([{"role": "user", "content": "seq2"}], None, "r2", cancelled=lambda: False, timeout_s=5)
            self.assertEqual(r1["content"], "ok:seq1")
            self.assertEqual(r2["content"], "ok:seq2")
            self.assertEqual(server.connections, 1)

            # Simultaneous calls across two sessions enforce process-wide limit=1.
            outcomes = {}

            def call_first():
                outcomes["a"] = chat_a.complete_cancellable(
                    [{"role": "user", "content": "hold"}], None, "ra", cancelled=lambda: False, timeout_s=5
                )

            def call_second():
                try:
                    outcomes["b"] = chat_b.complete_cancellable(
                        [{"role": "user", "content": "blocked"}], None, "rb", cancelled=lambda: False, timeout_s=0.1
                    )
                except ProviderError as exc:
                    outcomes["b"] = exc

            t1 = threading.Thread(target=call_first, daemon=True)
            t1.start()
            deadline = time.monotonic() + 5
            while "hold" not in server.received and time.monotonic() < deadline:
                time.sleep(0.01)
            t2 = threading.Thread(target=call_second, daemon=True)
            t2.start()
            t2.join(5)
            server.release("hold")
            t1.join(5)
            self.assertEqual(server.max_seen_in_flight, 1)
            self.assertEqual(outcomes["a"]["content"], "ok:hold")
            self.assertIsInstance(outcomes["b"], ProviderError)
            self.assertEqual(outcomes["b"].code, "provider_capacity")
            self.assertFalse(outcomes["b"].dispatched)
            # Per-actor allowance remains isolated.
            self.assertNotEqual(chat_a.meter.allowance_key, chat_b.meter.allowance_key)


class HostDrainTests(_BaseHostTest):
    def test_arc02_drain_refuses_new_work_and_waits_or_marks_indeterminate(self):
        with patch("web_operation_agent_yudao.host.native_server.ChatModel") as mock_chat:
            mock_chat.return_value.model = "qwen3.8-flash"
            host = self._make_host(native_writes=True, shutdown_drain_timeout_s=1.0)
            host.bootstrap("tok-1", "1", "tab-a")
            executor = host._sessions[("1", "1", "tab-a")].session.runner.executor

            httpd = ThreadingHTTPServer(("127.0.0.1", 0), native._handler(host))
            thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close(), thread.join(2)))

            # Before drain, /readyz returns 200 ready=True.
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
            conn.request("GET", "/readyz")
            resp = conn.getresponse()
            ready_before = json.loads(resp.read())
            conn.close()
            self.assertEqual(resp.status, 200)
            self.assertTrue(ready_before["accepting_sessions"])

            # Case 1: in-flight write finishes within drain timeout.
            write_entered = threading.Event()
            release_write = threading.Event()

            def in_flight():
                with host.track_inflight_write():
                    write_entered.set()
                    release_write.wait(5)

            worker = threading.Thread(target=in_flight, daemon=True)
            worker.start()
            self.assertTrue(write_entered.wait(2))
            drain_result = {}
            drainer = threading.Thread(target=lambda: drain_result.__setitem__("res", host.drain(timeout_s=2.0)))
            drainer.start()

            deadline = time.monotonic() + 2
            while not host.draining and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(host.draining)

            # While draining: /readyz -> 503, /bootstrap -> 503 server_draining, new turn refused.
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
            conn.request("GET", "/readyz")
            r_drain = conn.getresponse()
            body_drain = json.loads(r_drain.read())
            conn.close()
            self.assertEqual(r_drain.status, 503)
            self.assertFalse(body_drain["accepting_sessions"])
            self.assertTrue(body_drain["draining"])

            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
            conn.request(
                "POST",
                "/bootstrap",
                body=json.dumps({"runtime_instance_id": "tab-new"}),
                headers={"Authorization": "Bearer tok-1", "tenant-id": "1", "Content-Type": "application/json"},
            )
            r_boot = conn.getresponse()
            boot_err = json.loads(r_boot.read())
            conn.close()
            self.assertEqual(r_boot.status, 503)
            self.assertEqual(boot_err["error"], "server_draining")

            with self.assertRaises(AgentTurnError):
                executor.execute({"message_id": "m1", "text": "hello", "page_url": "https://yudao.invalid/crm"})

            release_write.set()
            worker.join(2)
            drainer.join(2)
            self.assertEqual(drain_result["res"]["drained"], True)
            self.assertEqual(drain_result["res"]["remaining_writes"], 0)

            # Case 2: write still dispatching at drain expiry -> marked indeterminate, never retried.
            digest = "c" * 64
            now_ms = int(time.time() * 1000)
            host._mutation_journal.register(digest, "d" * 64, expires_at=now_ms + 60_000, now_ms=now_ms)
            host._mutation_journal.claim(digest, "d" * 64, now_ms=now_ms + 1)
            stuck_entered = threading.Event()
            stuck_release = threading.Event()
            self.addCleanup(stuck_release.set)

            def stuck_write():
                with host.track_inflight_write():
                    stuck_entered.set()
                    stuck_release.wait(5)

            stuck_thread = threading.Thread(target=stuck_write, daemon=True)
            stuck_thread.start()
            self.assertTrue(stuck_entered.wait(2))
            expired_res = host.drain(timeout_s=0.05)
            self.assertFalse(expired_res["drained"])
            self.assertEqual(expired_res["marked_indeterminate"], 1)
            self.assertEqual(host._mutation_journal.get(digest).state, "indeterminate")
            audit_rows = [json.loads(line) for line in (host.config.state_dir / "audit.jsonl").read_text().splitlines()]
            self.assertTrue(any(
                r.get("event") == "business_mutation_indeterminate"
                and r.get("proposal_digest") == digest
                and r.get("dispatches") == 0
                for r in audit_rows
            ))
            stuck_release.set()
            stuck_thread.join(2)


class HostCleanupAndCapTests(_BaseHostTest):
    def test_ops01_cleanup_prunes_stores_rotates_audit_and_preserves_unresolved_and_fingerprints(self):
        with patch("web_operation_agent_yudao.host.native_server.ChatModel"):
            host = self._make_host(
                native_writes=True,
                session_retention_days=7,
                audit_max_bytes=1024,
                audit_retention_days=7,
                audit_max_segments=1,
            )
        now_ms = int(time.time() * 1000)
        old_ms = now_ms - 10 * 86_400_000

        # Seed session store with one expired inactive session.
        with host.store.connect() as db:
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", ("old-sid", "scope", "owner", 1, old_ms))
            db.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?)", ("old-sid", "m1", "d", "done", old_ms))
            db.execute("INSERT INTO outbox VALUES (?, ?, ?, ?)", ("old-sid", 1, "{}", old_ms))
            db.commit()

        # Seed conversation state DB with one old row and one fresh row.
        conv_db = host.config.state_dir / "conversation-state.sqlite3"
        with closing(sqlite3.connect(conv_db)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS conversation_contexts "
                "(conversation_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated INTEGER NOT NULL)"
            )
            db.execute("INSERT INTO conversation_contexts VALUES (?, ?, ?)", ("old-conv", "{}", old_ms))
            db.execute("INSERT INTO conversation_contexts VALUES (?, ?, ?)", ("new-conv", "{}", now_ms))

        # Seed mutation journal with resolved + unresolved rows.
        unresolved_digest = "e" * 64
        resolved_digest = "f" * 64
        host._mutation_journal.register(unresolved_digest, "a" * 64, expires_at=old_ms + 1000, now_ms=old_ms - 1000)
        host._mutation_journal.claim(unresolved_digest, "a" * 64, now_ms=old_ms)
        host._mutation_journal.finish(unresolved_digest, "indeterminate", now_ms=old_ms + 1)

        host._mutation_journal.register(resolved_digest, "a" * 64, expires_at=old_ms + 1000, now_ms=old_ms - 1000)
        host._mutation_journal.claim(resolved_digest, "a" * 64, now_ms=old_ms)
        host._mutation_journal.finish(resolved_digest, "completed", now_ms=old_ms + 1)

        # Seed oversized audit.jsonl containing policy fingerprint, unresolved row, and noise.
        audit_path = host.config.state_dir / "audit.jsonl"
        fp_line = json.dumps({"event": "policy_bound", "catalog_sha256": "deadbeef" * 8})
        unresolved_line = json.dumps({"event": "business_mutation_execution_started", "proposal_digest": unresolved_digest})
        noise = json.dumps({"event": "ordinary_read", "detail": "x" * 200})
        audit_path.write_text("\n".join([fp_line, unresolved_line] + [noise] * 20) + "\n", encoding="utf-8")

        # Create an old rotated segment that will be pruned during cleanup, checking preservation.
        old_seg = host.config.state_dir / "audit.1000-0.jsonl"
        old_seg_fp = json.dumps({"event": "run_started", "policy_fingerprint": "fp-keep-forever"})
        old_seg_unres = json.dumps({"event": "business_mutation_indeterminate", "proposal_digest": unresolved_digest})
        old_seg.write_text("\n".join([old_seg_fp, old_seg_unres, noise]) + "\n", encoding="utf-8")
        os.utime(old_seg, (time.time() - 20 * 86400,) * 2)

        report = host.cleanup(force=True, now_ms=now_ms)
        self.assertGreaterEqual(report["sessions_removed"], 1)
        self.assertEqual(report["conversations_removed"], 1)
        self.assertEqual(report["audit_rotated"], 1)
        self.assertFalse(audit_path.exists())
        self.assertIsNotNone(host._mutation_journal.get(unresolved_digest))
        with closing(sqlite3.connect(conv_db)) as db:
            remaining = [r[0] for r in db.execute("SELECT conversation_id FROM conversation_contexts").fetchall()]
        self.assertEqual(remaining, ["new-conv"])

        retained_text = (host.config.state_dir / "audit.retained.jsonl").read_text(encoding="utf-8")
        self.assertIn("fp-keep-forever", retained_text)
        self.assertIn(unresolved_digest, retained_text)
        self.assertNotIn("ordinary_read", retained_text)

    def test_ops02_host_global_spend_cap_refuses_with_clear_chinese_message(self):
        with patch("web_operation_agent_yudao.host.native_server.ChatModel") as mock_chat:
            mock_chat.return_value.model = "qwen3.8-flash"
            # Disabled by default for measurement configs.
            default_host = self._make_host()
            self.assertIsNone(default_host.spend_cap)

            host = self._make_host(
                state_dir=self.root / "capped",
                daily_total_cap_cny=0.05,
                monthly_total_cap_cny=0.10,
            )
            host.bootstrap("tok-1", "1", "tab-1")
            session = host._sessions[("1", "1", "tab-1")].session
            executor = session.runner.executor
            host.spend_cap.record_spend(0.05)

            res = executor.execute({"message_id": "m-cap", "text": "查一下客户", "page_url": "https://yudao.invalid/crm"})
            self.assertEqual(res.terminal_reason, "daily_cap_exceeded")
            self.assertIn("今日平台参考费用已达上限", res.user_answer)
            mock_chat.return_value.complete.assert_not_called()

            # Monthly cap exceeded on a fresh daily cap refuses with monthly message.
            host_m = self._make_host(
                state_dir=self.root / "capped-month",
                daily_total_cap_cny=10.0,
                monthly_total_cap_cny=0.10,
            )
            host_m.bootstrap("tok-1", "1", "tab-m")
            exec_m = host_m._sessions[("1", "1", "tab-m")].session.runner.executor
            host_m.spend_cap.record_spend(0.10)
            res_m = exec_m.execute({"message_id": "m-month", "text": "查一下客户", "page_url": "https://yudao.invalid/crm"})
            self.assertEqual(res_m.terminal_reason, "monthly_cap_exceeded")
            self.assertIn("本月平台参考费用已达上限", res_m.user_answer)

    def test_ops05_warm_index_routes_all_reads_through_fenced_gets_and_blocks_writing_gets(self):
        fenced_paths = (
            "/admin-api/report/wallet/init",
            "/admin-api/project/{id}/init",
        )
        with patch("web_operation_agent_yudao.host.native_server.ChatModel") as mock_chat:
            mock_chat.return_value.model = "qwen3.8-flash"
            host = self._make_host(writing_gets=fenced_paths)
            host.bootstrap("tok-1", "1", "tab-warm")
            session = host._sessions[("1", "1", "tab-warm")].session
            client = session.runner.executor.knowledge.client
            transport_calls: list[tuple[str, str]] = []

            def spy_urlopen(request, timeout=None):
                transport_calls.append((request.get_method(), request.full_url))
                payload = json.dumps({"code": 0, "data": {"user": {"id": 1}, "menus": [], "permissions": []}}).encode()
                return io.BytesIO(payload)

            # Build fake app/knowledge whose warm-up tries to invoke fenced writing GETs and a POST.
            attempts = []

            def malicious_outline():
                for path in ("report/wallet/init", "project/42/init"):
                    try:
                        client.get(path)
                    except Exception as exc:
                        attempts.append((path, exc))
                try:
                    client._request_transport("POST", "crm/customer/create")
                except Exception as exc:
                    attempts.append(("POST", exc))

            fake_app = SimpleNamespace(
                client=client,
                goal_data=SimpleNamespace(_writing_gets=host._fenced),
                goal_port=SimpleNamespace(outline_index=malicious_outline),
            )
            fake_knowledge = SimpleNamespace(
                client=client,
                writing_gets=host._fenced,
                _load=lambda: client.get("system/auth/get-permission-info"),
            )
            self._request_patcher.stop()
            with patch("urllib.request.urlopen", side_effect=spy_urlopen):
                YudaoHost._warm_index(fake_app, fake_knowledge)

            self.assertEqual(len(attempts), 3)
            for tag, exc in attempts:
                self.assertIn(getattr(exc, "reason", ""), {"fenced_writing_get", "fenced_non_get"}, tag)
            self.assertEqual(len(transport_calls), 1)
            self.assertEqual(transport_calls[0][0], "GET")
            self.assertIn("system/auth/get-permission-info", transport_calls[0][1])


if __name__ == "__main__":
    unittest.main()
