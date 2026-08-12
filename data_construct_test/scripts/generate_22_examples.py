from pathlib import Path

DOCS_DIR = Path("/home/roge/projects/sql-edu-main/docs")

content = """# Phase 1 全22类结构端到端详尽样例集

本文档针对 22 个 SQL 核心能力板块，分别列出了在当前系统（结构IR、ASTDiff、反例造数、变异验证）中“支持的样例”和“不支持的（失败/未穿透/未对齐的）样例”。排版严格遵循极简文本规范。

【SELECT】

支持的样例：投影缺列

标准：SELECT subject, priority FROM tickets;

学生：SELECT subject FROM tickets;

当前表现：ASTDiff 精准提取 projection_changed；造数与变异完美闭环。

结论：支持基础 SELECT 输出列增删诊断。


不支持的样例：暂无（全链路极度稳定）

标准：无

学生：无

当前表现：无

结论：SELECT 是主能力基石，无明显的结构性不支持场景。


【DISTINCT】

支持的样例：全局 DISTINCT 缺失

标准：SELECT DISTINCT department_id FROM employees;

学生：SELECT department_id FROM employees;

当前表现：提取 distinct_removed，造数引擎随机插入重复 dept_id 成功触发反例。

结论：支持顶层去重逻辑的诊断与造数。


不支持的样例：嵌套 DISTINCT 随机造数未穿透

标准：SELECT dept_id, COUNT(DISTINCT emp_id) FROM emp GROUP BY dept_id;

学生：SELECT dept_id, COUNT(emp_id) FROM emp GROUP BY dept_id;

当前表现：沙盒执行成功，但随机生成的数据中 emp_id 碰巧无重复项，导致结果相同。

结论：数据分布强约束场景极易由于随机碰撞而漏判。


【WHERE】

支持的样例：谓词完全缺失

标准：SELECT product_name FROM products WHERE price > 30;

学生：SELECT product_name FROM products;

当前表现：ASTDiff 提取 predicate_missing，成功造出 price <= 30 的反例数据。

结论：支持 WHERE 缺失诊断。


不支持的样例：复合逻辑短路等价误报

标准：SELECT * FROM users WHERE (age > 18 AND status = 'A') OR status = 'A';

学生：SELECT * FROM users WHERE status = 'A';

当前表现：逻辑等价，但 ASTDiff 报结构差异，且造数在极小规模内偶尔无法验证等价性。

结论：深层逻辑代数化简未做规范化。


【Comparison】

支持的样例：边界运算符错写

标准：SELECT * FROM users WHERE age >= 18;

学生：SELECT * FROM users WHERE age > 18;

当前表现：引擎精准识别，强制生成 age = 18 的探测数据，反例穿透成功。

结论：支持基础比较运算符的边界反例生成。


不支持的样例：字符串 Collation 隐式比较

标准：SELECT * FROM dict WHERE word > 'Apple';

学生：SELECT * FROM dict WHERE word >= 'apple';

当前表现：ASTDiff 捕捉到比较符变化，但 SQLite 沙盒区分大小写逻辑与 MySQL 等方言不同，导致变异验证失效。

结论：跨方言 Collation 与字符集隐式比较规则不受支持。


【NULL】

支持的样例：NULL 比较符错写

标准：SELECT * FROM orders WHERE ship_date IS NULL;

学生：SELECT * FROM orders WHERE ship_date = NULL;

当前表现：ASTDiff 提取 comparison_to_null，造数插入 NULL 数据成功证明后者永远返回空。

结论：支持典型的 IS NULL 诊断。


不支持的样例：三值逻辑陷阱 (NOT IN)

标准：SELECT * FROM a WHERE id NOT IN (SELECT b_id FROM b WHERE b_id IS NOT NULL);

学生：SELECT * FROM a WHERE id NOT IN (SELECT b_id FROM b);

当前表现：当 b 表包含 NULL 时两者行为不同，但造数引擎难以正好在 b 表造出 NULL 并在 a 表造出对应探测行。

结论：三值逻辑的极值碰撞成功率低。


【IN / BETWEEN / LIKE】

支持的样例：IN 列表成员变动

标准：SELECT * FROM logs WHERE level IN ('ERROR', 'FATAL');

学生：SELECT * FROM logs WHERE level IN ('ERROR');

当前表现：ASTDiff 提取 in_list_changed，并强制造出 level='FATAL' 的数据。

结论：支持集合列表成员的增删判定。


不支持的样例：复杂 LIKE 正则模式验证

标准：SELECT * FROM users WHERE name LIKE 'A_%_Z';

学生：SELECT * FROM users WHERE name LIKE 'A%Z';

当前表现：ASTDiff 提取 pattern_changed，但随机字符串生成器极难在 10 行内撞出一个“以 A 开头 Z 结尾且中间至少有一个字符”的精准反例。

结论：纯随机造数无法穿透复杂正则表达式。


【Logic】

支持的样例：AND 与 OR 错用

标准：SELECT * FROM items WHERE price < 10 OR stock = 0;

学生：SELECT * FROM items WHERE price < 10 AND stock = 0;

当前表现：提取 logic_operator_changed，成功生成满足一个条件但不满足另一个的数据。

结论：支持基础布尔逻辑错误诊断。


不支持的样例：长串条件嵌套顺序打乱

标准：SELECT * FROM t WHERE a=1 AND (b=2 OR c=3);

学生：SELECT * FROM t WHERE (c=3 OR b=2) AND a=1;

当前表现：逻辑等价，但由于 AST 树左右子树整体翻转，ASTDiff 误报大量错位。

结论：逻辑表达式的树级重排（Ordering Normalize）支持薄弱。


【JOIN】

支持的样例：JOIN 类型错用

标准：SELECT a.id FROM A LEFT JOIN B ON A.id = B.id;

学生：SELECT a.id FROM A INNER JOIN B ON A.id = B.id;

当前表现：插入悬空外键数据，执行后 INNER JOIN 漏行，反例穿透。

结论：支持显式 JOIN 类型变化的构造。


不支持的样例：隐式 JOIN 与显式 JOIN 树差异

标准：SELECT * FROM A JOIN B ON A.id = B.id;

学生：SELECT * FROM A, B WHERE A.id = B.id;

当前表现：ASTDiff 提取出 FROM 列表变化和 WHERE 新增，彻底丢失了 JOIN 语义，变异引擎无从下手。

结论：隐式表连接未展平规范化，端到端严重断裂。


【JOIN ON】

支持的样例：连接键写错

标准：SELECT * FROM emp e JOIN dept d ON e.dept_id = d.id;

学生：SELECT * FROM emp e JOIN dept d ON e.id = d.id;

当前表现：ASTDiff 提取 join_on_changed，造数引擎生成主键和外键不同的数据成功报错。

结论：支持 JOIN ON 内部条件的精细诊断。


不支持的样例：ON 与 WHERE 过滤下推等价

标准：SELECT * FROM A LEFT JOIN B ON A.id = B.id AND B.status = 1;

学生：SELECT * FROM A LEFT JOIN B ON A.id = B.id WHERE B.status = 1;

当前表现：语义完全不同（外连接中 ON 和 WHERE 作用不同），系统虽然报了错，但难以向学生解释逻辑差异，诊断退化为普通的 WHERE 新增。

结论：外连接条件与过滤条件的深层语义诊断不足。


【GROUP BY】

支持的样例：缺少分组键

标准：SELECT dept_id, job, COUNT(*) FROM emp GROUP BY dept_id, job;

学生：SELECT dept_id, job, COUNT(*) FROM emp GROUP BY dept_id;

当前表现：ASTDiff 提取 group_by_changed，造数成功触发 SQLite 报错或结果差异。

结论：支持基础分组键变化的捕捉。


不支持的样例：包含主键的冗余分组等价

标准：SELECT user_id, COUNT(*) FROM orders GROUP BY user_id;

学生：SELECT user_id, name, COUNT(*) FROM orders JOIN users u ON user_id = u.id GROUP BY user_id, name;

当前表现：因为 user_id 是主键，name 具有函数依赖，两者等价。但系统误报分组粒度过细。

结论：缺乏 Schema 函数依赖（FD）推导，无法识别分组等价。


【HAVING】

支持的样例：HAVING 阈值错误

标准：SELECT dept_id, SUM(salary) FROM emp GROUP BY dept_id HAVING SUM(salary) > 10000;

学生：SELECT dept_id, SUM(salary) FROM emp GROUP BY dept_id HAVING SUM(salary) > 5000;

当前表现：成功在沙盒中撞出总薪水在 5000 到 10000 之间的部门，反例穿透。

结论：支持 HAVING 过滤边界的诊断。


不支持的样例：HAVING 条件误放 WHERE 导致造数拦截

标准：SELECT dept_id, SUM(salary) FROM emp GROUP BY dept_id HAVING SUM(salary) > 10000;

学生：SELECT dept_id, SUM(salary) FROM emp WHERE salary > 10000 GROUP BY dept_id;

当前表现：结构提取成功，但随机生成的数据全被 WHERE salary > 10000 过滤，导致最终聚合结果都是空的，变异验证失效。

结论：前置过滤导致造数成功率大幅下降。


【Aggregate】

支持的样例：聚合函数写反

标准：SELECT dept_id, MAX(salary) FROM emp GROUP BY dept_id;

学生：SELECT dept_id, MIN(salary) FROM emp GROUP BY dept_id;

当前表现：ASTDiff 提取 aggregate_function_changed，数据表现完美闭环。

结论：支持基础聚合函数变动的诊断。


不支持的样例：聚合内部复合参数变异

标准：SELECT SUM(CASE WHEN status = 1 THEN amount ELSE 0 END) FROM sales;

学生：SELECT SUM(CASE WHEN status = 0 THEN amount ELSE 0 END) FROM sales;

当前表现：ASTDiff 只能报出粗粒度的 parameter_changed，沙盒极难正好造出 status 对应的数据差异。

结论：聚合内部包含深层控制流时，诊断粒度退化。


【ORDER BY】

支持的样例：排序决胜列缺失

标准：SELECT name, score FROM students ORDER BY score DESC, name ASC;

学生：SELECT name, score FROM students ORDER BY score DESC;

当前表现：引擎精准提取 tiebreaker_missing，并通过临时替换验证了错因。

结论：支持多列排序的深度诊断。


不支持的样例：无关紧要的排序缺失

标准：SELECT MAX(salary) FROM emp ORDER BY id;

学生：SELECT MAX(salary) FROM emp;

当前表现：由于输出只有单行单列聚合，ORDER BY 是毫无意义的。系统强行报了 order_by_missing。

结论：未结合输出基数（Cardinality）判断排序有效性。


【LIMIT / OFFSET】

支持的样例：分页偏置错误

标准：SELECT * FROM feed LIMIT 10 OFFSET 10;

学生：SELECT * FROM feed LIMIT 10 OFFSET 0;

当前表现：精准提取 offset_changed，造数引擎压入 20 行数据，结果输出不同截断段。

结论：支持标准分页语法的判定。


不支持的样例：高级 WITH TIES 降级失败

标准：SELECT TOP 5 WITH TIES * FROM scores ORDER BY point DESC;

学生：SELECT TOP 5 * FROM scores ORDER BY point DESC;

当前表现：底层 SQLite 沙盒无法执行带 WITH TIES 的方言，抛出 EXEC_ERROR。

结论：高级方言分页语法直接阻断验证闭环。


【Subquery】

支持的样例：标量子查询运算错误

标准：SELECT title FROM books WHERE price > (SELECT AVG(price) FROM books);

学生：SELECT title FROM books WHERE price > (SELECT MAX(price) FROM books);

当前表现：结构抓取成功，聚合函数改变导致沙盒执行出明显差异。

结论：支持基础标量子查询内容的验证。


不支持的样例：子查询与 JOIN 的等价改写

标准：SELECT name FROM users WHERE id IN (SELECT user_id FROM VIP);

学生：SELECT DISTINCT name FROM users JOIN VIP ON users.id = VIP.user_id;

当前表现：系统报出 WHERE 缺失、JOIN 新增等海量错误结构，完全无法对齐。

结论：缺乏子查询解套（Unnesting）规范化。


【Correlated Subquery】

支持的样例：关联条件错写

标准：SELECT * FROM a WHERE EXISTS (SELECT 1 FROM b WHERE a.id = b.a_id);

学生：SELECT * FROM a WHERE EXISTS (SELECT 1 FROM b WHERE a.name = b.name);

当前表现：提取 correlated_predicate_changed，造数验证闭环成功。

结论：支持关联子查询内部绑定的诊断。


不支持的样例：NOT EXISTS 与反连接的等价误判

标准：SELECT * FROM a WHERE NOT EXISTS (SELECT 1 FROM b WHERE a.id = b.a_id);

学生：SELECT a.* FROM a LEFT JOIN b ON a.id = b.a_id WHERE b.a_id IS NULL;

当前表现：系统认为结构完全改变，沙盒验证等价后，退化为“未知正确逻辑”。

结论：高级相关等价改写未建立 IR 映射。


【CTE】

支持的样例：CTE 内部投影缺失

标准：WITH cte AS (SELECT id, val FROM t) SELECT val FROM cte;

学生：WITH cte AS (SELECT id FROM t) SELECT val FROM cte;

当前表现：识别 CTE 内部 column_dropped，沙盒报错无此列。

结论：支持 CTE 内部逻辑的增删判定。


不支持的样例：单次 CTE 与内联展开等价

标准：WITH active_users AS (SELECT * FROM users WHERE status=1) SELECT * FROM active_users;

学生：SELECT * FROM (SELECT * FROM users WHERE status=1) AS active_users;

当前表现：结构判定树大幅度改变，未能归一化。

结论：缺乏 CTE 内联展开（Inline View）的结构扁平化支持。


【Recursive CTE】

支持的样例：递归步长错误

标准：WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n+1 FROM t WHERE n < 5) SELECT * FROM t;

学生：WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n+2 FROM t WHERE n < 5) SELECT * FROM t;

当前表现：精准提取 recursive_step_changed，数据穿透明显。

结论：支持递归代数运算的闭环验证。


不支持的样例：多重初始锚点降级失效

标准：WITH RECURSIVE t AS (SELECT 1 AS n UNION SELECT 2 AS n UNION ALL SELECT n+1 FROM t WHERE n < 5) SELECT * FROM t;

学生：...

当前表现：对于复杂多锚点递归，SQLite 沙盒解析层偶尔引发未定义行为或深层语法树错乱。

结论：复杂递归语法树支持偏弱。


【Set Operation】

支持的样例：集合算子混用

标准：SELECT id FROM a UNION ALL SELECT id FROM b;

学生：SELECT id FROM a UNION SELECT id FROM b;

当前表现：提取 set_operator_changed，造出包含重复行的数据，触发 ALL 差异。

结论：支持 UNION / UNION ALL / INTERSECT 基础判定。


不支持的样例：INTERSECT 与 EXISTS 等价改写

标准：SELECT id FROM a INTERSECT SELECT id FROM b;

学生：SELECT id FROM a WHERE EXISTS (SELECT 1 FROM b WHERE a.id = b.id);

当前表现：结构报大量差异，未建立集合交集与 EXISTS 的语义映射。

结论：缺乏集合算子的高级等价转换规则。


【CASE】

支持的样例：缺少 ELSE 分支

标准：SELECT CASE WHEN score >= 60 THEN 'Pass' ELSE 'Fail' END FROM exams;

学生：SELECT CASE WHEN score >= 60 THEN 'Pass' END FROM exams;

当前表现：提取 case_else_missing，插入 score = 50 的数据触发 NULL 返回，反例穿透。

结论：支持 CASE 条件边界与完整性的验证。


不支持的样例：条件互斥时的 WHEN 顺序打乱

标准：SELECT CASE WHEN a = 1 THEN 'x' WHEN a = 2 THEN 'y' ELSE 'z' END FROM t;

学生：SELECT CASE WHEN a = 2 THEN 'y' WHEN a = 1 THEN 'x' ELSE 'z' END FROM t;

当前表现：ASTDiff 认为是严重结构差异，但因为条件互相排斥，逻辑上是完美等价的。

结论：缺乏对互斥条件的无序集合比对支持。


【Window】

支持的样例：缺少分区键

标准：SELECT RANK() OVER (PARTITION BY dept_id ORDER BY salary) FROM emp;

学生：SELECT RANK() OVER (ORDER BY salary) FROM emp;

当前表现：提取 window_partition_missing，造数穿透成功。

结论：支持开窗函数核心组件的诊断。


不支持的样例：隐式默认 Frame 显式化误报

标准：SELECT SUM(salary) OVER (ORDER BY id) FROM emp;

学生：SELECT SUM(salary) OVER (ORDER BY id RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM emp;

当前表现：学生显式写出了默认的 Frame 子句，ASTDiff 误报新增了 window_frame。

结论：缺乏开窗函数默认行为的语义补全映射。


【Dialect Boundary】

支持的样例：简单降级执行

标准：SELECT TOP 10 * FROM users;

学生：SELECT * FROM users LIMIT 10;

当前表现：通过底层的粗略转译，勉强认定结构近似。

结论：部分简单方言字面量在清洗层可兜底。


不支持的样例：原生沙盒强制阻断

标准：SELECT * FROM sales PIVOT (SUM(amount) FOR month IN ('Jan', 'Feb')) AS p;

学生：SELECT ...

当前表现：底层 SQLite 抛出 EXEC_ERROR，由于根本不认识 PIVOT，ASTDiff 直接失败。

结论：无真机容器（Docker 实例）支持时，高级方言全链路阻断。
"""

with open(DOCS_DIR / "16-Phase1-全22类结构详尽样例集.md", "w", encoding="utf-8") as f:
    f.write(content)

print("16-Phase1-全22类结构详尽样例集.md created!")
