"""清册载入与校验：部门额度、合同周期、出口报价。

周期范围左闭右开；额度与报价都是最小货币单位；普通与应急余额分开。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Tenant:
    tenant: str
    normal_limit: int
    urgent_limit: int


@dataclass(frozen=True)
class Cycle:
    cycle: str
    start: int
    end: int


@dataclass(frozen=True)
class Outlet:
    outlet: str
    quote: int
    supports_lookup: bool


@dataclass(frozen=True)
class Config:
    tenants: dict[str, Tenant]
    cycles: tuple[Cycle, ...]
    outlets: dict[str, Outlet]

    def cycle_at(self, at: int) -> Cycle | None:
        for cycle in self.cycles:
            if cycle.start <= at < cycle.end:
                return cycle
        return None


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _natural(value, message: str) -> int:
    _require(type(value) is int and value >= 0, message)
    return value


def load_config(root: Path) -> Config:
    with (Path(root) / "inventory.json").open(encoding="utf-8") as source:
        raw = json.load(source)
    _require(isinstance(raw, dict), "清册应为对象")

    tenants = {}
    for entry in raw.get("tenants") or []:
        name = entry["tenant"]
        tenant = Tenant(
            tenant=name,
            normal_limit=_natural(entry["normal_limit"], f"{name} 普通额度应为非负整数"),
            urgent_limit=_natural(entry["urgent_limit"], f"{name} 应急额度应为非负整数"),
        )
        _require(name not in tenants, f"部门重复：{name}")
        tenants[name] = tenant
    _require(bool(tenants), "清册至少需要一个部门")

    cycles = []
    for entry in raw.get("cycles") or []:
        cycle = Cycle(
            cycle=entry["cycle"],
            start=_natural(entry["start"], "周期起点应为非负整数"),
            end=_natural(entry["end"], "周期终点应为非负整数"),
        )
        _require(cycle.start < cycle.end, f"周期 {cycle.cycle} 起点应早于终点")
        if cycles:
            _require(cycles[-1].end <= cycle.start, f"周期 {cycle.cycle} 与前一周期重叠")
        cycles.append(cycle)
    _require(bool(cycles), "清册至少需要一个合同周期")

    outlets = {}
    for entry in raw.get("outlets") or []:
        name = entry["outlet"]
        outlet = Outlet(
            outlet=name,
            quote=_natural(entry["quote"], f"{name} 报价应为非负整数"),
            supports_lookup=bool(entry.get("supports_lookup")),
        )
        _require(outlet.quote > 0, f"{name} 报价应为正数")
        _require(name not in outlets, f"出口重复：{name}")
        outlets[name] = outlet
    _require(bool(outlets), "清册至少需要一个出口")

    return Config(tenants=tenants, cycles=tuple(cycles), outlets=outlets)
