# 多粒度回收集市的聚合查询选路

本项目提供合成回收业务原件及物理检查入口。当前没有入仓、仓库发布或产品查询能力；检查通过只表示输入能读取，不表示动作已经处理。

使用 Python 3.11，依赖版本固定在 requirements.txt。安装命令为 python -m pip install -r requirements.txt。运行 python -m materials inputs 校验清单、摘要、Parquet行数并实际使用DuckDB读取；运行 python -m unittest discover -s tests -v 检查独立JSON值、原件损坏及业务材料形状。

可执行 docker build -t recycling-inputs .，再执行 docker run --rm recycling-inputs；测试命令为 docker run --rm recycling-inputs python -m unittest discover -s tests -v。环境不依赖外部业务站点。资料均为本地合成，不含真实个人、企业数据或凭据。

inputs/seed.json与同名Parquet是相同输入的两种载体；inputs/actions.json提供尚未应用的后续资料。docs/input-contract.md解释字段及原有业务约定。materials.physical只校验物理文件，不裁决身份、金额、匹配或有效版本。materials.build_inputs仅重建输入Parquet和物理清单，通常无需运行；不要把其产物误当未来业务结果。

查询产品位于querymart包，入口与规则见docs/query-product.md。受限查询执行python -I run_querymart.py query（结果写outputs/restricted/）；无预算明细核对执行python -I run_querymart.py reconcile --query <id>（独立缓存写outputs/reconcile/，受限请求禁用）；compare并排比较汇总复用与明细回退。测试执行python -I run_tests.py。选路在覆盖声明内优先复用同map_version汇总，不满足口径或预算时回合法明细或报缺口，绝不返回估计数；运行时记录真实打开的Parquet与扫描行数，明细预算按DuckDB物理扫描行数执行。

产品输出使用独立目录；原始资料保留供回查。实际入仓及针对业务的检查方式仍需在后续实现，已有输入检查不替代产品验收。
