"""The behaviour gold standard for `ServerAgentSession._run`, and its sensitivity proof.

`_run` is 932 lines. Every restructuring of it -- the constructor grouping, the handler
registry, the settlement split -- is a change that must provably preserve behaviour, and
"the suite is still green" does not prove that: the suite asserts the properties somebody
thought to write down, and a refactor breaks the ones nobody did.

So this file records behaviour instead of asserting it. For each scenario in
`golden_session.SCENARIOS` it captures what the run *did*: the tool surface offered at each
model call, the verbatim text of every tool result, the browser commands dispatched, the
whole audit ledger, and the terminal `RunResult`. The recording lives in
`golden/session_behavior.json` and is compared byte for byte.

Re-record deliberately, never reflexively:

    python -m web_operation_agent.tests.execution.test_session_golden --record

and read the diff. A diff that nobody can explain in one sentence is a defect, not a
re-record.

Write snapshots include Skill binding in the approval audit and the completed
mutation receipt in subsequent model context; dispatch counts and terminal states
remain part of the recorded contract.

`GoldenIsChangeSensitive` is the other half. A snapshot suite can rot into a snapshot of
nothing -- if the capture accidentally drops the fields that carry behaviour, it stays green
through any change. Each test there mutates one thing in the product, re-captures, and
requires the recording to move. Those tests failing means the gold standard has stopped
watching, which is worse than an ordinary failure.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from web_operation_agent.execution import loop as loop_module
from web_operation_agent.execution import session as session_module
from web_operation_agent.execution import context as context_module
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.support import golden_session
from web_operation_agent.tests.support.golden_session import SCENARIOS, tool_surface_compositions


GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "session_behavior.json"

#: Wall-clock and provider-timing fields. They are behaviour, but not *reproducible*
#: behaviour, and a gold standard that is red at random teaches a team to stop reading it.
_VOLATILE_LEDGER_FIELDS = frozenset({"t", "elapsed_s", "duration_ms", "latency_ms"})
_VOLATILE_RESULT_FIELDS = frozenset({"wall_seconds"})
_HEX32 = re.compile(r"\b[0-9a-f]{32}\b")
#: A mutation proposal is bound to a single-use nonce, so its digest is different on every
#: run by construction. Aliasing -- rather than erasing -- keeps what the digest is *for*:
#: the same alias in two records still proves the ledger bound the same proposal twice.
_NONCE_DERIVED_FIELDS = frozenset({"proposal_digest", "digest"})


def _alias_nonce_digests(value, aliases: dict[str, str], *, under_proposal: bool = False):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if (key in _NONCE_DERIVED_FIELDS and isinstance(item, str)
                    and re.fullmatch(r"[0-9a-f]{64}", item)
                    and (key != "digest" or under_proposal)):
                out[key] = aliases.setdefault(item, f"<proposal-digest:{len(aliases)}>")
            else:
                out[key] = _alias_nonce_digests(item, aliases,
                                                under_proposal=(key == "proposal"))
        return out
    if isinstance(value, list):
        return [_alias_nonce_digests(item, aliases, under_proposal=under_proposal)
                for item in value]
    return value


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable(value):
    """Replace any bare 32-hex run with a placeholder; uuid4 hexes are not behaviour."""
    if isinstance(value, str):
        return _HEX32.sub("<hex32>", value)
    if isinstance(value, dict):
        return {
            key: ("<unix-time>" if key == "observed_at" and isinstance(item, str)
                  and re.fullmatch(r"[0-9]{10}(?:\.[0-9]+)?", item) else _stable(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        # Tuples become lists: JSON has one sequence type, and a snapshot that compares
        # unequal after a round trip through its own file is a snapshot of nothing.
        return [_stable(item) for item in value]
    return value


def _ledger(ledger: SessionLedger) -> list[dict]:
    return [_stable({key: item for key, item in record.items()
                     if key not in _VOLATILE_LEDGER_FIELDS})
            for record in ledger.events]


def _result(result) -> dict:
    return _stable({key: value for key, value in result.to_json().items()
                    if key not in _VOLATILE_RESULT_FIELDS})


def _model_calls(chat) -> list[dict]:
    """One entry per provider call: the surface offered and what the last turn said.

    The system prompt is a digest because it is long and assembled from constants; the
    tool results are verbatim because they are the run's own words about what happened,
    and a change in them is exactly what this file exists to catch.
    """
    calls = []
    for offered, messages in zip(chat.offered_tools, chat.seen):
        system = next((message["content"] for message in messages
                       if message.get("role") == "system"), "")
        results = [{"name": message.get("name", ""),
                    "text": _stable(str(message.get("content", "")))}
                   for message in messages if message.get("role") == "tool"]
        opening = next((message["content"] for message in messages
                        if message.get("role") == "user"), "")
        calls.append({
            "tools_offered": offered,
            "system_digest": _digest(system),
            "system_chars": len(system),
            "opening": _stable(opening),
            "tool_results": results,
        })
    # Every call repeats the whole prior transcript, so keep each tool result once: the
    # last call carries them all, and the earlier ones only say when each appeared.
    for earlier, later in zip(calls, calls[1:]):
        shared = len(earlier["tool_results"])
        later["tool_results_new"] = later["tool_results"][shared:]
    for index, call in enumerate(calls):
        call["tool_results"] = (call["tool_results"] if index == 0
                                else call.pop("tool_results_new"))
        call.pop("opening" if index else "_", None)
    return calls


def capture_scenario(scenario) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        prepared = scenario.build(Path(tmp))
        try:
            outcome = {"result": _result(prepared.session.run())}
        except Exception as exc:
            # A defect that escapes `run()` is behaviour too: the run must still have
            # released its control run, and the ledger must still say what happened.
            outcome = {"raised": {"type": type(exc).__name__, "message": str(exc)}}
        record = {
            "why": scenario.why,
            **outcome,
            "model_calls": _model_calls(prepared.chat),
            "browser_commands": [
                {"action": action, "args": _stable(args),
                 "expect_navigation": expect_navigation}
                for action, args, expect_navigation in prepared.browser.commands],
            "audit": _ledger(prepared.session.ledger),
        }
        if prepared.extra is not None:
            record["doubles"] = _stable(prepared.extra())
        return _alias_nonce_digests(record, {})


def capture_tool_surfaces() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        return {name: session.tool_schemas()
                for name, session in tool_surface_compositions(Path(tmp)).items()}


def capture() -> dict:
    return {
        "schema_version": 1,
        "kind": "woa_session_behaviour_gold_standard",
        "tool_surfaces": capture_tool_surfaces(),
        "scenarios": {scenario.name: capture_scenario(scenario)
                      for scenario in SCENARIOS},
    }


def _serialise(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def record() -> None:
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(_serialise(capture()), encoding="utf-8")


def _stored() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _diff(recorded: dict, captured: dict, limit: int = 80) -> str:
    """A readable unified diff. Two whole snapshots printed side by side are unreadable."""
    lines = list(difflib.unified_diff(
        _serialise(recorded).splitlines(), _serialise(captured).splitlines(),
        fromfile="recorded", tofile="captured", lineterm=""))
    body = "\n".join(lines[:limit])
    return body + (f"\n... {len(lines) - limit} further diff lines"
                   if len(lines) > limit else "")


class SessionBehaviourIsRecorded(unittest.TestCase):
    """One test per scenario, so a red suite names the behaviour that moved."""

    @classmethod
    def setUpClass(cls):
        cls.stored = _stored()

    def test_the_recording_covers_every_scenario_and_nothing_else(self):
        self.assertEqual(sorted(self.stored["scenarios"]),
                         sorted(scenario.name for scenario in SCENARIOS),
                         "re-record after adding or removing a scenario")

    def test_the_tool_surface_of_every_composition_is_unchanged(self):
        captured = capture_tool_surfaces()
        if captured != self.stored["tool_surfaces"]:
            self.fail("the tool surface offered to the model changed:\n"
                      + _diff(self.stored["tool_surfaces"], captured))


def _scenario_test(scenario):
    def test(self):
        captured = capture_scenario(scenario)
        recorded = self.stored["scenarios"][scenario.name]
        if captured != recorded:
            self.fail(
                f"{scenario.name} behaves differently than the gold standard. Re-record "
                "only with a one-sentence reason for this diff:\n"
                + _diff(recorded, captured))
    test.__name__ = f"test_{scenario.name}"
    test.__doc__ = scenario.why
    return test


for _scenario in SCENARIOS:
    setattr(SessionBehaviourIsRecorded, f"test_{_scenario.name}", _scenario_test(_scenario))


class GoldenIsChangeSensitive(unittest.TestCase):
    """Prove the recording still watches. Each test breaks one thing and expects red.

    Without these, a capture that silently stopped recording tool results, audit events or
    terminal reasons would pass every scenario forever.
    """

    @classmethod
    def setUpClass(cls):
        cls.stored = _stored()

    def _scenario(self, name):
        return next(row for row in SCENARIOS if row.name == name)

    def assertDetects(self, scenario_name: str, what: str):
        captured = capture_scenario(self._scenario(scenario_name))
        if captured == self.stored["scenarios"][scenario_name]:
            self.fail(f"the gold standard did not notice {what}; it has stopped watching "
                      f"whatever {scenario_name} was recording")

    def test_a_changed_system_prompt_is_detected(self):
        # The prompt is read where the loop lives, so that is the module to disturb.
        original = loop_module.AGENT_SYSTEM
        loop_module.AGENT_SYSTEM = original + "\nOne more instruction."
        try:
            self.assertDetects("read_and_finish", "a changed system prompt")
        finally:
            loop_module.AGENT_SYSTEM = original

    def test_a_changed_observation_rendering_is_detected(self):
        original = context_module._render_observation

        def without_elements(obs, max_elements=context_module.RENDER_ELEMENTS):
            return original(obs, max_elements=0)

        context_module._render_observation = without_elements
        try:
            self.assertDetects("gateway_permitted_the_write", "a changed page rendering")
        finally:
            context_module._render_observation = original

    def test_a_missing_audit_event_is_detected(self):
        original = SessionLedger.append

        def swallow_observations(self, event, **fields):
            if event == "observation":
                return {"event": event}
            return original(self, event, **fields)

        SessionLedger.append = swallow_observations
        try:
            self.assertDetects("read_and_finish", "a dropped audit event")
        finally:
            SessionLedger.append = original

    def test_a_changed_tool_surface_is_detected(self):
        original = session_module.ServerAgentSession.tool_schemas

        def one_fewer_tool(self):
            return [tool for tool in original(self)
                    if tool["function"]["name"] != "wait_for"]

        session_module.ServerAgentSession.tool_schemas = one_fewer_tool
        try:
            self.assertNotEqual(
                capture_tool_surfaces(), self.stored["tool_surfaces"],
                "the tool-surface recording did not notice a withdrawn tool")
            self.assertDetects("read_and_finish", "a withdrawn tool at run time")
        finally:
            session_module.ServerAgentSession.tool_schemas = original

    def test_a_changed_terminal_reason_is_detected(self):
        original = golden_session.ScriptedBrowser.execute

        def refuse_everything(self, action, args=None, expect_navigation=False):
            receipt = original(self, action, args, expect_navigation)
            return type(receipt)(**{**receipt.to_json(), "ok": False,
                                    "error_code": "fixture_refused",
                                    "error_detail": "the fixture refused",
                                    "observation": receipt.observation})

        golden_session.ScriptedBrowser.execute = refuse_everything
        try:
            self.assertDetects("page_action_then_finish", "a failing browser command")
        finally:
            golden_session.ScriptedBrowser.execute = original

    def test_a_changed_write_settlement_is_detected(self):
        original = session_module.WriteWatch.write_outcome

        def never_refused(self, receipt, step, **kwargs):
            outcome = original(self, receipt, step, **kwargs)
            return {**outcome, "state": session_module.WRITE_NONE}

        session_module.WriteWatch.write_outcome = never_refused
        try:
            self.assertDetects("gateway_refused_the_write", "a changed write settlement")
        finally:
            session_module.WriteWatch.write_outcome = original


if __name__ == "__main__":
    if "--record" in sys.argv:
        record()
        print(f"recorded {GOLDEN}")
    else:
        unittest.main()
