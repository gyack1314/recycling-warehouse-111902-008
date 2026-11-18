"""无预算明细核对路径。

与受限请求物理隔离：
- 使用独立的 output 目录缓存核对结果，受限请求从不读取该目录；
- 独立内存连接、全量扫描 deliveries，没有 LIMIT 守卫；
- 核对缓存带有 mode='reconcile' 标记，受限入口若被指向该文件将拒绝使用。

库存是状态表、不可由明细推算，因此核对路径只覆盖 weight / price / people。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

from .catalog import Catalog, daterange
from .engine import QueryEngine, RuntimeRecord
from .planner import (
    Gap,
    MetricAnswer,
    Segment,
    _aggregate_amounts,
    _aggregate_people,
    _all_group_keys,
    _key_dict,
    requested_sites,
    validate_request,
)

RECONCILE_TAG = "reconcile-only-cache"


def _fingerprint(query: dict) -> str:
    payload = json.dumps(query, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def reconcile(query: dict, inputs_dir: str | Path, output_dir: str | Path) -> dict:
    """全量明细核对。忽略请求中的 detail_row_budget，但在结果中显式记录原值。"""
    inputs_dir = Path(inputs_dir).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    runtime = RuntimeRecord(detail_row_budget=-1)  # -1 表示无预算路径
    bootstrap = QueryEngine(inputs_dir, detail_row_budget=0)
    catalog = Catalog(inputs_dir)
    catalog.set_site_map(bootstrap.load_site_map(runtime))
    validate_request(query, catalog)

    days = daterange(query["from"], query["through"])
    sites = requested_sites(query, catalog)
    dims = query.get("dimensions", [])
    mv = query["map_version"]

    if not catalog.deliveries_range_covers(days):
        missing = [d for d in days if not catalog.deliveries_covers(d)]
        raise ValueError(f"核对范围超出完整明细覆盖: {missing}")

    # 独立引擎：无守卫、全量扫描（与受限请求的 LIMIT 守卫物理分离）
    engine = QueryEngine(inputs_dir, detail_row_budget=0)
    raw = engine.read_all_detail(days, sites, runtime,
                                 materials=list(query.get("material_filter") or []) or None)
    engine.commit_runtime_files(runtime)

    answers: list[MetricAnswer] = []
    seg = Segment(
        "deliveries", days, sites, mv,
        f"无预算核对：deliveries 全量扫描 {catalog.deliveries_from}.."
        f"{catalog.deliveries_through}；按 map={mv} 经 site_map 重关联",
        rows_read=len(raw),
    )
    if "material" in dims and not query.get("material_filter"):
        material_universe = sorted({r["material"] for r in raw})
    else:
        material_universe = list(query.get("material_filter") or [])
    expected_keys = _all_group_keys(query, catalog, sites, material_universe)

    if "weight" in query["metrics"] or "price" in query["metrics"]:
        sums = _aggregate_amounts(raw, dims, mv, catalog)
        if "weight" in query["metrics"]:
            ans = MetricAnswer("weight", dims, segments=[seg])
            for key in expected_keys:
                if key in sums:
                    ans.rows.append({"keys": _key_dict(dims, key),
                                     "value": str(sums[key][0]), "unit": "tonne"})
            answers.append(ans)
        if "price" in query["metrics"]:
            ans = MetricAnswer("price", dims, segments=[seg])
            for key in expected_keys:
                if key not in sums:
                    continue
                weight, amount = sums[key]
                if weight == 0:
                    ans.gaps.append(Gap(
                        "zero_denominator",
                        "总重量为零，单价无定义（零分母；与未知分母不同）",
                        _key_dict(dims, key)))
                else:
                    price = (Decimal(amount) / weight).quantize(Decimal("0.001"))
                    ans.rows.append({"keys": _key_dict(dims, key), "value": str(price),
                                     "unit": "CNY_minor_per_tonne"})
            answers.append(ans)

    if "people" in query["metrics"]:
        people_sets = _aggregate_people(raw, dims, mv, catalog)
        ans = MetricAnswer("people", dims, segments=[seg])
        for key in expected_keys:
            if key in people_sets:
                ans.rows.append({"keys": _key_dict(dims, key),
                                 "value": len(people_sets[key]), "unit": "distinct_person"})
        answers.append(ans)

    if "stock" in query["metrics"]:
        ans = MetricAnswer("stock", dims)
        ans.gaps.append(Gap(
            "stock_not_reconcilable_from_detail",
            "库存是站日状态，投递明细无法推算期末库存；请以 stock 汇总的同口径状态为准"))
        answers.append(ans)

    result = {
        "mode": RECONCILE_TAG,
        "query_id": query["id"],
        "request_budget_ignored": query["detail_row_budget"],
        "request": {
            "from": query["from"], "through": query["through"],
            "dimensions": dims, "metrics": query["metrics"],
            "site_filter": query.get("site_filter", []),
            "material_filter": query.get("material_filter", []),
            "map_version": mv,
        },
        "metrics": [a.as_dict() for a in answers],
        "runtime": runtime.as_dict(),
        "note": "无预算独立核对结果；受限请求不得读取本缓存充当执行结果。",
    }

    cache_path = output_dir / f"reconcile_{query['id']}_{_fingerprint(query)}.json"
    cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["cache_path"] = str(cache_path)
    return result


def assert_not_reconcile_cache(path: str | Path) -> None:
    """受限入口的守卫：拒绝把核对缓存当作受限执行结果。"""
    p = Path(path)
    if p.is_file():
        tag = json.loads(p.read_text(encoding="utf-8")).get("mode")
        if tag == RECONCILE_TAG:
            raise PermissionError("拒绝读取无预算核对缓存：该文件不能冒充受限请求的执行结果")
