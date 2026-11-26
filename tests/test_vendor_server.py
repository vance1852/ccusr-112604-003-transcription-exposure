"""合成供应商与 HTTP 服务的集成测试。"""

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from relay.core import RelayService
from relay.inventory import load_config
from relay.server import make_server
from relay.store import Journal
from relay.vendor import SimVendor

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


class SimVendorTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(SAMPLES)
        self.service = RelayService(self.config)
        self.vendor = SimVendor(self.service, self.config)
        self.service.gateway = self.vendor

    def test_default_flow_accept_complete_bill_at_cycle_close(self):
        self.service.intent("service", "n1", "rec-1", "zh", False, at=1)
        self.service.dispatch("service", "n1", "tracked", attempt="a1", at=2)
        self.vendor.advance(4)  # 受理回执于 at=4 到达
        self.assertEqual(self.service.attempts["a1"].observation, "accepted")
        self.assertEqual(self.service.quota_view("service", "march")["normal"]["held"], 35)
        self.vendor.advance(8)  # 完成回执于 at=7 到达
        self.assertEqual(self.service.customer_view("service", "n1")["status"], "completed")
        self.vendor.advance(100)  # 三月周期关账
        view = self.service.quota_view("service", "march")
        self.assertEqual((view["normal"]["settled"], view["normal"]["held"]), (35, 0))

    def test_scripted_timeout_keeps_exposure_until_void(self):
        self.vendor.script_attempt("a1", [
            {"delay": 1, "result": "timeout", "provider_operation": None},
            {"delay": 9, "result": "final_void", "provider_operation": None},
        ])
        self.service.intent("service", "n1", "rec-1", "zh", False, at=1)
        self.service.dispatch("service", "n1", "legacy", attempt="a1", at=2)
        self.vendor.advance(5)
        self.assertEqual(self.service.quota_view("service", "march")["normal"]["held"], 65)
        self.vendor.advance(200)  # 明确否定后才释放；两个周期都不产生账单
        self.assertEqual(self.service.quota_view("service", "march")["normal"]["held"], 0)
        self.assertEqual(self.service.reconciliation("march")["tenants"]["service"]["charges"], [])

    def test_inquire_reports_vendor_side_truth(self):
        self.vendor.script_attempt("a1", [
            {"delay": 1, "result": "timeout", "provider_operation": None},
        ])
        self.service.intent("service", "n1", "rec-1", "zh", False, at=1)
        self.service.dispatch("service", "n1", "tracked", attempt="a1", at=2)
        self.vendor.advance(3)
        view = self.service.track("service", "n1", at=4)
        self.assertEqual(view["inquiry"], "placed")
        self.vendor.advance(5)
        self.assertEqual(self.service.attempts["a1"].observation, "absent_pending")


class HttpServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Path(self.directory.name)
        self.server = make_server(SAMPLES, self.state, 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.directory.cleanup()

    def call(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        connection.request(method, path, body=payload,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def test_full_lifecycle_and_restart_durability(self):
        status, _ = self.call("POST", "/v1/intents", {
            "tenant": "service", "number": "m1", "digest": "rec-9",
            "language": "zh", "urgent": False, "at": 1})
        self.assertEqual(status, 200)
        status, sent = self.call("POST", "/v1/dispatches", {
            "tenant": "service", "number": "m1", "outlet": "tracked", "at": 2})
        self.assertEqual((status, sent["status"]), (200, "sent"))
        status, _ = self.call("POST", "/v1/vendor/advance", {"to": 100})
        self.assertEqual(status, 200)
        status, view = self.call("GET", "/v1/tasks/service/m1")
        self.assertEqual((status, view["status"]), (200, "completed"))
        status, quota = self.call("GET", "/v1/quota/service/march")
        self.assertEqual(quota["normal"]["settled"], 35)

        # 重启进程：已交付结果与已定费用仍在
        self.server.shutdown()
        self.server.server_close()
        self.server = make_server(SAMPLES, self.state, 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, view = self.call("GET", "/v1/tasks/service/m1")
        self.assertEqual((view["status"], view["result"]), ("completed", view["result"]))
        status, quota = self.call("GET", "/v1/quota/service/march")
        self.assertEqual(quota["normal"]["settled"], 35)

    def test_refusal_maps_to_409(self):
        self.call("POST", "/v1/intents", {
            "tenant": "service", "number": "m2", "digest": "rec-1",
            "language": "zh", "urgent": False, "at": 1})
        self.call("POST", "/v1/pauses", {"tenant": "service", "outlet": "legacy", "at": 2})
        status, body = self.call("POST", "/v1/dispatches", {
            "tenant": "service", "number": "m2", "outlet": "legacy", "at": 3})
        self.assertEqual((status, body["error"]), (409, "outlet_paused"))


if __name__ == "__main__":
    unittest.main()
