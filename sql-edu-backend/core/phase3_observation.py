"""Build trusted, bounded Phase 3 skill observations.

This module is the only bridge from Phase 2/Q-matrix evidence into a future
learning-state update.  It remains a pure function: no database writes, BKT
updates, scheduling decisions, or answer-side SQL are performed here.

Positive observations require both an operationally accepted Phase 2 verdict
and an authoritative PRIMARY Q-matrix row explicitly marked observable on a
correct submission.  Negative observations are authorized exclusively by the
frozen rule-to-atomic-skill projector in :mod:`core.phase3_skill_catalog`.
The display-only ``candidate.knowledge_points`` field is never consulted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
from typing import Any

from core.error_diagnosis import (
    DIAGNOSIS_VERSION as PHASE2_DIAGNOSIS_VERSION,
    LOGICAL_STAGE_ORDER,
    PUBLIC_SCHEMA_VERSION as PHASE2_PUBLIC_SCHEMA_VERSION,
    RULE_CATALOG_VERSION as PHASE2_RULE_CATALOG_VERSION,
)
from core.phase3_skill_catalog import (
    ATOMIC_SKILL_TAXONOMY_VERSION,
    RULE_SKILL_CATALOG,
    RULE_SKILL_MAP_VERSION,
    project_phase2_skill_candidates,
)
from core.sql_knowledge_points import get_knowledge_point_by_id
from models.question_skill import SQL_KNOWLEDGE_TAXONOMY_VERSION


OBSERVATION_SCHEMA_VERSION = "phase3.trusted_skill_observations.v1"
QUESTION_QMATRIX_SOURCE_VERSION = "question_skill_mapping.v1"
MAX_QUESTION_SKILLS = 8

_ATOMIC_SKILL_IDS = frozenset(item.skill_id for item in RULE_SKILL_CATALOG)
_LOGICAL_STAGES = frozenset(LOGICAL_STAGE_ORDER)
_TRUSTED_QMATRIX_PROVENANCE = frozenset(
    # A positive observation requires an authored or explicitly generated
    # assessment declaration.  An inferred row remains useful metadata, but it
    # cannot prove mastery without an explicit author/generator decision.
    {"AUTHOR_DECLARED", "GENERATED"}
)
_QMATRIX_PROVENANCE_ALIASES = {
    # Accept values emitted by the pre-v1 draft at this pure-function boundary
    # so an old read-only package is classified deterministically.  Persisted
    # Q-matrix rows and all newly emitted observations use the short v1 names.
    "AI_GENERATED": "GENERATED",
    "INFERRED_REVIEWED": "INFERRED",
}


class ObservationResult(str, Enum):
    CORRECT = "CORRECT"
    INCORRECT = "INCORRECT"


class ObservationSource(str, Enum):
    QUESTION_QMATRIX = "QUESTION_QMATRIX"
    PHASE2_RULE = "PHASE2_RULE"


class ObservationSkipReason(str, Enum):
    INPUT_NOT_MAPPING = "INPUT_NOT_MAPPING"
    PHASE2_SCHEMA_VERSION_UNSUPPORTED = "PHASE2_SCHEMA_VERSION_UNSUPPORTED"
    PHASE2_DIAGNOSIS_VERSION_UNSUPPORTED = (
        "PHASE2_DIAGNOSIS_VERSION_UNSUPPORTED"
    )
    PHASE2_RULE_CATALOG_VERSION_UNSUPPORTED = (
        "PHASE2_RULE_CATALOG_VERSION_UNSUPPORTED"
    )
    PHASE2_VERDICT_UNDECIDED = "PHASE2_VERDICT_UNDECIDED"
    PHASE2_VERDICT_UNSUPPORTED = "PHASE2_VERDICT_UNSUPPORTED"
    PHASE2_CORRECT_CONTRACT_INVALID = "PHASE2_CORRECT_CONTRACT_INVALID"
    ANSWER_REVEALED = "ANSWER_REVEALED"
    ANSWER_REVEALED_INVALID = "ANSWER_REVEALED_INVALID"
    SKIP_NO_ASSESSMENT_MAP = "SKIP_NO_ASSESSMENT_MAP"
    # Deprecated internal vocabulary.  New no-map results use the explicit
    # status above so callers can distinguish absent assessment design from a
    # malformed or unsupported Q-matrix.
    QMATRIX_UNMAPPED = "QMATRIX_UNMAPPED"
    QMATRIX_INPUT_INVALID = "QMATRIX_INPUT_INVALID"
    QMATRIX_LIMIT_EXCEEDED = "QMATRIX_LIMIT_EXCEEDED"
    QMATRIX_ITEM_INVALID = "QMATRIX_ITEM_INVALID"
    QMATRIX_SUPPORTING = "QMATRIX_SUPPORTING"
    QMATRIX_NOT_OBSERVABLE = "QMATRIX_NOT_OBSERVABLE"
    QMATRIX_PROVENANCE_UNTRUSTED = "QMATRIX_PROVENANCE_UNTRUSTED"
    QMATRIX_TAXONOMY_UNSUPPORTED = "QMATRIX_TAXONOMY_UNSUPPORTED"
    QMATRIX_SKILL_UNKNOWN = "QMATRIX_SKILL_UNKNOWN"
    DUPLICATE_SKILL_OBSERVATION = "DUPLICATE_SKILL_OBSERVATION"
    PHASE2_LOGICAL_STAGE_INVALID = "PHASE2_LOGICAL_STAGE_INVALID"


@dataclass(frozen=True, slots=True)
class ObservationSpec:
    """Immutable evidence specification for one later BKT update.

    ``observation_id`` is a deterministic content identity, not a submission
    event id.  A persistence layer should combine it with its submission/event
    identifier when enforcing write idempotency.
    """

    observation_id: str
    taxonomy_version: str
    skill_id: str
    result: ObservationResult
    source: ObservationSource
    source_version: str
    evidence_grade: str
    phase2_candidate_id: str | None = None
    phase2_rule_id: str | None = None
    source_role: str | None = None
    source_index: int | None = None
    logical_stage: str | None = None
    teaching_stage: str | None = None
    qmatrix_provenance: str | None = None
    trusted_atomic_observation: bool = False

    @property
    def is_correct(self) -> bool:
        return self.result is ObservationResult.CORRECT

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "taxonomy_version": self.taxonomy_version,
            "skill_id": self.skill_id,
            "result": self.result.value,
            "is_correct": self.is_correct,
            "source": self.source.value,
            "source_version": self.source_version,
            "evidence_grade": self.evidence_grade,
            "phase2_candidate_id": self.phase2_candidate_id,
            "phase2_rule_id": self.phase2_rule_id,
            "source_role": self.source_role,
            "source_index": self.source_index,
            "logical_stage": self.logical_stage,
            "teaching_stage": self.teaching_stage,
            "qmatrix_provenance": self.qmatrix_provenance,
            "trusted_atomic_observation": self.trusted_atomic_observation,
        }

    def to_persistence_kwargs(self) -> dict[str, Any]:
        """Return the explicit adapter payload for persistence input.

        The persistence repository intentionally uses storage-oriented names.
        Keeping this mapping next to the trusted observation contract prevents
        integration code from silently dropping causal provenance fields.
        """

        return {
            "taxonomy_version": self.taxonomy_version,
            "skill_id": self.skill_id,
            "is_correct": self.is_correct,
            "source_type": self.source.value,
            "source_version": self.source_version,
            "evidence_grade": self.evidence_grade,
            "phase2_candidate_id": self.phase2_candidate_id,
            "rule_id": self.phase2_rule_id,
            "source_role": self.source_role,
            "logical_stage": self.logical_stage,
            "source_provenance": self.qmatrix_provenance,
        }


@dataclass(frozen=True, slots=True)
class SkippedObservation:
    reason_code: str
    source: str
    source_index: int | None = None
    taxonomy_version: str | None = None
    skill_id: str | None = None
    phase2_candidate_id: str | None = None
    phase2_rule_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "source": self.source,
            "source_index": self.source_index,
            "taxonomy_version": self.taxonomy_version,
            "skill_id": self.skill_id,
            "phase2_candidate_id": self.phase2_candidate_id,
            "phase2_rule_id": self.phase2_rule_id,
        }


@dataclass(frozen=True, slots=True)
class ObservationBuildResult:
    verdict: str
    status: str
    observations: tuple[ObservationSpec, ...] = ()
    skipped: tuple[SkippedObservation, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "verdict": self.verdict,
            "status": self.status,
            "observation_count": len(self.observations),
            "observations": [item.to_dict() for item in self.observations],
            "skip_count": len(self.skipped),
            "skipped": [item.to_dict() for item in self.skipped],
        }


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _enum_text(value: Any) -> str | None:
    raw = getattr(value, "value", value)
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text if text else None


def _bounded_text(value: Any, *, maximum: int) -> str | None:
    text = _enum_text(value)
    if text is None or len(text) > maximum:
        return None
    return text


def _qmatrix_provenance(value: Any) -> str | None:
    text = _bounded_text(value, maximum=24)
    if text is None:
        return None
    return _QMATRIX_PROVENANCE_ALIASES.get(text, text)


def _status(
    observations: Sequence[ObservationSpec],
    skipped: Sequence[SkippedObservation],
) -> str:
    if observations:
        return "READY_WITH_SKIPS" if skipped else "READY"
    return "SKIPPED"


def _result(
    *,
    verdict: str,
    observations: Sequence[ObservationSpec] = (),
    skipped: Sequence[SkippedObservation] = (),
    status_override: str | None = None,
) -> ObservationBuildResult:
    return ObservationBuildResult(
        verdict=verdict,
        status=status_override or _status(observations, skipped),
        observations=tuple(observations),
        skipped=tuple(skipped),
    )


def _skip_result(
    verdict: str,
    reason: ObservationSkipReason | str,
    *,
    source: str = "PACKAGE",
    status_override: str | None = None,
) -> ObservationBuildResult:
    code = reason.value if isinstance(reason, ObservationSkipReason) else reason
    return _result(
        verdict=verdict,
        skipped=(SkippedObservation(reason_code=code, source=source),),
        status_override=status_override,
    )


def _phase2_versions_valid(package: Mapping[str, Any]) -> ObservationSkipReason | None:
    if package.get("schema_version") != PHASE2_PUBLIC_SCHEMA_VERSION:
        return ObservationSkipReason.PHASE2_SCHEMA_VERSION_UNSUPPORTED
    if package.get("diagnosis_version") != PHASE2_DIAGNOSIS_VERSION:
        return ObservationSkipReason.PHASE2_DIAGNOSIS_VERSION_UNSUPPORTED
    if package.get("rule_catalog_version") != PHASE2_RULE_CATALOG_VERSION:
        return ObservationSkipReason.PHASE2_RULE_CATALOG_VERSION_UNSUPPORTED
    return None


def _correct_contract_valid(package: Mapping[str, Any]) -> bool:
    if package.get("diagnosis_status") != "OPERATIONALLY_ACCEPTED":
        return False
    if package.get("primary") is not None:
        return False
    secondary = package.get("secondary")
    if not isinstance(secondary, (list, tuple)) or secondary:
        return False
    secondary_count = package.get("secondary_count")
    if (
        not isinstance(secondary_count, int)
        or isinstance(secondary_count, bool)
        or secondary_count != 0
    ):
        return False
    phase1 = package.get("phase1")
    return bool(
        isinstance(phase1, Mapping)
        and phase1.get("judge_status") == "CORRECT"
        and phase1.get("equivalence_conclusion") == "NO_COUNTEREXAMPLE_FOUND"
    )


def _observation_id(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"observation_{sha256(canonical.encode('utf-8')).hexdigest()[:20]}"


def _skill_is_known(taxonomy_version: str, skill_id: str) -> bool:
    if taxonomy_version == SQL_KNOWLEDGE_TAXONOMY_VERSION:
        return get_knowledge_point_by_id(skill_id) is not None
    if taxonomy_version == ATOMIC_SKILL_TAXONOMY_VERSION:
        return skill_id in _ATOMIC_SKILL_IDS
    return False


def _positive_observations(
    package: Mapping[str, Any],
    question_skills: Sequence[Any] | None,
    *,
    answer_revealed: bool,
) -> ObservationBuildResult:
    if not _correct_contract_valid(package):
        return _skip_result(
            "CORRECT",
            ObservationSkipReason.PHASE2_CORRECT_CONTRACT_INVALID,
        )
    if answer_revealed:
        return _skip_result(
            "CORRECT",
            ObservationSkipReason.ANSWER_REVEALED,
            source=ObservationSource.QUESTION_QMATRIX.value,
        )
    if question_skills is None:
        return _skip_result(
            "CORRECT",
            ObservationSkipReason.SKIP_NO_ASSESSMENT_MAP,
            source=ObservationSource.QUESTION_QMATRIX.value,
            status_override=ObservationSkipReason.SKIP_NO_ASSESSMENT_MAP.value,
        )
    if (
        not isinstance(question_skills, Sequence)
        or isinstance(question_skills, (str, bytes, bytearray, Mapping))
    ):
        return _skip_result(
            "CORRECT",
            ObservationSkipReason.QMATRIX_INPUT_INVALID,
            source=ObservationSource.QUESTION_QMATRIX.value,
        )
    if len(question_skills) > MAX_QUESTION_SKILLS:
        return _skip_result(
            "CORRECT",
            ObservationSkipReason.QMATRIX_LIMIT_EXCEEDED,
            source=ObservationSource.QUESTION_QMATRIX.value,
        )
    if not question_skills:
        return _skip_result(
            "CORRECT",
            ObservationSkipReason.SKIP_NO_ASSESSMENT_MAP,
            source=ObservationSource.QUESTION_QMATRIX.value,
            status_override=ObservationSkipReason.SKIP_NO_ASSESSMENT_MAP.value,
        )

    candidates: list[ObservationSpec] = []
    skipped: list[SkippedObservation] = []
    for index, raw in enumerate(question_skills):
        skill_id = _bounded_text(_field(raw, "skill_id"), maximum=128)
        taxonomy_version = _bounded_text(
            _field(raw, "taxonomy_version"), maximum=64
        )
        role = _bounded_text(_field(raw, "role"), maximum=16)
        provenance = _qmatrix_provenance(_field(raw, "provenance"))
        observable = _field(raw, "observable_on_correct")
        safe_identity = {
            "source_index": index,
            "taxonomy_version": taxonomy_version,
            "skill_id": skill_id,
        }

        def skip(reason: ObservationSkipReason) -> None:
            skipped.append(
                SkippedObservation(
                    reason_code=reason.value,
                    source=ObservationSource.QUESTION_QMATRIX.value,
                    **safe_identity,
                )
            )

        if None in {skill_id, taxonomy_version, role, provenance}:
            skip(ObservationSkipReason.QMATRIX_ITEM_INVALID)
            continue
        assert skill_id is not None
        assert taxonomy_version is not None
        assert role is not None
        assert provenance is not None
        if role != "PRIMARY":
            skip(ObservationSkipReason.QMATRIX_SUPPORTING)
            continue
        if observable is not True:
            skip(ObservationSkipReason.QMATRIX_NOT_OBSERVABLE)
            continue
        if provenance not in _TRUSTED_QMATRIX_PROVENANCE:
            skip(ObservationSkipReason.QMATRIX_PROVENANCE_UNTRUSTED)
            continue
        if taxonomy_version not in {
            SQL_KNOWLEDGE_TAXONOMY_VERSION,
            ATOMIC_SKILL_TAXONOMY_VERSION,
        }:
            skip(ObservationSkipReason.QMATRIX_TAXONOMY_UNSUPPORTED)
            continue
        if not _skill_is_known(taxonomy_version, skill_id):
            skip(ObservationSkipReason.QMATRIX_SKILL_UNKNOWN)
            continue

        identity = {
            "taxonomy_version": taxonomy_version,
            "skill_id": skill_id,
            "result": ObservationResult.CORRECT.value,
            "source": ObservationSource.QUESTION_QMATRIX.value,
            "source_version": QUESTION_QMATRIX_SOURCE_VERSION,
            "qmatrix_provenance": provenance,
        }
        candidates.append(
            ObservationSpec(
                observation_id=_observation_id(identity),
                taxonomy_version=taxonomy_version,
                skill_id=skill_id,
                result=ObservationResult.CORRECT,
                source=ObservationSource.QUESTION_QMATRIX,
                source_version=QUESTION_QMATRIX_SOURCE_VERSION,
                evidence_grade="OPERATIONALLY_ACCEPTED",
                source_role="PRIMARY",
                source_index=index,
                qmatrix_provenance=provenance,
                trusted_atomic_observation=(
                    taxonomy_version == ATOMIC_SKILL_TAXONOMY_VERSION
                ),
            )
        )

    provenance_rank = {
        "AUTHOR_DECLARED": 0,
        "GENERATED": 2,
        "INFERRED": 99,
    }
    candidates.sort(
        key=lambda item: (
            item.taxonomy_version,
            item.skill_id,
            provenance_rank.get(item.qmatrix_provenance or "", 99),
            item.source_index if item.source_index is not None else 999,
        )
    )
    deduplicated: list[ObservationSpec] = []
    seen: set[tuple[str, str, ObservationResult]] = set()
    for item in candidates:
        key = (item.taxonomy_version, item.skill_id, item.result)
        if key in seen:
            skipped.append(
                SkippedObservation(
                    reason_code=(
                        ObservationSkipReason.DUPLICATE_SKILL_OBSERVATION.value
                    ),
                    source=ObservationSource.QUESTION_QMATRIX.value,
                    source_index=item.source_index,
                    taxonomy_version=item.taxonomy_version,
                    skill_id=item.skill_id,
                )
            )
            continue
        seen.add(key)
        deduplicated.append(item)

    skipped.sort(
        key=lambda item: (
            item.reason_code,
            item.taxonomy_version or "",
            item.skill_id or "",
            item.source_index if item.source_index is not None else 999,
        )
    )
    return _result(
        verdict="CORRECT",
        observations=deduplicated,
        skipped=skipped,
    )


def _phase2_candidate_metadata(
    package: Mapping[str, Any],
) -> dict[str, tuple[str | None, str | None]]:
    result: dict[str, tuple[str | None, str | None]] = {}
    primary = package.get("primary")
    secondary = package.get("secondary")
    raw_items: list[Any] = [primary]
    if isinstance(secondary, (list, tuple)):
        raw_items.extend(secondary)
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        candidate_id = _bounded_text(raw.get("candidate_id"), maximum=128)
        if candidate_id is None or candidate_id in result:
            continue
        logical_stage = _bounded_text(raw.get("logical_stage"), maximum=32)
        teaching_stage = _bounded_text(raw.get("stage"), maximum=8)
        result[candidate_id] = (logical_stage, teaching_stage)
    return result


def _negative_observations(
    package: Mapping[str, Any],
    *,
    answer_revealed: bool,
) -> ObservationBuildResult:
    if answer_revealed:
        return _skip_result(
            "INCORRECT",
            ObservationSkipReason.ANSWER_REVEALED,
            source=ObservationSource.PHASE2_RULE.value,
        )
    projection = project_phase2_skill_candidates(package)
    skipped = [
        SkippedObservation(
            reason_code=item.reason_code.value,
            source="PHASE2_RULE_PROJECTION",
            source_index=item.source_index,
            phase2_candidate_id=item.phase2_candidate_id,
            phase2_rule_id=item.phase2_rule_id,
        )
        for item in projection.skipped
    ]
    metadata = _phase2_candidate_metadata(package)
    observations: list[ObservationSpec] = []
    seen: set[tuple[str, str, ObservationResult]] = set()
    for candidate in projection.candidates:
        logical_stage, teaching_stage = metadata.get(
            candidate.phase2_candidate_id, (None, None)
        )
        if logical_stage not in _LOGICAL_STAGES:
            skipped.append(
                SkippedObservation(
                    reason_code=(
                        ObservationSkipReason.PHASE2_LOGICAL_STAGE_INVALID.value
                    ),
                    source=ObservationSource.PHASE2_RULE.value,
                    source_index=candidate.source_index,
                    taxonomy_version=ATOMIC_SKILL_TAXONOMY_VERSION,
                    skill_id=candidate.skill_id,
                    phase2_candidate_id=candidate.phase2_candidate_id,
                    phase2_rule_id=candidate.phase2_rule_id,
                )
            )
            continue
        key = (
            ATOMIC_SKILL_TAXONOMY_VERSION,
            candidate.skill_id,
            ObservationResult.INCORRECT,
        )
        if key in seen:
            skipped.append(
                SkippedObservation(
                    reason_code=(
                        ObservationSkipReason.DUPLICATE_SKILL_OBSERVATION.value
                    ),
                    source=ObservationSource.PHASE2_RULE.value,
                    source_index=candidate.source_index,
                    taxonomy_version=ATOMIC_SKILL_TAXONOMY_VERSION,
                    skill_id=candidate.skill_id,
                    phase2_candidate_id=candidate.phase2_candidate_id,
                    phase2_rule_id=candidate.phase2_rule_id,
                )
            )
            continue
        seen.add(key)
        identity = {
            "taxonomy_version": ATOMIC_SKILL_TAXONOMY_VERSION,
            "skill_id": candidate.skill_id,
            "result": ObservationResult.INCORRECT.value,
            "source": ObservationSource.PHASE2_RULE.value,
            "source_version": RULE_SKILL_MAP_VERSION,
            "phase2_candidate_id": candidate.phase2_candidate_id,
            "phase2_rule_id": candidate.phase2_rule_id,
        }
        observations.append(
            ObservationSpec(
                observation_id=_observation_id(identity),
                taxonomy_version=ATOMIC_SKILL_TAXONOMY_VERSION,
                skill_id=candidate.skill_id,
                result=ObservationResult.INCORRECT,
                source=ObservationSource.PHASE2_RULE,
                source_version=RULE_SKILL_MAP_VERSION,
                evidence_grade=candidate.evidence_grade,
                phase2_candidate_id=candidate.phase2_candidate_id,
                phase2_rule_id=candidate.phase2_rule_id,
                source_role=candidate.source_role,
                source_index=candidate.source_index,
                logical_stage=logical_stage,
                teaching_stage=teaching_stage,
                trusted_atomic_observation=True,
            )
        )

    role_rank = {"PRIMARY": 0, "FDP": 0, "SECONDARY": 1}
    observations.sort(
        key=lambda item: (
            role_rank.get(item.source_role or "", 99),
            item.source_index if item.source_index is not None else 999,
            item.logical_stage or "",
            item.skill_id,
            item.observation_id,
        )
    )
    skipped.sort(
        key=lambda item: (
            item.reason_code,
            item.source_index if item.source_index is not None else 999,
            item.phase2_candidate_id or "",
        )
    )
    return _result(
        verdict="INCORRECT",
        observations=observations,
        skipped=skipped,
    )


def build_skill_observations(
    diagnostic_package: Mapping[str, Any],
    question_skills: Sequence[Any] | None = None,
    *,
    answer_revealed: bool = False,
) -> ObservationBuildResult:
    """Build a bounded trusted observation set from one submission.

    The caller must pass the server-produced *public* Phase 2 package and the
    authoritative Q-matrix rows for the current question.  Client-supplied
    candidate metadata must never be routed into this function.
    """

    if not isinstance(diagnostic_package, Mapping):
        return _skip_result(
            "UNKNOWN",
            ObservationSkipReason.INPUT_NOT_MAPPING,
        )
    if type(answer_revealed) is not bool:
        return _skip_result(
            str(diagnostic_package.get("verdict") or "UNKNOWN"),
            ObservationSkipReason.ANSWER_REVEALED_INVALID,
        )
    version_error = _phase2_versions_valid(diagnostic_package)
    if version_error is not None:
        return _skip_result(
            str(diagnostic_package.get("verdict") or "UNKNOWN"),
            version_error,
        )

    verdict = diagnostic_package.get("verdict")
    if verdict == "CORRECT":
        return _positive_observations(
            diagnostic_package,
            question_skills,
            answer_revealed=answer_revealed,
        )
    if verdict == "INCORRECT":
        return _negative_observations(
            diagnostic_package,
            answer_revealed=answer_revealed,
        )
    if verdict == "UNDECIDED":
        return _skip_result(
            "UNDECIDED",
            ObservationSkipReason.PHASE2_VERDICT_UNDECIDED,
        )
    return _skip_result(
        str(verdict or "UNKNOWN"),
        ObservationSkipReason.PHASE2_VERDICT_UNSUPPORTED,
    )


# Explicit long name for integration code that wants the trust boundary in
# call sites; both names intentionally share the same implementation.
build_trusted_skill_observations = build_skill_observations


__all__ = [
    "MAX_QUESTION_SKILLS",
    "OBSERVATION_SCHEMA_VERSION",
    "QUESTION_QMATRIX_SOURCE_VERSION",
    "ObservationBuildResult",
    "ObservationResult",
    "ObservationSkipReason",
    "ObservationSource",
    "ObservationSpec",
    "SkippedObservation",
    "build_skill_observations",
    "build_trusted_skill_observations",
]
