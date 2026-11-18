"""测试夹具：在临时目录构造不同覆盖形态的合成资料，全部经真实 DuckDB/Parquet 执行。"""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

# 与 inputs/seed.json 同形状，测试可按需增删行与覆盖声明。
BASE_DELIVERIES = [
    {"row": "R1", "day": "2026-10-01", "site": "N", "district": "A", "person": "P1",
     "material": "paper", "weight": "2.000", "amount_minor": 100},
    {"row": "R2", "day": "2026-10-01", "site": "S", "district": "A", "person": "P1",
     "material": "paper", "weight": "1.000", "amount_minor": 80},
    {"row": "R3", "day": "2026-10-02", "site": "N", "district": "A", "person": "P2",
     "material": "glass", "weight": "0.000", "amount_minor": 0},
    {"row": "R4", "day": "2026-10-03", "site": "N", "district": "A", "person": "P1",
     "material": "paper", "weight": "3.000", "amount_minor": 210},
]
BASE_SITE_DAY = [
    {"day": "2026-10-01", "site": "N", "map_version": "M1", "weight_sum": "2.000",
     "amount_sum": 100, "person_count": 1},
    {"day": "2026-10-01", "site": "S", "map_version": "M1", "weight_sum": "1.000",
     "amount_sum": 80, "person_count": 1},
    {"day": "2026-10-02", "site": "N", "map_version": "M1", "weight_sum": "0.000",
     "amount_sum": 0, "person_count": 1},
]
BASE_CITY_DAY = [
    {"day": "2026-10-01", "map_version": "M1", "weight_sum": "3.000",
     "amount_sum": 180, "person_count": 1},
]
BASE_STOCK = [
    {"day": "2026-10-01", "site": "N", "map_version": "M1", "closing": "10.000"},
    {"day": "2026-10-02", "site": "N", "map_version": "M1", "closing": "8.000"},
    {"day": "2026-10-03", "site": "N", "map_version": "M2", "closing": "11.000"},
]
BASE_SITE_MAP = [
    {"version": "M1", "site": "N", "district": "A"},
    {"version": "M1", "site": "S", "district": "A"},
    {"version": "M2", "site": "N", "district": "B"},
    {"version": "M2", "site": "S", "district": "A"},
]
BASE_COVERAGE = {
    "deliveries": {"from": "2026-10-01", "through": "2026-10-04", "complete": True},
    "site_day": [
        {"day": "2026-10-01", "sites": ["N", "S"], "map_version": "M1"},
        {"day": "2026-10-02", "sites": ["N"], "map_version": "M1"},
    ],
    "city_day": [{"day": "2026-10-01", "map_version": "M1"}],
    "stock": [
        {"day": "2026-10-01", "sites": ["N"], "map_version": "M1"},
        {"day": "2026-10-02", "sites": ["N"], "map_version": "M1"},
        {"day": "2026-10-03", "sites": ["N"], "map_version": "M2"},
    ],
}


def build_inputs(tmp: Path, *, rows: dict | None = None,
                 coverage: dict | None = None) -> Path:
    rows = rows or {}
    data = {
        "deliveries": rows.get("deliveries", BASE_DELIVERIES),
        "site_day": rows.get("site_day", BASE_SITE_DAY),
        "city_day": rows.get("city_day", BASE_CITY_DAY),
        "stock": rows.get("stock", BASE_STOCK),
        "site_map": rows.get("site_map", BASE_SITE_MAP),
    }
    root = tmp / "inputs"
    root.mkdir(parents=True, exist_ok=True)
    for name, payload in data.items():
        pq.write_table(pa.Table.from_pylist(payload), root / f"{name}.parquet",
                       row_group_size=2, compression="zstd")
    (root / "coverage.json").write_text(
        json.dumps(coverage or BASE_COVERAGE, ensure_ascii=False, indent=2),
        encoding="utf-8")
    (root / "queries.json").write_text("[]", encoding="utf-8")
    return root


def base_query(**overrides) -> dict:
    q = {
        "id": "tq",
        "from": "2026-10-01",
        "through": "2026-10-01",
        "dimensions": [],
        "metrics": ["weight"],
        "map_version": "M1",
        "detail_row_budget": 0,
    }
    q.update(overrides)
    return q
