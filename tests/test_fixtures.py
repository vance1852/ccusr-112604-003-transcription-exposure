"""端到端：用两份合成演练资料验证客户结果、额度与对账。"""

import tempfile
import unittest
from pathlib import Path

from relay.core import RelayService
from relay.demo import apply
from relay.inventory import load_config
from relay.store import Journal
from sample_reader import load_timeline

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def replay(timeline_name, journal=None, restart_at=None):
    config = load_config(SAMPLES)
    service = RelayService(config, Journal(journal) if journal else None)
    records = list(load_timeline(SAMPLES / timeline_name))
    outcomes = []
    for index, record in enumerate(records):
        if restart_at is not None and index == restart_at:
            if service.journal:
                service.journal.close()
            service = RelayService.restore(config, journal)
        outcomes.append(apply(service, record))
    return service, outcomes


class ShiftLogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service, cls.outcomes = replay("shift-log.jsonl")

    def test_customer_retry_upload_is_idempotent(self):
        self.assertEqual(self.outcomes[0]["status"], "registered")
        self.assertEqual(self.outcomes[3]["status"], "duplicate")  # at=69 相同设置重传
        view = self.service.customer_view("service", "d-28")
        self.assertEqual(len(view["attempts"]), 1)  # 没有因重传产生第二次外发

    def test_timeout_upload_still_delivers_result(self):
        view = self.service.customer_view("service", "d-28")
        self.assertEqual(view["status"], "completed")
        self.assertEqual(view["result"], "pv31")

    def test_cancelled_task_keeps_vendor_fee_visible(self):
        view = self.service.customer_view("service", "d-29")
        self.assertEqual(view["status"], "cancelled")
        report = self.service.reconciliation("march", tenant="service")
        (entry,) = report["tenants"]["service"]["cancelled_with_fee"]
        self.assertEqual(entry["number"], "d-29")
        self.assertEqual(entry["settled_fee"], 14)  # 修订后净额，不是 35+14
        self.assertIn("credit_customer", entry["options"])

    def test_quota_after_late_bills_and_revision(self):
        march = self.service.quota_view("service", "march")
        self.assertEqual(march["normal"], {"limit": 100, "settled": 65, "held": 0, "available": 35})
        self.assertEqual(march["urgent"], {"limit": 40, "settled": 14, "held": 0, "available": 26})
        april = self.service.quota_view("service", "april")
        self.assertEqual(april["normal"]["available"], 100)  # 迟到的三月材料不影响当前周期
        self.assertEqual(april["urgent"]["available"], 40)

    def test_reconciliation_has_no_open_exposure_or_flags(self):
        report = self.service.reconciliation("march", tenant="service")
        tenant = report["tenants"]["service"]
        self.assertEqual(tenant["open_attempts"], [])
        self.assertEqual(tenant["exposure"], {"normal": 0, "urgent": 0})
        self.assertEqual(report["unmatched_bills"], [])
        self.assertEqual(report["flags"], [])

    def test_restart_mid_stream_preserves_state(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.jsonl"
            restarted, _ = replay("shift-log.jsonl", journal=journal, restart_at=9)
            straight, _ = replay("shift-log.jsonl")
            for number in ("d-28", "d-29"):
                self.assertEqual(restarted.customer_view("service", number),
                                 straight.customer_view("service", number))
            self.assertEqual(restarted.reconciliation("march"), straight.reconciliation("march"))
            self.assertEqual(restarted.quota_view("service", "april"),
                             straight.quota_view("service", "april"))


class DefinitiveVoidTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service, cls.outcomes = replay("definitive-void.jsonl")

    def test_changed_settings_stay_pending_customer_decision(self):
        self.assertEqual(self.outcomes[3]["status"], "conflict")  # at=8 摘要与语言都变了
        view = self.service.customer_view("service", "t1")
        self.assertEqual((view["digest"], view["language"]), ("rec-20", "zh"))
        self.assertEqual(view["conflict"]["digest"], "rec-21")

    def test_retry_only_after_definitive_void(self):
        self.assertEqual(self.outcomes[4]["status"], "recorded")  # final_void
        self.assertEqual(self.service.attempts["x1"].state, "void")
        self.assertEqual(self.outcomes[5]["status"], "sent")  # 换出口重发
        view = self.service.customer_view("service", "t1")
        self.assertEqual(view["status"], "completed")
        self.assertEqual(view["result"], "x31")

    def test_completed_but_unbilled_stays_exposure(self):
        march = self.service.quota_view("service", "march")
        self.assertEqual(march["normal"]["settled"], 0)
        self.assertEqual(march["normal"]["held"], 65)  # x2 已交付未到账，仍占额度
        report = self.service.reconciliation("march", tenant="service")
        tenant = report["tenants"]["service"]
        self.assertEqual(tenant["exposure"]["normal"], 65)
        self.assertEqual(tenant["settled"], {"normal": 0, "urgent": 0})

    def test_foreign_bill_line_does_not_consume_quota(self):
        research = self.service.quota_view("research", "march")
        self.assertEqual(research["normal"]["available"], 60)
        report = self.service.reconciliation("march")
        self.assertEqual([line["charge"] for line in report["unmatched_bills"]], ["x1"])
        kinds = [flag["kind"] for flag in report["flags"]]
        self.assertIn("unmatched_bill", kinds)


if __name__ == "__main__":
    unittest.main()
