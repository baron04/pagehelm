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
            "在 ERP 客户编辑表单里将恒达印务的邮箱改成 sales@hengda.example.test，手机号照旧。": "draft",
            "在客户编辑表单中把邮箱改成新地址，留给我确认": "draft",
            "打开客户编辑表单改好邮箱后保存": "save",
            "在表单里改成新地址并提交": "save",
            "如何在客户编辑表单里改邮箱？": "neutral",
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
            "用户按创建时间从晚到早，列出姓名和部门": "neutral",
            "按新增日期排序": "neutral",
            "这些已审核订单里还没入库的多少": "neutral",
            "已经审批的申请有多少": "neutral",
            "创建一个用户": "save",
            "审核这些订单": "save",
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


    def test_read_queries_do_not_select_business_review_but_mixed_obligations_do(self):
        from unittest.mock import patch
        from web_operation_agent_yudao.slim_loop import _CURRENT_REQUEST
        parent = SlimTurnExecutor.__mro__[1]
        executor = SlimTurnExecutor.__new__(SlimTurnExecutor)
        cases = {
            "用户按创建时间列出姓名和部门": False,
            "这些已审核订单里还没入库的多少": False,
            "创建一个用户": True,
            "审核这些订单": True,
            "名称甲备注乙先填不保存": True,
            "打开用户页面，并列出姓名和部门": True,
            "列出供应商名称，并查看供应商页面": True,
            "列出供应商名称，并查看供应商详情": True,
            "查看供应商金额": False,
            "查看供应商名单": False,
            "查看供应商名称字段": False,
            "在用户列表筛选财务部门，再统计人数": True,
        }
        for request, expected in cases.items():
            with self.subTest(request=request), patch.object(parent, "_tools", return_value=[]):
                current = _CURRENT_REQUEST.set(request)
                intent = _WRITE_INTENT.set(classify_write_intent(request))
                turn = {"writes": None, "query_answers": {"read_1": object()}}
                try:
                    executor._tools(turn)
                    self.assertEqual(expected, bool(turn.get("review_completion")))
                finally:
                    _CURRENT_REQUEST.reset(current)
                    _WRITE_INTENT.reset(intent)

    def test_prepare_does_not_inject_save_skill_instructions(self):
        import contextlib
        from types import SimpleNamespace as NS
        from unittest.mock import patch
        from web_operation_agent_yudao.slim_loop import YudaoLoopProcedures
        knowledge = NS(client=NS(read_window=lambda check: contextlib.nullcontext()),
                       _load=lambda: None, suggest=lambda prompt: "已发布的查询入口")
        owner = YudaoLoopProcedures(knowledge, NS(data=object()), "https://synthetic.test")
        context = NS(operation=object(), page=NS(checkpoint=lambda: None))
        intent = _WRITE_INTENT.set(classify_write_intent("创建一个用户"))
        try:
            with patch("web_operation_agent_yudao.bound_data_reads.BoundDataReads",
                       return_value=NS(_check=lambda: None)):
                self.assertEqual(owner.prepare("创建一个用户", context), "已发布的查询入口")
        finally:
            _WRITE_INTENT.reset(intent)

    def test_a_plain_navigation_does_not_add_a_data_completion_review(self):
        from unittest.mock import patch
        from web_operation_agent_yudao.slim_loop import _CURRENT_REQUEST
        executor = SlimTurnExecutor.__new__(SlimTurnExecutor)
        current = _CURRENT_REQUEST.set("打开用户页面")
        intent = _WRITE_INTENT.set(classify_write_intent("打开用户页面"))
        try:
            with patch.object(SlimTurnExecutor.__mro__[1], "_tools", return_value=[]):
                turn = {"writes": None}
                executor._tools(turn)
                self.assertFalse(turn.get("review_completion"))
        finally:
            _CURRENT_REQUEST.reset(current)
            _WRITE_INTENT.reset(intent)

    def test_legacy_delivery_is_normalized_before_schema_validation(self):
        from unittest.mock import patch
        executor = SlimTurnExecutor.__new__(SlimTurnExecutor)
        original = {"path": "orders/page", "fields": ["name"], "deliver": True,
                    "summary": {"columns": ["name"], "order_by": [
                        {"column": "name", "direction": "asc", "nulls": "last"}]}}
        with patch.object(SlimTurnExecutor.__mro__[1], "_invoke_tool", return_value="ok") as parent:
            self.assertEqual("ok", executor._invoke_tool(None, "call_api", original, {}))
            normalized = parent.call_args.args[2]
            self.assertNotIn("deliver", normalized)
            self.assertEqual(original["summary"], normalized["summary"])
            self.assertIn("deliver", original)

    def test_invalid_legacy_delivery_cannot_fall_back_to_a_partial_plain_read(self):
        from unittest.mock import patch
        executor = SlimTurnExecutor.__new__(SlimTurnExecutor)
        for old in ({"path": "orders/page", "deliver": True},
                    {"path": "orders/page", "deliver": True, "fields": ["name"], "row_offset": 1}):
            with self.subTest(arguments=old), patch.object(
                    SlimTurnExecutor.__mro__[1], "_invoke_tool") as parent:
                result = executor._invoke_tool(None, "call_api", old, {})
                self.assertEqual("refused", result.state)
                self.assertEqual("invalid_arguments", result.error_code)
                self.assertIsNone(result.query_answer)
                parent.assert_not_called()

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
