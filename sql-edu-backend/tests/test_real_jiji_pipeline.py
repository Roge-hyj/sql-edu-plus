"""Opt-in live integration test for the configured CC Switch jiji provider.

This test is intentionally excluded from ordinary pytest runs because it
spends provider quota.  Run it with ``RUN_REAL_JIJI_PIPELINE=1`` after the
local ``.env`` selects the explicit CC Switch provider.  It exercises the
real PostgreSQL Phase 1 runner and the real Phase 2/Phase 5 LLM calls through
the normal ``/ai/check-sql`` route boundary.
"""

from __future__ import annotations

import json
import os

import pytest

import core.llm_teaching as llm_teaching
from core.llm_teaching import LLM_PROVIDER_KIND
from models.submission_teaching_audit import SubmissionTeachingAudit
from routers.ai import SQLCheckRequest, check_sql
from settings.config import settings


@pytest.mark.asyncio
async def test_real_jiji_postgres_phase1_to_phase5_pipeline(
    test_db_session,
    test_user,
    test_question,
    monkeypatch,
) -> None:
    if os.environ.get("RUN_REAL_JIJI_PIPELINE") != "1":
        pytest.skip("set RUN_REAL_JIJI_PIPELINE=1 to spend live provider quota")

    monkeypatch.setattr(settings, "LLM_TEACHING_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_PHASE2_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_PHASE5_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_TIMEOUT_SECONDS", 60.0)
    monkeypatch.setattr(settings, "PARSEVAL_EXECUTION_BACKEND", "auto")

    original_request_json = llm_teaching._request_json

    async def _diagnostic_request_json(**kwargs):
        import time

        started = time.monotonic()
        result = await original_request_json(**kwargs)
        phase2_raw_summary = {}
        if kwargs.get("stage") == "PHASE2" and isinstance(result, dict):
            permitted_candidates = set(
                kwargs.get("user_payload", {}).get("permitted_strong_candidate_ids", [])
            )
            permitted_evidence = set(
                kwargs.get("user_payload", {}).get("permitted_evidence_ids", [])
            )
            primary = result.get("primary_candidate_id")
            secondary = result.get("secondary_candidate_ids")
            evidence = result.get("evidence_ids")
            narrative = result.get("narrative")
            phase2_raw_summary = {
                "raw_decision": result.get("decision"),
                "raw_primary_allowed": primary is None or primary in permitted_candidates,
                "raw_secondary_allowed": isinstance(secondary, list)
                and all(item in permitted_candidates for item in secondary),
                "raw_evidence_allowed": isinstance(evidence, list)
                and all(item in permitted_evidence for item in evidence),
                "raw_evidence_count": len(evidence) if isinstance(evidence, list) else None,
                "raw_confidence_type": type(result.get("confidence")).__name__,
                "raw_rationale_forbidden": llm_teaching._forbidden_text(result.get("rationale", "")),
                "raw_uncertainty_forbidden": llm_teaching._forbidden_text(result.get("uncertainty", "")),
                "raw_narrative_keys": sorted(narrative) if isinstance(narrative, dict) else None,
            }
        print(
            json.dumps(
                {
                    "live_llm_stage": kwargs.get("stage"),
                    "live_llm_seconds": round(time.monotonic() - started, 3),
                    "live_llm_returned": result is not None,
                    "live_llm_keys": sorted(result.keys()) if isinstance(result, dict) else [],
                    **phase2_raw_summary,
                },
                ensure_ascii=False,
            )
        )
        return result

    monkeypatch.setattr(llm_teaching, "_request_json", _diagnostic_request_json)

    original_arbitrate = llm_teaching.arbitrate_phase2_evidence

    async def _diagnostic_arbitrate(**kwargs):
        result = await original_arbitrate(**kwargs)
        print(
            json.dumps(
                {
                    "phase2_assessment_accepted": result is not None,
                    "phase2_decision": result.decision if result is not None else None,
                    "phase2_primary": result.primary_candidate_id if result is not None else None,
                    "phase2_evidence_count": len(result.evidence_ids) if result is not None else 0,
                    "phase2_narrative_applied": result.narrative is not None if result is not None else False,
                },
                ensure_ascii=False,
            )
        )
        return result

    monkeypatch.setattr(llm_teaching, "arbitrate_phase2_evidence", _diagnostic_arbitrate)

    test_question.title = "PostgreSQL 边界条件教学闭环"
    test_question.content = "查询学分严格超过 3 的课程"
    test_question.correct_sql = "SELECT title FROM course WHERE credits > 3"
    test_question.sql_dialect = "postgres"
    test_question.engine_version = "16"
    test_question.schema_preview = json.dumps(
        {
            "tables": [
                {
                    "name": "course",
                    "columns": [
                        {"name": "course_id", "type": "INT", "primary_key": True},
                        {"name": "title", "type": "TEXT"},
                        {"name": "credits", "type": "INT"},
                    ],
                    "rows": [
                        {"course_id": 1, "title": "Boundary", "credits": 3},
                        {"course_id": 2, "title": "Database", "credits": 4},
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )
    test_db_session.add(test_question)
    await test_db_session.flush()

    response = await check_sql(
        payload=SQLCheckRequest(
            student_sql="SELECT title FROM course WHERE credits >= 3",
            question_id=test_question.id,
            attempt_id="00000000-0000-4000-8000-000000009901",
            language="zh-CN",
        ),
        user_id=test_user.id,
        session=test_db_session,
    )

    assert response.is_correct is False
    assert response.judge_status == "WRONG"
    assert response.phase3_learning is not None
    assert response.phase3_learning["status"] == "UPDATED"
    assert response.teaching_support is not None
    assert response.teaching_support["feedback_status"] == "PRIMARY"
    assert response.teaching_support["generation_source"] == "LLM"
    assert response.teaching_support["answer_revealed"] is False
    assert test_question.correct_sql not in response.hint["overall_comment"]

    submission = await test_db_session.get(SubmissionTeachingAudit, response.submission_id)
    assert submission is not None
    assert submission.generation_source == "PHASE5_LLM"
    assert submission.action_snapshot.get("phase2_llm_review", {}).get("provider") == LLM_PROVIDER_KIND
    assert submission.action_snapshot["phase2_llm_review"]["model"] == settings.AI_MODEL_NAME

    serialized_public = json.dumps(
        {
            "hint": response.hint,
            "teaching_support": response.teaching_support,
            "phase3_learning": response.phase3_learning,
        },
        ensure_ascii=False,
    )
    assert "correct_sql" not in serialized_public.lower()
    assert "answer_sql" not in serialized_public.lower()
