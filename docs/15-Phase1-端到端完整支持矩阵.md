# Phase 1 端到端完整支持矩阵

生成时间：`2026-07-24T06:58:54.797759+00:00`；随机种子：`20260724`；两组各 250 条均已重跑。

端到端成功定义：结构命中、造数产生可观察反例、变异定位成功三者同时成立。

| 测试集 | 样例数 | 结构通过 | 造数通过 | 变异通过 | 端到端闭环 | 闭环率 |
|---|---:|---:|---:|---:|---:|---:|
| web_common250 (无方言) | 250 | 244 | 199 | 227 | 178 | 71.2% |
| online_random250 (有方言) | 250 | 225 | 158 | 205 | 121 | 48.4% |

## 按结构统计

| SQL 结构 | web 闭环/总数 | online 闭环/总数 | 当前结论 |
|---|---:|---:|---|
| SELECT | 11/11 | 15/15 | 稳定支持 |
| DISTINCT | 11/11 | 2/15 | 短板，需继续定向修复 |
| WHERE | 8/11 | 9/15 | 中等，复杂组合仍会掉点 |
| Comparison | 11/11 | 0/0 | 稳定支持 |
| NULL | 6/10 | 0/0 | 中等，复杂组合仍会掉点 |
| IN / BETWEEN / LIKE | 18/30 | 0/0 | 中等，复杂组合仍会掉点 |
| Logic | 10/10 | 0/0 | 稳定支持 |
| JOIN | 0/11 | 0/0 | 短板，需继续定向修复 |
| JOIN ON | 11/11 | 10/15 | 中等，复杂组合仍会掉点 |
| GROUP BY | 11/11 | 10/15 | 中等，复杂组合仍会掉点 |
| HAVING | 6/10 | 12/15 | 中等，复杂组合仍会掉点 |
| Aggregate | 11/11 | 6/15 | 中等，复杂组合仍会掉点 |
| ORDER BY | 10/10 | 8/15 | 中等，复杂组合仍会掉点 |
| LIMIT / OFFSET | 10/10 | 13/15 | 稳定支持 |
| Subquery | 5/10 | 4/15 | 短板，需继续定向修复 |
| Correlated Subquery | 5/10 | 4/14 | 短板，需继续定向修复 |
| CTE | 6/10 | 8/15 | 中等，复杂组合仍会掉点 |
| Recursive CTE | 6/10 | 0/14 | 短板，需继续定向修复 |
| Set Operation | 4/10 | 2/14 | 短板，需继续定向修复 |
| CASE | 11/11 | 8/14 | 中等，复杂组合仍会掉点 |
| Window | 7/11 | 5/15 | 短板，需继续定向修复 |
| Dialect Boundary | 0/10 | 5/14 | 短板，需继续定向修复 |

## 真实完整样例

### 端到端闭环样例：无方言教学题
- 数据集：`web_common250` / 结构：`SELECT` / ID：`web_select_projection_3`
- 来源：SQLZoo SELECT basics style <https://sqlzoo.net/wiki/SELECT_basics>
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

### 端到端闭环样例：真实在线题
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

### 端到端断点样例：完整 SQL，不使用省略
- 数据集：`online_random250` / 结构：`Set Operation` / ID：`online_random250_c343ba4fe30422a24e7e`
- 来源：LeetCode SQL Summary <https://github.com/siqichen-usc/LeetCode-SQL-Summary/blob/master/Join/Simple-Join/1270_All_People_Report_to_the_Given_Manager.sql>
标准:
```sql
WITH cte AS ( SELECT employee_id FROM Employees WHERE manager_id = 1 AND employee_id != manager_id UNION ALL SELECT e.employee_id FROM Employees e JOIN cte ON e.manager_id = cte.employee_id ) SELECT employee_id FROM cte OPTION (MAXRECURSION 3);
```
学生:
```sql
WITH cte AS ( SELECT employee_id FROM Employees WHERE manager_id = 1 AND employee_id != manager_id UNION SELECT e.employee_id FROM Employees e JOIN cte ON e.manager_id = cte.employee_id ) SELECT employee_id FROM cte OPTION (MAXRECURSION 3);
```
- 造数状态：`PASS`
- diff_types：`['set_operator_changed', 'set_modifier_changed', 'recursive_cte_changed', 'set_all_modifier_changed']`
- mutation_summary：`{'executed': 0, 'fixed_by_replacement': 0, 'remove_kept_correct': 0}`

## 当前结论

- 无方言组端到端闭环 178/250，闭环率 71.2%。
- 有方言组端到端闭环 121/250，闭环率 48.4%。
- 主要断点仍在造数未穿透、方言执行失败、复杂集合/递归/窗口组合的变异定位。
