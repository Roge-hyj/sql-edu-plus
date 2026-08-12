from core.error_attribution import evidence_weights_from_observation


def _kp_ids(result):
    return {item.knowledge_point_id for item in result.attributions}


def _l2_codes(result):
    return {item.l2_code for item in result.attributions}


def test_missing_group_by_attributed_to_kp_level():
    result = evidence_weights_from_observation(
        student_sql="SELECT dept_name, COUNT(*) FROM instructor",
        answer_sql="SELECT dept_name, COUNT(*) FROM instructor GROUP BY dept_name",
        is_correct=False,
        error_message="结果行数不匹配：期望 3 行，实际 1 行。",
    )

    assert "group-by" in _kp_ids(result)
    assert "GB_SIMPLE" in _l2_codes(result)
    assert result.observation["E_AST"]["student_features"]["has_group"] is False


def test_aggregate_distinct_is_not_reported_as_select_distinct():
    result = evidence_weights_from_observation(
        student_sql="SELECT COUNT(dept_id) FROM instructor",
        answer_sql="SELECT COUNT(DISTINCT dept_id) FROM instructor",
        is_correct=False,
    )

    assert result.observation["E_AST"]["standard_features"]["has_distinct"] is False
    assert result.observation["E_AST"]["student_features"]["has_distinct"] is False
    assert "distinct" not in _kp_ids(result)


def test_missing_join_on_attributed_to_join_on_l2():
    result = evidence_weights_from_observation(
        student_sql="SELECT s.name, t.course_id FROM student s JOIN takes t",
        answer_sql="SELECT s.name, t.course_id FROM student s JOIN takes t ON s.ID = t.ID",
        is_correct=False,
        error_message="结果行数不匹配：期望 4 行，实际 16 行。",
    )

    assert "join-on" in _kp_ids(result)
    assert "JOIN_ON" in _l2_codes(result)


def test_order_by_data_mismatch_attributed_to_order_kp():
    result = evidence_weights_from_observation(
        student_sql="SELECT name FROM student ORDER BY name ASC",
        answer_sql="SELECT name FROM student ORDER BY name DESC",
        is_correct=False,
        error_message="第 1 行与标准答案不一致（顺序或数据有误，如 ORDER BY 方向相反）。",
        judge_detail={"comparison": {"ordered": True}},
    )

    assert "order-by" in _kp_ids(result)
    assert "SORT_ASC" in _l2_codes(result)


def test_correct_submission_has_no_error_attributions():
    result = evidence_weights_from_observation(
        student_sql="SELECT name FROM student",
        answer_sql="SELECT name FROM student",
        is_correct=True,
        error_message="结果匹配。",
    )

    assert result.attributions == []
    assert result.llm_arbitration_input["candidate_kps"] == []


def test_equivalent_cte_inline_rewrite_is_not_a_blocking_attribution():
    result = evidence_weights_from_observation(
        student_sql="SELECT name FROM employee WHERE salary > 3",
        answer_sql=(
            "WITH e AS (SELECT name FROM employee WHERE salary > 3) "
            "SELECT name FROM e"
        ),
        is_correct=True,
        judge_detail={"comparison": {"is_equivalent_on_generated_data": True}},
    )

    assert result.attributions
    assert all(item.severity < 0.7 or item.error_type == "complication" for item in result.attributions)
    assert all(item.error_type == "complication" for item in result.attributions)


def test_equivalent_implicit_join_is_not_a_blocking_attribution():
    result = evidence_weights_from_observation(
        student_sql="SELECT s.name FROM student s, takes t WHERE s.id = t.id",
        answer_sql="SELECT s.name FROM student s JOIN takes t ON s.id = t.id",
        is_correct=True,
        judge_detail={"comparison": {"is_equivalent_on_generated_data": True}},
    )

    assert result.attributions
    assert all(item.severity < 0.7 or item.error_type == "complication" for item in result.attributions)
    assert all(item.error_type == "complication" for item in result.attributions)


def test_scalar_subquery_aggregate_is_not_illegal_outer_where_aggregate():
    result = evidence_weights_from_observation(
        student_sql=(
            "SELECT name FROM student WHERE credits > "
            "(SELECT AVG(credits) FROM student WHERE dept = 'CS')"
        ),
        answer_sql="SELECT name FROM student WHERE credits > (SELECT AVG(credits) FROM student)",
        is_correct=False,
    )

    observed = result.observation["E_AST"]["observed_kp"]
    assert observed["illegal_aggregate_locations"] == []
    assert not any(
        evidence.signal == "aggregate_illegal_location"
        for item in result.attributions
        for evidence in item.evidence
    )


def test_phase1_packages_intended_observed_and_misalignment():
    result = evidence_weights_from_observation(
        student_sql="SELECT dept_name, COUNT(*) FROM instructor WHERE COUNT(*) > 1 GROUP BY dept_name",
        answer_sql="SELECT dept_name, COUNT(*) FROM instructor GROUP BY dept_name HAVING COUNT(*) > 1",
        is_correct=False,
        error_message="结果数据不匹配（可能顺序不同或数据有误）。",
        question_context={"q": "每个系人数超过1人的系", "l1": "KP_AGG", "l2": ["GB_SIMPLE", "HV_SIMPLE", "AGG_BASIC"]},
    )

    intended = result.observation["E_AST"]["intended_kp"]
    observed = result.observation["E_AST"]["observed_kp"]
    assert intended["comparison_locations"]["HAVING"]
    assert observed["comparison_locations"]["WHERE"]
    assert observed["illegal_aggregate_locations"] == ["WHERE"]
    assert any(item["category"] == "Confusion" for item in result.llm_arbitration_input["misalignment_comparison"])


def test_redundant_distinct_is_not_ranked_above_blocking_join_and_group_by():
    answer_sql = """
    WITH active_dept AS (
      SELECT dept_id FROM department WHERE dept_id BETWEEN 1000 AND 1006
    )
    SELECT DISTINCT
      d.dept_name,
      CASE WHEN AVG(e.salary) > 3 THEN 'high' ELSE 'normal' END AS salary_band,
      COUNT(DISTINCT e.emp_id) AS emp_count
    FROM employee e
    JOIN department d ON e.dept_id = d.dept_id
    WHERE e.salary BETWEEN 3 AND 6
      AND d.dept_id IN (SELECT dept_id FROM active_dept)
    GROUP BY d.dept_name
    HAVING COUNT(DISTINCT e.emp_id) >= 1
    ORDER BY emp_count DESC, d.dept_name ASC
    LIMIT 3 OFFSET 0
    """
    student_sql = """
    WITH active_dept AS (
      SELECT dept_id FROM department WHERE dept_id > 1000
    )
    SELECT
      d.dept_name,
      CASE WHEN AVG(e.salary) >= 3 THEN 'high' ELSE 'normal' END AS salary_band,
      COUNT(e.emp_id) AS emp_count
    FROM employee e
    JOIN department d ON e.emp_id = d.dept_id
    WHERE e.salary > 3
      AND d.dept_id IN (SELECT dept_id FROM active_dept)
    GROUP BY d.building
    HAVING COUNT(e.emp_id) > 1
    ORDER BY emp_count ASC
    LIMIT 2 OFFSET 1
    """

    result = evidence_weights_from_observation(
        student_sql=student_sql,
        answer_sql=answer_sql,
        is_correct=False,
        judge_detail={
            "comparison": {
                "sandbox_executed": True,
                "is_equivalent_on_generated_data": False,
                "row_count_match": False,
                "standard_row_count": 3,
                "student_row_count": 0,
                "columns_match": True,
            }
        },
        mutation_detail={
            "summary": {"executed": 10, "fixed_by_replacement": 0},
            "tests": [
                {"clause": "JOIN ON", "knowledge_point_id": "join-on", "replacement_exec_ok": True, "replacement_equivalent": False},
                {"clause": "GROUP BY", "knowledge_point_id": "group-by", "replacement_exec_ok": True, "replacement_equivalent": False},
                {"clause": "DISTINCT", "knowledge_point_id": "distinct", "replacement_exec_ok": True, "replacement_equivalent": False},
            ],
        },
    )

    by_kp = {item.knowledge_point_id: item for item in result.attributions}
    assert by_kp["join-on"].severity >= 0.9
    assert by_kp["group-by"].severity >= 0.9
    assert by_kp["distinct"].severity <= 0.24
    assert result.attributions[0].knowledge_point_id in {"join-on", "group-by"}
    assert result.observation["E_AST"]["standard_features"]["outer_distinct_likely_redundant"] is True
    assert result.observation["E_AST"]["student_features"]["only_full_group_by_risk"] is True


def test_trailing_comment_equivalence_has_no_syntax_attribution():
    result = evidence_weights_from_observation(
        student_sql="SELECT name FROM student; -- trailing comment",
        answer_sql="SELECT name FROM student",
        is_correct=True,
    )

    assert result.attributions == []


def test_nullif_projection_change_maps_to_null_handling():
    result = evidence_weights_from_observation(
        student_sql="SELECT NULLIF(amount, 4) FROM sales",
        answer_sql="SELECT NULLIF(amount, 3) FROM sales",
        is_correct=False,
        ast_diffs=[
            {
                "clause": "SELECT",
                "diff_type": "projection_changed",
                "knowledge_point_id": "select-basic",
                "standard_sql": "NULLIF(amount, 3)",
                "student_sql": "NULLIF(amount, 4)",
            }
        ],
    )

    assert "null-handling" in _kp_ids(result)


def test_qualify_change_maps_to_window_attribution():
    result = evidence_weights_from_observation(
        student_sql=(
            "SELECT name, ROW_NUMBER() OVER (ORDER BY salary) AS rn "
            "FROM instructor QUALIFY rn <= 2"
        ),
        answer_sql=(
            "SELECT name, ROW_NUMBER() OVER (ORDER BY salary) AS rn "
            "FROM instructor QUALIFY rn = 1"
        ),
        is_correct=False,
        ast_diffs=[
            {
                "clause": "QUALIFY",
                "diff_type": "qualify_changed",
                "knowledge_point_id": "window-row-number",
                "standard_sql": "QUALIFY rn = 1",
                "student_sql": "QUALIFY rn <= 2",
            }
        ],
    )

    assert "window-row-number" in _kp_ids(result)
