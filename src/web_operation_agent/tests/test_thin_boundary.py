"""The browser side must not be able to hold what it is not allowed to hold.

The browser keeps observation, tools, lifecycle and receipts, and nothing else: no source,
plan, contract, model key, ControlPlan or evaluator. The tests enforce this as a property of
the import graph and child-process environment.
"""
import ast
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PRODUCT = Path(__file__).resolve().parents[1]
FORBIDDEN_MODULES = {"sourcemap", "sourcetree", "contract", "session", "controlplane", "model",
                     "attest", "cli", "ledger"}


def _local_imports(path: Path) -> set[str]:
    """Product-internal modules this file imports, transitively."""
    seen, queue, out = set(), [path], set()
    while queue:
        p = queue.pop()
        if p in seen or not p.exists():
            continue
        seen.add(p)
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.level and node.module:
                names = [node.module.split(".")[0]]
            elif isinstance(node, ast.ImportFrom) and node.module and \
                    node.module.startswith("web_operation_agent."):
                names = [node.module.split(".")[1]]
            elif isinstance(node, ast.Import):
                names = [a.name.split(".")[1] for a in node.names
                         if a.name.startswith("web_operation_agent.")]
            for n in names:
                out.add(n)
                queue.append(PRODUCT / f"{n}.py")
    return out


class ImportClosure(unittest.TestCase):
    def test_the_browser_worker_cannot_reach_the_server_side(self):
        reachable = _local_imports(PRODUCT / "browser_worker.py")
        leaked = reachable & FORBIDDEN_MODULES
        self.assertEqual(leaked, set(),
                         f"the thin browser can import {sorted(leaked)}; it must only see schemas")
        self.assertEqual(reachable, {"privacy", "schemas"},
                         f"unexpected product imports in the browser worker: {sorted(reachable)}")

    def test_the_product_never_reaches_into_the_experiment(self):
        """No executable reference to lab/, in an import or in a path the code builds.

        The check walks the AST so only executable string constants and import statements
        count as dependencies; comments and docstrings cannot create a runtime dependency.
        """
        for py in PRODUCT.glob("*.py"):
            tree = ast.parse(py.read_text())
            docstrings = {id(n) for n in ast.walk(tree)
                          if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                            ast.AsyncFunctionDef))
                          and n.body and isinstance(n.body[0], ast.Expr)
                          and isinstance(n.body[0].value, ast.Constant)
                          and isinstance(n.body[0].value.value, str)
                          for n in [n.body[0].value]}
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and id(node) not in docstrings:
                    self.assertNotIn("lab/", node.value,
                                     f"{py.name} builds a path into the experiment harness: "
                                     f"{node.value!r}")
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.name for a in getattr(node, "names", [])]
                    names.append(getattr(node, "module", "") or "")
                    for n in names:
                        self.assertFalse(n.startswith(("lab", "evaluator", "task_eval")),
                                         f"{py.name} imports the experiment harness: {n}")

    def test_no_task_identifier_is_hardcoded_in_the_product(self):
        """Reusable product code must not know fixture task identifiers."""
        import re
        pattern = re.compile(r"\bC(0[1-9]|1[0-2])\b")
        for py in list(PRODUCT.glob("*.py")):
            hits = pattern.findall(py.read_text())
            self.assertFalse(hits, f"{py.name} hardcodes a task id: {hits}")


class ProcessBoundary(unittest.TestCase):
    def test_the_model_credential_is_stripped_from_the_browser_environment(self):
        from web_operation_agent.browser import ThinBrowserClient

        client = ThinBrowserClient(
            "http://127.0.0.1:1",
            env={
                "PATH": "/bin",
                "PYTHONPATH": "/tmp/source-checkout",
                "PYTHONHOME": "/tmp/python-home",
                "AGENT_OPENAI_API_KEY": "sk-agent",
                "OPENAI_API_KEY": "sk-openai",
                "ANTHROPIC_API_KEY": "sk-anthropic",
                "TWENTY_ACCESS_TOKEN": "application-token",
                "DATABASE_PASSWORD": "database-password",
                "SERVICE_SECRET": "service-secret",
                "APPLICATION_SETTING": "kept",
            },
        )
        child = client._child_env()
        self.assertEqual(child["APPLICATION_SETTING"], "kept")
        for key in ("PYTHONPATH", "PYTHONHOME", "AGENT_OPENAI_API_KEY",
                    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TWENTY_ACCESS_TOKEN",
                    "DATABASE_PASSWORD", "SERVICE_SECRET"):
            self.assertNotIn(key, child)

    def test_the_launch_line_runs_the_worker_module_and_nothing_else(self):
        from web_operation_agent.browser import ThinBrowserClient, describe_launch
        client = ThinBrowserClient("http://x")
        self.assertEqual(
            client._argv(),
            [sys.executable, "-u", "-m", "web_operation_agent.browser_worker"],
        )
        line = describe_launch(client)
        self.assertIn("web_operation_agent.browser_worker", line)
        self.assertNotIn("cli", line)
        self.assertNotIn(".q0-cache", line)

    def test_start_spawns_the_installed_worker_without_checkout_import_hooks(self):
        from web_operation_agent.browser import ThinBrowserClient

        process = SimpleNamespace(
            stdin=io.StringIO(),
            stdout=io.StringIO('{"kind":"started","ok":true,"url":"http://app/"}\n'),
            stderr=io.StringIO(),
            poll=lambda: None,
        )
        client = ThinBrowserClient(
            "http://app", env={"PATH": "/bin", "PYTHONPATH": "/tmp/checkout/src"}
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as state, \
             patch("web_operation_agent.browser.subprocess.Popen", return_value=process) as popen:
            state.write('{"cookies": [], "origins": []}')
            state.flush()
            reply = client.start(entry_path="/companies", storage_state_path=state.name)

        self.assertTrue(reply["ok"])
        args, kwargs = popen.call_args
        self.assertEqual(
            args[0], [sys.executable, "-u", "-m", "web_operation_agent.browser_worker"]
        )
        self.assertNotIn("PYTHONPATH", kwargs["env"])
        request = json.loads(process.stdin.getvalue())
        self.assertEqual(request["storage_state_path"], state.name)
        self.assertNotIn(state.name, " ".join(args[0]))
        self.assertNotIn(state.name, json.dumps(kwargs["env"]))
        self.assertFalse(hasattr(client, "storage_state_path"))
        self.assertNotIn("username", request)
        self.assertNotIn("password_env", request)

    def test_missing_storage_state_stops_before_spawning_the_worker(self):
        from web_operation_agent.browser import ThinBrowserClient

        client = ThinBrowserClient("http://app")
        with patch("web_operation_agent.browser.subprocess.Popen") as popen, \
             self.assertRaisesRegex(FileNotFoundError, "storage-state file does not exist"):
            client.start(storage_state_path="/definitely/missing/browser-state.json")
        popen.assert_not_called()

    def test_invalid_storage_state_stops_before_spawning_the_worker(self):
        from web_operation_agent.browser import ThinBrowserClient

        client = ThinBrowserClient("http://app")
        with tempfile.NamedTemporaryFile(mode="w", suffix="-private.json") as state:
            state.write("not json")
            state.flush()
            with patch("web_operation_agent.browser.subprocess.Popen") as popen, \
                 self.assertRaises(ValueError) as raised:
                client.start(storage_state_path=state.name)
        popen.assert_not_called()
        self.assertIn("not valid UTF-8 JSON", str(raised.exception))
        self.assertNotIn(state.name, str(raised.exception))

    def test_a_failed_start_handshake_reaps_the_child(self):
        from web_operation_agent.browser import ThinBrowserClient, TransportError

        process = MagicMock(name="worker-process")
        process.poll.return_value = None
        process.stdin = io.StringIO()
        process.stdout = io.StringIO('{"kind":"error","error":"start failed"}\n')
        process.stderr = io.StringIO()
        process.wait.side_effect = [subprocess.TimeoutExpired("worker", 5), 0]
        client = ThinBrowserClient("http://app", env={"PATH": "/bin"})
        with patch("web_operation_agent.browser.subprocess.Popen", return_value=process), \
             self.assertRaisesRegex(TransportError, "start failed"):
            client.start()

        process.terminate.assert_called_once_with()
        self.assertIsNone(client.proc)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_legacy_form_credentials_are_rejected_before_spawn(self):
        from web_operation_agent.browser import ThinBrowserClient

        client = ThinBrowserClient("http://app")
        with patch("web_operation_agent.browser.subprocess.Popen") as popen, \
             self.assertRaisesRegex(TypeError, "unexpected keyword argument 'username'"):
            client.start(username="legacy-user")
        popen.assert_not_called()

    def test_worker_contains_no_application_specific_login_automation(self):
        source = (PRODUCT / "browser_worker.py").read_text()
        for marker in ("OFBIZ_PW", "USERNAME", "PASSWORD", "_ensure_session"):
            self.assertNotIn(marker, source)


class AuthenticationBoundary(unittest.TestCase):
    def _playwright(self):
        page = MagicMock(name="page")
        page.url = "http://app/companies"
        context = MagicMock(name="context")
        context.new_page.return_value = page
        browser = MagicMock(name="browser")
        browser.new_context.return_value = context
        runtime = SimpleNamespace(chromium=MagicMock(name="chromium"))
        runtime.chromium.launch.return_value = browser
        manager = MagicMock(name="playwright-manager")
        manager.start.return_value = runtime
        module = SimpleNamespace(sync_playwright=MagicMock(return_value=manager))
        return module, browser, page

    def test_storage_state_is_an_explicit_browser_context_option(self):
        from web_operation_agent.browser_worker import ThinBrowser

        module, browser, _ = self._playwright()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as state:
            state.write('{"cookies": [], "origins": []}')
            state.flush()
            with patch.dict(sys.modules, {"playwright.sync_api": module}):
                result = ThinBrowser("http://app").start(
                    "/companies", storage_state_path=state.name
                )

        browser.new_context.assert_called_once_with(
            viewport={"width": 1280, "height": 900},
            storage_state=state.name,
        )
        self.assertEqual(result, {"ok": True, "url": "http://app/companies"})
        self.assertNotIn("logged_in", result)

    def test_default_context_has_no_authentication_state(self):
        from web_operation_agent.browser_worker import ThinBrowser

        module, browser, _ = self._playwright()
        with patch.dict(sys.modules, {"playwright.sync_api": module}):
            ThinBrowser("http://app").start("/")

        browser.new_context.assert_called_once_with(viewport={"width": 1280, "height": 900})

    def test_storage_state_must_be_a_path_not_inline_credentials(self):
        from web_operation_agent.browser_worker import ThinBrowser

        with self.assertRaisesRegex(TypeError, "string path"):
            ThinBrowser("http://app").start(storage_state_path={"cookies": []})

    def test_invalid_storage_state_fails_without_echoing_its_path(self):
        from web_operation_agent.browser_worker import ThinBrowser

        with tempfile.NamedTemporaryFile(mode="w", suffix="-private.json") as state:
            state.write("not json")
            state.flush()
            with self.assertRaises(ValueError) as raised:
                ThinBrowser("http://app").start(storage_state_path=state.name)
        self.assertIn("not valid UTF-8 JSON", str(raised.exception))
        self.assertNotIn(state.name, str(raised.exception))

    def test_playwright_storage_state_error_is_redacted(self):
        from web_operation_agent.browser_worker import ThinBrowser

        module, browser, _ = self._playwright()
        with tempfile.NamedTemporaryFile(mode="w", suffix="-private.json") as state:
            state.write('{"cookies": [], "origins": []}')
            state.flush()
            browser.new_context.side_effect = RuntimeError(f"could not read {state.name}")
            with patch.dict(sys.modules, {"playwright.sync_api": module}), \
                 self.assertRaises(RuntimeError) as raised:
                ThinBrowser("http://app").start(storage_state_path=state.name)
        self.assertIn("browser could not load the storage-state file", str(raised.exception))
        self.assertNotIn(state.name, str(raised.exception))

    def test_worker_error_does_not_echo_a_private_storage_state_path(self):
        from web_operation_agent.browser_worker import main

        with tempfile.NamedTemporaryFile(mode="w", suffix="-private.json") as state:
            state.write("not json")
            state.flush()
            stdout = io.StringIO()
            main(
                io.StringIO(json.dumps({
                    "kind": "start",
                    "base_url": "http://app",
                    "storage_state_path": state.name,
                }) + "\n"),
                stdout,
            )
        wire = stdout.getvalue()
        self.assertIn("storage-state file is not valid UTF-8 JSON", wire)
        self.assertNotIn(state.name, wire)


if __name__ == "__main__":
    unittest.main()
