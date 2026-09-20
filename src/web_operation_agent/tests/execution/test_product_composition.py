from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from web_operation_agent.execution import product, turn
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.composition.descriptor import (
    APPLICATION_ADAPTER_VERSION,
    ApplicationDescriptor,
)
from web_operation_agent.composition.ports import (
    AnonymousIdentityPort,
    ApplicationPorts,
    ConservativeMutationReconciler,
    DeclaredDataClassifier,
    ResultPostconditionVerifier,
)
from web_operation_agent.answer.answering import StructuredQueryAnswer
from web_operation_agent.business.runtime import (BusinessSelectionOption, BusinessSkill,
                                          BusinessToolPermissionError, BusinessToolResult,
                                          BusinessToolRuntime)
from web_operation_agent.state.conversation import ConversationState
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.answer.evidence import Origin, RecordEvidence
from web_operation_agent.state.identity import PrincipalContext
from web_operation_agent.modeling.client import ProviderError
from web_operation_agent.business.mutation import SQLiteMutationJournal
from web_operation_agent.execution.session import NO_LAB
from web_operation_agent.execution.product import (
    AgentTurnConfig,
    AgentTurnError,
    AgentTurnErrorCode,
    AgentTurnExecutor,
    AgentTurnMessage,
    AgentTurnStatus,
)
from web_operation_agent.transport import RemoteSessionClosed
from web_operation_agent.schemas import ApprovalDecision, ObservationEnvelope
from web_operation_agent.tests.support.business import (
    _Browser,
    _Chat,
    _Executor,
    _MutationExecutor,
    _observation,
    _skill,
    _write_skill,
)
from web_operation_agent.tests.support.route_fixture import (FIXTURE_APPLICATION_ID,
                                                     FIXTURE_ROUTE_PORT, fixture_route)
from web_operation_agent.tests.support.application import _InvoiceAdapter


from web_operation_agent.tests.execution.product_support import (
    _BrokenAfterRunChannel, _Channel, _ClosingAfterRunChannel, _PageChangingBrowser,
    _RemoteBrowser, _message, _principal,
)


class ApplicationEgressVocabulary(unittest.TestCase):
    """An adapter that declares session parameters must find them at the provider boundary.

    Composition, not runtime: a deployment that forgot to widen the egress policy would
    otherwise discover the gap by sending the secret. The check is scoped to adapters that
    actually declare something, so an application with no session parameters of its own -- the
    common case -- composes exactly as before.
    """

    def _adapter(self, keys):
        adapter = _InvoiceAdapter()
        object.__setattr__(adapter.ports.routes, "session_handoff_query_keys",
                           frozenset(keys))
        return bind_application(adapter)

    def _executor(self, application, chat):
        return AgentTurnExecutor(
            channel=_Channel(),
            config=AgentTurnConfig(supported_scope="Read invoice data.",
                                   ledger_path=self.output / "session.jsonl"),
            chat=chat, application=application, control=ReadOnlyControl(),
            run_id_factory=lambda _message: "egress-vocabulary")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_adapter_declaring_nothing_composes_unchanged(self):
        chat = _Chat([])
        self.assertIsNotNone(self._executor(self._adapter(()), chat))

    def test_composition_fails_when_the_policy_has_not_been_told(self):
        from web_operation_agent.modeling.egress import ProviderEgressPolicy

        class _ChatWithEgress(_Chat):
            egress_policy = ProviderEgressPolicy()

        with self.assertRaisesRegex(ValueError, "session_id"):
            self._executor(self._adapter({"session_id"}), _ChatWithEgress([]))

    def test_composition_succeeds_once_the_policy_covers_the_declaration(self):
        from web_operation_agent.modeling.egress import ProviderEgressPolicy

        class _ChatWithEgress(_Chat):
            egress_policy = ProviderEgressPolicy(
                session_handoff_keys=frozenset({"session_id"}))

        self.assertIsNotNone(
            self._executor(self._adapter({"session_id"}), _ChatWithEgress([])))


class LabApparatusNeverReachesTheProduct(unittest.TestCase):
    """A deployed turn must not be able to name an experiment knob at all.

    `ServerAgentSession` takes lab settings as one `LabApparatus` value, because they exist
    only so the comparison harness can define its arms. `oracle_contract` injects a
    known-good plan -- hidden expected state AGENTS.md forbids in an agent's context --
    and `plan_from_page` and `terms_cache` define one arm's planning apparatus.

    `product.py` is the only place a deployed turn constructs a session, so the check is
    what it actually passes. Before the constructor grouping this had to enumerate four
    keyword names and hope nobody added a fifth; now the whole family is one keyword, and
    its absence is the assertion.
    """

    def _recorded_keywords(self) -> dict:
        recorded = {}
        real = turn.ServerAgentSession

        class _Recording(real):
            def __init__(self, **kwargs):
                recorded.update(kwargs)
                super().__init__(**kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query-skill.json")
            executor = AgentTurnExecutor(
                channel=_Channel(),
                config=AgentTurnConfig(
                    supported_scope="Read company data only.",
                    ledger_path=root / "session.jsonl",
                    plain_business_answer_min_page_actions=0,
                    disabled_actions=frozenset({"click", "fill", "select", "submit"}),
                ),
                chat=_Chat([("query_companies", {}),
                            (None, {"content": "done"})]),
                business_tools=BusinessToolRuntime((skill,), _Executor()),
                control=ReadOnlyControl(),
                run_id_factory=lambda message: "turn-lab-apparatus",
            )
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser), \
                    patch.object(turn, "ServerAgentSession", _Recording):
                # Only the composition matters here; whether this synthetic turn reaches a
                # terminal result is a different test's subject.
                try:
                    executor.run_once(_message())
                except product.AgentTurnError:
                    pass
        self.assertTrue(recorded, "the product turn did not construct a session")
        return recorded

    def test_the_product_turn_passes_no_lab_apparatus(self):
        recorded = self._recorded_keywords()
        self.assertNotIn("lab", recorded, "a lab apparatus reached a product turn")
        # And the default it therefore gets carries no experiment: no injected plan, no
        # page-only planning arm, no shared term cache.
        self.assertIsNone(NO_LAB.oracle_contract)
        self.assertFalse(NO_LAB.plan_from_page)
        self.assertIsNone(NO_LAB.terms_cache)

    def test_the_product_turn_compiles_no_contract_from_source(self):
        recorded = self._recorded_keywords()
        capability = recorded["source"]
        # Source planning is a deployment capability, not a default: a product turn compiles
        # no contract, and looks up no source, unless a composition explicitly supplies one.
        self.assertIs(capability.source_contract, False)
        self.assertIs(capability.runtime_source, False)


class EntryScopesReachTheProductTurn(unittest.TestCase):
    """A compiled SourceMap the product path never consults is worse than none.

    `resolve_source_affordances` needs the scopes the *entry page* actually exposes, and the
    adapter derives them from that page. For as long as the product turn assigned
    `entry_observation` directly, `served_webapps` stayed empty and every candidate in auto
    mode was silently zero — the mechanism worked in a directly composed session and did
    nothing in a deployment. This pins the wiring, not the affordance logic.
    """

    def test_the_turn_derives_served_scopes_from_its_entry_observation(self):
        from web_operation_agent.schemas import ElementRef, PageObservation

        recorded = {}
        real = turn.ServerAgentSession

        class _Recording(real):
            def adopt_entry(self, obs):
                result = super().adopt_entry(obs)
                recorded["served_webapps"] = set(self.served_webapps)
                recorded["entry_url"] = self.entry_observation.url
                return result

        class _EntryLinkBrowser(_RemoteBrowser):
            def observe(self):
                return PageObservation(
                    schema_version=1, epoch=1, url="http://app.test/companies",
                    title="Companies", landmarks=[], elements=[ElementRef(
                        "e1", "link", "Invoices", "a", 1,
                        {"href": "/ledger/do/findInvoices"})])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = _skill(root / "query-skill.json")
            executor = AgentTurnExecutor(
                channel=_Channel(),
                config=AgentTurnConfig(
                    supported_scope="Read company data only.",
                    ledger_path=root / "session.jsonl",
                    plain_business_answer_min_page_actions=0,
                    disabled_actions=frozenset({"click", "fill", "select", "submit"}),
                ),
                chat=_Chat([("query_companies", {}), (None, {"content": "done"})]),
                business_tools=BusinessToolRuntime((skill,), _Executor()),
                control=ReadOnlyControl(),
                run_id_factory=lambda message: "turn-entry-scopes",
            )
            with patch.object(turn, "RemoteBrowserClient", _EntryLinkBrowser), \
                    patch.object(turn, "ServerAgentSession", _Recording):
                try:
                    executor.run_once(_message())
                except product.AgentTurnError:
                    pass

        self.assertEqual(recorded.get("entry_url"), "http://app.test/companies")
        # No adapter is bound in this composition, so the scope set is empty on purpose --
        # what is asserted is that the derivation ran at all, which is the step that was
        # missing. The application-bound case is covered by the live Odoo gate's
        # `source_treatment_is_non_empty` check.
        self.assertIn("served_webapps", recorded)


class _FixtureGrammarAdapter:
    """A bound application whose route grammar is the core suite's synthetic one.

    A source-enabled session needs an application route port -- `lookup_source` licenses
    view-only routes, and something has to say which strings are routes at all. The port
    here is the fixture grammar (`/{scope}/do/{route}`), which is the grammar the fixture
    `SourceMap` was written against and is deliberately neither OFBiz's nor Odoo's.
    """

    _TOOL = {
        "type": "function",
        "function": {"name": "query_ledger",
                     "description": "Read ledger rows from the fixture application.",
                     "parameters": {"type": "object", "properties": {},
                                    "additionalProperties": False}},
    }

    class _Executor:
        def tool_schemas(self):
            return (_FixtureGrammarAdapter._TOOL,)

        def tool_effects(self):
            return {"query_ledger": "read"}

        def execute(self, name, args):
            route = fixture_route("greenhouse", "findTrays")
            return BusinessToolResult(
                content={"rows": [{"id": "row-1", "ui_path": route}]},
                audit={"operation": name, "count": 1},
                allowed_routes=(route,))

    def __init__(self):
        skill = BusinessSkill.from_bytes(json.dumps({
            "schema_version": 1, "skill_id": "fixture.ledger-query",
            "title": "Ledger lookup", "intent": "Read current ledger rows.",
            "triggers": ["ledger", "rows", "trays"],
            "instructions": ["Call query_ledger and trust its current result."],
            "tool_names": ["query_ledger"],
            "provenance": ["api:fixture@v0:/ledger#get"], "read_only": True,
        }, sort_keys=True).encode(), artifact_uri="memory://fixture/ledger.json")
        data = DeclaredDataClassifier((("query_ledger", ("business.ledger",)),))
        postconditions = ResultPostconditionVerifier(())
        self.business_tools = BusinessToolRuntime(
            (skill,), self._Executor(), application_id=FIXTURE_APPLICATION_ID,
            data_classifier=data, postcondition_verifier=postconditions,
            route_validator=FIXTURE_ROUTE_PORT)
        self.ports = ApplicationPorts(
            identity=AnonymousIdentityPort(), routes=FIXTURE_ROUTE_PORT,
            postconditions=postconditions,
            reconciliation=ConservativeMutationReconciler(), data=data)
        self.descriptor = ApplicationDescriptor(
            schema_version=APPLICATION_ADAPTER_VERSION,
            application_id=FIXTURE_APPLICATION_ID, display_name="Fixture Grammar",
            origin="https://fixture.test", navigation_mode="document",
            page_controller_module="https://fixture.test/assets/page-controller.js",
            catalog_sha256=self.business_tools.catalog_sha256)

    def health(self):
        return {"passed": True, "http_status": 200}


class _FixtureGrammarChat(_Chat):
    """The egress policy the assembly gate requires for this adapter's session vocabulary."""

    from web_operation_agent.modeling.egress import ProviderEgressPolicy as _Policy

    egress_policy = _Policy(
        session_handoff_keys=FIXTURE_ROUTE_PORT.session_handoff_query_keys)


class DeploymentSourceCapabilityReachesTheProductTurn(unittest.TestCase):
    """What the adapter declared is what the deployed turn runs with -- all four settings.

    The product turn used to build a `SourceCapability` of its own out of two of the four
    settings a capability carries, so an adapter that declared `runtime_source=True` -- Odoo
    does, at `adapter.py:source_capability` -- got a deployment where `lookup_source` and
    `open_source_route` were never on the tool surface. Nothing failed; the ability was
    simply absent, which is the shape of defect a passing suite does not catch.

    So the assertion is on the tool surface the model was actually offered, in both
    directions: declaring the capability puts the tools there, and withholding it takes them
    away. The capability itself must also arrive whole, because "the turn rebuilt one out of
    the fields it chose to keep" is the defect, not the symptom.
    """

    def _turn(self, capability):
        """Run one product turn under `capability` and return (session kwargs, tool surface)."""
        recorded = {}
        real = turn.ServerAgentSession

        class _Recording(real):
            def __init__(self, **kwargs):
                recorded.update(kwargs)
                super().__init__(**kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat = _FixtureGrammarChat(
                [("query_ledger", {}), (None, {"content": "done"})])
            executor = AgentTurnExecutor(
                channel=_Channel(page_url="https://fixture.test/greenhouse/do/main"),
                config=AgentTurnConfig(
                    supported_scope="Read ledger data only.",
                    ledger_path=root / "session.jsonl",
                    plain_business_answer_min_page_actions=0,
                    disabled_actions=frozenset({"click", "fill", "select", "submit"}),
                    source=capability,
                ),
                chat=chat,
                application=bind_application(_FixtureGrammarAdapter()),
                control=ReadOnlyControl(),
                run_id_factory=lambda message: "turn-source-capability",
            )
            with patch.object(turn, "RemoteBrowserClient", _RemoteBrowser), \
                    patch.object(turn, "ServerAgentSession", _Recording):
                try:
                    executor.run_once(
                        _message(page_url="https://fixture.test/greenhouse/do/main"))
                except product.AgentTurnError:
                    pass
        self.assertTrue(chat.tool_names, "the turn made no model call")
        return recorded, chat.tool_names[0]

    def _capability(self, root: Path, *, runtime_source: bool):
        from web_operation_agent.composition.sources import SourceCapability
        from web_operation_agent.tests.support.source_fixture import fixture_source_map

        return SourceCapability(source_map=fixture_source_map(root),
                                runtime_source=runtime_source,
                                source_affordance_mode="auto")

    def test_a_declared_runtime_source_capability_puts_the_tools_on_the_surface(self):
        with tempfile.TemporaryDirectory() as tmp:
            capability = self._capability(Path(tmp), runtime_source=True)
        recorded, tools = self._turn(capability)
        self.assertIn("lookup_source", tools)
        self.assertIn("open_source_route", tools)
        # Whole, not rebuilt: the identical value the composition was handed. Asserted after
        # the surface so a regression names the capability that was lost, not the plumbing.
        self.assertIs(recorded["source"], capability)

    def test_withholding_it_takes_the_same_tools_away(self):
        """The reverse direction: same composition, one field, and the tools are gone."""
        with tempfile.TemporaryDirectory() as tmp:
            capability = self._capability(Path(tmp), runtime_source=False)
        recorded, tools = self._turn(capability)
        self.assertNotIn("lookup_source", tools)
        self.assertNotIn("open_source_route", tools)
        self.assertIs(recorded["source"], capability)

    def test_the_turn_cannot_be_handed_source_settings_any_other_way(self):
        """There is no second door: the executor takes no source keyword of its own.

        While `source_map=` was an executor keyword and `source_affordance_mode` a config
        field, "what this deployment's source enables" had two owners and no single place
        that could refuse an incoherent pair.
        """
        accepted = set(inspect.signature(AgentTurnExecutor.__init__).parameters)
        self.assertEqual(accepted & {"source_map", "source", "source_affordance_mode",
                                     "source_contract", "runtime_source"}, set())
        fields = {field.name for field in dataclasses.fields(AgentTurnConfig)}
        self.assertIn("source", fields)
        self.assertEqual(fields & {"source_map", "source_affordance_mode",
                                   "source_contract", "runtime_source"}, set())

    def test_an_incoherent_capability_is_refused_in_one_place(self):
        from web_operation_agent.composition.sources import SourceCapability

        with self.assertRaisesRegex(ValueError, "version-bound SourceMap"):
            SourceCapability(runtime_source=True)
        with self.assertRaisesRegex(TypeError, "SourceCapability"):
            AgentTurnConfig(supported_scope="Read only.", ledger_path=Path("x.jsonl"),
                            source="auto")


if __name__ == "__main__":
    unittest.main()
