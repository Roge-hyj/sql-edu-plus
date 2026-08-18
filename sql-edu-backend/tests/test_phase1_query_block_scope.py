"""Phase 1 regressions for query-block-aware structure and mutation evidence."""

from __future__ import annotations

from sqlglot import exp, parse_one

from core.parseval_data_generator import extract_ast_diffs, generate_and_compare


def _fixed_tests(run, clause: str) -> list[dict]:
    return [
        test
        for test in run.mutation_evidence.get("tests", [])
        if test.get("clause") == clause and test.get("fixed_by_replacement")
    ]


def test_nested_order_does_not_order_the_outer_result_or_create_false_mutations():
    standard = "SELECT id FROM (SELECT id FROM t ORDER BY id DESC) s"
    student = "SELECT id FROM t"

    run = generate_and_compare("t(id, value);", standard, student)
    diffs = extract_ast_diffs(standard, student)

    assert run.executed is True
    assert run.is_equivalent is True
    assert run.data_evidence["ordered_compare"] is False
    assert not any(diff.clause_category == "ORDER BY" for diff in diffs)
    assert not any(
        test.get("clause") in {"ORDER BY", "JOIN STRUCTURE"}
        for test in run.mutation_evidence.get("tests", [])
    )


def test_nested_where_mutation_stays_in_the_derived_query_block():
    standard = "SELECT id FROM (SELECT id FROM t WHERE score > 5) s"
    student = "SELECT id FROM (SELECT id FROM t) s"

    run = generate_and_compare("t(id, score);", standard, student)

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.standard_rows != run.student_rows
    fixed = _fixed_tests(run, "WHERE")
    assert len(fixed) == 1
    assert fixed[0]["query_scope"] != "root"

    mutated = parse_one(fixed[0]["replacement_sql"], read="sqlite")
    assert isinstance(mutated, exp.Select)
    assert mutated.args.get("where") is None
    derived = next(mutated.find_all(exp.Subquery)).this
    assert isinstance(derived, exp.Select)
    assert isinstance(derived.args.get("where"), exp.Where)
    assert not any(
        test.get("clause") == "JOIN STRUCTURE"
        for test in run.mutation_evidence.get("tests", [])
    )


def test_nested_limit_structure_and_mutation_are_reported_only_at_nested_depth():
    standard = "SELECT id FROM (SELECT id FROM t LIMIT 2) s"
    student = "SELECT id FROM (SELECT id FROM t LIMIT 3) s"

    diffs = extract_ast_diffs(standard, student)
    run = generate_and_compare("t(id, score);", standard, student)
    limit_diffs = [diff for diff in diffs if diff.clause_category == "LIMIT"]

    assert limit_diffs
    assert all(diff.extra.get("subquery_depth") == 1 for diff in limit_diffs)
    assert run.executed is True
    assert run.is_equivalent is False
    assert run.data_evidence["ordered_compare"] is False
    fixed = _fixed_tests(run, "LIMIT")
    assert len(fixed) == 1
    assert fixed[0]["query_scope"] != "root"
    assert not any(
        test.get("clause") == "JOIN STRUCTURE"
        for test in run.mutation_evidence.get("tests", [])
    )


def test_nested_order_with_limit_can_change_the_bag_without_ordering_outer_rows():
    standard = (
        "SELECT id FROM (SELECT id FROM t ORDER BY id DESC LIMIT 2) s"
    )
    student = (
        "SELECT id FROM (SELECT id FROM t ORDER BY id ASC LIMIT 2) s"
    )

    diffs = extract_ast_diffs(standard, student)
    run = generate_and_compare("t(id, score);", standard, student)
    order_diffs = [diff for diff in diffs if diff.clause_category == "ORDER BY"]

    assert order_diffs
    assert all(diff.extra.get("subquery_depth") == 1 for diff in order_diffs)
    assert run.executed is True
    assert run.is_equivalent is False
    assert run.data_evidence["ordered_compare"] is False
    fixed = _fixed_tests(run, "ORDER BY")
    assert len(fixed) == 1
    assert fixed[0]["query_scope"] != "root"


def test_root_set_order_remains_observable_and_mutatable():
    standard = (
        "SELECT id FROM t UNION ALL SELECT id FROM u ORDER BY id DESC"
    )
    student = (
        "SELECT id FROM t UNION ALL SELECT id FROM u ORDER BY id ASC"
    )

    run = generate_and_compare("t(id); u(id);", standard, student)

    assert run.executed is True
    assert run.is_equivalent is False
    assert run.data_evidence["ordered_compare"] is True
    fixed = _fixed_tests(run, "ORDER BY")
    assert len(fixed) == 1
    assert fixed[0]["query_scope"] == "root"


def test_cte_and_nested_having_mutations_preserve_their_query_blocks():
    cases = (
        (
            "t(id, score);",
            "WITH s AS (SELECT id FROM t WHERE score > 5) SELECT id FROM s",
            "WITH s AS (SELECT id FROM t) SELECT id FROM s",
            "WHERE",
        ),
        (
            "t(dept, score);",
            "SELECT dept FROM ("
            "SELECT dept FROM t GROUP BY dept HAVING COUNT(*) > 1"
            ") s",
            "SELECT dept FROM (SELECT dept FROM t GROUP BY dept) s",
            "HAVING",
        ),
    )

    for schema, standard, student, clause in cases:
        run = generate_and_compare(schema, standard, student)
        assert run.executed is True
        assert run.is_equivalent is False
        fixed = _fixed_tests(run, clause)
        assert fixed
        assert all(test["query_scope"] != "root" for test in fixed)


def test_nested_projection_and_distinct_have_single_block_repairs():
    cases = (
        (
            "t(id, name);",
            "SELECT id FROM (SELECT id FROM t) s",
            "SELECT id FROM (SELECT name AS id FROM t) s",
            "SELECT",
            "select-basic",
        ),
        (
            "t(id, score);",
            "SELECT id FROM (SELECT DISTINCT score AS id FROM t) s",
            "SELECT id FROM (SELECT score AS id FROM t) s",
            "DISTINCT",
            "distinct",
        ),
    )

    for schema, standard, student, clause, knowledge_point_id in cases:
        run = generate_and_compare(schema, standard, student)
        assert run.executed is True
        assert run.is_equivalent is False
        fixed = _fixed_tests(run, clause)
        assert len(fixed) == 1
        assert fixed[0]["knowledge_point_id"] == knowledge_point_id
        assert fixed[0]["query_scope"] != "root"
        assert fixed[0]["mutation_scope"] == [clause]


def test_cte_distinct_change_emits_scoped_atomic_diff():
    standard = (
        "WITH tb1 AS (SELECT DISTINCT customer_id, product_name FROM orders) "
        "SELECT customer_id FROM tb1"
    )
    student = standard.replace("SELECT DISTINCT", "SELECT", 1)

    diffs = extract_ast_diffs(standard, student)
    distinct = [diff for diff in diffs if diff.diff_type == "distinct_changed"]

    assert len(distinct) == 1
    assert distinct[0].target_table.lower() == "orders"
    assert distinct[0].extra["query_scope"] == "cte:tb1"
    assert distinct[0].extra["query_block_depth"] == 1


def test_missing_cte_definition_and_reference_are_one_cte_repair():
    run = generate_and_compare(
        "employee(id, name, salary);",
        (
            "WITH high_salary AS ("
            "SELECT * FROM employee WHERE salary > 50000"
            ") SELECT name FROM high_salary"
        ),
        "SELECT name FROM employee",
    )

    assert run.executed is True
    assert run.is_equivalent is False
    fixed = _fixed_tests(run, "CTE")
    assert len(fixed) == 1
    assert fixed[0]["knowledge_point_id"] == "cte"
    assert fixed[0]["mutation_scope"] == ["CTE"]
    assert fixed[0]["dependent_changes"] == ["FROM"]


def test_distinct_on_order_probe_creates_competing_rows_per_key():
    run = generate_and_compare(
        "orders(customer_id BIGINT, amount BIGINT);",
        (
            "SELECT DISTINCT ON (customer_id) customer_id, amount "
            "FROM orders ORDER BY customer_id, amount DESC"
        ),
        (
            "SELECT DISTINCT ON (customer_id) customer_id, amount "
            "FROM orders ORDER BY customer_id, amount ASC"
        ),
        sql_dialect="postgres",
        execution_backend="sqlite",
    )

    rows = run.test_database["orders"]
    assert run.executed is True
    assert run.is_equivalent is False
    assert rows[0]["customer_id"] == rows[1]["customer_id"]
    assert rows[0]["amount"] != rows[1]["amount"]
    assert run.standard_rows != run.student_rows
    fixed = _fixed_tests(run, "ORDER BY")
    assert len(fixed) == 1
    assert fixed[0]["knowledge_point_id"] == "order-by"
