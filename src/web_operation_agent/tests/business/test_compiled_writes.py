"""Unit tests for neutral CompiledWrites contracts (D1-D3, 2-level nested aggregates, relations)."""
from __future__ import annotations

from types import SimpleNamespace
import unittest

from web_operation_agent.business.compiled_writes import (
    CompiledAggregateUpdateHandler,
    CompiledAggregateUpdateSpec,
    CompiledChildCollectionSpec,
    CompiledCreateHandler,
    CompiledCreateSpec,
    CompiledFieldDefault,
    CompiledRelationHandler,
    CompiledRelationSpec,
    CompiledWriteEffects,
    PAGE_DEFAULT_ANNOTATION,
    compiled_write_tool_definitions,
    is_rejected_ui_sentinel,
)
from web_operation_agent.business.convention_writes import ConventionFieldSpec
from web_operation_agent.business.errors import BusinessToolRequestError


class _FakeCompiledPort:
    def __init__(self):
        self.vouchers = {
            (10, 501): {
                "id": 501,
                "accountSetId": 10,
                "voucherNumber": 1,
                "remark": "Old",
                "entries": [
                    {
                        "id": 1,
                        "digest": "Office supplies",
                        "debitAmount": 100,
                        "auxiliaries": [
                            {"typeId": 1, "itemId": 1001, "name": "R&D"},
                            {"typeId": 2, "itemId": 2001, "name": "HQ"},
                        ],
                    },
                    {
                        "id": 2,
                        "digest": "Cash",
                        "debitAmount": 0,
                        "creditAmount": 100,
                        "auxiliaries": [],
                    },
                ],
            }
        }
        self.created = {}
        self.relations = {11: [1]}
        self.invalidated = []

    def update_specs(self):
        return {}

    def status_specs(self):
        return {}

    def create_specs(self):
        return {
            "tasks": CompiledCreateSpec(
                entity="tasks",
                label="Task",
                fields=(
                    ConventionFieldSpec("title", "string", label="Title", required=True),
                    ConventionFieldSpec("ownerId", "integer", label="Owner", required=True, reference_entity="users"),
                    ConventionFieldSpec("parentId", "integer", label="Parent", required=True, reference_entity="tasks"),
                    ConventionFieldSpec("probationMonths", "integer", label="Probation", required=True),
                ),
                defaults=(
                    CompiledFieldDefault("ownerId", "actor", display="Alice"),
                    CompiledFieldDefault("parentId", "literal", value=0),
                    CompiledFieldDefault("probationMonths", "literal", value=0, display="0"),
                ),
                name_fields=("title",),
            )
        }

    def aggregate_update_specs(self):
        return {
            "vouchers": CompiledAggregateUpdateSpec(
                entity="vouchers",
                label="Voucher",
                fields=(
                    ConventionFieldSpec("accountSetId", "integer", label="AccountSet", required=True),
                    ConventionFieldSpec("voucherNumber", "integer", label="No", required=True),
                    ConventionFieldSpec("remark", "string", label="Remark", required=False),
                ),
                collections=(
                    CompiledChildCollectionSpec(
                        name="entries",
                        label="Entries",
                        fields=(
                            ConventionFieldSpec("digest", "string", label="Digest", required=True),
                            ConventionFieldSpec("debitAmount", "number", label="Debit", required=False),
                            ConventionFieldSpec("creditAmount", "number", label="Credit", required=False),
                        ),
                        name_fields=("digest",),
                        min_items=2,
                        subcollections=(
                            CompiledChildCollectionSpec(
                                name="auxiliaries",
                                label="Auxiliaries",
                                fields=(
                                    ConventionFieldSpec("typeId", "integer", label="TypeId", required=True),
                                    ConventionFieldSpec("itemId", "integer", label="ItemId", required=True),
                                ),
                                name_fields=("typeId",),
                            ),
                        ),
                    ),
                ),
                name_fields=("voucherNumber",),
                composite_id_fields=("accountSetId", "id"),
            )
        }

    def relation_specs(self):
        return {
            "users": (
                CompiledRelationSpec(
                    entity="users",
                    label="User",
                    relation="roleIds",
                    relation_label="Roles",
                    target_entity="roles",
                    action_path="users/assign-roles",
                    read_path="users/list-roles",
                    target_param="userId",
                    items_param="roleIds",
                ),
            )
        }

    def verify_actor(self, context) -> str:
        return "42"

    def resolve_target(self, entity: str, target: str, *, spec):
        if entity == "vouchers":
            return dict(self.vouchers[(10, 501)])
        return {"id": 11, "name": target}

    def read_record(self, entity: str, record_id):
        if entity == "tasks":
            return self.created.get(record_id)
        return dict(self.vouchers[(10, 501)])

    def read_aggregate_record(self, spec, record_id, *, composite_id=None):
        aset = (composite_id or {}).get("accountSetId", 10)
        return dict(self.vouchers[(aset, record_id)])

    def encode_field(self, spec, field, raw_value, current_record):
        return raw_value, str(current_record.get(field.name) or ""), str(raw_value), None

    def recheck_bindings(self, bindings, *, actor_id):
        return None

    def dispatch_create(self, spec, payload):
        nid = 100 + len(self.created) + 1
        self.created[nid] = {"id": nid, **payload}
        return nid

    def dispatch_aggregate_update(self, spec, record_id, payload, *, current_record):
        aset = payload["accountSetId"]
        self.vouchers[(aset, record_id)] = {
            **self.vouchers[(aset, record_id)],
            **payload,
        }
        return True

    def read_aggregate_after_update(self, spec, record_id, dispatch_result, payload, *, composite_id=None):
        aset = payload.get("accountSetId", 10)
        return dict(self.vouchers[(aset, record_id)])

    def dispatch_update(self, spec, record_id, payload):
        pass

    def dispatch_status(self, spec, record_id, status_code):
        pass

    def resolve_relation_items(self, spec, items):
        mapping = {"Admin": 1, "Auditor": 2}
        return [mapping[x] for x in items], list(items), []

    def read_relation_ids(self, spec, target_id):
        return list(self.relations[target_id])

    def relation_labels_for_ids(self, spec, ids):
        rev = {1: "Admin", 2: "Auditor"}
        return [rev[x] for x in ids]

    def dispatch_relation(self, spec, target_id, item_ids):
        self.relations[target_id] = list(item_ids)

    def invalidate_reads(self, entities):
        self.invalidated.append(entities)


class CompiledWritesTests(unittest.TestCase):
    def test_d3_sentinel_rejection_vs_scalar_zero(self):
        self.assertTrue(is_rejected_ui_sentinel(0, is_fk_or_enum=True))
        self.assertTrue(is_rejected_ui_sentinel(-1, is_fk_or_enum=True))
        self.assertTrue(is_rejected_ui_sentinel(0, is_hierarchy_parent=True))
        self.assertTrue(is_rejected_ui_sentinel("", is_fk_or_enum=False))
        self.assertTrue(is_rejected_ui_sentinel("NaN", is_fk_or_enum=False))
        self.assertTrue(is_rejected_ui_sentinel(float("nan"), is_fk_or_enum=False))
        self.assertFalse(is_rejected_ui_sentinel(0, is_fk_or_enum=False))

    def test_d3_omits_hierarchy_parent_zero_while_scalar_zero_shows_annotation(self):
        port = _FakeCompiledPort()
        handler = CompiledCreateHandler(port)
        # Missing parentId whose UI default was 0 sentinel triggers explicit need_input refusal
        with self.assertRaises(BusinessToolRequestError) as cm:
            handler.prepare({"entity": "tasks", "fields": [{"field": "title", "value": "T1"}]})
        self.assertIn("parentId", str(cm.exception))
        self.assertIn("不静默代填", str(cm.exception))

        # Supplying parentId succeeds and shows actor D1 + scalar zero D2 with （页面默认值）
        spec = handler.prepare({
            "entity": "tasks",
            "fields": [
                {"field": "title", "value": "T1"},
                {"field": "parentId", "value": 99},
            ],
        })
        details_text = "\n".join(d.text for d in spec.details)
        self.assertIn(f"Alice{PAGE_DEFAULT_ANNOTATION}", details_text)
        self.assertIn(f"0{PAGE_DEFAULT_ANNOTATION}", details_text)

    def test_two_level_nested_aggregate_preserves_siblings_and_parent(self):
        port = _FakeCompiledPort()
        agg = CompiledAggregateUpdateHandler(port)
        inputs = {
            "entity": "vouchers",
            "target": "1",
            "line_changes": [
                {
                    "collection": "entries",
                    "match": "#1",
                    "subcollection": "auxiliaries",
                    "submatch": "#1",
                    "changes": [{"field": "itemId", "value": 1009}],
                }
            ],
        }
        spec = agg.prepare(inputs)
        expected = CompiledWriteEffects("update_record", port).project_inputs(inputs)
        spec.effect_projection.require(expected)
        res = agg.dispatch(spec.args)
        self.assertTrue(res.content["verified"])
        after = port.vouchers[(10, 501)]
        self.assertEqual(after["remark"], "Old")
        self.assertEqual(after["entries"][0]["auxiliaries"][0]["itemId"], 1009)
        self.assertEqual(after["entries"][0]["auxiliaries"][1]["itemId"], 2001)
        self.assertEqual(after["entries"][1]["creditAmount"], 100)

    def test_tool_definitions_validate_under_strict_registry_schema(self):
        port = _FakeCompiledPort()
        defs = compiled_write_tool_definitions(port, scope="sample:write")
        self.assertEqual([d.name for d in defs], ["create_record", "update_record", "toggle_record_status", "assign_record_relations"])
        rel = CompiledRelationHandler(port)
        r_spec = rel.prepare({"entity": "users", "target": "u1", "relation": "roleIds", "items": ["Admin", "Auditor"]})
        r_res = rel.dispatch(r_spec.args)
        self.assertTrue(r_res.content["verified"])
        self.assertEqual(port.relations[11], [1, 2])


if __name__ == "__main__":
    unittest.main()
