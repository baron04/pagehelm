"""Strict validation and provider/mode expansion tests for `pagehelm.toml` + `.env` (SDK-01)."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from web_operation_agent.modeling.client import (
    OPERATION_MODES,
    PROVIDER_DEFAULTS,
    expand_operation_mode,
    load_pagehelm_toml,
    parse_pagehelm_toml,
    resolve_pagehelm_environment,
)
from web_operation_agent.modeling.provider import ProviderError


class PageHelmConfigTests(unittest.TestCase):
    def test_operation_modes_expand_matching_security_invariants(self):
        self.assertEqual(OPERATION_MODES, ("read-only", "assisted-draft", "verified-operate"))
        self.assertEqual(expand_operation_mode("read-only"), {
            "native_page_admission": "read-only-window",
            "native_writes": False,
            "native_submit": False,
            "native_write_preparation": False,
            "native_edit_prefill": False,
        })
        self.assertEqual(expand_operation_mode("assisted-draft"), {
            "native_page_admission": "read-only-window",
            "native_writes": False,
            "native_submit": False,
            "native_write_preparation": True,
            "native_edit_prefill": True,
        })
        self.assertEqual(expand_operation_mode("verified-operate"), {
            "native_page_admission": "read-only-window",
            "native_writes": True,
            "native_submit": True,
            "native_write_preparation": False,
            "native_edit_prefill": True,
        })
        for bad in ("", "write-all", 1, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                expand_operation_mode(bad)  # type: ignore[arg-type]

    def test_providers_derive_endpoints_and_default_models_from_one_line_env(self):
        cases = (
            ("deepseek", "DEEPSEEK_API_KEY", "https://api.deepseek.com", "deepseek-flash"),
            ("qwen", "DASHSCOPE_API_KEY", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.8-flash"),
            ("openai", "OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-6.1-sol"),
        )
        for provider, key_env, expected_url, expected_model in cases:
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / ".env").write_text(f"{key_env}=synthetic-secret-value\n", encoding="utf-8")
                (root / "pagehelm.toml").write_text(
                    f'provider = "{provider}"\nmode = "assisted-draft"\n', encoding="utf-8"
                )
                resolved = resolve_pagehelm_environment(root)
                self.assertEqual(resolved["AGENT_PROVIDER"], provider)
                self.assertEqual(resolved["AGENT_OPENAI_BASE_URL"], expected_url)
                self.assertEqual(resolved["AGENT_OPENAI_MODEL"], expected_model)
                self.assertEqual(resolved["AGENT_OPENAI_API_KEY"], "synthetic-secret-value")
                self.assertEqual(resolved["WOA_MODE"], "assisted-draft")
                self.assertEqual(resolved["WOA_NATIVE_WRITES"], "false")
                self.assertEqual(resolved["mode_options"], expand_operation_mode("assisted-draft"))

    def test_toml_allows_model_base_url_and_limit_overrides_and_legacy_env_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("DASHSCOPE_API_KEY=synthetic-qwen-key\n", encoding="utf-8")
            (root / "pagehelm.toml").write_text(
                'provider = "qwen"\nmodel = "qwen3.8-max"\n'
                'base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1/"\n'
                'mode = "verified-operate"\n'
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
                "WOA_NATIVE_WRITES=true\n",
                encoding="utf-8",
            )
            resolved = resolve_pagehelm_environment(root)
            self.assertEqual(resolved["AGENT_OPENAI_MODEL"], "deepseek-flash")
            self.assertEqual(resolved["WOA_MODE"], "verified-operate")
            self.assertEqual(resolved["WOA_NATIVE_WRITES"], "true")

        # pagehelm.toml provider derives official URL/model even if legacy AGENT_OPENAI_* sits in .env.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                "AGENT_OPENAI_BASE_URL=https://legacy.invalid/v1\n"
                "AGENT_OPENAI_API_KEY=synthetic-legacy-key\n"
                "AGENT_OPENAI_MODEL=legacy-model\n",
                encoding="utf-8",
            )
            (root / "pagehelm.toml").write_text('provider = "deepseek"\nmode = "verified-operate"\n', encoding="utf-8")
            resolved = resolve_pagehelm_environment(root)
            self.assertEqual(resolved["AGENT_PROVIDER"], "deepseek")
            self.assertEqual(resolved["AGENT_OPENAI_BASE_URL"], "https://api.deepseek.com")
            self.assertEqual(resolved["AGENT_OPENAI_MODEL"], "deepseek-flash")
            self.assertEqual(resolved["AGENT_OPENAI_API_KEY"], "synthetic-legacy-key")

    def test_strict_validation_rejects_secrets_unknown_keys_bad_types_and_conflicts(self):
        for bad in (
            {"api_key": "synthetic-secret"},
            {"deepseek_api_key": "synthetic-secret"},
            {"unknown_setting": 123},
            {"provider": "anthropic"},
            {"mode": "unsafe"},
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
                "DEEPSEEK_API_KEY=synthetic\nWOA_NATIVE_WRITES=true\n", encoding="utf-8"
            )
            (root / "pagehelm.toml").write_text('provider = "deepseek"\nmode = "read-only"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "conflicts"):
                resolve_pagehelm_environment(root)

        with self.assertRaises(ProviderError):
            resolve_pagehelm_environment(env_values={"AGENT_OPENAI_MODEL": "deepseek-flash"})
        self.assertEqual(set(PROVIDER_DEFAULTS), {"deepseek", "qwen", "openai"})
        with tempfile.TemporaryDirectory() as directory:
            toml = Path(directory) / "pagehelm.toml"
            toml.write_text('api_key = "nope"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"\.env"):
                load_pagehelm_toml(toml)


if __name__ == "__main__":
    unittest.main()
