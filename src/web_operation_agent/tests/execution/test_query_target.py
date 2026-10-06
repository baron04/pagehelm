"""Applied query completion is separate from a draft, click or unverified browser fact."""
from dataclasses import replace
import hashlib
import json
import unittest

from web_operation_agent.execution.action_admission import ActionEffect
from web_operation_agent.execution.target import parse_target, unmet_conditions
from web_operation_agent.tests.execution import test_action_admission as fixture
from web_operation_agent.tests.execution.test_action_admission import Policy, page


def digest(value):
    return hashlib.sha256(json.dumps([value], ensure_ascii=False,
        separators=(",", ":")).encode()).hexdigest()


def rest_list(epoch=1, *, value="", applied=False):
    obs = page(epoch, "/contacts")
    obs.elements[0] = replace(obs.elements[0], name="Search")
    obs.elements[1].attrs.update(page_control="query", query_field="term", field_label="Contact",
                                form_scope="form:search", form_instance="flat-html-form", value=value)
    obs.page_read = dict(path="/api/contacts", query_digest="a"*64, query_fields=(
        {"term": digest(value)} if applied else {}), revision=2 if applied else 1,
        page=1, page_size=10, total=1, rows=1)
    return obs


class QueryTargetTests(unittest.TestCase):
    def test_agent_observation_exposes_live_query_binding_but_not_applied_value_digests(self):
        from web_operation_agent.execution.context import _render_observation
        before = rest_list(value="Synthetic", applied=True)
        rendered = _render_observation(before)
        self.assertIn("page_control=query query_field=term", rendered)
        self.assertNotIn(digest("Synthetic"), rendered)
        self.assertEqual("term", parse_target({"query":[{"ref":"e2", "value":"Synthetic"}]}, before).query[0].field)

    def test_source_free_searchbox_without_html_form_still_needs_exact_applied_actor_read(self):
        before = rest_list()
        attrs = {key:value for key,value in before.elements[1].attrs.items()
                 if key not in {'form_scope', 'form_instance', 'field_label'}}
        before.elements[1] = replace(before.elements[1], role='searchbox', name='Contact search', attrs=attrs)
        target = parse_target({'query':[{'ref':'e2','value':'Synthetic'}]}, before)
        self.assertEqual('Contact search', target.query[0].label)
        after = rest_list(2, value='Synthetic', applied=True)
        self.assertTrue(unmet_conditions(target, after))
        self.assertFalse(unmet_conditions(target, after, verified_query=True))

    def test_query_binds_exact_live_control_and_checks_applied_value_not_draft(self):
        before = rest_list(value="澄海测绘")
        args = {"query": [{"ref": "e2", "value": "澄海测绘"}]}
        target = parse_target(args, before)
        self.assertEqual(args["query"], target.to_json()["query"])
        self.assertNotIn("澄海测绘", json.dumps(target.audit_projection(), ensure_ascii=False))
        self.assertEqual(("query result not application-verified",), unmet_conditions(target, before))
        self.assertTrue(unmet_conditions(target, before, verified_query=True))
        after = rest_list(2, value="澄海测绘", applied=True)
        self.assertEqual((), unmet_conditions(target, after, verified_query=True,
                                              required_effects=frozenset({"filter"})))
        for changed in (replace(after, render_state="pending"), replace(after, page_route="/other"),
                        replace(after, page_read=None), replace(after, page_read={**after.page_read,
                            "query_fields": {"term": digest("another")}}),
                        replace(after, page_read={**after.page_read, "path": "/api/another"})):
            with self.subTest(path=changed.page_route):
                self.assertTrue(unmet_conditions(target, changed, verified_query=True))

    def test_unbound_stale_secret_or_ambiguous_conditions_cannot_be_declared(self):
        args = {"query": [{"ref": "e2", "value": "Synthetic"}]}
        variants = [None, replace(rest_list(), render_state="pending"), replace(rest_list(), page_read=None)]
        for changes in ({"page_control": ""}, {"query_field": "password"}, {"query_field": "wrong/key"},
                        {"form_instance": ""}, {"form_scope": "dialog:write"}, {"disabled": "true"},
                        {"readonly": "true"}, {"field_label": ""}):
            obs = rest_list(); obs.elements[1].attrs.update(changes); variants.append(obs)
        stale = rest_list(); stale.elements[1] = replace(stale.elements[1], epoch=0); variants.append(stale)
        for obs in variants:
            with self.subTest(obs=obs is None), self.assertRaises(ValueError): parse_target(args, obs)
        for raw in (args["query"]*2, [{"ref": "e2", "value": ""}],
                    [{"ref": "absent", "value": "Synthetic"}],
                    [{"ref": "e2", "value": "Synthetic", "field": "hidden"}]):
            with self.subTest(raw=raw), self.assertRaises(ValueError): parse_target({"query": raw}, rest_list())


class RestQueryLoopTests(unittest.TestCase):
    execute = fixture.EffectLoopTests.execute
    def test_body_searchbox_uses_the_same_guarded_full_query_contract_without_source(self):
        policy = Policy(ActionEffect.READ_ONLY)
        policy.verify = lambda permit, intent, receipt, *, observation, **kw: (
            intent.action == 'click' and observation.page_read['revision'] == 2)
        before = rest_list()
        before.elements[1] = replace(before.elements[1], role='searchbox', name='Search contacts',
            attrs={key:value for key,value in before.elements[1].attrs.items()
                   if key not in {'form_scope','form_instance','field_label'}})
        after = rest_list(2, value='Synthetic', applied=True)
        result, session, browser, _ = self.execute(policy, [
            ('set_target', {'query':[{'ref':'e2','value':'Synthetic'}]}),
            ('click', {'ref':'e1'}), ('finish', {'done':True})], before=before, after=after,
            navigated=False, prompt='Filter contacts by Synthetic')
        self.assertEqual('agent_finished', result.terminal_reason)
        self.assertEqual(['click'], [name for name,_ in browser.done])
        self.assertTrue(session.action_admission.query_verified(after))

    def test_flat_rest_query_full_task_settles_only_after_application_verification(self):
        policy = Policy(ActionEffect.READ_ONLY)
        policy.verify = lambda permit, intent, receipt, *, observation, **kw: (
            intent.action == "click" and observation.page_read["revision"] == 2)
        before = rest_list()
        after = rest_list(2, value="Synthetic", applied=True)
        calls = [("set_target", {"query": [{"ref": "e2", "value": "Synthetic"}]}),
                 ("click", {"ref": "e1"}), ("finish", {"done": True})]
        result, session, browser, chat = self.execute(policy, calls, before=before, after=after,
            navigated=False, prompt="Filter contacts by Synthetic")
        self.assertEqual("agent_finished", result.terminal_reason)
        self.assertEqual(["click"], [name for name, _ in browser.done])
        self.assertEqual(["Contact"], session._answer_action_receipt["query"])
        self.assertNotIn("Synthetic", json.dumps(session.ledger.of_kind("target_state_declared")))
        self.assertEqual(3, len(chat.seen))
        self.assertTrue(session.action_admission.query_verified(after))
        self.assertFalse(session.action_admission.query_verified(replace(after, page_route="/another")))

    def test_draft_value_and_matching_browser_digest_alone_do_not_settle(self):
        before = rest_list(value="Synthetic", applied=True)
        result, session, browser, _ = self.execute(Policy(), [
            ("set_target", {"query": [{"ref": "e2", "value": "Synthetic"}]}),
            ("finish", {"done": True}), ("finish", {"done": False})], before=before,
            navigated=False, prompt="Filter contacts by Synthetic")
        self.assertFalse(session._target_reached)
        self.assertEqual([], browser.done)
        self.assertTrue(session.ledger.of_kind("page_target_claim_refused"))


class QueryReceiptTests(unittest.TestCase):
    setUp = fixture.ActionAdmissionTests.setUp
    prepare = fixture.ActionAdmissionTests.prepare
    receipt = fixture.ActionAdmissionTests.receipt
    def test_query_stamp_requires_guarded_click_verified_new_read_and_same_snapshot(self):
        self.before = rest_list()
        self.policy.verify = lambda *a, **kw: True
        intent, permit = self.prepare()
        self.runtime.consume(intent, permit, self.before, context=self.context, checkpoint=lambda: None)
        after = rest_list(2, value="Synthetic", applied=True)
        receipt = replace(self.receipt(), url_after=after.url, observation=after)
        self.assertFalse(self.runtime.query_verified(after))
        self.runtime.verify(intent, permit, receipt, context=self.context, checkpoint=lambda: None)
        self.assertTrue(self.runtime.query_verified(after))
        changed = replace(after, page_read={**after.page_read, "revision": 3})
        self.assertFalse(self.runtime.query_verified(changed))

    def test_verified_fill_does_not_issue_query_completion_even_with_matching_new_digest(self):
        self.before = rest_list()
        self.policy.verify = lambda *a, **kw: True
        intent, permit = self.prepare("fill", {"ref": "e2", "value": "Synthetic"})
        self.runtime.consume(intent, permit, self.before, context=self.context, checkpoint=lambda: None)
        after = rest_list(2, value="Synthetic", applied=True)
        from web_operation_agent.transport.protocol import _remote_args_echo
        receipt = replace(self.receipt("fill", _remote_args_echo({"ref": "e2", "value": "Synthetic", "epoch": 1})),
                          url_after=after.url, observation=after)
        self.runtime.verify(intent, permit, receipt, context=self.context, checkpoint=lambda: None)
        self.assertFalse(self.runtime.query_verified(after))


if __name__ == "__main__": unittest.main()
