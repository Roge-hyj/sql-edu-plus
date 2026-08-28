"""Phase 3 atomic-skill projection for trusted Phase 2 diagnoses.

This module is deliberately smaller than a learning-state model.  It binds
each frozen Phase 2 MVP rule to exactly one atomic skill and projects only
strong, root diagnostic candidates into *observation candidates*.  It does
not update BKT, infer question intent, or consume the display-only
``candidate.knowledge_points`` field.

The public Phase 2 package remains the source of fault truth.  Any contract,
version, evidence, or catalogue ambiguity fails closed with a stable reason
code so that a later Phase 3 policy can audit why no learning observation was
produced.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Any

from core.error_diagnosis import (
    DIAGNOSIS_VERSION as PHASE2_DIAGNOSIS_VERSION,
    PUBLIC_SCHEMA_VERSION as PHASE2_PUBLIC_SCHEMA_VERSION,
    RULE_CATALOG as PHASE2_RULE_CATALOG,
    RULE_CATALOG_VERSION as PHASE2_RULE_CATALOG_VERSION,
)


RULE_SKILL_MAP_VERSION = "phase3.rule_skill_map.v1"
ATOMIC_SKILL_TAXONOMY_VERSION = "phase3.atomic_sql_skills.v1"
PROJECTION_SCHEMA_VERSION = "phase3.skill-observation-candidates.v1"
STRONG_EVIDENCE_GRADES = frozenset({"CAUSAL_VERIFIED", "REPAIR_VERIFIED"})
_PHASE2_EVIDENCE_GRADE_RANK = MappingProxyType(
    {
        "AST_ONLY": 0,
        "OUTPUT_ONLY": 1,
        "PAIR_DISTINGUISHED": 2,
        "REPAIR_VERIFIED": 3,
        "CAUSAL_VERIFIED": 4,
    }
)
MAX_SECONDARY_ROOTS = 32
MAX_CANDIDATE_ID_CHARS = 128
_CANDIDATE_ID = re.compile(r"^candidate_[0-9a-f]{16}$")
_ATOMIC_SKILL_ID = re.compile(
    r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$"
)

# A teaching stage is a bounded semantic bucket, not always one physical CFG
# stage.  These are the only Phase 2 logical-stage symbols that the frozen
# Phase 3 adapter accepts for each bucket.  Keeping the set here makes the
# projection and persistence gates use the same symbol range.
ALLOWED_LOGICAL_STAGES_BY_TEACHING_STAGE = MappingProxyType(
    {
        "S1": frozenset({"SOURCE_JOIN"}),
        "S2": frozenset({"ROW_FILTER"}),
        "S3": frozenset({"GROUP_AGG"}),
        "S4": frozenset({"GROUP_FILTER"}),
        "S5": frozenset({"PROJECTION", "DISTINCT"}),
        "S6": frozenset({"ROOT_ORDER", "PAGINATION"}),
    }
)


class RuleSkillCatalogError(RuntimeError):
    """Raised when the frozen rule-to-skill catalogue is inconsistent."""


class ProjectionReasonCode(str, Enum):
    """Stable, non-sensitive reasons why a candidate was not projected."""

    INPUT_NOT_MAPPING = "INPUT_NOT_MAPPING"
    PHASE2_SCHEMA_VERSION_UNSUPPORTED = "PHASE2_SCHEMA_VERSION_UNSUPPORTED"
    PHASE2_DIAGNOSIS_VERSION_UNSUPPORTED = (
        "PHASE2_DIAGNOSIS_VERSION_UNSUPPORTED"
    )
    PHASE2_RULE_CATALOG_VERSION_UNSUPPORTED = (
        "PHASE2_RULE_CATALOG_VERSION_UNSUPPORTED"
    )
    PHASE2_VERDICT_NOT_INCORRECT = "PHASE2_VERDICT_NOT_INCORRECT"
    PHASE2_PHASE1_CONTRACT_INVALID = "PHASE2_PHASE1_CONTRACT_INVALID"
    PHASE2_DIAGNOSIS_PARTIAL = "PHASE2_DIAGNOSIS_PARTIAL"
    PHASE2_DIAGNOSIS_NOT_SUPPORTED = "PHASE2_DIAGNOSIS_NOT_SUPPORTED"
    PHASE2_PRIMARY_MISSING = "PHASE2_PRIMARY_MISSING"
    PHASE2_SECONDARY_INVALID = "PHASE2_SECONDARY_INVALID"
    PHASE2_SECONDARY_LIMIT_EXCEEDED = "PHASE2_SECONDARY_LIMIT_EXCEEDED"
    PHASE2_SECONDARY_COUNT_MISMATCH = "PHASE2_SECONDARY_COUNT_MISMATCH"
    CANDIDATE_NOT_MAPPING = "CANDIDATE_NOT_MAPPING"
    CANDIDATE_ID_INVALID = "CANDIDATE_ID_INVALID"
    CANDIDATE_ID_DUPLICATE = "CANDIDATE_ID_DUPLICATE"
    CANDIDATE_RULE_UNKNOWN = "CANDIDATE_RULE_UNKNOWN"
    CANDIDATE_STAGE_MISMATCH = "CANDIDATE_STAGE_MISMATCH"
    CANDIDATE_LOGICAL_STAGE_MISMATCH = "CANDIDATE_LOGICAL_STAGE_MISMATCH"
    CANDIDATE_EVIDENCE_NOT_STRONG = "CANDIDATE_EVIDENCE_NOT_STRONG"
    CANDIDATE_EVIDENCE_SCOPE_INVALID = "CANDIDATE_EVIDENCE_SCOPE_INVALID"
    DUPLICATE_SKILL = "DUPLICATE_SKILL"


@dataclass(frozen=True)
class RuleSkillSpec:
    """One frozen Phase 2 rule to one atomic Phase 3 skill."""

    rule_id: str
    skill_id: str
    teaching_stage: str
    title_zh: str
    title_en: str

    def to_dict(self) -> dict[str, str]:
        return {
            "rule_id": self.rule_id,
            "skill_id": self.skill_id,
            "teaching_stage": self.teaching_stage,
            "title_zh": self.title_zh,
            "title_en": self.title_en,
        }


# Keep this ordered exactly like Phase 2's frozen RULE_CATALOG.  Skill IDs are
# semantic atoms, not syntax-node labels and not aliases for the Phase 2
# candidate.knowledge_points display union.
RULE_SKILL_CATALOG: tuple[RuleSkillSpec, ...] = (
    RuleSkillSpec(
        "S1_MISSING_BRIDGE",
        "join.bridge_path",
        "S1",
        "关联桥接路径",
        "Join bridge path",
    ),
    RuleSkillSpec(
        "S1_CARTESIAN_PRODUCT",
        "join.constraint",
        "S1",
        "连接约束完整性",
        "Join constraint completeness",
    ),
    RuleSkillSpec(
        "S1_OUTER_JOIN_MISUSE",
        "join.outer_preservation",
        "S1",
        "外连接保留语义",
        "Outer-join preservation",
    ),
    RuleSkillSpec(
        "S1_SUBQUERY_CARDINALITY",
        "subquery.cardinality",
        "S1",
        "子查询基数语义",
        "Subquery cardinality",
    ),
    RuleSkillSpec(
        "S2_BOUNDARY",
        "filter.boundary",
        "S2",
        "行过滤边界",
        "Row-filter boundary",
    ),
    RuleSkillSpec(
        "S2_BOOLEAN_LOGIC",
        "filter.boolean_logic",
        "S2",
        "布尔条件组合",
        "Boolean filter composition",
    ),
    RuleSkillSpec(
        "S2_NULL_LOGIC",
        "null.three_valued_logic",
        "S2",
        "NULL 三值逻辑",
        "NULL three-valued logic",
    ),
    RuleSkillSpec(
        "S2_AGGREGATE_IN_WHERE",
        "aggregate.filter_placement",
        "S2",
        "聚合过滤位置",
        "Aggregate-filter placement",
    ),
    RuleSkillSpec(
        "S3_GRAIN_ENTITY_MISMATCH",
        "group.grain",
        "S3",
        "分组实体粒度",
        "Grouping entity grain",
    ),
    RuleSkillSpec(
        "S3_GROUP_KEY_MISSING",
        "group.key_completeness",
        "S3",
        "分组键完整性",
        "Grouping-key completeness",
    ),
    RuleSkillSpec(
        "S3_GROUP_KEY_REDUNDANT",
        "group.key_redundancy",
        "S3",
        "分组键最小性",
        "Grouping-key minimality",
    ),
    RuleSkillSpec(
        "S4_HAVING_MISSING",
        "having.required",
        "S4",
        "组级约束完整性",
        "HAVING constraint completeness",
    ),
    RuleSkillSpec(
        "S4_AGG_BOUNDARY",
        "having.aggregate_boundary",
        "S4",
        "聚合筛选边界",
        "Aggregate-filter boundary",
    ),
    RuleSkillSpec(
        "S4_ROW_FILTER_IN_HAVING",
        "filter.stage_placement",
        "S4",
        "行级与组级过滤时序",
        "Row/group filter placement",
    ),
    RuleSkillSpec(
        "S5_FANOUT_AGGREGATE",
        "aggregate.fanout",
        "S5",
        "关联扇出聚合",
        "Aggregate fan-out",
    ),
    RuleSkillSpec(
        "S5_COUNT_NULL_SENSITIVITY",
        "aggregate.count_null",
        "S5",
        "COUNT 空值敏感性",
        "COUNT null sensitivity",
    ),
    RuleSkillSpec(
        "S5_CASE_INCOMPLETE",
        "projection.case_coverage",
        "S5",
        "CASE 分支覆盖",
        "CASE branch coverage",
    ),
    RuleSkillSpec(
        "S5_TOP_LEVEL_DEDUP",
        "projection.dedup",
        "S5",
        "顶层结果去重",
        "Top-level result deduplication",
    ),
    RuleSkillSpec(
        "S6_TOPN_WITHOUT_ORDER",
        "result.topn_order",
        "S6",
        "Top-N 确定性",
        "Top-N determinism",
    ),
    RuleSkillSpec(
        "S6_ORDER_OFFSET",
        "result.order_offset",
        "S6",
        "排序与偏移",
        "Ordering and offset",
    ),
)


def validate_rule_skill_catalog(
    entries: Sequence[RuleSkillSpec] = RULE_SKILL_CATALOG,
    phase2_rules: Sequence[Any] = PHASE2_RULE_CATALOG,
) -> None:
    """Validate a bijective, ordered, stage-consistent Phase 2 mapping.

    This function is called at import time below.  It is also public so that a
    deployment or acceptance gate can re-run the same invariant explicitly.
    """

    entry_rule_ids = tuple(item.rule_id for item in entries)
    phase2_rule_ids = tuple(str(item.rule_id) for item in phase2_rules)
    if entry_rule_ids != phase2_rule_ids:
        raise RuleSkillCatalogError(
            "rule-skill catalogue must match the ordered Phase 2 RULE_CATALOG exactly"
        )
    if len(set(entry_rule_ids)) != len(entry_rule_ids):
        raise RuleSkillCatalogError("duplicate Phase 2 rule_id in rule-skill catalogue")

    skill_ids = tuple(item.skill_id for item in entries)
    if any(
        len(value) > 96 or _ATOMIC_SKILL_ID.fullmatch(value) is None
        for value in skill_ids
    ):
        raise RuleSkillCatalogError(
            "atomic skill_id must be a bounded dotted identifier"
        )
    if len(set(skill_ids)) != len(skill_ids):
        raise RuleSkillCatalogError(
            "each Phase 2 rule must map to a distinct atomic skill_id"
        )

    for item, phase2_rule in zip(entries, phase2_rules, strict=True):
        if item.teaching_stage != str(phase2_rule.teaching_stage):
            raise RuleSkillCatalogError(
                f"teaching stage mismatch for frozen rule {item.rule_id}"
            )
        if not item.title_zh or not item.title_en:
            raise RuleSkillCatalogError(
                f"stable labels are required for frozen rule {item.rule_id}"
            )


# A deployment must fail at startup/import rather than silently losing a new
# Phase 2 rule or applying an old skill meaning to a changed rule catalogue.
validate_rule_skill_catalog()


RULE_SKILL_MAP = MappingProxyType(
    {item.rule_id: item for item in RULE_SKILL_CATALOG}
)
_CATALOG_CANONICAL = json.dumps(
    {
        "version": RULE_SKILL_MAP_VERSION,
        "skill_taxonomy_version": ATOMIC_SKILL_TAXONOMY_VERSION,
        "entries": [item.to_dict() for item in RULE_SKILL_CATALOG],
    },
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
)
RULE_SKILL_MAP_DIGEST = sha256(_CATALOG_CANONICAL.encode("utf-8")).hexdigest()


def rule_skill_catalog_metadata() -> dict[str, Any]:
    """Return stable, JSON-safe metadata for audits and frozen reports."""

    return {
        "version": RULE_SKILL_MAP_VERSION,
        "skill_taxonomy_version": ATOMIC_SKILL_TAXONOMY_VERSION,
        "digest_sha256": RULE_SKILL_MAP_DIGEST,
        "phase2_diagnosis_version": PHASE2_DIAGNOSIS_VERSION,
        "phase2_rule_catalog_version": PHASE2_RULE_CATALOG_VERSION,
        "entry_count": len(RULE_SKILL_CATALOG),
        "entries": [item.to_dict() for item in RULE_SKILL_CATALOG],
    }


@dataclass(frozen=True)
class SkillObservationCandidate:
    """A strongly supported negative observation candidate, not a BKT write."""

    observation_candidate_id: str
    skill_id: str
    source_role: str
    source_index: int
    phase2_candidate_id: str
    phase2_rule_id: str
    phase2_stage: str
    evidence_grade: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_candidate_id": self.observation_candidate_id,
            "skill_id": self.skill_id,
            "proposed_observation": "INCORRECT",
            "source_role": self.source_role,
            "source_index": self.source_index,
            "phase2_candidate_id": self.phase2_candidate_id,
            "phase2_rule_id": self.phase2_rule_id,
            "phase2_stage": self.phase2_stage,
            "evidence_grade": self.evidence_grade,
        }


@dataclass(frozen=True)
class SkippedProjectionItem:
    reason_code: ProjectionReasonCode
    source_role: str
    source_index: int | None = None
    phase2_candidate_id: str | None = None
    phase2_rule_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code.value,
            "source_role": self.source_role,
            "source_index": self.source_index,
            "phase2_candidate_id": self.phase2_candidate_id,
            "phase2_rule_id": self.phase2_rule_id,
        }


@dataclass(frozen=True)
class SkillObservationProjection:
    status: str
    candidates: tuple[SkillObservationCandidate, ...] = ()
    skipped: tuple[SkippedProjectionItem, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROJECTION_SCHEMA_VERSION,
            "status": self.status,
            "rule_skill_map_version": RULE_SKILL_MAP_VERSION,
            "skill_taxonomy_version": ATOMIC_SKILL_TAXONOMY_VERSION,
            "rule_skill_map_digest": RULE_SKILL_MAP_DIGEST,
            "source_contract": {
                "schema_version": PHASE2_PUBLIC_SCHEMA_VERSION,
                "diagnosis_version": PHASE2_DIAGNOSIS_VERSION,
                "rule_catalog_version": PHASE2_RULE_CATALOG_VERSION,
            },
            "candidate_count": len(self.candidates),
            "candidates": [item.to_dict() for item in self.candidates],
            "skip_count": len(self.skipped),
            "skipped": [item.to_dict() for item in self.skipped],
        }


def _package_rejection(reason: ProjectionReasonCode) -> SkillObservationProjection:
    return SkillObservationProjection(
        status="SKIPPED",
        skipped=(SkippedProjectionItem(reason_code=reason, source_role="PACKAGE"),),
    )


def _safe_candidate_identity(raw: Mapping[str, Any]) -> tuple[str | None, str | None]:
    candidate_id = raw.get("candidate_id")
    rule_id = raw.get("rule_id")
    return (
        candidate_id
        if isinstance(candidate_id, str)
        and len(candidate_id) <= MAX_CANDIDATE_ID_CHARS
        else None,
        rule_id if isinstance(rule_id, str) and len(rule_id) <= 96 else None,
    )


def _candidate_skip(
    reason: ProjectionReasonCode,
    *,
    role: str,
    index: int,
    raw: Mapping[str, Any] | None = None,
) -> SkippedProjectionItem:
    candidate_id, rule_id = _safe_candidate_identity(raw or {})
    return SkippedProjectionItem(
        reason_code=reason,
        source_role=role,
        source_index=index,
        phase2_candidate_id=candidate_id,
        phase2_rule_id=rule_id,
    )


def _strong_evidence_scope_is_valid(
    raw: Mapping[str, Any],
    *,
    phase2_public: Mapping[str, Any],
) -> bool:
    """Check the mandatory Phase 2 per-diff evidence partition.

    The current ``phase2.public.v1`` contract carries this audit field for
    every candidate.  A missing partition is rejected rather than treated as
    implicit proof: the display knowledge-point union is never considered and
    a bundle's maximum grade cannot authorize an observation by itself.
    """

    refs = raw.get("evidence_refs")
    if not isinstance(refs, Mapping):
        return False
    if "verified_diff_ids" not in refs or "unverified_diff_ids" not in refs:
        return False
    diff_ids = refs.get("diff_ids")
    verified = refs.get("verified_diff_ids")
    unverified = refs.get("unverified_diff_ids", ())
    if (
        not isinstance(diff_ids, (list, tuple))
        or not diff_ids
        or not isinstance(verified, (list, tuple))
        or not verified
        or not isinstance(unverified, (list, tuple))
    ):
        return False
    if any(
        not isinstance(item, str) or not item
        for item in (*diff_ids, *verified, *unverified)
    ):
        return False
    diff_set = set(diff_ids)
    verified_set = set(verified)
    unverified_set = set(unverified)
    if (
        len(diff_set) != len(diff_ids)
        or len(verified_set) != len(verified)
        or len(unverified_set) != len(unverified)
        or not verified_set <= diff_set
        or not unverified_set <= diff_set
        or verified_set & unverified_set
        or verified_set | unverified_set != diff_set
    ):
        return False

    # The current Phase 2 package carries the public diff pipeline.  Once the
    # additive partition is present, the pipeline is mandatory: otherwise an
    # attacker could forge a strong ``verified_diff_ids`` list without any
    # auditable per-diff grade.  The partition is checked against the pipeline
    # in both directions so a weak diff cannot hide inside a strong bundle.
    pipeline = phase2_public.get("ordered_diff_pipeline")
    if not isinstance(pipeline, (list, tuple)):
        return False
    grades_by_id: dict[str, Any] = {}
    for item in pipeline:
        if not isinstance(item, Mapping):
            return False
        diff_id = item.get("diff_id")
        if not isinstance(diff_id, str) or not diff_id or diff_id in grades_by_id:
            return False
        grades_by_id[diff_id] = item.get("evidence_grade")
    if any(diff_id not in grades_by_id for diff_id in diff_ids):
        return False
    candidate_grades = [grades_by_id[diff_id] for diff_id in diff_ids]
    if any(grade not in _PHASE2_EVIDENCE_GRADE_RANK for grade in candidate_grades):
        return False
    expected_candidate_grade = max(
        candidate_grades,
        key=lambda grade: _PHASE2_EVIDENCE_GRADE_RANK[grade],
    )
    if raw.get("evidence_grade") != expected_candidate_grade:
        return False
    if any(
        grades_by_id[diff_id] not in STRONG_EVIDENCE_GRADES
        for diff_id in verified
    ):
        return False
    if any(
        grades_by_id[diff_id] in STRONG_EVIDENCE_GRADES
        for diff_id in unverified
    ):
            return False
    return True


def _observation_candidate_id(
    *, candidate_id: str, rule_id: str, skill_id: str, role: str
) -> str:
    canonical = json.dumps(
        {
            "mapping_version": RULE_SKILL_MAP_VERSION,
            "phase2_candidate_id": candidate_id,
            "phase2_rule_id": rule_id,
            "skill_id": skill_id,
            "source_role": role,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"skill_candidate_{sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


def project_phase2_skill_candidates(
    phase2_public: Mapping[str, Any],
) -> SkillObservationProjection:
    """Project strong Phase 2 root faults into deduplicated atomic skills.

    Accepted input is exactly ``phase2.public.v1`` with an ``INCORRECT`` and
    ``SUPPORTED`` diagnosis.  Primary is processed first, followed by the
    Phase 2-ranked secondary list.  Only ``CAUSAL_VERIFIED`` and
    ``REPAIR_VERIFIED`` candidates survive.  Duplicate skills keep the first
    root, making FDP/primary precedence explicit and deterministic.

    Importantly, this function never reads ``candidate.knowledge_points``.
    Those values are a display union and cannot authorize a learning-state
    observation.
    """

    if not isinstance(phase2_public, Mapping):
        return _package_rejection(ProjectionReasonCode.INPUT_NOT_MAPPING)
    if phase2_public.get("schema_version") != PHASE2_PUBLIC_SCHEMA_VERSION:
        return _package_rejection(
            ProjectionReasonCode.PHASE2_SCHEMA_VERSION_UNSUPPORTED
        )
    if phase2_public.get("diagnosis_version") != PHASE2_DIAGNOSIS_VERSION:
        return _package_rejection(
            ProjectionReasonCode.PHASE2_DIAGNOSIS_VERSION_UNSUPPORTED
        )
    if phase2_public.get("rule_catalog_version") != PHASE2_RULE_CATALOG_VERSION:
        return _package_rejection(
            ProjectionReasonCode.PHASE2_RULE_CATALOG_VERSION_UNSUPPORTED
        )
    if phase2_public.get("verdict") != "INCORRECT":
        return _package_rejection(
            ProjectionReasonCode.PHASE2_VERDICT_NOT_INCORRECT
        )
    phase1 = phase2_public.get("phase1")
    if not (
        isinstance(phase1, Mapping)
        and phase1.get("status") == "SUPPORTED"
        and phase1.get("equivalence_conclusion") == "NOT_EQUIVALENT"
        and phase1.get("judge_status") == "WRONG"
    ):
        return _package_rejection(
            ProjectionReasonCode.PHASE2_PHASE1_CONTRACT_INVALID
        )

    diagnosis_status = phase2_public.get("diagnosis_status")
    if diagnosis_status == "PARTIAL":
        return _package_rejection(ProjectionReasonCode.PHASE2_DIAGNOSIS_PARTIAL)
    if diagnosis_status != "SUPPORTED":
        return _package_rejection(
            ProjectionReasonCode.PHASE2_DIAGNOSIS_NOT_SUPPORTED
        )

    primary = phase2_public.get("primary")
    if not isinstance(primary, Mapping):
        return _package_rejection(ProjectionReasonCode.PHASE2_PRIMARY_MISSING)
    secondary = phase2_public.get("secondary")
    if not isinstance(secondary, (list, tuple)):
        return _package_rejection(ProjectionReasonCode.PHASE2_SECONDARY_INVALID)
    if len(secondary) > MAX_SECONDARY_ROOTS:
        return _package_rejection(
            ProjectionReasonCode.PHASE2_SECONDARY_LIMIT_EXCEEDED
        )
    secondary_count = phase2_public.get("secondary_count")
    if (
        not isinstance(secondary_count, int)
        or isinstance(secondary_count, bool)
        or secondary_count != len(secondary)
    ):
        return _package_rejection(
            ProjectionReasonCode.PHASE2_SECONDARY_COUNT_MISMATCH
        )

    sources: list[tuple[str, int, Any]] = [("PRIMARY", 0, primary)]
    sources.extend(("SECONDARY", index, raw) for index, raw in enumerate(secondary))

    projected: list[SkillObservationCandidate] = []
    skipped: list[SkippedProjectionItem] = []
    seen_skills: set[str] = set()
    seen_candidate_ids: set[str] = set()

    for role, index, raw in sources:
        if not isinstance(raw, Mapping):
            skipped.append(
                _candidate_skip(
                    ProjectionReasonCode.CANDIDATE_NOT_MAPPING,
                    role=role,
                    index=index,
                )
            )
            continue

        candidate_id, rule_id = _safe_candidate_identity(raw)
        if candidate_id is None or _CANDIDATE_ID.fullmatch(candidate_id) is None:
            item = _candidate_skip(
                ProjectionReasonCode.CANDIDATE_ID_INVALID,
                role=role,
                index=index,
                raw=raw,
            )
            if role == "PRIMARY":
                return SkillObservationProjection(status="SKIPPED", skipped=(item,))
            skipped.append(item)
            continue
        if candidate_id in seen_candidate_ids:
            skipped.append(
                _candidate_skip(
                    ProjectionReasonCode.CANDIDATE_ID_DUPLICATE,
                    role=role,
                    index=index,
                    raw=raw,
                )
            )
            continue
        seen_candidate_ids.add(candidate_id)

        spec = RULE_SKILL_MAP.get(rule_id or "")
        if spec is None:
            item = _candidate_skip(
                ProjectionReasonCode.CANDIDATE_RULE_UNKNOWN,
                role=role,
                index=index,
                raw=raw,
            )
            if role == "PRIMARY":
                return SkillObservationProjection(status="SKIPPED", skipped=(item,))
            skipped.append(item)
            continue
        if raw.get("stage") != spec.teaching_stage:
            item = _candidate_skip(
                ProjectionReasonCode.CANDIDATE_STAGE_MISMATCH,
                role=role,
                index=index,
                raw=raw,
            )
            if role == "PRIMARY":
                return SkillObservationProjection(status="SKIPPED", skipped=(item,))
            skipped.append(item)
            continue
        if raw.get("logical_stage") not in ALLOWED_LOGICAL_STAGES_BY_TEACHING_STAGE.get(
            spec.teaching_stage, frozenset()
        ):
            item = _candidate_skip(
                ProjectionReasonCode.CANDIDATE_LOGICAL_STAGE_MISMATCH,
                role=role,
                index=index,
                raw=raw,
            )
            if role == "PRIMARY":
                return SkillObservationProjection(status="SKIPPED", skipped=(item,))
            skipped.append(item)
            continue

        evidence_grade = raw.get("evidence_grade")
        if evidence_grade not in STRONG_EVIDENCE_GRADES:
            item = _candidate_skip(
                ProjectionReasonCode.CANDIDATE_EVIDENCE_NOT_STRONG,
                role=role,
                index=index,
                raw=raw,
            )
            if role == "PRIMARY":
                return SkillObservationProjection(status="SKIPPED", skipped=(item,))
            skipped.append(item)
            continue
        if not _strong_evidence_scope_is_valid(
            raw,
            phase2_public=phase2_public,
        ):
            item = _candidate_skip(
                ProjectionReasonCode.CANDIDATE_EVIDENCE_SCOPE_INVALID,
                role=role,
                index=index,
                raw=raw,
            )
            if role == "PRIMARY":
                return SkillObservationProjection(status="SKIPPED", skipped=(item,))
            skipped.append(item)
            continue
        if spec.skill_id in seen_skills:
            skipped.append(
                _candidate_skip(
                    ProjectionReasonCode.DUPLICATE_SKILL,
                    role=role,
                    index=index,
                    raw=raw,
                )
            )
            continue

        seen_skills.add(spec.skill_id)
        projected.append(
            SkillObservationCandidate(
                observation_candidate_id=_observation_candidate_id(
                    candidate_id=candidate_id,
                    rule_id=spec.rule_id,
                    skill_id=spec.skill_id,
                    role=role,
                ),
                skill_id=spec.skill_id,
                source_role=role,
                source_index=index,
                phase2_candidate_id=candidate_id,
                phase2_rule_id=spec.rule_id,
                phase2_stage=spec.teaching_stage,
                evidence_grade=str(evidence_grade),
            )
        )

    status = "READY" if projected and not skipped else "READY_WITH_SKIPS" if projected else "SKIPPED"
    return SkillObservationProjection(
        status=status,
        candidates=tuple(projected),
        skipped=tuple(skipped),
    )


__all__ = [
    "ATOMIC_SKILL_TAXONOMY_VERSION",
    "ALLOWED_LOGICAL_STAGES_BY_TEACHING_STAGE",
    "MAX_SECONDARY_ROOTS",
    "PHASE2_DIAGNOSIS_VERSION",
    "PHASE2_PUBLIC_SCHEMA_VERSION",
    "PHASE2_RULE_CATALOG_VERSION",
    "PROJECTION_SCHEMA_VERSION",
    "ProjectionReasonCode",
    "RULE_SKILL_CATALOG",
    "RULE_SKILL_MAP",
    "RULE_SKILL_MAP_DIGEST",
    "RULE_SKILL_MAP_VERSION",
    "RuleSkillCatalogError",
    "RuleSkillSpec",
    "STRONG_EVIDENCE_GRADES",
    "SkillObservationCandidate",
    "SkillObservationProjection",
    "project_phase2_skill_candidates",
    "rule_skill_catalog_metadata",
    "validate_rule_skill_catalog",
]
