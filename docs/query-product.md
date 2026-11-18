# 查询产品说明（querymart）

`querymart` 是在 `inputs/` 资料契约之上的预算感知查询入口，用 DuckDB 对真实
Parquet 执行查询。`materials` 仍只做物理校验，两者职责分离。所有命令均以
隔离模式运行（`python -I`），原件目录与代码目录分离，路径经参数传入。

## 命令

```bash
# 受限查询：严格遵守每个请求的 detail_row_budget，结果写 outputs/restricted/
python -I run_querymart.py query
python -I run_querymart.py query --query sum-budget

# 无预算明细核对：全量扫描 deliveries，结果写 outputs/reconcile/（独立缓存）
python -I run_querymart.py reconcile --query distinct-cross-days

# 并排比较汇总复用与明细回退，差异报告写 outputs/comparisons/
python -I run_querymart.py compare --query sum-budget

# 测试
python -I run_tests.py
```

## 选路与口径规则

- **来源选择**：无维度整市单日优先 `city_day`；站维度单日可用 `site_day`；
  其余回 `deliveries`。汇总必须与请求 `map_version` 同版本，旧区划汇总不换标签。
- **覆盖语义**：以 `coverage.json` 为准。声明区间内零行是合法空覆盖（贡献 0）；
  未声明日期不得视为空，只能回合法明细或报 `uncovered_day` 缺口。
- **来源不重叠**：按日切分，每天只归属一个来源；汇总段与明细段 disjoint，
  同一来源行不会被重复计入。每个指标的 `sources` 给出实际使用的来源、日段、
  站点、集市版本与覆盖依据。
- **独立人数**：`person_count` 没有身份集合，只在单日、单站（或整市单日）
  场景可用；任何跨日、跨站合并或 material 切分都回明细做
  `count(DISTINCT person)`，同一人在多站多日仍计 1 人。
- **单价**：`sum(amount)/sum(weight)`，不对行/日均价再平均。零重量是
  `zero_denominator`（无定义）；无数据导致的分母未知与之严格区分，均不返回估计。
- **期末库存**：取请求期间内最后一个同 `map_version` 且状态完整的业务日；
  末日状态缺站则报 `period_end_incomplete`，不拿更早日期顶替，不把缺失当 0，
  也不把多个日期的存量相加。库存不可由投递明细推算。
- **material**：三张汇总表均不支持 material 过滤或分组，出现该条件强制回明细；
  `stock` 无 material 粒度，直接报 `dimension_unsupported`，绝不静默删条件。
- **区域重关联**：明细自带的 `district` 是写入时口径，查询一律按请求的
  `map_version` 经 `site_map` 重关联。

## 明细预算纪律

- 预算只计从 `deliveries.parquet` 物理扫描的行数，取自 DuckDB profile 的
  `TABLE_SCAN` 基数；汇总表与 `site_map` 的读取不计入明细预算，但同样记录。
- 守卫查询带 `LIMIT budget+1`，DuckDB 在行组边界提前停止，**不会先扫全表**。
  超预算立即拒绝，缺口给出至少需要的行数；`detail_row_budget=0` 时不执行任何
  明细 SQL。
- 每个受限请求使用全新的内存 DuckDB 连接，不持有跨请求明细缓存。同一请求内
  多指标共享的明细块只扫一次，预算只扣一次。
- `runtime` 如实记录每条 SQL、每个扫描的来源/文件数/行数、实际打开的 Parquet
  文件与已读明细行数。

## 无预算核对路径的隔离

- `reconcile` 没有 LIMIT 守卫，全量扫描，结果带 `mode=reconcile-only-cache`
  标记，写在独立的 `outputs/reconcile/`。
- 受限入口不会读取该目录；`assert_not_reconcile_cache` 会拒绝把核对缓存当作
  受限执行结果。`compare` 只做只读比对，不回写受限结果。

## 结果状态

- `ok`：所有指标都有完整答案；`partial`：部分指标或部分分组有缺口；
  `refused`：没有任何指标能回答。指标级 `gaps` 给出机器可读代码与中文原因，
  任何缺口都不伴随估计数。
