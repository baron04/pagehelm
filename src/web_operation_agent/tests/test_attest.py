import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from web_operation_agent import attest as attestation
from web_operation_agent.controlplane import ControlGatewayConfig


def _deployment(root: Path, **changes) -> attestation.DeploymentConfig:
    values = {
        "deployment_id": "fixture-deployment",
        "source_policy": "fixture",
        "source_root": (root / "source").resolve(),
        "application_base_url": "http://127.0.0.1:3010",
        "application_container": "fixture-app",
        "expected_application_version": "fixture-v1",
        "application_version_probe": ("fixture-version", "--machine"),
        "proxy_container": "fixture-proxy",
        "proxy_source": (root / "proxy.js").resolve(),
        "gateway": ControlGatewayConfig(
            base_url="http://127.0.0.1:3099",
            key_file=(root / "control.key").resolve(),
            key_header="X-WOA-Fixture-Key",
            timeout_s=7,
        ),
    }
    values.update(changes)
    return attestation.DeploymentConfig(**values)


def _local_evidence(root: Path) -> None:
    (root / "source").mkdir()
    (root / "proxy.js").write_text("// deployment-owned fail-closed proxy\n")
    (root / "control.key").write_text("fixture-secret\n")
    (root / "control.key").chmod(0o600)


def _tree() -> MagicMock:
    tree = MagicMock()
    tree.available.return_value = True
    tree.files.return_value = ["descriptor.xml"]
    tree.repository_id = "fixture/app"
    tree.release = "fixture-release"
    tree.policy = {"allow_globs": ["**/*.xml"], "deny_globs": ["**/.env*"]}
    return tree


class DeploymentConfiguration(unittest.TestCase):
    def _document(self) -> dict:
        return {
            "schema_version": 1,
            "deployment_id": "staging-a",
            "source": {"policy": "ofbiz", "root": "source-checkout"},
            "application": {
                "base_url": "https://app.example.test/console",
                "container": "application",
                "expected_version": "release-1",
                "version_probe": ["print-version", "--short"],
            },
            "enforcement": {
                "proxy_container": "control-proxy",
                "proxy_source": "runtime/proxy.js",
                "gateway": {
                    "base_url": "https://gateway.example.test/control",
                    "key_file": "secrets/control.key",
                    "key_header": "X-Deployment-Control-Key",
                    "timeout_s": 9,
                },
            },
        }

    def test_relative_paths_are_resolved_beside_the_deployment_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config" / "deployment.json"
            path.parent.mkdir()
            path.write_text(json.dumps(self._document()))
            deployment = attestation.DeploymentConfig.from_file(path)

        self.assertEqual(deployment.deployment_id, "staging-a")
        self.assertEqual(deployment.source_root, (path.parent / "source-checkout").resolve())
        self.assertEqual(deployment.proxy_source, (path.parent / "runtime/proxy.js").resolve())
        self.assertEqual(
            deployment.gateway.key_file, (path.parent / "secrets/control.key").resolve())
        self.assertEqual(deployment.application_version_probe,
                         ("print-version", "--short"))

    def test_every_deployment_identity_field_is_required(self):
        removals = [
            ("deployment_id",),
            ("source", "root"),
            ("application", "base_url"),
            ("application", "version_probe"),
            ("enforcement", "proxy_source"),
            ("enforcement", "gateway", "base_url"),
            ("enforcement", "gateway", "key_file"),
            ("enforcement", "gateway", "key_header"),
        ]
        for keys in removals:
            with self.subTest(field=".".join(keys)), tempfile.TemporaryDirectory() as tmp:
                document = self._document()
                owner = document
                for key in keys[:-1]:
                    owner = owner[key]
                del owner[keys[-1]]
                with self.assertRaises(attestation.AttestationError):
                    attestation.DeploymentConfig.from_json(document, base_dir=tmp)

    def test_unknown_fields_are_refused_instead_of_ignored(self):
        document = self._document()
        document["enforcement"]["gateway"]["legacy_key"] = "silently unsafe"
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
                attestation.AttestationError, "unknown fields"):
            attestation.DeploymentConfig.from_json(document, base_dir=tmp)

    def test_plain_http_is_limited_to_loopback(self):
        document = self._document()
        document["application"]["base_url"] = "http://app.example.test"
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
                attestation.AttestationError, "loopback"):
            attestation.DeploymentConfig.from_json(document, base_dir=tmp)


class AttestationBoundary(unittest.TestCase):
    def test_a_missing_proxy_fails_before_source_docker_or_network_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            deployment = _deployment(Path(tmp))
            with (patch.object(attestation.SourceTree, "load") as load,
                  patch.object(attestation, "_docker") as docker,
                  patch.object(attestation.urllib.request, "urlopen") as urlopen,
                  self.assertRaisesRegex(attestation.AttestationError,
                                        "control proxy evidence")):
                attestation.attest(deployment=deployment, include_browser=False)
        load.assert_not_called()
        docker.assert_not_called()
        urlopen.assert_not_called()

    def test_a_missing_source_fails_before_policy_docker_or_network_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "proxy.js").write_text("// proxy")
            deployment = _deployment(root)
            with (patch.object(attestation.SourceTree, "load") as load,
                  patch.object(attestation, "_docker") as docker,
                  patch.object(attestation.urllib.request, "urlopen") as urlopen,
                  self.assertRaisesRegex(attestation.AttestationError, "source tree")):
                attestation.attest(deployment=deployment, include_browser=False)
        load.assert_not_called()
        docker.assert_not_called()
        urlopen.assert_not_called()

    def test_a_missing_key_fails_before_policy_docker_or_network_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source").mkdir()
            (root / "proxy.js").write_text("// proxy")
            deployment = _deployment(root)
            with (patch.object(attestation.SourceTree, "load") as load,
                  patch.object(attestation, "_docker") as docker,
                  patch.object(attestation.urllib.request, "urlopen") as urlopen,
                  self.assertRaisesRegex(attestation.AttestationError, "control key")):
                attestation.attest(deployment=deployment, include_browser=False)
        load.assert_not_called()
        docker.assert_not_called()
        urlopen.assert_not_called()

    def test_explicit_deployment_is_attested_and_configured_header_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _local_evidence(root)
            deployment = _deployment(root)
            expected_proxy_sha256 = hashlib.sha256(
                deployment.proxy_source.read_bytes()).hexdigest()
            tree = _tree()
            observed = {}

            def docker(*args, **_kwargs):
                calls = {
                    ("exec", "fixture-app", "fixture-version", "--machine"):
                        "fixture-v1",
                    ("inspect", "-f", "{{.State.Running}}", "fixture-app"): "true",
                    ("inspect", "-f", "{{.Config.Image}}", "fixture-app"):
                        "fixture/application:1",
                    ("inspect", "-f", "{{.State.Running}}", "fixture-proxy"): "true",
                    ("inspect", "-f",
                     "{{range .Mounts}}{{.Source}}:{{.Destination}}:{{.RW}} {{end}}",
                     "fixture-proxy"):
                        f"{deployment.proxy_source}:/srv/proxy.js:false",
                }
                return calls[args]

            def urlopen(request, *, timeout):
                observed["url"] = request.full_url
                observed["headers"] = {
                    key.casefold(): value for key, value in request.header_items()}
                observed["timeout"] = timeout
                return io.BytesIO(json.dumps({
                    "ok": True,
                    "gateway_loaded_sha256": expected_proxy_sha256,
                    "webapps": 3,
                }).encode())

            with (patch.object(attestation.SourceTree, "load", return_value=tree) as load,
                  patch.object(attestation, "_cached_surface_digest",
                               return_value="surface-digest"),
                  patch.object(attestation, "_docker", side_effect=docker),
                  patch.object(attestation.urllib.request, "urlopen",
                               side_effect=urlopen)):
                result = attestation.attest(
                    deployment=deployment, include_browser=False)

        load.assert_called_once_with("fixture", deployment.source_root)
        self.assertEqual(result.deployment_id, "fixture-deployment")
        self.assertEqual(result.enforcement["proxy_sha256"], expected_proxy_sha256)
        self.assertEqual(
            result.enforcement["gateway_loaded_sha256"], expected_proxy_sha256)
        self.assertEqual(result.application["base_url"], "http://127.0.0.1:3010")
        self.assertEqual(result.browser, {"skipped": True})
        self.assertEqual(observed["url"], "http://127.0.0.1:3099/admin/health")
        self.assertEqual(observed["headers"]["x-woa-fixture-key"], "fixture-secret")
        self.assertEqual(observed["timeout"], 7)

    def test_a_read_write_proxy_mount_is_refused_before_gateway_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _local_evidence(root)
            deployment = _deployment(root)
            tree = _tree()

            def docker(*args, **_kwargs):
                if args[:2] == ("exec", "fixture-app"):
                    return "fixture-v1"
                if args == ("inspect", "-f", "{{.Config.Image}}", "fixture-app"):
                    return "fixture/application:1"
                if args == ("inspect", "-f",
                            "{{range .Mounts}}{{.Source}}:{{.Destination}}:{{.RW}} {{end}}",
                            "fixture-proxy"):
                    return f"{deployment.proxy_source}:/srv/proxy.js:true"
                return "true"

            with (patch.object(attestation.SourceTree, "load", return_value=tree),
                  patch.object(attestation, "_cached_surface_digest", return_value="digest"),
                  patch.object(attestation, "_docker", side_effect=docker),
                  patch.object(attestation.urllib.request, "urlopen") as urlopen,
                  self.assertRaisesRegex(attestation.AttestationError, "read-only")):
                attestation.attest(deployment=deployment, include_browser=False)
        urlopen.assert_not_called()

    def test_gateway_digest_must_be_valid_and_match_local_proxy(self):
        for gateway_digest, message in (
                ("not-a-digest", "64 lowercase hex"),
                ("0" * 64, "does not match")):
            with self.subTest(gateway_digest=gateway_digest), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _local_evidence(root)
                deployment = _deployment(root)
                tree = _tree()

                def docker(*args, **_kwargs):
                    if args[:2] == ("exec", "fixture-app"):
                        return "fixture-v1"
                    if args == ("inspect", "-f", "{{.Config.Image}}", "fixture-app"):
                        return "fixture/application:1"
                    if args == ("inspect", "-f",
                                "{{range .Mounts}}{{.Source}}:{{.Destination}}:{{.RW}} {{end}}",
                                "fixture-proxy"):
                        return f"{deployment.proxy_source}:/srv/proxy.js:false"
                    return "true"

                response = io.BytesIO(json.dumps({
                    "ok": True, "gateway_loaded_sha256": gateway_digest}).encode())
                with (patch.object(attestation.SourceTree, "load", return_value=tree),
                      patch.object(attestation, "_cached_surface_digest", return_value="digest"),
                      patch.object(attestation, "_docker", side_effect=docker),
                      patch.object(attestation.urllib.request, "urlopen",
                                   return_value=response),
                      self.assertRaisesRegex(attestation.AttestationError, message)):
                    attestation.attest(deployment=deployment, include_browser=False)

    def test_application_version_probe_and_expectation_are_deployment_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _local_evidence(root)
            deployment = _deployment(
                root, expected_application_version="expected-build",
                application_version_probe=("custom", "version", "probe"))
            tree = _tree()

            def docker(*args, **_kwargs):
                if args == ("exec", "fixture-app", "custom", "version", "probe"):
                    return "different-build"
                if args == ("inspect", "-f", "{{.State.Running}}", "fixture-app"):
                    return "true"
                if args == ("inspect", "-f", "{{.Config.Image}}", "fixture-app"):
                    return "fixture/application:1"
                raise AssertionError(f"unexpected Docker call after version mismatch: {args}")

            with (patch.object(attestation.SourceTree, "load", return_value=tree),
                  patch.object(attestation, "_cached_surface_digest", return_value="digest"),
                  patch.object(attestation, "_docker", side_effect=docker),
                  patch.object(attestation.urllib.request, "urlopen") as urlopen,
                  self.assertRaisesRegex(attestation.AttestationError,
                                        "deployment expects 'expected-build'")):
                attestation.attest(deployment=deployment, include_browser=False)
        urlopen.assert_not_called()


class BindingIdentity(unittest.TestCase):
    def test_deployment_id_is_part_of_the_binding(self):
        common = dict(source={"release": "v1"}, application={}, enforcement={}, browser={})
        first = attestation.EnvironmentAttestation(deployment_id="a", **common)
        second = attestation.EnvironmentAttestation(deployment_id="b", **common)
        self.assertNotEqual(first.binding_id, second.binding_id)
        with self.assertRaisesRegex(attestation.AttestationError, "deployment_id"):
            attestation.require_binding(second, first.to_json())

    def test_serialized_attestation_requires_deployment_identity(self):
        with self.assertRaises(KeyError):
            attestation.EnvironmentAttestation.from_json({
                "source": {}, "application": {}, "enforcement": {}, "browser": {}})


if __name__ == "__main__":
    unittest.main()
