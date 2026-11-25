"""Deployment failure paths against disposable files and fake commands, never a host."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

REPO = Path(__file__).resolve().parents[5]
CLOUD = REPO / "examples/yudao/deploy/cloud"


def load(name):
    spec = importlib.util.spec_from_file_location(f"deploy_{name}", CLOUD / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


command = load("command")
manifest = load("manifest")


class CloudReadonlyTests(unittest.TestCase):
    def test_shipped_agent_command_disables_writes_edit_prefill_and_runtime_source(self):
        compose = (CLOUD.parent / "compose.cloud.yaml").read_text()
        agent = compose.split("\n  agent:", 1)[1]
        args = json.loads(re.search(r"command:\s*(\[[\s\S]*?\])", agent).group(1))
        self.assertNotIn("--native-writes", args)
        self.assertNotIn("--runtime-source", args)
        self.assertIn("--no-native-edit-prefill", args)
        self.assertIn("--source-root", args)
        self.assertEqual(args[args.index("--native-money-policy") + 1],
                         "/opt/native-policy/verified-money.json")


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="deploy with spaces ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(command, "ROOT", self.root).start()
        patch.object(command, "CACHE", self.root / ".q0-cache/yudao").start()
        (self.root / ".q0-cache").mkdir()

    def config(self, text=None):
        (self.root / "deploy.local.toml").write_text(text or
            'ssh_target="fixture-host"\npublic_origin="https://demo.example"\n')
        (self.root / "frontend").mkdir(exist_ok=True)
        (self.root / "frontend/package-lock.json").write_text("locked-dependencies")

    def test_invalid_or_missing_config_does_not_run_any_command(self):
        for value in (None, 'ssh_target="-oProxyCommand=bad"\npublic_origin="https://demo.example"',
                      'ssh_target="host"\npublic_origin="http://demo.example"',
                      'ssh_target="host"\npublic_origin="https://user:password@demo.example"',
                      'ssh_target="host"\npublic_origin="https://demo.example"\ncache="old"'):
            if value is not None:
                self.config(value)
            with patch.object(command, "run") as run, redirect_stderr(io.StringIO()):
                self.assertEqual(1, command.main([]))
                run.assert_not_called()

    def test_check_never_builds_or_deploys(self):
        self.config()
        with patch.object(command, "require_tools"), patch.object(command, "check_cache"), \
                patch.object(command, "remote_check") as remote, patch.object(command, "run") as run, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(0, command.main(["check"]))
            remote.assert_called_once_with("fixture-host", "https://demo.example")
            run.assert_not_called()

    def test_no_arguments_builds_and_uses_canonical_cache_ignoring_environment(self):
        self.config()
        with patch.object(command, "require_tools"), patch.object(command, "check_cache"), \
                patch.object(command, "remote_check"), patch.object(command, "run") as run, \
                patch.dict(os.environ, {"YUDAO_CACHE": "/obsolete/cache", "COMPOSE_PROFILES": "other"}), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(0, command.main([]))
            calls = run.call_args_list
            self.assertEqual("--skip-build", calls[1].args[0][-1])
            self.assertEqual(["sh", str(CLOUD / "deploy.sh"), "fixture-host"], calls[2].args[0])
            self.assertEqual(str(self.root / ".q0-cache/yudao"), calls[2].kwargs["env"]["YUDAO_CACHE"])
            self.assertNotIn("COMPOSE_PROFILES", calls[2].kwargs["env"])
            self.assertNotIn("--restore", calls[2].args[0])

    def test_build_failure_prevents_upload(self):
        self.config()
        with patch.object(command, "require_tools"), patch.object(command, "check_cache"), \
                patch.object(command, "remote_check"), \
                patch.object(command, "run", side_effect=subprocess.CalledProcessError(1, "build")) as run, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(1, command.main([]))
            self.assertEqual(1, run.call_count)

    def test_dependency_changes_trigger_locked_install_and_unchanged_dependencies_reuse_it(self):
        self.config()
        installed = self.root / "frontend/node_modules/.package-lock.json"
        installed.parent.mkdir()
        installed.write_text("installed-dependencies")
        with patch.object(command, "require_tools"), patch.object(command, "check_cache"), \
                patch.object(command, "remote_check"), patch.object(command, "run") as run, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(0, command.main([]))
            self.assertIn("--install", run.call_args_list[0].args[0])
            run.reset_mock()
            self.assertEqual(0, command.main([]))
            self.assertNotIn("--install", run.call_args_list[0].args[0])
            (self.root / "frontend/package-lock.json").write_text("new-dependencies")
            run.reset_mock()
            self.assertEqual(0, command.main([]))
            self.assertIn("--install", run.call_args_list[0].args[0])

    def test_wrong_cache_version_is_not_replaced_or_deployed(self):
        cache = command.CACHE
        (cache / "server/.git").mkdir(parents=True)
        with patch.object(command, "run", return_value="6aa235e493"):
            with self.assertRaises(command.DeploymentError):
                command.check_cache()
        self.assertTrue((cache / "server/.git").exists())

    def test_status_rejects_public_runtime_that_disagrees_with_release_manifest(self):
        self.config()
        expected = hashlib.sha256(b"expected").hexdigest()
        with patch.object(command, "run", return_value=json.dumps({"runtime_sha256": expected})), \
                patch.object(command, "urlopen") as fetch, redirect_stdout(io.StringIO()), \
                redirect_stderr(io.StringIO()):
            fetch.return_value.__enter__.return_value.read.return_value = b"obsolete"
            self.assertEqual(1, command.main(["status"]))


class ManifestTests(unittest.TestCase):
    def test_manifest_identifies_uploaded_bytes_and_refuses_stale_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / "src/web_operation_agent/web_runtime/assistant.js"
            runtime.parent.mkdir(parents=True)
            runtime.write_bytes(b"current-runtime")
            wheels = root / "wheels"
            wheels.mkdir()
            wheel = wheels / "web_operation_agent-0.1-py3-none-any.whl"
            for content, valid in ((b"stale-runtime", False), (runtime.read_bytes(), True)):
                with zipfile.ZipFile(wheel, "w") as archive:
                    archive.writestr("web_operation_agent/web_runtime/assistant.js", content)
                with patch.object(manifest.subprocess, "check_output", side_effect=["abc123\n", b" M file"]), \
                        redirect_stdout(io.StringIO()):
                    if not valid:
                        with self.assertRaisesRegex(ValueError, "stale"):
                            manifest.write_manifest(root, root, "release-1")
                        self.assertFalse((root / "release.json").exists())
                        continue
                    manifest.write_manifest(root, root, "release-1")
                data = json.loads((root / "release.json").read_text())
                self.assertTrue(data["modified"])
                self.assertEqual(hashlib.sha256(content).hexdigest(), data["runtime_sha256"])
                self.assertEqual(hashlib.sha256(wheel.read_bytes()).hexdigest(), data["wheels"][wheel.name])


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.host = self.root / "host"
        self.host.mkdir()
        for release in ("old", "new"):
            directory = self.host / "releases" / release
            jar = directory / "server/yudao-server/target/yudao-server.jar"
            jar.parent.mkdir(parents=True)
            jar.write_text("same-jar")
            (directory / "runtime.sha256").write_text(hashlib.sha256(release.encode()).hexdigest() + "\n")
        (self.host / "agent.env").touch()
        (self.host / "current").symlink_to("releases/old")
        source = (CLOUD / "activate.sh").read_text().replace("/opt/yudao", str(self.host))
        self.script = self.root / "activate.sh"
        self.script.write_text(source)
        preamble = (
            "#!/bin/sh\n"
            "tool=${0##*/}\n"
            'current=$(basename "$(readlink "$FAKE_HOST/current")")\n'
            'tab=$(printf "\\t")\n'
            'line="$tool$tab$current"\n'
            'for a in "$@"; do line="$line$tab$a"; done\n'
            'printf "%s\\n" "$line" >> "$FAKE_HOST/calls"\n'
        )
        scripts = {
            "flock": preamble + "exit 0\n",
            "sleep": preamble + "exit 0\n",
            "mv": preamble + 'prev=""; last=""; for a in "$@"; do prev="$last"; last="$a"; done\nrm -f "$last" && /bin/mv "$prev" "$last"\n',
            "date": preamble + (
                'if [ -f "$FAKE_HOST/clock" ]; then read now < "$FAKE_HOST/clock"; now=$((now + 240)); '
                'else now=0; fi\n'
                'printf "%s\\n" "$now" > "$FAKE_HOST/clock"\n'
                'printf "%s\\n" "$now"\n'
            ),
            "docker": preamble + (
                'b=0; hb=0; hu=0\n'
                'for a in "$@"; do case "$a" in *releases/new/compose.yaml*) b=1;; build) hb=1;; up) hu=1;; esac; done\n'
                'if [ "$FAKE_FAILURE" = "build" ] && [ "$hb" = "1" ] && [ "$b" = "1" ]; then exit 1; fi\n'
                'if [ "$FAKE_FAILURE" = "up" ] && [ "$hu" = "1" ] && [ "$current" = "new" ]; then exit 1; fi\n'
            ),
            "curl": preamble + (
                'last=""; ho=0\n'
                'for a in "$@"; do last="$a"; [ "$a" = "-o" ] && ho=1; done\n'
                'if [ "$current" = "new" ]; then\n'
                '  case "$last" in */readyz*) [ "$FAKE_FAILURE" = "assistant" ] && exit 22 ;; esac\n'
                '  case "$last" in https://*) [ "$FAKE_FAILURE" = "public" ] && exit 22 ;; esac\n'
                'fi\n'
                'if [ "$ho" = "0" ]; then\n'
                '  if [ "$current" = "new" ] && [ "$FAKE_FAILURE" = "hash" ]; then printf "wrong"; else printf "%s" "$current"; fi\n'
                'fi\n'
            ),
            "sha256sum": preamble + (
                'if [ -x /usr/bin/sha256sum ]; then exec /usr/bin/sha256sum; '
                'else /usr/bin/shasum -a 256 | awk \'{print $1"  -"}\'; fi\n'
            ),
        }
        for name, body in scripts.items():
            file = self.bin / name
            file.write_text(body)
            file.chmod(0o755)

    def activate(self, failure=""):
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                   FAKE_HOST=str(self.host), FAKE_FAILURE=failure)
        result = subprocess.run(["sh", str(self.script), "new", "https://demo.example"], env=env,
                                capture_output=True, text=True, timeout=20)
        calls = [[parts[0], parts[1], parts[2:]]
                 for line in (self.host / "calls").read_text().splitlines()
                 for parts in [line.split("\t")]]
        return result, calls

    def test_success_checks_internal_and_public_asset_without_restarting_unchanged_server(self):
        result, calls = self.activate()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("new", (self.host / "current").resolve().name)
        requests = [c[2][-1] for c in calls if c[0] == "curl"]
        self.assertIn("https://demo.example/woa-agent/v1/runtime/assistant.js", requests)
        self.assertIn("http://127.0.0.1:48091/v1/runtime/assistant.js", requests)
        self.assertFalse(any("--force-recreate" in c[2] for c in calls))

    def test_failed_image_build_keeps_current_without_starting_services(self):
        result, calls = self.activate("build")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("old", (self.host / "current").resolve().name)
        self.assertFalse(any(c[0] == "docker" and "up" in c[2] for c in calls))

    def test_failed_compose_up_restores_previous_release(self):
        result, calls = self.activate("up")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("old", (self.host / "current").resolve().name)
        self.assertIn("restored releases/old", result.stderr)

    def test_backend_200_does_not_hide_assistant_failure(self):
        result, _ = self.activate("assistant")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("old", (self.host / "current").resolve().name)
        self.assertIn("restored releases/old", result.stderr)

    def test_wrong_runtime_hash_triggers_rollback(self):
        result, _ = self.activate("hash")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("old", (self.host / "current").resolve().name)
        self.assertIn("restored releases/old", result.stderr)

    def test_public_site_failure_triggers_rollback(self):
        result, _ = self.activate("public")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("old", (self.host / "current").resolve().name)
        self.assertIn("restored releases/old", result.stderr)

    def test_compose_stop_grace_period_and_local_serve_graceful_drain(self):
        cloud_compose = (REPO / "examples/yudao/deploy/compose.cloud.yaml").read_text(encoding="utf-8")
        local_compose = (REPO / "examples/yudao/deploy/local/compose.yaml").read_text(encoding="utf-8")
        for label, text in (("compose.cloud.yaml", cloud_compose), ("local/compose.yaml", local_compose)):
            with self.subTest(file=label):
                self.assertIn("stop_grace_period: 25s", text)

        serve_src = (REPO / "examples/yudao/deploy/local/serve.py").read_text(encoding="utf-8")
        self.assertIn("host.begin_drain()", serve_src)
        self.assertIn("host.cleanup()", serve_src)
        self.assertIn("host.drain()", serve_src)
        self.assertLess(serve_src.index("host.drain()"), serve_src.index("boot.shutdown()"))
        self.assertLess(serve_src.index("host.drain()"), serve_src.index("host.close()"))


class DeployUploadTests(unittest.TestCase):
    """Verify deploy.sh upload layout and hardlink reuse via real rsync and disposable directories."""

    def setUp(self):
        self.assertIsNotNone(shutil.which("rsync"), "rsync must be installed to run upload tests")
        self.temp = tempfile.TemporaryDirectory(prefix="deploy with spaces ")
        self.addCleanup(self.temp.cleanup)
        self.host_temp = tempfile.TemporaryDirectory(prefix="deploy-host-")
        self.addCleanup(self.host_temp.cleanup)
        base = Path(self.temp.name)
        self.repo = base / "repo"
        self.cache = base / "cache"
        self.host = Path(self.host_temp.name) / "yudao"
        self.bin = base / "bin"
        self.bin.mkdir()
        (self.host / "releases").mkdir(parents=True)
        self._populate_repo()
        self._populate_cache()
        self._install_shims()

    def _write_text(self, path: Path, text: str, mode: int | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if mode is not None:
            path.chmod(mode)
        return path

    def _populate_repo(self):
        for rel in (
            "scripts/python.sh",
            "examples/yudao/deploy/compose.yaml",
            "examples/yudao/deploy/compose.cloud.yaml",
            "examples/yudao/deploy/cloud/deploy.sh",
            "examples/yudao/deploy/cloud/manifest.py",
            "examples/yudao/deploy/cloud/activate.sh",
            "examples/yudao/deploy/cloud/yudao-loader.js",
            "examples/yudao/deploy/cloud/agent.Dockerfile",
            "examples/yudao/deploy/cloud/native-policy/verified-money.json",
        ):
            target = self.repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, target)
        venv_python = self.repo / ".venv/bin/python"
        venv_python.parent.mkdir(parents=True, exist_ok=True)
        venv_python.symlink_to(sys.executable)
        for rel, body in (
            ("frontend/dist/integrations/yudao/page-facts.js", "facts\n"),
            ("frontend/dist/integrations/yudao/yudao-page-controller.js", "controller\n"),
            ("frontend/dist/integrations/yudao/form-surface.js", "form\n"),
            ("src/web_operation_agent/web_runtime/assistant.js", "assistant-v1\n"),
            ("src/web_operation_agent/web_runtime/trace_replay.html", "<html></html>\n"),
        ):
            self._write_text(self.repo / rel, body)

    def _populate_cache(self):
        jar = self.cache / "server/yudao-server/target/yudao-server.jar"
        jar.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr("cn/iocoder/yudao/server/woa/WoaPageGuard.class", b"guard")
        for rel, body in (
            ("server/sql/mysql/ruoyi-vue-pro.sql", "SELECT 1;\n"),
            ("server/sql/mysql/quartz.sql", "SELECT 2;\n"),
            ("business-schema.sql", "SELECT 3;\n"),
            ("ui/dist-cloud/index.html", "<html>ui</html>\n"),
            ("ui/dist-cloud/assets/app.js", "console.log('ui');\n"),
            ("woa/page-controller.js", "woa-controller\n"),
            ("woa/LICENSE", "MIT\n"),
            ("ui/src/main.ts", "export {};\n"),
            ("ui/src/views/system/User.vue", "<template></template>\n"),
            ("ui/src/views/system/README.md", "ignore\n"),
            ("ui/src/ignored_only/note.txt", "prune\n"),
            ("server/yudao-module-system/src/main/java/cn/iocoder/Sys.java", "class Sys {}\n"),
            ("server/yudao-module-mall/yudao-module-product/src/main/java/cn/iocoder/Prod.java", "class Prod {}\n"),
            ("server/yudao-module-system/src/main/resources/application.yaml", "ignored: true\n"),
        ):
            self._write_text(self.cache / rel, body)

    def _install_shims(self):
        self._write_text(
            self.bin / "uv",
            f"#!{sys.executable}\n"
            "from pathlib import Path\n"
            "import sys, zipfile\n"
            "args = sys.argv[1:]\n"
            "out = Path(args[args.index('--out-dir') + 1])\n"
            "target = Path(args[-1])\n"
            "runtime = Path(__import__('os').environ['FAKE_REPO']) / 'src/web_operation_agent/web_runtime/assistant.js'\n"
            "name = 'web_operation_agent_yudao-0.4.0-py3-none-any.whl' if target.name == 'yudao' else 'web_operation_agent-0.4.0-py3-none-any.whl'\n"
            "with zipfile.ZipFile(out / name, 'w') as zf:\n"
            "    if name.startswith('web_operation_agent-'):\n"
            "        zf.writestr('web_operation_agent/web_runtime/assistant.js', runtime.read_bytes())\n"
            "    else:\n"
            "        zf.writestr('web_operation_agent_yudao/__init__.py', b'')\n",
            0o755,
        )
        self._write_text(
            self.bin / "git",
            f"#!{sys.executable}\n"
            "import sys\n"
            "if 'rev-parse' in sys.argv:\n"
            "    print('a' * 40)\n",
            0o755,
        )
        self._write_text(
            self.bin / "ssh",
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "host = os.environ['FAKE_HOST']\n"
            "args = sys.argv[1:]\n"
            "with (Path(host) / 'ssh_calls.jsonl').open('a', encoding='utf-8') as log:\n"
            "    log.write(json.dumps(args) + '\\n')\n"
            "i = 0\n"
            "while i < len(args):\n"
            "    if args[i] in ('-l', '-o', '-p'):\n"
            "        i += 2; continue\n"
            "    if args[i].startswith('-'):\n"
            "        i += 1; continue\n"
            "    break\n"
            "cmd = args[i + 1:]\n"
            "if cmd[:3] == ['sh', '-s', '--']:\n"
            "    sys.stdin.read()\n"
            "    current = Path(host) / 'current'\n"
            "    nxt = Path(host) / 'current.next'\n"
            "    nxt.unlink(missing_ok=True)\n"
            "    nxt.symlink_to(f'releases/{cmd[3]}')\n"
            "    os.replace(nxt, current)\n"
            "    sys.exit(0)\n"
            "remote_cmd = cmd[0] if len(cmd) == 1 else ' '.join(cmd)\n"
            "os.execvp('sh', ['sh', '-c', remote_cmd.replace('/opt/yudao', host)])\n",
            0o755,
        )

    def _run_deploy(self):
        call_log = self.host / "ssh_calls.jsonl"
        before_calls = len(call_log.read_text(encoding="utf-8").splitlines()) if call_log.exists() else 0
        env = dict(
            os.environ,
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            YUDAO_CACHE=str(self.cache),
            WOA_PUBLIC_ORIGIN="https://demo.example",
            FAKE_HOST=str(self.host),
            FAKE_REPO=str(self.repo),
        )
        result = subprocess.run(
            ["/bin/sh", str(self.repo / "examples/yudao/deploy/cloud/deploy.sh"), "fixture-host"],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(0, result.returncode, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        lines = call_log.read_text(encoding="utf-8").splitlines()[before_calls:]
        release_dir = (self.host / "current").resolve()
        return release_dir, lines

    def test_first_deploy_layout_filtering_and_consolidated_ssh_calls(self):
        release, ssh_calls = self._run_deploy()
        self.assertLessEqual(len(ssh_calls), 8, ssh_calls)

        expected_files = (
            "server/yudao-server/target/yudao-server.jar",
            "server/sql/mysql/ruoyi-vue-pro.sql",
            "server/sql/mysql/quartz.sql",
            "business-schema.sql",
            "compose.yaml",
            "compose.cloud.yaml",
            "release.json",
            "runtime.sha256",
            "ui/index.html",
            "ui/assets/app.js",
            "woa/page-controller.js",
            "woa/LICENSE",
            "woa/yudao-loader.js",
            "woa/page-facts.js",
            "woa/yudao-page-controller.js",
            "woa/form-surface.js",
            "agent/agent.Dockerfile",
            "agent/wheels/web_operation_agent-0.4.0-py3-none-any.whl",
            "agent/wheels/web_operation_agent_yudao-0.4.0-py3-none-any.whl",
            "native-policy/verified-money.json",
            "source/ui/src/main.ts",
            "source/ui/src/views/system/User.vue",
            "source/server/yudao-module-system/src/main/java/cn/iocoder/Sys.java",
            "source/server/yudao-module-mall/yudao-module-product/src/main/java/cn/iocoder/Prod.java",
        )
        for rel in expected_files:
            self.assertTrue((release / rel).is_file(), f"missing uploaded file: {rel}")

        self.assertFalse((release / "source/ui/src/views/system/README.md").exists())
        self.assertFalse((release / "source/ui/src/ignored_only").exists())
        self.assertFalse((release / "source/server/yudao-module-system/src/main/resources").exists())

    def test_second_deploy_hardlinks_unchanged_cache_and_updates_runtime(self):
        rel1, _ = self._run_deploy()
        v1_digest = hashlib.sha256(b"assistant-v1\n").hexdigest()
        self.assertEqual(v1_digest + "\n", (rel1 / "runtime.sha256").read_text(encoding="utf-8"))

        (self.repo / "src/web_operation_agent/web_runtime/assistant.js").write_text("assistant-v2\n", encoding="utf-8")
        rel2, ssh_calls2 = self._run_deploy()
        self.assertNotEqual(rel1, rel2)
        self.assertLessEqual(len(ssh_calls2), 8, ssh_calls2)

        for rel in (
            "server/yudao-server/target/yudao-server.jar",
            "server/sql/mysql/ruoyi-vue-pro.sql",
            "ui/index.html",
            "ui/assets/app.js",
            "source/ui/src/main.ts",
            "source/ui/src/views/system/User.vue",
            "source/server/yudao-module-system/src/main/java/cn/iocoder/Sys.java",
            "source/server/yudao-module-mall/yudao-module-product/src/main/java/cn/iocoder/Prod.java",
        ):
            self.assertEqual(
                (rel1 / rel).stat().st_ino,
                (rel2 / rel).stat().st_ino,
                f"expected hardlink reuse across releases for {rel}",
            )

        v2_digest = hashlib.sha256(b"assistant-v2\n").hexdigest()
        self.assertEqual(v1_digest + "\n", (rel1 / "runtime.sha256").read_text(encoding="utf-8"))
        self.assertEqual(v2_digest + "\n", (rel2 / "runtime.sha256").read_text(encoding="utf-8"))
        with zipfile.ZipFile(rel1 / "agent/wheels/web_operation_agent-0.4.0-py3-none-any.whl") as z1:
            self.assertEqual(b"assistant-v1\n", z1.read("web_operation_agent/web_runtime/assistant.js"))
        with zipfile.ZipFile(rel2 / "agent/wheels/web_operation_agent-0.4.0-py3-none-any.whl") as z2:
            self.assertEqual(b"assistant-v2\n", z2.read("web_operation_agent/web_runtime/assistant.js"))


class NginxWoaCacheHeaderTests(unittest.TestCase):
    def test_woa_location_forces_revalidation_without_dropping_server_headers(self):
        repo = Path(__file__).resolve().parents[5]
        for rel in ("examples/yudao/deploy/cloud/nginx.site.conf", "examples/yudao/deploy/nginx.conf"):
            text = (repo / rel).read_text(encoding="utf-8")
            match = re.search(r"location\s+/woa/\s*\{(?P<body>[^}]*)\}", text)
            self.assertIsNotNone(match, f"missing location /woa/ block in {rel}")
            directives = [
                line.split("#", 1)[0].strip()
                for line in match.group("body").splitlines()
            ]
            directives = [line for line in directives if line]
            self.assertIn("expires -1;", directives, f"{rel} must disable heuristic browser caching for /woa/")
            self.assertFalse(
                any(line.startswith("add_header") for line in directives),
                f"{rel} /woa/ should preserve server-level add_header inheritance",
            )


if __name__ == "__main__":
    unittest.main()
