# Phase 3 v1：因果调度、支架分轴与原始 BKT

本文件是 Phase 3 当前实现合同。它取代旧文档中“用一个 `lambda_t` 同时控制错误排序、提示深度和题目难度”以及“把错误列表称为 A* 搜索”的设计描述。旧字段 `lambda_t` 仅为客户端兼容保留，服务端始终返回 `null`。

## 1. 已实现边界

```text
Phase 1 权威判定
        │
        ▼
Phase 2 Public DiagnosticPackage ──错误──► 强因果 rule_id → 原子技能观测
        │
        └──────────────────────────正确──► Q-matrix PRIMARY + observable
                                                 │
                                                 ▼
                                      Trusted Observation Gate
                                                 │
                      ┌──────────────────────────┴─────────────────────────┐
                      ▼                                                    ▼
         ConstrainedPriorityScheduler                          Raw BKT Update
         主因/FDP 硬层级优先                                   事件审计 + 技能状态
                      │                                                    │
                      ▼                                                    ▼
       support_need（本次支架建议）                     challenge_index（下一题）
```

以下路径不产生学习状态观测：语法错误、安全拦截、Phase 1 `UNDECIDED`、Phase 2 `PARTIAL/DEGRADED`、弱证据、正确但题目没有权威 Q-matrix、答案已揭示。

## 2. 不再使用伪 A*

这里没有状态图、路径代价、目标节点或可采纳启发函数，因此实现名为 `ConstrainedPriorityScheduler`（兼容函数名 `schedule_causal_priorities`）。

第一层是不可被分数越过的因果层级：

1. `FDP/PRIMARY`
2. `SECONDARY`

同层候选使用独立特征评分：

```text
priority_score =
    0.30 * instructional_impact
  + 0.25 * recurrence
  + 0.20 * mastery_deficit
  + 0.15 * question_alignment
  + 0.10 * evidence_strength
```

最终稳定排序键为：

```text
(causal_tier, -priority_score, logical_stage_rank,
 phase2_candidate_id, skill_id)
```

`instructional_impact` 是 Phase 3 的版本化教学配置，不读取 Phase 2 bundle 的最高 `severity`。所有权重当前均标记为 `UNCALIBRATED_MVP`。

## 3. 将一个 lambda 拆成两个可解释轴

本次提示支架建议：

```text
support_need = clamp(
    0.35 * (1 - mastery)
  + 0.30 * failure_streak_norm
  + 0.10 * recent_hint_ratio
  + 0.10 * behavioral_support_need
  - 0.15 * recent_unassisted_success,
  0, 1)
```

四档区间为：

| 区间 | 推荐支架等级 |
|---|---:|
| `[0, 0.25)` | 1 |
| `[0.25, 0.50)` | 2 |
| `[0.50, 0.75)` | 3 |
| `[0.75, 1]` | 4 |

下一题难度信号单独计算：

```text
challenge_index = clamp(
    0.50 * mastery
  + 0.30 * recent_unassisted_success
  + 0.20 * (1 - behavioral_support_need),
  0, 1)
```

`challenge_index` 不参与当前错误排序，也不决定当前提示深度；`challenge_readiness` 只是兼容别名。

当前实现建立的是版本化 `behavioral_support_need` 行为代理：只使用可信观测事件中的语义正确/错误模式，按最近窗口和连续错误计算；没有活动语义失败证据时保持缺失，不把未知伪装成低支持需求。它返回 `behavioral_proxy_status=BEHAVIORAL_SUPPORT_NEED_PROXY_V1`，不声称测量心理疲劳。

长于 30 分钟的最新空闲间隔会重置当前行为窗口；syntax/parse、平台异常、安全拦截和 `UNDECIDED` 单独排除，syntax 仅保留独立计数。一次正确会清零连续失败 streak，但不会删除窗口内更早的失败，因此不会让支持代理瞬间归零。

同样，当前事件中的 `assistance_level` 表示提交后实际发出的反馈，不能反向证明这次答案是在无辅助条件下完成。因此运行时把 `recent_unassisted_success` 保守置为 0，并返回 `attempt_context_status=PRE_ATTEMPT_ASSISTANCE_NOT_TRACKED`。后续必须显式记录“提交前已经看过的提示/答案”，才能启用该信号。

Phase 4/5 v1 已接入不同深度的教学动作和确定性反馈。API 同时返回 `recommended_support_level`、`delivered_support_level` 与 `support_recommendation_applied`。观测事件记录的是实际交付等级；当反馈生成或安全门禁降级时，推荐值保留用于审计，但实际等级降为 L1。

四档阈值在纯策略函数和端到端路由中均可达，API 以 `runtime_support_reachability=L1_TO_L4_WITH_PHASE4_V1` 明示当前能力。具体教学与事务合同见《29-Phase4-6-v1-教学支架反馈与事务闭环》。

## 4. 标准四参数 BKT，不再二次平滑

当前未校准 MVP 参数：

```text
P(L0)=0.20, slip=0.10, guess=0.20, transition=0.10
version=phase3.bkt_parameters.v1
```

正确观测：

```text
posterior = p*(1-slip) / (p*(1-slip) + (1-p)*guess)
```

错误观测：

```text
posterior = p*slip / (p*slip + (1-p)*(1-guess))
```

随后仅执行 BKT 自身的学习转移：

```text
next_prior = posterior + (1-posterior)*transition
```

下一次观测只读取 `next_prior`。不再计算 `0.6*old + 0.4*new`。可选 UI EMA 是独立函数，既不写回 `student_skill_states`，也不能成为下一次 BKT prior。

状态主键为：

```text
(user_id, taxonomy_version, skill_id)
```

因此课程级技能与 Phase 3 原子技能不会混为同一个概率。

## 5. 持久化与幂等

- `student_skill_states`：保存 raw `posterior_mastery`、`next_prior`、观测次数、参数版本和状态版本。
- `skill_observation_events`：保存每次可信观测的来源、证据等级、Phase 2 candidate/rule、实际支架等级及 BKT 前后数值。
- `submission_teaching_audits`：保存 Phase 3 推荐、Phase 4 动作、Phase 5 实际反馈、降级原因、反馈摘要及内部因果 provenance。
- 唯一键 `(submission_id, taxonomy_version, skill_id)` 防止同一提交重复更新。
- 每次提交按钮动作由客户端生成 UUID `attempt_id`；前端必须在动作边界显式创建并保留整个请求对象，认证刷新或网络结果未知后的重试复用同一个 ID 和完全相同的请求内容。
- `submissions` 使用 `(user_id, question_id, attempt_id)` 唯一键，并保存请求指纹与 learner-safe 响应快照。相同请求直接重放原响应，不重跑 Phase 1、不重复写提交/对话/BKT；同一 ID 携带不同内容返回 `409 ATTEMPT_ID_REUSED`。重放响应额外设置 `idempotency_replayed=true`，客户端据此抑制重复反馈动效。
- 同一服务进程内还会在第一次数据库读取和 Phase 1 之前，按 `(user_id, question_id, attempt_id)` 串行合并并发请求，避免同一个 key 重复占用昂贵的 Phase 1 worker。该 single-flight 只负责计算降载；多进程/多实例正确性仍由数据库唯一键、请求指纹和锁后 current read 保证。
- 幂等重试若来源或因果 provenance 不一致会 fail-closed。
- 参数版本变化不会静默续算，必须显式迁移状态。
- 在写提交、对话和 BKT 前取得用户行锁；锁后的提交计数、聊天计数、mastery 和 history 使用 MySQL current/locking read，避免早先幂等查询创建的 `REPEATABLE READ` 快照让 priority/support 读取陈旧状态。一组学习观测在同一事务/保存点内原子执行。

## 6. 当前效果样例

以首次出现 `filter.boundary` 错误为例：

```text
prior                  = 0.200000
incorrect posterior    = 0.030303
next prior             = 0.127273
support_need           = 0.380000
recommended level      = 2
challenge_readiness    = 0.063636
```

首次正确观测（提交前辅助状态尚未建模，相关信号按 0 处理）：

```text
prior                  = 0.200000
correct posterior      = 0.529412
next prior             = 0.576471
challenge_readiness    = 0.288235
```

这些数值只用于验证代码链路是否符合已声明公式，不代表参数已通过真实学生数据校准。

同一原子技能连续答错、且行为代理仍缺失时，Phase 4 已实际交付的效果为：

| 连续错误次数 | 本次 prior | support_need | 推荐等级 | 更新后 next_prior |
|---:|---:|---:|---:|---:|
| 1 | 0.200000 | 0.380000 | L2 | 0.127273 |
| 2 | 0.127273 | 0.682121 | L3 | 0.116113 |
| 3 | 0.116113 | 0.797694 | L4 | 0.114540 |

第二次以后，历史事件中的实际辅助等级大于 1，因此 `recent_hint_ratio` 开始按 v2 权重贡献 0.10。当前接线会把连续失败从 L2 实际提升到 L3、L4，并在 Submission、教学审计与 BKT 事件中保存同一个交付等级。

## 7. API 合同

`POST /ai/check-sql` 现在要求请求字段 `attempt_id: UUID`，并在响应中原样返回规范化后的 `attempt_id` 与 `idempotency_replayed`。它还新增聚合字段 `phase3_learning`；该字段不回传 Q-matrix 行或技能标识，主要内容包括：

- `status`: `UPDATED`、`SKIP_NO_ASSESSMENT_MAP`、`NO_ELIGIBLE_OBSERVATION`、`ALREADY_APPLIED` 或 `DEGRADED_NO_LEARNING_UPDATE`
- `observation_count` / `state_update_count`
- `priority_policy_version`（具体 score 不公开，因为其中包含私有 Q-matrix alignment）
- `support_need` / `recommended_support_level`
- `delivered_support_level` / `support_recommendation_applied`
- `challenge_index` / `next_exercise_challenge_readiness`（兼容别名为 `challenge_readiness`）
- `challenge_usage=NEXT_EXERCISE_DIFFICULTY_ONLY`
- `challenge_aggregation_scope=MIN_CURRENT_ATTEMPT_SKILLS`
- 各策略和 BKT 参数版本
- `calibration_status=UNCALIBRATED_MVP`

`GET /ai/mastery-radar` 返回两个命名空间的 raw posterior，并在 `state_details` 中同时给出 `posterior_mastery` 与 `next_prior`。未观测技能显示明确的 `P(L0)=0.20`，不再伪造静态 0.5。

学生响应另有 `teaching_support`，只公开推荐/实际等级、应用状态、三阶段策略版本和反馈来源，不公开候选 rule/skill/witness。完整 Phase 2 `diagnostic_package` 对新提交返回 `null`，防止绕过分级支架。

## 8. 尚未宣称完成的内容

- `behavioral_support_need` 已建立并由可信事件接线，但仍需要真实轨迹验证和校准；它不是认知负荷或疲劳的直接测量。
- broad skill 与 atomic skill 之间尚无经验证的聚合/桥接模型。
- 当前前端提供 `createSqlCheckAttempt(...)`，页面应在一次提交按钮动作时调用它，然后把返回对象交给 `checkSql(...)`；后者要求 `attempt_id` 必传，不再在调用内部生成一个无法恢复的 ID。
- Phase 3 普通业务异常由保存点降级为“不更新学习状态”，但数据库断连、事务级死锁等基础设施故障仍可能使外层事务失效；如需跨故障域的最终补偿，应在后续引入 submission/outbox 同事务与独立幂等消费者。
- 所有优先级、支架和 BKT 参数仍需离线数据校准、敏感性分析与 A/B 验证。
- Phase 5 已接入可选的受限 LLM 改写器；它只改写 Phase 4 已批准的教学片段，不参与 verdict、selected target、支架等级或 BKT 决策。默认配置关闭，失败时回到确定性模板。
- 因而当前成果是可运行、可审计、可测试并已接通 Phase 4～6 的 v1 MVP，不是完整心理测量模型或控制理论解析解。
