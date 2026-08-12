# Phase 1 测试变异支持矩阵

生成时间：`2026-07-24T06:58:54.797759+00:00`；随机种子：`20260724`；两组各 250 条均已重跑。

本文记录变异层：是否通过 clause/节点替换证明错因可被定位。

| 测试集 | 样例数 | 变异定位通过 | 失败 | 通过率 |
|---|---:|---:|---:|---:|
| web_common250 (无方言) | 250 | 227 | 23 | 90.8% |
| online_random250 (有方言) | 250 | 205 | 45 | 82.0% |

## 按结构统计

| SQL 结构 | web 变异/总数 | online 变异/总数 | 当前结论 |
|---|---:|---:|---|
| SELECT | 11/11 | 15/15 | 稳定支持 |
| DISTINCT | 11/11 | 9/15 | 中等，复杂组合仍会掉点 |
| WHERE | 11/11 | 15/15 | 稳定支持 |
| Comparison | 11/11 | 0/0 | 稳定支持 |
| NULL | 10/10 | 0/0 | 稳定支持 |
| IN / BETWEEN / LIKE | 30/30 | 0/0 | 稳定支持 |
| Logic | 10/10 | 0/0 | 稳定支持 |
| JOIN | 4/11 | 0/0 | 短板，需继续定向修复 |
| JOIN ON | 11/11 | 14/15 | 稳定支持 |
| GROUP BY | 11/11 | 15/15 | 稳定支持 |
| HAVING | 6/10 | 15/15 | 中等，复杂组合仍会掉点 |
| Aggregate | 11/11 | 12/15 | 稳定支持 |
| ORDER BY | 10/10 | 13/15 | 稳定支持 |
| LIMIT / OFFSET | 10/10 | 14/15 | 稳定支持 |
| Subquery | 10/10 | 13/15 | 稳定支持 |
| Correlated Subquery | 10/10 | 14/14 | 稳定支持 |
| CTE | 6/10 | 14/15 | 中等，复杂组合仍会掉点 |
| Recursive CTE | 6/10 | 0/14 | 短板，需继续定向修复 |
| Set Operation | 10/10 | 5/14 | 中等，复杂组合仍会掉点 |
| CASE | 11/11 | 12/14 | 稳定支持 |
| Window | 11/11 | 13/15 | 稳定支持 |
| Dialect Boundary | 6/10 | 12/14 | 中等，复杂组合仍会掉点 |

## 真实完整样例

### 支持样例：变异替换可恢复等价
- 数据集：`web_common250` / 结构：`DISTINCT` / ID：`web_distinct_basic_5`
- 来源：PostgreSQL tutorial: querying a table <https://www.postgresql.org/docs/current/tutorial-select.html>
标准:
```sql
SELECT DISTINCT title FROM books;
```
学生:
```sql
SELECT title FROM books;
```
- 造数状态：`PASS`
- diff_types：`['distinct_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### 支持样例：真实在线题变异定位成功
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

### 当前变异短板样例：已有反例但变异未定位
- 数据集：`web_common250` / 结构：`HAVING` / ID：`web_having_where_gap_6`
- 来源：PostgreSQL tutorial: aggregate functions <https://www.postgresql.org/docs/current/tutorial-agg.html>
标准:
```sql
SELECT account_id, AVG(amount) FROM payments GROUP BY account_id HAVING AVG(amount) > 60;
```
学生:
```sql
SELECT account_id, AVG(amount) FROM payments WHERE AVG(amount) > 60 GROUP BY account_id;
```
- 造数状态：`PASS`
- diff_types：`['where_changed', 'having_changed', 'aggregate_condition_in_where']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 0, 'remove_kept_correct': 0}`

## 当前结论

- 无方言组变异定位 227/250，仍是最稳模块。
- 有方言组变异定位 205/250；失败多由执行错误、复杂递归/集合或造数未穿透间接导致。
- 变异结果强依赖可执行 sandbox 和可观察反例。
