"""ParSEval-inspired test data generation for SQL evidence collection.

This is a bounded, practical implementation for the SQL tutoring pipeline.
It generates small operator-targeted databases from:
- schema text
- standard SQL
- student SQL

The generated rows are designed to expose common SQL DQL mistakes:
WHERE predicates, LIKE/IN/BETWEEN/NULL, JOIN links, GROUP BY/HAVING,
DISTINCT, ORDER BY, LIMIT/TOP, and simple aggregation differences.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from collections import Counter
import re
import sqlite3

import sqlglot
from sqlglot import ErrorLevel, exp


@dataclass
class SandboxRun:
    executed: bool
    is_equivalent: bool | None
    error: str | None
    standard_sqlite: str | None
    student_sqlite: str | None
    standard_rows: list[tuple[Any, ...]]
    student_rows: list[tuple[Any, ...]]
    standard_columns: list[str]
    student_columns: list[str]
    test_database: dict[str, list[dict[str, Any]]]
    data_evidence: dict[str, Any]
    mutation_evidence: dict[str, Any]


NUMERIC_HINTS = (
    "id", "no", "number", "num", "year", "cred", "credit", "salary", "budget",
    "capacity", "price", "unit", "stock", "order", "level", "hours", "discount",
    "freight", "count", "amount", "qty", "quantity", "ssn", "dno", "dnum", "pno",
)
DATE_HINTS = ("date", "bdate", "start", "end", "time")


def parse_schema_text(schema: str) -> dict[str, list[str]]:
    """Parse compact schema text like table(col, col); [Order Details](...)."""
    tables: dict[str, list[str]] = {}
    for raw_part in schema.split(";"):
        part = raw_part.strip()
        if not part or "(" not in part or ")" not in part:
            continue
        name = part[: part.find("(")].strip()
        cols = part[part.find("(") + 1 : part.rfind(")")]
        table_name = _clean_identifier(name)
        columns = [_clean_identifier(col.strip().split()[0]) for col in cols.split(",") if col.strip()]
        columns = [col for col in columns if col]
        if table_name and columns:
            tables[table_name] = columns
    return tables


def generate_and_compare(
    schema_text: str,
    standard_sql: str,
    student_sql: str,
    *,
    max_rows_per_table: int = 8,
) -> SandboxRun:
    schema = parse_schema_text(schema_text)
    if _mentions_sys_views(standard_sql) or _mentions_sys_views(student_sql):
        schema.setdefault("Sys.Views", ["Name"])
    if not schema and (_extract_table_names(standard_sql) or _extract_table_names(student_sql)):
        return _failed("schema_parse_failed", None, None, {}, [], [])

    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    rows = generate_test_database(schema, standard_sql, student_sql, max_rows_per_table=max_rows_per_table)

    standard_sqlite = transpile_to_sqlite(standard_sql)
    student_sqlite = transpile_to_sqlite(student_sql)
    if not standard_sqlite or not student_sqlite:
        return _failed("sql_transpile_failed", standard_sqlite, student_sqlite, rows, [], [])

    try:
        std_cols, std_rows = _execute_sqlite(schema, rows, standard_sqlite)
    except Exception as exc:
        return _failed(f"standard_sql_failed: {exc}", standard_sqlite, student_sqlite, rows, [], [])

    try:
        stu_cols, stu_rows = _execute_sqlite(schema, rows, student_sqlite)
        student_exec_error = None
    except Exception as exc:
        stu_cols, stu_rows = [], []
        student_exec_error = str(exc)

    ordered = _has_node(standard_ast, exp.Order)
    if student_exec_error:
        is_equivalent = False
    elif ordered:
        is_equivalent = std_cols == stu_cols and std_rows == stu_rows
    else:
        is_equivalent = std_cols == stu_cols and Counter(std_rows) == Counter(stu_rows)

    evidence = _build_data_evidence(
        is_equivalent=is_equivalent,
        ordered=ordered,
        standard_columns=std_cols,
        student_columns=stu_cols,
        standard_rows=std_rows,
        student_rows=stu_rows,
        standard_ast=standard_ast,
        student_ast=student_ast,
        student_exec_error=student_exec_error,
    )
    mutation_evidence = _run_mutation_tests(
        schema=schema,
        rows=rows,
        standard_sql=standard_sql,
        student_sql=student_sql,
        standard_columns=std_cols,
        standard_rows=std_rows,
        ordered=ordered,
    )
    return SandboxRun(
        executed=True,
        is_equivalent=is_equivalent,
        error=None,
        standard_sqlite=standard_sqlite,
        student_sqlite=student_sqlite,
        standard_rows=std_rows,
        student_rows=stu_rows,
        standard_columns=std_cols,
        student_columns=stu_cols,
        test_database=rows,
        data_evidence=evidence,
        mutation_evidence=mutation_evidence,
    )


def generate_test_database(
    schema: dict[str, list[str]],
    standard_sql: str,
    student_sql: str,
    *,
    max_rows_per_table: int = 8,
) -> dict[str, list[dict[str, Any]]]:
    """
    根据 Schema 以及标答和学生 SQL 提取的语法约束，动态为各表生成隔离测试数据。
    Generates test data dynamically for target database tables based on Schema and SQL predicate constraints.

    实现流程 (Implementation steps):
    1. 提取标答与学生 SQL 中所有的字面量约束条件 (如 WHERE, IN, LIKE, HAVING 等)；
    2. 计算查询语句涉及的目标物理表集合，过滤无关的表；
    3. 构建主外键拓扑对齐的值池 (Shared Values)，保证 JOIN 连接能匹配上；
    4. 逐行填充基础数值种子数据 (_base_value)，然后将谓词三态和空值探针约束注入源数据；
    5. 针对 DISTINCT 去重进行数据行的重复复制探测 (_add_duplicate_probe)。
    """
    # 1. 抽取标答与作答 SQL 内的所有比较、LIKE、IN、BETWEEN 和 NULL 等谓词字面量约束
    constraints = _extract_literal_constraints(standard_sql) + _extract_literal_constraints(student_sql)
    
    # 2. 筛选查询涉及到的表，仅为其生成测试数据以节省内存和执行开销
    tables_in_queries = _extract_table_names(standard_sql) | _extract_table_names(student_sql)
    if tables_in_queries:
        target_tables = {
            table: cols
            for table, cols in schema.items()
            if _norm_name(table) in tables_in_queries
        }
        if _mentions_sys_views(standard_sql) or _mentions_sys_views(student_sql):
            target_tables["Sys.Views"] = schema.get("Sys.Views", ["Name"])
        if not target_tables:
            target_tables = schema
    else:
        target_tables = schema

    # 3. 限制生成行数在 4 至 8 行之间，保证 LIMIT/OFFSET 查询能有切片效果
    row_count = max(4, min(max_rows_per_table, 8))
    
    # 4. 构建关联表的主外键种子池，保证 JOIN 条件不为空，解决拓扑对齐与多外键错位偏移
    shared_values = _build_shared_values(target_tables, row_count)
    data: dict[str, list[dict[str, Any]]] = {}

    for table, columns in target_tables.items():
        rows: list[dict[str, Any]] = []
        for idx in range(row_count):
            row = {}
            for col in columns:
                # 填充各字段的基础值（包括 Outer Join 不对称悬浮元组的 None 填充）
                row[col] = _base_value(col, idx, shared_values)
            rows.append(row)
        
        # 5. 注入数值边界三态值、HAVING 聚合以及 NULL 空值探针数据
        _apply_constraints(rows, columns, constraints)
        
        # 6. 在 Row 0 和 Row 1 的非主键字段注入完全重复的行，用以探测去重 DISTINCT 的缺失
        _add_duplicate_probe(rows, columns)
        
        data[table] = rows[:max_rows_per_table]

    return data


def transpile_to_sqlite(sql: str) -> str | None:
    manual = _manual_sqlite_compat(sql)
    if manual and re.match(r"(?is)^\s*(select\s+top|with\s+recursive|\(?\s*select)", sql):
        return manual
    for dialect in ("tsql", "sqlite", "mysql"):
        try:
            candidates = sqlglot.transpile(sql, read=dialect, write="sqlite", error_level=ErrorLevel.IGNORE)
            if candidates:
                return _sqlite_compat(candidates[0])
        except Exception:
            continue
    return _manual_sqlite_compat(sql)


def _failed(
    error: str,
    standard_sqlite: str | None,
    student_sqlite: str | None,
    rows: dict[str, list[dict[str, Any]]],
    std_rows: list[tuple[Any, ...]],
    stu_rows: list[tuple[Any, ...]],
) -> SandboxRun:
    return SandboxRun(
        executed=False,
        is_equivalent=None,
        error=error,
        standard_sqlite=standard_sqlite,
        student_sqlite=student_sqlite,
        standard_rows=std_rows,
        student_rows=stu_rows,
        standard_columns=[],
        student_columns=[],
        test_database=rows,
        data_evidence={
            "sandbox_executed": False,
            "sandbox_error": error,
        },
        mutation_evidence={
            "enabled": False,
            "summary": {"executed": 0, "fixed_by_replacement": 0},
            "tests": [],
            "error": error,
        },
    )


def _parse_sql(sql: str) -> exp.Expression | None:
    for dialect in ("tsql", "sqlite", "mysql"):
        try:
            parsed = sqlglot.parse_one(sql, dialect=dialect, error_level=ErrorLevel.IGNORE)
            if parsed is not None:
                return parsed
        except Exception:
            continue
    return None


def _has_node(ast: exp.Expression | None, node_type: type[exp.Expression]) -> bool:
    return bool(ast and ast.find(node_type))


def _extract_table_names(sql: str) -> set[str]:
    ast = _parse_sql(sql)
    if not ast:
        return set()
    return {_norm_name(table.name) for table in ast.find_all(exp.Table)}


def _extract_literal_constraints(sql: str) -> list[dict[str, Any]]:
    ast = _parse_sql(sql)
    if not ast:
        return []
    constraints: list[dict[str, Any]] = []
    for node in ast.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
        left, right = node.left, node.right
        if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
            constraints.append({"column": left.name, "op": type(node).__name__, "value": _literal_value(right)})
        elif isinstance(right, exp.Column) and isinstance(left, exp.Literal):
            constraints.append({"column": right.name, "op": type(node).__name__, "value": _literal_value(left)})
    for node in ast.find_all(exp.Like):
        if isinstance(node.this, exp.Column) and isinstance(node.expression, exp.Literal):
            constraints.append({"column": node.this.name, "op": "LIKE", "value": _literal_value(node.expression)})
    for node in ast.find_all(exp.In):
        if isinstance(node.this, exp.Column):
            values = [_literal_value(item) for item in node.expressions if isinstance(item, exp.Literal)]
            if values:
                constraints.append({"column": node.this.name, "op": "IN", "value": values[0], "values": values})
    for node in ast.find_all(exp.Between):
        if isinstance(node.this, exp.Column):
            constraints.append({"column": node.this.name, "op": "BETWEEN", "value": _literal_value(node.args.get("low"))})
            constraints.append({"column": node.this.name, "op": "BETWEEN", "value": _literal_value(node.args.get("high"))})
    for node in ast.find_all(exp.Is):
        if isinstance(node.this, exp.Column):
            constraints.append({"column": node.this.name, "op": "IS", "value": None})
    return constraints


def _literal_value(node: exp.Expression | None) -> Any:
    if node is None:
        return None
    if isinstance(node, exp.Literal):
        value = node.this
        if node.is_number:
            try:
                return int(value)
            except Exception:
                try:
                    return float(value)
                except Exception:
                    return value
        return value
    return getattr(node, "this", None)


def _apply_constraints(rows: list[dict[str, Any]], columns: list[str], constraints: list[dict[str, Any]]) -> None:
    """
    根据提取的语法约束，将特定值写入数据行中的对应列，并生成对抗性反例值（Counter-Value）。
    Applies extracted predicate constraints to columns by setting values in database rows
    and generating counter-values in the last row to expose logic errors.

    策略解析 (Strategy details):
    1. 分组：将约束按目标列分类。
    2. 阳性测试数据 (Positive Cases)：在前一半的数据行中，循环填入该谓词约束中出现的字面量值（如 18, 'Alice' 等），确保有符合条件的行。
    3. 阴性测试数据 / 对抗反例 (Negative Cases/Counter-Values)：在最后一行注入对抗反例（_counter_value，如 18+999 = 1017, 'not_Alice' 等）。
       如果学生逻辑有漏洞（例如无条件选择、或操作符写反），反例行的数据会暴露此错误。
    """
    # 按列对约束进行聚合分组
    by_col: dict[str, list[dict[str, Any]]] = {}
    column_lookup = {_norm_name(col): col for col in columns}
    for constraint in constraints:
        col = column_lookup.get(_norm_name(str(constraint.get("column"))))
        if col:
            by_col.setdefault(col, []).append(constraint)

    # 逐列应用数值和文本边界值
    for col, items in by_col.items():
        values: list[Any] = []
        for item in items:
            if item.get("op") == "IN":
                values.extend(item.get("values") or [])
            else:
                values.append(item.get("value"))
        values = [v for v in values if v is not None]
        
        # 如果列约束是 IS NULL / IS NOT NULL，设置第一行为 None，其余非空
        if not values:
            if rows:
                rows[0][col] = None
            continue
            
        # 阳性覆盖：将谓词值分布在前一半数据行中
        for idx, value in enumerate(values[: max(1, len(rows) // 2)]):
            rows[idx % len(rows)][col] = value
            
        # 阴性覆写 / 对抗性三态：在最后一行注入对抗性的异常反例，强制打破假等价
        if len(rows) > 1:
            rows[-1][col] = _counter_value(col, values[0])


def _add_duplicate_probe(rows: list[dict[str, Any]], columns: list[str]) -> None:
    """
    去重探测机制：在 Row 0 和 Row 1 的非主键字段上，复制生成完全重复的数据行。
    Distinct probe mechanism: clones values from Row 0 to Row 1 for non-key columns
    to trigger duplication mismatches if DISTINCT is missing in student SQL.
    """
    if len(rows) < 3 or not columns:
        return
    # 避开 ID、SSN 等核心表主键，只针对普通列（如 department, course_id, credit 等）复制
    probe_cols = [col for col in columns if not _is_key_column(col)]
    if not probe_cols:
        probe_cols = columns[:1]
    # 在 Row 1 的对应位置上复制 Row 0 的值
    for col in probe_cols[:2]:
        rows[1][col] = rows[0][col]


def _build_shared_values(schema: dict[str, list[str]], row_count: int) -> dict[str, list[Any]]:
    """
    拓扑对齐机制：识别 schema 中的连接键字段，并为具有关联性的列建立共享值池，防止 JOIN 时出现空关联。
    Topology alignment: builds shared values groups for join keys across tables to avoid empty JOIN outputs.
    """
    groups: dict[str, list[Any]] = {}
    for columns in schema.values():
        for col in columns:
            # _join_group_key 会提取列的根部语义（例如 e_id, s_id 均归类为 id）
            key = _join_group_key(col)
            if key not in groups:
                groups[key] = [_seed_value(col, idx) for idx in range(row_count)]
    return groups


def _base_value(col: str, idx: int, shared_values: dict[str, list[Any]]) -> Any:
    """
    主外键关联填充：如果当前列属于某个共享关联组，则从种子池中取值以保障表间能够成功连接。
    Fetches base value aligned with foreign key value pools if the column is part of a join group.
    """
    key = _join_group_key(col)
    if key in shared_values:
        return shared_values[key][idx % len(shared_values[key])]
    return _seed_value(col, idx)


def _seed_value(col: str, idx: int) -> Any:
    """
    根据列名分发基础测试数据，并强制包含单调性以检测 ORDER BY 错误。
    Generates a mock seed value for a column based on token name heuristics,
    ensuring monotonicity to expose ORDER BY/sorting logic bugs.
    """
    name = col.lower()
    
    # 姓名列循环生成
    if name == "name":
        return ["Alice", "Bob", "Carol", "Dave"][idx % 4]
        
    # 地理数据类型填充
    if name == "location":
        return f"POINT({idx} {idx})"
        
    # 日期字段：自增递增（单调性，支持 ORDER BY 校验）
    if any(token in name for token in DATE_HINTS):
        return f"2024-01-{(idx % 9) + 1:02d}"
        
    # 数字类型：idx + 1 单调递增自增，用于检测 >、>=、LIMIT 和聚合运算
    if _is_numeric_column(col):
        return idx + 1
        
    # 教学系统常用分类字段循环填充
    if "semester" in name:
        return ["Fall", "Spring", "Summer", "Winter"][idx % 4]
    if "grade" in name:
        return ["A", "B", "C", None][idx % 4]
    if "country" in name:
        return ["USA", "UK", "Germany", "Canada"][idx % 4]
    if "title" in name:
        return ["Sales Manager", "Marketing Lead", "Engineer", "Analyst"][idx % 4]
    if "dept" in name:
        return ["Comp. Sci.", "Math", "Physics", "History"][idx % 4]
    if "name" in name:
        return ["Alice", "Bob", "Carol", "Dave"][idx % 4]
        
    # 兜底生成唯一字符串，避免碰撞
    return f"{_clean_identifier(col)}_{idx + 1}"


def _counter_value(col: str, value: Any) -> Any:
    if value is None:
        return _seed_value(col, 3)
    if isinstance(value, (int, float, Decimal)):
        return value + 999
    text = str(value)
    if "%" in text:
        return text.replace("%", "X")
    if text:
        return f"not_{text}"
    return "counter_value"


def _execute_sqlite(
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    sql: str,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """
    在内存 SQLite 隔离沙盒中建表、插入模拟数据并执行 SQL 查询，带有无限递归熔断机制。
    Executes SQL inside an in-memory SQLite sandbox with mock UDFs and infinite recursion guards.
    """
    conn = sqlite3.connect(":memory:")
    try:
        # 1. 注册自定义标量函数与空间地理占位函数，避免执行报错
        conn.create_function("AVG_SALARY", 1, lambda _company: 50000)
        conn.create_function("avg_salary", 1, lambda _company: 50000)
        conn.create_function("ST_WITHIN", 2, lambda _point, _poly: 1)
        conn.create_function("ST_DWITHIN", 3, lambda _a, _b, _distance: 1)
        conn.create_function("ST_DISTANCE", 2, lambda a, b: 0 if a == b else 1)
        conn.create_function("WIDTH_BUCKET", 4, _width_bucket)
        conn.create_function("ROLLUP", 1, lambda value: value)
        
        # 2. 注册进度挂接器 (Progress Handler)，指令达到 10w 条时触发熔断中断，防御 Recursive CTE 死循环
        # Sandbox Guard: interrupts connection if query takes more than 100,000 instructions
        conn.set_progress_handler(lambda: 1, 100000)
        
        cur = conn.cursor()
        # 3. 动态创建测试表并批量插入当前模拟的元组数据
        for table, columns in schema.items():
            if table not in rows:
                continue
            defs = ", ".join(f'"{col}" {_sqlite_type(col)}' for col in columns)
            cur.execute(f'CREATE TABLE "{table}" ({defs})')
            placeholders = ", ".join("?" for _ in columns)
            quoted_cols = ", ".join(f'"{col}"' for col in columns)
            insert_sql = f'INSERT INTO "{table}" ({quoted_cols}) VALUES ({placeholders})'
            values = [tuple(row.get(col) for col in columns) for row in rows[table]]
            if values:
                cur.executemany(insert_sql, values)
                
        # 4. 执行 SQL 并读取数据列和数据行
        cur.execute(sql)
        result_rows = cur.fetchall()
        result_cols = [item[0] for item in (cur.description or [])]
        return result_cols, [tuple(_normalize_cell(cell) for cell in row) for row in result_rows]
    finally:
        conn.close()


def _build_data_evidence(
    *,
    is_equivalent: bool,
    ordered: bool,
    standard_columns: list[str],
    student_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    student_rows: list[tuple[Any, ...]],
    standard_ast: exp.Expression | None,
    student_ast: exp.Expression | None,
    student_exec_error: str | None,
) -> dict[str, Any]:
    standard_counter = Counter(standard_rows)
    student_counter = Counter(student_rows)
    only_standard = list((standard_counter - student_counter).elements())[:5]
    only_student = list((student_counter - standard_counter).elements())[:5]
    duplicate_student = sum(count - 1 for count in student_counter.values() if count > 1)
    duplicate_standard = sum(count - 1 for count in standard_counter.values() if count > 1)
    suspected_cartesian = (
        _join_count(student_ast) > 0
        and not _has_join_on(student_ast)
        and len(student_rows) > max(len(standard_rows) * 2, len(standard_rows) + 3)
    )
    return {
        "sandbox_executed": True,
        "student_exec_ok": student_exec_error is None,
        "student_exec_error": student_exec_error,
        "is_equivalent_on_generated_data": is_equivalent,
        "ordered_compare": ordered,
        "row_count_match": len(standard_rows) == len(student_rows),
        "standard_row_count": len(standard_rows),
        "student_row_count": len(student_rows),
        "columns_match": standard_columns == student_columns,
        "standard_columns": standard_columns,
        "student_columns": student_columns,
        "standard_duplicate_row_count": duplicate_standard,
        "student_duplicate_row_count": duplicate_student,
        "suspected_cartesian_product": suspected_cartesian,
        "only_in_standard_sample": only_standard,
        "only_in_student_sample": only_student,
        "standard_sample_rows": standard_rows[:5],
        "student_sample_rows": student_rows[:5],
    }


def _run_mutation_tests(
    *,
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    standard_sql: str,
    student_sql: str,
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    ordered: bool,
) -> dict[str, Any]:
    """
    变分隔离测试核心入口：基于 AST 对各算子进行单变量替换与移除测试，收集 Mutant 执行证据。
    Runs mutation tests by creating mutated student SQL variants (replacing/removing clauses)
    and evaluating them in the sandbox to isolate and locate specific faulty operators.
    """
    standard_ast = _parse_sql(standard_sql)
    student_ast = _parse_sql(student_sql)
    if standard_ast is None or student_ast is None:
        return {
            "enabled": False,
            "summary": {"executed": 0, "fixed_by_replacement": 0},
            "tests": [],
            "error": "parse_failed",
        }

    # 定义要参与变分比对的核心算子列表
    specs = [
        {"clause": "WHERE", "knowledge_point_id": "where", "arg": "where", "node_type": exp.Where},
        {"clause": "GROUP BY", "knowledge_point_id": "group-by", "arg": "group", "node_type": exp.Group},
        {"clause": "HAVING", "knowledge_point_id": "having", "arg": "having", "node_type": exp.Having},
        {"clause": "ORDER BY", "knowledge_point_id": "order-by", "arg": "order", "node_type": exp.Order},
        {"clause": "LIMIT", "knowledge_point_id": "limit", "arg": "limit", "node_type": exp.Limit},
    ]
    tests: list[dict[str, Any]] = []
    
    # 遍历算子进行替换与移除测试
    for spec in specs:
        std_node = standard_ast.args.get(spec["arg"]) or standard_ast.find(spec["node_type"])
        stu_node = student_ast.args.get(spec["arg"]) or student_ast.find(spec["node_type"])
        
        # 两者均没有该子句，跳过
        if std_node is None and stu_node is None:
            continue
        # 两者子句结构完全等价，跳过
        if std_node is not None and stu_node is not None and _sql_of(std_node) == _sql_of(stu_node):
            continue
            
        # 1. 替换变体 (Replacement Mutant)：用标准答案子句替换学生出错的子句
        replacement_sql = _mutate_select_arg(student_ast, spec["arg"], std_node)
        # 2. 移除变体 (Removal Mutant)：剔除学生多写或写错的冗余子句
        removal_sql = _mutate_select_arg(student_ast, spec["arg"], None) if stu_node is not None else None
        
        tests.append(_execute_mutation_case(
            schema=schema,
            rows=rows,
            clause=spec["clause"],
            knowledge_point_id=spec["knowledge_point_id"],
            replacement_sql=replacement_sql,
            removal_sql=removal_sql,
            standard_columns=standard_columns,
            standard_rows=standard_rows,
            ordered=ordered,
        ))

    # 3. 针对 JOIN ON 进行专项的连接条件变分测试
    join_test = _run_join_on_mutation(
        schema=schema,
        rows=rows,
        standard_ast=standard_ast,
        student_ast=student_ast,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
    )
    if join_test:
        tests.append(join_test)

    return {
        "enabled": True,
        "summary": {
            "executed": sum(1 for test in tests if test.get("replacement_exec_ok") or test.get("removal_exec_ok")),
            "fixed_by_replacement": sum(1 for test in tests if test.get("fixed_by_replacement")),
            "remove_kept_correct": sum(1 for test in tests if test.get("removed_student_clause_equivalent")),
        },
        "tests": tests,
    }


def _execute_mutation_case(
    *,
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    clause: str,
    knowledge_point_id: str,
    replacement_sql: str | None,
    removal_sql: str | None,
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    ordered: bool,
) -> dict[str, Any]:
    test: dict[str, Any] = {
        "clause": clause,
        "knowledge_point_id": knowledge_point_id,
        "action": "replace_student_clause_with_standard_clause",
        "replacement_sqlite": None,
        "replacement_exec_ok": False,
        "replacement_equivalent": None,
        "fixed_by_replacement": False,
        "removal_sqlite": None,
        "removal_exec_ok": False,
        "removed_student_clause_equivalent": None,
        "error": None,
    }
    if replacement_sql:
        sqlite_sql = transpile_to_sqlite(replacement_sql)
        test["replacement_sqlite"] = sqlite_sql
        if sqlite_sql:
            try:
                cols, result_rows = _execute_sqlite(schema, rows, sqlite_sql)
                equivalent = _rows_equivalent(standard_columns, standard_rows, cols, result_rows, ordered)
                test["replacement_exec_ok"] = True
                test["replacement_equivalent"] = equivalent
                test["fixed_by_replacement"] = equivalent
            except Exception as exc:
                test["error"] = f"replacement_failed: {exc}"
    if removal_sql:
        sqlite_sql = transpile_to_sqlite(removal_sql)
        test["removal_sqlite"] = sqlite_sql
        if sqlite_sql:
            try:
                cols, result_rows = _execute_sqlite(schema, rows, sqlite_sql)
                equivalent = _rows_equivalent(standard_columns, standard_rows, cols, result_rows, ordered)
                test["removal_exec_ok"] = True
                test["removed_student_clause_equivalent"] = equivalent
            except Exception as exc:
                prev = test.get("error")
                test["error"] = f"{prev}; removal_failed: {exc}" if prev else f"removal_failed: {exc}"
    return test


def _run_join_on_mutation(
    *,
    schema: dict[str, list[str]],
    rows: dict[str, list[dict[str, Any]]],
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    ordered: bool,
) -> dict[str, Any] | None:
    standard_joins = list(standard_ast.find_all(exp.Join))
    student_joins = list(student_ast.find_all(exp.Join))
    if not standard_joins or not student_joins:
        return None
    std_on = [join.args.get("on") for join in standard_joins]
    stu_on = [join.args.get("on") for join in student_joins]
    if [_sql_of(node) for node in std_on] == [_sql_of(node) for node in stu_on]:
        return None

    mutated = student_ast.copy()
    mutated_joins = list(mutated.find_all(exp.Join))
    for idx, join in enumerate(mutated_joins):
        replacement = std_on[idx] if idx < len(std_on) else None
        if replacement is not None:
            join.set("on", replacement.copy())
    replacement_sql = _sql_of(mutated)
    return _execute_mutation_case(
        schema=schema,
        rows=rows,
        clause="JOIN ON",
        knowledge_point_id="join-on",
        replacement_sql=replacement_sql,
        removal_sql=None,
        standard_columns=standard_columns,
        standard_rows=standard_rows,
        ordered=ordered,
    )


def _mutate_select_arg(ast: exp.Expression, arg: str, replacement: exp.Expression | None) -> str | None:
    mutated = ast.copy()
    mutated.set(arg, replacement.copy() if replacement is not None else None)
    return _sql_of(mutated)


def _rows_equivalent(
    standard_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    candidate_columns: list[str],
    candidate_rows: list[tuple[Any, ...]],
    ordered: bool,
) -> bool:
    if standard_columns != candidate_columns:
        return False
    if ordered:
        return standard_rows == candidate_rows
    return Counter(standard_rows) == Counter(candidate_rows)


def _sql_of(node: exp.Expression | None) -> str:
    if node is None:
        return ""
    try:
        return node.sql(dialect="sqlite", normalize=True)
    except Exception:
        return str(node)


def _sqlite_type(col: str) -> str:
    if any(token in col.lower() for token in DATE_HINTS):
        return "TEXT"
    return "REAL" if _is_numeric_column(col) else "TEXT"


def _is_numeric_column(col: str) -> bool:
    name = col.lower()
    return any(token in name for token in NUMERIC_HINTS)


def _is_key_column(col: str) -> bool:
    name = col.lower()
    return name == "id" or name.endswith("_id") or name.endswith("id") or name in {"ssn", "dno", "dnum", "pno"}


def _join_group_key(col: str) -> str:
    name = _norm_name(col)
    aliases = {
        "id": "id",
        "sid": "id",
        "s_id": "id",
        "iid": "id",
        "i_id": "id",
        "ssn": "ssn",
        "superssn": "ssn",
        "super_ssn": "ssn",
        "mgrssn": "ssn",
        "mgr_ssn": "ssn",
        "essn": "ssn",
        "dno": "department_number",
        "dnumber": "department_number",
        "dnum": "department_number",
        "pno": "project_number",
        "pnumber": "project_number",
    }
    return aliases.get(name, name)


def _join_count(ast: exp.Expression | None) -> int:
    return len(list(ast.find_all(exp.Join))) if ast else 0


def _has_join_on(ast: exp.Expression | None) -> bool:
    return any(bool(join.args.get("on")) for join in ast.find_all(exp.Join)) if ast else False


def _sqlite_compat(sql: str) -> str:
    sql = sql.rstrip().rstrip(";")
    sql = re.sub(r"\bISNULL\s*\(", "IFNULL(", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\[([^\]]+)\]", r'"\1"', sql)
    sql = re.sub(r"(?is)\bSys\.Views\b", '"Sys.Views"', sql)
    sql = re.sub(r"(?is)CURRENT_DATE\s*-\s*INTERVAL\s*'1'\s+DAY", "date('now', '-1 day')", sql)
    sql = re.sub(r"(?is)\bROLLUP\s*\(([^)]+)\)", r"\1", sql)
    sql = _rewrite_parenthesized_union(sql)
    sql = _rewrite_quantified_subqueries(sql)
    sql = _replace_named_parameters(sql)
    return sql + ";"


def _manual_sqlite_compat(sql: str) -> str | None:
    sql = sql.strip().rstrip(";")
    top = re.match(r"(?is)^select\s+top\s+(\d+)\s+(.+)$", sql)
    if top:
        limit = top.group(1)
        body = top.group(2)
        order_match = re.search(r"(?is)\s+order\s+by\s+", body)
        if order_match:
            sql = "SELECT " + body + f" LIMIT {limit}"
        else:
            sql = "SELECT " + body + f" LIMIT {limit}"
    return _sqlite_compat(sql)


def _replace_named_parameters(sql: str) -> str:
    sql = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", lambda match: _parameter_literal(match.group(1)), sql)
    sql = re.sub(r"(?i)(=\s*)student_name\b", r"\1'Alice'", sql)
    return sql


def _parameter_literal(name: str) -> str:
    normalized = name.lower()
    if "substring" in normalized:
        return "'A'"
    if "instructor" in normalized and "id" in normalized:
        return "1"
    if "student" in normalized and "name" in normalized:
        return "'Alice'"
    if normalized.endswith("id") or normalized.endswith("_id"):
        return "1"
    return "'Alice'"


def _rewrite_parenthesized_union(sql: str) -> str:
    return re.sub(
        r"(?is)^\s*\((SELECT.+?)\)\s+UNION\s+\((SELECT.+?)\)\s*$",
        r"\1 UNION \2",
        sql,
    )


def _rewrite_quantified_subqueries(sql: str) -> str:
    pattern = re.compile(
        r"(?is)([A-Za-z_][A-Za-z0-9_\\.]*|\"[^\"]+\")\s*(<=|>=|<>|!=|=|<|>)\s*"
        r"(ALL|ANY|SOME)\s*\(\s*SELECT\s+([A-Za-z_][A-Za-z0-9_\\.]*|\"[^\"]+\")\s+FROM\s+(.+?)\)",
    )

    def repl(match: re.Match) -> str:
        left, op, quantifier, selected, tail = match.groups()
        quantifier = quantifier.upper()
        aggregate = _quantifier_aggregate(op, quantifier)
        if aggregate is None:
            if op == "=" and quantifier in {"ANY", "SOME"}:
                return f"{left} IN (SELECT {selected} FROM {tail})"
            return match.group(0)
        return f"{left} {op} (SELECT {aggregate}({selected}) FROM {tail})"

    previous = None
    current = sql
    while previous != current:
        previous = current
        current = pattern.sub(repl, current)
    return current


def _quantifier_aggregate(op: str, quantifier: str) -> str | None:
    if quantifier == "ALL":
        if op in {">", ">="}:
            return "MAX"
        if op in {"<", "<="}:
            return "MIN"
    if quantifier in {"ANY", "SOME"}:
        if op in {">", ">="}:
            return "MIN"
        if op in {"<", "<="}:
            return "MAX"
    return None


def _mentions_sys_views(*sqls: str) -> bool:
    return any(re.search(r"(?is)\bSys\.Views\b", sql or "") for sql in sqls)


def _width_bucket(value: Any, minimum: Any, maximum: Any, buckets: Any) -> int:
    try:
        value_f = float(value)
        min_f = float(minimum)
        max_f = float(maximum)
        bucket_count = max(1, int(float(buckets)))
    except Exception:
        return 1
    if max_f <= min_f:
        return 1
    if value_f < min_f:
        return 0
    if value_f >= max_f:
        return bucket_count + 1
    width = (max_f - min_f) / bucket_count
    return int((value_f - min_f) / width) + 1


def _clean_identifier(value: str) -> str:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if value.startswith("`") and value.endswith("`"):
        value = value[1:-1]
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value.strip()


def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", _clean_identifier(value).lower())


def _normalize_cell(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


__all__ = [
    "SandboxRun",
    "generate_and_compare",
    "generate_test_database",
    "parse_schema_text",
    "transpile_to_sqlite",
]
