"""Persistence boundary for immutable Phase 4/5 teaching audits."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import re
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.submission import Submission
from models.submission_teaching_audit import (
    SUBMISSION_TEACHING_AUDIT_SCHEMA_VERSION,
    SubmissionTeachingAudit,
    SupportRecommendationStatus,
    TeachingFeedbackStatus,
)


MAX_ACTION_SNAPSHOT_BYTES = 128 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DEGRADATION_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _enum_value(value: str | Any, enum_type: type[Any], *, field_name: str) -> str:
    if isinstance(value, enum_type):
        value = value.value
    if not isinstance(value, str) or value not in {item.value for item in enum_type}:
        raise ValueError(f"{field_name} is not supported")
    return value


def _required_string(value: Any, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} must contain 1..{max_length} characters")
    return normalized


def _optional_string(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> str | None:
    if value is None:
        return None
    return _required_string(value, field_name=field_name, max_length=max_length)


def _support_level(value: Any, *, field_name: str, nullable: bool) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 4:
        raise ValueError(f"{field_name} must be an integer in [1, 4]")
    return value


def _canonical_action_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("action_snapshot must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ValueError("action_snapshot keys must be strings")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("action_snapshot must be finite JSON data") from exc
    if len(encoded) > MAX_ACTION_SNAPSHOT_BYTES:
        raise ValueError("action_snapshot exceeds the audit byte limit")
    canonical = json.loads(encoded.decode("utf-8"))
    if not isinstance(canonical, dict):
        raise ValueError("action_snapshot must encode a JSON object")
    return canonical


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmissionTeachingAuditInput:
    """Validated audit payload produced from Phase 4/5 artifacts."""

    recommendation_status: SupportRecommendationStatus | str
    support_need: float | None
    recommended_support_level: int | None
    delivered_support_level: int
    support_recommendation_applied: bool
    support_policy_version: str | None
    action_policy_version: str
    feedback_policy_version: str
    generation_source: str
    feedback_status: TeachingFeedbackStatus | str
    degradation_code: str | None
    answer_revealed: bool
    feedback_sha256: str
    action_snapshot: Mapping[str, Any]
    target_candidate_id: str | None = None
    target_rule_id: str | None = None
    target_observation_id: str | None = None
    target_skill_id: str | None = None
    target_taxonomy_version: str | None = None
    target_logical_stage: str | None = None
    target_source_role: str | None = None
    target_evidence_grade: str | None = None
    audit_schema_version: str = SUBMISSION_TEACHING_AUDIT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        recommendation_status = _enum_value(
            self.recommendation_status,
            SupportRecommendationStatus,
            field_name="recommendation_status",
        )
        feedback_status = _enum_value(
            self.feedback_status,
            TeachingFeedbackStatus,
            field_name="feedback_status",
        )
        recommended = _support_level(
            self.recommended_support_level,
            field_name="recommended_support_level",
            nullable=True,
        )
        delivered = _support_level(
            self.delivered_support_level,
            field_name="delivered_support_level",
            nullable=False,
        )
        if type(self.support_recommendation_applied) is not bool:
            raise TypeError("support_recommendation_applied must be bool")

        support_need = self.support_need
        if support_need is not None:
            if (
                isinstance(support_need, bool)
                or not isinstance(support_need, (int, float))
                or not math.isfinite(float(support_need))
                or not 0.0 <= float(support_need) <= 1.0
            ):
                raise ValueError("support_need must be a finite number in [0, 1]")
            support_need = float(support_need)
        if (recommended is None) is not (support_need is None):
            raise ValueError(
                "recommended_support_level and support_need must be present together"
            )

        if recommendation_status == SupportRecommendationStatus.APPLIED.value:
            if (
                not self.support_recommendation_applied
                or recommended is None
                or recommended != delivered
            ):
                raise ValueError("APPLIED recommendation metadata is inconsistent")
        elif recommendation_status == SupportRecommendationStatus.OVERRIDDEN.value:
            if self.support_recommendation_applied or recommended is None:
                raise ValueError("OVERRIDDEN recommendation metadata is inconsistent")
        elif self.support_recommendation_applied or recommended is not None:
            raise ValueError(
                "NOT_APPLICABLE recommendation metadata is inconsistent"
            )

        support_policy_version = _optional_string(
            self.support_policy_version,
            field_name="support_policy_version",
            max_length=64,
        )
        if recommended is not None and support_policy_version is None:
            raise ValueError(
                "a support recommendation requires support_policy_version"
            )

        action_policy_version = _required_string(
            self.action_policy_version,
            field_name="action_policy_version",
            max_length=64,
        )
        feedback_policy_version = _required_string(
            self.feedback_policy_version,
            field_name="feedback_policy_version",
            max_length=64,
        )
        generation_source = _required_string(
            self.generation_source,
            field_name="generation_source",
            max_length=64,
        )
        audit_schema_version = _required_string(
            self.audit_schema_version,
            field_name="audit_schema_version",
            max_length=64,
        )

        degradation_code = _optional_string(
            self.degradation_code,
            field_name="degradation_code",
            max_length=64,
        )
        if type(self.answer_revealed) is not bool:
            raise TypeError("answer_revealed must be bool")
        if (
            feedback_status == TeachingFeedbackStatus.FALLBACK.value
        ) is not (degradation_code is not None):
            raise ValueError(
                "only FALLBACK feedback may carry a degradation_code, and it is required"
            )
        if degradation_code is not None and not _DEGRADATION_CODE_RE.fullmatch(
            degradation_code
        ):
            raise ValueError("degradation_code must be a stable uppercase code")

        digest = _required_string(
            self.feedback_sha256,
            field_name="feedback_sha256",
            max_length=64,
        ).lower()
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError("feedback_sha256 must be a lowercase SHA-256 digest")

        target_fields = {
            "target_candidate_id": _optional_string(
                self.target_candidate_id,
                field_name="target_candidate_id",
                max_length=128,
            ),
            "target_rule_id": _optional_string(
                self.target_rule_id,
                field_name="target_rule_id",
                max_length=64,
            ),
            "target_observation_id": _optional_string(
                self.target_observation_id,
                field_name="target_observation_id",
                max_length=128,
            ),
            "target_skill_id": _optional_string(
                self.target_skill_id,
                field_name="target_skill_id",
                max_length=128,
            ),
            "target_taxonomy_version": _optional_string(
                self.target_taxonomy_version,
                field_name="target_taxonomy_version",
                max_length=64,
            ),
            "target_logical_stage": _optional_string(
                self.target_logical_stage,
                field_name="target_logical_stage",
                max_length=32,
            ),
            "target_source_role": _optional_string(
                self.target_source_role,
                field_name="target_source_role",
                max_length=32,
            ),
            "target_evidence_grade": _optional_string(
                self.target_evidence_grade,
                field_name="target_evidence_grade",
                max_length=32,
            ),
        }

        snapshot = _canonical_action_snapshot(self.action_snapshot)
        expected_snapshot_values = {
            "policy_version": action_policy_version,
            "support_need": support_need,
            "support_policy_version": support_policy_version,
            "recommended_support_level": recommended,
            "delivered_support_level": delivered,
            "support_recommendation_applied": self.support_recommendation_applied,
            **target_fields,
        }
        for key, expected in expected_snapshot_values.items():
            if key not in snapshot or snapshot[key] != expected:
                raise ValueError(f"action_snapshot conflicts with {key}")

        object.__setattr__(self, "recommendation_status", recommendation_status)
        object.__setattr__(self, "feedback_status", feedback_status)
        object.__setattr__(self, "support_need", support_need)
        object.__setattr__(self, "recommended_support_level", recommended)
        object.__setattr__(self, "delivered_support_level", delivered)
        object.__setattr__(self, "support_policy_version", support_policy_version)
        object.__setattr__(self, "action_policy_version", action_policy_version)
        object.__setattr__(self, "feedback_policy_version", feedback_policy_version)
        object.__setattr__(self, "generation_source", generation_source)
        object.__setattr__(self, "degradation_code", degradation_code)
        object.__setattr__(self, "feedback_sha256", digest)
        object.__setattr__(self, "action_snapshot", snapshot)
        object.__setattr__(self, "audit_schema_version", audit_schema_version)
        for field_name, value in target_fields.items():
            object.__setattr__(self, field_name, value)

    def to_model_kwargs(self) -> dict[str, Any]:
        return {
            "audit_schema_version": self.audit_schema_version,
            "support_need": self.support_need,
            "recommended_support_level": self.recommended_support_level,
            "delivered_support_level": self.delivered_support_level,
            "support_recommendation_applied": self.support_recommendation_applied,
            "recommendation_status": self.recommendation_status,
            "support_policy_version": self.support_policy_version,
            "action_policy_version": self.action_policy_version,
            "feedback_policy_version": self.feedback_policy_version,
            "generation_source": self.generation_source,
            "feedback_status": self.feedback_status,
            "degradation_code": self.degradation_code,
            "answer_revealed": self.answer_revealed,
            "target_candidate_id": self.target_candidate_id,
            "target_rule_id": self.target_rule_id,
            "target_observation_id": self.target_observation_id,
            "target_skill_id": self.target_skill_id,
            "target_taxonomy_version": self.target_taxonomy_version,
            "target_logical_stage": self.target_logical_stage,
            "target_source_role": self.target_source_role,
            "target_evidence_grade": self.target_evidence_grade,
            "feedback_sha256": self.feedback_sha256,
            "action_snapshot": self.action_snapshot,
        }


class SubmissionTeachingAuditRepository:
    """Create or validate one immutable teaching audit in the caller's tx."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_submission_id(
        self,
        submission_id: int,
        *,
        for_update: bool = False,
    ) -> SubmissionTeachingAudit | None:
        stmt = select(SubmissionTeachingAudit).where(
            SubmissionTeachingAudit.submission_id == submission_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    async def create_once_or_validate(
        self,
        submission_id: int,
        audit: SubmissionTeachingAuditInput,
    ) -> SubmissionTeachingAudit:
        """Persist the audit once, rejecting inconsistent idempotent reuse.

        The method never commits.  The route must commit this row atomically
        with Submission, chat, BKT, and the response snapshot.
        """

        if isinstance(submission_id, bool) or not isinstance(submission_id, int):
            raise TypeError("submission_id must be an integer")
        if submission_id <= 0:
            raise ValueError("submission_id must be positive")
        if not isinstance(audit, SubmissionTeachingAuditInput):
            raise TypeError("audit must be SubmissionTeachingAuditInput")

        # Lock the parent first so concurrent writers follow one lock order and
        # a retry performs a MySQL current read after waiting for the winner.
        submission = await self.session.scalar(
            select(Submission)
            .where(Submission.id == submission_id)
            .with_for_update()
        )
        if submission is None:
            raise ValueError("teaching audit submission does not exist")
        if submission.hint_level != audit.delivered_support_level:
            raise ValueError(
                "delivered support level does not match Submission.hint_level"
            )
        if not isinstance(submission.ai_hint, str):
            raise ValueError("audited Submission.ai_hint must be text")
        actual_digest = sha256(submission.ai_hint.encode("utf-8")).hexdigest()
        if actual_digest != audit.feedback_sha256:
            raise ValueError("feedback digest does not match Submission.ai_hint")

        existing = await self.get_by_submission_id(
            submission_id,
            for_update=True,
        )
        expected = audit.to_model_kwargs()
        if existing is not None:
            if any(getattr(existing, key) != value for key, value in expected.items()):
                raise ValueError("persisted teaching audit conflicts with retry")
            return existing

        row = SubmissionTeachingAudit(
            submission_id=submission_id,
            **expected,
        )
        self.session.add(row)
        await self.session.flush()
        return row


__all__ = [
    "MAX_ACTION_SNAPSHOT_BYTES",
    "SubmissionTeachingAuditInput",
    "SubmissionTeachingAuditRepository",
]
