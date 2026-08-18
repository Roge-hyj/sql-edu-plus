from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "data_construct_test" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from run_online_random250_structure_generation_tests import (  # noqa: E402
    _data_generation_status,
    _mutate_group_by,
    _mutate_join_comparison,
    _mutate_observable_set_operation,
    evaluate_case,
    infer_schema,
)


def test_online_audit_isolates_malformed_standard_input_before_generation():
    result = evaluate_case(
        {
            "id": "malformed_distinct_on",
            "standard": "SELECT DISTINCT ON ( expression",
            "student": "SELECT expression FROM events",
        },
        max_rows=8,
    )

    assert result["data_generation_status"] == "INPUT_GAP"
    assert result["verdict_status"] == "INPUT_GAP"
    assert result["equivalence_conclusion"] == "UNDECIDED"
    assert result["error_code"] == "STANDARD_SQL_PARSE_ERROR"
    assert result["executed"] is False


def test_online_audit_preserves_structured_phase1_boundaries():
    base = {"executed": False, "observable_mismatch": False, "generation_tactics": []}

    assert _data_generation_status({**base, "verdict_status": "ENGINE_GAP"}) == "ENGINE_GAP"
    assert _data_generation_status({**base, "verdict_status": "INPUT_GAP"}) == "INPUT_GAP"
    assert (
        _data_generation_status({**base, "verdict_status": "SEMANTIC_BOUNDARY"})
        == "SEMANTIC_BOUNDARY"
    )


def test_schema_inference_keeps_recursive_cte_output_columns_virtual():
    sql = (
        "WITH RECURSIVE search_tree(id, link, data, depth) AS ("
        "SELECT t.id, t.link, t.data, 0 FROM tree t UNION ALL "
        "SELECT t.id, t.link, t.data, depth + 1 "
        "FROM tree t, search_tree st WHERE t.id = st.link"
        ") SELECT * FROM search_tree ORDER BY depth"
    )

    assert infer_schema(sql) == "tree(id, data, link)"


def test_schema_inference_traces_numeric_cte_alias_to_physical_column():
    sql = (
        "WITH tb1 AS (SELECT seat_id, free AS free1 FROM cinema) "
        "SELECT seat_id FROM tb1 WHERE free1 = 1"
    )

    assert infer_schema(sql) == "cinema(seat_id, free NUMERIC)"


def test_online_recursive_cte_boundary_executes_with_scoped_inferred_schema():
    standard = (
        "WITH RECURSIVE search_tree(id, link, data, depth) AS ("
        "SELECT t.id, t.link, t.data, 0 FROM tree t UNION ALL "
        "SELECT t.id, t.link, t.data, depth + 1 "
        "FROM tree t, search_tree st WHERE t.id = st.link"
        ") SELECT * FROM search_tree ORDER BY depth"
    )
    result = evaluate_case(
        {
            "id": "recursive_depth_boundary",
            "standard": standard,
            "student": standard.replace("t.data, 0", "t.data, 1"),
        },
        max_rows=12,
    )

    assert result["schema"] == "tree(id, data, link)"
    assert result["executed"] is True
    assert result["observable_mismatch"] is True
    assert result["verdict_status"] == "SUPPORTED"


def test_join_mutation_replaces_complete_comparison_operator():
    standard = (
        "SELECT t1.visited_on FROM total t1 JOIN total t2 "
        "ON t1.visited_on >= t2.visited_on "
        "AND t1.visited_on <= DATEADD(day, 6, t2.visited_on)"
    )

    student = _mutate_join_comparison(standard)

    assert "><>" not in student
    assert "t1.visited_on > t2.visited_on" in student


def test_group_mutation_drops_key_before_functionally_dependent_label():
    standard = (
        "SELECT c.customer_id, c.customer_name, COUNT(*) FROM Customers c "
        "GROUP BY c.customer_id, c.customer_name"
    )

    student = _mutate_group_by(standard)

    assert "GROUP BY c.customer_name" in student
    assert "GROUP BY c.customer_id" not in student


def test_set_mutation_skips_bounded_monotonic_recursive_sequence():
    standard = (
        "WITH RECURSIVE numbers AS ("
        "SELECT 1 AS num UNION ALL "
        "SELECT num + 1 FROM numbers WHERE num < 6"
        ") SELECT num FROM numbers"
    )

    student, target = _mutate_observable_set_operation(standard)

    assert student == standard
    assert target == "no_mutation"


def test_set_mutation_skips_bounded_multiplicative_recursive_sequence():
    standard = (
        "WITH RECURSIVE sequence AS ("
        "SELECT 2 AS num UNION ALL "
        "SELECT num * 2 FROM sequence WHERE num < 64"
        ") SELECT num FROM sequence"
    )

    student, target = _mutate_observable_set_operation(standard)

    assert student == standard
    assert target == "no_mutation"


def test_set_mutation_skips_single_parent_recursive_hierarchy():
    standard = (
        "WITH RECURSIVE hierarchy AS ("
        "SELECT emp_id, manager_id FROM employees WHERE manager_id IS NULL "
        "UNION ALL "
        "SELECT e.emp_id, e.manager_id FROM employees e "
        "JOIN hierarchy h ON e.manager_id = h.emp_id"
        ") SELECT emp_id FROM hierarchy"
    )

    student, target = _mutate_observable_set_operation(standard)

    assert student == standard
    assert target == "no_mutation"
