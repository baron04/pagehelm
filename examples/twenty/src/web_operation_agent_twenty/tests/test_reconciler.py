"""Twenty's reconciler keeps a matching record uncertain.

A read that finds a matching record does not prove this proposal created it, and a read
that finds none cannot rule out a write still in flight. The generic `reconcile` operation
is tested in the core package; what belongs here is Twenty's own answer.
"""
import unittest


class TwentyReconciliation(unittest.TestCase):
        def test_twenty_readback_keeps_matching_record_uncertain(self):
            from types import SimpleNamespace
            from unittest.mock import Mock
            import time
            from web_operation_agent.tests.support.identity import principal
            from web_operation_agent_twenty.adapter import _TwentyMutationReconciler
            p = principal(issued_at=int(time.time() * 1000),
                          expires_at=int(time.time() * 1000) + 60_000)
            executor = SimpleNamespace(
                resolve_delegated_identity=Mock(return_value=p),
                _normalize_company=lambda args: args,
                _get_companies=Mock(return_value=({'data': {'companies': [
                    {'name': 'Synthetic', 'domainName': {'primaryLinkUrl': 'https://synthetic.example'}}]},
                    'totalCount': 1, '_woaPagination': {'complete': True}}, 200, False)))
            result = _TwentyMutationReconciler(executor, True).reconcile(
                'create_company', {'name': 'Synthetic', 'domain': 'synthetic.example'},
                evidence={}, principal=p)
            self.assertEqual(result.outcome, 'unknown')
            self.assertTrue(result.requires_human)
            self.assertIn('records: 1', result.reason)
            executor._get_companies.assert_called_once()

if __name__ == "__main__":
    unittest.main()
