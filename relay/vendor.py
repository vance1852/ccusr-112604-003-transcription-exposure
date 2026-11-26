"""确定性的合成供应商：接收外发、排队回执、周期关账后出账。

默认行为：受理（accepted）→ 完成（completed）→ 所属周期结束后按报价出账。
可用 `script_attempt` 为特定 attempt 指定回执序列（如 timeout、final_void），
模拟接口超时、暂未可查、明确否定受理等不确定状态。所有事件由 `advance`
按演练秒推进，无后台线程，结果可复现。
"""

from __future__ import annotations

from relay.core import Refused


class SimVendor:
    def __init__(self, service, config, accept_delay=2, complete_delay=5):
        self.service = service
        self.config = config
        self.accept_delay = accept_delay
        self.complete_delay = complete_delay
        self.pending: list[tuple[int, dict]] = []
        self.operations: dict[str, dict] = {}
        self.attempt_operation: dict[str, str] = {}
        self.scripted: dict[str, list[dict]] = {}
        self.billed_cycles: set[str] = set()
        self.now = 0

    # ------------------------------------------------------------- 外发侧接口

    def script_attempt(self, attempt: str, events: list[dict]) -> None:
        """为 attempt 指定回执序列：[{"delay": 秒, "result": ..., "provider_operation": ...}]"""
        self.scripted[attempt] = list(events)

    def send(self, attempt_view: dict, at: int) -> None:
        """服务外发回调。登记供应商侧操作并排队回执。"""
        attempt = attempt_view["attempt"]
        events = self.scripted.get(attempt)
        if events is None:
            operation = self._register(attempt_view, f"pv-{attempt}")
            self._schedule(at + self.accept_delay, attempt, "accepted", operation)
            self._schedule(at + self.complete_delay, attempt, "completed", operation)
            return
        for event in events:
            operation = event.get("provider_operation")
            if operation and event.get("result") in ("accepted", "completed"):
                self._register(attempt_view, operation)
            self._schedule(at + event.get("delay", 1), attempt,
                           event["result"], operation)

    def inquire(self, attempt: str, at: int) -> None:
        """tracked 出口查证：按供应商侧真实状态回执。"""
        operation = self.attempt_operation.get(attempt)
        if operation is None:
            self._schedule(at + 1, attempt, "absent_pending", None)
            return
        record = self.operations[operation]
        self._schedule(at + 1, attempt, record["status"], operation)

    # ------------------------------------------------------------- 时间推进

    def advance(self, to: int) -> dict:
        """推进到演练秒 to：释放到期回执，为已结束周期出账。"""
        if type(to) is not int or to < self.now:
            raise Refused("bad_clock", to=to, now=self.now)
        self.now = to
        due = sorted((event for event in self.pending if event[0] <= to),
                     key=lambda event: event[0])
        self.pending = [event for event in self.pending if event[0] > to]
        released = []
        for at, payload in due:
            self._apply_receipt(payload, at)
            released.append(payload)
        bills = self._bill_finished_cycles(to)
        return {"released": released, "bills": bills}

    # ------------------------------------------------------------- 内部

    def _register(self, attempt_view: dict, operation: str) -> str:
        self.attempt_operation[attempt_view["attempt"]] = operation
        self.operations[operation] = {
            "attempt": attempt_view["attempt"],
            "tenant": attempt_view["tenant"],
            "cycle": attempt_view["cycle"],
            "amount": attempt_view["quote"],
            "status": "accepted",
            "billed": False,
        }
        return operation

    def _schedule(self, at: int, attempt: str, result: str, operation) -> None:
        self.pending.append((at, {"attempt": attempt, "result": result,
                                  "provider_operation": operation}))

    def _apply_receipt(self, payload: dict, at: int) -> None:
        operation = self.attempt_operation.get(payload["attempt"])
        if operation is not None:
            record = self.operations[operation]
            if payload["result"] == "completed":
                record["status"] = "completed"
            elif payload["result"] == "final_void":
                record["status"] = "final_void"
        self.service.receipt(payload["attempt"], payload["result"],
                             payload.get("provider_operation"), at=at)

    def _bill_finished_cycles(self, to: int) -> list[dict]:
        bills = []
        for cycle in self.config.cycles:
            if cycle.end > to or cycle.cycle in self.billed_cycles:
                continue
            self.billed_cycles.add(cycle.cycle)
            for operation, record in self.operations.items():
                if record["cycle"] != cycle.cycle or record["billed"]:
                    continue
                if record["status"] not in ("accepted", "completed"):
                    continue
                record["billed"] = True
                self.service.bill(record["tenant"], cycle.cycle, f"c-{operation}", 1,
                                  operation, record["attempt"], record["amount"], at=to)
                bills.append({"charge": f"c-{operation}", "cycle": cycle.cycle,
                              "amount": record["amount"]})
        return bills
