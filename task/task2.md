# 阶段一：SQL 算子完备性测试与典型案例分析报告

本报告为**系统完备性测试报告**。涵盖了数据库系统 DQL 查询中的 **12 类核心算子/子句**，以及 **4 类复杂的混合算子场景**。
每个案例都通过 **结构传感器(AST)、数据传感器(沙盒)、变分隔离传感器(Mutation)** 三位一体的完整评估流程进行诊断分析，以验证系统的完备性。

---

## Case 1: Individual - SELECT (Lacking Column)

* **数据库 Schema**: `course(course_id, title, dept_name, credits)`
* **标准答案 SQL**:
  ```sql
  SELECT title, credits FROM course;
  ```
* **学生作答 SQL**:
  ```sql
  SELECT title FROM course;
  ```
* **沙盒判定等价性**: `False` (执行报错: `None`)

### 1. 结构传感器 (AST Structural Analysis)
| 结构特征项 | 标准答案 SQL 值 | 学生作答 SQL 值 | 匹配状态 |
| :--- | :--- | :--- | :--- |
| SELECT 子句 (has_select) | `True` | `True` | ✅ 匹配 |
| SELECT 投影列数 (projection_count) | `2` | `1` | ❌ 不匹配 |


| 子句位置 | 标准答案子句内容 | 学生作答子句内容 | 一致性 |
| :--- | :--- | :--- | :--- |
| SELECT | `title, credits` | `title` | ❌ 不匹配 |

### 2. 数据传感器 (Dynamic Database & Sandbox Run)
#### (1) 动态生成的数据集 (Test Database)
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
#### (2) 沙盒执行输出 (Rows)
* **标准输出行数/数据**: `8 行` -> `[('Sales Manager', 1), ('Marketing Lead', 2), ('Engineer', 3), ('Analyst', 4), ('Sales Manager', 5)]`
* **学生输出行数/数据**: `8 行` -> `[('Sales Manager',), ('Marketing Lead',), ('Engineer',), ('Analyst',), ('Sales Manager',)]`

### 3. 变分隔离传感器 (Mutation Isolation Testing)
```json
{
  "enabled": true,
  "summary": {
    "executed": 1,
    "fixed_by_replacement": 1,
    "remove_kept_correct": 0
  },
  "tests": [
    {
      "clause": "SELECT",
      "knowledge_point_id": "select-basic",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "SELECT"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT \"title\", \"credits\" FROM \"course\";",
      "replacement_sqlite": "SELECT \"title\", \"credits\" FROM \"course\";",
      "replacement_exec_ok": true,
      "replacement_equivalent": true,
      "fixed_by_replacement": true,
      "removal_sql": null,
      "removal_sqlite": null,
      "removal_exec_ok": false,
      "removed_student_clause_equivalent": null,
      "error": null
    }
  ]
}
```

### 4. 诊断与知识点归因结果 (Attributions)
```json
[
  {
    "knowledge_point_id": "select-basic",
    "l1_code": "KP_BASIC",
    "l2_code": "PROJ_COL",
    "clause": "SELECT",
    "error_type": "missing_partial",
    "severity": 0.88,
    "confidence": 1.0,
    "detail": "把学生 SQL 的 SELECT 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
    "evidence": [
      {
        "source": "E_AST",
        "signal": "projection_count",
        "detail": "学生 SELECT 输出列数量少于标准答案，可能漏选目标列或表达式",
        "weight": 0.7
      },
      {
        "source": "E_AST",
        "signal": "projection_mismatch",
        "detail": "SELECT 投影表达式与标准答案不一致，可能漏选、错选或改写了目标列。",
        "weight": 0.76
      },
      {
        "source": "E_MUT",
        "signal": "replacement_fixed:SELECT",
        "detail": "把学生 SQL 的 SELECT 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
        "weight": 0.88
      }
    ]
  }
]
```

---


## Case 2: Individual - WHERE (Predicate Operator Mismatch)

* **数据库 Schema**: `course(course_id, title, dept_name, credits)`
* **标准答案 SQL**:
  ```sql
  SELECT title FROM course WHERE credits > 3;
  ```
* **学生作答 SQL**:
  ```sql
  SELECT title FROM course WHERE credits >= 3;
  ```
* **沙盒判定等价性**: `False` (执行报错: `None`)

### 1. 结构传感器 (AST Structural Analysis)
| 结构特征项 | 标准答案 SQL 值 | 学生作答 SQL 值 | 匹配状态 |
| :--- | :--- | :--- | :--- |
| SELECT 子句 (has_select) | `True` | `True` | ✅ 匹配 |
| SELECT 投影列数 (projection_count) | `1` | `1` | ✅ 匹配 |
| WHERE 过滤 (has_where) | `True` | `True` | ✅ 匹配 |


| 子句位置 | 标准答案子句内容 | 学生作答子句内容 | 一致性 |
| :--- | :--- | :--- | :--- |
| SELECT | `title` | `title` | ✅ 匹配 |
| WHERE | `WHERE credits > 3` | `WHERE credits >= 3` | ❌ 不匹配 |

### 2. 数据传感器 (Dynamic Database & Sandbox Run)
#### (1) 动态生成的数据集 (Test Database)
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
#### (2) 沙盒执行输出 (Rows)
* **标准输出行数/数据**: `4 行` -> `[('Marketing Lead__predicate_row_001',), ('Sales Manager__predicate_row_004',), ('Marketing Lead__predicate_row_005',), ('Engineer__predicate_row_006',)]`
* **学生输出行数/数据**: `7 行` -> `[('Sales Manager__predicate_row_000',), ('Marketing Lead__predicate_row_001',), ('Analyst__predicate_row_003',), ('Sales Manager__predicate_row_004',), ('Marketing Lead__predicate_row_005',)]`

### 3. 变分隔离传感器 (Mutation Isolation Testing)
```json
{
  "enabled": true,
  "summary": {
    "executed": 1,
    "fixed_by_replacement": 1,
    "remove_kept_correct": 0
  },
  "tests": [
    {
      "clause": "WHERE",
      "knowledge_point_id": "where",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "WHERE"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT \"title\" FROM \"course\" WHERE \"credits\" > 3;",
      "replacement_sqlite": "SELECT \"title\" FROM \"course\" WHERE \"credits\" > 3;",
      "replacement_exec_ok": true,
      "replacement_equivalent": true,
      "fixed_by_replacement": true,
      "removal_sql": "SELECT \"title\" FROM \"course\";",
      "removal_sqlite": "SELECT \"title\" FROM \"course\";",
      "removal_exec_ok": true,
      "removed_student_clause_equivalent": false,
      "error": null
    }
  ]
}
```

### 4. 诊断与知识点归因结果 (Attributions)
```json
[
  {
    "knowledge_point_id": "where",
    "l1_code": "KP_FILTER",
    "l2_code": "COMP_VAL",
    "clause": "WHERE",
    "error_type": "logical",
    "severity": 0.88,
    "confidence": 0.714,
    "detail": "把学生 SQL 的 WHERE 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
    "evidence": [
      {
        "source": "E_MUT",
        "signal": "same_clause_mismatch:WHERE",
        "detail": "WHERE 结构存在，但表达式、边界、操作符或布尔逻辑与目标不一致。",
        "weight": 0.66
      }
    ]
  }
]
```

---


## Case 3: Individual - DISTINCT (Lacking DISTINCT)

* **数据库 Schema**: `takes(ID, course_id, sec_id, semester, year, grade)`
* **标准答案 SQL**:
  ```sql
  SELECT DISTINCT course_id FROM takes;
  ```
* **学生作答 SQL**:
  ```sql
  SELECT course_id FROM takes;
  ```
* **沙盒判定等价性**: `False` (执行报错: `None`)

### 1. 结构传感器 (AST Structural Analysis)
| 结构特征项 | 标准答案 SQL 值 | 学生作答 SQL 值 | 匹配状态 |
| :--- | :--- | :--- | :--- |
| SELECT 子句 (has_select) | `True` | `True` | ✅ 匹配 |
| SELECT 投影列数 (projection_count) | `1` | `1` | ✅ 匹配 |
| DISTINCT 去重 (has_distinct) | `True` | `False` | ❌ 不匹配 |

### 2. 数据传感器 (Dynamic Database & Sandbox Run)
#### (1) 动态生成的数据集 (Test Database)
```json
{
  "takes": [
    {
      "ID": 1,
      "course_id": 1,
      "sec_id": 1,
      "semester": "Fall",
      "year": "2024-01-01",
      "grade": 1
    },
    {
      "ID": 2,
      "course_id": 1,
      "sec_id": 2,
      "semester": "Spring",
      "year": "2024-01-02",
      "grade": 2
    },
    {
      "ID": 3,
      "course_id": 3,
      "sec_id": 3,
      "semester": "Summer",
      "year": "2024-01-03",
      "grade": 3
    },
    {
      "ID": 4,
      "course_id": 4,
      "sec_id": 4,
      "semester": "Winter",
      "year": "2024-01-04",
      "grade": 4
    },
    {
      "ID": 5,
      "course_id": 5,
      "sec_id": 5,
      "semester": "Fall",
      "year": "2024-01-05",
      "grade": 5
    },
    {
      "ID": 6,
      "course_id": 6,
      "sec_id": 6,
      "semester": "Spring",
      "year": "2024-01-06",
      "grade": 6
    },
    {
      "ID": 7,
      "course_id": 7,
      "sec_id": 7,
      "semester": "Summer",
      "year": "2024-01-07",
      "grade": 7
    },
    {
      "ID": 8,
      "course_id": 8,
      "sec_id": 8,
      "semester": "Winter",
      "year": "2024-01-08",
      "grade": 8
    }
  ]
}
```
#### (2) 沙盒执行输出 (Rows)
* **标准输出行数/数据**: `7 行` -> `[(1,), (3,), (4,), (5,), (6,)]`
* **学生输出行数/数据**: `8 行` -> `[(1,), (1,), (3,), (4,), (5,)]`

### 3. 变分隔离传感器 (Mutation Isolation Testing)
```json
{
  "enabled": true,
  "summary": {
    "executed": 1,
    "fixed_by_replacement": 1,
    "remove_kept_correct": 0
  },
  "tests": [
    {
      "clause": "DISTINCT",
      "knowledge_point_id": "distinct",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "DISTINCT"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT DISTINCT \"course_id\" FROM \"takes\";",
      "replacement_sqlite": "SELECT DISTINCT \"course_id\" FROM \"takes\";",
      "replacement_exec_ok": true,
      "replacement_equivalent": true,
      "fixed_by_replacement": true,
      "removal_sql": null,
      "removal_sqlite": null,
      "removal_exec_ok": false,
      "removed_student_clause_equivalent": null,
      "error": null
    }
  ]
}
```

### 4. 诊断与知识点归因结果 (Attributions)
```json
[
  {
    "knowledge_point_id": "distinct",
    "l1_code": "KP_BASIC",
    "l2_code": "DISTINCT_SET",
    "clause": "DISTINCT",
    "error_type": "lacking",
    "severity": 0.9,
    "confidence": 0.802,
    "detail": "标准答案需要 DISTINCT 去重，但学生 SQL 缺少去重",
    "evidence": [
      {
        "source": "E_AST",
        "signal": "target_missing:distinct",
        "detail": "目标知识点包含 distinct，但学生 SQL 没有对应结构。",
        "weight": 0.88
      }
    ]
  }
]
```

---


## Case 4: Individual - JOIN ON (Join Key Mismatch)

* **数据库 Schema**: `student(ID, name, dept_name); advisor(s_ID, i_ID)`
* **标准答案 SQL**:
  ```sql
  SELECT student.name FROM student JOIN advisor ON student.ID = advisor.s_ID;
  ```
* **学生作答 SQL**:
  ```sql
  SELECT student.name FROM student JOIN advisor ON student.ID = advisor.i_ID;
  ```
* **沙盒判定等价性**: `False` (执行报错: `None`)

### 1. 结构传感器 (AST Structural Analysis)
| 结构特征项 | 标准答案 SQL 值 | 学生作答 SQL 值 | 匹配状态 |
| :--- | :--- | :--- | :--- |
| SELECT 子句 (has_select) | `True` | `True` | ✅ 匹配 |
| SELECT 投影列数 (projection_count) | `1` | `1` | ✅ 匹配 |
| JOIN 连接数 (join_count) | `1` | `1` | ✅ 匹配 |
| JOIN ON 条件 (has_join_on) | `True` | `True` | ✅ 匹配 |


| 子句位置 | 标准答案子句内容 | 学生作答子句内容 | 一致性 |
| :--- | :--- | :--- | :--- |
| JOIN ON | `student.id = advisor.s_id` | `student.id = advisor.i_id` | ❌ 不匹配 |
| SELECT | `student.name` | `student.name` | ✅ 匹配 |

### 2. 数据传感器 (Dynamic Database & Sandbox Run)
#### (1) 动态生成的数据集 (Test Database)
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
#### (2) 沙盒执行输出 (Rows)
* **标准输出行数/数据**: `8 行` -> `[('Alice',), ('Bob',), ('Carol',), ('Dave',), ('Alice',)]`
* **学生输出行数/数据**: `4 行` -> `[('Alice',), ('Carol',), ('Alice',), ('Carol',)]`

### 3. 变分隔离传感器 (Mutation Isolation Testing)
```json
{
  "enabled": true,
  "summary": {
    "executed": 1,
    "fixed_by_replacement": 1,
    "remove_kept_correct": 0
  },
  "tests": [
    {
      "clause": "JOIN ON",
      "knowledge_point_id": "join-on",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "JOIN ON"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT \"student\".\"name\" FROM \"student\" JOIN \"advisor\" ON \"student\".\"id\" = \"advisor\".\"s_id\";",
      "replacement_sqlite": "SELECT \"student\".\"name\" FROM \"student\" JOIN \"advisor\" ON \"student\".\"id\" = \"advisor\".\"s_id\";",
      "replacement_exec_ok": true,
      "replacement_equivalent": true,
      "fixed_by_replacement": true,
      "removal_sql": null,
      "removal_sqlite": null,
      "removal_exec_ok": false,
      "removed_student_clause_equivalent": null,
      "error": null
    }
  ]
}
```

### 4. 诊断与知识点归因结果 (Attributions)
```json
[
  {
    "knowledge_point_id": "join-on",
    "l1_code": "KP_JOIN",
    "l2_code": "JOIN_ON",
    "clause": "JOIN ON",
    "error_type": "logical",
    "severity": 0.88,
    "confidence": 0.949,
    "detail": "把学生 SQL 的 JOIN ON 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
    "evidence": [
      {
        "source": "E_MUT",
        "signal": "same_clause_mismatch:JOIN ON",
        "detail": "JOIN ON 结构存在但连接键与目标不一致，可能造成表之间的数据流断裂。",
        "weight": 0.86
      },
      {
        "source": "E_MUT",
        "signal": "replace:join_on",
        "detail": "JOIN ON 条件与标准答案不一致，连接谓词是优先隔离方向",
        "weight": 0.86
      }
    ]
  }
]
```

---


## Case 5: Individual - GROUP BY (Grouping Attribute Mismatch / 分组列写错)

* **数据库 Schema**: `instructor(ID, name, dept_name, salary, building)`
* **标准答案 SQL**:
  ```sql
  SELECT SUM(salary) FROM instructor GROUP BY dept_name;
  ```
* **学生作答 SQL**:
  ```sql
  SELECT SUM(salary) FROM instructor GROUP BY building;
  ```
* **沙盒判定等价性**: `False` (执行报错: `None`)

### 1. 结构传感器 (AST Structural Analysis)
| 结构特征项 | 标准答案 SQL 值 | 学生作答 SQL 值 | 匹配状态 |
| :--- | :--- | :--- | :--- |
| SELECT 子句 (has_select) | `True` | `True` | ✅ 匹配 |
| SELECT 投影列数 (projection_count) | `1` | `1` | ✅ 匹配 |
| GROUP BY 分组 (has_group) | `True` | `True` | ✅ 匹配 |


| 子句位置 | 标准答案子句内容 | 学生作答子句内容 | 一致性 |
| :--- | :--- | :--- | :--- |
| GROUP BY | `GROUP BY dept_name` | `GROUP BY building` | ❌ 不匹配 |
| SELECT | `SUM(salary)` | `SUM(salary)` | ✅ 匹配 |

### 2. 数据传感器 (Dynamic Database & Sandbox Run)
#### (1) 动态生成的数据集 (Test Database)
```json
{
  "instructor": [
    {
      "ID": 1,
      "name": "Alice",
      "dept_name": "__group_0_0__",
      "salary": 1,
      "building": "__group_1_0__"
    },
    {
      "ID": 2,
      "name": "Bob",
      "dept_name": "__group_0_0__",
      "salary": 2,
      "building": "__group_1_1__"
    },
    {
      "ID": 3,
      "name": "Carol",
      "dept_name": "__group_0_1__",
      "salary": 3,
      "building": "__group_1_0__"
    },
    {
      "ID": 4,
      "name": "Dave",
      "dept_name": "__group_0_1__",
      "salary": 4,
      "building": "__group_1_1__"
    },
    {
      "ID": 5,
      "name": "Alice",
      "dept_name": "__group_0_2__",
      "salary": 5,
      "building": "__group_1_0__"
    },
    {
      "ID": 6,
      "name": "Bob",
      "dept_name": "__group_0_2__",
      "salary": 6,
      "building": "__group_1_1__"
    },
    {
      "ID": 7,
      "name": "Carol",
      "dept_name": "__group_0_3__",
      "salary": 7,
      "building": "__group_1_0__"
    },
    {
      "ID": 8,
      "name": "Dave",
      "dept_name": "__group_0_3__",
      "salary": 8,
      "building": "__group_1_1__"
    }
  ]
}
```
#### (2) 沙盒执行输出 (Rows)
* **标准输出行数/数据**: `4 行` -> `[(3,), (7,), (11,), (15,)]`
* **学生输出行数/数据**: `2 行` -> `[(16,), (20,)]`

### 3. 变分隔离传感器 (Mutation Isolation Testing)
```json
{
  "enabled": true,
  "summary": {
    "executed": 1,
    "fixed_by_replacement": 1,
    "remove_kept_correct": 0
  },
  "tests": [
    {
      "clause": "GROUP BY",
      "knowledge_point_id": "group-by",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "GROUP BY"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT SUM(\"salary\") FROM \"instructor\" GROUP BY \"dept_name\";",
      "replacement_sqlite": "SELECT SUM(\"salary\") FROM \"instructor\" GROUP BY \"dept_name\";",
      "replacement_exec_ok": true,
      "replacement_equivalent": true,
      "fixed_by_replacement": true,
      "removal_sql": "SELECT SUM(\"salary\") FROM \"instructor\";",
      "removal_sqlite": "SELECT SUM(\"salary\") FROM \"instructor\";",
      "removal_exec_ok": true,
      "removed_student_clause_equivalent": false,
      "error": null
    }
  ]
}
```

### 4. 诊断与知识点归因结果 (Attributions)
```json
[
  {
    "knowledge_point_id": "group-by",
    "l1_code": "KP_AGG",
    "l2_code": "GB_SIMPLE",
    "clause": "GROUP BY",
    "error_type": "logical",
    "severity": 0.88,
    "confidence": 0.826,
    "detail": "把学生 SQL 的 GROUP BY 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
    "evidence": [
      {
        "source": "E_MUT",
        "signal": "same_clause_mismatch:GROUP BY",
        "detail": "GROUP BY 结构存在，但表达式、边界、操作符或布尔逻辑与目标不一致。",
        "weight": 0.66
      },
      {
        "source": "E_MUT",
        "signal": "replace:group-by",
        "detail": "GROUP BY 子句与标准答案不一致，分组粒度是优先隔离方向",
        "weight": 0.62
      }
    ]
  }
]
```

---


## Case 6: Individual - HAVING (Having Predicate Mismatch)

* **数据库 Schema**: `instructor(ID, name, dept_name, salary)`
* **标准答案 SQL**:
  ```sql
  SELECT dept_name, SUM(salary) FROM instructor GROUP BY dept_name HAVING SUM(salary) > 80000;
  ```
* **学生作答 SQL**:
  ```sql
  SELECT dept_name, SUM(salary) FROM instructor GROUP BY dept_name HAVING SUM(salary) < 80000;
  ```
* **沙盒判定等价性**: `False` (执行报错: `None`)

### 1. 结构传感器 (AST Structural Analysis)
| 结构特征项 | 标准答案 SQL 值 | 学生作答 SQL 值 | 匹配状态 |
| :--- | :--- | :--- | :--- |
| SELECT 子句 (has_select) | `True` | `True` | ✅ 匹配 |
| SELECT 投影列数 (projection_count) | `2` | `2` | ✅ 匹配 |
| GROUP BY 分组 (has_group) | `True` | `True` | ✅ 匹配 |
| HAVING 分组后筛选 (has_having) | `True` | `True` | ✅ 匹配 |


| 子句位置 | 标准答案子句内容 | 学生作答子句内容 | 一致性 |
| :--- | :--- | :--- | :--- |
| GROUP BY | `GROUP BY dept_name` | `GROUP BY dept_name` | ✅ 匹配 |
| HAVING | `HAVING SUM(salary) > 80000` | `HAVING SUM(salary) < 80000` | ❌ 不匹配 |
| SELECT | `dept_name, SUM(salary)` | `dept_name, SUM(salary)` | ✅ 匹配 |

### 2. 数据传感器 (Dynamic Database & Sandbox Run)
#### (1) 动态生成的数据集 (Test Database)
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
#### (2) 沙盒执行输出 (Rows)
* **标准输出行数/数据**: `2 行` -> `[('1', 80001), ('4', 80007)]`
* **学生输出行数/数据**: `1 行` -> `[('3', 79999)]`

### 3. 变分隔离传感器 (Mutation Isolation Testing)
```json
{
  "enabled": true,
  "summary": {
    "executed": 1,
    "fixed_by_replacement": 1,
    "remove_kept_correct": 0
  },
  "tests": [
    {
      "clause": "HAVING",
      "knowledge_point_id": "having",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "HAVING"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT \"dept_name\", SUM(\"salary\") FROM \"instructor\" GROUP BY \"dept_name\" HAVING SUM(\"salary\") > 80000;",
      "replacement_sqlite": "SELECT \"dept_name\", SUM(\"salary\") FROM \"instructor\" GROUP BY \"dept_name\" HAVING SUM(\"salary\") > 80000;",
      "replacement_exec_ok": true,
      "replacement_equivalent": true,
      "fixed_by_replacement": true,
      "removal_sql": "SELECT \"dept_name\", SUM(\"salary\") FROM \"instructor\" GROUP BY \"dept_name\";",
      "removal_sqlite": "SELECT \"dept_name\", SUM(\"salary\") FROM \"instructor\" GROUP BY \"dept_name\";",
      "removal_exec_ok": true,
      "removed_student_clause_equivalent": false,
      "error": null
    }
  ]
}
```

### 4. 诊断与知识点归因结果 (Attributions)
```json
[
  {
    "knowledge_point_id": "having",
    "l1_code": "KP_AGG",
    "l2_code": "HV_SIMPLE",
    "clause": "HAVING",
    "error_type": "logical",
    "severity": 0.88,
    "confidence": 0.714,
    "detail": "把学生 SQL 的 HAVING 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
    "evidence": [
      {
        "source": "E_MUT",
        "signal": "same_clause_mismatch:HAVING",
        "detail": "HAVING 结构存在，但表达式、边界、操作符或布尔逻辑与目标不一致。",
        "weight": 0.66
      }
    ]
  }
]
```

---


## Case 7: Individual - ORDER BY (Sorting Direction Mismatch)

* **数据库 Schema**: `course(course_id, title, dept_name, credits)`
* **标准答案 SQL**:
  ```sql
  SELECT title FROM course ORDER BY credits DESC;
  ```
* **学生作答 SQL**:
  ```sql
  SELECT title FROM course ORDER BY credits ASC;
  ```
* **沙盒判定等价性**: `False` (执行报错: `None`)

### 1. 结构传感器 (AST Structural Analysis)
| 结构特征项 | 标准答案 SQL 值 | 学生作答 SQL 值 | 匹配状态 |
| :--- | :--- | :--- | :--- |
| SELECT 子句 (has_select) | `True` | `True` | ✅ 匹配 |
| SELECT 投影列数 (projection_count) | `1` | `1` | ✅ 匹配 |
| ORDER BY 排序 (has_order) | `True` | `True` | ✅ 匹配 |


| 子句位置 | 标准答案子句内容 | 学生作答子句内容 | 一致性 |
| :--- | :--- | :--- | :--- |
| ORDER BY | `ORDER BY credits DESC` | `ORDER BY credits ASC` | ❌ 不匹配 |
| SELECT | `title` | `title` | ✅ 匹配 |

### 2. 数据传感器 (Dynamic Database & Sandbox Run)
#### (1) 动态生成的数据集 (Test Database)
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
#### (2) 沙盒执行输出 (Rows)
* **标准输出行数/数据**: `8 行` -> `[('Engineer__row_006__row_006',), ('Sales Manager__row_004__row_004',), ('Marketing Lead__row_005__row_005',), ('Engineer__row_002__row_002',), ('Analyst__row_003__row_003',)]`
* **学生输出行数/数据**: `8 行` -> `[('Analyst__row_007__row_007',), ('Sales Manager__row_000__row_000',), ('Marketing Lead__row_001__row_001',), ('Engineer__row_002__row_002',), ('Analyst__row_003__row_003',)]`

### 3. 变分隔离传感器 (Mutation Isolation Testing)
```json
{
  "enabled": true,
  "summary": {
    "executed": 1,
    "fixed_by_replacement": 1,
    "remove_kept_correct": 0
  },
  "tests": [
    {
      "clause": "ORDER BY",
      "knowledge_point_id": "order-by",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "ORDER BY"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT \"title\" FROM \"course\" ORDER BY \"credits\" DESC;",
      "replacement_sqlite": "SELECT \"title\" FROM \"course\" ORDER BY \"credits\" DESC;",
      "replacement_exec_ok": true,
      "replacement_equivalent": true,
      "fixed_by_replacement": true,
      "removal_sql": "SELECT \"title\" FROM \"course\";",
      "removal_sqlite": "SELECT \"title\" FROM \"course\";",
      "removal_exec_ok": true,
      "removed_student_clause_equivalent": false,
      "error": null
    }
  ]
}
```

### 4. 诊断与知识点归因结果 (Attributions)
```json
[
  {
    "knowledge_point_id": "order-by",
    "l1_code": "KP_ORDER",
    "l2_code": "SORT_ASC",
    "clause": "ORDER BY",
    "error_type": "logical",
    "severity": 0.88,
    "confidence": 0.826,
    "detail": "把学生 SQL 的 ORDER BY 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
    "evidence": [
      {
        "source": "E_MUT",
        "signal": "same_clause_mismatch:ORDER BY",
        "detail": "ORDER BY 结构存在，但表达式、边界、操作符或布尔逻辑与目标不一致。",
        "weight": 0.66
      },
      {
        "source": "E_MUT",
        "signal": "replace:order-by",
        "detail": "ORDER BY 子句与标准答案不一致，排序字段或方向是优先隔离方向",
        "weight": 0.62
      }
    ]
  }
]
```

---


## Case 8: Individual - LIMIT (Limit Count Mismatch)

* **数据库 Schema**: `course(course_id, title, dept_name, credits)`
* **标准答案 SQL**:
  ```sql
  SELECT title FROM course LIMIT 3;
  ```
* **学生作答 SQL**:
  ```sql
  SELECT title FROM course LIMIT 5;
  ```
* **沙盒判定等价性**: `False` (执行报错: `None`)

### 1. 结构传感器 (AST Structural Analysis)
| 结构特征项 | 标准答案 SQL 值 | 学生作答 SQL 值 | 匹配状态 |
| :--- | :--- | :--- | :--- |
| SELECT 子句 (has_select) | `True` | `True` | ✅ 匹配 |
| SELECT 投影列数 (projection_count) | `1` | `1` | ✅ 匹配 |
| LIMIT 限制数 (has_limit) | `True` | `True` | ✅ 匹配 |


| 子句位置 | 标准答案子句内容 | 学生作答子句内容 | 一致性 |
| :--- | :--- | :--- | :--- |
| LIMIT | `LIMIT 3` | `LIMIT 5` | ❌ 不匹配 |
| SELECT | `title` | `title` | ✅ 匹配 |

### 2. 数据传感器 (Dynamic Database & Sandbox Run)
#### (1) 动态生成的数据集 (Test Database)
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
#### (2) 沙盒执行输出 (Rows)
* **标准输出行数/数据**: `3 行` -> `[('Sales Manager',), ('Marketing Lead',), ('Engineer',)]`
* **学生输出行数/数据**: `5 行` -> `[('Sales Manager',), ('Marketing Lead',), ('Engineer',), ('Analyst',), ('Sales Manager',)]`

### 3. 变分隔离传感器 (Mutation Isolation Testing)
```json
{
  "enabled": true,
  "summary": {
    "executed": 1,
    "fixed_by_replacement": 1,
    "remove_kept_correct": 0
  },
  "tests": [
    {
      "clause": "LIMIT",
      "knowledge_point_id": "limit",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "LIMIT"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT \"title\" FROM \"course\" LIMIT 3;",
      "replacement_sqlite": "SELECT \"title\" FROM \"course\" LIMIT 3;",
      "replacement_exec_ok": true,
      "replacement_equivalent": true,
      "fixed_by_replacement": true,
      "removal_sql": "SELECT \"title\" FROM \"course\";",
      "removal_sqlite": "SELECT \"title\" FROM \"course\";",
      "removal_exec_ok": true,
      "removed_student_clause_equivalent": false,
      "error": null
    }
  ]
}
```

### 4. 诊断与知识点归因结果 (Attributions)
```json
[
  {
    "knowledge_point_id": "limit",
    "l1_code": "KP_BASIC",
    "l2_code": "LIMIT_OFF",
    "clause": "LIMIT",
    "error_type": "logical",
    "severity": 0.88,
    "confidence": 0.714,
    "detail": "把学生 SQL 的 LIMIT 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
    "evidence": [
      {
        "source": "E_MUT",
        "signal": "same_clause_mismatch:LIMIT",
        "detail": "LIMIT 结构存在，但表达式、边界、操作符或布尔逻辑与目标不一致。",
        "weight": 0.66
      }
    ]
  }
]
```

---


## Case 9: Individual - UNION (UNION vs UNION ALL Mismatch)

* **数据库 Schema**: `course(course_id, title, dept_name, credits)`
* **标准答案 SQL**:
  ```sql
  SELECT title FROM course WHERE dept_name = 'Math' UNION SELECT title FROM course WHERE dept_name = 'Physics';
  ```
* **学生作答 SQL**:
  ```sql
  SELECT title FROM course WHERE dept_name = 'Math' UNION ALL SELECT title FROM course WHERE dept_name = 'Physics';
  ```
* **沙盒判定等价性**: `False` (执行报错: `None`)

### 1. 结构传感器 (AST Structural Analysis)
| 结构特征项 | 标准答案 SQL 值 | 学生作答 SQL 值 | 匹配状态 |
| :--- | :--- | :--- | :--- |
| SELECT 子句 (has_select) | `True` | `True` | ✅ 匹配 |
| SELECT 投影列数 (projection_count) | `1` | `1` | ✅ 匹配 |
| WHERE 过滤 (has_where) | `True` | `True` | ✅ 匹配 |
| UNION 并集 (has_union) | `True` | `True` | ✅ 匹配 |

### 2. 数据传感器 (Dynamic Database & Sandbox Run)
#### (1) 动态生成的数据集 (Test Database)
```json
{
  "course": [
    {
      "course_id": 1,
      "title": "__set_overlap_0__",
      "dept_name": "Math",
      "credits": 1
    },
    {
      "course_id": 2,
      "title": "__set_overlap_0__",
      "dept_name": "Physics",
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
      "title": "Analyst",
      "dept_name": "not_Math",
      "credits": 8
    }
  ]
}
```
#### (2) 沙盒执行输出 (Rows)
* **标准输出行数/数据**: `4 行` -> `[('Analyst',), ('Engineer',), ('Marketing Lead',), ('__set_overlap_0__',)]`
* **学生输出行数/数据**: `6 行` -> `[('__set_overlap_0__',), ('Engineer',), ('Marketing Lead',), ('__set_overlap_0__',), ('Analyst',)]`

### 3. 变分隔离传感器 (Mutation Isolation Testing)
```json
{
  "enabled": true,
  "summary": {
    "executed": 1,
    "fixed_by_replacement": 1,
    "remove_kept_correct": 0
  },
  "tests": [
    {
      "clause": "UNION",
      "knowledge_point_id": "union",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "UNION"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT \"title\" FROM \"course\" WHERE \"dept_name\" = 'Math' UNION SELECT \"title\" FROM \"course\" WHERE \"dept_name\" = 'Physics';",
      "replacement_sqlite": "SELECT \"title\" FROM \"course\" WHERE \"dept_name\" = 'Math' UNION SELECT \"title\" FROM \"course\" WHERE \"dept_name\" = 'Physics';",
      "replacement_exec_ok": true,
      "replacement_equivalent": true,
      "fixed_by_replacement": true,
      "removal_sql": null,
      "removal_sqlite": null,
      "removal_exec_ok": false,
      "removed_student_clause_equivalent": null,
      "error": null
    }
  ]
}
```

### 4. 诊断与知识点归因结果 (Attributions)
```json
[
  {
    "knowledge_point_id": "union",
    "l1_code": "KP_ADVANCED",
    "l2_code": "SET_UNION",
    "clause": "UNION",
    "error_type": "logical",
    "severity": 0.88,
    "confidence": 0.936,
    "detail": "把学生 SQL 的 UNION 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
    "evidence": [
      {
        "source": "E_AST",
        "signal": "set_operator_mismatch:union_vs_union",
        "detail": "集合操作结构或去重语义与标准答案不一致，优先检查 UNION/UNION ALL/INTERSECT/EXCEPT。",
        "weight": 0.82
      },
      {
        "source": "E_MUT",
        "signal": "replacement_fixed:UNION",
        "detail": "把学生 SQL 的 UNION 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
        "weight": 0.88
      }
    ]
  }
]
```

---


## Case 10: Individual - SUBQUERY (Quantified Subquery IN vs NOT IN)

* **数据库 Schema**: `student(ID, name, dept_name); takes(ID, course_id, sec_id, semester, year, grade)`
* **标准答案 SQL**:
  ```sql
  SELECT name FROM student WHERE ID IN (SELECT ID FROM takes WHERE year = 2017);
  ```
* **学生作答 SQL**:
  ```sql
  SELECT name FROM student WHERE ID NOT IN (SELECT ID FROM takes WHERE year = 2017);
  ```
* **沙盒判定等价性**: `False` (执行报错: `None`)

### 1. 结构传感器 (AST Structural Analysis)
| 结构特征项 | 标准答案 SQL 值 | 学生作答 SQL 值 | 匹配状态 |
| :--- | :--- | :--- | :--- |
| SELECT 子句 (has_select) | `True` | `True` | ✅ 匹配 |
| SELECT 投影列数 (projection_count) | `1` | `1` | ✅ 匹配 |
| WHERE 过滤 (has_where) | `True` | `True` | ✅ 匹配 |
| 简单子查询 (has_subquery) | `True` | `True` | ✅ 匹配 |


| 子句位置 | 标准答案子句内容 | 学生作答子句内容 | 一致性 |
| :--- | :--- | :--- | :--- |
| SELECT | `name` | `name` | ✅ 匹配 |
| WHERE | `WHERE id IN (SELECT id FROM takes WHERE year = 2017)` | `WHERE NOT id IN (SELECT id FROM takes WHERE year = 2017)` | ❌ 不匹配 |

### 2. 数据传感器 (Dynamic Database & Sandbox Run)
#### (1) 动态生成的数据集 (Test Database)
```json
{
  "student": [
    {
      "ID": 1000,
      "name": "Alice__predicate_row_000",
      "dept_name": "Comp. Sci."
    },
    {
      "ID": 1000,
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
      "sec_id": 1,
      "semester": "Fall",
      "year": "2024-01-01",
      "grade": 1
    },
    {
      "ID": 3,
      "course_id": 1,
      "sec_id": 2,
      "semester": "Spring",
      "year": "2024-01-02",
      "grade": 2
    },
    {
      "ID": 2019,
      "course_id": 2,
      "sec_id": 3,
      "semester": "Summer",
      "year": "2024-01-03",
      "grade": 3
    },
    {
      "ID": 2016,
      "course_id": 2,
      "sec_id": 4,
      "semester": "Winter",
      "year": "2024-01-04",
      "grade": 4
    },
    {
      "ID": 2022,
      "course_id": 3,
      "sec_id": 5,
      "semester": "Fall",
      "year": "2024-01-05",
      "grade": 5
    },
    {
      "ID": 2024,
      "course_id": 3,
      "sec_id": 6,
      "semester": "Spring",
      "year": "2024-01-06",
      "grade": 6
    },
    {
      "ID": 2014,
      "course_id": 4,
      "sec_id": 7,
      "semester": "Summer",
      "year": "2024-01-07",
      "grade": 7
    },
    {
      "ID": 2023,
      "course_id": 4,
      "sec_id": 8,
      "semester": "Winter",
      "year": "2024-01-08",
      "grade": 8
    }
  ]
}
```
#### (2) 沙盒执行输出 (Rows)
* **标准输出行数/数据**: `0 行` -> `[]`
* **学生输出行数/数据**: `8 行` -> `[('Alice__predicate_row_000',), ('Bob__predicate_row_001',), ('Carol__predicate_row_002',), ('Dave__predicate_row_003',), ('Alice__predicate_row_004',)]`

### 3. 变分隔离传感器 (Mutation Isolation Testing)
```json
{
  "enabled": true,
  "summary": {
    "executed": 1,
    "fixed_by_replacement": 1,
    "remove_kept_correct": 0
  },
  "tests": [
    {
      "clause": "WHERE",
      "knowledge_point_id": "where",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "WHERE"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT \"name\" FROM \"student\" WHERE \"id\" IN (SELECT \"id\" FROM \"takes\" WHERE \"year\" = 2017);",
      "replacement_sqlite": "SELECT \"name\" FROM \"student\" WHERE \"id\" IN (SELECT \"id\" FROM \"takes\" WHERE \"year\" = 2017);",
      "replacement_exec_ok": true,
      "replacement_equivalent": true,
      "fixed_by_replacement": true,
      "removal_sql": "SELECT \"name\" FROM \"student\";",
      "removal_sqlite": "SELECT \"name\" FROM \"student\";",
      "removal_exec_ok": true,
      "removed_student_clause_equivalent": false,
      "error": null
    }
  ]
}
```

### 4. 诊断与知识点归因结果 (Attributions)
```json
[
  {
    "knowledge_point_id": "where",
    "l1_code": "KP_FILTER",
    "l2_code": "COMP_VAL",
    "clause": "WHERE",
    "error_type": "logical",
    "severity": 0.88,
    "confidence": 0.714,
    "detail": "把学生 SQL 的 WHERE 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
    "evidence": [
      {
        "source": "E_MUT",
        "signal": "same_clause_mismatch:WHERE",
        "detail": "WHERE 结构存在，但表达式、边界、操作符或布尔逻辑与目标不一致。",
        "weight": 0.66
      }
    ]
  },
  {
    "knowledge_point_id": "subquery-scalar",
    "l1_code": "KP_SUBQUERY",
    "l2_code": "SUB_TABLE",
    "clause": "SUBQUERY",
    "error_type": "logical",
    "severity": 0.8,
    "confidence": 0.77,
    "detail": "子查询结构与标准答案不一致。",
    "evidence": [
      {
        "source": "E_AST",
        "signal": "subquery_mismatch",
        "detail": "子查询结构与标准答案不一致。",
        "weight": 0.8
      }
    ]
  }
]
```

---


## Case 11: Individual - CASE WHEN (Case Cond Operator Mismatch)

* **数据库 Schema**: `instructor(ID, name, dept_name, salary)`
* **标准答案 SQL**:
  ```sql
  SELECT name, CASE WHEN salary > 70000 THEN 'High' ELSE 'Low' END AS salary_level FROM instructor;
  ```
* **学生作答 SQL**:
  ```sql
  SELECT name, CASE WHEN salary >= 70000 THEN 'High' ELSE 'Low' END AS salary_level FROM instructor;
  ```
* **沙盒判定等价性**: `False` (执行报错: `None`)

### 1. 结构传感器 (AST Structural Analysis)
| 结构特征项 | 标准答案 SQL 值 | 学生作答 SQL 值 | 匹配状态 |
| :--- | :--- | :--- | :--- |
| SELECT 子句 (has_select) | `True` | `True` | ✅ 匹配 |
| SELECT 投影列数 (projection_count) | `2` | `2` | ✅ 匹配 |
| CASE 条件分支 (has_case) | `True` | `True` | ✅ 匹配 |


| 子句位置 | 标准答案子句内容 | 学生作答子句内容 | 一致性 |
| :--- | :--- | :--- | :--- |
| SELECT | `name, CASE WHEN salary > 70000 THEN 'High' ELSE 'Low' END AS salary_level` | `name, CASE WHEN salary >= 70000 THEN 'High' ELSE 'Low' END AS salary_level` | ❌ 不匹配 |

### 2. 数据传感器 (Dynamic Database & Sandbox Run)
#### (1) 动态生成的数据集 (Test Database)
```json
{
  "instructor": [
    {
      "ID": 1,
      "name": "Alice",
      "dept_name": "Comp. Sci.",
      "salary": 70000
    },
    {
      "ID": 2,
      "name": "Bob",
      "dept_name": "Math",
      "salary": 70001
    },
    {
      "ID": 3,
      "name": "Carol",
      "dept_name": "Physics",
      "salary": 69999
    },
    {
      "ID": 4,
      "name": "Dave",
      "dept_name": "History",
      "salary": 70000
    },
    {
      "ID": 5,
      "name": "Alice",
      "dept_name": "Comp. Sci.",
      "salary": 5
    },
    {
      "ID": 6,
      "name": "Bob",
      "dept_name": "Math",
      "salary": 6
    },
    {
      "ID": 7,
      "name": "Carol",
      "dept_name": "Physics",
      "salary": 7
    },
    {
      "ID": 8,
      "name": "Dave",
      "dept_name": "History",
      "salary": 70000
    }
  ]
}
```
#### (2) 沙盒执行输出 (Rows)
* **标准输出行数/数据**: `8 行` -> `[('Alice', 'Low'), ('Bob', 'High'), ('Carol', 'Low'), ('Dave', 'Low'), ('Alice', 'Low')]`
* **学生输出行数/数据**: `8 行` -> `[('Alice', 'High'), ('Bob', 'High'), ('Carol', 'Low'), ('Dave', 'High'), ('Alice', 'Low')]`

### 3. 变分隔离传感器 (Mutation Isolation Testing)
```json
{
  "enabled": true,
  "summary": {
    "executed": 2,
    "fixed_by_replacement": 2,
    "remove_kept_correct": 0
  },
  "tests": [
    {
      "clause": "SELECT",
      "knowledge_point_id": "select-basic",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "SELECT"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT \"name\", CASE WHEN \"salary\" > 70000 THEN 'High' ELSE 'Low' END AS \"salary_level\" FROM \"instructor\";",
      "replacement_sqlite": "SELECT \"name\", CASE WHEN \"salary\" > 70000 THEN 'High' ELSE 'Low' END AS \"salary_level\" FROM \"instructor\";",
      "replacement_exec_ok": true,
      "replacement_equivalent": true,
      "fixed_by_replacement": true,
      "removal_sql": null,
      "removal_sqlite": null,
      "removal_exec_ok": false,
      "removed_student_clause_equivalent": null,
      "error": null
    },
    {
      "clause": "CASE",
      "knowledge_point_id": "case",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "CASE"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT \"name\", CASE WHEN \"salary\" > 70000 THEN 'High' ELSE 'Low' END AS \"salary_level\" FROM \"instructor\";",
      "replacement_sqlite": "SELECT \"name\", CASE WHEN \"salary\" > 70000 THEN 'High' ELSE 'Low' END AS \"salary_level\" FROM \"instructor\";",
      "replacement_exec_ok": true,
      "replacement_equivalent": true,
      "fixed_by_replacement": true,
      "removal_sql": null,
      "removal_sqlite": null,
      "removal_exec_ok": false,
      "removed_student_clause_equivalent": null,
      "error": null
    }
  ]
}
```

### 4. 诊断与知识点归因结果 (Attributions)
```json
[
  {
    "knowledge_point_id": "select-basic",
    "l1_code": "KP_BASIC",
    "l2_code": "PROJ_COL",
    "clause": "SELECT",
    "error_type": "logical",
    "severity": 0.88,
    "confidence": 0.912,
    "detail": "把学生 SQL 的 SELECT 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
    "evidence": [
      {
        "source": "E_AST",
        "signal": "projection_mismatch",
        "detail": "SELECT 投影表达式与标准答案不一致，可能漏选、错选或改写了目标列。",
        "weight": 0.76
      },
      {
        "source": "E_MUT",
        "signal": "replacement_fixed:SELECT",
        "detail": "把学生 SQL 的 SELECT 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
        "weight": 0.88
      }
    ]
  },
  {
    "knowledge_point_id": "case",
    "l1_code": "KP_FUNC",
    "l2_code": "CASE_SEARCH",
    "clause": "CASE",
    "error_type": "logical",
    "severity": 0.88,
    "confidence": 0.802,
    "detail": "把学生 SQL 的 CASE 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
    "evidence": [
      {
        "source": "E_MUT",
        "signal": "replacement_fixed:CASE",
        "detail": "把学生 SQL 的 CASE 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
        "weight": 0.88
      }
    ]
  }
]
```

---


## Case 12: Individual - WINDOW (Missing Partition By in Window OVER)

* **数据库 Schema**: `instructor(ID, name, dept_name, salary)`
* **标准答案 SQL**:
  ```sql
  SELECT name, ROW_NUMBER() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS rank FROM instructor;
  ```
* **学生作答 SQL**:
  ```sql
  SELECT name, ROW_NUMBER() OVER (ORDER BY salary DESC) AS rank FROM instructor;
  ```
* **沙盒判定等价性**: `False` (执行报错: `None`)

### 1. 结构传感器 (AST Structural Analysis)
| 结构特征项 | 标准答案 SQL 值 | 学生作答 SQL 值 | 匹配状态 |
| :--- | :--- | :--- | :--- |
| SELECT 子句 (has_select) | `True` | `True` | ✅ 匹配 |
| SELECT 投影列数 (projection_count) | `2` | `2` | ✅ 匹配 |
| ORDER BY 排序 (has_order) | `True` | `True` | ✅ 匹配 |
| WINDOW 窗口函数 (has_window) | `True` | `True` | ✅ 匹配 |


| 子句位置 | 标准答案子句内容 | 学生作答子句内容 | 一致性 |
| :--- | :--- | :--- | :--- |
| ORDER BY | `ORDER BY salary DESC` | `ORDER BY salary DESC` | ✅ 匹配 |
| SELECT | `name, ROW_NUMBER() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS `rank`` | `name, ROW_NUMBER() OVER (ORDER BY salary DESC) AS `rank`` | ❌ 不匹配 |

### 2. 数据传感器 (Dynamic Database & Sandbox Run)
#### (1) 动态生成的数据集 (Test Database)
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
#### (2) 沙盒执行输出 (Rows)
* **标准输出行数/数据**: `8 行` -> `[('Carol', 1), ('Alice', 2), ('Bob', 3), ('Alice', 1), ('Bob', 2)]`
* **学生输出行数/数据**: `8 行` -> `[('Carol', 1), ('Dave', 2), ('Alice', 3), ('Bob', 4), ('Carol', 5)]`

### 3. 变分隔离传感器 (Mutation Isolation Testing)
```json
{
  "enabled": true,
  "summary": {
    "executed": 2,
    "fixed_by_replacement": 2,
    "remove_kept_correct": 0
  },
  "tests": [
    {
      "clause": "SELECT",
      "knowledge_point_id": "select-basic",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "SELECT"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT \"name\", ROW_NUMBER() OVER (PARTITION BY \"dept_name\" ORDER BY \"salary\" DESC) AS \"rank\" FROM \"instructor\";",
      "replacement_sqlite": "SELECT \"name\", ROW_NUMBER() OVER (PARTITION BY \"dept_name\" ORDER BY \"salary\" DESC) AS \"rank\" FROM \"instructor\";",
      "replacement_exec_ok": true,
      "replacement_equivalent": true,
      "fixed_by_replacement": true,
      "removal_sql": null,
      "removal_sqlite": null,
      "removal_exec_ok": false,
      "removed_student_clause_equivalent": null,
      "error": null
    },
    {
      "clause": "WINDOW",
      "knowledge_point_id": "window-row-number",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "WINDOW"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT \"name\", ROW_NUMBER() OVER (PARTITION BY \"dept_name\" ORDER BY \"salary\" DESC) AS \"rank\" FROM \"instructor\";",
      "replacement_sqlite": "SELECT \"name\", ROW_NUMBER() OVER (PARTITION BY \"dept_name\" ORDER BY \"salary\" DESC) AS \"rank\" FROM \"instructor\";",
      "replacement_exec_ok": true,
      "replacement_equivalent": true,
      "fixed_by_replacement": true,
      "removal_sql": null,
      "removal_sqlite": null,
      "removal_exec_ok": false,
      "removed_student_clause_equivalent": null,
      "error": null
    }
  ]
}
```

### 4. 诊断与知识点归因结果 (Attributions)
```json
[
  {
    "knowledge_point_id": "window-row-number",
    "l1_code": "KP_ADVANCED",
    "l2_code": "WIN_OVER",
    "clause": "WINDOW",
    "error_type": "logical",
    "severity": 0.88,
    "confidence": 0.936,
    "detail": "把学生 SQL 的 WINDOW 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
    "evidence": [
      {
        "source": "E_AST",
        "signal": "window_over_mismatch",
        "detail": "窗口函数 OVER 子句与标准答案不一致，优先检查 PARTITION BY 或 ORDER BY。",
        "weight": 0.82
      },
      {
        "source": "E_MUT",
        "signal": "replacement_fixed:WINDOW",
        "detail": "把学生 SQL 的 WINDOW 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
        "weight": 0.88
      }
    ]
  },
  {
    "knowledge_point_id": "select-basic",
    "l1_code": "KP_BASIC",
    "l2_code": "PROJ_COL",
    "clause": "SELECT",
    "error_type": "logical",
    "severity": 0.88,
    "confidence": 0.912,
    "detail": "把学生 SQL 的 SELECT 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
    "evidence": [
      {
        "source": "E_AST",
        "signal": "projection_mismatch",
        "detail": "SELECT 投影表达式与标准答案不一致，可能漏选、错选或改写了目标列。",
        "weight": 0.76
      },
      {
        "source": "E_MUT",
        "signal": "replacement_fixed:SELECT",
        "detail": "把学生 SQL 的 SELECT 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
        "weight": 0.88
      }
    ]
  }
]
```

---


## Case 13: Mixed - JOIN + GROUP BY + HAVING + ORDER BY (Dual Operator Mismatch)

* **数据库 Schema**: `employee(emp_id, name, dept_id, salary); department(dept_id, dept_name)`
* **标准答案 SQL**:
  ```sql
  SELECT department.dept_name, SUM(employee.salary) AS total_payroll FROM employee JOIN department ON employee.dept_id = department.dept_id GROUP BY department.dept_name HAVING AVG(employee.salary) > 50000 ORDER BY total_payroll DESC;
  ```
* **学生作答 SQL**:
  ```sql
  SELECT department.dept_name, SUM(employee.salary) AS total_payroll FROM employee JOIN department ON employee.dept_id = department.dept_id GROUP BY department.dept_name HAVING AVG(employee.salary) <= 50000 ORDER BY total_payroll ASC;
  ```
* **沙盒判定等价性**: `False` (执行报错: `None`)

### 1. 结构传感器 (AST Structural Analysis)
| 结构特征项 | 标准答案 SQL 值 | 学生作答 SQL 值 | 匹配状态 |
| :--- | :--- | :--- | :--- |
| SELECT 子句 (has_select) | `True` | `True` | ✅ 匹配 |
| SELECT 投影列数 (projection_count) | `2` | `2` | ✅ 匹配 |
| JOIN 连接数 (join_count) | `1` | `1` | ✅ 匹配 |
| JOIN ON 条件 (has_join_on) | `True` | `True` | ✅ 匹配 |
| GROUP BY 分组 (has_group) | `True` | `True` | ✅ 匹配 |
| HAVING 分组后筛选 (has_having) | `True` | `True` | ✅ 匹配 |
| ORDER BY 排序 (has_order) | `True` | `True` | ✅ 匹配 |


| 子句位置 | 标准答案子句内容 | 学生作答子句内容 | 一致性 |
| :--- | :--- | :--- | :--- |
| GROUP BY | `GROUP BY department.dept_name` | `GROUP BY department.dept_name` | ✅ 匹配 |
| HAVING | `HAVING AVG(employee.salary) > 50000` | `HAVING AVG(employee.salary) <= 50000` | ❌ 不匹配 |
| JOIN ON | `employee.dept_id = department.dept_id` | `employee.dept_id = department.dept_id` | ✅ 匹配 |
| ORDER BY | `ORDER BY total_payroll DESC` | `ORDER BY total_payroll ASC` | ❌ 不匹配 |
| SELECT | `department.dept_name, SUM(employee.salary) AS total_payroll` | `department.dept_name, SUM(employee.salary) AS total_payroll` | ✅ 匹配 |

### 2. 数据传感器 (Dynamic Database & Sandbox Run)
#### (1) 动态生成的数据集 (Test Database)
```json
{
  "employee": [
    {
      "emp_id": 1,
      "name": "Alice",
      "dept_id": 1,
      "salary": 50000
    },
    {
      "emp_id": 2,
      "name": "Bob",
      "dept_id": 2,
      "salary": 50001
    },
    {
      "emp_id": 3,
      "name": "Carol",
      "dept_id": 3,
      "salary": 49999
    },
    {
      "emp_id": 4,
      "name": "Dave",
      "dept_id": 4,
      "salary": 50000
    },
    {
      "emp_id": 5,
      "name": "Alice",
      "dept_id": 5,
      "salary": 50001
    },
    {
      "emp_id": 6,
      "name": "Bob",
      "dept_id": 6,
      "salary": 49999
    },
    {
      "emp_id": 7,
      "name": "Carol",
      "dept_id": 7,
      "salary": 50000
    },
    {
      "emp_id": 8,
      "name": "Dave",
      "dept_id": 8,
      "salary": 50001
    }
  ],
  "department": [
    {
      "dept_id": 1,
      "dept_name": "__having_group_0__"
    },
    {
      "dept_id": 2,
      "dept_name": "__having_group_1__"
    },
    {
      "dept_id": 3,
      "dept_name": "__having_group_2__"
    },
    {
      "dept_id": 4,
      "dept_name": "__having_group_3__"
    },
    {
      "dept_id": 5,
      "dept_name": "__having_group_4__"
    },
    {
      "dept_id": 6,
      "dept_name": "__having_group_5__"
    },
    {
      "dept_id": 7,
      "dept_name": "__having_group_6__"
    },
    {
      "dept_id": 8,
      "dept_name": "__having_group_7__"
    }
  ]
}
```
#### (2) 沙盒执行输出 (Rows)
* **标准输出行数/数据**: `3 行` -> `[('__having_group_7__', 50001), ('__having_group_4__', 50001), ('__having_group_1__', 50001)]`
* **学生输出行数/数据**: `5 行` -> `[('__having_group_2__', 49999), ('__having_group_5__', 49999), ('__having_group_0__', 50000), ('__having_group_3__', 50000), ('__having_group_6__', 50000)]`

### 3. 变分隔离传感器 (Mutation Isolation Testing)
```json
{
  "enabled": true,
  "summary": {
    "executed": 2,
    "fixed_by_replacement": 0,
    "remove_kept_correct": 0
  },
  "tests": [
    {
      "clause": "HAVING",
      "knowledge_point_id": "having",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "HAVING"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT \"department\".\"dept_name\", SUM(\"employee\".\"salary\") AS \"total_payroll\" FROM \"employee\" JOIN \"department\" ON \"employee\".\"dept_id\" = \"department\".\"dept_id\" GROUP BY \"department\".\"dept_name\" HAVING AVG(\"employee\".\"salary\") > 50000 ORDER BY \"total_payroll\" ASC;",
      "replacement_sqlite": "SELECT \"department\".\"dept_name\", SUM(\"employee\".\"salary\") AS \"total_payroll\" FROM \"employee\" JOIN \"department\" ON \"employee\".\"dept_id\" = \"department\".\"dept_id\" GROUP BY \"department\".\"dept_name\" HAVING AVG(\"employee\".\"salary\") > 50000 ORDER BY \"total_payroll\" ASC;",
      "replacement_exec_ok": true,
      "replacement_equivalent": false,
      "fixed_by_replacement": false,
      "removal_sql": "SELECT \"department\".\"dept_name\", SUM(\"employee\".\"salary\") AS \"total_payroll\" FROM \"employee\" JOIN \"department\" ON \"employee\".\"dept_id\" = \"department\".\"dept_id\" GROUP BY \"department\".\"dept_name\" ORDER BY \"total_payroll\" ASC;",
      "removal_sqlite": "SELECT \"department\".\"dept_name\", SUM(\"employee\".\"salary\") AS \"total_payroll\" FROM \"employee\" JOIN \"department\" ON \"employee\".\"dept_id\" = \"department\".\"dept_id\" GROUP BY \"department\".\"dept_name\" ORDER BY \"total_payroll\" ASC;",
      "removal_exec_ok": true,
      "removed_student_clause_equivalent": false,
      "error": null
    },
    {
      "clause": "ORDER BY",
      "knowledge_point_id": "order-by",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "ORDER BY"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT \"department\".\"dept_name\", SUM(\"employee\".\"salary\") AS \"total_payroll\" FROM \"employee\" JOIN \"department\" ON \"employee\".\"dept_id\" = \"department\".\"dept_id\" GROUP BY \"department\".\"dept_name\" HAVING AVG(\"employee\".\"salary\") <= 50000 ORDER BY \"total_payroll\" DESC;",
      "replacement_sqlite": "SELECT \"department\".\"dept_name\", SUM(\"employee\".\"salary\") AS \"total_payroll\" FROM \"employee\" JOIN \"department\" ON \"employee\".\"dept_id\" = \"department\".\"dept_id\" GROUP BY \"department\".\"dept_name\" HAVING AVG(\"employee\".\"salary\") <= 50000 ORDER BY \"total_payroll\" DESC;",
      "replacement_exec_ok": true,
      "replacement_equivalent": false,
      "fixed_by_replacement": false,
      "removal_sql": "SELECT \"department\".\"dept_name\", SUM(\"employee\".\"salary\") AS \"total_payroll\" FROM \"employee\" JOIN \"department\" ON \"employee\".\"dept_id\" = \"department\".\"dept_id\" GROUP BY \"department\".\"dept_name\" HAVING AVG(\"employee\".\"salary\") <= 50000;",
      "removal_sqlite": "SELECT \"department\".\"dept_name\", SUM(\"employee\".\"salary\") AS \"total_payroll\" FROM \"employee\" JOIN \"department\" ON \"employee\".\"dept_id\" = \"department\".\"dept_id\" GROUP BY \"department\".\"dept_name\" HAVING AVG(\"employee\".\"salary\") <= 50000;",
      "removal_exec_ok": true,
      "removed_student_clause_equivalent": false,
      "error": null
    }
  ]
}
```

### 4. 诊断与知识点归因结果 (Attributions)
```json
[
  {
    "knowledge_point_id": "order-by",
    "l1_code": "KP_ORDER",
    "l2_code": "SORT_ASC",
    "clause": "ORDER BY",
    "error_type": "logical",
    "severity": 0.66,
    "confidence": 0.826,
    "detail": "ORDER BY 结构存在，但表达式、边界、操作符或布尔逻辑与目标不一致。",
    "evidence": [
      {
        "source": "E_MUT",
        "signal": "same_clause_mismatch:ORDER BY",
        "detail": "ORDER BY 结构存在，但表达式、边界、操作符或布尔逻辑与目标不一致。",
        "weight": 0.66
      },
      {
        "source": "E_MUT",
        "signal": "replace:order-by",
        "detail": "ORDER BY 子句与标准答案不一致，排序字段或方向是优先隔离方向",
        "weight": 0.62
      }
    ]
  },
  {
    "knowledge_point_id": "having",
    "l1_code": "KP_AGG",
    "l2_code": "HV_SIMPLE",
    "clause": "HAVING",
    "error_type": "logical",
    "severity": 0.66,
    "confidence": 0.714,
    "detail": "HAVING 结构存在，但表达式、边界、操作符或布尔逻辑与目标不一致。",
    "evidence": [
      {
        "source": "E_MUT",
        "signal": "same_clause_mismatch:HAVING",
        "detail": "HAVING 结构存在，但表达式、边界、操作符或布尔逻辑与目标不一致。",
        "weight": 0.66
      }
    ]
  }
]
```

---


## Case 14: Mixed - CTE (WITH) + JOIN + WHERE (WHERE Predicate Mismatch)

* **数据库 Schema**: `works(company_name, person_name, salary); company(company_name, city)`
* **标准答案 SQL**:
  ```sql
  WITH big_co AS (SELECT company_name FROM company WHERE city = 'Beijing') SELECT person_name FROM works JOIN big_co ON works.company_name = big_co.company_name WHERE salary > 10000;
  ```
* **学生作答 SQL**:
  ```sql
  WITH big_co AS (SELECT company_name FROM company WHERE city = 'Beijing') SELECT person_name FROM works JOIN big_co ON works.company_name = big_co.company_name WHERE salary < 10000;
  ```
* **沙盒判定等价性**: `False` (执行报错: `None`)

### 1. 结构传感器 (AST Structural Analysis)
| 结构特征项 | 标准答案 SQL 值 | 学生作答 SQL 值 | 匹配状态 |
| :--- | :--- | :--- | :--- |
| SELECT 子句 (has_select) | `True` | `True` | ✅ 匹配 |
| SELECT 投影列数 (projection_count) | `1` | `1` | ✅ 匹配 |
| WHERE 过滤 (has_where) | `True` | `True` | ✅ 匹配 |
| JOIN 连接数 (join_count) | `1` | `1` | ✅ 匹配 |
| JOIN ON 条件 (has_join_on) | `True` | `True` | ✅ 匹配 |
| 简单 CTE (WITH) (has_cte) | `True` | `True` | ✅ 匹配 |


| 子句位置 | 标准答案子句内容 | 学生作答子句内容 | 一致性 |
| :--- | :--- | :--- | :--- |
| JOIN ON | `works.company_name = big_co.company_name` | `works.company_name = big_co.company_name` | ✅ 匹配 |
| SELECT | `person_name` | `person_name` | ✅ 匹配 |
| WHERE | `WHERE salary > 10000` | `WHERE salary < 10000` | ❌ 不匹配 |

### 2. 数据传感器 (Dynamic Database & Sandbox Run)
#### (1) 动态生成的数据集 (Test Database)
```json
{
  "works": [
    {
      "company_name": "Alice",
      "person_name": "Alice__predicate_row_000__cte_row_000",
      "salary": -1
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
#### (2) 沙盒执行输出 (Rows)
* **标准输出行数/数据**: `1 行` -> `[('Bob__predicate_row_001__cte_row_001',)]`
* **学生输出行数/数据**: `3 行` -> `[('Alice__predicate_row_000__cte_row_000',), ('Alice__predicate_row_004__cte_row_004',), ('Bob__predicate_row_005__cte_row_005',)]`

### 3. 变分隔离传感器 (Mutation Isolation Testing)
```json
{
  "enabled": true,
  "summary": {
    "executed": 1,
    "fixed_by_replacement": 1,
    "remove_kept_correct": 0
  },
  "tests": [
    {
      "clause": "WHERE",
      "knowledge_point_id": "where",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "WHERE"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "WITH RECURSIVE \"big_co\" AS (SELECT \"company_name\" FROM \"company\" WHERE \"city\" = 'Beijing') SELECT \"person_name\" FROM \"works\" JOIN \"big_co\" ON \"works\".\"company_name\" = \"big_co\".\"company_name\" WHERE \"salary\" > 10000;",
      "replacement_sqlite": "WITH RECURSIVE \"big_co\" AS (SELECT \"company_name\" FROM \"company\" WHERE \"city\" = 'Beijing') SELECT \"person_name\" FROM \"works\" JOIN \"big_co\" ON \"works\".\"company_name\" = \"big_co\".\"company_name\" WHERE \"salary\" > 10000;",
      "replacement_exec_ok": true,
      "replacement_equivalent": true,
      "fixed_by_replacement": true,
      "removal_sql": "WITH RECURSIVE \"big_co\" AS (SELECT \"company_name\" FROM \"company\" WHERE \"city\" = 'Beijing') SELECT \"person_name\" FROM \"works\" JOIN \"big_co\" ON \"works\".\"company_name\" = \"big_co\".\"company_name\";",
      "removal_sqlite": "WITH RECURSIVE \"big_co\" AS (SELECT \"company_name\" FROM \"company\" WHERE \"city\" = 'Beijing') SELECT \"person_name\" FROM \"works\" JOIN \"big_co\" ON \"works\".\"company_name\" = \"big_co\".\"company_name\";",
      "removal_exec_ok": true,
      "removed_student_clause_equivalent": false,
      "error": null
    }
  ]
}
```

### 4. 诊断与知识点归因结果 (Attributions)
```json
[
  {
    "knowledge_point_id": "where",
    "l1_code": "KP_FILTER",
    "l2_code": "COMP_VAL",
    "clause": "WHERE",
    "error_type": "logical",
    "severity": 0.88,
    "confidence": 0.714,
    "detail": "把学生 SQL 的 WHERE 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
    "evidence": [
      {
        "source": "E_MUT",
        "signal": "same_clause_mismatch:WHERE",
        "detail": "WHERE 结构存在，但表达式、边界、操作符或布尔逻辑与目标不一致。",
        "weight": 0.66
      }
    ]
  }
]
```

---


## Case 15: Mixed - SUBQUERY + GROUP BY + HAVING (Having Subquery Mismatch)

* **数据库 Schema**: `instructor(ID, name, dept_name, salary)`
* **标准答案 SQL**:
  ```sql
  SELECT dept_name, COUNT(ID) FROM instructor GROUP BY dept_name HAVING AVG(salary) > (SELECT AVG(salary) FROM instructor);
  ```
* **学生作答 SQL**:
  ```sql
  SELECT dept_name, COUNT(ID) FROM instructor GROUP BY dept_name HAVING AVG(salary) <= (SELECT AVG(salary) FROM instructor);
  ```
* **沙盒判定等价性**: `False` (执行报错: `None`)

### 1. 结构传感器 (AST Structural Analysis)
| 结构特征项 | 标准答案 SQL 值 | 学生作答 SQL 值 | 匹配状态 |
| :--- | :--- | :--- | :--- |
| SELECT 子句 (has_select) | `True` | `True` | ✅ 匹配 |
| SELECT 投影列数 (projection_count) | `2` | `2` | ✅ 匹配 |
| GROUP BY 分组 (has_group) | `True` | `True` | ✅ 匹配 |
| HAVING 分组后筛选 (has_having) | `True` | `True` | ✅ 匹配 |
| 简单子查询 (has_subquery) | `True` | `True` | ✅ 匹配 |


| 子句位置 | 标准答案子句内容 | 学生作答子句内容 | 一致性 |
| :--- | :--- | :--- | :--- |
| GROUP BY | `GROUP BY dept_name` | `GROUP BY dept_name` | ✅ 匹配 |
| HAVING | `HAVING AVG(salary) > (SELECT AVG(salary) FROM instructor)` | `HAVING AVG(salary) <= (SELECT AVG(salary) FROM instructor)` | ❌ 不匹配 |
| SELECT | `dept_name, COUNT(id)` | `dept_name, COUNT(id)` | ✅ 匹配 |

### 2. 数据传感器 (Dynamic Database & Sandbox Run)
#### (1) 动态生成的数据集 (Test Database)
```json
{
  "instructor": [
    {
      "ID": 1,
      "name": "Alice",
      "dept_name": "Comp. Sci.",
      "salary": 1
    },
    {
      "ID": 2,
      "name": "Bob",
      "dept_name": "Math",
      "salary": 2
    },
    {
      "ID": 3,
      "name": "Carol",
      "dept_name": "Physics",
      "salary": 3
    },
    {
      "ID": 4,
      "name": "Dave",
      "dept_name": "History",
      "salary": 4
    },
    {
      "ID": 5,
      "name": "Alice",
      "dept_name": "Comp. Sci.",
      "salary": 5
    },
    {
      "ID": 6,
      "name": "Bob",
      "dept_name": "Math",
      "salary": 6
    },
    {
      "ID": 7,
      "name": "Carol",
      "dept_name": "Physics",
      "salary": 7
    },
    {
      "ID": 8,
      "name": "Dave",
      "dept_name": "History",
      "salary": 8
    }
  ]
}
```
#### (2) 沙盒执行输出 (Rows)
* **标准输出行数/数据**: `2 行` -> `[('History', 2), ('Physics', 2)]`
* **学生输出行数/数据**: `2 行` -> `[('Comp. Sci.', 2), ('Math', 2)]`

### 3. 变分隔离传感器 (Mutation Isolation Testing)
```json
{
  "enabled": true,
  "summary": {
    "executed": 1,
    "fixed_by_replacement": 1,
    "remove_kept_correct": 0
  },
  "tests": [
    {
      "clause": "HAVING",
      "knowledge_point_id": "having",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "HAVING"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT \"dept_name\", COUNT(\"id\") FROM \"instructor\" GROUP BY \"dept_name\" HAVING AVG(\"salary\") > (SELECT AVG(\"salary\") FROM \"instructor\");",
      "replacement_sqlite": "SELECT \"dept_name\", COUNT(\"id\") FROM \"instructor\" GROUP BY \"dept_name\" HAVING AVG(\"salary\") > (SELECT AVG(\"salary\") FROM \"instructor\");",
      "replacement_exec_ok": true,
      "replacement_equivalent": true,
      "fixed_by_replacement": true,
      "removal_sql": "SELECT \"dept_name\", COUNT(\"id\") FROM \"instructor\" GROUP BY \"dept_name\";",
      "removal_sqlite": "SELECT \"dept_name\", COUNT(\"id\") FROM \"instructor\" GROUP BY \"dept_name\";",
      "removal_exec_ok": true,
      "removed_student_clause_equivalent": false,
      "error": null
    }
  ]
}
```

### 4. 诊断与知识点归因结果 (Attributions)
```json
[
  {
    "knowledge_point_id": "having",
    "l1_code": "KP_AGG",
    "l2_code": "HV_SIMPLE",
    "clause": "HAVING",
    "error_type": "logical",
    "severity": 0.88,
    "confidence": 0.714,
    "detail": "把学生 SQL 的 HAVING 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。",
    "evidence": [
      {
        "source": "E_MUT",
        "signal": "same_clause_mismatch:HAVING",
        "detail": "HAVING 结构存在，但表达式、边界、操作符或布尔逻辑与目标不一致。",
        "weight": 0.66
      }
    ]
  },
  {
    "knowledge_point_id": "subquery-scalar",
    "l1_code": "KP_SUBQUERY",
    "l2_code": "SUB_TABLE",
    "clause": "SUBQUERY",
    "error_type": "logical",
    "severity": 0.8,
    "confidence": 0.77,
    "detail": "子查询结构与标准答案不一致。",
    "evidence": [
      {
        "source": "E_AST",
        "signal": "subquery_mismatch",
        "detail": "子查询结构与标准答案不一致。",
        "weight": 0.8
      }
    ]
  }
]
```

---


## Case 16: Mixed - CASE WHEN + SELECT + GROUP BY + ORDER BY (Conditional Cond Mismatch + Order Direction Mismatch)

* **数据库 Schema**: `sales(sale_id, category, amount)`
* **标准答案 SQL**:
  ```sql
  SELECT category, SUM(CASE WHEN amount > 100 THEN amount ELSE 0 END) AS big_sales FROM sales GROUP BY category ORDER BY big_sales DESC;
  ```
* **学生作答 SQL**:
  ```sql
  SELECT category, SUM(CASE WHEN amount >= 100 THEN amount ELSE 0 END) AS big_sales FROM sales GROUP BY category ORDER BY big_sales ASC;
  ```
* **沙盒判定等价性**: `False` (执行报错: `None`)

### 1. 结构传感器 (AST Structural Analysis)
| 结构特征项 | 标准答案 SQL 值 | 学生作答 SQL 值 | 匹配状态 |
| :--- | :--- | :--- | :--- |
| SELECT 子句 (has_select) | `True` | `True` | ✅ 匹配 |
| SELECT 投影列数 (projection_count) | `2` | `2` | ✅ 匹配 |
| GROUP BY 分组 (has_group) | `True` | `True` | ✅ 匹配 |
| ORDER BY 排序 (has_order) | `True` | `True` | ✅ 匹配 |
| CASE 条件分支 (has_case) | `True` | `True` | ✅ 匹配 |


| 子句位置 | 标准答案子句内容 | 学生作答子句内容 | 一致性 |
| :--- | :--- | :--- | :--- |
| GROUP BY | `GROUP BY category` | `GROUP BY category` | ✅ 匹配 |
| ORDER BY | `ORDER BY big_sales DESC` | `ORDER BY big_sales ASC` | ❌ 不匹配 |
| SELECT | `category, SUM(CASE WHEN amount > 100 THEN amount ELSE 0 END) AS big_sales` | `category, SUM(CASE WHEN amount >= 100 THEN amount ELSE 0 END) AS big_sales` | ❌ 不匹配 |

### 2. 数据传感器 (Dynamic Database & Sandbox Run)
#### (1) 动态生成的数据集 (Test Database)
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
#### (2) 沙盒执行输出 (Rows)
* **标准输出行数/数据**: `8 行` -> `[('category_2', 101), ('category_8', 0), ('category_7', 0), ('category_6', 0), ('category_5', 0)]`
* **学生输出行数/数据**: `8 行` -> `[('category_3', 0), ('category_5', 0), ('category_6', 0), ('category_7', 0), ('category_1', 100)]`

### 3. 变分隔离传感器 (Mutation Isolation Testing)
```json
{
  "enabled": true,
  "summary": {
    "executed": 3,
    "fixed_by_replacement": 0,
    "remove_kept_correct": 0
  },
  "tests": [
    {
      "clause": "ORDER BY",
      "knowledge_point_id": "order-by",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "ORDER BY"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT \"category\", SUM(CASE WHEN \"amount\" >= 100 THEN \"amount\" ELSE 0 END) AS \"big_sales\" FROM \"sales\" GROUP BY \"category\" ORDER BY \"big_sales\" DESC;",
      "replacement_sqlite": "SELECT \"category\", SUM(CASE WHEN \"amount\" >= 100 THEN \"amount\" ELSE 0 END) AS \"big_sales\" FROM \"sales\" GROUP BY \"category\" ORDER BY \"big_sales\" DESC;",
      "replacement_exec_ok": true,
      "replacement_equivalent": false,
      "fixed_by_replacement": false,
      "removal_sql": "SELECT \"category\", SUM(CASE WHEN \"amount\" >= 100 THEN \"amount\" ELSE 0 END) AS \"big_sales\" FROM \"sales\" GROUP BY \"category\";",
      "removal_sqlite": "SELECT \"category\", SUM(CASE WHEN \"amount\" >= 100 THEN \"amount\" ELSE 0 END) AS \"big_sales\" FROM \"sales\" GROUP BY \"category\";",
      "removal_exec_ok": true,
      "removed_student_clause_equivalent": false,
      "error": null
    },
    {
      "clause": "SELECT",
      "knowledge_point_id": "select-basic",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "SELECT"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT \"category\", SUM(CASE WHEN \"amount\" > 100 THEN \"amount\" ELSE 0 END) AS \"big_sales\" FROM \"sales\" GROUP BY \"category\" ORDER BY \"big_sales\" ASC;",
      "replacement_sqlite": "SELECT \"category\", SUM(CASE WHEN \"amount\" > 100 THEN \"amount\" ELSE 0 END) AS \"big_sales\" FROM \"sales\" GROUP BY \"category\" ORDER BY \"big_sales\" ASC;",
      "replacement_exec_ok": true,
      "replacement_equivalent": false,
      "fixed_by_replacement": false,
      "removal_sql": null,
      "removal_sqlite": null,
      "removal_exec_ok": false,
      "removed_student_clause_equivalent": null,
      "error": null
    },
    {
      "clause": "CASE",
      "knowledge_point_id": "case",
      "action": "replace_student_clause_with_standard_clause",
      "mutation_scope": [
        "CASE"
      ],
      "execution_backend": "sqlite",
      "sql_dialect": "mysql",
      "replacement_sql": "SELECT \"category\", SUM(CASE WHEN \"amount\" > 100 THEN \"amount\" ELSE 0 END) AS \"big_sales\" FROM \"sales\" GROUP BY \"category\" ORDER BY \"big_sales\" ASC;",
      "replacement_sqlite": "SELECT \"category\", SUM(CASE WHEN \"amount\" > 100 THEN \"amount\" ELSE 0 END) AS \"big_sales\" FROM \"sales\" GROUP BY \"category\" ORDER BY \"big_sales\" ASC;",
      "replacement_exec_ok": true,
      "replacement_equivalent": false,
      "fixed_by_replacement": false,
      "removal_sql": null,
      "removal_sqlite": null,
      "removal_exec_ok": false,
      "removed_student_clause_equivalent": null,
      "error": null
    }
  ]
}
```

### 4. 诊断与知识点归因结果 (Attributions)
```json
[
  {
    "knowledge_point_id": "select-basic",
    "l1_code": "KP_BASIC",
    "l2_code": "PROJ_COL",
    "clause": "SELECT",
    "error_type": "logical",
    "severity": 0.76,
    "confidence": 0.855,
    "detail": "SELECT 投影表达式与标准答案不一致，可能漏选、错选或改写了目标列。",
    "evidence": [
      {
        "source": "E_AST",
        "signal": "projection_mismatch",
        "detail": "SELECT 投影表达式与标准答案不一致，可能漏选、错选或改写了目标列。",
        "weight": 0.76
      },
      {
        "source": "E_MUT",
        "signal": "replacement_not_enough:SELECT",
        "detail": "替换 SELECT 后仍未通过动态沙盒，说明该子句相关但可能不是唯一错因。",
        "weight": 0.56
      }
    ]
  },
  {
    "knowledge_point_id": "case",
    "l1_code": "KP_FUNC",
    "l2_code": "CASE_SEARCH",
    "clause": "CASE",
    "error_type": "logical",
    "severity": 0.68,
    "confidence": 0.674,
    "detail": "CASE 条件表达式与标准答案不一致，优先检查 WHEN 条件、NULL 判断或 ELSE 分支",
    "evidence": [
      {
        "source": "E_MUT",
        "signal": "replacement_not_enough:CASE",
        "detail": "替换 CASE 后仍未通过动态沙盒，说明该子句相关但可能不是唯一错因。",
        "weight": 0.56
      }
    ]
  },
  {
    "knowledge_point_id": "order-by",
    "l1_code": "KP_ORDER",
    "l2_code": "SORT_ASC",
    "clause": "ORDER BY",
    "error_type": "logical",
    "severity": 0.66,
    "confidence": 0.826,
    "detail": "ORDER BY 结构存在，但表达式、边界、操作符或布尔逻辑与目标不一致。",
    "evidence": [
      {
        "source": "E_MUT",
        "signal": "same_clause_mismatch:ORDER BY",
        "detail": "ORDER BY 结构存在，但表达式、边界、操作符或布尔逻辑与目标不一致。",
        "weight": 0.66
      },
      {
        "source": "E_MUT",
        "signal": "replace:order-by",
        "detail": "ORDER BY 子句与标准答案不一致，排序字段或方向是优先隔离方向",
        "weight": 0.62
      }
    ]
  }
]
```

---


## 五、 样例生成设计考量与完备性辩证

在对 `WHERE` 等过滤算子谓词边界的测试中，系统采用并验证了**基于答案与学生 SQL 真值交集划分（三态划分）的数学完备性验证策略**，以保障一定能捕获和定位逻辑差异：

1. **真值交集三态划分（Cardinal Truth-Intersection Regions）**：
   对于任何题目，标准答案谓词 $P_{std}$ 与学生作答谓词 $P_{stu}$ 会将数据域分割为以下三个关键区域，测试数据集必须生成这三类数据以实现完备诊断：
   - **均符合数据 ($T_{both}$)**：$P_{std} \land P_{stu} = \text{True}$。双方都返回的阳性数据，建立正确基线。
   - **差异数据 ($T_{diff}$)**：$P_{std} \oplus P_{stu} = \text{True}$（一个对一个不对）。这是**判定不等价的唯一绝对证据来源**！若数据集中缺少此区间数据，则两边执行结果必然相同，造成假阳性漏报。
   - **均不符合数据 ($T_{neither}$)**：$P_{std} \lor P_{stu} = \text{False}$（双方都不对的阴性数据），用以排除冗余匹配干扰。

2. **数值边界双向攻击与差异捕获**：
   在 Case 2（标答 `> 3`，学生 `>= 3`）中，系统提取临界值 `3` 放入数据集。
   - 对于 `credits = 3`，标答判断为 False，学生判断为 True。这恰好落在了**差异区 ($T_{diff}$)**。
   - 对于 `credits = 4, 5, 6, 7`，双方均判断为 True。落在了**均符合区 ($T_{both}$)**。
   - 由于存在差异区数据，学生 SQL 在沙盒中输出了这些行，而标准 SQL 将其过滤，产生了 8 行 vs 5 行的显著不匹配，从而 100% 暴露出逻辑错误并由变分模块精准锁定。

3. **效率收益**：
   通过分析标准 SQL 和学生 SQL 提取的谓词集合，动态构造并满足这三个区（$T_{both}, T_{diff}, T_{neither}$）的数据行，能够在不依赖繁重外部 SMT 约束求解器的情况下，将数据生成与沙盒执行限制在 **2 毫秒** 级别，完全满足高并发教学诊断的需求。