"""Regression tests for Phase 1 AST-diff gate ordering.

These pairs intentionally share the same WHERE predicate shape so that the
supported boolean-absorption rewrite detector cannot hide a real top-level
set-operation or DISTINCT change.
"""

from core.parseval_data_generator import (
    _parse_sql,
    extract_ast_diffs,
    generate_and_compare,
)


def test_guarded_parser_accepts_lowercase_aggregate_function_roundtrip():
    parsed = _parse_sql(
        "select dept, count(*) from enrollment group by dept having count(*) > 2"
    )

    assert parsed is not None
    assert "COUNT(*)" in parsed.sql()


def test_guarded_parser_still_rejects_a_silently_dropped_table_token():
    assert _parse_sql("SELECT * FORM orders") is None


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
