"""Failed reads remain distinct from empty data, permissions and query repairs."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from web_operation_agent.goal.data import DataRefused, Ref
from web_operation_agent.goal.executor import _not_done
from web_operation_agent.goal.resolve.resolver import Ambiguous, Resolver
from web_operation_agent.goal.spec import Deliverable, Filter, Metric


class ReadFailureTests(unittest.TestCase):
    def test_operational_failure_never_drops_conditions_or_exposes_exception_text(self):
        resolver = Resolver.__new__(Resolver)
        resolver._resolve_one = Mock()
        d = Deliverable(kind="aggregate", metric=Metric("sum", field="金额", filters=(
            Filter("Date", ("today",)), Filter("Owner", ("Ada",)))))
        result = resolver._refused_read(d, DataRefused("private endpoint details", read_failed=True))
        resolver._resolve_one.assert_not_called()
        self.assertEqual(result.read_failure, "unavailable")
        self.assertIsNone(result.aggregate)
        for zh, label in ((True, "金额合计"), (False, "total of 金额")):
            answer = _not_done(result, zh, name="明细")
            self.assertIn(label, answer)
            self.assertNotIn("private endpoint", answer)
            self.assertNotIn("权限", answer)

    def test_unclassified_refusal_retains_read_result_without_guessing_permissions(self):
        resolver = Resolver.__new__(Resolver)
        d = Deliverable(kind="aggregate", metric=Metric("count"))
        result = resolver._refused_read(d, DataRefused("403 permission denied"))
        self.assertEqual(result.read_failure, "refused")
        answer = _not_done(result, True, name="合同")
        self.assertIn("合同未完成读取", answer)
        self.assertIn("无法确定数量", answer)
        self.assertNotIn("权限", answer)
        self.assertNotIn("服务暂不可用", answer)

    def test_typed_denial_and_user_facing_requirement_remain_distinct(self):
        resolver = Resolver.__new__(Resolver)
        d = Deliverable(kind="list")
        denied = resolver._refused_read(d, DataRefused("opaque", permission_denied=True))
        self.assertEqual(denied.denied, "read")
        self.assertFalse(denied.read_failure)
        required = resolver._refused_read(d, DataRefused("opaque", reason="请选择时间范围"))
        self.assertIn("请选择时间范围", _not_done(required, True))
        self.assertNotIn("opaque", _not_done(required, True))

    def test_operational_flag_is_not_truthy_prose(self):
        with self.assertRaises(TypeError):
            DataRefused("bad", read_failed="yes")

    def test_duplicate_names_on_different_ids_are_never_a_unique_record(self):
        resolver = Resolver.__new__(Resolver)
        resolver.port = SimpleNamespace(name_of=lambda x: x)
        resolver._chosen = frozenset()
        resolver._find = Mock(return_value=(Ref("people", "1", "Alex"), Ref("people", "2", "Alex")))
        self.assertIsInstance(resolver._find_record("people", "Alex"), Ambiguous)
