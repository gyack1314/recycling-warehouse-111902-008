"""资料目录：加载覆盖声明与站点映射，回答“某天/某站是否有合法覆盖”。

覆盖语义（见 docs/input-contract.md）：
- 声明区间内零行是空覆盖（合法的完整空结果）；
- 未声明日期不可视为空，只能回合法明细或报缺口；
- 每条汇总声明固定自己的 map_version，旧区划汇总不能换标签。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


def _day(value: str) -> date:
    return date.fromisoformat(value)


def daterange(lo: str, hi: str) -> list[str]:
    a, b = _day(lo), _day(hi)
    if a > b:
        raise ValueError(f"请求区间反转: {lo} > {hi}")
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


@dataclass(frozen=True)
class DayCoverage:
    day: str
    sites: tuple[str, ...]          # 该日已声明覆盖的站点；空元组=完整空覆盖
    map_version: str

    @property
    def is_empty(self) -> bool:
        return len(self.sites) == 0


class Catalog:
    """只读资料目录。构造时只读取声明 JSON 与小维度表 site_map。

    汇总与明细 Parquet 不在此处预读，避免“先扫全表再报低成本”。
    """

    def __init__(self, inputs_dir: str | Path):
        self.root = Path(inputs_dir).resolve()
        coverage = json.loads((self.root / "coverage.json").read_text(encoding="utf-8"))
        d = coverage["deliveries"]
        self.deliveries_from = d["from"]
        self.deliveries_through = d["through"]
        self.deliveries_complete = bool(d["complete"])
        # site_day / stock: day -> {site: map_version}
        self._site_day: dict[str, dict[str, str]] = {}
        for item in coverage.get("site_day", []):
            self._site_day[item["day"]] = {s: item["map_version"] for s in item["sites"]}
        self._stock: dict[str, dict[str, str]] = {}
        for item in coverage.get("stock", []):
            self._stock[item["day"]] = {s: item["map_version"] for s in item["sites"]}
        # city_day: day -> map_version
        self._city_day: dict[str, str] = {
            item["day"]: item["map_version"] for item in coverage.get("city_day", [])
        }
        # site_map 由引擎真实读取 Parquet 后注入；这里先留空，保证不绕开 DuckDB。
        self._site_map: dict[str, dict[str, str]] = {}

    # ---- 站点映射 -----------------------------------------------------
    def set_site_map(self, rows: list[dict]) -> None:
        """注入由 DuckDB 从 site_map.parquet 读出的映射行。"""
        sm: dict[str, dict[str, str]] = {}
        for r in rows:
            sm.setdefault(r["version"], {})[r["site"]] = r["district"]
        self._site_map = sm

    def map_versions(self) -> tuple[str, ...]:
        return tuple(sorted(self._site_map))

    def sites_for(self, map_version: str) -> list[str]:
        if map_version not in self._site_map:
            raise KeyError(f"未知映射版本: {map_version}")
        return sorted(self._site_map[map_version])

    def district_of(self, map_version: str, site: str) -> str:
        try:
            return self._site_map[map_version][site]
        except KeyError as exc:
            raise KeyError(f"映射 {map_version} 不含站点 {site}") from exc

    def sites_in_district(self, map_version: str, district: str) -> list[str]:
        return sorted(
            s for s, d in self._site_map.get(map_version, {}).items() if d == district
        )

    # ---- 明细覆盖 -----------------------------------------------------
    def deliveries_covers(self, day: str) -> bool:
        return self.deliveries_complete and (
            self.deliveries_from <= day <= self.deliveries_through
        )

    def deliveries_range_covers(self, days: list[str]) -> bool:
        return all(self.deliveries_covers(d) for d in days)

    # ---- 汇总覆盖 -----------------------------------------------------
    def city_day_coverage(self, day: str) -> DayCoverage | None:
        if day not in self._city_day:
            return None
        # 市日汇总粒度是整市；sites 元组仅用于 is_empty 语义，市日声明本身不带站点。
        return DayCoverage(day=day, sites=("__city__",), map_version=self._city_day[day])

    def site_day_coverage(self, day: str) -> DayCoverage | None:
        if day not in self._site_day:
            return None
        m = self._site_day[day]
        versions = set(m.values())
        if len(versions) != 1:  # 契约保证同日同版本；防御性检查
            raise ValueError(f"site_day 同日出现多个 map_version: {day}")
        return DayCoverage(day=day, sites=tuple(sorted(m)), map_version=versions.pop())

    def stock_coverage(self, day: str) -> DayCoverage | None:
        if day not in self._stock:
            return None
        m = self._stock[day]
        versions = set(m.values())
        if len(versions) != 1:
            raise ValueError(f"stock 同日出现多个 map_version: {day}")
        return DayCoverage(day=day, sites=tuple(sorted(m)), map_version=versions.pop())
