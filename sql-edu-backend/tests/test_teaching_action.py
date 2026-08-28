"""Phase 4 teaching-action contract tests.

These tests stay below the HTTP route.  Phase 4 is expected to consume the
learner-safe Phase 2 package and the trusted target selected by Phase 3; it
must not rediscover a target or synthesize SQL.
"""

from __future__ import annotations

from copy import deepcopy
import json
import re
from types import SimpleNamespace

import pytest

from core.teaching_action import (
    TEACHING_ACTION_POLICY_VERSION,
    TEACHING_ACTION_SCHEMA_VERSION,
    TeachingActionKind,
    select_teaching_actions,
)


_SUPPORT_NEED = {1: 0.10, 2: 0.30, 3: 0.60, 4: 0.90}
_SQL_SHAPE = re.compile(
    r"\b(?:sql|select|from|where|join|group\s+by|having|order\s+by|limit|offset)\b",
    re.IGNORECASE,
)
_REFERENCE_SQL = "SELECT title FROM course WHERE credits > 3"

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


def _correct_package() -> dict:
    return {"verdict": "CORRECT"}


def _incorrect_package(language: str = "en") -> dict:
    return {
        "verdict": "INCORRECT",
        "primary": {
            "candidate_id": "candidate_primary",
            "rule_id": "S2_BOUNDARY",
            "logical_stage": "ROW_FILTER",
        },
        "secondary": [
            {
                "candidate_id": "candidate_selected_secondary",
                "rule_id": "S6_ORDER_OFFSET",
                "logical_stage": "ROOT_ORDER",
            },
            {
                "candidate_id": "candidate_unselected_secondary",
                "rule_id": "S1_CARTESIAN_PRODUCT",
                "logical_stage": "SOURCE_JOIN",
            },
        ],
        "narrative": dict(_NARRATIVES[language]),
    }


def _phase3_plan(
    level: int,
    *,
    candidate_id: str = "candidate_primary",
    rule_id: str = "S2_BOUNDARY",
    logical_stage: str = "ROW_FILTER",
    skill_id: str = "filter.boundary",
    source_role: str = "PRIMARY",
):
    return SimpleNamespace(
        selected=SimpleNamespace(phase2_candidate_id=candidate_id),
        selected_target=SimpleNamespace(
            observation_id=f"observation_{candidate_id}",
            phase2_candidate_id=candidate_id,
            phase2_rule_id=rule_id,
            logical_stage=logical_stage,
            skill_id=skill_id,
            taxonomy_version="sql_atomic_skills.v1",
            source_role=source_role,
            evidence_grade="CAUSAL_VERIFIED",
        ),
        support=SimpleNamespace(
            support_level=level,
            support_need=_SUPPORT_NEED[level],
        ),
    )


def _action_text(plan) -> str:
    return "\n".join(action.text for action in plan.actions)


def test_correct_submission_returns_non_adaptive_level_one_acceptance() -> None:
    plan = select_teaching_actions(
        _correct_package(),
        phase3_plan=None,
        expected_is_correct=True,
        language="en",
    )

    assert plan.status == "CORRECT_ACCEPTED"
    assert plan.verdict == "CORRECT"
    assert plan.recommended_support_level is None
    assert plan.delivered_support_level == 1
    assert plan.support_recommendation_applied is False
    assert plan.adaptive_target_selected is False
    assert plan.target_candidate_id is None
    assert [action.kind for action in plan.actions] == [
        TeachingActionKind.ACCEPTANCE
    ]
    assert plan.to_public_dict() == {
        "schema_version": TEACHING_ACTION_SCHEMA_VERSION,
        "policy_version": TEACHING_ACTION_POLICY_VERSION,
        "status": "CORRECT_ACCEPTED",
        "recommended_support_level": None,
        "delivered_support_level": 1,
        "support_recommendation_applied": False,
        "adaptive_target_selected": False,
        "action_count": 1,
    }


def test_incorrect_submission_without_trusted_target_falls_back_safely() -> None:
    plan = select_teaching_actions(
        _incorrect_package(),
        phase3_plan=None,
        expected_is_correct=False,
        language="en",
    )

    assert plan.status == "DIAGNOSTIC_FALLBACK"
    assert plan.recommended_support_level is None
    assert plan.delivered_support_level == 1
    assert plan.support_recommendation_applied is False
    assert plan.adaptive_target_selected is False
    assert plan.target_candidate_id is None
    assert [action.kind for action in plan.actions] == [
        TeachingActionKind.SOCRATIC_QUESTION,
    ]
    public = plan.to_public_dict()
    assert public["action_count"] == 1
    assert not any(key.startswith("target_") for key in public)
    assert "actions" not in public


@pytest.mark.parametrize(
    ("level", "expected_kinds"),
    [
        (1, (TeachingActionKind.SOCRATIC_QUESTION,)),
        (
            2,
            (
                TeachingActionKind.STUDENT_BEHAVIOR,
                TeachingActionKind.SOCRATIC_QUESTION,
            ),
        ),
        (
            3,
            (
                TeachingActionKind.STUDENT_BEHAVIOR,
                TeachingActionKind.CONFLICT_WITNESS,
                TeachingActionKind.SOCRATIC_QUESTION,
            ),
        ),
        (
            4,
            (
                TeachingActionKind.STUDENT_BEHAVIOR,
                TeachingActionKind.CONFLICT_WITNESS,
                TeachingActionKind.REPAIR_REFLECTION,
                TeachingActionKind.SOCRATIC_QUESTION,
            ),
        ),
    ],
)
def test_incorrect_l1_to_l4_apply_distinct_monotonic_action_sets(
    level: int,
    expected_kinds: tuple[TeachingActionKind, ...],
) -> None:
    plan = select_teaching_actions(
        _incorrect_package(),
        _phase3_plan(level),
        expected_is_correct=False,
        language="en",
    )

    assert plan.status == "ADAPTIVE_READY"
    assert plan.recommended_support_level == level
    assert plan.delivered_support_level == level
    assert plan.support_recommendation_applied is True
    assert plan.adaptive_target_selected is True
    assert tuple(action.kind for action in plan.actions) == expected_kinds
    assert [action.action_id for action in plan.actions] == [
        f"action_{index}" for index in range(1, level + 1)
    ]
    public = plan.to_public_dict()
    assert public == {
        "schema_version": TEACHING_ACTION_SCHEMA_VERSION,
        "policy_version": TEACHING_ACTION_POLICY_VERSION,
        "status": "ADAPTIVE_READY",
        "recommended_support_level": level,
        "delivered_support_level": level,
        "support_recommendation_applied": True,
        "adaptive_target_selected": True,
        "action_count": level,
    }
    learner_text = _action_text(plan)
    assert _REFERENCE_SQL.lower() not in learner_text.lower()
    assert "credits > 3" not in learner_text.lower()
    assert _SQL_SHAPE.search(learner_text) is None


def test_only_phase3_selected_target_can_drive_the_adaptive_action() -> None:
    package = _incorrect_package()
    package["narrative"]["student_behavior"] = "PRIMARY_ONLY_SENTINEL"
    package["narrative"]["conflict_and_witness"] = "PRIMARY_WITNESS_SENTINEL"
    plan = select_teaching_actions(
        package,
        _phase3_plan(
            4,
            candidate_id="candidate_selected_secondary",
            rule_id="S6_ORDER_OFFSET",
            logical_stage="ROOT_ORDER",
            skill_id="result.order_offset",
            source_role="SECONDARY",
        ),
        expected_is_correct=False,
        language="en",
    )

    assert plan.adaptive_target_selected is True
    assert plan.target_candidate_id == "candidate_selected_secondary"
    assert plan.target_rule_id == "S6_ORDER_OFFSET"
    assert plan.target_skill_id == "result.order_offset"
    assert plan.target_source_role == "SECONDARY"
    text = _action_text(plan)
    assert "PRIMARY_ONLY_SENTINEL" not in text
    assert "PRIMARY_WITNESS_SENTINEL" not in text
    assert "candidate_unselected_secondary" not in text
    assert "target rank" in text

    # Internal causal identities belong to the audit representation only.
    public_json = json.dumps(plan.to_public_dict(), sort_keys=True)
    assert "candidate_selected_secondary" not in public_json
    assert "result.order_offset" not in public_json
    assert "S6_ORDER_OFFSET" not in public_json


def test_selection_is_deterministic_and_secondary_order_independent() -> None:
    package = _incorrect_package()
    original = deepcopy(package)
    reordered = deepcopy(package)
    reordered["secondary"].reverse()
    phase3_plan = _phase3_plan(
        4,
        candidate_id="candidate_selected_secondary",
        rule_id="S6_ORDER_OFFSET",
        logical_stage="ROOT_ORDER",
        skill_id="result.order_offset",
        source_role="SECONDARY",
    )

    first = select_teaching_actions(
        package,
        phase3_plan,
        expected_is_correct=False,
        language="en",
    )
    repeat = select_teaching_actions(
        package,
        phase3_plan,
        expected_is_correct=False,
        language="en",
    )
    permuted = select_teaching_actions(
        reordered,
        phase3_plan,
        expected_is_correct=False,
        language="en",
    )

    assert first == repeat == permuted
    assert first.to_audit_dict() == repeat.to_audit_dict()
    assert package == original


@pytest.mark.parametrize(
    ("language", "localized_marker"),
    [
        ("zh-CN", "把题目中的严格"),
        ("zh-TW", "把題目中的嚴格"),
        ("en", "Translate words such as strictly"),
    ],
)
def test_all_supported_languages_use_localized_level_four_actions(
    language: str,
    localized_marker: str,
) -> None:
    plan = select_teaching_actions(
        _incorrect_package(language),
        _phase3_plan(4),
        expected_is_correct=False,
        language=language,
    )

    assert plan.language == language
    assert plan.delivered_support_level == 4
    assert localized_marker in _action_text(plan)
    assert _SQL_SHAPE.search(_action_text(plan)) is None
