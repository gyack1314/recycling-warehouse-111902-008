"""无预算核对路径、缓存隔离与运行时记录测试。"""
import json
import tempfile
import unittest
from pathlib import Path

from querymart.reconcile import RECONCILE_TAG, assert_not_reconcile_cache, reconcile
from querymart.service import run_query
from tests.fixtures import build_inputs, base_query


class ReconcilePath(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = build_inputs(base)
        self.outputs = base / "outputs"

    def tearDown(self):
        self._tmp.cleanup()

    def test_reconcile_ignores_budget_and_scans_all(self):
        q = base_query(through="2026-10-03", metrics=["people"], detail_row_budget=0)
        result = reconcile(q, self.root, self.outputs)
        self.assertEqual(result["mode"], RECONCILE_TAG)
        self.assertEqual(result["request_budget_ignored"], 0)
        self.assertEqual(result["metrics"][0]["rows"][0]["value"], 2)
        self.assertEqual(result["runtime"]["detail_row_budget"], -1)
        self.assertGreaterEqual(result["runtime"]["detail_rows_read"], 4)
        self.assertTrue(Path(result["cache_path"]).is_file())

    def test_reconcile_cache_is_rejected_for_restricted_use(self):
        q = base_query(through="2026-10-03", metrics=["people"], detail_row_budget=0)
        result = reconcile(q, self.root, self.outputs)
        with self.assertRaisesRegex(PermissionError, "核对缓存"):
            assert_not_reconcile_cache(result["cache_path"])

    def test_restricted_request_never_reads_reconcile_directory(self):
        q = base_query(through="2026-10-03", metrics=["people"], detail_row_budget=0)
        reconcile(q, self.root, self.outputs)
        # 受限请求独立执行，产物与核对目录分离，结果仍为拒绝
        r = run_query(q, self.root)
        self.assertEqual(r["metrics"][0]["status"], "gap")
        for stmt in r["runtime"]["statements"]:
            self.assertNotIn("outputs", stmt)
            self.assertNotIn("reconcile", stmt)

    def test_reconcile_and_restricted_agree_when_budget_allows(self):
        q = base_query(through="2026-10-03", metrics=["weight", "people"],
                       detail_row_budget=4)
        restricted = run_query(q, self.root)
        check = reconcile(q, self.root, self.outputs)
        def values(answer_set):
            return {
                (m["metric"], json.dumps(row["keys"], sort_keys=True)): row["value"]
                for m in answer_set["metrics"] for row in m["rows"]
            }
        self.assertEqual(values(restricted), values(check))

    def test_stock_not_reconcilable_from_detail(self):
        q = base_query(metrics=["stock"], dimensions=["site"], site_filter=["N"])
        result = reconcile(q, self.root, self.outputs)
        gap = result["metrics"][0]["gaps"][0]
        self.assertEqual(gap["code"], "stock_not_reconcilable_from_detail")


class RuntimeAccounting(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_inputs(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_runtime_lists_real_files_and_scans(self):
        # 无维度整市单日 -> city_day 汇总
        q = base_query(metrics=["weight"], dimensions=[])
        r = run_query(q, self.root)
        opened = r["runtime"]["files_opened"]
        self.assertIn("city_day.parquet", opened)
        self.assertIn("site_map.parquet", opened)
        # 每条 scan 都能定表，没有 unknown
        self.assertTrue(all(s["source"] != "unknown" for s in r["runtime"]["scans"]))
        self.assertTrue(r["runtime"]["statements"])

        # 站维度 -> site_day 汇总
        q2 = base_query(metrics=["weight"], dimensions=["site"], site_filter=["N"])
        r2 = run_query(q2, self.root)
        self.assertIn("site_day.parquet", r2["runtime"]["files_opened"])

    def test_detail_rows_never_exceed_budget(self):
        for budget in range(0, 5):
            q = base_query(through="2026-10-03", metrics=["weight"],
                           detail_row_budget=budget)
            r = run_query(q, self.root)
            self.assertLessEqual(r["runtime"]["detail_rows_read"], budget)

    def test_shared_detail_block_charged_once(self):
        # 无汇总可用时，weight 与 people 共用同一连续明细段，物理只扫一次
        coverage = {
            "deliveries": {"from": "2026-10-01", "through": "2026-10-04",
                           "complete": True},
            "site_day": [], "city_day": [], "stock": [],
        }
        with tempfile.TemporaryDirectory() as d:
            root = build_inputs(Path(d), coverage=coverage)
            q = base_query(through="2026-10-03", metrics=["weight", "people"],
                           detail_row_budget=4)
            r = run_query(q, root)
            deliveries_scans = [s for s in r["runtime"]["scans"]
                                if s["source"] == "deliveries"]
            self.assertEqual(len(deliveries_scans), 1)
            self.assertEqual(r["runtime"]["detail_rows_read"], 4)


if __name__ == "__main__":
    unittest.main()
