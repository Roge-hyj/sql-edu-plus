"""Contract tests for the evidence-bounded Phase 2/5 LLM adapters."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import core.llm_teaching as llm_teaching
from core.student_feedback import render_llm_student_feedback
from core.teaching_action import select_teaching_actions
from settings.config import settings


class _FakeCompletions:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))
                )
            ]
        )


class _FakeResponses:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_text=json.dumps(self.payload, ensure_ascii=False),
            output=[],
        )


class _FakeClient:
    def __init__(
        self,
        completions: _FakeCompletions,
        responses: _FakeResponses | None = None,
        **_kwargs,
    ):
        self.chat = SimpleNamespace(completions=completions)
        self.responses = responses or _FakeResponses({})
        self.closed = False

    async def close(self):
        self.closed = True


def _enable_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LLM_TEACHING_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_PHASE2_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_PHASE5_ENABLED", True)
    monkeypatch.setattr(settings, "AI_API_KEY", "test-key")
    monkeypatch.setattr(settings, "AI_MODEL_NAME", "test-model")
    monkeypatch.setattr(settings, "AI_WIRE_API", "chat_completions")
    monkeypatch.setattr(settings, "AI_CC_SWITCH_DB_PATH", "")
    monkeypatch.setattr(settings, "AI_CC_SWITCH_PROVIDER_ID", "")
    monkeypatch.setattr(settings, "AI_CC_SWITCH_MODEL", "")
    monkeypatch.setattr(settings, "LLM_TIMEOUT_SECONDS", 2.0)


def _package() -> dict:
    return {
        "schema_version": "phase2.public.v1",
        "diagnosis_version": "phase2.diagnosis.v1",
        "rule_catalog_version": "phase2.rules.mvp20.v1",
        "verdict": "INCORRECT",
        "diagnosis_status": "SUPPORTED",
        "phase1": {
            "status": "SUPPORTED",
            "equivalence_conclusion": "NOT_EQUIVALENT",
            "judge_status": "WRONG",
        },
        "ordered_diff_pipeline": [
            {
                "diff_id": "diff_1",
                "obligation_id": "obligation_1",
                "logical_stage": "ROW_FILTER",
                "teaching_stage": "S2",
                "clause": "WHERE",
                "diff_type": "comparison_operator_changed",
                "evidence_grade": "CAUSAL_VERIFIED",
            }
        ],
        "primary": {
            "candidate_id": "candidate_1",
            "rule_id": "S2_BOUNDARY",
            "logical_stage": "ROW_FILTER",
            "stage": "S2",
            "evidence_grade": "CAUSAL_VERIFIED",
            "evidence_refs": {
                "diff_ids": ["diff_1"],
                "verified_diff_ids": ["diff_1"],
                "obligation_ids": ["obligation_1"],
                "mutation_test_ids": ["mutation_1"],
            },
        },
        "secondary": [],
        "secondary_count": 0,
        "suppressed_symptoms": [],
        "unresolved_count": 0,
        "witness": {"availability": "PAIR_DISTINGUISHED", "cases": []},
        "qss": {},
        "narrative": {
            "student_behavior": "当前比较会保留临界记录。",
            "conflict_and_witness": "可信物证表明，该临界记录不属于题目要求的结果。",
            "guidance_question": "请再想一想这个临界记录是否应该保留？",
        },
        "boundary_notes": [],
    }


def _run() -> SimpleNamespace:
    return SimpleNamespace(
        status="SUPPORTED",
        equivalence_conclusion="NOT_EQUIVALENT",
        judge_status="WRONG",
        executed=True,
        data_evidence={
            "status": "SUPPORTED",
            "standard_row_count": 1,
            "student_row_count": 2,
            "any_world_distinguished": True,
        },
        mutation_evidence={
            "enabled": True,
            "summary": {"executed": 2, "fixed_by_replacement": 1},
            "tests": [{"test_id": "mutation_1", "distinguished": True}],
        },
        ast_diffs=[],
    )


@pytest.mark.asyncio
async def test_phase2_llm_can_only_select_existing_strong_evidence_and_keep_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_llm(monkeypatch)
    response = {
        "decision": "SUPPORTED_WRONG",
        "primary_candidate_id": "candidate_1",
        "secondary_candidate_ids": [],
        "evidence_ids": ["diff_1", "obligation_1", "mutation_1"],
        "confidence": 0.91,
        "rationale": "独立的执行与修复证据支持该候选。",
        "uncertainty": "结论仍限定在当前有界检查范围内。",
        "narrative": {
            "student_behavior": "当前比较会保留临界记录。",
            "conflict_and_witness": "可信物证表明，该临界记录不属于题目要求的结果。",
            "guidance_question": "这个临界记录应该被包含吗？",
        },
    }
    fake_completions = _FakeCompletions(response)
    fake_client = _FakeClient(fake_completions)
    monkeypatch.setattr(llm_teaching, "AsyncOpenAI", lambda **kwargs: fake_client)

    assessment = await llm_teaching.arbitrate_phase2_evidence(
        package=_package(),
        sandbox_run=_run(),
        question="查询严格超过边界的课程",
        schema={"tables": [{"name": "course", "columns": ["credits"]}]},
        standard_sql="SELECT title FROM course WHERE credits > 3",
        student_sql="SELECT title FROM course WHERE credits >= 3",
    )

    assert assessment is not None
    assert assessment.authoritative_verdict == "INCORRECT"
    assert assessment.primary_candidate_id == "candidate_1"
    merged = llm_teaching.merge_phase2_llm_assessment(_package(), assessment)
    assert merged["verdict"] == "INCORRECT"
    assert merged["primary"]["candidate_id"] == "candidate_1"
    assert merged["narrative"]["guidance_question"] == _package()["narrative"]["guidance_question"]
    assert fake_client.closed is True
    assert fake_completions.calls
    assert "SELECT title FROM course WHERE credits > 3" in fake_completions.calls[0]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_responses_wire_uses_instructions_and_extracts_output_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_llm(monkeypatch)
    monkeypatch.setattr(settings, "AI_WIRE_API", "responses")
    fake_responses = _FakeResponses({"ok": True})
    fake_client = _FakeClient(_FakeCompletions({}), fake_responses)
    monkeypatch.setattr(llm_teaching, "AsyncOpenAI", lambda **kwargs: fake_client)

    result = await llm_teaching._request_json(
        stage="PHASE5",
        system_prompt="只输出 JSON。",
        user_payload={"probe": True},
    )

    assert result == {"ok": True}
    assert fake_responses.calls == [
        {
            "model": "test-model",
            "instructions": "只输出 JSON。",
            "input": '{"probe":true}',
            "max_output_tokens": 1200,
            "store": False,
        }
    ]
    assert fake_client.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_decision",
    [
        "OPERATIONALLY_EQUIVALENT",
        "SUPPORTED_WRONG",
    ],
)
async def test_phase2_llm_rejects_verdict_or_candidate_escalation(
    monkeypatch: pytest.MonkeyPatch,
    bad_decision: str,
) -> None:
    _enable_llm(monkeypatch)
    response = {
        "decision": bad_decision,
        "primary_candidate_id": "candidate_1" if bad_decision == "SUPPORTED_WRONG" else None,
        "secondary_candidate_ids": [],
        "evidence_ids": ["diff_1"] if bad_decision == "SUPPORTED_WRONG" else [],
        "confidence": 0.9,
        "rationale": "证据复核结果。",
        "uncertainty": "仍受有界范围限制。",
        "narrative": None,
    }
    fake_completions = _FakeCompletions(response)
    monkeypatch.setattr(
        llm_teaching,
        "AsyncOpenAI",
        lambda **kwargs: _FakeClient(fake_completions),
    )
    package = _package()
    if bad_decision == "SUPPORTED_WRONG":
        package["primary"]["candidate_id"] = "unknown_candidate"
    result = await llm_teaching.arbitrate_phase2_evidence(
        package=package,
        sandbox_run=_run(),
        standard_sql="SELECT title FROM course WHERE credits > 3",
        student_sql="SELECT title FROM course WHERE credits >= 3",
    )
    assert result is None


@pytest.mark.asyncio
async def test_phase5_llm_rewrites_only_editable_approved_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_llm(monkeypatch)
    package = _package()
    phase3 = SimpleNamespace(
        selected=SimpleNamespace(phase2_candidate_id="candidate_1"),
        selected_target=SimpleNamespace(
            observation_id="observation_1",
            phase2_candidate_id="candidate_1",
            phase2_rule_id="S2_BOUNDARY",
            logical_stage="ROW_FILTER",
            skill_id="filter.boundary",
            taxonomy_version="sql_atomic_skills.v1",
            source_role="PRIMARY",
            evidence_grade="CAUSAL_VERIFIED",
        ),
        support=SimpleNamespace(support_level=3, support_need=0.6),
    )
    plan = select_teaching_actions(
        package,
        phase3,
        expected_is_correct=False,
        language="zh-CN",
    )
    actions = list(plan.actions)
    editable_actions = [
        action
        for action in actions
        if action.kind.value not in {"SOCRATIC_QUESTION", "ACCEPTANCE", "SYSTEM_NOTICE"}
    ]
    response = {
        "segments": [
            {"action_id": action.action_id, "text": f"提醒：{action.text}"}
            for action in editable_actions
        ]
    }
    fake_completions = _FakeCompletions(response)
    monkeypatch.setattr(
        llm_teaching,
        "AsyncOpenAI",
        lambda **kwargs: _FakeClient(fake_completions),
    )

    generated = await llm_teaching.generate_phase5_feedback(plan)

    assert generated is not None
    artifact = render_llm_student_feedback(plan, generated)
    assert artifact.renderer == "LLM_SAFE_REPHRASE"
    assert artifact.feedback_source == "PHASE5_LLM"
    assert artifact.to_public_dict()["generation_source"] == "LLM"
    assert "SELECT" not in artifact.text
    assert plan.actions[2].text in artifact.text


@pytest.mark.asyncio
async def test_phase5_llm_malformed_or_sql_shaped_output_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_llm(monkeypatch)
    package = _package()
    phase3 = SimpleNamespace(
        selected=SimpleNamespace(phase2_candidate_id="candidate_1"),
        selected_target=SimpleNamespace(
            observation_id="observation_1",
            phase2_candidate_id="candidate_1",
            phase2_rule_id="S2_BOUNDARY",
            logical_stage="ROW_FILTER",
            skill_id="filter.boundary",
            taxonomy_version="sql_atomic_skills.v1",
            source_role="PRIMARY",
            evidence_grade="CAUSAL_VERIFIED",
        ),
        support=SimpleNamespace(support_level=2, support_need=0.3),
    )
    plan = select_teaching_actions(package, phase3, expected_is_correct=False)
    actions = list(plan.actions)
    response = {
        "segments": [
            {"action_id": actions[0].action_id, "text": "SELECT title FROM course"},
        ]
    }
    fake_completions = _FakeCompletions(response)
    monkeypatch.setattr(
        llm_teaching,
        "AsyncOpenAI",
        lambda **kwargs: _FakeClient(fake_completions),
    )

    assert await llm_teaching.generate_phase5_feedback(plan) is None


@pytest.mark.asyncio
async def test_llm_is_not_called_when_feature_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "LLM_TEACHING_ENABLED", False)

    def must_not_construct(**_kwargs):
        raise AssertionError("disabled LLM must not construct a provider client")

    monkeypatch.setattr(llm_teaching, "AsyncOpenAI", must_not_construct)
    assert await llm_teaching.arbitrate_phase2_evidence(
        package=_package(),
        sandbox_run=_run(),
    ) is None


@pytest.mark.asyncio
async def test_provider_close_is_time_bounded_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_llm(monkeypatch)
    monkeypatch.setattr(settings, "LLM_TIMEOUT_SECONDS", 1.0)

    class _SlowCloseClient(_FakeClient):
        async def close(self):
            await asyncio.sleep(2.0)

    fake_completions = _FakeCompletions({"ok": True})
    client = _SlowCloseClient(fake_completions)
    monkeypatch.setattr(llm_teaching, "AsyncOpenAI", lambda **kwargs: client)

    result = await llm_teaching._request_json(
        stage="PHASE5",
        system_prompt="Return JSON.",
        user_payload={"health_check": True},
    )
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_provider_request_timeout_is_hard_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_llm(monkeypatch)
    monkeypatch.setattr(settings, "LLM_TIMEOUT_SECONDS", 1.0)
    fake_completions = _FakeCompletions({"ok": True})

    async def _hang(**_kwargs):
        await asyncio.sleep(2.0)

    fake_completions.create = _hang
    monkeypatch.setattr(
        llm_teaching,
        "AsyncOpenAI",
        lambda **kwargs: _FakeClient(fake_completions),
    )

    started = time.monotonic()
    result = await llm_teaching._request_json(
        stage="PHASE5",
        system_prompt="Return JSON.",
        user_payload={"health_check": True},
    )
    elapsed = time.monotonic() - started
    assert result is None
    assert elapsed < 1.8
