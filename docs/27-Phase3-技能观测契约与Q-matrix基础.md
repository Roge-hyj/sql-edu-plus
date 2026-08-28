# Phase 3 技能观测契约与 Q-matrix 基础

## 当前状态

`PHASE3_V1_IMPLEMENTED_BOUNDED`

本轮在两项基础契约之上，已经接通并审计了 Phase 3 v1 的观测、调度、行为代理和原始 BKT 闭环；这仍不是离线校准完成的最终教学策略：

1. Phase 2 原子错因规则到 Phase 3 原子技能的可审计映射与强证据投影。
2. 服务端保存、版本化且可校验的 Question–Skill Q-matrix。
3. 仅凭权威来源产生观测、受约束的目标调度、行为支持代理和不二次平滑的原始 BKT。

权重和 BKT 参数仍标记为 `UNCALIBRATED_MVP`；行为值是可审计的行为支持代理，不是心理疲劳测量。

## 1. Phase 2 规则到原子技能

映射版本：`phase3.rule_skill_map.v1`

原子技能 taxonomy：`phase3.atomic_sql_skills.v1`

| Phase 2 rule_id | Phase 3 skill_id |
|---|---|
| S1_MISSING_BRIDGE | join.bridge_path |
| S1_CARTESIAN_PRODUCT | join.constraint |
| S1_OUTER_JOIN_MISUSE | join.outer_preservation |
| S1_SUBQUERY_CARDINALITY | subquery.cardinality |
| S2_BOUNDARY | filter.boundary |
| S2_BOOLEAN_LOGIC | filter.boolean_logic |
| S2_NULL_LOGIC | null.three_valued_logic |
| S2_AGGREGATE_IN_WHERE | aggregate.filter_placement |
| S3_GRAIN_ENTITY_MISMATCH | group.grain |
| S3_GROUP_KEY_MISSING | group.key_completeness |
| S3_GROUP_KEY_REDUNDANT | group.key_redundancy |
| S4_HAVING_MISSING | having.required |
| S4_AGG_BOUNDARY | having.aggregate_boundary |
| S4_ROW_FILTER_IN_HAVING | filter.stage_placement |
| S5_FANOUT_AGGREGATE | aggregate.fanout |
| S5_COUNT_NULL_SENSITIVITY | aggregate.count_null |
| S5_CASE_INCOMPLETE | projection.case_coverage |
| S5_TOP_LEVEL_DEDUP | projection.dedup |
| S6_TOPN_WITHOUT_ORDER | result.topn_order |
| S6_ORDER_OFFSET | result.order_offset |

投影器只接受精确版本的 `phase2.public.v1` 服务端诊断包，且要求：

- `verdict=INCORRECT`；
- `diagnosis_status=SUPPORTED`；
- primary 必须是 `CAUSAL_VERIFIED` 或 `REPAIR_VERIFIED`；
- secondary 只逐项接受同等强证据；
- primary-first 稳定去重，并保留固定 skip reason code；
- 只输出 observation candidate，不写入任何学习状态。

`candidate.knowledge_points` 仅是 Phase 2 展示和一致性检查信号。投影器不读取、不遍历、不渲染该字段，因此它不能成为后续 BKT 更新的授权来源。

## 2. Question–Skill Q-matrix

数据库表 `question_skills` 保存：

- `question_id`；
- `skill_id`；
- `taxonomy_version`；
- `skill_role`: `PRIMARY | SUPPORTING`；
- `observable_on_correct`；
- `provenance`: `AUTHOR_DECLARED | GENERATED | INFERRED`。

主要不变量：

- `(question_id, skill_id, taxonomy_version)` 唯一；
- 每题最多 8 个技能，其中最多 3 个 PRIMARY；
- SUPPORTING 不得设为 `observable_on_correct=true`；
- 客户端不得指定 provenance；
- PUT 省略 `skills` 时保留原映射，显式传入 `[]` 时清空；
- 题目修改和映射替换在同一事务内提交或回滚；
- 删除题目时数据库级联删除映射；
- 旧题不回填，不从 `correct_sql` 临时推断权威技能。
- 正观测还必须匹配数据库中同一题目的 `PRIMARY + observable_on_correct=true` 行；仅在调用参数中伪造 provenance、role 或 skill 不能写 BKT。
- `INFERRED` 映射可以保留作设计元数据，但不能单独产生一次正确的正观测。

当前同时支持两个明确的技能空间：

- `sql_knowledge_points.v1`：现有 AI 按知识点出题的 broad curriculum ID；
- `phase3.atomic_sql_skills.v1`：Phase 2 原子错因对应的 atomic skill ID。

省略 taxonomy 时，只有 skill ID 能在两个冻结 catalog 中唯一解析才会被接受。系统不会把 broad skill 自动猜成某个 atomic skill。

## 3. Phase 3 v1 的硬边界

当前运行时必须满足：

- 学习状态主键至少包含 `(taxonomy_version, skill_id)`；
- 正观测只能来自 Q-matrix 的 `PRIMARY + observable_on_correct=true`；
- 负观测只能来自服务端 Phase 2 强证据原子投影；
- broad mastery 和 atomic mastery 不能直接混合为同一概率；
- Phase 3 不接受客户端回传的诊断投影作为受信事实。
- 错误目标使用 `ConstrainedPriorityScheduler`：FDP/PRIMARY 先于 SECONDARY，支架预算最多加入一个独立 SECONDARY，suppressed/unresolved 永不选择。
- `challenge_index` 是可解释加权指标，不宣称 sigmoid、变分解析解或心理测量；`challenge_readiness` 仅作兼容别名。
- `behavioral_support_need` 只由当前会话的可信语义观测计算，长空闲会话重置；syntax/platform/safety/UNDECIDED 不进入语义失败统计。
- BKT 持久化保存 raw posterior 和 next_prior；展示 EMA 单独计算，不能覆盖学习状态。

## 4. 已知后续项

- 为题意或标准 SQL 变更增加 `question_revision/assessment_fingerprint`，防止旧 Q-matrix 继续产生正观测。
- 若需要完整审计，增加操作者、时间、复核状态和历史版本；当前 provenance 只能说明来源类型。
- 在后续校准阶段单独决定 broad–atomic 聚合或桥接方式；当前两种 taxonomy 始终分开存储。

## 5. 验收证据

- Phase 3/Q-matrix 聚焦测试通过；
- 学生 token 无法创建或替换 Q-matrix；
- 公开列表和详情即使已加载 ORM 映射，也不序列化 `correct_sql`、skills、taxonomy 或 provenance；
- 创建、更新和 AI 批量出题的失败回滚均有测试覆盖；
- MySQL 8.0.46 一次性实例完成 `upgrade head → downgrade c2d3e4f5a6b7 → upgrade head`；Q-matrix provenance 规范化迁移后，最终 head 以 Alembic 当前单 head 为准。
