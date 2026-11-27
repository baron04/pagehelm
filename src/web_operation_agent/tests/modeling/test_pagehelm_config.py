"""Strict validation and provider/mode expansion tests for `pagehelm.toml` + `.env` (SDK-01)."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from web_operation_agent.modeling.client import (
    PROVIDER_DEFAULTS,
    expand_write_policy,
    load_pagehelm_toml,
    parse_pagehelm_toml,
    resolve_pagehelm_environment,
)
from web_operation_agent.modeling.provider import ProviderError


class PageHelmConfigTests(unittest.TestCase):
    def test_write_policy_keeps_form_prefill_on_and_supports_read_only_kill_switch(self):
        self.assertEqual(expand_write_policy(read_only=False), {
            "native_page_admission": "read-only-window",
            "native_writes": True,
            "native_submit": True,
            "native_write_preparation": False,
            "native_edit_prefill": True,
        })
        self.assertEqual(expand_write_policy(read_only=True), {
            "native_page_admission": "read-only-window",
            "native_writes": False,
            "native_submit": False,
            "native_write_preparation": False,
            "native_edit_prefill": True,
        })
        for bad in ("", "read-only", 1, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                expand_write_policy(read_only=bad)  # type: ignore[arg-type]

    def test_providers_derive_endpoints_and_default_models_from_one_line_env(self):
        cases = (
            ("deepseek", "DEEPSEEK_API_KEY", "https://api.deepseek.com", "deepseek-flash"),
            ("qwen", "DASHSCOPE_API_KEY", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.8-flash"),
            ("openai", "OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-6.1-sol"),
        )
        for provider, key_env, expected_url, expected_model in cases:
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / ".env").write_text(f"{key_env}=synthetic-secret-value\nWOA_READ_ONLY=true\n", encoding="utf-8")
                resolved = resolve_pagehelm_environment(root)
                self.assertEqual(resolved["AGENT_PROVIDER"], provider)
                self.assertEqual(resolved["AGENT_OPENAI_BASE_URL"], expected_url)
                self.assertEqual(resolved["AGENT_OPENAI_MODEL"], expected_model)
                self.assertEqual(resolved["AGENT_OPENAI_API_KEY"], "synthetic-secret-value")
                self.assertEqual(resolved["WOA_READ_ONLY"], "true")
                self.assertEqual(resolved["WOA_NATIVE_WRITES"], "false")
                self.assertEqual(resolved["write_options"], expand_write_policy(read_only=True))

    def test_toml_allows_model_base_url_and_limit_overrides_and_legacy_env_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("DASHSCOPE_API_KEY=synthetic-qwen-key\n", encoding="utf-8")
            (root / "pagehelm.toml").write_text(
                'provider = "qwen"\nmodel = "qwen3.8-max"\n'
                'base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1/"\n'
                "read_only = false\n"
                "daily_allowance_cny = 12.5\n"
                "daily_total_cap_cny = 40\n"
                "monthly_total_cap_cny = 500\n"
                "max_sessions = 120\n"
                "turn_timeout_s = 90\n",
                encoding="utf-8",
            )
            resolved = resolve_pagehelm_environment(root)
            self.assertEqual(resolved["AGENT_OPENAI_MODEL"], "qwen3.8-max")
            self.assertEqual(resolved["AGENT_OPENAI_BASE_URL"], "https://dashscope.aliyuncs.com/compatible-mode/v1")
            self.assertEqual(resolved["WOA_READ_ONLY"], "false")
            self.assertEqual(resolved["WOA_NATIVE_WRITES"], "true")
            self.assertEqual(resolved["daily_allowance_cny"], 12.5)
            self.assertEqual(resolved["daily_total_cap_cny"], 40.0)
            self.assertEqual(resolved["monthly_total_cap_cny"], 500.0)
            self.assertEqual(resolved["max_sessions"], 120)
            self.assertEqual(resolved["turn_timeout_s"], 90)

        # Backwards-compatible fallback when only legacy .env exists.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                "AGENT_OPENAI_BASE_URL=https://api.deepseek.com\n"
                "AGENT_OPENAI_API_KEY=synthetic-legacy\n"
                "AGENT_OPENAI_MODEL=deepseek-flash\n"
                "WOA_NATIVE_WRITES=false\n",
                encoding="utf-8",
            )
            resolved = resolve_pagehelm_environment(root)
            self.assertEqual(resolved["AGENT_OPENAI_MODEL"], "deepseek-flash")
            self.assertEqual(resolved["WOA_READ_ONLY"], "true")
            self.assertEqual(resolved["WOA_NATIVE_WRITES"], "false")

        # pagehelm.toml provider derives official URL/model even if legacy AGENT_OPENAI_* sits in .env.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                "AGENT_OPENAI_BASE_URL=https://legacy.invalid/v1\n"
                "AGENT_OPENAI_API_KEY=synthetic-legacy-key\n"
                "AGENT_OPENAI_MODEL=legacy-model\n",
                encoding="utf-8",
            )
            (root / "pagehelm.toml").write_text('provider = "deepseek"\n', encoding="utf-8")
            resolved = resolve_pagehelm_environment(root)
            self.assertEqual(resolved["AGENT_PROVIDER"], "deepseek")
            self.assertEqual(resolved["AGENT_OPENAI_BASE_URL"], "https://api.deepseek.com")
            self.assertEqual(resolved["AGENT_OPENAI_MODEL"], "deepseek-flash")
            self.assertEqual(resolved["AGENT_OPENAI_API_KEY"], "synthetic-legacy-key")
            self.assertEqual(resolved["WOA_READ_ONLY"], "false")

    def test_strict_validation_rejects_secrets_unknown_keys_bad_types_and_conflicts(self):
        for bad in (
            {"api_key": "synthetic-secret"},
            {"deepseek_api_key": "synthetic-secret"},
            {"unknown_setting": 123},
            {"provider": "anthropic"},
            {"provider": "deepseek", "self_hosted": True},
            {"provider": "qwen", "self_hosted": True},
            {"provider": "openai", "self_hosted": True},
            {"self_hosted": "yes"},
            {"read_only": "yes"},
            {"model": "   "},
            {"base_url": "http://remote.invalid/v1"},
            {"max_sessions": True},
            {"max_sessions": 0},
            {"turn_timeout_s": 1.5},
            {"daily_allowance_cny": False},
            {"daily_allowance_cny": -5},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_pagehelm_toml(bad)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                "DEEPSEEK_API_KEY=synthetic\nWOA_READ_ONLY=true\nWOA_NATIVE_WRITES=true\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "conflicts"):
                resolve_pagehelm_environment(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("AGENT_MODEL_SELF_HOSTED=false\n", encoding="utf-8")
            (root / "pagehelm.toml").write_text(
                'provider = "ollama"\nmodel = "qwen3:14b"\nself_hosted = true\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "conflicts"):
                resolve_pagehelm_environment(root)

        with self.assertRaises(ProviderError):
            resolve_pagehelm_environment(env_values={"AGENT_OPENAI_MODEL": "deepseek-flash"})
        self.assertEqual(
            set(PROVIDER_DEFAULTS),
            {"deepseek", "qwen", "openai", "ollama", "openai-compatible"},
        )
        with tempfile.TemporaryDirectory() as directory:
            toml = Path(directory) / "pagehelm.toml"
            toml.write_text('api_key = "nope"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"\.env"):
                load_pagehelm_toml(toml)

    def test_ollama_and_openai_compatible_self_hosted_presets_and_docker_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("", encoding="utf-8")
            (root / "pagehelm.toml").write_text(
                'provider = "ollama"\nmodel = "qwen3:14b"\n',
                encoding="utf-8",
            )
            resolved = resolve_pagehelm_environment(root)
            self.assertEqual(resolved["AGENT_PROVIDER"], "ollama")
            self.assertEqual(resolved["AGENT_OPENAI_BASE_URL"], "http://localhost:11434/v1")
            self.assertEqual(resolved["AGENT_OPENAI_MODEL"], "qwen3:14b")
            self.assertEqual(resolved["AGENT_OPENAI_API_KEY"], "")
            self.assertEqual(resolved["AGENT_MODEL_SELF_HOSTED"], "true")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("", encoding="utf-8")
            (root / "pagehelm.toml").write_text(
                'provider = "openai-compatible"\n'
                'base_url = "http://host.docker.internal:11434/v1"\n'
                'model = "Qwen/Qwen3-32B"\n'
                "self_hosted = true\n",
                encoding="utf-8",
            )
            resolved = resolve_pagehelm_environment(root)
            self.assertEqual(resolved["AGENT_PROVIDER"], "openai-compatible")
            self.assertEqual(resolved["AGENT_OPENAI_BASE_URL"], "http://host.docker.internal:11434/v1")
            self.assertEqual(resolved["AGENT_OPENAI_MODEL"], "Qwen/Qwen3-32B")
            self.assertEqual(resolved["AGENT_OPENAI_API_KEY"], "")
            self.assertEqual(resolved["AGENT_MODEL_SELF_HOSTED"], "true")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("AGENT_OPENAI_API_KEY=internal-token\n", encoding="utf-8")
            (root / "pagehelm.toml").write_text(
                'provider = "openai-compatible"\n'
                'base_url = "https://vllm.internal.example/v1"\n'
                'model = "Qwen/Qwen3-32B"\n'
                "self_hosted = true\n",
                encoding="utf-8",
            )
            resolved = resolve_pagehelm_environment(root)
            self.assertEqual(resolved["AGENT_OPENAI_API_KEY"], "internal-token")
            self.assertEqual(resolved["AGENT_MODEL_SELF_HOSTED"], "true")

        # Setting OPENAI_API_KEY alone still unambiguously infers "openai", never colliding with "openai-compatible".
        resolved_openai = resolve_pagehelm_environment(
            env_values={"OPENAI_API_KEY": "synthetic-openai-key"}
        )
        self.assertEqual(resolved_openai["AGENT_PROVIDER"], "openai")
        self.assertEqual(resolved_openai["AGENT_MODEL_SELF_HOSTED"], "false")

    def test_single_env_supports_model_host_overrides_ollama_and_host_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                "DEEPSEEK_API_KEY=synthetic-deepseek-key\n"
                "AGENT_OPENAI_MODEL=deepseek-chat\n"
                "AGENT_OPENAI_BASE_URL=https://proxy.example.com/v1/\n"
                "WOA_READ_ONLY=true\n"
                "WOA_DAILY_ALLOWANCE_CNY=15.5\n"
                "WOA_DAILY_TOTAL_CAP_CNY=60\n"
                "WOA_MONTHLY_TOTAL_CAP_CNY=800\n"
                "WOA_MAX_SESSIONS=150\n"
                "WOA_TURN_TIMEOUT_S=90\n",
                encoding="utf-8",
            )
            resolved = resolve_pagehelm_environment(root)
            self.assertEqual(resolved["AGENT_PROVIDER"], "deepseek")
            self.assertEqual(resolved["AGENT_OPENAI_API_KEY"], "synthetic-deepseek-key")
            self.assertEqual(resolved["AGENT_OPENAI_MODEL"], "deepseek-chat")
            self.assertEqual(resolved["AGENT_OPENAI_BASE_URL"], "https://proxy.example.com/v1")
            self.assertEqual(resolved["WOA_READ_ONLY"], "true")
            self.assertEqual(resolved["WOA_NATIVE_WRITES"], "false")
            self.assertEqual(resolved["daily_allowance_cny"], 15.5)
            self.assertEqual(resolved["daily_total_cap_cny"], 60.0)
            self.assertEqual(resolved["monthly_total_cap_cny"], 800.0)
            self.assertEqual(resolved["max_sessions"], 150)
            self.assertEqual(resolved["turn_timeout_s"], 90)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                "AGENT_PROVIDER=ollama\nAGENT_OPENAI_MODEL=qwen3:14b\n",
                encoding="utf-8",
            )
            resolved_ollama = resolve_pagehelm_environment(root)
            self.assertEqual(resolved_ollama["AGENT_PROVIDER"], "ollama")
            self.assertEqual(resolved_ollama["AGENT_OPENAI_BASE_URL"], "http://localhost:11434/v1")
            self.assertEqual(resolved_ollama["AGENT_OPENAI_MODEL"], "qwen3:14b")
            self.assertEqual(resolved_ollama["AGENT_MODEL_SELF_HOSTED"], "true")

        for bad_env in (
            {"DEEPSEEK_API_KEY": "k", "WOA_DAILY_ALLOWANCE_CNY": "-1"},
            {"DEEPSEEK_API_KEY": "k", "WOA_MAX_SESSIONS": "0"},
            {"DEEPSEEK_API_KEY": "k", "WOA_TURN_TIMEOUT_S": "1.5"},
        ):
            with self.subTest(bad_env=bad_env), self.assertRaises(ValueError):
                resolve_pagehelm_environment(env_values=bad_env)

    def test_self_hosted_bypass_provider_conflict_and_explicit_toml_loading(self):
        # Commercial hosts and hosted providers can never opt into zero-cost self_hosted pricing,
        # regardless of provider casing, provider alias, or generic base_url configuration.
        for bad_toml in (
            {"provider": "openai-compatible", "base_url": "https://api.openai.com/v1", "self_hosted": True},
            {"provider": "ollama", "base_url": "https://api.deepseek.com"},
        ):
            with self.subTest(bad_toml=bad_toml), self.assertRaises(ValueError):
                parse_pagehelm_toml(bad_toml)

        for bad_env in (
            {
                "AGENT_PROVIDER": "DeepSeek",
                "DEEPSEEK_API_KEY": "synthetic-key",
                "AGENT_MODEL_SELF_HOSTED": "true",
            },
            {
                "AGENT_PROVIDER": "ollama",
                "AGENT_OPENAI_BASE_URL": "https://api.openai.com/v1",
                "AGENT_OPENAI_MODEL": "gpt-6.1-sol",
            },
            {
                "AGENT_PROVIDER": "openai-compatible",
                "AGENT_OPENAI_BASE_URL": "https://api.deepseek.com",
                "AGENT_OPENAI_MODEL": "deepseek-flash",
                "AGENT_MODEL_SELF_HOSTED": "true",
            },
            {
                "AGENT_OPENAI_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "AGENT_OPENAI_API_KEY": "synthetic-key",
                "AGENT_OPENAI_MODEL": "qwen3.8-flash",
                "AGENT_MODEL_SELF_HOSTED": "true",
            },
        ):
            with self.subTest(bad_env=bad_env), self.assertRaisesRegex(ValueError, "self_hosted"):
                resolve_pagehelm_environment(env_values=bad_env)

        # Provider conflict between pagehelm.toml and AGENT_PROVIDER fails loudly.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pagehelm.toml").write_text('provider = "deepseek"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "conflicts with AGENT_PROVIDER"):
                resolve_pagehelm_environment(
                    root,
                    env_values={"AGENT_PROVIDER": "qwen", "DASHSCOPE_API_KEY": "synthetic-key"},
                )
            # Explicit toml_path fails loudly when missing.
            with self.assertRaises(FileNotFoundError):
                resolve_pagehelm_environment(
                    root,
                    env_values={"DEEPSEEK_API_KEY": "synthetic-key"},
                    toml_path=root / "does-not-exist.toml",
                )
            # load_toml=False ignores root/pagehelm.toml completely.
            resolved = resolve_pagehelm_environment(
                root,
                env_values={"AGENT_PROVIDER": "ollama", "AGENT_OPENAI_MODEL": "qwen3:14b"},
                load_toml=False,
            )
            self.assertEqual(resolved["AGENT_PROVIDER"], "ollama")
            self.assertEqual(resolved["AGENT_OPENAI_BASE_URL"], "http://localhost:11434/v1")


if __name__ == "__main__":
    unittest.main()
