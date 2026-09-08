"""CLI wiring for optional typed source affordances."""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from web_operation_agent import cli
from web_operation_agent.attest import AttestationError, DeploymentConfig
from web_operation_agent.controlplane import ControlGatewayConfig
from web_operation_agent.session import TaskSpec


def _deployment(root: Path) -> DeploymentConfig:
    return DeploymentConfig(
        deployment_id="fixture-deployment",
        source_policy="fixture",
        source_root=(root / "source").resolve(),
        application_base_url="http://127.0.0.1:18080",
        application_container="app",
        expected_application_version="v1",
        application_version_probe=("version",),
        proxy_container="proxy",
        proxy_source=(root / "proxy.js").resolve(),
        gateway=ControlGatewayConfig(
            base_url="http://127.0.0.1:18096",
            key_file=(root / "control.key").resolve(),
            key_header="X-WOA-Control-Key",
        ),
    )


class SourceAffordanceArguments(unittest.TestCase):
    def test_default_is_off_and_all_supported_modes_parse(self):
        parser = cli.build_parser()
        default = parser.parse_args(
            ["--deployment", "deployment.json", "run", "--task", "task.json"])
        self.assertEqual(default.source_affordances, "off")
        for mode in ("off", "auto", "empty"):
            with self.subTest(mode=mode):
                args = parser.parse_args(
                    ["--deployment", "deployment.json", "run", "--task", "task.json",
                     "--source-affordances", mode])
                self.assertEqual(args.source_affordances, mode)

    def test_unknown_mode_is_rejected_by_the_parser(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(
                ["--deployment", "deployment.json", "run", "--task", "task.json",
                 "--source-affordances", "invented"])

    def test_deployment_json_is_required(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["run", "--task", "task.json"])

    def test_deployment_json_is_an_explicit_argument(self):
        args = cli.build_parser().parse_args(
            ["--deployment", "/srv/deployment/deployment.json",
             "run", "--task", "task.json"]
        )
        self.assertEqual(args.deployment, "/srv/deployment/deployment.json")


class BuildSessionWiring(unittest.TestCase):
    def _build(self, root: Path, *, arm: str, mode: str):
        deployment = _deployment(root)
        attestation = SimpleNamespace(
            source_binding="source-binding", binding_id="binding",
            source={"release": "release"}, application={"version": "version"},
            enforcement={"gateway_loaded_sha256": "a" * 64, "proxy_sha256": "b" * 64})
        source_map = object()
        session = object()
        browser = MagicMock(name="browser")
        meter = MagicMock(name="meter")
        session_class = MagicMock(return_value=session)
        load = MagicMock(return_value=source_map)
        attested = MagicMock(return_value=attestation)
        with (patch.object(cli, "attest", attested),
              patch.object(cli, "load_or_compile", load),
              patch.object(cli, "SpendMeter", return_value=meter),
              patch.object(cli, "ChatModel", return_value=MagicMock(name="chat")),
              patch.object(cli, "SessionLedger", return_value=MagicMock(name="ledger")),
              patch.object(cli, "ThinBrowserClient", return_value=browser),
              patch.object(cli, "ControlPlaneClient", return_value=MagicMock(name="control")),
              patch.object(cli, "ServerAgentSession", session_class)):
            result = cli.build_session(
                task=TaskSpec("T", "inspect"), arm=arm, deployment=deployment,
                model=None, batch_id="batch", run_id="run",
                step_budget=4, wall_budget=30, oracle_path=None, ledger_dir=root,
                source_affordances=mode)
        return result, session_class, load, source_map, attested, deployment

    def test_missing_proxy_evidence_stops_before_a_browser_can_start(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                cli, "ThinBrowserClient") as browser:
            root = Path(tmp)
            deployment = _deployment(root)
            with self.assertRaisesRegex(AttestationError, "control proxy evidence"):
                cli.build_session(
                    task=TaskSpec("T", "inspect"), arm="U", deployment=deployment,
                    model=None, batch_id="batch", run_id="run",
                    step_budget=4, wall_budget=30, oracle_path=None,
                    ledger_dir=Path(tmp), source_affordances="off"
                )
        browser.assert_not_called()

    def test_build_session_forwards_the_typed_deployment_to_attestation(self):
        with tempfile.TemporaryDirectory() as tmp:
            *_, attested, deployment = self._build(Path(tmp), arm="U", mode="off")
        self.assertIs(attested.call_args.kwargs["deployment"], deployment)

    def test_run_command_loads_and_forwards_the_deployment(self):
        args = cli.build_parser().parse_args(
            ["--deployment", "/srv/deployment/deployment.json",
             "run", "--task", "task.json"]
        )
        deployment = _deployment(Path("/srv/deployment"))
        built = MagicMock(side_effect=AttestationError("stop after argument capture"))
        with (patch.object(cli, "_task_from", return_value=TaskSpec("T", "inspect")),
              patch.object(cli, "_deployment", return_value=deployment),
              patch.object(cli, "build_session", built), redirect_stdout(io.StringIO())):
            self.assertEqual(cli.cmd_run(args), 3)
        self.assertIs(built.call_args.kwargs["deployment"], deployment)

    def test_auto_loads_and_passes_a_source_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, session_class, load, source_map, _, deployment = self._build(
                Path(tmp), arm="S", mode="auto")
        self.assertIs(result[0], session_class.return_value)
        load.assert_called_once_with(
            "fixture", deployment.source_root, "source-binding")
        kwargs = session_class.call_args.kwargs
        self.assertIs(kwargs["source_map"], source_map)
        self.assertTrue(kwargs["plan_from_page"])
        self.assertFalse(kwargs["source_contract"])
        self.assertEqual(kwargs["source_affordance_mode"], "auto")

    def test_empty_control_needs_no_source_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, session_class, load, _, _, _ = self._build(
                Path(tmp), arm="P", mode="empty")
        load.assert_not_called()
        kwargs = session_class.call_args.kwargs
        self.assertIsNone(kwargs["source_map"])
        self.assertTrue(kwargs["plan_from_page"])
        self.assertFalse(kwargs["source_contract"])
        self.assertEqual(kwargs["source_affordance_mode"], "empty")

    def test_off_keeps_source_backed_contract_planning(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, session_class, load, source_map, _, deployment = self._build(
                Path(tmp), arm="S", mode="off"
            )
        load.assert_called_once_with(
            "fixture", deployment.source_root, "source-binding")
        kwargs = session_class.call_args.kwargs
        self.assertIs(kwargs["source_map"], source_map)
        self.assertFalse(kwargs["plan_from_page"])
        self.assertTrue(kwargs["source_contract"])
        self.assertEqual(kwargs["source_affordance_mode"], "off")

    def test_auto_is_rejected_when_the_arm_has_no_source_map(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(cli, "attest") as attested:
            with self.assertRaisesRegex(ValueError, "requires arm S and a SourceMap"):
                cli.build_session(
                    task=TaskSpec("T", "inspect"), arm="P",
                    deployment=_deployment(Path(tmp)), model=None,
                    batch_id="batch", run_id="run",
                    step_budget=4, wall_budget=30, oracle_path=None,
                    ledger_dir=Path(tmp), source_affordances="auto")
        attested.assert_not_called()

    def test_empty_is_rejected_outside_the_page_only_arm(self):
        for arm in ("U", "S", "O"):
            with self.subTest(arm=arm), tempfile.TemporaryDirectory() as tmp:
                with patch.object(cli, "attest") as attested:
                    with self.assertRaisesRegex(ValueError, "empty requires arm P"):
                        cli.build_session(
                            task=TaskSpec("T", "inspect"), arm=arm,
                            deployment=_deployment(Path(tmp)), model=None,
                            batch_id="batch", run_id="run",
                            step_budget=4, wall_budget=30, oracle_path=None,
                            ledger_dir=Path(tmp), source_affordances="empty")
                attested.assert_not_called()

    def test_runtime_lookup_cannot_be_combined_with_affordance_modes(self):
        for arm, mode in (("S", "auto"), ("P", "empty")):
            with self.subTest(arm=arm, mode=mode), tempfile.TemporaryDirectory() as tmp:
                with patch.object(cli, "attest") as attested:
                    with self.assertRaisesRegex(ValueError, "cannot be combined"):
                        cli.build_session(
                            task=TaskSpec("T", "inspect"), arm=arm,
                            deployment=_deployment(Path(tmp)), model=None,
                            batch_id="batch", run_id="run",
                            step_budget=4, wall_budget=30, oracle_path=None,
                            ledger_dir=Path(tmp), runtime_source=True,
                            source_affordances=mode)
                attested.assert_not_called()


if __name__ == "__main__":
    unittest.main()
