"""领域核心：受理、外发、回执、账单、暂停、取消与对账。

不变量：
- 客户用稳定业务编号登记意图；摘要、语言、是否加急共同界定已同意的工作。
  编号相同而设置不同构成冲突，在客户明确决定前不改动已登记的工作。
- 每次外发按出口报价占用本部门当周期的普通或应急额度；只有供应商明确
  否定受理（final_void）或账单确认扣款时才解除占用。请求超时、暂未可查、
  当天未上账单都不能解除占用——宁可保留敞口。
- 同一工作同一时间只允许一个在途外发；前次外发未获明确否定前不得换出口
  重发，避免同一工作被两次付费处理。
- 账单按 charge 归集，revision 是同一扣款的新净额，不得叠加；迟到的上一
  周期材料只记回所属周期，不给当前周期凭空增加可用额度。
- 出口暂停只阻断新的外发，查证、回执与收账照常处理。
- 所有被受理的决定先写日志再生效，重启后重放恢复。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from relay.inventory import Config
from relay.store import Journal, read_entries

RECEIPT_RESULTS = frozenset({"timeout", "accepted", "completed", "absent_pending", "final_void"})
FOLLOWUP_ACTIONS = frozenset({"credit_customer", "absorb_cost", "dispute_vendor", "redeliver_result"})


class Refused(Exception):
    """业务拒绝：reason 是稳定标识，detail 携带上下文。"""

    def __init__(self, reason: str, **detail):
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


@dataclass
class Task:
    tenant: str
    number: str
    digest: str
    language: str
    urgent: bool
    attempts: list[str] = field(default_factory=list)
    result: str | None = None
    cancelled_at: int | None = None
    conflict: dict | None = None
    followups: list[dict] = field(default_factory=list)


@dataclass
class Attempt:
    attempt: str
    tenant: str
    number: str
    outlet: str
    cycle: str
    pool: str
    quote: int
    sent_at: int
    state: str = "open"  # open | void | settled
    observation: str | None = None
    provider_operation: str | None = None
    charge: str | None = None


@dataclass
class Charge:
    charge: str
    tenant: str
    cycle: str
    revision: int
    amount: int
    provider_operation: str | None
    attempt: str | None
    matched: bool


class RelayService:
    """外发与账单核对的连续业务服务。状态可全部由日志重放重建。"""

    def __init__(self, config: Config, journal: Journal | None = None):
        self.config = config
        self.journal = journal
        self.gateway = None  # 可选：外发网关（如合成供应商），发送/查证时回调
        self.tasks: dict[tuple[str, str], Task] = {}
        self.attempts: dict[str, Attempt] = {}
        self.charges: dict[str, Charge] = {}
        self.pauses: set[tuple[str, str]] = set()
        self.unmatched_receipts: list[dict] = []
        self.flags: list[dict] = []
        self.clock = 0

    # ------------------------------------------------------------------ 框架

    @classmethod
    def restore(cls, config: Config, journal_path) -> "RelayService":
        """从日志重建服务；重放不再落盘也不再触发网关。"""
        service = cls(config)
        for entry in read_entries(journal_path):
            service._replay(entry)
        service.journal = Journal(journal_path)
        return service

    def _replay(self, entry: dict) -> None:
        op = entry["op"]
        handlers = {
            "intent": self.intent,
            "dispatch": self.dispatch,
            "receipt": self.receipt,
            "bill": self.bill,
            "pause": self.pause,
            "resume": self.resume,
            "cancel": self.cancel,
            "decide_conflict": self.decide_conflict,
            "followup": self.followup,
        }
        if op not in handlers:
            raise ValueError(f"未知日志操作：{op}")
        kwargs = {key: value for key, value in entry.items() if key != "op"}
        handlers[op](_replay=True, **kwargs)

    def _record(self, replay: bool, **entry) -> None:
        if not replay and self.journal is not None:
            self.journal.append(entry)

    def _tick(self, at: int) -> None:
        if type(at) is not int or at < self.clock:
            raise Refused("bad_clock", at=at, clock=self.clock)
        self.clock = at

    def _flag(self, kind: str, **detail) -> None:
        self.flags.append({"kind": kind, "at": self.clock, **detail})

    def _tenant(self, name: str):
        tenant = self.config.tenants.get(name)
        if tenant is None:
            raise Refused("unknown_tenant", tenant=name)
        return tenant

    def _outlet(self, name: str):
        outlet = self.config.outlets.get(name)
        if outlet is None:
            raise Refused("unknown_outlet", outlet=name)
        return outlet

    def _cycle(self, name: str):
        for cycle in self.config.cycles:
            if cycle.cycle == name:
                return cycle
        raise Refused("unknown_cycle", cycle=name)

    def _task(self, tenant: str, number: str) -> Task:
        task = self.tasks.get((tenant, number))
        if task is None:
            raise Refused("unknown_task", tenant=tenant, number=number)
        return task

    # ------------------------------------------------------------------ 受理

    def intent(self, tenant, number, digest, language, urgent, at=None, *, _replay=False):
        """登记客户上传意图。相同编号相同设置为幂等；设置不同记为冲突。"""
        at = self.clock if at is None else at
        self._tick(at)
        self._tenant(tenant)
        if not (isinstance(number, str) and number and isinstance(digest, str) and digest
                and isinstance(language, str) and language):
            raise Refused("missing_field", number=number)
        task = self.tasks.get((tenant, number))
        if task is None:
            self.tasks[(tenant, number)] = Task(tenant, number, digest, language, bool(urgent))
            self._record(_replay, op="intent", tenant=tenant, number=number, digest=digest,
                         language=language, urgent=bool(urgent), at=at)
            return {"status": "registered"}
        if (task.digest, task.language, task.urgent) == (digest, language, bool(urgent)):
            return {"status": "duplicate"}
        task.conflict = {"digest": digest, "language": language,
                         "urgent": bool(urgent), "at": at}
        self._record(_replay, op="intent", tenant=tenant, number=number, digest=digest,
                     language=language, urgent=bool(urgent), at=at)
        return {"status": "conflict"}

    def decide_conflict(self, tenant, number, action, at=None, *, _replay=False):
        """客户对冲突设置的决定：keep 保留原工作，replace 以新设置为准。"""
        at = self.clock if at is None else at
        self._tick(at)
        task = self._task(tenant, number)
        if task.conflict is None:
            raise Refused("no_conflict", number=number)
        if action == "keep":
            task.conflict = None
        elif action == "replace":
            if task.cancelled_at is not None:
                raise Refused("task_cancelled", number=number)
            if task.result is not None:
                raise Refused("task_completed", number=number)
            for aid in task.attempts:
                attempt = self.attempts[aid]
                if attempt.state == "open":
                    raise Refused("attempt_in_flight", attempt=aid)
                if attempt.state == "settled":
                    raise Refused("work_already_billed", attempt=aid)
            task.digest = task.conflict["digest"]
            task.language = task.conflict["language"]
            task.urgent = task.conflict["urgent"]
            task.conflict = None
        else:
            raise Refused("bad_action", action=action)
        self._record(_replay, op="decide_conflict", tenant=tenant, number=number,
                     action=action, at=at)
        return {"status": "resolved", "action": action}

    # ------------------------------------------------------------------ 外发

    def dispatch(self, tenant, number, outlet, attempt=None, at=None, *, _replay=False):
        """发起一次外发。占用报价额度；拒绝一切可能造成二次付费的重发。"""
        at = self.clock if at is None else at
        self._tick(at)
        self._tenant(tenant)
        outlet_cfg = self._outlet(outlet)
        task = self._task(tenant, number)
        if attempt is not None and attempt in self.attempts:
            previous = self.attempts[attempt]
            if (previous.tenant, previous.number) == (tenant, number) and outlet_cfg.supports_lookup:
                return {"status": "duplicate", "attempt": attempt}
            raise Refused("attempt_clash", attempt=attempt)
        if task.cancelled_at is not None:
            raise Refused("task_cancelled", number=number)
        if task.result is not None:
            raise Refused("task_completed", number=number)
        for aid in task.attempts:
            existing = self.attempts[aid]
            if existing.state == "open":
                raise Refused("attempt_in_flight", attempt=aid)
            if existing.state == "settled":
                raise Refused("paid_pending_result", attempt=aid)
        if (tenant, outlet) in self.pauses:
            raise Refused("outlet_paused", tenant=tenant, outlet=outlet)
        cycle = self.config.cycle_at(at)
        if cycle is None:
            raise Refused("no_active_cycle", at=at)
        pool = "urgent" if task.urgent else "normal"
        if self._available(tenant, cycle.cycle, pool) < outlet_cfg.quote:
            raise Refused("quota_insufficient", tenant=tenant, cycle=cycle.cycle, pool=pool)
        attempt = attempt or f"{tenant}:{number}:{len(task.attempts) + 1}"
        self.attempts[attempt] = Attempt(attempt, tenant, number, outlet, cycle.cycle,
                                         pool, outlet_cfg.quote, at)
        task.attempts.append(attempt)
        self._record(_replay, op="dispatch", tenant=tenant, number=number, outlet=outlet,
                     attempt=attempt, at=at)
        if not _replay and self.gateway is not None:
            self.gateway.send(self._attempt_view(self.attempts[attempt]), at)
        return {"status": "sent", "attempt": attempt, "cycle": cycle.cycle,
                "pool": pool, "quote": outlet_cfg.quote}

    def pause(self, tenant, outlet, at=None, *, _replay=False):
        """暂停部门某出口的新外发；查证与收账不受影响。"""
        at = self.clock if at is None else at
        self._tick(at)
        self._tenant(tenant)
        self._outlet(outlet)
        self.pauses.add((tenant, outlet))
        self._record(_replay, op="pause", tenant=tenant, outlet=outlet, at=at)
        return {"status": "paused"}

    def resume(self, tenant, outlet, at=None, *, _replay=False):
        at = self.clock if at is None else at
        self._tick(at)
        self._tenant(tenant)
        self._outlet(outlet)
        self.pauses.discard((tenant, outlet))
        self._record(_replay, op="resume", tenant=tenant, outlet=outlet, at=at)
        return {"status": "resumed"}

    def cancel(self, tenant, number, at=None, *, _replay=False):
        """客户撤回处理。决定立即生效并落盘；在途外发的额度占用不变。"""
        at = self.clock if at is None else at
        self._tick(at)
        task = self._task(tenant, number)
        if task.cancelled_at is not None:
            return {"status": "duplicate"}
        if task.result is not None:
            raise Refused("task_completed", number=number)
        task.cancelled_at = at
        self._record(_replay, op="cancel", tenant=tenant, number=number, at=at)
        return {"status": "cancelled"}

    def followup(self, tenant, number, action, note="", at=None, *, _replay=False):
        """运营对已取消但仍有供应商费用的任务登记客户处理决定。"""
        at = self.clock if at is None else at
        self._tick(at)
        task = self._task(tenant, number)
        if task.cancelled_at is None:
            raise Refused("task_not_cancelled", number=number)
        if action not in FOLLOWUP_ACTIONS:
            raise Refused("bad_action", action=action)
        if action == "redeliver_result" and task.result is None:
            raise Refused("no_result", number=number)
        entry = {"action": action, "note": note, "at": at}
        task.followups.append(entry)
        self._record(_replay, op="followup", tenant=tenant, number=number,
                     action=action, note=note, at=at)
        return {"status": "recorded"}

    # ------------------------------------------------------------------ 回执

    def receipt(self, attempt, result, provider_operation=None, at=None, *, _replay=False):
        """受理供应商回执。final_void 之外的结果都不能解除额度占用。"""
        at = self.clock if at is None else at
        self._tick(at)
        if result not in RECEIPT_RESULTS:
            raise Refused("bad_result", result=result)
        current = self.attempts.get(attempt)
        if current is None:
            entry = {"attempt": attempt, "result": result,
                     "provider_operation": provider_operation, "at": at}
            self.unmatched_receipts.append(entry)
            self._record(_replay, op="receipt", attempt=attempt, result=result,
                         provider_operation=provider_operation, at=at)
            return {"status": "unmatched"}
        if provider_operation:
            if current.provider_operation and current.provider_operation != provider_operation:
                self._flag("provider_operation_mismatch", attempt=attempt,
                           known=current.provider_operation, got=provider_operation)
            else:
                current.provider_operation = provider_operation
        current.observation = result
        task = self.tasks[(current.tenant, current.number)]
        if result in ("accepted", "completed") and current.state == "void":
            self._flag("receipt_after_void", attempt=attempt, result=result)
        if result == "completed":
            if task.cancelled_at is not None:
                self._flag("result_after_cancel", tenant=current.tenant, number=current.number)
            if task.result is None:
                task.result = provider_operation or current.provider_operation or attempt
        elif result == "final_void":
            if current.state == "settled":
                self._flag("void_after_billed", attempt=attempt, charge=current.charge)
            elif current.state == "open":
                current.state = "void"
        self._record(_replay, op="receipt", attempt=attempt, result=result,
                     provider_operation=provider_operation, at=at)
        return {"status": "recorded"}

    # ------------------------------------------------------------------ 账单

    def bill(self, tenant, cycle, charge, revision, provider_operation, attempt,
             amount, at=None, *, _replay=False):
        """受理账单条目。revision 是同一扣款的新净额；无法归属的条目不占额度。"""
        at = self.clock if at is None else at
        self._tick(at)
        self._tenant(tenant)
        self._cycle(cycle)
        if type(amount) is not int or amount < 0 or type(revision) is not int or revision < 1:
            raise Refused("bad_bill", charge=charge)
        existing = self.charges.get(charge)
        if existing is not None:
            if revision <= existing.revision:
                return {"status": "stale_revision"}
            existing.revision = revision
            existing.amount = amount
            self._record(_replay, op="bill", tenant=tenant, cycle=cycle, charge=charge,
                         revision=revision, provider_operation=provider_operation,
                         attempt=attempt, amount=amount, at=at)
            return {"status": "revised"}
        matched = None
        if attempt is not None and attempt in self.attempts:
            matched = attempt
        elif provider_operation:
            for candidate in self.attempts.values():
                if candidate.provider_operation == provider_operation:
                    matched = candidate.attempt
                    break
        self.charges[charge] = Charge(charge, tenant, cycle, revision, amount,
                                      provider_operation, matched, matched is not None)
        status = "unmatched"
        if matched is None:
            self._flag("unmatched_bill", charge=charge, tenant=tenant, cycle=cycle,
                       amount=amount, attempt=attempt, provider_operation=provider_operation)
        else:
            status = "matched"
            current = self.attempts[matched]
            current.charge = charge
            if current.state == "open":
                current.state = "settled"
            elif current.state == "void":
                self._flag("billed_after_void", charge=charge, attempt=matched)
            if current.tenant != tenant:
                self._flag("tenant_mismatch", charge=charge, attempt=matched)
            if current.cycle != cycle:
                self._flag("cross_cycle", charge=charge, attempt=matched,
                           attempt_cycle=current.cycle, bill_cycle=cycle)
            if amount > current.quote:
                self._flag("over_quote", charge=charge, attempt=matched,
                           amount=amount, quote=current.quote)
        self._record(_replay, op="bill", tenant=tenant, cycle=cycle, charge=charge,
                     revision=revision, provider_operation=provider_operation,
                     attempt=attempt, amount=amount, at=at)
        return {"status": status}

    # ------------------------------------------------------------------ 额度

    def _usage(self, tenant: str, cycle: str, pool: str) -> tuple[int, int]:
        settled = sum(
            charge.amount for charge in self.charges.values()
            if charge.matched and charge.tenant == tenant and charge.cycle == cycle
            and self.attempts[charge.attempt].pool == pool
        )
        held = sum(
            attempt.quote for attempt in self.attempts.values()
            if attempt.tenant == tenant and attempt.cycle == cycle
            and attempt.pool == pool and attempt.state == "open"
        )
        return settled, held

    def _available(self, tenant: str, cycle: str, pool: str) -> int:
        owner = self.config.tenants[tenant]
        limit = owner.urgent_limit if pool == "urgent" else owner.normal_limit
        settled, held = self._usage(tenant, cycle, pool)
        return limit - settled - held

    # ------------------------------------------------------------------ 视图

    def _attempt_view(self, attempt: Attempt) -> dict:
        return {"attempt": attempt.attempt, "tenant": attempt.tenant,
                "number": attempt.number, "outlet": attempt.outlet,
                "cycle": attempt.cycle, "pool": attempt.pool,
                "quote": attempt.quote, "sent_at": attempt.sent_at}

    def customer_view(self, tenant: str, number: str) -> dict:
        """客户看到的结果：状态、可交付结果、待决冲突与已给出的决定。"""
        task = self._task(tenant, number)
        if task.cancelled_at is not None:
            status = "cancelled"
        elif task.result is not None:
            status = "completed"
        elif any(self.attempts[aid].state == "open" for aid in task.attempts):
            status = "processing"
        else:
            status = "registered"
        return {
            "tenant": tenant,
            "number": number,
            "digest": task.digest,
            "language": task.language,
            "urgent": task.urgent,
            "status": status,
            "result": task.result,
            "conflict": task.conflict,
            "cancelled_at": task.cancelled_at,
            "followups": list(task.followups),
            "attempts": [
                {"attempt": attempt.attempt, "outlet": attempt.outlet,
                 "state": attempt.state, "observation": attempt.observation,
                 "provider_operation": attempt.provider_operation}
                for attempt in (self.attempts[aid] for aid in task.attempts)
            ],
        }

    def track(self, tenant: str, number: str, at=None) -> dict:
        """追查：返回当前状态；出口支持查证时在供应商侧登记一次查询。"""
        at = self.clock if at is None else at
        self._tick(at)
        task = self._task(tenant, number)
        view = self.customer_view(tenant, number)
        inquiry = "none_open"
        for aid in reversed(task.attempts):
            attempt = self.attempts[aid]
            if attempt.state != "open":
                continue
            if not self.config.outlets[attempt.outlet].supports_lookup:
                inquiry = "unavailable"
            elif self.gateway is None:
                inquiry = "offline"
            else:
                inquiry = "placed"
                self.gateway.inquire(attempt.attempt, at)
            break
        view["inquiry"] = inquiry
        return view

    def quota_view(self, tenant: str, cycle: str) -> dict:
        """部门在该周期还能承诺的额度：普通与应急分开。"""
        owner = self._tenant(tenant)
        self._cycle(cycle)
        view = {"tenant": tenant, "cycle": cycle}
        for pool, limit in (("normal", owner.normal_limit), ("urgent", owner.urgent_limit)):
            settled, held = self._usage(tenant, cycle, pool)
            view[pool] = {"limit": limit, "settled": settled, "held": held,
                          "available": limit - settled - held}
        return view

    def _task_fees(self, task: Task) -> tuple[int, int]:
        settled = 0
        held = 0
        for aid in task.attempts:
            attempt = self.attempts[aid]
            if attempt.charge is not None:
                settled += self.charges[attempt.charge].amount
            if attempt.state == "open":
                held += attempt.quote
        return settled, held

    def reconciliation(self, cycle: str, tenant: str | None = None) -> dict:
        """运营对账依据：已定费用、仍承担风险的敞口、无法归属与冲突材料。"""
        self._cycle(cycle)
        names = [tenant] if tenant else sorted(self.config.tenants)
        for name in names:
            self._tenant(name)
        report = {"cycle": cycle, "tenants": {}, "unmatched_bills": [],
                  "unmatched_receipts": list(self.unmatched_receipts),
                  "flags": [flag for flag in self.flags]}
        for charge in self.charges.values():
            if not charge.matched and charge.cycle == cycle:
                report["unmatched_bills"].append({
                    "charge": charge.charge, "tenant": charge.tenant,
                    "amount": charge.amount, "revision": charge.revision,
                    "provider_operation": charge.provider_operation,
                    "attempt": charge.attempt})
        for name in names:
            charges, open_attempts, cancelled = [], [], []
            totals = {"settled": {"normal": 0, "urgent": 0},
                      "exposure": {"normal": 0, "urgent": 0}}
            for charge in self.charges.values():
                if charge.matched and charge.tenant == name and charge.cycle == cycle:
                    pool = self.attempts[charge.attempt].pool
                    totals["settled"][pool] += charge.amount
                    charges.append({"charge": charge.charge, "attempt": charge.attempt,
                                    "pool": pool, "amount": charge.amount,
                                    "revision": charge.revision})
            for attempt in self.attempts.values():
                if attempt.tenant == name and attempt.cycle == cycle and attempt.state == "open":
                    totals["exposure"][attempt.pool] += attempt.quote
                    open_attempts.append({"attempt": attempt.attempt, "number": attempt.number,
                                          "outlet": attempt.outlet, "pool": attempt.pool,
                                          "quote": attempt.quote,
                                          "observation": attempt.observation,
                                          "sent_at": attempt.sent_at})
            for task in self.tasks.values():
                if task.tenant != name or task.cancelled_at is None:
                    continue
                settled_fee, held_fee = self._task_fees(task)
                if not (settled_fee or held_fee):
                    continue
                options = ["credit_customer", "absorb_cost", "dispute_vendor"]
                if task.result is not None:
                    options.append("redeliver_result")
                cancelled.append({"number": task.number, "settled_fee": settled_fee,
                                  "exposure_fee": held_fee, "options": options,
                                  "followups": list(task.followups)})
            report["tenants"][name] = {
                "settled": totals["settled"],
                "exposure": totals["exposure"],
                "available": {pool: self._available(name, cycle, pool)
                              for pool in ("normal", "urgent")},
                "charges": sorted(charges, key=lambda item: item["charge"]),
                "open_attempts": sorted(open_attempts, key=lambda item: item["attempt"]),
                "cancelled_with_fee": sorted(cancelled, key=lambda item: item["number"]),
            }
        return report
