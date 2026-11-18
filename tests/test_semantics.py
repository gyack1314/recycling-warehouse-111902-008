"""指标语义测试：独立人数、单价分母、期末库存、person_count 边界。"""
import tempfile
import unittest
from pathlib import Path

from querymart.service import run_query
from tests.fixtures import build_inputs, base_query


class DistinctPeople(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_inputs(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_cross_day_distinct_uses_identity_not_sum(self):
        # P1 在 N、S、跨三天反复交投，独立人数仍是 2（P1/P2），不是各日相加
        q = base_query(through="2026-10-03", metrics=["people"], detail_row_budget=4)
        r = run_query(q, self.root)
        self.assertEqual(r["metrics"][0]["rows"][0]["value"], 2)
        self.assertEqual(r["metrics"][0]["sources"][0]["source"], "deliveries")

    def test_person_count_not_merged_across_sites(self):
        # 单日两站都有 person_count=1，但无身份集合；无维度整市请求可走 city_day=1，
        # 带 site_filter 跨站时不能把两个 person_count 相加。
        q = base_query(metrics=["people"], site_filter=["N", "S"], detail_row_budget=10)
        r = run_query(q, self.root)
        metric = r["metrics"][0]
        # 回明细做身份集合：P1 同一人 -> 1
        self.assertEqual(metric["rows"][0]["value"], 1)
        self.assertEqual(metric["sources"][0]["source"], "deliveries")

    def test_single_site_single_day_uses_summary_count(self):
        q = base_query(metrics=["people"], dimensions=["site"],
                       site_filter=["N"], detail_row_budget=0)
        r = run_query(q, self.root)
        self.assertEqual(r["metrics"][0]["rows"][0]["value"], 1)
        self.assertEqual(r["metrics"][0]["sources"][0]["source"], "site_day")
        self.assertEqual(r["runtime"]["detail_rows_read"], 0)

    def test_district_people_requires_detail(self):
        q = base_query(through="2026-10-01", metrics=["people"],
                       dimensions=["district"], detail_row_budget=10)
        r = run_query(q, self.root)
        rows = {row["keys"]["district"]: row["value"] for row in r["metrics"][0]["rows"]}
        self.assertEqual(rows, {"A": 1})  # P1 在两站仍是同一人


class PriceSemantics(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_inputs(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_price_is_total_over_total_not_average_of_rates(self):
        # 10-01: 180 分 / 3 吨 = 60；行均价 (50+80)/2=65 必须不被采用
        q = base_query(metrics=["price"])
        r = run_query(q, self.root)
        self.assertEqual(r["metrics"][0]["rows"][0]["value"], "60.000")

    def test_zero_denominator_distinct_from_unknown(self):
        # 10-02 N 站：重量 0、金额 0 -> 零分母，无定义
        q = base_query(**{"from": "2026-10-02", "through": "2026-10-02"},
                       metrics=["price"], site_filter=["N"])
        r = run_query(q, self.root)
        metric = r["metrics"][0]
        self.assertEqual(metric["rows"], [])
        self.assertEqual(metric["gaps"][0]["code"], "zero_denominator")

    def test_unknown_denominator_when_no_coverage(self):
        # 只有 10-04 的请求：明细覆盖但零行，分母未知（无数据），与零分母区分
        q = base_query(**{"from": "2026-10-04", "through": "2026-10-04"},
                       metrics=["price"], detail_row_budget=10)
        r = run_query(q, self.root)
        metric = r["metrics"][0]
        self.assertEqual(metric["rows"], [])
        # 零行明细段：分母未知，不能报零分母，也不能编造单价
        self.assertTrue(all(g["code"] != "zero_denominator" for g in metric["gaps"]))


class StockSemantics(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_inputs(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_period_end_uses_last_completed_same_caliber_day(self):
        q = base_query(through="2026-10-02", metrics=["stock"],
                       dimensions=["site"], site_filter=["N"])
        r = run_query(q, self.root)
        row = r["metrics"][0]["rows"][0]
        self.assertEqual(row["value"], "8.000")
        self.assertEqual(row["as_of_day"], "2026-10-02")

    def test_older_day_not_used_when_latest_caliber_incomplete(self):
        # M2 窗口末日 10-03 只有 N 站；S 缺失不能拿更早 M1 状态顶替，也不能当 0
        q = base_query(through="2026-10-03", metrics=["stock"],
                       dimensions=["district"], map_version="M2")
        r = run_query(q, self.root)
        metric = r["metrics"][0]
        self.assertEqual(metric["status"], "partial")
        b_row = next(x for x in metric["rows"] if x["keys"] == {"district": "B"})
        self.assertEqual(b_row["value"], "11.000")
        gap = next(g for g in metric["gaps"] if g["scope"] == {"district": "A"})
        self.assertEqual(gap["code"], "period_end_incomplete")

    def test_stock_never_falls_back_to_deliveries(self):
        # 无任何同口径库存声明时（清空 stock 覆盖），即便有明细预算也拒绝推算
        with tempfile.TemporaryDirectory() as d:
            root = build_inputs(Path(d), coverage={
                "deliveries": {"from": "2026-10-01", "through": "2026-10-04",
                               "complete": True},
                "site_day": [], "city_day": [], "stock": [],
            })
            q = base_query(through="2026-10-03", metrics=["stock"],
                           map_version="M2", detail_row_budget=10)
            r = run_query(q, root)
            metric = r["metrics"][0]
            self.assertTrue(all(g["code"] == "no_same_caliber_stock"
                                for g in metric["gaps"]))
            self.assertNotIn("deliveries.parquet", r["runtime"]["files_opened"])

    def test_stock_not_summed_across_days(self):
        # 即使窗口含多个库存日，也只取末日状态，绝不把 10+8 相加
        q = base_query(through="2026-10-02", metrics=["stock"],
                       dimensions=["site"], site_filter=["N"])
        r = run_query(q, self.root)
        self.assertEqual(r["metrics"][0]["rows"][0]["value"], "8.000")


if __name__ == "__main__":
    unittest.main()
