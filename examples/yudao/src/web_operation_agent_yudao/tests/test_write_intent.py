"""Application wording never turns draft work into a Skill write."""
import unittest

from web_operation_agent_yudao.write_intent import classify_write_intent, preceding_draft_handoff
from web_operation_agent_yudao.slim_loop import SlimTurnExecutor, _WRITE_INTENT


class WriteIntentTests(unittest.TestCase):
    def test_draft_save_mixed_and_follow_up(self):
        examples = {
            "先填一位联系人，名字为顾溪": "draft",
            "预填资料，无须立即保存": "draft",
            "准备给晨岚机电新增联系人": "draft",
            "先填 A 不提交，再新建 B": "draft",
            "先填好，然后提交": "save",
            "替我提交一份新的 CRM 合同": "save",
            "帮我来提交一份新的 CRM 合同": "save",
            "把这份合同存为草稿，我来提交": "draft",
            "把税率改成9%": "save",
            "把晶华电子的联系人手机号换成新的": "save",
            "把联系人手机号更换为13800000002": "save",
            "如何更换联系人手机号？": "neutral",
            "联系人手机号已经更换了吗？": "neutral",
            "更换后的手机号是什么？": "neutral",
            "新建供应商": "save",
            "打开新建供应商页面": "neutral",
            "怎么新建供应商": "neutral",
            "不用新建供应商，查一下即可": "neutral",
            "不用创建记录，只查已有的": "neutral",
            "未审核的销售订单有几张？": "neutral",
            "我提交的请假申请一共有几条？": "neutral",
            "还没提交审批（草稿）的合同有几份？": "neutral",
            "把合同按审核状态分组：草稿、审批中各几份？": "neutral",
            "草稿状态、金额超过 6 万的合同有几份？": "neutral",
            "办公耗材里停用的产品有哪些？": "neutral",
            "打开关闭状态的仓库列表": "neutral",
            "分别创建了几张采购入库单？": "neutral",
            "查一下供应商": "neutral",
        }
        for request, expected in examples.items():
            with self.subTest(request=request):
                self.assertEqual(expected, classify_write_intent(request).mode)
        self.assertEqual("draft", classify_write_intent("把邮箱也清空", prior_draft=True).mode)
        self.assertEqual("save", classify_write_intent("现在保存", prior_draft=True).mode)

    def test_draft_tool_list_hides_skill_and_loaded_operations(self):
        class Writes:
            def definitions(self):
                return [type("Definition", (), {"name": "crm_contact_create"})()]
        executor = SlimTurnExecutor.__new__(SlimTurnExecutor)
        offered = [{"function": {"name": name}} for name in (
            "find_record", "fill_fields", "load_skill", "read_skill_resource",
            "crm_contact_create", "finish")]
        parent = SlimTurnExecutor.__mro__[1]
        old = parent._tools
        parent._tools = lambda self, turn: offered
        token = _WRITE_INTENT.set(classify_write_intent("先填联系人，不提交"))
        try:
            self.assertEqual([tool["function"]["name"] for tool in executor._tools({"writes": Writes()})],
                             ["find_record", "fill_fields", "finish"])
        finally:
            _WRITE_INTENT.reset(token)
            parent._tools = old

    def test_only_completed_draft_handoff_carries_to_follow_up(self):
        draft = {"user": "先填客户资料，不保存", "assistant": "请检查表单后手动提交。",
                 "status": "finished"}
        self.assertTrue(preceding_draft_handoff([draft]))
        self.assertTrue(preceding_draft_handoff([draft,
            {"user": "把邮箱也清空", "assistant": "请检查表单后手动提交。", "status": "finished"}]))
        self.assertFalse(preceding_draft_handoff([draft,
            {"user": "把邮箱也清空", "assistant": "未能填写", "status": "refused"}]))
        self.assertFalse(preceding_draft_handoff([{"user": draft["user"],
            "assistant": "未能填写", "status": "finished"}]))


if __name__ == "__main__":
    unittest.main()
