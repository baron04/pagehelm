"""Target handoff uses full actor reads and actual applied requests over ChannelServer."""
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from web_operation_agent.schemas import ActionReceipt
from web_operation_agent.tests.execution.test_native_submit import observation
from web_operation_agent.tests.transport.submit_wire import SubmitWire
from web_operation_agent_yudao.slim_loop import YudaoLoopProcedures, _CURRENT_REQUEST

PATH = "/admin-api/synthetic/entry/page"


def hashed(value):
    return hashlib.sha256(json.dumps([value], separators=(",", ":")).encode()).hexdigest()


class ReadTargetHandoffTests(unittest.TestCase):
    def setUp(self):
        self.query = "pageNo=1&pageSize=10&departmentId=13&remark=SYN"
        read = {"path": PATH, "query_digest": "a" * 64, "revision": 1,
                "page": 1, "page_size": 10, "rows": 1, "total": 1,
                "query_fields": {"departmentId": hashed("13"), "remark": hashed("SYN")}}
        self.obs = replace(observation(), url="http://app/synthetic/entry", page_route="/synthetic/entry",
                           page_read=read, page_query={"path": PATH, "parameters": {}})
        self.wire = SubmitWire(self.obs)
        self.record = {"id": 7, "name": "Synthetic", "no": "SYN-17"}
        self.listed = {"list": [self.record], "total": 1}
        self.row = {"record_id": "7", "name": "Synthetic", "mode": "action", "source": PATH}
        self.calls = []
        self.client = NS(get=lambda *_a, **_kw: None)
        self.knowledge = NS(turn_targets={PATH.rpartition("/")[0]: {"7"}}, _load=lambda: None,
                            _validate_params=lambda path, params: self.calls.append((path, params)))
        self.proc = YudaoLoopProcedures(self.knowledge, NS(data=NS(_client=self.client)), "http://app")
        self.proc.page_reads = lambda: ((PATH, self.query),)
        self.state = NS(obs=self.obs, operation=object(), audit=lambda *_a, **_kw: None,
                        page=NS(checkpoint=lambda: None, execute=self.execute))
        self.bound = NS(_invoke=lambda method, path, **params: self.listed)
        self.patch = patch("web_operation_agent_yudao.bound_data_reads.BoundDataReads", return_value=self.bound)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def execute(self, action, args, **_kwargs):
        issued = self.wire.command(action, args)
        after = replace(self.state.obs, epoch=self.state.obs.epoch + 1,
                        elements=[replace(e, epoch=self.state.obs.epoch + 1) for e in self.state.obs.elements])
        receipt = ActionReceipt(1, issued.action_id, action, issued.command.args, True,
            observation=after, url_before=self.state.obs.url, url_after=after.url, form_data=self.row if action == "form_model" else None)
        returned = self.wire.result(issued, receipt)
        self.state.obs = returned.observation
        return NS(state="completed", dispatch="acknowledged", receipt=returned)

    def ready(self, words="审批时间最早的那张"):
        return self.proc._list_handoff_target(self.state, self.state.obs.elements[-1], words)

    def test_full_read_selection_and_non_name_query_bind_the_unique_real_row(self):
        self.assertTrue(self.ready())
        self.assertEqual(self.calls[-1], (PATH, {"pageNo": "1", "pageSize": "10", "departmentId": "13", "remark": "SYN"}))
        self.assertEqual(self.wire.commands[-1].args["kind"], "row")

    def test_indexed_array_queries_keep_the_actual_page_spelling_and_reject_writing_gets(self):
        self.query += "&createdAt%5B0%5D=2026-11-01&createdAt%5B1%5D=2026-11-30"
        read = {**self.state.obs.page_read, "query_fields": {**self.state.obs.page_read["query_fields"],
            "createdAt[0]": hashed("2026-11-01"), "createdAt[1]": hashed("2026-11-30")}}
        self.state.obs = replace(self.state.obs, page_read=read)
        self.assertTrue(self.ready())
        self.assertEqual(self.calls[-1][1]["createdAt"], ["2026-11-01", "2026-11-30"])
        self.knowledge.writing_gets = {PATH}
        self.assertFalse(self.ready())

    def test_a_first_page_or_visible_single_row_is_not_a_complete_read(self):
        self.knowledge.turn_targets = {}
        self.assertFalse(self.ready())
        self.assertTrue(self.ready("审批SYN-17"))
        self.assertFalse(self.ready("审批SYN-170"))

    def test_actual_applied_query_digest_and_fresh_actor_identity_are_required(self):
        for change in ("query", "total", "id", "source", "page"):
            with self.subTest(change=change):
                self.setUp()
                if change == "query": self.query = self.query.replace("departmentId=13", "departmentId=14")
                if change == "total": self.listed["total"] = 2
                if change == "id": self.listed["list"] = [{"id": 8}]
                if change == "source": self.row["source"] = PATH.replace("entry", "other")
                if change == "page": self.state.obs = replace(self.obs, page_read={**self.obs.page_read, "page": 2})
                self.assertFalse(self.ready())

    def highlight(self, label):
        before = self.state.obs
        target = replace(before.elements[-1], name=label, attrs={"record_id": "7", "record_table": "live-table"})
        self.state.obs = replace(before, elements=[*before.elements[:-1], target], content=f"[3]<button>{label} />")
        self.wire = SubmitWire(self.state.obs)
        self.state.obs = self.wire.channel.wait_for_observation(timeout_s=0).observation
        token = _CURRENT_REQUEST.set("处理本轮选出的那条记录")
        try:
            return self.proc._tool(self.state, "highlight", {"index": 3}, {"marked": ""})
        finally:
            _CURRENT_REQUEST.reset(token)

    def test_unlisted_action_words_use_the_real_row_binding_before_highlight(self):
        for label in ("关闭", "作废", "禁用", "上架", "自定义状态动作"):
            with self.subTest(label=label):
                self.setUp()
                result = self.highlight(label)
                self.assertEqual(result.state, "completed")
                self.assertEqual([command.action for command in self.wire.commands], ["form_model", "highlight"])
        for kind in ("first_page", "many_rows", "no_applied_read", "changed_row"):
            with self.subTest(kind=kind):
                self.setUp()
                if kind == "first_page": self.knowledge.turn_targets = {}
                if kind == "many_rows":
                    self.state.obs = replace(self.state.obs, page_read={**self.state.obs.page_read, "total": 2, "rows": 2})
                if kind == "no_applied_read": self.state.obs = replace(self.state.obs, page_read=None)
                if kind == "changed_row": self.row["record_id"] = "8"
                result = self.highlight("关闭")
                self.assertEqual((result.state, result.error_code), ("refused", "invalid_target"))
                self.assertNotIn("highlight", [command.action for command in self.wire.commands])

    def test_detail_target_from_complete_turn_read_still_requires_the_current_actor_record(self):
        path = "/synthetic/entry/detail/7"
        self.state.obs = replace(self.state.obs, url="http://app" + path, page_route=path, record_route=path)
        self.proc.goal_port = NS(data=NS(_ui=NS(records={"entry": "/synthetic/entry/detail/{id}"}),
            _entities={"entry": NS(has_get=True, get_path="synthetic/entry/get")}, _client=self.client,
            _display=lambda *_: "Synthetic"), index=lambda **_: NS(pages=[NS(key="entry", entity="entry")]))
        self.bound._invoke = lambda *_a, **_kw: self.record
        self.bound.record_address = lambda *_: path
        self.bound.find = lambda *_a, **_kw: ()
        self.assertTrue(self.proc._detail_handoff_target(self.state, "审批最早的那张"))
        self.knowledge.turn_targets = {}
        self.assertFalse(self.proc._detail_handoff_target(self.state, "审批最早的那张"))
        self.knowledge.turn_targets = {PATH.rpartition("/")[0]: {"7"}}
        self.record["id"] = 8
        self.assertFalse(self.proc._detail_handoff_target(self.state, "审批最早的那张"))


if __name__ == "__main__":
    unittest.main()
