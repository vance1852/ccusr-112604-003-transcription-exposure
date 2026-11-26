"""重放演练资料：把事件文件逐条喂给服务，展示客户结果、额度与对账。

    python3 -m relay.demo samples/shift-log.jsonl
    python3 -m relay.demo samples/definitive-void.jsonl --restart-at 5

`--state` 指定状态目录（默认临时目录）；`--restart-at N` 在第 N 条之后
从日志重建服务，验证重启不丢失已发未决请求与已给出的决定。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from sample_reader import load_timeline

from relay.core import Refused, RelayService
from relay.inventory import load_config


def apply(service: RelayService, record: dict) -> dict:
    kind = record["kind"]
    if kind == "intent":
        return service.intent(record["tenant"], record["number"], record["digest"],
                              record["language"], record["urgent"], at=record["at"])
    if kind == "send":
        return service.dispatch(record["tenant"], record["number"], record["outlet"],
                                attempt=record["attempt"], at=record["at"])
    if kind == "receipt":
        return service.receipt(record["attempt"], record["result"],
                               record.get("provider_operation"), at=record["at"])
    if kind == "pause":
        return service.pause(record["tenant"], record["outlet"], at=record["at"])
    if kind == "cancel":
        return service.cancel(record["tenant"], record["number"], at=record["at"])
    if kind == "bill":
        return service.bill(record["tenant"], record["cycle"], record["charge"],
                            record["revision"], record["provider_operation"],
                            record["attempt"], record["amount"], at=record["at"])
    return {"status": "ignored", "kind": kind}


def run(timeline: Path, state: Path, restart_at: int | None = None, out=print) -> RelayService:
    config = load_config(timeline.parent)
    journal_path = state / "journal.jsonl"
    service = RelayService.restore(config, journal_path)  # 已有日志则续接既有状态
    records = list(load_timeline(timeline))
    for index, record in enumerate(records):
        if restart_at is not None and index == restart_at:
            service.journal.close()
            service = RelayService.restore(config, journal_path)
            out(f"-- 第 {index} 条之后重启，已从日志恢复 --")
        try:
            outcome = apply(service, record)
        except Refused as error:
            outcome = {"status": "refused", "reason": error.reason, **error.detail}
        out(f"at={record['at']:>4} {record['kind']:<8} -> {json.dumps(outcome, ensure_ascii=False)}")

    out("\n== 客户看到的结果 ==")
    for tenant, number in sorted(service.tasks):
        out(json.dumps(service.customer_view(tenant, number), ensure_ascii=False, indent=2))

    out("\n== 部门还能承诺的额度 ==")
    for tenant in sorted(config.tenants):
        for cycle in config.cycles:
            out(json.dumps(service.quota_view(tenant, cycle.cycle), ensure_ascii=False))

    out("\n== 运营对账依据 ==")
    for cycle in config.cycles:
        out(json.dumps(service.reconciliation(cycle.cycle), ensure_ascii=False, indent=2))
    return service


def main():
    parser = argparse.ArgumentParser(description="重放演练资料")
    parser.add_argument("timeline", type=Path, help="演练事件文件")
    parser.add_argument("--state", type=Path, help="状态目录（默认临时目录）")
    parser.add_argument("--restart-at", type=int, metavar="N", help="在第 N 条之后模拟重启")
    args = parser.parse_args()
    if args.state is not None:
        args.state.mkdir(parents=True, exist_ok=True)
        run(args.timeline, args.state, args.restart_at)
        return
    with tempfile.TemporaryDirectory() as directory:
        run(args.timeline, Path(directory), args.restart_at)


if __name__ == "__main__":
    main()
