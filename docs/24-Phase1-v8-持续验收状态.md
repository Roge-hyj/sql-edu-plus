# Phase 1 v8 持续验收状态

更新时间：2026-08-22（Asia/Shanghai）。本报告只引用公开 train/public 统计和 hidden 冻结摘要；hidden SQL、学生 SQL 和失败家族明文不会写入报告。

## 当前快照

- corpus snapshot：`992244c60df695bb77240e8f4ae9e776dbdf69b4da6219054d136ea62e0e6424`
- 唯一题目家族：58,330；train/public/hidden：46,675 / 5,809 / 5,846。
- v8 分割泄漏审计：`pass=true`；family、lineage、mutation lineage、raw record、schema 等硬重叠为 0。少量 normalized SQL/template overlap 仅报告，不作为硬泄漏。
- 能力矩阵 12 个核心类别均达到每类 300 家族目标；v15 的公开 observed scenario axes 也全部达到 30，详见统计 JSON。
- 当前公开证据链仍为 v15；在修复冻结配对的逐记录 schema 作用域后，另完成 v16 hidden 双次无反馈冻结。v11–v15 结果保留为可复现历史基线；v16 不回写公开指标，也不作为同一冻结上的优化输入。

## 独立 Gold Oracle（v11 历史基线）

v11 的 `gold_oracle_audit_stratified_10seed_v11_eq4000.jsonl` 共 8,800 个去重家族对，seed 为 0–9，row scale 为 4/8/16，单表最多 32 行：

- NOT_EQUIVALENT：3,559/3,559；Wilson 95% 下界 99.8922%。
- EQUIVALENT：3,962/3,962；Wilson 95% 下界 99.9031%。
- UNDECIDED：760，ENGINE_GAP：519；二者均未计入正确率。
- 原子 obligation 覆盖率和结构绑定率均为 100%；确定性标签错配为 0。

## 生产链抽样（v11 历史基线）

v11 按类别抽取 50 个目标（共 435 家族）：311 PASS、124 EXCLUDED、0 FAIL。对 232 个可纳入 NOT_EQUIVALENT 链路分母的家族，validator activation、execution difference、targeted repair、attribution 和 full chain 均为 232/232，Wilson 95% 下界 98.3712%；这是分层抽样，不替代每类别 300 家族的最终门禁。

## Hidden 冻结（v11 历史基线）

v11 的 `final_hidden_freeze_verification_v11.json` 对 5,846 个 hidden 家族、11,575 个 paired rows 做了两次独立、无反馈执行（10 seed、4/8/16 scale）：

- 5,736 家族进入当前声明支持范围（可生成 mutation + equivalence control），覆盖率 98.1184%。另有 5,839 家族生成 mutation，5,736 家族生成 equivalence control。
- 110 个生成缺口（103 个 equivalence、7 个 parse）只保存 digest；冻结完成后没有根据 hidden 结果修改实现。
- EQUIVALENT 5,601、NOT_EQUIVALENT 4,991、UNDECIDED 626、ENGINE_GAP 357；两次结果完全稳定，确定性标签错配为 0。
- `acceptance.pass=false`，唯一未通过项是 hidden generation scope 尚未 100%；因此当前不能宣称“全 hidden 冻结通过”。

冻结摘要按轴保存于 JSON：

| 轴 | 主要结果 |
| --- | --- |
| 方言 | generic：5,558 EQ / 4,952 NEQ / 621 UNDECIDED / 262 ENGINE_GAP；SQLite：43 / 39 / 5 / 13；MySQL、PostgreSQL、T-SQL 目前均为 ENGINE_GAP 主导 |
| 类别 | select_projection 与 where_logic_null 各 5,243 EQ；group_having_aggregate 1,751 EQ、1,321 NEQ；其余 9 类均逐类保留计数 |
| schema 规模 | small：4,537 EQ / 4,108 NEQ；medium：501 / 440；multi-table：563 / 443；每类均区分 ENGINE_GAP 与 UNDECIDED |
| 稳定性 | 两次 `rows=11,575`、verdict 分布与 digest 完全一致 |

完整的类别/方言/schema 交叉计数仍以冻结 JSON 为准；下面给出本次首轮的可读摘要（列顺序：总行数 / EQUIVALENT / NOT_EQUIVALENT / UNDECIDED / ENGINE_GAP）：

| 类别 | 计数 |
| --- | ---: |
| select_projection | 10,851 / 5,243 / 4,715 / 553 / 340 |
| where_logic_null | 10,816 / 5,243 / 4,668 / 591 / 314 |
| group_having_aggregate | 3,621 / 1,751 / 1,321 / 433 / 116 |
| distinct_order_limit | 610 / 283 / 244 / 39 / 44 |
| join_outer_on | 492 / 213 / 147 / 69 / 63 |
| subqueries_correlation | 296 / 123 / 78 / 27 / 68 |
| dialect_features | 264 / 84 / 75 / 10 / 95 |
| window_functions | 242 / 116 / 91 / 25 / 10 |
| in_between_like | 187 / 79 / 64 / 21 / 23 |
| set_operations | 194 / 88 / 59 / 29 / 18 |
| cte_recursive | 133 / 50 / 32 / 16 / 35 |
| case | 90 / 43 / 35 / 8 / 4 |

| 方言 | 计数 |
| --- | ---: |
| generic | 11,393 / 5,558 / 4,952 / 621 / 262 |
| sqlite | 100 / 43 / 39 / 5 / 13 |
| mysql | 56 / 0 / 0 / 0 / 56 |
| postgres | 20 / 0 / 0 / 0 / 20 |
| tsql | 6 / 0 / 0 / 0 / 6 |

| schema 规模 | 计数 |
| --- | ---: |
| small_1_table_1_8_cols | 9,286 / 4,537 / 4,108 / 442 / 199 |
| multi_table | 1,246 / 563 / 443 / 120 / 120 |
| medium_1_table_9_32_cols | 1,043 / 501 / 440 / 64 / 38 |

边界条件固定为：独立 seed `0..9`、row scale `4/8/16`、单表最多 32 行；公开能力矩阵还显式记录 `NULL`、空结果、重复值、多表、边界值和 schema constraint 轴。公开观察轴的不足不被 candidate 计数掩盖，缺口已在上节列出。

## 本轮公开修复

- mutation layer v11 增加 schema-aware 数字开头标识符、保留字列名和抓取文本前导说明的有界重解析，并保留 `COUNT(*)`→`COUNT(column)`、`COUNT(DISTINCT x)`→`COUNT(x)`、冗余真谓词等可解释控制；train/public 52,447 家族生成 104,894 行，parse 失败 34、无适用 operator 3，mutation/equivalence 覆盖率均为 99.9295%。所有 15 个必需 operator family 均有覆盖。公开 gap 审计把 34 个 parse gap 明确分类为 INPUT_GAP_PROSE 20、INPUT_GAP_MULTI_STATEMENT 10、INPUT_GAP_TEMPLATE 2、ENGINE_GAP_SYNTAX 2，未留下未分类 parse error。
- v11 能力矩阵有 52,447 个唯一家族、12 个核心类别均达到 300 家族；观察轴仍有 6 个短缺：case 的 dialect_feature/empty_result（29/30）、cte_recursive 的 dialect_feature（0/30）、set_operations 的 dialect_feature（23/30）、subqueries_correlation 的 dialect_feature（26/30）、window_functions 的 dialect_feature/duplicate_candidate（22/30、29/30）。v11 layer 与既有公开 Gold observed evidence 按 `(family_id, mutation_layer_role)` 严格键合；merge 只复制 observed axes，未读取 hidden，也未把 candidate axes 当作执行证据。
- 生产链重算时区分“通用 SQL 的稳定 AST”与“执行时默认方言”，避免默认 MySQL 解析造成结构绑定假失败。
- join validator 对数值字符串/数值型键使用有界 canonical key，修复 SQLite 执行与 Python 集合键类型不一致造成的假失败；新增保留字、抓取前导说明、TOP 方言回退及安全边界测试。
- Gold native URL 仅在端口可达时启用，陈旧 `.env` 不再伪装成可用引擎。
- 窗口计数型 AST diff 保留 OVER 元数据；窗口/CASE 证据可正确绑定。
- `IS NULL`/`IS NOT NULL` 使用专用 predicate-path obligation；AND↔OR 在最终 materialization 阶段保留完整四格 truth table。
- mutation binding 对单一 NULL predicate diff 做精确 ASTDiff 绑定。
- 统计报告把生产 `EXCLUDED` 状态从 correctness 分母排除，并保留排除计数。

## v12 公开 native 扩展（历史记录）

- 在同一固定 train/public 快照上启动了仅用于验证的临时 MySQL 8.0.46（`127.0.0.1:13306`，数据目录在 `/tmp`）；没有改动项目数据库，也没有读取 hidden。
- v12 Gold（8,800 对、10 seed、4/8/16、单表最多 32 行）得到 EQUIVALENT 3,962、NOT_EQUIVALENT 3,614、UNDECIDED 783、ENGINE_GAP 441；确定标签全部匹配，Wilson 95% 下界分别为 EQ 99.9031%、NEQ 99.8938%。MySQL 分层中 55 个 NEQ、23 个 UNDECIDED、70 个 ENGINE_GAP；PostgreSQL/T-SQL 仍全部保留 ENGINE_GAP，未伪装为 SQLite。
- v12 生产链为 313 PASS、122 EXCLUDED、0 FAIL；234 个可计入 NOT_EQUIVALENT 链路的家族在 validator activation、execution difference、targeted repair、attribution、full chain 上均为 234/234，下界 98.3849%。
- v12 能力矩阵仍为 52,447 个唯一家族，公开观察轴缺口与 v11 相同（case 2 项、cte_recursive 方言轴 30 项、set_operations 7 项、subqueries 4 项、window 2 项）；这部分仍是声明边界，不以 candidate 轴充数。
- v12 hidden 已在公开证据稳定后单独、无反馈运行；其结果保留在下方作为历史基线。v13 公开修复与版本元数据冻结完成后，以 v13 工件作为当前证据链。

### v12 hidden 冻结结果

`final_hidden_freeze_verification_v12_mysql.json` 已按上述规则完成双次运行：5,846 个 hidden 家族、11,575 paired rows，5,736 家族进入当前支持范围，覆盖率 98.1184%；EQUIVALENT 5,617、NOT_EQUIVALENT 5,006、UNDECIDED 627、ENGINE_GAP 325。两次 verdict/rows/digest 完全稳定，确定性标签错配为 0；`acceptance.pass=false` 仍只因为 110 个生成缺口（103 equivalence、7 parse），没有把缺口计为正确。该次命令使用临时 MySQL 8.0.46；冻结 JSON 的 `configured_native_engine_versions` 仍为空，这是下一轮需要补齐的可复现性元数据缺口。

## v13 当前公开证据与冻结

- v13 修复了 SQLite 方言渲染递归 CTE 时丢失命名列（例如 `nums(n)`）的问题：当方言渲染破坏 `TableAlias.columns` 时，渲染器有界回退到通用 SQL；新增回归测试覆盖该路径。该修复只改变公开 mutation layer 的可执行渲染，不读取 hidden 失败。
- v13 mutation layer 仍覆盖 train/public 的 52,447 个唯一家族、104,894 行；mutation/equivalence 覆盖率 99.9295%，parse gap 34、无适用 operator 3，15 个 operator family 均有覆盖。gap 审计仍将 34 个缺口明确分为 INPUT_GAP_PROSE 20、INPUT_GAP_MULTI_STATEMENT 10、INPUT_GAP_TEMPLATE 2、ENGINE_GAP_SYNTAX 2，且 `hidden_partition_read=false`、`contains_sql=false`。
- v13 Gold native（MySQL 8.0.46，8,800 对、10 seed、4/8/16、单表最多 32 行）得到 EQUIVALENT 3,962、NOT_EQUIVALENT 3,637、UNDECIDED 784、ENGINE_GAP 417；UNDECIDED/ENGINE_GAP 未进入正确率分母。确定性标签全部匹配；EQUIVALENT Wilson 95% 下界 99.9031%，NOT_EQUIVALENT 下界 99.8945%。
- v13 生产链按类别抽取 50 个目标（435 家族）：319 PASS、116 EXCLUDED、0 FAIL；240 个可计入 NOT_EQUIVALENT 链路的家族在 validator activation、execution difference、targeted repair、attribution、full chain 上均为 240/240，Wilson 95% 下界 98.4246%。
- v13 能力矩阵仍有 52,447 个唯一开发家族，12 个核心类别均达到 ≥300；公开 observed 证据合并了 v12 与 v13 Gold，严格键合且不读取 hidden。唯一未达 30 的 scenario axis 是 `cte_recursive/dialect_feature`（29/30），明确保留为能力边界。

### v13 hidden 冻结结果

`final_hidden_freeze_verification_v13_mysql.json` 在公开工件固定后运行两次完整 hidden 验证：5,846 个 hidden 家族、11,575 paired rows，5,736 家族进入当前支持范围，覆盖率 98.1184%；EQUIVALENT 5,620、NOT_EQUIVALENT 5,009、UNDECIDED 627、ENGINE_GAP 319。两次 verdict/rows/digest 完全稳定，确定性标签错配为 0；`acceptance.pass=false` 仅因为 110 个 generation gap（103 equivalence、7 parse），没有把缺口计为正确。冻结元数据记录 `PARSEVAL_MYSQL_VERSION=8.0.46-0ubuntu0.24.04.3`；hidden 失败只保存 digest，不能作为后续优化输入，本报告不会把 generation gap 或 ENGINE_GAP 计入等价正确率。

## v14 当前公开证据与冻结

- v14 增加了可解释的 `COUNT(column) → COUNT(*)` 攻击性 mutation 及 `COUNT(column) → COUNT(CASE WHEN NOT column IS NULL THEN 1 END)` 等价控制；同时补齐带引号列名的 aggregate NULL obligation validator。回归测试覆盖 NULL 与无 NULL 两条路径，生产链不再出现该 mutation 的 validator 误报。
- v14 mutation layer 读取 52,484 个 train/public 家族，为 52,450 个家族各生成一条 mutation 和一条 equivalence control，共 104,900 行；mutation/equivalence 覆盖率均为 99.935218%，15/15 个必需 operator family 均有覆盖，`no_applicable_operator=0`，parse gap 34。gap 审计仍为 INPUT_GAP_PROSE 20、INPUT_GAP_MULTI_STATEMENT 10、INPUT_GAP_TEMPLATE 2、ENGINE_GAP_SYNTAX 2，且 `hidden_partition_read=false`、`contains_sql=false`。
- v14 observed merge 以 v14 layer 为源，合并 v12/v13/v14 公开 Gold 与 top-up 证据；104,900 个 layer row 中 104,894 个有公开 observed audit 键，新增的 6 行只保留其可复现 layer 记录，不用 candidate 轴冒充执行证据。能力矩阵为 52,450 个唯一开发家族，12 个核心类别均达到 ≥300，所有场景轴均达到 30（包括 `cte_recursive/dialect_feature=30/30`）。
- v14 Gold native 使用 MySQL 8.0.46、8,800 对、10 seed、row scale 4/8/16、单表最多 32 行：EQUIVALENT 3,962、NOT_EQUIVALENT 3,672、UNDECIDED 745、ENGINE_GAP 421；原子 obligation 覆盖率和结构绑定率均为 100%，确定性标签匹配 7,634/7,634。Wilson 95% 下界：NOT_EQUIVALENT 99.8955%，等价误报率上界约 0.0969%。UNDECIDED/ENGINE_GAP 不进入正确率分母。
- v14 生产链按类别抽取 50 个目标：320 PASS、115 EXCLUDED、0 FAIL；其中 241 个可计入 NOT_EQUIVALENT 链路的家族在 validator activation、execution difference、targeted repair、attribution、full chain 上均为 241/241，Wilson 95% 下界 98.4310%。

### v14 hidden 冻结结果

`final_hidden_freeze_verification_v14_mysql.json` 在 v14 公开工件固定后完成两次独立、无反馈 hidden 验证：5,846 个 hidden 家族、11,575 paired rows，5,736 家族进入声明支持范围，覆盖率 98.1184%；EQUIVALENT 5,604、NOT_EQUIVALENT 4,940、UNDECIDED 680、ENGINE_GAP 351。两次 verdict/rows/digest 完全稳定，确定性标签错配为 0；`acceptance.pass=false` 仅因为 110 个 generation gap（103 equivalence、7 parse），没有把缺口计为正确。冻结记录 `PARSEVAL_MYSQL_VERSION=8.0.46-0ubuntu0.24.04.3`，失败样本仅保存 digest，冻结后没有根据 hidden 结果修改实现。

## v15 当前公开证据与冻结

- v15 为 generic-tagged `TOP … WITH TIES` 增加有界 T-SQL AST 回退；保留 `WITH TIES` 语义，不降级成 `LIMIT`。公开 mutation layer 读取 52,484 个 train/public 家族，为 52,452 个家族生成 104,904 行，mutation/equivalence 覆盖率均为 99.939029%，15/15 个必需 operator family 均有覆盖，parse gap 降至 32（INPUT_GAP_PROSE 20、INPUT_GAP_MULTI_STATEMENT 10、INPUT_GAP_TEMPLATE 2）。
- v15 observed merge 严格按 `(family_id, mutation_layer_role)` 合并公开 evidence；104,904 个 layer row 中 104,901 个有公开 observed audit 键，3 个新增未抽中的 row 保留空 observed 字段，不使用 candidate 轴冒充执行证据。能力矩阵为 52,452 个唯一开发家族，12 个类别均达到 ≥300，所有 scenario axis 均达到 30。
- v15 Gold native 使用 MySQL 8.0.46、8,801 对、10 seed、row scale 4/8/16、单表最多 32 行：EQUIVALENT 3,962、NOT_EQUIVALENT 3,665、UNDECIDED 755、ENGINE_GAP 419；原子 obligation 覆盖率和结构绑定率均为 100%。NOT_EQUIVALENT Wilson 95% 下界 99.8953%，等价误报率上界约 0.0969%；UNDECIDED/ENGINE_GAP 不进入正确率分母。
- v15 production chain 按类别抽取 50 个目标（435 家族）：318 PASS、117 EXCLUDED、0 FAIL；统计报告中的可计入 NOT_EQUIVALENT 链路分母为 239 个，五个链路维度均 239/239，Wilson 95% 下界 98.4181%。

### v15 hidden 冻结结果

`final_hidden_freeze_verification_v15.json` 在 v15 公开工件固定后运行两次独立、无反馈 hidden 验证：5,846 个 hidden 家族、11,575 个 paired rows，5,736 家族进入声明支持范围，覆盖率 98.1184%；EQUIVALENT 5,604、NOT_EQUIVALENT 4,940、UNDECIDED 680、ENGINE_GAP 351。两次 verdict/rows/digest 完全稳定，确定性标签错配为 0；`acceptance.pass=false` 仅因为 110 个 generation gap（103 equivalence、7 parse），没有把缺口计为正确。此次最终冻结对不可达 vendor engine 明确记为 ENGINE_GAP（MySQL 56、PostgreSQL 20、T-SQL 6），公开 Gold 仍保留 MySQL native 证据。hidden SQL、学生 SQL 和失败家族明文不进入报告；冻结后不得根据 hidden 失败修改实现并重新宣称同一冻结。

## v16 冻结配对回归（当前冻结记录）

- 冻结前公开回归为 76 项通过；修复 `run_phase1_freeze_verification.py` 中等价控制错误复用前一条记录 schema 的作用域缺陷，并用公开构造回归测试锁定该行为。
- `final_hidden_freeze_verification_v16.json` 在同一 hidden 快照上完成两次独立、无反馈执行：5,846 个 hidden 家族，11,678 个 paired rows，5,839 个家族进入声明支持范围，scope coverage 为 99.8803%。原先的 103 个 equivalence generation gap 已消失，剩余 7 个均为 parser/input gap；失败仅保存 digest。
- 两次运行的行数、分层 verdict 和 mismatch digest 完全稳定。EQUIVALENT 5,605、NOT_EQUIVALENT 4,962、UNDECIDED 680、ENGINE_GAP 431；确定性标签 mismatch 为 22，因此 `acceptance.pass=false`。这 22 个 mismatch 和 7 个解析 gap 只作为冻结边界报告，不能据此继续修改实现或重新宣称同一冻结通过。
- v16 不改变 v15 的公开 Gold、能力矩阵、production-chain 或统计验收工件；公开硬泄漏审计仍为 0，公开 gap 审计仍为 32 个明确分类的 INPUT_GAP，且所有公开读者保持 `hidden_partition_read=false`。

## 复现入口

- [v8 manifest](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/manifest.json)
- [v11 mutation manifest](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/mutation_layer_manifest_v11.json)
- [v11 mutation layer JSONL](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/mutation_layer_train_public_v11.jsonl)
- [v11 Gold summary](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/gold_oracle_audit_stratified_10seed_v11_eq4000_summary.json)
- [v11 Gold compact JSONL](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/gold_oracle_audit_stratified_10seed_v11_eq4000.jsonl)
- [v11 能力矩阵](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/capability_matrix_train_public_observed_10seed_v11_full.json)
- [v11 observed axes merge summary](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/observed_axes_merge_10seed_v11_full_summary.json)
- [v11 统计验收 JSON](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/statistical_acceptance_v11_chain_per50.json)
- [v11 统计验收 Markdown](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/statistical_acceptance_v11_chain_per50.md)
- [v11 生产链 summary](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/production_chain_audit_stratified_10seed_v11_eq4000_per50_v2_summary.json)
- [v11 hidden freeze summary](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/final_hidden_freeze_verification_v11.json)
- [v11 public mutation-gap audit](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/mutation_gap_audit_v11.json)
- [v12 Gold summary（MySQL native）](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/gold_oracle_audit_stratified_10seed_v12_mysql_summary.json)
- [v12 Gold compact JSONL（MySQL native）](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/gold_oracle_audit_stratified_10seed_v12_mysql.jsonl)
- [v12 能力矩阵](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/capability_matrix_train_public_observed_10seed_v12_full.json)
- [v12 observed axes merge summary](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/observed_axes_merge_10seed_v12_full_summary.json)
- [v12 统计验收 JSON](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/statistical_acceptance_v12_mysql_chain_per50_full.json)
- [v12 统计验收 Markdown](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/statistical_acceptance_v12_mysql_chain_per50_full.md)
- [v12 生产链 summary](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/production_chain_audit_stratified_10seed_v12_mysql_per50_summary.json)
- [v12 hidden freeze summary](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/final_hidden_freeze_verification_v12_mysql.json)
- [v13 mutation manifest](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/mutation_layer_manifest_v13.json)
- [v13 mutation layer JSONL](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/mutation_layer_train_public_v13.jsonl)
- [v13 public mutation-gap audit](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/mutation_gap_audit_v13.json)
- [v13 Gold summary（MySQL native）](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/gold_oracle_audit_stratified_10seed_v13_mysql_summary.json)
- [v13 Gold compact JSONL（MySQL native）](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/gold_oracle_audit_stratified_10seed_v13_mysql.jsonl)
- [v13 observed axes merge summary](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/observed_axes_merge_10seed_v13_full_summary.json)
- [v13 能力矩阵](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/capability_matrix_train_public_observed_10seed_v13_full.json)
- [v13 统计验收 JSON](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/statistical_acceptance_v13_mysql_chain_per50_full.json)
- [v13 统计验收 Markdown](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/statistical_acceptance_v13_mysql_chain_per50_full.md)
- [v13 生产链 summary](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/production_chain_audit_stratified_10seed_v13_mysql_per50_summary.json)
- [v13 hidden freeze summary](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/final_hidden_freeze_verification_v13_mysql.json)
- [v14 mutation manifest](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/mutation_layer_manifest_v14.json)
- [v14 mutation layer JSONL](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/mutation_layer_train_public_v14.jsonl)
- [v14 public mutation-gap audit](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/mutation_gap_audit_v14.json)
- [v14 observed-axis top-up summary](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/observed_axis_topup_10seed_v14_summary.json)
- [v14 observed-axis merge summary](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/observed_axes_merge_10seed_v14_full_summary.json)
- [v14 Gold summary（MySQL native）](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/gold_oracle_audit_stratified_10seed_v14_mysql_summary.json)
- [v14 Gold compact JSONL（MySQL native）](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/gold_oracle_audit_stratified_10seed_v14_mysql.jsonl)
- [v14 能力矩阵](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/capability_matrix_train_public_observed_10seed_v14_full.json)
- [v14 统计验收 JSON](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/statistical_acceptance_v14_mysql_chain_per50_full.json)
- [v14 统计验收 Markdown](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/statistical_acceptance_v14_mysql_chain_per50_full.md)
- [v14 生产链 JSONL](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/production_chain_audit_stratified_10seed_v14_mysql_per50.jsonl)
- [v14 生产链 summary](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/production_chain_audit_stratified_10seed_v14_mysql_per50_summary.json)
- [v14 hidden freeze summary](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/final_hidden_freeze_verification_v14_mysql.json)
- [v15 mutation manifest](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/mutation_layer_manifest_v15.json)
- [v15 mutation layer JSONL](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/mutation_layer_train_public_v15.jsonl)
- [v15 public mutation-gap audit](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/mutation_gap_audit_v15.json)
- [v15 observed-axis top-up summary](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/observed_axis_topup_10seed_v15_summary.json)
- [v15 observed-axis merge summary](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/observed_axes_merge_10seed_v15_full_summary.json)
- [v15 Gold summary（MySQL native）](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/gold_oracle_audit_stratified_10seed_v15_mysql_summary.json)
- [v15 Gold compact JSONL（MySQL native）](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/gold_oracle_audit_stratified_10seed_v15_mysql.jsonl)
- [v15 能力矩阵](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/capability_matrix_train_public_observed_10seed_v15_full.json)
- [v15 统计验收 JSON](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/statistical_acceptance_v15_mysql_chain_per50_full.json)
- [v15 统计验收 Markdown](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/statistical_acceptance_v15_mysql_chain_per50_full.md)
- [v15 生产链 JSONL](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/production_chain_audit_stratified_10seed_v15_mysql_per50.jsonl)
- [v15 生产链 summary](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/production_chain_audit_stratified_10seed_v15_mysql_per50_summary.json)
- [v15 hidden freeze summary](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/final_hidden_freeze_verification_v15.json)
- [v16 hidden freeze summary](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/final_hidden_freeze_verification_v16.json)
- [split leakage report](../data_construct_test/outputs/phase1_corpus_universe_dev_v8/split_leakage_report.json)

下一阶段仍需：接入 PostgreSQL/SQL Server/Oracle 原生执行器；处理公开语料中有明确语义的多语句/模板边界；处理 hidden 生成缺口。任何进一步优化都必须只基于公开证据，之后重新生成冻结工件；当前持续目标保持 active，未作最终全范围高覆盖率宣称。
