"""Original operation and every question survive an application-owned form handoff."""
import json
from pathlib import Path
import tempfile
import unittest

from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.business.runtime import BusinessToolRuntime
from web_operation_agent.composition.ports import DeclaredRoutePort
from web_operation_agent.execution.session import ServerAgentSession, TaskSpec
from web_operation_agent.goal.read_tools import DataReadTools
from web_operation_agent.state.ledger import SessionLedger
from web_operation_agent.tests.business import test_generic_forms as form_fixture
from web_operation_agent.tests.execution import test_product_deferred_context as source_fixture
from web_operation_agent.tests.goal.test_data_read_tools import Data, context
from web_operation_agent.tests.support.business import _Chat, _Executor


class Chat(_Chat):
    def __init__(self, script):
        super().__init__(script)
        self.descriptions = []

    def complete(self, messages, tools, run_id, **kwargs):
        self.descriptions.append({tool['function']['name']: tool['function']['description']
                                  for tool in tools})
        if callable(self.script[0]):
            self.script[0] = self.script[0](messages)
        return super().complete(messages, tools, run_id, **kwargs)


def prepare(messages):
    descriptor = json.loads(messages[-1]['content'].split('\n', 1)[1])
    field = next(item for item in descriptor['fields'] if item['label'] == 'Company name')
    return 'prepare_form', {'form_id':descriptor['form_id'],
        'values':[{'field_id':field['field_id'], 'value':'NovaForge'}]}


def finish_mixed(*, outcome='acted', omit_source=False):
    conclusions = [{'kind':'field_value', 'evidence_ref':'call6-data#2'}]
    bindings = [{'request_ref':'a1', 'evidence_refs':['call6-data#2']}]
    if not omit_source:
        conclusions.append({'kind':'source_excerpt', 'evidence_ref':'source#1',
            'statement':'The code batch limit is 125.', 'support':'batch_limit = 125'})
        bindings.append({'request_ref':'a2', 'evidence_refs':['source#1']})
    return {'outcome':outcome, 'answer':{'version':1,
        'coverage':'partial' if outcome == 'partial' else 'complete',
        'conclusions':conclusions, 'answers_for':bindings}}


class TaskFormSettlementTests(unittest.TestCase):
    def execute(self, prompt, declaration, followups=(), *, before_prefill=(),
                before_describe=(), business_tools=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = source_fixture.ProductDeferredContextTests().source(root)
            browser = form_fixture.GenericFormProductPathNeedsNoPerFormSkill.Browser()
            data = Data()
            chat = Chat([('declare_task', declaration), *before_describe, ('describe_form', {}), prepare,
                *before_prefill, ('prefill_form', {}), *followups])
            session = ServerAgentSession(task=TaskSpec('T', prompt), chat=chat, browser=browser,
                control=ReadOnlyControl(), ledger=SessionLedger(root/'audit.jsonl', 'handoff-task'),
                source=source, data_reads=DataReadTools(lambda ctx, check:data, 'sample', 'records:read'),
                business_tools=business_tools,
                application_routes=DeclaredRoutePort('sample', (r'/companies/new',)),
                application_forms=form_fixture._Forms(), allow_form_prefill=True, require_task_contract=True,
                principal=context().principal, session_id='session', turn_id='turn',
                disabled_actions={'click', 'fill', 'select', 'submit'}, step_budget=16, wall_budget_s=30)
            session.entry_observation = browser.observe()
            session.plan()
            result = session.run()
            return result, chat, browser, session.ledger.events, data

    def test_natural_create_and_field_change_reach_the_default_manual_submit_handoff(self):
        for prompt in ('Create company NovaForge', 'Change the company name to NovaForge',
                       '新建公司NovaForge', '公司名称改成NovaForge'):
            with self.subTest(prompt=prompt):
                result, chat, browser, events, _ = self.execute(prompt, {
                    'action_request':prompt, 'action_kind':'draft', 'answer_requests':[],
                    'answer_kinds':[]}, before_describe=[('check_capability', {})],
                    business_tools=BusinessToolRuntime((), _Executor()))
                self.assertEqual('agent_finished', result.terminal_reason)
                self.assertEqual('complete', result.verified_answer['coverage'])
                self.assertEqual('form_prefill', result.verified_answer['action']['kind'])
                self.assertEqual(['fill', 'handoff_form'], browser.actions)
                self.assertTrue('has not been submitted' in result.agent_said
                                or '未提交' in result.agent_said)
                self.assertIn('DEFAULT FORM EXECUTION MODE',
                              chat.descriptions[0]['declare_task'])
                self.assertIn('DEFAULT FORM EXECUTION MODE', chat.messages[0][0]['content'])
                self.assertIn('persistent writes only', chat.messages[2][-1]['content'])
                checks = [event for event in events if event['event'] == 'capability_check']
                self.assertEqual(1, len(checks))
                self.assertEqual([], checks[0]['matches'])
                self.assertEqual(1, sum(event['event'] == 'form_prefill_completed'
                                        for event in events))
                self.assertFalse(any(event['event'] == 'business_mutation_proposed'
                                     for event in events))

    def test_pre_effect_correction_to_default_draft_preserves_the_natural_request(self):
        prompt = 'Create company NovaForge'
        declaration = {'action_request':prompt, 'action_kind':'mutation',
                       'answer_requests':[], 'answer_kinds':[]}
        result, chat, browser, events, _ = self.execute(prompt, declaration,
            before_describe=[('check_capability', {}), ('declare_task', {
                **declaration, 'action_kind':'draft'})],
            business_tools=BusinessToolRuntime((), _Executor()))
        self.assertEqual('agent_finished', result.terminal_reason)
        self.assertEqual('complete', result.verified_answer['coverage'])
        self.assertEqual(['fill', 'handoff_form'], browser.actions)
        self.assertEqual(1, sum(event['event'] == 'task_interpretation_revised'
                               for event in events))
        self.assertIn(prompt, chat.messages[-1][1]['content'])

    def test_pure_draft_settles_without_an_extra_model_call_and_exposes_exact_handoff_receipt(self):
        prompt = 'Prefill NovaForge; do not submit'
        result, chat, browser, _, _ = self.execute(prompt, {'action_request':prompt,
            'action_kind':'draft', 'answer_requests':[], 'answer_kinds':[]})
        self.assertEqual('agent_finished', result.terminal_reason)
        self.assertEqual(4, len(chat.messages))
        self.assertEqual('complete', result.verified_answer['coverage'])
        self.assertEqual('form_prefill', result.verified_answer['action']['kind'])
        self.assertEqual(['fill', 'handoff_form'], browser.actions)
        self.assertIn('has not been submitted', result.agent_said)

    def test_highlight_cannot_replace_prefill_or_release_the_guard_before_real_handoff(self):
        prompt = 'Prefill NovaForge'
        result, chat, browser, events, _ = self.execute(prompt, {'action_request':prompt,
            'action_kind':'draft', 'answer_requests':[], 'answer_kinds':[]}, before_prefill=[
                ('highlight', {'ref':'name'})])
        self.assertEqual('agent_finished', result.terminal_reason)
        self.assertEqual(['fill', 'handoff_form'], browser.actions)
        self.assertTrue(any(e['event'] == 'task_highlight_refused' for e in events))
        self.assertIn('No control was highlighted', str(chat.messages))

    def test_verified_prefill_cannot_complete_a_requested_persisted_change(self):
        prompt = 'Save NovaForge'
        result, chat, browser, events, _ = self.execute(prompt, {'action_request':prompt,
            'action_kind':'mutation', 'answer_requests':[], 'answer_kinds':[]}, [
            ('finish', {'outcome':'acted'}), ('finish', {'outcome':'unable'})],
            before_describe=[('check_capability', {})],
            business_tools=BusinessToolRuntime((), _Executor()))
        self.assertEqual('agent_refused', result.terminal_reason)
        self.assertIn('Unfinished operation: Save NovaForge', result.agent_said)
        self.assertIn('has not been submitted', result.agent_said)
        self.assertEqual('partial', result.verified_answer['coverage'])
        self.assertTrue(any(e['event'] == 'task_action_claim_refused' for e in events))
        self.assertFalse(any(e['event'] == 'task_interpretation_revised' for e in events))
        self.assertNotIn('submit', browser.actions)
        self.assertNotIn('prefill_form', chat.tool_names[-1])
        self.assertNotIn('goto', chat.tool_names[-1])

    def test_correcting_a_prepared_draft_requires_fresh_preparation_before_any_input(self):
        prompt = 'Save NovaForge'
        declaration = {'action_request':prompt, 'action_kind':'draft',
            'answer_requests':[], 'answer_kinds':[]}
        result, chat, browser, events, _ = self.execute(prompt, declaration,
            [('finish', {'outcome':'unable'})], before_prefill=[
                ('declare_task', {**declaration, 'action_kind':'mutation'}),
                ('prefill_form', {}), ('describe_form', {}), prepare])
        self.assertEqual('agent_refused', result.terminal_reason)
        self.assertIn('No current application-issued form draft', str(chat.messages))
        self.assertEqual(2, sum(e['event'] == 'application_form_prepared' for e in events))
        self.assertEqual(1, browser.actions.count('fill'))
        self.assertEqual(1, browser.actions.count('handoff_form'))
        self.assertIn('Unfinished operation: Save NovaForge', result.agent_said)

    def mixed(self, followups):
        return self.execute("Prefill NovaForge; what is Smaller's amount and the code batch limit?", {
            'action_request':'Prefill NovaForge', 'action_kind':'draft',
            'answer_requests':["what is Smaller's amount", 'the code batch limit'],
            'answer_kinds':['data', 'knowledge']}, [
            ('lookup_source', {'query':'batch limit'}),
            ('resolve_data', {'deliverables':[{'kind':'fact', 'entity':'records',
                'record':'Smaller', 'fields':['Amount']}]}), *followups])

    def test_data_and_source_questions_continue_in_the_same_agent_after_handoff(self):
        result, chat, browser, _, data = self.mixed([('finish', finish_mixed())])
        self.assertEqual('agent_finished', result.terminal_reason,
            [m[-1]['content'] for m in chat.messages])
        self.assertIn('-3', result.agent_said)
        self.assertIn('125', result.agent_said)
        self.assertIn('has not been submitted', result.agent_said)
        self.assertEqual('form_prefill', result.verified_answer['action']['kind'])
        self.assertIn('read', data.calls)
        self.assertEqual(['fill', 'handoff_form', 'observe', 'observe'], browser.actions)
        for names in chat.tool_names[4:]:
            self.assertTrue({'lookup_source','resolve_data','finish'} <= names)
            self.assertFalse({'goto','fill','prepare_form','prefill_form','submit'} & names)

    def test_missing_question_is_repairable_without_repeating_or_losing_the_handoff(self):
        result, chat, browser, events, _ = self.mixed([
            ('finish', finish_mixed(omit_source=True)), ('finish', finish_mixed())])
        self.assertEqual('agent_finished', result.terminal_reason)
        self.assertEqual(1, browser.actions.count('handoff_form'))
        self.assertEqual(1, browser.actions.count('fill'))
        self.assertTrue(any(e['event'] == 'answer_contract_refused' for e in events))
        self.assertIn('Unanswered request: the code batch limit', str(chat.messages[-1]))

    def test_partial_retains_entered_form_valid_fact_and_original_unanswered_question(self):
        result, _, browser, _, _ = self.mixed([
            ('finish', finish_mixed(outcome='partial', omit_source=True))])
        self.assertEqual('agent_refused', result.terminal_reason)
        self.assertIn('-3', result.agent_said)
        self.assertIn('has not been submitted', result.agent_said)
        self.assertIn('Unanswered request: the code batch limit', result.agent_said)
        self.assertEqual(1, browser.actions.count('fill'))


if __name__ == '__main__':
    unittest.main()
