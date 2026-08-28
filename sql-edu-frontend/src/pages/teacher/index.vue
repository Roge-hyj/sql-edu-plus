<template>
  <view class="page teacher-page">
    <app-navbar :title="L.title">
      <template #right>
        <lang-switch />
      </template>
    </app-navbar>

    <!-- 概览 -->
    <view class="t-hero">
      <view class="t-hero-info">
        <text class="t-hero-title">{{ L.title }}</text>
        <text class="t-hero-sub">{{ L.heroSub }}</text>
      </view>
      <view class="t-hero-stat">
        <text class="t-hero-num">{{ questions.length }}</text>
        <text class="t-hero-label">{{ L.statQuestions }}</text>
      </view>
    </view>

    <!-- Tabs -->
    <view class="t-tabs">
      <view
        v-for="tab in TABS"
        :key="tab.key"
        class="t-tab"
        :class="{ active: activeTab === tab.key }"
        @tap="activeTab = tab.key"
      >
        <text>{{ tab.label() }}</text>
      </view>
    </view>

    <!-- ===== 题库列表 ===== -->
    <view v-if="activeTab === 'list'">
      <view class="card filter-card">
        <view class="search-box">
          <text class="search-icon">🔍</text>
          <input
            v-model="keyword"
            class="search-input"
            :placeholder="L.searchPlaceholder"
            placeholder-class="ph"
          />
        </view>
      </view>
      <view v-if="loadingList" class="loading-box">
        <text class="anim-pulse">{{ L.loading }}</text>
      </view>
      <view v-else-if="filteredQuestions.length === 0" class="card">
        <empty-state icon="🗃️" :title="L.emptyList" :desc="L.emptyListDesc" />
      </view>
      <view
        v-for="q in filteredQuestions"
        :key="q.id"
        class="card q-card"
        @tap="startEdit(q)"
      >
        <view class="q-head">
          <text class="q-id mono">#{{ q.id }}</text>
          <difficulty-badge :value="q.difficulty" :show-label="true" />
          <view v-if="q.sql_dialect" class="chip mono">{{ q.sql_dialect }}</view>
          <view v-if="q.required_output_columns" class="chip chip-warning">🎯</view>
          <view v-if="q.title_en" class="chip">EN</view>
          <view v-if="q.title_zh_tw" class="chip">繁</view>
          <text class="q-edit">{{ L.edit }} →</text>
        </view>
        <text class="q-title">{{ q.title }}</text>
        <text class="q-content">{{ excerpt(q.content, 96) }}</text>
      </view>
    </view>

    <!-- ===== 手动出题 / 编辑 ===== -->
    <view v-else-if="activeTab === 'form'" class="card">
      <view class="form-head">
        <text class="form-head-title">{{ editingId ? L.editTitle : L.newTitle }}</text>
        <text v-if="editingId" class="mono form-head-id">#{{ editingId }}</text>
      </view>

      <view class="field">
        <text class="field-label">{{ L.fieldTitle }} *</text>
        <input
          v-model="form.title"
          class="input"
          :placeholder="L.fieldTitlePlaceholder"
          placeholder-class="ph"
          :maxlength="200"
        />
      </view>

      <view class="field">
        <text class="field-label">{{ L.fieldContent }} *</text>
        <textarea
          v-model="form.content"
          class="textarea"
          :placeholder="L.fieldContentPlaceholder"
          placeholder-class="ph"
          :maxlength="-1"
          auto-height
        />
      </view>

      <view class="field">
        <text class="field-label">{{ L.fieldSql }} *</text>
        <view v-if="editingId" class="edit-sql-notice">
          <text>🔒 {{ L.editSqlNotice }}</text>
        </view>
        <textarea
          v-model="form.correctSql"
          class="textarea input-mono sql-area"
          :placeholder="L.fieldSqlPlaceholder"
          placeholder-style="color:#a6aec0"
          :maxlength="-1"
          auto-height
        />
      </view>

      <view class="field-row">
        <view class="field field-half">
          <text class="field-label">{{ L.fieldDifficulty }}</text>
          <picker
            mode="selector"
            :range="difficultyPickerLabels"
            :value="difficultyPickerIndex"
            @change="onDifficultyPick"
          >
            <view class="picker-value">
              {{ difficultyPickerLabels[difficultyPickerIndex] }}
            </view>
          </picker>
        </view>
        <view class="field field-half">
          <text class="field-label">{{ L.fieldDialect }}</text>
          <picker
            mode="selector"
            :range="dialectPickerLabels"
            :value="dialectPickerIndex"
            @change="onDialectPick"
          >
            <view class="picker-value mono">{{ dialectPickerLabels[dialectPickerIndex] }}</view>
          </picker>
        </view>
      </view>

      <view class="form-tip">{{ L.formTip }}</view>

      <button
        class="btn btn-primary btn-block"
        :class="{ 'is-disabled': saving }"
        :loading="saving"
        :disabled="saving"
        @tap="saveQuestion"
      >
        {{ saving ? L.saving : editingId ? L.save : L.create }}
      </button>

      <!-- 编辑态工具 -->
      <view v-if="editingId" class="tool-grid">
        <button class="btn btn-ghost btn-sm" :loading="toolLoading === 'preview'" :disabled="!!toolLoading" @tap="toolGeneratePreview">
          {{ L.toolPreview }}
        </button>
        <button class="btn btn-ghost btn-sm" :loading="toolLoading === 'i18n'" :disabled="!!toolLoading" @tap="toolGenerateI18n">
          {{ L.toolI18n }}
        </button>
        <button class="btn btn-ghost btn-sm" :loading="toolLoading === 'columns'" :disabled="!!toolLoading" @tap="toolInferColumns">
          {{ L.toolColumns }}
        </button>
        <button class="btn btn-danger btn-sm" @tap="confirmDelete">
          {{ L.toolDelete }}
        </button>
      </view>
      <view v-if="inferredColumns" class="inferred-box">
        <text class="inferred-label">{{ L.inferredLabel }}</text>
        <text class="inferred-value mono">{{ inferredColumns }}</text>
      </view>
    </view>

    <!-- ===== AI 生成 ===== -->
    <view v-else class="card">
      <view class="ai-banner">
        <text class="ai-banner-icon">✨</text>
        <view class="ai-banner-main">
          <text class="ai-banner-title">{{ L.aiTitle }}</text>
          <text class="ai-banner-desc">{{ L.aiDesc }}</text>
        </view>
      </view>

      <view class="field">
        <text class="field-label">{{ L.fieldLevel }}</text>
        <view class="level-chips">
          <view
            v-for="lv in levelGroups"
            :key="lv.key"
            class="level-chip"
            :class="{ active: selectedLevelKey === lv.key }"
            @tap="selectLevel(lv.key)"
          >
            {{ lv.label() }}
          </view>
        </view>
      </view>

      <view class="field">
        <text class="field-label">{{ L.fieldPoint }}</text>
        <picker
          mode="selector"
          :range="currentPointLabels"
          :value="currentPointIndex"
          @change="(e: any) => (currentPointIndex = Number(e.detail.value))"
        >
          <view class="picker-value">
            {{ currentPointLabels[currentPointIndex] || L.loading }}
          </view>
        </picker>
        <text v-if="currentPointDesc" class="point-desc">{{ currentPointDesc }}</text>
      </view>

      <view class="field">
        <text class="field-label">{{ L.fieldCount }}</text>
        <view class="stepper">
          <view class="stepper-btn" :class="{ disabled: generateCount <= 1 }" @tap="generateCount = Math.max(1, generateCount - 1)">−</view>
          <text class="stepper-num mono">{{ generateCount }}</text>
          <view class="stepper-btn" :class="{ disabled: generateCount >= 5 }" @tap="generateCount = Math.min(5, generateCount + 1)">＋</view>
          <text class="stepper-hint">{{ L.countHint }}</text>
        </view>
      </view>

      <button
        class="btn btn-primary btn-block generate-btn"
        :class="{ 'is-disabled': generating || currentPoints.length === 0 }"
        :loading="generating"
        :disabled="generating || currentPoints.length === 0"
        @tap="generateByAi"
      >
        {{ generating ? L.generating : L.generateBtn }}
      </button>
    </view>
  </view>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { onShow } from "@dcloudio/uni-app";
import {
  createQuestion,
  deleteQuestion,
  generateQuestionI18n,
  generateQuestionsByAI,
  generateSchemaPreview,
  getKnowledgePoints,
  getQuestions,
  inferOutputColumns,
  updateQuestion,
} from "@/api/questions";
import { ensureAuthed, requireTeacher } from "@/utils/auth";
import { language, localizedDict } from "@/utils/i18n";
import { clampDifficulty } from "@/utils/format";
import type { KnowledgePoint, QuestionOut, UserSchema } from "@/types";

const STRINGS = {
  "zh-CN": {
    title: "题目管理",
    heroSub: "出题、AI 生成与题库维护",
    statQuestions: "道题目",
    tabList: "题库",
    tabForm: "出题",
    tabAi: "AI 生成",
    searchPlaceholder: "搜索题目…",
    loading: "加载中…",
    emptyList: "题库为空",
    emptyListDesc: "切换到「出题」手动添加，或用 AI 生成",
    edit: "编辑",
    newTitle: "新建题目",
    editTitle: "编辑题目",
    fieldTitle: "题目标题",
    fieldTitlePlaceholder: "例如：查询销量前 10 的商品",
    fieldContent: "题目描述",
    fieldContentPlaceholder: "描述业务背景、要求与输出格式；若要求列别名请显式写明（如“将列命名为 total_amount”）",
    fieldSql: "标准答案 SQL",
    fieldSqlPlaceholder: "SELECT ... FROM ...",
    fieldDifficulty: "难度",
    difficultyAuto: "AI 自动评估",
    fieldDialect: "SQL 方言",
    editSqlNotice: "安全设计：标准答案不会回显。编辑时需重新输入完整 SQL，保存后将覆盖原答案。",
    formTip: "留空难度由 AI 评估；若题面要求列别名，保存时会自动解析必需输出列并启用列名校验。",
    saving: "保存中…",
    save: "保存修改",
    create: "创建题目",
    toolPreview: "生成表结构预览",
    toolI18n: "补全多语言",
    toolColumns: "解析输出列",
    toolDelete: "删除题目",
    deleteConfirmDesc: "删除后不可恢复，且会级联删除相关提交记录。",
    inferredLabel: "解析结果（必需输出列）",
    aiTitle: "AI 出题",
    aiDesc: "选择知识点，AI 按冻结知识分类自动生成题目与标准答案",
    fieldLevel: "难度层级",
    fieldPoint: "知识点",
    fieldCount: "生成数量",
    countHint: "1～5 道",
    generateBtn: "生成并加入题库",
    generating: "生成中，约需数十秒…",
  },
  en: {
    title: "Question bank",
    heroSub: "Author, AI-generate and maintain",
    statQuestions: "questions",
    tabList: "Bank",
    tabForm: "Author",
    tabAi: "AI generate",
    searchPlaceholder: "Search questions…",
    loading: "Loading…",
    emptyList: "Bank is empty",
    emptyListDesc: "Author manually or generate with AI",
    edit: "Edit",
    newTitle: "New question",
    editTitle: "Edit question",
    fieldTitle: "Title",
    fieldTitlePlaceholder: "e.g. Top 10 products by sales",
    fieldContent: "Description",
    fieldContentPlaceholder: "Describe the business context and requirements. State alias requirements explicitly (e.g. \"name the column total_amount\")",
    fieldSql: "Reference SQL",
    fieldSqlPlaceholder: "SELECT ... FROM ...",
    fieldDifficulty: "Difficulty",
    difficultyAuto: "AI auto",
    fieldDialect: "SQL dialect",
    editSqlNotice: "By design the reference SQL is never echoed back. Re-enter the full SQL to overwrite it on save.",
    formTip: "Empty difficulty is inferred by AI; if the description demands column aliases, required output columns are parsed automatically on save.",
    saving: "Saving…",
    save: "Save changes",
    create: "Create question",
    toolPreview: "Generate schema preview",
    toolI18n: "Generate i18n",
    toolColumns: "Infer output columns",
    toolDelete: "Delete",
    deleteConfirmDesc: "Deletion is irreversible and cascades to related submissions.",
    inferredLabel: "Inferred required output columns",
    aiTitle: "AI generation",
    aiDesc: "Pick a knowledge point; AI generates questions with reference SQL",
    fieldLevel: "Level",
    fieldPoint: "Knowledge point",
    fieldCount: "Count",
    countHint: "1–5 questions",
    generateBtn: "Generate & add to bank",
    generating: "Generating… may take a while",
  },
  "zh-TW": {
    title: "題目管理",
    heroSub: "出題、AI 生成與題庫維護",
    statQuestions: "道題目",
    tabList: "題庫",
    tabForm: "出題",
    tabAi: "AI 生成",
    searchPlaceholder: "搜尋題目…",
    loading: "載入中…",
    emptyList: "題庫為空",
    emptyListDesc: "切換到「出題」手動新增，或用 AI 生成",
    edit: "編輯",
    newTitle: "新建題目",
    editTitle: "編輯題目",
    fieldTitle: "題目標題",
    fieldTitlePlaceholder: "例如：查詢銷量前 10 的商品",
    fieldContent: "題目描述",
    fieldContentPlaceholder: "描述業務背景、要求與輸出格式；若要求欄別名請顯式寫明（如「將欄命名為 total_amount」）",
    fieldSql: "標準答案 SQL",
    fieldSqlPlaceholder: "SELECT ... FROM ...",
    fieldDifficulty: "難度",
    difficultyAuto: "AI 自動評估",
    fieldDialect: "SQL 方言",
    editSqlNotice: "安全設計：標準答案不會回顯。編輯時需重新輸入完整 SQL，保存後將覆蓋原答案。",
    formTip: "留空難度由 AI 評估；若題面要求欄別名，保存時會自動解析必需輸出欄並啟用欄名校驗。",
    saving: "保存中…",
    save: "保存修改",
    create: "建立題目",
    toolPreview: "生成表結構預覽",
    toolI18n: "補全多語言",
    toolColumns: "解析輸出欄",
    toolDelete: "刪除題目",
    deleteConfirmDesc: "刪除後不可恢復，且會級聯刪除相關提交記錄。",
    inferredLabel: "解析結果（必需輸出欄）",
    aiTitle: "AI 出題",
    aiDesc: "選擇知識點，AI 按凍結知識分類自動生成題目與標準答案",
    fieldLevel: "難度層級",
    fieldPoint: "知識點",
    fieldCount: "生成數量",
    countHint: "1～5 道",
    generateBtn: "生成並加入題庫",
    generating: "生成中，約需數十秒…",
  },
} as const;

const L = computed(() => STRINGS[language.value]);

type TabKey = "list" | "form" | "ai";
const TABS: Array<{ key: TabKey; label: () => string }> = [
  { key: "list", label: () => L.value.tabList },
  { key: "form", label: () => L.value.tabForm },
  { key: "ai", label: () => L.value.tabAi },
];

const user = ref<UserSchema | null>(null);
const questions = ref<QuestionOut[]>([]);
const loadingList = ref(true);
const keyword = ref("");
const activeTab = ref<TabKey>("list");

/* ---------- 手动表单 ---------- */
const editingId = ref<number | null>(null);
const form = ref({
  title: "",
  content: "",
  correctSql: "",
});
const formDifficulty = ref<number | null>(null); // null = AI 自动
const formDialect = ref<string | null>(null); // null = 自动
const saving = ref(false);
const toolLoading = ref<"preview" | "i18n" | "columns" | "">("");
const inferredColumns = ref("");

const difficultyPickerLabels = computed(() => [
  L.value.difficultyAuto,
  ...Array.from({ length: 10 }, (_, i) => String(i + 1)),
]);
const difficultyPickerIndex = computed(() =>
  formDifficulty.value === null ? 0 : formDifficulty.value,
);

const DIALECT_OPTIONS: Array<{ value: string | null; labels: Record<string, string> }> = [
  { value: null, labels: { "zh-CN": "自动识别", en: "Auto", "zh-TW": "自動識別" } },
  { value: "mysql", labels: { "zh-CN": "MySQL", en: "MySQL", "zh-TW": "MySQL" } },
  { value: "postgres", labels: { "zh-CN": "PostgreSQL", en: "PostgreSQL", "zh-TW": "PostgreSQL" } },
  { value: "tsql", labels: { "zh-CN": "T-SQL", en: "T-SQL", "zh-TW": "T-SQL" } },
  { value: "oracle", labels: { "zh-CN": "Oracle", en: "Oracle", "zh-TW": "Oracle" } },
  { value: "generic", labels: { "zh-CN": "标准 SQL", en: "Standard SQL", "zh-TW": "標準 SQL" } },
];
const dialectPickerLabels = computed(() =>
  DIALECT_OPTIONS.map((d) => d.labels[language.value] ?? d.labels["zh-CN"]),
);
const dialectPickerIndex = computed(
  () => DIALECT_OPTIONS.findIndex((d) => d.value === formDialect.value) || 0,
);

function onDifficultyPick(e: { detail: { value: number } }) {
  const idx = Number(e.detail.value);
  formDifficulty.value = idx === 0 ? null : idx;
}

function onDialectPick(e: { detail: { value: number } }) {
  formDialect.value = DIALECT_OPTIONS[Number(e.detail.value)]?.value ?? null;
}

/* ---------- AI 生成 ---------- */
const knowledgePoints = ref<KnowledgePoint[]>([]);
const selectedLevelKey = ref("入门");
const currentPointIndex = ref(0);
const generateCount = ref(1);
const generating = ref(false);

const LEVEL_KEYS = ["入门", "进阶", "精通"];
const LEVEL_LABELS: Record<string, Record<string, string>> = {
  入门: { "zh-CN": "入门", en: "Beginner", "zh-TW": "入門" },
  进阶: { "zh-CN": "进阶", en: "Intermediate", "zh-TW": "進階" },
  精通: { "zh-CN": "精通", en: "Advanced", "zh-TW": "精通" },
};

const levelGroups = computed(() =>
  LEVEL_KEYS.map((key) => ({
    key,
    label: () => LEVEL_LABELS[key][language.value] ?? key,
  })),
);

const currentPoints = computed(() =>
  knowledgePoints.value.filter((p) => p.level === selectedLevelKey.value),
);

const currentPointLabels = computed(() =>
  currentPoints.value.map(
    (p) => `${localizedDict(p.name, p.name_i18n)} · ${localizedDict(p.level, p.level_i18n)}`,
  ),
);

const currentPointDesc = computed(() => {
  const p = currentPoints.value[currentPointIndex.value];
  if (!p) return "";
  return localizedDict(p.description, p.description_i18n);
});

function selectLevel(key: string) {
  selectedLevelKey.value = key;
  currentPointIndex.value = 0;
}

/* ---------- 列表 ---------- */
const filteredQuestions = computed(() => {
  const kw = keyword.value.trim().toLowerCase();
  if (!kw) return questions.value;
  return questions.value.filter((q) =>
    [q.title, q.title_en ?? "", q.content, `#${q.id}`]
      .join("\n")
      .toLowerCase()
      .includes(kw),
  );
});

function excerpt(text: string, max: number): string {
  const clean = (text || "").replace(/\s+/g, " ").trim();
  return clean.length > max ? `${clean.slice(0, max)}…` : clean;
}

/* ---------- 生命周期 ---------- */
onShow(() => {
  if (!ensureAuthed()) return;
  const stored = uni.getStorageSync("user");
  user.value = stored || null;
  if (!stored) return;
  if (!requireTeacher(stored as UserSchema)) return;
  if (questions.value.length === 0) {
    loadAll();
  }
});

async function loadAll() {
  loadingList.value = true;
  try {
    const [qs, kps] = await Promise.all([
      getQuestions({ skip: 0, limit: 1000 }),
      getKnowledgePoints().catch(() => [] as KnowledgePoint[]),
    ]);
    questions.value = qs;
    knowledgePoints.value = kps;
  } catch {
    /* 统一错误处理 */
  } finally {
    loadingList.value = false;
  }
}

/* ---------- 编辑 / 保存 ---------- */
async function startEdit(q: QuestionOut) {
  editingId.value = q.id;
  // 后端公开接口不下发 correct_sql（安全设计）：编辑时需重新输入覆盖
  form.value = { title: q.title, content: q.content, correctSql: "" };
  formDifficulty.value = clampDifficulty(q.difficulty, 3);
  formDialect.value = q.sql_dialect ?? null;
  inferredColumns.value = q.required_output_columns ?? "";
  activeTab.value = "form";
  uni.pageScrollTo({ scrollTop: 0, duration: 200 });
}

async function saveQuestion() {
  const f = form.value;
  if (!f.title.trim() || !f.content.trim() || !f.correctSql.trim()) {
    uni.showToast({ title: L.value.fieldTitle, icon: "none" });
    return;
  }
  saving.value = true;
  try {
    const payload = {
      title: f.title.trim(),
      content: f.content.trim(),
      correct_sql: f.correctSql.trim(),
      difficulty: formDifficulty.value,
      sql_dialect: formDialect.value ?? undefined,
    };
    if (editingId.value) {
      await updateQuestion(editingId.value, payload);
    } else {
      await createQuestion(payload);
    }
    uni.showToast({ title: "✅", icon: "none" });
    activeTab.value = "list";
    await loadAll();
  } catch {
    /* 统一错误处理 */
  } finally {
    saving.value = false;
  }
}

function confirmDelete() {
  if (!editingId.value) return;
  const title = form.value.title || `#${editingId.value}`;
  uni.showModal({
    title: L.value.toolDelete,
    content: `${title}\n\n${L.value.deleteConfirmDesc}`,
    confirmText: L.value.toolDelete,
    confirmColor: "#E5484D",
    success: async (r) => {
      if (!r.confirm) return;
      try {
        await deleteQuestion(editingId.value!);
        uni.showToast({ title: "🗑️", icon: "none" });
        editingId.value = null;
        resetForm();
        activeTab.value = "list";
        await loadAll();
      } catch {
        /* 统一错误处理 */
      }
    },
  });
}

function resetForm() {
  form.value = { title: "", content: "", correctSql: "" };
  formDifficulty.value = null;
  formDialect.value = null;
  inferredColumns.value = "";
}

/* ---------- 工具按钮 ---------- */
async function toolGeneratePreview() {
  if (!editingId.value) return;
  toolLoading.value = "preview";
  try {
    const updated = await generateSchemaPreview(editingId.value);
    applyUpdated(updated);
    uni.showToast({ title: "▦ ✅", icon: "none" });
  } catch {
    /* 统一错误处理 */
  } finally {
    toolLoading.value = "";
  }
}

async function toolGenerateI18n() {
  if (!editingId.value) return;
  toolLoading.value = "i18n";
  try {
    const updated = await generateQuestionI18n(editingId.value);
    applyUpdated(updated);
    uni.showToast({ title: "🌐 ✅", icon: "none" });
  } catch {
    /* 统一错误处理 */
  } finally {
    toolLoading.value = "";
  }
}

async function toolInferColumns() {
  if (!form.value.correctSql.trim()) return;
  toolLoading.value = "columns";
  try {
    const res = await inferOutputColumns(form.value.correctSql);
    inferredColumns.value = res.required_output_columns || "—";
  } catch {
    /* 统一错误处理 */
  } finally {
    toolLoading.value = "";
  }
}

function applyUpdated(updated: Partial<QuestionOut> | null) {
  if (!updated || !editingId.value) return;
  const idx = questions.value.findIndex((q) => q.id === editingId.value);
  if (idx >= 0) {
    questions.value.splice(idx, 1, { ...questions.value[idx], ...updated } as QuestionOut);
  }
}

/* ---------- AI 生成 ---------- */
async function generateByAi() {
  const point = currentPoints.value[currentPointIndex.value];
  if (!point) return;
  generating.value = true;
  try {
    const created = await generateQuestionsByAI({
      knowledge_point_id: point.id,
      count: generateCount.value,
    });
    uni.showToast({ title: `✨ ${created.length}`, icon: "none" });
    activeTab.value = "list";
    await loadAll();
  } catch {
    /* 统一错误处理 */
  } finally {
    generating.value = false;
  }
}
</script>

<style lang="scss" scoped>
.teacher-page {
  max-width: 700px;
  margin: 0 auto;
}

.mono {
  font-family: $font-mono;
}

/* ---------- hero ---------- */
.t-hero {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 50rpx 0 30rpx;
}

.t-hero-title {
  display: block;
  font-size: 38rpx;
  font-weight: 800;
  color: $text-1;
}

.t-hero-sub {
  display: block;
  margin-top: 6rpx;
  font-size: 24rpx;
  color: $text-3;
}

.t-hero-stat {
  display: flex;
  flex-direction: column;
  align-items: center;
  background: #fff;
  border-radius: 20rpx;
  box-shadow: $shadow-card;
  padding: 18rpx 32rpx;

  .t-hero-num {
    font-size: 40rpx;
    font-weight: 800;
    color: $brand-deep;
    font-family: $font-mono;
    line-height: 1.2;
  }

  .t-hero-label {
    font-size: 20rpx;
    color: $text-3;
  }
}

/* ---------- tabs ---------- */
.t-tabs {
  display: flex;
  gap: 12rpx;
  margin-bottom: 24rpx;
}

.t-tab {
  flex: 1;
  text-align: center;
  padding: 18rpx 0;
  border-radius: 16rpx;
  background: #fff;
  color: $text-2;
  font-size: 27rpx;
  font-weight: 600;
  box-shadow: $shadow-card;
  transition: all 0.18s ease;

  &.active {
    background: $text-1;
    color: #fff;
    box-shadow: 0 10rpx 26rpx rgba(23, 29, 43, 0.24);
  }
}

/* ---------- 列表 ---------- */
.filter-card {
  padding: 20rpx;
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
}

.q-card {
  transition: transform 0.12s ease;

  &:active {
    transform: scale(0.985);
  }
}

.q-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12rpx;
  margin-bottom: 14rpx;

  .q-id {
    font-size: 22rpx;
    font-weight: 700;
    color: $brand-deep;
    background: $brand-soft;
    padding: 4rpx 14rpx;
    border-radius: 8rpx;
  }

  .q-edit {
    margin-left: auto;
    font-size: 22rpx;
    color: $brand;
    font-weight: 600;
  }
}

.q-title {
  display: block;
  font-size: 29rpx;
  font-weight: 700;
  color: $text-1;
}

.q-content {
  display: block;
  margin-top: 6rpx;
  font-size: 23rpx;
  color: $text-2;
  line-height: 1.6;
}

.loading-box {
  text-align: center;
  padding: 120rpx 0;
  color: $text-3;
  font-size: 26rpx;
}

/* ---------- 表单 ---------- */
.form-head {
  display: flex;
  align-items: center;
  gap: 14rpx;
  margin-bottom: 26rpx;

  .form-head-title {
    font-size: 32rpx;
    font-weight: 800;
    color: $text-1;
  }

  .form-head-id {
    font-size: 22rpx;
    color: $brand-deep;
    background: $brand-soft;
    padding: 4rpx 14rpx;
    border-radius: 8rpx;
  }
}

.field-row {
  display: flex;
  gap: 18rpx;

  .field-half {
    flex: 1;
    min-width: 0;
  }
}

.picker-value {
  background: #f7f9fd;
  border-radius: 16rpx;
  padding: 20rpx 24rpx;
  font-size: 27rpx;
  color: $text-1;
}

.sql-area {
  background: #101528;
  color: #dbe6ff;
}

.edit-sql-notice {
  background: $warning-soft;
  border-radius: 12rpx;
  padding: 14rpx 20rpx;
  margin-bottom: 12rpx;

  text {
    font-size: 22rpx;
    color: #a06500;
    line-height: 1.6;
  }
}

.form-tip {
  font-size: 22rpx;
  color: $text-3;
  line-height: 1.65;
  margin: 4rpx 0 26rpx;
  padding-left: 4rpx;
}

.tool-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 14rpx;
  margin-top: 22rpx;
  padding-top: 26rpx;
  border-top: 2rpx dashed $border-color;

  .btn {
    flex: 1;
    min-width: 40%;
  }
}

.inferred-box {
  margin-top: 20rpx;
  background: $success-soft;
  border-radius: 12rpx;
  padding: 16rpx 22rpx;

  .inferred-label {
    display: block;
    font-size: 21rpx;
    color: #0b7d5b;
    margin-bottom: 4rpx;
  }

  .inferred-value {
    font-size: 24rpx;
    color: $text-1;
    word-break: break-all;
  }
}

/* ---------- AI 生成 ---------- */
.ai-banner {
  display: flex;
  gap: 20rpx;
  background: linear-gradient(135deg, #f2efff 0%, #eef3ff 100%);
  border-radius: 18rpx;
  padding: 26rpx 28rpx;
  margin-bottom: 30rpx;

  .ai-banner-icon {
    font-size: 44rpx;
  }

  .ai-banner-title {
    display: block;
    font-size: 28rpx;
    font-weight: 800;
    color: #5b4bc4;
  }

  .ai-banner-desc {
    display: block;
    margin-top: 6rpx;
    font-size: 22rpx;
    color: #6d64a8;
    line-height: 1.6;
  }
}

.level-chips {
  display: flex;
  gap: 14rpx;
}

.level-chip {
  flex: 1;
  text-align: center;
  padding: 16rpx 0;
  border-radius: 14rpx;
  background: #f2f4fa;
  color: $text-2;
  font-size: 26rpx;
  font-weight: 600;

  &.active {
    background: $brand-gradient;
    color: #fff;
    box-shadow: 0 10rpx 24rpx rgba(76, 111, 255, 0.3);
  }
}

.point-desc {
  display: block;
  margin-top: 10rpx;
  font-size: 22rpx;
  color: $text-3;
  line-height: 1.6;
}

.stepper {
  display: flex;
  align-items: center;
  gap: 20rpx;

  .stepper-btn {
    width: 72rpx;
    height: 72rpx;
    border-radius: 16rpx;
    background: #f2f4fa;
    color: $text-1;
    font-size: 34rpx;
    display: flex;
    align-items: center;
    justify-content: center;

    &.disabled {
      opacity: 0.35;
    }
  }

  .stepper-num {
    min-width: 60rpx;
    text-align: center;
    font-size: 34rpx;
    font-weight: 800;
  }

  .stepper-hint {
    font-size: 22rpx;
    color: $text-3;
  }
}

.generate-btn {
  margin-top: 16rpx;
}
</style>
