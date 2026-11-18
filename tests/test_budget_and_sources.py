"""预算纪律与来源选择测试。"""
import tempfile
import unittest
from pathlib import Path

from querymart.planner import RequestError
from querymart.service import run_query
from tests.fixtures import BASE_COVERAGE, build_inputs, base_query


class SourceSelection(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_inputs(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_city_day_served_without_detail_budget(self):
        q = base_query(metrics=["weight", "price"])
        r = run_query(q, self.root)
        self.assertEqual(r["status"], "ok")
        weight, price = r["metrics"]
        self.assertEqual(weight["rows"][0]["value"], "3.000")
        self.assertEqual(price["rows"][0]["value"], "60.000")
        self.assertEqual(r["runtime"]["detail_rows_read"], 0)
        self.assertNotIn("deliveries.parquet", r["runtime"]["files_opened"])
        self.assertEqual(weight["sources"][0]["source"], "city_day")
        self.assertEqual(weight["mart_versions"], ["M1"])

    def test_district_dimension_cannot_use_m1_summary_under_m2(self):
        # M1 汇总不能重贴 M2 标签：district 维度 + M2 只能回明细
        q = base_query(through="2026-10-03", dimensions=["district"],
                       metrics=["weight"], map_version="M2", detail_row_budget=1)
        r = run_query(q, self.root)
        metric = r["metrics"][0]
        self.assertEqual(metric["status"], "gap")
        self.assertEqual(metric["gaps"][0]["code"], "detail_budget_exceeded")
        self.assertEqual(metric["gaps"][0]["needed_detail_rows"], 2)
        self.assertEqual(r["runtime"]["detail_rows_read"], 1)

    def test_detail_rejoin_under_requested_map(self):
        q = base_query(through="2026-10-01", dimensions=["district"],
                       metrics=["weight"], map_version="M2", detail_row_budget=10)
        r = run_query(q, self.root)
        rows = {row["keys"]["district"]: row["value"] for row in r["metrics"][0]["rows"]}
        # M2: N->B(2.000), S->A(1.000)；明细自带的 A 标签不得沿用
        self.assertEqual(rows, {"A": "1.000", "B": "2.000"})
        self.assertEqual(r["runtime"]["detail_rows_read"], 2)

    def test_unknown_metric_or_dimension_is_refused_not_dropped(self):
        with self.assertRaises(RequestError):
            run_query(base_query(metrics=["weight", "mystery"]), self.root)
        with self.assertRaises(RequestError):
            run_query(base_query(dimensions=["road"]), self.root)
        with self.assertRaises(RequestError):
            run_query(base_query(map_version="M9"), self.root)

    def test_disjoint_summary_and_detail_segments(self):
        # 10-01 走 city_day，10-03 走明细，10-02 无市日 -> 明细；段不可重叠
        q = base_query(through="2026-10-03", metrics=["weight"], detail_row_budget=10)
        r = run_query(q, self.root)
        srcs = {(s["source"], tuple(s["days"])) for s in r["metrics"][0]["sources"]}
        self.assertIn(("city_day", ("2026-10-01",)), srcs)
        detail_days = [d for src, days in srcs if src == "deliveries" for d in days]
        self.assertEqual(sorted(detail_days), ["2026-10-02", "2026-10-03"])
        # 总重量 3 + (0 + 3) = 6
        self.assertEqual(r["metrics"][0]["rows"][0]["value"], "6.000")


class BudgetDiscipline(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_inputs(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_zero_budget_runs_no_detail_sql(self):
        q = base_query(through="2026-10-03", metrics=["people"], detail_row_budget=0)
        r = run_query(q, self.root)
        self.assertEqual(r["metrics"][0]["status"], "gap")
        self.assertEqual(r["metrics"][0]["gaps"][0]["code"], "detail_budget_exceeded")
        self.assertNotIn("deliveries.parquet", r["runtime"]["files_opened"])
        self.assertEqual(r["runtime"]["detail_rows_read"], 0)

    def test_budget_exactly_matching_rows_succeeds(self):
        q = base_query(through="2026-10-03", metrics=["people"], detail_row_budget=4)
        r = run_query(q, self.root)
        self.assertEqual(r["metrics"][0]["rows"][0]["value"], 2)
        self.assertEqual(r["runtime"]["detail_rows_read"], 4)

    def test_budget_short_by_one_guards_without_full_scan(self):
        q = base_query(through="2026-10-03", metrics=["people"], detail_row_budget=3)
        r = run_query(q, self.root)
        gap = r["metrics"][0]["gaps"][0]
        self.assertEqual(gap["code"], "detail_budget_exceeded")
        self.assertEqual(gap["needed_detail_rows"], 4)
        # 守卫只读到预算上限即止，绝不预读全表后伪装低成本
        self.assertLessEqual(r["runtime"]["detail_rows_read"], 3)

    def test_no_estimates_returned_on_refusal(self):
        # 跨 3 天且预算为 0：汇总无法独立覆盖整段，两个指标都只能拒绝
        q = base_query(through="2026-10-03", metrics=["weight", "people"],
                       detail_row_budget=0)
        r = run_query(q, self.root)
        for metric in r["metrics"]:
            self.assertTrue(metric["gaps"])
        # 没有任何明细读取痕迹
        self.assertNotIn("deliveries.parquet", r["runtime"]["files_opened"])


class UncoveredDays(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # 明细只到 10-02；10-03 未声明且无明细
        coverage = dict(BASE_COVERAGE)
        coverage["deliveries"] = {"from": "2026-10-01", "through": "2026-10-02",
                                  "complete": True}
        self.root = build_inputs(Path(self._tmp.name), coverage=coverage)

    def tearDown(self):
        self._tmp.cleanup()

    def test_undeclared_day_is_gap_not_zero(self):
        q = base_query(through="2026-10-03", metrics=["weight"], detail_row_budget=10)
        r = run_query(q, self.root)
        metric = r["metrics"][0]
        codes = {g["code"] for g in metric["gaps"]}
        self.assertIn("uncovered_day", codes)
        # 10-01 的合法结果仍给出，但 10-03 不得当 0 参与
        self.assertEqual(metric["rows"][0]["value"], "3.000")

    def test_declared_empty_coverage_is_legal_zero(self):
        # 10-04 明细完整空覆盖（零行）：合法空，不是缺口
        coverage = {
            "deliveries": {"from": "2026-10-01", "through": "2026-10-04", "complete": True},
            "site_day": [], "city_day": [], "stock": [],
        }
        with tempfile.TemporaryDirectory() as d:
            root = build_inputs(Path(d), coverage=coverage)
            q = base_query(**{"from": "2026-10-04", "through": "2026-10-04"},
                           metrics=["weight", "people"], detail_row_budget=10)
            r = run_query(q, root)
            for metric in r["metrics"]:
                self.assertEqual(metric["gaps"], [])
            weight = next(m for m in r["metrics"] if m["metric"] == "weight")
            people = next(m for m in r["metrics"] if m["metric"] == "people")
            self.assertEqual(weight["rows"][0]["value"], "0.000")
            self.assertEqual(people["rows"][0]["value"], 0)


if __name__ == "__main__":
    unittest.main()
