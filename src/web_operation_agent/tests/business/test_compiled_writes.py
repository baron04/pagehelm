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
    INHERENT_MASTER_ATTR_ANNOTATION,
    PAGE_DEFAULT_ANNOTATION,
    compiled_write_tool_definitions,
    is_rejected_ui_sentinel,
)
from web_operation_agent.business.convention_writes import (
    CompanionWriteSpec,
    ConventionFieldSpec,
    ConventionUpdateHandler,
    ConventionUpdateSpec,
    ValidationRuleSpec,
    WRITE_PORT_OPTIONAL,
    evaluate_validation_rules,
    extract_projected_field_value,
    format_companion_details,
    normalize_create_arguments,
    resolve_spec_field,
    run_companion_readback,
    write_capabilities_of,
)
from web_operation_agent.business.errors import (
    BusinessMutationIndeterminate,
    BusinessMutationRefusedError,
    BusinessToolRequestError,
)
from web_operation_agent.business.tools.contracts import BusinessMutationProposal


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
        self.drop_child_readback = False

    def derive_child_inherent_fields(self, spec, col_spec, line_payload, line_bindings, *, actor_id=""):
        if spec.entity == "orders" and col_spec.name == "items" and line_payload.get("itemId") == 77:
            return [("unitId", 9, "Box", None)]
        return []

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
            ),
            "orders": CompiledCreateSpec(
                entity="orders",
                label="Order",
                fields=(
                    ConventionFieldSpec("code", "string", label="Code", required=True),
                ),
                collections=(
                    CompiledChildCollectionSpec(
                        name="items",
                        label="Items",
                        min_items=1,
                        fields=(
                            ConventionFieldSpec("itemId", "integer", label="Item", required=True, reference_entity="catalog"),
                            ConventionFieldSpec("unitId", "integer", label="Unit", required=True),
                            ConventionFieldSpec("qty", "number", label="Qty", required=True),
                            ConventionFieldSpec("taxRate", "number", label="TaxRate", required=False),
                        ),
                        defaults=(
                            CompiledFieldDefault("taxRate", "literal", value=0, display="0"),
                        ),
                        subcollections=(
                            CompiledChildCollectionSpec(
                                name="tags",
                                label="Tags",
                                fields=(
                                    ConventionFieldSpec("tag", "string", label="Tag", required=True),
                                ),
                            ),
                        ),
                    ),
                ),
                name_fields=("code",),
            ),
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
        if entity in {"tasks", "orders"}:
            rec = self.created.get(record_id)
            if entity == "orders" and rec and self.drop_child_readback:
                return {"id": rec["id"], "code": rec["code"], "items": []}
            return rec
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
        import json
        from web_operation_agent.business.tools.registry import ToolRegistry, validate_input

        port = _FakeCompiledPort()
        defs = compiled_write_tool_definitions(port, scope="sample:write")
        self.assertEqual([d.name for d in defs], ["create_record", "update_record", "toggle_record_status", "assign_record_relations"])
        registry = ToolRegistry(defs)
        self.assertIn("create_record", {d.name for d in registry.definitions})
        validate_input(
            json.loads(defs[0].input_json),
            {"entity": "orders", "fields": [{"field": "tagIds", "value": [1, 2]}]},
        )
        rel = CompiledRelationHandler(port)
        r_spec = rel.prepare({"entity": "users", "target": "u1", "relation": "roleIds", "items": ["Admin", "Auditor"]})
        r_res = rel.dispatch(r_spec.args)
        self.assertTrue(r_res.content["verified"])
        self.assertEqual(port.relations[11], [1, 2])

    def test_cat5_two_level_master_detail_create_provenance_and_readback(self):
        port = _FakeCompiledPort()
        handler = CompiledCreateHandler(port)
        raw_inputs = normalize_create_arguments({
            "entity": "orders",
            "fields": {"code": "ORD-9"},
            "lines": [
                {
                    "fields": {"itemId": 77, "qty": 3},
                    "sublines": [{"fields": {"tag": "urgent"}}],
                }
            ],
        })
        spec = handler.prepare(raw_inputs)
        expected = CompiledWriteEffects("create_record", port).project_inputs(raw_inputs)
        spec.effect_projection.require(expected)
        details_text = "\n".join(d.text for d in spec.details)
        self.assertIn(f"Box{INHERENT_MASTER_ATTR_ANNOTATION}", details_text)
        self.assertIn(f"0{PAGE_DEFAULT_ANNOTATION}", details_text)
        proposal = BusinessMutationProposal(
            tool="create_record",
            args=spec.args,
            effect=spec.effect,
            targets=spec.targets,
            nonce="0" * 32,
            details=spec.details,
            effect_projection=spec.effect_projection,
        )
        # Child scalar qty=3 supplied by prompt, FK itemId=77 in catalog candidates, subline tag='urgent' in prompt
        handler.validate_provenance(
            raw_inputs,
            ("新建 ORD-9，数量 3，加 urgent 标签",),
            proposal,
            existing_values={"catalog": ("77",), "orders": ("999",)},
        )
        # Cross-record scalar qty='999' only found in candidate ledger must fail closed
        bad_inputs = normalize_create_arguments({
            "entity": "orders",
            "fields": {"code": "ORD-9"},
            "lines": [{"fields": {"itemId": 77, "qty": 999}}],
        })
        with self.assertRaises(BusinessToolRequestError):
            handler.validate_provenance(
                bad_inputs,
                ("新建 ORD-9",),
                proposal,
                existing_values={"catalog": ("77",), "orders": ("999",)},
            )

        res = handler.dispatch(spec.args)
        self.assertTrue(res.content["verified"])
        rec = port.created[int(res.content["record_id"])]
        self.assertEqual(rec["items"][0]["unitId"], 9)
        self.assertEqual(rec["items"][0]["taxRate"], 0)
        self.assertEqual(rec["items"][0]["tags"][0]["tag"], "urgent")

        # Child readback mismatch raises BusinessMutationIndeterminate
        port.drop_child_readback = True
        with self.assertRaises(BusinessMutationIndeterminate):
            handler.dispatch(spec.args)

    def test_companion_write_disclosure_and_same_actor_readback(self):
        comp_verified = CompanionWriteSpec(
            target="audit_log",
            action="新增伴生记录 insert",
            source_method="TaskService.createTask -> AuditLogMapper.insert",
            condition="always",
            readback_entity="audit-logs",
            readback_query_field="taskId",
        )
        comp_unreadable = CompanionWriteSpec(
            target="ledger_counter",
            action="更新汇总统计",
            source_method="TaskService.createTask -> LedgerCounterMapper.updateById",
        )
        details = format_companion_details((comp_verified, comp_unreadable))
        self.assertEqual([d.kind for d in details], ["side_effect", "side_effect"])
        self.assertIn("写后同账号回读：audit-logs", details[0].text)
        self.assertIn("写后未核验（无同账号回读接口）", details[1].text)

        port = _FakeCompiledPort()
        base_task = port.create_specs()["tasks"]
        spec_with_comp = CompiledCreateSpec(
            entity="tasks_comp",
            label="Task",
            fields=base_task.fields,
            defaults=base_task.defaults,
            side_effects=(comp_verified, comp_unreadable),
        )
        port.create_specs = lambda: {"tasks_comp": spec_with_comp}
        companion_outcomes = []

        def verify_companion(sp, record_id, payload, *, before_record, after_record, side_effects):
            if companion_outcomes and companion_outcomes[0] == "mismatch":
                raise BusinessMutationIndeterminate("companion readback mismatch", phase="postcondition")
            return [
                {
                    "effect": "companion_write",
                    "target": se["target"],
                    "verified": bool(se.get("readback_entity")),
                    "note": "同账号回读一致" if se.get("readback_entity") else "写后未核验",
                }
                for se in side_effects
            ]

        port.capabilities = lambda: WRITE_PORT_OPTIONAL
        port.verify_companion_readback = verify_companion
        port.read_created_record = lambda sp, rid, p: dict(port.created[rid])
        handler = CompiledCreateHandler(port)
        mut = handler.prepare({
            "entity": "tasks_comp",
            "fields": [{"field": "title", "value": "T-comp"}, {"field": "parentId", "value": 99}],
        })
        side_texts = [d.text for d in mut.details if d.kind == "side_effect"]
        self.assertEqual(len(side_texts), 2)

        res = handler.dispatch(mut.args)
        comp_fx = [e for e in res.content["effects"] if e.get("effect") == "companion_write"]
        self.assertEqual([(e["target"], e["verified"]) for e in comp_fx], [("audit_log", True), ("ledger_counter", False)])

        # Without port hook, run_companion_readback falls back honestly to unverified
        no_hook = run_companion_readback(
            object(),
            spec_with_comp,
            101,
            mut.args["payload"],
            before_record=None,
            after_record={"id": 101},
            side_effects=mut.args["side_effects"],
        )
        self.assertEqual(len(no_hook), 2)
        self.assertTrue(all(not e["verified"] and "写后未核验" in e["note"] for e in no_hook))

        # Companion mismatch raises BusinessMutationIndeterminate
        companion_outcomes.append("mismatch")
        with self.assertRaises(BusinessMutationIndeterminate):
            handler.dispatch(mut.args)

    def test_validation_rules_evaluate_before_card_and_label_resolution(self):
        fields = (
            ConventionFieldSpec("startDate", "string", label="开始日期", date_kind="date"),
            ConventionFieldSpec("endDate", "string", label="结束日期", date_kind="date"),
            ConventionFieldSpec("amountFen", "integer", label="金额（元）", unit_scale=100, display_unit="元"),
            ConventionFieldSpec("primaryOwnerId", "integer", label="主负责人"),
            ConventionFieldSpec("backupOwnerId", "integer", label="副负责人"),
            ConventionFieldSpec("mode", "string", label="模式"),
            ConventionFieldSpec("reason", "string", label="原因"),
            ConventionFieldSpec("tagA", "string", label="标签A"),
            ConventionFieldSpec("tagB", "string", label="标签B"),
        )
        # Exact key, case-insensitive key, exact label, unique unit-stripped label, ambiguous collision
        self.assertEqual(resolve_spec_field(fields, "startDate").name, "startDate")
        self.assertEqual(resolve_spec_field(fields, "startdate").name, "startDate")
        self.assertEqual(resolve_spec_field(fields, "金额（元）").name, "amountFen")
        self.assertEqual(resolve_spec_field(fields, "金额").name, "amountFen")
        ambig_fields = (
            ConventionFieldSpec("qtyBox", "integer", label="数量(箱)"),
            ConventionFieldSpec("qtyPiece", "integer", label="数量(个)"),
        )
        self.assertIsNone(resolve_spec_field(ambig_fields, "数量"))

        rules = (
            ValidationRuleSpec("date_order", ("startDate", "endDate"), "结束日期不能早于开始日期"),
            ValidationRuleSpec("paired_required", ("startDate", "endDate"), "起止日期须同时填写"),
            ValidationRuleSpec("not_equal", ("primaryOwnerId", "backupOwnerId"), "主副负责人不能为同一人"),
            ValidationRuleSpec(
                "conditional_required",
                ("reason",),
                "特批模式下必须填写原因",
                condition_field="mode",
                condition_values=("special",),
            ),
            ValidationRuleSpec("at_least_one", ("tagA", "tagB"), "标签A与标签B至少填一项"),
        )
        errs = evaluate_validation_rules(
            rules,
            {"startDate": "2026-05-10", "endDate": "2026-05-01", "primaryOwnerId": 7, "backupOwnerId": 7, "mode": "special"},
            lambda tok: resolve_spec_field(fields, tok),
        )
        self.assertEqual(len(errs), 4)
        self.assertTrue(any("结束日期不能早于开始日期" in e for e in errs))
        self.assertTrue(any("主副负责人不能为同一人" in e for e in errs))
        self.assertTrue(any("特批模式下必须填写原因" in e for e in errs))
        self.assertTrue(any("至少填写一项" in e for e in errs))

        # Paired required triggers when only one is supplied
        errs_paired = evaluate_validation_rules(
            rules,
            {"startDate": "2026-05-10", "tagA": "ok"},
            lambda tok: resolve_spec_field(fields, tok),
        )
        self.assertTrue(any("起止日期须同时填写" in e for e in errs_paired))

        # Pre-card enforcement on CompiledCreateHandler using Chinese page labels
        port = _FakeCompiledPort()
        c_spec = CompiledCreateSpec(
            entity="projects",
            label="Project",
            fields=fields,
            validation_rules=rules,
            name_fields=("tagA",),
        )
        port.create_specs = lambda: {"projects": c_spec}
        handler = CompiledCreateHandler(port)
        with self.assertRaises(BusinessToolRequestError) as cm:
            handler.prepare({
                "entity": "projects",
                "fields": [
                    {"field": "开始日期", "value": "2026-05-10"},
                    {"field": "结束日期", "value": "2026-05-01"},
                    {"field": "标签A", "value": "alpha"},
                ],
            })
        self.assertIn("结束日期不能早于开始日期", str(cm.exception))

    def test_string_array_provenance_ref_variants_and_empty_list_default(self):
        port = _FakeCompiledPort()
        c_spec = CompiledCreateSpec(
            entity="boards",
            label="Board",
            fields=(
                ConventionFieldSpec("title", "string", label="标题", required=True),
                ConventionFieldSpec("regionId", "integer", label="地区", reference_entity="regions"),
                ConventionFieldSpec("keywords", "string_array", label="关键词", required=False),
            ),
            defaults=(
                CompiledFieldDefault("keywords", "literal", value=[], display="[]（空列表）"),
            ),
            name_fields=("title",),
        )
        port.create_specs = lambda: {"boards": c_spec}
        port.encode_field = lambda sp, f_sp, raw, cur: (
            (330000, "", "浙江省", {"entity": "regions", "id": 330000})
            if f_sp.name == "regionId" and raw in {"浙江", "浙江省"}
            else (raw, "", str(raw), None)
        )
        port.capabilities = lambda: WRITE_PORT_OPTIONAL
        port.reference_provenance_variants = lambda f_sp, tok: (
            ("浙江", "浙江省") if f_sp.reference_entity == "regions" and tok in {"浙江", "浙江省"} else ()
        )
        handler = CompiledCreateHandler(port)

        # Empty-list default is surfaced on card when omitted
        mut_default = handler.prepare({
            "entity": "boards",
            "fields": [{"field": "标题", "value": "B1"}],
        })
        self.assertEqual(mut_default.args["payload"]["keywords"], [])
        self.assertIn(f"[]（空列表）{PAGE_DEFAULT_ANNOTATION}", "\n".join(d.text for d in mut_default.details))

        # Explicit string_array + FK variant provenance check ("浙江省" matches prompt "浙江")
        raw_in = normalize_create_arguments({
            "entity": "boards",
            "fields": {"标题": "B2", "regionId": "浙江省", "keywords": ["alpha", "beta"]},
        })
        mut = handler.prepare(raw_in)
        self.assertEqual(mut.args["payload"]["regionId"], 330000)
        proposal = BusinessMutationProposal(
            tool="create_record",
            args=mut.args,
            effect=mut.effect,
            targets=mut.targets,
            nonce="1" * 32,
            details=mut.details,
            effect_projection=mut.effect_projection,
        )
        handler.validate_provenance(
            raw_in,
            ("新建看板 B2，地区选浙江，关键词填 alpha 和 beta",),
            proposal,
            existing_values={"regions": ("330000",)},
        )
        # Ungrounded string_array element must be refused
        bad_in = normalize_create_arguments({
            "entity": "boards",
            "fields": {"标题": "B2", "keywords": ["alpha", "invented_kw"]},
        })
        with self.assertRaises(BusinessToolRequestError):
            handler.validate_provenance(bad_in, ("新建看板 B2，关键词填 alpha",), proposal)

    def test_readback_projection_update_and_preservation_ignore(self):
        spec = ConventionUpdateSpec(
            entity="items",
            label="Item",
            fields=(
                ConventionFieldSpec("name", "string", label="名称", required=True),
                ConventionFieldSpec("tag", "string", label="备注"),
                ConventionFieldSpec(
                    "watcherIds",
                    "id_array",
                    label="关注人",
                    readback_projection=("watchers", "userId"),
                ),
            ),
            side_effects=(
                CompanionWriteSpec(
                    target="item_watcher",
                    action="同步关注人列表",
                    source_method="ItemService.updateItem -> ItemWatcherMapper.insertBatch",
                ),
            ),
        )
        store = {
            1: {
                "id": 1,
                "name": "Alpha",
                "tag": "keep-me",
                "watchers": [{"id": 91, "userId": 10}, {"id": 92, "userId": 20}],
            }
        }
        mode = {"drop_watcher_write": False}

        class _Port:
            def update_specs(self):
                return {"items": spec}

            def status_specs(self):
                return {}

            def verify_actor(self, context):
                return "actor-1"

            def resolve_target(self, entity, target, *, spec):
                return dict(store[1])

            def read_record(self, entity, record_id):
                return dict(store[1])

            def encode_field(self, _spec, field, raw_value, cur):
                return raw_value, cur.get(field.name), raw_value, None

            def recheck_bindings(self, bindings, *, actor_id):
                pass

            def dispatch_update(self, _spec, record_id, payload):
                if mode["drop_watcher_write"]:
                    store[1]["name"] = payload["name"]
                    return
                store[1]["name"] = payload["name"]
                store[1]["tag"] = payload["tag"]
                store[1]["watchers"] = [{"id": 100 + i, "userId": uid} for i, uid in enumerate(payload["watcherIds"])]

            def dispatch_status(self, _spec, record_id, status_code):
                pass

            def invalidate_reads(self, entities):
                pass

        port = _Port()
        self.assertEqual(extract_projected_field_value(port, spec, spec.resolve_field("关注人"), store[1]), [10, 20])
        handler = ConventionUpdateHandler(port)
        mut = handler.prepare({
            "entity": "items",
            "target": "Alpha",
            "changes": [{"field": "关注人", "value": [10, 30]}],
        })
        self.assertEqual(mut.args["payload"]["name"], "Alpha")
        self.assertEqual(mut.args["payload"]["tag"], "keep-me")
        self.assertEqual(mut.args["payload"]["watcherIds"], [10, 30])
        self.assertTrue(any(d.kind == "side_effect" and "item_watcher" in d.text for d in mut.details))

        res = handler.dispatch(mut.args)
        self.assertTrue(res.content["verified"])
        self.assertEqual([w["userId"] for w in store[1]["watchers"]], [10, 30])

        # Updating again when projected readback fails must raise BusinessMutationIndeterminate
        mut2 = handler.prepare({
            "entity": "items",
            "target": "Alpha",
            "changes": [{"field": "关注人", "value": [20]}],
        })
        mode["drop_watcher_write"] = True
        with self.assertRaises(BusinessMutationIndeterminate):
            handler.dispatch(mut2.args)

    def test_write_capability_gating_destructive_delete_derived_display_and_negated_boolean(self):
        # 1. Optional capabilities default empty when port does not declare them
        class _UndeclaredPort:
            def extract_preserved_field(self, *_a):
                raise AssertionError("must not call extract_preserved_field without capability")

            def verify_companion_readback(self, *_a, **_kw):
                raise AssertionError("must not call verify_companion_readback without capability")

        undeclared = _UndeclaredPort()
        self.assertEqual(write_capabilities_of(undeclared), frozenset())
        rb = run_companion_readback(
            undeclared,
            object(),
            1,
            {},
            before_record=None,
            after_record={"id": 1},
            side_effects=[{"target": "child_tbl"}],
        )
        self.assertFalse(rb[0]["verified"])

        # 2. Item 17 destructive delete guard + derived_display_fields + negated boolean provenance
        spec = ConventionUpdateSpec(
            entity="plans",
            label="检验计划",
            fields=(
                ConventionFieldSpec("remark", "string", label="备注"),
                ConventionFieldSpec("frozen", "boolean", label="冻结"),
                ConventionFieldSpec(
                    "keeperUserId",
                    "integer",
                    label="保管人",
                    reference_entity="users",
                    derived_display_fields=("keeperName",),
                ),
                ConventionFieldSpec(
                    "planId",
                    "integer",
                    label="关联方案",
                    reference_entity="schemes",
                ),
            ),
            side_effects=(
                CompanionWriteSpec(
                    target="plan_lines",
                    action="清理伴生记录（deleteByPlanId）",
                    source_method="PlanService.updatePlan -> PlanLineMapper.deleteByPlanId",
                    condition="条件成立（涉及 planId）",
                    readback_entity="plan-lines",
                    readback_query_field="planId",
                    destructive_delete=True,
                    trigger_mode="on_field_change",
                    trigger_fields=("planId",),
                ),
            ),
            name_fields=("remark",),
        )
        store = {1: {"id": 1, "remark": "初始", "frozen": True, "keeperUserId": 10, "keeperName": "张三", "planId": 5}}
        child_count = {"n": 3, "fail_count": False, "corrupt_keeper": False}

        class _Port:
            def capabilities(self):
                return WRITE_PORT_OPTIONAL

            def update_specs(self):
                return {"plans": spec}

            def status_specs(self):
                return {}

            def verify_actor(self, context):
                return "actor-1"

            def resolve_target(self, entity, target, *, spec):
                return dict(store[1])

            def read_record(self, entity, record_id):
                return dict(store[1])

            def encode_field(self, _spec, field, raw_value, cur):
                if field.name == "frozen":
                    bval = False if str(raw_value).strip() in {"false", "不冻结", "否"} else bool(raw_value)
                    return bval, cur.get(field.name), str(raw_value), None
                return raw_value, cur.get(field.name), f"User#{raw_value}", None

            def recheck_bindings(self, bindings, *, actor_id):
                pass

            def count_companion_rows(self, _spec, record_id, payload, *, before_record, side_effect, changed=None):
                if child_count["fail_count"]:
                    return None
                return child_count["n"]

            def dispatch_update(self, _spec, record_id, payload):
                store[1].update(payload)
                if payload.get("keeperUserId") == 20:
                    store[1]["keeperName"] = "李四"
                if child_count["corrupt_keeper"]:
                    store[1]["keeperName"] = "意外篡改"

            def verify_companion_readback(self, _spec, record_id, payload, *, before_record, after_record, side_effects):
                return [
                    {"effect": "companion_write", "target": se["target"], "verified": True, "note": "已核验"}
                    for se in side_effects
                ]

            def dispatch_status(self, _spec, record_id, status_code):
                pass

            def invalidate_reads(self, entities):
                pass

        handler = ConventionUpdateHandler(_Port())

        # Negated boolean provenance ("不冻结") passes; non-triggering update succeeds without count read
        mut_neg = handler.prepare({
            "entity": "plans",
            "target": "初始",
            "changes": [{"field": "冻结", "value": "不冻结"}, {"field": "保管人", "value": 20}],
        })
        prop_neg = BusinessMutationProposal(
            tool="update_record",
            args=mut_neg.args,
            effect=mut_neg.effect,
            targets=mut_neg.targets,
            nonce="a" * 32,
            details=mut_neg.details,
            effect_projection=mut_neg.effect_projection,
        )
        handler.validate_provenance(
            mut_neg.args,
            ("把计划改为不冻结，保管人换成 李四", "User#20"),
            prop_neg,
        )
        self.assertIn("本次未触发", "\n".join(d.text for d in mut_neg.details if d.kind == "side_effect"))
        res_ok = handler.dispatch(mut_neg.args)
        self.assertTrue(res_ok.content["verified"])
        self.assertEqual(store[1]["keeperName"], "李四")

        # When keeperUserId is NOT changed, keeperName mutation MUST still fail closed
        mut_unprot = handler.prepare({"entity": "plans", "target": "初始", "changes": [{"field": "备注", "value": "新备注"}]})
        child_count["corrupt_keeper"] = True
        with self.assertRaises(BusinessMutationIndeterminate):
            handler.dispatch(mut_unprot.args)
        child_count["corrupt_keeper"] = False
        store[1]["keeperName"] = "李四"

        # Triggering destructive-delete update discloses live count on card and blocks drift at dispatch
        mut_del = handler.prepare({
            "entity": "plans",
            "target": "初始",
            "changes": [{"field": "关联方案", "value": 9}],
        })
        self.assertIn("受影响 3 条记录", "\n".join(d.text for d in mut_del.details if d.kind == "side_effect"))
        child_count["n"] = 4
        with self.assertRaises(BusinessMutationRefusedError):
            handler.dispatch(mut_del.args)

        # Unreadable pre-dispatch count fails closed before proposal card creation
        child_count["fail_count"] = True
        with self.assertRaises(BusinessToolRequestError):
            handler.prepare({"entity": "plans", "target": "初始", "changes": [{"field": "关联方案", "value": 10}]})


if __name__ == "__main__":
    unittest.main()
