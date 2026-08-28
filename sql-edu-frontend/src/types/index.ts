/**
 * 统一类型定义文件
 * 集中管理前端所有 TypeScript 类型
 */

// ==================== 通用响应 ====================

export type ResponseOut = {
  result: "success" | "failure";
  detail?: string | null;
};

// ==================== 用户相关 ====================

export type UserSchema = {
  id: number;
  email: string;
  username: string;
  role: "student" | "teacher";
};

export type LoginOut = {
  user: UserSchema;
  token: string;
  refresh_token: string;
};

// ==================== 题目相关 ====================

export type QuestionSkillRole = "PRIMARY" | "SUPPORTING";

export type QuestionSkillProvenance =
  | "AUTHOR_DECLARED"
  | "GENERATED"
  | "INFERRED";

/** 教师在出题时声明的 Q-matrix 条目。来源由服务端固定，客户端不可伪造。 */
export type QuestionSkillDeclaration = {
  skill_id: string;
  taxonomy_version?: string;
  role?: QuestionSkillRole;
  observable_on_correct?: boolean;
};

/** 仅教师写接口返回的权威题目—技能映射。 */
export type QuestionSkillOut = {
  skill_id: string;
  taxonomy_version: string;
  role: QuestionSkillRole;
  observable_on_correct: boolean;
  provenance: QuestionSkillProvenance;
};

export type QuestionOut = {
  id: number;
  title: string;
  content: string;
  /** 多语言题面（可选；未填写则前端回退到 title/content） */
  title_en?: string | null;
  content_en?: string | null;
  title_zh_tw?: string | null;
  content_zh_tw?: string | null;
  difficulty: number;
  /** 题目绑定的 SQL 方言（mysql/postgres/tsql/oracle 等；null 表示自动解析） */
  sql_dialect?: string | null;
  /** 判题引擎版本（如 MySQL 8.0.46） */
  engine_version?: string | null;
  /** 仅教师端写接口/受权响应返回；学生公开题目接口不会下发答案。 */
  correct_sql?: string;
  /** 表结构预览 JSON：tables[{name,columns,rows}]，供学生查看列名与示例数据 */
  schema_preview?: string | null;
  /** 要求的结果列名或完整说明，供学生端显著展示，避免列名不规范错误 */
  required_output_columns?: string | null;
  /** 动态难度 1～10，由客观数据与学生评分综合计算 */
  display_difficulty?: number | null;
  /** 仅教师写接口返回；学生公开题目接口不下发。 */
  skills?: QuestionSkillOut[];
};

/** SQL 知识点（入门→精通），教师端按知识点生成题目用 */
export type KnowledgePoint = {
  id: string;
  name: string;
  level: string;
  description: string;
  /** 多语言可选字段（后端可能返回），前端按 ai_language 选择显示 */
  name_i18n?: Record<string, string>;
  level_i18n?: Record<string, string>;
  description_i18n?: Record<string, string>;
};

// ==================== AI 判题相关 ====================

/** Backend-supported locale values for SQL diagnosis and teaching feedback. */
export type AiLanguage = "zh-CN" | "zh-TW" | "en";

export type SqlHintResponse = {
  hint: any;
};

export type TeachingSupportLevel = 1 | 2 | 3 | 4;
export type TeachingSupportStatus =
  | "APPLIED"
  | "OVERRIDDEN"
  | "NOT_APPLICABLE";
export type TeachingSupportGenerationSource = "LOCAL_TEMPLATE";
export type TeachingFeedbackStatus = "PRIMARY" | "FALLBACK" | "BYPASS";

/**
 * Learner-safe Phase 4–6 delivery metadata.
 *
 * This reports what support was actually delivered. It deliberately excludes
 * the internal Phase 2 evidence graph, selected skill identity, and BKT state.
 */
export type TeachingSupport = {
  schema_version: string;
  status: TeachingSupportStatus;
  language: AiLanguage;
  /** Null when Phase 3 had no eligible support recommendation. */
  recommended_support_level: TeachingSupportLevel | null;
  /** Assistance level represented by the feedback returned in this response. */
  delivered_support_level: TeachingSupportLevel;
  support_recommendation_applied: boolean;
  generation_source: TeachingSupportGenerationSource;
  focused_error_count: 0 | 1;
  answer_revealed: false;
  support_policy_version: string | null;
  action_policy_version: string;
  feedback_policy_version: string;
  feedback_status: TeachingFeedbackStatus;
};

export type DiagnosticEvidenceRefs = {
  diff_ids: string[];
  /** Strong-evidence partition; the knowledge-point union is display-only. */
  verified_diff_ids: string[];
  unverified_diff_ids: string[];
  obligation_ids: string[];
  mutation_test_ids: string[];
};

export type DiagnosticCandidate = {
  candidate_id: string;
  rule_id: string;
  title: string;
  stage: string;
  logical_stage: string;
  scope_id: string;
  knowledge_points: string[];
  knowledge_points_scope: "DISPLAY_UNION_ONLY";
  evidence_grade: string;
  evidence_refs: DiagnosticEvidenceRefs;
};

export type DiagnosticPipelineDiff = {
  diff_id: string;
  obligation_id?: string | null;
  scope_id: string;
  scope_kind: string;
  logical_stage: string;
  teaching_stage: string;
  clause: string;
  diff_type: string;
  knowledge_point_id: string;
  evidence_grade: string;
};

/** 学生可见的 Phase 2 包；不包含参考 SQL、参考 AST 或完整 witness 数据库。 */
export type PublicDiagnosticPackage = {
  schema_version: string;
  diagnosis_version: string;
  rule_catalog_version: string;
  verdict: "CORRECT" | "INCORRECT" | "UNDECIDED" | string;
  diagnosis_status: string;
  phase1: {
    status: string;
    equivalence_conclusion: string;
    judge_status: string;
  };
  ordered_diff_pipeline: DiagnosticPipelineDiff[];
  primary: DiagnosticCandidate | null;
  secondary: DiagnosticCandidate[];
  secondary_count: number;
  suppressed_symptoms: Array<Record<string, unknown>>;
  unresolved_count: number;
  witness: Record<string, unknown> | null;
  qss: Record<string, unknown>;
  narrative: {
    student_behavior: string;
    conflict_and_witness: string;
    guidance_question: string;
  };
  boundary_notes: string[];
};

export type SqlCheckResponse = {
  is_correct: boolean;
  hint: any;
  submission_id: number;
  /** Stable idempotency identity for the submit action. */
  attempt_id: string;
  /** Suppress duplicate UI effects when a committed response is replayed. */
  idempotency_replayed: boolean;
  error_message?: string | null;
  /** Phase 1 rich verdict 的裁决状态。 */
  judge_status: string;
  /** 因危险操作（DROP/DELETE 等）被拒，而非结果不正确 */
  is_safety_blocked?: boolean;
  /** @deprecated Phase 3 v1 uses separate support/challenge signals. */
  lambda_t?: number | null;
  /** Phase 4–6 support decision and the assistance actually delivered. */
  teaching_support: TeachingSupport | null;
  /** Auditable Phase 3 decision summary; authoritative skill mappings remain server-side. */
  phase3_learning?: {
    schema_version: string;
    runtime_policy_version: string;
    status: string;
    observation_count: number;
    state_update_count: number;
    bkt_parameter_version: string;
    priority_policy_version?: string | null;
    support_policy_version?: string | null;
    support_need?: number | null;
    recommended_support_level?: TeachingSupportLevel | null;
    delivered_support_level?: TeachingSupportLevel | null;
    support_recommendation_applied: boolean;
    challenge_policy_version: string;
    challenge_index_policy_version?: string | null;
    challenge_index?: number | null;
    /** Alias retained for v1 clients; use next_exercise_challenge_readiness. */
    challenge_readiness?: number | null;
    next_exercise_challenge_readiness?: number | null;
    challenge_usage: "NEXT_EXERCISE_DIFFICULTY_ONLY";
    challenge_aggregation_scope: "MIN_CURRENT_ATTEMPT_SKILLS";
    behavioral_proxy_status: string;
    behavioral_proxy_version?: string | null;
    behavioral_support_need?: number | null;
    behavioral_session_reset?: boolean;
    semantic_failure_count?: number;
    syntax_error_count?: number;
    attempt_context_status: string;
    runtime_support_reachability: string;
    calibration_status: string;
  } | null;
  /** 学生响应默认不返回内部原始观测，仅为旧客户端保留可选契约。 */
  observation?: Record<string, unknown> | null;
  /** 学生响应默认清空内部归因明细。 */
  error_attributions?: Array<Record<string, unknown>>;
  /**
   * @deprecated Compatibility-only Phase 2 payload. New learner UI should use
   * `hint` together with `teaching_support`; the server may omit this package.
   */
  diagnostic_package?: PublicDiagnosticPackage | null;
};

export type SubmissionOut = {
  id: number;
  user_id: number;
  question_id: number;
  attempt_id: string | null;
  student_sql: string;
  ai_hint: string | null;
  is_correct: boolean;
  hint_level: number;
  created_at: string;
};

// ==================== 对话相关 ====================

export type ChatMessage = {
  id: number;
  role: "system" | "user" | "assistant";
  content: string;
  created_at: string;
};

// ==================== 学习画像相关 ====================

/** /ai/mastery-radar 返回的 BKT 掌握度原始画像 */
export type MasteryRadar = {
  schema_version: string;
  /** 课程知识点 id → 后验掌握度（未观测时为 BKT P(L0)） */
  mastery_state: Record<string, number>;
  /** 原子技能 id → 后验掌握度 */
  atomic_mastery_state: Record<string, number>;
  state_details: Array<{
    taxonomy_version: string;
    skill_id: string;
    posterior_mastery: number;
    next_prior: number;
    observation_count: number;
    bkt_parameter_version: string;
    state_version: number;
  }>;
  display_value: string;
  unobserved_prior: number;
  bkt_parameter_version: string;
  bkt_calibration_status: string;
  bkt_calibration_artifact_digest: string | null;
  calibration_status: string;
};
