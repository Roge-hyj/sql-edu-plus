import json

import pytest
from sqlglot import parse_one

from core.ast_schema import SQLStructureIR
from core.parseval_data_generator import extract_ast_diffs


def _ir(sql: str, dialect: str | None = None) -> SQLStructureIR:
    return SQLStructureIR.from_ast(parse_one(sql, read=dialect))


@pytest.mark.parametrize(
    ("dialect", "option_sql", "plain_sql", "option", "feature"),
    [
        (
            "tsql",
            "SELECT TOP 10 PERCENT id FROM t ORDER BY id",
            "SELECT TOP 10 id FROM t ORDER BY id",
            "percent",
            "limit-percent",
        ),
        (
            "tsql",
            "SELECT TOP 10 WITH TIES id FROM t ORDER BY id",
            "SELECT TOP 10 id FROM t ORDER BY id",
            "with_ties",
            "limit-with-ties",
        ),
        (
            None,
            "SELECT id FROM t ORDER BY id FETCH FIRST 10 ROWS WITH TIES",
            "SELECT id FROM t ORDER BY id FETCH FIRST 10 ROWS ONLY",
            "with_ties",
            "limit-with-ties",
        ),
    ],
)
def test_limit_options_are_typed_and_change_ir(
    dialect, option_sql, plain_sql, option, feature
):
    option_ir = _ir(option_sql, dialect)
    plain_ir = _ir(plain_sql, dialect)

    assert option_ir.limit_offset == plain_ir.limit_offset == {"limit": "10"}
    assert option_ir.limit_options[option] is True
    assert plain_ir.limit_options[option] is False
    assert option_ir.to_dict() != plain_ir.to_dict()
    assert feature in option_ir.feature_kps()


def test_oracle_hierarchical_query_is_typed_and_nocycle_changes_ir():
    base = (
        "SELECT employee_id FROM employees "
        "START WITH manager_id IS NULL "
        "CONNECT BY {nocycle}PRIOR employee_id = manager_id"
    )
    nocycle_ir = _ir(base.format(nocycle="NOCYCLE "), "oracle")
    plain_ir = _ir(base.format(nocycle=""), "oracle")

    assert nocycle_ir.hierarchical_queries == [
        {
            "start_with": "manager_id IS NULL",
            "connect_by": "PRIOR employee_id = manager_id",
            "nocycle": True,
        }
    ]
    assert plain_ir.hierarchical_queries[0]["nocycle"] is False
    assert nocycle_ir.to_dict() != plain_ir.to_dict()
    assert {
        "hierarchical-query",
        "start-with",
        "connect-by",
        "connect-by-nocycle",
    } <= set(nocycle_ir.feature_kps())


def test_tsql_pivot_fields_are_typed_and_change_ir():
    two_columns = _ir(
        "SELECT * FROM sales "
        "PIVOT (SUM(amount) FOR quarter IN ([Q1], [Q2])) p",
        "tsql",
    )
    one_column = _ir(
        "SELECT * FROM sales "
        "PIVOT (SUM(amount) FOR quarter IN ([Q1])) p",
        "tsql",
    )

    assert two_columns.pivot_details[0]["kind"] == "PIVOT"
    assert two_columns.pivot_details[0]["expressions"] == ["SUM(amount)"]
    assert two_columns.pivot_details[0]["fields"] == [
        {"expression": "quarter", "values": ['"Q1"', '"Q2"']}
    ]
    assert two_columns.to_dict() != one_column.to_dict()
    assert "pivot" in two_columns.feature_kps()


def test_oracle_table_sample_is_typed_and_rate_changes_ir():
    ten_percent = _ir(
        "SELECT id FROM users SAMPLE BLOCK (10) SEED (42)", "oracle"
    )
    twenty_percent = _ir(
        "SELECT id FROM users SAMPLE BLOCK (20) SEED (42)", "oracle"
    )

    assert ten_percent.table_samples == [
        {
            "table": "users",
            "alias": "users",
            "method": "BLOCK",
            "percent": "10",
            "size": "",
            "seed": "42",
            "bucket_numerator": "",
            "bucket_denominator": "",
            "bucket_field": "",
        }
    ]
    assert ten_percent.to_dict() != twenty_percent.to_dict()
    assert "table-sample" in ten_percent.feature_kps()


def test_postgres_from_only_is_typed_and_changes_ir():
    only_ir = _ir("SELECT id FROM ONLY users", "postgres")
    normal_ir = _ir("SELECT id FROM users", "postgres")

    assert only_ir.table_only == [{"table": "users", "alias": "users"}]
    assert normal_ir.table_only == []
    assert only_ir.to_dict() != normal_ir.to_dict()
    assert "table-only" in only_ir.feature_kps()
    json.dumps(only_ir.to_dict())


@pytest.mark.parametrize(
    ("dialect", "standard_sql", "student_sql", "clause", "diff_type", "kp"),
    [
        (
            "tsql",
            "SELECT TOP 10 PERCENT id FROM t ORDER BY id",
            "SELECT TOP 10 id FROM t ORDER BY id",
            "LIMIT",
            "limit_changed",
            "limit",
        ),
        (
            "standard",
            "SELECT id FROM t ORDER BY id FETCH FIRST 10 ROWS WITH TIES",
            "SELECT id FROM t ORDER BY id FETCH FIRST 10 ROWS ONLY",
            "LIMIT",
            "limit_changed",
            "limit",
        ),
        (
            "oracle",
            "SELECT employee_id FROM employees START WITH manager_id IS NULL "
            "CONNECT BY NOCYCLE PRIOR employee_id = manager_id",
            "SELECT employee_id FROM employees START WITH manager_id IS NULL "
            "CONNECT BY PRIOR employee_id = manager_id",
            "CONNECT BY",
            "hierarchical_query_changed",
            "hierarchical-query",
        ),
        (
            "tsql",
            "SELECT * FROM sales "
            "PIVOT (SUM(amount) FOR quarter IN ([Q1], [Q2])) p",
            "SELECT * FROM sales "
            "PIVOT (SUM(amount) FOR quarter IN ([Q1])) p",
            "PIVOT",
            "pivot_changed",
            "pivot",
        ),
        (
            "oracle",
            "SELECT id FROM users SAMPLE BLOCK (10) SEED (42)",
            "SELECT id FROM users SAMPLE BLOCK (20) SEED (42)",
            "TABLE SAMPLE",
            "table_sample_changed",
            "table-sample",
        ),
        (
            "postgres",
            "SELECT id FROM ONLY users",
            "SELECT id FROM users",
            "FROM ONLY",
            "table_only_changed",
            "table-only",
        ),
    ],
)
def test_teaching_dialect_ast_diff_is_nonempty_and_serializable(
    dialect, standard_sql, student_sql, clause, diff_type, kp
):
    matching = [
        diff
        for diff in extract_ast_diffs(standard_sql, student_sql, dialect=dialect)
        if (diff.clause_category, diff.diff_type, diff.knowledge_point_id)
        == (clause, diff_type, kp)
    ]

    assert len(matching) == 1
    payload = matching[0].to_dict()
    assert payload["standard_sql"]
    assert payload["standard_sql"] != payload["student_sql"]
    json.dumps(payload)
