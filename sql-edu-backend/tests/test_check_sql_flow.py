import asyncio
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

import core.parseval_data_generator as parseval
import routers.ai as ai_router
from models.submission import Submission
from models.chat import ChatMessage
from core.phase3_skill_catalog import ATOMIC_SKILL_TAXONOMY_VERSION
from models.phase3_learning import SkillObservationEvent, StudentSkillState
from models.submission_teaching_audit import SubmissionTeachingAudit
from models.question_skill import QuestionSkillProvenance
from repository.question_skill_repo import (
    QuestionSkillRepository,
    QuestionSkillSpec,
)
from repository.phase3_learning_repo import Phase3LearningRepository
from routers.ai import check_sql, get_mastery_radar, SQLCheckRequest
from schemas.agent import SQLCheckResultSchema


TEST_ATTEMPT_ID = "00000000-0000-4000-8000-000000000001"


def _sleeping_phase1_worker(**_kwargs):
    time.sleep(2)
    return "late"


def _crashing_phase1_worker(**_kwargs):
    os._exit(91)


def _successful_phase1_worker(**_kwargs):
    return "recreated"


def _busy_phase1_worker(**_kwargs):
    while True:
        pass


def _memory_hungry_phase1_worker(**_kwargs):
    # The parent sets RLIMIT_AS before invoking this function.  The allocation
    # should therefore fail inside the child without threatening the API host.
    return bytearray(512 * 1024 * 1024)


def _spawning_phase1_worker(*, pid_path: str):
    """Spawn a descendant so timeout cleanup can be tested at process-group level."""
    child = subprocess.Popen(["sleep", "30"])
    Path(pid_path).write_text(str(child.pid), encoding="ascii")
    time.sleep(2)


def test_check_sql_request_rejects_unbounded_sql_payload():
    with pytest.raises(ValidationError):
        SQLCheckRequest(
            student_sql="x" * 32769,
            question_id=1,
            attempt_id=TEST_ATTEMPT_ID,
        )


@pytest.mark.asyncio
async def test_phase1_process_timeout_kills_child_worker():
    with pytest.raises(TimeoutError, match="exceeded"):
        await ai_router._run_phase1_process_bounded(
            _sleeping_phase1_worker,
            timeout_seconds=0.05,
        )


@pytest.mark.asyncio
async def test_phase1_worker_crash_isolated_and_next_request_recreates_worker():
    with pytest.raises(RuntimeError, match="without a result"):
        await ai_router._run_phase1_process_bounded(
            _crashing_phase1_worker,
            timeout_seconds=3.0,
        )

    assert await ai_router._run_phase1_process_bounded(
        _successful_phase1_worker,
        timeout_seconds=3.0,
    ) == "recreated"


@pytest.mark.asyncio
async def test_phase1_process_cpu_limit_terminates_busy_child(monkeypatch):
    monkeypatch.setattr(ai_router.settings, "PARSEVAL_WORKER_CPU_SECONDS", 1)
    # RLIMIT_CPU may terminate the child with SIGKILL before it can write an
    # error envelope; the parent hard wall-clock kill is the final bound.
    with pytest.raises(TimeoutError, match="exceeded"):
        await ai_router._run_phase1_process_bounded(
            _busy_phase1_worker,
            timeout_seconds=3.0,
        )


@pytest.mark.asyncio
async def test_phase1_process_memory_limit_fails_closed(monkeypatch):
    if ai_router.resource is None:
        pytest.skip("resource limits are unavailable on this platform")
    monkeypatch.setattr(ai_router.settings, "PARSEVAL_WORKER_MEMORY_MB", 256)
    with pytest.raises((RuntimeError, TimeoutError), match="worker failed|without a result|resource limit"):
        await ai_router._run_phase1_process_bounded(
            _memory_hungry_phase1_worker,
            timeout_seconds=3.0,
        )
    assert await ai_router._run_phase1_process_bounded(
        _successful_phase1_worker,
        timeout_seconds=3.0,
    ) == "recreated"


@pytest.mark.asyncio
async def test_phase1_process_timeout_kills_descendant_process_group(tmp_path):
    if os.name != "posix":
        pytest.skip("process groups are tested on the Linux/WSL deployment target")
    pid_path = tmp_path / "descendant.pid"
    with pytest.raises(TimeoutError, match="exceeded"):
        await ai_router._run_phase1_process_bounded(
            _spawning_phase1_worker,
            timeout_seconds=1.0,
            pid_path=str(pid_path),
        )
    assert pid_path.exists()
    descendant_pid = int(pid_path.read_text(encoding="ascii"))
    for _ in range(20):
        try:
            os.kill(descendant_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("Phase 1 timeout left a descendant process alive")


@pytest.mark.asyncio
async def test_real_phase1_parser_runs_inside_bounded_process():
    run = await ai_router._run_phase1_bounded(
        parseval.generate_and_compare,
        schema_text="course(id INT)",
        standard_sql="SELECT id FROM course WHERE id > 1",
        student_sql="SELECT id FROM course WHERE id > 1",
        sql_dialect="sqlite",
        execution_backend="sqlite",
    )
    assert run.executed is True
    assert run.is_equivalent is True
    assert run.judge_status == "CORRECT"


@pytest.mark.asyncio
async def test_phase1_admission_rejects_requests_beyond_bounded_queue(monkeypatch):
    admission = asyncio.Semaphore(1)
    work_slots = asyncio.Semaphore(1)
    started = asyncio.Event()
    release_worker = asyncio.Event()

    async def fake_to_thread(function, **kwargs):
        return await function(**kwargs)

    async def blocked_worker(**_kwargs):
        started.set()
        await release_worker.wait()
        return "finished"

    monkeypatch.setattr(ai_router, "_PHASE1_ADMISSION_SLOTS", admission)
    monkeypatch.setattr(ai_router, "_PHASE1_WORK_SLOTS", work_slots)
    monkeypatch.setattr(ai_router, "_PHASE1_QUEUE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(ai_router.asyncio, "to_thread", fake_to_thread)

    first = asyncio.create_task(ai_router._run_phase1_bounded(blocked_worker))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    with pytest.raises(TimeoutError, match="queue is full"):
        await ai_router._run_phase1_bounded(blocked_worker)

    release_worker.set()
    assert await asyncio.wait_for(first, timeout=1.0) == "finished"
    await asyncio.sleep(0)
    assert admission._value == 1
    assert work_slots._value == 1


def test_check_sql_request_rejects_unsupported_feedback_locale():
    with pytest.raises(ValidationError):
        SQLCheckRequest(
            student_sql="SELECT 1",
            question_id=1,
            attempt_id=TEST_ATTEMPT_ID,
            language="zh",
        )


def test_check_sql_request_rejects_removed_challenge_mode():
    with pytest.raises(ValidationError):
        SQLCheckRequest(
            student_sql="SELECT 1",
            question_id=1,
            attempt_id=TEST_ATTEMPT_ID,
            challenge_mode=True,
        )


@pytest.mark.asyncio
async def test_phase1_capacity_stays_held_until_timed_out_thread_finishes(monkeypatch):
    gate = asyncio.Event()
    slots = asyncio.Semaphore(1)
    monkeypatch.setattr(ai_router, "_PHASE1_WORK_SLOTS", slots)
    monkeypatch.setattr(ai_router, "_PHASE1_RUN_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(ai_router, "_PHASE1_QUEUE_TIMEOUT_SECONDS", 0.01)

    async def fake_to_thread(function, **kwargs):
        return await function(**kwargs)

    async def blocked_worker(**_kwargs):
        await gate.wait()
        return "finished"

    monkeypatch.setattr(ai_router.asyncio, "to_thread", fake_to_thread)

    try:
        with pytest.raises(TimeoutError, match="verification exceeded"):
            await ai_router._run_phase1_bounded(blocked_worker)
        with pytest.raises(TimeoutError, match="capacity"):
            await ai_router._run_phase1_bounded(lambda: "must not be queued")
    finally:
        gate.set()
    await asyncio.wait_for(slots.acquire(), timeout=1.0)
    slots.release()


@pytest.mark.asyncio
async def test_same_attempt_singleflight_coalesces_before_expensive_work(monkeypatch):
    """Same-process retries wait before their first DB/Phase1 operation."""

    store: dict[tuple[int, int, str], ai_router.SQLCheckResponse] = {}
    expensive_calls = 0
    active = 0
    max_active = 0

    async def fake_impl(*, payload, user_id, session):
        del session
        nonlocal expensive_calls, active, max_active
        key = (user_id, payload.question_id, str(payload.attempt_id))
        committed = store.get(key)
        if committed is not None:
            return committed.model_copy(update={"idempotency_replayed": True})
        expensive_calls += 1
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        response = ai_router.SQLCheckResponse(
            is_correct=True,
            hint={},
            submission_id=1,
            attempt_id=str(payload.attempt_id),
            judge_status="CORRECT",
        )
        store[key] = response
        return response

    monkeypatch.setattr(ai_router, "_check_sql_impl", fake_impl)
    payload = SQLCheckRequest(
        student_sql="SELECT 1",
        question_id=1,
        attempt_id=TEST_ATTEMPT_ID,
    )
    first, replay = await asyncio.gather(
        check_sql(payload=payload, user_id=7, session=object()),
        check_sql(payload=payload, user_id=7, session=object()),
    )

    assert expensive_calls == 1
    assert max_active == 1
    assert {first.idempotency_replayed, replay.idempotency_replayed} == {
        False,
        True,
    }
    assert ai_router._ATTEMPT_FLIGHTS == {}


@pytest.fixture(autouse=True)
def _use_sqlite_compatibility_backend(monkeypatch):
    monkeypatch.setattr("routers.ai.settings.PARSEVAL_EXECUTION_BACKEND", "sqlite")


@pytest.mark.asyncio
async def test_check_sql_syntax_error(
    test_db_session,
    test_user,
    test_question,
):
    """测试 SQL 语法错误时触发早期检查并直接返回语法纠错提示。"""
    payload = SQLCheckRequest(
        student_sql="SELECT * FROM (students",  # 语法错误，缺失括号
        question_id=test_question.id,
        attempt_id=TEST_ATTEMPT_ID,
    )

    response = await check_sql(
        payload=payload,
        user_id=test_user.id,
        session=test_db_session,
    )

    assert response.is_correct is False
    assert response.is_safety_blocked is False
    assert "SQL 语法错误" in response.error_message
    assert "语法" in response.hint["overall_comment"] or "syntax" in response.hint["overall_comment"]
    replay = await check_sql(
        payload=payload,
        user_id=test_user.id,
        session=test_db_session,
    )
    assert response.idempotency_replayed is False
    assert replay.idempotency_replayed is True
    assert replay.submission_id == response.submission_id
    assert await test_db_session.scalar(
        select(func.count()).select_from(Submission)
    ) == 1
    assert await test_db_session.scalar(
        select(func.count()).select_from(ChatMessage)
    ) == 3


@pytest.mark.asyncio
async def test_check_sql_safety_blocked(test_db_session, test_user, test_question):
    """测试安全检查拦截危险 SQL 操作。"""
    payload = SQLCheckRequest(
        student_sql="DROP TABLE students",  # 危险 SQL
        question_id=test_question.id,
        attempt_id=TEST_ATTEMPT_ID,
    )

    response = await check_sql(
        payload=payload,
        user_id=test_user.id,
        session=test_db_session,
    )

    assert response.is_correct is False
    assert response.is_safety_blocked is True
    assert "危险操作" in response.error_message or "安全拦截" in response.hint["overall_comment"]
    replay = await check_sql(
        payload=payload,
        user_id=test_user.id,
        session=test_db_session,
    )
    assert replay.idempotency_replayed is True
    assert replay.submission_id == response.submission_id
    assert await test_db_session.scalar(
        select(func.count()).select_from(Submission)
    ) == 1
    assert await test_db_session.scalar(
        select(func.count()).select_from(SkillObservationEvent)
    ) == 0


@pytest.mark.asyncio
async def test_check_sql_normal_flow(test_db_session, test_user, test_question):
    """测试正常 SQL 执行和归因闭环流程（ParSEval 为唯一判题来源）。"""
    # 设置 schema_preview 供 ParSEval 造数判题
    test_question.correct_sql = "SELECT * FROM students WHERE age > 18"
    test_question.schema_preview = '{"tables":[{"name":"students","columns":["id","age"],"rows":[{"id":1,"age":20}]}]}'
    test_db_session.add(test_question)
    await test_db_session.flush()

    payload = SQLCheckRequest(
        student_sql="SELECT * FROM students WHERE age > 18",
        question_id=test_question.id,
        attempt_id=TEST_ATTEMPT_ID,
    )

    response = await check_sql(
        payload=payload,
        user_id=test_user.id,
        session=test_db_session,
    )

    assert response.is_correct is True
    assert response.is_safety_blocked is False
    assert "有界" in response.hint["overall_comment"]
    assert response.diagnostic_package is None
    assert response.teaching_support is not None
    assert response.teaching_support["status"] == "NOT_APPLICABLE"
    assert response.teaching_support["delivered_support_level"] == 1
    assert response.teaching_support["focused_error_count"] == 0
    assert response.phase3_learning is not None
    assert response.phase3_learning["status"] == "SKIP_NO_ASSESSMENT_MAP"
    assert response.phase3_learning["state_update_count"] == 0
    audit = await test_db_session.get(SubmissionTeachingAudit, response.submission_id)
    assert audit is not None
    assert audit.feedback_status == "BYPASS"
    assert audit.delivered_support_level == 1


@pytest.mark.asyncio
async def test_check_sql_correct_updates_only_declared_primary_raw_bkt_state(
    test_db_session,
    test_user,
    test_question,
):
    test_question.correct_sql = "SELECT title FROM course WHERE credits > 3"
    test_question.schema_preview = (
        '{"tables":[{"name":"course","columns":['
        '{"name":"course_id","type":"INT","primary_key":true},'
        '{"name":"title","type":"TEXT"},'
        '{"name":"credits","type":"INT"}],'
        '"rows":[{"course_id":1,"title":"DB","credits":4}]}]}'
    )
    test_db_session.add(test_question)
    await QuestionSkillRepository(test_db_session).replace_for_question(
        test_question.id,
        [
            QuestionSkillSpec(
                skill_id="filter.boundary",
                taxonomy_version=ATOMIC_SKILL_TAXONOMY_VERSION,
            )
        ],
        provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
    )
    await test_db_session.flush()

    response = await check_sql(
        payload=SQLCheckRequest(
            student_sql=test_question.correct_sql,
            question_id=test_question.id,
            attempt_id=TEST_ATTEMPT_ID,
        ),
        user_id=test_user.id,
        session=test_db_session,
    )

    assert response.is_correct is True
    assert response.lambda_t is None
    assert response.phase3_learning is not None
    assert response.phase3_learning["status"] == "UPDATED"
    assert response.phase3_learning["observation_count"] == 1
    assert response.phase3_learning["state_update_count"] == 1
    assert response.phase3_learning["support_need"] is None
    assert response.phase3_learning["challenge_readiness"] is not None
    assert response.phase3_learning["next_exercise_challenge_readiness"] == (
        response.phase3_learning["challenge_readiness"]
    )
    assert response.phase3_learning["challenge_usage"] == (
        "NEXT_EXERCISE_DIFFICULTY_ONLY"
    )
    assert response.phase3_learning["attempt_context_status"] == (
        "PRE_ATTEMPT_ASSISTANCE_NOT_TRACKED"
    )
    # The learner response is aggregate-only: authoritative Q-matrix identity
    # is available in the authenticated mastery profile, not echoed here.
    assert "filter.boundary" not in json.dumps(response.phase3_learning)

    state = await test_db_session.scalar(select(StudentSkillState))
    event = await test_db_session.scalar(select(SkillObservationEvent))
    assert state is not None and event is not None
    assert state.skill_id == "filter.boundary"
    assert state.posterior_mastery != pytest.approx(0.5)
    assert event.source_type == "QUESTION_QMATRIX"
    profile = await get_mastery_radar(
        user_id=test_user.id,
        session=test_db_session,
    )
    assert profile["display_value"] == "RAW_BKT_POSTERIOR"
    assert profile["atomic_mastery_state"]["filter.boundary"] == pytest.approx(
        state.posterior_mastery
    )


@pytest.mark.asyncio
async def test_check_sql_replays_committed_attempt_without_duplicate_side_effects(
    test_db_session,
    test_user,
    test_question,
    monkeypatch,
):
    """A transport retry returns the snapshot and never becomes new evidence."""

    test_question.correct_sql = "SELECT title FROM course WHERE credits > 3"
    test_question.schema_preview = (
        '{"tables":[{"name":"course","columns":['
        '{"name":"course_id","type":"INT","primary_key":true},'
        '{"name":"title","type":"TEXT"},'
        '{"name":"credits","type":"INT"}],'
        '"rows":[{"course_id":1,"title":"DB","credits":4}]}]}'
    )
    test_db_session.add(test_question)
    await QuestionSkillRepository(test_db_session).replace_for_question(
        test_question.id,
        [
            QuestionSkillSpec(
                skill_id="filter.boundary",
                taxonomy_version=ATOMIC_SKILL_TAXONOMY_VERSION,
            )
        ],
        provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
    )
    await test_db_session.flush()
    payload = SQLCheckRequest(
        student_sql=test_question.correct_sql,
        question_id=test_question.id,
        attempt_id=TEST_ATTEMPT_ID,
    )

    first = await check_sql(
        payload=payload,
        user_id=test_user.id,
        session=test_db_session,
    )
    first_snapshot = first.model_dump(mode="json")
    first_state = await test_db_session.scalar(select(StudentSkillState))
    assert first_state is not None
    first_state_version = first_state.state_version
    first_observation_count = first_state.observation_count

    def phase1_must_not_run_on_replay(**_kwargs):
        raise AssertionError("committed attempt replay must not execute Phase 1")

    monkeypatch.setattr(
        parseval,
        "generate_and_compare",
        phase1_must_not_run_on_replay,
    )
    replay = await check_sql(
        payload=payload,
        user_id=test_user.id,
        session=test_db_session,
    )

    assert first.idempotency_replayed is False
    assert replay.idempotency_replayed is True
    replay_snapshot = replay.model_dump(mode="json")
    replay_snapshot["idempotency_replayed"] = False
    assert replay_snapshot == first_snapshot
    assert await test_db_session.scalar(
        select(func.count()).select_from(Submission)
    ) == 1
    assert await test_db_session.scalar(
        select(func.count()).select_from(SkillObservationEvent)
    ) == 1
    assert await test_db_session.scalar(
        select(func.count()).select_from(StudentSkillState)
    ) == 1
    assert await test_db_session.scalar(
        select(func.count()).select_from(ChatMessage)
    ) == 3
    replayed_state = await test_db_session.scalar(select(StudentSkillState))
    assert replayed_state is not None
    assert replayed_state.state_version == first_state_version
    assert replayed_state.observation_count == first_observation_count


@pytest.mark.asyncio
async def test_check_sql_rejects_attempt_id_reuse_with_different_fingerprint(
    test_db_session,
    test_user,
    test_question,
):
    first_payload = SQLCheckRequest(
        student_sql="SELECT * FROM (students",
        question_id=test_question.id,
        attempt_id=TEST_ATTEMPT_ID,
    )
    first = await check_sql(
        payload=first_payload,
        user_id=test_user.id,
        session=test_db_session,
    )
    assert first.is_correct is False

    counts_before = {
        "submissions": await test_db_session.scalar(
            select(func.count()).select_from(Submission)
        ),
        "events": await test_db_session.scalar(
            select(func.count()).select_from(SkillObservationEvent)
        ),
        "states": await test_db_session.scalar(
            select(func.count()).select_from(StudentSkillState)
        ),
        "chats": await test_db_session.scalar(
            select(func.count()).select_from(ChatMessage)
        ),
    }

    with pytest.raises(HTTPException) as caught:
        await check_sql(
            payload=SQLCheckRequest(
                student_sql="SELECT * FROM students",
                question_id=test_question.id,
                attempt_id=TEST_ATTEMPT_ID,
            ),
            user_id=test_user.id,
            session=test_db_session,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "ATTEMPT_ID_REUSED"
    assert await test_db_session.scalar(
        select(func.count()).select_from(Submission)
    ) == counts_before["submissions"]
    assert await test_db_session.scalar(
        select(func.count()).select_from(SkillObservationEvent)
    ) == counts_before["events"]
    assert await test_db_session.scalar(
        select(func.count()).select_from(StudentSkillState)
    ) == counts_before["states"]
    assert await test_db_session.scalar(
        select(func.count()).select_from(ChatMessage)
    ) == counts_before["chats"]


@pytest.mark.asyncio
async def test_check_sql_verified_boundary_error_drives_atomic_scheduler_and_bkt(
    test_db_session,
    test_user,
    test_question,
):
    test_question.content = "查询学分严格超过 3 的课程"
    test_question.correct_sql = "SELECT title FROM course WHERE credits > 3"
    test_question.schema_preview = (
        '{"tables":[{"name":"course","columns":['
        '{"name":"course_id","type":"INT","primary_key":true},'
        '{"name":"title","type":"TEXT"},'
        '{"name":"credits","type":"INT"}],'
        '"rows":[{"course_id":1,"title":"Boundary","credits":3},'
        '{"course_id":2,"title":"DB","credits":4}]}]}'
    )
    test_db_session.add(test_question)
    await test_db_session.flush()

    response = await check_sql(
        payload=SQLCheckRequest(
            student_sql="SELECT title FROM course WHERE credits >= 3",
            question_id=test_question.id,
            attempt_id=TEST_ATTEMPT_ID,
        ),
        user_id=test_user.id,
        session=test_db_session,
    )

    assert response.is_correct is False
    assert response.diagnostic_package is None
    assert response.teaching_support is not None
    assert response.teaching_support["status"] == "APPLIED"
    assert response.teaching_support["recommended_support_level"] == 2
    assert response.teaching_support["delivered_support_level"] == 2
    assert response.teaching_support["focused_error_count"] == 1
    assert response.phase3_learning is not None
    assert response.phase3_learning["status"] == "UPDATED"
    assert response.phase3_learning["priority_policy_version"] == (
        "phase3.priority_policy.v1"
    )
    assert "priority_score" not in response.phase3_learning
    assert response.phase3_learning["support_policy_version"] == (
        "phase3.support_policy.v2"
    )
    assert response.phase3_learning["support_need"] == pytest.approx(0.38)
    assert response.phase3_learning["recommended_support_level"] == 2
    assert response.phase3_learning["support_recommendation_applied"] is True
    assert response.phase3_learning["delivered_support_level"] == 2
    assert response.phase3_learning["behavioral_proxy_status"] == (
        "BEHAVIORAL_SUPPORT_NEED_PROXY_V1"
    )
    assert response.phase3_learning["behavioral_support_need"] == pytest.approx(
        0.65 + 0.35 / 3
    )

    state = await test_db_session.scalar(select(StudentSkillState))
    event = await test_db_session.scalar(select(SkillObservationEvent))
    assert state is not None and event is not None
    assert state.taxonomy_version == ATOMIC_SKILL_TAXONOMY_VERSION
    assert state.skill_id == "filter.boundary"
    assert state.observation_count == 1
    assert event.source_type == "PHASE2_RULE"
    assert event.rule_id == "S2_BOUNDARY"
    assert event.assistance_level == 2
    submission = await test_db_session.get(Submission, response.submission_id)
    audit = await test_db_session.get(SubmissionTeachingAudit, response.submission_id)
    assert submission is not None and audit is not None
    assert submission.hint_level == event.assistance_level == (
        audit.delivered_support_level
    ) == 2
    assert audit.target_rule_id == "S2_BOUNDARY"
    assert audit.target_skill_id == "filter.boundary"
    assert audit.feedback_status == "PRIMARY"


@pytest.mark.asyncio
async def test_phase3_persistence_failure_cannot_rewrite_authoritative_verdict(
    test_db_session,
    test_user,
    test_question,
    monkeypatch,
):
    test_question.correct_sql = "SELECT title FROM course WHERE credits > 3"
    test_question.schema_preview = (
        '{"tables":[{"name":"course","columns":['
        '{"name":"course_id","type":"INT","primary_key":true},'
        '{"name":"title","type":"TEXT"},'
        '{"name":"credits","type":"INT"}],'
        '"rows":[{"course_id":1,"title":"DB","credits":4}]}]}'
    )
    test_db_session.add(test_question)
    await QuestionSkillRepository(test_db_session).replace_for_question(
        test_question.id,
        [
            QuestionSkillSpec(
                skill_id="filter.boundary",
                taxonomy_version=ATOMIC_SKILL_TAXONOMY_VERSION,
            )
        ],
        provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
    )
    await test_db_session.flush()

    async def injected_failure(*_args, **_kwargs):
        raise RuntimeError("injected Phase3 storage failure")

    monkeypatch.setattr(
        Phase3LearningRepository,
        "apply_trusted_observations",
        injected_failure,
    )
    response = await check_sql(
        payload=SQLCheckRequest(
            student_sql=test_question.correct_sql,
            question_id=test_question.id,
            attempt_id=TEST_ATTEMPT_ID,
        ),
        user_id=test_user.id,
        session=test_db_session,
    )

    assert response.is_correct is True
    assert response.judge_status == "CORRECT"
    assert response.phase3_learning is not None
    assert response.phase3_learning["status"] == "DEGRADED_NO_LEARNING_UPDATE"
    assert await test_db_session.scalar(
        select(func.count()).select_from(Submission)
    ) == 1
    assert await test_db_session.scalar(
        select(func.count()).select_from(StudentSkillState)
    ) == 0
    assert await test_db_session.scalar(
        select(func.count()).select_from(SkillObservationEvent)
    ) == 0


@pytest.mark.asyncio
async def test_check_sql_hides_phase2_package_and_returns_cropped_teaching_support(
    test_db_session,
    test_user,
    test_question,
):
    """Phase 1/2 evidence stays internal; learners receive one cropped action."""
    # 设置题目元数据和 schema_preview（ParSEval 造数用）
    test_question.correct_sql = "SELECT name FROM students WHERE age > 20"
    test_question.schema_preview = '{"tables":[{"name":"students","columns":["id","age","name"],"rows":[{"id":1,"age":20,"name":"Alice"},{"id":2,"age":22,"name":"Bob"}]}]}'
    test_db_session.add(test_question)
    await test_db_session.flush()

    # 学生 SQL 故意少些了 WHERE 条件
    payload = SQLCheckRequest(
        student_sql="SELECT name FROM students",
        question_id=test_question.id,
        attempt_id=TEST_ATTEMPT_ID,
    )

    response = await check_sql(
        payload=payload,
        user_id=test_user.id,
        session=test_db_session,
    )

    assert response.is_correct is False
    assert response.observation is None
    assert response.error_attributions == []

    assert response.diagnostic_package is None
    support = response.teaching_support
    assert support is not None
    assert support["schema_version"] == "phase4.teaching_support.v1"
    assert support["focused_error_count"] in {0, 1}
    assert support["answer_revealed"] is False
    forbidden_keys = {"rule_id", "skill_id", "candidate_id", "witness", "qss"}
    assert forbidden_keys.isdisjoint(support)
    serialized = json.dumps(response.model_dump(), ensure_ascii=False).lower()
    assert test_question.correct_sql.lower() not in serialized
    assert "age > 20" not in serialized
    assert "where age > 20" not in serialized


@pytest.mark.asyncio
async def test_check_sql_unsupported_dialect_feature_is_not_attributed_to_student(
    test_db_session,
    test_user,
    test_question,
):
    test_question.correct_sql = (
        "SELECT region, product, SUM(amount) FROM sales "
        "GROUP BY ROLLUP(region, product)"
    )
    test_question.schema_preview = (
        '{"tables":[{"name":"sales","columns":["region","product","amount"],'
        '"rows":[{"region":"East","product":"A","amount":10}]}]}'
    )
    test_db_session.add(test_question)
    await test_db_session.flush()

    payload = SQLCheckRequest(
        student_sql="SELECT region, product, SUM(amount) FROM sales GROUP BY region, product",
        question_id=test_question.id,
        attempt_id=TEST_ATTEMPT_ID,
    )

    with pytest.raises(HTTPException) as caught:
        await check_sql(
            payload=payload,
            user_id=test_user.id,
            session=test_db_session,
        )

    assert getattr(caught.value, "status_code", None) == 422
    assert caught.value.detail["code"] == "UNSUPPORTED"
    assert await test_db_session.scalar(
        select(func.count()).select_from(Submission)
    ) == 0


@pytest.mark.asyncio
async def test_check_sql_undecided_never_becomes_correct_or_writes_learning_state(
    monkeypatch,
):
    question = SimpleNamespace(
        id=71,
        title="测试题目",
        content="查询学生编号",
        correct_sql="SELECT id FROM students",
        sql_dialect=None,
        engine_version=None,
        schema_preview='{"tables":[{"name":"students","columns":["id"]}]}',
        required_output_columns=None,
        difficulty=1,
    )

    class ReadOnlyQuestionRepository:
        def __init__(self, _session):
            pass

        async def get_by_id(self, _question_id):
            return question

    class NoWriteSession:
        commits = 0

        async def commit(self):
            self.commits += 1

    session = NoWriteSession()

    monkeypatch.setattr(ai_router, "QuestionRepository", ReadOnlyQuestionRepository)

    class IdempotencyReadOnlySubmissionRepository:
        reads = 0

        def __init__(self, _session):
            pass

        async def get_by_attempt_id(self, *_args, **_kwargs):
            self.__class__.reads += 1
            return None

        async def create(self, *_args, **_kwargs):
            raise AssertionError("UNDECIDED must not create a submission")

    def learning_repository_must_not_be_reached(_session):
        raise AssertionError("UNDECIDED must not access side-effect repositories")

    monkeypatch.setattr(
        ai_router,
        "SubmissionRepository",
        IdempotencyReadOnlySubmissionRepository,
    )
    monkeypatch.setattr(ai_router, "ChatRepository", learning_repository_must_not_be_reached)

    def undecided_run(**_kwargs):
        return parseval.SandboxRun(
            executed=True,
            is_equivalent=True,  # legacy bounded-world observation
            error=None,
            standard_sqlite="SELECT id FROM students",
            student_sqlite="SELECT id FROM students WHERE id IS NOT NULL",
            standard_rows=[(1,)],
            student_rows=[(1,)],
            standard_columns=["id"],
            student_columns=["id"],
            test_database={"students": [{"id": 1}]},
            data_evidence={
                "judge_status": "UNDECIDED",
                "status": "KNOWN_GAP",
                "equivalence_conclusion": "UNDECIDED",
                "verdict_guard": {
                    "reason": "ast_differences_without_distinguished_obligation"
                },
            },
            mutation_evidence={},
            judge_status="UNDECIDED",
            status="KNOWN_GAP",
            equivalence_conclusion="UNDECIDED",
        )

    monkeypatch.setattr(parseval, "generate_and_compare", undecided_run)

    async def run_without_worker_thread(function, **kwargs):
        return function(**kwargs)

    monkeypatch.setattr(ai_router.asyncio, "to_thread", run_without_worker_thread)

    def attribution_must_not_run(**_kwargs):
        raise AssertionError("UNDECIDED must stop before attribution")

    monkeypatch.setattr(
        ai_router,
        "evidence_weights_from_observation",
        attribution_must_not_run,
    )

    with pytest.raises(HTTPException) as caught:
        await check_sql(
            payload=SQLCheckRequest(
                student_sql="SELECT id FROM students WHERE id IS NOT NULL",
                question_id=question.id,
                attempt_id=TEST_ATTEMPT_ID,
            ),
            user_id=99,
            session=session,
        )

    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "JUDGE_UNDECIDED"
    assert caught.value.detail["judge_status"] == "UNDECIDED"
    assert IdempotencyReadOnlySubmissionRepository.reads == 1
    assert session.commits == 0


def test_phase1_no_counterexample_requires_clean_supported_contract():
    valid = parseval.SandboxRun(
        executed=True,
        is_equivalent=True,
        error=None,
        standard_sqlite="SELECT id FROM students",
        student_sqlite="SELECT id FROM students",
        standard_rows=[(1,)],
        student_rows=[(1,)],
        standard_columns=["id"],
        student_columns=["id"],
        test_database={"students": [{"id": 1}]},
        data_evidence={},
        mutation_evidence={},
        judge_status="CORRECT",
        status="SUPPORTED",
        equivalence_conclusion="NO_COUNTEREXAMPLE_FOUND",
    )

    assert ai_router._authoritative_phase1_decision(valid) == (True, "CORRECT")

    valid.boundary_evidence = {"reason": "unsupported_recursive_shape"}
    assert ai_router._authoritative_phase1_decision(valid) == (None, "UNDECIDED")

    valid.executed = False
    valid.boundary_evidence = {}
    valid.equivalence_conclusion = ""
    valid.status = ""
    assert ai_router._authoritative_phase1_decision(valid) == (None, "UNDECIDED")

    # The compatibility branch must respect rich boundary/guard evidence even
    # when an older test double omits equivalence_conclusion and status.
    valid.executed = True
    valid.is_equivalent = True
    valid.boundary_evidence = {"reason": "legacy boundary"}
    assert ai_router._authoritative_phase1_decision(valid) == (None, "UNDECIDED")

    valid.boundary_evidence = {}
    valid.data_evidence = {"verdict_guard": {"reason": "unproven difference"}}
    assert ai_router._authoritative_phase1_decision(valid) == (None, "UNDECIDED")


def test_public_diagnostic_gate_binds_verdict_and_rejects_reference_fragments():
    correct_package, _ = ai_router._fallback_phase2_result(
        is_correct=True,
        language="en",
    )
    with pytest.raises(ValueError, match="Phase 1 verdict"):
        ai_router._validated_public_diagnostic_package(
            correct_package,
            correct_sql="SELECT name FROM students WHERE age > 20",
            expected_is_correct=False,
        )

    wrong_package, _ = ai_router._fallback_phase2_result(
        is_correct=False,
        language="en",
    )
    wrong_package["narrative"]["conflict_and_witness"] = (
        "The hidden target predicate is age > 20."
    )
    with pytest.raises(ValueError, match="reference SQL"):
        ai_router._validated_public_diagnostic_package(
            wrong_package,
            correct_sql="SELECT name FROM students WHERE age > 20",
            expected_is_correct=False,
        )

    wrong_package["narrative"]["conflict_and_witness"] = (
        'Leaked query: SELECT "secret_name" FROM students'
    )
    with pytest.raises(ValueError, match="reference SQL"):
        ai_router._validated_public_diagnostic_package(
            wrong_package,
            correct_sql='SELECT "secret_name" FROM students',
            expected_is_correct=False,
        )
