"""A local synthetic HTTP service proves Yudao's real one-shot transport evidence."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from web_operation_agent.business.dispatch_evidence import mutation_evidence
from web_operation_agent.business.errors import (BusinessMutationIndeterminate,
    BusinessMutationRefusedError, BusinessToolPermissionError)
from web_operation_agent.execution.events import TurnEvents
from web_operation_agent_yudao.reads.client import YudaoClient
from web_operation_agent_yudao.writes.write_definitions import BACKEND_WRITE_ATOMICITY, RECORD_OPERATIONS


class DispatchEvidenceTests(unittest.TestCase):
    def test_every_native_operation_has_a_reviewed_backend_service_contract(self):
        from web_operation_agent_yudao.writes.write_special import STATE_PATHS
        self.assertEqual(set(BACKEND_WRITE_ATOMICITY), set(RECORD_OPERATIONS) | set(STATE_PATHS) |
                         {"crm_receivable_plan_create", "oa_announcement_create", "crm_follow_up_create",
                      "erp_document_status", "promotion_coupon_template_status", "promotion_seckill_activity_close",
                      "create_record", "update_record", "toggle_record_status", "assign_record_relations"})
        self.assertFalse(BACKEND_WRITE_ATOMICITY["crm_follow_up_create"][0])

    def test_native_ack_refusal_invalid_ack_and_partial_error_remain_distinct(self):
        replies = [({"code": 0, "data": 31}, "applied", None),
            ({"code": 403}, "rejected", BusinessToolPermissionError),
            ({"code": 400, "msg": "请求参数类型错误:synthetic-invalid"}, "rejected", BusinessMutationRefusedError),
            ({"code": 400, "msg": "请求参数不正确:synthetic-invalid"}, "unknown", BusinessMutationIndeterminate),
            ({"code": 0}, "unknown", BusinessMutationIndeterminate),
            ({"code": 500, "msg": "server token=synthetic-secret"}, "unknown", BusinessMutationIndeterminate)]
        requests = []
        selected = [None]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                requests.append(self.path)
                body = json.dumps(selected[0]).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = YudaoClient.for_token(f"http://127.0.0.1:{server.server_port}", "synthetic-test-token", "1")
            for position, (reply, expected, error) in enumerate(replies):
                with self.subTest(reply=reply):
                    selected[0] = reply
                    events = TurnEvents()
                    with events.turn("normal-turn"), mutation_evidence(
                        SimpleNamespace(content_digest="digest"), SimpleNamespace(approval_id="approval"),
                        SimpleNamespace(operation_id="authorized-effect")) as evidence:
                        evidence.claimed()
                        if error:
                            with self.assertRaises(error) as caught:
                                client.write_once("POST", "crm/customer/create", body={"name": "Synthetic"},
                                                  operation="crm_customer_create")
                            if reply.get("code") == 500:
                                self.assertEqual(caught.exception.reconciliation,
                                    {"response_class": "application_error", "upstream_code": "500",
                                     "message_class": "server_error", "phase": "dispatch",
                                     "backend_transactional": True,
                                     "backend_service_method": "CrmCustomerServiceImpl#createCustomer"})
                                self.assertNotIn("synthetic-secret", str(caught.exception.reconciliation))
                        else:
                            self.assertEqual(client.write_once("POST", "crm/customer/create",
                                                               body={"name": "Synthetic"},
                                                               operation="crm_customer_create"), 31)
                    receipt, = events.snapshot("normal-turn")["receipts"]
                    self.assertEqual(receipt["effect_status"], expected)
                    self.assertEqual(receipt["dispatch_state"], "dispatched")
                    self.assertEqual(receipt["dispatch_count"], 1)
                    self.assertEqual(receipt["approval_consumed_count"], 1)
                    self.assertEqual(len(requests), position+1)  # No retry on any reply.
                    kinds = [e["kind"] for e in events.snapshot("normal-turn")["events"]]
                    self.assertEqual(kinds.count("application_begin"), 1)
                    self.assertEqual(kinds.count("application_end"), 1)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
