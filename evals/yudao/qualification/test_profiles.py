"""Accounting and dispatch boundaries; no provider connections."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from web_operation_agent.modeling.client import PRICES, options_for
from .profiles import EvaluationMeter, off_peak, register_profile


class ProfileTests(unittest.TestCase):
    def test_shared_fast_profile_disables_thinking_without_changing_product_policy(self):
        with patch.dict(PRICES):
            self.assertEqual({'thinking': {'type': 'disabled'}}, register_profile('deepseek-flash', 'fast'))
            self.assertEqual(False, register_profile('qwen3.8-flash', 'fast')['enable_thinking'])
            from web_operation_agent.modeling.reasoning import request_options
            self.assertEqual(request_options('qwen3.8-flash', '', options_for('qwen3.8-flash')),
                             register_profile('qwen3.8-flash', 'production'))
            with self.assertRaises(ValueError):
                register_profile('unreviewed', 'fast')

    def test_peak_hours_are_fail_closed_and_weekends_are_off_peak(self):
        for hour in (1, 3, 6, 9):
            self.assertFalse(off_peak(datetime(2026, 10, 2, hour, tzinfo=timezone.utc)))
        for hour in (0, 4, 5, 10, 23):
            self.assertTrue(off_peak(datetime(2026, 10, 2, hour, tzinfo=timezone.utc)))
        self.assertTrue(off_peak(datetime(2026, 10, 3, 2, tzinfo=timezone.utc)))
        with tempfile.TemporaryDirectory() as tmp:
            meter=EvaluationMeter(model='deepseek-v4-pro',ledger=Path(tmp)/'meter.jsonl')
            with patch('evals.yudao.qualification.profiles.off_peak',return_value=False):
                self.assertFalse(meter.within_allowance())

    def test_top_level_cache_hits_are_metered_and_conflicts_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Path(tmp)/'meter.jsonl'
            meter=EvaluationMeter(model='deepseek-flash',ledger=ledger,
                                  price={'input_per_m':1,'cache_read_per_m':.02,'output_per_m':4})
            usage={'prompt_tokens':200,'completion_tokens':50,'prompt_cache_hit_tokens':100}
            meter.record(provider='deepseek',model='deepseek-flash',run_id='synthetic',usage=usage)
            row=json.loads(ledger.read_text())
            self.assertEqual(100,row['cached'])
            self.assertAlmostEqual(.000302,row['call_cost_cny'])
            self.assertNotIn('prompt_tokens_details',usage)
            with self.assertRaises(ValueError):
                meter.record(provider='deepseek',model='deepseek-flash',run_id='synthetic',usage={**usage,'prompt_tokens_details':{'cached_tokens':99}})
            self.assertEqual(1,len(ledger.read_text().splitlines()))


if __name__ == '__main__':
    unittest.main()
