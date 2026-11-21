"""The fixed goal coverage mapping: thirty reviewed operations, one package per business goal.

Each package is an independently judgeable goal; the mapping below is the reviewed
artifact, and shared reference material is assembled from one source instead of
being copied into packages. This test does not establish model success rates.
"""
import unittest
from importlib import resources

from ..rules.skill_packages import _SHARED_REFERENCES, packaged_skills
from ..writes.write_definitions import RECORD_OPERATIONS

GOAL_PACKAGES = {
    "crm-customer-creation": ("crm_customer_create",),
    "crm-contact-creation": ("crm_contact_create",),
    "crm-customer-update": ("crm_customer_update",),
    "crm-contact-update": ("crm_contact_update",),
    "crm-customer-pool-release": ("crm_customer_put_pool",),
    "crm-customer-receive": ("crm_customer_receive",),
    "crm-customer-lock": ("crm_customer_lock",),
    "crm-customer-deal-status": ("crm_customer_deal_status",),
    "crm-clue-maintenance": ("crm_clue_create", "crm_clue_update"),
    "crm-follow-up": ("crm_follow_up_create",),
    "crm-receivable-planning": ("crm_receivable_plan_create",),
    "erp-supplier-maintenance": ("erp_supplier_create", "erp_supplier_update"),
    "erp-warehouse-maintenance": ("erp_warehouse_create", "erp_warehouse_update", "erp_account_create", "erp_account_update", "erp_document_status"),
    "erp-customer-maintenance": ("erp_customer_create", "erp_customer_update"),
    "erp-product-maintenance": ("erp_product_create", "erp_product_update", "erp_product_unit_create", "erp_product_unit_update"),
    "system-post-creation": ("system_post_create", "system_post_update"),
    "system-dept-creation": ("system_dept_create", "system_dept_update"),
    "convention-record-mutation": ("create_record", "update_record", "toggle_record_status", "assign_record_relations"),
    "system-notice-maintenance": ("system_notice_create",),
    "member-tag-creation": ("member_tag_create",),
    "member-group-creation": ("member_group_create",),
    "product-brand-maintenance": ("product_brand_create", "product_brand_update", "promotion_coupon_template_status", "promotion_seckill_activity_close"),
    "product-category-creation": ("product_category_create",),
    "bpm-category-maintenance": ("bpm_category_create",),
    "oa-announcement-publication": ("oa_announcement_create",),
}

DECLARATIONS = ("适用目标", "相邻目标", "所需用户信息", "利用当前事实的流程", "完成条件")


class SkillGoalCoverage(unittest.TestCase):
    def test_thirty_operations_map_once_each_onto_goal_scoped_packages(self):
        registry = packaged_skills()
        self.assertEqual({s.name for s in registry.artifacts}, set(GOAL_PACKAGES))
        covered = {}
        for skill in registry.artifacts:
            self.assertEqual(skill.write_operations, GOAL_PACKAGES[skill.name])
            for operation in skill.write_operations:
                self.assertNotIn(operation, covered, "operation is mapped by two goals")
                covered[operation] = skill.name
        self.assertEqual(len(covered), 43)
        self.assertEqual(set(covered), set(RECORD_OPERATIONS) | {
            "crm_customer_put_pool", "crm_customer_receive", "crm_customer_lock",
            "crm_customer_deal_status", "crm_follow_up_create",
            "crm_receivable_plan_create", "oa_announcement_create",
            "erp_document_status", "promotion_coupon_template_status",
            "promotion_seckill_activity_close", "create_record", "update_record",
            "toggle_record_status", "assign_record_relations"})

    def test_each_goal_declares_completion_conditions_and_adjacent_goals(self):
        for skill in packaged_skills().artifacts:
            for marker in DECLARATIONS:
                self.assertIn(marker, skill.body, f"{skill.name} lacks {marker}")

    def test_shared_references_assembled_from_single_source_and_used_by_bodies(self):
        by_name = {s.name: s for s in packaged_skills().artifacts}
        for name, packages in _SHARED_REFERENCES.items():
            for package in packages:
                skill = by_name[package]
                self.assertIn(f"references/{name}", skill.text_resources)
                self.assertIn(f"references/{name}", skill.body, "assembled material is not reused")
            for skill in by_name.values():
                if skill.name not in packages:
                    self.assertNotIn(f"references/{name}", skill.text_resources)

    def test_package_tree_holds_no_private_copies_of_shared_material(self):
        root = resources.files("web_operation_agent_yudao").joinpath("skills")
        for package in root.iterdir():
            self.assertFalse(package.joinpath("references").is_dir(),
                             f"{package.name} keeps a private references copy")
