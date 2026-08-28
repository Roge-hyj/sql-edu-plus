"""Regression tests for Phase 1 AST-diff gate ordering.

These pairs intentionally share the same WHERE predicate shape so that the
supported boolean-absorption rewrite detector cannot hide a real top-level
set-operation or DISTINCT change.
"""

import sys
from pathlib import Path

from sqlglot import exp, parse_one

from core.parseval_data_generator import (
    _parse_sql,
    extract_ast_diffs,
    generate_and_compare,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "data_construct_test" / "scripts"))
from run_phase1_cfg_convergence_benchmark import (  # noqa: E402
    _has_ancestor,
    _web_mutations,
)


def test_guarded_parser_accepts_lowercase_aggregate_function_roundtrip():
    parsed = _parse_sql(
        "select dept, count(*) from enrollment group by dept having count(*) > 2"
    )

    assert parsed is not None
    assert "COUNT(*)" in parsed.sql()


def test_guarded_parser_accepts_dialect_function_alias_roundtrip():
    parsed = _parse_sql(
        "SELECT SUBSTR(code, 1, 2) FROM catalog WHERE code NOT IN ('x')",
        dialect="mysql",
    )

    assert parsed is not None
    assert "SUBSTRING" in parsed.sql(dialect="mysql")


def test_guarded_parser_still_rejects_a_silently_dropped_table_token():
    assert _parse_sql("SELECT * FORM orders") is None


def test_web_order_mutation_does_not_target_window_order_by():
    window_only = "SELECT id, ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rn FROM t"
    assert not any(
        name.startswith("order_")
        for name, _sql, _labels in _web_mutations(
            window_only,
            declared_dialect="mysql",
        )
    )

    mixed = (
        "SELECT id, ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rn "
        "FROM t ORDER BY id DESC"
    )
    mutation = next(
        (item for item in _web_mutations(mixed, declared_dialect="mysql") if item[0].startswith("order_")),
        None,
    )
    assert mutation is not None
    parsed = parse_one(mutation[1], read="mysql")
    window_order = next(
        node
        for node in parsed.walk()
        if isinstance(node, exp.Ordered) and _has_ancestor(node, exp.Window)
    )
    assert window_order.args.get("desc") is True
    assert "ID ASC" in mutation[1].upper()


def test_not_in_to_in_keeps_atomic_diff_when_other_mysql_aliases_are_present():
    standard = (
        "SELECT DISTINCT SUBSTR(code, 1, 2) AS prefix, id "
        "FROM catalog WHERE id NOT IN ('x', 'y')"
    )
    student = standard.replace("SUBSTR", "SUBSTRING").replace("NOT IN", "IN")

    diffs = extract_ast_diffs(standard, student, dialect="mysql")

    assert any(diff.diff_type == "in_predicate_negation_changed" for diff in diffs)


def test_correlated_subquery_distinct_change_is_not_mislabeled_as_correlation_change():
    standard = (
        "SELECT o.id FROM outer_rows o WHERE o.id IN ("
        "SELECT DISTINCT i.id FROM inner_rows i WHERE i.outer_id = o.id)"
    )
    student = standard.replace("SELECT DISTINCT i.id", "SELECT i.id")

    diffs = extract_ast_diffs(standard, student, dialect="mysql")
    diff_types = {diff.diff_type for diff in diffs}

    assert "distinct_changed" in diff_types
    assert "correlated_predicate_changed" not in diff_types


def test_set_operator_change_is_not_suppressed_by_boolean_rewrite_gate():
    standard = (
        "SELECT title FROM course WHERE dept_name = 'CS' "
        "INTERSECT SELECT title FROM course WHERE credits > 3"
    )
    student = (
        "SELECT title FROM course WHERE dept_name = 'CS' "
        "UNION SELECT title FROM course WHERE credits > 3"
    )

    diffs = extract_ast_diffs(standard, student)

    assert any(diff.diff_type == "set_operator_changed" for diff in diffs)
    assert any(diff.knowledge_point_id == "intersect" for diff in diffs)


def test_removed_set_operator_produces_atomic_repair_evidence():
    result = generate_and_compare(
        "course(id, title, dept_name);",
        "SELECT title FROM course EXCEPT "
        "SELECT title FROM course WHERE dept_name = 'History'",
        "SELECT title FROM course",
        sql_dialect="sqlite",
    )

    assert result.executed is True
    assert result.is_equivalent is False
    assert result.mutation_evidence["summary"]["fixed_by_replacement"] == 1
    assert result.mutation_evidence["tests"][0]["action"] == "restore_removed_set_operator"


def test_union_right_branch_predicate_change_is_not_suppressed_as_equivalent():
    standard = (
        "SELECT project_id FROM project_staff WHERE project_id > 0 "
        "UNION SELECT project_id FROM project_staff WHERE role_code = 'leader'"
    )
    student = (
        "SELECT project_id FROM project_staff WHERE project_id > 0 "
        "UNION SELECT project_id FROM project_staff WHERE role_code <> 'leader'"
    )

    diffs = extract_ast_diffs(standard, student)

    assert diffs
    assert any(
        diff.diff_type in {"comparison_operator_changed", "where_changed"}
        for diff in diffs
    )


def test_scalar_subquery_boundary_does_not_emit_connector_function_diff():
    standard = (
        "SELECT t2.makeid FROM cars_data t1 "
        "JOIN car_names t2 ON t1.id = t2.makeid "
        "WHERE t1.horsepower > (SELECT MIN(horsepower) FROM cars_data) "
        "AND t1.cylinders < 4"
    )
    student = standard.replace("horsepower >", "horsepower >=")

    diff_types = {diff.diff_type for diff in extract_ast_diffs(standard, student)}

    assert "comparison_operator_changed" in diff_types
    assert "function_argument_changed" not in diff_types


def test_left_join_on_predicate_moved_to_where_is_one_atomic_diff():
    standard = (
        "SELECT A.id, B.status FROM A "
        "LEFT JOIN B ON A.id = B.id AND B.status = 1"
    )
    student = (
        "SELECT A.id, B.status FROM A "
        "LEFT JOIN B ON A.id = B.id WHERE B.status = 1"
    )

    diffs = extract_ast_diffs(standard, student)

    assert {diff.diff_type for diff in diffs} == {
        "join_predicate_placement_changed"
    }
    assert diffs[0].knowledge_point_id == "join-on"
    assert diffs[0].extra["movement"] == "ON_TO_WHERE"
    assert diffs[0].extra["moved_predicate_sql"].lower() == "b.status = 1"


def test_set_branch_aggregate_change_has_one_specific_diff_family():
    standard = (
        "SELECT name FROM scientists EXCEPT "
        "SELECT s.name FROM assignedto a "
        "JOIN projects p ON a.project = p.code "
        "JOIN scientists s ON a.scientist = s.ssn "
        "WHERE p.hours = (SELECT MAX(hours) FROM projects)"
    )
    student = standard.replace("MAX(hours)", "MIN(hours)")

    diffs = extract_ast_diffs(standard, student)
    diff_types = {diff.diff_type for diff in diffs}

    assert diff_types == {"aggregate_function_changed"}
    assert diffs[0].get("subquery_depth") == 1


def test_boolean_absorption_rewrite_can_be_recognized_inside_union_branch():
    standard = (
        "SELECT project_id FROM project_staff WHERE project_id > 0 "
        "UNION SELECT project_id FROM project_staff "
        "WHERE (role_code = 'leader' AND active = 1) OR active = 1"
    )
    student = (
        "SELECT project_id FROM project_staff WHERE project_id > 0 "
        "UNION SELECT project_id FROM project_staff WHERE active = 1"
    )

    assert extract_ast_diffs(standard, student) == []


def test_top_level_distinct_change_is_not_suppressed_by_boolean_rewrite_gate():
    standard = "SELECT DISTINCT dept_name FROM course WHERE credits > 3"
    student = "SELECT dept_name FROM course WHERE credits > 3"

    diffs = extract_ast_diffs(standard, student)

    assert any(diff.diff_type == "distinct_changed" for diff in diffs)
    assert any(diff.knowledge_point_id == "distinct" for diff in diffs)


def test_boolean_absorption_rewrite_still_has_no_structural_diff():
    standard = "SELECT * FROM course WHERE (credits > 3 AND dept_name = 'CS') OR dept_name = 'CS'"
    student = "SELECT * FROM course WHERE dept_name = 'CS'"

    assert extract_ast_diffs(standard, student) == []


def test_between_and_closed_comparison_expansion_are_supported_equivalent():
    standard = (
        "SELECT TrackId FROM Track "
        "WHERE UnitPrice BETWEEN 0.99 AND 1.99"
    )
    student = (
        "SELECT TrackId FROM Track "
        "WHERE UnitPrice >= 0.99 AND UnitPrice <= 1.99"
    )

    assert extract_ast_diffs(standard, student) == []


def test_between_is_not_equivalent_to_an_open_lower_bound():
    standard = (
        "SELECT TrackId FROM Track "
        "WHERE UnitPrice BETWEEN 0.99 AND 1.99"
    )
    student = (
        "SELECT TrackId FROM Track "
        "WHERE UnitPrice > 0.99 AND UnitPrice <= 1.99"
    )

    assert extract_ast_diffs(standard, student)


def test_global_max_equality_and_greater_equal_are_supported_equivalent():
    standard = (
        "SELECT InvoiceId FROM Invoice "
        "WHERE Total = (SELECT MAX(Total) FROM Invoice)"
    )
    student = standard.replace("Total =", "Total >=", 1)

    assert extract_ast_diffs(standard, student) == []


def test_global_min_equality_and_less_equal_are_supported_equivalent():
    standard = (
        "SELECT InvoiceId FROM Invoice "
        "WHERE Total = (SELECT MIN(Total) FROM Invoice)"
    )
    student = standard.replace("Total =", "Total <=", 1)

    assert extract_ast_diffs(standard, student) == []


def test_singleton_in_rewrite_is_supported_in_filter_context():
    standard = "SELECT county FROM places WHERE listed = '1992-06-29'"
    student = "SELECT county FROM places WHERE listed IN ('1992-06-29')"

    assert extract_ast_diffs(standard, student) == []


def test_singleton_in_rewrite_is_not_applied_to_projected_boolean_values():
    standard = "SELECT listed = '1992-06-29' FROM places"
    student = "SELECT listed IN ('1992-06-29') FROM places"

    assert extract_ast_diffs(standard, student)


def test_comparison_operand_mirror_is_equivalent_without_erasing_boolean_3vl():
    standard = "SELECT AVG(yards) FROM plays WHERE yards > 214"
    student = "SELECT AVG(yards) FROM plays WHERE 214 < yards"

    assert extract_ast_diffs(standard, student) == []

    projected_standard = "SELECT yards > 214 FROM plays"
    projected_student = "SELECT (yards > 214) IS TRUE FROM plays"
    assert extract_ast_diffs(projected_standard, projected_student)


def test_correlated_exists_negation_keeps_polarity_in_atomic_variant():
    result = generate_and_compare(
        "students(id, name); scores(student_id);",
        "SELECT s.name FROM students AS s WHERE EXISTS "
        "(SELECT 1 FROM scores AS x WHERE x.student_id = s.id)",
        "SELECT s.name FROM students AS s WHERE NOT EXISTS "
        "(SELECT 1 FROM scores AS x WHERE x.student_id = s.id)",
    )

    assert result.executed is True
    assert result.is_equivalent is False
    effectiveness = result.data_evidence["obligation_effectiveness"]
    assert effectiveness
    assert effectiveness[0]["constraints_satisfied"] is True
    assert effectiveness[0]["distinguished"] is True


def test_correlated_predicate_mutation_replaces_not_exists_wrapper_and_binds_diff():
    result = generate_and_compare(
        "students(id, name); scores(student_id);",
        "SELECT s.name FROM students AS s WHERE EXISTS "
        "(SELECT 1 FROM scores AS x WHERE x.student_id = s.id)",
        "SELECT s.name FROM students AS s WHERE NOT EXISTS "
        "(SELECT 1 FROM scores AS x WHERE x.student_id = s.id)",
    )

    tests = result.mutation_evidence["tests"]
    correlated = next(
        item for item in tests if item["action"] == "restore_correlated_predicate"
    )
    assert correlated["fixed_by_replacement"] is True
    assert correlated["binding_quality"] == "exact"
    assert correlated["diff_ids"]
    assert "NOT EXISTS" not in correlated["replacement_source_sql"].upper()


def test_filtered_max_subquery_is_not_treated_as_extreme_equivalence():
    standard = (
        "SELECT InvoiceId FROM Invoice "
        "WHERE Total = (SELECT MAX(Total) FROM Invoice WHERE BillingCountry = 'USA')"
    )
    student = standard.replace("Total =", "Total >=", 1)

    assert extract_ast_diffs(standard, student)


def test_distinct_probe_keeps_not_in_anti_match_rows_observable():
    standard = (
        "SELECT DISTINCT name FROM student "
        "WHERE id NOT IN (SELECT id FROM takes)"
    )
    student = "SELECT name FROM student WHERE id NOT IN (SELECT id FROM takes)"

    result = generate_and_compare(
        "student(id, name); takes(id, course_id);",
        standard,
        student,
    )

    assert result.executed is True
    assert result.is_equivalent is False
    assert result.standard_rows
    assert len(result.student_rows) > len(result.standard_rows)
    assert all(row["id"] is not None for row in result.test_database["takes"])


def test_keyless_distinct_world_keeps_duplicate_projection_to_final_execution():
    result = generate_and_compare(
        "products(product_name);",
        "SELECT DISTINCT product_name FROM products",
        "SELECT product_name FROM products",
    )

    values = [row["product_name"] for row in result.test_database["products"]]
    assert result.executed is True
    assert result.is_equivalent is False
    assert len(values) > len(set(values))
    validation = next(
        iter(result.data_evidence["witness_suite"]["worlds"][0]["execution"]["obligation_validations"].values())
    )
    assert validation["constraints_satisfied"] is True
