"""Choice reads keep the actor and reject ambiguous or changed application facts."""
from dataclasses import replace
from types import SimpleNamespace
import unittest

from web_operation_agent.business.errors import (
    BusinessToolRequestError, BusinessToolAuthenticationError,
    BusinessToolPermissionError, BusinessToolUnavailableError)
from web_operation_agent_yudao.client import YudaoReadRefused
from web_operation_agent_yudao.form_choices import NativeFormChoices
from . import test_forms as fixture
from .test_source_forms import _PATH


class FormChoiceReads(unittest.TestCase):
    def setUp(self):
        fixture.FormsTests.setUp(self)
        self.calls = []
        self.rows = [{'dictType':'synthetic','value':'0','label':'普通'},
                     {'dictType':'other','value':'1','label':'不属于此字段'}]
        def get(path):
            self.calls.append(path)
            return self.rows
        self.verifier.client = SimpleNamespace(get=get)
        self.reader_choices = NativeFormChoices(self.verifier)
        self.fact = replace(self.reader.read(_PATH).fields[0], input_type='choice',
                            choice_source='dictionary', choice_reference='synthetic')

    def test_dictionary_read_is_fixed_actor_scoped_and_preserves_zero(self):
        choices = self.reader_choices(self.fact,self.state,self.context)
        self.assertEqual([(c.value,c.label) for c in choices],[('0','普通')])
        self.assertEqual(self.calls,['system/dict-data/simple-list'])
        self.verifier.allowed = False
        with self.assertRaises(BusinessToolRequestError):
            self.reader_choices(self.fact,self.state,self.context)
        self.assertEqual(len(self.calls),1)

    def test_invalid_values_duplicate_labels_and_overlarge_lists_are_not_declared(self):
        for rows in ([{'dictType':'synthetic','value':'1x','label':'普通'}],
                     [{'dictType':'synthetic','value':str(i),'label':'同名'} for i in range(2)],
                     [{'dictType':'synthetic','value':str(i),'label':str(i)} for i in range(51)]):
            self.rows = rows
            self.assertEqual(self.reader_choices(self.fact,self.state,self.context),())

    def test_relation_read_binds_exact_record_ids_and_rechecks_actor_after_read(self):
        self.rows = [{'id':17,'nickname':'合成负责人'}]
        fact = replace(self.fact,choice_source='users',choice_reference='')
        result = self.reader_choices(fact,self.state,self.context)
        self.assertEqual([(c.value,c.label) for c in result],[('17','合成负责人')])
        self.assertEqual(self.calls,['system/user/simple-list'])
        def changed(path):
            self.verifier.allowed = False
            return self.rows
        self.verifier.client.get = changed
        self.assertEqual(self.reader_choices(fact,self.state,self.context),())

    def test_denied_expired_and_failed_reads_preserve_distinct_error_types(self):
        for code, error in ((401, BusinessToolAuthenticationError),
                            (403, BusinessToolPermissionError), (503, BusinessToolUnavailableError)):
            def refused(path):
                raise YudaoReadRefused('synthetic refusal', code=code)
            self.verifier.client.get = refused
            with self.subTest(code=code), self.assertRaises(error):
                self.reader_choices(self.fact,self.state,self.context)
