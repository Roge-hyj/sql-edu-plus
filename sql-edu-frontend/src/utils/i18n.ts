/**
 * 轻量三语 UI 文案系统。
 *
 * 后端 AI 反馈（check-sql / chat）与题目题面（title_en 等）都按
 * zh-CN / en / zh-TW 三语返回，前端 UI 保持同一语言开关，
 * storage key 沿用 `ai_language`。
 */
import { computed, ref } from "vue";
import type { AiLanguage, QuestionOut } from "@/types";

export type { AiLanguage };

const LANGUAGE_KEY = "ai_language";

function loadLanguage(): AiLanguage {
  try {
    const saved = uni.getStorageSync(LANGUAGE_KEY);
    if (saved === "en" || saved === "zh-TW") return saved;
  } catch {
    /* storage 不可用时回退默认 */
  }
  return "zh-CN";
}

/** 全局语言状态（跨页面共享同一份 ref）。 */
export const language = ref<AiLanguage>(loadLanguage());

export function setLanguage(lang: AiLanguage) {
  language.value = lang;
  try {
    uni.setStorageSync(LANGUAGE_KEY, lang);
  } catch {
    /* ignore */
  }
}

export const LANGUAGE_OPTIONS: Array<{ value: AiLanguage; label: string }> = [
  { value: "zh-CN", label: "简体中文" },
  { value: "en", label: "English" },
  { value: "zh-TW", label: "繁體中文" },
];

/** 按当前语言取三语文案字典。 */
export function useLang() {
  return computed(() => language.value);
}

/** 题面本地化回退链：en → title_en；zh-TW → title_zh_tw；默认原文。 */
export function localizedQuestionTitle(q: Pick<QuestionOut, "title" | "title_en" | "title_zh_tw">): string {
  if (language.value === "en") return q.title_en || q.title;
  if (language.value === "zh-TW") return q.title_zh_tw || q.title;
  return q.title;
}

export function localizedQuestionContent(
  q: Pick<QuestionOut, "content" | "content_en" | "content_zh_tw">,
): string {
  if (language.value === "en") return q.content_en || q.content;
  if (language.value === "zh-TW") return q.content_zh_tw || q.content;
  return q.content;
}

/** 三语字段字典取值（知识点 name_i18n 等），缺失回退原文。 */
export function localizedDict(
  base: string | null | undefined,
  dict?: Record<string, string> | null,
): string {
  if (language.value !== "zh-CN" && dict) {
    const hit = dict[language.value];
    if (hit) return hit;
  }
  return base || "";
}
