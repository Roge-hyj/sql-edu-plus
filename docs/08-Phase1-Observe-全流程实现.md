# Phase 1: Observe（感知阶段）全流程实现

## 一、总体流程

```
POST /ai/check-sql (student_sql, correct_sql, schema_preview)
  │
  ├── Step 1: 安全拦截 ──── 危险关键字扫描
  ├── Step 2: 语法检查 ──── sqlglot 严格模式解析
  │     ├── 语法错误 → 直接返回纠错提示，流程结束
  │     └── 通过 → 继续
  │
  └── Step 3: ParSEval ─── 唯一判题 + 造数 + 变异
        ├── 3a: AST 解析 → Diff Graph (ASTDiffNode)
        ├── 3b: 动态造数 (8 个基础探针 + 5 个策略探针)
        ├── 3c: SQLite 沙盒执行 → is_equivalent (原始等价性信号)
        └── 3d: 变异消融测试 → mutation_evidence
```

**输入：** `student_sql`（学生 SQL）、`correct_sql`（标准 SQL）、`schema_preview`（表结构 JSON，仅含列名，不含数据）

**输出：** 全量证据（Diff Graph + 执行结果 + 变异信号），传递给第二阶段归因

---



## 二、各步骤详细实现



### Step 1: 安全拦截

**文件：** `core/sql_judge.py` → `_check_sql_safety()`

**实现：** 正则扫描学生 SQL，检测 DROP / DELETE / INSERT / UPDATE / ALTER / TRUNCATE 等危险关键字。命中即返回 `is_safety_blocked=True`，不进入后续步骤。

---



### Step 2: 语法检查

**文件：** `routers/ai.py` → `check_sql()` 内联

```python
sqlglot.parse_one(student_sql, dialect="mysql", error_level=ErrorLevel.RAISE)
```

严格模式解析，语法错误（如括号不匹配、拼写错误）直接捕获并返回纠错提示，不进入后续步骤。

---



### Step 3: 结构+造数+变异（核心引擎）

**文件：** `core/parseval_data_generator.py` → `generate_and_compare()`（~3958 行）

这是 Phase 1 的核心，承担**造数、执行、变异**三重职责。

#### 3a: AST 解析 + Diff Graph 构建

**函数：** `extract_ast_diffs(standard_sql, student_sql) → list[ASTDiffNode]`

10 个专用 diff 函数，覆盖 SQL 全部语法结构：


| diff 函数                           | 比较维度                     | 产出示例                           |
| --------------------------------- | ------------------------ | ------------------------------ |
| `_clause_ast_diffs()`             | WHERE/GB/HAVING/OB/LIMIT | `WHERE: where_missing`         |
| `_projection_column_ast_diffs()`  | SELECT 列差异               | `SELECT: projection_changed`   |
| `_comparison_ast_diffs()`         | 比较运算符                    | `PREDICATE: predicate_changed` |
| `_logical_operator_ast_diffs()`   | AND/OR 逻辑                | `LOGIC: logic_changed`         |
| `_join_ast_diffs()`               | JOIN 类型/条件/缺失            | `JOIN: join_missing`           |
| `_set_operator_ast_diffs()`       | UNION/INTERSECT/EXCEPT   | `UNION: set_operator_changed`  |
| `_window_ast_diffs()`             | OVER 分区/排序               | `WINDOW: window_over_changed`  |
| `_cte_ast_diffs()`                | WITH 递归/结构               | `CTE: cte_changed`             |
| `_aggregate_function_ast_diffs()` | 聚合函数集合                   | `AGGREGATE: agg_changed`       |
| `_subquery_ast_diffs()`           | 子查询递归比较                  | `SUBQUERY: subquery_missing`   |


输出 `list[ASTDiffNode]` 是 Diff Graph，被三个下游消费者共享（造数、变异、第二阶段归因）。

#### 3b: 动态造数

**函数：** `generate_test_database(schema, standard_sql, student_sql, ast_diffs) → dict[str, list[dict]]`

生成的测试数据**不是随机的**，而是根据 Diff Graph **定向构造反例数据**，使学生 SQL 和标准 SQL 在特定维度上产出不同结果。

**9 个基础探针（单表级约束注入）：**


| 探针                                   | 功能                                      |
| ------------------------------------ | --------------------------------------- |
| `_apply_constraints()`               | 谓词三态值：对每个 WHERE/HAVING 约束注入匹配值、反值、边界值   |
| `_add_duplicate_probe()`             | 重复投影行：DISTINCT 去重 / UNION ALL 保留        |
| `_apply_join_key_drift()`            | JOIN 键漂移：FK 值偏移导致 JOIN 丢行               |
| `_apply_dangling_tuple_probe()`      | 悬浮元组：右表无匹配行，LEFT JOIN 保留而 INNER JOIN 丢弃 |
| `_apply_having_aggregate_probes()`   | HAVING 聚合边界：COUNT boundary±1            |
| `_apply_count_group_probe()`         | COUNT+GROUP BY 基数：让某组恰好等于 boundary      |
| `_apply_subquery_aggregate_probes()` | 子查询聚合边界                                 |
| `_apply_subquery_membership_probe()` | IN 子查询成员命中/未命中                          |
| `_apply_correlated_subquery_probe()` | 相关子查询：确保内外层关联列有交叉数据                 |


**7 个策略探针（TacticRegistry，跨表/全局反例）：**


| Tactic                       | 触发条件                        | 功能           |
| ---------------------------- | --------------------------- | ------------ |
| `JoinOnCounterexampleTactic` | JOIN/JOIN ON diff           | JOIN ON 反例构造 |
| `OrderByTiesTactic`          | ORDER BY diff               | 排序并列值        |
| `WindowPartitionTactic`      | WINDOW diff                 | 窗口分区边界       |
| `GroupByProbesTactic`        | GROUP BY/HAVING diff        | 分组基数控制       |
| `SetOperatorProbesTactic`    | UNION/INTERSECT/EXCEPT diff | 集合重叠行        |
| `CteProbesTactic`            | CTE/CTE_RECURSIVE diff      | 简单 CTE 传递约束；递归 CTE 构造多层层级 |
| `CaseWhenProbesTactic`       | 始终触发（内部检测 CASE）     | CASE WHEN 分支遍历，各分支覆盖边界值 |


**造数流程：**

```
schema_preview (表结构) → parse_schema_text() → {table: [columns]}
    ↓
AST 解析 → _extract_literal_constraints() → 提取 WHERE/HAVING/JOIN 字面约束
    ↓
动态行数计算 → _dynamic_row_count() → 基于 COUNT boundary 确定行数
    ↓
基础值填充 → _base_value() → 按列名语义推断类型 (id→整数, name→字符串, date→日期)
    ↓
约束注入 → 8 个基础探针 + 5 个策略探针 → 定向构造反例
    ↓
主键修复 → _repair_primary_key_candidate_duplicates()
    ↓
输出: {table_name: [{col: val, ...}, ...]}
```



#### 3c: SQLite 沙盒执行

```python
standard_sqlite = transpile_to_sqlite(standard_sql)  # T-SQL/MySQL → SQLite 转译
student_sqlite = transpile_to_sqlite(student_sql)

std_cols, std_rows = _execute_sqlite(schema, rows, standard_sqlite)
stu_cols, stu_rows = _execute_sqlite(schema, rows, student_sqlite)

# 等价性判定（原始信号，is_correct 由第二阶段归因阶段判定）
if ordered:  # 标准 SQL 含 ORDER BY
    is_equivalent = std_cols == stu_cols and std_rows == stu_rows
else:        # 无序比较
    is_equivalent = std_cols == stu_cols and Counter(std_rows) == Counter(stu_rows)
```

**转译链：** `transpile_to_sqlite()` 采用三方言回退：tsql → sqlite → mysql → manual_compat。兼容层处理 `CONCAT→||`、`ISNULL→COALESCE`、`GETDATE→datetime('now')`、`TOP→LIMIT`、`Sys.Views` 表名转换等。

**安全：** SQLite 沙箱设有进度回调（10 万条指令上限），防止死循环/笛卡尔积导致超时。

#### 3d: 变异消融测试

**函数：** `_run_mutation_tests() → mutation_evidence`

对标准 SQL 的每个 clause（WHERE, GROUP BY, HAVING, ORDER BY, LIMIT, JOIN ON）执行两种变异：

1. **替换变异：** 将学生 SQL 的 clause 替换为标准 SQL 对应 clause → 重新执行 → 检测是否恢复等价
2. **移除变异：** 将学生 SQL 的 clause 直接移除 → 重新执行 → 检测是否保持等价

**输出：**

```python
mutation_evidence = {
    "tests": [
        {
            "clause": "WHERE",
            "knowledge_point_id": "where",
            "action": "replacement",
            "fixed_by_replacement": True,       # 替换后修复 = 高因果信号
            "removed_student_clause_equivalent": False,
        },
        ...
    ],
    "summary": {"executed": 6, "fixed_by_replacement": 1}
}
```

**因果推断逻辑：**

- `fixed_by_replacement=True` → 该 clause 是错因（权重 0.88）
- `removed_student_clause_equivalent=True` → 该 clause 是不必要结构（权重 0.48）

---



## 三、Phase 1 输出

```python
SandboxRun(
    executed=True,
    is_equivalent=False,                    # 原始等价性信号（非最终 is_correct）
    standard_rows=[...], student_rows=[...], # 沙盒执行结果
    data_evidence={...},                    # E_data 证据
    mutation_evidence={                     # E_MUT 证据
        "tests": [...],
        "summary": {"executed": 6, "fixed_by_replacement": 1}
    },
    ast_diffs=[ASTDiffNode(...), ...],      # Diff Graph (传递给第二阶段)
)
```

---



## 四、数据流全景

```
student_sql ──┐
              ├── _parse_sql() × 2
correct_sql ──┘
    │
    ▼
extract_ast_diffs() ──→ list[ASTDiffNode] (Diff Graph)
    │                        │                    │
    ▼                        ▼                    ▼
generate_test_database()  TacticRegistry    _run_mutation_tests()
  (9 个基础探针)          (7 个策略)         (clause 替换/移除)
    │                        │                    │
    ▼                        ▼                    ▼
  反例数据                  增强数据          mutation_evidence
    │                                             │
    ▼                                             ▼
_execute_sqlite() × 2                             │
    │                                             │
    ▼                                             │
  is_equivalent (原始信号)                         │
  data_evidence                                   │
  ast_diffs                                       │
    │                                             │
    └─────────────────────────────────────────────┘
    ▼
  Phase 1 输出 ──→ 传递给第二阶段归因
```

---



## 五、关键文件


| 文件                                | 行数    | 职责                                |
| --------------------------------- | ----- | --------------------------------- |
| `routers/ai.py`                   | ~566  | 流水线编排                             |
| `core/parseval_data_generator.py` | ~3958 | Diff Graph + 造数 + 沙盒 + 变异         |
| `core/ast_schema.py`              | ~270  | SQLStructureIR + ASTDiffNode 数据模型 |
| `core/sql_judge.py`               | ~350  | 安全拦截                              |
| `core/sql_knowledge_points.py`    | ~378  | 22 个知识点分类体系                       |
| `core/sql_parser.py`              | ~113  | 输出列推断                             |




## 六、测试覆盖


| 测试文件                              | 用例数 | 覆盖范围                 |
| --------------------------------- | --- | -------------------- |
| `test_check_sql_flow.py`          | 4   | 语法错误、安全拦截、正确流程、全链路   |
| `test_parseval_data_generator.py` | 29  | 造数、变异、TacticRegistry、探针覆盖、完整流水线集成 |


