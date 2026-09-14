import json
import tempfile
import unittest
from pathlib import Path

from evals.odoo import equivalence


def _fake_core(root: Path, *, late_registration: bool = False, extra_method: str = "") -> Path:
    (root / "dist" / "esm").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"name": "@page-agent/core",
                                                   "version": "0.0.0"}))
    later = ("\n\tregister(tool) { this.tools.set(tool.name, tool); "
             "Object.assign(this.tools, this.config.customTools) }" if late_registration else "")
    (root / "dist" / "esm" / "page-agent-core.js").write_text(
        "var PageAgentCore = class extends EventTarget {\n"
        "\tconstructor(config) {\n\t\tsuper();\n\t\tthis.config = { ...config };\n"
        "\t\tif (this.config.customTools) for (const [n, t] of "
        "Object.entries(this.config.customTools)) { this.tools.set(n, t); }\n\t}"
        + later + "\n};\n")
    (root / "dist" / "esm" / "PageAgentCore.d.ts").write_text(
        "export declare class PageAgentCore {\n    readonly tools: typeof tools;\n"
        + extra_method + "}\n")
    return root


class PageAgentInspectionTests(unittest.TestCase):
    def test_constructor_only_tool_set_is_recognised(self):
        with tempfile.TemporaryDirectory() as directory:
            facts = equivalence.inspect_pageagent(_fake_core(Path(directory)))
        self.assertTrue(facts["custom_tools_read_only_in_constructor"])
        self.assertTrue(facts["tools_declared_readonly"])
        self.assertEqual(facts["public_tool_registration_methods"], [])

    def test_a_later_registration_path_is_not_mistaken_for_a_fixed_tool_set(self):
        with tempfile.TemporaryDirectory() as directory:
            facts = equivalence.inspect_pageagent(
                _fake_core(Path(directory), late_registration=True,
                           extra_method="    registerTool(tool: unknown): void;\n"))
        self.assertFalse(facts["custom_tools_read_only_in_constructor"])
        self.assertEqual(facts["public_tool_registration_methods"], ["registerTool("])

    def test_missing_distribution_is_an_input_error_not_a_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(equivalence.EquivalenceInputError):
                equivalence.inspect_pageagent(Path(directory))


class RulingTests(unittest.TestCase):
    def test_woa_discovery_is_prompt_selected_on_the_shipped_catalog(self):
        facts = equivalence.inspect_woa_discovery()
        self.assertTrue(facts["candidates_differ_by_prompt"])
        self.assertTrue(facts["candidates_are_strict_subset_of_catalog"])
        self.assertTrue(facts["session_selects_from_task_prompt"])

    @unittest.skipUnless(equivalence.PAGEAGENT_CORE.joinpath("package.json").is_file(),
                         "pinned PageAgentCore is not installed (npm ci in evals/pageagent)")
    def test_pinned_releases_exclude_b(self):
        ruling = equivalence.rule()
        self.assertEqual(ruling["verdict"], "B_EXCLUDED")
        self.assertIn("pageagent_tool_set_fixed_at_construction", ruling["reasons"])
        self.assertIn("woa_initial_catalog_is_prompt_selected", ruling["reasons"])
        self.assertEqual(ruling["pageagent"]["version"], "1.12.4")

    def test_ruling_admits_b_only_when_no_blocking_fact_holds(self):
        with tempfile.TemporaryDirectory() as directory:
            core = _fake_core(Path(directory), late_registration=True,
                              extra_method="    registerTool(tool: unknown): void;\n")
            ruling = equivalence.rule(core)
        self.assertNotIn("pageagent_tool_set_fixed_at_construction", ruling["reasons"])
        # The WOA side still excludes B on its own.
        self.assertEqual(ruling["verdict"], "B_EXCLUDED")


if __name__ == "__main__":
    unittest.main()
