# Phase 1 结构 IR 与 ASTDiff 支持矩阵

两组各 250 条均已重跑。

本文只记录结构层：解析、IR 可见性和 ASTDiff 是否能命中目标结构；不把造数失败计入结构失败。


| 测试集                    | 样例数 | 通过  | 失败  | 通过率   | 口径                             |
| ---------------------- | --- | --- | --- | ----- | ------------------------------ |
| web_common250 (无方言)    | 250 | 244 | 6   | 97.6% | 结构 IR/ASTDiff 满足 strict target |
| online_random250 (有方言) | 250 | 225 | 25  | 90.0% | 解析成功且产生 ASTDiff                |


## 按结构统计


| SQL 结构              | web 通过/总数 | online 通过/总数 | 当前结论        |
| ------------------- | --------- | ------------ | ----------- |
| SELECT              | 11/11     | 15/15        | 稳定支持        |
| DISTINCT            | 11/11     | 12/15        | 稳定支持        |
| WHERE               | 11/11     | 15/15        | 稳定支持        |
| Comparison          | 11/11     | 0/0          | 稳定支持        |
| NULL                | 10/10     | 0/0          | 稳定支持        |
| IN / BETWEEN / LIKE | 30/30     | 0/0          | 稳定支持        |
| Logic               | 10/10     | 0/0          | 稳定支持        |
| JOIN                | 11/11     | 0/0          | 稳定支持        |
| JOIN ON             | 11/11     | 15/15        | 稳定支持        |
| GROUP BY            | 11/11     | 13/15        | 稳定支持        |
| HAVING              | 10/10     | 15/15        | 稳定支持        |
| Aggregate           | 11/11     | 13/15        | 稳定支持        |
| ORDER BY            | 10/10     | 12/15        | 稳定支持        |
| LIMIT / OFFSET      | 10/10     | 15/15        | 稳定支持        |
| Subquery            | 10/10     | 14/15        | 稳定支持        |
| Correlated Subquery | 10/10     | 14/14        | 稳定支持        |
| CTE                 | 10/10     | 14/15        | 稳定支持        |
| Recursive CTE       | 10/10     | 12/14        | 稳定支持        |
| Set Operation       | 4/10      | 13/14        | 中等，复杂组合仍会掉点 |
| CASE                | 11/11     | 13/14        | 稳定支持        |
| Window              | 11/11     | 9/15         | 中等，复杂组合仍会掉点 |
| Dialect Boundary    | 10/10     | 11/14        | 稳定支持        |




## 真实完整样例



### 支持样例：基础投影结构命中

- 数据集：`web_common250` / 结构：`SELECT` / ID：`web_select_projection_3`
- 来源：SQLZoo SELECT basics style [https://sqlzoo.net/wiki/SELECT_basics](https://sqlzoo.net/wiki/SELECT_basics)
标准:

```sql
SELECT product_name, price FROM products;
```

学生:

```sql
SELECT product_name FROM products;
```

- 造数状态：`PASS`
- diff_types：`['projection_changed', 'column_dropped']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`



### 支持样例：逻辑结构命中且可执行

- 数据集：`web_common250` / 结构：`Logic` / ID：`web_logic_operator_5`
- 来源：SQLZoo SELECT basics style [https://sqlzoo.net/wiki/SELECT_basics](https://sqlzoo.net/wiki/SELECT_basics)
标准:

```sql
SELECT title FROM books WHERE price > 10 AND title LIKE 'A%';
```

学生:

```sql
SELECT title FROM books WHERE price > 10 OR title LIKE 'A%';
```

- 造数状态：`PASS`
- diff_types：`['where_changed', 'logical_operator_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`



### 当前结构短板样例：集合操作 strict target 未完全命中

- 数据集：`web_common250` / 结构：`Set Operation` / ID：`web_set_operator_4`
- 来源：PostgreSQL docs: UNION, CASE, and SELECT reference topics [https://www.postgresql.org/docs/current/queries-union.html](https://www.postgresql.org/docs/current/queries-union.html)
标准:

```sql
SELECT customer_name FROM orders WHERE total_amount > 40 UNION SELECT customer_name FROM orders WHERE total_amount < 4;
```

学生:

```sql
SELECT customer_name FROM orders WHERE total_amount > 40 INTERSECT SELECT customer_name FROM orders WHERE total_amount < 4;
```

- diff_types：`[]`
- mutation_summary：`{}`



## 当前结论

- 无方言组结构通过率已到 97.6%，剩余失败集中在 Set Operation 的 strict target。
- 有方言组按解析+ASTDiff 口径为 90.0%，方言深水区仍会造成 parse/diff 缺失。
- 样例均来自本轮实际 case，不使用省略 SQL。

