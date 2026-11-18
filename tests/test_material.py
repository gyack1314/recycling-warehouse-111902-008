"""material 过滤/分组：汇总不支持时必须回明细，禁止静默删除条件。"""
import tempfile
import unittest
from pathlib import Path

from querymart.service import run_query
from tests.fixtures import build_inputs, base_query


class MaterialSupport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = build_inputs(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_material_filter_zero_budget_refused_not_ignored(self):
        q = base_query(metrics=["weight"], material_filter=["glass"])
        r = run_query(q, self.root)
        self.assertEqual(r["status"], "refused")
        self.assertEqual(r["metrics"][0]["gaps"][0]["code"], "detail_budget_exceeded")
        # 零预算下没有任何明细 Parquet 被打开
        self.assertNotIn("deliveries.parquet", r["runtime"]["files_opened"])

    def test_material_filter_pushed_down_and_counted_honestly(self):
        q_paper = base_query(through="2026-10-03", metrics=["weight"],
                             material_filter=["paper"], detail_row_budget=10)
        r = run_query(q_paper, self.root)
        self.assertEqual(r["metrics"][0]["rows"][0]["value"], "6.000")  # 2+1+3
        # 过滤下推到扫描（SQL 含 material IN）；物理行组内含混合 material 无法裁剪，
        # 故物理扫描 4 行如实记账，过滤后参与计算 3 行在段级记录，两者不混淆。
        self.assertIn("material IN", r["runtime"]["statements"][-1])
        self.assertEqual(r["runtime"]["detail_rows_read"], 4)
        self.assertEqual(r["metrics"][0]["sources"][0]["detail_rows_read"], 3)

    def test_material_filter_budget_shortfall_uses_physical_scan(self):
        # 预算 2：物理扫描在行组边界停止，守卫拒绝，不返回估计
        q = base_query(through="2026-10-03", metrics=["weight"],
                       material_filter=["paper"], detail_row_budget=2)
        r = run_query(q, self.root)
        self.assertEqual(r["metrics"][0]["status"], "gap")
        self.assertLessEqual(r["runtime"]["detail_rows_read"], 2)

    def test_material_filter_absent_material_yields_legal_zero(self):
        # 10-01 只有 paper；过滤 glass 为完整覆盖下的零行 -> 0，不是缺口
        q = base_query(metrics=["weight"], material_filter=["glass"],
                       detail_row_budget=10)
        r = run_query(q, self.root)
        self.assertEqual(r["metrics"][0]["rows"][0]["value"], "0.000")
        self.assertEqual(r["metrics"][0]["gaps"], [])

    def test_material_grouping_returns_per_material_totals(self):
        q = base_query(through="2026-10-03", metrics=["weight"],
                       dimensions=["material"], detail_row_budget=10)
        r = run_query(q, self.root)
        rows = {x["keys"]["material"]: x["value"] for x in r["metrics"][0]["rows"]}
        self.assertEqual(rows, {"glass": "0.000", "paper": "6.000"})

    def test_stock_rejects_material_condition(self):
        q = base_query(metrics=["stock"], dimensions=["site"], site_filter=["N"],
                       material_filter=["paper"])
        r = run_query(q, self.root)
        self.assertEqual(r["metrics"][0]["gaps"][0]["code"], "dimension_unsupported")

    def test_material_people_uses_identity_set(self):
        # paper 的投递人：P1（R1,R2,R4）；glass：P2（R3）
        q = base_query(through="2026-10-03", metrics=["people"],
                       material_filter=["paper"], detail_row_budget=10)
        r = run_query(q, self.root)
        self.assertEqual(r["metrics"][0]["rows"][0]["value"], 1)


if __name__ == "__main__":
    unittest.main()
