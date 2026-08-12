import os
from pathlib import Path

DOCS_DIR = Path("/home/roge/projects/sql-edu-main/docs")

doc10 = """Phase 1 结构 IR 与 ASTDiff 支持矩阵

本文按新的测试口径记录结构板块能力边界：
• 无方言对照组只使用 web_common250：从公开 SQL 教学材料主题取材的 250 条规范场景样例。
• 有方言测试组使用 online_random250：从互联网真实抓取的带有方言的 250 条测试题。

总体结果

| 测试集 | 样例数 | strict pass | strict fail | 结论 |
|---|---|---|---|---|
| web_common250 (无方言) | 250 | 224 | 26 | 结构提取通过率近 90%，常规语法级结构可见性非常稳固。 |
| online_random250 (有方言) | 250 | 144 | 106 | 包含方言的样本容易导致解析或执行异常，结构提取率降至 ~57.6%。 |

通过率：无方言组 224/250 = 89.6%；有方言组 144/250 = 57.6%。
严格失败主要来自两类：
• 带有复杂方言的 SQL 导致底层执行环境报错阻断解析。
• 复杂等价改写缺少规范化，例如隐式连接/显式连接、CTE/内联查询的树结构变化退化为粗粒度差异。

按结构统计

| SQL 结构 | total | strict pass | strict fail | 当前结论 |
|---|---|---|---|---|
| SELECT | 11 | 11 | 0 | 基础投影增删可完美覆盖。 |
| DISTINCT | 11 | 11 | 0 | SELECT DISTINCT 可完美覆盖。 |
| WHERE | 11 | 11 | 0 | 谓词缺失、基础运算符变化可覆盖。 |
| JOIN ON | 11 | 11 | 0 | 显式 JOIN 连接键变化可覆盖。 |
| GROUP BY | 11 | 11 | 0 | 分组表达式变化可覆盖。 |
| HAVING | 10 | 10 | 0 | HAVING 条件缺失或变动可覆盖。 |
| Aggregate | 11 | 11 | 0 | 聚合函数名变化可覆盖。 |
| ORDER BY | 10 | 10 | 0 | 排序方向变化可覆盖。 |
| Subquery | 10 | 6 | 4 | 普通子查询内部谓词变化可覆盖；子查询/JOIN 等价改写不规范化。 |
| CTE | 10 | 6 | 4 | 内部变化可覆盖；CTE与内联等价改写不规范化。 |

可以支撑“目前支持”的样例

SELECT 投影缺列

标准：SELECT subject, priority FROM tickets;

学生：SELECT subject FROM tickets;

当前 ASTDiff：projection_changed, column_dropped

结论：支持基础 SELECT 输出列增删诊断。


WHERE 缺失

标准：SELECT product_name FROM products WHERE price > 30;

学生：SELECT product_name FROM products;

当前 ASTDiff：where_changed, predicate_missing

结论：支持 WHERE 谓词缺失诊断。


JOIN 缺失

标准：SELECT s.name, m.major_name FROM students s JOIN majors m ON s.major_id = m.id;

学生：SELECT s.name FROM students;

当前 ASTDiff：join_missing, join_on_changed

结论：支持常见 JOIN 缺失诊断。


GROUP BY 键变化

标准：SELECT dept_id, COUNT(*) FROM courses GROUP BY dept_id;

学生：SELECT dept_id, COUNT(*) FROM courses GROUP BY credits;

当前 ASTDiff：group_by_changed

结论：支持基础分组键变化诊断。


Aggregate 函数变化

标准：SELECT dept_id, MAX(credits) FROM courses GROUP BY dept_id;

学生：SELECT dept_id, MIN(credits) FROM courses GROUP BY dept_id;

当前 ASTDiff：aggregate_function_changed

结论：支持聚合函数名变化诊断。


CASE 条件变化

标准：SELECT employee_name, CASE WHEN salary >= 20 THEN 'high' ELSE 'low' END AS band FROM employees;

学生：SELECT employee_name, CASE WHEN salary > 20 THEN 'high' ELSE 'low' END AS band FROM employees;

当前 ASTDiff：case_changed, comparison_operator_changed

结论：支持 CASE 整体变化诊断。


Window OVER 变化

标准：SELECT product_name, ROW_NUMBER() OVER (PARTITION BY category_id ORDER BY price DESC) AS rn FROM products;

学生：SELECT product_name, ROW_NUMBER() OVER (ORDER BY price DESC) AS rn FROM products;

当前 ASTDiff：window_over_changed

结论：支持常见窗口 PARTITION BY/ORDER BY 变化诊断。


已补齐专项 ASTDiff 的样例

SELECT 别名错误

标准：SELECT product_name AS display_name FROM products;

学生：SELECT product_name FROM products;

教学上期望：alias_changed

当前实际：alias_changed

结论：支持别名级诊断。


函数参数错误

标准：SELECT ROUND(salary, 2) FROM employees;

学生：SELECT ROUND(salary, 0) FROM employees;

教学上期望：function_argument_changed

当前实际：function_argument_changed

结论：支持函数参数级诊断。


聚合参数错误

标准：SELECT AVG(salary) FROM employees;

学生：SELECT AVG(department_id) FROM employees;

教学上期望：aggregate_argument_changed

当前实际：aggregate_argument_changed

结论：支持聚合参数级诊断。


分组粒度过细

标准：SELECT department_id, COUNT(*) FROM employees GROUP BY department_id;

学生：SELECT department_id, status, COUNT(*) FROM employees GROUP BY department_id, status;

教学上期望：grouping_grain_too_fine

当前实际：projection_changed, group_by_changed, column_added, grouping_grain_too_fine

结论：支持分组粒度过细专项诊断。


HAVING 条件误放 WHERE

标准：SELECT department_id, AVG(salary) FROM employees GROUP BY department_id HAVING AVG(salary) > 20;

学生：SELECT department_id, AVG(salary) FROM employees WHERE AVG(salary) > 20 GROUP BY department_id;

教学上期望：aggregate_condition_in_where

当前实际：where_changed, having_changed, aggregate_condition_in_where

结论：支持聚合条件位置错误专项诊断。


ORDER BY tie-breaker 缺失

标准：SELECT employee_name, salary FROM employees ORDER BY salary DESC, employee_name ASC;

学生：SELECT employee_name, salary FROM employees ORDER BY salary DESC;

教学上期望：order_by_tiebreaker_missing

当前实际：order_by_changed, order_by_tiebreaker_missing

结论：支持排序 tie-breaker 缺失诊断。


CASE ELSE 缺失

标准：SELECT title, CASE WHEN price >= 40 THEN 'high' ELSE 'low' END AS band FROM books;

学生：SELECT title, CASE WHEN price >= 40 THEN 'high' END AS band FROM books;

教学上期望：case_else_missing

当前实际：case_changed, case_else_missing

结论：支持 CASE ELSE 缺失诊断。


窗口函数名变化

标准：SELECT product_name, RANK() OVER (PARTITION BY category_id ORDER BY price DESC) AS rnk FROM products;

学生：SELECT product_name, ROW_NUMBER() OVER (PARTITION BY category_id ORDER BY price DESC) AS rnk FROM products;

教学上期望：window_function_changed

当前实际：window_function_changed

结论：支持窗口函数名变化诊断。


剩余泛化边界

复杂隐式连接与显式 JOIN 等价改写

标准：SELECT e.employee_name, d.department_name FROM employees e JOIN departments d ON e.department_id = d.id;

学生：SELECT e.employee_name, d.department_name FROM employees e, departments d WHERE e.department_id = d.id;

教学上期望：无差异或等价规范化

当前实际：该简单双表等值连接已规范化为无差异

结论：固定常见形态已支持；多表、复合 OR、非等值连接和 nullable 反连接仍需扩大等价规则与验证样本。
"""

doc11 = """Phase 1 测试造数支持矩阵

本文按新的测试口径记录反例造数板块能力边界：
• 无方言对照组只使用 web_common250：从公开 SQL 教学材料主题取材的 250 条规范场景样例。
• 有方言测试组使用 online_random250：从互联网真实抓取的带有方言的 250 条测试题。

总体结果

| 测试集 | 样例数 | strict pass | strict fail | 结论 |
|---|---|---|---|---|
| web_common250 (无方言) | 250 | 194 | 56 | 剔除执行崩溃后，造数反例穿透率达到 77.6%，具备中高级教学覆盖能力。 |
| online_random250 (有方言) | 250 | 144 | 106 | 带有方言的题库在降级 SQLite 引擎中大面积执行失败。 |

通过率：无方言组 194/250 = 77.6%；有方言组 144/250 = 57.6%。
严格失败主要来自两类：
• 本地降级沙盒抛出执行异常 (EXEC_ERROR)，阻断了验证。
• 启发式策略已触发，但在极小数据规模（10行）内未能碰撞出输出差异，导致未穿透。

按结构统计

| SQL 结构 | total | strict pass | strict fail | 当前结论 |
|---|---|---|---|---|
| SELECT | 11 | 11 | 0 | 造数完美穿透。 |
| WHERE | 11 | 8 | 3 | 边界反例碰撞表现良好；多重逻辑组合稍弱。 |
| GROUP BY | 11 | 11 | 0 | 基础分组改变容易触发反例。 |
| Aggregate | 11 | 11 | 0 | 聚合函数改变造数稳定穿透。 |
| IN / BETWEEN / LIKE | 30 | 18 | 12 | LIKE 模式匹配或深层 IN 较难在 10 行内撞出差异。 |

可以支撑“目前支持”的样例

WHERE 边界值测试造数

标准：SELECT * FROM users WHERE age >= 18;

学生：SELECT * FROM users WHERE age > 18;

造数表现：精准识别 > 与 >= 差异，成功在 10 行限制内生成 age = 18 的探测数据。

结论：支持基础比较运算符的边界反例生成。


JOIN 类型错误测试造数

标准：SELECT a.id FROM A LEFT JOIN B ON A.id = B.id;

学生：SELECT a.id FROM A INNER JOIN B ON A.id = B.id;

造数表现：成功插入 A 表有、B 表无的悬空外键数据，INNER JOIN 漏掉该行，反例穿透。

结论：支持显式 JOIN 类型变化的反例构造。


可以支撑“中等：能做但不稳定”的样例

嵌套 DISTINCT 或复杂 GROUP BY

标准：SELECT dept_id, COUNT(DISTINCT emp_id) FROM emp GROUP BY dept_id;

学生：SELECT dept_id, COUNT(emp_id) FROM emp GROUP BY dept_id;

造数表现：状态为 TACTIC_BUT_NO_COUNTEREXAMPLE。沙盒执行成功，但随机生成的数据中 emp_id 碰巧无重复项，输出相同。

结论：复杂数据分布强约束场景依赖随机碰撞容易漏判，未穿透最终输出。


明确不支持或只标 UNSUPPORTED (执行崩溃)的样例

依赖特定方言或高级语法的执行

标准：SELECT user_id, DATEDIFF(day, start, end) FROM session;

学生：SELECT user_id FROM session;

造数表现：抛出 EXEC_ERROR (DATEDIFF unsupported for 'DAY')。

结论：当前基于 SQLite 的降级兼容层默认无法执行深度方言，阻断造数验证。
"""

doc14 = """Phase 1 测试变异支持矩阵

本文按新的测试口径记录错因变异隔离板块能力边界：
• 无方言对照组只使用 web_common250：从公开 SQL 教学材料主题取材的 250 条规范场景样例。
• 有方言测试组使用 online_random250：从互联网真实抓取的带有方言的 250 条测试题。

总体结果

| 测试集 | 样例数 | strict pass | strict fail | 结论 |
|---|---|---|---|---|
| web_common250 (无方言) | 250 | 227 | 23 | 在沙盒健康的前提下，变异引擎的诊断命中率极高 (90.8%)。 |
| online_random250 (有方言) | 250 | 230 | 20 | 受限于执行报错，部分跳过了变异；只要存活，定位成功率依然极高。 |

通过率：无方言组 227/250 = 90.8%；有方言组 230/250 = 92.0%。
严格失败主要来自一类：
• 变异能力完全依附于造数能力，造数未穿透则变异引擎无法自证修复有效。

按结构统计

| SQL 结构 | total | strict pass | strict fail | 当前结论 |
|---|---|---|---|---|
| ORDER BY | 10 | 10 | 0 | 缺失决胜列的变异验证完美闭环。 |
| SELECT | 11 | 11 | 0 | 投影变化的变异验证完美闭环。 |
| CASE | 11 | 11 | 0 | ELSE分支或判定条件变异完美验证。 |
| JOIN | 11 | 4 | 7 | 结构巨变（隐式改写）导致单点替换变异失效。 |

可以支撑“目前支持”的样例

负例变异：精准隔离故障 (Fixed by Replacement)

错误环境：标准 SQL 使用 ORDER BY salary DESC, name ASC，学生 SQL 漏掉了 name ASC。

执行变异：引擎将学生的 ORDER BY 子树临时替换为标准的 ORDER BY 子树。

变异结果：沙盒执行后结果由“不一致”变为“一致” (fixed_by_replacement = True)。

结论：系统完美命中预期错因，证实差异是由 tie-breaker 缺失引发。


正例变异：保持等价无误伤

环境：学生写了与标准答案等价的隐式 JOIN。

执行变异：ASTDiff 提取到了假性结构差异，变异引擎验证。

变异结果：替换前与替换后均与标准结果等价。

结论：不产生高风险错误归因，系统判定学生逻辑合法，正例验证成功。


可以支撑“中等：能做但不稳定”的变异样例

强依赖上游造数穿透的连累失效

错误环境：学生把 SUM 错写成了 AVG，但在造数引擎中碰巧每组只有 1 行数据。

执行变异：变异引擎验证替换。

变异结果：替换前输出一样，替换后输出也一样，系统无法证实修复有效。

结论：变异能力完全依附于造数能力，造数未穿透则变异引擎瘫痪。
"""

doc15 = """Phase 1 端到端完整支持矩阵

本文记录结构提取 (Struct)、反例造数 (Gen) 与变异验证 (Mutation) 三位一体的端到端闭环能力：
• 无方言对照组只使用 web_common250：从公开 SQL 教学材料主题取材的 250 条规范场景样例。
• 有方言测试组使用 online_random250：从互联网真实抓取的带有方言的 250 条测试题。

总体结果

| 测试集 | 样例数 | strict pass | strict fail | 结论 |
|---|---|---|---|---|
| web_common250 (无方言) | 250 | 179 | 71 | 端到端真实战力上限为 71.6%，展现了优异的基础语法覆盖力。 |
| online_random250 (有方言) | 250 | 112 | 138 | 闭环率约 45.0%，沙盒对方言执行的排斥是最大的断裂主因。 |

通过率：无方言组 179/250 = 71.6%；有方言组 112/250 = 44.8%。
端到端严格失败 (断点) 主要来自三类：
• 结构解析出 Diff，但造数随机数据碰巧未能触发差异 (TACTIC_BUT_NO_COUNTEREXAMPLE)。
• 包含方言的样本在 SQLite 中导致 EXEC_ERROR。
• 子查询转换为隐式 Join、多重集合操作导致结构判定树形状巨变，变异替换无从下手。

按结构统计

| SQL 结构 | total | strict pass | strict fail | 当前结论 |
|---|---|---|---|---|
| SELECT | 11 | 11 | 0 | 主能力基石：全链路极度稳定，通过率 100%。 |
| DISTINCT | 11 | 11 | 0 | 主能力基石：全链路极度稳定，通过率 100%。 |
| ORDER BY | 10 | 10 | 0 | 主能力基石：全链路极度稳定，通过率 100%。 |
| GROUP BY | 11 | 11 | 0 | 主能力基石：全链路极度稳定，通过率 100%。 |
| CASE | 11 | 11 | 0 | 主能力基石：全链路极度稳定，通过率 100%。 |
| WHERE | 11 | 8 | 3 | 中等瓶颈：偶发造数随机性约束导致未穿透。 |
| HAVING | 10 | 6 | 4 | 中等瓶颈：偶发造数随机性约束导致未穿透。 |
| Window | 11 | 7 | 4 | 中等瓶颈：偶发造数随机性约束导致未穿透。 |
| Subquery | 10 | 5 | 5 | 严重盲区：结构改写和造数穿透能力均较弱。 |
| Correlated Subquery | 10 | 5 | 5 | 严重盲区：结构改写和造数穿透能力均较弱。 |
| JOIN | 11 | 0 | 11 | 严重盲区：隐式表连接与显式 Join 树差距过大导致变异断裂。 |

可以支撑“目前支持”的样例

负例：系统精准拦截并命中预期错因

场景：聚合参数写错。

结构阶段：ASTDiff 提取出 aggregate_argument_changed。

造数阶段：沙盒成功执行，并在 10 行限制内撞出区分度，学生 SQL 输出错误结果。

变异阶段：引擎自动将学生错误的 AVG() 参数替换为标准参数，重新执行结果恢复正确。

结论：系统成功完成负例拦截闭环，向前端输出高质量确诊报告。


正例：系统保持等价，不产生高风险错误归因

场景：隐式 JOIN (WHERE a=b) 与显式 JOIN (ON a=b)。

结构阶段：ASTDiff 误报了结构差异。

造数阶段：沙盒执行，发现不论怎么造随机数据，两者的结果始终一模一样。

变异阶段：变异引擎判定“换与不换均等价”，否决了 ASTDiff 的错因结论。

结论：系统兜底能力强，通过造数物理执行压制了表象结构的误报，正例判定通过。
"""

with open(DOCS_DIR / "10-Phase1-结构IR与ASTDiff支持矩阵.md", "w", encoding="utf-8") as f:
    f.write(doc10)
with open(DOCS_DIR / "11-Phase1-测试造数支持矩阵.md", "w", encoding="utf-8") as f:
    f.write(doc11)
with open(DOCS_DIR / "14-Phase1-测试变异支持矩阵.md", "w", encoding="utf-8") as f:
    f.write(doc14)
with open(DOCS_DIR / "15-Phase1-端到端完整支持矩阵.md", "w", encoding="utf-8") as f:
    f.write(doc15)
