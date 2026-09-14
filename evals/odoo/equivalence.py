"""Zero-model ruling on whether PageAgent with Skills/API can be made equivalent to WOA.

P7.4 admits a PageAgent Skill/API arm (B) only if both arms start from the same Skill
catalog, receive byte-identical Skill text and tool schemas only after the model itself
selects a Skill, and no harness reads the prompt to choose Skills -- all without modifying
PageAgentCore or moving WOA off its default product path. This module checks the two facts
that decide it, from the pinned PageAgent distribution and the shipped WOA code, and emits a
content-free ruling. It never builds or runs a B runner.

The ruling is structural. It does not say which engine is better, and a changed PageAgent or
WOA release must be re-ruled rather than assumed to keep the verdict.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import re
import tempfile
from pathlib import Path

RULING_VERSION = 1
REPOSITORY = Path(__file__).resolve().parents[2]
PAGEAGENT_CORE = REPOSITORY / "evals" / "pageagent" / "node_modules" / "@page-agent" / "core"

#: Two ordinary requests that name different business objects. Equal candidate sets would
#: mean discovery ignores the prompt; different sets mean an arm that does not run WOA's
#: matcher cannot start from WOA's catalog.
_DISCOVERY_PROMPTS = ("客户 Halvorsen 的邮箱是什么？", "报价单 S00012 的金额是多少？")


class EquivalenceInputError(RuntimeError):
    """A required pinned artifact is missing or has an unexpected shape."""


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _constructor_span(source: str, class_name: str) -> tuple[int, int]:
    """Return the character span of ``class_name``'s constructor body in bundled ESM."""
    start = source.find(f"var {class_name} = class")
    if start < 0:
        raise EquivalenceInputError(f"{class_name} class is absent from the distribution")
    constructor = source.find("constructor(", start)
    body = source.find("{", constructor)
    if constructor < 0 or body < 0:
        raise EquivalenceInputError(f"{class_name} constructor is absent")
    depth = 0
    for index in range(body, len(source)):
        character = source[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return body, index
    raise EquivalenceInputError(f"{class_name} constructor is not closed")


def inspect_pageagent(core_root: Path = PAGEAGENT_CORE) -> dict:
    """Where PageAgentCore reads ``customTools``, and whether it offers a later registration API."""
    try:
        package = json.loads((core_root / "package.json").read_text(encoding="utf-8"))
        bundle = (core_root / "dist" / "esm" / "page-agent-core.js").read_bytes()
        types = (core_root / "dist" / "esm" / "PageAgentCore.d.ts").read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise EquivalenceInputError(
            "the pinned PageAgentCore distribution is not installed; run npm ci in "
            "evals/pageagent") from exc
    source = bundle.decode("utf-8")
    start, end = _constructor_span(source, "PageAgentCore")
    uses = [match.start() for match in re.finditer(r"config\.customTools", source)]
    registration_methods = sorted(set(re.findall(
        r"\b(?:add|register|set|update|replace)Tools?\s*\(", types)))
    return {
        "package": package.get("name"),
        "version": package.get("version"),
        "bundle_digest": _sha256(bundle),
        "types_digest": _sha256(types.encode("utf-8")),
        "custom_tools_reads": len(uses),
        "custom_tools_read_only_in_constructor": bool(uses) and all(
            start <= position <= end for position in uses),
        "tools_declared_readonly": bool(re.search(r"readonly\s+tools\s*:", types)),
        "public_tool_registration_methods": registration_methods,
    }


def inspect_woa_discovery() -> dict:
    """Whether WOA's first Skill catalog for a turn is derived from the user's prompt."""
    from web_operation_agent import session as session_module
    from web_operation_agent_odoo.adapter import OdooApplicationAdapter

    with tempfile.TemporaryDirectory() as directory:
        key = Path(directory) / "key.json"
        key.write_text('{"database":"unused","login":"unused@example.test",'
                       '"api_key":"unused-unused-unused"}', encoding="utf-8")
        adapter = OdooApplicationAdapter(base_url="http://127.0.0.1:9", api_key_path=key)
        runtime = adapter.business_tools
        candidates = [
            sorted(skill.skill_id for skill in runtime.select(
                prompt, allow_mutations=False, limit=5).skills)
            for prompt in _DISCOVERY_PROMPTS]
        installed = sorted(skill.skill_id for skill in runtime.capability_skills(
            allow_mutations=False))
    constructor = inspect.getsource(session_module.ServerAgentSession.__init__)
    return {
        "catalog_digest": "sha256:" + runtime.catalog_sha256,
        "installed_read_skills": len(installed),
        "candidates_per_prompt": [len(item) for item in candidates],
        "candidates_differ_by_prompt": candidates[0] != candidates[1],
        "candidates_are_strict_subset_of_catalog": all(
            set(item) < set(installed) for item in candidates),
        "session_selects_from_task_prompt": "business_tools.select(" in constructor
        and "task.prompt" in constructor,
        "session_auto_loads_unique_read_skill": "business_skill_auto_loaded" in constructor,
        "session_source_digest": _sha256(constructor.encode("utf-8")),
    }


def rule(core_root: Path = PAGEAGENT_CORE) -> dict:
    """Return the B/C ruling. B is admitted only if every equivalence requirement holds."""
    pageagent = inspect_pageagent(core_root)
    woa = inspect_woa_discovery()
    reasons = []
    if (pageagent["custom_tools_read_only_in_constructor"]
            and pageagent["tools_declared_readonly"]
            and not pageagent["public_tool_registration_methods"]):
        # A tool revealed only after load_skill would have to be added to a running agent;
        # PageAgentCore takes its tool set once, at construction. Registering every schema
        # up front instead shows the model tools WOA reveals only after a Skill is loaded.
        reasons.append("pageagent_tool_set_fixed_at_construction")
    if (woa["candidates_differ_by_prompt"] and woa["candidates_are_strict_subset_of_catalog"]
            and woa["session_selects_from_task_prompt"]):
        # WOA's first catalog is chosen from the prompt on its default path. Giving B that
        # catalog requires the harness to read the prompt; giving B the full catalog makes
        # the starting catalogs differ; widening WOA moves it off its default path.
        reasons.append("woa_initial_catalog_is_prompt_selected")
    if woa["session_auto_loads_unique_read_skill"]:
        reasons.append("woa_may_deliver_skill_text_without_model_selection")
    return {
        "artifact_version": RULING_VERSION,
        "kind": "p74_bc_equivalence_ruling",
        "verdict": "B_EXCLUDED" if reasons else "B_ELIGIBLE_FOR_QUALIFICATION",
        "reasons": reasons,
        "pageagent": pageagent,
        "woa": woa,
        "consequence": ("P7.4 keeps only A pageagent_browser_only and C woa_product; no B "
                        "runner is built" if reasons else
                        "a B runner may be qualified against the equivalence contract"),
    }


def main() -> int:
    print(json.dumps(rule(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
