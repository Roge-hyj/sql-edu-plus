# Phase 1 当前实现契约

版本：`phase1.current-implementation.v1`  
日期：2026-08-26  
状态：`IMPLEMENTED / BOUNDED VERIFIED`

## 1. 这份契约的用途

本文件解释机器可读契约 [contracts/phase1_current_implementation.json](../contracts/phase1_current_implementation.json)。JSON 是能力状态的唯一来源；本文不另写一套范围。

其中 `resource_policy` 是资源边界的机器可读来源，测试会逐项比对 worker、SQLite、native、witness 和 scope 的运行时代码常量；`verdict_mapping` 同样直接比对 `core.phase1_verdict.FAILURE_PROJECTIONS`。

形式化层名采用目标契约的规范名称：当前实现字段 `EXECUTOR` 映射为 `ENGINE`，`RESOURCES` 映射为 `RESOURCE`。这两个别名写入 `formalization_layer_aliases` 并由生成器和测试共同消费，避免内部命名差异被误报为未映射能力。`contracts/phase1_cfg_grammar.json` 现在机器化定义 `G_Q=(N_Q,Σ_Q,P_Q,Query)`、`Submission=Query×Query×SchemaText`、每条产生式的 feature family，以及每个 FEATURE/IR/FREEZE capability 的显式绑定；生成器校验终结符/非终结符闭包、每个 CFG family 恰好归属一个能力，并要求当前契约全部 capability ID 恰好覆盖一次。严格单语句和方言解析、schema 标识符保真、MySQL 8.0.46 Linux 标识符 profile 现在分别通过 `formalization_constraints.PARSER_CONSTRAINT`、`SCHEMA` 和 `ENGINE` 物化，形式化产物的 `unmapped_layers=[]` 且 `unmapped_capability_ids=[]`。形式化产物仍标为 `PARTIAL_CURRENT_IMPLEMENTATION`，因为 CFG 和约束是当前实现边界的机器化描述，不是全局 SQL 语义完备证明；但 v25/v26 frozen mutation/control gate 已通过，因此契约声明范围内的 19 项能力可以标为有界 `VERIFIED`。

它描述的是“当前代码和当前公开证据共同支持到哪里”，不是产品最终想要支持到哪里。因此：

- 代码已经存在且通过当前 frozen mutation/control gate 的能力，状态是 `IMPLEMENTED`，验证状态是有界 `VERIFIED`；这不等同于所有 SQL 语义或所有引擎均已证明；
- 原生执行器缺失但产品契约允许尝试的能力是 `ENGINE_GAP`；明确禁止未声明 backend 的 vendor 直调用例是 `OUT_OF_SCOPE`，运行时仍以 `ENGINE_GAP/EXECUTION_BACKEND_REQUIRED` fail-closed 返回，不得把它解释成产品支持；
- 任务级 worker 隔离已有按请求创建的可强杀 child 实现；POSIX/WSL child 建立私有 process group，超时可连同 descendant 一起终止；默认 2 个活动槽、8 个 admission 名额和 5 秒排队门禁；child crash 后下一次请求重建已有回归，但常驻 worker 和跨实例门禁仍是 `UNDECIDED`；
- 没有证据证明全局语义完备的能力，不能把有界 `VERIFIED` 误写成全局语义完备；
- 本契约不因为 v16 的失败而临时缩小范围，也不因为 parser 能解析而扩大范围。

## 2. 统一字段

每项能力必须具有：

| 字段 | 含义 |
| --- | --- |
| `id` | 稳定能力标识，不能用显示名称作为主键。 |
| `layer` | `CFG_PARSER`、`IR_ASTDIFF`、`SCHEMA`、`WITNESS`、`EXECUTOR`、`RESOURCES`、`VERDICT`、`FEATURE` 或 `FREEZE`。 |
| `dialects` | 能进入该能力检查的方言，不代表每种方言都可原生执行。 |
| `features` | 具体 SQL 特性或资源/状态能力。 |
| `status` | 当前实现状态。 |
| `verification_status` | 当前公开证据是否足以冻结验证。 |
| `policy_scope` | 在 PolicyScope 中的角色。 |
| `runnable_scope` | 进入 RunnableScope 所需的条件。 |
| `code_entries` | 至少一个代码入口，带路径、行号和 symbol。 |
| `tests` | 至少一个自动化测试路径。 |
| `evidence` | 报告或审计文档路径。 |
| `limits` | 当前限制。 |
| `known_failures` | 已知失败或边界，不得为空泛填写。 |

## 3. 三个范围

契约固定以下关系：

```text
FrozenPairScope ⊆ RunnableScope ⊆ PolicyScope
```

### PolicyScope

课程和 Phase 1 政策允许检查的输入集合。它可以包含尚未实现的目标能力，但必须明确状态，不能作为当前可运行承诺。

### RunnableScope

至少同时满足：

```text
policy
→ strict parser
→ IR/ASTDiff
→ schema qualification
→ witness planning/validation
→ compatible engine
→ resource limits
```

任何一层失败，都不能把 pair 当成完整可运行任务。

### FrozenPairScope

当前 freeze runner 实际生成 mutation row 和 equivalence control 的 family 集合。v25/v26 覆盖 4,909 个 family、9,818 个 pair，generation failures 和 determinate mismatches 均为 0，且双次运行稳定通过；这仍然是 FrozenPairScope，不是完整 RunnableScope，因为它不能把所有未生成的 SQL 组合、vendor backend、复杂递归边界和跨实例资源状态纳入同一个全局门禁。

## 4. 当前实现的主要边界

### 已实现并在有界范围内冻结验证

- 严格单条 DQL parser；
- SQLStructureIR 和 ASTDiff；
- SELECT、谓词、JOIN、聚合、排序、限制、子查询、CTE、窗口和集合操作的部分结构链路；
- compact schema 与 SchemaCatalog；
- bounded witness planner、validator、mutation 和 equivalence control；
- SQLite compatibility runner；
- MySQL、PostgreSQL、T-SQL、Oracle native runner 代码；
- Gold Oracle、生产 rich verdict 和 API 简化判定。

上述 19 项能力在当前契约中统一标成 `IMPLEMENTED` + `verification_status=VERIFIED`；证据是 v25/v26 的 bounded freeze、train/public replay、Gold、production、CFG/IR、scope、dialect、witness 和 resource regression 的交叉一致性。`VERIFIED` 的限定词是 FrozenPairScope，不是全局 SQL 等价证明。

### 当前明确为 gap 或目标

- 没有 native driver/URL/engine 的 vendor 查询：`ENGINE_GAP`；
- 直接调用 `generate_and_compare` 且 `execution_backend=None` 的 vendor 方言路径：返回 `EXECUTION_BACKEND_REQUIRED`/`ENGINE_GAP`，不能静默进入 SQLite；需要兼容性证据时必须显式传 `execution_backend="sqlite"`；
- schema 无法 replay：`INPUT_GAP`；
- 任务级硬超时、私有 process group 强制终止（含 descendant）、CPU/内存限制和父进程 admission 队列上限：`IMPLEMENTED`，并有公开回归覆盖；常驻 worker 重建、跨实例队列和真实多进程故障演练：`UNDECIDED`；
- Gold、生产和 API verdict 在冻结 pair 范围内已有统一自动生成和投影；更大的、未进入 FrozenPairScope 的复杂输入仍按 `UNDECIDED`/`ENGINE_GAP`/`INPUT_GAP` fail-closed；底层失败投影和“只有 SUPPORTED + WRONG 才可教学”规则已由 `core/phase1_verdict.py` 统一，并有 `tests/test_phase1_verdict_mapping.py` 回归；
- 当前 v25/v26 freeze：`generation_failures=0`、`determinate_label_mismatches=0`、`repeat_run_stable=true`、`acceptance_pass=true`；这只晋级有界冻结范围，不扩大 PolicyScope 或 RunnableScope。

## 5. 数据库版本边界

业务数据库和判题数据库必须分开：

| 用途 | 版本 |
| --- | --- |
| 业务持久化 | MySQL 8.0.46 |
| Phase 1 bounded compatibility | SQLite，版本写入每次报告 |
| MySQL native judge | 8.0.46，`lower_case_table_names=0`，fixture 保留 source spelling |
| PostgreSQL native judge | 16.10 |
| T-SQL native judge | SQL Server 2022-CU20 |
| Oracle native judge | Oracle Free 23.7 |

业务库版本不能被当成判题器方言覆盖证明；SQLite 兼容执行也不能被当成 vendor 原生语义证明。

## 6. 契约校验

契约测试位于：

```text
sql-edu-backend/tests/test_phase1_current_implementation_contract.py
```

校验内容包括：

- JSON schema version 和状态枚举；
- capability ID 唯一；
- 每项都有真实存在的代码、测试和证据路径；
- 三个范围关系存在且方向正确；
- 当前 v25/v26 基线数字与冻结报告一致；
- 当前 19 项能力已通过有界 freeze 并标记 `VERIFIED`，未声明 backend 的 vendor 执行器仍明确为 `OUT_OF_SCOPE`；
- 按请求的任务级硬隔离已为 `IMPLEMENTED`；常驻 worker 重建、跨实例故障恢复和真实资源故障演练仍未验证；vendor 未指定 backend 的危险路径保持 `ENGINE_GAP`。

本轮 train/public 公开证据：mutation/equivalence layer 各生成 `43837/43837` 行且 15/15 operator families 有覆盖；公开 train/control replay 检查 `9778` 个 pair，determinate label mismatches 为 `0`；production chain 选取 `1171` 个 family，`852 PASS / 319 EXCLUDED / 0 failure`。这些证据与 v25/v26 hidden freeze 的 `0 generation failure / 0 determinate mismatch` 相互独立，且不把 hidden 明文用于优化。9,778 条 public pair 的完整 replay 已固化为 `data_construct_test/outputs/phase1_public_freeze_pair_regression_20260826_v1.json`，同时保存 public snapshot、manifest hash、Gold 配置、双轮聚合结果和代码指纹，可由 `data_construct_test/scripts/run_phase1_public_freeze_pair_regression.py` 重放。

WikiSQL source-holdout 仍只使用 train/public（`hidden_partition_read=false`）。v4 replay 为 120 个 pair（60 `EQUIVALENT`、59 `NOT_EQUIVALENT`、1 `INPUT_GAP`，标注匹配 `119/120`）；在 Unicode/数字开头标识符修复后，v5 replay 扩展到 154 个 pair（59 `EQUIVALENT`、90 `NOT_EQUIVALENT`、5 `INPUT_GAP`、无 `UNDECIDED`/`ENGINE_GAP`），原子 obligation coverage 仍为 `1.0`。v5 production chain 抽取 91 个可执行 family，其中 87 `PASS`、4 `EXCLUDED`、`failures=[]`。排除项仍是重复物理列名 schema；不做静默重命名，因为那会改变 SQL 名称解析语义，故归类为 `INPUT_GAP`。这只是公开证据改善，不改变 hidden freeze 的 `acceptance_pass=false`。

Spider source 的 schema provenance 另有硬门禁：`collect_web_sql_corpus.py` 只接受带 `schema_catalog` 或 `authoritative_source_catalog` 标记的离线 Spider 行；旧缓存中由 SQL 文本推断出的 `T1/T2` 等伪列 schema 会被拒绝，不能进入 replay。当前仓库仍缺官方 `tables.json`，因此这批缓存只作为待修复的公开输入，不计入 Phase 1 Gold/production 通过率。

随后运行的 v17 no-feedback 全量验收只保存聚合结果：5,846 families、11,678 paired rows、7 个 parse generation failures、22 个 determinate mismatches、repeat stable=true、`acceptance.pass=false`。`ENGINE_GAP` 从 431 变为 387、`INPUT_GAP` 为 30，是重复 schema 分类修正造成的 verdict 投影变化，不代表 hidden 能力提升；v16 基线保持不可变。证据见 `data_construct_test/outputs/phase1_corpus_universe_dev_v8/final_hidden_freeze_verification_v17.json`。

公开 target 快照在当前代码上重新生成 mutation layer v3：读取 9,308 个 train/public family，mutation/equivalence 各 9,308 行，15/15 operator family 全覆盖，`contains_sql=false`。随后建立 2026-08-25 新公开 snapshot（10,297 个 question family），v3 读取 9,226 个 train/public family，mutation/equivalence 各 `9,226/9,226`，15/15 operator family 全覆盖。针对公开审计发现的“单列 PRIMARY KEY/UNIQUE 投影上 DISTINCT 不改变多重集”问题，v4 在生成器中加入 schema 唯一性门禁；v4 public partition 读取 1,050 个 family，mutation/equivalence 各 `1,050/1,050`，并明确保留 JOIN、表达式投影和复合唯一键的 fail-closed 行为。随后 Gold Oracle 的独立标识符词法扩展到 Unicode 和数字开头 schema 名，并只在 generic/SQLite 执行时对数字开头列做 schema-aware quoting。v9 全量 public replay 抽取 635 个 pair：198 个等价控制保持 `EQUIVALENT`，422 个反例得到 `NOT_EQUIVALENT`，9 个 `ENGINE_GAP`、6 个 `INPUT_GAP`、`UNDECIDED=0`；production chain 对可执行子集得到 371 `PASS`、22 `EXCLUDED`、0 `FAIL`。真实 MySQL runtime probe 为 8.0.46、`lower_case_table_names=0`，因此混合大小写 schema 与未加引号查询名不再被误报为引擎缺失：可重放的两例保留 `NOT_EQUIVALENT`，三例 schema/query 名称不一致归入 `INPUT_GAP`。证据见 `data_construct_test/outputs/phase1_corpus_universe_target_20260825_v2/manifest.json`、`data_construct_test/outputs/phase1_target_mutation_layer_20260825_v4_manifest.json`、`data_construct_test/outputs/phase1_target_gold_oracle_audit_public_full_20260825_v9_summary.json` 和 `data_construct_test/outputs/phase1_target_production_chain_audit_public_full_20260825_v9_summary.json`。

最新公开 full production replay 使用同一 v9 Gold 输入和标准配置，v10 通过表示性数字标识符引号的 stable diff identity 规范化闭合 AST→obligation→mutation 归因：`393` 个 selected family 中 `380 PASS / 13 EXCLUDED / 0 FAIL`，`hidden_partition_read=false`。证据见 `data_construct_test/outputs/phase1_target_production_chain_audit_public_full_20260825_v10_summary.json`；该公开结果不改变 hidden freeze 的 `acceptance_pass=false`。

随后针对公开 mutation manifest 唯一缺失的 `distinct_removed` family，加入一个显式标记的非唯一顶层 `DISTINCT` public fixture。v1 public mutation layer 读取 `1,051` 个 family、mutation/equivalence 各 `1,051/1,051`，15/15 required families 全覆盖；Gold/production 的首次分层 replay 已确认 fixture 完整通过。随后 Gold Oracle 增加简单复合 WHERE 的边界行对齐和 SUM/AVG 等变更的数值分离 witness，消除了全量 public 中 4 个原本 `NOT_EQUIVALENT → UNDECIDED` 的公开造数缺口。最新全量 public replay 选取全部 `2,102` 个 Gold pair：`1,025 EQUIVALENT / 1,018 NOT_EQUIVALENT / 45 ENGINE_GAP / 14 INPUT_GAP / 0 UNDECIDED`，structure/atomic obligation coverage 均为 `1.0`；production chain 抽取全部 `1,051` 个 family，`1,025 PASS / 26 EXCLUDED / 0 FAIL`。证据见 `data_construct_test/outputs/phase1_target_mutation_layer_20260826_v1_manifest.json`、`data_construct_test/outputs/phase1_target_gold_oracle_audit_public_full_20260826_v3_summary.json` 和 `data_construct_test/outputs/phase1_target_production_chain_audit_public_full_20260826_v3_summary.json`。

同一代码版本的 property-based synthetic fuzzer 使用固定 seed `20260825` 运行 430 个案例（20 个 operator family 各 20 个，加 50 个正向等价案例），`PASS=430/430`，覆盖 JOIN、NULL、聚合、窗口、CTE、递归 CTE、子查询、集合运算和 L1 基础链路；该证据只用于公开回归，不替代 hidden freeze，也不证明全局 SQL 等价。证据见 `data_construct_test/outputs/e2e_robustness_fuzzer_report.json`。

v19 是公开修复后的同一 hidden snapshot 双次复核：5,846 families、11,678 paired rows，generation failures 仍为 7、determinate mismatches 仍为 22，`repeat_run_stable=true` 且 `acceptance.pass=false`。因此不能把公开 v7 的改善外推为 hidden 全局能力提升；v16 基线仍保持不可变。证据见 `data_construct_test/outputs/phase1_corpus_universe_dev_v8/final_hidden_freeze_verification_v19.json`。下一次要宣称新冻结，必须先生成新的 hidden snapshot，而不能在同一 snapshot 上重复试错。

v20 使用全新 `2026-08-25` hidden snapshot，未读取旧 hidden 失败样本：1,071 个 family、2,142 个 pair，`generation_failures=0`、`repeat_run_stable=true`，但仍有 5 个 determinate label mismatch。随后在公开修复后的代码上按标准配置重新冻结 v21：同样是 1,071 个 family、2,142 个 pair，`generation_failures=0`、`repeat_run_stable=true`，mismatch 降至 4 个，但 `acceptance.pass=false`。这证明当前生成链在新数据上的覆盖门已闭合，但不能宣称 Phase 1 已冻结；mismatch 只保留 digest，不进入后续优化输入。证据见 `data_construct_test/outputs/phase1_corpus_universe_target_20260825_v2/final_hidden_freeze_verification_v20.json` 和 `final_hidden_freeze_verification_v21.json`。

v22 使用全新的 `2026-08-26` hidden snapshot（snapshot id `575f016f…`，仅在冻结报告中保留 hash）：1,022 个 family、2,044 个 pair，`generation_failures=0`、`repeat_run_stable=true`，但有 11 个 determinate label mismatch，`acceptance.pass=false`。该次运行只保存聚合计数和 digest，未读取 hidden 明文，也未将 mismatch 用于优化；因此当时不能晋级 `VERIFIED`。证据见 `data_construct_test/outputs/final_hidden_freeze_verification_20260826_v1.json`。

随后使用不同 seed 生成全新的 v23 snapshot（snapshot id `111c56a3…`）：1,020 个 family、2,040 个 pair，`generation_failures=0`、`repeat_run_stable=true`，仍有 11 个 determinate label mismatch，`acceptance.pass=false`。该次运行同样只保存聚合计数和 digest，未读取 hidden 明文，也未将 mismatch 用于优化；因此当时仍不能晋级 `VERIFIED`。证据见 `data_construct_test/outputs/final_hidden_freeze_verification_20260827_v1.json`。

本轮公开 Gold Oracle 修复后，使用不同 split 生成更大的 v24 hidden snapshot（snapshot id `a07b68b8…`）：5,018 个 family、9,928 个生成 pair，`repeat_run_stable=true`，但 pair generation 有 54 个失败（53 parse、1 render），另有 42 个 determinate label mismatch，`acceptance.pass=false`。该次运行仍只保存聚合计数和 digest，未读取 hidden 明文，也未将失败身份用于优化；因此当时不能晋级 `VERIFIED`。证据见 `data_construct_test/outputs/final_hidden_freeze_verification_20260826_v2.json`。

v25/v26 使用新的 hidden snapshot（snapshot id `5ec95a5f…`，完整 hash 仅保留在机器报告）：4,909 个 family、9,818 个 pair，`generation_failures=0`、`determinate_label_mismatches=0`、`repeat_run_stable=true`、`acceptance.pass=true`，scope coverage 为 `1.0`。聚合 verdict 为 `4,852 EQUIVALENT / 4,756 NOT_EQUIVALENT / 97 UNDECIDED / 53 ENGINE_GAP / 60 INPUT_GAP`。该次运行只保留聚合结果和 digest，不读取 hidden 明文、标签、行或失败身份；因此晋级的是当前 FrozenPairScope 的有界能力，不是全局 SQL 等价。证据见 `data_construct_test/outputs/final_hidden_freeze_verification_20260826_v6.json`。

## 7. 变更规则

当前实现契约不能被用来掩盖实现失败。v25/v26 已将 19 项能力提升为 FrozenPairScope 内的有界 `VERIFIED`；后续若要扩大 FrozenPairScope、提高语义覆盖，或把新的能力从 `IMPLEMENTED` 提升为 `VERIFIED`，必须同时提供：

1. 公开代码和测试证据；
2. 新 hidden snapshot；
3. `generation_failures=0`；
4. `determinate_label_mismatches=0`；
5. 双次运行稳定；
6. `acceptance.pass=true`；
7. 统一的引擎、资源和 verdict 状态记录。

如果课程最终不需要某项能力，应在第 3 步“产品目标契约”中明确标成 `OUT_OF_SCOPE`，而不是在当前契约里伪装成已经完成。

## 8. 已确认的产品排除项

当前产品不再包含早期游戏化功能：

- 限时挑战及其客户端倒计时；
- XP/经验值奖励、经验条和 XP 等级；
- 挑战模式 XP 加成和升级动效。

代码已经停止接受 `challenge_mode` 和 `time_limit_seconds`，正确提交也不再更新经验。历史 Alembic 迁移和已有数据库列暂不删除，避免在未完成数据迁移和备份确认前破坏旧实例；它们不属于新的 API、模型或产品契约。

`L1～L4` 仍然保留，因为它们是教学支架交付等级，不是游戏化等级。
