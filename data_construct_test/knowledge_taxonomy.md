# SQL DQL 知识点标签体系

本标签体系沿用小规模数据中的 L1/L2 双层结构，并补充 PDF 抽题时可能遇到的 DQL 细分点。

## L1 一级知识点

- `KP_BASIC`: 基础查询、投影、别名、去重、限制返回。
- `KP_FILTER`: WHERE 过滤、比较、逻辑组合、范围、集合、模式匹配。
- `KP_ORDER`: ORDER BY 排序。
- `KP_AGG`: 聚合、分组、HAVING。
- `KP_JOIN`: 多表连接。
- `KP_SUBQUERY`: 子查询。
- `KP_FUNC`: 字符串、数值、日期、CASE、类型转换等函数或表达式。
- `KP_ADVANCED`: 窗口函数、CTE、集合运算、NULL 处理等高级查询。

## L2 原子知识点

`KP_BASIC`

- `PROJ_COL`: 选择列。
- `PROJ_EXPR`: 查询表达式或计算列。
- `ALIAS_COL`: 列别名。
- `ALIAS_TAB`: 表别名。
- `DISTINCT_SET`: DISTINCT 去重。
- `LIMIT_OFF`: LIMIT/OFFSET 或 FETCH FIRST。

`KP_FILTER`

- `COMP_VAL`: 值比较。
- `COMP_NULL`: NULL 判断。
- `LOGIC_AND_OR`: AND/OR 组合条件。
- `LOGIC_NOT`: NOT 条件。
- `RANGE_BET`: BETWEEN 范围。
- `SET_IN`: IN/NOT IN。
- `LIKE_STR`: LIKE 模式匹配。

`KP_ORDER`

- `SORT_ASC`: 升序排序。
- `SORT_DESC`: 降序排序。
- `SORT_MULTI`: 多字段排序。
- `SORT_NULLS`: NULL 排序规则。

`KP_AGG`

- `AGG_BASIC`: COUNT/SUM/AVG/MIN/MAX。
- `AGG_DISTINCT`: 聚合中使用 DISTINCT。
- `GB_SIMPLE`: 单字段 GROUP BY。
- `GB_MULTI`: 多字段 GROUP BY。
- `HV_SIMPLE`: 简单 HAVING。
- `HV_COMPLEX`: HAVING 与复杂条件、子查询或聚合表达式组合。

`KP_JOIN`

- `JOIN_INNER`: 内连接。
- `JOIN_LEFT`: 左外连接。
- `JOIN_RIGHT`: 右外连接。
- `JOIN_FULL`: 全外连接。
- `JOIN_SELF`: 自连接。
- `JOIN_CROSS`: 笛卡尔积或 CROSS JOIN。
- `JOIN_ON`: ON 连接条件。
- `JOIN_USING`: USING 连接。
- `JOIN_NATURAL`: NATURAL JOIN。

`KP_SUBQUERY`

- `SUB_SCALAR`: 标量子查询。
- `SUB_ROW`: 行子查询。
- `SUB_TABLE`: 派生表或 FROM 子查询。
- `SUB_IN_ALL_ANY`: IN/ALL/ANY/SOME 子查询。
- `SUB_EXISTS`: EXISTS/NOT EXISTS。
- `SUB_CORR`: 相关子查询。

`KP_FUNC`

- `STR_CASE`: 字符串大小写处理。
- `STR_SUB`: 字符串截取、拼接或模式处理。
- `NUM_ROUND`: 数值函数或取整。
- `DATE_EXT`: 日期提取。
- `DATE_DIFF`: 日期差值。
- `CASE_SIMPLE`: 简单 CASE。
- `CASE_SEARCH`: 搜索 CASE。
- `TYPE_CAST`: 类型转换。

`KP_ADVANCED`

- `WIN_OVER`: OVER 窗口定义。
- `WIN_RANK`: RANK/DENSE_RANK/ROW_NUMBER。
- `WIN_LEAD_LAG`: LEAD/LAG。
- `WIN_FRAME`: 窗口框架。
- `CTE_SIMPLE`: 非递归 CTE。
- `CTE_RECURSIVE`: 递归 CTE。
- `SET_UNION`: UNION。
- `SET_INTERSECT`: INTERSECT。
- `SET_EXCEPT`: EXCEPT/MINUS。
- `NULL_COAL`: COALESCE/NULLIF 或 NULL 值替换。

## 补充标注规则
- SQL DQL 题包括 SELECT 查询、集合查询、查询嵌套、连接查询、聚合查询、窗口查询；不包括 CREATE/ALTER/INSERT/UPDATE/DELETE/GRANT/COMMIT。
