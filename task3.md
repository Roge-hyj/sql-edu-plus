# 阶段一：SQL 算子覆盖与策略总结表

在 SQL 智能教学系统的阶段一（Observe）中，不同的 SQL 算子（对应关系代数算子或 DQL 子句）有着各自独立的 **AST 识别规则**、**动态造数（数据生成）策略** 与 **变分隔离测试机制**。

以下表格系统化整理了当前架构中运用和支持的所有核心算子及造数策略：

| 算子类别                      | 算子/子句名称             | SQL 语法表现                        | Sqlglot AST 节点                | 动态造数 (数据生成) 策略                                                                                                                                                                                                                                                                | 变分隔离 (Mutation) 机制                                       | 映射知识点 ID (KP ID)                                                    |
| ------------------------- | ------------------- | ------------------------------- | ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------- |
| **选择 (Selection)**        | `WHERE` (值过滤)       | `WHERE col = 10` 等              | `exp.Where`, `exp.Comparison` | **边界三态划分注入**：抽取谓词边界值 $c$，在前三行分别注入 `[c, c + 1, c - 1]`，以确保完全覆盖均符合区 ($T_{both}$)、差异区 ($T_{diff}$) 和均不符合区 ($T_{neither}$)，杜绝边界滑移的漏报。                                                                                                                                             | 替换变体：强行植入标答 `WHERE`；移除变体：移除学生 `WHERE`。                   | `where` (`COMP_VAL`)                                                |
| **空值过滤 (Null Filter)**    | `WHERE col IS NULL` | `WHERE col IS NULL`             | `exp.Is`                      | 专门检测 `exp.Is` 且包含 `exp.Null` 的空值匹配项。确保在特定行注入 `None`，以验证学生是否混淆了 `col = NULL` 与 `col IS NULL`。                                                                                                                                                                                  | 伴随 `WHERE` 变体一同替换。                                       | `comp-null` (`COMP_NULL`)                                           |
| **投影 (Projection)**       | `SELECT`            | `SELECT col1, col2`             | `exp.Select`                  | 动态识别引用的列名，仅针对目标列生成基础种子值 (`_seed_value`)，限制输出行数。                                                                                                                                                                                                                               | 暂不进行单独的变体替换（通常受 `WHERE` / `JOIN` 错误连带影响）。                | `select-basic` (`PROJ_COL`)                                         |
| **去重 (Duplicate)**        | `DISTINCT`          | `SELECT DISTINCT col`           | `exp.Distinct`                | **去重探针**：在 Row 0 和 Row 1 的非主键列（排除 ID/SSN 核心主键，但包含 `course_id`/`dept_name` 等外键或普通列）复制生成完全重复的值，以此检验 `DISTINCT` 缺失。                                                                                                                                                              | 暂不进行单独的子句变体替换。                                           | `distinct` (`DISTINCT_SET`)                                         |
| **连接 (Join)**             | `JOIN ON` / `USING` | `JOIN t2 ON t1.id = t2.id`      | `exp.Join`                    | 1. **拓扑对齐**：使用 `_join_group_key` 建立共享值池，防止空连接。<br/>2. **join-group 内唯一偏移漂移**：对同一关联值池中的不同 `table.column` 分配确定性且尽量不重复的 offset，防止多外键列（如 `s_ID` 与 `i_ID`）值在行内相同而产生同构屏蔽；不再依赖简单 ASCII 求和是否碰巧不同。<br/>3. **外连接悬浮元组设计**：对关系/子表（如 `takes`）最后一行强制赋 `None`，为父表保留非匹配行，从而区分 LEFT JOIN 与 INNER JOIN。 | 连接条件变体：提取标答 `JOIN ON` 的 ON 条件（如 `std_on`）移植替换到学生 Join 中。 | `join-on` / `join-inner` / `join-left` / `join-right` / `join-full` |
| **分组 (Grouping)**         | `GROUP BY`          | `GROUP BY col`                  | `exp.Group`                   | 每张表默认生成 4~8 行，确保分组字段存在多个不同组合的行，暴露未分组或分组字段写错的情况。                                                                                                                                                                                                                               | 替换变体：将标答的 `GROUP BY` 子句移植注入到学生 SQL 中进行沙盒复测。              | `group-by` (`GB_SIMPLE`)                                            |
| **分组过滤 (Having)**         | `HAVING`            | `HAVING COUNT(*) > 1`           | `exp.Having`                  | **聚合边界三态造数**：对 `SUM/AVG/MIN/MAX` 直接控制每个分组的聚合指标，使分组分别落在 `c + 1`、`c`、`c - 1`；对 `COUNT` 不改写数值列，而是重排分组键，使组大小分别落在 `c + 1`、`c`、`c - 1`。                                                                                                                                             | 替换变体：强行植入标答 `HAVING`；移除变体：移除学生 `HAVING`。                 | `having` (`HV_SIMPLE`)                                              |
| **排序 (Sorting)**          | `ORDER BY`          | `ORDER BY col ASC`              | `exp.Order`                   | 在 `_seed_value` 中生成具有单调递增/递减特征的数据序列。                                                                                                                                                                                                                                          | 替换变体：强行植入标答 `ORDER BY`；激活**有序模式精确比对**（禁用无序的 Counter 比对）。 | `order-by` (`SORT_ASC`)                                             |
| **限制 (Limiting)**         | `LIMIT` / `OFFSET`  | `LIMIT 5`                       | `exp.Limit` / `exp.Offset`    | 限制输出列表的行数，与 standard_rows 的长度进行核对。                                                                                                                                                                                                                                            | 替换变体：替换学生 SQL 中的 `LIMIT` 参数值。                            | `limit` (`LIMIT_OFF`)                                               |
| **简单子查询 (Subquery)**      | 非相关子查询              | `WHERE col IN (SELECT...)`      | `exp.Subquery` 等              | 提取子查询中的关联列，在父子表之间建立主外键或数据范围 of 重合。                                                                                                                                                                                                                                               | 结合 `WHERE` 算子作为条件的一部分进行替换变体测试。                           | `subquery-scalar` / `subquery-in` / `subquery-exists`               |
| **相关子查询 (Corr-Subquery)** | 关联子查询               | `WHERE s.ID IN (SELECT...)`     | `exp.Subquery` 带有父表引用         | 静态检查嵌套子查询的列名，识别是否引用了外层主表的表名或别名。并在造数时确保内外层列具有交叉数据。                                                                                                                                                                                                                             | 结合 `WHERE` 算子作为条件的一部分进行替换变体测试。                           | `subquery-correlated` (`SUB_CORR`)                                  |
| **简单 CTE (CTE)**          | 公用表表达式              | `WITH temp AS (...)`            | `exp.CTE` 非递归                 | 提取 CTE 内外层 SQL 引用到的底层基表、连接键与谓词约束，并只生成这些基表数据；`WITH` 临时结果由 SQLite 沙盒原生执行，从而验证 CTE 基表约束能否传递到最终查询。                                                                                                                                                                                | 暂不进行单独变体替换。                                              | `cte` (`CTE_SIMPLE`)                                                |
| **递归 CTE (Rec-CTE)**      | 递归公用表表达式            | `WITH RECURSIVE x AS (...)`     | `exp.CTE` 递归或自引用              | 识别自引用递归并启动**安全沙盒熔断机制**（`conn.set_progress_handler` 限制 10 万周期），防止由于数据成环引起的无限递归挂死。                                                                                                                                                                                              | 暂不进行单独变体替换。                                              | `cte-recursive` (`CTE_RECURSIVE`)                                   |
| **并集运算 (Union)**          | 并集操作                | `SELECT... UNION SELECT...`     | `exp.Union`                   | 分别对 UNION 两侧的查询提取谓词约束，合成多表模拟数据并在沙盒中校验输出元组。                                                                                                                                                                                                                                    | 暂不进行单独的集合算子变体替换。                                         | `union` (`SET_UNION`)                                               |
| **交集运算 (Intersect)**      | 交集操作                | `SELECT... INTERSECT SELECT...` | `exp.Intersect`               | 同并集操作，分别提取两侧的约束并在沙盒中校验。                                                                                                                                                                                                                                                       | 暂不进行单独的集合算子变体替换。                                         | `intersect` (`SET_INTERSECT`)                                       |
| **差集运算 (Except)**         | 差集操作                | `SELECT... EXCEPT SELECT...`    | `exp.Except`                  | 关系差集操作。抽取 EXCEPT 右侧的过滤条件，并在数据中生成排他数据行以校验结果。                                                                                                                                                                                                                                   | 暂不进行单独的集合算子变体替换。                                         | `except` (`SET_EXCEPT`)                                             |
| **条件分支 (Conditional)**    | `CASE WHEN`         | `CASE WHEN... THEN... END`      | `exp.Case`                    | 针对 CASE WHEN 条件块中的各分支条件，分别产生匹配条件的模拟数据以遍历各计算分支。                                                                                                                                                                                                                                | 暂不进行变体替换。                                                | `case` (`CASE_SEARCH`)                                              |
| **窗口函数 (Windowing)**      | `OVER`              | `ROW_NUMBER() OVER (...)`       | `exp.Window`                  | 提取窗口排序列与分组列，在数据行中产生重复分组和乱序行，以检验排名的正确性。                                                                                                                                                                                                                                        | 暂不进行变体替换。                                                | `window-row-number` (`WIN_OVER`)                                    |

---

## 💡 总结与感知层优化要点

1. **变分隔离测试（Mutation Testing）的单变量局限性**：
   对于 `SELECT` 投影项的写错、`UNION` 算子缺失、或 `CASE WHEN` 的微小逻辑错误，通常无法通过简单的“单个子句替换”来完全修复。这些错误会在**结构传感器 (E_AST)** 中留下记录，并结合沙盒的**数据差异 (E_data)**，一同汇聚至归因层，确保没有漏网之鱼。
2. **有序与无序的判定分流**：
   - 当检测到标准 SQL 包含 `ORDER BY`（`exp.Order` 节点）时，沙盒执行比对强制使用**有序精确匹配**（`res_std == res_stu`），此时排序算子的写错（如少写、方向反了）会直接被拦截。
   - 当不包含 `ORDER BY` 时，系统自动放宽限制，使用**无序频次比对**（通过 Python 的 `Counter(res_std) == Counter(res_stu)`），防止因数据库行物理输出顺序不同造成误判。
3. **主外键关联的拓扑对齐与漂移**：
   系统通过自定义的别名库（例如将 `mgr_ssn`, `essn`, `superssn` 归入 `ssn` 值池），以及多项式哈希与动态去重漂移，解决了多外键字段或跨表同名列撞车产生的伪同构遮蔽难题。
4. **安全运行沙盒熔断**：
   通过注册 progress handler，将单条 SQL 执行周期的上限牢牢限定在 10 万个虚拟机 VM 指令以内，这彻底规避了递归 CTE 题目和学生错误死循环查询挂死系统的高危风险。

---

## 三、 23 种动态造数策略与用例详析

为了确保评测系统的绝对健壮性与教学完备性，系统在 ParSEval 核心层设计并实现了以下 23 项动态造数（数据生成）策略。下面结合系统真实的测试用例、生成的数据集与沙盒输出，进行底层的机制拆解与原理分析：

### 1. WHERE 数值边界三态策略
* **策略说明**：针对数值谓词条件中的边界 $c$（如 `> c`、`<= c`），在数据行中强行注入临界值 $[c, c + 1, c - 1]$，分别覆盖均符合区 ($T_{both}$)、临界差异区 ($T_{diff}$) 和均不符合区 ($T_{neither}$)。这能打破比较操作符（如 `>` 与 `>=`）在常规随机值下的假等价遮蔽，迫使边界逻辑错误显形。
* **题目用例**：
  * **标准答案**：`SELECT title FROM course WHERE credits > 3;` (边界 $c = 3$)
  * **学生作答**：`SELECT title FROM course WHERE credits >= 3;`
* **动态测试数据**：
  ```json
  {
    "course": [
      {"course_id": 6, "title": "Engineer", "credits": 3},        // 临界差异 c (T_diff)
      {"course_id": 7, "title": "Analyst", "credits": 4},         // 阳性通过 c + 1 (T_both)
      {"course_id": 8, "title": "Sales Manager", "credits": 2}    // 阴性拦截 c - 1 (T_neither)
    ]
  }
  ```
* **沙盒输出分化**：
  * 标准输出：`[('Analyst',)]` (仅保留 credits=4)
  * 学生输出：`[('Engineer',), ('Analyst',)]` (保留了 credits=3 和 4)
  * **结论**：行数不匹配，判定不等价，归因归因为 `where`。

### 2. SELECT 投影列完整性策略
* **策略说明**：在数据生成阶段，根据 SQL 语法树仅对引用的列生成种子值限制行宽。当学生 SQL 漏投、多投或改写了投影字段（导致列名或列数不符）时，沙盒执行引擎的列结构验证机制（`columns_match`）将直接拦截并在 `select-basic`（投影缺失/错误）知识点上归因。
* **题目用例**：
  * **标准答案**：`SELECT title, credits FROM course WHERE credits > 3;`
  * **学生作答**：`SELECT title FROM course WHERE credits > 3;`
* **沙盒输出分化**：
  * 标准输出列：`["title", "credits"]` (列宽为 2)
  * 学生输出列：`["title"]` (列宽为 1)
  * **结论**：`columns_match = False`，直接拦截并归位至 `select-basic`。

### 3. NULL 空值过滤探针策略
* **策略说明**：主动在某些数据行中注入 `None` (SQL 中的 `NULL`)，同时在其它行生成普通有效值。由于 SQL 采用三值逻辑，非标准的 `col = NULL` 比较永远返回 `Unknown` (即过滤后的空集)，而标准的 `col IS NULL` 能够匹配 `None` 行。因此，主动注入 `None` 能产生悬殊的执行结果差异。
* **针对错因**：学生混淆 `WHERE col IS NULL` 与 `WHERE col = NULL`（后者永远返回 `Unknown` 空集）。
* **题目用例**：
  * **标准答案**：`SELECT name FROM student WHERE grade IS NULL;`
  * **学生作答**：`SELECT name FROM student WHERE grade = NULL;`
* **动态测试数据**：
  ```json
  {
    "student": [
      {"ID": 4, "name": "Dave", "grade": null},  // 故意注入 null
      {"ID": 5, "name": "Alice", "grade": "C"}   // 正常数据
    ]
  }
  ```
* **沙盒输出分化**：
  * 标准输出：`[('Dave',)]`
  * 学生输出：`[]` (空集，由于 `= NULL` 不成立)
  * **结论**：结果集合发生分化，归因为 `comp-null`。

### 4. DISTINCT 去重探针策略
* **策略说明**：在满足数据表唯一性约束（排除 ID/SSN 等核心主键）的安全范围内，在 Row 0 和 Row 1 的非主键列上复制生成完全重复的数据行。当学生漏写 `DISTINCT` 去重修饰符时，学生 SQL 的执行结果将产生行数膨胀（包含重复行），与标准去重 SQL 产生行数分化。
* **针对错因**：学生漏写了 `DISTINCT` 关键字。
* **动态测试数据**：
  ```json
  {
    "takes": [
      {"ID": 5, "course_id": 5},
      {"ID": 6, "course_id": 5}  // 强行复制 Row 0 的 course_id，但保留其主键 ID 的唯一性
    ]
  }
  ```
* **沙盒输出分化**：
  * 标准答案 (`DISTINCT course_id`) 输出：`[(5,)]`
  * 学生作答 (`course_id`) 输出：`[(5,), (5,)]`
  * **结论**：学生结果行膨胀，判定非等价并归位至 `distinct`。

### 5. JOIN 拓扑对齐与跨键漂移策略
* **策略说明**：使用多项式滚动哈希（Polynomial Hashing）对同组的 `table.column` 分配确定性且互不重合的偏移量（Shift），并在 Join Group 共享值池内进行动态碰撞排重。这能打乱同一行中多个外键列的值，防止因数据过于对称（如 `s_ID` 与 `i_ID` 相同）导致错连连接键（ON 条件）被同构屏蔽。
* **针对错因**：学生错连了连接键（例如应该关联 `s_ID` 却错连成了 `i_ID`）。
* **题目用例**：
  * **标准答案**：`SELECT student.name FROM student JOIN advisor ON student.ID = advisor.s_ID;`
  * **学生作答**：`SELECT student.name FROM student JOIN advisor ON student.ID = advisor.i_ID;`
* **动态测试数据**：
  ```json
  {
    "student": [{"ID": 1, "name": "Alice"}, {"ID": 2, "name": "Bob"}],
    "advisor": [
      {"s_ID": 1, "i_ID": 2}, // 行内发生值漂移：s_ID (1) != i_ID (2)
      {"s_ID": 2, "i_ID": 1}
    ]
  }
  ```
* **沙盒输出分化**：
  * 标准输出：`[('Alice',)]`
  * 学生输出：`[('Bob',)]`
  * **结论**：关联结果项完全错位，直接暴露错连接，归因为 `join-on`。

### 6. LEFT JOIN 悬浮元组策略
* **策略说明**：对关系子表（如 `takes`、`advisor`）的最后一行强制赋予 `None`，作为未匹配的“孤儿行”。这构建了天然的外连接悬浮元组，使得 `LEFT JOIN`（保留该孤儿行并填充 NULL）与 `INNER JOIN`（剔除该行）产生行数和空值项差异。
* **针对错因**：学生误将外连接写成了内连接。
* **题目用例**：
  * **标准答案**：`SELECT student.name, takes.course_id FROM student LEFT JOIN takes ON student.ID = takes.ID;`
  * **学生作答**：`SELECT student.name, takes.course_id FROM student INNER JOIN takes ON student.ID = takes.ID;`
* **动态测试数据**：
  ```json
  {
    "student": [{"ID": 4, "name": "Dave"}, {"ID": 5, "name": "Alice"}],
    "takes": [
      {"ID": 5, "course_id": 101},
      {"ID": null, "course_id": null} // 最后一行为 null，形成 Dave (ID 4) 的悬浮孤儿行
    ]
  }
  ```
* **沙盒输出分化**：
  * 标准输出：`[('Dave', None), ('Alice', 101)]`
  * 学生输出：`[('Alice', 101)]`
  * **结论**：外连接保留了悬浮元组而内连接截断之，发生行数差异，归位至 `join-left`。

### 7. GROUP BY 分组粒度错策略
* **策略说明**：系统对每张表默认生成 4~8 行，并在分组列上填充多个不同的异构分类键。这能保证当学生把分组字段写错（例如按 `building` 错写为按 `dept_name` 分组）时，各组的聚合与累加组合必然发生错位，导致求和或计数数组与标答不等价。
* **题目用例**：
  * **标准答案**：`SELECT SUM(salary) FROM instructor GROUP BY dept_name;`
  * **学生作答**：`SELECT SUM(salary) FROM instructor GROUP BY building;`
* **沙盒输出分化**：
  * 按 `dept_name` 分组的求和为：`[(6,), (2,)]`
  * 按 `building` 分组的求和为：`[(4,), (4,)]`（根据多项式哈希打乱分配的 building 列）
  * **结论**：求和结果数组大小和无序集合完全错位，归位至 `group-by`。

### 8. HAVING SUM 聚合边界三态策略
* **策略说明**：由于 HAVING 过滤发生在分组聚合之后，不能直接改写基表单行数据。系统将记录按分组归类，并分别对各组数据做三态控制，使各分组的聚合 `SUM` 目标值精确达到 $c + 1$（阳性通过）、$c$（临界差异）和 $c - 1$（阴性过滤）。再除以组内行数 $k$ 填充回单行记录中，激活 HAVING 谓词边界过滤。
* **题目用例**：
  * **标准答案**：`SELECT dept_name FROM instructor GROUP BY dept_name HAVING SUM(salary) > 80000;` (边界 $c = 80000$)
  * **学生作答**：`SELECT dept_name FROM instructor GROUP BY dept_name HAVING SUM(salary) < 80000;`
* **动态测试数据**：
  ```json
  {
    "instructor": [
      {"dept_name": "Math", "salary": 40000.5},    // Math 组两行，SUM = 80001 (c + 1)
      {"dept_name": "Math", "salary": 40000.5},
      {"dept_name": "Physics", "salary": 39999.5}, // Physics 组两行，SUM = 79999 (c - 1)
      {"dept_name": "Physics", "salary": 39999.5},
      {"dept_name": "History", "salary": 40000.0}, // History 组两行，SUM = 80000 (临界值 c)
      {"dept_name": "History", "salary": 40000.0}
    ]
  }
  ```
* **沙盒输出分化**：
  * 标准输出：`[('Math',)]`
  * 学生输出：`[('Physics',)]`
  * **结论**：输出完全不一致，检测成功，归位至 `having`。

### 9. HAVING AVG 聚合边界三态策略
* **策略说明**：同 SUM 策略。控制每个分组的 `AVG`（均值）结果值，使其分别精确达到 $[c+1, c, c-1]$。数据生成时，直接使该分组内所有行的数值列均等于对应的目标值，使其平均值精确被控，引爆 HAVING 边界判断差异。
* **题目用例**：
  * **标准答案**：`SELECT dept_name FROM instructor GROUP BY dept_name HAVING AVG(salary) > 50000;`
  * **学生作答**：`SELECT dept_name FROM instructor GROUP BY dept_name HAVING AVG(salary) < 50000;`
* **动态测试数据**：
  ```json
  {
    "instructor": [
      {"dept_name": "Math", "salary": 50001},    // Math 组：AVG = 50001 (c + 1)
      {"dept_name": "Math", "salary": 50001},
      {"dept_name": "Physics", "salary": 49999}, // Physics 组：AVG = 49999 (c - 1)
      {"dept_name": "Physics", "salary": 49999},
      {"dept_name": "History", "salary": 50000}  // History 组：AVG = 50000 (临界 c)
    ]
  }
  ```
* **沙盒输出分化**：输出行分化，成功归位至 `having`。

### 10. HAVING MIN 聚合边界三态策略
* **策略说明**：控制每个分组的 `MIN`（极小值）结果值，使其分别达到 $[c+1, c, c-1]$。数据干预时，将该组 Row 0 设为目标值 `T`，组内其余行均设为 `T + 1`，保证该组的最小值精确锁定在 `T`，校验极值过滤逻辑。
* **题目用例**：
  * **标准答案**：`SELECT dept_name FROM instructor GROUP BY dept_name HAVING MIN(salary) > 30000;`
  * **学生作答**：`SELECT dept_name FROM instructor GROUP BY dept_name HAVING MIN(salary) < 30000;`
* **动态测试数据**：
  ```json
  {
    "instructor": [
      {"dept_name": "Math", "salary": 30001},    // Math 组：Row 0=30001, Row 1=30002. MIN = 30001 (c + 1)
      {"dept_name": "Math", "salary": 30002},
      {"dept_name": "Physics", "salary": 29999}, // Physics 组：MIN = 29999 (c - 1)
      {"dept_name": "Physics", "salary": 30000}
    ]
  }
  ```
* **沙盒输出分化**：输出结果不一致，检测成功。

### 11. HAVING MAX 聚合边界三态策略
* **策略说明**：控制每个分组的 `MAX`（极大值）结果值，使其分别达到 $[c+1, c, c-1]$。数据干预时，将该组 Row 0 设为目标值 `T`，组内其余行均设为 `T - 1`，保证该组的最大值精确锁定在 `T`，校验极值过滤逻辑。
* **题目用例**：
  * **标准答案**：`SELECT dept_name FROM instructor GROUP BY dept_name HAVING MAX(salary) > 90000;`
  * **学生作答**：`SELECT dept_name FROM instructor GROUP BY dept_name HAVING MAX(salary) < 90000;`
* **动态测试数据**：
  ```json
  {
    "instructor": [
      {"dept_name": "Math", "salary": 90001},    // Math 组：Row 0=90001, Row 1=90000. MAX = 90001 (c + 1)
      {"dept_name": "Math", "salary": 90000},
      {"dept_name": "Physics", "salary": 89999}, // Physics 组：MAX = 89999 (c - 1)
      {"dept_name": "Physics", "salary": 89998}
    ]
  }
  ```
* **沙盒输出分化**：分化成功，判定非等价。

### 12. HAVING COUNT 组大小三态策略
* **策略说明**：对于行记录数限制（`COUNT`），无法通过改写数值列生效。本策略直接重排和限制分组列的物理键分配，使得各分组的物理行数（即组大小）分别等于 $[c+1, c, c-1]$，从而当比较行数写错时直接被沙盒过滤拦截。
* **题目用例**：
  * **标准答案**：`SELECT dept_name FROM instructor GROUP BY dept_name HAVING COUNT(ID) >= 2;`
  * **学生作答**：`SELECT dept_name FROM instructor GROUP BY dept_name HAVING COUNT(ID) > 2;`
* **动态测试数据**：
  ```json
  {
    "instructor": [
      {"ID": 2, "dept_name": "Comp. Sci."}, // 组内包含 3 行 (c + 1)
      {"ID": 3, "dept_name": "Comp. Sci."},
      {"ID": 4, "dept_name": "Comp. Sci."},
      {"ID": 5, "dept_name": "Math"},       // 组内包含 2 行 (临界 c)
      {"ID": 6, "dept_name": "Math"},
      {"ID": 7, "dept_name": "Physics"}      // 组内包含 1 行 (c - 1)
    ]
  }
  ```
* **沙盒输出分化**：
  * 标准输出：`[('Comp. Sci.',), ('Math',)]` (行数 >= 2)
  * 学生输出：`[('Comp. Sci.',)]` (行数 > 2)
  * **结论**：行数不匹配，成功归类到 `having`。

### 13. ORDER BY 有序精确比对策略
* **策略说明**：提取排序关键字并生成具有单调递增/递减特征的数据序列。一旦检测到 SQL 中包含 `ORDER BY`，沙盒结果比对模块将关闭无序的频次比对（Counter），转为严格的顺序列表比对（`std_rows == stu_rows`），使排序方向或排序列写错直接暴露。
* **题目用例**：
  * **标准答案**：`SELECT title FROM course ORDER BY credits DESC;`
  * **学生作答**：`SELECT title FROM course ORDER BY credits ASC;`
* **沙盒输出分化**：
  * 标准输出：`[('Analyst',), ('Engineer',)]` ( credits 降序)
  * 学生输出：`[('Engineer',), ('Analyst',)]` (credits 升序)
  * **结论**：即便元素集合完全一致，但因顺序未精确对齐而拦截，归因为 `order-by`。

### 14. LIMIT 行数边界策略
* **策略说明**：限制输出的元组行数。通过沙盒直接验证 `LIMIT` 或 `OFFSET` 参数的数值偏差，让多取或少取数据的学生 SQL 产生行数不等。
* **题目用例**：
  * **标准答案**：`SELECT title FROM course LIMIT 3;`
  * **学生作答**：`SELECT title FROM course LIMIT 5;`
* **沙盒输出分化**：标准输出 3 行，学生输出 5 行。判定不等价，归因为 `limit`。

### 15. 子查询内外层值域重合策略
* **策略说明**：提取子查询中的关联列，在父子表之间建立主外键或数据范围的重合（对齐 ID 共享值池，打通拓扑通路）。再配合子查询内部的过滤谓词，强行在子表中构造阳性重合行（满足过滤）、阴性混淆行（不满足过滤）和悬浮行，触发子查询的过滤选择权，暴露内外层值域逻辑错。
* **题目用例**：
  * **标准答案**：`SELECT name FROM student WHERE ID IN (SELECT ID FROM takes WHERE year = 2017);`
  * **学生作答**：`SELECT name FROM student WHERE ID NOT IN (SELECT ID FROM takes WHERE year = 2017);`
* **动态测试数据**：
  ```json
  {
    "student": [{"ID": 4, "name": "Dave"}, {"ID": 5, "name": "Alice"}],
    "takes": [
      {"ID": 5, "year": 2017}, // ID 5 重合且年符合
      {"ID": 6, "year": 2018}
    ]
  }
  ```
* **沙盒输出分化**：
  * 标准输出：`[('Alice',)]`
  * 学生输出：`[('Dave',)]`
  * **结论**：发生数据反向分化，判定成功。

### 16. 相关子查询内外层关联策略
* **策略说明**：静态扫描子查询的过滤条件，识别并提取外层主表的引用列（如 `t.ID = s.ID`）。在生成数据时，确保被引用的主表 ID 与子查询中关联表的 ID 发生数据交叉（在内层表生成对应相关变量的多态数据），以便在子查询被多次关联扫描时，逻辑漏洞能被沙盒识别。
* **题目用例**：
  * **标准答案**：`SELECT name FROM student s WHERE EXISTS (SELECT 1 FROM takes t WHERE t.ID = s.ID AND t.year = 2017);`
  * **学生作答**：`SELECT name FROM student s WHERE EXISTS (SELECT 1 FROM takes t WHERE t.ID = s.ID AND t.year = 2018);`
* **动态测试数据**：
  ```json
  {
    "student": [{"ID": 5, "name": "Alice"}, {"ID": 6, "name": "Bob"}],
    "takes": [
      {"ID": 5, "year": 2017}, // t.ID = s.ID 发生交叉关联，且 year = 2017
      {"ID": 6, "year": 2018}  // t.ID = s.ID 发生交叉关联，且 year = 2018
    ]
  }
  ```
* **沙盒输出分化**：
  * 标准输出：`[('Alice',)]`
  * 学生输出：`[('Bob',)]`
  * **结论**：输出完全对立，归位至 `subquery-correlated`。

### 17. 集合操作 UNION 去重差异策略
* **策略说明**：在集合算子（`UNION` / `UNION ALL`）左右两侧的子查询结果中生成完全重复的行。当学生混淆 `UNION`（集合自动去重）与 `UNION ALL`（保留所有重复行）时，学生 SQL 的执行输出将包含额外的重复行，行数随之分化。
* **题目用例**：
  * **标准答案**：`SELECT title FROM course WHERE dept_name = 'Math' UNION SELECT title FROM course WHERE dept_name = 'Physics';`
  * **学生作答**：`SELECT title FROM course WHERE dept_name = 'Math' UNION ALL SELECT title FROM course WHERE dept_name = 'Physics';`
* **沙盒输出分化**：
  * 标准输出：`[('Engineer',)]` (已去重合并)
  * 学生输出：`[('Engineer',), ('Engineer',)]` (未去重保留)
  * **结论**：结果行数不匹配，判定非等价，归为 `union`。

### 18. 集合操作 INTERSECT 交集差异策略
* **策略说明**：在数据生成阶段，分别生成“仅满足左侧条件”、“仅满足右侧条件”以及“同时满足两侧条件”的记录。当学生错写集合操作符（如用 `UNION` 替代了 `INTERSECT`）时，沙盒执行结果将从交集空集或子集膨胀为并集，暴露逻辑错。
* **题目用例**：
  * **标准答案**：`SELECT title FROM course WHERE dept_name = 'Math' INTERSECT SELECT title FROM course WHERE credits > 3;`
  * **学生作答**：`SELECT title FROM course WHERE dept_name = 'Math' UNION SELECT title FROM course WHERE credits > 3;`
* **沙盒输出分化**：标准输出交集空集，学生输出并集多行。成功判定，归因为 `intersect`。

### 19. 集合操作 EXCEPT 排他差异策略
* **策略说明**：提取 EXCEPT 右侧的过滤条件并在数据中生成排他数据行。这能保证当学生漏写了 `EXCEPT` 差集排除逻辑时，学生 SQL 的输出中会多出本应该被剔除的行，打破等价性。
* **题目用例**：
  * **标准答案**：`SELECT title FROM course EXCEPT SELECT title FROM course WHERE dept_name = 'Physics';`
  * **学生作答**：`SELECT title FROM course;`
* **沙盒输出分化**：标准输出排除了 `Physics` 的记录，学生未排除。归因为 `except`。

### 20. CASE WHEN 分支边界三态策略
* **策略说明**：针对 CASE WHEN 块中的各个分支条件（如 `amount > 100`），分别产生满足三态边界（$c$、$c+1$、$c-1$）的测试数据，从而在沙盒执行时强制遍历所有计算和转换分支，校验条件边界的准确性。
* **题目用例**：
  * **标准答案**：`SELECT category, SUM(CASE WHEN amount > 100 THEN amount ELSE 0 END) AS big_sales FROM sales GROUP BY category;`
  * **学生作答**：`SELECT category, SUM(CASE WHEN amount >= 100 THEN amount ELSE 0 END) AS big_sales FROM sales GROUP BY category;`
* **动态测试数据**：
  ```json
  {
    "sales": [
      {"category": "category_6", "amount": 100},  // 临界 c，标准走 ELSE 0，学生走 THEN amount
      {"category": "category_7", "amount": 101},  // 阳性 c+1，均走 THEN
      {"category": "category_8", "amount": 99}    // 阴性 c-1，均走 ELSE
    ]
  }
  ```
* **沙盒输出分化**：
  * 标准输出：`[('category_6', 0), ('category_7', 101), ...]`
  * 学生输出：`[('category_6', 100), ('category_7', 101), ...]`
  * **结论**：求和累加值不同，归位为 `case`。

### 21. WINDOW 分区与排序数据策略
* **策略说明**：提取窗口函数的排序列与分区列，在数据行中产生重复分区和乱序行，以检验排名的正确性。
* **针对错因**：学生在 `OVER` 子句中漏写 `PARTITION BY` 等分区子句。
* **沙盒输出分化**：标准输出每个部门独立分配 rank（如 1、2、1、2），学生输出全局分配（如 1、2、3、4），数据发生冲突。

### 22. CTE 基表约束传递策略
* **策略说明**：回溯 CTE（WITH 表达式）内部引用的底层基表并针对这些基表进行三态造数，而拒绝直接预造 CTE 临时表。CTE 定义与外层 `JOIN` 均交给 SQLite 原生执行，确保基表约束能自然传导至最外层，校验 CTE 基表约束传递性。
* **题目用例**：
  * **标准答案**：
    ```sql
    WITH big_co AS (SELECT company_name FROM company WHERE city = 'Beijing') 
    SELECT person_name, salary FROM works JOIN big_co ON works.company_name = big_co.company_name WHERE salary > 10000;
    ```
  * **学生作答**：( 错写为 `salary < 10000` )
* **沙盒输出分化**：底层的 3 行 `works.salary` 三态数据传导到了外层，使标准输出 Bob (10001) 而学生输出 Bob (7)，成功识别非等价，归位为 `cte` 与 `where`。

### 23. 递归 CTE 终止边界与沙盒熔断策略
* **策略说明**：静态检测 `WITH RECURSIVE` 结构。除了在自引用序列上产生离散数据校验终止边界外，还在 SQLite 沙盒执行时启用虚拟机周期计数器（Progress Handler），将指令周期锁定在 10 万个以内。一旦死循环立即熔断，防止系统被学生错误 SQL 挂死。
* **题目用例**：
  * **标准答案**：`WITH RECURSIVE nums AS (SELECT 1 AS n UNION ALL SELECT n + 1 AS n FROM nums WHERE n < 3) SELECT n FROM nums;`
  * **学生作答**：`WITH RECURSIVE nums AS (SELECT 1 AS n UNION ALL SELECT n + 1 AS n FROM nums WHERE n < 5) SELECT n FROM nums;`
* **沙盒输出分化**：
  * 标准输出：`[(1,), (2,), (3,)]`
  * 学生输出：`[(1,), (2,), (3,), (4,), (5,)]`
  * **结论**：行数不一致。若有死循环，progress handler 抛出中断错误进行熔断防御，归位至 `cte-recursive`。
