"""Effect bindings participate in approval and journal settlement."""
import tempfile
import json
import unittest
from dataclasses import replace
from pathlib import Path

from web_operation_agent.business.effects import RequestedEffect, EffectProjection, canonical
from web_operation_agent.business.errors import BusinessMutationIndeterminate, BusinessToolError
from web_operation_agent.business.mutation import MemoryMutationJournal
from web_operation_agent.business.runtime import BusinessToolRuntime
from web_operation_agent.tests.support import business as fixture


class EffectProjectionTests(unittest.TestCase):
    def test_incomplete_acknowledgement_is_journaled_once_and_cannot_be_replayed(self):
        class Executor(fixture._MutationExecutor):
            @staticmethod
            def prepare_mutation(name, args, **kwargs):
                spec = fixture._MutationExecutor.prepare_mutation(name, args, **kwargs)
                effect = RequestedEffect("companies", "create", values_json=canonical(spec.args))
                return replace(spec, effect_projection=EffectProjection(effect,
                    required_receipts=("record", "related_record")))
            def execute_mutation(self, name, args, **kwargs):
                result = super().execute_mutation(name, args, **kwargs)
                return replace(result, content={**result.content, "record_id": "2", "verified": True,
                    "effects": [{"effect": "record", "verified": True, "id": "2"}]})
        with tempfile.TemporaryDirectory() as tmp:
            skill = fixture._write_skill(Path(tmp)/"skill.json")
            journal, executor = MemoryMutationJournal(), Executor()
            runtime = BusinessToolRuntime((skill,), executor, mutation_journal=journal)
            proposal = runtime.prepare_mutation("create_company", {"name": "Synthetic Harbor"}, {skill.skill_id})
            projection = proposal.effect_projection
            forged = replace(proposal, effect_projection=replace(projection, required_receipts=("record",)))
            self.assertNotEqual(proposal.content_digest, forged.content_digest)
            with self.assertRaises(BusinessToolError):
                runtime.execute_mutation(forged, proposal.content_digest, {skill.skill_id})
            self.assertEqual(executor.executions, 0)
            with self.assertRaises(BusinessMutationIndeterminate):
                runtime.execute_mutation(proposal, proposal.content_digest, {skill.skill_id})
            entry = journal.get(proposal.content_digest)
            self.assertEqual(entry.state, "indeterminate")
            self.assertEqual(json.loads(entry.reconciliation_json)["record_id"], "2")
            with self.assertRaises(BusinessToolError):
                runtime.execute_mutation(proposal, proposal.content_digest, {skill.skill_id})
            self.assertEqual(executor.executions, 1)


if __name__ == "__main__":
    unittest.main()
