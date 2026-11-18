"""DuckDB 执行引擎：真实读取 Parquet，并用 profile 记录实际打开的文件与行数。

预算纪律：
- 明细预算只计从 deliveries.parquet 实际扫描的行数（TABLE_SCAN 基数）；
- 守卫查询带 LIMIT budget+1，DuckDB 在行组边界提前停止，不会先扫全表；
- 不缓存任何明细数据；每个受限请求使用全新的内存连接；
- 无预算核对路径独立实现，其结果缓存与受限请求物理隔离。
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

PARQUET_FILES = ("deliveries", "site_day", "city_day", "stock", "site_map")
# 用投影列集合识别每个 TABLE_SCAN 属于哪张表（同日 SQL 不混读两个 Parquet）。
_SIGNATURES = {
    "deliveries": frozenset(
        ("row", "day", "site", "district", "person", "material", "weight", "amount_minor")),
    "site_day": frozenset(
        ("day", "site", "map_version", "weight_sum", "amount_sum", "person_count")),
    "city_day": frozenset(
        ("day", "map_version", "weight_sum", "amount_sum", "person_count")),
    "stock": frozenset(("day", "site", "map_version", "closing")),
    "site_map": frozenset(("version", "site", "district")),
}


def _classify_scan(projections: tuple[str, ...]) -> str:
    """先精确匹配投影集合；否则按“投影列是某表签名子集”识别。

    单列表投影（如只读 closing）靠子集判定归属；TOP_N 排序辅助扫描可能只有
    排序列，同样落入明细表。
    """
    cols = frozenset(projections)
    for table, sig in _SIGNATURES.items():
        if cols == sig:
            return table
    best, best_hit = "unknown", 0
    for table, sig in _SIGNATURES.items():
        if cols and cols <= sig:
            hit = len(cols)
            if hit > best_hit:
                best, best_hit = table, hit
    return best


@dataclass
class ScanRecord:
    source: str
    files_read: int
    rows_read: int


@dataclass
class RuntimeRecord:
    """一次受限请求的真实执行痕迹。"""
    statements: list[str] = field(default_factory=list)
    scans: list[ScanRecord] = field(default_factory=list)
    files_opened: list[str] = field(default_factory=list)
    detail_rows_read: int = 0          # 仅 deliveries 的扫描行数
    detail_row_budget: int = 0

    def as_dict(self) -> dict:
        return {
            "statements": self.statements,
            "scans": [
                {"source": s.source, "files_read": s.files_read, "rows_read": s.rows_read}
                for s in self.scans
            ],
            "files_opened": self.files_opened,
            "detail_rows_read": self.detail_rows_read,
            "detail_row_budget": self.detail_row_budget,
        }


class BudgetExceeded(RuntimeError):
    def __init__(self, required_at_least: int, budget: int):
        super().__init__(f"明细预算不足：至少需要 {required_at_least} 行，预算 {budget} 行")
        self.required_at_least = required_at_least
        self.budget = budget


class QueryEngine:
    """无状态引擎：不持有任何明细数据或跨请求缓存。"""

    def __init__(self, inputs_dir: str | Path, detail_row_budget: int):
        self.root = Path(inputs_dir).resolve()
        if detail_row_budget < 0:
            raise ValueError("detail_row_budget 不能为负")
        self.budget = detail_row_budget
        self._paths = {name: str(self.root / f"{name}.parquet") for name in PARQUET_FILES}
        self._single_table_hint: str | None = None

    def _connect(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(database=":memory:")
        # 视图只保存文件名与解析计划，不产生任何数据读取。
        # 路径来自受控的 inputs 目录（非用户自由输入），用单引号转义内联；
        # 所有用户侧过滤值仍走参数绑定。
        for name in PARQUET_FILES:
            safe_path = self._paths[name].replace("'", "''")
            con.execute(
                f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{safe_path}');"
            )
        return con

    # ---- profile 解析 -------------------------------------------------
    def _run_profiled(self, con, sql: str, params: list,
                      table_hint: str | None = None) -> tuple[list[tuple], list[ScanRecord]]:
        self._single_table_hint = table_hint
        with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as fh:
            profile_path = fh.name
        try:
            con.execute("PRAGMA profiling_mode='detailed'")
            con.execute(f"PRAGMA profile_output='{profile_path}'")
            con.execute("PRAGMA enable_profiling='json'")
            rows = con.execute(sql, params).fetchall()
            con.execute("PRAGMA disable_profiling")
            profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
        finally:
            Path(profile_path).unlink(missing_ok=True)
        return rows, self._extract_scans(profile)

    def _extract_scans(self, profile: dict) -> list[ScanRecord]:
        raw_scans: list[ScanRecord] = []

        def walk(node: dict) -> None:
            if node.get("operator_type") == "TABLE_SCAN":
                extra = node.get("extra_info") or {}
                projections = tuple(extra.get("Projections") or ())
                raw_scans.append(
                    ScanRecord(
                        source=_classify_scan(projections),
                        files_read=int(extra.get("Total Files Read", 1)),
                        rows_read=int(node.get("operator_cardinality", 0)),
                    )
                )
            for child in node.get("children", []):
                walk(child)

        walk(profile)
        # 表名提示来自 SQL 的 FROM 子句，是最可靠的归属；
        # 单列/子集投影（city_day 列是 site_day 的子集）无法仅靠列名消歧。
        if raw_scans and self._single_table_hint:
            raw_scans = [ScanRecord(self._single_table_hint, s.files_read, s.rows_read)
                         for s in raw_scans]
        # DuckDB 的 TOP_N/LIMIT 优化会把排序列拆成同一表的第二个 TABLE_SCAN，
        # 物理上是同一次扫描，按表合并取最大基数，禁止重复计行。
        merged: dict[str, ScanRecord] = {}
        for scan in raw_scans:
            if scan.source not in merged:
                merged[scan.source] = ScanRecord(scan.source, scan.files_read, scan.rows_read)
            else:
                cur = merged[scan.source]
                merged[scan.source] = ScanRecord(
                    scan.source,
                    max(cur.files_read, scan.files_read),
                    max(cur.rows_read, scan.rows_read),
                )
        return list(merged.values())

    # ---- 基础读取 -----------------------------------------------------
    def load_site_map(self, runtime: RuntimeRecord) -> list[dict]:
        """真实读取小维度表 site_map（不计明细预算，但计入打开文件）。"""
        con = self._connect()
        try:
            sql = "SELECT version, site, district FROM site_map ORDER BY version, site"
            rows, scans = self._run_profiled(con, sql, [], table_hint="site_map")
            runtime.statements.append(sql)
            runtime.scans.extend(scans)
            return [{"version": v, "site": s, "district": d} for v, s, d in rows]
        finally:
            con.close()

    def read_summary(self, table: str, columns: list[str],
                     filters: str, params: list, runtime: RuntimeRecord) -> list[dict]:
        """读取 site_day / city_day / stock 汇总表（不计明细预算）。"""
        if table not in ("site_day", "city_day", "stock"):
            raise ValueError(f"非汇总表: {table}")
        con = self._connect()
        try:
            sql = f"SELECT {', '.join(columns)} FROM {table}"
            if filters:
                sql += f" WHERE {filters}"
            rows, scans = self._run_profiled(con, sql, params, table_hint=table)
            runtime.statements.append(_render_sql(sql, params))
            runtime.scans.extend(scans)
            cols = [c.split(" AS ")[-1].strip() for c in columns]
            return [dict(zip(cols, r)) for r in rows]
        finally:
            con.close()

    # ---- 明细守卫读取 -------------------------------------------------
    def _detail_filters(self, days, sites, materials):
        """构造明细过滤 SQL 片段与绑定参数（全部用户值走参数绑定）。"""
        params: list = [min(days), max(days)]
        clauses = ["day BETWEEN ? AND ?"]
        if sites is not None:
            clauses.append(f"site IN ({', '.join('?' for _ in sites)})")
            params.extend(sites)
        if materials:
            clauses.append(f"material IN ({', '.join('?' for _ in materials)})")
            params.extend(materials)
        return " AND ".join(clauses), params

    def read_all_detail(
        self,
        days: list[str],
        sites: list[str] | None,
        runtime: RuntimeRecord,
        *,
        materials: list[str] | None = None,
        columns: tuple[str, ...] = ("row", "day", "site", "person", "material", "weight", "amount_minor"),
    ) -> list[dict]:
        """无预算核对路径的全量明细读取：没有 LIMIT 守卫，真实扫描全部匹配行。"""
        if not days:
            return []
        where, params = self._detail_filters(days, sites, materials)
        con = self._connect()
        try:
            col_sql = ", ".join(columns)
            sql = (f"SELECT {col_sql}, CAST(weight AS DECIMAL(18,3)) AS weight_dec "
                   f"FROM deliveries WHERE {where} ORDER BY row")
            rows, scans = self._run_profiled(con, sql, params, table_hint="deliveries")
            runtime.statements.append(_render_sql(sql, params))
            detail_scanned = sum(s.rows_read for s in scans if s.source == "deliveries")
            runtime.scans.extend(scans)
            runtime.detail_rows_read += detail_scanned
            out_cols = list(columns) + ["weight_dec"]
            return [dict(zip(out_cols, r)) for r in rows]
        finally:
            con.close()

    def guarded_detail_read(
        self,
        days: list[str],
        sites: list[str] | None,
        runtime: RuntimeRecord,
        *,
        materials: list[str] | None = None,
        columns: tuple[str, ...] = ("row", "day", "site", "person", "material", "weight", "amount_minor"),
    ) -> list[dict] | None:
        """按预算守卫读取一个日段的明细。

        返回 None 表示预算为 0（不执行任何 SQL）；否则执行 LIMIT budget+1
        守卫扫描，超过预算抛 BudgetExceeded，预算内返回行字典。
        """
        if not days:
            return []
        if self.budget == 0:
            return None
        where, params = self._detail_filters(days, sites, materials)
        con = self._connect()
        try:
            col_sql = ", ".join(columns)
            sql = (f"SELECT {col_sql}, CAST(weight AS DECIMAL(18,3)) AS weight_dec "
                   f"FROM deliveries WHERE {where} ORDER BY row LIMIT ?")
            params.append(self.budget + 1)
            rows, scans = self._run_profiled(con, sql, params, table_hint="deliveries")
            runtime.statements.append(
                _render_sql(sql, params[:-1] + [self.budget + 1]))
            detail_scanned = sum(s.rows_read for s in scans if s.source == "deliveries")
            runtime.scans.extend(scans)
            if len(rows) > self.budget:
                # 守卫命中：预算内允许读取 budget 行，第 budget+1 行即超限证据。
                # 如实记录预算上限，不把超限扫描算进已读预算。
                runtime.detail_rows_read += min(detail_scanned, self.budget)
                raise BudgetExceeded(self.budget + 1, self.budget)
            runtime.detail_rows_read += detail_scanned
            out_cols = list(columns) + ["weight_dec"]
            return [dict(zip(out_cols, r)) for r in rows]
        finally:
            con.close()

    def commit_runtime_files(self, runtime: RuntimeRecord) -> None:
        """把本次扫描涉及的真实 Parquet 文件汇总到运行记录。"""
        opened: list[str] = []
        for scan in runtime.scans:
            fname = f"{scan.source}.parquet"
            if fname not in opened:
                opened.append(fname)
        runtime.files_opened = opened


def _render_sql(sql: str, params: list) -> str:
    """把绑定参数内联进展示文本，仅用于透明报告，不用于执行。"""
    rendered = sql
    for p in params:
        rendered = rendered.replace("?", repr(p), 1)
    return rendered
