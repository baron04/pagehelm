"""Shell entry points must not fall through to an unsupported host interpreter."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINTS = ('local', 'deploy', 'scripts/check_wheels.sh', 'scripts/qualify.sh',
               'examples/yudao/deploy/cloud/deploy.sh')


class PythonEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in (*ENTRYPOINTS, 'scripts/python.sh'):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.env = dict(os.environ, ROOT=str(self.root))
        self.env.pop('PYTHON', None)

    def executable(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!{sys.executable}\n' + body)
        path.chmod(0o755)
        return path

    def interpreter(self, minor):
        return self.executable(self.bin / f'python3.{minor}',
                               f'import sys\nsys.version_info = (3, {minor}, 0)\nexec(sys.argv[2])\n')

    def venv(self, interpreter):
        target = self.root / '.venv/bin/python'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(interpreter)
        return target

    def run_entrypoint(self, name, *args, env=None):
        shell = '/bin/bash' if name.startswith('scripts/') else '/bin/sh'
        return subprocess.run([shell, str(self.root / name), *args], env=env or self.env,
                              capture_output=True, text=True, timeout=15)

    def select(self, **overrides):
        env = dict(self.env, **overrides)
        return subprocess.run(['/bin/sh', '-c',
                               '. "$ROOT/scripts/python.sh"; printf "%s" "$PYTHON"'],
                              env=env, capture_output=True, text=True, timeout=15)

    def test_each_entrypoint_rejects_unsupported_python_before_work(self):
        marker = self.root / 'dist/preserve'
        marker.parent.mkdir()
        marker.write_text('existing build')
        for minor in (12, 14):
            wrong = self.interpreter(minor)
            venv = self.root / '.venv/bin/python'
            venv.unlink(missing_ok=True)
            self.venv(wrong)
            for name in ENTRYPOINTS:
                with self.subTest(minor=minor, entrypoint=name):
                    result = self.run_entrypoint(name, 'fixture-host',
                                                 env=dict(self.env, PYTHON=str(wrong)))
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn('Python 3.13', result.stderr)
                    self.assertEqual(marker.read_text(), 'existing build')

    def test_local_requires_its_checkout_venv_even_with_explicit_python(self):
        result = self.run_entrypoint('local', env=dict(self.env, PYTHON=sys.executable))
        self.assertEqual(result.returncode, 2)
        self.assertIn('缺少 .venv', result.stderr)

    def test_local_and_deploy_run_current_venv_and_preserve_arguments(self):
        venv = self.venv(sys.executable)
        for name, target in (('local', 'local'), ('deploy', 'cloud')):
            command = self.root / f'examples/yudao/deploy/{target}/command.py'
            command.parent.mkdir(parents=True, exist_ok=True)
            command.write_text('import json, os, sys\nprint(json.dumps('
                               '{"version": list(sys.version_info[:2]), '
                               '"python": os.environ["PYTHON"], "args": sys.argv[1:]}))\n')
            with self.subTest(entrypoint=name):
                result = self.run_entrypoint(name, 'check')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout),
                                 {'version': [3, 13], 'python': str(venv), 'args': ['check']})

    def test_explicit_supported_python_overrides_wrong_venv(self):
        self.venv(self.interpreter(14))
        result = self.select(PYTHON=sys.executable)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, sys.executable)

    def test_no_venv_selects_named_python313_not_unqualified_python3(self):
        named = self.bin / 'python3.13'
        named.symlink_to(sys.executable)
        (self.bin / 'python3').symlink_to(self.interpreter(14))
        result = self.select(PATH=str(self.bin))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(named))

    def test_unqualified_python3_alone_is_not_a_fallback(self):
        (self.bin / 'python3').symlink_to(sys.executable)
        result = self.select(PATH=str(self.bin))
        self.assertEqual(result.returncode, 2)
        self.assertIn('Python 3.13', result.stderr)

    def test_check_cli_rejects_wrong_python_before_running_checks(self):
        for name in ('scripts/check.py', 'src/web_operation_agent/__init__.py'):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        probe = self.root / 'must-not-parse.py'
        probe.write_text('invalid Python syntax !')
        program = ('import argparse, ast, json, os, pathlib, re, runpy, subprocess, sys, time, urllib.parse; '
                   'sys.version_info = (3, int(sys.argv[1]), 0); '
                   'sys.argv = [sys.argv[2], "--syntax-only", sys.argv[3]]; '
                   'runpy.run_path(sys.argv[0], run_name="__main__")')
        for minor in (12, 14):
            with self.subTest(minor=minor):
                result = subprocess.run([sys.executable, '-c', program, str(minor),
                                         str(self.root / 'scripts/check.py'), str(probe)],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('Python 3.13', result.stderr)
                self.assertNotIn('SyntaxError', result.stderr)
                self.assertEqual(result.stdout, '')

    def test_cloud_builds_and_manifest_share_selected_python(self):
        venv = self.venv(sys.executable)
        cache = self.root / '.q0-cache/yudao'
        for name in ('server/yudao-server/target/yudao-server.jar', 'ui/dist-cloud/index.html',
                     'woa/page-controller.js', 'business-schema.sql',
                     'server/sql/mysql/ruoyi-vue-pro.sql', 'server/sql/mysql/quartz.sql'):
            path = cache / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        for name in ('frontend/src/integrations/yudao/page-facts.js',
                     'frontend/src/integrations/yudao/yudao-page-controller.js',
                     'frontend/src/integrations/yudao/form-surface.js',
                     'src/web_operation_agent/web_runtime/assistant.js',
                     'src/web_operation_agent/web_runtime/trace_replay.html'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        self.executable(self.bin / 'uv', 'import json, os, sys\n'
                        'with open(os.environ["BUILD_LOG"], "a") as output:\n'
                        '    output.write(json.dumps(sys.argv[1:]) + "\\n")\n')
        manifest = self.root / 'examples/yudao/deploy/cloud/manifest.py'
        manifest.write_text('import json, os, sys\n'
                            'with open(os.environ["MANIFEST_LOG"], "w") as output:\n'
                            '    json.dump({"version": list(sys.version_info[:2]), '
                            '"python": os.environ["PYTHON"]}, output)\n'
                            'raise SystemExit(19)\n')  # Stop before any SSH or upload.
        builds, recorded = self.root / 'builds.jsonl', self.root / 'manifest.json'
        env = dict(self.env, PATH=str(self.bin) + os.pathsep + self.env['PATH'],
                   BUILD_LOG=str(builds), MANIFEST_LOG=str(recorded), YUDAO_CACHE=str(cache))
        result = self.run_entrypoint('examples/yudao/deploy/cloud/deploy.sh', 'fixture-host', env=env)
        self.assertEqual(result.returncode, 19, result.stderr)
        commands = [json.loads(line) for line in builds.read_text().splitlines()]
        self.assertEqual(len(commands), 2)
        self.assertTrue(all(command[:3] == ['build', '--python', str(venv)] for command in commands))
        self.assertEqual(json.loads(recorded.read_text()), {'version': [3, 13], 'python': str(venv)})


if __name__ == '__main__':
    unittest.main()
