"""Phase 5 deterministic learner-feedback contract tests."""

from __future__ import annotations

from hashlib import sha256
import re
from types import SimpleNamespace

import pytest

from core.student_feedback import (
    DETERMINISTIC_RENDERER,
    PUBLIC_GENERATION_SOURCE,
    STUDENT_FEEDBACK_POLICY_VERSION,
    STUDENT_FEEDBACK_SCHEMA_VERSION,
    build_teaching_support_summary,
    render_student_feedback,
)
from core.teaching_action import (
    TEACHING_ACTION_POLICY_VERSION,
    TEACHING_SUPPORT_SCHEMA_VERSION,
    select_teaching_actions,
)


_SUPPORT_NEED = {1: 0.10, 2: 0.30, 3: 0.60, 4: 0.90}
_REFERENCE_SQL = "SELECT title FROM course WHERE credits > 3"
_SQL_SHAPE = re.compile(
    r"\b(?:sql|select|from|where|join|group\s+by|having|order\s+by|limit|offset)\b",
    re.IGNORECASE,
)
_NARRATIVES = {
    "zh-CN": {
        "student_behavior": "当前比较会保留临界记录。",
        "conflict_and_witness": "可信物证表明，该临界记录不属于题目要求的结果。",
        "guidance_question": "这个临界记录应该被包含吗？",
    },
    "zh-TW": {
        "student_behavior": "目前比較會保留臨界記錄。",
        "conflict_and_witness": "可信物證表明，該臨界記錄不屬於題目要求的結果。",
        "guidance_question": "這個臨界記錄應該被包含嗎？",
    },
    "en": {
        "student_behavior": "The current comparison retains the boundary record.",
        "conflict_and_witness": "Trusted evidence shows that the boundary record is outside the requested result.",
        "guidance_question": "Should that boundary record be included?",
    },
}


def _package(language: str = "en", *, correct: bool = False) -> dict:
    if correct:
        return {"verdict": "CORRECT"}
    return {
        "verdict": "INCORRECT",
        "primary": {
            "candidate_id": "candidate_primary",
            "rule_id": "S2_BOUNDARY",
            "logical_stage": "ROW_FILTER",
        },
        "secondary": [],
        "narrative": dict(_NARRATIVES[language]),
    }


def _phase3_plan(level: int):
    return SimpleNamespace(
        selected=SimpleNamespace(phase2_candidate_id="candidate_primary"),
        selected_target=SimpleNamespace(
            observation_id="observation_primary",
            phase2_candidate_id="candidate_primary",
            phase2_rule_id="S2_BOUNDARY",
            logical_stage="ROW_FILTER",
            skill_id="filter.boundary",
            taxonomy_version="sql_atomic_skills.v1",
            source_role="PRIMARY",
            evidence_grade="CAUSAL_VERIFIED",
        ),
        support=SimpleNamespace(
            support_level=level,
            support_need=_SUPPORT_NEED[level],
        ),
    )


def _feedback(level: int, language: str = "en"):
    plan = select_teaching_actions(
        _package(language),
        _phase3_plan(level),
        expected_is_correct=False,
        language=language,
    )
    return plan, render_student_feedback(plan)


def test_correct_submission_renders_plain_acceptance_and_public_metadata() -> None:
    plan = select_teaching_actions(
        _package(correct=True),
        phase3_plan=None,
        expected_is_correct=True,
        language="en",
    )
    artifact = render_student_feedback(plan)

    assert artifact.text == plan.actions[0].text
    assert artifact.status == "RENDERED"
    assert artifact.renderer == DETERMINISTIC_RENDERER
    assert artifact.language == "en"
    assert artifact.delivered_support_level == 1
    assert artifact.segment_count == 1
    assert artifact.content_digest == sha256(artifact.text.encode("utf-8")).hexdigest()
    assert artifact.to_audit_dict() == {
        "schema_version": STUDENT_FEEDBACK_SCHEMA_VERSION,
        "policy_version": STUDENT_FEEDBACK_POLICY_VERSION,
        "status": "RENDERED",
        "renderer": DETERMINISTIC_RENDERER,
        "feedback_source": "PHASE5_LOCAL_TEMPLATE",
        "feedback_status": "BYPASS",
        "degradation_code": None,
        "answer_revealed": False,
        "language": "en",
        "delivered_support_level": 1,
        "segment_count": 1,
        "content_digest": artifact.content_digest,
        "content_bytes": len(artifact.text.encode("utf-8")),
    }
    assert artifact.to_public_dict() == {
        "schema_version": STUDENT_FEEDBACK_SCHEMA_VERSION,
        "policy_version": STUDENT_FEEDBACK_POLICY_VERSION,
        "generation_source": PUBLIC_GENERATION_SOURCE,
        "feedback_status": "BYPASS",
        "delivered_support_level": 1,
    }
    assert "text" not in artifact.to_public_dict()


@pytest.mark.parametrize(
    ("level", "expected_labels"),
    [
        (1, ("Question to consider",)),
        (2, ("What your query currently does", "Question to consider")),
        (
            3,
            (
                "What your query currently does",
                "Conflict and witness",
                "Question to consider",
            ),
        ),
        (
            4,
            (
                "What your query currently does",
                "Conflict and witness",
                "A check before revising",
                "Question to consider",
            ),
        ),
    ],
)
def test_l1_to_l4_render_only_the_approved_segments(
    level: int,
    expected_labels: tuple[str, ...],
) -> None:
    plan, artifact = _feedback(level)

    assert artifact.delivered_support_level == level
    assert artifact.segment_count == level
    for action in plan.actions:
        assert artifact.text.count(action.text) == 1
    for label in expected_labels:
        assert label in artifact.text
    assert _REFERENCE_SQL.lower() not in artifact.text.lower()
    assert "credits > 3" not in artifact.text.lower()
    assert _SQL_SHAPE.search(artifact.text) is None


def test_renderer_is_byte_deterministic_for_the_same_approved_plan() -> None:
    plan, first = _feedback(4)
    second = render_student_feedback(plan)

    assert first == second
    assert first.text.encode("utf-8") == second.text.encode("utf-8")
    assert first.content_digest == second.content_digest
    assert first.to_public_dict() == second.to_public_dict()


@pytest.mark.parametrize(
    ("language", "labels"),
    [
        (
            "zh-CN",
            ("你当前的查询行为", "冲突与物证", "修改前的检查方向", "请思考"),
        ),
        (
            "zh-TW",
            ("你目前的查詢行為", "衝突與物證", "修改前的檢查方向", "請思考"),
        ),
        (
            "en",
            (
                "What your query currently does",
                "Conflict and witness",
                "A check before revising",
                "Question to consider",
            ),
        ),
    ],
)
def test_renderer_supports_all_three_languages_without_adding_sql_shape(
    language: str,
    labels: tuple[str, ...],
) -> None:
    plan, artifact = _feedback(4, language)

    assert artifact.language == language
    assert artifact.delivered_support_level == 4
    assert artifact.segment_count == 4
    for label in labels:
        assert label in artifact.text
    for action in plan.actions:
        assert action.text in artifact.text
    assert _REFERENCE_SQL.lower() not in artifact.text.lower()
    assert "credits > 3" not in artifact.text.lower()
    assert _SQL_SHAPE.search(artifact.text) is None


def test_public_metadata_contains_no_feedback_text_or_causal_target_identity() -> None:
    plan, artifact = _feedback(4)
    public = artifact.to_public_dict()

    assert set(public) == {
        "schema_version",
        "policy_version",
        "generation_source",
        "feedback_status",
        "delivered_support_level",
    }
    serialized = repr(public)
    assert plan.target_candidate_id not in serialized
    assert plan.target_skill_id not in serialized
    assert plan.target_rule_id not in serialized
    assert artifact.text not in serialized


def test_combined_teaching_support_metadata_is_complete_and_learner_safe() -> None:
    plan, artifact = _feedback(4, "zh-TW")
    public = build_teaching_support_summary(plan, artifact)

    assert public == {
        "schema_version": TEACHING_SUPPORT_SCHEMA_VERSION,
        "status": "APPLIED",
        "language": "zh-TW",
        "recommended_support_level": 4,
        "delivered_support_level": 4,
        "support_recommendation_applied": True,
        "generation_source": PUBLIC_GENERATION_SOURCE,
        "focused_error_count": 1,
        "answer_revealed": False,
        "support_policy_version": "phase3.support_policy.v2",
        "action_policy_version": TEACHING_ACTION_POLICY_VERSION,
        "feedback_policy_version": STUDENT_FEEDBACK_POLICY_VERSION,
        "feedback_status": "PRIMARY",
    }
    serialized = repr(public)
    assert artifact.text not in serialized
    assert artifact.content_digest not in serialized
    assert plan.target_candidate_id not in serialized
    assert plan.target_skill_id not in serialized
    assert plan.target_rule_id not in serialized
