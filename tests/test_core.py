"""领域规则单元测试：受理、外发、回执、账单、暂停、取消、重启。"""

import tempfile
import unittest
from pathlib import Path

from relay.core import Refused, RelayService
from relay.inventory import load_config
from relay.store import Journal

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.config = load_config(SAMPLES)
        self.service = RelayService(self.config)

    def intent(self, number="n1", tenant="service", digest="rec-1",
               language="zh", urgent=False, at=1):
        return self.service.intent(tenant, number, digest, language, urgent, at=at)

    def dispatch(self, number="n1", tenant="service", outlet="tracked",
                 attempt=None, at=2):
        return self.service.dispatch(tenant, number, outlet, attempt=attempt, at=at)

    def available(self, pool, tenant="service", cycle="march"):
        return self.service.quota_view(tenant, cycle)[pool]["available"]


class IntentTests(ServiceCase):
    def test_same_number_same_settings_is_idempotent(self):
        self.assertEqual(self.intent()["status"], "registered")
        self.assertEqual(self.intent(at=2)["status"], "duplicate")
        self.assertEqual(len(self.service.tasks), 1)

    def test_changed_settings_raise_conflict_until_customer_decides(self):
        self.intent()
        outcome = self.intent(digest="rec-2", language="en", at=2)
        self.assertEqual(outcome["status"], "conflict")
        view = self.service.customer_view("service", "n1")
        self.assertEqual(view["digest"], "rec-1")
        self.assertEqual(view["conflict"]["digest"], "rec-2")
        self.service.decide_conflict("service", "n1", "keep", at=3)
        self.assertIsNone(self.service.customer_view("service", "n1")["conflict"])

    def test_replace_applies_new_settings_only_when_no_work_at_risk(self):
        self.intent()
        self.intent(digest="rec-2", language="en", at=2)
        self.dispatch(attempt="a1", at=3)
        with self.assertRaises(Refused) as caught:
            self.service.decide_conflict("service", "n1", "replace", at=4)
        self.assertEqual(caught.exception.reason, "attempt_in_flight")
        self.service.receipt("a1", "final_void", at=5)
        self.service.decide_conflict("service", "n1", "replace", at=6)
        view = self.service.customer_view("service", "n1")
        self.assertEqual((view["digest"], view["language"]), ("rec-2", "en"))

    def test_missing_fields_and_unknown_tenant_refused(self):
        with self.assertRaises(Refused):
            self.intent(digest="")
        with self.assertRaises(Refused) as caught:
            self.intent(tenant="ghost")
        self.assertEqual(caught.exception.reason, "unknown_tenant")


class DispatchTests(ServiceCase):
    def test_urgent_pool_separate_and_zero_limit_refuses(self):
        self.intent(tenant="research", urgent=True)
        with self.assertRaises(Refused) as caught:
            self.dispatch(tenant="research")
        self.assertEqual(caught.exception.reason, "quota_insufficient")

    def test_quote_must_fit_pool(self):
        self.intent(tenant="research")  # 普通额度 60，legacy 报价 65
        with self.assertRaises(Refused):
            self.dispatch(tenant="research", outlet="legacy")
        outcome = self.dispatch(tenant="research", outlet="tracked")
        self.assertEqual(outcome["status"], "sent")
        self.assertEqual(self.available("normal", tenant="research"), 25)

    def test_urgent_intent_holds_urgent_pool(self):
        self.intent(urgent=True)
        self.dispatch(attempt="a1")
        self.assertEqual(self.available("urgent"), 5)
        self.assertEqual(self.available("normal"), 100)

    def test_second_attempt_blocked_until_definitive_void(self):
        self.intent()
        self.dispatch(outlet="legacy", attempt="a1")
        at = 3
        for result in ("timeout", "absent_pending", "accepted"):
            self.service.receipt("a1", result, "pv1", at=at)
            at += 1
            with self.assertRaises(Refused) as caught:
                self.dispatch(attempt="a2", at=at)
            self.assertEqual(caught.exception.reason, "attempt_in_flight")
            at += 1
        self.service.receipt("a1", "final_void", at=at)
        self.assertEqual(self.dispatch(attempt="a2", at=at + 1)["status"], "sent")
        self.assertEqual(self.available("normal"), 65)

    def test_billed_without_result_blocks_redispatch(self):
        self.intent()
        self.dispatch(attempt="a1")
        self.service.bill("service", "march", "c1", 1, "pv1", "a1", 35, at=3)
        with self.assertRaises(Refused) as caught:
            self.dispatch(attempt="a2", at=4)
        self.assertEqual(caught.exception.reason, "paid_pending_result")

    def test_tracked_attempt_id_is_idempotent_legacy_is_not(self):
        self.intent()
        self.dispatch(attempt="a1")
        self.assertEqual(self.dispatch(attempt="a1")["status"], "duplicate")
        self.assertEqual(self.available("normal"), 65)
        self.intent(number="n2", at=3)
        self.dispatch(number="n2", outlet="legacy", attempt="b1", at=4)
        with self.assertRaises(Refused) as caught:
            self.dispatch(number="n2", outlet="legacy", attempt="b1", at=5)
        self.assertEqual(caught.exception.reason, "attempt_clash")

    def test_no_active_cycle(self):
        self.intent()
        with self.assertRaises(Refused) as caught:
            self.dispatch(at=500)
        self.assertEqual(caught.exception.reason, "no_active_cycle")

    def test_clock_must_not_go_backwards(self):
        self.intent(at=10)
        with self.assertRaises(Refused) as caught:
            self.intent(number="n2", at=3)
        self.assertEqual(caught.exception.reason, "bad_clock")


class ReceiptTests(ServiceCase):
    def test_uncertain_receipts_keep_hold_final_void_releases(self):
        self.intent()
        self.dispatch(outlet="legacy", attempt="a1")
        self.assertEqual(self.available("normal"), 35)
        self.service.receipt("a1", "timeout", at=3)
        self.service.receipt("a1", "absent_pending", at=4)
        self.assertEqual(self.available("normal"), 35)
        self.service.receipt("a1", "final_void", at=5)
        self.assertEqual(self.available("normal"), 100)

    def test_completed_delivers_result_to_customer(self):
        self.intent()
        self.dispatch(attempt="a1")
        self.service.receipt("a1", "accepted", "pv9", at=3)
        self.service.receipt("a1", "completed", "pv9", at=4)
        view = self.service.customer_view("service", "n1")
        self.assertEqual((view["status"], view["result"]), ("completed", "pv9"))

    def test_unknown_attempt_goes_to_ops_inbox(self):
        outcome = self.service.receipt("ghost", "completed", "pv1", at=1)
        self.assertEqual(outcome["status"], "unmatched")
        report = self.service.reconciliation("march")
        self.assertEqual(report["unmatched_receipts"][0]["attempt"], "ghost")

    def test_conflicting_provider_operation_is_flagged(self):
        self.intent()
        self.dispatch(attempt="a1")
        self.service.receipt("a1", "accepted", "pv1", at=3)
        self.service.receipt("a1", "completed", "pv2", at=4)
        kinds = [flag["kind"] for flag in self.service.flags]
        self.assertIn("provider_operation_mismatch", kinds)


class BillTests(ServiceCase):
    def test_revision_replaces_net_amount_not_additive(self):
        self.intent()
        self.dispatch(attempt="a1")
        self.service.bill("service", "march", "c1", 1, "pv1", "a1", 35, at=3)
        self.assertEqual(self.available("normal"), 65)
        self.service.bill("service", "march", "c1", 2, "pv1", "a1", 14, at=4)
        self.assertEqual(self.available("normal"), 86)
        outcome = self.service.bill("service", "march", "c1", 1, "pv1", "a1", 35, at=5)
        self.assertEqual(outcome["status"], "stale_revision")
        self.assertEqual(self.available("normal"), 86)

    def test_bill_matches_by_provider_operation(self):
        self.intent()
        self.dispatch(attempt="a1")
        self.service.receipt("a1", "accepted", "pv7", at=3)
        outcome = self.service.bill("service", "march", "c9", 1, "pv7", None, 35, at=4)
        self.assertEqual(outcome["status"], "matched")
        self.assertEqual(self.service.attempts["a1"].state, "settled")

    def test_unmatched_bill_does_not_consume_quota(self):
        outcome = self.service.bill("research", "march", "x1", 1, "x91", "x3", 15, at=1)
        self.assertEqual(outcome["status"], "unmatched")
        self.assertEqual(self.service.quota_view("research", "march")["normal"]["available"], 60)
        report = self.service.reconciliation("march")
        self.assertEqual(report["unmatched_bills"][0]["charge"], "x1")

    def test_late_previous_cycle_bill_books_to_own_cycle(self):
        self.intent()
        self.dispatch(outlet="legacy", attempt="a1")
        self.service.bill("service", "march", "c1", 1, "pv1", "a1", 65, at=150)
        self.assertEqual(self.available("normal", cycle="march"), 35)
        self.assertEqual(self.available("normal", cycle="april"), 100)

    def test_bill_after_void_and_over_quote_are_flagged(self):
        self.intent()
        self.dispatch(outlet="legacy", attempt="a1")
        self.service.receipt("a1", "final_void", at=3)
        self.service.bill("service", "march", "c1", 1, "pv1", "a1", 70, at=4)
        kinds = [flag["kind"] for flag in self.service.flags]
        self.assertIn("billed_after_void", kinds)
        self.assertIn("over_quote", kinds)


class PauseTests(ServiceCase):
    def test_pause_blocks_dispatch_but_not_verification_or_collection(self):
        self.intent()
        self.dispatch(outlet="legacy", attempt="a1")
        self.service.pause("service", "legacy", at=3)
        self.intent(number="n2", at=4)
        with self.assertRaises(Refused) as caught:
            self.dispatch(number="n2", outlet="legacy", at=5)
        self.assertEqual(caught.exception.reason, "outlet_paused")
        self.service.receipt("a1", "completed", "pv1", at=6)
        self.service.bill("service", "march", "c1", 1, "pv1", "a1", 30, at=7)
        self.assertEqual(self.service.customer_view("service", "n1")["status"], "completed")
        self.service.resume("service", "legacy", at=8)
        self.assertEqual(self.dispatch(number="n2", outlet="legacy", attempt="a2", at=9)["status"], "sent")


class CancelTests(ServiceCase):
    def test_cancel_keeps_hold_and_blocks_redispatch(self):
        self.intent()
        self.dispatch(attempt="a1")
        self.service.cancel("service", "n1", at=3)
        self.assertEqual(self.available("normal"), 65)
        with self.assertRaises(Refused) as caught:
            self.dispatch(attempt="a2", at=4)
        self.assertEqual(caught.exception.reason, "task_cancelled")
        self.assertEqual(self.service.cancel("service", "n1", at=5)["status"], "duplicate")

    def test_completed_task_cannot_be_cancelled(self):
        self.intent()
        self.dispatch(attempt="a1")
        self.service.receipt("a1", "completed", "pv1", at=3)
        with self.assertRaises(Refused) as caught:
            self.service.cancel("service", "n1", at=4)
        self.assertEqual(caught.exception.reason, "task_completed")

    def test_result_ops_sees_fee_and_executable_followups(self):
        self.intent(urgent=True)
        self.dispatch(attempt="a1")
        self.service.cancel("service", "n1", at=3)
        self.service.bill("service", "march", "c1", 1, "pv1", "a1", 35, at=4)
        self.service.followup("service", "n1", "credit_customer", note="客户已取消", at=5)
        report = self.service.reconciliation("march", tenant="service")
        (entry,) = report["tenants"]["service"]["cancelled_with_fee"]
        self.assertEqual(entry["settled_fee"], 35)
        self.assertIn("dispute_vendor", entry["options"])
        self.assertEqual(entry["followups"][0]["action"], "credit_customer")

    def test_result_arriving_after_cancel_is_flagged(self):
        self.intent()
        self.dispatch(attempt="a1")
        self.service.cancel("service", "n1", at=3)
        self.service.receipt("a1", "completed", "pv1", at=4)
        kinds = [flag["kind"] for flag in self.service.flags]
        self.assertIn("result_after_cancel", kinds)
        view = self.service.customer_view("service", "n1")
        self.assertEqual(view["status"], "cancelled")
        self.assertEqual(view["result"], "pv1")  # 结果事实保留，运营可选择补交付
        outcome = self.service.followup("service", "n1", "redeliver_result", at=5)
        self.assertEqual(outcome["status"], "recorded")


class RestartTests(unittest.TestCase):
    def test_journal_replay_preserves_pending_work_and_decisions(self):
        config = load_config(SAMPLES)
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.jsonl"
            service = RelayService(config, Journal(journal))
            service.intent("service", "n1", "rec-1", "zh", False, at=1)
            service.dispatch("service", "n1", "legacy", attempt="a1", at=2)
            service.receipt("a1", "timeout", at=3)
            service.pause("service", "legacy", at=4)
            service.intent("service", "n2", "rec-2", "en", True, at=5)
            service.dispatch("service", "n2", "tracked", attempt="a2", at=6)
            service.cancel("service", "n2", at=7)
            service.followup("service", "n2", "absorb_cost", at=8)
            service.journal.close()

            restored = RelayService.restore(config, journal)
            self.assertEqual(restored.clock, 8)
            self.assertEqual(restored.customer_view("service", "n1")["status"], "processing")
            self.assertEqual(restored.customer_view("service", "n2")["status"], "cancelled")
            self.assertEqual(restored.customer_view("service", "n2")["followups"][0]["action"],
                             "absorb_cost")
            self.assertEqual(restored.quota_view("service", "march")["normal"]["available"], 35)
            self.assertEqual(restored.quota_view("service", "march")["urgent"]["held"], 35)
            with self.assertRaises(Refused):  # 暂停决定在重启后仍然生效
                restored.dispatch("service", "n1", "legacy", attempt="a3", at=9)
            with self.assertRaises(Refused):  # 在途外发重启后仍阻止重发
                restored.dispatch("service", "n1", "tracked", attempt="a4", at=10)
            restored.journal.close()


if __name__ == "__main__":
    unittest.main()
