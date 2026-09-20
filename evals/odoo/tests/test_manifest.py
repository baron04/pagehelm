import ast
import unittest

from evals.odoo import manifest
from evals.odoo.population import digest_json

INPUTS = {
    "odoo_base_image": "odoo@sha256:" + "1" * 64,
    "compose_pinned_images": ["postgres@sha256:" + "2" * 64],
    "image_ids": {"db": "sha256:" + "3" * 64, "odoo": "sha256:" + "4" * 64},
    "addon_digest": "sha256:" + "5" * 64,
    "installed_modules": ["account", "sale_management", "woa_runtime"],
    "demo_data": False,
    "odoo_conf_max_cron_threads": 0,
    "odoo_conf_digest": "sha256:" + "8" * 64,
    "population_digest": "sha256:" + "6" * 64,
    "population_seed_commitment": "sha256:" + "7" * 64,
    "fixture_sources": {name: "sha256:" + str(index % 10) * 64
                        for index, name in enumerate(manifest.FIXTURE_SOURCES)},
}
GUARD = "sha256:" + "a" * 64


def recorded(**changes):
    inputs = {**INPUTS, **changes.pop("fixture_inputs", {})}
    body = {"artifact_version": 1, "kind": "p74_fixture_manifest", "database": "woa_trial",
            "fixture_inputs": inputs, "fixture_inputs_digest": digest_json(inputs),
            "taken_with_candidate": {"commit": "c" * 40},
            "baseline": {"guard_digest": GUARD}, **changes}
    return {**body, "manifest_digest": digest_json(body)}


class VerifyManifestTests(unittest.TestCase):
    def test_a_current_manifest_yields_the_fixture_a_batch_binds(self):
        record = manifest.verify_manifest(recorded(), inputs=INPUTS, baseline_guard_digest=GUARD)
        self.assertEqual(manifest.batch_fixture(record),
                         {"manifest_digest": record["manifest_digest"],
                          "baseline_guard_digest": GUARD})

    def test_the_candidate_it_was_taken_with_does_not_make_it_stale(self):
        """The database is seeded through the application's own RPC and the Agent reaches it
        only as a trial's actor, so a rebuilt candidate leaves the fixture as it was. What ran
        is bound by the batch header, not by the fixture the batch ran on."""
        rebuilt = recorded(taken_with_candidate={"commit": "e" * 40})
        self.assertEqual(
            manifest.verify_manifest(rebuilt, inputs=INPUTS, baseline_guard_digest=GUARD),
            rebuilt)

    def test_a_changed_stale_or_foreign_manifest_is_refused(self):
        cases = {
            "changed_after_recording": ({**recorded(), "database": "another"}, INPUTS, GUARD),
            "recorded_before_inputs_were_bound": (
                {key: value for key, value in recorded().items()
                 if key != "fixture_inputs_digest"}, INPUTS, GUARD),
            "another_application_image": (
                recorded(), {**INPUTS, "odoo_base_image": "odoo@sha256:" + "9" * 64}, GUARD),
            "another_addon": (recorded(), {**INPUTS, "addon_digest": "sha256:" + "9" * 64},
                              GUARD),
            "another_population": (
                recorded(), {**INPUTS, "population_digest": "sha256:" + "9" * 64}, GUARD),
            # Every source is bound by content, and each one alone makes the manifest stale.
            **{f"another_{name}": (
                recorded(), {**INPUTS, "fixture_sources": {**INPUTS["fixture_sources"],
                                                           name: "sha256:" + "f" * 64}}, GUARD)
               for name in manifest.FIXTURE_SOURCES},
            "cron_threads_it_was_not_frozen_with": (
                recorded(), {**INPUTS, "odoo_conf_max_cron_threads": 2}, GUARD),
            # The whole file is mounted, so every option is bound.
            "another_odoo_conf": (
                recorded(), {**INPUTS, "odoo_conf_digest": "sha256:" + "e" * 64}, GUARD),
            "another_baseline": (recorded(), INPUTS, "sha256:" + "b" * 64),
            "wrong_kind": (recorded(kind="p74_fixture"), INPUTS, GUARD),
        }
        for label, (record, inputs, guard) in cases.items():
            with self.subTest(label=label), self.assertRaises(manifest.ManifestError):
                manifest.verify_manifest(record, inputs=inputs, baseline_guard_digest=guard)

    def test_every_source_provisioning_reads_is_bound(self):
        """The inputs bind every source by content, the deployment that builds and initialises
        the application included. Each source is checked here against the tree it names."""
        from pathlib import Path as _Path
        repository = _Path(__file__).resolve().parents[3]
        for relative in manifest.FIXTURE_SOURCES:
            self.assertTrue((repository / relative).exists(), relative)
        for expected in ("evals/odoo/odoo_seed.py", "evals/odoo/provision.py",
                         "evals/odoo/compose.p74.yml", "examples/odoo/deploy/single-node"):
            self.assertIn(expected, manifest.FIXTURE_SOURCES)
        digests = manifest._source_digests()
        self.assertEqual(set(digests), set(manifest.FIXTURE_SOURCES))
        self.assertTrue(all(value.startswith("sha256:") for value in digests.values()))

    def test_the_bound_sources_are_the_closure_of_what_builds_the_fixture(self):
        """A list of the parts someone remembered goes stale the moment an import is added. The
        closure is recomputed
        from the two programs that build the fixture: every module they reach, at any depth and
        from anywhere in the file, is bound or exempt by name."""
        from pathlib import Path as _Path
        repository = _Path(__file__).resolve().parents[3]

        def module_path(name: str) -> _Path | None:
            for candidate in (repository / (name.replace(".", "/") + ".py"),
                              repository / name.replace(".", "/") / "__init__.py"):
                if candidate.exists():
                    return candidate
            return None

        def imported(path: _Path) -> set[str]:
            package = ".".join(path.relative_to(repository).parts[:-1])
            names: set[str] = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    # ``from . import x`` and ``from .x import y`` both name modules here; a
                    # name that is not a module simply has no file and drops out below.
                    base = package if node.level else (node.module or "")
                    module = f"{base}.{node.module}" if node.level and node.module else base
                    names.add(module)
                    names.update(f"{module}.{alias.name}" for alias in node.names)
            return names

        closure: set[str] = set()
        pending = [repository / entry for entry in manifest.FIXTURE_ENTRY_POINTS]
        while pending:
            path = pending.pop()
            relative = path.relative_to(repository).as_posix()
            if relative in closure:
                continue
            closure.add(relative)
            for name in imported(path):
                reached = module_path(name)
                if reached is not None:
                    pending.append(reached)
        # Every entry point is reachable from itself, so the closure includes them.
        self.assertTrue(set(manifest.FIXTURE_ENTRY_POINTS) <= closure)
        unbound = closure - set(manifest.FIXTURE_SOURCES) - set(manifest.FIXTURE_EXEMPT)
        self.assertEqual(unbound, set(), "the fixture is built from sources nothing binds")
        # An exemption is a claim about a module the closure really holds, not a spare name.
        self.assertTrue(set(manifest.FIXTURE_EXEMPT) <= closure)

    def test_the_application_options_are_bound_and_the_credentials_are_not(self):
        """`odoo.conf` is mounted whole, so every option that decides what the application does
        is bound; the two that say how to authenticate are not, so a fresh private root is not
        another fixture."""
        conf = ("[options]\n"
                "addons_path = /mnt/extra-addons\n"
                "admin_passwd = one\n"
                "db_password = two\n"
                "workers = 2\n"
                "max_cron_threads = 0\n"
                "# workers = 9\n")
        base = manifest._config_digest(conf)
        self.assertTrue(base.startswith("sha256:"))
        # Another credential is the same fixture.
        self.assertEqual(base, manifest._config_digest(
            conf.replace("admin_passwd = one", "admin_passwd = three")
                .replace("db_password = two", "db_password = four")))
        # Any other option, including one nothing reads by name, is another fixture.
        for changed in ("addons_path = /mnt/other-addons", "workers = 4",
                        "max_cron_threads = 1"):
            name = changed.split(" =")[0]
            line = next(item for item in conf.splitlines()
                        if item.startswith(name) and not item.startswith("#"))
            with self.subTest(option=name):
                self.assertNotEqual(base, manifest._config_digest(conf.replace(line, changed)))
        # An option added later is bound without being named here.
        self.assertNotEqual(base, manifest._config_digest(conf + "limit_time_real = 600\n"))
        # A commented option is not an option.
        self.assertEqual(base, manifest._config_digest(conf + "# limit_time_real = 600\n"))

    def test_what_the_application_is_given_is_what_compose_mounts(self):
        """Compose mounts the whole directory that holds the addon, so a sibling addon -- an
        auto-installing one above all -- would reach the application's addons path and change
        what the database becomes. What is bound is read from the mount, so the two cannot drift
        apart."""
        from pathlib import Path as _Path
        repository = _Path(__file__).resolve().parents[3]
        compose = (repository / "examples/odoo/deploy/single-node/docker-compose.yml").read_text(
            encoding="utf-8")
        mounts = [line.strip().lstrip("- ").split(":")[0] for line in compose.splitlines()
                  if f":{manifest.ADDONS_MOUNT}:" in line]
        # Several services mount it; one digest stands for them only while they name one tree.
        self.assertTrue(mounts)
        self.assertEqual(len(set(mounts)), 1, mounts)
        source = manifest.addons_source()
        self.assertEqual(source, (repository / "examples/odoo/deploy/single-node" / mounts[0]).resolve())
        self.assertTrue(source.is_dir(), source)

        # An addon beside the one this file used to name is another fixture.
        digest = manifest._tree_digest(source)
        sibling = source / "_sibling_probe_addon"
        sibling.mkdir()
        try:
            (sibling / "__manifest__.py").write_text("{'auto_install': True}\n")
            self.assertNotEqual(digest, manifest._tree_digest(source))
        finally:
            (sibling / "__manifest__.py").unlink()
            sibling.rmdir()
        self.assertEqual(digest, manifest._tree_digest(source))

    def test_a_refusal_names_the_input_that_changed(self):
        with self.assertRaises(manifest.ManifestError) as raised:
            manifest.verify_manifest(recorded(),
                                     inputs={**INPUTS, "addon_digest": "sha256:" + "9" * 64},
                                     baseline_guard_digest=GUARD)
        self.assertIn("addon_digest", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
