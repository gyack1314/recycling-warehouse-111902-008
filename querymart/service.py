"""请求编排：为一个受限查询选择来源、执行并组装可审计结果。"""
from __future__ import annotations

from pathlib import Path

from .catalog import Catalog
from .engine import QueryEngine, RuntimeRecord
from .planner import (
    DetailBlockCache,
    MetricAnswer,
    plan_amounts,
    plan_people,
    plan_stock,
    validate_request,
)


def run_query(query: dict, inputs_dir: str | Path) -> dict:
    """执行单个受限请求。不抛业务缺口异常；缺口写入 metrics/gaps。"""
    inputs_dir = Path(inputs_dir).resolve()
    runtime = RuntimeRecord(detail_row_budget=query.get("detail_row_budget", 0))

    # 目录与映射：site_map 也经 DuckDB 真实读取
    bootstrap = QueryEngine(inputs_dir, detail_row_budget=0)
    catalog = Catalog(inputs_dir)
    catalog.set_site_map(bootstrap.load_site_map(runtime))
    validate_request(query, catalog)

    engine = QueryEngine(inputs_dir, detail_row_budget=query["detail_row_budget"])
    cache = DetailBlockCache(engine, runtime)

    metric_answers: list[MetricAnswer] = []
    amount_metrics = [m for m in query["metrics"] if m in ("weight", "price")]
    if amount_metrics:
        metric_answers.extend(
            plan_amounts(query, catalog, engine, runtime, cache).values())
    if "people" in query["metrics"]:
        metric_answers.append(plan_people(query, catalog, engine, runtime, cache))
    if "stock" in query["metrics"]:
        metric_answers.append(plan_stock(query, catalog, engine, runtime))

    engine.commit_runtime_files(runtime)

    statuses = {a.status for a in metric_answers}
    if statuses == {"ok"}:
        overall = "ok"
    elif not any(a.rows for a in metric_answers):
        overall = "refused"
    else:
        overall = "partial"

    return {
        "query_id": query["id"],
        "status": overall,
        "request": {
            "from": query["from"],
            "through": query["through"],
            "dimensions": query.get("dimensions", []),
            "metrics": query["metrics"],
            "site_filter": query.get("site_filter", []),
            "material_filter": query.get("material_filter", []),
            "map_version": query["map_version"],
            "detail_row_budget": query["detail_row_budget"],
        },
        "metrics": [a.as_dict() for a in metric_answers],
        "runtime": runtime.as_dict(),
        "coverage_reference": "inputs/coverage.json（声明区间内零行为合法空覆盖；未声明日期不视为空）",
    }


def run_all(queries: list[dict], inputs_dir: str | Path) -> list[dict]:
    return [run_query(q, inputs_dir) for q in queries]
