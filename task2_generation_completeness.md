# 动态造数策略完备性专项检验

## 六、动态造数策略完备性专项检验

本节逐项对应 `task3.md` 的动态造数策略，验证两个层面：

1. **策略完备性**：生成的数据必须包含能区分标准 SQL 与学生 SQL 的攻击样本，例如三态边界、重复探针、JOIN 键漂移、悬浮元组或聚合边界组。
2. **实现完备性**：实际后端 `generate_and_compare` 必须在这些数据上判定不等价，并产出非空且命中预期 KP 的归因。

| 策略板块 | 沙盒等价 | 期望 KP | 实际 KP | 策略检查 | 结论 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| WHERE 数值边界三态 | `False` | `where` | `where` | `PASS；PASS` | `PASS` |
| SELECT 投影列完整性 | `False` | `select-basic` | `select-basic` | `PASS；PASS；PASS` | `PASS` |
| NULL 空值过滤探针 | `False` | `comp-null` | `where, comp-null` | `PASS；PASS` | `PASS` |
| DISTINCT 去重探针 | `False` | `distinct` | `distinct` | `PASS；PASS` | `PASS` |
| JOIN 拓扑对齐与跨键漂移 | `False` | `join-on` | `join-on` | `PASS；PASS` | `PASS` |
| LEFT JOIN 悬浮元组 | `False` | `join-left` | `join-left` | `PASS；PASS` | `PASS` |
| GROUP BY 分组粒度错 | `False` | `group-by` | `group-by` | `PASS` | `PASS` |
| HAVING SUM 聚合边界三态 | `False` | `having` | `having` | `PASS；PASS` | `PASS` |
| HAVING AVG 聚合边界三态 | `False` | `having` | `having` | `PASS；PASS` | `PASS` |
| HAVING MIN 聚合边界三态 | `False` | `having` | `having` | `PASS；PASS` | `PASS` |
| HAVING MAX 聚合边界三态 | `False` | `having` | `having` | `PASS；PASS` | `PASS` |
| HAVING COUNT 组大小三态 | `False` | `having` | `having` | `PASS；PASS` | `PASS` |
| ORDER BY 有序精确比对 | `False` | `order-by` | `order-by` | `PASS；PASS` | `PASS` |
| LIMIT 行数边界 | `False` | `limit` | `limit` | `PASS；PASS` | `PASS` |
| 子查询内外层值域重合 | `False` | `where` | `where, subquery-scalar` | `PASS；PASS` | `PASS` |
| 相关子查询内外层关联 | `False` | `subquery-correlated` | `where, subquery-correlated` | `PASS；PASS；PASS` | `PASS` |
| 集合操作 UNION 去重差异 | `False` | `union` | `union` | `PASS` | `PASS` |
| 集合操作 INTERSECT 交集差异 | `False` | `intersect` | `intersect` | `PASS` | `PASS` |
| 集合操作 EXCEPT 排他差异 | `False` | `except` | `except, where` | `PASS` | `PASS` |
| CASE WHEN 分支边界三态 | `False` | `case` | `select-basic, case` | `PASS；PASS` | `PASS` |
| WINDOW 分区与排序数据 | `False` | `window-row-number` | `window-row-number, select-basic` | `PASS；PASS` | `PASS` |
| CTE 基表约束传递 | `False` | `where` | `where` | `PASS；PASS` | `PASS` |
| 递归 CTE 终止边界与沙盒熔断 | `False` | `cte-recursive` | `cte-recursive, union, where` | `PASS；PASS` | `PASS` |

### WHERE 数值边界三态
* **策略说明**：针对数值谓词条件中的边界 $c$（如 `> c`、`<= c`），在数据行中强行注入临界值 $[c, c + 1, c - 1]$，分别覆盖均符合区 ($T_{both}$)、临界差异区 ($T_{diff}$) 和均不符合区 ($T_{neither}$)。这能打破比较操作符（如 `>` 与 `>=`）在常规随机值下的假等价遮蔽，迫使边界逻辑错误显形。
* **Schema**: `course(course_id, title, dept_name, credits)`
* **标准答案 SQL**:
```sql
SELECT title FROM course WHERE credits > 3;
```
* **学生作答 SQL**:
```sql
SELECT title FROM course WHERE credits >= 3;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['where']`
* **AST 差异子树**: `[{'standard_sql': 'credits > 3', 'student_sql': 'credits >= 3', 'clause': 'WHERE', 'diff_type': 'where_changed', 'column': None, 'table': None}, {'column': 'credits', 'standard_op': 'GT', 'student_op': 'GTE', 'value': 3, 'student_value': 3, 'values': None, 'student_values': None, 'standard_sql': 'credits > 3', 'student_sql': 'credits >= 3', 'clause': 'PREDICATE', 'diff_type': 'comparison_operator_changed', 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'predicate_counterexample', 'clause': 'WHERE', 'diff_type': 'where_changed'}, {'tactic': 'comparison_boundary_tristate', 'clause': 'PREDICATE', 'diff_type': 'comparison_operator_changed'}]`
* **策略检查结果**:
  1. `PASS` - course.credits values=[3, 4, 2, 3, 5, 6, 7, 3], required=[2, 3, 4]
  2. `PASS` - expected KP=where, actual=['where']
* **动态生成的数据集**:
```json
{
  "course": [
    {
      "course_id": 1,
      "title": "Sales Manager__predicate_row_000",
      "dept_name": "Comp. Sci.",
      "credits": 3
    },
    {
      "course_id": 2,
      "title": "Marketing Lead__predicate_row_001",
      "dept_name": "Math",
      "credits": 4
    },
    {
      "course_id": 3,
      "title": "Engineer__predicate_row_002",
      "dept_name": "Physics",
      "credits": 2
    },
    {
      "course_id": 4,
      "title": "Analyst__predicate_row_003",
      "dept_name": "History",
      "credits": 3
    },
    {
      "course_id": 5,
      "title": "Sales Manager__predicate_row_004",
      "dept_name": "Comp. Sci.",
      "credits": 5
    },
    {
      "course_id": 6,
      "title": "Marketing Lead__predicate_row_005",
      "dept_name": "Math",
      "credits": 6
    },
    {
      "course_id": 7,
      "title": "Engineer__predicate_row_006",
      "dept_name": "Physics",
      "credits": 7
    },
    {
      "course_id": 8,
      "title": "Analyst__predicate_row_007",
      "dept_name": "History",
      "credits": 3
    }
  ]
}
```
* **标准输出样本**: `[('Marketing Lead__predicate_row_001',), ('Sales Manager__predicate_row_004',), ('Marketing Lead__predicate_row_005',), ('Engineer__predicate_row_006',)]`
* **学生输出样本**: `[('Sales Manager__predicate_row_000',), ('Marketing Lead__predicate_row_001',), ('Analyst__predicate_row_003',), ('Sales Manager__predicate_row_004',), ('Marketing Lead__predicate_row_005',)]`

### SELECT 投影列完整性
* **策略说明**：在数据生成阶段，根据 SQL 语法树仅对引用的列生成种子值限制行宽。当学生 SQL 漏投、多投或改写了投影字段（导致列名或列数不符）时，沙盒执行引擎的列结构验证机制（`columns_match`）将直接拦截并在 `select-basic`（投影缺失/错误）知识点上归因。
* **Schema**: `course(course_id, title, dept_name, credits)`
* **标准答案 SQL**:
```sql
SELECT title, credits FROM course WHERE credits > 3;
```
* **学生作答 SQL**:
```sql
SELECT title FROM course WHERE credits > 3;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['select-basic']`
* **AST 差异子树**: `[{'standard_sql': 'title, credits', 'student_sql': 'title', 'clause': 'SELECT', 'diff_type': 'projection_changed', 'column': None, 'table': None}, {'standard_sql': 'credits', 'student_sql': '', 'position': 1, 'clause': 'SELECT', 'diff_type': 'column_dropped', 'column': 'credits', 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'projection_shape_check', 'clause': 'SELECT', 'diff_type': 'projection_changed'}, {'tactic': 'projection_shape_check', 'clause': 'SELECT', 'diff_type': 'column_dropped'}]`
* **策略检查结果**:
  1. `PASS` - course.credits values=[3, 4, 2, 3, 5, 6, 7, 3], required=[2, 3, 4]
  2. `PASS` - standard_columns=['title', 'credits'], student_columns=['title']
  3. `PASS` - expected KP=select-basic, actual=['select-basic']
* **动态生成的数据集**:
```json
{
  "course": [
    {
      "course_id": 1,
      "title": "Sales Manager",
      "dept_name": "Comp. Sci.",
      "credits": 3
    },
    {
      "course_id": 2,
      "title": "Marketing Lead",
      "dept_name": "Math",
      "credits": 4
    },
    {
      "course_id": 3,
      "title": "Engineer",
      "dept_name": "Physics",
      "credits": 2
    },
    {
      "course_id": 4,
      "title": "Analyst",
      "dept_name": "History",
      "credits": 3
    },
    {
      "course_id": 5,
      "title": "Sales Manager",
      "dept_name": "Comp. Sci.",
      "credits": 5
    },
    {
      "course_id": 6,
      "title": "Marketing Lead",
      "dept_name": "Math",
      "credits": 6
    },
    {
      "course_id": 7,
      "title": "Engineer",
      "dept_name": "Physics",
      "credits": 7
    },
    {
      "course_id": 8,
      "title": "Analyst",
      "dept_name": "History",
      "credits": 3
    }
  ]
}
```
* **标准输出样本**: `[('Marketing Lead', 4), ('Sales Manager', 5), ('Marketing Lead', 6), ('Engineer', 7)]`
* **学生输出样本**: `[('Marketing Lead',), ('Sales Manager',), ('Marketing Lead',), ('Engineer',)]`

### NULL 空值过滤探针
* **策略说明**：主动在某些数据行中注入 `None` (SQL 中的 `NULL`)，同时在其它行生成普通有效值。由于 SQL 采用三值逻辑，非标准的 `col = NULL` 比较永远返回 `Unknown` (即过滤后的空集)，而标准的 `col IS NULL` 能够匹配 `None` 行。因此，主动注入 `None` 能产生悬殊的执行结果差异。
* **Schema**: `student(ID, name, grade)`
* **标准答案 SQL**:
```sql
SELECT name FROM student WHERE grade IS NULL;
```
* **学生作答 SQL**:
```sql
SELECT name FROM student WHERE grade = NULL;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['where', 'comp-null']`
* **AST 差异子树**: `[{'standard_sql': 'grade IS NULL', 'student_sql': 'grade = NULL', 'clause': 'WHERE', 'diff_type': 'where_changed', 'column': None, 'table': None}, {'column': 'grade', 'standard_op': 'IS', 'student_op': 'EQ', 'value': None, 'student_value': None, 'values': None, 'student_values': None, 'standard_sql': 'grade IS NULL', 'student_sql': 'grade = NULL', 'clause': 'PREDICATE', 'diff_type': 'comparison_operator_changed', 'table': None}, {'column': 'grade', 'value': None, 'standard_sql': 'grade IS NULL', 'student_sql': 'grade = NULL', 'clause': 'NULL', 'diff_type': 'null_equality_changed', 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'predicate_counterexample', 'clause': 'WHERE', 'diff_type': 'where_changed'}, {'tactic': 'comparison_boundary_tristate', 'clause': 'PREDICATE', 'diff_type': 'comparison_operator_changed'}, {'tactic': 'null_probe', 'clause': 'NULL', 'diff_type': 'null_equality_changed'}]`
* **策略检查结果**:
  1. `PASS` - student.grade values=[None, 'B', 'C', None, 'A', 'B', 'C', None]
  2. `PASS` - expected KP=comp-null, actual=['where', 'comp-null']
* **动态生成的数据集**:
```json
{
  "student": [
    {
      "ID": 1,
      "name": "Alice__predicate_row_000",
      "grade": null
    },
    {
      "ID": 2,
      "name": "Bob__predicate_row_001",
      "grade": "B"
    },
    {
      "ID": 3,
      "name": "Carol__predicate_row_002",
      "grade": "C"
    },
    {
      "ID": 4,
      "name": "Dave__predicate_row_003",
      "grade": null
    },
    {
      "ID": 5,
      "name": "Alice__predicate_row_004",
      "grade": "A"
    },
    {
      "ID": 6,
      "name": "Bob__predicate_row_005",
      "grade": "B"
    },
    {
      "ID": 7,
      "name": "Carol__predicate_row_006",
      "grade": "C"
    },
    {
      "ID": 8,
      "name": "Dave__predicate_row_007",
      "grade": null
    }
  ]
}
```
* **标准输出样本**: `[('Alice__predicate_row_000',), ('Dave__predicate_row_003',), ('Dave__predicate_row_007',)]`
* **学生输出样本**: `[]`

### DISTINCT 去重探针
* **策略说明**：在满足数据表唯一性约束（排除 ID/SSN 等核心主键）的安全范围内，在 Row 0 和 Row 1 的非主键列上复制生成完全重复的数据行。当学生漏写 `DISTINCT` 去重修饰符时，学生 SQL 的执行结果将产生行数膨胀（包含重复行），与标准去重 SQL 产生行数分化。
* **Schema**: `takes(ID, course_id, sec_id, semester, year, grade)`
* **标准答案 SQL**:
```sql
SELECT DISTINCT course_id FROM takes;
```
* **学生作答 SQL**:
```sql
SELECT course_id FROM takes;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['distinct']`
* **AST 差异子树**: `[{'standard_sql': 'True', 'student_sql': 'False', 'clause': 'DISTINCT', 'diff_type': 'distinct_changed', 'column': None, 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'duplicate_projection_probe', 'clause': 'DISTINCT', 'diff_type': 'distinct_changed'}]`
* **策略检查结果**:
  1. `PASS` - takes.course_id duplicate_values=[1], values=[1, 1, 3, 4, 5, 6, 7, 8]
  2. `PASS` - expected KP=distinct, actual=['distinct']
* **动态生成的数据集**:
```json
{
  "takes": [
    {
      "ID": 1,
      "course_id": 1,
      "sec_id": 1,
      "semester": "Fall",
      "year": 1,
      "grade": "A"
    },
    {
      "ID": 2,
      "course_id": 1,
      "sec_id": 2,
      "semester": "Spring",
      "year": 2,
      "grade": "B"
    },
    {
      "ID": 3,
      "course_id": 3,
      "sec_id": 3,
      "semester": "Summer",
      "year": 3,
      "grade": "C"
    },
    {
      "ID": 4,
      "course_id": 4,
      "sec_id": 4,
      "semester": "Winter",
      "year": 4,
      "grade": null
    },
    {
      "ID": 5,
      "course_id": 5,
      "sec_id": 5,
      "semester": "Fall",
      "year": 5,
      "grade": "A"
    },
    {
      "ID": 6,
      "course_id": 6,
      "sec_id": 6,
      "semester": "Spring",
      "year": 6,
      "grade": "B"
    },
    {
      "ID": 7,
      "course_id": 7,
      "sec_id": 7,
      "semester": "Summer",
      "year": 7,
      "grade": "C"
    },
    {
      "ID": 8,
      "course_id": 8,
      "sec_id": 8,
      "semester": "Winter",
      "year": 8,
      "grade": null
    }
  ]
}
```
* **标准输出样本**: `[(1,), (3,), (4,), (5,), (6,)]`
* **学生输出样本**: `[(1,), (1,), (3,), (4,), (5,)]`

### JOIN 拓扑对齐与跨键漂移
* **策略说明**：使用多项式滚动哈希（Polynomial Hashing）对同组的 `table.column` 分配确定性且互不重合的偏移量（Shift），并在 Join Group 共享值池内进行动态碰撞排重。这能打乱同一行中多个外键列的值，防止因数据过于对称（如 `s_ID` 与 `i_ID` 相同）导致错连连接键（ON 条件）被同构屏蔽。
* **Schema**: `student(ID, name, dept_name); advisor(s_ID, i_ID)`
* **标准答案 SQL**:
```sql
SELECT student.name FROM student JOIN advisor ON student.ID = advisor.s_ID;
```
* **学生作答 SQL**:
```sql
SELECT student.name FROM student JOIN advisor ON student.ID = advisor.i_ID;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['join-on']`
* **AST 差异子树**: `[{'standard_sql': 'student.id = advisor.s_id', 'student_sql': '', 'clause': 'JOIN ON', 'diff_type': 'join_on_changed', 'column': None, 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'join_key_drift_probe', 'clause': 'JOIN ON', 'diff_type': 'join_on_changed'}]`
* **策略检查结果**:
  1. `PASS` - student.ID=[1000, 1001, 1002, 1003, 1004, 1005, 1006, 1007], advisor.s_ID=[1000, 1001, 1002, 1003, 1004, 1005, 1006, 1007], advisor.i_ID=[1000, 9101, 1002, 9103, 1004, 9105, 1006, 9107]
  2. `PASS` - expected KP=join-on, actual=['join-on']
* **动态生成的数据集**:
```json
{
  "student": [
    {
      "ID": 1000,
      "name": "Alice",
      "dept_name": "Comp. Sci."
    },
    {
      "ID": 1001,
      "name": "Bob",
      "dept_name": "Math"
    },
    {
      "ID": 1002,
      "name": "Carol",
      "dept_name": "Physics"
    },
    {
      "ID": 1003,
      "name": "Dave",
      "dept_name": "History"
    },
    {
      "ID": 1004,
      "name": "Alice",
      "dept_name": "Comp. Sci."
    },
    {
      "ID": 1005,
      "name": "Bob",
      "dept_name": "Math"
    },
    {
      "ID": 1006,
      "name": "Carol",
      "dept_name": "Physics"
    },
    {
      "ID": 1007,
      "name": "Dave",
      "dept_name": "History"
    }
  ],
  "advisor": [
    {
      "s_ID": 1000,
      "i_ID": 1000
    },
    {
      "s_ID": 1001,
      "i_ID": 9101
    },
    {
      "s_ID": 1002,
      "i_ID": 1002
    },
    {
      "s_ID": 1003,
      "i_ID": 9103
    },
    {
      "s_ID": 1004,
      "i_ID": 1004
    },
    {
      "s_ID": 1005,
      "i_ID": 9105
    },
    {
      "s_ID": 1006,
      "i_ID": 1006
    },
    {
      "s_ID": 1007,
      "i_ID": 9107
    }
  ]
}
```
* **标准输出样本**: `[('Alice',), ('Bob',), ('Carol',), ('Dave',), ('Alice',)]`
* **学生输出样本**: `[('Alice',), ('Carol',), ('Alice',), ('Carol',)]`

### LEFT JOIN 悬浮元组
* **策略说明**：对关系子表（如 `takes`、`advisor`）的最后一行强制赋予 `None`，作为未匹配的“孤儿行”。这构建了天然的外连接悬浮元组，使得 `LEFT JOIN`（保留该孤儿行并填充 NULL）与 `INNER JOIN`（剔除该行）产生行数和空值项差异。
* **Schema**: `student(ID, name, dept_name); takes(ID, course_id)`
* **标准答案 SQL**:
```sql
SELECT student.name, takes.course_id FROM student LEFT JOIN takes ON student.ID = takes.ID;
```
* **学生作答 SQL**:
```sql
SELECT student.name, takes.course_id FROM student INNER JOIN takes ON student.ID = takes.ID;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['join-left']`
* **AST 差异子树**: `[{'standard_side': 'LEFT', 'student_side': 'INNER', 'right_table': 'takes', 'standard_sql': 'LEFT JOIN takes ON student.id = takes.id', 'student_sql': 'INNER JOIN takes ON student.id = takes.id', 'clause': 'JOIN_TYPE', 'diff_type': 'join_type_changed', 'column': None, 'table': 'takes'}]`
* **差异驱动造数策略**: `[{'tactic': 'outer_join_dangling_tuple_probe', 'clause': 'JOIN_TYPE', 'diff_type': 'join_type_changed'}]`
* **策略检查结果**:
  1. `PASS` - takes.ID values=[1, 2, 3, 4, 5, 6, 7, None], evidence={'sandbox_executed': True, 'student_exec_ok': True, 'student_exec_error': None, 'is_equivalent_on_generated_data': False, 'ordered_compare': False, 'row_count_match': False, 'standard_row_count': 8, 'student_row_count': 7, 'columns_match': True, 'column_names_match': True, 'standard_columns': ['name', 'course_id'], 'student_columns': ['name', 'course_id'], 'standard_duplicate_row_count': 0, 'student_duplicate_row_count': 0, 'suspected_cartesian_product': False, 'only_in_standard_sample': [('Dave', None)], 'only_in_student_sample': [], 'standard_sample_rows': [('Alice', 1), ('Bob', 2), ('Carol', 3), ('Dave', 4), ('Alice', 5)], 'student_sample_rows': [('Alice', 1), ('Bob', 2), ('Carol', 3), ('Dave', 4), ('Alice', 5)], 'ast_diffs': [{'standard_side': 'LEFT', 'student_side': 'INNER', 'right_table': 'takes', 'standard_sql': 'LEFT JOIN takes ON student.id = takes.id', 'student_sql': 'INNER JOIN takes ON student.id = takes.id', 'clause': 'JOIN_TYPE', 'diff_type': 'join_type_changed', 'column': None, 'table': 'takes'}], 'generation_tactics': [{'tactic': 'outer_join_dangling_tuple_probe', 'clause': 'JOIN_TYPE', 'diff_type': 'join_type_changed'}]}
  2. `PASS` - expected KP=join-left, actual=['join-left']
* **动态生成的数据集**:
```json
{
  "student": [
    {
      "ID": 1,
      "name": "Alice",
      "dept_name": "Comp. Sci."
    },
    {
      "ID": 2,
      "name": "Bob",
      "dept_name": "Math"
    },
    {
      "ID": 3,
      "name": "Carol",
      "dept_name": "Physics"
    },
    {
      "ID": 4,
      "name": "Dave",
      "dept_name": "History"
    },
    {
      "ID": 5,
      "name": "Alice",
      "dept_name": "Comp. Sci."
    },
    {
      "ID": 6,
      "name": "Bob",
      "dept_name": "Math"
    },
    {
      "ID": 7,
      "name": "Carol",
      "dept_name": "Physics"
    },
    {
      "ID": 8,
      "name": "Dave",
      "dept_name": "History"
    }
  ],
  "takes": [
    {
      "ID": 1,
      "course_id": 1
    },
    {
      "ID": 2,
      "course_id": 2
    },
    {
      "ID": 3,
      "course_id": 3
    },
    {
      "ID": 4,
      "course_id": 4
    },
    {
      "ID": 5,
      "course_id": 5
    },
    {
      "ID": 6,
      "course_id": 6
    },
    {
      "ID": 7,
      "course_id": 7
    },
    {
      "ID": null,
      "course_id": 8
    }
  ]
}
```
* **标准输出样本**: `[('Alice', 1), ('Bob', 2), ('Carol', 3), ('Dave', 4), ('Alice', 5)]`
* **学生输出样本**: `[('Alice', 1), ('Bob', 2), ('Carol', 3), ('Dave', 4), ('Alice', 5)]`

### GROUP BY 分组粒度错
* **策略说明**：系统对每张表默认生成 4~8 行，并在分组列上填充多个不同的异构分类键。这能保证当学生把分组字段写错（例如按 `building` 错写为按 `dept_name` 分组）时，各组 of 聚合与累加组合必然发生错位，导致求和或计数数组与标答不等价。
* **Schema**: `instructor(ID, name, dept_name, salary, building)`
* **标准答案 SQL**:
```sql
SELECT SUM(salary) FROM instructor GROUP BY dept_name;
```
* **学生作答 SQL**:
```sql
SELECT SUM(salary) FROM instructor GROUP BY building;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['group-by']`
* **AST 差异子树**: `[{'standard_sql': 'GROUP BY dept_name', 'student_sql': 'GROUP BY building', 'clause': 'GROUP BY', 'diff_type': 'group_by_changed', 'column': None, 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'group_cardinality_probe', 'clause': 'GROUP BY', 'diff_type': 'group_by_changed'}]`
* **策略检查结果**:
  1. `PASS` - expected KP=group-by, actual=['group-by']
* **动态生成的数据集**:
```json
{
  "instructor": [
    {
      "ID": 1,
      "name": "Alice",
      "dept_name": 1,
      "salary": 1,
      "building": "building_1"
    },
    {
      "ID": 2,
      "name": "Bob",
      "dept_name": 1,
      "salary": 2,
      "building": "building_2"
    },
    {
      "ID": 3,
      "name": "Carol",
      "dept_name": 2,
      "salary": 3,
      "building": "building_3"
    },
    {
      "ID": 4,
      "name": "Dave",
      "dept_name": 2,
      "salary": 4,
      "building": "building_4"
    },
    {
      "ID": 5,
      "name": "Alice",
      "dept_name": 3,
      "salary": 5,
      "building": "building_5"
    },
    {
      "ID": 6,
      "name": "Bob",
      "dept_name": 3,
      "salary": 6,
      "building": "building_6"
    },
    {
      "ID": 7,
      "name": "Carol",
      "dept_name": 4,
      "salary": 7,
      "building": "building_7"
    },
    {
      "ID": 8,
      "name": "Dave",
      "dept_name": 4,
      "salary": 8,
      "building": "building_8"
    }
  ]
}
```
* **标准输出样本**: `[(3,), (7,), (11,), (15,)]`
* **学生输出样本**: `[(1,), (2,), (3,), (4,), (5,)]`

### HAVING SUM 聚合边界三态
* **策略说明**：由于 HAVING 过滤发生在分组聚合之后，不能直接改写基表单行数据。系统将记录按分组归类，并分别对各组数据做三态控制，使各分组的聚合 `SUM` 目标值精确达到 $c + 1$（阳性通过）、$c$（临界差异）和 $c - 1$（阴性过滤）。再除以组内行数 $k$ 填充回单行记录中，激活 HAVING 谓词边界过滤。
* **Schema**: `instructor(ID, name, dept_name, salary)`
* **标准答案 SQL**:
```sql
SELECT dept_name FROM instructor GROUP BY dept_name HAVING SUM(salary) > 80000;
```
* **学生作答 SQL**:
```sql
SELECT dept_name FROM instructor GROUP BY dept_name HAVING SUM(salary) < 80000;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['having']`
* **AST 差异子树**: `[{'standard_sql': 'HAVING SUM(salary) > 80000', 'student_sql': 'HAVING SUM(salary) < 80000', 'clause': 'HAVING', 'diff_type': 'having_changed', 'column': None, 'table': None}, {'column': 'salary', 'standard_op': 'GT', 'student_op': 'LT', 'value': 80000, 'student_value': 80000, 'values': None, 'student_values': None, 'standard_sql': 'SUM(salary) > 80000', 'student_sql': 'SUM(salary) < 80000', 'clause': 'PREDICATE', 'diff_type': 'comparison_operator_changed', 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'aggregate_boundary_probe', 'clause': 'HAVING', 'diff_type': 'having_changed'}, {'tactic': 'comparison_boundary_tristate', 'clause': 'PREDICATE', 'diff_type': 'comparison_operator_changed'}]`
* **策略检查结果**:
  1. `PASS` - SUM metrics={1: 80001.0, 2: 80000.0, 3: 79999.0, 4: 80007}, boundary=80000
  2. `PASS` - expected KP=having, actual=['having']
* **动态生成的数据集**:
```json
{
  "instructor": [
    {
      "ID": 1,
      "name": "Alice",
      "dept_name": 1,
      "salary": 40000.5
    },
    {
      "ID": 2,
      "name": "Bob",
      "dept_name": 1,
      "salary": 40000.5
    },
    {
      "ID": 3,
      "name": "Carol",
      "dept_name": 2,
      "salary": 40000.0
    },
    {
      "ID": 4,
      "name": "Dave",
      "dept_name": 2,
      "salary": 40000.0
    },
    {
      "ID": 5,
      "name": "Alice",
      "dept_name": 3,
      "salary": 39999.5
    },
    {
      "ID": 6,
      "name": "Bob",
      "dept_name": 3,
      "salary": 39999.5
    },
    {
      "ID": 7,
      "name": "Carol",
      "dept_name": 4,
      "salary": 7
    },
    {
      "ID": 8,
      "name": "Dave",
      "dept_name": 4,
      "salary": 80000
    }
  ]
}
```
* **标准输出样本**: `[('1',), ('4',)]`
* **学生输出样本**: `[('3',)]`

### HAVING AVG 聚合边界三态
* **策略说明**：同 SUM 策略。控制每个分组的 `AVG`（均值）结果值，使其分别精确达到 $[c+1, c, c-1]$。数据生成时，直接使该分组内所有行的数值列均等于对应的目标值，使其平均值精确被控，引爆 HAVING 边界判断差异。
* **Schema**: `instructor(ID, name, dept_name, salary)`
* **标准答案 SQL**:
```sql
SELECT dept_name FROM instructor GROUP BY dept_name HAVING AVG(salary) > 50000;
```
* **学生作答 SQL**:
```sql
SELECT dept_name FROM instructor GROUP BY dept_name HAVING AVG(salary) < 50000;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['having']`
* **AST 差异子树**: `[{'standard_sql': 'HAVING AVG(salary) > 50000', 'student_sql': 'HAVING AVG(salary) < 50000', 'clause': 'HAVING', 'diff_type': 'having_changed', 'column': None, 'table': None}, {'column': 'salary', 'standard_op': 'GT', 'student_op': 'LT', 'value': 50000, 'student_value': 50000, 'values': None, 'student_values': None, 'standard_sql': 'AVG(salary) > 50000', 'student_sql': 'AVG(salary) < 50000', 'clause': 'PREDICATE', 'diff_type': 'comparison_operator_changed', 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'aggregate_boundary_probe', 'clause': 'HAVING', 'diff_type': 'having_changed'}, {'tactic': 'comparison_boundary_tristate', 'clause': 'PREDICATE', 'diff_type': 'comparison_operator_changed'}]`
* **策略检查结果**:
  1. `PASS` - AVG metrics={1: 50001.0, 2: 50000.0, 3: 49999.0, 4: 25003.5}, boundary=50000
  2. `PASS` - expected KP=having, actual=['having']
* **动态生成的数据集**:
```json
{
  "instructor": [
    {
      "ID": 1,
      "name": "Alice",
      "dept_name": 1,
      "salary": 50000
    },
    {
      "ID": 2,
      "name": "Bob",
      "dept_name": 1,
      "salary": 50002
    },
    {
      "ID": 3,
      "name": "Carol",
      "dept_name": 2,
      "salary": 49999
    },
    {
      "ID": 4,
      "name": "Dave",
      "dept_name": 2,
      "salary": 50001
    },
    {
      "ID": 5,
      "name": "Alice",
      "dept_name": 3,
      "salary": 49998
    },
    {
      "ID": 6,
      "name": "Bob",
      "dept_name": 3,
      "salary": 50000
    },
    {
      "ID": 7,
      "name": "Carol",
      "dept_name": 4,
      "salary": 7
    },
    {
      "ID": 8,
      "name": "Dave",
      "dept_name": 4,
      "salary": 50000
    }
  ]
}
```
* **标准输出样本**: `[('1',)]`
* **学生输出样本**: `[('3',), ('4',)]`

### HAVING MIN 聚合边界三态
* **策略说明**：控制每个分组的 `MIN`（极小值）结果值，使其分别达到 $[c+1, c, c-1]$。数据干预时，将该组 Row 0 设为目标值 `T`，组内其余行均设为 `T + 1`，保证该组的最小值精确锁定在 `T`，校验极值过滤逻辑。
* **Schema**: `instructor(ID, name, dept_name, salary)`
* **标准答案 SQL**:
```sql
SELECT dept_name FROM instructor GROUP BY dept_name HAVING MIN(salary) > 30000;
```
* **学生作答 SQL**:
```sql
SELECT dept_name FROM instructor GROUP BY dept_name HAVING MIN(salary) < 30000;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['having']`
* **AST 差异子树**: `[{'standard_sql': 'HAVING MIN(salary) > 30000', 'student_sql': 'HAVING MIN(salary) < 30000', 'clause': 'HAVING', 'diff_type': 'having_changed', 'column': None, 'table': None}, {'column': 'salary', 'standard_op': 'GT', 'student_op': 'LT', 'value': 30000, 'student_value': 30000, 'values': None, 'student_values': None, 'standard_sql': 'MIN(salary) > 30000', 'student_sql': 'MIN(salary) < 30000', 'clause': 'PREDICATE', 'diff_type': 'comparison_operator_changed', 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'aggregate_boundary_probe', 'clause': 'HAVING', 'diff_type': 'having_changed'}, {'tactic': 'comparison_boundary_tristate', 'clause': 'PREDICATE', 'diff_type': 'comparison_operator_changed'}]`
* **策略检查结果**:
  1. `PASS` - MIN metrics={1: 30001, 2: 30000, 3: 29999, 4: 7}, boundary=30000
  2. `PASS` - expected KP=having, actual=['having']
* **动态生成的数据集**:
```json
{
  "instructor": [
    {
      "ID": 1,
      "name": "Alice",
      "dept_name": 1,
      "salary": 30001
    },
    {
      "ID": 2,
      "name": "Bob",
      "dept_name": 1,
      "salary": 30002
    },
    {
      "ID": 3,
      "name": "Carol",
      "dept_name": 2,
      "salary": 30000
    },
    {
      "ID": 4,
      "name": "Dave",
      "dept_name": 2,
      "salary": 30001
    },
    {
      "ID": 5,
      "name": "Alice",
      "dept_name": 3,
      "salary": 29999
    },
    {
      "ID": 6,
      "name": "Bob",
      "dept_name": 3,
      "salary": 30000
    },
    {
      "ID": 7,
      "name": "Carol",
      "dept_name": 4,
      "salary": 7
    },
    {
      "ID": 8,
      "name": "Dave",
      "dept_name": 4,
      "salary": 30000
    }
  ]
}
```
* **标准输出样本**: `[('1',)]`
* **学生输出样本**: `[('3',), ('4',)]`

### HAVING MAX 聚合边界三态
* **策略说明**：控制每个分组的 `MAX`（极大值）结果值，使其分别达到 $[c+1, c, c-1]$。数据干预时，将该组 Row 0 设为目标值 `T`，组内其余行均设为 `T - 1`，保证该组的最大值精确锁定在 `T`，校验极值过滤逻辑。
* **Schema**: `instructor(ID, name, dept_name, salary)`
* **标准答案 SQL**:
```sql
SELECT dept_name FROM instructor GROUP BY dept_name HAVING MAX(salary) > 90000;
```
* **学生作答 SQL**:
```sql
SELECT dept_name FROM instructor GROUP BY dept_name HAVING MAX(salary) < 90000;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['having']`
* **AST 差异子树**: `[{'standard_sql': 'HAVING MAX(salary) > 90000', 'student_sql': 'HAVING MAX(salary) < 90000', 'clause': 'HAVING', 'diff_type': 'having_changed', 'column': None, 'table': None}, {'column': 'salary', 'standard_op': 'GT', 'student_op': 'LT', 'value': 90000, 'student_value': 90000, 'values': None, 'student_values': None, 'standard_sql': 'MAX(salary) > 90000', 'student_sql': 'MAX(salary) < 90000', 'clause': 'PREDICATE', 'diff_type': 'comparison_operator_changed', 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'aggregate_boundary_probe', 'clause': 'HAVING', 'diff_type': 'having_changed'}, {'tactic': 'comparison_boundary_tristate', 'clause': 'PREDICATE', 'diff_type': 'comparison_operator_changed'}]`
* **策略检查结果**:
  1. `PASS` - MAX metrics={1: 90001, 2: 90000, 3: 89999, 4: 90000}, boundary=90000
  2. `PASS` - expected KP=having, actual=['having']
* **动态生成的数据集**:
```json
{
  "instructor": [
    {
      "ID": 1,
      "name": "Alice",
      "dept_name": 1,
      "salary": 90001
    },
    {
      "ID": 2,
      "name": "Bob",
      "dept_name": 1,
      "salary": 90000
    },
    {
      "ID": 3,
      "name": "Carol",
      "dept_name": 2,
      "salary": 90000
    },
    {
      "ID": 4,
      "name": "Dave",
      "dept_name": 2,
      "salary": 89999
    },
    {
      "ID": 5,
      "name": "Alice",
      "dept_name": 3,
      "salary": 89999
    },
    {
      "ID": 6,
      "name": "Bob",
      "dept_name": 3,
      "salary": 89998
    },
    {
      "ID": 7,
      "name": "Carol",
      "dept_name": 4,
      "salary": 7
    },
    {
      "ID": 8,
      "name": "Dave",
      "dept_name": 4,
      "salary": 90000
    }
  ]
}
```
* **标准输出样本**: `[('1',)]`
* **学生输出样本**: `[('3',)]`

### HAVING COUNT 组大小三态
* **策略说明**：对于行记录数限制（`COUNT`），无法通过改写数值列生效。本策略直接重排和限制分组列的物理键分配，使得各分组的物理行数（即组大小）分别等于 $[c+1, c, c-1]$，从而当比较行数写错时直接被沙盒过滤拦截。
* **Schema**: `instructor(ID, name, dept_name, salary)`
* **标准答案 SQL**:
```sql
SELECT dept_name FROM instructor GROUP BY dept_name HAVING COUNT(ID) >= 2;
```
* **学生作答 SQL**:
```sql
SELECT dept_name FROM instructor GROUP BY dept_name HAVING COUNT(ID) > 2;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['having']`
* **AST 差异子树**: `[{'standard_sql': 'HAVING COUNT(id) >= 2', 'student_sql': 'HAVING COUNT(id) > 2', 'clause': 'HAVING', 'diff_type': 'having_changed', 'column': None, 'table': None}, {'column': 'ID', 'standard_op': 'GTE', 'student_op': 'GT', 'value': 2, 'student_value': 2, 'values': None, 'student_values': None, 'standard_sql': 'COUNT(id) >= 2', 'student_sql': 'COUNT(id) > 2', 'clause': 'PREDICATE', 'diff_type': 'comparison_operator_changed', 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'aggregate_boundary_probe', 'clause': 'HAVING', 'diff_type': 'having_changed'}, {'tactic': 'comparison_boundary_tristate', 'clause': 'PREDICATE', 'diff_type': 'comparison_operator_changed'}]`
* **策略检查结果**:
  1. `PASS` - COUNT metrics={'Comp. Sci.': 2, 'Math': 3, 'Physics': 1, 'Biology': 2}, boundary=2
  2. `PASS` - expected KP=having, actual=['having']
* **动态生成的数据集**:
```json
{
  "instructor": [
    {
      "ID": 2,
      "name": "Alice",
      "dept_name": "Comp. Sci.",
      "salary": 1
    },
    {
      "ID": 3,
      "name": "Bob",
      "dept_name": "Comp. Sci.",
      "salary": 2
    },
    {
      "ID": 1,
      "name": "Carol",
      "dept_name": "Math",
      "salary": 3
    },
    {
      "ID": 4,
      "name": "Dave",
      "dept_name": "Math",
      "salary": 4
    },
    {
      "ID": 5,
      "name": "Alice",
      "dept_name": "Math",
      "salary": 5
    },
    {
      "ID": 6,
      "name": "Bob",
      "dept_name": "Physics",
      "salary": 6
    },
    {
      "ID": 7,
      "name": "Carol",
      "dept_name": "Biology",
      "salary": 7
    },
    {
      "ID": 8,
      "name": "Dave",
      "dept_name": "Biology",
      "salary": 8
    }
  ]
}
```
* **标准输出样本**: `[('Biology',), ('Comp. Sci.',), ('Math',)]`
* **学生输出样本**: `[('Math',)]`

### ORDER BY 有序精确比对
* **策略说明**：提取排序关键字并生成具有单调递增/递减特征的数据序列。一旦检测到 SQL 中包含 `ORDER BY`，沙盒结果比对模块将关闭无序的频次比对（Counter），转为严格的顺序列表比对（`std_rows == stu_rows`），使排序方向或排序列写错直接暴露。
* **Schema**: `course(course_id, title, dept_name, credits)`
* **标准答案 SQL**:
```sql
SELECT title FROM course ORDER BY credits DESC;
```
* **学生作答 SQL**:
```sql
SELECT title FROM course ORDER BY credits ASC;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['order-by']`
* **AST 差异子树**: `[{'standard_sql': 'ORDER BY credits DESC', 'student_sql': 'ORDER BY credits ASC', 'clause': 'ORDER BY', 'diff_type': 'order_by_changed', 'column': None, 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'ordered_compare_probe', 'clause': 'ORDER BY', 'diff_type': 'order_by_changed'}]`
* **策略检查结果**:
  1. `PASS` - evidence={'sandbox_executed': True, 'student_exec_ok': True, 'student_exec_error': None, 'is_equivalent_on_generated_data': False, 'ordered_compare': True, 'row_count_match': True, 'standard_row_count': 8, 'student_row_count': 8, 'columns_match': True, 'column_names_match': True, 'standard_columns': ['title'], 'student_columns': ['title'], 'standard_duplicate_row_count': 0, 'student_duplicate_row_count': 0, 'suspected_cartesian_product': False, 'only_in_standard_sample': [], 'only_in_student_sample': [], 'standard_sample_rows': [('Engineer__row_006__row_006',), ('Sales Manager__row_004__row_004',), ('Marketing Lead__row_005__row_005',), ('Engineer__row_002__row_002',), ('Analyst__row_003__row_003',)], 'student_sample_rows': [('Analyst__row_007__row_007',), ('Sales Manager__row_000__row_000',), ('Marketing Lead__row_001__row_001',), ('Engineer__row_002__row_002',), ('Analyst__row_003__row_003',)], 'ast_diffs': [{'standard_sql': 'ORDER BY credits DESC', 'student_sql': 'ORDER BY credits ASC', 'clause': 'ORDER BY', 'diff_type': 'order_by_changed', 'column': None, 'table': None}], 'generation_tactics': [{'tactic': 'ordered_compare_probe', 'clause': 'ORDER BY', 'diff_type': 'order_by_changed'}]}
  2. `PASS` - expected KP=order-by, actual=['order-by']
* **动态生成的数据集**:
```json
{
  "course": [
    {
      "course_id": 1,
      "title": "Sales Manager__row_000__row_000",
      "dept_name": "Comp. Sci.",
      "credits": 1
    },
    {
      "course_id": 2,
      "title": "Marketing Lead__row_001__row_001",
      "dept_name": "Math",
      "credits": 1
    },
    {
      "course_id": 3,
      "title": "Engineer__row_002__row_002",
      "dept_name": "Physics",
      "credits": 3
    },
    {
      "course_id": 4,
      "title": "Analyst__row_003__row_003",
      "dept_name": "History",
      "credits": 3
    },
    {
      "course_id": 5,
      "title": "Sales Manager__row_004__row_004",
      "dept_name": "Comp. Sci.",
      "credits": 5
    },
    {
      "course_id": 6,
      "title": "Marketing Lead__row_005__row_005",
      "dept_name": "Math",
      "credits": 5
    },
    {
      "course_id": 7,
      "title": "Engineer__row_006__row_006",
      "dept_name": "Physics",
      "credits": 7
    },
    {
      "course_id": 8,
      "title": "Analyst__row_007__row_007",
      "dept_name": "History",
      "credits": null
    }
  ]
}
```
* **标准输出样本**: `[('Engineer__row_006__row_006',), ('Sales Manager__row_004__row_004',), ('Marketing Lead__row_005__row_005',), ('Engineer__row_002__row_002',), ('Analyst__row_003__row_003',)]`
* **学生输出样本**: `[('Analyst__row_007__row_007',), ('Sales Manager__row_000__row_000',), ('Marketing Lead__row_001__row_001',), ('Engineer__row_002__row_002',), ('Analyst__row_003__row_003',)]`

### LIMIT 行数边界
* **策略说明**：限制输出的元组行数。通过沙盒直接验证 `LIMIT` 或 `OFFSET` 参数的数值偏差，让多取或少取数据的学生 SQL 产生行数不等。
* **Schema**: `course(course_id, title, dept_name, credits)`
* **标准答案 SQL**:
```sql
SELECT title FROM course LIMIT 3;
```
* **学生作答 SQL**:
```sql
SELECT title FROM course LIMIT 5;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['limit']`
* **AST 差异子树**: `[{'standard_sql': 'LIMIT 3', 'student_sql': 'LIMIT 5', 'clause': 'LIMIT', 'diff_type': 'limit_changed', 'column': None, 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'limit_row_count_probe', 'clause': 'LIMIT', 'diff_type': 'limit_changed'}]`
* **策略检查结果**:
  1. `PASS` - standard/student=3/5
  2. `PASS` - expected KP=limit, actual=['limit']
* **动态生成的数据集**:
```json
{
  "course": [
    {
      "course_id": 1,
      "title": "Sales Manager",
      "dept_name": "Comp. Sci.",
      "credits": 1
    },
    {
      "course_id": 2,
      "title": "Marketing Lead",
      "dept_name": "Math",
      "credits": 2
    },
    {
      "course_id": 3,
      "title": "Engineer",
      "dept_name": "Physics",
      "credits": 3
    },
    {
      "course_id": 4,
      "title": "Analyst",
      "dept_name": "History",
      "credits": 4
    },
    {
      "course_id": 5,
      "title": "Sales Manager",
      "dept_name": "Comp. Sci.",
      "credits": 5
    },
    {
      "course_id": 6,
      "title": "Marketing Lead",
      "dept_name": "Math",
      "credits": 6
    },
    {
      "course_id": 7,
      "title": "Engineer",
      "dept_name": "Physics",
      "credits": 7
    },
    {
      "course_id": 8,
      "title": "Analyst",
      "dept_name": "History",
      "credits": 8
    }
  ]
}
```
* **标准输出样本**: `[('Sales Manager',), ('Marketing Lead',), ('Engineer',)]`
* **学生输出样本**: `[('Sales Manager',), ('Marketing Lead',), ('Engineer',), ('Analyst',), ('Sales Manager',)]`

### 子查询内外层值域重合
* **策略说明**：提取子查询中的关联列，在父子表之间建立主外键或数据范围的重合（对齐 ID 共享值池，打通拓扑通路）。再配合子查询内部的过滤谓词，强行在子表中构造阳性重合行（满足过滤）、阴性混淆行（不满足过滤）和悬浮行，触发子查询的过滤选择权，暴露内外层值域逻辑错。
* **Schema**: `student(ID, name, dept_name); takes(ID, course_id, year)`
* **标准答案 SQL**:
```sql
SELECT name FROM student WHERE ID IN (SELECT ID FROM takes WHERE year = 2017);
```
* **学生作答 SQL**:
```sql
SELECT name FROM student WHERE ID NOT IN (SELECT ID FROM takes WHERE year = 2017);
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['where', 'subquery-scalar']`
* **AST 差异子树**: `[{'standard_sql': 'id IN (SELECT id FROM takes WHERE year = 2017)', 'student_sql': 'NOT id IN (SELECT id FROM takes WHERE year = 2017)', 'clause': 'WHERE', 'diff_type': 'where_changed', 'column': None, 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'predicate_counterexample', 'clause': 'WHERE', 'diff_type': 'where_changed'}]`
* **策略检查结果**:
  1. `PASS` - student.ID=[1, 2, 3, 4, 5, 6, 7, 8], takes.ID=[1, 2014, 2016, 2019, 2022, 2023, 2024, None]
  2. `PASS` - expected KP=where, actual=['where', 'subquery-scalar']
* **动态生成的数据集**:
```json
{
  "student": [
    {
      "ID": 1,
      "name": "Alice__predicate_row_000",
      "dept_name": "Comp. Sci."
    },
    {
      "ID": 2,
      "name": "Bob__predicate_row_001",
      "dept_name": "Math"
    },
    {
      "ID": 3,
      "name": "Carol__predicate_row_002",
      "dept_name": "Physics"
    },
    {
      "ID": 4,
      "name": "Dave__predicate_row_003",
      "dept_name": "History"
    },
    {
      "ID": 5,
      "name": "Alice__predicate_row_004",
      "dept_name": "Comp. Sci."
    },
    {
      "ID": 6,
      "name": "Bob__predicate_row_005",
      "dept_name": "Math"
    },
    {
      "ID": 7,
      "name": "Carol__predicate_row_006",
      "dept_name": "Physics"
    },
    {
      "ID": 8,
      "name": "Dave__predicate_row_007",
      "dept_name": "History"
    }
  ],
  "takes": [
    {
      "ID": null,
      "course_id": 1,
      "year": 1
    },
    {
      "ID": 1,
      "course_id": 1,
      "year": 2
    },
    {
      "ID": 2019,
      "course_id": 2,
      "year": 3
    },
    {
      "ID": 2016,
      "course_id": 2,
      "year": 4
    },
    {
      "ID": 2022,
      "course_id": 3,
      "year": 5
    },
    {
      "ID": 2024,
      "course_id": 3,
      "year": 6
    },
    {
      "ID": 2014,
      "course_id": 4,
      "year": 7
    },
    {
      "ID": 2023,
      "course_id": 4,
      "year": 8
    }
  ]
}
```
* **标准输出样本**: `[]`
* **学生输出样本**: `[('Alice__predicate_row_000',), ('Bob__predicate_row_001',), ('Carol__predicate_row_002',), ('Dave__predicate_row_003',), ('Alice__predicate_row_004',)]`

### 相关子查询内外层关联
* **策略说明**：静态扫描子查询的过滤条件，识别并提取外层主表的引用列（如 `t.ID = s.ID`）。在生成数据时，确保被引用的主表 ID 与子查询中关联表的 ID 发生数据交叉（在内层表生成对应相关变量的多态数据），以便在子查询被多次关联扫描时，逻辑漏洞能被沙盒识别。
* **Schema**: `student(ID, name, dept_name); takes(ID, course_id, year)`
* **标准答案 SQL**:
```sql
SELECT name FROM student s WHERE EXISTS (SELECT 1 FROM takes t WHERE t.ID = s.ID AND t.year = 2017);
```
* **学生作答 SQL**:
```sql
SELECT name FROM student s WHERE EXISTS (SELECT 1 FROM takes t WHERE t.ID = s.ID AND t.year = 2018);
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['where', 'subquery-correlated']`
* **AST 差异子树**: `[{'standard_sql': 'EXISTS(SELECT 1 FROM takes AS t WHERE t.id = s.id AND t.year = 2017)', 'student_sql': 'EXISTS(SELECT 1 FROM takes AS t WHERE t.id = s.id AND t.year = 2018)', 'clause': 'WHERE', 'diff_type': 'where_changed', 'column': None, 'table': None}, {'standard_sql': 'EXISTS(SELECT 1 FROM takes AS t WHERE t.id = s.id AND t.year = 2017)', 'student_sql': 'EXISTS(SELECT 1 FROM takes AS t WHERE t.id = s.id AND t.year = 2018)', 'clause': 'CORRELATED SUBQUERY', 'diff_type': 'correlated_predicate_changed', 'column': None, 'table': None}, {'subquery_depth': 1, 'standard_sql': 'SELECT 1 FROM takes AS t WHERE t.id = s.id AND t.year = 2017', 'student_sql': 'SELECT 1 FROM takes AS t WHERE t.id = s.id AND t.year = 2018', 'clause': 'CORRELATED SUBQUERY', 'diff_type': 'correlated_predicate_changed', 'column': None, 'table': None}, {'standard_sql': 't.year = 2017', 'student_sql': 't.year = 2018', 'subquery_depth': 1, 'clause': 'WHERE', 'diff_type': 'where_changed', 'column': None, 'table': None}, {'column': 'year', 'standard_op': 'EQ', 'student_op': 'EQ', 'value': 2017, 'student_value': 2018, 'values': None, 'student_values': None, 'standard_sql': 't.year = 2017', 'student_sql': 't.year = 2018', 'subquery_depth': 1, 'clause': 'PREDICATE', 'diff_type': 'literal_changed', 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'predicate_counterexample', 'clause': 'WHERE', 'diff_type': 'where_changed'}, {'tactic': 'predicate_counterexample', 'clause': 'WHERE', 'diff_type': 'where_changed'}, {'tactic': 'literal_boundary_tristate', 'clause': 'PREDICATE', 'diff_type': 'literal_changed'}]`
* **策略检查结果**:
  1. `PASS` - student.ID=[1, 2, 3, 4, 5, 6, 7, 8], takes.ID=[1, 2, 3, 4, 5, 6, 7, 8]
  2. `PASS` - takes.year values=[2017, 2018, 2016, 2018, 5, 6, 7, 2018], required=[2016, 2017, 2018]
  3. `PASS` - expected KP=subquery-correlated, actual=['where', 'subquery-correlated']
* **动态生成的数据集**:
```json
{
  "student": [
    {
      "ID": 1,
      "name": "Alice__predicate_row_000",
      "dept_name": "Comp. Sci."
    },
    {
      "ID": 2,
      "name": "Bob__predicate_row_001",
      "dept_name": "Math"
    },
    {
      "ID": 3,
      "name": "Carol__predicate_row_002",
      "dept_name": "Physics"
    },
    {
      "ID": 4,
      "name": "Dave__predicate_row_003",
      "dept_name": "History"
    },
    {
      "ID": 5,
      "name": "Alice__predicate_row_004",
      "dept_name": "Comp. Sci."
    },
    {
      "ID": 6,
      "name": "Bob__predicate_row_005",
      "dept_name": "Math"
    },
    {
      "ID": 7,
      "name": "Carol__predicate_row_006",
      "dept_name": "Physics"
    },
    {
      "ID": 8,
      "name": "Dave__predicate_row_007",
      "dept_name": "History"
    }
  ],
  "takes": [
    {
      "ID": 1,
      "course_id": 1,
      "year": 2017
    },
    {
      "ID": 2,
      "course_id": 2,
      "year": 2018
    },
    {
      "ID": 3,
      "course_id": 3,
      "year": 2016
    },
    {
      "ID": 4,
      "course_id": 4,
      "year": 2018
    },
    {
      "ID": 5,
      "course_id": 5,
      "year": 5
    },
    {
      "ID": 6,
      "course_id": 6,
      "year": 6
    },
    {
      "ID": 7,
      "course_id": 7,
      "year": 7
    },
    {
      "ID": 8,
      "course_id": 8,
      "year": 2018
    }
  ]
}
```
* **标准输出样本**: `[('Alice__predicate_row_000',)]`
* **学生输出样本**: `[('Bob__predicate_row_001',), ('Dave__predicate_row_003',), ('Dave__predicate_row_007',)]`

### 集合操作 UNION 去重差异
* **策略说明**：在集合算子（`UNION` / `UNION ALL`）左右两侧 of 子查询结果中生成完全重复的行。当学生混淆 `UNION`（集合自动去重）与 `UNION ALL`（保留所有重复行）时，学生 SQL 的执行输出将包含额外的重复行，行数随之分化。
* **Schema**: `course(course_id, title, dept_name, credits)`
* **标准答案 SQL**:
```sql
SELECT title FROM course WHERE dept_name = 'Math' UNION SELECT title FROM course WHERE dept_name = 'Physics';
```
* **学生作答 SQL**:
```sql
SELECT title FROM course WHERE dept_name = 'Math' UNION ALL SELECT title FROM course WHERE dept_name = 'Physics';
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['union']`
* **AST 差异子树**: `[{'standard_op': 'UNION', 'student_op': 'UNION', 'standard_modifier': 'DISTINCT', 'student_modifier': 'ALL', 'standard_sql': "SELECT title FROM course WHERE dept_name = 'Math' UNION SELECT title FROM course WHERE dept_name = 'Physics'", 'student_sql': "SELECT title FROM course WHERE dept_name = 'Math' UNION ALL SELECT title FROM course WHERE dept_name = 'Physics'", 'clause': 'UNION', 'diff_type': 'set_operator_changed', 'column': None, 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'set_operator_overlap_probe', 'clause': 'UNION', 'diff_type': 'set_operator_changed'}]`
* **策略检查结果**:
  1. `PASS` - expected KP=union, actual=['union']
* **动态生成的数据集**:
```json
{
  "course": [
    {
      "course_id": 1,
      "title": "Sales Manager",
      "dept_name": "Math",
      "credits": 1
    },
    {
      "course_id": 2,
      "title": "Sales Manager",
      "dept_name": "Math",
      "credits": 2
    },
    {
      "course_id": 3,
      "title": "Engineer",
      "dept_name": "Math",
      "credits": 3
    },
    {
      "course_id": 4,
      "title": "Analyst",
      "dept_name": "Physics",
      "credits": 4
    },
    {
      "course_id": 5,
      "title": "Sales Manager",
      "dept_name": "Comp. Sci.",
      "credits": 5
    },
    {
      "course_id": 6,
      "title": "Marketing Lead",
      "dept_name": "Math",
      "credits": 6
    },
    {
      "course_id": 7,
      "title": "Engineer",
      "dept_name": "Physics",
      "credits": 7
    },
    {
      "course_id": 8,
      "title": null,
      "dept_name": "not_Math",
      "credits": 8
    }
  ]
}
```
* **标准输出样本**: `[('Analyst',), ('Engineer',), ('Marketing Lead',), ('Sales Manager',)]`
* **学生输出样本**: `[('Sales Manager',), ('Sales Manager',), ('Engineer',), ('Marketing Lead',), ('Analyst',)]`

### 集合操作 INTERSECT 交集差异
* **策略说明**：在数据生成阶段，分别生成“仅满足左侧条件”、“仅满足右侧条件”以及“同时满足两侧条件”的记录。当学生错写集合操作符（如用 `UNION` 替代了 `INTERSECT`）时，沙盒执行结果将从交集空集或子集膨胀为并集，暴露逻辑错。
* **Schema**: `course(course_id, title, dept_name, credits)`
* **标准答案 SQL**:
```sql
SELECT title FROM course WHERE dept_name = 'Math' INTERSECT SELECT title FROM course WHERE credits > 3;
```
* **学生作答 SQL**:
```sql
SELECT title FROM course WHERE dept_name = 'Math' UNION SELECT title FROM course WHERE credits > 3;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['intersect']`
* **AST 差异子树**: `[{'standard_op': 'INTERSECT', 'student_op': 'UNION', 'standard_modifier': 'DISTINCT', 'student_modifier': 'DISTINCT', 'standard_sql': "SELECT title FROM course WHERE dept_name = 'Math' INTERSECT SELECT title FROM course WHERE credits > 3", 'student_sql': "SELECT title FROM course WHERE dept_name = 'Math' UNION SELECT title FROM course WHERE credits > 3", 'clause': 'INTERSECT', 'diff_type': 'set_operator_changed', 'column': None, 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'set_operator_overlap_probe', 'clause': 'INTERSECT', 'diff_type': 'set_operator_changed'}]`
* **策略检查结果**:
  1. `PASS` - expected KP=intersect, actual=['intersect']
* **动态生成的数据集**:
```json
{
  "course": [
    {
      "course_id": 1,
      "title": "Sales Manager",
      "dept_name": "Math",
      "credits": 3
    },
    {
      "course_id": 2,
      "title": "Sales Manager",
      "dept_name": "Math",
      "credits": 4
    },
    {
      "course_id": 3,
      "title": "Engineer",
      "dept_name": "Physics",
      "credits": 2
    },
    {
      "course_id": 4,
      "title": "Analyst",
      "dept_name": "History",
      "credits": 3
    },
    {
      "course_id": 5,
      "title": "Sales Manager",
      "dept_name": "Comp. Sci.",
      "credits": 5
    },
    {
      "course_id": 6,
      "title": "Marketing Lead",
      "dept_name": "Math",
      "credits": 6
    },
    {
      "course_id": 7,
      "title": "Engineer",
      "dept_name": "not_Math",
      "credits": 4
    },
    {
      "course_id": 8,
      "title": null,
      "dept_name": "Math",
      "credits": 3
    }
  ]
}
```
* **标准输出样本**: `[('Marketing Lead',), ('Sales Manager',)]`
* **学生输出样本**: `[(None,), ('Engineer',), ('Marketing Lead',), ('Sales Manager',)]`

### 集合操作 EXCEPT 排他差异
* **策略说明**：提取 EXCEPT 右侧的过滤条件并在数据中生成排他数据行。这能保证当学生漏写了 `EXCEPT` 差集排除逻辑时，学生 SQL 的输出中会多出本应该被剔除的行，打破等价性。
* **Schema**: `course(course_id, title, dept_name, credits)`
* **标准答案 SQL**:
```sql
SELECT title FROM course EXCEPT SELECT title FROM course WHERE dept_name = 'Physics';
```
* **学生作答 SQL**:
```sql
SELECT title FROM course;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['except', 'where']`
* **AST 差异子树**: `[{'standard_sql': "dept_name = 'Physics'", 'student_sql': '', 'clause': 'WHERE', 'diff_type': 'where_changed', 'column': None, 'table': None}, {'column': 'dept_name', 'op': 'EQ', 'value': 'Physics', 'value_is_null': False, 'sql': "dept_name = 'Physics'", 'node': EQ(
  this=Column(
    this=Identifier(this=dept_name, quoted=False)),
  expression=Literal(this='Physics', is_string=True)), 'standard_sql': "dept_name = 'Physics'", 'student_sql': '', 'clause': 'PREDICATE', 'diff_type': 'predicate_missing', 'table': None}, {'standard_op': 'EXCEPT', 'student_op': None, 'standard_modifier': 'DISTINCT', 'student_modifier': None, 'standard_sql': "SELECT title FROM course EXCEPT SELECT title FROM course WHERE dept_name = 'Physics'", 'student_sql': 'SELECT title FROM course', 'clause': 'EXCEPT', 'diff_type': 'set_operator_changed', 'column': None, 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'predicate_counterexample', 'clause': 'WHERE', 'diff_type': 'where_changed'}, {'tactic': 'predicate_positive_negative_probe', 'clause': 'PREDICATE', 'diff_type': 'predicate_missing'}, {'tactic': 'set_operator_overlap_probe', 'clause': 'EXCEPT', 'diff_type': 'set_operator_changed'}]`
* **策略检查结果**:
  1. `PASS` - expected KP=except, actual=['except', 'where']
* **动态生成的数据集**:
```json
{
  "course": [
    {
      "course_id": 1,
      "title": "Sales Manager__predicate_row_000",
      "dept_name": "Physics",
      "credits": 1
    },
    {
      "course_id": 2,
      "title": "Sales Manager__predicate_row_001",
      "dept_name": "Physics",
      "credits": 2
    },
    {
      "course_id": 3,
      "title": "Engineer__predicate_row_002",
      "dept_name": "Physics",
      "credits": 3
    },
    {
      "course_id": 4,
      "title": "Analyst__predicate_row_003",
      "dept_name": "History",
      "credits": 4
    },
    {
      "course_id": 5,
      "title": "Sales Manager__predicate_row_004",
      "dept_name": "Comp. Sci.",
      "credits": 5
    },
    {
      "course_id": 6,
      "title": "Marketing Lead__predicate_row_005",
      "dept_name": "Math",
      "credits": 6
    },
    {
      "course_id": 7,
      "title": "Engineer__predicate_row_006",
      "dept_name": "Physics",
      "credits": 7
    },
    {
      "course_id": 8,
      "title": null,
      "dept_name": "not_Physics",
      "credits": 8
    }
  ]
}
```
* **标准输出样本**: `[(None,), ('Analyst__predicate_row_003',), ('Marketing Lead__predicate_row_005',), ('Sales Manager__predicate_row_004',)]`
* **学生输出样本**: `[('Sales Manager__predicate_row_000',), ('Sales Manager__predicate_row_001',), ('Engineer__predicate_row_002',), ('Analyst__predicate_row_003',), ('Sales Manager__predicate_row_004',)]`

### CASE WHEN 分支边界三态
* **策略说明**：针对 CASE WHEN 块中的各个分支条件（如 `amount > 100`），分别产生满足三态边界（$c$、$c+1$、$c-1$）的测试数据，从而在沙盒执行时强制遍历所有计算和转换分支，校验条件边界的准确性。
* **Schema**: `sales(sale_id, category, amount)`
* **标准答案 SQL**:
```sql
SELECT category, SUM(CASE WHEN amount > 100 THEN amount ELSE 0 END) AS big_sales FROM sales GROUP BY category;
```
* **学生作答 SQL**:
```sql
SELECT category, SUM(CASE WHEN amount >= 100 THEN amount ELSE 0 END) AS big_sales FROM sales GROUP BY category;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['select-basic', 'case']`
* **AST 差异子树**: `[{'standard_sql': 'category, SUM(CASE WHEN amount > 100 THEN amount ELSE 0 END) AS big_sales', 'student_sql': 'category, SUM(CASE WHEN amount >= 100 THEN amount ELSE 0 END) AS big_sales', 'clause': 'SELECT', 'diff_type': 'projection_changed', 'column': None, 'table': None}, {'standard_sql': 'SUM(CASE WHEN amount > 100 THEN amount ELSE 0 END) AS big_sales', 'student_sql': '', 'position': 1, 'clause': 'SELECT', 'diff_type': 'column_dropped', 'column': 'amount', 'table': None}, {'standard_sql': '', 'student_sql': 'SUM(CASE WHEN amount >= 100 THEN amount ELSE 0 END) AS big_sales', 'position': 1, 'clause': 'SELECT', 'diff_type': 'column_added', 'column': 'amount', 'table': None}, {'column': 'amount', 'standard_op': 'GT', 'student_op': 'GTE', 'value': 100, 'student_value': 100, 'values': None, 'student_values': None, 'standard_sql': 'amount > 100', 'student_sql': 'amount >= 100', 'clause': 'PREDICATE', 'diff_type': 'comparison_operator_changed', 'table': None}, {'standard_sql': 'CASE WHEN amount > 100 THEN amount ELSE 0 END', 'student_sql': 'CASE WHEN amount >= 100 THEN amount ELSE 0 END', 'clause': 'CASE', 'diff_type': 'case_changed', 'column': None, 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'projection_shape_check', 'clause': 'SELECT', 'diff_type': 'projection_changed'}, {'tactic': 'projection_shape_check', 'clause': 'SELECT', 'diff_type': 'column_dropped'}, {'tactic': 'projection_shape_check', 'clause': 'SELECT', 'diff_type': 'column_added'}, {'tactic': 'comparison_boundary_tristate', 'clause': 'PREDICATE', 'diff_type': 'comparison_operator_changed'}, {'tactic': 'case_branch_probe', 'clause': 'CASE', 'diff_type': 'case_changed'}]`
* **策略检查结果**:
  1. `PASS` - sales.amount values=[100, 101, 99, 100, 5, 6, 7, 100], required=[99, 100, 101]
  2. `PASS` - expected KP=case, actual=['select-basic', 'case']
* **动态生成的数据集**:
```json
{
  "sales": [
    {
      "sale_id": 1,
      "category": "category_1",
      "amount": 100
    },
    {
      "sale_id": 2,
      "category": "category_2",
      "amount": 101
    },
    {
      "sale_id": 3,
      "category": "category_3",
      "amount": 99
    },
    {
      "sale_id": 4,
      "category": "category_4",
      "amount": 100
    },
    {
      "sale_id": 5,
      "category": "category_5",
      "amount": 5
    },
    {
      "sale_id": 6,
      "category": "category_6",
      "amount": 6
    },
    {
      "sale_id": 7,
      "category": "category_7",
      "amount": 7
    },
    {
      "sale_id": 8,
      "category": "category_8",
      "amount": 100
    }
  ]
}
```
* **标准输出样本**: `[('category_1', 0), ('category_2', 101), ('category_3', 0), ('category_4', 0), ('category_5', 0)]`
* **学生输出样本**: `[('category_1', 100), ('category_2', 101), ('category_3', 0), ('category_4', 100), ('category_5', 0)]`

### WINDOW 分区与排序数据
* **策略说明**：提取窗口函数的排序列与分区列，在数据行中产生乱序值和重复的分组值。如果学生在 `OVER` 子句中遗漏了 `PARTITION BY`，排序编号会出现全局自增而非分区独立重置的特征，导致数据不一致。
* **Schema**: `instructor(ID, name, dept_name, salary)`
* **标准答案 SQL**:
```sql
SELECT name, ROW_NUMBER() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS rank FROM instructor;
```
* **学生作答 SQL**:
```sql
SELECT name, ROW_NUMBER() OVER (ORDER BY salary DESC) AS rank FROM instructor;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['window-row-number', 'select-basic']`
* **AST 差异子树**: `[{'standard_sql': 'name, ROW_NUMBER() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS rank', 'student_sql': 'name, ROW_NUMBER() OVER (ORDER BY salary DESC) AS rank', 'clause': 'SELECT', 'diff_type': 'projection_changed', 'column': None, 'table': None}, {'standard_sql': 'ROW_NUMBER() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS rank', 'student_sql': '', 'position': 1, 'clause': 'SELECT', 'diff_type': 'column_dropped', 'column': 'dept_name', 'table': None}, {'standard_sql': '', 'student_sql': 'ROW_NUMBER() OVER (ORDER BY salary DESC) AS rank', 'position': 1, 'clause': 'SELECT', 'diff_type': 'column_added', 'column': 'salary', 'table': None}, {'standard_sql': 'ROW_NUMBER() OVER (PARTITION BY dept_name ORDER BY salary DESC)', 'student_sql': 'ROW_NUMBER() OVER (ORDER BY salary DESC)', 'clause': 'WINDOW', 'diff_type': 'window_over_changed', 'column': None, 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'projection_shape_check', 'clause': 'SELECT', 'diff_type': 'projection_changed'}, {'tactic': 'projection_shape_check', 'clause': 'SELECT', 'diff_type': 'column_dropped'}, {'tactic': 'projection_shape_check', 'clause': 'SELECT', 'diff_type': 'column_added'}, {'tactic': 'window_partition_order_probe', 'clause': 'WINDOW', 'diff_type': 'window_over_changed'}]`
* **策略检查结果**:
  1. `PASS` - dept group_counts={'dept_name_group_1': 3, 'dept_name_group_2': 3, 'dept_name_group_3': 2}, salaries=[1, 1, 3, 3, 5, 5, 7, 7]
  2. `PASS` - expected KP=window-row-number, actual=['window-row-number', 'select-basic']
* **动态生成的数据集**:
```json
{
  "instructor": [
    {
      "ID": 1,
      "name": "Alice",
      "dept_name": "dept_name_group_1",
      "salary": 1
    },
    {
      "ID": 2,
      "name": "Bob",
      "dept_name": "dept_name_group_1",
      "salary": 1
    },
    {
      "ID": 3,
      "name": "Carol",
      "dept_name": "dept_name_group_1",
      "salary": 3
    },
    {
      "ID": 4,
      "name": "Dave",
      "dept_name": "dept_name_group_2",
      "salary": 3
    },
    {
      "ID": 5,
      "name": "Alice",
      "dept_name": "dept_name_group_2",
      "salary": 5
    },
    {
      "ID": 6,
      "name": "Bob",
      "dept_name": "dept_name_group_2",
      "salary": 5
    },
    {
      "ID": 7,
      "name": "Carol",
      "dept_name": "dept_name_group_3",
      "salary": 7
    },
    {
      "ID": 8,
      "name": "Dave",
      "dept_name": "dept_name_group_3",
      "salary": 7
    }
  ]
}
```
* **标准输出样本**: `[('Carol', 1), ('Alice', 2), ('Bob', 3), ('Alice', 1), ('Bob', 2)]`
* **学生输出样本**: `[('Carol', 1), ('Dave', 2), ('Alice', 3), ('Bob', 4), ('Carol', 5)]`

### CTE 基表约束传递
* **策略说明**：回溯 CTE（WITH 表达式）内部引用的底层基表并针对这些基表进行三态造数，而拒绝直接预造 CTE 临时表。CTE 定义与外层 `JOIN` 均交给 SQLite 原生执行，确保基表约束能自然传导至最外层，校验 CTE 基表约束传递性。
* **Schema**: `works(company_name, person_name, salary); company(company_name, city)`
* **标准答案 SQL**:
```sql
WITH big_co AS (SELECT company_name FROM company WHERE city = 'Beijing') SELECT person_name, salary FROM works JOIN big_co ON works.company_name = big_co.company_name WHERE salary > 10000;
```
* **学生作答 SQL**:
```sql
WITH big_co AS (SELECT company_name FROM company WHERE city = 'Beijing') SELECT person_name, salary FROM works JOIN big_co ON works.company_name = big_co.company_name WHERE salary < 10000;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['where']`
* **AST 差异子树**: `[{'standard_sql': 'salary > 10000', 'student_sql': 'salary < 10000', 'clause': 'WHERE', 'diff_type': 'where_changed', 'column': None, 'table': None}, {'column': 'salary', 'standard_op': 'GT', 'student_op': 'LT', 'value': 10000, 'student_value': 10000, 'values': None, 'student_values': None, 'standard_sql': 'salary > 10000', 'student_sql': 'salary < 10000', 'clause': 'PREDICATE', 'diff_type': 'comparison_operator_changed', 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'predicate_counterexample', 'clause': 'WHERE', 'diff_type': 'where_changed'}, {'tactic': 'comparison_boundary_tristate', 'clause': 'PREDICATE', 'diff_type': 'comparison_operator_changed'}]`
* **策略检查结果**:
  1. `PASS` - works.salary values=[10000, 10001, 9999, 10000, 5, 6, 7, 10000], required=[9999, 10000, 10001]
  2. `PASS` - expected KP=where, actual=['where']
* **动态生成的数据集**:
```json
{
  "works": [
    {
      "company_name": "Alice",
      "person_name": "Alice__predicate_row_000__cte_row_000",
      "salary": 10000
    },
    {
      "company_name": "Bob",
      "person_name": "Bob__predicate_row_001__cte_row_001",
      "salary": 10001
    },
    {
      "company_name": "Carol",
      "person_name": "Carol__predicate_row_002__cte_row_002",
      "salary": 9999
    },
    {
      "company_name": "Dave",
      "person_name": "Dave__predicate_row_003__cte_row_003",
      "salary": 10000
    },
    {
      "company_name": "Alice",
      "person_name": "Alice__predicate_row_004__cte_row_004",
      "salary": 5
    },
    {
      "company_name": "Bob",
      "person_name": "Bob__predicate_row_005__cte_row_005",
      "salary": 6
    },
    {
      "company_name": "Carol",
      "person_name": "Carol__predicate_row_006__cte_row_006",
      "salary": 7
    },
    {
      "company_name": "Dave",
      "person_name": "Dave__predicate_row_007__cte_row_007",
      "salary": 10000
    }
  ],
  "company": [
    {
      "company_name": "Alice",
      "city": "Beijing"
    },
    {
      "company_name": "Bob",
      "city": "Beijing"
    },
    {
      "company_name": "Carol",
      "city": "city_3"
    },
    {
      "company_name": "Dave",
      "city": "city_4"
    },
    {
      "company_name": "Alice",
      "city": "city_5"
    },
    {
      "company_name": "Bob",
      "city": "city_6"
    },
    {
      "company_name": "Carol",
      "city": "city_7"
    },
    {
      "company_name": "Dave",
      "city": "not_Beijing"
    }
  ]
}
```
* **标准输出样本**: `[('Bob__predicate_row_001__cte_row_001', 10001)]`
* **学生输出样本**: `[('Alice__predicate_row_004__cte_row_004', 5), ('Bob__predicate_row_005__cte_row_005', 6)]`

### 递归 CTE 终止边界与沙盒熔断
* **策略说明**：静态检测 `WITH RECURSIVE` 结构。除了在自引用序列上产生离散数据校验终止边界外，还在 SQLite 沙盒执行时启用虚拟机周期计数器（Progress Handler），将指令周期锁定在 10 万个以内。一旦死循环立即熔断，防止系统被学生错误 SQL 挂死。
* **Schema**: `dummy(id)`
* **标准答案 SQL**:
```sql
WITH RECURSIVE nums AS (SELECT 1 AS n UNION ALL SELECT n + 1 AS n FROM nums WHERE n < 3) SELECT n FROM nums;
```
* **学生作答 SQL**:
```sql
WITH RECURSIVE nums AS (SELECT 1 AS n UNION ALL SELECT n + 1 AS n FROM nums WHERE n < 5) SELECT n FROM nums;
```
* **沙盒判定等价性**: `False`
* **归因 KP**: `['cte-recursive', 'union', 'where']`
* **AST 差异子树**: `[{'standard_sql': 'n < 3', 'student_sql': 'n < 5', 'clause': 'WHERE', 'diff_type': 'where_changed', 'column': None, 'table': None}, {'column': 'n', 'standard_op': 'LT', 'student_op': 'LT', 'value': 3, 'student_value': 5, 'values': None, 'student_values': None, 'standard_sql': 'n < 3', 'student_sql': 'n < 5', 'clause': 'PREDICATE', 'diff_type': 'literal_changed', 'table': None}, {'standard_sql': 'nums AS (SELECT 1 AS n UNION ALL SELECT n + 1 AS n FROM nums WHERE n < 3)', 'student_sql': 'nums AS (SELECT 1 AS n UNION ALL SELECT n + 1 AS n FROM nums WHERE n < 5)', 'standard_recursive': True, 'student_recursive': True, 'clause': 'CTE_RECURSIVE', 'diff_type': 'recursive_cte_changed', 'column': None, 'table': None}]`
* **差异驱动造数策略**: `[{'tactic': 'predicate_counterexample', 'clause': 'WHERE', 'diff_type': 'where_changed'}, {'tactic': 'literal_boundary_tristate', 'clause': 'PREDICATE', 'diff_type': 'literal_changed'}, {'tactic': 'recursive_cte_boundary_probe', 'clause': 'CTE_RECURSIVE', 'diff_type': 'recursive_cte_changed'}]`
* **策略检查结果**:
  1. `PASS` - standard/student=3/5, error=None
  2. `PASS` - expected KP=cte-recursive, actual=['cte-recursive', 'union', 'where']
* **动态生成的数据集**:
```json
{
  "dummy": [
    {
      "id": 1
    },
    {
      "id": 2
    },
    {
      "id": 3
    },
    {
      "id": 4
    },
    {
      "id": 5
    },
    {
      "id": 6
    },
    {
      "id": 7
    },
    {
      "id": 8
    }
  ]
}
```
* **标准输出样本**: `[(1,), (2,), (3,)]`
* **学生输出样本**: `[(1,), (2,), (3,), (4,), (5,)]`

## 七、阶段一 SQL 算子覆盖与策略总结表

本表按当前主链路整理：`generate_and_compare` 负责动态造数、沙盒执行与变分证据，`evidence_weights_from_observation` 负责阶段一归因合并。

| 算子类别 | SQL 表现 | Sqlglot AST 节点 | 动态造数策略 | 变分/归因机制 | KP ID |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 选择 | WHERE | `exp.Where`, `exp.Comparison` | 谓词边界三态 `[c, c+1, c-1]` | 替换/移除 WHERE | `where` |
| 空值过滤 | `IS NULL` / `= NULL` | `exp.Is`, `exp.Null` | 注入 `None` 行，区分 `IS NULL` 与 `= NULL` | 伴随 WHERE 证据归因 | `comp-null` |
| 投影 | SELECT | `exp.Select` | 按引用列生成并校验列结构 | 不单独变分，随数据证据归因 | `select-basic` |
| 去重 | DISTINCT | `exp.Distinct` | 对 DISTINCT 投影列注入重复值 | 不单独变分，随行数/重复证据归因 | `distinct` |
| 连接 | JOIN ON / USING | `exp.Join` | 共享键池、同组外键漂移、外连接悬浮元组 | JOIN ON 条件替换 | `join-on`, `join-inner`, `join-left`, `join-right`, `join-full` |
| 分组 | GROUP BY | `exp.Group` | 生成多组分类键，暴露分组粒度错误 | 替换 GROUP BY | `group-by` |
| 分组过滤 | HAVING | `exp.Having` | SUM/AVG/MIN/MAX 聚合三态；COUNT 组大小三态 | 替换/移除 HAVING | `having` |
| 排序 | ORDER BY | `exp.Order` | 生成单调/乱序值并启用有序精确比对 | 替换 ORDER BY | `order-by` |
| 限制 | LIMIT / OFFSET | `exp.Limit`, `exp.Offset` | 校验标准/学生输出行数边界 | 替换 LIMIT/OFFSET | `limit` |
| 简单子查询 | IN / EXISTS / 标量子查询 | `exp.Subquery`, `exp.In`, `exp.Exists` | 父子表值域重合与子查询过滤探针 | 随 WHERE 子句变分 | `subquery-scalar`, `subquery-in`, `subquery-exists` |
| 相关子查询 | 引用外层表的子查询 | `exp.Subquery`, `exp.Exists` | 内外层关联列交叉数据与过滤边界 | 随 WHERE 子句变分 | `subquery-correlated` |
| 简单 CTE | WITH | `exp.CTE` | 只生成底层基表，CTE 由 SQLite 原生执行 | 暂不单独变分 | `cte` |
| 递归 CTE | WITH RECURSIVE | `exp.CTE`, `exp.With` | 递归终止边界与 SQLite progress handler 熔断 | 暂不单独变分 | `cte-recursive` |
| 并集 | UNION / UNION ALL | `exp.Union` | 两侧谓词联合造数并校验去重差异 | 集合算子差异归因 | `union` |
| 交集 | INTERSECT | `exp.Intersect` | 构造左侧、右侧、交集三类记录 | 集合算子差异归因 | `intersect` |
| 差集 | EXCEPT | `exp.Except` | 抽取右侧过滤条件并生成排他数据行 | 集合算子差异归因 | `except` |
| 条件分支 | CASE WHEN | `exp.Case` | CASE 条件边界三态并遍历分支 | CASE 差异归因 | `case` |
| 窗口函数 | OVER | `exp.Window` | 重复分区键与乱序排序值，验证分区/排名 | 窗口 OVER 差异归因 | `window-row-number` |