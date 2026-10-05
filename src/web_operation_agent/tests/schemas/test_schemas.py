import unittest

from web_operation_agent import schemas as S


class TerminalReasonContract(unittest.TestCase):
    """Every emitted terminal reason must have one shared classification."""

    def test_every_reason_the_runner_emits_is_classified(self):
        import importlib
        import pkgutil

        import web_operation_agent.execution as ex
        from web_operation_agent.business.approvals import ApprovalWatch
        from web_operation_agent.execution.session import ServerAgentSession
        modules = [importlib.import_module(f'{ex.__name__}.{m.name}')
                   for m in pkgutil.iter_modules(ex.__path__)]
        emitted = _reasons_emitted_by(ServerAgentSession, ApprovalWatch, *modules)
        self.assertTrue(emitted, "could not find any terminal reason in the runner source")
        S.assert_terminal_reason_contract(emitted, set(S.SCORABLE_TERMINAL_REASONS))

    def test_classification_is_total_and_disjoint(self):
        groups = [S.SCORABLE_TERMINAL_REASONS, S.INVALIDATING_TERMINAL_REASONS,
                  S.INDETERMINATE_TERMINAL_REASONS]
        for i, a in enumerate(groups):
            for b in groups[i + 1:]:
                self.assertEqual(a & b, set(), "a reason may not be in two classes")
        for r in S.TERMINAL_REASONS:
            self.assertIn(S.classify_terminal(r), ("scorable", "invalid", "indeterminate"))

    def test_an_unclassified_reason_is_rejected(self):
        with self.assertRaises(S.SchemaError):
            S.assert_terminal_reason_contract({"something_new"}, set())
        with self.assertRaises(S.SchemaError):
            S.classify_terminal("something_new")


def _reasons_emitted_by(*classes) -> set[str]:
    """Read terminal reasons from the session and its collaborating state objects.

    Comparing a hand-written list against another hand-written list proves nothing; the
    point is to catch the runner learning a new reason that the analyser has never heard
    of, so the runner's code is the source of truth. Step code now lives in handler modules
    beside the session, so a module is scanned as itself and the caller passes every
    module that makes up the runner.
    """
    import inspect
    import re
    src = "\n".join(inspect.getsource(cls if inspect.ismodule(cls)
                                      else inspect.getmodule(cls)) for cls in classes)
    found = set()
    for m in re.finditer(r'_finish\(\s*result,\s*t0,\s*("[a-z_]+"|[a-z_]+\s+if)', src):
        token = m.group(1)
        if token.startswith('"'):
            found.add(token.strip('"'))
    # Collaborators return terminal values for the session to pass to ``_finish``. Keep
    # discovering unknown literals at direct finish sites above, and add every already-known
    # terminal literal used by those collaborators to the reachability side of the contract.
    found |= (set(re.findall(r'["\']([a-z_]+)["\']', src)) & set(S.TERMINAL_REASONS))
    found |= set(re.findall(r'"(agent_finished|agent_refused|step_budget|wall_clock_budget|'
                            r'token_budget|action_policy_budget|provider_error|transport_error|'
                            r'indeterminate_write|operation_not_confirmed|'
                            r'planning_failed|control_plane_error)"', src))
    return found


class WireTypes(unittest.TestCase):
    def test_list_read_proof_is_optional_bounded_and_does_not_accept_credentials(self):
        base = dict(schema_version=1, epoch=1, url="https://app.test/contacts",
                    title="", landmarks=[], elements=[])
        proof = dict(path="/api/contacts/page", query_digest="a"*64, query_fields={"name":"b"*64},
                     revision=1, page=2, page_size=10, total=15, rows=5)
        self.assertNotIn("page_read", S.PageObservation(**base).to_json())
        page = S.PageObservation(**base, page_read=proof)
        proof["query_fields"]["name"] = "changed"
        self.assertEqual("b"*64, S.PageObservation.from_json(page.to_json()).page_read["query_fields"]["name"])
        valid = page.page_read
        for bad in ({**valid,"path":"//foreign/page"}, {**valid,"page":True},
                    {**valid,"query_fields":{"token":"a"*64}}, {**valid,"rows":11},
                    {**valid,"revision":0}, {**valid,"query_digest":"raw value"}):
            with self.subTest(proof=bad), self.assertRaises(S.SchemaError):
                S.PageObservation(**base, page_read=bad)

    def test_applied_query_is_optional_bounded_and_rejects_credential_keys(self):
        base = dict(schema_version=1, epoch=1, url="https://app.test/contacts",
                    title="", landmarks=[], elements=[])
        self.assertNotIn("page_query", S.PageObservation(**base).to_json())
        query = {"path":"/api/contacts", "parameters":{"domain":"example.test"}}
        page = S.PageObservation(**base, page_query=query)
        query["parameters"]["domain"] = "changed"
        self.assertEqual("example.test", S.PageObservation.from_json(page.to_json()).page_query["parameters"]["domain"])
        for q in ({}, {"path":"//foreign/api", "parameters":{}},
                  {"path":"/api?x=1", "parameters":{}},
                  {"path":"/api", "parameters":{"token":"secret"}},
                  {"path":"/api", "parameters":{"domain":["a","b"]}},
                  {"path":"/api", "parameters":{"domain":"x"*257}}):
            with self.subTest(query=q), self.assertRaises(S.SchemaError):
                S.PageObservation(**base,page_query=q)
    def test_rendered_page_route_round_trips_without_changing_absent_wire_shape(self):
        base = dict(schema_version=1, epoch=1, url="https://app.test/help",
                    title="Help", landmarks=[], elements=[])
        self.assertNotIn("page_route", S.PageObservation(**base).to_json())
        page = S.PageObservation(**base, page_route="/help")
        self.assertEqual("/help", S.PageObservation.from_json(page.to_json()).page_route)
        self.assertNotIn("record_route", page.to_json())
        for bad in (None, 4, "//other.test/help", "https://other.test/help", "/help?x=1",
                    "/help#tab", "/a\\b", "/a/../help", "/a b", "/" + "x" * 1000):
            with self.subTest(route=bad), self.assertRaises(S.SchemaError):
                S.PageObservation(**base, page_route=bad)

    def test_active_view_is_optional_bounded_and_round_trips(self):
        base = dict(schema_version=1, epoch=1, url="https://app.test/orders",
                    title="Orders", landmarks=[], elements=[])
        self.assertNotIn("active_view", S.PageObservation(**base).to_json())
        page = S.PageObservation(**base, active_view="List")
        self.assertEqual(S.PageObservation.from_json(page.to_json()).active_view, "List")
        for bad in (None, 4, "x" * 121):
            with self.subTest(bad=bad), self.assertRaises(S.SchemaError):
                S.PageObservation(**base, active_view=bad)

    def test_selected_controls_are_bounded_optional_presentation_facts(self):
        page = S.PageObservation(schema_version=1, epoch=1,
                                 url="https://app.test/orders", title="Orders",
                                 landmarks=[], elements=[], selected_controls=["Pivot"])
        self.assertEqual(S.PageObservation.from_json(page.to_json()).selected_controls,
                         ["Pivot"])
        self.assertNotIn("selected_controls", S.PageObservation(
            schema_version=1, epoch=1, url="https://app.test/orders", title="Orders",
            landmarks=[], elements=[]).to_json())
        with self.assertRaises(S.SchemaError):
            S.PageObservation(schema_version=1, epoch=1, url="https://app.test/orders",
                              title="Orders", landmarks=[], elements=[],
                              selected_controls=["x"] * 17)

    def test_application_page_facets_distinguish_unknown_from_empty(self):
        base = dict(schema_version=1, epoch=1, url="https://app.test/orders",
                    title="Orders", landmarks=[], elements=[])
        self.assertNotIn("page_facets", S.PageObservation(**base).to_json())
        self.assertEqual(S.PageObservation.from_json(
            S.PageObservation(**base, page_facets=[]).to_json()).page_facets, [])
        facets = [{"kind": "filter", "name": "Open"},
                  {"kind": "group", "name": "Customer"}]
        self.assertEqual(S.PageObservation.from_json(
            S.PageObservation(**base, page_facets=facets).to_json()).page_facets, facets)
        for bad in ([{"kind": "sort", "name": "Customer"}],
                    [{"kind": "filter", "name": ""}],
                    [{"kind": "filter", "name": "X"}] * 17):
            with self.subTest(bad=bad), self.assertRaises(S.SchemaError):
                S.PageObservation(**base, page_facets=bad)

    def test_facet_candidates_are_bounded_and_round_trip_as_choices_not_state(self):
        base = dict(schema_version=1, epoch=1, url="https://app.test/orders",
                    title="Orders", landmarks=[], elements=[])
        self.assertNotIn("facet_candidates", S.PageObservation(**base).to_json())
        candidates = [{"ref": "fc_1", "kind": "group", "name": "Owner", "active": False}]
        page = S.PageObservation(**base, facet_candidates=candidates)
        candidates[0]["name"] = "changed"
        self.assertEqual(S.PageObservation.from_json(page.to_json()).facet_candidates,
                         [{"ref": "fc_1", "kind": "group", "name": "Owner",
                           "active": False}])
        for bad in ([{"ref": "fc_1", "kind": "sort", "name": "Owner", "active": False}],
                    [{"ref": "fc_1", "kind": "group", "name": "Owner", "active": 1}],
                    [{"ref": "fc_1", "kind": "group", "name": "Owner", "active": False}] * 2,
                    [{"ref": f"fc_{i}", "kind": "group", "name": "Owner",
                      "active": False} for i in range(33)]):
            with self.subTest(bad=bad), self.assertRaises(S.SchemaError):
                S.PageObservation(**base, facet_candidates=bad)

    def test_page_render_state_round_trips_and_rejects_unknown_values(self):
        page = S.PageObservation(schema_version=1, epoch=1,
                                 url="https://app.test/orders", title="Orders",
                                 landmarks=[], elements=[], render_state="pending")
        self.assertEqual(S.PageObservation.from_json(page.to_json()).render_state,
                         "pending")
        page.render_state = "blocked"
        self.assertEqual(S.PageObservation.from_json(page.to_json()).render_state,
                         "blocked")
        self.assertEqual(S.PageObservation.from_json({
            key: value for key, value in page.to_json().items()
            if key != "render_state"}).render_state, "unknown")
        with self.assertRaises(S.SchemaError):
            S.PageObservation(schema_version=1, epoch=1,
                              url="https://app.test/orders", title="Orders",
                              landmarks=[], elements=[], render_state="complete")

    def test_page_record_route_is_optional_bounded_observation_not_authority(self):
        base = dict(schema_version=1, epoch=1, url="https://app.test/items/4",
                    title="Item", landmarks=[], elements=[])
        page = S.PageObservation(**base, record_route="/items/4")
        self.assertEqual(S.PageObservation.from_json(page.to_json()).record_route,
                         "/items/4")
        self.assertNotIn("record_route", S.PageObservation(**base).to_json())
        for bad in ("//other.test/x", "https://other.test/x", "/items/4?x=1",
                    "/a\\b", "/a/../b", "/a b"):
            with self.subTest(route=bad), self.assertRaises(S.SchemaError):
                S.PageObservation(**base, record_route=bad)

    def test_approval_request_and_decision_are_strict_wire_types(self):
        request = S.ApprovalRequest(
            schema_version=1, approval_id="approval-1",
            effect="Accept supplier quote", targets=("Quote Q-1042",),
            expires_at=2_000_000_000_000)
        self.assertEqual(S.ApprovalRequest.from_json(request.to_json()), request)
        decision = S.ApprovalDecision(
            schema_version=1, session_id="session-1",
            approval_id=request.approval_id, decision="approved")
        self.assertEqual(S.ApprovalDecision.from_json(decision.to_json()), decision)
        with self.assertRaises(S.SchemaError):
            S.ApprovalDecision(schema_version=1, session_id="session-1",
                               approval_id="approval-1", decision="maybe")
        bound = S.ApprovalRequest(
            schema_version=1, approval_id="approval-bound",
            effect="Create company", targets=("Acme",),
            expires_at=2_000_000_000_000,
            principal_digest="a" * 64, turn_id="message-1", operation_id="op-1")
        self.assertEqual(S.ApprovalRequest.from_json(bound.to_json()), bound)
        with self.assertRaises(S.SchemaError):
            S.ApprovalDecision(
                schema_version=1, session_id="session-1",
                approval_id="approval-bound", decision="approved",
                principal_digest="a" * 64)
        smuggled = request.to_json(); smuggled["request"] = "hiddenBackendEndpoint"
        with self.assertRaises(S.SchemaError):
            S.ApprovalRequest.from_json(smuggled)

    def test_cancellation_is_strict_and_bound_to_one_message(self):
        request = S.CancellationRequest(
            schema_version=1, session_id="session-1", message_id="message-1")
        self.assertEqual(S.CancellationRequest.from_json(request.to_json()), request)
        with self.assertRaises(S.SchemaError):
            S.CancellationRequest.from_json({**request.to_json(), "reason": "skip"})
        with self.assertRaises(S.SchemaError):
            S.CancellationRequest(
                schema_version=1, session_id="session-1", message_id="x" * 201)

    def test_action_name_survives_its_arguments(self):
        """Argument keys cannot overwrite the command's explicit action name."""
        cmd = S.ActionCommand(command_id="c1", action="fill",
                              args={"click": "not the action", "ref": "e1", "value": "x"})
        back = S.ActionCommand.from_json(cmd.to_json())
        self.assertEqual(back.action, "fill")
        self.assertEqual(back.args["click"], "not the action")

    def test_unknown_action_is_refused(self):
        with self.assertRaises(S.SchemaError):
            S.ActionCommand(command_id="c1", action="drag", args={})

    def test_scroll_is_a_read_only_browser_action(self):
        command = S.ActionCommand(
            command_id="c-scroll", action="scroll",
            args={"direction": "down", "pages": 1})

        self.assertEqual(command.action, "scroll")
        self.assertIn("scroll", S.INTRINSICALLY_READ_ONLY_ACTIONS)
        self.assertNotIn("scroll", S.POTENTIALLY_WRITING_ACTIONS)

    def test_page_observation_refuses_unbounded_elements_and_content(self):
        with self.assertRaisesRegex(S.SchemaError, "at most 200"):
            S.PageObservation(
                schema_version=1, epoch=1, url="http://x/y", title="T", landmarks=[],
                elements=[S.ElementRef(f"e{i}", "button", str(i), "button", 1)
                          for i in range(201)])
        with self.assertRaisesRegex(S.SchemaError, "at most 120000"):
            S.PageObservation(
                schema_version=1, epoch=1, url="http://x/y", title="T", landmarks=[],
                elements=[], content="x" * 120_001)

    def test_version_mismatch_is_refused(self):
        d = S.ActionCommand(command_id="c1", action="observe").to_json()
        d["schema_version"] = 99
        with self.assertRaises(S.SchemaError):
            S.ActionCommand.from_json(d)

    def test_receipt_round_trips_every_field(self):
        obs = S.PageObservation(schema_version=1, epoch=3, url="http://x/y", title="T",
                                landmarks=["A"], elements=[S.ElementRef("e1", "link", "n", "a", 3)])
        r = S.ActionReceipt(schema_version=1, command_id="c9", action="click",
                            args_echo={"ref": "e1"}, ok=False, error_code="stale_ref",
                            error_detail="d", url_before="a", url_after="b", navigated=True,
                            epoch_before=2, epoch_after=3, elapsed_ms=17, relogin=True,
                            write_guard_held=True, observation=obs)
        back = S.ActionReceipt.from_json(r.to_json())
        self.assertEqual(back.to_json(), r.to_json())
        self.assertTrue(back.relogin)
        self.assertTrue(back.write_guard_held)
        # A Runtime that predates the field sends none, which means it held no window.
        legacy = r.to_json()
        del legacy["write_guard_held"]
        self.assertFalse(S.ActionReceipt.from_json(legacy).write_guard_held)
        self.assertEqual(back.observation.elements[0].ref, "e1")

    def test_receipt_error_codes_are_a_closed_set(self):
        self.assertIn("stale_ref", S.RECEIPT_ERRORS)
        self.assertIn("", S.RECEIPT_ERRORS)


class Evidence(unittest.TestCase):
    def test_traversal_and_absolute_paths_are_refused(self):
        for bad in ("/etc/passwd", "a/../../secret.xml"):
            with self.assertRaises(S.SchemaError):
                S.EvidenceRef("r", "v", bad, 1, 2, "0" * 64)

    def test_line_span_must_be_ordered(self):
        with self.assertRaises(S.SchemaError):
            S.EvidenceRef("r", "v", "a.xml", 9, 2, "0" * 64)

    def test_unknown_fields_are_refused(self):
        d = S.EvidenceRef("r", "v", "a.xml", 1, 2, "0" * 64).to_json()
        d["comment"] = "smuggled"
        with self.assertRaises(S.SchemaError):
            S.EvidenceRef.from_json(d)

    def test_a_relation_without_evidence_is_refused(self):
        with self.assertRaises(S.SchemaError):
            S.SourceRelation(kind="menu_link", subject="a", object="b")

    def test_an_invented_relation_kind_is_refused(self):
        ref = S.EvidenceRef("r", "v", "a.xml", 1, 2, "0" * 64)
        with self.assertRaises(S.SchemaError):
            S.SourceRelation(kind="calls_into", subject="a", object="b", evidence=(ref,))


class Contract(unittest.TestCase):
    def test_unknown_fields_are_refused(self):
        with self.assertRaises(S.SchemaError):
            S.OperationContract.from_json({"schema_version": 1, "task_ref": "t", "summary": "s",
                                           "confidence": 0.9})

    def test_citations_are_collected_from_every_level(self):
        c = S.OperationContract.from_json({
            "schema_version": 1, "task_ref": "t", "summary": "s",
            "entry_candidates": [{"intent": "i", "webapp": "w", "request": "r",
                                  "citations": ["a.xml:1-2"]}],
            "steps": [{"intent": "i", "webapp": "w", "request": "r", "citations": ["b.xml:3-4"]}],
            "mutation": {"webapp": "w", "request": "r", "citations": ["c.xml:5-6"]},
            "citations": ["d.xml:7-8"]})
        self.assertEqual(c.cited_keys(),
                         {"a.xml:1-2", "b.xml:3-4", "c.xml:5-6", "d.xml:7-8"})

    def test_empty_contract_is_valid_and_says_nothing(self):
        c = S.empty_contract("t")
        self.assertEqual(c.cited_keys(), set())
        self.assertTrue(c.unknowns)


class ControlPlanRedaction(unittest.TestCase):
    def test_redaction_leaks_no_ticket_content(self):
        plan = S.ControlPlan(plan_id="p", task_ref="t", tickets=(
            S.ControlTicket(webapp="accounting", request="createSomething",
                            targets=({"widgetId": "SECRET-42"},), changes={"amount": "20"}),))
        blob = S.canonical_json(plan.redacted())
        for leak in ("accounting", "createSomething", "SECRET-42", "amount", "20"):
            self.assertNotIn(leak, blob, f"{leak!r} leaked into the ledger-safe view")
        self.assertEqual(plan.redacted()["ticket_count"], 1)


if __name__ == "__main__":
    unittest.main()
