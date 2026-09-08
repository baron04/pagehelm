"""The public Twenty example is a thin, explicit product composition."""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from examples.twenty import demo
from web_operation_agent.identity import PrincipalContext
from web_operation_agent.live import LiveConversationRunner, LiveTurnOutcome
from web_operation_agent.product import (AgentTurnExecution, AgentTurnMessage,
                                         AgentTurnStatus)
from web_operation_agent.session import RunResult


BASE_ENV = {
    "WOA_TWENTY_BASE_URL": "http://127.0.0.1:3020",
    "WOA_TWENTY_STORAGE_STATE": "/tmp/twenty-storage.json",
    "WOA_TWENTY_OAUTH_TOKEN": "/tmp/twenty-token.json",
    "WOA_TWENTY_OAUTH_REGISTRATION": "/tmp/twenty-registration.json",
    "WOA_TWENTY_PAGE_CONTROLLER_MODULE": (
        "http://127.0.0.1:3020/woa/page-controller.js"),
    "WOA_AUDIT_LEDGER": "/tmp/twenty-audit.jsonl",
    "WOA_SPEND_LEDGER": "/tmp/twenty-spend.jsonl",
    "WOA_OUTPUT_DIR": "/tmp/twenty-output",
    "WOA_MUTATION_JOURNAL": "/tmp/twenty-mutations.sqlite3",
}


class TwentyDemoArgumentsTests(unittest.TestCase):
    def test_environment_supplies_required_locations_and_defaults_read_only(self):
        config = demo.parse_config([], BASE_ENV)

        self.assertEqual(config.base_url, "http://127.0.0.1:3020")
        self.assertEqual(config.entry_path, "/settings/profile")
        self.assertFalse(config.allow_mutations)
        self.assertFalse(config.headless)
        self.assertEqual(config.tasks, ())

    def test_cli_precedes_environment_and_mutations_require_the_exact_flag(self):
        env = {**BASE_ENV, "WOA_TWENTY_BASE_URL": "http://127.0.0.1:3020",
               "WOA_ALLOW_MUTATIONS": "true"}
        read_only = demo.parse_config([], env)
        enabled = demo.parse_config([
            "--base-url", "http://localhost:3021",
            "--page-controller-module", "http://localhost:3021/controller.js",
            "--allow-mutations",
        ], env)

        self.assertFalse(read_only.allow_mutations)
        self.assertTrue(enabled.allow_mutations)
        self.assertEqual(enabled.base_url, "http://localhost:3021")
        self.assertEqual(enabled.page_controller_module,
                         "http://localhost:3021/controller.js")

    def test_missing_required_locations_fail_in_argument_parsing(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            demo.parse_config([], {})
        self.assertEqual(caught.exception.code, 2)

    def test_help_does_not_require_application_configuration(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            demo.parse_config(["--help"], {})
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("WOA_TWENTY_BASE_URL", output.getvalue())

    def test_headless_write_session_is_rejected_before_startup(self):
        with self.assertRaisesRegex(ValueError, "visible browser"):
            demo.parse_config([
                "--headless", "--task", "create company Acme with domain acme.test",
                "--allow-mutations",
            ], BASE_ENV)

    def test_mutation_session_requires_a_dedicated_journal(self):
        env = {key: value for key, value in BASE_ENV.items()
               if key != "WOA_MUTATION_JOURNAL"}
        with self.assertRaisesRegex(ValueError, "durable mutation journal"):
            demo.parse_config(["--allow-mutations"], env)

        with self.assertRaisesRegex(ValueError, "dedicated path"):
            demo.parse_config([
                "--allow-mutations",
                "--mutation-journal", BASE_ENV["WOA_AUDIT_LEDGER"],
            ], BASE_ENV)

    def test_page_controller_must_share_the_application_origin(self):
        with self.assertRaisesRegex(ValueError, "Twenty origin"):
            demo.parse_config([
                "--page-controller-module", "https://cdn.example/controller.js",
            ], BASE_ENV)

    def test_same_origin_normalizes_default_http_and_https_ports(self):
        http = demo.parse_config([
            "--base-url", "http://localhost",
            "--page-controller-module", "http://localhost:80/controller.js",
        ], BASE_ENV)
        https = demo.parse_config([
            "--base-url", "https://crm.example.test:443",
            "--page-controller-module", "https://crm.example.test/controller.js",
        ], BASE_ENV)
        self.assertEqual(http.base_url, "http://localhost")
        self.assertEqual(https.base_url, "https://crm.example.test:443")

    def test_input_validation_is_local_and_does_not_contact_twenty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [root / name for name in ("storage.json", "token.json", "client.json")]
            for path in paths:
                path.write_text("{}", encoding="utf-8")
            config = demo.TwentyDemoConfig(
                base_url="http://127.0.0.1:3020",
                entry_path="/settings/profile",
                storage_state_path=paths[0],
                oauth_token_path=paths[1],
                oauth_registration_path=paths[2],
                page_controller_module="http://127.0.0.1:3020/controller.js",
                audit_ledger_path=root / "audit.jsonl",
                spend_ledger_path=root / "spend.jsonl",
                output_root=root / "output",
            )

            demo.validate_inputs(config)
            paths[1].unlink()
            with self.assertRaisesRegex(RuntimeError, "OAuth token"):
                demo.validate_inputs(config)

    def test_positive_numeric_arguments_reject_nonfinite_values(self):
        parser = demo.build_parser(BASE_ENV)
        for argv in (["--turn-timeout", "nan"], ["--keep-open-seconds", "-1"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as caught:
                parser.parse_args(argv)
            self.assertEqual(caught.exception.code, 2)

        invalid_env = {**BASE_ENV, "WOA_STEP_BUDGET": "nan"}
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            demo.parse_config([], invalid_env)
        self.assertEqual(caught.exception.code, 2)


class TwentyDemoCompositionTests(unittest.TestCase):
    def _config(self, root: Path, *, allow_mutations: bool = False):
        return demo.TwentyDemoConfig(
            base_url="http://127.0.0.1:3020",
            entry_path="/settings/profile",
            storage_state_path=root / "storage.json",
            oauth_token_path=root / "token.json",
            oauth_registration_path=root / "registration.json",
            page_controller_module="http://127.0.0.1:3020/controller.js",
            audit_ledger_path=root / "audit.jsonl",
            spend_ledger_path=root / "spend.jsonl",
            output_root=root / "output",
            mutation_journal_path=(root / "mutations.sqlite3"
                                   if allow_mutations else None),
            allow_mutations=allow_mutations,
        )

    def test_read_only_adapter_and_turn_policy_expose_no_create_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            adapter = demo.build_adapter(config)
            turn = demo.build_turn_config(config)

        self.assertEqual(adapter.business_tools.tool_names, frozenset({"query_companies"}))
        self.assertFalse(turn.allow_business_mutations)
        self.assertTrue(turn.require_identity)
        self.assertTrue({"click", "fill", "select", "submit"} <= turn.disabled_actions)

    def test_exact_opt_in_enables_create_skill_and_turn_mutations_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp), allow_mutations=True)
            adapter = demo.build_adapter(config)
            turn = demo.build_turn_config(config)

            self.assertTrue(config.mutation_journal_path.is_file())
            self.assertEqual(
                config.mutation_journal_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                adapter.business_tools._mutation_journal.path,
                config.mutation_journal_path,
            )

        self.assertEqual(
            adapter.business_tools.tool_names,
            frozenset({"query_companies", "create_company"}),
        )
        self.assertTrue(turn.allow_business_mutations)
        self.assertIn("fresh confirmation", turn.supported_scope)

    def test_one_executor_is_reused_by_the_conversation_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            adapter = demo.build_adapter(config)
            channel = object()
            chat = object()
            conversation = demo.build_conversation(
                channel, config=config, adapter=adapter, chat=chat,
                run_prefix="twenty-public-test",
            )

        self.assertIsInstance(conversation, LiveConversationRunner)
        self.assertIs(conversation.channel, channel)
        self.assertIs(conversation.executor.chat, chat)
        self.assertIs(conversation.executor.business_tools, adapter.business_tools)
        run_id = conversation.executor.run_id_factory(AgentTurnMessage(
            message_id="m1", text="list companies",
            page_url="http://127.0.0.1:3020/companies"))
        self.assertRegex(run_id, r"^twenty-public-test-[0-9a-f]{12}$")

    def test_model_environment_is_explicit_and_process_values_take_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_env = root / "model.env"
            model_env.write_text(
                "AGENT_OPENAI_BASE_URL=https://provider.example/v1\n"
                "AGENT_OPENAI_API_KEY=file-secret\n"
                "AGENT_OPENAI_MODEL_PRO=file-model\n"
                "UNRELATED_VALUE=not-forwarded\n",
                encoding="utf-8",
            )
            config = replace(self._config(root), model_env_path=model_env)
            values = demo.model_environment(config, {
                "AGENT_OPENAI_MODEL_PRO": "process-model",
                "ANOTHER_VALUE": "not-forwarded",
            })

        self.assertEqual(values, {
            "AGENT_OPENAI_BASE_URL": "https://provider.example/v1",
            "AGENT_OPENAI_API_KEY": "file-secret",
            "AGENT_OPENAI_MODEL_PRO": "process-model",
        })

    def test_live_host_uses_configured_controller_and_spa_navigation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            adapter = demo.build_adapter(config)
            holder = {}
            sentinel = object()
            principal = PrincipalContext(
                1, "twenty", "workspace-1", "workspace-1", "actor-1", "delegation-1",
                1_000_000, 1_060_000, ("companies:read",))
            with patch.object(demo, "LiveProductSession", return_value=sentinel) as session:
                live = demo.build_live_session(
                    config, adapter, object(), run_prefix="twenty-public-test",
                    conversation_holder=holder, principal=principal,
                    session_id="twenty-public-session",
                )
            self.assertIs(live, sentinel)
            options = session.call_args.kwargs
            self.assertEqual(options["allowed_origin"], "http://127.0.0.1:3020")
            self.assertEqual(
                options["page_controller_module"],
                "http://127.0.0.1:3020/controller.js",
            )
            self.assertEqual(options["navigation_mode"], "spa")
            self.assertEqual(options["session_id"], "twenty-public-session")
            self.assertIs(options["principal"], principal)
            conversation = options["runner_factory"](object())
            self.assertIs(holder["conversation"], conversation)

    def test_browser_cookie_replacement_closes_the_bound_agent_session(self):
        class Context:
            @staticmethod
            def storage_state():
                return {"cookies": []}

        class Adapter:
            binding = "same-binding"

            @classmethod
            def page_session_binding(cls, _state):
                return cls.binding

        class Live:
            closed = False

            @classmethod
            def close(cls):
                cls.closed = True

        demo._assert_browser_session_unchanged(
            Context(), adapter=Adapter(), live=Live(),
            expected_binding="same-binding")
        self.assertFalse(Live.closed)

        Adapter.binding = "replacement-binding"
        with self.assertRaisesRegex(RuntimeError, "session changed"):
            demo._assert_browser_session_unchanged(
                Context(), adapter=Adapter(), live=Live(),
                expected_binding="same-binding")
        self.assertTrue(Live.closed)

    def test_public_launcher_and_process_have_no_historical_fixture_dependency(self):
        source = (ROOT / "examples" / "twenty" / "demo.py").read_text().casefold()
        launcher = (ROOT / "demo.sh").read_text().casefold()
        for marker in ("lab/", "m7", "phase", ".q0-cache", "twenty_reset"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, source)
                self.assertNotIn(marker, launcher)
        for marker in ("twentydatabasereset", "business_state", "product_success",
                       "acceptance_passed"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker.casefold(), source)
        self.assertIn(
            "await window.__woa_runtime_ready__; return true",
            source,
        )

    def test_outcome_record_reports_only_the_agent_terminal_state(self):
        message = AgentTurnMessage(
            message_id="m1", text="list companies",
            page_url="http://127.0.0.1:3020/companies",
        )
        agent = RunResult(
            run_id="twenty-turn", task_ref="twenty-turn", arm="U",
            terminal_reason="agent_finished", steps_used=3, page_actions=0,
            agent_said="The current companies are listed above.",
        )
        execution = AgentTurnExecution(
            schema_version=1,
            run_id="twenty-turn",
            session_id="twenty-session",
            message=message,
            entry_observation_id="observation-1",
            entry_observation_digest="digest",
            status=AgentTurnStatus.FINISHED,
            terminal_reason="agent_finished",
            user_answer="The current companies are listed above.",
            agent=agent,
            business_result=None,
            answer_fallback_used=False,
            started_at=1.0,
            finished_at=2.0,
        )

        record = demo.summarize_outcome(LiveTurnOutcome(
            turn=1, result=execution, task=message.text))

        self.assertEqual(record["status"], "finished")
        self.assertEqual(record["terminal_reason"], "agent_finished")
        self.assertFalse(any("success" in key or "verified" in key for key in record))


if __name__ == "__main__":
    unittest.main()
