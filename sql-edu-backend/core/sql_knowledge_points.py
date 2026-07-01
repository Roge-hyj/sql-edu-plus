"""
SQL Knowledge Point Taxonomy.

This module defines the classification, levels (Beginner, Intermediate, Advanced), and
localization mapping (Simplified Chinese, English, Traditional Chinese) for all SQL
knowledge points. These points are referenced across the system for auto-generated exercises
and Bayesian Knowledge Tracing (BKT) student models.
"""

from typing import TypedDict


class KnowledgePoint(TypedDict):
    """
    TypedDict schema representing a standard SQL knowledge point.

    Attributes:
        id (str): Unique identifier for the knowledge point.
        name (str): Chinese name of the knowledge point.
        level (str): Category level: "入门" (Beginner), "进阶" (Intermediate), or "精通" (Advanced).
        description (str): Short description of covered SQL features.
        name_i18n (dict[str, str]): Multi-language mapping for the name (zh-CN, en, zh-TW).
        level_i18n (dict[str, str]): Multi-language mapping for the level.
        description_i18n (dict[str, str]): Multi-language mapping for the description.
    """
    id: str
    name: str
    level: str  # 入门 / 进阶 / 精通
    description: str
    # Multi-language support (Chinese, English, and Traditional Chinese)
    name_i18n: dict[str, str]  # keys: zh-CN / en / zh-TW
    level_i18n: dict[str, str]
    description_i18n: dict[str, str]


# Defined taxonomy list categorized by complexity
SQL_KNOWLEDGE_POINTS: list[KnowledgePoint] = [
    # ---------- 入门 (Beginner) ----------
    {
        "id": "select-basic",
        "name": "SELECT 基础查询",
        "level": "入门",
        "description": "SELECT 列名、SELECT *、从单表查询数据",
        "name_i18n": {"zh-CN": "SELECT 基础查询", "en": "SELECT basics", "zh-TW": "SELECT 基礎查詢"},
        "level_i18n": {"zh-CN": "入门", "en": "Beginner", "zh-TW": "入門"},
        "description_i18n": {
            "zh-CN": "SELECT 列名、SELECT *、从单表查询数据",
            "en": "SELECT columns / SELECT *; query from a single table",
            "zh-TW": "SELECT 欄名、SELECT *、從單表查詢資料",
        },
    },
    {
        "id": "where",
        "name": "WHERE 条件筛选",
        "level": "入门",
        "description": "等于、不等于、大于小于、AND/OR、IN、BETWEEN、LIKE、IS NULL",
        "name_i18n": {"zh-CN": "WHERE 条件筛选", "en": "WHERE filtering", "zh-TW": "WHERE 條件篩選"},
        "level_i18n": {"zh-CN": "入门", "en": "Beginner", "zh-TW": "入門"},
        "description_i18n": {
            "zh-CN": "等于、不等于、大于小于、AND/OR、IN、BETWEEN、LIKE、IS NULL",
            "en": "Comparison ops, AND/OR, IN, BETWEEN, LIKE, IS NULL",
            "zh-TW": "等於、不等於、大於小於、AND/OR、IN、BETWEEN、LIKE、IS NULL",
        },
    },
    {
        "id": "order-by",
        "name": "ORDER BY 排序",
        "level": "入门",
        "description": "单列/多列排序、ASC/DESC",
        "name_i18n": {"zh-CN": "ORDER BY 排序", "en": "ORDER BY sorting", "zh-TW": "ORDER BY 排序"},
        "level_i18n": {"zh-CN": "入门", "en": "Beginner", "zh-TW": "入門"},
        "description_i18n": {
            "zh-CN": "单列/多列排序、ASC/DESC",
            "en": "Single/multiple-column sorting; ASC/DESC",
            "zh-TW": "單列/多列排序、ASC/DESC",
        },
    },
    {
        "id": "limit-offset",
        "name": "LIMIT 与分页",
        "level": "入门",
        "description": "LIMIT n、LIMIT n OFFSET m、取前几条",
        "name_i18n": {"zh-CN": "LIMIT 与分页", "en": "LIMIT & pagination", "zh-TW": "LIMIT 與分頁"},
        "level_i18n": {"zh-CN": "入门", "en": "Beginner", "zh-TW": "入門"},
        "description_i18n": {
            "zh-CN": "LIMIT n、LIMIT n OFFSET m、取前几条",
            "en": "LIMIT n; LIMIT n OFFSET m; top N rows",
            "zh-TW": "LIMIT n、LIMIT n OFFSET m、取前幾條",
        },
    },
    {
        "id": "distinct",
        "name": "DISTINCT 去重",
        "level": "入门",
        "description": "对查询结果去重",
        "name_i18n": {"zh-CN": "DISTINCT 去重", "en": "DISTINCT", "zh-TW": "DISTINCT 去重"},
        "level_i18n": {"zh-CN": "入门", "en": "Beginner", "zh-TW": "入門"},
        "description_i18n": {
            "zh-CN": "对查询结果去重",
            "en": "Remove duplicates in result set",
            "zh-TW": "對查詢結果去重",
        },
    },
    {
        "id": "alias",
        "name": "AS 别名",
        "level": "入门",
        "description": "列别名、表别名",
        "name_i18n": {"zh-CN": "AS 别名", "en": "Aliases (AS)", "zh-TW": "AS 別名"},
        "level_i18n": {"zh-CN": "入门", "en": "Beginner", "zh-TW": "入門"},
        "description_i18n": {
            "zh-CN": "列别名、表别名",
            "en": "Column aliases and table aliases",
            "zh-TW": "欄別名、表別名",
        },
    },
    {
        "id": "arithmetic",
        "name": "算术与常用函数",
        "level": "入门",
        "description": "加减乘除、CONCAT、LENGTH、UPPER/LOWER、日期函数等",
        "name_i18n": {"zh-CN": "算术与常用函数", "en": "Arithmetic & common functions", "zh-TW": "算術與常用函數"},
        "level_i18n": {"zh-CN": "入门", "en": "Beginner", "zh-TW": "入門"},
        "description_i18n": {
            "zh-CN": "加减乘除、CONCAT、LENGTH、UPPER/LOWER、日期函数等",
            "en": "Math ops; CONCAT, LENGTH, UPPER/LOWER, date functions, etc.",
            "zh-TW": "加減乘除、CONCAT、LENGTH、UPPER/LOWER、日期函數等",
        },
    },
    # ---------- 进阶 (Intermediate) ----------
    {
        "id": "agg-count",
        "name": "聚合函数 COUNT/SUM/AVG",
        "level": "进阶",
        "description": "COUNT、SUM、AVG、MIN、MAX 及与 WHERE 结合",
        "name_i18n": {"zh-CN": "聚合函数 COUNT/SUM/AVG", "en": "Aggregates: COUNT/SUM/AVG", "zh-TW": "聚合函數 COUNT/SUM/AVG"},
        "level_i18n": {"zh-CN": "进阶", "en": "Intermediate", "zh-TW": "進階"},
        "description_i18n": {
            "zh-CN": "COUNT、SUM、AVG、MIN、MAX 及与 WHERE 结合",
            "en": "COUNT, SUM, AVG, MIN, MAX; combine with WHERE",
            "zh-TW": "COUNT、SUM、AVG、MIN、MAX 及與 WHERE 結合",
        },
    },
    {
        "id": "group-by",
        "name": "GROUP BY 分组",
        "level": "进阶",
        "description": "按列分组、分组后聚合",
        "name_i18n": {"zh-CN": "GROUP BY 分组", "en": "GROUP BY", "zh-TW": "GROUP BY 分組"},
        "level_i18n": {"zh-CN": "进阶", "en": "Intermediate", "zh-TW": "進階"},
        "description_i18n": {
            "zh-CN": "按列分组、分组后聚合",
            "en": "Group rows; aggregate per group",
            "zh-TW": "按欄分組、分組後聚合",
        },
    },
    {
        "id": "having",
        "name": "HAVING 分组后筛选",
        "level": "进阶",
        "description": "对分组结果进行条件筛选",
        "name_i18n": {"zh-CN": "HAVING 分组后筛选", "en": "HAVING", "zh-TW": "HAVING 分組後篩選"},
        "level_i18n": {"zh-CN": "进阶", "en": "Intermediate", "zh-TW": "進階"},
        "description_i18n": {
            "zh-CN": "对分组结果进行条件筛选",
            "en": "Filter aggregated groups",
            "zh-TW": "對分組結果進行條件篩選",
        },
    },
    {
        "id": "join-inner",
        "name": "多表 INNER JOIN",
        "level": "进阶",
        "description": "内连接、多表等值连接",
        "name_i18n": {"zh-CN": "多表 INNER JOIN", "en": "INNER JOIN", "zh-TW": "多表 INNER JOIN"},
        "level_i18n": {"zh-CN": "进阶", "en": "Intermediate", "zh-TW": "進階"},
        "description_i18n": {
            "zh-CN": "内连接、多表等值连接",
            "en": "Inner joins; equi-joins across tables",
            "zh-TW": "內連接、多表等值連接",
        },
    },
    {
        "id": "join-left",
        "name": "LEFT JOIN 左连接",
        "level": "进阶",
        "description": "左外连接、保留左表全部行",
        "name_i18n": {"zh-CN": "LEFT JOIN 左连接", "en": "LEFT JOIN", "zh-TW": "LEFT JOIN 左連接"},
        "level_i18n": {"zh-CN": "进阶", "en": "Intermediate", "zh-TW": "進階"},
        "description_i18n": {
            "zh-CN": "左外连接、保留左表全部行",
            "en": "Left outer join; keep all rows from left table",
            "zh-TW": "左外連接、保留左表全部行",
        },
    },
    {
        "id": "join-right-full",
        "name": "RIGHT JOIN / FULL JOIN",
        "level": "进阶",
        "description": "右外连接、全外连接",
        "name_i18n": {"zh-CN": "RIGHT JOIN / FULL JOIN", "en": "RIGHT/FULL JOIN", "zh-TW": "RIGHT JOIN / FULL JOIN"},
        "level_i18n": {"zh-CN": "进阶", "en": "Intermediate", "zh-TW": "進階"},
        "description_i18n": {
            "zh-CN": "右外连接、全外连接",
            "en": "Right outer join; full outer join",
            "zh-TW": "右外連接、全外連接",
        },
    },
    {
        "id": "subquery-scalar",
        "name": "子查询（标量）",
        "level": "进阶",
        "description": "返回单行单列的子查询，用于 WHERE/SELECT",
        "name_i18n": {"zh-CN": "子查询（标量）", "en": "Scalar subquery", "zh-TW": "子查詢（標量）"},
        "level_i18n": {"zh-CN": "进阶", "en": "Intermediate", "zh-TW": "進階"},
        "description_i18n": {
            "zh-CN": "返回单行单列的子查询，用于 WHERE/SELECT",
            "en": "Returns a single value; used in WHERE/SELECT",
            "zh-TW": "返回單行單列的子查詢，用於 WHERE/SELECT",
        },
    },
    {
        "id": "subquery-in",
        "name": "子查询 IN / NOT IN",
        "level": "进阶",
        "description": "IN (子查询)、NOT IN、多值子查询",
        "name_i18n": {"zh-CN": "子查询 IN / NOT IN", "en": "IN / NOT IN subquery", "zh-TW": "子查詢 IN / NOT IN"},
        "level_i18n": {"zh-CN": "进阶", "en": "Intermediate", "zh-TW": "進階"},
        "description_i18n": {
            "zh-CN": "IN (子查询)、NOT IN、多值子查询",
            "en": "IN (subquery), NOT IN; multi-value subquery",
            "zh-TW": "IN (子查詢)、NOT IN、多值子查詢",
        },
    },
    {
        "id": "subquery-exists",
        "name": "EXISTS 子查询",
        "level": "进阶",
        "description": "EXISTS、NOT EXISTS 相关子查询",
        "name_i18n": {"zh-CN": "EXISTS 子查询", "en": "EXISTS subquery", "zh-TW": "EXISTS 子查詢"},
        "level_i18n": {"zh-CN": "进阶", "en": "Intermediate", "zh-TW": "進階"},
        "description_i18n": {
            "zh-CN": "EXISTS、NOT EXISTS 相关子查询",
            "en": "EXISTS / NOT EXISTS correlated subqueries",
            "zh-TW": "EXISTS、NOT EXISTS 相關子查詢",
        },
    },
    {
        "id": "union",
        "name": "UNION 集合操作",
        "level": "进阶",
        "description": "UNION、UNION ALL 合并结果集",
        "name_i18n": {"zh-CN": "UNION 集合操作", "en": "UNION / UNION ALL", "zh-TW": "UNION 集合操作"},
        "level_i18n": {"zh-CN": "进阶", "en": "Intermediate", "zh-TW": "進階"},
        "description_i18n": {
            "zh-CN": "UNION、UNION ALL 合并结果集",
            "en": "Combine result sets with UNION / UNION ALL",
            "zh-TW": "UNION、UNION ALL 合併結果集",
        },
    },
    {
        "id": "case",
        "name": "CASE 条件表达式",
        "level": "进阶",
        "description": "CASE WHEN THEN ELSE END、条件分支",
        "name_i18n": {"zh-CN": "CASE 条件表达式", "en": "CASE expression", "zh-TW": "CASE 條件表達式"},
        "level_i18n": {"zh-CN": "进阶", "en": "Intermediate", "zh-TW": "進階"},
        "description_i18n": {
            "zh-CN": "CASE WHEN THEN ELSE END、条件分支",
            "en": "CASE WHEN THEN ELSE END; conditional branching",
            "zh-TW": "CASE WHEN THEN ELSE END、條件分支",
        },
    },
    # ---------- 精通 (Advanced) ----------
    {
        "id": "window-row-number",
        "name": "窗口函数 ROW_NUMBER/RANK",
        "level": "精通",
        "description": "ROW_NUMBER()、RANK()、DENSE_RANK() OVER (PARTITION BY ... ORDER BY ...)",
        "name_i18n": {"zh-CN": "窗口函数 ROW_NUMBER/RANK", "en": "Window functions: ROW_NUMBER/RANK", "zh-TW": "視窗函數 ROW_NUMBER/RANK"},
        "level_i18n": {"zh-CN": "精通", "en": "Advanced", "zh-TW": "精通"},
        "description_i18n": {
            "zh-CN": "ROW_NUMBER()、RANK()、DENSE_RANK() OVER (PARTITION BY ... ORDER BY ...)",
            "en": "ROW_NUMBER(), RANK(), DENSE_RANK() OVER (PARTITION BY ... ORDER BY ...)",
            "zh-TW": "ROW_NUMBER()、RANK()、DENSE_RANK() OVER (PARTITION BY ... ORDER BY ...)",
        },
    },
    {
        "id": "window-agg",
        "name": "窗口聚合 SUM/AVG OVER",
        "level": "精通",
        "description": "SUM() OVER (PARTITION BY ...)、移动平均等",
        "name_i18n": {"zh-CN": "窗口聚合 SUM/AVG OVER", "en": "Window aggregates: SUM/AVG OVER", "zh-TW": "視窗聚合 SUM/AVG OVER"},
        "level_i18n": {"zh-CN": "精通", "en": "Advanced", "zh-TW": "精通"},
        "description_i18n": {
            "zh-CN": "SUM() OVER (PARTITION BY ...)、移动平均等",
            "en": "SUM() OVER (PARTITION BY ...), moving averages, etc.",
            "zh-TW": "SUM() OVER (PARTITION BY ...)、移動平均等",
        },
    },
    {
        "id": "cte",
        "name": "CTE 公共表表达式",
        "level": "精通",
        "description": "WITH cte AS (SELECT ...) 递归与多 CTE",
        "name_i18n": {"zh-CN": "CTE 公共表表达式", "en": "CTE (WITH)", "zh-TW": "CTE 公共表表達式"},
        "level_i18n": {"zh-CN": "精通", "en": "Advanced", "zh-TW": "精通"},
        "description_i18n": {
            "zh-CN": "WITH cte AS (SELECT ...) 递归与多 CTE",
            "en": "WITH cte AS (...); recursive and multiple CTEs",
            "zh-TW": "WITH cte AS (SELECT ...) 遞歸與多 CTE",
        },
    },
    {
        "id": "complex-join",
        "name": "复杂多表与自连接",
        "level": "精通",
        "description": "多表 JOIN、自连接、复杂业务逻辑",
        "name_i18n": {"zh-CN": "复杂多表与自连接", "en": "Complex joins & self-joins", "zh-TW": "複雜多表與自連接"},
        "level_i18n": {"zh-CN": "精通", "en": "Advanced", "zh-TW": "精通"},
        "description_i18n": {
            "zh-CN": "多表 JOIN、自连接、复杂业务逻辑",
            "en": "Multi-table joins, self joins, complex business logic",
            "zh-TW": "多表 JOIN、自連接、複雜業務邏輯",
        },
    },
    {
        "id": "null-handling",
        "name": "NULL 处理与 COALESCE",
        "level": "精通",
        "description": "COALESCE、NULLIF、三值逻辑",
        "name_i18n": {"zh-CN": "NULL 处理与 COALESCE", "en": "NULL handling & COALESCE", "zh-TW": "NULL 處理與 COALESCE"},
        "level_i18n": {"zh-CN": "精通", "en": "Advanced", "zh-TW": "精通"},
        "description_i18n": {
            "zh-CN": "COALESCE、NULLIF、三值逻辑",
            "en": "COALESCE, NULLIF, three-valued logic",
            "zh-TW": "COALESCE、NULLIF、三值邏輯",
        },
    },
]

# Order levels sequentially for sorting logic
LEVEL_ORDER = ("入门", "进阶", "精通")


def get_all_knowledge_points() -> list[KnowledgePoint]:
    """
    Retrieves all registered knowledge points sorted by levels and then list order.

    Returns:
        list[KnowledgePoint]: Sorted list of knowledge point records.
    """
    order = {l: i for i, l in enumerate(LEVEL_ORDER)}
    return sorted(SQL_KNOWLEDGE_POINTS, key=lambda x: (order.get(x["level"], 99), x["id"]))


def get_knowledge_point_by_id(point_id: str) -> KnowledgePoint | None:
    """
    Locates and returns a single knowledge point by its ID.

    Args:
        point_id (str): The unique ID of the target knowledge point.

    Returns:
        KnowledgePoint | None: The matching KnowledgePoint record if found, else None.
    """
    for p in SQL_KNOWLEDGE_POINTS:
        if p["id"] == point_id:
            return p
    return None


__all__ = [
    "KnowledgePoint",
    "SQL_KNOWLEDGE_POINTS",
    "get_all_knowledge_points",
    "get_knowledge_point_by_id",
]
