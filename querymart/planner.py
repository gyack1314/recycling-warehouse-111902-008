"""查询规划与业务裁决。

核心纪律：
- 汇总粒度/口径不支持的请求，回合法明细或报缺口，绝不删条件、不给估计数；
- 明细段与汇总段按日 disjoint，同一来源行不重复计入；
- person_count 不可跨站跨日合并；独立人数只认 person 身份集合；
- 单价=总金额/总重量，未知分母与零分母严格区分；
- 期末库存只取请求期间内最后一个同口径完整业务日状态，不拿更早日期顶替。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .catalog import Catalog, DayCoverage, daterange
from .engine import BudgetExceeded, QueryEngine, RuntimeRecord

METRICS = ("weight", "price", "people", "stock")
DIMENSIONS = ("site", "district", "material")
SUMMARY_TABLES = ("site_day", "city_day", "stock")
# 汇总表均不支持 material 过滤或分组（见 input-contract.md）
SUMMARY_INCAPABLE_DIMS = ("material", "district")


class RequestError(ValueError):
    pass


@dataclass
class Gap:
    code: str
    reason: str
    scope: dict = field(default_factory=dict)
    needed_detail_rows: int | None = None

    def as_dict(self) -> dict:
        out = {"code": self.code, "reason": self.reason, "scope": self.scope}
        if self.needed_detail_rows is not None:
            out["needed_detail_rows"] = self.needed_detail_rows
        return out


@dataclass
class Segment:
    """一个指标实际使用的来源范围。"""
    source: str                       # city_day / site_day / deliveries / stock
    days: list[str]
    sites: list[str]
    map_version: str
    coverage_basis: str
    rows_read: int = 0                # deliveries 段记录真实读取行数

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "days": self.days,
            "sites": self.sites,
            "map_version": self.map_version,
            "coverage_basis": self.coverage_basis,
            "detail_rows_read": self.rows_read,
        }


@dataclass
class MetricAnswer:
    metric: str
    dimensions: list[str]
    rows: list[dict] = field(default_factory=list)   # [{keys, value, unit}]
    gaps: list[Gap] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)

    @property
    def status(self) -> str:
        if not self.rows:
            return "gap"
        return "partial" if self.gaps else "ok"

    def as_dict(self) -> dict:
        versions = sorted({s.map_version for s in self.segments})
        return {
            "metric": self.metric,
            "status": self.status,
            "dimensions": self.dimensions,
            "rows": self.rows,
            "gaps": [g.as_dict() for g in self.gaps],
            "sources": [s.as_dict() for s in self.segments],
            "mart_versions": versions,
        }


# ---------------------------------------------------------------------
# 请求校验与范围
# ---------------------------------------------------------------------
def validate_request(q: dict, catalog: Catalog) -> None:
    for key in ("id", "from", "through", "metrics", "map_version", "detail_row_budget"):
        if key not in q:
            raise RequestError(f"请求缺少字段: {key}")
    if q["map_version"] not in catalog.map_versions():
        raise RequestError(f"未知 map_version: {q['map_version']}")
    unknown_metrics = [m for m in q["metrics"] if m not in METRICS]
    if unknown_metrics:
        raise RequestError(f"不支持的指标: {unknown_metrics}（不会静默忽略）")
    dims = q.get("dimensions", [])
    unknown_dims = [d for d in dims if d not in DIMENSIONS]
    if unknown_dims:
        raise RequestError(f"不支持的维度: {unknown_dims}（不会静默删除查询条件）")
    if len(dims) != len(set(dims)):
        raise RequestError("维度重复")
    daterange(q["from"], q["through"])  # 区间合法性
    if not isinstance(q["detail_row_budget"], int) or q["detail_row_budget"] < 0:
        raise RequestError("detail_row_budget 必须为非负整数")
    for site in q.get("site_filter", []):
        if site not in catalog.sites_for(q["map_version"]):
            raise RequestError(f"站点 {site} 不在映射 {q['map_version']} 中")
    if "material" in dims and q.get("material_filter"):
        raise RequestError("material 分组与 material_filter 不能同时出现")
    if not isinstance(q.get("material_filter", []), list) or any(
            not isinstance(m, str) for m in q.get("material_filter", [])):
        raise RequestError("material_filter 必须是字符串列表")


def requested_sites(q: dict, catalog: Catalog) -> list[str]:
    if q.get("site_filter"):
        return sorted(q["site_filter"])
    return catalog.sites_for(q["map_version"])


# ---------------------------------------------------------------------
# 明细块缓存：同一请求内共享，预算只花一次；失败结论也缓存，不重复扫
# ---------------------------------------------------------------------
class DetailBlockCache:
    """同一请求内共享明细读取：物理行集合相同的块只扫一次，预算只花一次。

    不同指标的日段切分可能不同（如 weight 含汇总段、people 全回明细），
    因此按“日段+站点”归一：新块若被已缓存块覆盖，直接在内存过滤，不再扫 Parquet。
    """

    def __init__(self, engine: QueryEngine, runtime: RuntimeRecord):
        self._engine = engine
        self._runtime = runtime
        self._rows: dict[tuple, list[dict]] = {}
        self._failures: dict[tuple, BudgetExceeded] = {}

    def read(self, days: list[str], sites: list[str],
             materials: list[str] | None = None) -> list[dict]:
        key = (tuple(days), tuple(sites), tuple(materials or ()))
        if key in self._rows:
            return self._rows[key]
        if key in self._failures:
            raise self._failures[key]
        # 复用覆盖本块的已缓存行（内存过滤，零 Parquet IO、零预算消耗）
        for (c_days, c_sites, c_mats), cached_rows in self._rows.items():
            if tuple(sites) == c_sites and tuple(materials or ()) == c_mats \
                    and set(days) <= set(c_days):
                wanted = set(days)
                subset = [r for r in cached_rows if r["day"] in wanted]
                self._rows[key] = subset
                return subset
        remaining = self._engine.budget - self._runtime.detail_rows_read
        if remaining <= 0:
            exc = BudgetExceeded(self._runtime.detail_rows_read + 1, self._engine.budget)
            self._failures[key] = exc
            raise exc
        rows = self._engine.guarded_detail_read(days, sites, self._runtime,
                                                materials=materials)
        if rows is None:  # 预算为 0：引擎不执行任何 SQL
            exc = BudgetExceeded(1, 0)
            self._failures[key] = exc
            raise exc
        self._rows[key] = rows
        return rows

    def all_rows(self) -> list[list[dict]]:
        return list(self._rows.values())


# ---------------------------------------------------------------------
# 纯内存聚合：DuckDB 只负责真实 Parquet 读取，聚合不产生第二次明细 IO
# ---------------------------------------------------------------------
def _group_key(row: dict, dims: list[str], map_version: str, catalog: Catalog) -> tuple:
    keys = []
    for d in dims:
        if d == "site":
            keys.append(row["site"])
        elif d == "material":
            keys.append(row["material"])
        else:  # 明细自带的 district 列是旧口径标签，必须按所选映射重关联
            keys.append(catalog.district_of(map_version, row["site"]))
    return tuple(keys)


def _aggregate_amounts(rows: list[dict], dims: list[str], map_version: str,
                       catalog: Catalog) -> dict[tuple, tuple[Decimal, int]]:
    acc: dict[tuple, list] = {}
    for r in rows:
        key = _group_key(r, dims, map_version, catalog)
        w, a = acc.setdefault(key, [Decimal("0"), 0])
        acc[key] = [w + r["weight_dec"], a + int(r["amount_minor"])]
    return {k: (v[0], v[1]) for k, v in acc.items()}


def _aggregate_people(rows: list[dict], dims: list[str], map_version: str,
                      catalog: Catalog) -> dict[tuple, set[str]]:
    acc: dict[tuple, set[str]] = {}
    for r in rows:
        key = _group_key(r, dims, map_version, catalog)
        acc.setdefault(key, set()).add(r["person"])
    return acc


def _key_dict(dims: list[str], key: tuple) -> dict:
    return dict(zip(dims, key)) if dims else {}


def _all_group_keys(q: dict, catalog: Catalog, sites: list[str],
                    materials: list[str]) -> list[tuple]:
    dims = q.get("dimensions", [])
    if not dims:
        return [()]
    base: list[tuple]
    if dims == ["site"]:
        base = [(s,) for s in sites]
    elif dims == ["district"]:
        districts = sorted({catalog.district_of(q["map_version"], s) for s in sites})
        base = [(d,) for d in districts]
    elif dims == ["material"]:
        base = [(m,) for m in materials]
    else:
        # site + district（其余组合按需扩展）
        base = [(s, catalog.district_of(q["map_version"], s)) for s in sites]
    return base


# ---------------------------------------------------------------------
# 来源规划：按日裁决，合并连续段，保证 disjoint
# ---------------------------------------------------------------------
@dataclass
class DayPlan:
    day: str
    source: str                 # city_day / site_day / deliveries / gap
    coverage: DayCoverage | None = None
    reason: str = ""


def _plan_amount_days(q: dict, catalog: Catalog, days: list[str],
                      sites: list[str]) -> list[DayPlan]:
    dims = q.get("dimensions", [])
    mv = q["map_version"]
    # material 过滤/分组：site_day/city_day 均不支持，只能回明细
    material_required = "material" in dims or bool(q.get("material_filter"))
    whole_city = not q.get("site_filter")
    plans: list[DayPlan] = []
    for day in days:
        # 1) 市日汇总：仅无维度、整市范围、同版本、无 material 条件
        if not dims and whole_city and not material_required:
            cov = catalog.city_day_coverage(day)
            if cov is not None and cov.map_version == mv:
                plans.append(DayPlan(day, "city_day", cov))
                continue
        # 2) 站日汇总：不支持 district/material 维度；需覆盖全部请求站点且同版本
        if not any(d in SUMMARY_INCAPABLE_DIMS for d in dims) and not material_required:
            cov = catalog.site_day_coverage(day)
            if cov is not None and cov.map_version == mv:
                if cov.is_empty or all(s in cov.sites for s in sites):
                    plans.append(DayPlan(day, "site_day", cov))
                    continue
        # 3) 合法明细
        if catalog.deliveries_covers(day):
            plans.append(DayPlan(day, "deliveries"))
            continue
        # 4) 缺口：未声明且无明细，禁止当成空覆盖
        plans.append(DayPlan(day, "gap", reason="该日无同口径汇总声明，且不在完整明细覆盖区间内"))
    return plans


def _merge_blocks(plans: list[DayPlan], source: str) -> list[list[str]]:
    """把同一来源的服务日按日历连续性切成互不重叠的日段。"""
    picked = sorted(p.day for p in plans if p.source == source)
    if not picked:
        return []
    all_days = daterange(picked[0], picked[-1])
    blocks: list[list[str]] = []
    current: list[str] = []
    for day in all_days:
        if day in picked:
            current.append(day)
        else:
            if current:
                blocks.append(current)
                current = []
    if current:
        blocks.append(current)
    return blocks


# ---------------------------------------------------------------------
# 指标规划：weight / price
# ---------------------------------------------------------------------
def plan_amounts(q: dict, catalog: Catalog, engine: QueryEngine,
                 runtime: RuntimeRecord, cache: DetailBlockCache) -> dict[str, MetricAnswer]:
    days = daterange(q["from"], q["through"])
    sites = requested_sites(q, catalog)
    dims = q.get("dimensions", [])
    mv = q["map_version"]
    materials = list(q.get("material_filter") or [])
    wanted = [m for m in ("weight", "price") if m in q["metrics"]]
    answers = {m: MetricAnswer(m, dims) for m in wanted}
    day_plans = _plan_amount_days(q, catalog, days, sites)

    #  disjoint 断言：每天只归属一个来源
    served = [p.day for p in day_plans if p.source != "gap"]
    if len(served) != len(set(served)):
        raise RuntimeError("来源段在日期上重叠")

    gap_days = [p.day for p in day_plans if p.source == "gap"]
    # 汇总累加器
    sums: dict[tuple, list] = {}

    def add(key: tuple, weight: Decimal, amount: int) -> None:
        w, a = sums.setdefault(key, [Decimal("0"), 0])
        sums[key] = [w + weight, a + amount]

    # ---- 市日汇总段 ----
    for block in _merge_blocks(day_plans, "city_day"):
        rows = engine.read_summary(
            "city_day",
            ["day", "CAST(weight_sum AS DECIMAL(18,3)) AS wd", "amount_sum"],
            "day BETWEEN ? AND ? AND map_version = ?",
            [block[0], block[-1], mv], runtime,
        )
        declared = {p.day: p.coverage for p in day_plans if p.source == "city_day"}
        returned_days = {r["day"] for r in rows}
        for day in block:
            if day not in returned_days:
                if declared[day].is_empty:
                    continue  # 合法空覆盖：零贡献
                raise RuntimeError(f"声明覆盖但读取缺行: {day}")
        for r in rows:
            add((), r["wd"], int(r["amount_sum"]))
        seg = Segment("city_day", block, sites, mv,
                      f"coverage.json city_day {block[0]}..{block[-1]} map={mv}（整市口径）")
        for a in answers.values():
            a.segments.append(seg)

    # ---- 站日汇总段 ----
    for block in _merge_blocks(day_plans, "site_day"):
        ph = ", ".join("?" for _ in sites)
        rows = engine.read_summary(
            "site_day",
            ["day", "site", "CAST(weight_sum AS DECIMAL(18,3)) AS wd", "amount_sum"],
            f"day BETWEEN ? AND ? AND map_version = ? AND site IN ({ph})",
            [block[0], block[-1], mv, *sites], runtime,
        )
        declared = {p.day: p.coverage for p in day_plans if p.source == "site_day"}
        for day in block:
            cov = declared[day]
            if cov.is_empty:
                continue
            missing = [s for s in sites if not any(
                r["day"] == day and r["site"] == s for r in rows)]
            if missing:
                raise RuntimeError(f"声明覆盖但读取缺站: {day} {missing}")
        for r in rows:
            key = (r["site"],) if dims == ["site"] else ()
            add(key, r["wd"], int(r["amount_sum"]))
        seg = Segment("site_day", block, sites, mv,
                      f"coverage.json site_day {block[0]}..{block[-1]} map={mv} sites={sites}")
        for a in answers.values():
            a.segments.append(seg)

    # ---- 明细段 ----
    detail_failure: BudgetExceeded | None = None
    for block in _merge_blocks(day_plans, "deliveries"):
        try:
            raw = cache.read(block, sites, materials or None)
        except BudgetExceeded as exc:
            detail_failure = exc
            break
        for key, (w, a) in _aggregate_amounts(raw, dims, mv, catalog).items():
            add(key, w, a)
        seg = Segment("deliveries", block, sites, mv,
                      f"coverage.json deliveries complete {catalog.deliveries_from}.."
                      f"{catalog.deliveries_through}；按 map={mv} 经 site_map 重关联"
                      + (f"；material IN {materials}" if materials else ""),
                      rows_read=len(raw))
        for a in answers.values():
            a.segments.append(seg)

    # material 分组的分组全集：有显式过滤取过滤值，否则取明细中实际出现的材料
    if "material" in dims and not materials:
        material_universe = sorted({
            r["material"]
            for block_rows in cache.all_rows()
            for r in block_rows
        })
    else:
        material_universe = materials
    expected_keys = _all_group_keys(q, catalog, sites, material_universe)
    # 全段都有合法覆盖（汇总声明或完整明细）时，零行分组是合法空覆盖，
    # 重量补 0；存在未声明日时不补，避免把未知当空。
    fully_covered = not gap_days and detail_failure is None

    # ---- 组装 weight ----
    if "weight" in answers:
        ans = answers["weight"]
        if detail_failure is not None:
            ans.gaps.append(Gap(
                "detail_budget_exceeded",
                f"汇总不支持该范围，回退明细需读取超过预算的行数（守卫已提前停止，未预读全表）: {detail_failure}",
                needed_detail_rows=detail_failure.required_at_least,
            ))
        for day in gap_days:
            ans.gaps.append(Gap("uncovered_day", f"{day} 无同口径覆盖，未声明日期不得视为空",
                                {"day": day}))
        for key in expected_keys:
            if key in sums:
                ans.rows.append({"keys": _key_dict(dims, key),
                                 "value": str(sums[key][0]), "unit": "tonne"})
            elif fully_covered:
                ans.rows.append({"keys": _key_dict(dims, key),
                                 "value": "0.000", "unit": "tonne"})

    # ---- 组装 price：总金额/总重量，区分未知分母与零分母 ----
    if "price" in answers:
        ans = answers["price"]
        if detail_failure is not None:
            ans.gaps.append(Gap(
                "detail_budget_exceeded",
                f"单价所需明细超出预算，拒绝估计: {detail_failure}",
                needed_detail_rows=detail_failure.required_at_least,
            ))
        for day in gap_days:
            ans.gaps.append(Gap("uncovered_day", f"{day} 无同口径覆盖，分母未知",
                                {"day": day}))
        for key in expected_keys:
            if key not in sums:
                continue
            weight, amount = sums[key]
            if weight == 0:
                ans.gaps.append(Gap(
                    "zero_denominator",
                    "总重量为零，单价无定义（零分母；与未知分母不同，不返回估计数）",
                    _key_dict(dims, key),
                ))
            else:
                price = (Decimal(amount) / weight).quantize(Decimal("0.001"))
                ans.rows.append({"keys": _key_dict(dims, key),
                                 "value": str(price), "unit": "CNY_minor_per_tonne"})
    return answers


# ---------------------------------------------------------------------
# 指标规划：people（独立人数）
# ---------------------------------------------------------------------
def plan_people(q: dict, catalog: Catalog, engine: QueryEngine,
                runtime: RuntimeRecord, cache: DetailBlockCache) -> MetricAnswer:
    days = daterange(q["from"], q["through"])
    sites = requested_sites(q, catalog)
    dims = q.get("dimensions", [])
    mv = q["map_version"]
    materials = list(q.get("material_filter") or [])
    material_required = "material" in dims or bool(materials)
    ans = MetricAnswer("people", dims)
    single_day = len(days) == 1

    # 汇总 person_count 仅在“单日 + 无 district/material 条件”时可用；它没有身份集合，
    # 任何跨日、跨站合并或 material 切分都必须回明细。
    summary_usable = single_day and "district" not in dims and not material_required
    if summary_usable:
        day = days[0]
        if not dims and not q.get("site_filter"):
            cov = catalog.city_day_coverage(day)
            if cov is not None and cov.map_version == mv:
                rows = engine.read_summary(
                    "city_day", ["person_count"],
                    "day = ? AND map_version = ?", [day, mv], runtime)
                if rows:  # 声明空覆盖时无行：合法的零人
                    ans.rows.append({"keys": {}, "value": int(rows[0]["person_count"]),
                                     "unit": "distinct_person"})
                else:
                    ans.rows.append({"keys": {}, "value": 0, "unit": "distinct_person"})
                ans.segments.append(Segment(
                    "city_day", [day], sites, mv,
                    f"coverage.json city_day day={day} map={mv}；单日整市 person_count"))
                return ans
        cov = catalog.site_day_coverage(day)
        if cov is not None and cov.map_version == mv and (
                cov.is_empty or all(s in cov.sites for s in sites)):
            if not cov.is_empty:
                ph = ", ".join("?" for _ in sites)
                rows = engine.read_summary(
                    "site_day", ["site", "person_count"],
                    f"day = ? AND map_version = ? AND site IN ({ph})",
                    [day, mv, *sites], runtime)
                got = {r["site"]: int(r["person_count"]) for r in rows}
                for s in sites:
                    if s not in got:
                        raise RuntimeError(f"声明覆盖但读取缺站: {day} {s}")
                if dims == ["site"]:
                    for s in sites:
                        ans.rows.append({"keys": {"site": s}, "value": got[s],
                                         "unit": "distinct_person"})
                else:
                    # 无维度但带 site_filter 的单日请求：站日 person_count 无法跨站去重，
                    # 没有身份集合，不能相加 -> 必须回明细。
                    pass
                if ans.rows:
                    ans.segments.append(Segment(
                        "site_day", [day], sites, mv,
                        f"coverage.json site_day day={day} map={mv}；单站单日 person_count"))
                    return ans

    # 回明细：独立身份集合
    if not catalog.deliveries_range_covers(days):
        missing = [d for d in days if not catalog.deliveries_covers(d)]
        ans.gaps.append(Gap(
            "identity_unavailable",
            f"汇总无居民身份集合，无法合并 person_count；明细又未完整覆盖 {missing}",
            {"days": missing}))
        return ans
    try:
        raw = cache.read(days, sites, materials or None)
    except BudgetExceeded as exc:
        ans.gaps.append(Gap(
            "detail_budget_exceeded",
            f"跨站跨日独立人数需要 person 身份集合，明细预算不足: {exc}",
            needed_detail_rows=exc.required_at_least))
        return ans
    people_sets = _aggregate_people(raw, dims, mv, catalog)
    if "material" in dims:
        material_universe = sorted({r["material"] for r in raw})
    else:
        material_universe = materials
    for key in _all_group_keys(q, catalog, sites, material_universe):
        if key in people_sets:
            ans.rows.append({"keys": _key_dict(dims, key),
                             "value": len(people_sets[key]), "unit": "distinct_person"})
        else:
            # 完整明细覆盖下的零行分组 = 合法空覆盖，独立人数为 0
            ans.rows.append({"keys": _key_dict(dims, key),
                             "value": 0, "unit": "distinct_person"})
    ans.segments.append(Segment(
        "deliveries", days, sites, mv,
        f"coverage.json deliveries complete {catalog.deliveries_from}.."
        f"{catalog.deliveries_through}；count(DISTINCT person)，按 map={mv} 重关联区域",
        rows_read=len(raw)))
    return ans


# ---------------------------------------------------------------------
# 指标规划：stock（期末库存状态，不可加总，不可回明细推算）
# ---------------------------------------------------------------------
def plan_stock(q: dict, catalog: Catalog, engine: QueryEngine,
               runtime: RuntimeRecord) -> MetricAnswer:
    days = daterange(q["from"], q["through"])
    sites = requested_sites(q, catalog)
    dims = q.get("dimensions", [])
    mv = q["map_version"]
    ans = MetricAnswer("stock", dims)

    # 库存是站日状态，没有 material 粒度：不支持就明确拒绝，绝不删条件
    if "material" in dims or q.get("material_filter"):
        ans.gaps.append(Gap(
            "dimension_unsupported",
            "stock 是站日状态表，不支持 material 过滤或分组；不能静默忽略该条件",
            {"material_filter": q.get("material_filter", [])}))
        return ans

    # 窗口内同口径的库存声明日（升序）
    candidates = [(d, catalog.stock_coverage(d)) for d in days]
    candidates = [(d, c) for d, c in candidates if c is not None and c.map_version == mv]
    if not candidates:
        ans.gaps.append(Gap(
            "no_same_caliber_stock",
            f"请求期间内没有 map={mv} 的库存状态，库存不能由投递明细推算，也不能套用其他版本",
            {"days": days}))
        return ans

    # 每个分组需要的站点集合
    group_sites: dict[tuple, list[str]] = {}
    for key in _all_group_keys(q, catalog, sites, []):
        if not dims:
            group_sites[key] = sites
        elif dims == ["site"]:
            group_sites[key] = [key[0]]
        elif dims == ["district"]:
            group_sites[key] = [s for s in sites
                                if catalog.district_of(mv, s) == key[0]]
        else:
            group_sites[key] = [key[0]]

    latest_day, latest_cov = candidates[-1]
    for key, need_sites in group_sites.items():
        # 期末=期间内最后一个同口径业务日；该日状态不完整就报缺口，
        # 不允许回退到更早一天假装期末。
        missing_at_latest = [s for s in need_sites if s not in latest_cov.sites]
        if missing_at_latest:
            ans.gaps.append(Gap(
                "period_end_incomplete",
                f"期末日 {latest_day}(map={mv}) 缺少站点 {missing_at_latest} 的同口径状态；"
                "不能用更早日期顶替，也不能把缺失当库存 0",
                _key_dict(dims, key)))
            continue
        if len(need_sites) > 1:
            ans.gaps.append(Gap(
                "stock_not_additive",
                "库存是站点状态，不支持跨站点相加", _key_dict(dims, key)))
            continue
        site = need_sites[0]
        rows = engine.read_summary(
            "stock", ["closing"],
            "day = ? AND map_version = ? AND site = ?",
            [latest_day, mv, site], runtime)
        if len(rows) != 1:
            raise RuntimeError(f"库存声明与数据不一致: {latest_day} {site}")
        ans.rows.append({"keys": _key_dict(dims, key),
                         "value": str(rows[0]["closing"]),
                         "unit": "tonne_closing",
                         "as_of_day": latest_day})
    ans.segments.append(Segment(
        "stock", [latest_day], sorted({s for ss in group_sites.values() for s in ss}), mv,
        f"coverage.json stock day={latest_day} map={mv}；请求期间最后一个同口径完整业务日"))
    return ans
