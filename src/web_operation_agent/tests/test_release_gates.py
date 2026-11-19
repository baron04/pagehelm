"""The gate scripts, checked against the tampering they exist to catch.

Three of the four scripts under `scripts/` had no test of their own. A detector with no test
is one that can weaken without anybody noticing, and one of them already had: `--check`
compared names and versions and never the hashes, so a digest in the lock could be edited to
anything at all and the gate stayed green. pip would have refused it on install -- but the
gate whose whole job is the dependency inventory would not have said a word.

These copy the committed artifacts into a throwaway tree, tamper with one thing, and run the
gate's own copy over it -- because a fixture that merely resembles the repository is one that
can drift away from it, and because a gate reading the committed files while the tampering
sits in a temporary directory would pass every test for the wrong reason.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"


def _run(script: str, *args, cwd: Path) -> subprocess.CompletedProcess:
    """Run the copy inside the tree under test.

    Both gates resolve the repository from their own `__file__`, so running the committed
    script would read the committed files and report on those -- the tampering would sit in
    the temporary tree, unexamined, and every one of these tests would pass for the wrong
    reason. That is what the first version of this file did.
    """
    copied = cwd / "scripts" / script
    assert copied.is_file(), f"{script} was not copied into the tree under test"
    return subprocess.run([sys.executable, str(copied), *args],
                          cwd=str(cwd), capture_output=True, text=True, timeout=300)


class TheLocalEnvironmentTemplateIsSafeAndUseful(unittest.TestCase):
    def setUp(self):
        lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        pairs = [line.split("=", 1) for line in lines
                 if line.strip() and not line.lstrip().startswith("#")]
        self.values = dict(pairs)
        self.assertEqual(len(self.values), len(pairs), "duplicate environment setting")

    def test_it_documents_the_minimum_model_configuration(self):
        self.assertTrue(1 <= len(self.values) <= 3, "only 1-3 API key lines belong in .env.example")
        self.assertTrue(all(key.endswith("_API_KEY") for key in self.values), self.values)
        self.assertFalse(any(self.values[name] for name in self.values),
                         "copying the template must not enable a live service")
        self.assertFalse(any(key.startswith("WOA_ODOO_") for key in self.values))

    def test_it_contains_neither_a_credential_nor_a_local_path(self):
        for name in (".env.example", "pagehelm.example.toml"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"/(Users|home)/[^/]+/", name)
            self.assertNotIn("odoo", text.casefold(), name)

    def test_pagehelm_toml_template_parses_cleanly_and_expands_safely(self):
        import tomllib
        from web_operation_agent.modeling.client import expand_operation_mode, load_pagehelm_toml

        toml_path = ROOT / "pagehelm.example.toml"
        self.assertTrue(toml_path.is_file(), "pagehelm.example.toml is missing")
        parsed = load_pagehelm_toml(toml_path)
        self.assertIn(parsed.get("provider"), {"deepseek", "qwen", "openai"})
        self.assertEqual(parsed.get("mode"), "verified-operate")
        self.assertTrue(expand_operation_mode(str(parsed["mode"]))["native_writes"])
        raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        self.assertEqual(raw, parsed)


class _Copy:
    """A throwaway tree carrying the files one gate reads, and nothing else."""

    def __init__(self, *paths: str):
        self.paths = paths

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        for name in self.paths:
            source = ROOT / name
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                # The directory inputs are the boundary checker's Python and neutral TypeScript trees.
                # Neither generated assets nor interpreter caches are checker inputs.
                for path in source.rglob("*"):
                    if not path.is_file() or path.suffix not in {".py", ".ts"} or "__pycache__" in path.parts:
                        continue
                    copied = target / path.relative_to(source)
                    copied.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, copied)
            elif source.is_file():
                shutil.copy2(source, target)
        return root

    def __exit__(self, *exc) -> None:
        self._tmp.cleanup()


class TheDependencyGateChecksDigestsAndNotOnlyNames(unittest.TestCase):
    """The gap that made this file worth writing."""

    FILES = ("requirements.lock", "sbom.json", "THIRD_PARTY.md", "scripts/lock_and_sbom.py",
             "frontend/package-lock.json", "frontend/docs/package-lock.json",
             # The checker also reconciles the pinned browser asset against the notice.
             "src/web_operation_agent/policies/page_controller.json",
             # The checker imports the release's supported-Python declaration. Keep the
             # copied gate executable rather than accidentally importing a checkout next
             # to the isolated fixture.
             "src/web_operation_agent/__init__.py")

    def test_a_digest_edited_in_the_lock_is_caught(self):
        """Before this, editing a hash to sixty-four zeroes passed."""
        with _Copy(*self.FILES) as root:
            lock = root / "requirements.lock"
            text = lock.read_text()
            first = re.search(r"--hash=sha256:([0-9a-f]{64})", text).group(1)
            lock.write_text(text.replace(first, "0" * 64, 1))
            done = _run("lock_and_sbom.py", "--check", cwd=root)

        self.assertEqual(done.returncode, 1)
        self.assertIn("records", done.stdout + done.stderr)

    def test_a_component_removed_from_the_sbom_is_caught(self):
        with _Copy(*self.FILES) as root:
            path = root / "sbom.json"
            payload = json.loads(path.read_text())
            payload["components"] = [item for item in payload["components"]
                                     if item["name"].casefold() != "greenlet"]
            path.write_text(json.dumps(payload, indent=2) + "\n")
            done = _run("lock_and_sbom.py", "--check", cwd=root)

        self.assertEqual(done.returncode, 1)
        self.assertIn("greenlet", (done.stdout + done.stderr).casefold())

    def test_a_direct_dependency_dropped_from_the_notice_is_caught(self):
        """Playwright, which this rule used to exempt.

        pip marks a project as "requested" and everything it pulls as not, so the one
        dependency this repository declares was classed transitive -- and the rule, which
        exempts transitive packages, constrained nothing on the Python side at all.
        """
        with _Copy(*self.FILES) as root:
            notice = root / "THIRD_PARTY.md"
            notice.write_text(re.sub(r"^.*[Pp]laywright.*$", "", notice.read_text(),
                                     flags=re.MULTILINE))
            done = _run("lock_and_sbom.py", "--check", cwd=root)

        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("playwright", (done.stdout + done.stderr).casefold())

    def test_every_locked_requirement_carries_a_hash_for_more_than_one_platform(self):
        """A lock resolved only where it was written installs only there.

        It pinned `macosx_11_0_arm64` wheels, so `--require-hashes` refused on Linux with no
        message saying why, and "a reviewer can run it" was false for every reviewer not on
        a Mac.
        """
        text = (ROOT / "requirements.lock").read_text()
        platform_specific = [name for name in ("greenlet", "playwright")
                             if re.search(rf"^{name}==", text, re.MULTILINE)]
        self.assertTrue(platform_specific, "the lock no longer pins a compiled wheel")
        for name in platform_specific:
            lines = text.splitlines()
            start = next(index for index, line in enumerate(lines)
                         if line.startswith(f"{name}=="))
            digests = 0
            for line in lines[start + 1:]:
                if "--hash=sha256:" not in line:
                    break
                digests += 1
            self.assertGreater(
                digests, 1,
                f"{name} carries one artifact hash, so the lock installs on one platform")


class TheCoreBoundaryGateCatchesWhatItNames(unittest.TestCase):
    # The checker only reads this core tree, including its tests, and the neutral runtime.
    FILES = ("scripts/check_core_boundary.py", "src/web_operation_agent", "frontend/src/runtime")

    def test_an_application_route_in_core_execution_logic_is_caught(self):
        with _Copy(*self.FILES) as root:
            target = root / "src/web_operation_agent/execution/session.py"
            target.write_text(target.read_text().replace(
                "class ServerAgentSession",
                'ODOO_ROUTE = "/odoo/customers"\n\n\nclass ServerAgentSession', 1))
            done = _run("check_core_boundary.py", cwd=root)

        self.assertEqual(done.returncode, 1)

    def test_ui_framework_selector_in_browser_runtime_is_caught(self):
        with _Copy(*self.FILES) as root:
            target = root / "frontend/src/runtime/combobox.ts"
            target.write_text(target.read_text() + '\nconst LEAK = ".el-tree-node__content"\n')
            done = _run("check_core_boundary.py", cwd=root)

        self.assertEqual(done.returncode, 1)

    def test_core_importing_the_evaluation_harness_is_caught(self):
        with _Copy(*self.FILES) as root:
            target = root / "src/web_operation_agent/execution/session.py"
            target.write_text("from evals.source_operations import workflow_gate  # noqa\n"
                              + target.read_text())
            done = _run("check_core_boundary.py", cwd=root)

        self.assertEqual(done.returncode, 1)


class TheRepositoryHygieneGateCatchesWhatItNames(unittest.TestCase):
    FILES = ("scripts/check_repo_hygiene.py", ".env.example")

    def test_clean_tree_passes(self):
        with _Copy(*self.FILES) as root:
            done = _run("check_repo_hygiene.py", cwd=root)

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_local_user_home_paths_are_caught_while_container_home_is_allowed(self):
        with _Copy(*self.FILES) as root:
            (root / "container.txt").write_text("WORKDIR /home/ubuntu/workspace\n")
            self.assertEqual(_run("check_repo_hygiene.py", cwd=root).returncode, 0)

            (root / "leak.py").write_text('PATH = "/' + 'Users/alice/repo/secret.txt"\n')
            done = _run("check_repo_hygiene.py", cwd=root)
            self.assertEqual(done.returncode, 1)
            self.assertIn("leak.py", done.stdout)

            (root / "leak.py").write_text('PATH = "/ho' + 'me/alice/repo/secret.txt"\n')
            done = _run("check_repo_hygiene.py", cwd=root)
            self.assertEqual(done.returncode, 1)
            self.assertIn("leak.py", done.stdout)

    def test_file_uri_and_secrets_are_caught(self):
        with _Copy(*self.FILES) as root:
            (root / "uri.txt").write_text("fi" + "le:///tmp/local-artifact\n")
            self.assertEqual(_run("check_repo_hygiene.py", cwd=root).returncode, 1)
            (root / "uri.txt").unlink()

            (root / "key.pem").write_text("-----BEGIN OPENSSH " + "PRIVATE KEY-----\n")
            self.assertEqual(_run("check_repo_hygiene.py", cwd=root).returncode, 1)
            (root / "key.pem").unlink()

            (root / ".env.local").write_text("AGENT_OPENAI_API_KEY=" + "sk-" + "Ab3dEf9hIjKlMnOpQrStUvWx\n")
            self.assertEqual(_run("check_repo_hygiene.py", cwd=root).returncode, 1)
            (root / ".env.local").unlink()

            (root / "deepseek.env").write_text("DEEPSEEK_API_KEY=" + "Ab3dEf9Gh2Jk5Lm8Np1Qr4St7Uv0Wx\n")
            self.assertEqual(_run("check_repo_hygiene.py", cwd=root).returncode, 1)
            (root / "deepseek.env").unlink()

            (root / "creds.json").write_text('{"password": "' + "Str0ngProdPassw0rd!2026_XyZ" + '"}\n')
            self.assertEqual(_run("check_repo_hygiene.py", cwd=root).returncode, 1)
            (root / "creds.json").unlink()

            (root / "pat.txt").write_text("github_" + "pat_11ABCDEFG0123456789abcdef_XYZ\n")
            self.assertEqual(_run("check_repo_hygiene.py", cwd=root).returncode, 1)
            (root / "pat.txt").unlink()

            (root / "safe.env").write_text("DEEPSEEK_API_KEY=replace-with-your-example-token\n")
            self.assertEqual(_run("check_repo_hygiene.py", cwd=root).returncode, 0)


class TheWheelGateCatchesWhatItNames(unittest.TestCase):
    def test_it_refuses_a_wheel_carrying_an_application_name(self):
        """Run directly rather than over a built wheel: the point is the rule, not the build."""
        sys.path.insert(0, str(SCRIPTS))
        try:
            import check_wheel_contents as gate
        finally:
            sys.path.pop(0)

        core = gate.DISTRIBUTIONS["web-operation-agent (core)"]
        for expected in ("odoo", "contacts", "testapps"):
            self.assertIn(expected, core["forbidden_name_substrings"],
                          "an application outside the core distribution is not "
                          "forbidden in the core wheel")
        self.assertTrue(core["required"], "the core wheel requires nothing of itself")


if __name__ == "__main__":
    unittest.main()
