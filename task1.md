# 阶段一：SQL 算子精准定位与测试样例生成流程框架

在 SQL 智能教学系统中，**阶段一（Observe：证据采集与感知）** 的核心目标是：在学生提交 SQL 作答后，系统如何通过 AST 传感器、测试样例自动生成（ParSEval 算法）以及变分隔离测试（Mutation Testing），**精准定位到具体错误的 SQL 算子并动态生成具有针对性的测试数据**。

---

### 图例说明 (Legend)

- **I (Input)**: 输入数据结构 / 变量。
- **T (Tool / Technology / Formula / Algorithm)**: 执行代码模块、核心算法或数学公式。
- **O (Output)**: 输出数据结构 / 结果变量。

---

## 一、 算子精准定位与样例生成流程图 (Mermaid)

```mermaid
flowchart TD
    %% Phase 1: Input & Parser
    SUBMIT(["学生提交作答"]) --> I_INPUT["I: 输入数据 (student_sql, correct_sql, schema_text)"]
    I_INPUT --> T_AST_PARSE["T: AST 语法树解析验证"]

    %% Syntax Error Branch
    T_AST_PARSE -->|解析失败| O_SYNTAX_ERR["O: 语法解析错误记录"]
    O_SYNTAX_ERR --> EXIT_ERR(["结束：直接返回语法错误"])

    %% Successful Parsing leads to Parallel Sensors
    T_AST_PARSE -->|解析成功| AST_ROOT["AST 抽象语法树"]
    
    AST_ROOT --> T_AST_SENSOR["T: AST 结构传感器"]
    T_AST_SENSOR --> O_AST_FEAT["O: E_AST 结构特征对比与子句差分表"]

    AST_ROOT --> T_CONSTRAINT_EXT["T: 谓词/约束抽取"]
    T_CONSTRAINT_EXT --> O_CONSTRAINTS["O: constraints 列表"]

    %% Phase 2: Dynamic Data Generation
    I_INPUT -.->|提供 Schema 结构| T_DATA_GEN
    O_CONSTRAINTS --> T_DATA_GEN
    
    AST_ROOT -->|提取算子特征 (Distinct/Group/Having/Join等)| T_TACTIC_SELECT["动态匹配选择造数策略与探针"]
    T_TACTIC_SELECT --> T_DATA_GEN["T: 拓扑对齐与动态造数"]
    T_DATA_GEN --> O_MOCK_DB["O: E_db 动态测试数据集"]

    %% Phase 3: Sandbox Run
    O_MOCK_DB --> T_SANDBOX["T: 内存沙盒执行与等价性判定"]
    T_SANDBOX --> O_EXEC_RES["O: E_data 沙盒执行输出与等价判定"]

    %% Phase 4: Mutation Testing (Conditional on Equivalence)
    O_EXEC_RES -->|等价性为 False 时启动| T_MUTATION["T: 变分隔离测试"]
    
    AST_ROOT -.->|提供语法树进行变体比对| T_COMPARE_STRUCT["比对标准与学生 AST 结构差异"]
    T_COMPARE_STRUCT -->|识别差异算子 (WHERE/GROUP/HAVING等)| T_MUT_SELECT["选择待测试的变分算子"]
    T_MUT_SELECT --> T_MUT_GEN["构建对应的 mutant 变体查询"]
    
    T_MUTATION --> T_MUT_GEN
    T_MUT_GEN --> T_MUT_RUN["在沙盒中执行变体查询并对比"]
    T_MUT_RUN --> O_MUT_EVIDENCE["O: E_MUT 变分证据"]

    %% Phase 5: Decision & Phi Attribution Arbiter
    O_AST_FEAT --> T_ATTRIBUTION["T: 错因归因与证据包合并"]
    O_EXEC_RES --> T_ATTRIBUTION
    O_MUT_EVIDENCE --> T_ATTRIBUTION

    T_ATTRIBUTION --> O_FINAL_ATTRIBUTIONS["O: 首席仲裁归因结果"]
    O_FINAL_ATTRIBUTIONS --> EXIT_OK(["进入阶段二：错因诊断与归因判定"])
```



```
O_FINAL_ATTRIBUTIONS --> EXIT_OK(["进入阶段二：错因诊断与归因判定"])
```

---



## 二、 核心机制解析：如何实现精准定位与样例生成



### 1. 精准定位固定算子：变分隔离测试 (Mutation Testing)

当学生提交的 SQL 执行结果不正确时，仅仅通过 AST 静态对比或输出数据比对无法 100% 确认是哪个算子/子句写错了（例如：学生可能同时写错了 `WHERE` 和 `JOIN ON`）。系统通过**变分隔离测试**来进行单变量排查：

- **替换变体 (Replacement Mutation)**:
  - **操作**：将学生 SQL 中的某个差异算子（如 `WHERE`）剔除，强行把标准答案的 `WHERE` 子句移植进去（并在 `mutation_evidence` 中记录 `original_part` 和 `replacement_part` 细节），形成 Mutant-SQL，并在生成的沙盒数据库中运行。
  - **定位逻辑**：若 Mutant-SQL 的执行结果与标准答案**完全一致**（即 `fixed_by_replacement = True`），则在数学和实验上证明：**该算子的错误是导致整个 SQL 结果不正确的充分必要原因**。由此，系统可以精准定位错误根源。
- **移除变体 (Removal Mutation)**:
  - **操作**：将学生 SQL 中的某个算子（如 `HAVING`）完全移除，并在沙盒中运行。
  - **定位逻辑**：若移除该子句后，学生 SQL 的输出与原输出一致（即 `removed_student_clause_equivalent = True`），说明该算子在当前测试数据下是**冗余/无效果的**。



### 2. 算子定位映射表 (Operator to Knowledge Point Mapping)

系统在 `core/error_attribution.py` 中维护了一套严格的算子物理表征到知识点 (Knowledge Point, KP) 的映射关系（已针对教学大纲细化原子点）：


| 算子/子句                 | AST 节点类型 (Sqlglot)             | 对应 L1 知识点     | 对应 L2 知识点                                                           | 严重度权重 |
| --------------------- | ------------------------------ | ------------- | ------------------------------------------------------------------- | ----- |
| **SELECT (投影)**       | `exp.Select`                   | `KP_BASIC`    | `PROJ_COL` (投影列)                                                    | 0.3   |
| **WHERE (选择比较)**      | `exp.Where` / `exp.Comparison` | `KP_FILTER`   | `COMP_VAL` (值比较)                                                    | 0.5   |
| **WHERE (空值过滤)**      | `exp.Is` (包含 `exp.Null`)       | `KP_FILTER`   | `COMP_NULL` (NULL 判断)                                               | 0.5   |
| **ORDER BY (排序)**     | `exp.Order`                    | `KP_ORDER`    | `SORT_ASC` (排序)                                                     | 0.2   |
| **LIMIT (限制)**        | `exp.Limit` / `exp.Offset`     | `KP_BASIC`    | `LIMIT_OFF` (分页限制)                                                  | 0.2   |
| **DISTINCT (去重)**     | `exp.Distinct`                 | `KP_BASIC`    | `DISTINCT_SET` (去重)                                                 | 0.3   |
| **GROUP BY (分组)**     | `exp.Group`                    | `KP_AGG`      | `GB_SIMPLE` (分组聚合)                                                  | 0.6   |
| **HAVING (分组过滤)**     | `exp.Having`                   | `KP_AGG`      | `HV_SIMPLE` (分组后过滤)                                                 | 0.6   |
| **JOIN ON (连接)**      | `exp.Join`                     | `KP_JOIN`     | `JOIN_ON` / `JOIN_INNER` / `JOIN_LEFT` / `JOIN_RIGHT` / `JOIN_FULL` | 0.8   |
| **UNION (并集)**        | `exp.Union`                    | `KP_ADVANCED` | `SET_UNION` (集合并集)                                                  | 0.7   |
| **INTERSECT (交集)**    | `exp.Intersect`                | `KP_ADVANCED` | `SET_INTERSECT` (集合交集)                                              | 0.7   |
| **EXCEPT (差集)**       | `exp.Except`                   | `KP_ADVANCED` | `SET_EXCEPT` (集合差集)                                                 | 0.7   |
| **SUBQUERY (简单子查询)**  | `exp.Subquery` (非相关)           | `KP_SUBQUERY` | `SUB_TABLE` / `SUB_IN_ALL_ANY` / `SUB_EXISTS`                       | 0.6   |
| **SUBQUERY (相关子查询)**  | `exp.Subquery` (引用父表)          | `KP_SUBQUERY` | `SUB_CORR` (相关子查询)                                                  | 0.7   |
| **CTE (简单公用表)**       | `exp.CTE` (非递归)                | `KP_ADVANCED` | `CTE_SIMPLE` (简单公用表)                                                | 0.6   |
| **CTE (递归公用表)**       | `exp.CTE` (递归或自引用)             | `KP_ADVANCED` | `CTE_RECURSIVE` (递归公用表)                                             | 0.8   |
| **CASE WHEN (分支表达式)** | `exp.Case`                     | `KP_FUNC`     | `CASE_SEARCH` (分支控制)                                                | 0.4   |
| **WINDOW (窗口函数)**     | `exp.Window`                   | `KP_ADVANCED` | `WIN_OVER` (窗口计算)                                                   | 0.5   |




### 3. 精准定位的造数机制：基于三态交集划分与漂移的完备造数

ParSEval 算法通过以下机制动态造数，以确保精准捕捉逻辑差异：

1. **三态真值交集划分 (Truth-Intersection Partitioning)**：
  - 针对 `WHERE` 和 `HAVING` 的每个数值谓词边界值 $c$，强制在数据行中注入临界值 `c` (提供阴性 False 状态，即 $T_{diff}$)、`c + 1` (提供阳性 True 状态，即 $T_{both}$)、以及 `c - 1` (提供双阴性 False 状态，即 $T_{neither}$)。
  - **完备性意义**：保障数据集一定包含能够揭示 $Q_{std} \neq Q_{stu}$ 语义差异的测试元组，彻底消除了操作符滑移带来的漏判定。
2. **表名与列名多项式哈希与动态去重漂移**:
  - 在使用共享池对齐主外键（Topology Alignment）的基础之上，对键值采用 `(Table.Column)` 结合的多项式滚动哈希，并在 Join Group 内进行动态碰撞去重与漂移。
  - **完备性意义**：打破了同名关联列（如 `student.dept_name` 与 `advisor.dept_name`）或表内多外键（如 `s_ID` 与 `i_ID`）的假对齐，让 NATURAL JOIN 的过载多键条件和错误 Join Key 必然在沙盒中暴露差异。
3. **关系表末行 None 注入 (悬浮元组设计)**:
  - 针对 `takes`, `advisor`, `works` 等关系子表，强制将其最后一行的数据设为 `None`。
  - **完备性意义**：在数据库中构造了天然的非匹配悬浮元组，促使 `LEFT JOIN`（保留该孤儿行）与 `INNER JOIN`（截断该孤儿行）产生行数差异，保障外连接校验完备。
4. **去重探针过滤 (Duplicate Probe)**:
  - 限制只对排除 ID、SSN 等核心主键的普通/外键列进行去重行复制（Row 1 复写 Row 0 的值）。
  - **完备性意义**：在满足主键约束的安全范围内注入重复列值，使得缺少 `DISTINCT` 修饰符的查询在执行时必定产生行数膨胀，安全检验去重算子。
5. **沙盒递归安全熔断 (Recursive Sandbox Guard)**:
  - 注册 `conn.set_progress_handler`，一旦 SQLite 虚拟机执行超过 10 万个 VM 指令，立即中止抛出 `interrupted` 异常。
  - **完备性意义**：安全隔绝了递归 CTE（WITH RECURSIVE）成环或学生拼写死循环挂死系统的高危状况，确保系统安全。

---



## 三、 数据输入与输出规范



### 1. 输入数据接口 (Input Schema)

- `schema_text`: 教材或系统数据库的 Compact Schema，例如：
  ```
  EMPLOYEE(Fname, Lname, Ssn, Salary, Dno);
  DEPARTMENT(Dname, Dnumber, Mgr_ssn)
  ```
- `standard_sql`: 题目的标准参考答案。
- `student_sql`: 学生提交的待测 SQL 代码。



### 2. 中间过程数据 (Intermediate Datatypes)

- `constraints`: 提取出的算子约束列表
  ```json
  [
    { "column": "Salary", "op": "GT", "value": 30000 },
    { "column": "Dname", "op": "LIKE", "value": "Research" }
  ]
  ```
- `test_database`: 生成的表数据结构 (Table -> Rows)
  ```json
  {
    "EMPLOYEE": [
      { "Fname": "Alice", "Lname": "Smith", "Ssn": 1, "Salary": 30000, "Dno": 10 },
      { "Fname": "Bob", "Lname": "Brown", "Ssn": 2, "Salary": 35000, "Dno": 10 }
    ]
  }
  ```



### 3. 输出数据接口 (Output Schema)

`generate_and_compare` 的最终产物为包含执行细节的 `SandboxRun`，其中 `mutation_evidence` 已扩充：

- `is_equivalent`: `True / False`（沙盒执行是否完全等价）。
- `mutation_evidence`:
  ```json
  {
    "enabled": true,
    "summary": {
      "executed": 1,
      "fixed_by_replacement": 1
    },
    "tests": [
      {
        "clause": "WHERE",
        "knowledge_point_id": "where",
        "original_part": "WHERE Salary >= 30000",
        "replacement_part": "WHERE Salary > 30000",
        "replacement_sqlite": "SELECT * FROM EMPLOYEE WHERE Salary > 30000",
        "replacement_exec_ok": true,
        "replacement_equivalent": true,
        "fixed_by_replacement": true,
        "removal_sqlite": "SELECT * FROM EMPLOYEE",
        "removal_exec_ok": true,
        "removed_student_clause_equivalent": false
      }
    ]
  }
  ```
- `attributions`: (缺陷向量列表，新增细粒度字段映射)
  ```json
  [
    {
      "knowledge_point_id": "where",
      "l1_code": "KP_FILTER",
      "l2_code": "COMP_VAL",
      "clause": "WHERE",
      "error_type": "logical",
      "severity": 0.88,
      "confidence": 0.984,
      "detail": "把学生 SQL 的 WHERE 替换为标答对应子句后，动态沙盒结果变为正确；该子句是高优先错因。"
    }
  ]
  ```

