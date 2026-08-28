<template>
  <view class="page bank-page">
    <!-- 顶部 -->
    <view class="bank-hero">
      <view class="hero-top">
        <view class="greeting">
          <text class="greet-hi">{{ L.hello }}，</text>
          <text class="greet-name">{{ user?.username || "Learner" }}</text>
          <view v-if="isTeacher" class="role-badge">{{ L.teacher }}</view>
        </view>
        <view class="hero-actions">
          <lang-switch />
          <view class="avatar-btn" @tap="goProfile">
            <text class="avatar-text">{{ avatarChar }}</text>
          </view>
        </view>
      </view>
      <view class="hero-banner">
        <view class="hero-banner-main">
          <text class="hero-title">{{ L.appName }}</text>
          <text class="hero-sub">{{ L.heroTip }}</text>
        </view>
        <text class="hero-deco">⌘</text>
      </view>
    </view>

    <!-- 统计 -->
    <view class="stats-row">
      <view class="stat-card">
        <text class="stat-num">{{ questions.length }}</text>
        <text class="stat-label">{{ L.statQuestions }}</text>
      </view>
      <view class="stat-card">
        <text class="stat-num">{{ totalSubmissions }}</text>
        <text class="stat-label">{{ L.statSubmissions }}</text>
      </view>
      <view class="stat-card stat-success">
        <text class="stat-num">{{ solvedIds.size }}</text>
        <text class="stat-label">{{ L.statSolved }}</text>
      </view>
      <view class="stat-card stat-brand">
        <text class="stat-num">{{ accuracyText }}</text>
        <text class="stat-label">{{ L.statAccuracy }}</text>
      </view>
    </view>

    <!-- 教师入口 -->
    <view v-if="isTeacher" class="teacher-entry" @tap="goTeacher">
      <view class="teacher-entry-icon">🧑‍🏫</view>
      <view class="teacher-entry-info">
        <text class="teacher-entry-title">{{ L.teacherEntry }}</text>
        <text class="teacher-entry-desc">{{ L.teacherEntryDesc }}</text>
      </view>
      <text class="teacher-entry-arrow">→</text>
    </view>

    <!-- 搜索 + 筛选 -->
    <view class="filter-card">
      <view class="search-box">
        <text class="search-icon">🔍</text>
        <input
          v-model="keyword"
          class="search-input"
          :placeholder="L.searchPlaceholder"
          placeholder-class="ph"
          confirm-type="search"
        />
        <text v-if="keyword" class="search-clear" @tap="keyword = ''">✕</text>
      </view>
      <scroll-view scroll-x class="filter-scroll" :show-scrollbar="false">
        <view class="filter-chips">
          <view
            v-for="f in FILTERS"
            :key="f.key"
            class="filter-chip"
            :class="{ active: activeFilter === f.key }"
            @tap="activeFilter = f.key"
          >
            {{ f.label() }}
          </view>
        </view>
      </scroll-view>
    </view>

    <!-- 题目列表 -->
    <view v-if="loadingQuestions" class="loading-box">
      <text class="anim-pulse">{{ L.loading }}</text>
    </view>
    <template v-else>
      <view v-if="filteredQuestions.length === 0" class="card">
        <empty-state
          :icon="keyword ? '🔍' : '🗂️'"
          :title="keyword ? L.emptySearch : L.emptyQuestions"
          :desc="keyword ? L.emptySearchDesc : L.emptyQuestionsDesc"
        />
      </view>
      <view
        v-for="q in filteredQuestions"
        :key="q.id"
        class="question-card"
        @tap="goPractice(q.id)"
      >
        <view class="q-head">
          <text class="q-id">#{{ q.id }}</text>
          <difficulty-badge :value="q.display_difficulty ?? q.difficulty" />
          <view v-if="solvedIds.has(q.id)" class="solved-chip">✓ {{ L.solved }}</view>
          <view class="q-dialect" v-if="q.sql_dialect">{{ dialectLabel(q.sql_dialect) }}</view>
        </view>
        <text class="q-title">{{ localizedQuestionTitle(q) }}</text>
        <text class="q-content">{{ excerpt(localizedQuestionContent(q), 88) }}</text>
        <view class="q-foot">
          <view class="q-diff-detail">
            <text class="q-diff-label">{{ L.displayDifficulty }}</text>
            <text class="q-diff-value">{{ difficultyText(q.display_difficulty ?? q.difficulty) }}</text>
          </view>
          <text class="q-go">{{ L.startPractice }} →</text>
        </view>
      </view>
    </template>
  </view>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { onPullDownRefresh, onShow } from "@dcloudio/uni-app";
import { getQuestions } from "@/api/questions";
import { getMySubmissions } from "@/api/ai";
import { ensureAuthed } from "@/utils/auth";
import { language, localizedQuestionContent, localizedQuestionTitle } from "@/utils/i18n";
import { difficultyText } from "@/utils/format";
import type { QuestionOut, SubmissionOut, UserSchema } from "@/types";

const STRINGS = {
  "zh-CN": {
    appName: "SQL 智能教学系统",
    hello: "你好",
    teacher: "教师",
    heroTip: "写 SQL，提交运行，获得证据驱动的即时诊断反馈",
    statQuestions: "题目",
    statSubmissions: "提交",
    statSolved: "已通过",
    statAccuracy: "通过率",
    teacherEntry: "题目管理",
    teacherEntryDesc: "出题、AI 生成与题库维护",
    searchPlaceholder: "搜索题目关键词…",
    loading: "加载题目中…",
    emptyQuestions: "题库还是空的",
    emptyQuestionsDesc: "等待教师发布题目，或稍后下拉刷新",
    emptySearch: "没有匹配的题目",
    emptySearchDesc: "换个关键词试试",
    solved: "已通过",
    displayDifficulty: "动态难度",
    startPractice: "去练习",
  },
  en: {
    appName: "SQL Learning Lab",
    hello: "Hi",
    teacher: "Teacher",
    heroTip: "Write SQL, run it, and get evidence-driven instant feedback",
    statQuestions: "Questions",
    statSubmissions: "Submissions",
    statSolved: "Solved",
    statAccuracy: "Accuracy",
    teacherEntry: "Question bank",
    teacherEntryDesc: "Author, AI-generate and maintain questions",
    searchPlaceholder: "Search questions…",
    loading: "Loading questions…",
    emptyQuestions: "The bank is empty",
    emptyQuestionsDesc: "Waiting for teachers to publish, or pull to refresh",
    emptySearch: "No matching questions",
    emptySearchDesc: "Try another keyword",
    solved: "Solved",
    displayDifficulty: "Live difficulty",
    startPractice: "Practice",
  },
  "zh-TW": {
    appName: "SQL 智能教學系統",
    hello: "你好",
    teacher: "教師",
    heroTip: "寫 SQL，提交運行，獲得證據驅動的即時診斷回饋",
    statQuestions: "題目",
    statSubmissions: "提交",
    statSolved: "已通過",
    statAccuracy: "通過率",
    teacherEntry: "題目管理",
    teacherEntryDesc: "出題、AI 生成與題庫維護",
    searchPlaceholder: "搜尋題目關鍵詞…",
    loading: "載入題目中…",
    emptyQuestions: "題庫還是空的",
    emptyQuestionsDesc: "等待教師發布題目，或稍後下拉刷新",
    emptySearch: "沒有符合的題目",
    emptySearchDesc: "換個關鍵詞試試",
    solved: "已通過",
    displayDifficulty: "動態難度",
    startPractice: "去練習",
  },
} as const;

const L = computed(() => STRINGS[language.value]);

type FilterKey = "all" | "easy" | "basic" | "medium" | "hard" | "expert";
const FILTER_DEFS: Array<{ key: FilterKey; min: number; max: number; labels: Record<string, string> }> = [
  { key: "all", min: 1, max: 10, labels: { "zh-CN": "全部", en: "All", "zh-TW": "全部" } },
  { key: "easy", min: 1, max: 2, labels: { "zh-CN": "入门", en: "Easy", "zh-TW": "入門" } },
  { key: "basic", min: 3, max: 4, labels: { "zh-CN": "简单", en: "Basic", "zh-TW": "簡單" } },
  { key: "medium", min: 5, max: 6, labels: { "zh-CN": "中等", en: "Medium", "zh-TW": "中等" } },
  { key: "hard", min: 7, max: 8, labels: { "zh-CN": "较难", en: "Hard", "zh-TW": "較難" } },
  { key: "expert", min: 9, max: 10, labels: { "zh-CN": "挑战", en: "Expert", "zh-TW": "挑戰" } },
];
const FILTERS = FILTER_DEFS.map((f) => ({
  key: f.key,
  label: () => f.labels[language.value] ?? f.labels["zh-CN"],
}));

const user = ref<UserSchema | null>(null);
const questions = ref<QuestionOut[]>([]);
const submissions = ref<SubmissionOut[]>([]);
const loadingQuestions = ref(true);
const keyword = ref("");
const activeFilter = ref<FilterKey>("all");

const isTeacher = computed(() => user.value?.role === "teacher");
const avatarChar = computed(() => (user.value?.username || "?").slice(0, 1).toUpperCase());

const solvedIds = computed(() => {
  const set = new Set<number>();
  for (const s of submissions.value) {
    if (s.is_correct) set.add(s.question_id);
  }
  return set;
});

const totalSubmissions = computed(() => submissions.value.length);

const attemptedIds = computed(
  () => new Set(submissions.value.map((s) => s.question_id)),
);

const accuracyText = computed(() => {
  const attempted = attemptedIds.value.size;
  if (attempted === 0) return "–";
  return `${Math.round((solvedIds.value.size / attempted) * 100)}%`;
});

const filteredQuestions = computed(() => {
  const filter = FILTER_DEFS.find((f) => f.key === activeFilter.value)!;
  const kw = keyword.value.trim().toLowerCase();
  return questions.value.filter((q) => {
    const diff = Math.round(q.display_difficulty ?? q.difficulty);
    if (diff < filter.min || diff > filter.max) return false;
    if (!kw) return true;
    const hay = [
      q.title,
      q.title_en ?? "",
      q.title_zh_tw ?? "",
      q.content,
      q.content_en ?? "",
      q.content_zh_tw ?? "",
      `#${q.id}`,
    ]
      .join("\n")
      .toLowerCase();
    return hay.includes(kw);
  });
});

function dialectLabel(dialect: string): string {
  const map: Record<string, string> = {
    mysql: "MySQL",
    postgres: "PostgreSQL",
    postgresql: "PostgreSQL",
    tsql: "T-SQL",
    oracle: "Oracle",
    generic: "SQL",
  };
  return map[dialect?.toLowerCase?.()] ?? dialect;
}

function excerpt(text: string, max: number): string {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > max ? `${clean.slice(0, max)}…` : clean;
}

function goPractice(id: number) {
  uni.navigateTo({ url: `/pages/practice/index?id=${id}` });
}

function goTeacher() {
  uni.navigateTo({ url: "/pages/teacher/index" });
}

function goProfile() {
  uni.navigateTo({ url: "/pages/profile/index" });
}

async function bootstrap() {
  if (!ensureAuthed()) return;
  user.value = uni.getStorageSync("user") || null;
  await Promise.all([loadQuestions(), loadSubmissions()]);
}

async function loadQuestions() {
  loadingQuestions.value = questions.value.length === 0;
  try {
    questions.value = await getQuestions({ skip: 0, limit: 1000 });
  } catch {
    /* 统一错误处理 */
  } finally {
    loadingQuestions.value = false;
  }
}

async function loadSubmissions() {
  try {
    submissions.value = await getMySubmissions({ limit: 200 });
  } catch {
    submissions.value = [];
  }
}

onShow(() => {
  bootstrap();
});

onPullDownRefresh(async () => {
  await bootstrap();
  uni.stopPullDownRefresh();
});
</script>

<style lang="scss" scoped>
.bank-page {
  max-width: 700px;
  margin: 0 auto;
}

/* ---------- 顶部 ---------- */
.bank-hero {
  padding: 60rpx 0 30rpx;
}

.hero-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 30rpx;
}

.greeting {
  display: flex;
  align-items: center;
  gap: 8rpx;
  min-width: 0;

  .greet-hi {
    font-size: 30rpx;
    color: $text-2;
  }

  .greet-name {
    font-size: 34rpx;
    font-weight: 800;
    color: $text-1;
    max-width: 320rpx;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.role-badge {
  margin-left: 10rpx;
  padding: 4rpx 14rpx;
  border-radius: 999rpx;
  background: $warning-soft;
  color: #c77d00;
  font-size: 20rpx;
  font-weight: 600;
  flex-shrink: 0;
}

.hero-actions {
  display: flex;
  align-items: center;
  gap: 16rpx;
}

.avatar-btn {
  width: 68rpx;
  height: 68rpx;
  border-radius: 50%;
  background: $brand-gradient;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 8rpx 22rpx rgba(76, 111, 255, 0.32);

  .avatar-text {
    color: #fff;
    font-size: 30rpx;
    font-weight: 700;
  }
}

.hero-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: $brand-gradient;
  border-radius: 28rpx;
  padding: 34rpx 36rpx;
  box-shadow: 0 18rpx 44rpx rgba(76, 111, 255, 0.3);
  position: relative;
  overflow: hidden;

  .hero-deco {
    position: absolute;
    right: 30rpx;
    top: -20rpx;
    font-size: 160rpx;
    color: rgba(255, 255, 255, 0.14);
    font-family: $font-mono;
    transform: rotate(12deg);
  }

  .hero-title {
    display: block;
    color: #fff;
    font-size: 34rpx;
    font-weight: 800;
    letter-spacing: 1rpx;
  }

  .hero-sub {
    display: block;
    margin-top: 10rpx;
    color: rgba(255, 255, 255, 0.82);
    font-size: 22rpx;
    max-width: 480rpx;
    line-height: 1.6;
  }
}

/* ---------- 统计 ---------- */
.stats-row {
  display: flex;
  gap: 16rpx;
  margin-bottom: 24rpx;
}

.stat-card {
  flex: 1;
  background: #fff;
  border-radius: 20rpx;
  box-shadow: $shadow-card;
  padding: 22rpx 0;
  display: flex;
  flex-direction: column;
  align-items: center;

  .stat-num {
    font-size: 38rpx;
    font-weight: 800;
    color: $text-1;
    font-family: $font-mono;
    line-height: 1.2;
  }

  .stat-label {
    margin-top: 4rpx;
    font-size: 20rpx;
    color: $text-3;
  }

  &.stat-success .stat-num {
    color: $success;
  }

  &.stat-brand .stat-num {
    color: $brand-deep;
  }
}

/* ---------- 教师入口 ---------- */
.teacher-entry {
  display: flex;
  align-items: center;
  gap: 20rpx;
  background: #fff;
  border: 2rpx dashed #c9d6ff;
  border-radius: 20rpx;
  padding: 24rpx 28rpx;
  margin-bottom: 24rpx;

  .teacher-entry-icon {
    font-size: 44rpx;
  }

  .teacher-entry-info {
    flex: 1;
    min-width: 0;
  }

  .teacher-entry-title {
    display: block;
    font-size: 28rpx;
    font-weight: 700;
    color: $text-1;
  }

  .teacher-entry-desc {
    display: block;
    font-size: 22rpx;
    color: $text-3;
    margin-top: 2rpx;
  }

  .teacher-entry-arrow {
    color: $brand;
    font-size: 30rpx;
  }
}

/* ---------- 搜索筛选 ---------- */
.filter-card {
  background: #fff;
  border-radius: 20rpx;
  box-shadow: $shadow-card;
  padding: 20rpx;
  margin-bottom: 24rpx;
}

.search-box {
  display: flex;
  align-items: center;
  background: #f7f9fd;
  border-radius: 14rpx;
  padding: 0 22rpx;

  .search-icon {
    font-size: 26rpx;
    margin-right: 14rpx;
    opacity: 0.6;
  }

  .search-input {
    flex: 1;
    height: 76rpx;
    font-size: 26rpx;
  }

  .search-clear {
    padding: 10rpx;
    color: $text-3;
    font-size: 26rpx;
  }
}

.filter-scroll {
  margin-top: 16rpx;
  white-space: nowrap;
}

.filter-chips {
  display: inline-flex;
  gap: 14rpx;
}

.filter-chip {
  padding: 10rpx 28rpx;
  border-radius: 999rpx;
  background: #f2f4fa;
  color: $text-2;
  font-size: 24rpx;
  font-weight: 500;
  flex-shrink: 0;
  transition: all 0.18s ease;

  &.active {
    background: $text-1;
    color: #fff;
  }
}

/* ---------- 题目卡片 ---------- */
.question-card {
  background: #fff;
  border-radius: 22rpx;
  box-shadow: $shadow-card;
  padding: 28rpx 30rpx;
  margin-bottom: 20rpx;
  transition: transform 0.12s ease;

  &:active {
    transform: scale(0.985);
  }
}

.q-head {
  display: flex;
  align-items: center;
  gap: 14rpx;
  margin-bottom: 14rpx;

  .q-id {
    font-family: $font-mono;
    font-size: 22rpx;
    font-weight: 700;
    color: $brand-deep;
    background: $brand-soft;
    padding: 4rpx 14rpx;
    border-radius: 8rpx;
  }

  .solved-chip {
    background: $success-soft;
    color: $success;
    font-size: 20rpx;
    font-weight: 600;
    padding: 4rpx 14rpx;
    border-radius: 999rpx;
  }

  .q-dialect {
    margin-left: auto;
    font-size: 20rpx;
    color: $text-3;
    font-family: $font-mono;
    background: #f2f4fa;
    padding: 4rpx 14rpx;
    border-radius: 8rpx;
  }
}

.q-title {
  display: block;
  font-size: 30rpx;
  font-weight: 700;
  color: $text-1;
  line-height: 1.5;
}

.q-content {
  display: block;
  margin-top: 8rpx;
  font-size: 24rpx;
  color: $text-2;
  line-height: 1.6;
}

.q-foot {
  margin-top: 18rpx;
  padding-top: 18rpx;
  border-top: 2rpx solid #f1f3f9;
  display: flex;
  align-items: center;
  justify-content: space-between;

  .q-diff-detail {
    display: flex;
    align-items: baseline;
    gap: 10rpx;
  }

  .q-diff-label {
    font-size: 20rpx;
    color: $text-3;
  }

  .q-diff-value {
    font-size: 26rpx;
    font-weight: 700;
    color: $text-1;
    font-family: $font-mono;
  }

  .q-go {
    font-size: 24rpx;
    font-weight: 600;
    color: $brand;
  }
}

.loading-box {
  text-align: center;
  padding: 120rpx 0;
  color: $text-3;
  font-size: 26rpx;
}
</style>
