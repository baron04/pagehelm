"""Single-step drafts bind actor, command, local form instance and one dispatch."""
from dataclasses import replace
import unittest

from web_operation_agent.business.draft_steps import DraftStepAdmission, DraftStepPermit
from web_operation_agent.execution.action_admission import ActionAdmissionRefused
from web_operation_agent.tests.execution.test_action_admission import page
from web_operation_agent.tests.goal.test_data_read_tools import context
from web_operation_agent.schemas import ActionReceipt
from web_operation_agent.transport.protocol import _remote_args_echo


class Port:
    application_id = "fixture.app"
    required_scope = "records:read"
    actions = frozenset({"fill", "click"})

    def handles(self, intent): return True
    def prepare_step(self, intent, **kwargs):
        return DraftStepPermit(intent.binding_digest, "explicit-submit-review", "exact-field-value",
                               "form:create", "live-instance", "enter_value", (("Date", intent.arguments["value"]),))
    def verify_step(self, permit, intent, receipt, *, observation, **kwargs):
        return (observation.elements[1].attrs.get("form_instance") == permit.instance
                and observation.elements[1].attrs.get("value") == intent.arguments["value"])


class DraftContractTests(unittest.TestCase):
    def setUp(self):
        self.port = Port()
        self.runtime = DraftStepAdmission(self.port, "fixture.app")
        self.context, self.before = context(application="fixture.app"), page()
        self.args = {"ref": "e2", "value": "2026-10-20", "commit": False}
        self.intent, self.permit = self.runtime.prepare("fill", self.args, self.before,
            context=self.context, checkpoint=lambda: None)

    def test_date_commit_timing_is_bound_and_consumed_once(self):
        changed = replace(self.intent, arguments_json=self.intent.arguments_json.replace('false', 'true'))
        with self.assertRaisesRegex(ActionAdmissionRefused, "stale_binding"):
            self.runtime.consume(changed, self.permit, self.before, context=self.context, checkpoint=lambda: None)
        self.runtime.consume(self.intent, self.permit, self.before, context=self.context, checkpoint=lambda: None)
        with self.assertRaisesRegex(ActionAdmissionRefused, "already_consumed"):
            self.runtime.consume(self.intent, self.permit, self.before, context=self.context, checkpoint=lambda: None)

    def test_changed_actor_observation_or_permit_never_dispatches(self):
        actor = replace(self.context, principal=replace(self.context.principal, actor_id="changed"))
        for obs, ctx, permit in ((page(2), self.context, self.permit),
                (self.before, actor, self.permit),
                (self.before, self.context, replace(self.permit, instance="another-instance"))):
            with self.subTest(ctx=ctx), self.assertRaises(ActionAdmissionRefused):
                self.runtime.consume(self.intent, permit, obs, context=ctx, checkpoint=lambda: None)

    def test_readback_requires_guard_exact_echo_and_same_live_form(self):
        self.runtime.consume(self.intent, self.permit, self.before, context=self.context, checkpoint=lambda: None)
        after = page(2); after.elements[1].attrs.update(form_instance="live-instance", value=self.args["value"])
        receipt = ActionReceipt(1, "command", "fill", _remote_args_echo({**self.args, "epoch": 1}), True,
            observation=after, url_before=self.before.url, url_after=after.url, write_guard_held=True)
        self.runtime.verify(self.intent, self.permit, receipt, context=self.context, checkpoint=lambda: None)
        changed = page(3); changed.elements[1].attrs.update(form_instance="replaced", value=self.args["value"])
        for bad in (replace(receipt, write_guard_held=False), replace(receipt, observation=changed),
                    replace(receipt, args_echo={**receipt.args_echo, "commit": True})):
            with self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
                self.runtime.verify(self.intent, self.permit, bad, context=self.context, checkpoint=lambda: None)

    def test_persistence_and_submit_cannot_enter_this_contract(self):
        with self.assertRaises(ValueError):
            replace(self.permit, persistence="save_on_leave")
        with self.assertRaises(ActionAdmissionRefused):
            self.runtime.prepare("submit", {"ref": "e1"}, self.before, context=self.context, checkpoint=lambda: None)

    def test_no_effect_requires_explicit_mechanism_fact_and_application_proof(self):
        class ChoicePort(Port):
            actions = frozenset({"select"})
            def verify_no_effect(self, permit, intent, receipt, *, observation, **kwargs):
                return observation.elements[1].attrs.get("form_instance") == "live-instance"
        admission = DraftStepAdmission(ChoicePort(), "fixture.app")
        args = {"ref": "e2", "value": "Missing", "option_label": "Missing"}
        intent, permit = admission.prepare("select", args, self.before, context=self.context, checkpoint=lambda: None)
        admission.consume(intent, permit, self.before, context=self.context, checkpoint=lambda: None)
        after = page(2); after.elements[1].attrs.update(form_instance="live-instance")
        receipt = ActionReceipt(1, "choice", "select", _remote_args_echo({**args, "epoch": 1}), False,
            error_code="option_unavailable", no_effect=True, candidate_labels=("Exact option",),
            observation=after, url_before=self.before.url, url_after=after.url, write_guard_held=True)
        admission.verify_no_effect(intent, permit, receipt, context=self.context, checkpoint=lambda: None)
        replaced = page(3); replaced.elements[1].attrs.update(form_instance="replaced")
        for bad in (replace(receipt, no_effect=False), replace(receipt, write_guard_held=False),
                    replace(receipt, observation=replaced), replace(receipt, args_echo={"ref": "e3", "epoch": 1}),
                    replace(receipt, observation=self.before)):
            with self.subTest(bad=bad), self.assertRaises(ActionAdmissionRefused):
                admission.verify_no_effect(intent, permit, bad, context=self.context, checkpoint=lambda: None)
        actor = replace(self.context, principal=replace(self.context.principal, actor_id="changed"))
        with self.assertRaises(ActionAdmissionRefused):
            admission.verify_no_effect(intent, permit, receipt, context=actor, checkpoint=lambda: None)
        # An existing port without this optional proof remains fail closed.
        admission.port = Port()
        with self.assertRaises(ActionAdmissionRefused):
            admission.verify_no_effect(intent, permit, receipt, context=self.context, checkpoint=lambda: None)

    def test_unverified_hook_diagnostics_do_not_change_permission_exception_or_raw_receipt(self):
        from web_operation_agent.business import draft_steps as boundary
        from web_operation_agent.execution.action_admission import verified_observation
        from unittest.mock import patch
        for allowed in (False, True, "error"):
            seen = []
            failure = RuntimeError("original application failure")
            class DiagnosticPort(Port):
                def may_continue_unverified(self, permit, intent, receipt, **kwargs):
                    seen.append(receipt.execution_trace)
                    if allowed == "error":
                        raise failure
                    return allowed if receipt.execution_trace is None else not allowed
            admission = DraftStepAdmission(DiagnosticPort(), "fixture.app")
            intent, permit = admission.prepare("fill", self.args, self.before,
                context=self.context, checkpoint=lambda:None)
            admission.consume(intent, permit, self.before, context=self.context, checkpoint=lambda:None)
            original = ActionReceipt(1,"command","fill",_remote_args_echo({**self.args,"epoch":1}),True,
                observation=page(2),url_before=self.before.url,url_after=page(2).url,write_guard_held=True)
            trace = {"trace_id":"private-draft","events":[{"sequence":1,"operation":"fill","path":"binding"}]}
            traced = replace(original,execution_trace=trace)
            settlement = ActionReceipt(1,"settle","observe",{},True,observation=page(3),
                url_before=self.before.url,url_after=page(3).url,write_guard_held=True)
            settlement.execution_trace = trace  # even an internally attached non-business value is stripped
            with patch.object(boundary,"verified_observation",wraps=verified_observation) as verification:
                for receipt in (original,traced):
                    if allowed == "error":
                        with self.assertRaises(RuntimeError) as caught:
                            admission.may_continue_unverified(intent,permit,receipt,context=self.context,
                                checkpoint=lambda:None,settlement=settlement)
                        self.assertIs(caught.exception,failure)
                    else:
                        self.assertIs(admission.may_continue_unverified(intent,permit,receipt,
                            context=self.context,checkpoint=lambda:None,settlement=settlement),allowed)
                for call in verification.call_args_list:
                    self.assertIsNone(call.args[1].execution_trace)
                    self.assertIsNone(call.kwargs["settlement"].execution_trace)
            self.assertEqual(seen,[None,None])
            self.assertEqual(traced.execution_trace,trace)
            self.assertEqual(settlement.execution_trace,trace)

    def test_unverified_draft_can_continue_only_with_application_proof_and_guard(self):
        class LocalPort(Port):
            def may_continue_unverified(self, permit, intent, receipt, *, observation, **kwargs):
                return observation.url == intent.observation.url
        admission = DraftStepAdmission(LocalPort(), "fixture.app")
        intent, permit = admission.prepare("fill", self.args, self.before,
            context=self.context, checkpoint=lambda: None)
        admission.consume(intent, permit, self.before, context=self.context, checkpoint=lambda: None)
        after = page(2)
        receipt = ActionReceipt(1, "command", "fill", _remote_args_echo({**self.args, "epoch": 1}), True,
            observation=after, url_before=self.before.url, url_after=after.url, write_guard_held=True)
        with self.assertRaisesRegex(ActionAdmissionRefused, "unverified"):
            admission.verify(intent, permit, receipt, context=self.context, checkpoint=lambda: None)
        self.assertTrue(admission.may_continue_unverified(intent, permit, receipt,
            context=self.context, checkpoint=lambda: None))
        self.assertFalse(admission.may_continue_unverified(intent, permit,
            replace(receipt, write_guard_held=False), context=self.context, checkpoint=lambda: None))
        admission.port = Port()
        self.assertFalse(admission.may_continue_unverified(intent, permit, receipt,
            context=self.context, checkpoint=lambda: None))


if __name__ == "__main__": unittest.main()
