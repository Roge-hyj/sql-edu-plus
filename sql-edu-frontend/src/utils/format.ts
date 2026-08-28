/**
 * 难度配色 / 标签与时间格式化工具。
 */
import { language } from "@/utils/i18n";

/** 1~10 十档难度色（绿 → 蓝 → 琥珀 → 橙红 → 紫）。 */
const DIFFICULTY_COLORS: Record<number, string> = {
  1: "#10B981",
  2: "#22C08E",
  3: "#0EA5E9",
  4: "#3B82F6",
  5: "#8B5CF6",
  6: "#F59E0B",
  7: "#F97316",
  8: "#EF4444",
  9: "#DC2626",
  10: "#B91C9C",
};

export function difficultyColor(value: number | null | undefined): string {
  const v = Math.max(1, Math.min(10, Math.round(Number(value ?? 3))));
  return DIFFICULTY_COLORS[v] ?? "#0EA5E9";
}

export function clampDifficulty(value: number | null | undefined, fallback = 3): number {
  const v = Number(value ?? fallback);
  if (!Number.isFinite(v)) return fallback;
  return Math.max(1, Math.min(10, Math.round(v)));
}

type DifficultyTier = { min: number; max: number; labels: Record<string, string> };

const TIERS: DifficultyTier[] = [
  { min: 1, max: 2, labels: { "zh-CN": "入门", en: "Easy", "zh-TW": "入門" } },
  { min: 3, max: 4, labels: { "zh-CN": "简单", en: "Basic", "zh-TW": "簡單" } },
  { min: 5, max: 6, labels: { "zh-CN": "中等", en: "Medium", "zh-TW": "中等" } },
  { min: 7, max: 8, labels: { "zh-CN": "较难", en: "Hard", "zh-TW": "較難" } },
  { min: 9, max: 10, labels: { "zh-CN": "挑战", en: "Expert", "zh-TW": "挑戰" } },
];

export function difficultyLabel(value: number | null | undefined): string {
  const v = clampDifficulty(value);
  const tier = TIERS.find((t) => v >= t.min && v <= t.max) ?? TIERS[0];
  return tier.labels[language.value] ?? tier.labels["zh-CN"];
}

/** 展示难度优先；数值保留 1 位小数。 */
export function displayDifficultyOf(q: {
  difficulty: number;
  display_difficulty?: number | null;
}): number {
  return clampDifficulty(q.display_difficulty ?? q.difficulty);
}

export function difficultyText(value: number | null | undefined): string {
  const v = Number(value ?? 0);
  return Number.isFinite(v) ? v.toFixed(1).replace(/\.0$/, "") : "-";
}

/** 仿微信时间：今天 HH:mm / 昨天 HH:mm / 同年 M/D HH:mm / 跨年 Y-M-D HH:mm。 */
export function formatChatTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const sameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate();
  if (sameDay(d, now)) return hm;
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (sameDay(d, yesterday)) return `昨天 ${hm}`;
  if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()} ${hm}`;
}

/**
 * schema_preview JSON → [{name, columns, rows}]，过滤非法结构。
 *
 * 后端净化后的格式：tables[{name, columns: ["x"|{name}], rows: [{x: v}]}]；
 * 兼容未净化的 rows 为二维数组的形式。统一输出按 columns 对齐的二维数组。
 */
export type SchemaTable = { name: string; columns: string[]; rows: unknown[][] };

function columnName(raw: unknown): string {
  if (typeof raw === "string") return raw;
  if (raw && typeof raw === "object") {
    const name = (raw as { name?: unknown }).name;
    if (typeof name === "string" && name) return name;
  }
  return "";
}

export function parseSchemaPreview(raw: string | null | undefined): SchemaTable[] {
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  const tables = (parsed as { tables?: unknown })?.tables;
  if (!Array.isArray(tables)) return [];
  const out: SchemaTable[] = [];
  for (const t of tables) {
    const table = t as { name?: unknown; columns?: unknown; rows?: unknown };
    const name = typeof table?.name === "string" ? table.name : "";
    const columns = Array.isArray(table?.columns)
      ? table.columns.map(columnName).filter(Boolean)
      : [];
    if (!name || columns.length === 0) continue;
    const rows: unknown[][] = [];
    if (Array.isArray(table?.rows)) {
      for (const r of table.rows) {
        if (Array.isArray(r)) {
          if (r.length === columns.length) rows.push(r);
        } else if (r && typeof r === "object") {
          const obj = r as Record<string, unknown>;
          rows.push(columns.map((c) => obj[c]));
        }
      }
    }
    out.push({ name, columns, rows });
  }
  return out;
}

export function cellText(value: unknown): string {
  if (value === null || value === undefined) return "NULL";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
