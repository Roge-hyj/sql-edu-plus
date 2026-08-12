import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_ROOT / "sql-edu-backend"))

from core.error_attribution import evidence_weights_from_observation
from core.parseval_data_generator import generate_and_compare


Validation = Callable[[dict[str, Any]], tuple[bool, str]]


def _rows(ctx: dict[str, Any], table: str) -> list[dict[str, Any]]:
    return ctx["run"].test_database.get(table, [])


def _kp_ids(ctx: dict[str, Any]) -> list[str]:
    return [item.knowledge_point_id for item in ctx["attr"].attributions]


def _check_kp(expected: str) -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        ids = _kp_ids(ctx)
        return expected in ids, f"expected KP={expected}, actual={ids}"

    return validate


def _check_numeric_tristate(table: str, column: str, boundary: int | float) -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        values = [row.get(column) for row in _rows(ctx, table)]
        required = {boundary - 1, boundary, boundary + 1}
        present = set(values)
        return required.issubset(present), f"{table}.{column} values={values}, required={sorted(required)}"

    return validate


def _check_column_shape_mismatch() -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        evidence = ctx["run"].data_evidence
        ok = evidence.get("columns_match") is False
        return ok, f"standard_columns={evidence.get('standard_columns')}, student_columns={evidence.get('student_columns')}"

    return validate


def _check_null_probe(table: str, column: str) -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        values = [row.get(column) for row in _rows(ctx, table)]
        return any(value is None for value in values), f"{table}.{column} values={values}"

    return validate


def _check_duplicate_probe(table: str, column: str) -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        values = [row.get(column) for row in _rows(ctx, table)]
        counts = Counter(values)
        duplicated = [value for value, count in counts.items() if count > 1]
        return bool(duplicated), f"{table}.{column} duplicate_values={duplicated}, values={values}"

    return validate


def _check_join_key_drift() -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        students = {row.get("ID") for row in _rows(ctx, "student")}
        advisor = _rows(ctx, "advisor")
        s_values = [row.get("s_ID") for row in advisor if row.get("s_ID") is not None]
        i_values = [row.get("i_ID") for row in advisor if row.get("i_ID") is not None]
        has_overlap = bool(students & set(s_values)) and bool(students & set(i_values))
        has_drift = any(row.get("s_ID") != row.get("i_ID") for row in advisor if row.get("s_ID") is not None and row.get("i_ID") is not None)
        return has_overlap and has_drift, f"student.ID={sorted(students)}, advisor.s_ID={s_values}, advisor.i_ID={i_values}"

    return validate


def _check_dangling_tuple(table: str, column: str) -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        values = [row.get(column) for row in _rows(ctx, table)]
        row_counts_diverge = ctx["run"].data_evidence.get("row_count_match") is False
        return any(value is None for value in values) and row_counts_diverge, f"{table}.{column} values={values}, evidence={ctx['run'].data_evidence}"

    return validate


def _group_metric(ctx: dict[str, Any], agg: str) -> dict[Any, float]:
    groups: dict[Any, list[float]] = defaultdict(list)
    for row in _rows(ctx, "instructor"):
        groups[row.get("dept_name")].append(row.get("salary"))
    metrics: dict[Any, float] = {}
    for key, values in groups.items():
        if agg == "SUM":
            metrics[key] = sum(values)
        elif agg == "AVG":
            metrics[key] = sum(values) / len(values)
        elif agg == "MIN":
            metrics[key] = min(values)
        elif agg == "MAX":
            metrics[key] = max(values)
        elif agg == "COUNT":
            metrics[key] = len(values)
    return metrics


def _check_aggregate_tristate(agg: str, boundary: int | float) -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        metrics = _group_metric(ctx, agg)
        values = list(metrics.values())
        has_above = any(value > boundary for value in values)
        has_equal = any(value == boundary for value in values)
        has_below = any(value < boundary for value in values)
        return has_above and has_equal and has_below, f"{agg} metrics={metrics}, boundary={boundary}"

    return validate


def _check_ordered_compare() -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        evidence = ctx["run"].data_evidence
        ok = evidence.get("ordered_compare") is True and evidence.get("is_equivalent_on_generated_data") is False
        return ok, f"evidence={evidence}"

    return validate


def _check_limit_counts(std_count: int, stu_count: int) -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        evidence = ctx["run"].data_evidence
        ok = evidence.get("standard_row_count") == std_count and evidence.get("student_row_count") == stu_count
        return ok, f"standard/student={evidence.get('standard_row_count')}/{evidence.get('student_row_count')}"

    return validate


def _check_subquery_overlap(parent_table: str, child_table: str, column: str) -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        parent_values = {row.get(column) for row in _rows(ctx, parent_table)}
        child_values = {row.get(column) for row in _rows(ctx, child_table)}
        return bool(parent_values & child_values), f"{parent_table}.{column}={sorted(parent_values, key=str)}, {child_table}.{column}={sorted(child_values, key=str)}"

    return validate


def _check_case_values(table: str, column: str, boundary: int | float) -> Validation:
    return _check_numeric_tristate(table, column, boundary)


def _check_window_partition_data() -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        rows = _rows(ctx, "instructor")
        group_counts = Counter(row.get("dept_name") for row in rows)
        salaries = [row.get("salary") for row in rows]
        ok = any(count > 1 for count in group_counts.values()) and len(set(salaries)) > 1
        return ok, f"dept group_counts={dict(group_counts)}, salaries={salaries}"

    return validate


def _check_recursive_cte_counts(std_count: int, stu_count: int) -> Validation:
    def validate(ctx: dict[str, Any]) -> tuple[bool, str]:
        evidence = ctx["run"].data_evidence
        ok = evidence.get("standard_row_count") == std_count and evidence.get("student_row_count") == stu_count
        return ok, f"standard/student={evidence.get('standard_row_count')}/{evidence.get('student_row_count')}, error={ctx['run'].error}"

    return validate


GENERATION_CASES: list[dict[str, Any]] = [
    {
        "strategy": "WHERE 数值边界三态",
        "schema": "course(course_id, title, dept_name, credits)",
        "standard": "SELECT title FROM course WHERE credits > 3;",
        "student": "SELECT title FROM course WHERE credits >= 3;",
        "expected_kp": "where",
        "checks": [_check_numeric_tristate("course", "credits", 3), _check_kp("where")],
    },
    {
        "strategy": "SELECT 投影列完整性",
        "schema": "course(course_id, title, dept_name, credits)",
        "standard": "SELECT title, credits FROM course WHERE credits > 3;",
        "student": "SELECT title FROM course WHERE credits > 3;",
        "expected_kp": "select-basic",
        "checks": [_check_numeric_tristate("course", "credits", 3), _check_column_shape_mismatch(), _check_kp("select-basic")],
    },
    {
        "strategy": "NULL 空值过滤探针",
        "schema": "student(ID, name, grade)",
        "standard": "SELECT name FROM student WHERE grade IS NULL;",
        "student": "SELECT name FROM student WHERE grade = NULL;",
        "expected_kp": "comp-null",
        "checks": [_check_null_probe("student", "grade"), _check_kp("comp-null")],
    },
    {
        "strategy": "DISTINCT 去重探针",
        "schema": "takes(ID, course_id, sec_id, semester, year, grade)",
        "standard": "SELECT DISTINCT course_id FROM takes;",
        "student": "SELECT course_id FROM takes;",
        "expected_kp": "distinct",
        "checks": [_check_duplicate_probe("takes", "course_id"), _check_kp("distinct")],
    },
    {
        "strategy": "JOIN 拓扑对齐与跨键漂移",
        "schema": "student(ID, name, dept_name); advisor(s_ID, i_ID)",
        "standard": "SELECT student.name FROM student JOIN advisor ON student.ID = advisor.s_ID;",
        "student": "SELECT student.name FROM student JOIN advisor ON student.ID = advisor.i_ID;",
        "expected_kp": "join-on",
        "checks": [_check_join_key_drift(), _check_kp("join-on")],
    },
    {
        "strategy": "LEFT JOIN 悬浮元组",
        "schema": "student(ID, name, dept_name); takes(ID, course_id)",
        "standard": "SELECT student.name, takes.course_id FROM student LEFT JOIN takes ON student.ID = takes.ID;",
        "student": "SELECT student.name, takes.course_id FROM student INNER JOIN takes ON student.ID = takes.ID;",
        "expected_kp": "join-left",
        "checks": [_check_dangling_tuple("takes", "ID"), _check_kp("join-left")],
    },
    {
        "strategy": "GROUP BY 分组粒度错",
        "schema": "instructor(ID, name, dept_name, salary, building)",
        "standard": "SELECT SUM(salary) FROM instructor GROUP BY dept_name;",
        "student": "SELECT SUM(salary) FROM instructor GROUP BY building;",
        "expected_kp": "group-by",
        "checks": [_check_kp("group-by")],
    },
    {
        "strategy": "HAVING SUM 聚合边界三态",
        "schema": "instructor(ID, name, dept_name, salary)",
        "standard": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING SUM(salary) > 80000;",
        "student": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING SUM(salary) < 80000;",
        "expected_kp": "having",
        "checks": [_check_aggregate_tristate("SUM", 80000), _check_kp("having")],
    },
    {
        "strategy": "HAVING AVG 聚合边界三态",
        "schema": "instructor(ID, name, dept_name, salary)",
        "standard": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING AVG(salary) > 50000;",
        "student": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING AVG(salary) < 50000;",
        "expected_kp": "having",
        "checks": [_check_aggregate_tristate("AVG", 50000), _check_kp("having")],
    },
    {
        "strategy": "HAVING MIN 聚合边界三态",
        "schema": "instructor(ID, name, dept_name, salary)",
        "standard": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING MIN(salary) > 30000;",
        "student": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING MIN(salary) < 30000;",
        "expected_kp": "having",
        "checks": [_check_aggregate_tristate("MIN", 30000), _check_kp("having")],
    },
    {
        "strategy": "HAVING MAX 聚合边界三态",
        "schema": "instructor(ID, name, dept_name, salary)",
        "standard": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING MAX(salary) > 90000;",
        "student": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING MAX(salary) < 90000;",
        "expected_kp": "having",
        "checks": [_check_aggregate_tristate("MAX", 90000), _check_kp("having")],
    },
    {
        "strategy": "HAVING COUNT 组大小三态",
        "schema": "instructor(ID, name, dept_name, salary)",
        "standard": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING COUNT(ID) >= 2;",
        "student": "SELECT dept_name FROM instructor GROUP BY dept_name HAVING COUNT(ID) > 2;",
        "expected_kp": "having",
        "checks": [_check_aggregate_tristate("COUNT", 2), _check_kp("having")],
    },
    {
        "strategy": "ORDER BY 有序精确比对",
        "schema": "course(course_id, title, dept_name, credits)",
        "standard": "SELECT title FROM course ORDER BY credits DESC;",
        "student": "SELECT title FROM course ORDER BY credits ASC;",
        "expected_kp": "order-by",
        "checks": [_check_ordered_compare(), _check_kp("order-by")],
    },
    {
        "strategy": "LIMIT 行数边界",
        "schema": "course(course_id, title, dept_name, credits)",
        "standard": "SELECT title FROM course LIMIT 3;",
        "student": "SELECT title FROM course LIMIT 5;",
        "expected_kp": "limit",
        "checks": [_check_limit_counts(3, 5), _check_kp("limit")],
    },
    {
        "strategy": "子查询内外层值域重合",
        "schema": "student(ID, name, dept_name); takes(ID, course_id, year)",
        "standard": "SELECT name FROM student WHERE ID IN (SELECT ID FROM takes WHERE year = 2017);",
        "student": "SELECT name FROM student WHERE ID NOT IN (SELECT ID FROM takes WHERE year = 2017);",
        "expected_kp": "where",
        "checks": [_check_subquery_overlap("student", "takes", "ID"), _check_kp("where")],
    },
    {
        "strategy": "相关子查询内外层关联",
        "schema": "student(ID, name, dept_name); takes(ID, course_id, year)",
        "standard": "SELECT name FROM student s WHERE EXISTS (SELECT 1 FROM takes t WHERE t.ID = s.ID AND t.year = 2017);",
        "student": "SELECT name FROM student s WHERE EXISTS (SELECT 1 FROM takes t WHERE t.ID = s.ID AND t.year = 2018);",
        "expected_kp": "subquery-correlated",
        "checks": [_check_subquery_overlap("student", "takes", "ID"), _check_numeric_tristate("takes", "year", 2017), _check_kp("subquery-correlated")],
    },
    {
        "strategy": "集合操作 UNION 去重差异",
        "schema": "course(course_id, title, dept_name, credits)",
        "standard": "SELECT title FROM course WHERE dept_name = 'Math' UNION SELECT title FROM course WHERE dept_name = 'Physics';",
        "student": "SELECT title FROM course WHERE dept_name = 'Math' UNION ALL SELECT title FROM course WHERE dept_name = 'Physics';",
        "expected_kp": "union",
        "checks": [_check_kp("union")],
    },
    {
        "strategy": "集合操作 INTERSECT 交集差异",
        "schema": "course(course_id, title, dept_name, credits)",
        "standard": "SELECT title FROM course WHERE dept_name = 'Math' INTERSECT SELECT title FROM course WHERE credits > 3;",
        "student": "SELECT title FROM course WHERE dept_name = 'Math' UNION SELECT title FROM course WHERE credits > 3;",
        "expected_kp": "intersect",
        "checks": [_check_kp("intersect")],
    },
    {
        "strategy": "集合操作 EXCEPT 排他差异",
        "schema": "course(course_id, title, dept_name, credits)",
        "standard": "SELECT title FROM course EXCEPT SELECT title FROM course WHERE dept_name = 'Physics';",
        "student": "SELECT title FROM course;",
        "expected_kp": "except",
        "checks": [_check_kp("except")],
    },
    {
        "strategy": "CASE WHEN 分支边界三态",
        "schema": "sales(sale_id, category, amount)",
        "standard": "SELECT category, SUM(CASE WHEN amount > 100 THEN amount ELSE 0 END) AS big_sales FROM sales GROUP BY category;",
        "student": "SELECT category, SUM(CASE WHEN amount >= 100 THEN amount ELSE 0 END) AS big_sales FROM sales GROUP BY category;",
        "expected_kp": "case",
        "checks": [_check_case_values("sales", "amount", 100), _check_kp("case")],
    },
    {
        "strategy": "WINDOW 分区与排序数据",
        "schema": "instructor(ID, name, dept_name, salary)",
        "standard": "SELECT name, ROW_NUMBER() OVER (PARTITION BY dept_name ORDER BY salary DESC) AS rank FROM instructor;",
        "student": "SELECT name, ROW_NUMBER() OVER (ORDER BY salary DESC) AS rank FROM instructor;",
        "expected_kp": "window-row-number",
        "checks": [_check_window_partition_data(), _check_kp("window-row-number")],
    },
    {
        "strategy": "CTE 基表约束传递",
        "schema": "works(company_name, person_name, salary); company(company_name, city)",
        "standard": "WITH big_co AS (SELECT company_name FROM company WHERE city = 'Beijing') SELECT person_name, salary FROM works JOIN big_co ON works.company_name = big_co.company_name WHERE salary > 10000;",
        "student": "WITH big_co AS (SELECT company_name FROM company WHERE city = 'Beijing') SELECT person_name, salary FROM works JOIN big_co ON works.company_name = big_co.company_name WHERE salary < 10000;",
        "expected_kp": "where",
        "checks": [_check_numeric_tristate("works", "salary", 10000), _check_kp("where")],
    },
    {
        "strategy": "递归 CTE 终止边界与沙盒熔断",
        "schema": "dummy(id)",
        "standard": "WITH RECURSIVE nums AS (SELECT 1 AS n UNION ALL SELECT n + 1 AS n FROM nums WHERE n < 3) SELECT n FROM nums;",
        "student": "WITH RECURSIVE nums AS (SELECT 1 AS n UNION ALL SELECT n + 1 AS n FROM nums WHERE n < 5) SELECT n FROM nums;",
        "expected_kp": "cte-recursive",
        "checks": [_check_recursive_cte_counts(3, 5), _check_kp("cte-recursive")],
    },
]


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    run = generate_and_compare(case["schema"], case["standard"], case["student"])
    is_correct = bool(run.is_equivalent)
    if run.error:
        is_correct = False
    attr = evidence_weights_from_observation(
        student_sql=case["student"],
        answer_sql=case["standard"],
        is_correct=is_correct,
        error_message=run.error or run.data_evidence.get("student_exec_error"),
        judge_detail=run.data_evidence,
        mutation_detail=run.mutation_evidence,
    )
    ctx = {"case": case, "run": run, "attr": attr, "is_correct": is_correct}
    checks = []
    for validate in case["checks"]:
        ok, detail = validate(ctx)
        checks.append({"ok": ok, "detail": detail})
    return {
        "strategy": case["strategy"],
        "schema": case["schema"],
        "standard": case["standard"],
        "student": case["student"],
        "expected_kp": case["expected_kp"],
        "is_correct": is_correct,
        "kp_ids": _kp_ids(ctx),
        "checks": checks,
        "passed": (not is_correct) and all(item["ok"] for item in checks),
        "data_evidence": run.data_evidence,
        "ast_diffs": run.data_evidence.get("ast_diffs", []),
        "generation_tactics": run.data_evidence.get("generation_tactics", []),
        "test_database": run.test_database,
        "standard_rows": run.standard_rows[:5],
        "student_rows": run.student_rows[:5],
    }


def _compact_db(db: dict[str, list[dict[str, Any]]]) -> str:
    return json.dumps(db, ensure_ascii=False, indent=2)


def render_report(results: list[dict[str, Any]]) -> str:
    explanations = {
        "WHERE 数值边界三态": "针对数值谓词条件中的边界 $c$（如 `> c`、`<= c`），在数据行中强行注入临界值 $[c, c + 1, c - 1]$，分别覆盖均符合区 ($T_{both}$)、临界差异区 ($T_{diff}$) 和均不符合区 ($T_{neither}$)。这能打破比较操作符（如 `>` 与 `>=`）在常规随机值下的假等价遮蔽，迫使边界逻辑错误显形。",
        "SELECT 投影列完整性": "在数据生成阶段，根据 SQL 语法树仅对引用的列生成种子值限制行宽。当学生 SQL 漏投、多投或改写了投影字段（导致列名或列数不符）时，沙盒执行引擎的列结构验证机制（`columns_match`）将直接拦截并在 `select-basic`（投影缺失/错误）知识点上归因。",
        "NULL 空值过滤探针": "主动在某些数据行中注入 `None` (SQL 中的 `NULL`)，同时在其它行生成普通有效值。由于 SQL 采用三值逻辑，非标准的 `col = NULL` 比较永远返回 `Unknown` (即过滤后的空集)，而标准的 `col IS NULL` 能够匹配 `None` 行。因此，主动注入 `None` 能产生悬殊的执行结果差异。",
        "DISTINCT 去重探针": "在满足数据表唯一性约束（排除 ID/SSN 等核心主键）的安全范围内，在 Row 0 和 Row 1 的非主键列上复制生成完全重复的数据行。当学生漏写 `DISTINCT` 去重修饰符时，学生 SQL 的执行结果将产生行数膨胀（包含重复行），与标准去重 SQL 产生行数分化。",
        "JOIN 拓扑对齐与跨键漂移": "使用多项式滚动哈希（Polynomial Hashing）对同组的 `table.column` 分配确定性且互不重合的偏移量（Shift），并在 Join Group 共享值池内进行动态碰撞排重。这能打乱同一行中多个外键列的值，防止因数据过于对称（如 `s_ID` 与 `i_ID` 相同）导致错连连接键（ON 条件）被同构屏蔽。",
        "LEFT JOIN 悬浮元组": "对关系子表（如 `takes`、`advisor`）的最后一行强制赋予 `None`，作为未匹配的“孤儿行”。这构建了天然的外连接悬浮元组，使得 `LEFT JOIN`（保留该孤儿行并填充 NULL）与 `INNER JOIN`（剔除该行）产生行数和空值项差异。",
        "GROUP BY 分组粒度错": "系统对每张表默认生成 4~8 行，并在分组列上填充多个不同的异构分类键。这能保证当学生把分组字段写错（例如按 `building` 错写为按 `dept_name` 分组）时，各组 of 聚合与累加组合必然发生错位，导致求和或计数数组与标答不等价。",
        "HAVING SUM 聚合边界三态": "由于 HAVING 过滤发生在分组聚合之后，不能直接改写基表单行数据。系统将记录按分组归类，并分别对各组数据做三态控制，使各分组的聚合 `SUM` 目标值精确达到 $c + 1$（阳性通过）、$c$（临界差异）和 $c - 1$（阴性过滤）。再除以组内行数 $k$ 填充回单行记录中，激活 HAVING 谓词边界过滤。",
        "HAVING AVG 聚合边界三态": "同 SUM 策略。控制每个分组的 `AVG`（均值）结果值，使其分别精确达到 $[c+1, c, c-1]$。数据生成时，直接使该分组内所有行的数值列均等于对应的目标值，使其平均值精确被控，引爆 HAVING 边界判断差异。",
        "HAVING MIN 聚合边界三态": "控制每个分组的 `MIN`（极小值）结果值，使其分别达到 $[c+1, c, c-1]$。数据干预时，将该组 Row 0 设为目标值 `T`，组内其余行均设为 `T + 1`，保证该组的最小值精确锁定在 `T`，校验极值过滤逻辑。",
        "HAVING MAX 聚合边界三态": "控制每个分组的 `MAX`（极大值）结果值，使其分别达到 $[c+1, c, c-1]$。数据干预时，将该组 Row 0 设为目标值 `T`，组内其余行均设为 `T - 1`，保证该组的最大值精确锁定在 `T`，校验极值过滤逻辑。",
        "HAVING COUNT 组大小三态": "对于行记录数限制（`COUNT`），无法通过改写数值列生效。本策略直接重排和限制分组列的物理键分配，使得各分组的物理行数（即组大小）分别等于 $[c+1, c, c-1]$，从而当比较行数写错时直接被沙盒过滤拦截。",
        "ORDER BY 有序精确比对": "提取排序关键字并生成具有单调递增/递减特征的数据序列。一旦检测到 SQL 中包含 `ORDER BY`，沙盒结果比对模块将关闭无序的频次比对（Counter），转为严格的顺序列表比对（`std_rows == stu_rows`），使排序方向或排序列写错直接暴露。",
        "LIMIT 行数边界": "限制输出的元组行数。通过沙盒直接验证 `LIMIT` 或 `OFFSET` 参数的数值偏差，让多取或少取数据的学生 SQL 产生行数不等。",
        "子查询内外层值域重合": "提取子查询中的关联列，在父子表之间建立主外键或数据范围的重合（对齐 ID 共享值池，打通拓扑通路）。再配合子查询内部的过滤谓词，强行在子表中构造阳性重合行（满足过滤）、阴性混淆行（不满足过滤）和悬浮行，触发子查询的过滤选择权，暴露内外层值域逻辑错。",
        "相关子查询内外层关联": "静态扫描子查询的过滤条件，识别并提取外层主表的引用列（如 `t.ID = s.ID`）。在生成数据时，确保被引用的主表 ID 与子查询中关联表的 ID 发生数据交叉（在内层表生成对应相关变量的多态数据），以便在子查询被多次关联扫描时，逻辑漏洞能被沙盒识别。",
        "集合操作 UNION 去重差异": "在集合算子（`UNION` / `UNION ALL`）左右两侧 of 子查询结果中生成完全重复的行。当学生混淆 `UNION`（集合自动去重）与 `UNION ALL`（保留所有重复行）时，学生 SQL 的执行输出将包含额外的重复行，行数随之分化。",
        "集合操作 INTERSECT 交集差异": "在数据生成阶段，分别生成“仅满足左侧条件”、“仅满足右侧条件”以及“同时满足两侧条件”的记录。当学生错写集合操作符（如用 `UNION` 替代了 `INTERSECT`）时，沙盒执行结果将从交集空集或子集膨胀为并集，暴露逻辑错。",
        "集合操作 EXCEPT 排他差异": "提取 EXCEPT 右侧的过滤条件并在数据中生成排他数据行。这能保证当学生漏写了 `EXCEPT` 差集排除逻辑时，学生 SQL 的输出中会多出本应该被剔除的行，打破等价性。",
        "CASE WHEN 分支边界三态": "针对 CASE WHEN 块中的各个分支条件（如 `amount > 100`），分别产生满足三态边界（$c$、$c+1$、$c-1$）的测试数据，从而在沙盒执行时强制遍历所有计算和转换分支，校验条件边界的准确性。",
        "WINDOW 分区与排序数据": "提取窗口函数的排序列与分区列，在数据行中产生乱序值和重复的分组值。如果学生在 `OVER` 子句中遗漏了 `PARTITION BY`，排序编号会出现全局自增而非分区独立重置的特征，导致数据不一致。",
        "CTE 基表约束传递": "回溯 CTE（WITH 表达式）内部引用的底层基表并针对这些基表进行三态造数，而拒绝直接预造 CTE 临时表。CTE 定义与外层 `JOIN` 均交给 SQLite 原生执行，确保基表约束能自然传导至最外层，校验 CTE 基表约束传递性。",
        "递归 CTE 终止边界与沙盒熔断": "静态检测 `WITH RECURSIVE` 结构。除了在自引用序列上产生离散数据校验终止边界外，还在 SQLite 沙盒执行时启用虚拟机周期计数器（Progress Handler），将指令周期锁定在 10 万个以内。一旦死循环立即熔断，防止系统被学生错误 SQL 挂死。"
    }
    lines = [
        "## 六、动态造数策略完备性专项检验",
        "",
        "本节逐项对应 `task3.md` 的动态造数策略，验证两个层面：",
        "",
        "1. **策略完备性**：生成的数据必须包含能区分标准 SQL 与学生 SQL 的攻击样本，例如三态边界、重复探针、JOIN 键漂移、悬浮元组或聚合边界组。",
        "2. **实现完备性**：实际后端 `generate_and_compare` 必须在这些数据上判定不等价，并产出非空且命中预期 KP 的归因。",
        "",
        "| 策略板块 | 沙盒等价 | 期望 KP | 实际 KP | 策略检查 | 结论 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for result in results:
        check_status = "；".join("PASS" if check["ok"] else "FAIL" for check in result["checks"])
        lines.append(
            f"| {result['strategy']} | `{result['is_correct']}` | `{result['expected_kp']}` | "
            f"`{', '.join(result['kp_ids'])}` | `{check_status}` | `{'PASS' if result['passed'] else 'FAIL'}` |"
        )

    for result in results:
        lines.extend(
            [
                f"\n### {result['strategy']}",
                f"* **策略说明**：{explanations.get(result['strategy'], '')}",
                f"* **Schema**: `{result['schema']}`",
                "* **标准答案 SQL**:",
                f"```sql\n{result['standard']}\n```",
                "* **学生作答 SQL**:",
                f"```sql\n{result['student']}\n```",
                f"* **沙盒判定等价性**: `{result['is_correct']}`",
                f"* **归因 KP**: `{result['kp_ids']}`",
                f"* **AST 差异子树**: `{result['ast_diffs']}`",
                f"* **差异驱动造数策略**: `{result['generation_tactics']}`",
                "* **策略检查结果**:",
            ]
        )
        for idx, check in enumerate(result["checks"], 1):
            lines.append(f"  {idx}. `{'PASS' if check['ok'] else 'FAIL'}` - {check['detail']}")
        lines.extend(
            [
                "* **动态生成的数据集**:",
                "```json",
                _compact_db(result["test_database"]),
                "```",
                f"* **标准输出样本**: `{result['standard_rows']}`",
                f"* **学生输出样本**: `{result['student_rows']}`",
            ]
        )
    return "\n".join(lines)


def render_operator_summary() -> str:
    rows = [
        ("选择", "WHERE", "`exp.Where`, `exp.Comparison`", "谓词边界三态 `[c, c+1, c-1]`", "替换/移除 WHERE", "`where`"),
        ("空值过滤", "`IS NULL` / `= NULL`", "`exp.Is`, `exp.Null`", "注入 `None` 行，区分 `IS NULL` 与 `= NULL`", "伴随 WHERE 证据归因", "`comp-null`"),
        ("投影", "SELECT", "`exp.Select`", "按引用列生成并校验列结构", "不单独变分，随数据证据归因", "`select-basic`"),
        ("去重", "DISTINCT", "`exp.Distinct`", "对 DISTINCT 投影列注入重复值", "不单独变分，随行数/重复证据归因", "`distinct`"),
        ("连接", "JOIN ON / USING", "`exp.Join`", "共享键池、同组外键漂移、外连接悬浮元组", "JOIN ON 条件替换", "`join-on`, `join-inner`, `join-left`, `join-right`, `join-full`"),
        ("分组", "GROUP BY", "`exp.Group`", "生成多组分类键，暴露分组粒度错误", "替换 GROUP BY", "`group-by`"),
        ("分组过滤", "HAVING", "`exp.Having`", "SUM/AVG/MIN/MAX 聚合三态；COUNT 组大小三态", "替换/移除 HAVING", "`having`"),
        ("排序", "ORDER BY", "`exp.Order`", "生成单调/乱序值并启用有序精确比对", "替换 ORDER BY", "`order-by`"),
        ("限制", "LIMIT / OFFSET", "`exp.Limit`, `exp.Offset`", "校验标准/学生输出行数边界", "替换 LIMIT/OFFSET", "`limit`"),
        ("简单子查询", "IN / EXISTS / 标量子查询", "`exp.Subquery`, `exp.In`, `exp.Exists`", "父子表值域重合与子查询过滤探针", "随 WHERE 子句变分", "`subquery-scalar`, `subquery-in`, `subquery-exists`"),
        ("相关子查询", "引用外层表的子查询", "`exp.Subquery`, `exp.Exists`", "内外层关联列交叉数据与过滤边界", "随 WHERE 子句变分", "`subquery-correlated`"),
        ("简单 CTE", "WITH", "`exp.CTE`", "只生成底层基表，CTE 由 SQLite 原生执行", "暂不单独变分", "`cte`"),
        ("递归 CTE", "WITH RECURSIVE", "`exp.CTE`, `exp.With`", "递归终止边界与 SQLite progress handler 熔断", "暂不单独变分", "`cte-recursive`"),
        ("并集", "UNION / UNION ALL", "`exp.Union`", "两侧谓词联合造数并校验去重差异", "集合算子差异归因", "`union`"),
        ("交集", "INTERSECT", "`exp.Intersect`", "构造左侧、右侧、交集三类记录", "集合算子差异归因", "`intersect`"),
        ("差集", "EXCEPT", "`exp.Except`", "抽取右侧过滤条件并生成排他数据行", "集合算子差异归因", "`except`"),
        ("条件分支", "CASE WHEN", "`exp.Case`", "CASE 条件边界三态并遍历分支", "CASE 差异归因", "`case`"),
        ("窗口函数", "OVER", "`exp.Window`", "重复分区键与乱序排序值，验证分区/排名", "窗口 OVER 差异归因", "`window-row-number`"),
    ]
    lines = [
        "## 七、阶段一 SQL 算子覆盖与策略总结表",
        "",
        "本表按当前主链路整理：`generate_and_compare` 负责动态造数、沙盒执行与变分证据，`evidence_weights_from_observation` 负责阶段一归因合并。",
        "",
        "| 算子类别 | SQL 表现 | Sqlglot AST 节点 | 动态造数策略 | 变分/归因机制 | KP ID |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    lines.extend(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} | {row[5]} |" for row in rows)
    return "\n".join(lines)


def write_reports(results: list[dict[str, Any]]) -> None:
    section = render_report(results) + "\n\n" + render_operator_summary()
    standalone = PROJECT_ROOT / "task2_generation_completeness.md"
    standalone.write_text("# 动态造数策略完备性专项检验\n\n" + section, encoding="utf-8")

    task2 = PROJECT_ROOT / "task2.md"
    marker = "## 六、动态造数策略完备性专项检验"
    content = task2.read_text(encoding="utf-8") if task2.exists() else ""
    if marker in content:
        content = content[: content.index(marker)].rstrip()
    if task2.exists():
        task2.write_text(content.rstrip() + "\n\n" + section + "\n", encoding="utf-8")
    print(f"Generation completeness report written to {standalone}")
    if task2.exists():
        print(f"Generation completeness section appended to {task2}")


def main() -> None:
    results = [run_case(case) for case in GENERATION_CASES]
    write_reports(results)
    for result in results:
        print(
            f"{result['strategy']}: eq={result['is_correct']} "
            f"kp={result['kp_ids']} passed={result['passed']}"
        )
    failed = [result for result in results if not result["passed"]]
    if failed:
        raise SystemExit(f"{len(failed)} generation completeness cases failed")


if __name__ == "__main__":
    main()
