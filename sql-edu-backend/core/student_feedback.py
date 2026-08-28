"""Phase 5: learner-safe rendering of an approved teaching-action plan.

The deterministic renderer remains the safety fallback.  When the optional
LLM adapter returns a validated one-to-one segment rewrite, this module builds
the final artifact from the same approved action plan and applies the same
size/language/answer-free invariants.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re
from typing import Any

from core.teaching_action import TeachingActionKind, TeachingActionPlan
from core.teaching_action import (
    TEACHING_ACTION_POLICY_VERSION,
    TEACHING_SUPPORT_SCHEMA_VERSION,
)


STUDENT_FEEDBACK_SCHEMA_VERSION = "phase5.student_feedback.v1"
STUDENT_FEEDBACK_POLICY_VERSION = "phase5.safe_renderer.v1"
DETERMINISTIC_RENDERER = "DETERMINISTIC_SAFE_TEMPLATE"
PUBLIC_GENERATION_SOURCE = "LOCAL_TEMPLATE"
LLM_RENDERER = "LLM_SAFE_REPHRASE"
LLM_FEEDBACK_SOURCE = "PHASE5_LLM"
PUBLIC_LLM_GENERATION_SOURCE = "LLM"
MAX_FEEDBACK_BYTES = 16 * 1024
_LLM_TEXT_FORBIDDEN = re.compile(
    r"(?:```|\b(?:SELECT|INSERT|UPDATE|DELETE|WITH)\b[\s\S]{0,240}\b(?:FROM|SET|AS)\b|"
    r"\b(?:WHERE|HAVING|JOIN|GROUP\s+BY|ORDER\s+BY|LIMIT|OFFSET)\b\s+[A-Za-z_\"`]|"
    r"[A-Za-z_][A-Za-z0-9_.\"`]*\s*(?:<>|!=|<=|>=|=|<|>)\s*[-+A-Za-z_\"`0-9])",
    flags=re.IGNORECASE,
)


class StudentFeedbackError(ValueError):
    """Raised when Phase 5 cannot safely render the Phase 4 contract."""


_LABELS: dict[str, dict[TeachingActionKind, str]] = {
    "zh-CN": {
        TeachingActionKind.STUDENT_BEHAVIOR: "你当前的查询行为",
        TeachingActionKind.CONFLICT_WITNESS: "冲突与物证",
        TeachingActionKind.REPAIR_REFLECTION: "修改前的检查方向",
        TeachingActionKind.SOCRATIC_QUESTION: "请思考",
    },
    "zh-TW": {
        TeachingActionKind.STUDENT_BEHAVIOR: "你目前的查詢行為",
        TeachingActionKind.CONFLICT_WITNESS: "衝突與物證",
        TeachingActionKind.REPAIR_REFLECTION: "修改前的檢查方向",
        TeachingActionKind.SOCRATIC_QUESTION: "請思考",
    },
    "en": {
        TeachingActionKind.STUDENT_BEHAVIOR: "What your query currently does",
        TeachingActionKind.CONFLICT_WITNESS: "Conflict and witness",
        TeachingActionKind.REPAIR_REFLECTION: "A check before revising",
        TeachingActionKind.SOCRATIC_QUESTION: "Question to consider",
    },
}


@dataclass(frozen=True, slots=True)
class StudentFeedbackArtifact:
    status: str
    renderer: str
    feedback_source: str
    feedback_status: str
    degradation_code: str | None
    answer_revealed: bool
    language: str
    delivered_support_level: int
    text: str
    segment_count: int
    content_digest: str

    @property
    def public_generation_source(self) -> str:
        if self.feedback_source == LLM_FEEDBACK_SOURCE:
            return PUBLIC_LLM_GENERATION_SOURCE
        return PUBLIC_GENERATION_SOURCE

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "schema_version": STUDENT_FEEDBACK_SCHEMA_VERSION,
            "policy_version": STUDENT_FEEDBACK_POLICY_VERSION,
            "status": self.status,
            "renderer": self.renderer,
            "feedback_source": self.feedback_source,
            "feedback_status": self.feedback_status,
            "degradation_code": self.degradation_code,
            "answer_revealed": self.answer_revealed,
            "language": self.language,
            "delivered_support_level": self.delivered_support_level,
            "segment_count": self.segment_count,
            "content_digest": self.content_digest,
            "content_bytes": len(self.text.encode("utf-8")),
        }

    def to_public_dict(self) -> dict[str, Any]:
        """Expose delivery metadata without the text digest or internal status."""

        return {
            "schema_version": STUDENT_FEEDBACK_SCHEMA_VERSION,
            "policy_version": STUDENT_FEEDBACK_POLICY_VERSION,
            "generation_source": self.public_generation_source,
            "feedback_status": self.feedback_status,
            "delivered_support_level": self.delivered_support_level,
        }


def _delivery_status(plan: TeachingActionPlan) -> tuple[str, str | None]:
    if plan.status == "ADAPTIVE_READY":
        return "PRIMARY", None
    if plan.status == "DIAGNOSTIC_FALLBACK":
        return "FALLBACK", "NO_TRUSTED_TEACHING_TARGET"
    if "DEGRADED" in plan.status:
        return "FALLBACK", plan.status
    return "BYPASS", None


def _render_segments(plan: TeachingActionPlan) -> str:
    if len(plan.actions) == 1 and plan.actions[0].kind in {
        TeachingActionKind.ACCEPTANCE,
        TeachingActionKind.SYSTEM_NOTICE,
    }:
        return plan.actions[0].text

    labels = _LABELS[plan.language]
    segments: list[str] = []
    for index, action in enumerate(plan.actions, start=1):
        label = labels.get(action.kind)
        if label is None:
            raise StudentFeedbackError("unsupported teaching action kind")
        segments.append(f"{index}. {label}：{action.text}")
    return "\n\n".join(segments)


def render_student_feedback(plan: TeachingActionPlan) -> StudentFeedbackArtifact:
    """Render exactly the approved Phase 4 fragments and no new answer facts."""

    if not isinstance(plan, TeachingActionPlan):
        raise StudentFeedbackError("plan must be a TeachingActionPlan")
    text = _render_segments(plan).strip()
    encoded = text.encode("utf-8")
    if not text or len(encoded) > MAX_FEEDBACK_BYTES:
        raise StudentFeedbackError("rendered feedback is empty or too large")
    feedback_status, degradation_code = _delivery_status(plan)
    return StudentFeedbackArtifact(
        status="RENDERED",
        renderer=DETERMINISTIC_RENDERER,
        feedback_source="PHASE5_LOCAL_TEMPLATE",
        feedback_status=feedback_status,
        degradation_code=degradation_code,
        answer_revealed=False,
        language=plan.language,
        delivered_support_level=plan.delivered_support_level,
        text=text,
        segment_count=len(plan.actions),
        content_digest=sha256(encoded).hexdigest(),
    )


def render_llm_student_feedback(
    plan: TeachingActionPlan,
    replacements: Any,
) -> StudentFeedbackArtifact:
    """Render a validated LLM segment map without changing Phase 4 structure.

    ``replacements`` is intentionally duck-typed so the provider module does
    not become part of this low-level renderer's public API.  It must expose a
    ``text_by_action_id()`` method returning exactly one string per approved
    action.  Fixed system notices, acceptance text, and Socratic questions are
    checked again here even though the provider validator already checked them.
    """

    if not isinstance(plan, TeachingActionPlan):
        raise StudentFeedbackError("plan must be a TeachingActionPlan")
    getter = getattr(replacements, "text_by_action_id", None)
    values = getter() if callable(getter) else None
    if not isinstance(values, dict):
        raise StudentFeedbackError("LLM replacements must provide an action map")
    expected_ids = [action.action_id for action in plan.actions]
    if set(values) != set(expected_ids):
        raise StudentFeedbackError("LLM replacements do not match the Phase 4 plan")
    fixed_kinds = {
        TeachingActionKind.SOCRATIC_QUESTION,
        TeachingActionKind.ACCEPTANCE,
        TeachingActionKind.SYSTEM_NOTICE,
    }
    replaced: list[tuple[TeachingActionKind, str]] = []
    for action in plan.actions:
        text = values.get(action.action_id)
        if not isinstance(text, str) or not text.strip() or len(text) > 1200:
            raise StudentFeedbackError("LLM replacement text is invalid")
        text = text.strip()
        if action.kind in fixed_kinds and text != action.text:
            raise StudentFeedbackError("LLM may not alter fixed teaching actions")
        if (
            action.kind not in fixed_kinds
            and (";" in text or _LLM_TEXT_FORBIDDEN.search(text))
        ):
            raise StudentFeedbackError("LLM replacement contains SQL-shaped text")
        replaced.append((action.kind, text))

    if len(replaced) == 1 and replaced[0][0] in {
        TeachingActionKind.ACCEPTANCE,
        TeachingActionKind.SYSTEM_NOTICE,
    }:
        text = replaced[0][1]
    else:
        labels = _LABELS[plan.language]
        segments: list[str] = []
        for index, (action, (_, replacement)) in enumerate(
            zip(plan.actions, replaced),
            start=1,
        ):
            label = labels.get(action.kind)
            if label is None:
                raise StudentFeedbackError("unsupported teaching action kind")
            segments.append(f"{index}. {label}：{replacement}")
        text = "\n\n".join(segments)

    text = text.strip()
    encoded = text.encode("utf-8")
    if not text or len(encoded) > MAX_FEEDBACK_BYTES:
        raise StudentFeedbackError("LLM feedback is empty or too large")
    feedback_status, degradation_code = _delivery_status(plan)
    return StudentFeedbackArtifact(
        status="RENDERED",
        renderer=LLM_RENDERER,
        feedback_source=LLM_FEEDBACK_SOURCE,
        feedback_status=feedback_status,
        degradation_code=degradation_code,
        answer_revealed=False,
        language=plan.language,
        delivered_support_level=plan.delivered_support_level,
        text=text,
        segment_count=len(plan.actions),
        content_digest=sha256(encoded).hexdigest(),
    )


def render_emergency_feedback(plan: TeachingActionPlan) -> StudentFeedbackArtifact:
    """Minimal independent renderer for an already-degraded one-action plan.

    This path intentionally does not delegate to ``render_student_feedback``:
    a failure in the primary renderer must not make its own fallback
    unreachable.  Only a single local ``SYSTEM_NOTICE`` action is accepted.
    """

    if not isinstance(plan, TeachingActionPlan):
        raise StudentFeedbackError("plan must be a TeachingActionPlan")
    if (
        len(plan.actions) != 1
        or plan.actions[0].kind is not TeachingActionKind.SYSTEM_NOTICE
        or plan.delivered_support_level != 1
        or "DEGRADED" not in plan.status
    ):
        raise StudentFeedbackError("emergency feedback requires a degraded L1 notice")
    text = plan.actions[0].text.strip()
    encoded = text.encode("utf-8")
    if not text or len(encoded) > MAX_FEEDBACK_BYTES:
        raise StudentFeedbackError("emergency feedback is empty or too large")
    return StudentFeedbackArtifact(
        status="RENDERED",
        renderer="EMERGENCY_SAFE_TEMPLATE",
        feedback_source="PHASE5_EMERGENCY_TEMPLATE",
        feedback_status="FALLBACK",
        degradation_code=plan.status,
        answer_revealed=False,
        language=plan.language,
        delivered_support_level=1,
        text=text,
        segment_count=1,
        content_digest=sha256(encoded).hexdigest(),
    )


def build_teaching_support_summary(
    plan: TeachingActionPlan,
    artifact: StudentFeedbackArtifact,
) -> dict[str, Any]:
    """Build the complete learner-safe Phase 4/5 delivery contract."""

    if artifact.delivered_support_level != plan.delivered_support_level:
        raise StudentFeedbackError("Phase 4/5 delivered support levels disagree")
    if artifact.language != plan.language:
        raise StudentFeedbackError("Phase 4/5 feedback languages disagree")
    if artifact.answer_revealed:
        raise StudentFeedbackError("learner teaching support cannot reveal the answer")
    if plan.support_recommendation_applied:
        status = "APPLIED"
    elif plan.recommended_support_level is not None:
        status = "OVERRIDDEN"
    else:
        status = "NOT_APPLICABLE"
    return {
        "schema_version": TEACHING_SUPPORT_SCHEMA_VERSION,
        "status": status,
        "language": plan.language,
        "recommended_support_level": plan.recommended_support_level,
        "delivered_support_level": plan.delivered_support_level,
        "support_recommendation_applied": plan.support_recommendation_applied,
        "generation_source": artifact.public_generation_source,
        "focused_error_count": 1 if plan.adaptive_target_selected else 0,
        "answer_revealed": artifact.answer_revealed,
        "support_policy_version": plan.support_policy_version,
        "action_policy_version": TEACHING_ACTION_POLICY_VERSION,
        "feedback_policy_version": STUDENT_FEEDBACK_POLICY_VERSION,
        "feedback_status": artifact.feedback_status,
    }


__all__ = [
    "DETERMINISTIC_RENDERER",
    "LLM_FEEDBACK_SOURCE",
    "LLM_RENDERER",
    "PUBLIC_GENERATION_SOURCE",
    "PUBLIC_LLM_GENERATION_SOURCE",
    "MAX_FEEDBACK_BYTES",
    "STUDENT_FEEDBACK_POLICY_VERSION",
    "STUDENT_FEEDBACK_SCHEMA_VERSION",
    "StudentFeedbackArtifact",
    "StudentFeedbackError",
    "build_teaching_support_summary",
    "render_emergency_feedback",
    "render_llm_student_feedback",
    "render_student_feedback",
]
