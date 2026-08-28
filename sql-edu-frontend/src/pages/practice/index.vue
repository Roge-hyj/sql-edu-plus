<template>
  <view class="page practice-page">
    <app-navbar :title="navTitle">
      <template #right>
        <lang-switch />
      </template>
    </app-navbar>

    <!-- 题目卡 -->
    <view v-if="question" class="card anim-in">
      <view class="q-meta">
        <text class="q-id">#{{ question.id }}</text>
        <difficulty-badge :value="question.display_difficulty ?? question.difficulty" />
        <view v-if="question.sql_dialect" class="chip chip-brand">
          {{ question.sql_dialect.toUpperCase() }}
        </view>
        <view v-if="question.engine_version" class="chip">
          {{ question.engine_version }}
        </view>
      </view>
      <text class="q-title">{{ localizedQuestionTitle(question) }}</text>
      <text class="q-content">{{ localizedQuestionContent(question) }}</text>

      <!-- 必需输出列（后端约定：供学生端显著展示） -->
      <view v-if="requiredColumns.length" class="required-columns">
        <view class="rc-head">
          <text class="rc-icon">🎯</text>
          <text class="rc-title">{{ L.requiredColumns }}</text>
        </view>
        <view class="rc-body">
          <view v-for="(col, i) in requiredColumns" :key="i" class="rc-col">
            <text class="rc-col-text">{{ col }}</text>
          </view>
        </view>
      </view>
    </view>
    <view v-else class="card">
      <view class="loading-box">
        <text class="anim-pulse">{{ loadingQuestion ? L.loadingQuestion : L.questionMissing }}</text>
      </view>
    </view>

    <!-- 表结构预览 -->
    <view v-if="schemaTables.length" class="card">
      <view class="card-title" @tap="schemaOpen = !schemaOpen">
        <view class="dot" />
        <text>{{ L.schemaPreview }}</text>
        <text class="extra">{{ schemaTables.length }} {{ L.tablesUnit }} {{ schemaOpen ? "∧" : "∨" }}</text>
      </view>
      <view v-if="schemaOpen">
        <view v-for="(table, ti) in schemaTables" :key="ti" class="schema-table">
          <view class="schema-table-name">
            <text class="stn-icon">▦</text>
            <text class="stn-text">{{ table.name }}</text>
            <text class="stn-cols">{{ table.columns.length }} {{ L.columnsUnit }}</text>
          </view>
          <scroll-view scroll-x class="schema-scroll" :show-scrollbar="false">
            <view class="schema-grid" :style="{ width: schemaGridWidth(table) }">
              <view class="schema-row schema-head">
                <view v-for="(col, ci) in table.columns" :key="ci" class="schema-cell">
                  <text class="schema-cell-text th">{{ col }}</text>
                </view>
              </view>
              <view
                v-for="(row, ri) in table.rows.slice(0, 4)"
                :key="ri"
                class="schema-row"
                :class="{ alt: ri % 2 === 1 }"
              >
                <view v-for="(cell, ci) in row" :key="ci" class="schema-cell">
                  <text class="schema-cell-text" :class="{ nullv: cell === null }">
                    {{ cellText(cell) }}
                  </text>
                </view>
              </view>
            </view>
          </scroll-view>
          <text v-if="table.rows.length > 4" class="schema-more">
            {{ L.moreRows.replace("{n}", String(table.rows.length)) }}
          </text>
        </view>
      </view>
    </view>

    <!-- SQL 编辑器 -->
    <view class="card">
      <view class="card-title">
        <view class="dot" />
        <text>{{ L.sqlEditor }}</text>
        <text class="extra mono">{{ sqlText.length }} {{ L.charsUnit }}</text>
      </view>
      <view class="editor-shell">
        <view class="editor-gutter">
          <text v-for="n in gutterLines" :key="n" class="gutter-num">{{ n }}</text>
        </view>
        <textarea
          v-model="sqlText"
          class="sql-editor"
          :placeholder="L.editorPlaceholder"
          placeholder-style="color:#5d6b8c"
          :maxlength="-1"
          :auto-height="false"
          :adjust-position="false"
          :disabled="checking"
        />
      </view>
      <view class="editor-actions">
        <button class="btn btn-plain btn-sm" :disabled="checking || !sqlText" @tap="resetSql">
          {{ L.reset }}
        </button>
        <button
          class="btn btn-primary editor-submit"
          :class="{ 'is-disabled': checking || !sqlText.trim() }"
          :loading="checking"
          :disabled="checking || !sqlText.trim()"
          @tap="submitSql"
        >
          {{ checking ? L.checking : L.submitCheck }}
        </button>
      </view>
    </view>

    <!-- 判题结果 -->
    <view v-if="result" class="card result-card anim-in">
      <!-- 结果横幅 -->
      <view class="verdict" :class="verdictClass">
        <text class="verdict-icon">{{ verdictIcon }}</text>
        <view class="verdict-main">
          <text class="verdict-title">{{ verdictTitle }}</text>
          <text class="verdict-sub">{{ verdictSub }}</text>
        </view>
      </view>

      <!-- 元信息 -->
      <view class="result-meta">
        <view class="chip mono">#{{ result.submission_id }}</view>
        <view v-if="result.judge_status" class="chip mono">{{ result.judge_status }}</view>
        <view v-if="supportLevelText" class="chip chip-brand">{{ supportLevelText }}</view>
        <view v-if="result.idempotency_replayed" class="chip">{{ L.replayed }}</view>
      </view>

      <!-- 安全拦截提示 -->
      <view v-if="result.is_safety_blocked" class="safety-block">
        <text class="sb-title">🛡️ {{ L.safetyBlocked }}</text>
        <text class="sb-desc">{{ L.safetyBlockedDesc }}</text>
      </view>

      <!-- AI 反馈 -->
      <view class="feedback-block">
        <view class="feedback-head">
          <text class="feedback-avatar">AI</text>
          <text class="feedback-title">{{ L.aiFeedback }}</text>
        </view>
        <view class="md-body">
          <rich-text :nodes="feedbackHtml" />
        </view>
      </view>
    </view>

    <!-- AI 助教对话 -->
    <view class="card">
      <view class="card-title">
        <view class="dot" />
        <text>{{ L.chatTitle }}</text>
        <text class="extra link" @tap="confirmClearChat">{{ L.clearChat }}</text>
      </view>
      <scroll-view
        scroll-y
        class="chat-scroll"
        :scroll-into-view="chatAnchor"
        scroll-with-animation
      >
        <view v-if="chatMessages.length === 0" class="chat-empty">
          <text class="chat-empty-icon">💬</text>
          <text class="chat-empty-text">{{ L.chatEmpty }}</text>
        </view>
        <view
          v-for="msg in chatMessages"
          :key="msg.id"
          :id="`msg-${msg.id}`"
          class="chat-row"
          :class="{ mine: msg.role === 'user', system: msg.role === 'system' }"
        >
          <text v-if="msg.role === 'system'" class="chat-system">{{ msg.content }}</text>
          <template v-else>
            <view class="chat-avatar" :class="{ ai: msg.role === 'assistant' }">
              <text>{{ msg.role === "assistant" ? "AI" : avatarChar }}</text>
            </view>
            <view class="chat-bubble" :class="{ ai: msg.role === 'assistant' }">
              <view class="md-body md-in-bubble">
                <rich-text :nodes="renderMarkdown(msg.content)" />
              </view>
              <text class="chat-time">{{ formatChatTime(msg.created_at) }}</text>
            </view>
          </template>
        </view>
      </scroll-view>
      <view class="chat-input-row">
        <input
          v-model="chatInput"
          class="chat-input"
          :placeholder="L.chatPlaceholder"
          placeholder-class="ph"
          :maxlength="2000"
          confirm-type="send"
          @confirm="sendChat"
        />
        <button
          class="btn btn-primary btn-sm chat-send"
          :class="{ 'is-disabled': sendingChat || !chatInput.trim() }"
          :disabled="sendingChat || !chatInput.trim()"
          @tap="sendChat"
        >
          {{ L.send }}
        </button>
      </view>
    </view>

    <!-- 我的提交记录 -->
    <view class="card">
      <view class="card-title" @tap="attemptsOpen = !attemptsOpen">
        <view class="dot" />
        <text>{{ L.myAttempts }}</text>
        <text class="extra">{{ submissions.length }} {{ L.timesUnit }} {{ attemptsOpen ? "∧" : "∨" }}</text>
      </view>
      <view v-if="attemptsOpen">
        <view v-if="submissions.length === 0" class="attempts-empty">
          <text>{{ L.attemptsEmpty }}</text>
        </view>
        <view
          v-for="(s, i) in submissions"
          :key="s.id"
          class="attempt-item"
          @tap="toggleAttemptSql(s.id)"
        >
          <view class="attempt-head">
            <text class="attempt-no">{{ L.attemptNo.replace("{n}", String(submissions.length - i)) }}</text>
            <view class="chip" :class="s.is_correct ? 'chip-success' : 'chip-danger'">
              {{ s.is_correct ? "✓ " + L.verdictCorrect : "✕ " + L.verdictIncorrect }}
            </view>
            <text class="attempt-time">{{ formatChatTime(s.created_at) }}</text>
          </view>
          <view v-if="expandedAttemptId === s.id" class="attempt-sql">
            <text class="attempt-sql-text">{{ s.student_sql }}</text>
          </view>
        </view>
      </view>
    </view>

    <!-- 难度评分弹窗 -->
    <view v-if="ratingVisible" class="rating-mask" @tap="closeRating(false)">
      <view class="rating-sheet" @tap.stop>
        <text class="rating-title">{{ L.ratingTitle }}</text>
        <text class="rating-desc">{{ L.ratingDesc }}</text>
        <view class="rating-grid">
          <view
            v-for="n in 10"
            :key="n"
            class="rating-cell"
            :class="{ active: ratingValue === n }"
            :style="ratingValue === n ? { background: difficultyColor(n), color: '#fff' } : {}"
            @tap="ratingValue = n"
          >
            {{ n }}
          </view>
        </view>
        <view class="rating-actions">
          <button class="btn btn-plain btn-sm" @tap="closeRating(false)">{{ L.skipRating }}</button>
          <button
            class="btn btn-primary btn-sm rating-submit"
            :class="{ 'is-disabled': !ratingValue }"
            :disabled="!ratingValue"
            @tap="submitRating"
          >
            {{ L.submitRating }}
          </button>
        </view>
      </view>
    </view>
  </view>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { onLoad, onShow } from "@dcloudio/uni-app";
import {
  checkSql,
  clearChatMessages,
  createSqlCheckAttempt,
  getChatMessages,
  getMySubmissions,
  chatWithTeacher,
  type SqlCheckAttempt,
} from "@/api/ai";
import { getQuestion, generateQuestionI18n, submitDifficultyFeedback } from "@/api/questions";
import { ensureAuthed } from "@/utils/auth";
import {
  language,
  localizedQuestionContent,
  localizedQuestionTitle,
  type AiLanguage,
} from "@/utils/i18n";
import { renderMarkdown } from "@/utils/markdown";
import {
  cellText,
  difficultyColor,
  formatChatTime,
  parseSchemaPreview,
  type SchemaTable,
} from "@/utils/format";
import type { ChatMessage, QuestionOut, SqlCheckResponse, SubmissionOut } from "@/types";

const STRINGS = {
  "zh-CN": {
    loadingQuestion: "加载题目中…",
    questionMissing: "题目不存在或已下架",
    requiredColumns: "要求的输出列名",
    schemaPreview: "表结构预览",
    tablesUnit: "张表",
    columnsUnit: "列",
    moreRows: "示例仅展示前 4 行，共 {n} 行",
    sqlEditor: "SQL 编辑器",
    editorPlaceholder: "在此编写你的 SQL 查询…",
    reset: "重置",
    submitCheck: "提交判题",
    checking: "判题中…",
    replayed: "幂等重放",
    verdictCorrect: "回答正确",
    verdictIncorrect: "回答不正确",
    verdictSafety: "危险操作已拦截",
    verdictSubCorrect: "结果与标准答案一致，干得漂亮！",
    verdictSubIncorrect: "查看下方诊断反馈，修改后再试一次。",
    verdictSubSafety: "系统已拒绝执行该语句。仅允许只读查询，禁止增删改操作。",
    supportLevelLabel: "辅导等级",
    supportLevels: ["轻提示", "定向引导", "详细讲解", "完整辅导"],
    safetyBlocked: "危险操作已拦截",
    safetyBlockedDesc: "检测到非查询语句（如修改数据或结构），已安全拦截。本系统仅接受 SELECT 查询。",
    aiFeedback: "AI 诊断反馈",
    chatTitle: "AI 助教",
    clearChat: "清空记录",
    clearChatConfirm: "确定清空当前题目的全部对话记录吗？",
    chatEmpty: "先提交一次 SQL，助教会针对你的作答给出针对性建议；也可以直接提问。",
    chatPlaceholder: "向助教提问…",
    send: "发送",
    myAttempts: "我的提交记录",
    timesUnit: "次",
    attemptsEmpty: "还没有提交记录，写下你的第一条 SQL 吧。",
    attemptNo: "第 {n} 次",
    ratingTitle: "这道题实际难度如何？",
    ratingDesc: "你的评分将用于校准题目的动态难度",
    skipRating: "跳过",
    submitRating: "提交评分",
    ratingDone: "评分已记录",
    charsUnit: "字符",
    emptySql: "请先编写 SQL 语句",
  },
  en: {
    loadingQuestion: "Loading question…",
    questionMissing: "Question not found or unpublished",
    requiredColumns: "Required output columns",
    schemaPreview: "Schema preview",
    tablesUnit: "tables",
    columnsUnit: "cols",
    moreRows: "Showing first 4 of {n} rows",
    sqlEditor: "SQL editor",
    editorPlaceholder: "Write your SQL query here…",
    reset: "Reset",
    submitCheck: "Submit & run",
    checking: "Judging…",
    replayed: "replayed",
    verdictCorrect: "Correct",
    verdictIncorrect: "Incorrect",
    verdictSafety: "Blocked",
    verdictSubCorrect: "Your output matches the reference solution. Great job!",
    verdictSubIncorrect: "Read the feedback below, revise, and try again.",
    verdictSubSafety: "The statement was rejected. Only read-only SELECT queries are accepted.",
    supportLevelLabel: "Support",
    supportLevels: ["Nudge", "Guided", "Detailed", "Full walkthrough"],
    safetyBlocked: "Dangerous statement blocked",
    safetyBlockedDesc: "A non-query statement (data or schema change) was detected and blocked. Only SELECT queries are accepted.",
    aiFeedback: "AI feedback",
    chatTitle: "AI tutor",
    clearChat: "Clear",
    clearChatConfirm: "Clear all chat history for this question?",
    chatEmpty: "Submit your SQL first — the tutor then gives targeted advice. You can also just ask.",
    chatPlaceholder: "Ask the tutor…",
    send: "Send",
    myAttempts: "My attempts",
    timesUnit: "attempts",
    attemptsEmpty: "No attempts yet — write your first SQL!",
    attemptNo: "Attempt {n}",
    ratingTitle: "How hard was this question?",
    ratingDesc: "Your rating calibrates the live difficulty",
    skipRating: "Skip",
    submitRating: "Submit",
    ratingDone: "Rating saved",
    charsUnit: "chars",
    emptySql: "Write some SQL first",
  },
  "zh-TW": {
    loadingQuestion: "載入題目中…",
    questionMissing: "題目不存在或已下架",
    requiredColumns: "要求的輸出欄名",
    schemaPreview: "表結構預覽",
    tablesUnit: "張表",
    columnsUnit: "欄",
    moreRows: "範例僅展示前 4 行，共 {n} 行",
    sqlEditor: "SQL 編輯器",
    editorPlaceholder: "在此編寫你的 SQL 查詢…",
    reset: "重置",
    submitCheck: "提交判題",
    checking: "判題中…",
    replayed: "冪等重放",
    verdictCorrect: "回答正確",
    verdictIncorrect: "回答不正確",
    verdictSafety: "危險操作已攔截",
    verdictSubCorrect: "結果與標準答案一致，幹得漂亮！",
    verdictSubIncorrect: "查看下方診斷回饋，修改後再試一次。",
    verdictSubSafety: "系統已拒絕執行該語句。僅允許唯讀查詢，禁止增刪改操作。",
    supportLevelLabel: "輔導等級",
    supportLevels: ["輕提示", "定嚮導引", "詳細講解", "完整輔導"],
    safetyBlocked: "危險操作已攔截",
    safetyBlockedDesc: "偵測到非查詢語句（如修改資料或結構），已安全攔截。本系統僅接受 SELECT 查詢。",
    aiFeedback: "AI 診斷回饋",
    chatTitle: "AI 助教",
    clearChat: "清空記錄",
    clearChatConfirm: "確定清空當前題目的全部對話記錄嗎？",
    chatEmpty: "先提交一次 SQL，助教會針對你的作答給出針對性建議；也可以直接提問。",
    chatPlaceholder: "向助教提問…",
    send: "發送",
    myAttempts: "我的提交記錄",
    timesUnit: "次",
    attemptsEmpty: "還沒有提交記錄，寫下你的第一條 SQL 吧。",
    attemptNo: "第 {n} 次",
    ratingTitle: "這道題實際難度如何？",
    ratingDesc: "你的評分將用於校準題目的動態難度",
    skipRating: "跳過",
    submitRating: "提交評分",
    ratingDone: "評分已記錄",
    charsUnit: "字符",
    emptySql: "請先編寫 SQL 語句",
  },
} as const;

const L = computed(() => STRINGS[language.value]);

const questionId = ref(0);
const question = ref<QuestionOut | null>(null);
const loadingQuestion = ref(true);
const schemaOpen = ref(true);
const attemptsOpen = ref(false);
const expandedAttemptId = ref<number | null>(null);

const sqlText = ref("");
const checking = ref(false);
const result = ref<SqlCheckResponse | null>(null);
/** 一次提交动作对应一个 attempt；网络层重试复用同一 id。 */
let pendingAttempt: SqlCheckAttempt | null = null;
let pendingAttemptSql = "";

const chatMessages = ref<ChatMessage[]>([]);
const chatInput = ref("");
const sendingChat = ref(false);
const chatAnchor = ref("");

const submissions = ref<SubmissionOut[]>([]);

const ratingVisible = ref(false);
const ratingValue = ref(0);
/** 已评分过的题目不再重复弹窗（本地记忆） */
const ratedQids = ref<Set<number>>(new Set());

const i18nRequested = new Set<number>();

const avatarChar = computed(() => {
  const user = uni.getStorageSync("user");
  return String(user?.username || "ME").slice(0, 1).toUpperCase();
});

const navTitle = computed(() =>
  question.value ? `#${question.value.id} ${localizedQuestionTitle(question.value)}` : "SQL",
);

const requiredColumns = computed(() => {
  const raw = question.value?.required_output_columns;
  if (!raw) return [];
  return raw
    .split(/[,，\n、]/)
    .map((s) => s.trim())
    .filter(Boolean);
});

const schemaTables = computed<SchemaTable[]>(() => parseSchemaPreview(question.value?.schema_preview));

const gutterLines = computed(() => {
  const lines = sqlText.value.split("\n").length;
  return Math.max(6, Math.min(lines, 40));
});

const feedbackHtml = computed(() => {
  const hint = result.value?.hint as { overall_comment?: string } | undefined;
  return renderMarkdown(hint?.overall_comment || "");
});

const verdictClass = computed(() => {
  if (!result.value) return "";
  if (result.value.is_safety_blocked) return "warn";
  return result.value.is_correct ? "ok" : "bad";
});

const verdictIcon = computed(() => {
  if (!result.value) return "";
  if (result.value.is_safety_blocked) return "🛡️";
  return result.value.is_correct ? "🎉" : "💭";
});

const verdictTitle = computed(() => {
  if (!result.value) return "";
  if (result.value.is_safety_blocked) return L.value.verdictSafety;
  return result.value.is_correct ? L.value.verdictCorrect : L.value.verdictIncorrect;
});

const verdictSub = computed(() => {
  if (!result.value) return "";
  if (result.value.is_safety_blocked) return L.value.verdictSubSafety;
  return result.value.is_correct ? L.value.verdictSubCorrect : L.value.verdictSubIncorrect;
});

const supportLevelText = computed(() => {
  const ts = result.value?.teaching_support;
  if (!ts || !ts.delivered_support_level) return "";
  const idx = Math.max(0, Math.min(3, ts.delivered_support_level - 1));
  return `${L.value.supportLevelLabel} L${ts.delivered_support_level} · ${L.value.supportLevels[idx]}`;
});

function schemaGridWidth(table: SchemaTable): string {
  const width = Math.max(...table.columns.map((c) => c.length), 4);
  const cols = table.columns.length;
  return `${cols * Math.max(width * 16 + 40, 140)}rpx`;
}

onLoad((options) => {
  const id = Number(options?.id ?? 0);
  if (!id || !Number.isFinite(id)) {
    uni.showToast({ title: L.value.questionMissing, icon: "none" });
    setTimeout(() => uni.reLaunch({ url: "/pages/index/index" }), 800);
    return;
  }
  questionId.value = id;
  try {
    const raw = uni.getStorageSync("rated_qids");
    if (Array.isArray(raw)) ratedQids.value = new Set(raw);
  } catch {
    /* ignore */
  }
});

onShow(() => {
  if (!ensureAuthed()) return;
  if (questionId.value) {
    loadQuestion();
    loadChat();
    loadSubmissions();
  }
});

async function loadQuestion() {
  loadingQuestion.value = true;
  try {
    question.value = await getQuestion(questionId.value);
    restoreDraft();
    maybeRequestI18n();
  } catch {
    question.value = null;
  } finally {
    loadingQuestion.value = false;
  }
}

/** 非简中语言且缺少翻译时，触发一次后端补全。 */
function maybeRequestI18n() {
  const q = question.value;
  if (!q || language.value === "zh-CN" || i18nRequested.has(q.id)) return;
  const needEn = language.value === "en" && !(q.title_en && q.content_en);
  const needTw = language.value === "zh-TW" && !(q.title_zh_tw && q.content_zh_tw);
  if (!needEn && !needTw) return;
  i18nRequested.add(q.id);
  generateQuestionI18n(q.id)
    .then((updated) => {
      if (updated && question.value && updated.id === question.value.id) {
        question.value = updated;
      }
    })
    .catch(() => {
      /* 静默失败，保留原文 */
    });
}

/* ---------- 草稿 ---------- */

let draftTimer: ReturnType<typeof setTimeout> | null = null;

watch(sqlText, (text) => {
  if (draftTimer) clearTimeout(draftTimer);
  draftTimer = setTimeout(() => {
    if (questionId.value) {
      uni.setStorageSync(`draft_sql_${questionId.value}`, text);
    }
  }, 500);
});

function restoreDraft() {
  try {
    const draft = uni.getStorageSync(`draft_sql_${questionId.value}`);
    if (typeof draft === "string" && draft.trim()) sqlText.value = draft;
  } catch {
    /* ignore */
  }
}

function resetSql() {
  sqlText.value = "";
  if (questionId.value) uni.removeStorageSync(`draft_sql_${questionId.value}`);
}

/* ---------- 判题 ---------- */

async function submitSql() {
  const sql = sqlText.value.trim();
  if (!sql) {
    uni.showToast({ title: L.value.emptySql, icon: "none" });
    return;
  }
  checking.value = true;
  try {
    // 上次提交失败且 SQL 未变时，复用同一 attempt_id 触发服务端幂等重放
    if (!pendingAttempt || pendingAttemptSql !== sql) {
      pendingAttempt = createSqlCheckAttempt({
        student_sql: sql,
        question_id: questionId.value,
        language: language.value as AiLanguage,
      });
      pendingAttemptSql = sql;
    } else {
      pendingAttempt.language = language.value as AiLanguage;
    }
    const res = await checkSql(pendingAttempt);
    result.value = res;
    pendingAttempt = null;
    pendingAttemptSql = "";
    loadChat();
    loadSubmissions();
    if (res.is_correct && !res.idempotency_replayed && !ratedQids.value.has(questionId.value)) {
      setTimeout(() => {
        ratingValue.value = 0;
        ratingVisible.value = true;
      }, 600);
    }
  } catch {
    // 请求失败保留 pendingAttempt 供重试复用
  } finally {
    checking.value = false;
  }
}

/* ---------- 难度评分 ---------- */

function closeRating(_submitted: boolean) {
  ratingVisible.value = false;
}

async function submitRating() {
  if (!ratingValue.value) return;
  try {
    const res = await submitDifficultyFeedback(questionId.value, ratingValue.value);
    if (res?.result === "success") {
      ratedQids.value.add(questionId.value);
      uni.setStorageSync("rated_qids", Array.from(ratedQids.value));
      uni.showToast({ title: L.value.ratingDone, icon: "none" });
    }
  } catch {
    /* 统一错误处理 */
  } finally {
    ratingVisible.value = false;
  }
}

/* ---------- AI 对话 ---------- */

async function loadChat() {
  try {
    chatMessages.value = await getChatMessages({ question_id: questionId.value, limit: 120 });
    scrollToLastMessage();
  } catch {
    chatMessages.value = [];
  }
}

function scrollToLastMessage() {
  const last = chatMessages.value[chatMessages.value.length - 1];
  if (last) {
    setTimeout(() => {
      chatAnchor.value = "";
      setTimeout(() => {
        chatAnchor.value = `msg-${last.id}`;
      }, 50);
    }, 100);
  }
}

async function sendChat() {
  const message = chatInput.value.trim();
  if (!message || sendingChat.value) return;
  sendingChat.value = true;
  const backup = chatInput.value;
  chatInput.value = "";
  try {
    await chatWithTeacher({
      question_id: questionId.value,
      message,
      language: language.value as AiLanguage,
    });
    await loadChat();
  } catch {
    chatInput.value = backup;
  } finally {
    sendingChat.value = false;
  }
}

function confirmClearChat() {
  uni.showModal({
    title: L.value.clearChat,
    content: L.value.clearChatConfirm,
    confirmColor: "#E5484D",
    success: async (r) => {
      if (!r.confirm) return;
      try {
        await clearChatMessages(questionId.value);
        chatMessages.value = [];
      } catch {
        /* 统一错误处理 */
      }
    },
  });
}

/* ---------- 提交记录 ---------- */

async function loadSubmissions() {
  try {
    submissions.value = await getMySubmissions({ question_id: questionId.value, limit: 100 });
  } catch {
    submissions.value = [];
  }
}

function toggleAttemptSql(id: number) {
  expandedAttemptId.value = expandedAttemptId.value === id ? null : id;
}
</script>

<style lang="scss" scoped>
.practice-page {
  max-width: 700px;
  margin: 0 auto;
}

.mono {
  font-family: $font-mono;
}

/* ---------- 题目 ---------- */
.q-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 14rpx;
  margin-bottom: 18rpx;

  .q-id {
    font-family: $font-mono;
    font-size: 22rpx;
    font-weight: 700;
    color: $brand-deep;
    background: $brand-soft;
    padding: 4rpx 14rpx;
    border-radius: 8rpx;
  }
}

.q-title {
  display: block;
  font-size: 32rpx;
  font-weight: 700;
  color: $text-1;
  line-height: 1.5;
}

.q-content {
  display: block;
  margin-top: 12rpx;
  font-size: 27rpx;
  color: $text-2;
  line-height: 1.7;
  white-space: pre-wrap;
}

.required-columns {
  margin-top: 22rpx;
  background: $warning-soft;
  border: 2rpx solid rgba(245, 165, 36, 0.25);
  border-radius: 16rpx;
  padding: 20rpx 24rpx;

  .rc-head {
    display: flex;
    align-items: center;
    gap: 10rpx;

    .rc-title {
      font-size: 25rpx;
      font-weight: 700;
      color: #c77d00;
    }
  }

  .rc-body {
    display: flex;
    flex-wrap: wrap;
    gap: 12rpx;
    margin-top: 14rpx;
  }

  .rc-col {
    background: rgba(255, 255, 255, 0.85);
    border-radius: 8rpx;
    padding: 6rpx 16rpx;

    .rc-col-text {
      font-family: $font-mono;
      font-size: 24rpx;
      color: $text-1;
    }
  }
}

.loading-box {
  text-align: center;
  padding: 80rpx 0;
  color: $text-3;
  font-size: 26rpx;
}

/* ---------- 表结构 ---------- */
.schema-table {
  margin-bottom: 26rpx;

  &:last-child {
    margin-bottom: 4rpx;
  }
}

.schema-table-name {
  display: flex;
  align-items: center;
  gap: 10rpx;
  margin-bottom: 12rpx;

  .stn-icon {
    color: $brand;
    font-size: 24rpx;
  }

  .stn-text {
    font-family: $font-mono;
    font-size: 26rpx;
    font-weight: 700;
    color: $text-1;
  }

  .stn-cols {
    font-size: 20rpx;
    color: $text-3;
  }
}

.schema-scroll {
  border: 2rpx solid $border-color;
  border-radius: 14rpx;
  background: #fafbfe;
}

.schema-grid {
  padding: 0;
}

.schema-row {
  display: flex;

  &.schema-head {
    background: #eef2fb;

    .schema-cell-text {
      color: $text-1;
      font-weight: 700;
    }
  }

  &.alt {
    background: rgba(238, 242, 251, 0.45);
  }
}

.schema-cell {
  flex-shrink: 0;
  width: 180rpx;
  padding: 12rpx 18rpx;
  border-right: 2rpx solid #eef1f8;
  overflow: hidden;

  &:last-child {
    border-right: none;
  }
}

.schema-cell-text {
  font-size: 23rpx;
  color: $text-2;
  font-family: $font-mono;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;

  &.nullv {
    color: #b6bece;
    font-style: italic;
  }
}

.schema-more {
  display: block;
  margin-top: 8rpx;
  font-size: 20rpx;
  color: $text-3;
}

/* ---------- SQL 编辑器 ---------- */
.editor-shell {
  display: flex;
  background: #101528;
  border-radius: 18rpx;
  overflow: hidden;
}

.editor-gutter {
  padding: 24rpx 0;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  background: rgba(255, 255, 255, 0.03);
  border-right: 2rpx solid rgba(255, 255, 255, 0.06);
  min-width: 64rpx;

  .gutter-num {
    font-family: $font-mono;
    font-size: 24rpx;
    color: #4c5878;
    line-height: 1.9;
    padding: 0 16rpx;
  }
}

.sql-editor {
  flex: 1;
  width: auto;
  min-height: 380rpx;
  padding: 24rpx 26rpx;
  font-family: $font-mono;
  font-size: 26rpx;
  line-height: 1.9;
  color: #dbe6ff;
  background: transparent;
}

.editor-actions {
  display: flex;
  gap: 16rpx;
  margin-top: 20rpx;

  .editor-submit {
    flex: 1;
  }
}

/* ---------- 判题结果 ---------- */
.verdict {
  display: flex;
  align-items: center;
  gap: 20rpx;
  border-radius: 18rpx;
  padding: 26rpx 28rpx;
  margin-bottom: 20rpx;

  .verdict-icon {
    font-size: 48rpx;
    flex-shrink: 0;
  }

  .verdict-title {
    display: block;
    font-size: 32rpx;
    font-weight: 800;
  }

  .verdict-sub {
    display: block;
    margin-top: 4rpx;
    font-size: 23rpx;
    opacity: 0.85;
    line-height: 1.55;
  }

  &.ok {
    background: $success-soft;

    .verdict-title {
      color: $success;
    }

    .verdict-sub {
      color: #0b7d5b;
    }
  }

  &.bad {
    background: $danger-soft;

    .verdict-title {
      color: $danger;
    }

    .verdict-sub {
      color: #b23b40;
    }
  }

  &.warn {
    background: $warning-soft;

    .verdict-title {
      color: #c77d00;
    }

    .verdict-sub {
      color: #a06500;
    }
  }
}

.result-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 12rpx;
  margin-bottom: 18rpx;
}

.safety-block {
  background: #fff7e8;
  border: 2rpx dashed #f0b429;
  border-radius: 14rpx;
  padding: 18rpx 22rpx;
  margin-bottom: 18rpx;

  .sb-title {
    display: block;
    font-size: 25rpx;
    font-weight: 700;
    color: #b57b00;
  }

  .sb-desc {
    display: block;
    margin-top: 6rpx;
    font-size: 22rpx;
    color: #9a6d00;
    line-height: 1.6;
  }
}

.feedback-block {
  background: #fafbfe;
  border-radius: 16rpx;
  padding: 24rpx 26rpx;

  .feedback-head {
    display: flex;
    align-items: center;
    gap: 14rpx;
    margin-bottom: 14rpx;
  }

  .feedback-avatar {
    width: 52rpx;
    height: 52rpx;
    border-radius: 14rpx;
    background: $brand-gradient;
    color: #fff;
    font-size: 22rpx;
    font-weight: 800;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .feedback-title {
    font-size: 26rpx;
    font-weight: 700;
    color: $text-1;
  }
}

/* ---------- 对话 ---------- */
.chat-scroll {
  height: 560rpx;
  background: #fafbfe;
  border-radius: 16rpx;
  padding: 20rpx;
  box-sizing: border-box;
}

.chat-empty {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14rpx;
  padding: 0 40rpx;

  .chat-empty-icon {
    font-size: 56rpx;
    opacity: 0.6;
  }

  .chat-empty-text {
    font-size: 24rpx;
    color: $text-3;
    text-align: center;
    line-height: 1.7;
  }
}

.chat-row {
  display: flex;
  margin-bottom: 22rpx;

  &.mine {
    flex-direction: row-reverse;

    .chat-bubble {
      background: $brand;
      color: #fff;
      border-radius: 20rpx 6rpx 20rpx 20rpx;

      .chat-time {
        color: rgba(255, 255, 255, 0.65);
      }

      :deep(.md-p),
      :deep(.md-ul li) {
        color: #fff;
      }
    }

    .chat-avatar {
      background: #e3e8f4;
      color: $text-2;
    }
  }

  &.system {
    justify-content: center;

    .chat-system {
      font-size: 20rpx;
      color: $text-3;
      background: #eef1f8;
      padding: 6rpx 20rpx;
      border-radius: 999rpx;
    }
  }
}

.chat-avatar {
  width: 56rpx;
  height: 56rpx;
  border-radius: 16rpx;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 22rpx;
  font-weight: 700;
  color: #fff;
  background: #b9c3d8;

  &.ai {
    background: $brand-gradient;
  }
}

.chat-bubble {
  max-width: 76%;
  margin: 0 16rpx;
  background: #fff;
  border-radius: 6rpx 20rpx 20rpx 20rpx;
  padding: 18rpx 22rpx;
  box-shadow: 0 4rpx 14rpx rgba(23, 29, 43, 0.06);

  .chat-time {
    display: block;
    margin-top: 8rpx;
    font-size: 18rpx;
    color: $text-3;
  }
}

.chat-input-row {
  display: flex;
  gap: 14rpx;
  margin-top: 18rpx;

  .chat-input {
    flex: 1;
    height: 76rpx;
    background: #f7f9fd;
    border-radius: 14rpx;
    padding: 0 24rpx;
    font-size: 26rpx;
  }

  .chat-send {
    flex-shrink: 0;
    padding: 0 34rpx;
  }
}

/* ---------- 提交记录 ---------- */
.attempt-item {
  padding: 20rpx 0;
  border-bottom: 2rpx solid #f1f3f9;

  &:last-child {
    border-bottom: none;
  }
}

.attempt-head {
  display: flex;
  align-items: center;
  gap: 14rpx;

  .attempt-no {
    font-size: 24rpx;
    font-weight: 600;
    color: $text-2;
    flex-shrink: 0;
  }

  .attempt-time {
    margin-left: auto;
    font-size: 20rpx;
    color: $text-3;
  }
}

.attempt-sql {
  margin-top: 14rpx;
  background: #101528;
  border-radius: 12rpx;
  padding: 18rpx 22rpx;

  .attempt-sql-text {
    font-family: $font-mono;
    font-size: 23rpx;
    color: #dbe6ff;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-all;
  }
}

.attempts-empty {
  padding: 40rpx 0;
  text-align: center;
  font-size: 24rpx;
  color: $text-3;
}

/* ---------- 评分弹窗 ---------- */
.rating-mask {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(15, 23, 42, 0.5);
  z-index: 200;
  display: flex;
  align-items: flex-end;
}

.rating-sheet {
  width: 100%;
  background: #fff;
  border-radius: 36rpx 36rpx 0 0;
  padding: 44rpx 40rpx calc(44rpx + env(safe-area-inset-bottom));
  animation: fade-slide-up 0.25s ease both;

  .rating-title {
    display: block;
    font-size: 32rpx;
    font-weight: 800;
    color: $text-1;
    text-align: center;
  }

  .rating-desc {
    display: block;
    margin-top: 8rpx;
    font-size: 24rpx;
    color: $text-3;
    text-align: center;
  }
}

.rating-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 16rpx;
  margin: 36rpx 0;
}

.rating-cell {
  width: calc((100% - 32rpx * 4) / 5);
  aspect-ratio: 1;
  border-radius: 16rpx;
  background: #f2f4fa;
  color: $text-2;
  font-size: 30rpx;
  font-weight: 700;
  font-family: $font-mono;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s ease;

  &.active {
    box-shadow: 0 8rpx 20rpx rgba(23, 29, 43, 0.18);
    transform: translateY(-2rpx);
  }
}

.rating-actions {
  display: flex;
  gap: 16rpx;

  .rating-submit {
    flex: 1;
  }
}
</style>
