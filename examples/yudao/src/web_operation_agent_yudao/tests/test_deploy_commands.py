"""Deployment failure paths against disposable files and fake commands, never a host."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
import re
from pathlib import Path
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
        program = '''import hashlib,json,os,pathlib,sys
root=pathlib.Path(os.environ['FAKE_HOST']); args=sys.argv[1:]; tool=pathlib.Path(sys.argv[0]).name
current=(root/'current').resolve().name
mode=os.environ.get('FAKE_FAILURE','')
with (root/'calls').open('a') as f:f.write(json.dumps([tool,current,args])+'\\n')
if tool=='flock' or tool=='sleep':sys.exit(0)
if tool=='mv':os.replace(args[-2],args[-1])
elif tool=='date':
    p=root/'clock'; now=int(p.read_text())+240 if p.exists() else 0;p.write_text(str(now));print(now)
elif tool=='sha256sum':print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()+'  -')
elif tool=='docker':
    building_new=any('releases/new/compose.yaml' in a for a in args)
    if (mode=='build' and 'build' in args and building_new) or (mode=='up' and 'up' in args and current=='new'):sys.exit(1)
elif tool=='curl':
    url=args[-1]
    if current=='new' and ((mode=='assistant' and '/readyz' in url) or (mode=='public' and url.startswith('https://'))):sys.exit(22)
    if '-o' not in args:sys.stdout.write('wrong' if current=='new' and mode=='hash' else current)
'''
        for name in ("flock", "sleep", "date", "sha256sum", "docker", "curl", "mv"):
            file = self.bin / name
            file.write_text(f"#!{sys.executable}\n" + program)
            file.chmod(0o755)

    def activate(self, failure=""):
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                   FAKE_HOST=str(self.host), FAKE_FAILURE=failure)
        result = subprocess.run(["sh", str(self.script), "new", "https://demo.example"], env=env,
                                capture_output=True, text=True, timeout=20)
        calls = [json.loads(line) for line in (self.host / "calls").read_text().splitlines()]
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


if __name__ == "__main__":
    unittest.main()
