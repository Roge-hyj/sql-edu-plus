"""Regression tests for Phase 1 AST-diff gate ordering.

These pairs intentionally share the same WHERE predicate shape so that the
supported boolean-absorption rewrite detector cannot hide a real top-level
set-operation or DISTINCT change.
"""

from core.parseval_data_generator import extract_ast_diffs, generate_and_compare


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
