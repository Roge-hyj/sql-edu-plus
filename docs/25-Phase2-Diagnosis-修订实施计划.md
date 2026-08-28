# Phase 2 Diagnosis 修订实施计划

> 文档状态：Phase 2 有界 MVP 已通过冻结验收  
> 审计与实施日期：2026-08-24  
> 适用基线：Phase 1 rich verdict、stable diff/obligation ID、scope metadata、Witness World、Mutation Evidence 与 `AttributionResult`  
> 当前结论：`PHASE2_MVP_ACCEPTED / PHASE1_GLOBAL_ACCEPTANCE_OPEN`。Phase 2 只在声明的 20 条规则与证据契约内完成，不构成对任意 SQL 的全局完备性声明。

## 1. 结论与修订边界

Phase 2 的目标应保持为：把 Phase 1 的多源证据转换成可审计、可排序、可抑制伴随现象、且不会泄露答案的诊断包。

原规划中“六阶段教学管道、错因打包、最早偏离点、物理反例、三段式教学链”这五个方向可以保留，但需要做以下根本修订：

1. **Phase 1 是唯一判决源**。LLM 不得重新判定 SQL 正误，也不得把 `UNDECIDED` 改成正确或错误。
2. **判决必须使用 rich verdict**。`SandboxRun.is_equivalent` 只是当前有界世界的兼容观测，不是最终等价结论。
3. **六插槽必须升级为作用域图**。CTE、派生表、相关子查询、集合分支分别拥有自己的查询管道，不能被压平成一条全局链。
4. **规则清单实际是 20 条，不是 18 条**。它们只是 MVP 教学规则集，不代表 SQL 错误的全覆盖。
5. **FDP 必须建立在因果 DAG 上**。发现早期候选后不能直接停止扫描，只能抑制有明确因果关系的伴随现象。
6. **Witness 必须是 Minimal Witness Slice**。不得把整套测试数据库或任意结果样本直接塞入反馈，也不得在证据不足时生成“看似具体”的虚构物证。
7. **内部诊断与公开输出必须分层**。标准 SQL、标准 AST 片段、内部 mutation SQL 和完整 witness world 永远不能进入学生可见包。
8. **正确提交、确定错误、未决与平台故障必须四路分流**。`UNDECIDED` 不是错误提交，也不能计入失败次数、BKT 错误观测或挫败度。

因此，Phase 2 的声明范围应是：

> 在 Phase 1 已给出确定判决、且证据链可追踪的查询对上，对 MVP 规则集内的错误进行作用域感知的因果归因与教学打包。

不得再声明“任何合法 SQL 都能映射为一条完整且可诊断的六阶段管道”。更准确的说法是：当前系统在 Phase 1 声明的 `L_struct`、`L_scope` 与 `L_exec` 有界能力域内，构建可扩展的教学逻辑图。

## 2. 当前仓库基线与实施状态

### 2.1 已有能力

当前仓库已经具备 Phase 2 可以复用的基础：

- `core/ast_schema.py` 已有 `SQLStructureIR` 和 `ASTDiffNode`；
- `core/parseval_data_generator.py` 已有 `SandboxRun`、rich verdict、stable `diff_id`、obligation、multi-world witness、mutation evidence 和 `obligation_effectiveness`；
- `core/error_attribution.py` 已能汇总 `E_AST`、`E_data`、`E_MUT`，输出 `AttributionResult` 与 `llm_arbitration_input`；
- `docs/sql_error_clustering_query_path_bundling.md` 已确定 Query Path Bundling 位于 Phi Arbiter 之后、BKT/ActionSelector 之前；
- `docs/07-具体化系统闭环控制流图.md` 已约定 Phase 2 外观模块为 `core/error_diagnosis.py`，并约定 `diagnose_record` / `diagnose_record_with_llm` 两个入口。

### 2.2 当前实施闭环

截至本轮实现，仓库中的实际状态如下：

| 项目 | 当前状态 | 说明 |
|---|---|---|
| Phase 1 rich verdict | 已实现 | `status`、`equivalence_conclusion`、`judge_status` 已存在 |
| 路由正确消费 rich verdict | 已实现 | API 只接受受支持的 `WRONG + NOT_EQUIVALENT` 或满足严格门禁的 operational acceptance；`UNDECIDED` 返回 422 且不写学习状态 |
| stable diff/obligation ID | 已接通 | `Phase1EvidenceAdapter` 优先消费 `data_evidence.ast_diffs`，并跨 AST、mutation、effectiveness、bundle 保留稳定 ID |
| Rich witness evidence | 已接通并分层 | 内部消费 selected world、obligation effectiveness 与 constraint row index；公开包只输出有界最小切片 |
| Question/QSS 输入 | 已实现 | 路由传入本地化题意、公开 Schema 与学生 SQL；QSS 显式标记 Question、SchemaCatalog 与 Phase 1 Evidence 来源 |
| SchemaCatalog | 已实现 | 有界适配 dict/JSON、在线 preview、Phase 1 catalog 与 Spider 索引形式；保留 type/nullable/PK/FK/unique，提供 bridge/path/cardinality/fan-out 查询，缺失事实明确降级为 `STRUCTURE_ONLY/UNKNOWN` |
| `core/error_diagnosis.py` + `core/llm_teaching.py` | 已实现 | 确定性诊断先产出版本化候选；受限 LLM 可在已有 strong candidate/evidence 集合内复核排序并润色三段式话术，失败回退确定性结果 |
| Phase 1 scope contract / ScopedQueryGraph | 已实现 | Phase 1 输出双侧 scope、精确 conceptual pairing、parent 与 CTE/derived/correlation/set 显式边；Phase 2 构建有界 14 阶段图并做稳定拓扑排序。证据不足时保留 `PARTIAL`，不按名称猜边或串 scope |
| 20 条 MVP 规则目录 | 已实现并建立冻结矩阵 | 20 个稳定 rule ID 与 `phase2.rules.mvp20.v1` 目录一一对齐；每条均有支持例、邻近反例和证据不足例 |
| Causal DAG / FDP | 已实现 | 全量扫描、可验证因果根、secondary root、suppressed/unresolved 分流已落地；只有显式 `CAUSES/MASKS/RELOCATES_TO` 可抑制非独立伴随现象 |
| Minimal Witness Slice | 已实现 | 通过 `diff/obligation/world/row_index` 抽取有界物证；链路不完整时降级而不编造 |
| Internal/Public 包 | 已实现 | 内部包保留有界 graph/catalog、candidate trace、causal DAG 与 sanitizer report；学生包递归剔除参考 SQL/AST、mutation SQL、完整数据库和 raw attribution |
| 公开安全契约 | 已实现 | Public DTO 不返回 `correct_sql`；Schema Preview 校验关系并净化示例值；DiagnosticPackage 递归删除参考 SQL/AST、mutation SQL、完整 witness world 和 raw attribution |
| 离线冻结验收 | 已通过 | 静态白名单覆盖 Phase 1 scope contract、SchemaCatalog、ScopedQueryGraph、20-rule 三态矩阵、诊断与公开契约、纯路由门禁；最终计数见第 14.8 节 |

**当前总状态：`PHASE2_MVP_ACCEPTED / PHASE1_GLOBAL_ACCEPTANCE_OPEN`。** 前半句只表示本文声明的 Phase 2 有界诊断 MVP 已闭环并通过冻结门禁；不表示 20 条之外的高级 SQL 规则已覆盖，也不表示 Phase 1 已全局验收。

### 2.3 本轮已落地的安全边界

- Phase 1 仍是唯一正误判决源，Phase 2 的受限 LLM reviewer 和 Phase 5 renderer 都不能改写 verdict；LLM 只能在已有 strong candidate 集合内有限排序；
- `PAIR_DISTINGUISHED` 只能证明查询对不同，不能自动升级为某个原子错因；只有因果或修复验证证据可成为 blocking root；
- FDP 来自全量候选扫描，早期错误不会停止后续独立根因发现；
- 完整 side-aware ScopedQueryGraph 只进入内部审计包；学生管道只接收已证明的 side-neutral conceptual scope，配对不足的 diff 保留为 unscoped；
- 学生公开题目与诊断响应均经过答案泄漏门禁；
- LLM 生成的 `schema_preview` 只保留白名单结构；表列名采用严格标识符格式，所有自由文本示例单元格降为 `[text]`，不允许携带答案表达；
- Public Schema Preview 已阻断 SQL 形态、分隔符编码和自由文本单元格，但若上游 LLM 可任意创造标识符且未与权威 DDL 做交集校验，仍存在通过“非 SQL 形态标识符”做隐蔽编码的理论风险；当前冻结结论是有界工程门禁，不是形式化零泄漏证明。
- 所有数组、字符串、物证行和递归清洗都有显式上界；学生 SQL 上限 32,768 字符，Phase 1 在线工作限制为 2 个并发槽、5 秒排队和 45 秒请求等待，超时线程结束前不会释放槽位；Phase 2 自身不执行 SQL，也不加载本地大模型，而是可选调用受限的 OpenAI-compatible 外部 LLM，并以确定性证据门禁和模板回退为准。

### 2.4 MVP 边界与后续扩展

以下项目不再是 Phase 2 有界 MVP 的阻断项，但必须作为后续扩展记录，不得被“Phase 2 完成”的表述隐去：

1. WINDOW、QUALIFY、复杂 set operation、递归 CTE、LATERAL 与复杂相关子查询仍需新的教学规则；当前图会保留这些结构或降级为 `EXTENSION/UNCLASSIFIED_SUPPORTED_DIFF`，不伪装成 MVP 标签。
2. 老题目若只有列名而没有声明 PK/FK/unique/nullable，SchemaCatalog 会如实标记 `STRUCTURE_ONLY/UNKNOWN`；这是输入证据边界，不是推断失败。
3. 冻结门禁是无数据库、无网络的纯 Phase 2 验收；完整 `test_check_sql_flow.py` 和真实数据库/引擎回归属于独立系统验收层，最终结果必须单独记录。
4. 公开评测集校准、内部审计包持久化、Phase 3 BKT/认知负荷/ActionSelector 接入是下一阶段工作，不反向扩大 Phase 2 的完备性声明。

### 2.5 与 Phase 1 冻结结论的严格分离

Phase 2 完成不等于 Phase 1 全局验收。当前 Phase 1 v16 冻结记录是：5,846 个 hidden 家族中 5,839 个进入声明支持范围，scope coverage 为 99.8803%；仍有 22 个确定性标签 mismatch 与 7 个 parser/input gap，因此 `acceptance.pass=false`。这些是已冻结的历史边界，本阶段不将它们改写为通过，也不把 Phase 2 门禁的 PASS 解释成 Phase 1 的 PASS。

## 3. 修订后的端到端架构

```text
Phase 1
  SQLStructureIR
  + DiffGraph (stable diff_id)
  + SandboxRun rich verdict
  + Witness/Obligation evidence
  + Mutation evidence
  + AttributionResult
        |
        v
P0 Verdict Gate（纯确定性）
  ├─ CORRECT / NO_COUNTEREXAMPLE_FOUND -> Correct DiagnosticPackage
  ├─ WRONG / NOT_EQUIVALENT            -> Phase 2 Diagnosis
  ├─ UNDECIDED / KNOWN_GAP / BOUNDARY  -> Undecided，不进行教学归因
  └─ ENGINE/INPUT/SECURITY/SYNTAX       -> 对应专用出口
        |
        v
Phase 2 Diagnosis
  Phase1EvidenceAdapter
    -> ScopedQueryGraph
    -> 20-rule Candidate Detection
    -> Causal DAG + Fault Bundling
    -> FDP / Secondary Roots
    -> Minimal Witness Slice
    -> InternalDiagnosticPackage
    -> Public Sanitizer
    -> Public DiagnosticPackage（Phase 3 唯一输入）
```

Phase 3 只能接收经过 Public Sanitizer 的 `DiagnosticPackage`，不得重新读取 `correct_sql`、原始标准 AST、完整 witness database 或内部 mutation SQL。

## 4. Rich verdict 与 LLM 权限

### 4.1 判决状态机

Phase 2 入口必须依据 `SandboxRun.equivalence_conclusion` 和 `judge_status`，不能依据单独的 `is_equivalent`：

| Phase 1 结论 | Phase 2 行为 | 是否记错误提交 | 是否更新 BKT |
|---|---|---:|---:|
| `NO_COUNTEREXAMPLE_FOUND` 且 `judge_status=CORRECT` | 签发正确包，跳过错误诊断 | 否 | 可记录已掌握观测 |
| `NOT_EQUIVALENT` 且 `judge_status=WRONG` | 进入错误诊断 | 是 | 只更新被确定归因的知识点 |
| `UNDECIDED` | 返回未决状态，不做教学错因断言 | 否 | 否 |
| `ENGINE_GAP` / `INPUT_GAP` / `SEMANTIC_BOUNDARY` / `KNOWN_GAP` | 返回平台或能力边界 | 否 | 否 |
| 语法错误 | 保留专用语法反馈出口 | 可单独统计 | 不进入语义规则集 |
| 安全拦截 | 保留专用安全出口 | 否 | 否 |

现有 `Submission.is_correct: bool` 无法表达未决。P0 的最小安全策略是：**未决不落为错误 submission，也不进入失败计数**。如后续需要完整审计历史，再增加独立的 `judge_status` / `equivalence_conclusion` 持久化字段，不能用 `False` 代替未决。

### 4.2 LLM 可以做什么

LLM 只允许：

- 在确定性规则已经生成、且有 strong evidence 支撑的候选标签中做受限排序/选择；
- 把结构化 QSS 和 witness facts 渲染成自然语言；
- 在不改变事实与标签的前提下完成多语言表达；
- 当输出未通过结构校验或泄漏校验时被确定性模板替代。

LLM 禁止：

- 修改 Phase 1 verdict；
- 将 `UNDECIDED` 签成正确或错误；
- 新增没有规则 ID 和证据 ID 支撑的错因；
- 虚构 witness tuple、Schema 约束或题目意图；
- 删除一个具有独立因果证据的错误；
- 输出标准 SQL、标准 AST 片段、修复后完整 SQL；
- 直接决定 BKT 的 True/False 观测。

`diagnose_record` 仍负责确定性诊断；`core/llm_teaching.arbitrate_phase2_evidence` 负责异步 provider 复核。既有 `diagnose_record_with_llm` 名称保留为同步 renderer 兼容 facade，不能表示“LLM 自由判定对错”。

## 5. P0：判决与证据契约修复

P0 的目标不是增加规则，而是保证 Phase 2 不建立在错误或断裂的证据链上。

### P0.1 修复在线判决分流

- 路由不得用 `sandbox_run.is_equivalent` 生成最终 `is_correct`；
- 路由不得在 attribution 后覆盖 Phase 1 的 `UNDECIDED`；
- `NOT_EQUIVALENT` 才能进入语义错误诊断；
- `UNDECIDED` 不保存为失败、不发放经验、不生成“你错在……”文本；
- Phase 2 异常不得反向改变 Phase 1 判决：诊断失败时仍返回原判决和稳定降级提示。

### P0.2 建立 `Phase1EvidenceAdapter`

统一输入至少包含：

- `status`、`equivalence_conclusion`、`judge_status`；
- `diff_id`、`obligation_id`、`query_scope`、`subquery_depth`；
- AST diff 的 clause、diff type、KP、严重度和安全化结构摘要；
- mutation test 的 `diff_ids`、replacement 结果和 causal fix 标志；
- `selected_witness_world_id`、`obligation_effectiveness`；
- `only_in_standard_sample`、`only_in_student_sample`、列信息和行数信息；
- 当前 selected world 的 bounded `test_database`；
- 题目 Q、方言、SchemaCatalog、输出列要求。

路由必须使用 `sandbox_run.data_evidence["ast_diffs"]` 或等价的稳定序列化接口，不能再用丢失 ID 的普通 AST diff 字典作为跨证据主键。

### P0.3 接通权威 SchemaCatalog

- 将 `schema_preview` 的字符串列和对象列统一转换成规范 catalog；
- 保留 `data_type`、`nullable`、PK、FK、unique；
- 继续兼容旧题目只有列名的情况，但标注 `schema_confidence=STRUCTURE_ONLY/UNKNOWN`；
- Missing Bridge 只能在声明 FK 证明中间桥表路径时命名，否则保留 `UNCLASSIFIED_SUPPORTED_DIFF`；fan-out 在没有声明 1:N 关系时必须有直接行倍增/聚合差证据，不能从表名或列名猜测。

### P0.4 建立版本化数据类型

当前 MVP 按以下外观落地：

```text
core/error_diagnosis.py          # facade、evidence adapter、rules、DAG、witness、package
core/phase2_schema_catalog.py    # 有界 SchemaCatalog
core/scoped_query_graph.py       # 有界 ScopedQueryGraph
core/public_schema_preview.py    # 学生可见 Schema 净化
routers/ai.py                    # rich verdict 分流与公开诊断包路由
schemas/agent.py                 # 现有 SQLCheckResult 公开响应外观
```

公开函数签名与 `phase2.public.v1`、`phase2.internal.v1`、`phase2.rules.mvp20.v1` 版本已固定。后续若将 `error_diagnosis.py` 拆包，不得改变这些外观契约。

### P0.5 公共泄漏门禁

- 学生响应不得含 `answer_sql`、`correct_sql`、`standard_sql`、`standard_node`、mutation SQL；
- raw `observation` 与 raw `error_attributions` 只允许教师/调试权限获取；
- 当前题目公开 Schema 中若仍向学生返回 `correct_sql`，必须在 Phase 2 上线前另行修正，否则“诊断不泄漏答案”的系统承诺不成立；
- 对公开包进行递归 key/value 泄漏扫描，而不是只检查最终 hint 文本。

### P0 验收门禁

P0 完成必须同时满足：

1. 构造 `KNOWN_GAP + is_equivalent=True` 时，API 仍返回 `UNDECIDED`，绝不返回正确；
2. `UNDECIDED` 不增加失败次数、不更新 BKT、不写错误诊断；
3. 一个 diff 在 AST、obligation、witness、mutation、bundle 中保持同一 stable ID；
4. Phase 2 不可用时，Phase 1 判决仍可稳定返回；
5. 公共响应不包含任何标准 SQL 片段；
6. 旧客户端仍能读取 `is_correct`、`hint`、`submission_id` 等既有字段；新增 `diagnostic_package` 采用可选、可版本化字段。

## 6. P1：ScopedQueryGraph

### 6.1 为什么不能使用一条平坦六槽链

以下结构都不能被安全压平：

- CTE 自身包含完整 SELECT 管道；
- `WHERE`、`SELECT`、`FROM` 中的子查询具有不同作用域；
- 相关子查询存在外层列依赖边；
- `UNION` / `INTERSECT` / `EXCEPT` 连接多个查询分支；
- WINDOW 与 `QUALIFY` 位于普通六槽模型之外；
- 递归 CTE、LATERAL、PIVOT、层次查询具有特殊数据流。

因此，六槽应作为每个 query block 的**教学兼容视图**，而不是整个 SQL 的唯一结构。

### 6.2 图模型

已落地的内部数据结构为：

```text
ScopedQueryGraph
  status: COMPLETE | PARTIAL
  scopes[]
    QueryScopeNode
      scope_id
      scope_kind: ROOT | CTE | DERIVED | SUBQUERY | SET | SET_BRANCH | UNKNOWN
      side: standard | student
      conceptual_scope_id?
      metadata_complete
      stages[14]
  parent_edges[]
  composition_edges[]
    edge_type: CTE_FEEDS | DERIVED_FEEDS | SUBQUERY_OF |
               CORRELATED_TO | SET_MEMBER_OF | LATERAL_TO
  conceptual_bindings[]
  limitations[] / counts / truncated
```

Phase 1 双侧 `scope_id` 永不直接合并。只有两侧 AST 结构路径和 scope kind 能够精确配对时，diff 才取得 side-neutral `conceptual_scope_id`；否则保留为 unscoped/`PARTIAL`，防止同名 CTE 或嵌套子查询被误合并。

每个 `QueryScopeNode` 内保留六个教学主阶段：

| 阶段 | 教学含义 | 典型结构 |
|---|---|---|
| S1 | 数据来源与关系 | FROM、JOIN、ON、CTE/派生表引用 |
| S2 | 行级过滤 | WHERE、行级 predicate |
| S3 | 数据粒度与折叠 | GROUP BY、grouping grain |
| S4 | 组级过滤 | HAVING、aggregate filter placement |
| S5 | 计算、投影与集合语义 | SELECT、aggregate、CASE、window、QUALIFY、DISTINCT 子阶段 |
| S6 | 最终整理与截断 | ORDER BY、LIMIT、OFFSET、FETCH、TOP |

高级结构通过显式子阶段或 composition node 表示：

- `WINDOW`、`QUALIFY` 在 S5 中保持独立 `substage` 和确定顺序；
- set operator 连接各 branch 的输出，再进入根结果的 S6；
- CTE/derived/subquery 的内部逻辑是独立 scope，外层 S1 只保存引用关系；
- 无法安全归入六槽的结构进入 `EXTENSION`，不得静默丢弃或错误塞入 S1。

### 6.3 Ordered Diff 序列化

`Ordered_Diff_Pipeline` 是 ScopedQueryGraph 的学生可见投影。作用域先按显式生产者→消费者边做稳定拓扑排序（递归自环不阻塞投影），再按以下键排序：

```text
(scope_topological_order,
 stage_order,
 diff_type,
 diff_id)
```

它必须满足：

- 同一输入重复运行顺序完全一致；
- diff 原始列表换序不改变输出；
- 不同 query scope 的同名 WHERE/GROUP BY 不被合并；
- 未识别 diff 保留在 `UNMAPPED/EXTENSION` 并附边界原因；
- 每个 ordered item 都保留原始 `diff_id` 和证据引用。

## 7. 20 条 MVP 教学规则

原规划列出的数量为 `4 + 4 + 3 + 3 + 4 + 2 = 20`。Phase 2 v1 应明确称为 **20 条 MVP 规则**，不能称“18 种全覆盖教学规则”。

| Rule ID | 阶段 | 教学错因 | MVP 最低证据要求 | 边界说明 |
|---|---|---|---|---|
| `S1_MISSING_BRIDGE` | S1 | 关联路径断裂 | join/from diff + SchemaCatalog 声明 FK 明确证明端点间存在中间桥表路径 | 无权威 FK 桥路径时不命名 Missing Bridge，保留 `UNCLASSIFIED_SUPPORTED_DIFF` |
| `S1_CARTESIAN_PRODUCT` | S1 | 笛卡尔积失控 | 缺连接约束 + 行倍增 witness | 显式且题意需要的 CROSS JOIN 不是错误 |
| `S1_OUTER_JOIN_MISUSE` | S1 | 外连接语义偏差 | join type diff + dangling tuple witness | 必须区分保全侧 |
| `S1_SUBQUERY_CARDINALITY` | S1 | 集合/标量错配 | scalar-vs-set shape + 多行或执行错误证据 | 不能只凭 `=` 和子查询文本猜测必然多行 |
| `S2_BOUNDARY` | S2 | 临界值边界偏差 | comparison operator diff + boundary witness | 包含日期、数值和聚合外层比较 |
| `S2_BOOLEAN_LOGIC` | S2 | 复合布尔逻辑错误 | typed logic-tree diff + truth-assignment witness | 不以字符串括号数量判定 |
| `S2_NULL_LOGIC` | S2 | NULL/三值逻辑陷阱 | Phase 1 `null_predicate_negation_changed` 或已证明的 missing `IS NULL` branch + NULL witness | 包含 NOT IN、否定比较等子类；只有普通 predicate diff 不足以命名 |
| `S2_AGGREGATE_IN_WHERE` | S2 | 聚合过滤前置 | aggregate placement diff 或引擎错误 | 与合法标量聚合子查询区分 |
| `S3_GRAIN_ENTITY_MISMATCH` | S3 | 分组实体错位 | group diff + Q/Schema 实体证据 + cardinality witness | LLM 题意抽取不能单独定罪 |
| `S3_GROUP_KEY_MISSING` | S3 | 分组维度缺失 | `grouping_grain_too_coarse` + projection/结果证据 | 要考虑函数依赖和方言严格模式 |
| `S3_GROUP_KEY_REDUNDANT` | S3 | 分组维度冗余 | `grouping_grain_too_fine` + split witness | 若新增键函数依赖于主键，可能语义等价 |
| `S4_HAVING_MISSING` | S4 | 组级约束缺失 | HAVING diff + group boundary witness | 不把普通 WHERE 缺失误归到 HAVING |
| `S4_AGG_BOUNDARY` | S4 | 聚合统计边界偏差 | aggregate comparison diff + group-size witness | 与 S2 boundary 使用不同 scope/stage |
| `S4_ROW_FILTER_IN_HAVING` | S4 | 行过滤后置 | Phase 1 `where_changed` + `having_changed`，且在同一 conceptual scope 中配对到同一 predicate | 语义等价或 Phase 1 为 `UNDECIDED` 时不得成为 blocking root |
| `S5_FANOUT_AGGREGATE` | S5 | 关联导致度量虚高 | Phase 1 `aggregate_distinct_changed` + JOIN，并且有 SchemaCatalog 声明 1:N 关系或直接行倍增/聚合 delta 证据 | 没有 cardinality 声明也没有直接证据时不命名；修复不一定是 DISTINCT，也可能是删除错误 JOIN 或预聚合 |
| `S5_COUNT_NULL_SENSITIVITY` | S5 | COUNT 空值敏感度错用 | COUNT shape + nullable Schema + NULL witness | 题目意图必须区分“行数”和“非空值数” |
| `S5_CASE_INCOMPLETE` | S5 | CASE 分支覆盖不全 | CASE diff + uncovered branch witness | 缺 ELSE 不总是错误，题意可能允许 NULL |
| `S5_TOP_LEVEL_DEDUP` | S5 | 顶层去重遗漏 | DISTINCT diff + duplicate output witness | GROUP BY 已保证唯一时不得重复报错 |
| `S6_TOPN_WITHOUT_ORDER` | S6 | 未决排序后截断 | LIMIT/FETCH + 无有效结果排序 | ORDER key ties 还需检查次级稳定排序 |
| `S6_ORDER_OFFSET` | S6 | 排序方向/偏移偏差 | Phase 1 `order_by_changed` 或 `limit_changed` + cutoff witness；真实 OFFSET 差异由 `limit_changed` 适配 | “第 N 高”必须先定义 ties 语义，不能从 OFFSET 字面单独断言 |

### 7.1 “MVP 非全覆盖”的强制声明

下列 Phase 1 已识别能力不能被这 20 条静默降级：

- set operator 类型与 ALL/DISTINCT 语义；
- window partition/order/frame 与 `QUALIFY`；
- CTE/递归 CTE 的 base/step/termination；
- correlated subquery key、EXISTS/IN、NULL-sensitive antijoin；
- alias、函数参数、正则/LIKE/GLOB/SIMILAR、日期和方言语义；
- hierarchical query、PIVOT/UNPIVOT、TABLESAMPLE、LATERAL 等扩展。

这些差异应进入 `EXTENSION_RULE` 或 `UNCLASSIFIED_SUPPORTED_DIFF`，保留 Phase 1 证据和边界状态，不得错误套用最相近的 MVP 标签。

## 8. Causal DAG、Fault Bundling 与 FDP

### 8.1 两趟算法不再“早停”

第一趟仍应扫描所有 scope 和阶段，生成候选错误。第二趟构建因果关系并压缩输出，但**不得因 S1 命中就跳过后续扫描**。

原因是同一提交可能同时包含两个独立错误，例如错误 JOIN 与错误 ORDER BY。它们在教学上可以有主次，但后者不能被伪装成前者的派生现象。

### 8.2 Causal DAG

每个候选错误节点至少包含：

- `candidate_id`、`rule_id`、`scope_id`、`stage`；
- `diff_ids`、`obligation_ids`、`mutation_test_ids`；
- `confidence`、`severity`、`evidence_grade`；
- `blocking`、`independent`、`boundary_reason`。

边类型至少包含：

- `CAUSES`：上游错误有证据造成下游差异；
- `SUPPORTS`：证据增强某候选，但不是因果关系；
- `RELOCATES_TO`：同一条件在 WHERE/HAVING/ON 等位置移动；
- `AMPLIFIES`：JOIN fan-out 放大 S5 度量错误；
- `MASKS`：空结果或执行错误遮蔽了下游可观测性；
- `CO_OCCURS`：共同出现但没有足够因果证据，禁止据此抑制。

### 8.3 允许抑制的条件

一个候选只能在以下条件同时成立时降级为伴随现象：

1. 与根因有明确的 DAG 边；
2. 边由 stable diff/obligation/mutation 证据支持，而不是仅按阶段先后推断；
3. 下游候选没有独立 causal witness；
4. 抑制原因被结构化记录，能够在内部审计中恢复。

典型规则：

- 缺失桥表可抑制由不可达表引起的字段/投影伴随差异，但不能抑制独立 ORDER BY 方向错误；
- WHERE 导致空结果只能建立 `MASKS`，不能自动证明 S3-S6 都正确或都错误；
- GROUP BY 粒度错误可与依赖它的非聚合投影合并；
- WHERE 缺失与 HAVING 多余若绑定到同一 predicate，合并为 relocation bundle；
- JOIN multiplicity 与 COUNT/SUM 异常通过 `AMPLIFIES` 连接，修复建议由两侧证据共同决定；
- ORDER BY 与 LIMIT 的 cutoff 错误可合并，只有排序错误时才抑制“LIMIT 结果不一致”这一伴随描述。

### 8.4 FDP 定义

FDP 不再等于“第一个出现的 stage”。修订定义为：

> 在当前 query scope 的 causal roots 中，按教学逻辑阶段、因果证据等级、mutation 修复性与严重度排序后选出的首要根节点。

输出应同时保留：

- `primary_fdp`：本轮优先教学的根因；
- `secondary_roots`：独立但本轮不优先展开的错误；
- `suppressed_symptoms`：已证明依赖于根因的伴随现象；
- `unresolved_candidates`：证据不足，不能断言也不能抑制的候选。

既有文档中的 `PEDAGOGICAL_PRIORITY` 和 `E_AST=1.0 / E_MUT=0.9 / E_data=0.78` 可以作为初始排序常量，但这些权重只能排序，不能创造事实。`causal_attribution_verified`、`fixed_by_replacement` 等验证标志必须先作为门槛，再应用权重。

## 9. Minimal Witness Slice

### 9.1 目标

Minimal Witness Slice 不是“截取结果前两行”，而是从 Phase 1 已验证的 witness world 中提取能够解释主错因的最小事实集合。

默认边界：

- 最多 2 个 logical witness case；
- 每个 case 只包含完成证明所需的表；
- 每张表只保留相关主/外键、predicate、group/order/aggregate 列；
- 默认最多 8 个单元格字段，单字符串截断到安全长度；
- 只保留最小 output delta，不返回整张测试表。

多表 JOIN 的一个 logical case 可以包含多个表各一行；“1～2 条物证”指 1～2 个最小案例，而不是强迫跨三张表的证据只能保留两张物理行。

### 9.2 抽取算法

1. 从 `primary_fdp.diff_ids` 找到对应 obligation；
2. 优先选择 `causal_attribution_verified=true` 的 effectiveness；
3. 校验 effectiveness 的 `world_id` 与 selected world 数据是否一致；
4. 从 `constraint_application.applied` 取得与 diff/obligation 绑定的 `table + row_index + column`；
5. 在 selected `test_database` 中取出相应行，并根据 SchemaCatalog 补充最小连接键；
6. 绑定 `only_in_standard_sample` / `only_in_student_sample` 或 atomic mutant delta；
7. 输出“题意期望行为 vs 学生实际行为”的事实描述，不输出标准 SQL；
8. 若 world、row index 或 diff ID 无法可靠连接，返回 `UNAVAILABLE`，绝不退化为 LLM 编造。

### 9.3 证据等级

```text
CAUSAL_VERIFIED     原子 diff 的义务和 witness 均验证成功
PAIR_DISTINGUISHED  查询对已被真实数据区分，但原子因果未完全验证
OUTPUT_ONLY         只有结果 delta，无法定位物理输入行
UNAVAILABLE         没有安全、可追踪的物证
```

公开话术必须明确区分这些等级。只有存在真实数据时才能使用“测试数据中有……”的确定表达；`OUTPUT_ONLY` 只能描述结果差异；`UNAVAILABLE` 只能给概念性引导。

## 10. QSS 与三段式教学链

### 10.1 QSS 修订

- **Q — Question intent**：来自题目文本、教师元数据和确定性关键词抽取；LLM 只能生成候选，必须与参考行为及证据一致。
- **S — Schema facts**：只使用声明的列类型、nullable、PK/FK/unique；启发式推断必须标注置信度。
- **S — Student behavior**：只描述学生 SQL 和 Phase 1 的实际执行行为。

QSS 中不得把参考 SQL 的具体写法伪装成“题目唯一意图”。当题目文本本身存在歧义时，应输出 `QUESTION_AMBIGUITY` 边界，而不是强行诊断学生。

### 10.2 三段式规范

1. **What Student Did**：客观描述学生在当前 scope/stage 的数据变换；可以引用学生自己写出的结构。
2. **Conflict & Witness**：只使用 Public 包中的 Schema fact 和 Minimal Witness Slice，说明实际冲突。
3. **Target Mental Guidance**：用概念性问题引导自查，不得给出标准 SQL、完整修复片段、参考字段名或答案字面量。

三段都必须由结构化字段生成并经过 validator：

- 第三段必须是启发式问题或方向性提示；
- 不得出现完整 `SELECT ...` 修复代码；
- 不得出现仅存在于标准 SQL、而学生 SQL 和公开 Schema 中没有的秘密片段；
- LLM 输出校验失败时使用确定性多语言模板降级。

## 11. Internal / Public 包契约

### 11.1 `InternalDiagnosticPackage`

内部包用于 Phase 2 审计，已包含：

- `schema_version`、`diagnosis_version`；
- 原始 Phase 1 verdict；
- ScopedQueryGraph；
- 全部 candidates 和 causal DAG；
- primary/secondary/suppressed/unresolved；
- stable evidence references；
- Minimal Witness Slice 及证据等级；
- rule trace、排序分数、边界原因；
- sanitizer report。

当前内部包仍然是有界审计形式：它通过 stable evidence reference 指向上游，不复制完整标准 SQL 或完整 witness database。若未来要做完整重放，必须在另行的权限和持久化设计中对标准侧原始片段标记 `INTERNAL_SECRET`，不能扩展当前学生响应。

### 11.2 `PublicDiagnosticPackage`

这是 Phase 3 的唯一输入，也是学生 API 可返回的结构。下例是学生可见核心字段的节选，完整 v1 还包含 Phase 1 状态、ordered diff、secondary/suppressed/unresolved 计数和 QSS：

```json
{
  "schema_version": "phase2.public.v1",
  "verdict": "INCORRECT",
  "diagnosis_status": "SUPPORTED",
  "primary": {
    "rule_id": "S2_BOUNDARY",
    "stage": "S2",
    "scope_id": "paired:root",
    "knowledge_points": ["where"]
  },
  "secondary_count": 0,
  "witness": {
    "availability": "CAUSAL_VERIFIED",
    "cases": []
  },
  "narrative": {
    "student_behavior": "...",
    "conflict_and_witness": "...",
    "guidance_question": "..."
  },
  "boundary_notes": []
}
```

Public 包禁止出现：

- `answer_sql`、`correct_sql`、`standard_sql`、`standard_node`；
- mutation replacement SQL；
- 完整 test database / witness world；
- 内部 confidence 校准细节和未公开候选；
- 能直接拼接成标准答案的结构片段。

正确包应为：`verdict=CORRECT`、`primary=null`、`witness=null`，只携带鼓励语和可选的非阻断风格提示。未决包应为：`verdict=UNDECIDED`、`primary=null`，只说明当前系统无法可靠判定，不能伪造错误原因。

### 11.3 API 兼容

- 现有 `hint`、`is_correct`、`submission_id` 暂时保留；
- 新增可选 `diagnostic_package` 和明确 `judge_status`；
- 前端类型先以 optional 字段接入；
- raw observation 默认不进入学生响应；
- 暂不要求 P0 增加数据库迁移，仍可把公开 narrative 写入 `ai_hint`；
- 若要支持完整审计重放，再增加独立 JSON 字段和版本号，不应把内部包塞进 `ai_hint` 文本列。

## 12. 原规划逐项修订

### 12.1 原步骤 2.1：信号清洗与逻辑执行管道

保留：正确提交快速出口、按教学逻辑组织 AST diff。

修订：

- 正确性由 rich verdict 确认，不由 LLM 确认；
- `UNDECIDED` 单独退出；
- 单条六槽管道改成 ScopedQueryGraph + 每 scope 六槽视图；
- CTE/子查询不是简单挂在 S1 的普通节点，其内部必须另建 scope；
- WINDOW/QUALIFY/set operator 等进入显式扩展节点；
- 所有排序项保留 stable diff ID。

### 12.2 原步骤 2.2：错误依赖分析与成因打包

保留：规则目录、教学命名、局部差异聚合。

修订：

- 规则数量改为 20；
- 明确 MVP、证据前置条件和边界；
- 每条规则同时提供 positive/negative/ambiguous fixture；
- 规则输出候选，不直接输出最终话术；
- 等价但低效的写法只能标为 non-blocking complication。

### 12.3 原步骤 2.3：FDP 与下游抑制

保留：优先教学最早根因、减少认知负荷。

修订：

- 全量扫描，禁止命中后早停；
- 构建 causal DAG；
- 只抑制有因果边的伴随现象；
- 保留 independent secondary roots；
- 空结果只能说明可观测性被遮蔽，不能自动证明后续错误都是假的。

### 12.4 原步骤 2.4：物理反例与 QSS

保留：用具体数据帮助理解。

修订：

- 抽取 Minimal Witness Slice，而不是任意取前 1～2 行；
- witness 必须绑定 diff/obligation/world/row index；
- 没有物证时显式 `UNAVAILABLE`；
- Q 和 Schema fact 都必须带来源与置信度；
- Phase 2 provider 可在内部复核时接收有界的标准/学生 SQL；这些内容只进入 provider 请求和私有审计，不进入学生响应。完整测试数据库、大 AST、mutation SQL 和 raw observation 不发送给 LLM。

### 12.5 原步骤 2.5：DiagnosticPackage 与三段式话术

保留：三段式教学链，作为 Phase 3 唯一结构化输入。

修订：

- 先生成 Internal 包，再通过 sanitizer 得到 Public 包；
- Phase 3 只能读取 Public 包；
- 第三段禁止字段名、答案字面量和修复代码；
- 正确、错误、未决三类包有不同结构；
- 每个 narrative fact 可追溯到公开包中的具体 evidence reference。

## 13. 原示例中的必要修正

1. **规则数量**：全文“18 种”改为“20 条 MVP 规则”。
2. **5.1 各系学生总人数**：原标准 SQL 通过 `INNER JOIN takes` 会漏掉未选课学生。若题意是“各系全部学生”，应直接在 `student` 上统计；若题意是“各系有选课学生”，应改写题目并明确口径。修复方向也不能默认总是 `COUNT(DISTINCT ...)`。
3. **5.2 COUNT 空值敏感度**：统一 Schema 中没有 `bonus` 列，示例 Schema 必须补充 `instructor.bonus` 及 nullable 声明。
4. **3.2 分组维度缺失**：`takes` 与 `course` 都含 `course_id`，`COUNT(course_id)` 存在歧义，应显式限定来源列。
5. **6.2 第二高工资**：必须先定义“第二行”还是“第二个不同工资等级”。前者需要确定性 tie-breaker；后者应使用 distinct rank / `DENSE_RANK` 语义，不能简单把 `OFFSET 1` 视为通用标准答案。
6. **4.3 行过滤写在 HAVING**：对 group key 的常量条件在许多引擎上与 WHERE 语义等价。此时它最多是性能/风格 complication，不能作为错误判决依据。
7. **2.3 成绩题意**：“尚未录入成绩”通常只指 `IS NULL`；若目标是“非 F 或尚未录入”，题干必须明确写出两类记录。
8. **1.2 笛卡尔积**：CROSS JOIN 可能是有意语义。只有题意、参考关系路径和 witness 都表明不需要乘积时，才能标记为 fallacy。
9. **“任何合法 SQL”**：改为“Phase 1 当前声明支持范围内的查询结构”，高级结构进入 extension scope/node。

## 14. 测试与验收标准

### 14.1 单元测试

- 每个 stage/clause/diff type 的确定映射；
- nested subquery、CTE、derived table、set branch 的 scope 隔离；
- AST diff 输入换序后 Ordered Pipeline 不变；
- unknown diff 不丢失，进入 extension/unclassified；
- 20 条规则每条至少一个 positive、negative、ambiguous 用例；
- 正确等价改写不产生 blocking bundle；
- rule ID、stage、scope 和 evidence ID 序列化稳定。

### 14.2 因果与抑制测试

- S1 bridge fault 只抑制真正依赖它的 projection/column symptom；
- S1 fault 与独立 S6 fault 同时存在时，S6 保留为 secondary root；
- WHERE 产生空结果时，不自动删除 GROUP/ORDER 候选；
- GROUP BY 与依赖投影能合并成 grain bundle；
- WHERE/HAVING 同一 predicate relocation 合并成一个 bundle；
- JOIN fan-out 与 aggregate inflation 形成 `AMPLIFIES` 边；
- ORDER BY 错误与 LIMIT cutoff symptom 正确合并；
- 没有 causal edge 时不得抑制。

### 14.3 Verdict 回归测试

- `CORRECT`、`NOT_EQUIVALENT`、`UNDECIDED`、`ENGINE_GAP`、`INPUT_GAP`、`SEMANTIC_BOUNDARY` 全分支；
- `is_equivalent=True + equivalence_conclusion=UNDECIDED` 绝不能成为正确；
- Phase 2 抛异常不改变 Phase 1 verdict；
- 未决不计失败、不更新 BKT，也不生成教学错误反馈；
- 语法错误和安全拦截保持现有专用出口。

### 14.4 Witness 测试

- 通过 applied constraint 的 `row_index` 精确取到输入行；
- multi-table case 只保留必要连接行与列；
- NULL、boundary、duplicate、dangling outer row、order ties 均有针对性 fixture；
- selected world 不匹配时降级为 `OUTPUT_ONLY/UNAVAILABLE`；
- 没有 witness 时绝不生成具体值；
- 大 world 输出始终满足 case/row/column/string 上限。

### 14.5 防泄漏测试

对 Public 包进行递归断言：

- 禁止敏感 key；
- 标准 SQL 全文和规范化片段均不出现；
- mutation replacement SQL 不出现；
- 第三段不包含完整修复代码、参考字段名或答案字面量；
- LLM 输出故意注入标准答案时 validator 能拒绝并回退模板；
- 学生、教师、调试权限响应边界明确。

### 14.6 API 与兼容测试

- 旧响应字段保持；
- 新 `diagnostic_package` 可选且 schema version 固定；
- zh-CN、zh-TW、en 都有确定性降级模板；
- submission/chat 持久化只保存 Public narrative；
- 前端旧版本忽略新字段仍能运行；
- 新前端能区分 CORRECT / INCORRECT / UNDECIDED / SYSTEM_ERROR。

### 14.7 资源与确定性门禁

- Phase 2 不重新执行 SQL，不复制完整 witness world；
- 不把大 AST、完整 DB 或全 mutation SQL 发送给 LLM；
- 所有集合排序稳定，固定输入输出 digest 一致；
- 测试在受限虚拟内存下运行，避免并发加载大模型或无界 payload；
- 规则和 witness 处理保持线性或有明确上界。

### 14.8 最终验收记录

最终计数必须从合并后实际运行产生，不从开发过程中的部分轮次推算。权威纯 Phase 2 记录路径为 `data_construct_test/outputs/phase2_acceptance_report.json`。

| 验收层 | 最终结果 | 通过/收集计数 | 说明 |
|---|---|---:|---|
| 离线 Phase 2 冻结门禁 | `PASS` | `169 / 169` | 7/7 组通过（`6+24+18+61+37+18+5`）；20/20 规则精确匹配；权威 JSON 报告为 `phase2.acceptance-report.v1` |
| 冻结 runner 自身单元测试 | `PASS` | `15 / 15` | 单独验证离线阻断、资源上界、fail-closed 与原子写报告 |
| 完整 DB 判题路由回归 | `PASS` | `10 / 10` | `test_check_sql_flow.py` 全量执行；独立系统验收层，不计入离线门禁 |
| 广义 API/安全邻接回归 | `PASS` | `51 / 51` | 额外验证 API 契约、路由与公开安全边界；不重复计入离线门禁 `totals` |
| Phase 1 相关回归 | `PASS` | `640 / 640` | 保护 rich verdict/scope 接缝，不改写 v16 `acceptance.pass=false` |

回填规则：只能写实际收集和执行的数量；不得把 deselected、skip 或未执行目标算作通过，不得用历史计数替代本轮最终报告。

## 15. 推荐实施顺序

### P0（必须先完成）

当前实施状态：以下 6 项已在有界 MVP 内关闭。SchemaCatalog 对未声明的关系返回 unknown，而不通过命名约定猜测；这是安全语义，不是未完成项。

1. 修复 router rich verdict 消费；
2. 建立 `Phase1EvidenceAdapter`，保留 stable IDs 和 rich witness evidence；
3. 接通 question context 与规范 SchemaCatalog；
4. 新建 `core/error_diagnosis.py` facade 和版本化类型；
5. 建立 Internal/Public sanitizer 与泄漏测试；
6. 增加 optional API 字段和未决分流测试。

P0 以及 Public Sanitizer 是后续任何 Phase 3 接入的持续门禁，不能因为本次完成而绕过。

### P1（MVP Diagnosis）

当前实施状态：以下项目均已落地；第 7 项的最终验收记录见第 14.8 节。

1. 实现 ScopedQueryGraph 与 Ordered Pipeline；
2. 实现 20-rule catalog 与证据前置条件；
3. 实现 causal DAG、bundling、FDP 与 secondary roots；
4. 实现 Minimal Witness Slice；
5. 实现三段式 deterministic narrative；
6. 接入受限 Phase 2/Phase 5 LLM adapter，并保留确定性回退；
7. 完成规则、因果、witness、泄漏、API 和资源验收。

### P1 之后

- 扩展 Phase 1 已支持但 20-rule 未覆盖的高级 SQL 规则；
- 使用公开、冻结的 Phase 2 评测集校准优先级和置信度；
- 再接入 Phase 3 的 BKT、认知负荷和 ActionSelector；
- 如需审计重放，再设计诊断包持久化迁移。

## 16. 阶段完成定义

只有同时满足以下条件，才能把 Phase 2 标记为“阶段性完成”：

1. P0 所有门禁通过，rich verdict 不被降级成布尔误判；
2. ScopedQueryGraph 对根查询、CTE、子查询和 set branch 不串 scope；
3. 20 条 MVP 规则的支持/不支持边界明确，并有正反例；
4. FDP 来自 causal DAG，而不是简单的阶段早停；
5. 独立 secondary root 不被错误抑制；
6. 可用 witness 均能追溯到 diff/obligation/world/row，证据不足时明确降级；
7. Public DiagnosticPackage 不包含答案泄漏；
8. 正确、错误、未决和平台故障全链路分流正确；
9. API 兼容、确定性、资源上界和多语言降级测试通过；
10. 文档中的“20 条 MVP、非全覆盖、有界能力域”与代码声明一致。

当前实施已满足上述有界 MVP 定义：SchemaCatalog、ScopedQueryGraph、20-rule 三态矩阵、因果 FDP、Minimal Witness/QSS/三段式教学链、Internal/Public 分层、API 安全分流和离线冻结 runner 均已落地；第 14.8 节所列最终门禁与邻接回归全部通过。状态统一为：

> `PHASE2_MVP_ACCEPTED / PHASE1_GLOBAL_ACCEPTANCE_OPEN`：Phase 2 在 `phase2.rules.mvp20.v1` 的声明边界内阶段性完成；高级 SQL 教学规则仍是扩展项。Phase 1 v16 仍是 `acceptance.pass=false`，不得因 Phase 2 验收结果而改写。
