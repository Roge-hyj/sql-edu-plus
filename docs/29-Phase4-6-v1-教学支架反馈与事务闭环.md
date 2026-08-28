# Phase 4～6 v1：教学支架、学生反馈与事务闭环

本文记录 Phase 4、5、6 的当前真实实现。它接续 Phase 1 的权威判题、Phase 2 的因果诊断和 Phase 3 的原子技能调度，不再沿用初版图中的“伪 A* + 单一 lambda + 提示固定 L1 + LLM 决策”描述。

## 1. 当前完整通路

```text
学生提交 + attempt_id
        │
        ▼
Phase 1：权威判题与证据
        │
        ▼
Phase 2：DiagnosticPackage（仅服务端内部继续使用）
        │
        ▼
Phase 3：可信观测门控 + 单一 selected_target + support recommendation
        │
        ▼
Phase 4：TeachingActionPlan
        │  只消费 selected_target；按 L1～L4 裁剪一个错误
        ▼
Phase 5：StudentFeedbackArtifact
        │  确定性模板 + 可选受限 LLM 改写；最终答案泄漏检查；失败降级到 L1
        ▼
Phase 6：同一事务写入
        ├─ submissions + response_snapshot
        ├─ submission_teaching_audits
        ├─ skill_observation_events + student_skill_states（保存点隔离）
        └─ chat_messages
        │
        ▼
学生响应：hint + teaching_support + phase3_learning
```

关键不变量：

```text
Submission.hint_level
  = SubmissionTeachingAudit.delivered_support_level
  = SkillObservationEvent.assistance_level
  = teaching_support.delivered_support_level
```

Phase 3 的推荐值不能冒充实际交付值。只有 Phase 4/5 成功执行对应深度时，`support_recommendation_applied=true`；否则实际交付降为 L1，并记录 `OVERRIDDEN`。

## 2. Phase 4：单目标教学动作

实现文件：`core/teaching_action.py`。

Phase 4 不读取 `candidate.knowledge_points` 来猜教学目标，也不自行重新排序错误。它只接受 Phase 3 已明确绑定的 `selected_target`：

```text
observation_id
phase2_candidate_id
phase2_rule_id
skill_id
taxonomy_version
logical_stage
source_role
evidence_grade
```

只有该目标能进入本轮反馈。其他可信错误仍可更新各自的 BKT 状态，但不会同时展示给学生，从而避免“一次倾倒全部错误”。

四级支架如下：

| 等级 | 实际动作 |
|---|---|
| L1 | 只给一个苏格拉底式自查问题 |
| L2 | 当前查询行为 + 自查问题 |
| L3 | 当前行为 + 冲突/物证 + 自查问题 |
| L4 | 当前行为 + 冲突/物证 + 通用修复检查方向 + 自查问题 |

L4 的修复方向来自 20 条版本化规则模板，只提供概念级检查步骤，不生成完整 SQL。正确提交、语法错误、安全拦截和无可信目标路径不伪造自适应目标，统一采用非自适应 L1 动作。

Phase 4 内部审计合同为 `phase4.teaching_action.v1`，策略版本为 `phase4.action_selector.v1`。

## 3. Phase 5：确定性兜底与受限 LLM 安全反馈

实现文件：`core/student_feedback.py`。

Phase 5 先把 Phase 4 已批准的动作按 `zh-CN`、`zh-TW`、`en` 三种语言组织为确定性文本；在显式开启配置且存在可用 OpenAI-compatible provider 时，LLM 只能对 L2～L4 的可编辑片段做一对一安全改写。两条路径都不改变：

- Phase 1 verdict；
- Phase 3 selected target；
- 推荐或实际支架等级；
- witness 事实；
- 修复方向。

LLM 只收到已批准动作的 `action_id`、动作类型、支架等级、语言和原始教学片段，不收到标准答案 SQL、Phase 1 完整证据、数据库行或 AST。Phase 2 的 LLM 也只能在已有候选 rule/evidence 集合内复核和润色，不能推翻 Phase 1 权威 verdict、创造新证据或决定 BKT。

最终文本还必须通过路由中的参考答案泄漏闸门。完整 Phase 2 `diagnostic_package` 不再返回学生端，因为它会让 L1/L2 客户端绕过支架裁剪，直接读取 primary、secondary、witness 和 QSS。

若 Phase 4 选择、Phase 5 主渲染或最终安全校验失败：

1. 保留 Phase 1 权威正误；
2. 使用独立的本地应急渲染器生成不含答案的 L1 文本；
3. 若原来存在推荐，公开状态记为 `OVERRIDDEN`；
4. 审计状态记为 `FALLBACK` 并保存稳定 `degradation_code`；
5. BKT 事件只记录实际交付的 L1。

Phase 5 合同为 `phase5.student_feedback.v1`，确定性策略版本为 `phase5.safe_renderer.v1`，LLM 策略版本为 `phase5.llm_feedback.v1`。LLM 调用位于数据库用户锁之外；provider 超时、JSON 不合法、动作不匹配、语义锚点丢失或答案泄漏时，丢弃 LLM 文案并保留确定性文本。LLM 不得决定 verdict、target 或 level。

## 4. Phase 6：原子持久化与反馈审计

新增表 `submission_teaching_audits`，迁移版本为 `f6a7b8c9d0e1`。该表以 `submission_id` 同时作为主键和外键，因此一次幂等提交最多对应一条不可变教学审计。

审计内容包括：

- `APPLIED / OVERRIDDEN / NOT_APPLICABLE` 推荐状态；
- `support_need`、推荐等级、实际等级及 Phase 3 策略版本；
- Phase 4/5 策略版本、生成来源、反馈状态和降级码；
- 内部 target candidate/rule/observation/skill/taxonomy/stage provenance；
- `answer_revealed`；
- 最终反馈 SHA-256；
- 有界 `action_snapshot`。

仓储在写入前验证：

- 推荐等级与 `support_need` 必须同时存在或同时为空；
- `APPLIED` 必须满足推荐等级等于实际等级；
- `FALLBACK` 必须有降级码；
- `Submission.hint_level` 必须等于审计实际等级；
- `Submission.ai_hint` 的 SHA-256 必须等于审计摘要；
- 同一 `submission_id` 的重复写必须逐字段一致，否则拒绝。

正常事务顺序是：取得用户行锁、再次检查 attempt、读取 Phase 3 历史、生成本地反馈、创建 Submission、创建教学审计、在保存点内更新 BKT、写对话和响应快照、统一 commit。

BKT 更新失败只回滚保存点，提交、反馈审计和聊天仍按权威判题提交；教学审计本身失败会使外层事务整体失败，避免产生“有反馈但无反馈审计”的半状态。

## 5. 学生 API 合同

`POST /ai/check-sql` 的 `language` 只允许：

```text
zh-CN | zh-TW | en
```

公开 `teaching_support` 示例：

```json
{
  "schema_version": "phase4.teaching_support.v1",
  "status": "APPLIED",
  "language": "zh-CN",
  "recommended_support_level": 2,
  "delivered_support_level": 2,
  "support_recommendation_applied": true,
  "generation_source": "LOCAL_TEMPLATE",
  "focused_error_count": 1,
  "answer_revealed": false,
  "support_policy_version": "phase3.support_policy.v2",
  "action_policy_version": "phase4.action_selector.v1",
  "feedback_policy_version": "phase5.safe_renderer.v1",
  "feedback_status": "PRIMARY"
}
```

该对象不包含 candidate、rule、skill、observation、Q-matrix、witness 或参考 SQL。`diagnostic_package` 旧字段暂时保留在响应 schema 以兼容客户端，但新提交始终返回 `null`。

## 6. 已验证效果

同一可信 `filter.boundary` 错误连续出现时，当前真实端到端路径为：

| 连续错误 | support_need（约） | 推荐 | 实际交付 | 反馈结构 |
|---:|---:|---:|---:|---|
| 1 | 0.380 | L2 | L2 | 行为 + 问题 |
| 2 | 0.705 | L3 | L3 | 行为 + 物证 + 问题 |
| 3 | 0.809 | L4 | L4 | 行为 + 物证 + 检查方向 + 问题 |

相关测试覆盖：

- Phase 4/5 纯函数的 L1～L4、三语言、单目标和确定性；
- 正确、语法、安全、错误及无自适应目标路径；
- 主渲染失败后推荐 L2、实际 L1 的 `OVERRIDDEN` 降级；
- Submission、审计和 BKT 实际等级一致；
- 相同 `attempt_id` 重放不重新运行 Phase 1/4/5，不重复写状态；
- 学生响应中 Phase 2 包与参考答案不泄漏；
- Alembic 单一 head 和 MySQL 离线 DDL 生成。

## 7. 当前明确边界

- 支架权重和 BKT 参数仍是 `UNCALIBRATED_MVP`，需要真实学习数据做离线校准和 A/B 验证。
- `behavioral_support_need` 已接入版本化行为代理；它只反映当前活动窗口内的可信语义观测模式，不把答题时长直接宣称为真实心理疲劳。
- `recent_unassisted_success` 在提交前提示暴露事件建立前仍保守为 0。
- Phase 5 已实现可选 LLM 改写层；代码默认关闭，当前工作区 `.env` 已显式打开 `LLM_TEACHING_ENABLED=true`。已通过 CC Switch 的 `jiji` Responses provider（`gpt-5.6-luna`）最小请求，并在 Docker PostgreSQL 16 的真实路由中完成 Phase1→Phase5 集成验收：Phase2 复核被接受、Phase3 更新、Phase4 分层决策和 Phase5 LLM 文案生成均通过。该证据证明的是 provider 接入、协议契约、超时/回退和答案安全链路可运行；真实教学语言质量、规模化成本/延迟、模型漂移和大规模答案泄漏红队验收仍未完成。
- 聊天记录尚未以 `submission_id` 直接关联，属于后续审计增强项。
- `UNDECIDED` 仍在写入任何 Submission/BKT 前返回 422，不制造学习观测。

因此当前成果可以称为“Phase 4～6 v1 完整可运行通路”，但不是已经完成实验校准的最终教学策略。
