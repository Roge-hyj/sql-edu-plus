# Phase 1 真实代码能力盘点

版本：v1（2026-08-24）  
目的：记录当前代码真正能做什么、不能做什么，以及每个结论的可复现证据。

本文是第 1 步“盘点真实代码能力”的结果，不是目标产品契约，也不是新的支持范围声明。当前实现契约已经建立在 `contracts/phase1_current_implementation.json`，只能引用本文中已有代码和证据；不得把文档中的抽象 CFG、有限样本覆盖率或“未发现反例”扩大解释为 SQL 全局等价证明。

## 1. 盘点方法和状态含义

本次盘点逐项检查七个能力域：

1. CFG/Parser
2. IR/ASTDiff
3. schema
4. witness
5. 执行器
6. 资源限制
7. verdict

每项都记录代码入口、自动化测试、当前限制、已知失败和可复现证据。状态使用以下含义：

| 状态 | 含义 |
| --- | --- |
| `TARGET` | 产品希望支持，但当前代码尚未形成实现。 |
| `IMPLEMENTED` | 代码路径已存在，能够在给定输入下运行；尚未满足全局冻结证据要求。 |
| `VERIFIED` | 已通过公开回归和冻结门禁，且冻结条件没有未解释失败。 |
| `OUT_OF_SCOPE` | 当前产品契约明确不支持。 |
| `ENGINE_GAP` | 结构或契约允许，但所需原生执行器/驱动/连接不可用。 |
| `UNDECIDED` | 现有有限证据无法给出可靠的确定结论。 |

`IMPLEMENTED` 不是 `VERIFIED` 的同义词；`ENGINE_GAP` 和 `UNDECIDED` 也不是代码崩溃的同义词。它们分别表示执行基础设施缺失和证据不足。

## 2. 当前可复现基线

### 2.1 代码和环境

- Git HEAD：`d29a5f6 docs: expand phase1 terminology glossary`
- SQL parser：SQLGlot 29.0.1（由 `sqlglot.parse(..., error_level=ErrorLevel.RAISE)` 驱动）
- 业务数据库契约：MySQL 8.0.46；业务连接与判题连接仍是不同用途、不同生命周期的实例。
- Phase 1 判题器：可使用 bounded SQLite，或按题目方言选择 MySQL/PostgreSQL/T-SQL/Oracle 原生 runner。
- 工作区在盘点时已有用户修改和生成物；本次没有清理、回滚或覆盖这些修改。

### 2.2 已执行公开回归

从 `sql-edu-backend` 目录执行：

```bash
PYTHONPATH=. pytest -q \
  tests/test_phase1_sql_cfg_coverage.py \
  tests/test_phase1_advanced_structure_ir.py \
  tests/test_phase1_scope_contract.py \
  tests/test_phase1_dialect_semantics.py \
  tests/test_phase1_gold_oracle.py \
  tests/test_phase1_mutation_layer.py \
  tests/test_witness_generation_foundation.py \
  tests/test_witness_validators.py \
  tests/test_phase1_acceptance_and_freeze.py
```

结果：`251 passed in 8.96s`。这证明公开单元/集成测试通过，不证明 hidden family 全部通过，也不证明任意 SQL 的全局语义等价。在线公开 smoke builder 另有逐案进程硬超时，避免 pathological SQL 阻塞构建。

当前公开 witness/validator 回归为 `540 passed`；契约、Gold、CFG、作用域和 mutation-layer 回归在本轮继续覆盖 Gold Oracle 的复合边界/聚合回归。public mutation layer 另以显式非唯一 `DISTINCT` fixture 补齐 `distinct_removed`，15/15 required operator families 均有公开生成证据；最新全量 public Gold 为 `2,102/2,102` 已分类、`UNDECIDED=0`，production chain 为 `1,025 PASS / 26 EXCLUDED / 0 FAIL`。

## 3. CFG / Parser

### 3.1 代码入口

| 入口 | 位置 | 作用 |
| --- | --- | --- |
| `parse_single_query` | `sql-edu-backend/core/sql_dialect_resolver.py:310` | 方言解析器入口，要求单条查询。 |
| `resolve_sql_dialect` / `resolve_sql_dialect_or_raise` | `sql-edu-backend/core/sql_dialect_resolver.py:383,567` | 合并声明方言、检测到的专属特征和 parse 结果。 |
| `_parse_sql_strict` | `sql-edu-backend/core/parseval_data_generator.py:8606` | 严格解析单条 `exp.Query`，拒绝多语句和非查询语句。 |
| `_dialect_candidates` | `sql-edu-backend/core/parseval_data_generator.py:8630` | 根据反引号、`TOP`、`DISTINCT ON`、`::` 等线索选择尝试顺序。 |
| `generate_and_compare` | `sql-edu-backend/core/parseval_data_generator.py:298` | 生产主链路，先解析方言再进入 schema、witness 和执行阶段。 |

### 3.2 自动化测试

- `tests/test_phase1_sql_cfg_coverage.py`：文档化 CFG case/label 的端到端覆盖。
- `tests/test_phase1_dialect_semantics.py`：方言识别、默认方言和 SQLite 语义边界。
- `tests/test_phase1_mutation_layer.py`：SQL 变体重新解析、方言特有 `TOP`/递归 CTE 等边界。
- `tests/test_witness_generation_foundation.py`：多语句、DML、未知 schema 的解析/资格门禁。

### 3.3 当前限制

1. 项目没有一份独立、可执行的 `N_Q/Sigma_Q/P_Q` grammar 文件；`docs/23-Phase1-dev_v4-当前能力范围与CFG.md` 中的 CFG 是抽象规格，实际 parser 是 SQLGlot 加方言规则和手写结构 cases。
2. 严格入口只接受一条完整 DQL query；多语句、DML 或无法保留为 `exp.Query` 的输入不会进入等价比较。
3. 方言识别基于声明、regex 特征和 SQLGlot parse 的组合。能识别或能 parse 不代表 IR、schema replay、witness 和原生执行都支持。
4. SQLGlot 能读到的结构可能超出项目 IR/执行器能力；因此 parser 层不能单独授予“支持”标签。

### 3.4 已知失败和边界

- `contracts/phase1_cfg_grammar.json` 是当前 CFG 的机器权威源，明确 `Submission`、`Query`、`SchemaText`、终结符/非终结符和产生式 feature family；`phase1_cfg_fragment_capability.json` 的开发样本为 150 cases：148 supported、2 engine gap、support rate 98.67%。这是有限 case 集的能力快照，不是 CFG 的语言覆盖证明。
- v16 hidden freeze 仍有 7 个 generation/parser-input gap；这些 family 未能形成完整可冻结 pair，不能被当作 `UNDECIDED` 掩盖。
- 方言特征冲突、未知方言和不完整 SQL 会走 `DIALECT_CONFLICT`、`UNSUPPORTED_DIALECT`、`SYNTAX_ERROR` 等边界；对外 API 的错误映射仍需在当前实现契约中统一。
- generic/未声明方言的双引号兼容有明确边界：只有双引号内容命中已声明 schema 表名或列名时，才切换到标准标识符语义；未命中的 `"Sales"` 等 token 仍按 MySQL 默认字符串字面量处理。回归见 `tests/test_phase1_dialect_semantics.py::test_generic_schema_double_quoted_column_uses_identifier_semantics`。

### 3.5 可复现证据和状态

```bash
cd sql-edu-backend
PYTHONPATH=. pytest -q tests/test_phase1_sql_cfg_coverage.py tests/test_phase1_dialect_semantics.py
```

当前状态：`IMPLEMENTED`（解析组件已实现）；全 SQL 语言覆盖：`UNDECIDED`；有限样本 engine gap：`ENGINE_GAP`。尚不能标记 `VERIFIED`。

## 4. IR / ASTDiff

### 4.1 代码入口

| 入口 | 位置 | 作用 |
| --- | --- | --- |
| `SQLStructureIR` / AST schema | `sql-edu-backend/core/ast_schema.py`（约第 460 行） | 表达查询结构、子句、作用域和 typed 节点。 |
| `extract_ast_diffs` | `sql-edu-backend/core/parseval_data_generator.py:8924` | 对标准答案和学生 SQL 生成聚焦的结构差异。 |
| `_build_phase1_scope_metadata` | `sql-edu-backend/core/parseval_data_generator.py` | 生成版本化、有限深度的 Phase 1 → Phase 2 scope metadata。 |
| `compile_obligations` | `sql-edu-backend/core/witness_generation/obligations.py` | 将 AST diff 编译为可验证 obligation。 |

### 4.2 自动化测试

- `tests/test_phase1_advanced_structure_ir.py`：窗口、QUALIFY、集合、嵌套作用域等结构 IR。
- `tests/test_phase1_scope_contract.py`：scope/edge allowlist、稳定 ID 和跨作用域绑定。
- `tests/test_phase1_sql_cfg_coverage.py`：CFG case 到 diff/obligation 的链路。
- `tests/test_witness_generation_foundation.py`：结构 diff 到 obligation 的语义绑定。
- Gold Oracle 审计工件记录 ASTDiff-obligation 绑定 `6,888/6,888`，但该比例只表示抽样审计中的绑定成功。
- `tests/test_phase1_scope_contract.py` 还直接覆盖 `generate_and_compare → scope_metadata → ScopedQueryGraph`：普通子查询自动产生 `SUBQUERY_OF`；LATERAL 派生查询产生 `DERIVED_FEEDS + LATERAL_TO`，存在真实外层限定引用时再产生 `CORRELATED_TO`。两条链路均要求图状态为 `COMPLETE`。

### 4.3 当前限制

1. IR 是项目自定义的教学结构视图，不是完整 SQL 标准 AST 的同构副本。
2. scope metadata 有扫描节点、scope、edge、diff、binding 和路径深度上限（见 `parseval_data_generator.py:126-135`）；触顶会保留 limitation 标记而不是无限展开。
3. AST diff 能说明结构变化和候选知识点，不能独立证明结果不等价；必须继续通过 schema、witness 和执行器。
4. 某些等价重写会被保留为 alias diff 或直接不产生语义 diff；这一策略依赖有限的已实现 rewrite 规则。

### 4.4 已知失败和边界

- 结构可识别但 SQLite 或目标 vendor engine 不可执行的查询会停在 `ENGINE_GAP`，不能从 IR 成功推导语义 verdict。
- hidden v16 的 22 个确定性 label mismatch 表明“结构标签/预期标签/最终 verdict”仍有未闭合差异；因此 100% diff 绑定不能解释为 100% 语义正确。

### 4.5 可复现证据和状态

```bash
cd sql-edu-backend
PYTHONPATH=. pytest -q tests/test_phase1_advanced_structure_ir.py tests/test_phase1_scope_contract.py
```

当前状态：`IMPLEMENTED`；公开结构回归已通过，但全量语义冻结：`UNDECIDED`，不能标记 `VERIFIED`。

补充原生证据：上述普通子查询和 LATERAL 关系已在 Docker PostgreSQL 16 上真实执行，Phase1 `scope_metadata` 与下游 `ScopedQueryGraph` 均为 `COMPLETE`。这验证的是作用域关系生成和原生执行链路，不把有限查询形状扩大成任意嵌套 SQL 的完备作用域证明。

## 5. Schema

### 5.1 代码入口

| 入口 | 位置 | 作用 |
| --- | --- | --- |
| `SchemaCatalog` | `sql-edu-backend/core/witness_generation/schema_scope.py:57` | 保存物理表、列类型、nullability、主键、唯一约束和外键。 |
| `SchemaCatalog.from_legacy` | `schema_scope.py:73` | 将 compact schema 转为结构化 catalog。 |
| `parse_schema_text` / `parse_schema_column_types` | `sql-edu-backend/core/parseval_data_generator.py:184` 附近 | 解析公开的紧凑 schema 文本。 |
| `analyze_schema_qualification` | `sql-edu-backend/core/witness_generation/schema_scope.py:461` | 区分物理表、CTE、derived/lateral relation，并报告缺失表列。 |
| `generate_and_compare` | `parseval_data_generator.py:384-520` | 在 AST 执行前对标准/学生两侧做 schema qualification。 |

### 5.2 自动化测试

- `tests/test_witness_generation_foundation.py`：catalog 约束、CTE/derived scope、缺失表列、歧义列、多语句/DML。
- `tests/test_phase1_scope_contract.py`：query scope 和跨作用域边界。
- `tests/test_phase1_gold_oracle.py`：schema replay、约束和输入/执行 gap 分类。

### 5.3 当前限制

1. compact schema 主要表达表名和列名；类型、nullability、约束只有显式声明时才可靠，未知 nullability 按可空保守处理。
2. schema qualification 能报告静态缺失，但 vendor 原生引擎的最终名称解析仍可能不同。
3. legacy compact schema 和结构化 catalog 共存；标准答案 schema 无法 replay 时统一归为 `INPUT_GAP`，学生侧缺失物理表仍按答案错误处理。
4. schema 可解析不等于 witness 可满足；主键/外键/唯一约束可能使某些 obligation 无法在行数上限内满足。

### 5.4 已知失败和边界

- vendor 原生引擎的最终名称解析仍可能与静态 qualification 不同；标准答案 schema qualification 失败现在返回 `INPUT_ERROR`/`INPUT_GAP`，与 Gold Oracle 的输入边界保持一致。MySQL 8.0.46 Linux profile 明确固定 `lower_case_table_names=0`，fixture 保留 source spelling；因此表/列名称解析失败是 `INPUT_GAP`，而版本或标识符模式不匹配才是 `ENGINE_GAP`。
- unknown table/column、歧义未限定列、无法 replay 的 schema 会阻止可信执行；不能降级为“学生答案错误”。

### 5.5 可复现证据和状态

```bash
cd sql-edu-backend
PYTHONPATH=. pytest -q tests/test_witness_generation_foundation.py tests/test_phase1_gold_oracle.py
```

当前状态：`IMPLEMENTED`；schema 不可重放的具体 family：`INPUT_GAP`；legacy/catalog 其他跨层映射仍需公开审计后再标记 `VERIFIED`。

## 6. Witness

### 6.1 代码入口

| 入口 | 位置 | 作用 |
| --- | --- | --- |
| `generate_witness_suite` | `sql-edu-backend/core/parseval_data_generator.py:7346` | 编译 obligation、规划 bounded world、物化测试数据库。 |
| `WitnessPlanner` | `sql-edu-backend/core/witness_generation/planner.py:765` | 按冲突和成本隔离/打包 world。 |
| `compile_obligations` | `sql-edu-backend/core/witness_generation/obligations.py` | 生成比较边界、NULL、JOIN、聚合、窗口、集合等约束。 |
| `validate_obligation` | `sql-edu-backend/core/witness_generation/validators.py:3979` | 根据执行结果验证 obligation 是否真的被激活。 |
| `generate_and_compare` | `parseval_data_generator.py:298` | 调度 witness、执行、mutation 和 evidence 汇总。 |

### 6.2 自动化测试

- `tests/test_witness_generation_foundation.py`：跨查询形状的 obligation、world split、生成完整性和 fail-closed。
- `tests/test_witness_validators.py`：JOIN、聚合、窗口、NULL、集合、LIKE/regex、投影形状等 validator。
- `tests/test_phase1_mutation_layer.py`：mutation 生成、repair 和 equivalence control。

### 6.3 当前限制

代码中的硬上限为：

```text
_MAX_WITNESS_WORLDS = 8
_MAX_WITNESS_ATTEMPTS = 8
_MAX_WITNESS_ROWS_PER_TABLE = 32
```

实际调用还可能通过 `max_rows_per_table` 进一步缩小范围；planner 在 obligation 冲突和 world 上限下会记录 `uncovered_obligations` 或 diagnostic。witness 是有界的反例构造，不是任意关系实例的完备搜索。

### 6.4 已知失败和边界

1. freeze runner 的 declared scope 目前主要按“能生成 mutation + equivalence control”定义；没有把 witness validator 成功、schema replay、native engine availability 作为同一门禁。因此 `FrozenPairScope` 不能直接等同于 `L_exec`。
2. 无法满足约束、达到 world/attempt/row 上限、执行结果未激活 obligation 时，应保持 `UNDECIDED` 或 gap，不应强行判错。
3. 单个 finite witness 找到差异可以证明该实例上的 `NOT_EQUIVALENT`；多个有限 world 一致不能证明全局 `EQUIVALENT`。
4. 对聚合和顶层 `DISTINCT` 的直接单表查询，最终物化阶段会为简单可比较谓词写入可命中行；聚合分支按 MIN/MAX/SUM/AVG/COUNT 的受限 measure 形状构造差异，COUNT(column) 对 COUNT(*) 还会保留一个 NULL measure，DISTINCT 分支会复制两个满足过滤条件的非键投影行。该修复只覆盖公开证据中已验证的窄形状，不扩大为任意谓词或全局等价保证。

### 6.5 可复现证据和状态

```bash
cd sql-edu-backend
PYTHONPATH=. pytest -q tests/test_witness_generation_foundation.py tests/test_witness_validators.py tests/test_phase1_mutation_layer.py
```

当前状态：`IMPLEMENTED`；对任意 SQL pair 的完备 witness 能力：`UNDECIDED`；公开 full replay 中 4 个曾经 Gold `UNDECIDED` 的 family 已通过独立造数修复并进入 production chain `PASS`，但触及约束/资源上限的实例仍为 `UNDECIDED` 或 `ENGINE_GAP`（按实际原因分类）。

## 7. 执行器

### 7.1 代码入口

| 入口 | 位置 | 作用 |
| --- | --- | --- |
| `_select_execution_backend` | `sql-edu-backend/core/parseval_data_generator.py:8121` | 选择 SQLite、native 或目标方言 backend；声明 vendor 方言而不传 backend 时 fail-closed。 |
| `_execute_with_backend` | `parseval_data_generator.py:21882` | 将执行委托给 SQLite 或 `execute_native_query`。 |
| `NativeQuerySession` | `sql-edu-backend/core/native_engine_runner.py:142` | 共享隔离 fixture、savepoint 和查询恢复。 |
| `execute_native_query` / `native_query_session` | `native_engine_runner.py:191,277` | 按 MySQL/PostgreSQL/T-SQL/Oracle 驱动执行。 |

### 7.2 自动化测试

- `tests/test_phase1_dialect_semantics.py`：方言语义边界和 SQLite 不可保留结构。
- `tests/test_phase1_gold_oracle.py`：Gold Oracle 的 engine/input gap、native fixture 和有限语义比较。
- `tests/test_witness_generation_foundation.py`：生产链路对 backend 和 schema 的路由。
- 公开冻结报告记录各方言配置版本和 ENGINE_GAP 计数。

### 7.3 当前限制

原生 runner 当前声明的资源参数为：

```text
statement timeout: 3 秒
最大结果行数: 10,000
最大结果字节数: 8 MiB
连接超时: 3 秒（驱动配置）
```

业务数据库和 MySQL native judge 当前都使用 MySQL 8.0.46；两者仍是独立配置，不能共享业务数据或连接凭据。native judge 还必须满足 Linux `lower_case_table_names=0`，并保留 schema 表/列的 source spelling；runner 会在建 fixture 前探测并拒绝不匹配 profile。

### 7.4 已知失败和边界

- `_select_execution_backend` 对声明的 vendor 方言不再接受隐式 SQLite 回退。直接调用 `generate_and_compare(..., sql_dialect="mysql", execution_backend=None)` 会返回 `EXECUTION_BACKEND_REQUIRED`/`ENGINE_GAP`；调用方必须传 `execution_backend="auto"` 走配置的原生 runner，或显式传 `execution_backend="sqlite"` 仅请求兼容性证据。未声明方言的 generic 调用仍保留 bounded SQLite 路径。
- 原生 driver、URL、Docker engine 任一不可用时只能返回 `ENGINE_GAP`；不能把 SQLite fallback 结果写成 MySQL/PostgreSQL 语义证明。
- SQLite 可通过转译执行部分 vendor 结构，但转译成功只表示兼容执行，不表示原生语义保真。

### 7.5 可复现证据和状态

```bash
cd sql-edu-backend
PYTHONPATH=. pytest -q tests/test_phase1_dialect_semantics.py tests/test_phase1_gold_oracle.py
PYTHONPATH=. pytest -q tests/test_parseval_data_generator.py -k execution_backend
```

当前状态：

- SQLite bounded runner：`IMPLEMENTED`；
- 原生 runner 代码：`IMPLEMENTED`；
- 缺少 driver/连接的 vendor family：`ENGINE_GAP`；
- 声明 vendor 方言而未传 backend：明确返回 `EXECUTION_BACKEND_REQUIRED`/`ENGINE_GAP`；未声明方言的 generic 路径仍可显式或默认使用 bounded SQLite；
- 全方言原生语义冻结：尚非 `VERIFIED`。

本次公开回归证据：`tests/test_parseval_data_generator.py -k execution_backend` 为 `6 passed`；其中
MySQL、PostgreSQL、T-SQL、Oracle 四种声明方言的隐式 backend 均 fail-closed，显式 SQLite 兼容路径和
显式 `auto` 路径保持原有行为。

## 8. 资源限制

### 8.1 代码入口

| 资源面 | 位置 | 当前机制 |
| --- | --- | --- |
| SQLite VM 指令预算和 wall-clock | `parseval_data_generator.py:21848-21871` | `set_progress_handler`，1,000,000 VM steps、0.5 秒。 |
| SQLite fixture 行数和记录数 | `parseval_data_generator.py:114-124`、生成/执行函数 | 每表最多 32 行；记录结果最多 256 行。 |
| Native statement/result 限制 | `native_engine_runner.py:48-50`、`1181-1217` | 3 秒、10,000 行、8 MiB。 |
| Witness planning 上限 | `parseval_data_generator.py:114-116`、`generate_witness_suite` | 8 worlds、8 attempts、32 rows/table。 |
| API 超时与 worker 生命周期 | `sql-edu-backend/routers/ai.py:161-305` | 真实 parser job 按请求进入默认 `spawn` 的可强杀 child；child 在 POSIX/WSL 中建立私有 process group，超时或清理优先 `SIGKILL` 整组（包含意外 descendant），子进程默认设置 50 秒 CPU / 2 GiB 地址空间上限，父进程最多保留 2 个活动槽，并以 8 个 admission 名额限制额外排队请求；admission 和 work-slot 等待均为 5 秒。 |

### 8.2 自动化测试

- `tests/test_witness_generation_foundation.py`：行数、world split、超大 LIMIT 和 fail-closed。
- `tests/test_phase1_dialect_semantics.py`：执行后端边界。
- `tests/test_phase1_acceptance_and_freeze.py`：冻结重复稳定性和统计门禁。

### 8.3 当前限制和已知失败

1. SQLite/native 的查询级限制与 API child 的 CPU/地址空间限制尚未由同一机器可读 policy 生成。
2. 真实 parser job 的超时可以 kill child 及其私有 process group，不再遗留后台线程或由 child 派生的孤儿进程；测试替身仍允许 thread seam，因此不能把 thread seam 当作生产隔离证据。
3. 当前是按请求创建 child；child crash 后下一次请求自动创建新 child 已有公开回归，超时清理会终止私有 process group 中的 descendant；但常驻 worker 池、跨实例队列和真实 OOM 故障演练尚未完成。

### 8.4 可复现证据和状态

```bash
cd sql-edu-backend
PYTHONPATH=. pytest -q tests/test_witness_generation_foundation.py tests/test_phase1_acceptance_and_freeze.py
```

当前状态：查询级限制 `IMPLEMENTED`；按请求的任务级硬隔离和私有 process group descendant kill `IMPLEMENTED`；默认 `spawn` child、child crash 后下一次请求重建、CPU 限制和硬超时已有公开回归；公开语料构建也采用逐案可杀 child，超时投影为 `RESOURCE_LIMIT/UNDECIDED`；常驻 worker 池、跨实例故障恢复和真实 OOM 演练仍为 `UNDECIDED`。正式学生试运行前不能把资源安全标成 `VERIFIED`。

本次公开回归证据：`tests/test_check_sql_flow.py -k process_timeout or process_memory_limit or real_phase1_parser or phase1_capacity or singleflight` 为 `5 passed`；覆盖硬超时、CPU/内存限制、队列上限、私有 process group descendant kill、child crash/recreate，以及真实 `generate_and_compare` child-process smoke。公开 v2 smoke 报告：`data_construct_test/outputs/online_random250_structure_generation_report_v2.json`，50 cases，38 PASS，12 个明确 gap（11 KNOWN_GAP、1 ENGINE_GAP），无错误判题。

## 9. Verdict / 结果状态

### 9.1 代码入口

| 层 | 位置 | 输出 |
| --- | --- | --- |
| Gold Oracle | `data_construct_test/scripts/phase1_gold_oracle.py` | `EQUIVALENT`、`NOT_EQUIVALENT`、`UNDECIDED`、`ENGINE_GAP`、`INPUT_GAP`。 |
| 生产 `SandboxRun` | `parseval_data_generator.py:150-171`、`core/phase1_verdict.py` 及 `_failed`/主链路 | `status`（SUPPORTED、SUPPORTED_WITH_LIMITS、SEMANTIC_BOUNDARY、KNOWN_GAP、ENGINE_GAP、INPUT_GAP）；明确不支持的 feature 返回 `KNOWN_GAP`，执行器不可用返回 `ENGINE_GAP`；安全拒绝不再伪装成学生错误；`equivalence_conclusion` 仍为 NOT_EQUIVALENT、NO_COUNTEREXAMPLE_FOUND 或 UNDECIDED。 |
| API 权威决策 | `sql-edu-backend/routers/ai.py` 的 `_authoritative_phase1_decision` | 对外映射为 `CORRECT`、`WRONG`、`UNDECIDED`，并保留反馈降级信息。 |
| Freeze runner | `data_construct_test/scripts/run_phase1_freeze_verification.py` | 聚合 family scope、verdict、mismatch、generation failure 和 repeat stability。 |

### 9.2 自动化测试

- `tests/test_phase1_gold_oracle.py`：Gold Oracle 结果优先级、输入/执行 gap、有限匹配不晋升为等价。
- `tests/test_phase1_dialect_semantics.py`：生产 rich verdict 与方言边界。
- `tests/test_phase1_acceptance_and_freeze.py`：统计报告、分区泄漏和 freeze selector。
- `tests/test_check_sql_flow.py`、`tests/test_phase1_diff_gate_regressions.py`：API/判题链路兼容性。

### 9.3 当前限制和已知失败

Gold Oracle 的关键语义是：

- 找到有限反例：`NOT_EQUIVALENT`；
- 没有反例但没有可信 expected equivalent：`UNDECIDED`；
- 只有显式 `expected=EQUIVALENT` 且 bounded worlds 全部一致时才返回 `EQUIVALENT`；
- 不可用原生引擎：`ENGINE_GAP`；schema/query 无法重放：`INPUT_GAP`。

生产层和 Gold 层的字段/状态不是同一枚举，但已明确主要边界映射。特别是：

```text
Gold: INPUT_GAP / ENGINE_GAP
Production: KNOWN_GAP / ENGINE_GAP / INPUT_GAP + equivalence_conclusion
API: CORRECT / WRONG / UNDECIDED
```

`UNSUPPORTED` feature 和 `SECURITY_REJECTED` 统一映射为生产 `KNOWN_GAP`；原生引擎不可用（含 `ENGINE_ERROR`/`TIMEOUT`）映射为 `ENGINE_GAP`，schema/query 无法重放是 `INPUT_GAP`。只有 `SUPPORTED + NOT_EQUIVALENT + judge_status=WRONG` 才能形成可教学 WRONG；如果只看到 `is_equivalent` 或 `CORRECT/WRONG`，仍可能丢失“没有证据”和“执行器不可用”的原因。

### 9.4 可复现证据和状态

```bash
cd sql-edu-backend
PYTHONPATH=. pytest -q tests/test_phase1_gold_oracle.py tests/test_phase1_acceptance_and_freeze.py
```

v16 hidden freeze 工件：

```text
data_construct_test/outputs/phase1_corpus_universe_dev_v8/final_hidden_freeze_verification_v16.json
```

其关键结果为：5,846 hidden family；5,839 进入当前声明范围；7 generation failures；22 determinate label mismatches；repeat stable=true；`acceptance.pass=false`。这是真实失败证据，不能写成 Phase 1 已冻结通过。

当前状态：内部 verdict 结构 `IMPLEMENTED`；Gold/production/API 统一契约 `UNDECIDED`；具体不可用执行器 `ENGINE_GAP`；有限证据不足 `UNDECIDED`。在线公开 SQL smoke builder 的候选解析超时已 fail-closed，不会把外部数据源的 pathological SQL 当作产品能力或让构建任务无限等待。

## 10. Freeze scope 的真实含义

当前 `run_phase1_freeze_verification.py:261-275` 的 family scope 主要由以下条件构成：

```text
能生成 mutation
且能生成 equivalence control
```

它没有同时要求 parser/IR、schema replay、witness validator 成功、engine available 和资源状态全部通过。因此当前必须分开记录：

```text
FrozenPairScope ⊆ RunnableScope ⊆ PolicyScope
```

其中：

- `PolicyScope`：产品/课程允许检查的输入集合；
- `RunnableScope`：解析、IR、schema、witness、执行器和资源条件都能运行的集合；
- `FrozenPairScope`：freeze runner 实际生成并保存 mutation/equivalence pair 的集合。

在这三个集合由同一机器可读契约生成前，不能用 `families_in_declared_supported_scope` 代替“完整可运行支持范围”。

## 11. 盘点结论

当前代码已经形成可运行的 Phase 1 MVP 主链路，但七个能力域尚未达到“契约、形式化、实际代码完全统一”：

1. parser/IR/witness 组件是真实实现，但 CFG 和有限测试不能推出全局语言完备。
2. schema catalog 和原生 runner 已存在，但 legacy/schema gap 和 backend fallback 的状态映射不一致。
3. 查询级资源限制和按请求的任务级硬超时/私有 process group 强杀/内存隔离已实现；跨实例调度和真实 OOM 演练仍未完成。
4. verdict 内部结构已存在；失败投影已由 `core/phase1_verdict.py` 统一，但 Gold、生产和 API 三层字段尚未由同一生成器完整生成。
5. v16 freeze 稳定但 `acceptance.pass=false`，因此当前总状态不能标成 `VERIFIED`。

后续应继续以机器可读“当前实现契约”为唯一状态来源，逐项补充公开证据和实现；产品希望但尚未完成的能力另列为 `TARGET`，不可通过缩小契约来消除现有失败。
