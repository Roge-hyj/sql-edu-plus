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
