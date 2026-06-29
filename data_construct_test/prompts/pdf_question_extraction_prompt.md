# PDF SQL DQL 题目抽取提示词

你是数据库教材题库构建助手。请从我提供的 PDF 内容中抽取所有 SQL DQL 相关题目，并整理为 JSON。

## 抽取范围

只抽取要求写 SQL 查询或可直接改写为 SQL 查询的题目，包括：

- SELECT 基础查询
- WHERE 过滤
- ORDER BY
- JOIN
- GROUP BY / HAVING / 聚合函数
- 子查询
- 集合运算
- CASE、函数、NULL 处理
- 窗口函数、CTE 等高级查询

排除：

- DDL: CREATE/ALTER/DROP
- DML: INSERT/UPDATE/DELETE
- DCL/TCL
- 纯概念解释题
- 数据库设计、ER 图、规范化、事务、索引、存储、恢复、并发控制题
- 无法确定表结构或无法形成标准答案 SQL 的题

## 输出要求

按 PDF 中知识点和题目出现的先后顺序输出 JSON 数组。每个对象必须包含：

```json
{
  "id": 1,
  "difficulty": 1.0,
  "l1": "KP_BASIC",
  "l2": ["PROJ_COL"],
  "schema": "table_name(col1, col2)",
  "q": "question text",
  "ans_sql": "SELECT ...;",
  "source": "Book name, chapter/section/page/exercise number"
}
```

## 标签规则

使用以下 L1：

- `KP_BASIC`
- `KP_FILTER`
- `KP_ORDER`
- `KP_AGG`
- `KP_JOIN`
- `KP_SUBQUERY`
- `KP_FUNC`
- `KP_ADVANCED`

L2 标签参考 `knowledge_taxonomy.md`。如果题目涉及多个原子知识点，`l2` 可以包含多个标签。

## 难度评估

- 1.0-2.5: 单表、选择列、简单表达式、简单条件。
- 2.6-4.0: 多条件过滤、排序、LIKE、BETWEEN、IN、简单函数。
- 4.1-6.0: JOIN、基础聚合、GROUP BY、简单 HAVING。
- 6.1-8.0: 子查询、相关子查询、复杂 HAVING、多表组合、集合运算。
- 8.1-10.0: 窗口函数、递归 CTE、多层嵌套、复杂综合题。

## 质量要求

- 不要编造 PDF 中不存在的题目。
- 如果原题没有直接给出标准答案，请根据题意和表结构生成规范 SQL。
- 如果表结构散落在章节前文，请补齐到 `schema` 字段。
- `source` 必须能定位回 PDF。
- 输出必须是纯 JSON，不要输出解释文本。
