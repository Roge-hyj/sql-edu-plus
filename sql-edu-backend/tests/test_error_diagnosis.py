from core.error_attribution import evidence_weights_from_observation
from core.error_diagnosis import diagnose_record


def _record(question_l2, standard_sql, student_sql, error_message, question_l1="KP_FILTER"):
    attribution = evidence_weights_from_observation(
        student_sql=student_sql,
        answer_sql=standard_sql,
        is_correct=False,
        error_message=error_message,
    )
    return {
        "persona": "Test",
        "q_id": 1,
        "question_context": {
            "q": "test question",
            "l1": question_l1,
            "l2": question_l2,
        },
        "standard_sql": standard_sql,
        "student_sql": student_sql,
        "sandbox_status": "Incorrect",
        "phase1_observation": attribution.observation,
        "phase2_candidate_attributions": [item.to_dict() for item in attribution.attributions],
    }


def test_phase2_refines_where_like_to_l2_level():
    record = _record(
        ["PROJ_COL", "COMP_VAL", "LOGIC_NOT", "LIKE_STR"],
        "SELECT ContactName FROM Customers WHERE ContactTitle LIKE 'Sales%'",
        "SELECT ContactName FROM Customers WHERE ContactTitle NOT LIKE 'Sales%'",
        "结果行数不匹配：期望 5 行，实际 3 行。",
    )

    diagnosis = diagnose_record(record)

    assert diagnosis["diagnosis_status"] == "Incorrect"
    assert diagnosis["primary_diagnosis"]["l2_code"] == "LIKE_STR"
    assert diagnosis["primary_diagnosis"]["decision"] == "primary"


def test_phase2_keeps_correct_submission_empty():
    diagnosis = diagnose_record({
        "persona": "Test",
        "q_id": 1,
        "sandbox_status": "Correct",
        "phase2_candidate_attributions": [],
    })

    assert diagnosis["diagnosis_status"] == "Correct"
    assert diagnosis["primary_diagnosis"] is None
    assert diagnosis["final_attributions"] == []


def test_phase2_keeps_incorrect_without_candidates_incorrect():
    diagnosis = diagnose_record({
        "persona": "Test",
        "q_id": 1,
        "sandbox_status": "Incorrect",
        "phase2_candidate_attributions": [],
    })

    assert diagnosis["diagnosis_status"] == "Incorrect"
    assert diagnosis["primary_diagnosis"] is None
    assert diagnosis["undetermined_reason"]


def test_phase2_marks_having_written_as_where_as_confusion():
    record = _record(
        ["PROJ_COL", "AGG_BASIC", "GB_SIMPLE", "HV_SIMPLE"],
        "SELECT company_name FROM works GROUP BY company_name HAVING AVG(salary) > 70000",
        "SELECT company_name FROM works GROUP BY company_name WHERE AVG(salary) > 70000",
        "列结构不匹配。",
        question_l1="KP_AGG",
    )

    diagnosis = diagnose_record(record)

    assert diagnosis["primary_diagnosis"]["l2_code"] == "HV_SIMPLE"
    assert diagnosis["primary_diagnosis"]["error_type"] == "confusion"
    assert diagnosis["primary_diagnosis"]["misalignment_type"] == "Confusion"


def test_phase2_placeholder_attempt_prefers_core_high_order_kp():
    record = _record(
        ["PROJ_COL", "COMP_VAL", "CTE_RECURSIVE", "SET_UNION"],
        (
            "WITH RECURSIVE included_parts(part_id) AS ("
            "SELECT sub_part_id FROM part WHERE part_id = 1 "
            "UNION SELECT part.sub_part_id FROM part JOIN included_parts "
            "ON part.part_id = included_parts.part_id) "
            "SELECT DISTINCT part_id FROM included_parts"
        ),
        "SELECT part_id FROM part",
        "列结构不匹配。",
        question_l1="KP_ADVANCED",
    )

    diagnosis = diagnose_record(record)

    assert diagnosis["placeholder_attempt"] is True
    assert diagnosis["primary_diagnosis"]["l2_code"] == "CTE_RECURSIVE"
    assert diagnosis["primary_diagnosis"]["error_type"] == "abandoned_attempt"


def test_phase2_placeholder_attempt_can_use_question_l2_when_ast_misses_set_except():
    record = _record(
        ["PROJ_COL", "COMP_VAL", "LOGIC_NOT", "JOIN_INNER", "SUB_EXISTS", "SET_EXCEPT"],
        (
            "SELECT customer.ID FROM customer WHERE NOT EXISTS "
            "(SELECT branch_name FROM branch WHERE branch_city = 'Brooklyn' "
            "EXCEPT SELECT account.branch_name FROM account JOIN depositor "
            "USING (account_number) WHERE depositor.ID = customer.ID)"
        ),
        "SELECT branch_name FROM branch",
        "列结构不匹配。",
        question_l1="KP_ADVANCED",
    )

    diagnosis = diagnose_record(record)

    assert diagnosis["placeholder_attempt"] is True
    assert diagnosis["primary_diagnosis"]["l2_code"] == "SET_EXCEPT"
