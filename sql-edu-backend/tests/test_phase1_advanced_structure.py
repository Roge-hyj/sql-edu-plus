import pytest
from sqlglot import parse_one

from core.ast_schema import SQLStructureIR
from core.parseval_data_generator import (
    _detect_sqlite_unsupported_features,
    extract_ast_diffs,
    generate_and_compare,
)


def _diff_pairs(standard_sql: str, student_sql: str, dialect: str | None = None):
    return {
        (diff.clause_category, diff.diff_type)
        for diff in extract_ast_diffs(standard_sql, student_sql, dialect=dialect)
    }


def test_aggregate_filter_is_not_misclassified_as_top_level_where():
    ir = SQLStructureIR.from_ast(
        parse_one("SELECT COUNT(*) FILTER (WHERE score > 0) FROM exam")
    )

    assert ir.where_predicates == []
    assert "where" not in ir.feature_kps()
    assert "AGGREGATE FILTER" in {
        item["context"] for item in ir.predicate_ir
    }
    assert ir.aggregate_functions[0]["filter_predicate"] == "score > 0"


def test_window_order_is_not_misclassified_as_top_level_order_by():
    ir = SQLStructureIR.from_ast(
        parse_one(
            "SELECT ROW_NUMBER() OVER (PARTITION BY dept ORDER BY score DESC) "
            "FROM exam"
        )
    )

    assert ir.order_by == []
    assert "order-by" not in ir.feature_kps()
    assert ir.window_function_details[0]["order_by"]


def test_qualify_has_typed_predicate_context():
    ir = SQLStructureIR.from_ast(
        parse_one(
            "SELECT name, ROW_NUMBER() OVER (ORDER BY score DESC) AS rn "
            "FROM exam QUALIFY rn = 1"
        )
    )

    assert ir.qualify_predicates == ["QUALIFY rn = 1"]
    assert "qualify" in ir.feature_kps()
    assert "QUALIFY" in {item["context"] for item in ir.predicate_ir}


@pytest.mark.parametrize(
    ("standard_sql", "student_sql", "dialect", "expected"),
    [
        (
            "SELECT DISTINCT ON (dept) dept, name FROM student ORDER BY dept, name",
            "SELECT DISTINCT dept, name FROM student ORDER BY dept, name",
            "postgres",
            ("DISTINCT ON", "distinct_on_changed"),
        ),
        (
            "SELECT region, SUM(amount) FROM sales GROUP BY ROLLUP(region)",
            "SELECT region, SUM(amount) FROM sales GROUP BY region",
            None,
            ("ROLLUP", "rollup_changed"),
        ),
        (
            "SELECT region, SUM(amount) FROM sales "
            "GROUP BY GROUPING SETS ((region), ())",
            "SELECT region, SUM(amount) FROM sales GROUP BY region",
            None,
            ("GROUPING SETS", "grouping_sets_changed"),
        ),
        (
            "SELECT COUNT(*) FILTER (WHERE score > 0) FROM exam",
            "SELECT COUNT(*) FROM exam",
            None,
            ("AGGREGATE FILTER", "aggregate_filter_changed"),
        ),
        (
            "SELECT s.name, x.value FROM student s CROSS JOIN LATERAL "
            "(SELECT s.id + 1 AS value) x",
            "SELECT s.name FROM student s",
            "postgres",
            ("LATERAL", "lateral_changed"),
        ),
        (
            "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL "
            "SELECT n + 1 FROM t WHERE n < 3) "
            "SEARCH DEPTH FIRST BY n SET ord SELECT n FROM t",
            "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL "
            "SELECT n + 1 FROM t WHERE n < 3) SELECT n FROM t",
            "postgres",
            ("SEARCH", "recursive_search_changed"),
        ),
        (
            "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL "
            "SELECT n + 1 FROM t WHERE n < 3) "
            "CYCLE n SET is_cycle USING path SELECT n FROM t",
            "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL "
            "SELECT n + 1 FROM t WHERE n < 3) SELECT n FROM t",
            "postgres",
            ("CYCLE", "recursive_cycle_changed"),
        ),
    ],
)
def test_advanced_clause_diff_is_typed(
    standard_sql, student_sql, dialect, expected
):
    assert expected in _diff_pairs(standard_sql, student_sql, dialect)


def test_grouping_sets_is_an_explicit_sqlite_execution_boundary():
    unsupported = _detect_sqlite_unsupported_features(
        "SELECT region, SUM(amount) FROM sales "
        "GROUP BY GROUPING SETS ((region), ())"
    )

    assert "GROUPING_SETS" in unsupported


def test_identical_order_signature_does_not_emit_direction_change():
    diffs = extract_ast_diffs(
        "SELECT DISTINCT ON (dept) dept, name FROM student ORDER BY dept, name",
        "SELECT DISTINCT dept, name FROM student ORDER BY dept, name",
        dialect="postgres",
    )

    assert not any(diff.diff_type == "order_direction_changed" for diff in diffs)


@pytest.mark.parametrize(
    ("schema", "standard_sql", "student_sql", "clause"),
    [
        (
            "instructor(name, dept, salary);",
            "SELECT name, ROW_NUMBER() OVER "
            "(PARTITION BY dept ORDER BY salary DESC) AS rn "
            "FROM instructor QUALIFY rn = 1",
            "SELECT name, ROW_NUMBER() OVER "
            "(PARTITION BY dept ORDER BY salary DESC) AS rn "
            "FROM instructor QUALIFY rn <= 2",
            "QUALIFY",
        ),
        (
            "student(dept, name);",
            "SELECT DISTINCT ON (dept) dept, name FROM student "
            "ORDER BY dept, name",
            "SELECT DISTINCT dept, name FROM student ORDER BY dept, name",
            "DISTINCT ON",
        ),
    ],
)
def test_advanced_executable_clause_closes_data_and_mutation_loop(
    schema, standard_sql, student_sql, clause
):
    run = generate_and_compare(schema, standard_sql, student_sql)
    clause_tests = [
        test for test in run.mutation_evidence["tests"]
        if test["clause"] == clause
    ]

    assert run.executed is True
    assert run.is_equivalent is False
    assert clause_tests
    assert clause_tests[0]["replacement_exec_ok"] is True
    assert clause_tests[0]["fixed_by_replacement"] is True
