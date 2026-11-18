"""命令行入口。

python -m querymart query [--query ID] [--inputs DIR] [--outputs DIR]
python -m querymart reconcile --query ID [--inputs DIR] [--outputs DIR]
python -m querymart compare --query ID [--inputs DIR] [--outputs DIR]

受限查询与无预算核对的产物分目录存放；compare 只做只读比对，不回写受限结果。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .reconcile import reconcile
from .service import run_all, run_query

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUTS = ROOT / "inputs"
DEFAULT_OUTPUTS = ROOT / "outputs"


def _load_query(inputs: Path, query_id: str | None) -> list[dict]:
    queries = json.loads((inputs / "queries.json").read_text(encoding="utf-8"))
    if query_id is None:
        return queries
    matched = [q for q in queries if q["id"] == query_id]
    if not matched:
        raise SystemExit(f"queries.json 中没有 id={query_id} 的请求")
    return matched


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_query(args: argparse.Namespace) -> int:
    inputs = Path(args.inputs).resolve()
    outputs = Path(args.outputs).resolve() / "restricted"
    queries = _load_query(inputs, args.query)
    results = run_all(queries, inputs)
    for result in results:
        _write_json(outputs / f"{result['query_id']}.json", result)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    refused = [r["query_id"] for r in results if r["status"] != "ok"]
    if refused:
        print(f"# 未完全回答的请求: {', '.join(refused)}", file=sys.stderr)
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    inputs = Path(args.inputs).resolve()
    outputs = Path(args.outputs).resolve() / "reconcile"
    (query,) = _load_query(inputs, args.query)
    result = reconcile(query, inputs, outputs)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"# 核对缓存（受限请求禁用）: {result['cache_path']}", file=sys.stderr)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """并排比较受限执行与无预算明细核对，仅报告差异，不修改任何受限结果。"""
    inputs = Path(args.inputs).resolve()
    outputs = Path(args.outputs).resolve()
    (query,) = _load_query(inputs, args.query)
    restricted = run_query(query, inputs)
    check = reconcile(query, inputs, outputs / "reconcile")

    def index(answer_set: list[dict]) -> dict[tuple, dict]:
        idx = {}
        for m in answer_set:
            for r in m["rows"]:
                idx[(m["metric"], json.dumps(r["keys"], sort_keys=True))] = r
        return idx

    left, right = index(restricted["metrics"]), index(check["metrics"])
    diffs = []
    for key in sorted(set(left) | set(right)):
        a, b = left.get(key), right.get(key)
        if a is None or b is None or a["value"] != b["value"]:
            diffs.append({"metric_key": list(key), "restricted": a, "reconcile": b})
    report = {
        "query_id": query["id"],
        "restricted_status": restricted["status"],
        "restricted_detail_rows_read": restricted["runtime"]["detail_rows_read"],
        "reconcile_detail_rows_read": check["runtime"]["detail_rows_read"],
        "value_differences": diffs,
        "verdict": "一致" if not diffs else "存在差异，需核对来源口径",
    }
    _write_json(outputs / "comparisons" / f"{query['id']}.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="querymart",
                                description="多粒度回收集市的预算感知查询入口")
    p.add_argument("--inputs", default=str(DEFAULT_INPUTS), help="原件目录")
    p.add_argument("--outputs", default=str(DEFAULT_OUTPUTS), help="产品输出根目录")
    sub = p.add_subparsers(dest="command", required=True)

    q = sub.add_parser("query", help="执行受限查询（遵守 detail_row_budget）")
    q.add_argument("--query", default=None, help="只跑指定 id；缺省跑全部")
    q.set_defaults(func=cmd_query)

    r = sub.add_parser("reconcile", help="无预算全量明细核对（独立缓存，受限请求禁用）")
    r.add_argument("--query", required=True)
    r.set_defaults(func=cmd_reconcile)

    c = sub.add_parser("compare", help="并排比较汇总复用与明细回退")
    c.add_argument("--query", required=True)
    c.set_defaults(func=cmd_compare)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
