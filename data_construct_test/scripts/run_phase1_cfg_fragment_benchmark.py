"""Production-level adversarial benchmark for the Phase 1 SQL CFG fragment."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from run_phase1_capability_samples import _case, _json_safe, run_case


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "data_construct_test" / "outputs"


def fragment_case(
    case_id: str,
    production: str,
    alternative: str,
    expectation: str,
    schema: str,
    standard: str,
    student: str,
    expected_kps: list[str] | None = None,
    *,
    cfg_labels: list[str] | None = None,
    attack_kind: str = "semantic_mutation",
    max_rows_per_table: int = 10,
    note: str = "",
    sql_dialect: str | None = None,
) -> dict[str, Any]:
    case = _case(
        case_id,
        production,
        expectation,
        schema,
        standard,
        student,
        expected_kps,
        cfg_labels=cfg_labels,
        attack_kind=attack_kind,
        max_rows_per_table=max_rows_per_table,
        note=note,
        sql_dialect=sql_dialect,
    )
    case["production"] = production
    case["alternative"] = alternative
    return case


def build_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    add = cases.append

    # Query / parser boundary.
    malformed = [
        ("unclosed_parenthesis", "SELECT name FROM (student"),
        ("misspelled_from", "SELECT name FORM student"),
        ("dangling_where", "SELECT name FROM student WHERE"),
        ("double_select", "SELECT SELECT name FROM student"),
        ("multiple_statements", "SELECT name FROM student; SELECT id FROM student"),
    ]
    for suffix, student_sql in malformed:
        add(fragment_case(
            f"query_syntax_{suffix}", "Query", f"syntax:{suffix}", "syntax_rejected",
            "student(id, name);", "SELECT name FROM student", student_sql,
            ["select-basic"], cfg_labels=["select-basic"], attack_kind="parser",
        ))

    # Comparison ::= Expr CompOp Expr.
    comparison_pairs = [
        ("eq_to_neq", "=", "<>"),
        ("neq_to_eq", "<>", "="),
        ("lt_to_lte", "<", "<="),
        ("lte_to_lt", "<=", "<"),
        ("gt_to_gte", ">", ">="),
        ("gte_to_gt", ">=", ">"),
    ]
    for suffix, standard_op, student_op in comparison_pairs:
        add(fragment_case(
            f"comparison_{suffix}", "Comparison", f"CompOp:{standard_op}", "not_equivalent",
            "course(id, title, credits);",
            f"SELECT title FROM course WHERE credits {standard_op} 3",
            f"SELECT title FROM course WHERE credits {student_op} 3",
            ["where"], cfg_labels=["where-comp"], attack_kind="boundary_probe",
        ))

    # Predicate boolean tree.
    logical_cases = [
        ("and_predicate_missing", "credits > 2 AND credits < 7", "credits > 2"),
        ("or_changed_to_and", "credits < 3 OR credits > 6", "credits < 3 AND credits > 6"),
        ("not_removed", "NOT (credits = 3)", "credits = 3"),
        ("precedence_parentheses", "(credits = 1 OR credits = 3) AND id > 2", "credits = 1 OR credits = 3 AND id > 2"),
    ]
    for suffix, standard_pred, student_pred in logical_cases:
        add(fragment_case(
            f"predicate_{suffix}", "Predicate", suffix, "not_equivalent",
            "course(id, title, credits);",
            f"SELECT title FROM course WHERE {standard_pred}",
            f"SELECT title FROM course WHERE {student_pred}",
            ["where"], cfg_labels=["where"], attack_kind="boolean_structure",
        ))

    # NULL and conditional expressions.
    null_cases = [
        ("is_null_vs_not_null", "not_equivalent", "manager_id IS NULL", "manager_id IS NOT NULL", ["where"]),
        ("is_null_vs_equals_null", "not_equivalent", "manager_id IS NULL", "manager_id = NULL", ["comp-null", "where"]),
    ]
    for suffix, expectation, standard_pred, student_pred, kps in null_cases:
        add(fragment_case(
            f"null_{suffix}", "NullExpr", suffix, expectation,
            "employee(id, name, manager_id);",
            f"SELECT name FROM employee WHERE {standard_pred}",
            f"SELECT name FROM employee WHERE {student_pred}",
            kps, cfg_labels=["null-handling"], attack_kind="null_three_valued_logic",
        ))
    add(fragment_case(
        "null_coalesce_case_equivalent", "NullExpr", "COALESCE", "equivalent",
        "employee(id, dept_name);", "SELECT COALESCE(dept_name, 'Unknown') FROM employee",
        "SELECT CASE WHEN dept_name IS NULL THEN 'Unknown' ELSE dept_name END FROM employee",
        cfg_labels=["null-handling", "case"], attack_kind="equivalent_rewrite",
    ))
    add(fragment_case(
        "null_nullif_case_equivalent", "NullExpr", "NULLIF", "equivalent",
        "sales(id, amount);", "SELECT NULLIF(amount, 0) FROM sales",
        "SELECT CASE WHEN amount = 0 THEN NULL ELSE amount END FROM sales",
        cfg_labels=["null-handling", "case"], attack_kind="equivalent_rewrite",
    ))

    # BETWEEN / IN / LIKE.
    predicate_cases = [
        ("between_boundary", "BetweenExpr", "BETWEEN", "credits BETWEEN 3 AND 5", "credits > 3 AND credits <= 5"),
        ("not_between", "BetweenExpr", "NOT BETWEEN", "credits NOT BETWEEN 3 AND 5", "credits BETWEEN 3 AND 5"),
        ("in_list_member_removed", "InExpr", "IN ValueList", "credits IN (1, 3, 5)", "credits IN (1, 5)"),
        ("not_in_list", "InExpr", "NOT IN ValueList", "credits NOT IN (1, 3)", "credits IN (1, 3)"),
        ("like_prefix_suffix", "LikeExpr", "LIKE", "title LIKE 'Data%'", "title LIKE '%Data'"),
        ("not_like", "LikeExpr", "NOT LIKE", "title NOT LIKE 'Data%'", "title LIKE 'Data%'"),
    ]
    for suffix, production, alternative, standard_pred, student_pred in predicate_cases:
        add(fragment_case(
            f"predicate_{suffix}", production, alternative, "not_equivalent",
            "course(id, title, credits);",
            f"SELECT title FROM course WHERE {standard_pred}",
            f"SELECT title FROM course WHERE {student_pred}",
            ["where"], cfg_labels=["between" if "between" in suffix else "in-list" if "in" in suffix else "like"],
            attack_kind="predicate_counterexample",
        ))
    add(fragment_case(
        "in_list_or_equivalent", "InExpr", "IN equivalent OR", "equivalent",
        "course(id, title, credits);", "SELECT title FROM course WHERE credits IN (1, 3)",
        "SELECT title FROM course WHERE credits = 1 OR credits = 3",
        cfg_labels=["in-list"], attack_kind="equivalent_rewrite",
    ))

    # Projection, alias, arithmetic and CASE alternatives.
    projection_cases = [
        ("column_dropped", "SELECT title, credits FROM course", "SELECT title FROM course"),
        ("column_order", "SELECT title, credits FROM course", "SELECT credits, title FROM course"),
        ("arithmetic_add", "SELECT credits + 1 FROM course", "SELECT credits FROM course"),
        ("arithmetic_subtract", "SELECT credits - 1 FROM course", "SELECT credits FROM course"),
        ("arithmetic_multiply", "SELECT credits * 2 FROM course", "SELECT credits FROM course"),
        ("arithmetic_divide", "SELECT credits / 2 FROM course", "SELECT credits FROM course"),
        ("arithmetic_modulo", "SELECT credits % 2 FROM course", "SELECT credits % 3 FROM course"),
    ]
    for suffix, standard_sql, student_sql in projection_cases:
        add(fragment_case(
            f"projection_{suffix}", "ProjElem", suffix, "not_equivalent",
            "course(id, title, credits);", standard_sql, student_sql,
            ["select-basic"], cfg_labels=["arithmetic" if "arithmetic" in suffix else "select-basic"],
        ))
    add(fragment_case(
        "projection_star_named_equivalent", "ProjElem", "*", "equivalent",
        "course(id, title, credits);", "SELECT * FROM course", "SELECT id, title, credits FROM course",
        cfg_labels=["select-basic"], attack_kind="equivalent_rewrite",
    ))
    add(fragment_case(
        "projection_alias_equivalent", "Alias", "AS Ident", "equivalent",
        "course(id, title);", "SELECT title AS course_title FROM course", "SELECT title FROM course",
        cfg_labels=["alias"], attack_kind="equivalent_rewrite",
    ))
    add(fragment_case(
        "case_simple_searched_equivalent", "CaseExpr", "simple/searched CASE", "equivalent",
        "takes(id, grade);", "SELECT CASE grade WHEN 'A' THEN 'pass' ELSE 'other' END FROM takes",
        "SELECT CASE WHEN grade = 'A' THEN 'pass' ELSE 'other' END FROM takes",
        cfg_labels=["case"], attack_kind="equivalent_rewrite",
    ))
    add(fragment_case(
        "case_when_branch_removed", "CaseExpr", "WhenClause+", "not_equivalent",
        "takes(id, grade);",
        "SELECT CASE WHEN grade = 'A' THEN 'A' WHEN grade = 'B' THEN 'B' ELSE 'other' END FROM takes",
        "SELECT CASE WHEN grade = 'A' THEN 'A' ELSE 'other' END FROM takes",
        ["case"], cfg_labels=["case"], attack_kind="branch_coverage",
    ))

    # FROM sources and JOIN alternatives.
    add(fragment_case(
        "from_alias_rename_equivalent", "TSource", "TableName Alias", "equivalent",
        "student(id, name);", "SELECT s.name FROM student s", "SELECT x.name FROM student x",
        cfg_labels=["alias", "select-basic"], attack_kind="equivalent_rewrite",
    ))
    add(fragment_case(
        "from_derived_inline_equivalent", "TSource", "SubQuery Alias", "equivalent",
        "student(id, name, credits);",
        "SELECT x.name FROM (SELECT name FROM student WHERE credits > 3) x",
        "SELECT name FROM student WHERE credits > 3",
        cfg_labels=["subquery-scalar", "select-basic"], attack_kind="equivalent_rewrite",
    ))
    add(fragment_case(
        "join_comma_explicit_equivalent", "JoinStmt", "implicit/INNER", "equivalent",
        "student(id, name); takes(id, course_id);",
        "SELECT s.name FROM student s JOIN takes t ON s.id = t.id",
        "SELECT s.name FROM student s, takes t WHERE s.id = t.id",
        cfg_labels=["join-inner", "join-on"], attack_kind="equivalent_rewrite",
    ))
    join_cases = [
        ("inner_key_changed", "INNER JOIN", "SELECT s.name FROM student s JOIN takes t ON s.id = t.id", "SELECT s.name FROM student s JOIN takes t ON s.id = t.course_id", ["join-on"]),
        ("left_to_inner", "LEFT JOIN", "SELECT s.name FROM student s LEFT JOIN takes t ON s.id = t.id", "SELECT s.name FROM student s JOIN takes t ON s.id = t.id", ["join-left"]),
        ("right_to_inner", "RIGHT JOIN", "SELECT s.name FROM takes t RIGHT JOIN student s ON s.id = t.id", "SELECT s.name FROM takes t JOIN student s ON s.id = t.id", ["join-right"]),
        ("full_to_left", "FULL JOIN", "SELECT s.name FROM student s FULL JOIN takes t ON s.id = t.id", "SELECT s.name FROM student s LEFT JOIN takes t ON s.id = t.id", ["join-full"]),
        ("cross_to_inner", "CROSS JOIN", "SELECT s.name FROM student s CROSS JOIN takes t", "SELECT s.name FROM student s JOIN takes t ON s.id = t.id", ["join-on"]),
        ("non_equi_boundary", "ON Predicate", "SELECT s.name FROM student s JOIN takes t ON s.id < t.id", "SELECT s.name FROM student s JOIN takes t ON s.id <= t.id", ["join-on"]),
    ]
    for suffix, alternative, standard_sql, student_sql, kps in join_cases:
        add(fragment_case(
            f"join_{suffix}", "JoinStmt", alternative, "not_equivalent",
            "student(id, name); takes(id, course_id);", standard_sql, student_sql,
            kps, cfg_labels=["join-right-full" if alternative in {"RIGHT JOIN", "FULL JOIN"} else "join-left" if alternative == "LEFT JOIN" else "join-inner", "join-on"],
            attack_kind="join_topology",
        ))
    add(fragment_case(
        "join_using_on_equivalent", "JoinCond", "USING/ON", "equivalent",
        "student(id, name); takes(id, course_id);",
        "SELECT s.name FROM student s JOIN takes t USING (id)",
        "SELECT s.name FROM student s JOIN takes t ON s.id = t.id",
        cfg_labels=["join-on"], attack_kind="equivalent_rewrite",
    ))
    add(fragment_case(
        "join_three_table_middle_key", "JoinStmt", "JoinStmt*", "not_equivalent",
        "student(id, name, dept_id); department(dept_id, title); takes(id, course_id);",
        "SELECT s.name, d.title FROM student s JOIN department d ON s.dept_id = d.dept_id JOIN takes t ON s.id = t.id",
        "SELECT s.name, d.title FROM student s JOIN department d ON s.id = d.dept_id JOIN takes t ON s.id = t.id",
        ["join-on"], cfg_labels=["complex-join"], attack_kind="join_topology",
    ))

    # Subquery positions and quantifiers.
    subquery_cases = [
        ("in_negated", "InExpr SelectQuery", "SELECT name FROM student WHERE id IN (SELECT id FROM takes)", "SELECT name FROM student WHERE id NOT IN (SELECT id FROM takes)", ["where", "subquery-scalar"]),
        ("exists_negated", "ExistsExpr", "SELECT name FROM student s WHERE EXISTS (SELECT 1 FROM takes t WHERE t.id = s.id)", "SELECT name FROM student s WHERE NOT EXISTS (SELECT 1 FROM takes t WHERE t.id = s.id)", ["where", "subquery-correlated"]),
        ("scalar_filtered", "SubScalarExpr", "SELECT name FROM student WHERE credits > (SELECT AVG(credits) FROM student)", "SELECT name FROM student WHERE credits > (SELECT AVG(credits) FROM student WHERE dept = 'CS')", ["subquery-scalar", "where"]),
        ("correlation_removed", "correlated SubQuery", "SELECT name FROM student s WHERE credits > (SELECT AVG(credits) FROM student x WHERE x.dept = s.dept)", "SELECT name FROM student s WHERE credits > (SELECT AVG(credits) FROM student x)", ["subquery-correlated", "where"]),
    ]
    for suffix, alternative, standard_sql, student_sql, kps in subquery_cases:
        schema = "student(id, name, dept, credits); takes(id, course_id);"
        add(fragment_case(
            f"subquery_{suffix}", "SubQuery", alternative, "not_equivalent",
            schema, standard_sql, student_sql, kps,
            cfg_labels=["subquery-exists" if "exists" in suffix else "subquery-in" if suffix == "in_negated" else "subquery-scalar"],
            attack_kind="subquery_distribution",
        ))
    add(fragment_case(
        "subquery_any_min_equivalent", "SubQuery", "ANY", "equivalent",
        "student(id, name, credits);", "SELECT name FROM student WHERE credits > (SELECT MIN(credits) FROM student)",
        "SELECT name FROM student WHERE credits > ANY (SELECT credits FROM student)",
        cfg_labels=["subquery-scalar"], attack_kind="equivalent_rewrite",
    ))
    add(fragment_case(
        "subquery_all_max_equivalent", "SubQuery", "ALL", "equivalent",
        "student(id, name, credits);", "SELECT name FROM student WHERE credits >= (SELECT MAX(credits) FROM student)",
        "SELECT name FROM student WHERE credits >= ALL (SELECT credits FROM student)",
        cfg_labels=["subquery-scalar"], attack_kind="equivalent_rewrite",
    ))

    # Aggregate and grouping alternatives.
    aggregate_cases = [
        ("count_star_column", "COUNT", "SELECT COUNT(*) FROM instructor", "SELECT COUNT(salary) FROM instructor"),
        ("count_distinct", "COUNT DISTINCT", "SELECT COUNT(DISTINCT dept) FROM instructor", "SELECT COUNT(dept) FROM instructor"),
        ("sum_avg", "SUM/AVG", "SELECT SUM(salary) FROM instructor", "SELECT AVG(salary) FROM instructor"),
        ("min_max", "MIN/MAX", "SELECT MIN(salary) FROM instructor", "SELECT MAX(salary) FROM instructor"),
    ]
    for suffix, alternative, standard_sql, student_sql in aggregate_cases:
        add(fragment_case(
            f"aggregate_{suffix}", "FuncCall", alternative, "not_equivalent",
            "instructor(id, dept, salary);", standard_sql, student_sql,
            ["agg-count", "select-basic"], cfg_labels=["agg-count"], attack_kind="aggregate_probe",
        ))
    add(fragment_case(
        "aggregate_avg_sum_count_equivalent", "FuncCall", "AVG equivalent", "equivalent",
        "instructor(id, salary);", "SELECT AVG(salary) FROM instructor",
        "SELECT SUM(salary) * 1.0 / COUNT(salary) FROM instructor",
        cfg_labels=["agg-count", "arithmetic"], attack_kind="equivalent_rewrite",
    ))
    add(fragment_case(
        "group_column_changed", "GroupByClause", "GROUP BY ExprList", "not_equivalent",
        "instructor(id, dept, building, salary);", "SELECT SUM(salary) FROM instructor GROUP BY dept",
        "SELECT SUM(salary) FROM instructor GROUP BY building", ["group-by"], cfg_labels=["group-by"],
    ))
    add(fragment_case(
        "group_order_equivalent", "GroupByClause", "ExprList order", "equivalent",
        "instructor(id, dept, building);", "SELECT dept, building, COUNT(*) FROM instructor GROUP BY dept, building",
        "SELECT dept, building, COUNT(*) FROM instructor GROUP BY building, dept",
        cfg_labels=["group-by"], attack_kind="equivalent_rewrite",
    ))
    for agg in ("COUNT(*)", "SUM(salary)", "AVG(salary)", "MIN(salary)", "MAX(salary)"):
        suffix = agg.split("(")[0].lower()
        boundary = "3" if suffix == "count" else "50000"
        add(fragment_case(
            f"having_{suffix}_boundary", "HavingClause", f"HAVING {agg}", "not_equivalent",
            "instructor(id, dept, salary);",
            f"SELECT dept FROM instructor GROUP BY dept HAVING {agg} > {boundary}",
            f"SELECT dept FROM instructor GROUP BY dept HAVING {agg} >= {boundary}",
            ["having"], cfg_labels=["having", "agg-count"], attack_kind="aggregate_exact_boundary",
        ))

    # Ordering and cardinality.
    order_cases = [
        ("direction", "ORDER BY ASC/DESC", "SELECT title FROM course ORDER BY credits DESC", "SELECT title FROM course ORDER BY credits ASC"),
        ("secondary_missing", "OrderItem list", "SELECT title FROM course ORDER BY credits ASC, title DESC", "SELECT title FROM course ORDER BY credits ASC"),
        ("nulls_last", "NULLS LAST", "SELECT title, credits FROM course ORDER BY credits ASC NULLS LAST", "SELECT title, credits FROM course ORDER BY credits ASC"),
    ]
    for suffix, alternative, standard_sql, student_sql in order_cases:
        add(fragment_case(
            f"order_{suffix}", "OrderByClause", alternative, "not_equivalent",
            "course(id, title, credits);", standard_sql, student_sql,
            ["order-by"], cfg_labels=["order-by"], attack_kind="ordered_compare",
        ))
    add(fragment_case(
        "limit_value", "LimitClause", "LIMIT Number", "not_equivalent",
        "course(id, title);", "SELECT title FROM course LIMIT 2", "SELECT title FROM course LIMIT 4",
        ["limit"], cfg_labels=["limit-offset"], attack_kind="cardinality_boundary",
    ))
    add(fragment_case(
        "limit_offset", "OffsetClause", "OFFSET Number", "not_equivalent",
        "course(id, title);", "SELECT title FROM course ORDER BY id LIMIT 2 OFFSET 1",
        "SELECT title FROM course ORDER BY id LIMIT 2 OFFSET 2",
        ["limit"], cfg_labels=["limit-offset"], attack_kind="cardinality_boundary",
    ))
    add(fragment_case(
        "limit_top_equivalent", "LimitClause", "TOP/LIMIT", "equivalent",
        "course(id, title);", "SELECT TOP 2 title FROM course ORDER BY id", "SELECT title FROM course ORDER BY id LIMIT 2",
        cfg_labels=["limit-offset"], attack_kind="dialect_equivalence",
    ))

    # Window functions, partition/order and frames.
    window_cases = [
        ("row_number_partition", "ROW_NUMBER", "ROW_NUMBER() OVER (PARTITION BY dept ORDER BY salary DESC)", "ROW_NUMBER() OVER (ORDER BY salary DESC)"),
        ("rank_row_number", "RANK/ROW_NUMBER", "RANK() OVER (PARTITION BY dept ORDER BY salary DESC)", "ROW_NUMBER() OVER (PARTITION BY dept ORDER BY salary DESC)"),
        ("dense_rank_rank", "DENSE_RANK/RANK", "DENSE_RANK() OVER (PARTITION BY dept ORDER BY salary DESC)", "RANK() OVER (PARTITION BY dept ORDER BY salary DESC)"),
        ("aggregate_partition", "SUM OVER", "SUM(salary) OVER (PARTITION BY dept)", "SUM(salary) OVER ()"),
        ("order_frame_default", "default frame", "SUM(salary) OVER (PARTITION BY dept ORDER BY salary)", "SUM(salary) OVER (PARTITION BY dept)"),
        ("rows_range", "ROWS/RANGE", "SUM(salary) OVER (PARTITION BY dept ORDER BY salary ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)", "SUM(salary) OVER (PARTITION BY dept ORDER BY salary RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"),
    ]
    for suffix, alternative, standard_expr, student_expr in window_cases:
        add(fragment_case(
            f"window_{suffix}", "OverClause", alternative, "not_equivalent",
            "instructor(id, name, dept, salary);",
            f"SELECT name, {standard_expr} AS value FROM instructor",
            f"SELECT name, {student_expr} AS value FROM instructor",
            ["window-row-number", "order-by"], cfg_labels=["window-agg" if standard_expr.startswith("SUM") else "window-row-number"],
            attack_kind="window_tie_frame",
        ))
    add(fragment_case(
        "window_named_inline_equivalent", "OverClause", "WINDOW reference", "equivalent",
        "instructor(id, name, dept, salary);",
        "SELECT name, SUM(salary) OVER w FROM instructor WINDOW w AS (PARTITION BY dept)",
        "SELECT name, SUM(salary) OVER (PARTITION BY dept) FROM instructor",
        cfg_labels=["window-agg"], attack_kind="equivalent_rewrite",
    ))

    # Set operators.
    set_cases = [
        ("union_all", "UNION ALL", "SELECT title FROM course WHERE credits > 2 UNION SELECT title FROM course WHERE credits < 5", "SELECT title FROM course WHERE credits > 2 UNION ALL SELECT title FROM course WHERE credits < 5", ["union"]),
        ("intersect_union", "INTERSECT", "SELECT title FROM course WHERE credits > 2 INTERSECT SELECT title FROM course WHERE credits < 5", "SELECT title FROM course WHERE credits > 2 UNION SELECT title FROM course WHERE credits < 5", ["intersect"]),
        ("except_removed", "EXCEPT", "SELECT title FROM course EXCEPT SELECT title FROM course WHERE credits = 3", "SELECT title FROM course", ["except"]),
        ("branch_swapped_predicates", "branch predicates", "SELECT title FROM course WHERE credits = 1 UNION SELECT title FROM course WHERE credits = 3", "SELECT title FROM course WHERE credits = 2 UNION SELECT title FROM course WHERE credits = 4", ["union", "where"]),
    ]
    for suffix, alternative, standard_sql, student_sql, kps in set_cases:
        add(fragment_case(
            f"set_{suffix}", "SetQuery", alternative, "not_equivalent",
            "course(id, title, credits);", standard_sql, student_sql, kps,
            cfg_labels=["intersect" if "intersect" in suffix else "except" if "except" in suffix else "union"],
            attack_kind="set_overlap",
        ))
    add(fragment_case(
        "set_union_branch_order_equivalent", "SetQuery", "UNION commutativity", "equivalent",
        "course(id, title, credits);",
        "SELECT title FROM course WHERE credits = 1 UNION SELECT title FROM course WHERE credits = 3",
        "SELECT title FROM course WHERE credits = 3 UNION SELECT title FROM course WHERE credits = 1",
        cfg_labels=["union"], attack_kind="equivalent_rewrite",
    ))

    # CTE single/list/recursive alternatives.
    add(fragment_case(
        "cte_inline_equivalent", "WithClause", "single CTE", "equivalent",
        "employee(id, name, salary);",
        "WITH e AS (SELECT name FROM employee WHERE salary > 3) SELECT name FROM e",
        "SELECT name FROM employee WHERE salary > 3",
        cfg_labels=["cte"], attack_kind="equivalent_rewrite",
    ))
    add(fragment_case(
        "cte_filter_changed", "CteDef", "CTE body", "not_equivalent",
        "employee(id, name, salary);",
        "WITH e AS (SELECT name FROM employee WHERE salary > 3) SELECT name FROM e",
        "WITH e AS (SELECT name FROM employee WHERE salary < 3) SELECT name FROM e",
        ["cte", "where"], cfg_labels=["cte"], attack_kind="cte_base_constraint",
    ))
    add(fragment_case(
        "cte_list_inline_equivalent", "CteList", "CteDef (, CteDef)*", "equivalent",
        "employee(id, name, salary);",
        "WITH a AS (SELECT * FROM employee), b AS (SELECT name FROM a WHERE salary > 3) SELECT name FROM b",
        "SELECT name FROM employee WHERE salary > 3",
        cfg_labels=["cte"], attack_kind="equivalent_rewrite",
    ))
    add(fragment_case(
        "cte_recursive_boundary", "WithClause", "WITH RECURSIVE", "not_equivalent", "",
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 5) SELECT x FROM n",
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 3) SELECT x FROM n",
        ["cte-recursive"], cfg_labels=["cte-recursive"], attack_kind="recursive_boundary",
    ))

    # Extended production alternatives: lexical forms, expressions, dialects,
    # and semantic edge cases omitted by the first operator-oriented corpus.
    extended_cases = [
        fragment_case(
            "query_trailing_semicolon_comment", "Query", "SelectQuery ; Comment", "equivalent",
            "student(id, name);", "SELECT name FROM student", "SELECT name FROM student; -- trailing comment",
            cfg_labels=["select-basic"], attack_kind="lexical_equivalence",
        ),
        fragment_case(
            "query_select_without_from", "SelectBody", "SELECT Expr", "not_equivalent",
            "", "SELECT 1", "SELECT 2", ["select-basic"], cfg_labels=["select-basic"],
            attack_kind="constant_query",
        ),
        fragment_case(
            "query_distinct_missing", "SelectBody", "SELECT DISTINCT", "not_equivalent",
            "takes(id, course_id);", "SELECT DISTINCT course_id FROM takes", "SELECT course_id FROM takes",
            ["distinct"], cfg_labels=["distinct"], attack_kind="duplicate_projection",
        ),
        fragment_case(
            "query_delete_outside_fragment", "Query", "DELETE", "syntax_rejected",
            "student(id, name);", "SELECT name FROM student", "DELETE FROM student WHERE id = 1",
            ["select-basic"], cfg_labels=["select-basic"], attack_kind="non_query_guard",
            note="Phase 1 Query is intended to admit read-only SELECT statements only.",
        ),
        fragment_case(
            "query_update_outside_fragment", "Query", "UPDATE", "syntax_rejected",
            "student(id, name);", "SELECT name FROM student", "UPDATE student SET name = 'x' WHERE id = 1",
            ["select-basic"], cfg_labels=["select-basic"], attack_kind="non_query_guard",
        ),
        fragment_case(
            "comparison_bang_neq", "Comparison", "CompOp:!=", "not_equivalent",
            "course(id, title, credits);", "SELECT title FROM course WHERE credits != 3",
            "SELECT title FROM course WHERE credits = 3", ["where"], cfg_labels=["where-comp"],
            attack_kind="boundary_probe",
        ),
        fragment_case(
            "comparison_is_distinct_null", "Comparison", "IS DISTINCT FROM", "not_equivalent",
            "employee(id, name, manager_id);",
            "SELECT name FROM employee WHERE manager_id IS DISTINCT FROM 3",
            "SELECT name FROM employee WHERE manager_id <> 3", ["where", "comp-null"],
            cfg_labels=["where-comp", "null-handling"], attack_kind="null_three_valued_logic",
        ),
        fragment_case(
            "comparison_is_not_distinct_null", "Comparison", "IS NOT DISTINCT FROM", "not_equivalent",
            "employee(id, name, manager_id);",
            "SELECT manager_id IS NOT DISTINCT FROM 3 FROM employee",
            "SELECT manager_id = 3 FROM employee", ["select-basic", "comp-null"],
            cfg_labels=["where-comp", "null-handling"], attack_kind="null_three_valued_logic",
        ),
        fragment_case(
            "predicate_de_morgan_equivalent", "Predicate", "NOT/AND/OR De Morgan", "equivalent",
            "course(id, title, credits);",
            "SELECT title FROM course WHERE NOT (credits < 3 OR credits > 6)",
            "SELECT title FROM course WHERE credits >= 3 AND credits <= 6",
            cfg_labels=["where"], attack_kind="equivalent_rewrite",
        ),
        fragment_case(
            "predicate_double_not_equivalent", "Predicate", "NOT NOT Predicate", "equivalent",
            "course(id, title, credits);", "SELECT title FROM course WHERE NOT NOT credits = 3",
            "SELECT title FROM course WHERE credits = 3", cfg_labels=["where"],
            attack_kind="equivalent_rewrite",
        ),
        fragment_case(
            "predicate_is_true", "Predicate", "Predicate IS TRUE", "equivalent",
            "course(id, title, credits);", "SELECT title FROM course WHERE (credits > 3) IS TRUE",
            "SELECT title FROM course WHERE credits > 3", cfg_labels=["where"],
            attack_kind="boolean_three_valued_logic",
        ),
        fragment_case(
            "null_coalesce_third_argument", "NullExpr", "COALESCE ExprList", "not_equivalent",
            "employee(id, name, dept_name);",
            "SELECT COALESCE(dept_name, name, 'Unknown') FROM employee",
            "SELECT COALESCE(dept_name, 'Unknown') FROM employee", ["null-handling"],
            cfg_labels=["null-handling"], attack_kind="null_branch_coverage",
        ),
        fragment_case(
            "null_nullif_argument_changed", "NullExpr", "NULLIF (Expr, Expr)", "not_equivalent",
            "sales(id, amount);", "SELECT NULLIF(amount, 3) FROM sales", "SELECT NULLIF(amount, 4) FROM sales",
            ["null-handling"], cfg_labels=["null-handling"], attack_kind="null_boundary",
        ),
        fragment_case(
            "between_inclusive_expansion", "BetweenExpr", "BETWEEN inclusive", "equivalent",
            "course(id, title, credits);", "SELECT title FROM course WHERE credits BETWEEN 3 AND 5",
            "SELECT title FROM course WHERE credits >= 3 AND credits <= 5",
            cfg_labels=["between"], attack_kind="equivalent_rewrite",
        ),
        fragment_case(
            "like_single_wildcard", "LikeExpr", "LIKE '_'", "not_equivalent",
            "course(id, title);", "SELECT title FROM course WHERE title LIKE 'Data_'",
            "SELECT title FROM course WHERE title LIKE 'Data%'", ["where"], cfg_labels=["like"],
            attack_kind="pattern_counterexample",
        ),
        fragment_case(
            "like_escape_literal_percent", "LikeExpr", "LIKE ESCAPE", "not_equivalent",
            "course(id, title);", "SELECT title FROM course WHERE title LIKE 'Data!%' ESCAPE '!'",
            "SELECT title FROM course WHERE title LIKE 'Data%'", ["where"], cfg_labels=["like"],
            attack_kind="pattern_counterexample",
        ),
        fragment_case(
            "expr_unary_minus", "Expr", "UnaryOp:-", "not_equivalent",
            "sales(id, amount);", "SELECT -amount FROM sales", "SELECT amount FROM sales",
            ["select-basic"], cfg_labels=["arithmetic"], attack_kind="expression_value",
        ),
        fragment_case(
            "expr_parenthesized_precedence", "Expr", "(Expr)", "not_equivalent",
            "sales(id, amount);", "SELECT (amount + 2) * 3 FROM sales", "SELECT amount + 2 * 3 FROM sales",
            ["select-basic"], cfg_labels=["arithmetic"], attack_kind="operator_precedence",
        ),
        fragment_case(
            "expr_cast_integer", "Expr", "CAST AS INTEGER", "not_equivalent",
            "sales(id, amount);", "SELECT CAST(amount AS INTEGER) FROM sales", "SELECT amount FROM sales",
            ["select-basic"], cfg_labels=["arithmetic"], attack_kind="type_coercion",
            sql_dialect="sqlite",
        ),
        fragment_case(
            "expr_string_concat", "Expr", "ConcatOp:||", "not_equivalent",
            "student(id, name);", "SELECT name || '_x' FROM student", "SELECT name FROM student",
            ["select-basic"], cfg_labels=["arithmetic"], attack_kind="expression_value",
            sql_dialect="sqlite",
        ),
        fragment_case(
            "literal_decimal_scientific", "Literal", "Decimal/Scientific", "equivalent",
            "", "SELECT 100.0", "SELECT 1e2", cfg_labels=["select-basic"], attack_kind="literal_equivalence",
        ),
        fragment_case(
            "literal_string_escape", "Literal", "Escaped String", "not_equivalent",
            "", "SELECT 'O''Brien'", "SELECT 'OBrien'", ["select-basic"], cfg_labels=["select-basic"],
            attack_kind="literal_value",
        ),
        fragment_case(
            "literal_boolean_numeric", "Literal", "BOOL", "equivalent",
            "", "SELECT TRUE", "SELECT 1", cfg_labels=["select-basic"], attack_kind="literal_equivalence",
        ),
        fragment_case(
            "identifier_quoted_columns", "ColumnRef", "Quoted Ident", "equivalent",
            '"order"("select", "group");', 'SELECT "select" FROM "order"', 'SELECT `select` FROM `order`',
            cfg_labels=["select-basic"], attack_kind="identifier_quoting",
            sql_dialect="sqlite",
        ),
        fragment_case(
            "function_abs", "FuncCall", "ABS", "not_equivalent",
            "sales(id, amount);", "SELECT ABS(amount) FROM sales", "SELECT amount FROM sales",
            ["select-basic"], cfg_labels=["select-basic"], attack_kind="scalar_function",
        ),
        fragment_case(
            "function_lower_upper", "FuncCall", "LOWER/UPPER", "not_equivalent",
            "student(id, name);", "SELECT LOWER(name) FROM student", "SELECT UPPER(name) FROM student",
            ["select-basic"], cfg_labels=["select-basic"], attack_kind="scalar_function",
        ),
        fragment_case(
            "function_round_precision", "FuncCall", "ROUND", "not_equivalent",
            "sales(id, amount);", "SELECT ROUND(amount, 0) FROM sales", "SELECT ROUND(amount, 2) FROM sales",
            ["select-basic"], cfg_labels=["select-basic"], attack_kind="scalar_function",
        ),
        fragment_case(
            "function_trim", "FuncCall", "TRIM", "not_equivalent",
            "student(id, name);", "SELECT TRIM(name) FROM student", "SELECT name FROM student",
            ["select-basic"], cfg_labels=["select-basic"], attack_kind="scalar_function",
        ),
        fragment_case(
            "from_self_join_key", "JoinStmt", "Self JOIN", "not_equivalent",
            "employee(id, name, manager_id);",
            "SELECT e.name FROM employee e JOIN employee m ON e.manager_id = m.id",
            "SELECT e.name FROM employee e JOIN employee m ON e.id = m.id", ["join-on"],
            cfg_labels=["join-inner", "join-on"], attack_kind="join_topology",
        ),
        fragment_case(
            "join_natural_on_equivalent", "JoinCond", "NATURAL JOIN", "equivalent",
            "student(id, name); takes(id, course_id);",
            "SELECT name FROM student NATURAL JOIN takes",
            "SELECT name FROM student JOIN takes ON student.id = takes.id",
            cfg_labels=["join-inner", "join-on"], attack_kind="equivalent_rewrite",
        ),
        fragment_case(
            "join_using_multiple_columns", "JoinCond", "USING ColumnList", "equivalent",
            "enrollment(id, year, grade); exam(id, year, score);",
            "SELECT grade FROM enrollment JOIN exam USING (id, year)",
            "SELECT grade FROM enrollment e JOIN exam x ON e.id = x.id AND e.year = x.year",
            cfg_labels=["join-inner", "join-on"], attack_kind="equivalent_rewrite",
        ),
        fragment_case(
            "join_on_conjunct_removed", "JoinCond", "ON Predicate AND Predicate", "not_equivalent",
            "enrollment(id, year, grade); exam(id, year, score);",
            "SELECT grade FROM enrollment e JOIN exam x ON e.id = x.id AND e.year = x.year",
            "SELECT grade FROM enrollment e JOIN exam x ON e.id = x.id", ["join-on"],
            cfg_labels=["join-inner", "join-on"], attack_kind="join_topology",
        ),
        fragment_case(
            "from_lateral_correlated", "TSource", "LATERAL SubQuery", "equivalent",
            "student(id, name);",
            "SELECT s.name, x.value FROM student s CROSS JOIN LATERAL (SELECT s.id + 1 AS value) x",
            "SELECT name, id + 1 AS value FROM student", cfg_labels=["subquery-scalar"],
            attack_kind="dialect_equivalence",
        ),
        fragment_case(
            "subquery_in_exists_equivalent", "SubQuery", "IN/EXISTS", "equivalent",
            "student(id, name); takes(id, course_id);",
            "SELECT name FROM student s WHERE id IN (SELECT id FROM takes)",
            "SELECT name FROM student s WHERE EXISTS (SELECT 1 FROM takes t WHERE t.id = s.id)",
            cfg_labels=["subquery-in", "subquery-exists"], attack_kind="equivalent_rewrite",
        ),
        fragment_case(
            "subquery_not_in_null_trap", "SubQuery", "NOT IN with NULL", "not_equivalent",
            "employee(id, name, manager_id);",
            "SELECT name FROM employee e WHERE NOT EXISTS (SELECT 1 FROM employee m WHERE m.manager_id = e.id)",
            "SELECT name FROM employee WHERE id NOT IN (SELECT manager_id FROM employee)",
            ["subquery-correlated", "where"], cfg_labels=["subquery-in", "subquery-exists", "null-handling"],
            attack_kind="null_three_valued_logic",
        ),
        fragment_case(
            "subquery_nested_in", "SubQuery", "nested SubQuery", "not_equivalent",
            "student(id, name); takes(id, course_id); course(id, credits);",
            "SELECT name FROM student WHERE id IN (SELECT id FROM takes WHERE course_id IN (SELECT id FROM course WHERE credits > 3))",
            "SELECT name FROM student WHERE id IN (SELECT id FROM takes WHERE course_id IN (SELECT id FROM course WHERE credits > 5))",
            ["where", "subquery-scalar"], cfg_labels=["subquery-in"], attack_kind="nested_subquery_boundary",
        ),
        fragment_case(
            "subquery_scalar_empty_null", "SubQuery", "empty scalar SubQuery", "equivalent",
            "student(id, name);", "SELECT (SELECT id FROM student WHERE 1 = 0)", "SELECT NULL",
            cfg_labels=["subquery-scalar", "null-handling"], attack_kind="empty_set_semantics",
        ),
        fragment_case(
            "subquery_equal_any_in", "SubQuery", "= ANY", "equivalent",
            "student(id, name); takes(id, course_id);",
            "SELECT name FROM student WHERE id = ANY (SELECT id FROM takes)",
            "SELECT name FROM student WHERE id IN (SELECT id FROM takes)",
            cfg_labels=["subquery-in"], attack_kind="quantified_subquery",
        ),
        fragment_case(
            "aggregate_count_nullable", "FuncCall", "COUNT nullable Expr", "not_equivalent",
            "employee(id, manager_id);", "SELECT COUNT(manager_id) FROM employee", "SELECT COUNT(*) FROM employee",
            ["agg-count"], cfg_labels=["agg-count", "null-handling"], attack_kind="null_aggregate",
        ),
        fragment_case(
            "aggregate_sum_null_coalesce", "FuncCall", "SUM NULL result", "not_equivalent",
            "sales(id, amount);", "SELECT SUM(amount) FROM sales WHERE 1 = 0",
            "SELECT COALESCE(SUM(amount), 0) FROM sales WHERE 1 = 0", ["agg-count", "null-handling"],
            cfg_labels=["agg-count", "null-handling"], attack_kind="empty_set_semantics",
        ),
        fragment_case(
            "group_expression", "GroupByClause", "GROUP BY Expr", "not_equivalent",
            "sales(id, amount);", "SELECT amount % 2, COUNT(*) FROM sales GROUP BY amount % 2",
            "SELECT amount % 3, COUNT(*) FROM sales GROUP BY amount % 3", ["group-by"],
            cfg_labels=["group-by", "arithmetic"], attack_kind="group_partition",
        ),
        fragment_case(
            "having_without_group", "HavingClause", "HAVING without GROUP BY", "not_equivalent",
            "sales(id, amount);", "SELECT COUNT(*) FROM sales HAVING COUNT(*) > 3",
            "SELECT COUNT(*) FROM sales HAVING COUNT(*) > 20", ["having"], cfg_labels=["having", "agg-count"],
            attack_kind="aggregate_exact_boundary",
        ),
        fragment_case(
            "group_rollup", "GroupByClause", "ROLLUP", "not_equivalent",
            "sales(id, region, amount);", "SELECT region, SUM(amount) FROM sales GROUP BY ROLLUP(region)",
            "SELECT region, SUM(amount) FROM sales GROUP BY region", ["group-by"], cfg_labels=["group-by"],
            attack_kind="dialect_boundary",
        ),
        fragment_case(
            "group_cube", "GroupByClause", "CUBE", "not_equivalent",
            "sales(id, region, amount);", "SELECT region, SUM(amount) FROM sales GROUP BY CUBE(region)",
            "SELECT region, SUM(amount) FROM sales GROUP BY region", ["group-by"], cfg_labels=["group-by"],
            attack_kind="dialect_boundary",
        ),
        fragment_case(
            "order_ordinal_column_equivalent", "OrderItem", "Ordinal", "equivalent",
            "course(id, title, credits);", "SELECT title, credits FROM course ORDER BY 2, 1",
            "SELECT title, credits FROM course ORDER BY credits, title", cfg_labels=["order-by"],
            attack_kind="equivalent_rewrite",
        ),
        fragment_case(
            "order_alias_expression_equivalent", "OrderItem", "Alias", "equivalent",
            "course(id, title, credits);", "SELECT title, credits + 1 AS c FROM course ORDER BY c DESC",
            "SELECT title, credits + 1 AS c FROM course ORDER BY credits + 1 DESC", cfg_labels=["order-by", "alias"],
            attack_kind="equivalent_rewrite",
        ),
        fragment_case(
            "order_expression_changed", "OrderItem", "Expr", "not_equivalent",
            "course(id, title, credits);", "SELECT title FROM course ORDER BY credits % 2, id",
            "SELECT title FROM course ORDER BY credits % 3, id", ["order-by"], cfg_labels=["order-by", "arithmetic"],
            attack_kind="ordered_compare",
        ),
        fragment_case(
            "order_nulls_first_last", "OrderItem", "NULLS FIRST/LAST", "not_equivalent",
            "course(id, title, credits);", "SELECT title, credits FROM course ORDER BY credits NULLS FIRST",
            "SELECT title, credits FROM course ORDER BY credits NULLS LAST", ["order-by"], cfg_labels=["order-by"],
            attack_kind="ordered_compare",
        ),
        fragment_case(
            "limit_fetch_first_equivalent", "LimitClause", "FETCH FIRST", "equivalent",
            "course(id, title);", "SELECT title FROM course ORDER BY id FETCH FIRST 2 ROWS ONLY",
            "SELECT title FROM course ORDER BY id LIMIT 2", cfg_labels=["limit-offset"], attack_kind="dialect_equivalence",
        ),
        fragment_case(
            "limit_mysql_comma_equivalent", "LimitClause", "LIMIT offset,count", "equivalent",
            "course(id, title);", "SELECT title FROM course ORDER BY id LIMIT 1, 2",
            "SELECT title FROM course ORDER BY id LIMIT 2 OFFSET 1", cfg_labels=["limit-offset"],
            attack_kind="dialect_equivalence",
        ),
        fragment_case(
            "window_lag_offset", "FuncCall", "LAG", "not_equivalent",
            "instructor(id, name, salary);", "SELECT name, LAG(salary, 1) OVER (ORDER BY id) FROM instructor",
            "SELECT name, LAG(salary, 2) OVER (ORDER BY id) FROM instructor", ["window-row-number"],
            cfg_labels=["window-agg"], attack_kind="window_offset",
        ),
        fragment_case(
            "window_lead_default", "FuncCall", "LEAD", "not_equivalent",
            "instructor(id, name, salary);", "SELECT name, LEAD(salary, 1, 0) OVER (ORDER BY id) FROM instructor",
            "SELECT name, LEAD(salary, 1, -1) OVER (ORDER BY id) FROM instructor", ["window-row-number"],
            cfg_labels=["window-agg"], attack_kind="window_boundary",
        ),
        fragment_case(
            "window_ntile_bucket", "FuncCall", "NTILE", "not_equivalent",
            "instructor(id, name, salary);", "SELECT name, NTILE(2) OVER (ORDER BY salary) FROM instructor",
            "SELECT name, NTILE(3) OVER (ORDER BY salary) FROM instructor", ["window-row-number"],
            cfg_labels=["window-row-number"], attack_kind="window_boundary",
        ),
        fragment_case(
            "window_first_last_value", "FuncCall", "FIRST_VALUE/LAST_VALUE", "not_equivalent",
            "instructor(id, name, dept, salary);",
            "SELECT name, FIRST_VALUE(salary) OVER (PARTITION BY dept ORDER BY salary ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) FROM instructor",
            "SELECT name, LAST_VALUE(salary) OVER (PARTITION BY dept ORDER BY salary ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) FROM instructor",
            ["window-row-number"], cfg_labels=["window-agg"], attack_kind="window_frame",
        ),
        fragment_case(
            "window_frame_preceding", "Frame", "N PRECEDING", "not_equivalent",
            "instructor(id, name, salary);",
            "SELECT name, SUM(salary) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM instructor",
            "SELECT name, SUM(salary) OVER (ORDER BY id ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) FROM instructor",
            ["window-row-number"], cfg_labels=["window-agg"], attack_kind="window_frame",
        ),
        fragment_case(
            "window_qualify", "SelectBody", "QUALIFY", "not_equivalent",
            "instructor(id, name, dept, salary);",
            "SELECT name, ROW_NUMBER() OVER (PARTITION BY dept ORDER BY salary DESC) AS rn FROM instructor QUALIFY rn = 1",
            "SELECT name, ROW_NUMBER() OVER (PARTITION BY dept ORDER BY salary DESC) AS rn FROM instructor QUALIFY rn <= 2",
            ["window-row-number"], cfg_labels=["window-row-number"], attack_kind="dialect_boundary",
        ),
        fragment_case(
            "set_three_union_branches", "SetQuery", "SetOp chain", "not_equivalent",
            "course(id, title, credits);",
            "SELECT title FROM course WHERE credits = 1 UNION SELECT title FROM course WHERE credits = 3 UNION SELECT title FROM course WHERE credits = 5",
            "SELECT title FROM course WHERE credits = 1 UNION SELECT title FROM course WHERE credits = 3",
            ["union"], cfg_labels=["union"], attack_kind="set_branch_asymmetry",
        ),
        fragment_case(
            "set_intersect_all", "SetOp", "INTERSECT ALL", "not_equivalent",
            "course(id, title, credits);", "SELECT title FROM course INTERSECT ALL SELECT title FROM course",
            "SELECT title FROM course INTERSECT SELECT title FROM course", ["intersect"], cfg_labels=["intersect"],
            attack_kind="dialect_boundary",
        ),
        fragment_case(
            "set_except_all", "SetOp", "EXCEPT ALL", "not_equivalent",
            "course(id, title, credits);", "SELECT title FROM course EXCEPT ALL SELECT title FROM course WHERE credits = 3",
            "SELECT title FROM course EXCEPT SELECT title FROM course WHERE credits = 3", ["except"], cfg_labels=["except"],
            attack_kind="dialect_boundary",
        ),
        fragment_case(
            "cte_column_list", "CteDef", "Ident ColumnList AS", "equivalent",
            "employee(id, name);", "WITH e(x, y) AS (SELECT id, name FROM employee) SELECT y FROM e",
            "SELECT name FROM employee", cfg_labels=["cte"], attack_kind="equivalent_rewrite",
        ),
        fragment_case(
            "cte_dependency_chain", "CteList", "dependent CteDef", "not_equivalent",
            "employee(id, name, salary);",
            "WITH a AS (SELECT * FROM employee), b AS (SELECT name FROM a WHERE salary > 3) SELECT name FROM b",
            "WITH a AS (SELECT * FROM employee), b AS (SELECT name FROM a WHERE salary > 5) SELECT name FROM b",
            ["cte", "where"], cfg_labels=["cte"], attack_kind="cte_dependency",
        ),
        fragment_case(
            "cte_recursive_union_distinct", "WithClause", "recursive UNION/UNION ALL", "equivalent", "",
            "WITH RECURSIVE n(x) AS (SELECT 1 UNION SELECT x + 1 FROM n WHERE x < 4) SELECT x FROM n",
            "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 4) SELECT x FROM n",
            cfg_labels=["cte-recursive", "union"], attack_kind="recursive_set_semantics",
        ),
    ]
    cases.extend(extended_cases)

    return cases


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    bucket_names = (
        "supported",
        "supported_with_limits",
        "semantic_boundary",
        "known_gap",
        "engine_gap",
    )
    by_production = defaultdict(
        lambda: {"total": 0, **{name: 0 for name in bucket_names}}
    )
    by_expectation = Counter()
    for result in results:
        bucket = result["capability_bucket"]
        entry = by_production[result["production"]]
        entry["total"] += 1
        entry[bucket] += 1
        by_expectation[result["expectation"]] += 1
    supported = sum(item["capability_bucket"] == "supported" for item in results)
    failure_classes = Counter(
        item.get("failure_class") or "unclassified"
        for item in results
        if item["capability_bucket"] != "supported"
    )
    return {
        "total_cases": len(results),
        "supported_cases": supported,
        "semantic_boundary_cases": sum(
            item["capability_bucket"] == "semantic_boundary" for item in results
        ),
        "known_gap_cases": sum(
            item["capability_bucket"] == "known_gap" for item in results
        ),
        "engine_gap_cases": sum(
            item["capability_bucket"] == "engine_gap" for item in results
        ),
        "unresolved_cases": len(results) - supported,
        "support_rate": round(supported / len(results), 4) if results else 0,
        "by_expectation": dict(by_expectation),
        "by_production": dict(sorted(by_production.items())),
        "by_failure_class": dict(sorted(failure_classes.items())),
        "supported_ids": [item["id"] for item in results if item["capability_bucket"] == "supported"],
        "known_gap_ids": [item["id"] for item in results if item["capability_bucket"] == "known_gap"],
        "semantic_boundary_ids": [
            item["id"] for item in results
            if item["capability_bucket"] == "semantic_boundary"
        ],
        "engine_gap_ids": [
            item["id"] for item in results
            if item["capability_bucket"] == "engine_gap"
        ],
    }


def classify_failure(result: dict[str, Any]) -> str | None:
    if result["capability_bucket"] == "supported":
        return None
    if result["capability_bucket"] == "semantic_boundary":
        return "semantic_boundary"
    if result["capability_bucket"] == "engine_gap":
        return "engine_gap"
    if not result["strict_standard_parse_ok"] or not result["strict_student_parse_ok"]:
        return "parser_or_dialect"
    if not result["standard_ir_build_ok"] or not result["student_ir_build_ok"]:
        return "ir_build"
    if not result["executed"]:
        return "transpile_or_execution"
    if not result["data_stage_met"]:
        return "data_generation_or_equivalence"
    if not result["attribution_stage_met"]:
        return "attribution"
    if not result["structure_stage_met"]:
        return "ast_diff"
    return "unclassified"


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 1 SQL CFG Fragment Capability",
        "",
        f"Generated at: `{payload['generated_at']}`",
        "",
        "This is production-level coverage. A supported result means the current bounded",
        "pipeline handled this concrete adversarial pair; it is not a formal proof for all databases.",
        "",
        "## Summary",
        "",
        f"- Total fragment attacks: `{summary['total_cases']}`",
        f"- Supported: `{summary['supported_cases']}`",
        f"- Semantic boundaries: `{summary['semantic_boundary_cases']}`",
        f"- Known gaps: `{summary['known_gap_cases']}`",
        f"- Engine gaps: `{summary['engine_gap_cases']}`",
        f"- Support rate: `{summary['support_rate']:.1%}`",
        f"- Failure classes: `{summary['by_failure_class']}`",
        "",
        "## Production Matrix",
        "",
        "| production | total | supported | semantic boundary | known gap | engine gap |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for production, stats in summary["by_production"].items():
        lines.append(
            f"| {production} | {stats['total']} | {stats['supported']} | "
            f"{stats['semantic_boundary']} | {stats['known_gap']} | {stats['engine_gap']} |"
        )
    lines.extend([
        "",
        "## Fragment Matrix",
        "",
        "| result | production | alternative | attack | expected | executed | equivalent |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ])
    for item in payload["results"]:
        lines.append(
            f"| {item['capability_bucket']} | {item['production']} | {item['alternative']} | "
            f"{item['id']} | {item['expectation']} | {item['executed']} | {item['is_equivalent']} |"
        )
    gaps = [item for item in payload["results"] if item["capability_bucket"] != "supported"]
    lines.extend(["", "## Boundaries And Gaps", ""])
    if not gaps:
        lines.append("No gap was found by this concrete corpus.")
    for item in gaps:
        lines.extend([
            f"### {item['id']}",
            "",
            f"- Production: `{item['production']}` / `{item['alternative']}`",
            f"- Expected: `{item['expectation']}`",
            f"- Pipeline: executed=`{item['executed']}`, equivalent=`{item['is_equivalent']}`",
            f"- Stages parse/structure/data/attribution: `{item['parse_stage_met']}` / `{item['structure_stage_met']}` / `{item['data_stage_met']}` / `{item['attribution_stage_met']}`",
            f"- Error: `{item['error']}`",
            f"- Failure class: `{item['failure_class']}`",
            "",
            "```sql",
            f"-- standard\n{item['standard']}",
            "",
            f"-- student\n{item['student']}",
            "```",
            "",
            f"- Generated database: `{item['test_database']}`",
            f"- Standard rows: `{item['standard_rows_sample']}`",
            f"- Student rows: `{item['student_rows_sample']}`",
            f"- Attributions: `{item['top_attributions']}`",
            "",
        ])
    return "\n".join(lines)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results = [run_case(case) for case in build_cases()]
    for result in results:
        result["failure_class"] = classify_failure(result)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summarize(results),
        "results": results,
    }
    json_path = OUTPUT_DIR / "phase1_cfg_fragment_capability.json"
    md_path = OUTPUT_DIR / "phase1_cfg_fragment_capability.md"
    supported_path = OUTPUT_DIR / "phase1_cfg_supported_samples.jsonl"
    gaps_path = OUTPUT_DIR / "phase1_cfg_known_gaps.jsonl"
    json_path.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    supported_path.write_text(
        "\n".join(
            json.dumps(_json_safe(item), ensure_ascii=False)
            for item in results
            if item["capability_bucket"] == "supported"
        ) + "\n",
        encoding="utf-8",
    )
    gaps_path.write_text(
        "\n".join(
            json.dumps(_json_safe(item), ensure_ascii=False)
            for item in results
            if item["capability_bucket"] != "supported"
        ) + "\n",
        encoding="utf-8",
    )
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")
    print(f"Supported samples: {supported_path}")
    print(f"Known gaps: {gaps_path}")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
