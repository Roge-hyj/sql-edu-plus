/**
 * SQL 知识点静态映射（与后端冻结的 SQL_KNOWLEDGE_TAXONOMY 同步）。
 *
 * 学生端学习画像（/ai/mastery-radar 返回按知识点 id 索引的 BKT 掌握度）
 * 需要展示知识点名称；后端 /questions/knowledge-points 仅教师可见，
 * 因此前端内置一份只读副本，未知 id 回退显示原始 id。
 */
import type { AiLanguage } from "@/utils/i18n";

export type KnowledgeMeta = {
  id: string;
  name: string;
  level: "beginner" | "intermediate" | "advanced";
  nameI18n: Partial<Record<AiLanguage, string>>;
};

export const LEVEL_ORDER: Array<{ key: KnowledgeMeta["level"]; labels: Record<string, string> }> = [
  { key: "beginner", labels: { "zh-CN": "入门", en: "Beginner", "zh-TW": "入門" } },
  { key: "intermediate", labels: { "zh-CN": "进阶", en: "Intermediate", "zh-TW": "進階" } },
  { key: "advanced", labels: { "zh-CN": "精通", en: "Advanced", "zh-TW": "精通" } },
];

export const KNOWLEDGE_META: KnowledgeMeta[] = [
  { id: "select-basic", name: "SELECT 基础查询", level: "beginner", nameI18n: { en: "SELECT basics", "zh-TW": "SELECT 基礎查詢" } },
  { id: "where", name: "WHERE 条件筛选", level: "beginner", nameI18n: { en: "WHERE filtering", "zh-TW": "WHERE 條件篩選" } },
  { id: "order-by", name: "ORDER BY 排序", level: "beginner", nameI18n: { en: "ORDER BY sorting", "zh-TW": "ORDER BY 排序" } },
  { id: "limit-offset", name: "LIMIT 与分页", level: "beginner", nameI18n: { en: "LIMIT & pagination", "zh-TW": "LIMIT 與分頁" } },
  { id: "distinct", name: "DISTINCT 去重", level: "beginner", nameI18n: { en: "DISTINCT", "zh-TW": "DISTINCT 去重" } },
  { id: "alias", name: "AS 别名", level: "beginner", nameI18n: { en: "Aliases (AS)", "zh-TW": "AS 別名" } },
  { id: "arithmetic", name: "算术与常用函数", level: "beginner", nameI18n: { en: "Arithmetic & functions", "zh-TW": "算術與常用函數" } },
  { id: "agg-count", name: "聚合函数 COUNT/SUM/AVG", level: "intermediate", nameI18n: { en: "Aggregates: COUNT/SUM/AVG", "zh-TW": "聚合函數 COUNT/SUM/AVG" } },
  { id: "group-by", name: "GROUP BY 分组", level: "intermediate", nameI18n: { en: "GROUP BY", "zh-TW": "GROUP BY 分組" } },
  { id: "having", name: "HAVING 分组后筛选", level: "intermediate", nameI18n: { en: "HAVING", "zh-TW": "HAVING 分組後篩選" } },
  { id: "join-inner", name: "多表 INNER JOIN", level: "intermediate", nameI18n: { en: "INNER JOIN", "zh-TW": "多表 INNER JOIN" } },
  { id: "join-left", name: "LEFT JOIN 左连接", level: "intermediate", nameI18n: { en: "LEFT JOIN", "zh-TW": "LEFT JOIN 左連接" } },
  { id: "join-right-full", name: "RIGHT JOIN / FULL JOIN", level: "intermediate", nameI18n: { en: "RIGHT/FULL JOIN", "zh-TW": "RIGHT JOIN / FULL JOIN" } },
  { id: "subquery-scalar", name: "子查询（标量）", level: "intermediate", nameI18n: { en: "Scalar subquery", "zh-TW": "子查詢（標量）" } },
  { id: "subquery-in", name: "子查询 IN / NOT IN", level: "intermediate", nameI18n: { en: "IN / NOT IN subquery", "zh-TW": "子查詢 IN / NOT IN" } },
  { id: "subquery-exists", name: "EXISTS 子查询", level: "intermediate", nameI18n: { en: "EXISTS subquery", "zh-TW": "EXISTS 子查詢" } },
  { id: "union", name: "UNION 集合操作", level: "intermediate", nameI18n: { en: "UNION / UNION ALL", "zh-TW": "UNION 集合操作" } },
  { id: "case", name: "CASE 条件表达式", level: "intermediate", nameI18n: { en: "CASE expression", "zh-TW": "CASE 條件表達式" } },
  { id: "window-row-number", name: "窗口函数 ROW_NUMBER/RANK", level: "advanced", nameI18n: { en: "ROW_NUMBER/RANK", "zh-TW": "視窗函數 ROW_NUMBER/RANK" } },
  { id: "window-agg", name: "窗口聚合 SUM/AVG OVER", level: "advanced", nameI18n: { en: "Window aggregates", "zh-TW": "視窗聚合 SUM/AVG OVER" } },
  { id: "cte", name: "CTE 公共表表达式", level: "advanced", nameI18n: { en: "CTE (WITH)", "zh-TW": "CTE 公共表表達式" } },
  { id: "complex-join", name: "复杂多表与自连接", level: "advanced", nameI18n: { en: "Complex joins", "zh-TW": "複雜多表與自連接" } },
  { id: "null-handling", name: "NULL 处理与 COALESCE", level: "advanced", nameI18n: { en: "NULL handling", "zh-TW": "NULL 處理與 COALESCE" } },
];

const META_BY_ID = new Map(KNOWLEDGE_META.map((m) => [m.id, m]));

export function knowledgeName(id: string, lang: AiLanguage): string {
  const meta = META_BY_ID.get(id);
  if (!meta) return id;
  return meta.nameI18n[lang] ?? meta.name;
}

export function knowledgeLevel(id: string): KnowledgeMeta["level"] {
  return META_BY_ID.get(id)?.level ?? "intermediate";
}
