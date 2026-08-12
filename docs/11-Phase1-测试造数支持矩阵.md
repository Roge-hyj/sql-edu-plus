# Phase 1 测试造数支持矩阵

生成时间：`2026-07-24T06:58:54.797759+00:00`；随机种子：`20260724`；两组各 250 条均已重跑。

本文记录造数层：是否执行成功，以及是否生成可观察 counterexample。

| 测试集 | 样例数 | executed | 反例穿透 | 未穿透/执行失败 | 反例率 |
|---|---:|---:|---:|---:|---:|
| web_common250 (无方言) | 250 | 250 | 199 | 51 | 79.6% |
| online_random250 (有方言) | 250 | 237 | 158 | 92 | 63.2% |

## 状态分布

- web_common250：`{"PASS": 199, "MISSED_COUNTEREXAMPLE": 26, "TACTIC_BUT_NO_COUNTEREXAMPLE": 25}`
- online_random250：`{"TACTIC_BUT_NO_COUNTEREXAMPLE": 69, "PASS": 158, "MISSED_COUNTEREXAMPLE": 10, "EXEC_ERROR": 13}`

## 按结构统计

| SQL 结构 | web 反例/总数 | online 反例/总数 | 当前结论 |
|---|---:|---:|---|
| SELECT | 11/11 | 15/15 | 稳定支持 |
| DISTINCT | 11/11 | 9/15 | 中等，复杂组合仍会掉点 |
| WHERE | 8/11 | 9/15 | 中等，复杂组合仍会掉点 |
| Comparison | 11/11 | 0/0 | 稳定支持 |
| NULL | 6/10 | 0/0 | 中等，复杂组合仍会掉点 |
| IN / BETWEEN / LIKE | 18/30 | 0/0 | 中等，复杂组合仍会掉点 |
| Logic | 10/10 | 0/0 | 稳定支持 |
| JOIN | 7/11 | 0/0 | 中等，复杂组合仍会掉点 |
| JOIN ON | 11/11 | 10/15 | 中等，复杂组合仍会掉点 |
| GROUP BY | 11/11 | 10/15 | 中等，复杂组合仍会掉点 |
| HAVING | 10/10 | 12/15 | 稳定支持 |
| Aggregate | 11/11 | 7/15 | 中等，复杂组合仍会掉点 |
| ORDER BY | 10/10 | 9/15 | 中等，复杂组合仍会掉点 |
| LIMIT / OFFSET | 10/10 | 13/15 | 稳定支持 |
| Subquery | 5/10 | 6/15 | 短板，需继续定向修复 |
| Correlated Subquery | 5/10 | 4/14 | 短板，需继续定向修复 |
| CTE | 6/10 | 8/15 | 中等，复杂组合仍会掉点 |
| Recursive CTE | 10/10 | 10/14 | 中等，复杂组合仍会掉点 |
| Set Operation | 10/10 | 11/14 | 稳定支持 |
| CASE | 11/11 | 8/14 | 中等，复杂组合仍会掉点 |
| Window | 7/11 | 11/15 | 中等，复杂组合仍会掉点 |
| Dialect Boundary | 0/10 | 6/14 | 短板，需继续定向修复 |

## 真实完整样例

### 支持样例：嵌套 COUNT(DISTINCT) 可穿透
- 数据集：`web_common250` / 结构：`DISTINCT` / ID：`web_distinct_aggregate_gap_4`
- 来源：PostgreSQL tutorial: aggregate functions <https://www.postgresql.org/docs/current/tutorial-agg.html>
标准:
```sql
SELECT COUNT(DISTINCT customer_name) FROM orders;
```
学生:
```sql
SELECT COUNT(customer_name) FROM orders;
```
- 造数状态：`PASS`
- diff_types：`['projection_changed', 'column_dropped', 'column_added', 'aggregate_distinct_changed']`
- mutation_summary：`{'executed': 2, 'fixed_by_replacement': 2, 'remove_kept_correct': 0}`

### 支持样例：真实在线题可穿透
- 数据集：`online_random250` / 结构：`SELECT` / ID：`online_random250_66f226c84c4196d8a2f5`
- 来源：w3resource SQL Exercises mirror <https://github.com/tweichle/w3resource-SQL-Exercises/blob/master/SQL%20Exercises%20-%20SUBQUERIES%20on%20HR%20Database.sql>
标准:
```sql
SELECT employee_id, first_name, last_name FROM employees WHERE salary > (SELECT AVG(salary) FROM employees);
```
学生:
```sql
SELECT employee_id FROM employees WHERE salary > (SELECT AVG(salary) FROM employees);
```
- 造数状态：`PASS`
- diff_types：`['projection_changed', 'column_dropped', 'column_dropped']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### 当前造数短板样例：策略触发但未反例穿透
- 数据集：`online_random250` / 结构：`Window` / ID：`online_random250_b35890838a090e0cea34`
- 来源：LeetCode SQL Summary <https://github.com/siqichen-usc/LeetCode-SQL-Summary/blob/master/CASE-WHEN/1159_Market_Analysis_II.sql>
标准:
```sql
WITH tb1 AS ( SELECT seller_id, item_id FROM ( SELECT seller_id, item_id, ROW_NUMBER() OVER (PARTITION BY seller_id ORDER BY order_date) AS r FROM Orders ) rank WHERE r = 2 ) SELECT u.user_id AS seller_id, CASE WHEN u.favorite_brand = i.item_brand THEN 'yes' ELSE 'no' END AS '2nd_item_fav_brand' FROM Users u LEFT JOIN tb1 ON u.user_id = tb1.seller_id LEFT JOIN Items i ON tb1.item_id = i.item_id;
```
学生:
```sql
WITH tb1 AS ( SELECT seller_id, item_id FROM ( SELECT seller_id, item_id, ROW_NUMBER() OVER ( ORDER BY order_date) AS r FROM Orders ) rank WHERE r = 2 ) SELECT u.user_id AS seller_id, CASE WHEN u.favorite_brand = i.item_brand THEN 'yes' ELSE 'no' END AS '2nd_item_fav_brand' FROM Users u LEFT JOIN tb1 ON u.user_id = tb1.seller_id LEFT JOIN Items i ON tb1.item_id = i.item_id;
```
- 造数状态：`TACTIC_BUT_NO_COUNTEREXAMPLE`
- diff_types：`['cte_changed', 'projection_changed', 'column_dropped', 'column_added', 'window_over_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

## 当前结论

- 无方言组 250/250 全部执行，反例穿透 199/250。
- 有方言组执行 237/250，反例穿透 158/250；主要失败仍是 TACTIC_BUT_NO_COUNTEREXAMPLE 和 EXEC_ERROR。
- 本轮复合逻辑真值表和 COUNT(DISTINCT) 分组重复探针已纳入结果。
