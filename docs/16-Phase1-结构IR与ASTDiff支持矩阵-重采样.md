# Phase 1 结构 IR 与 ASTDiff 支持矩阵（重采样）

无方言与有方言两组各 250 条均已重跑。

本文只记录结构层：解析、IR 可见性和 ASTDiff 是否能命中目标结构；不把造数失败计入结构失败。

| 测试集 | 样例数 | 通过 | 失败 | 通过率 | 口径 |
|---|---:|---:|---:|---:|---|
| web_common250 (无方言) | 250 | 244 | 6 | 97.6% | 结构 IR/ASTDiff 满足 strict target |
| online_random250 (有方言) | 250 | 226 | 24 | 90.4% | 解析成功且产生 ASTDiff |

## 按结构统计

| SQL 结构 | web 通过/总数 | online 通过/总数 | 当前结论 |
|---|---:|---:|---|
| SELECT | 11/11 | 15/15 | 稳定支持 |
| DISTINCT | 11/11 | 11/15 | 中等，复杂组合仍会掉点 |
| WHERE | 11/11 | 15/15 | 稳定支持 |
| Comparison | 11/11 | 0/0 | 稳定支持 |
| NULL | 10/10 | 0/0 | 稳定支持 |
| IN / BETWEEN / LIKE | 30/30 | 0/0 | 稳定支持 |
| Logic | 10/10 | 0/0 | 稳定支持 |
| JOIN | 11/11 | 0/0 | 稳定支持 |
| JOIN ON | 11/11 | 15/15 | 稳定支持 |
| GROUP BY | 11/11 | 14/15 | 稳定支持 |
| HAVING | 10/10 | 15/15 | 稳定支持 |
| Aggregate | 11/11 | 11/15 | 中等，复杂组合仍会掉点 |
| ORDER BY | 10/10 | 13/15 | 稳定支持 |
| LIMIT / OFFSET | 10/10 | 15/15 | 稳定支持 |
| Subquery | 10/10 | 15/15 | 稳定支持 |
| Correlated Subquery | 10/10 | 14/14 | 稳定支持 |
| CTE | 10/10 | 13/15 | 稳定支持 |
| Recursive CTE | 10/10 | 12/14 | 稳定支持 |
| Set Operation | 4/10 | 13/14 | 中等，复杂组合仍会掉点 |
| CASE | 11/11 | 14/14 | 稳定支持 |
| Window | 11/11 | 9/15 | 中等，复杂组合仍会掉点 |
| Dialect Boundary | 10/10 | 12/14 | 稳定支持 |

## 真实完整样例

### 支持样例：基础投影结构命中
- 数据集：`web_common250` / 结构：`SELECT` / ID：`web_select_alias_gap_4`
- 来源：PostgreSQL tutorial: querying a table <https://www.postgresql.org/docs/current/tutorial-select.html>
- 表结构：`orders(customer_name)`
标准:
```sql
SELECT customer_name AS display_name FROM orders;
```
学生:
```sql
SELECT customer_name FROM orders;
```
- 造数状态：`PASS`
- diff_types：`['alias_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### 支持样例：真实在线题结构命中
- 数据集：`online_random250` / 结构：`Correlated Subquery` / ID：`online_random250_1e0a4ddb8e0aec06065a`
- 来源：w3resource SQL Exercises mirror <https://github.com/tweichle/w3resource-SQL-Exercises/blob/master/SQL%20Exercises%20-%20SUBQUERIES%20on%20Sales%20Database.sql>
- 表结构：`orders(ord_date, purch_amt)`
标准:
```sql
SELECT ord_date, SUM(purch_amt) FROM orders a GROUP BY ord_date HAVING SUM(purch_amt) > (SELECT MAX(purch_amt) + 1000 FROM orders b WHERE a.ord_date = b.ord_date);
```
学生:
```sql
SELECT ord_date, SUM(purch_amt) FROM orders a GROUP BY ord_date HAVING SUM(purch_amt) >= (SELECT MAX(purch_amt) + 1000 FROM orders b WHERE a.ord_date = b.ord_date);
```
- 造数状态：`TACTIC_BUT_NO_COUNTEREXAMPLE`
- diff_types：`['having_changed', 'correlated_predicate_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### 当前结构短板样例：strict target 未完全命中
- 数据集：`online_random250` / 结构：`Window` / ID：`online_random250_41bdf22cc750f04e74f5`
- 来源：SQL-Exercises <https://github.com/amirai31/SQL-Exercises/blob/main/SQL_Window%20functions%20and%20CTEs.sql>
- 表结构：`sales(order_date, region, revenue)`
标准:
```sql
WITH RankedSales AS ( SELECT region, order_date, revenue, ROW_NUMBER() OVER (PARTITION BY region ORDER BY revenue DESC) AS row_num FROM sales ) SELECT region, order_date, revenue FROM RankedSales WHERE row_num <= 3 ORDER BY region, row_num;
```
学生:
```sql
WITH RankedSales AS ( SELECT region, order_date, revenue, ROW_NUMBER() OVER ( ORDER BY revenue DESC) AS row_num FROM sales ) SELECT region, order_date, revenue FROM RankedSales WHERE row_num <= 3 ORDER BY region, row_num;
```
- 造数状态：`PASS`
- diff_types：`[]`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

## 按结构展开样例

### SELECT

### 支持样例
- 数据集：`online_random250` / 结构：`SELECT` / ID：`online_random250_8a48b7cda0d080bd1dbf`
- 来源：LeetCode SQL Summary <https://github.com/siqichen-usc/LeetCode-SQL-Summary/blob/master/CASE-WHEN/1193_Monthly_Transactions_I.sql>
- 表结构：`Transactions(id, amount, country, state, trans_date)`
标准:
```sql
SELECT LEFT(trans_date,7) AS month, country, COUNT(id) AS trans_count, SUM(CASE WHEN state = 'approved' THEN 1 ELSE 0 END) AS approved_count, SUM(amount) AS trans_total_amount, SUM(CASE WHEN state = 'approved' THEN amount ELSE 0 END) AS approved_total_amount FROM Transactions GROUP BY LEFT(trans_date,7), country;
```
学生:
```sql
SELECT LEFT(trans_date FROM Transactions GROUP BY LEFT(trans_date,7), country;
```
- 结构判定：`支持`
- 造数状态：`EXEC_ERROR`
- diff_types：`['projection_changed', 'column_dropped', 'column_dropped', 'column_dropped', 'column_dropped', 'column_dropped', 'column_dropped', 'column_added', 'function_argument_changed', 'predicate_missing', 'case_changed']`
- mutation_summary：`{'executed': 0, 'fixed_by_replacement': 0}`

### 不支持样例
- 数据集：`online_random250` / 结构：`SELECT` / ID：`online_random250_8a48b7cda0d080bd1dbf`
- 来源：LeetCode SQL Summary <https://github.com/siqichen-usc/LeetCode-SQL-Summary/blob/master/CASE-WHEN/1193_Monthly_Transactions_I.sql>
- 表结构：`Transactions(id, amount, country, state, trans_date)`
标准:
```sql
SELECT LEFT(trans_date,7) AS month, country, COUNT(id) AS trans_count, SUM(CASE WHEN state = 'approved' THEN 1 ELSE 0 END) AS approved_count, SUM(amount) AS trans_total_amount, SUM(CASE WHEN state = 'approved' THEN amount ELSE 0 END) AS approved_total_amount FROM Transactions GROUP BY LEFT(trans_date,7), country;
```
学生:
```sql
SELECT LEFT(trans_date FROM Transactions GROUP BY LEFT(trans_date,7), country;
```
- 造数判定：`不支持`
- 造数状态：`EXEC_ERROR`
- diff_types：`['projection_changed', 'column_dropped', 'column_dropped', 'column_dropped', 'column_dropped', 'column_dropped', 'column_dropped', 'column_added', 'function_argument_changed', 'predicate_missing', 'case_changed']`
- mutation_summary：`{'executed': 0, 'fixed_by_replacement': 0}`

### DISTINCT

### 支持样例
- 数据集：`online_random250` / 结构：`DISTINCT` / ID：`online_random250_8b1fb5bdfc490aeb01c4`
- 来源：PostgreSQL SELECT reference <https://www.postgresql.org/docs/current/sql-select.html>
- 表结构：`weather_reports(location, report, time)`
标准:
```sql
SELECT DISTINCT ON (location) location, time, report FROM weather_reports ORDER BY location, time DESC;
```
学生:
```sql
SELECT location, time, report FROM weather_reports ORDER BY location, time DESC
```
- 结构判定：`支持`
- 造数状态：`PASS`
- diff_types：`['distinct_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### 不支持样例
- 数据集：`online_random250` / 结构：`DISTINCT` / ID：`online_random250_e4c16c6b14118f40f359`
- 来源：LeetCode SQL Summary <https://github.com/siqichen-usc/LeetCode-SQL-Summary/blob/master/Basics/1148_Article_Views_I.sql>
- 表结构：`Views(id, author_id, viewer_id)`
标准:
```sql
SELECT DISTINCT author_id AS id FROM Views WHERE author_id = viewer_id ORDER BY id;
```
学生:
```sql
SELECT author_id AS id FROM Views WHERE author_id = viewer_id ORDER BY id
```
- 结构判定：`不支持`
- 造数状态：`PASS`
- diff_types：`[]`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### WHERE

### 支持样例
- 数据集：`web_common250` / 结构：`WHERE` / ID：`web_where_expression_gap_6`
- 来源：PostgreSQL tutorial: querying a table <https://www.postgresql.org/docs/current/tutorial-select.html>
- 表结构：`courses(course_name, credits)`
标准:
```sql
SELECT course_name FROM courses WHERE credits * 2 > 600;
```
学生:
```sql
SELECT course_name FROM courses WHERE credits + 2 > 600;
```
- 结构判定：`支持`
- 造数状态：`TACTIC_BUT_NO_COUNTEREXAMPLE`
- diff_types：`['where_changed', 'predicate_expression_operator_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### 不支持样例
- 数据集：`web_common250` / 结构：`WHERE` / ID：`web_where_expression_gap_6`
- 来源：PostgreSQL tutorial: querying a table <https://www.postgresql.org/docs/current/tutorial-select.html>
- 表结构：`courses(course_name, credits)`
标准:
```sql
SELECT course_name FROM courses WHERE credits * 2 > 600;
```
学生:
```sql
SELECT course_name FROM courses WHERE credits + 2 > 600;
```
- 造数判定：`不支持`
- 造数状态：`TACTIC_BUT_NO_COUNTEREXAMPLE`
- diff_types：`['where_changed', 'predicate_expression_operator_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### Comparison

### 支持样例
- 数据集：`web_common250` / 结构：`Comparison` / ID：`web_comparison_operator_11`
- 来源：SQLZoo SELECT basics style <https://sqlzoo.net/wiki/SELECT_basics>
- 表结构：`products(price, product_name)`
标准:
```sql
SELECT product_name FROM products WHERE price <= 77;
```
学生:
```sql
SELECT product_name FROM products WHERE price < 77;
```
- 结构判定：`支持`
- 造数状态：`PASS`
- diff_types：`['where_changed', 'comparison_operator_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### 不支持样例
- 数据集：`web_common250` / 结构：`Comparison` / ID：`web_comparison_column_gap_1`
- 来源：PostgreSQL tutorial: querying a table <https://www.postgresql.org/docs/current/tutorial-select.html>
- 表结构：`students(gpa, name)`
标准:
```sql
SELECT name FROM students WHERE gpa > 50;
```
学生:
```sql
SELECT name FROM students WHERE name > 50;
```
- 造数判定：`不支持`
- 造数状态：`TACTIC_BUT_NO_COUNTEREXAMPLE`
- diff_types：`['where_changed', 'predicate_missing', 'predicate_added', 'comparison_left_column_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### NULL

### 支持样例
- 数据集：`web_common250` / 结构：`NULL` / ID：`web_null_antijoin_gap_3`
- 来源：PostgreSQL tutorial: querying a table <https://www.postgresql.org/docs/current/tutorial-select.html>
- 表结构：`majors(id, inactive_at); students(major_id, name)`
标准:
```sql
SELECT name FROM students WHERE major_id NOT IN (SELECT id FROM majors WHERE inactive_at IS NULL);
```
学生:
```sql
SELECT s.name FROM students s WHERE NOT EXISTS (SELECT 1 FROM majors m WHERE m.inactive_at IS NULL AND m.id = s.major_id);
```
- 结构判定：`支持`
- 造数状态：`TACTIC_BUT_NO_COUNTEREXAMPLE`
- diff_types：`['where_changed', 'predicate_missing', 'correlated_predicate_changed', 'correlated_predicate_changed', 'projection_changed', 'where_changed', 'column_dropped', 'column_added', 'logical_operator_changed', 'null_sensitive_antijoin_equivalence']`
- mutation_summary：`{'executed': 2, 'fixed_by_replacement': 2, 'remove_kept_correct': 1}`

### 不支持样例
- 数据集：`web_common250` / 结构：`NULL` / ID：`web_null_antijoin_gap_3`
- 来源：PostgreSQL tutorial: querying a table <https://www.postgresql.org/docs/current/tutorial-select.html>
- 表结构：`majors(id, inactive_at); students(major_id, name)`
标准:
```sql
SELECT name FROM students WHERE major_id NOT IN (SELECT id FROM majors WHERE inactive_at IS NULL);
```
学生:
```sql
SELECT s.name FROM students s WHERE NOT EXISTS (SELECT 1 FROM majors m WHERE m.inactive_at IS NULL AND m.id = s.major_id);
```
- 造数判定：`不支持`
- 造数状态：`TACTIC_BUT_NO_COUNTEREXAMPLE`
- diff_types：`['where_changed', 'predicate_missing', 'correlated_predicate_changed', 'correlated_predicate_changed', 'projection_changed', 'where_changed', 'column_dropped', 'column_added', 'logical_operator_changed', 'null_sensitive_antijoin_equivalence']`
- mutation_summary：`{'executed': 2, 'fixed_by_replacement': 2, 'remove_kept_correct': 1}`

### IN / BETWEEN / LIKE

### 支持样例
- 数据集：`web_common250` / 结构：`IN / BETWEEN / LIKE` / ID：`web_like_pattern_6`
- 来源：W3Schools NULL / IN / BETWEEN / LIKE style <https://www.w3schools.com/sql/sql_null_values.asp>
- 表结构：`courses(course_name)`
标准:
```sql
SELECT course_name FROM courses WHERE course_name LIKE 'A%';
```
学生:
```sql
SELECT course_name FROM courses WHERE course_name LIKE 'B%';
```
- 结构判定：`支持`
- 造数状态：`PASS`
- diff_types：`['where_changed', 'literal_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### 不支持样例
- 数据集：`web_common250` / 结构：`IN / BETWEEN / LIKE` / ID：`web_in_exists_gap_4`
- 来源：SQLZoo nested SELECT style <https://sqlzoo.net/wiki/SELECT_within_SELECT_Tutorial>
- 表结构：`departments(id, active); orders(customer_id, customer_name)`
标准:
```sql
SELECT customer_name FROM orders WHERE customer_id IN (SELECT id FROM departments WHERE active = 1);
```
学生:
```sql
SELECT t.customer_name FROM orders t WHERE EXISTS (SELECT 1 FROM departments d WHERE d.active = 1 AND d.id = t.customer_id);
```
- 造数判定：`不支持`
- 造数状态：`TACTIC_BUT_NO_COUNTEREXAMPLE`
- diff_types：`['where_changed', 'predicate_missing', 'correlated_predicate_changed', 'correlated_predicate_changed', 'projection_changed', 'where_changed', 'column_dropped', 'column_added', 'logical_operator_changed', 'in_exists_equivalence']`
- mutation_summary：`{'executed': 2, 'fixed_by_replacement': 2, 'remove_kept_correct': 0}`

### Logic

### 支持样例
- 数据集：`web_common250` / 结构：`Logic` / ID：`web_logic_operator_6`
- 来源：SQLZoo SELECT basics style <https://sqlzoo.net/wiki/SELECT_basics>
- 表结构：`courses(course_name, credits)`
标准:
```sql
SELECT course_name FROM courses WHERE credits > 10 AND course_name LIKE 'A%';
```
学生:
```sql
SELECT course_name FROM courses WHERE credits > 10 OR course_name LIKE 'A%';
```
- 结构判定：`支持`
- 造数状态：`PASS`
- diff_types：`['where_changed', 'logical_operator_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### 不支持样例
- 数据集：`ai_robustness_run_report_10k` / 结构：`Logic` / ID：`ai_10k_1_020`
- 来源：AI robustness run report 10k
- 表结构：`employee(id, name, department, status);`
标准:
```sql
SELECT name FROM employee WHERE department = 'Sales' AND status = 'Active';
```
学生:
```sql
SELECT name FROM employee WHERE department = 'Sales' OR status = 'Active';
```
- 结构判定：`不支持`
- 造数状态：`MISS_EQUIV_TRUE`
- diff_types：`['projection_changed', 'where_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### JOIN

### 支持样例
- 数据集：`web_common250` / 结构：`JOIN` / ID：`web_join_missing_2`
- 来源：SQLZoo JOIN style <https://sqlzoo.net/wiki/The_JOIN_operation>
- 表结构：`departments(id, department_name); employees(department_id, employee_name)`
标准:
```sql
SELECT e.employee_name, d.department_name FROM employees e JOIN departments d ON e.department_id = d.id;
```
学生:
```sql
SELECT e.employee_name FROM employees;
```
- 结构判定：`支持`
- 造数状态：`PASS`
- diff_types：`['projection_changed', 'column_dropped', 'join_missing', 'join_on_changed']`
- mutation_summary：`{'executed': 0, 'fixed_by_replacement': 0, 'remove_kept_correct': 0}`

### 不支持样例
- 数据集：`web_common250` / 结构：`JOIN` / ID：`web_join_implicit_equiv_gap_2`
- 来源：PostgreSQL tutorial: joins between tables <https://www.postgresql.org/docs/current/tutorial-join.html>
- 表结构：`departments(id, department_name); employees(department_id, employee_name)`
标准:
```sql
SELECT e.employee_name, d.department_name FROM employees e JOIN departments d ON e.department_id = d.id;
```
学生:
```sql
SELECT e.employee_name, d.department_name FROM employees e, departments d WHERE e.department_id = d.id;
```
- 造数判定：`不支持`
- 造数状态：`MISSED_COUNTEREXAMPLE`
- diff_types：`[]`
- mutation_summary：`{'executed': 3, 'fixed_by_replacement': 2, 'remove_kept_correct': 0}`

### JOIN ON

### 支持样例
- 数据集：`online_random250` / 结构：`JOIN ON` / ID：`online_random250_c88e5813f15dce3f8a2f`
- 来源：LeetCode SQL Summary <https://github.com/siqichen-usc/LeetCode-SQL-Summary/blob/master/Join/Simple-Join/574_Winning_Candidate.sql>
- 表结构：`Candidate(id, Name); Vote(CandidateId)`
标准:
```sql
SELECT TOP 1 c.Name FROM Candidate c JOIN Vote v ON c.id = v.CandidateId GROUP BY c.id, c.Name ORDER BY COUNT(*) DESC;
```
学生:
```sql
SELECT TOP 1 c.Name FROM Candidate c JOIN Vote v ON c.id <> v.CandidateId GROUP BY c.id, c.Name ORDER BY COUNT(*) DESC;
```
- 结构判定：`支持`
- 造数状态：`TACTIC_BUT_NO_COUNTEREXAMPLE`
- diff_types：`['join_on_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### 不支持样例
- 数据集：`online_random250` / 结构：`JOIN ON` / ID：`online_random250_c88e5813f15dce3f8a2f`
- 来源：LeetCode SQL Summary <https://github.com/siqichen-usc/LeetCode-SQL-Summary/blob/master/Join/Simple-Join/574_Winning_Candidate.sql>
- 表结构：`Candidate(id, Name); Vote(CandidateId)`
标准:
```sql
SELECT TOP 1 c.Name FROM Candidate c JOIN Vote v ON c.id = v.CandidateId GROUP BY c.id, c.Name ORDER BY COUNT(*) DESC;
```
学生:
```sql
SELECT TOP 1 c.Name FROM Candidate c JOIN Vote v ON c.id <> v.CandidateId GROUP BY c.id, c.Name ORDER BY COUNT(*) DESC;
```
- 造数判定：`不支持`
- 造数状态：`TACTIC_BUT_NO_COUNTEREXAMPLE`
- diff_types：`['join_on_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### GROUP BY

### 支持样例
- 数据集：`web_common250` / 结构：`GROUP BY` / ID：`web_group_grain_gap_5`
- 来源：PostgreSQL tutorial: aggregate functions <https://www.postgresql.org/docs/current/tutorial-agg.html>
- 表结构：`courses(dept_id, status)`
标准:
```sql
SELECT dept_id, COUNT(*) FROM courses GROUP BY dept_id;
```
学生:
```sql
SELECT dept_id, status, COUNT(*) FROM courses GROUP BY dept_id, status;
```
- 结构判定：`支持`
- 造数状态：`PASS`
- diff_types：`['projection_changed', 'group_by_changed', 'column_added', 'grouping_grain_too_fine']`
- mutation_summary：`{'executed': 2, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### 不支持样例
- 数据集：`online_random250` / 结构：`GROUP BY` / ID：`online_random250_5c3baa64f2a7a27eaeec`
- 来源：LeetCode SQL Summary <https://github.com/siqichen-usc/LeetCode-SQL-Summary/blob/master/Join/Advanced-Join/1194_Tournament_Winners.sql>
- 表结构：`Matches(first_player, first_score, second_player, second_score); Players(player_id, group_id)`
标准:
```sql
WITH tb1 AS ( SELECT first_player AS player, first_score as score FROM Matches UNION ALL SELECT second_player, second_score FROM Matches ), tb2 AS ( SELECT p.player_id, p.group_id, SUM(tb1.score) AS tp FROM Players p LEFT JOIN tb1 ON p.player_id = tb1.player GROUP BY p.player_id, p.group_id ) SELECT group_id, player_id FROM ( SELECT player_id, group_id, ROW_NUMBER() OVER (PARTITION BY group_id ORDER BY tp DESC, player_id) AS r FROM tb2 ) tb3 WHERE r = 1;
```
学生:
```sql
WITH tb1 AS (SELECT first_player AS player, first_score AS score FROM Matches UNION ALL SELECT second_player, second_score FROM Matches), tb2 AS (SELECT p.player_id, p.group_id, SUM(tb1.score) AS tp FROM Players AS p LEFT JOIN tb1 ON p.player_id = tb1.player GROUP BY p.player_id) SELECT group_id, player_id FROM (SELECT player_id, group_id, ROW_NUMBER() OVER (PARTITION BY group_id ORDER BY tp DESC, player_id) AS r FROM tb2) AS tb3 WHERE r = 1
```
- 结构判定：`不支持`
- 造数状态：`MISSED_COUNTEREXAMPLE`
- diff_types：`[]`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### HAVING

### 支持样例
- 数据集：`web_common250` / 结构：`HAVING` / ID：`web_having_where_gap_5`
- 来源：PostgreSQL tutorial: aggregate functions <https://www.postgresql.org/docs/current/tutorial-agg.html>
- 表结构：`courses(dept_id, credits)`
标准:
```sql
SELECT dept_id, AVG(credits) FROM courses GROUP BY dept_id HAVING AVG(credits) > 50;
```
学生:
```sql
SELECT dept_id, AVG(credits) FROM courses WHERE AVG(credits) > 50 GROUP BY dept_id;
```
- 结构判定：`支持`
- 造数状态：`PASS`
- diff_types：`['where_changed', 'having_changed', 'aggregate_condition_in_where']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 0, 'remove_kept_correct': 0}`

### 不支持样例
- 数据集：`online_random250` / 结构：`HAVING` / ID：`online_random250_28cd51b62234bc6b4bb2`
- 来源：w3resource SQL Exercises mirror <https://github.com/tweichle/w3resource-SQL-Exercises/blob/master/SQL%20Exercises%20-%20JOINS%20on%20Sales%20Database.sql>
- 表结构：`emp_department(dpt_code, dpt_name); emp_details(emp_dept, emp_idno)`
标准:
```sql
SELECT edep.dpt_name, COUNT(edet.emp_idno) FROM emp_details edet INNER JOIN emp_department edep ON edet.emp_dept = edep.dpt_code GROUP BY edep.dpt_name HAVING COUNT(edet.emp_idno) > 2;
```
学生:
```sql
SELECT edep.dpt_name, COUNT(edet.emp_idno) FROM emp_details edet INNER JOIN emp_department edep ON edet.emp_dept = edep.dpt_code GROUP BY edep.dpt_name HAVING COUNT(edet.emp_idno) >= 2;
```
- 造数判定：`不支持`
- 造数状态：`TACTIC_BUT_NO_COUNTEREXAMPLE`
- diff_types：`['having_changed', 'comparison_operator_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 1}`

### Aggregate

### 支持样例
- 数据集：`online_random250` / 结构：`Aggregate` / ID：`online_random250_ca6b9abbe5d8093d66a8`
- 来源：LeetCode SQL Summary <https://github.com/siqichen-usc/LeetCode-SQL-Summary/blob/master/Questions_by_ID/1113_Reported_Posts.sql>
- 表结构：`Actions(post_id, Action_date, action, extra)`
标准:
```sql
SELECT extra AS report_reason, COUNT(DISTINCT post_id) AS report_count FROM Actions WHERE Action_date = @d AND action = 'report' GROUP BY extra;
```
学生:
```sql
SELECT extra AS report_reason, SUM(DISTINCT post_id) AS report_count FROM Actions WHERE Action_date = @d AND action = 'report' GROUP BY extra;
```
- 结构判定：`支持`
- 造数状态：`PASS`
- diff_types：`['projection_changed', 'column_dropped', 'column_added', 'aggregate_function_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### 不支持样例
- 数据集：`online_random250` / 结构：`Aggregate` / ID：`online_random250_9bc84576a535ade427f9`
- 来源：SQL-Exercises <https://github.com/amirai31/SQL-Exercises/blob/main/SQL_Window%20functions%20and%20CTEs.sql>
- 表结构：`product_inventory(product_id, DAY, quantity, transaction_date)`
标准:
```sql
WITH CumulativeQuantityLast90Days AS ( SELECT product_id, SUM(quantity) AS total_quantity FROM product_inventory WHERE transaction_date >= DATEADD(DAY, -90, GETDATE()) GROUP BY product_id ) SELECT product_id, total_quantity FROM CumulativeQuantityLast90Days WHERE total_quantity = (SELECT MAX(total_quantity) FROM CumulativeQuantityLast90Days);
```
学生:
```sql
WITH CumulativeQuantityLast90Days AS ( SELECT product_id, AVG(quantity) AS total_quantity FROM product_inventory WHERE transaction_date >= DATEADD(DAY, -90, GETDATE()) GROUP BY product_id ) SELECT product_id, total_quantity FROM CumulativeQuantityLast90Days WHERE total_quantity = (SELECT MAX(total_quantity) FROM CumulativeQuantityLast90Days);
```
- 结构判定：`不支持`
- 造数状态：`MISSED_COUNTEREXAMPLE`
- diff_types：`[]`
- mutation_summary：`{'executed': 0, 'fixed_by_replacement': 0, 'remove_kept_correct': 0}`

### ORDER BY

### 支持样例
- 数据集：`online_random250` / 结构：`ORDER BY` / ID：`online_random250_4d1f1cd70d3fc8d167d2`
- 来源：Advanced SQL Practice MySQL <https://github.com/santoshkhatri9860/advanced-sql-practice-mysql/blob/main/archive/00_original_everything.sql>
- 表结构：`employees(dept_id, emp_id, emp_name, salary)`
标准:
```sql
with mid_range_salaries as ( select emp_id, emp_name, dept_id, salary from employees where (salary between 4000 and 5000) and (dept_id in (1,4)) ) select * from mid_range_salaries order by dept_id asc, salary desc;
```
学生:
```sql
with mid_range_salaries as ( select emp_id, emp_name, dept_id, salary from employees where (salary between 4000 and 5000) and (dept_id in (1,4)) ) select * from mid_range_salaries order by dept_id asc, salary ASC;
```
- 结构判定：`支持`
- 造数状态：`TACTIC_BUT_NO_COUNTEREXAMPLE`
- diff_types：`['order_by_changed', 'order_direction_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 1}`

### 不支持样例
- 数据集：`online_random250` / 结构：`ORDER BY` / ID：`online_random250_5251f121fb72befb8dd2`
- 来源：SQL-Exercises <https://github.com/amirai31/SQL-Exercises/blob/main/SQL_Window%20functions%20and%20CTEs.sql>
- 表结构：`student_scores(student_id, score, subject)`
标准:
```sql
WITH RankedScores AS ( SELECT student_id, subject, score, RANK() OVER (PARTITION BY subject ORDER BY score DESC) AS ranks FROM student_scores ) SELECT student_id, subject, score FROM RankedScores WHERE ranks = 1;
```
学生:
```sql
WITH RankedScores AS ( SELECT student_id, subject, score, RANK() OVER (PARTITION BY subject ORDER BY score ASC) AS ranks FROM student_scores ) SELECT student_id, subject, score FROM RankedScores WHERE ranks = 1;
```
- 结构判定：`不支持`
- 造数状态：`PASS`
- diff_types：`[]`
- mutation_summary：`{'executed': 2, 'fixed_by_replacement': 2, 'remove_kept_correct': 0}`

### LIMIT / OFFSET

### 支持样例
- 数据集：`online_random250` / 结构：`LIMIT / OFFSET` / ID：`online_random250_2b0e66f6c82d800ea7c2`
- 来源：SQL-Exercises <https://github.com/amirai31/SQL-Exercises/blob/main/SQL%20-%20Group%20BY%2C%20HAVING%2C%20ORDER%20BY.sql>
- 表结构：`Employees(AvgSalary, Department, Salary)`
标准:
```sql
SELECT Department, AVG(Salary) AS AvgSalary FROM Employees GROUP BY Department ORDER BY AvgSalary LIMIT 1;
```
学生:
```sql
SELECT Department, AVG(Salary) AS AvgSalary FROM Employees GROUP BY Department ORDER BY AvgSalary LIMIT 2;
```
- 结构判定：`支持`
- 造数状态：`PASS`
- diff_types：`['limit_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### 不支持样例
- 数据集：`online_random250` / 结构：`LIMIT / OFFSET` / ID：`online_random250_a831f051a8e229896650`
- 来源：LeetCode SQL Summary <https://github.com/siqichen-usc/LeetCode-SQL-Summary/blob/master/Questions_by_ID/1341_Movie_Rating.sql>
- 表结构：`Movie_Rating(movie_id, user_id, created_at, rating); Movies(movie_id, title); Users(user_id, name)`
标准:
```sql
SELECT MIN(name) AS results FROM Users WHERE user_id IN ( SELECT TOP 1 WITH TIES user_id FROM Movie_Rating GROUP BY user_id ORDER BY COUNT(DISTINCT movie_id) DESC ) UNION ALL SELECT MIN(title) AS results FROM Movies WHERE movie_id IN ( SELECT TOP 1 WITH TIES movie_id FROM Movie_Rating WHERE YEAR(created_at) = 2020 AND MONTH(created_at) = 2 GROUP BY movie_id ORDER BY AVG(rating*1.0) DESC );
```
学生:
```sql
SELECT MIN(name) AS results FROM Users WHERE user_id IN ( SELECT TOP 2 WITH TIES user_id FROM Movie_Rating GROUP BY user_id ORDER BY COUNT(DISTINCT movie_id) DESC ) UNION ALL SELECT MIN(title) AS results FROM Movies WHERE movie_id IN ( SELECT TOP 1 WITH TIES movie_id FROM Movie_Rating WHERE YEAR(created_at) = 2020 AND MONTH(created_at) = 2 GROUP BY movie_id ORDER BY AVG(rating*1.0) DESC );
```
- 造数判定：`不支持`
- 造数状态：`TACTIC_BUT_NO_COUNTEREXAMPLE`
- diff_types：`['where_changed', 'limit_changed', 'limit_changed']`
- mutation_summary：`{'executed': 3, 'fixed_by_replacement': 3, 'remove_kept_correct': 2}`

### Subquery

### 支持样例
- 数据集：`online_random250` / 结构：`Subquery` / ID：`online_random250_fb5d1a3bbd6ad10185e4`
- 来源：LeetCode SQL Summary <https://github.com/siqichen-usc/LeetCode-SQL-Summary/blob/master/Questions_by_ID/1270_All_People_Report_to_the_Given_Manager.sql>
- 表结构：`Employees(employee_id, manager_id)`
标准:
```sql
With tb1 AS ( SELECT employee_id FROM Employees WHERE manager_id = 1 AND employee_id != manager_id ), tb2 AS ( SELECT employee_id FROM Employees WHERE manager_id IN (SELECT * FROM tb1) ), tb3 AS ( SELECT employee_id FROM Employees WHERE manager_id IN (SELECT * FROM tb2) ) SELECT * FROM tb1 UNION SELECT * FROM tb2 UNION SELECT * FROM tb3;
```
学生:
```sql
With tb1 AS ( SELECT employee_id FROM Employees WHERE manager_id = 1 AND employee_id = manager_id ), tb2 AS ( SELECT employee_id FROM Employees WHERE manager_id IN (SELECT * FROM tb1) ), tb3 AS ( SELECT employee_id FROM Employees WHERE manager_id IN (SELECT * FROM tb2) ) SELECT * FROM tb1 UNION SELECT * FROM tb2 UNION SELECT * FROM tb3;
```
- 结构判定：`支持`
- 造数状态：`PASS`
- diff_types：`['where_changed', 'function_argument_changed', 'comparison_operator_changed', 'cte_changed']`
- mutation_summary：`{'executed': 2, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### 不支持样例
- 数据集：`online_random250` / 结构：`Subquery` / ID：`online_random250_671d8505bde0c3f597f7`
- 来源：LeetCode SQL Summary <https://github.com/siqichen-usc/LeetCode-SQL-Summary/blob/master/Join/Simple-Join/1364_Number_of_Trusted_Contacts_of_a_Customer.sql>
- 表结构：`Contacts(user_id, contact_email, contact_name); Customers(customer_id, customer_name, email); Invoices(invoice_id, customer_id, user_id, customer_name, price)`
标准:
```sql
WITH tb1 AS ( SELECT c.customer_id, c.customer_name, COUNT(con.contact_name) AS contacts_cnt, COUNT(c2.customer_id) AS trusted_contacts_cnt FROM Customers c LEFT JOIN Contacts con ON c.customer_id = con.user_id LEFT JOIN Customers c2 ON con.contact_email = c2.email GROUP BY c.customer_id, c.customer_name ) SELECT i.invoice_id, tb1.customer_name, i.price, tb1.contacts_cnt, tb1.trusted_contacts_cnt FROM Invoices i JOIN tb1 ON i.user_id = tb1.customer_id ORDER BY i.invoice_id;
```
学生:
```sql
WITH tb1 AS ( SELECT c.customer_id, c.customer_name, COUNT(con.contact_name) AS contacts_cnt, COUNT(c2.customer_id) AS trusted_contacts_cnt FROM Customers c LEFT JOIN Contacts con ON c.customer_id <> con.user_id LEFT JOIN Customers c2 ON con.contact_email = c2.email GROUP BY c.customer_id, c.customer_name ) SELECT i.invoice_id, tb1.customer_name, i.price, tb1.contacts_cnt, tb1.trusted_contacts_cnt FROM Invoices i JOIN tb1 ON i.user_id = tb1.customer_id ORDER BY i.invoice_id;
```
- 造数判定：`不支持`
- 造数状态：`TACTIC_BUT_NO_COUNTEREXAMPLE`
- diff_types：`['join_on_changed', 'cte_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### Correlated Subquery

### 支持样例
- 数据集：`online_random250` / 结构：`Correlated Subquery` / ID：`online_random250_1e0a4ddb8e0aec06065a`
- 来源：w3resource SQL Exercises mirror <https://github.com/tweichle/w3resource-SQL-Exercises/blob/master/SQL%20Exercises%20-%20SUBQUERIES%20on%20Sales%20Database.sql>
- 表结构：`orders(ord_date, purch_amt)`
标准:
```sql
SELECT ord_date, SUM(purch_amt) FROM orders a GROUP BY ord_date HAVING SUM(purch_amt) > (SELECT MAX(purch_amt) + 1000 FROM orders b WHERE a.ord_date = b.ord_date);
```
学生:
```sql
SELECT ord_date, SUM(purch_amt) FROM orders a GROUP BY ord_date HAVING SUM(purch_amt) >= (SELECT MAX(purch_amt) + 1000 FROM orders b WHERE a.ord_date = b.ord_date);
```
- 结构判定：`支持`
- 造数状态：`TACTIC_BUT_NO_COUNTEREXAMPLE`
- diff_types：`['having_changed', 'correlated_predicate_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### 不支持样例
- 数据集：`online_random250` / 结构：`Correlated Subquery` / ID：`online_random250_1e0a4ddb8e0aec06065a`
- 来源：w3resource SQL Exercises mirror <https://github.com/tweichle/w3resource-SQL-Exercises/blob/master/SQL%20Exercises%20-%20SUBQUERIES%20on%20Sales%20Database.sql>
- 表结构：`orders(ord_date, purch_amt)`
标准:
```sql
SELECT ord_date, SUM(purch_amt) FROM orders a GROUP BY ord_date HAVING SUM(purch_amt) > (SELECT MAX(purch_amt) + 1000 FROM orders b WHERE a.ord_date = b.ord_date);
```
学生:
```sql
SELECT ord_date, SUM(purch_amt) FROM orders a GROUP BY ord_date HAVING SUM(purch_amt) >= (SELECT MAX(purch_amt) + 1000 FROM orders b WHERE a.ord_date = b.ord_date);
```
- 造数判定：`不支持`
- 造数状态：`TACTIC_BUT_NO_COUNTEREXAMPLE`
- diff_types：`['having_changed', 'correlated_predicate_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### CTE

### 支持样例
- 数据集：`web_common250` / 结构：`CTE` / ID：`web_cte_predicate_1`
- 来源：PostgreSQL docs: WITH queries <https://www.postgresql.org/docs/current/queries-with.html>
- 表结构：`students(gpa, name)`
标准:
```sql
WITH high_value AS (SELECT name, gpa FROM students WHERE gpa > 10) SELECT name FROM high_value;
```
学生:
```sql
WITH high_value AS (SELECT name, gpa FROM students WHERE gpa > 20) SELECT name FROM high_value;
```
- 结构判定：`支持`
- 造数状态：`PASS`
- diff_types：`['where_changed', 'literal_changed', 'cte_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 1}`

### 不支持样例
- 数据集：`online_random250` / 结构：`CTE` / ID：`online_random250_22661f2fc71cac5a9471`
- 来源：LeetCode SQL Summary <https://github.com/siqichen-usc/LeetCode-SQL-Summary/blob/master/CASE-WHEN/1264_Page_Recommendations.sql>
- 表结构：`Friendship(user1_id, user2_id); Likes(page_id, user_id)`
标准:
```sql
WITH f AS ( SELECT CASE WHEN user1_id = 1 THEN user2_id ELSE user1_id END AS fid FROM Friendship WHERE user1_id = 1 OR user2_id =1 ) SELECT DISTINCT page_id AS recommended_page FROM Likes WHERE user_id IN (SELECT * FROM f) EXCEPT SELECT page_id FROM Likes WHERE user_id = 1;
```
学生:
```sql
WITH f AS ( SELECT CASE WHEN user1_id <> 1 THEN user2_id ELSE user1_id END AS fid FROM Friendship WHERE user1_id = 1 OR user2_id =1 ) SELECT DISTINCT page_id AS recommended_page FROM Likes WHERE user_id IN (SELECT * FROM f) EXCEPT SELECT page_id FROM Likes WHERE user_id = 1;
```
- 结构判定：`不支持`
- 造数状态：`PASS`
- diff_types：`[]`
- mutation_summary：`{'executed': 2, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### Recursive CTE

### 支持样例
- 数据集：`online_random250` / 结构：`Recursive CTE` / ID：`online_random250_ca725d1571b4e1baebaa`
- 来源：Advanced SQL Practice MySQL <https://github.com/santoshkhatri9860/advanced-sql-practice-mysql/blob/main/solutions/10_recursive_cte_numbers_solutions.sql>
- 表结构：`未记录`
标准:
```sql
with recursive numbers as ( select 1 as num union all select num+1 from numbers where num < 10 ) select num from numbers;
```
学生:
```sql
with recursive numbers as ( select 2 as num union all select num+1 from numbers where num < 10 ) select num from numbers;
```
- 结构判定：`支持`
- 造数状态：`PASS`
- diff_types：`['recursive_cte_changed']`
- mutation_summary：`{'executed': 0, 'fixed_by_replacement': 0, 'remove_kept_correct': 0}`

### 不支持样例
- 数据集：`online_random250` / 结构：`Recursive CTE` / ID：`online_random250_e27c9bf972f4a80a3979`
- 来源：Advanced SQL Practice MySQL <https://github.com/santoshkhatri9860/advanced-sql-practice-mysql/blob/main/archive/00_original_everything.sql>
- 表结构：`employees(emp_id, manager_id, emp_name)`
标准:
```sql
with recursive managing_levels as ( select 1 as level, emp_id, emp_name, manager_id from employees where manager_id is null union all select level+1 as level, e.emp_id, e.emp_name, e.manager_id from employees e join managing_levels m on e.manager_id = m.emp_id ) select level, emp_id, emp_name, manager_id from managing_levels where level >= 3;
```
学生:
```sql
with recursive managing_levels as ( select 2 as level, emp_id, emp_name, manager_id from employees where manager_id is null union all select level+1 as level, e.emp_id, e.emp_name, e.manager_id from employees e join managing_levels m on e.manager_id = m.emp_id ) select level, emp_id, emp_name, manager_id from managing_levels where level >= 3;
```
- 结构判定：`不支持`
- 造数状态：`PASS`
- diff_types：`[]`
- mutation_summary：`{'executed': 0, 'fixed_by_replacement': 0, 'remove_kept_correct': 0}`

### Set Operation

### 支持样例
- 数据集：`web_common250` / 结构：`Set Operation` / ID：`web_set_all_gap_1`
- 来源：PostgreSQL docs: UNION, CASE, and SELECT reference topics <https://www.postgresql.org/docs/current/queries-union.html>
- 表结构：`students(name)`
标准:
```sql
SELECT name FROM students UNION ALL SELECT name FROM students;
```
学生:
```sql
SELECT name FROM students UNION SELECT name FROM students;
```
- 结构判定：`支持`
- 造数状态：`PASS`
- diff_types：`['set_operator_changed', 'set_modifier_changed', 'set_all_modifier_changed']`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### 不支持样例
- 数据集：`web_common250` / 结构：`Set Operation` / ID：`web_set_operator_6`
- 来源：PostgreSQL docs: UNION, CASE, and SELECT reference topics <https://www.postgresql.org/docs/current/queries-union.html>
- 表结构：`courses(course_name, credits)`
标准:
```sql
SELECT course_name FROM courses WHERE credits > 60 UNION SELECT course_name FROM courses WHERE credits < 6;
```
学生:
```sql
SELECT course_name FROM courses WHERE credits > 60 INTERSECT SELECT course_name FROM courses WHERE credits < 6;
```
- 结构判定：`不支持`
- 造数状态：`PASS`
- diff_types：`[]`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### CASE

### 支持样例
- 数据集：`web_common250` / 结构：`CASE` / ID：`web_case_changed_1`
- 来源：SQLTutorial CASE expression style <https://www.sqltutorial.org/sql-case/>
- 表结构：`students(gpa, name)`
标准:
```sql
SELECT name, CASE WHEN gpa >= 10 THEN 'high' ELSE 'low' END AS band FROM students;
```
学生:
```sql
SELECT name, CASE WHEN gpa > 10 THEN 'high' ELSE 'low' END AS band FROM students;
```
- 结构判定：`支持`
- 造数状态：`PASS`
- diff_types：`['projection_changed', 'column_dropped', 'column_added', 'function_argument_changed', 'function_argument_changed', 'comparison_operator_changed', 'case_changed']`
- mutation_summary：`{'executed': 2, 'fixed_by_replacement': 2, 'remove_kept_correct': 0}`

### 不支持样例
- 数据集：`online_random250` / 结构：`CASE` / ID：`online_random250_5a9ca9d97b28d353ae45`
- 来源：w3resource SQL Exercises mirror <https://github.com/tweichle/w3resource-SQL-Exercises/blob/master/SQL%20Exercises%20-%20SUBQUERIES%20on%20HR%20Database.sql>
- 表结构：`employees(employee_id, first_name, last_name, salary)`
标准:
```sql
SELECT employee_id, first_name, last_name, salary AS salary_drawn, ROUND(salary - (SELECT AVG(salary) FROM employees), 2) AS avg_compare, CASE WHEN salary >= (SELECT AVG(salary) FROM employees) THEN 'HIGH' ELSE 'LOW' END AS salary_status FROM employees;
```
学生:
```sql
SELECT employee_id, first_name, last_name, salary AS salary_drawn, ROUND(salary - (SELECT AVG(salary) FROM employees), 2) AS avg_compare, CASE WHEN salary > (SELECT AVG(salary) FROM employees) THEN 'HIGH' ELSE 'LOW' END AS salary_status FROM employees;
```
- 造数判定：`不支持`
- 造数状态：`TACTIC_BUT_NO_COUNTEREXAMPLE`
- diff_types：`['projection_changed', 'column_dropped', 'column_added', 'function_argument_changed', 'function_argument_changed', 'comparison_operator_changed', 'case_changed']`
- mutation_summary：`{'executed': 2, 'fixed_by_replacement': 2, 'remove_kept_correct': 0}`

### Window

### 支持样例
- 数据集：`web_common250` / 结构：`Window` / ID：`web_window_partition_6`
- 来源：PostgreSQL tutorial: window functions <https://www.postgresql.org/docs/current/tutorial-window.html>
- 表结构：`courses(dept_id, course_name, credits)`
标准:
```sql
SELECT course_name, ROW_NUMBER() OVER (PARTITION BY dept_id ORDER BY credits DESC) AS rn FROM courses;
```
学生:
```sql
SELECT course_name, ROW_NUMBER() OVER (ORDER BY credits DESC) AS rn FROM courses;
```
- 结构判定：`支持`
- 造数状态：`PASS`
- diff_types：`['projection_changed', 'column_dropped', 'column_added', 'window_over_changed']`
- mutation_summary：`{'executed': 2, 'fixed_by_replacement': 2, 'remove_kept_correct': 0}`

### 不支持样例
- 数据集：`online_random250` / 结构：`Window` / ID：`online_random250_41bdf22cc750f04e74f5`
- 来源：SQL-Exercises <https://github.com/amirai31/SQL-Exercises/blob/main/SQL_Window%20functions%20and%20CTEs.sql>
- 表结构：`sales(order_date, region, revenue)`
标准:
```sql
WITH RankedSales AS ( SELECT region, order_date, revenue, ROW_NUMBER() OVER (PARTITION BY region ORDER BY revenue DESC) AS row_num FROM sales ) SELECT region, order_date, revenue FROM RankedSales WHERE row_num <= 3 ORDER BY region, row_num;
```
学生:
```sql
WITH RankedSales AS ( SELECT region, order_date, revenue, ROW_NUMBER() OVER ( ORDER BY revenue DESC) AS row_num FROM sales ) SELECT region, order_date, revenue FROM RankedSales WHERE row_num <= 3 ORDER BY region, row_num;
```
- 结构判定：`不支持`
- 造数状态：`PASS`
- diff_types：`[]`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### Dialect Boundary

### 支持样例
- 数据集：`web_common250` / 结构：`Dialect Boundary` / ID：`web_dialect_fetch_limit_3`
- 来源：W3Schools SELECT TOP / LIMIT style <https://www.w3schools.com/sql/sql_top.asp>
- 表结构：`products(price, product_name)`
标准:
```sql
SELECT product_name FROM products ORDER BY price DESC FETCH FIRST 5 ROWS ONLY;
```
学生:
```sql
SELECT product_name FROM products ORDER BY price DESC LIMIT 5;
```
- 结构判定：`支持`
- 造数状态：`MISSED_COUNTEREXAMPLE`
- diff_types：`[]`
- mutation_summary：`{'executed': 1, 'fixed_by_replacement': 1, 'remove_kept_correct': 0}`

### 不支持样例
- 数据集：`online_random250` / 结构：`Dialect Boundary` / ID：`online_random250_1d8bb59ec3178e62a99a`
- 来源：PostgreSQL WITH documentation <https://www.postgresql.org/docs/current/queries-with.html>
- 表结构：`orders(amount, product, quantity, region)`
标准:
```sql
WITH regional_sales AS ( SELECT region, SUM(amount) AS total_sales FROM orders GROUP BY region ), top_regions AS ( SELECT region FROM regional_sales WHERE total_sales > (SELECT SUM(total_sales)/10 FROM regional_sales) ) SELECT region, product, SUM(quantity) AS product_units, SUM(amount) AS product_sales FROM orders WHERE region IN (SELECT region FROM top_regions) GROUP BY region, product;
```
学生:
```sql
WITH regional_sales AS ( SELECT region, SUM(amount) AS total_sales FROM orders GROUP BY region ), top_regions AS ( SELECT region FROM regional_sales WHERE total_sales > (SELECT SUM(total_sales)/11 FROM regional_sales) ) SELECT region, product, SUM(quantity) AS product_units, SUM(amount) AS product_sales FROM orders WHERE region IN (SELECT region FROM top_regions) GROUP BY region, product;
```
- 结构判定：`不支持`
- 造数状态：`MISSED_COUNTEREXAMPLE`
- diff_types：`[]`
- mutation_summary：`{'executed': 0, 'fixed_by_replacement': 0, 'remove_kept_correct': 0}`

## 当前结论

- 无方言组结构通过 244/250，主要失败仍集中在复杂等价或 strict target 过细场景。
- 有方言组结构通过 226/250，方言解析边界仍会导致 ASTDiff 缺失。
- 本文样例均来自本轮实际 case，并保留完整标准 SQL 与学生 SQL。
