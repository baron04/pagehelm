"""The index a data port's schema renders into."""
from __future__ import annotations

import unittest

from web_operation_agent.goal.data import EntitySchema, FieldSchema, PageSchema, Schema
from web_operation_agent.goal.index import ApplicationIndex, IndexEntity, IndexPage, IndexRule
from web_operation_agent.goal.resolve.indexing import index_from_schema


class IndexFromSchemaTests(unittest.TestCase):
    def test_large_index_is_kept_whole_or_rejected_explicitly(self):
        entities = tuple(IndexEntity(key=f"item-{n}", label=f"Item {n}") for n in range(300))
        pages = tuple(IndexPage(key=f"/item/{n}", label=f"Page {n}",
                                breadcrumb=f"Pages / {n}", entity=entities[n % 300].key)
                      for n in range(370))
        index = ApplicationIndex(entities=entities, pages=pages)
        self.assertEqual((len(index.entities), len(index.pages)), (300, 370))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            ApplicationIndex(entities=(entities[0],) * 2001)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            ApplicationIndex(pages=(pages[0],) * 4001)

    def test_rules_from_source_sit_on_their_entity_and_the_rest_render_as_before(self):
        schema = Schema(entities=(EntitySchema(key="contract", label="合同", fields=(
            FieldSchema("name", "合同名称", "text", listed=True),)),),
            pages=(PageSchema(key="/contract", entity="contract", label="合同管理"),))
        rule = IndexRule(id="r1", kind="visibility", about="提交审核", text="显示条件：审批状态 === 0",
                         citation="ui/a.vue:1-2@v1 sha256:" + "0" * 64, subject="contract/submit")
        plain = index_from_schema(schema)
        ruled = index_from_schema(schema, rules={"contract": (rule,)}, source_digest="v1")
        self.assertEqual(ruled.entity("contract").rules, (rule,))
        self.assertEqual(ruled.rule("r1"), (ruled.entity("contract"), rule))
        # The full index never lists rules; only a focused one does.
        self.assertEqual(plain.render(), ruled.render())


    def test_the_person_asking_is_named_and_kept_in_a_neighbourhood(self):
        from web_operation_agent.goal.narrow import neighbourhood
        schema = Schema(entities=(EntitySchema(key="customer", label="客户", fields=(
            FieldSchema("owner", "负责人", "text", listed=True),)),),
            pages=(PageSchema(key="/customer", entity="customer", label="客户管理"),))
        index = index_from_schema(schema, actor="评测员")
        self.assertIn("THE PERSON ASKING is 评测员", index.render())
        self.assertIn("THE PERSON ASKING is 评测员", neighbourhood(index, ("customer",)).render())
        self.assertNotIn("PERSON ASKING", index_from_schema(schema).render())


if __name__ == "__main__":
    unittest.main()
