"""Route-level acceptance tests for the Phase 4--6 teaching delivery path.

The tests intentionally exercise the existing ``/ai/check-sql`` function
boundary with the shared database fixtures.  Internal Phase 2 diagnostics and
causal target identities must remain server-side; the response exposes only
the applied support metadata and the final learner-safe text.
"""

from __future__ import annotations

import json
from hashlib import sha256

import pytest
from sqlalchemy import func, select

import core.parseval_data_generator as parseval
import core.student_feedback as student_feedback
import core.teaching_action as teaching_action
from models.chat import ChatMessage
from models.phase3_learning import (
    Phase3BehaviorEvent,
    Phase3BehaviorEventKind,
    SkillObservationEvent,
    StudentSkillState,
)
from models.submission import Submission
from models.submission_teaching_audit import SubmissionTeachingAudit
from routers.ai import SQLCheckRequest, check_sql
from core.llm_teaching import Phase2LLMAssessment, Phase5LLMFeedback


BOUNDARY_ATTEMPT_ID = "00000000-0000-4000-8000-000000000041"
CORRECT_ATTEMPT_ID = "00000000-0000-4000-8000-000000000042"
SYNTAX_ATTEMPT_ID = "00000000-0000-4000-8000-000000000043"
SAFETY_ATTEMPT_ID = "00000000-0000-4000-8000-000000000044"
REPLAY_ATTEMPT_ID = "00000000-0000-4000-8000-000000000045"
RENDER_FAILURE_ATTEMPT_ID = "00000000-0000-4000-8000-000000000049"
ESCALATION_ATTEMPT_IDS = (
    "00000000-0000-4000-8000-000000000046",
    "00000000-0000-4000-8000-000000000047",
    "00000000-0000-4000-8000-000000000048",
)

_TEACHING_SUPPORT_KEYS = {
    "schema_version",
    "status",
    "language",
    "recommended_support_level",
    "delivered_support_level",
    "support_recommendation_applied",
    "generation_source",
    "focused_error_count",
    "answer_revealed",
    "support_policy_version",
    "action_policy_version",
    "feedback_policy_version",
    "feedback_status",
}


@pytest.fixture(autouse=True)
def _use_sqlite_compatibility_backend(monkeypatch):
    monkeypatch.setattr("routers.ai.settings.PARSEVAL_EXECUTION_BACKEND", "sqlite")


async def _configure_boundary_question(session, question) -> None:
    question.content = "查询学分严格超过 3 的课程"
    question.correct_sql = "SELECT title FROM course WHERE credits > 3"
    question.schema_preview = (
        '{"tables":[{"name":"course","columns":['
        '{"name":"course_id","type":"INT","primary_key":true},'
        '{"name":"title","type":"TEXT"},'
        '{"name":"credits","type":"INT"}],'
        '"rows":[{"course_id":1,"title":"Boundary","credits":3},'
        '{"course_id":2,"title":"DB","credits":4}]}]}'
    )
    session.add(question)
    await session.flush()


async def _configure_correct_question(session, question) -> None:
    question.content = "查询年龄大于 18 的学生"
    question.correct_sql = "SELECT * FROM students WHERE age > 18"
    question.schema_preview = (
        '{"tables":[{"name":"students","columns":["id","age"],'
        '"rows":[{"id":1,"age":20}]}]}'
    )
    session.add(question)
    await session.flush()


def _assert_learner_safe_support_metadata(support: dict) -> None:
    assert set(support) == _TEACHING_SUPPORT_KEYS
    assert support["schema_version"] == "phase4.teaching_support.v1"
    assert support["generation_source"] == "LOCAL_TEMPLATE"
    assert support["action_policy_version"] == "phase4.action_selector.v1"
    assert support["feedback_policy_version"] == "phase5.safe_renderer.v1"
    assert support["answer_revealed"] is False
    serialized = json.dumps(support, ensure_ascii=False).lower()
    for forbidden in (
        "target_candidate",
        "target_rule",
        "target_skill",
        "target_observation",
        "taxonomy_version",
        "correct_sql",
        "answer_sql",
        "reference_sql",
    ):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_wrong_answer_persists_the_actual_delivered_level_everywhere(
    test_db_session,
    test_user,
    test_question,
) -> None:
    await _configure_boundary_question(test_db_session, test_question)

    response = await check_sql(
        payload=SQLCheckRequest(
            student_sql="SELECT title FROM course WHERE credits >= 3",
            question_id=test_question.id,
            attempt_id=BOUNDARY_ATTEMPT_ID,
        ),
        user_id=test_user.id,
        session=test_db_session,
    )

    assert response.is_correct is False
    assert response.judge_status == "WRONG"
    assert response.diagnostic_package is None
    assert response.observation is None
    assert response.error_attributions == []

    support = response.teaching_support
    assert support is not None
    _assert_learner_safe_support_metadata(support)
    assert support == {
        "schema_version": "phase4.teaching_support.v1",
        "status": "APPLIED",
        "language": "zh-CN",
        "recommended_support_level": 2,
        "delivered_support_level": 2,
        "support_recommendation_applied": True,
        "generation_source": "LOCAL_TEMPLATE",
        "focused_error_count": 1,
        "answer_revealed": False,
        "support_policy_version": "phase3.support_policy.v2",
        "action_policy_version": "phase4.action_selector.v1",
        "feedback_policy_version": "phase5.safe_renderer.v1",
        "feedback_status": "PRIMARY",
    }

    phase3 = response.phase3_learning
    assert phase3 is not None
    assert phase3["status"] == "UPDATED"
    assert phase3["recommended_support_level"] == 2
    assert phase3["delivered_support_level"] == 2
    assert phase3["support_recommendation_applied"] is True

    submission = await test_db_session.get(Submission, response.submission_id)
    audit = await test_db_session.get(
        SubmissionTeachingAudit,
        response.submission_id,
    )
    event = await test_db_session.scalar(select(SkillObservationEvent))
    assert submission is not None and audit is not None
    assert event is not None
    assert submission.is_correct is False
    assert submission.hint_level == 2
    assert event.assistance_level == audit.delivered_support_level == 2
    assert event.answer_revealed is False
    assert submission.ai_hint == response.hint["overall_comment"]
    assert submission.response_snapshot["teaching_support"] == support
    assert submission.response_snapshot["diagnostic_package"] is None
    assert audit.recommendation_status == "APPLIED"
    assert audit.feedback_status == "PRIMARY"
    assert audit.target_rule_id == "S2_BOUNDARY"
    assert audit.target_skill_id == "filter.boundary"
    assert audit.answer_revealed is False
    assert audit.feedback_sha256 == sha256(
        submission.ai_hint.encode("utf-8")
    ).hexdigest()


@pytest.mark.asyncio
async def test_correct_answer_returns_non_adaptive_teaching_support(
    test_db_session,
    test_user,
    test_question,
) -> None:
    await _configure_correct_question(test_db_session, test_question)

    response = await check_sql(
        payload=SQLCheckRequest(
            student_sql=test_question.correct_sql,
            question_id=test_question.id,
            attempt_id=CORRECT_ATTEMPT_ID,
            language="en",
        ),
        user_id=test_user.id,
        session=test_db_session,
    )

    assert response.is_correct is True
    assert response.judge_status == "CORRECT"
    assert response.diagnostic_package is None
    support = response.teaching_support
    assert support is not None
    _assert_learner_safe_support_metadata(support)
    assert support["status"] == "NOT_APPLICABLE"
    assert support["language"] == "en"
    assert support["recommended_support_level"] is None
    assert support["delivered_support_level"] == 1
    assert support["support_recommendation_applied"] is False
    assert support["focused_error_count"] == 0
    assert support["support_policy_version"] is None
    assert support["feedback_status"] == "BYPASS"

    submission = await test_db_session.get(Submission, response.submission_id)
    audit = await test_db_session.get(
        SubmissionTeachingAudit,
        response.submission_id,
    )
    assert submission is not None and audit is not None
    assert submission.hint_level == 1
    assert audit.recommendation_status == "NOT_APPLICABLE"
    assert audit.feedback_status == "BYPASS"
    assert audit.delivered_support_level == 1
    assert audit.target_rule_id is None
    assert audit.answer_revealed is False
    assert response.phase3_learning is not None
    assert response.phase3_learning["delivered_support_level"] == 1
    assert response.phase3_learning["support_recommendation_applied"] is False


@pytest.mark.asyncio
async def test_syntax_error_returns_baseline_teaching_support(
    test_db_session,
    test_user,
    test_question,
) -> None:
    response = await check_sql(
        payload=SQLCheckRequest(
            student_sql="SELECT * FROM (students",
            question_id=test_question.id,
            attempt_id=SYNTAX_ATTEMPT_ID,
            language="zh-TW",
        ),
        user_id=test_user.id,
        session=test_db_session,
    )

    assert response.is_correct is False
    assert response.is_safety_blocked is False
    assert response.judge_status == "WRONG"
    phase3 = response.phase3_learning
    assert phase3 is not None
    assert phase3["status"] == "SKIP_SYNTAX_ERROR"
    assert phase3["semantic_failure_count"] == 0
    assert phase3["syntax_error_count"] == 1
    assert response.diagnostic_package is None
    support = response.teaching_support
    assert support is not None
    _assert_learner_safe_support_metadata(support)
    assert support["status"] == "NOT_APPLICABLE"
    assert support["language"] == "zh-TW"
    assert support["recommended_support_level"] is None
    assert support["delivered_support_level"] == 1
    assert support["support_recommendation_applied"] is False
    assert support["focused_error_count"] == 0
    assert support["feedback_status"] == "BYPASS"

    submission = await test_db_session.get(Submission, response.submission_id)
    audit = await test_db_session.get(
        SubmissionTeachingAudit,
        response.submission_id,
    )
    assert submission is not None and audit is not None
    assert submission.hint_level == 1
    assert audit.recommendation_status == "NOT_APPLICABLE"
    assert audit.feedback_status == "BYPASS"
    assert audit.delivered_support_level == 1
    assert audit.answer_revealed is False
    assert await test_db_session.scalar(
        select(func.count()).select_from(SkillObservationEvent)
    ) == 0
    behavior_event = await test_db_session.scalar(
        select(Phase3BehaviorEvent).where(
            Phase3BehaviorEvent.submission_id == response.submission_id
        )
    )
    assert behavior_event is not None
    assert behavior_event.event_kind == Phase3BehaviorEventKind.SYNTAX_ERROR.value


@pytest.mark.asyncio
async def test_safety_block_returns_baseline_teaching_support_without_bkt(
    test_db_session,
    test_user,
    test_question,
) -> None:
    response = await check_sql(
        payload=SQLCheckRequest(
            student_sql="DROP TABLE students",
            question_id=test_question.id,
            attempt_id=SAFETY_ATTEMPT_ID,
            language="zh-CN",
        ),
        user_id=test_user.id,
        session=test_db_session,
    )

    assert response.is_correct is False
    assert response.is_safety_blocked is True
    assert response.judge_status == "WRONG"
    assert response.phase3_learning is None
    assert response.diagnostic_package is None
    support = response.teaching_support
    assert support is not None
    _assert_learner_safe_support_metadata(support)
    assert support["status"] == "NOT_APPLICABLE"
    assert support["recommended_support_level"] is None
    assert support["delivered_support_level"] == 1
    assert support["support_recommendation_applied"] is False
    assert support["focused_error_count"] == 0
    assert support["feedback_status"] == "BYPASS"

    submission = await test_db_session.get(Submission, response.submission_id)
    audit = await test_db_session.get(
        SubmissionTeachingAudit,
        response.submission_id,
    )
    assert submission is not None and audit is not None
    assert submission.hint_level == 1
    assert audit.recommendation_status == "NOT_APPLICABLE"
    assert audit.feedback_status == "BYPASS"
    assert audit.delivered_support_level == 1
    assert audit.answer_revealed is False
    assert await test_db_session.scalar(
        select(func.count()).select_from(StudentSkillState)
    ) == 0
    assert await test_db_session.scalar(
        select(func.count()).select_from(SkillObservationEvent)
    ) == 0


@pytest.mark.asyncio
async def test_committed_attempt_replay_restores_identical_support_without_regeneration(
    test_db_session,
    test_user,
    test_question,
    monkeypatch,
) -> None:
    await _configure_boundary_question(test_db_session, test_question)
    payload = SQLCheckRequest(
        student_sql="SELECT title FROM course WHERE credits >= 3",
        question_id=test_question.id,
        attempt_id=REPLAY_ATTEMPT_ID,
        language="en",
    )

    first = await check_sql(
        payload=payload,
        user_id=test_user.id,
        session=test_db_session,
    )
    counts_before = {
        "submissions": await test_db_session.scalar(
            select(func.count()).select_from(Submission)
        ),
        "messages": await test_db_session.scalar(
            select(func.count()).select_from(ChatMessage)
        ),
        "states": await test_db_session.scalar(
            select(func.count()).select_from(StudentSkillState)
        ),
        "events": await test_db_session.scalar(
            select(func.count()).select_from(SkillObservationEvent)
        ),
        "audits": await test_db_session.scalar(
            select(func.count()).select_from(SubmissionTeachingAudit)
        ),
    }

    def must_not_regenerate(*_args, **_kwargs):
        raise AssertionError("a committed attempt replay must not regenerate feedback")

    monkeypatch.setattr(parseval, "generate_and_compare", must_not_regenerate)
    monkeypatch.setattr(
        teaching_action,
        "select_teaching_actions",
        must_not_regenerate,
    )
    monkeypatch.setattr(
        student_feedback,
        "render_student_feedback",
        must_not_regenerate,
    )

    replay = await check_sql(
        payload=payload,
        user_id=test_user.id,
        session=test_db_session,
    )

    assert first.idempotency_replayed is False
    assert replay.idempotency_replayed is True
    assert replay.submission_id == first.submission_id
    assert replay.teaching_support == first.teaching_support
    assert replay.phase3_learning == first.phase3_learning
    assert replay.hint == first.hint
    assert replay.diagnostic_package is None
    replay_payload = replay.model_dump(mode="json")
    replay_payload["idempotency_replayed"] = False
    assert replay_payload == first.model_dump(mode="json")

    assert await test_db_session.scalar(
        select(func.count()).select_from(Submission)
    ) == counts_before["submissions"] == 1
    assert await test_db_session.scalar(
        select(func.count()).select_from(ChatMessage)
    ) == counts_before["messages"] == 3
    assert await test_db_session.scalar(
        select(func.count()).select_from(StudentSkillState)
    ) == counts_before["states"] == 1
    assert await test_db_session.scalar(
        select(func.count()).select_from(SkillObservationEvent)
    ) == counts_before["events"] == 1
    assert await test_db_session.scalar(
        select(func.count()).select_from(SubmissionTeachingAudit)
    ) == counts_before["audits"] == 1


@pytest.mark.asyncio
async def test_repeated_verified_failure_escalates_actual_support_l2_l3_l4(
    test_db_session,
    test_user,
    test_question,
) -> None:
    """The interpretable Phase 3 policy must reach every adaptive depth."""

    await _configure_boundary_question(test_db_session, test_question)
    responses = []
    for attempt_id in ESCALATION_ATTEMPT_IDS:
        responses.append(
            await check_sql(
                payload=SQLCheckRequest(
                    student_sql="SELECT title FROM course WHERE credits >= 3",
                    question_id=test_question.id,
                    attempt_id=attempt_id,
                ),
                user_id=test_user.id,
                session=test_db_session,
            )
        )

    assert [
        item.teaching_support["recommended_support_level"]
        for item in responses
    ] == [2, 3, 4]
    assert [
        item.teaching_support["delivered_support_level"]
        for item in responses
    ] == [2, 3, 4]
    assert all(
        item.teaching_support["support_recommendation_applied"]
        for item in responses
    )

    submissions = list(
        (
            await test_db_session.scalars(
                select(Submission).order_by(Submission.id)
            )
        ).all()
    )
    events = list(
        (
            await test_db_session.scalars(
                select(SkillObservationEvent).order_by(SkillObservationEvent.id)
            )
        ).all()
    )
    audits = list(
        (
            await test_db_session.scalars(
                select(SubmissionTeachingAudit).order_by(
                    SubmissionTeachingAudit.submission_id
                )
            )
        ).all()
    )
    assert [item.hint_level for item in submissions] == [2, 3, 4]
    assert [item.assistance_level for item in events] == [2, 3, 4]
    assert [item.delivered_support_level for item in audits] == [2, 3, 4]
    assert all(item.target_rule_id == "S2_BOUNDARY" for item in audits)


@pytest.mark.asyncio
async def test_phase5_failure_records_an_overridden_l1_fallback(
    test_db_session,
    test_user,
    test_question,
    monkeypatch,
) -> None:
    await _configure_boundary_question(test_db_session, test_question)

    def fail_primary_renderer(*_args, **_kwargs):
        raise RuntimeError("injected primary renderer failure")

    monkeypatch.setattr(
        student_feedback,
        "render_student_feedback",
        fail_primary_renderer,
    )
    response = await check_sql(
        payload=SQLCheckRequest(
            student_sql="SELECT title FROM course WHERE credits >= 3",
            question_id=test_question.id,
            attempt_id=RENDER_FAILURE_ATTEMPT_ID,
        ),
        user_id=test_user.id,
        session=test_db_session,
    )

    support = response.teaching_support
    assert support is not None
    assert support["status"] == "OVERRIDDEN"
    assert support["recommended_support_level"] == 2
    assert support["delivered_support_level"] == 1
    assert support["support_recommendation_applied"] is False
    assert support["feedback_status"] == "FALLBACK"
    assert support["focused_error_count"] == 0

    submission = await test_db_session.get(Submission, response.submission_id)
    audit = await test_db_session.get(
        SubmissionTeachingAudit,
        response.submission_id,
    )
    event = await test_db_session.scalar(select(SkillObservationEvent))
    assert submission is not None and audit is not None and event is not None
    assert submission.hint_level == event.assistance_level == (
        audit.delivered_support_level
    ) == 1
    assert audit.recommendation_status == "OVERRIDDEN"
    assert audit.feedback_status == "FALLBACK"
    assert audit.degradation_code == "PHASE45_DEGRADED_RENDER_OR_SAFETY"
    assert audit.target_rule_id == "S2_BOUNDARY"


@pytest.mark.asyncio
async def test_configured_phase5_llm_artifact_reaches_route_and_audit(
    test_db_session,
    test_user,
    test_question,
    monkeypatch,
) -> None:
    """The route uses the provider result only after Phase 4 selects actions."""

    await _configure_boundary_question(test_db_session, test_question)

    async def fake_phase5_llm(plan):
        return Phase5LLMFeedback(
            segments=tuple(
                (action.action_id, action.text)
                for action in plan.actions
            ),
            model="test-model",
        )

    monkeypatch.setattr(
        "core.llm_teaching.generate_phase5_feedback",
        fake_phase5_llm,
    )
    response = await check_sql(
        payload=SQLCheckRequest(
            student_sql="SELECT title FROM course WHERE credits >= 3",
            question_id=test_question.id,
            attempt_id="00000000-0000-4000-8000-000000000050",
        ),
        user_id=test_user.id,
        session=test_db_session,
    )

    assert response.teaching_support is not None
    assert response.teaching_support["generation_source"] == "LLM"
    audit = await test_db_session.get(
        SubmissionTeachingAudit,
        response.submission_id,
    )
    assert audit is not None
    assert audit.generation_source == "PHASE5_LLM"
    assert audit.feedback_sha256 == sha256(
        response.hint["overall_comment"].encode("utf-8")
    ).hexdigest()


@pytest.mark.asyncio
async def test_phase2_llm_review_is_private_and_does_not_override_phase1(
    test_db_session,
    test_user,
    test_question,
    monkeypatch,
) -> None:
    await _configure_boundary_question(test_db_session, test_question)
    calls = []

    async def fake_phase2_llm(**kwargs):
        calls.append(kwargs)
        package = kwargs["package"].to_dict()
        candidate_id = package["primary"]["candidate_id"]
        return Phase2LLMAssessment(
            decision="SUPPORTED_WRONG",
            authoritative_verdict="INCORRECT",
            primary_candidate_id=candidate_id,
            secondary_candidate_ids=(),
            evidence_ids=("private_evidence",),
            confidence=0.88,
            rationale="内部复核支持当前强候选。",
            uncertainty="仍受有界执行范围限制。",
            narrative=None,
            model="test-model",
        )

    monkeypatch.setattr(
        "core.llm_teaching.arbitrate_phase2_evidence",
        fake_phase2_llm,
    )
    response = await check_sql(
        payload=SQLCheckRequest(
            student_sql="SELECT title FROM course WHERE credits >= 3",
            question_id=test_question.id,
            attempt_id="00000000-0000-4000-8000-000000000051",
        ),
        user_id=test_user.id,
        session=test_db_session,
    )

    assert len(calls) == 1
    assert response.judge_status == "WRONG"
    assert response.teaching_support is not None
    public_text = response.hint["overall_comment"]
    assert "private_evidence" not in public_text
    assert response.diagnostic_package is None
    audit = await test_db_session.get(
        SubmissionTeachingAudit,
        response.submission_id,
    )
    assert audit is not None
    assert audit.action_snapshot["phase2_llm_review"]["decision"] == "SUPPORTED_WRONG"
    assert audit.action_snapshot["phase2_llm_review"]["primary_candidate_id"]
