from sqlglot import parse_one

from core.ast_schema import SQLStructureIR
from core.parseval_data_generator import extract_ast_diffs


def test_structure_ir_preserves_join_using_columns():
    ir = SQLStructureIR.from_ast(
        parse_one(
            "SELECT * FROM enrollment JOIN grades "
            "USING (student_id, course_id)",
            read="mysql",
        )
    )

    assert len(ir.joins) == 1
    join = ir.joins[0]
    assert join["condition"] == "USING (student_id, course_id)"
    assert join["using"] == ["student_id", "course_id"]
    assert "join-on" in ir.feature_kps()


def test_extract_ast_diffs_detects_changed_join_using_key():
    diffs = extract_ast_diffs(
        "SELECT a.id FROM a JOIN b USING (id)",
        "SELECT a.id FROM a JOIN b USING (account_id)",
    )

    join_diffs = [diff for diff in diffs if diff.diff_type == "join_on_changed"]
    assert join_diffs
    assert all(diff.clause_category == "JOIN ON" for diff in join_diffs)
    reported = {
        text
        for diff in join_diffs
        for text in (
            diff.extra.get("standard_sql", ""),
            diff.extra.get("student_sql", ""),
        )
        if text
    }
    assert reported == {"USING (id)", "USING (account_id)"}


def test_extract_ast_diffs_detects_removed_multi_column_using_key():
    diffs = extract_ast_diffs(
        "SELECT a.student_id FROM a JOIN b USING (student_id, course_id)",
        "SELECT a.student_id FROM a JOIN b USING (student_id)",
    )

    assert any(
        diff.diff_type == "join_on_changed"
        and diff.extra.get("standard_sql") == "USING (student_id, course_id)"
        for diff in diffs
    )


def test_extract_ast_diffs_does_not_flag_identical_join_using():
    sql = "SELECT a.id FROM a LEFT JOIN b USING (id)"

    assert extract_ast_diffs(sql, sql) == []


def test_join_using_and_join_on_remain_structurally_distinct_for_select_star():
    diffs = extract_ast_diffs(
        "SELECT * FROM a JOIN b USING (id)",
        "SELECT * FROM a JOIN b ON a.id = b.id",
    )

    # USING coalesces the join key in SELECT * while ON retains both input
    # columns, so folding these two forms together would hide an output-shape
    # difference.
    assert any(diff.diff_type == "join_on_changed" for diff in diffs)
