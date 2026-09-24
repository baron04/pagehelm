"""The provisioning step, checked without Docker, a network or an Odoo.

What these pin is the part that cannot be re-derived by reading the code later: the order the
steps run in, the refusals that keep a fixture password on loopback, and the fact that the
repository ships no third-party asset. The parts that genuinely need a container -- creating
a database, minting a key -- are exercised by actually running it, and their result is the
live gate.
"""
from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web_operation_agent.transport.page_controller import PINNED_PAGE_CONTROLLER, pinned_version
from web_operation_agent_odoo import deploy
from web_operation_agent_odoo.deploy.__main__ import build_parser, main


class TheFixtureIsLoopbackByConstruction(unittest.TestCase):
    """It carries a master password and types a well-known one. That is the whole rule."""

    def test_the_configured_base_url_is_loopback(self):
        deploy._require_loopback(deploy.BASE_URL)

    def test_a_reachable_host_is_refused_by_name(self):
        for host in ("http://odoo.example.com", "https://10.0.0.4:8069", "http://[::2]:8069"):
            with self.subTest(host=host):
                with self.assertRaisesRegex(deploy.ProvisioningError, "not loopback"):
                    deploy._require_loopback(host)

    def test_the_demo_password_is_odoo_s_own_and_is_not_treated_as_a_secret(self):
        # It ships in the upstream fixture. Pretending otherwise would be theatre; keeping it
        # on loopback is the control that actually matters.
        self.assertEqual((deploy.DEMO_LOGIN, deploy.DEMO_PASSWORD), ("admin", "admin"))


class OnlyFirstPartyBootstrapCodeIsCommitted(unittest.TestCase):
    def test_the_served_directory_tracks_only_the_first_party_loader(self):
        """Local modules ship here; third-party PageController remains provisioned."""
        first_party = {"loader.js", "page-controller-with-state.js", "page-facets.js",
                       "facet-bridge.js", "record-bridge.js", "page-record-route.js",
                       "page-blocker.js", "page-active-view.js"}
        tracked = sorted(path.name for path in deploy.SERVED_ASSETS.glob("*")
                         if path.name not in {".gitignore", *first_party})
        for name in tracked:
            with self.subTest(name=name):
                self.assertIn(name, PINNED_PAGE_CONTROLLER["served_files"],
                              "an unpinned file is being served from the addon")
        loader = deploy.SERVED_ASSETS / "loader.js"
        self.assertTrue(loader.is_file())
        self.assertIn("woa-assistant-bootstrap-shell", loader.read_text())
        self.assertIn("readPageFacets", (deploy.SERVED_ASSETS / "page-facets.js").read_text())
        self.assertIn("readPageRecordRoute", (deploy.SERVED_ASSETS /
            "page-controller-with-state.js").read_text())
        self.assertIn("./page-controller.js", (deploy.SERVED_ASSETS /
            "page-controller-with-state.js").read_text())
        self.assertTrue((deploy.SERVED_ASSETS / ".gitignore").is_file(),
                        "without this the fetched assets would be committed")

    def test_the_addon_declares_that_it_serves_files_it_does_not_contain(self):
        import ast
        manifest_text = (deploy.ADDONS_ROOT / "woa_runtime"
                         / "__manifest__.py").read_text()
        manifest = ast.literal_eval(manifest_text)
        self.assertIn("page_controller.json", manifest_text)
        self.assertEqual(manifest["license"], "Other OSI approved licence")

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the Odoo wrapper")
    def test_the_odoo_input_adapter_changes_only_existing_customer_records(self):
        # Load the actual first-party module against a tiny stand-in for the pinned
        # controller. A string assertion would not catch an event on a create form.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copyfile(deploy.SERVED_ASSETS / "page-controller-with-state.js",
                            root / "page-controller-with-state.js")
            shutil.copyfile(deploy.SERVED_ASSETS / "page-blocker.js",
                            root / "page-blocker.js")
            shutil.copyfile(deploy.SERVED_ASSETS / "page-active-view.js",
                            root / "page-active-view.js")
            (root / "package.json").write_text('{"type":"module"}', encoding="utf-8")
            (root / "page-controller.js").write_text("""
export class PageController {
  constructor(element, success = true) {
    this.selectorMap = [element]; this.success = success;
  }
  async inputText(index, text) {
    this.selectorMap[index].value = text;
    return {success: this.success};
  }
}
export const getElementByIndex = (map, index) => map[index];
""", encoding="utf-8")
            (root / "page-facets.js").write_text("""
export const readPageFacets = () => {};
export const readFacetCandidates = () => {};
export const applyPageFacet = () => {};
""", encoding="utf-8")
            (root / "page-record-route.js").write_text(
                "export const readPageRecordRoute = () => {};\n", encoding="utf-8")
            script = """
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {PageController} = await import(pathToFileURL(process.argv[1]).href);
async function check(path, success, connected, expected) {
  globalThis.location = {pathname: path};
  const events = [];
  const element = {value: '', isConnected: connected,
    dispatchEvent: event => events.push(event.type)};
  const outcome = await new PageController(element, success).inputText(0, 'new');
  assert.deepEqual(events, expected);
  assert.equal(outcome.success, success && connected);
}
await check('/odoo/customers/11', true, true, ['change']);
await check('/odoo/customers/new', true, true, []);
await check('/odoo/sales/new', true, true, []);
await check('/odoo/customers/11', false, true, []);
await check('/odoo/customers/11', true, false, []);
"""
            completed = subprocess.run(
                ["node", "--input-type=module", "-e", script,
                 str(root / "page-controller-with-state.js")],
                capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_the_compose_file_binds_loopback_only(self):
        compose = deploy.COMPOSE_FILE.read_text()
        self.assertIn('"127.0.0.1:8069:8069"', compose)
        self.assertNotIn('"8069:8069"', compose)

    def test_the_guard_fences_older_same_session_transactions(self):
        # The live row-lock gate proves the ordering. These source checks keep a future
        # addon edit from silently removing either half of that fence.
        source = (deploy.ADDONS_ROOT / "woa_runtime" / "models" /
                  "page_guard.py").read_text(encoding="utf-8")
        self.assertIn("SELECT pg_advisory_xact_lock_shared(%s)", source)
        self.assertIn("SELECT pg_advisory_xact_lock(%s)", source)
        self.assertLess(source.index("SELECT pg_advisory_xact_lock(%s)"),
                        source.index("expires_at = time.time() + ttl_s"))
        self.assertLess(source.index("SELECT pg_advisory_xact_lock_shared(%s)"),
                        source.index("if not _guarded_request():"))

    def test_the_addon_keeps_a_local_shell_but_runtime_uses_server_identity(self):
        import ast
        import xml.etree.ElementTree as ElementTree
        addon = deploy.ADDONS_ROOT / "woa_runtime"
        manifest = ast.literal_eval((addon / "__manifest__.py").read_text())
        controller = (addon / "controllers" / "session.py").read_text()
        self.assertEqual(manifest["assets"], {
            "web.assets_backend": ["woa_runtime/static/src/facet-bridge.js",
                               "woa_runtime/static/src/record-bridge.js"],
        })
        self.assertIn("toggleSearchItem", (addon / "static/src/facet-bridge.js").read_text())
        self.assertIn("FormController", (addon / "static/src/record-bridge.js").read_text())
        self.assertEqual(manifest["data"], ["views/assistant_entry.xml"])
        template = ElementTree.parse(addon / "views" / "assistant_entry.xml").find("template")
        self.assertEqual(template.get("inherit_id"), "web.webclient_bootstrap")
        script = template.find("xpath/script")
        self.assertEqual(script.attrib, {
            "type": "module",
            "src": "/woa_runtime/static/src/loader.js?v=1.4",
            "data-woa-runtime": "/assistant/v1/runtime/assistant.js",
            "data-woa-identity": "/woa/session-assertion",
            "data-woa-bootstrap": "/assistant/bootstrap",
        })
        self.assertIn('auth="user"', controller)
        self.assertIn("request.session.uid", controller)
        self.assertIn("WOA_SESSION_SECRET_FILE", controller)

    def test_the_manual_qualification_customer_is_seeded_idempotently(self):
        single_node = deploy.SINGLE_NODE_DEPLOY
        source = (single_node / "qualification-users.py").read_text()
        self.assertIn('"Azure Interior", "azure.interior@example.test"', source)
        self.assertIn('with_context(active_test=False)', source)
        self.assertIn('_crypt_context().verify(password, stored)', source)
        self.assertIn("synthetic customer fixture did not reach its exact postcondition", source)
        compose = (single_node / "docker-compose.yml").read_text()
        self.assertEqual(
            compose.count("image: ${WOA_ODOO_IMAGE:-woa-odoo-single-node-app}:"), 3,
            "runtime, setup and qualification must execute the same Odoo build")


class AFetchedAssetIsCheckedTwice(unittest.TestCase):
    """The tarball's integrity and each file's digest answer different questions."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.served = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _tarball(contents: dict[str, bytes]) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for name, body in contents.items():
                info = tarfile.TarInfo(name)
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
        return buffer.getvalue()

    def test_a_tarball_that_fails_its_integrity_is_never_written_out(self):
        archive = self._tarball({"package/dist/lib/page-controller.js": b"not the real thing"})
        with patch.object(deploy, "SERVED_ASSETS", self.served), \
                patch.object(deploy.urllib.request, "urlopen") as opener:
            opener.return_value.__enter__.return_value.read.return_value = archive
            with self.assertRaisesRegex(deploy.ProvisioningError, "integrity"):
                deploy.fetch_assets(force=True)
        self.assertEqual(sorted(self.served.iterdir()), [],
                         "a refused download must leave nothing behind")

    def test_an_already_correct_directory_is_not_downloaded_again(self):
        source = (Path(__file__).resolve().parents[5] / "evals" / "pageagent" / "node_modules"
                  / "@page-agent" / "page-controller")
        if not source.is_dir():
            self.skipTest("needs the pinned npm package; run npm ci in evals/pageagent")
        import shutil
        for name, entry in PINNED_PAGE_CONTROLLER["served_files"].items():
            shutil.copyfile(source / entry["tarball_path"].split("package/", 1)[1],
                            self.served / name)
        with patch.object(deploy, "SERVED_ASSETS", self.served), \
                patch.object(deploy.urllib.request, "urlopen") as opener:
            report = deploy.fetch_assets()
        opener.assert_not_called()
        self.assertFalse(report["fetched"])
        self.assertIn(pinned_version(), report["detail"])

    def test_a_verified_installed_package_avoids_a_registry_dependency(self):
        source = (Path(__file__).resolve().parents[5] / "evals" / "pageagent" / "node_modules"
                  / "@page-agent" / "page-controller")
        if not source.is_dir():
            self.skipTest("needs the pinned npm package; run npm ci in evals/pageagent")
        with patch.object(deploy, "SERVED_ASSETS", self.served), \
                patch.object(deploy, "LOCAL_PAGE_CONTROLLER", source), \
                patch.object(deploy.urllib.request, "urlopen") as opener:
            report = deploy.fetch_assets()
        opener.assert_not_called()
        self.assertTrue(report["fetched"])
        self.assertEqual(report["source"], "installed_package")
        self.assertTrue(deploy.verify_served_directory(self.served)["verified"])


class TheStepsRunInTheOnlyOrderThatWorks(unittest.TestCase):
    def test_the_snapshot_is_taken_after_the_credential_exists(self):
        """A snapshot taken first would delete the API key on the gate's first restore.

        `restore_snapshot` puts the whole database back. Provisioning that recorded the
        baseline before minting the key would produce a fixture that works exactly once.
        """
        order: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            patches = {
                "fetch_assets": {"step": "assets"},
                "start_stack": {"step": "up"},
                "create_database": {"step": "database"},
                "extract_source_tree": {"step": "source"},
                "mint_api_key": {"step": "api_key"},
                "capture_browser_session": {"step": "browser_session"},
                "record_baseline": {"step": "baseline"},
            }
            from web_operation_agent_odoo.deploy import __main__ as entry

            stack = []
            for name, result in patches.items():
                def recorder(_name=name, _result=result, **_kwargs):
                    order.append(_name)
                    return dict(_result)
                stack.append(patch.object(entry, name, recorder))
            stack.append(patch.object(entry, "_serves_the_pinned_asset", lambda: None))
            stack.append(patch.object(entry, "environment_lines", lambda **_k: []))
            stack.append(patch.object(entry, "image_release", lambda: "18.0-test"))
            for item in stack:
                item.start()
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["provision", "--state-dir", tmp,
                                           "--source-root", tmp + "/source"]), 0)
            finally:
                for item in stack:
                    item.stop()
        self.assertLess(order.index("mint_api_key"), order.index("record_baseline"))
        self.assertLess(order.index("create_database"), order.index("mint_api_key"))
        self.assertEqual(order[0], "fetch_assets")

    def test_every_step_is_runnable_on_its_own(self):
        """Each fails differently, and a person debugging one must not repeat the other six."""
        choices = build_parser()._actions[1].choices
        for name in ("assets", "up", "database", "source", "credentials", "baseline",
                     "env", "down", "provision"):
            with self.subTest(name=name):
                self.assertIn(name, choices)


class TheDemoOpensWhereAPersonCanSeeIt(unittest.TestCase):
    """A demo nobody can find is a demo that does not work.

    The panel remembers whether a viewer left it open, in `localStorage`, and defaults to a
    small unlabelled launcher in the corner. A demo launches a throwaway browser profile, so
    there is never anything to remember: every run started collapsed, and the first thing a
    person saw was a page that looked like nothing had loaded.
    """

    def test_the_demo_seeds_the_remembered_open_state(self):
        from web_operation_agent_odoo.demo import demo as demo_module

        source = Path(demo_module.__file__).read_text()
        self.assertIn("woa.panel.open.v1", source)
        self.assertIn("localStorage.setItem", source)

    def test_it_is_seeded_in_the_demo_and_nowhere_in_the_product(self):
        """Remembering the viewer's own choice is right in a real deployment."""
        import web_operation_agent

        product = Path(web_operation_agent.__file__).resolve().parent
        offenders = [path for path in product.rglob("*.js")
                     if "panel/" not in path.as_posix()
                     and "woa.panel.open.v1" in path.read_text(errors="ignore")]
        self.assertEqual(offenders, [])


class TheSourceTreeIsACopyOfWhatIsCompiled(unittest.TestCase):
    def test_the_modules_are_the_set_the_citations_need(self):
        """Not "all of them": G3 measured which set makes every packaged citation resolve."""
        self.assertIn("sale", deploy.SOURCE_MODULES)
        self.assertIn("account", deploy.SOURCE_MODULES)
        self.assertEqual(len(deploy.SOURCE_MODULES), 10)

    def test_warehouse_operations_are_installed_nowhere(self):
        """`stock` moves the fixture off the state the write cases were verified against.

        With it, 22 of 36 saleable demo products invoice on delivery and nothing has been
        delivered, so no order is left invoiceable and the gate's invoicing case has no
        target. The module is still compiled -- citations reach it -- but not installed.
        """
        self.assertNotIn("stock", deploy.BUSINESS_MODULES)
        self.assertIn("stock", deploy.SOURCE_MODULES)


if __name__ == "__main__":
    unittest.main()
