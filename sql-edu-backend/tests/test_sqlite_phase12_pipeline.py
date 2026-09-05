from __future__ import annotations

import inspect
import json

import pytest

from core.parseval_data_generator import generate_and_compare
from core.pipeline import run_pipeline


SCHEMA = "students(id INTEGER PRIMARY KEY, score INTEGER)"
REFERENCE = "SELECT id FROM students WHERE score >= 60"
STUDENT = "SELECT id FROM students WHERE score > 60"


def _all_keys(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key).lower()
            yield from _all_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _all_keys(nested)


def test_public_phase1_entry_is_sqlite_only():
    parameters = inspect.signature(generate_and_compare).parameters

    assert set(parameters) == {
        "schema_text",
        "standard_sql",
        "student_sql",
        "max_rows_per_table",
        "schema_catalog",
    }
    assert "sql_dialect" not in parameters
    assert "execution_backend" not in parameters
    assert "native_executor_url" not in parameters


def test_boundary_error_runs_phase1_then_phase2_with_repair_verified_evidence():
    result = run_pipeline(
        schema_text=SCHEMA,
        reference_sql=REFERENCE,
        student_sql=STUDENT,
        question="找出所有及格学生",
    )

    assert result.phase1.executed is True
    assert result.phase1.status == "SUPPORTED"
    assert result.phase1.equivalence_conclusion == "NOT_EQUIVALENT"
    assert result.phase1.data_evidence["execution_backend"] == "sqlite"
    assert result.phase1.data_evidence["sql_dialect"] == "sqlite"
    assert result.phase1.mutation_evidence["summary"]["fixed_by_replacement"] >= 1

    assert result.phase2.verdict == "INCORRECT"
    assert result.phase2.diagnosis_status == "SUPPORTED"
    assert result.phase2.primary is not None
    assert result.phase2.primary.rule_id == "S2_BOUNDARY"
    assert result.phase2.primary.evidence_grade in {
        "REPAIR_VERIFIED",
        "CAUSAL_VERIFIED",
    }
    assert result.phase2.witness is not None
    assert result.phase2.witness["cases"]


def test_cross_join_topology_repair_restores_the_missing_on_dependency():
    result = run_pipeline(
        schema_text=(
            "instructor(id INTEGER, dept_name TEXT); "
            "department(dept_name TEXT, building TEXT)"
        ),
        reference_sql=(
            "SELECT i.id, d.building FROM instructor AS i "
            "JOIN department AS d ON i.dept_name = d.dept_name"
        ),
        student_sql=(
            "SELECT i.id, d.building FROM instructor AS i, department AS d"
        ),
    )

    assert result.phase1.equivalence_conclusion == "NOT_EQUIVALENT"
    assert result.phase1.mutation_evidence["summary"]["fixed_by_replacement"] >= 1
    assert result.phase2.primary is not None
    assert result.phase2.primary.rule_id == "S1_CARTESIAN_PRODUCT"
    assert result.phase2.primary.evidence_grade in {
        "REPAIR_VERIFIED",
        "CAUSAL_VERIFIED",
    }


def test_progressive_hints_disclose_one_primary_slot_at_a_time_without_answer_sql():
    result = run_pipeline(
        schema_text=SCHEMA,
        reference_sql=REFERENCE,
        student_sql=STUDENT,
    )
    narratives = result.phase2.narrative
    forbidden_keys = {
        "standard_sql",
        "answer_sql",
        "correct_sql",
        "replacement_sql",
        "mutation_sql",
        "test_database",
        "witness_world",
    }

    for level, narrative_key in enumerate(
        ("student_behavior", "conflict_and_witness", "guidance_question"),
        start=1,
    ):
        hint = result.learner_hint(level)
        encoded = json.dumps(hint, ensure_ascii=False)

        assert hint["hint"]["message"] == narratives[narrative_key]
        assert encoded.count(narratives[narrative_key]) == 1
        assert REFERENCE not in encoded
        assert forbidden_keys.isdisjoint(set(_all_keys(hint)))
        assert ("witness" in hint) is (level == 2)

        for other_key, other_message in narratives.items():
            if other_key != narrative_key:
                assert other_message not in encoded


def test_identical_queries_are_only_operationally_accepted_not_globally_proved():
    result = run_pipeline(
        schema_text=SCHEMA,
        reference_sql=REFERENCE,
        student_sql=REFERENCE,
    )

    assert result.phase1.executed is True
    assert result.phase1.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"
    assert result.phase2.verdict == "CORRECT"
    assert result.phase2.diagnosis_status == "OPERATIONALLY_ACCEPTED"
    assert result.phase2.primary is None
    assert "不代表已经证明全局等价" in result.learner_hint(3)["hint"]["message"]


def test_invalid_reference_query_fails_closed_without_diagnosing_the_student():
    result = run_pipeline(
        schema_text=SCHEMA,
        reference_sql=(
            "SELECT id FROM students; "
            "SELECT score FROM students"
        ),
        student_sql=STUDENT,
    )

    assert result.phase1.executed is False
    assert result.phase1.status == "INPUT_GAP"
    assert result.phase1.equivalence_conclusion == "UNDECIDED"
    assert result.phase2.verdict == "UNDECIDED"
    assert result.phase2.primary is None


@pytest.mark.parametrize("level", [0, 4, -1])
def test_hint_level_is_bounded(level):
    result = run_pipeline(
        schema_text=SCHEMA,
        reference_sql=REFERENCE,
        student_sql=STUDENT,
    )

    with pytest.raises(ValueError, match="1, 2, or 3"):
        result.learner_hint(level)
