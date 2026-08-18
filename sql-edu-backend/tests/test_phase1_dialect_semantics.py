import pytest
from sqlglot import parse_one

from core.parseval_data_generator import (
    _prepare_executable_sql_pair,
    extract_ast_diffs,
    generate_and_compare,
)


@pytest.mark.parametrize(
    ("sql_dialect", "schema", "sql", "expected_feature"),
    [
        (
            "standard",
            "scores(id INTEGER, score INTEGER);",
            "SELECT id FROM scores ORDER BY score DESC "
            "FETCH FIRST 1 ROWS WITH TIES",
            "LIMIT_WITH_TIES",
        ),
        (
            "tsql",
            "scores(id INT, score INT);",
            "SELECT TOP 1 WITH TIES id FROM scores ORDER BY score DESC",
            "LIMIT_WITH_TIES",
        ),
        (
            "mysql",
            "students(name VARCHAR(64));",
            "SELECT GROUP_CONCAT(name ORDER BY name DESC SEPARATOR ',') "
            "FROM students",
            "MYSQL_GROUP_CONCAT_ORDERING",
        ),
        (
            "oracle",
            "students(id NUMBER);",
            "SELECT id FROM students WHERE ROWNUM <= 2",
            "ORACLE_ROWNUM",
        ),
        (
            "oracle",
            "employees(employee_id NUMBER, manager_id NUMBER);",
            "SELECT employee_id FROM employees START WITH manager_id IS NULL "
            "CONNECT BY PRIOR employee_id = manager_id",
            "ORACLE_CONNECT_BY",
        ),
        (
            "oracle",
            "students(name VARCHAR2(64));",
            "SELECT LISTAGG(name, ',') WITHIN GROUP (ORDER BY name) FROM students",
            "ORACLE_LISTAGG",
        ),
    ],
)
def test_dialect_semantics_that_sqlite_cannot_preserve_are_explicit_boundaries(
    sql_dialect,
    schema,
    sql,
    expected_feature,
):
    run = generate_and_compare(
        schema,
        sql,
        sql,
        sql_dialect=sql_dialect,
        execution_backend="sqlite",
    )

    assert run.executed is False
    assert run.is_equivalent is None
    assert run.judge_status == "UNSUPPORTED"
    assert expected_feature in run.data_evidence["unsupported_features"]


@pytest.mark.parametrize(
    ("sql", "expected_feature"),
    [
        (
            "SELECT a.id FROM a FULL OUTER JOIN b ON a.id = b.id",
            "FULL_OUTER_JOIN",
        ),
        (
            "SELECT region, SUM(amount) FROM sales "
            "GROUP BY GROUPING SETS ((region), ())",
            "GROUPING_SETS",
        ),
    ],
)
def test_standard_sql_defaulting_to_mysql_rejects_unavailable_engine_features(
    sql,
    expected_feature,
):
    run = generate_and_compare(
        "a(id INT); b(id INT); sales(region VARCHAR(20), amount DECIMAL(10,2));",
        sql,
        sql,
        sql_dialect="standard",
        default_sql_dialect="mysql",
        execution_backend="auto",
    )

    assert run.executed is False
    assert run.judge_status == "UNSUPPORTED"
    assert expected_feature in run.data_evidence["unsupported_features"]


def test_standard_fetch_with_ties_renders_as_tsql_top_with_ties():
    standard_ast = parse_one(
        "SELECT id FROM scores ORDER BY score DESC "
        "FETCH FIRST 1 ROWS WITH TIES"
    )
    student_ast = parse_one(
        "SELECT id FROM scores ORDER BY score DESC FETCH FIRST 1 ROW ONLY"
    )

    standard, student = _prepare_executable_sql_pair(
        "tsql",
        "unused standard source",
        "unused student source",
        standard_ast=standard_ast,
        student_ast=student_ast,
        target_dialect="tsql",
    )

    assert standard is not None
    assert "TOP 1 WITH TIES" in standard.upper()
    assert "OFFSET" not in standard.upper()
    assert student is not None
    assert "WITH TIES" not in student.upper()


def test_standard_fetch_with_ties_and_offset_has_no_silent_tsql_rendering():
    ast = parse_one(
        "SELECT id FROM scores ORDER BY score DESC OFFSET 2 ROWS "
        "FETCH NEXT 1 ROWS WITH TIES"
    )

    standard, student = _prepare_executable_sql_pair(
        "tsql",
        "unused",
        "unused",
        standard_ast=ast,
        student_ast=ast,
        target_dialect="tsql",
    )

    assert standard is None
    assert student is None


def test_mysql_group_concat_order_and_separator_are_structural_differences():
    diffs = extract_ast_diffs(
        "SELECT GROUP_CONCAT(name ORDER BY name ASC SEPARATOR ',') FROM students",
        "SELECT GROUP_CONCAT(name ORDER BY name DESC SEPARATOR ';') FROM students",
        dialect="mysql",
    )

    aggregate_diffs = [
        diff for diff in diffs
        if diff.clause_category == "AGGREGATE"
        and diff.diff_type == "aggregate_argument_changed"
    ]
    assert aggregate_diffs
    assert aggregate_diffs[0].knowledge_point_id == "aggregate"
    assert "ORDER BY" in aggregate_diffs[0].extra["standard_sql"].upper()
    assert "SEPARATOR" in aggregate_diffs[0].extra["standard_sql"].upper()


@pytest.mark.parametrize("target_dialect", ["postgres", "oracle"])
def test_standard_fetch_with_ties_is_preserved_for_supporting_engines(target_dialect):
    ast = parse_one(
        "SELECT id FROM scores ORDER BY score DESC "
        "FETCH FIRST 1 ROWS WITH TIES"
    )

    standard, _ = _prepare_executable_sql_pair(
        target_dialect,
        "unused",
        "unused",
        standard_ast=ast,
        student_ast=ast,
        target_dialect=target_dialect,
    )

    assert standard is not None
    assert "WITH TIES" in standard.upper()


def test_default_mysql_double_quoted_strings_keep_literal_semantics_in_sqlite():
    run = generate_and_compare(
        "Employees(Age, Department, Firstname);",
        'SELECT Firstname FROM Employees WHERE (Department="Sales" AND Age>25) '
        'OR Department="Marketing"',
        'SELECT Firstname FROM Employees WHERE (Department="Sales" AND Age>=25) '
        'OR Department="Marketing"',
    )

    assert run.executed is True, run.error
    assert run.is_equivalent is False
    assert "'Sales'" in (run.standard_sqlite or "")
    assert "'Marketing'" in (run.standard_sqlite or "")
