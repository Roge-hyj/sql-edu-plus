"""Deterministic causal-priority scheduling for Phase 3 teaching targets.

This module intentionally does not implement, or claim to implement, A* search.
There is no path graph, transition cost, or admissible heuristic in this task.
Instead, it applies a small auditable policy to already-trusted atomic skill
observations:

1. primary/FDP observations form a hard tier before secondary observations;
2. candidates inside a tier receive an independent weighted score;
3. suppressed and unresolved candidates are never selectable; and
4. a support budget admits at most one independent secondary target.

All weights and instructional-impact values are an **uncalibrated MVP policy**.
They are versioned so offline calibration can replace them without silently
changing the meaning of previously recorded decisions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
import math
import re
from types import MappingProxyType
from typing import Any

from core.error_diagnosis import LOGICAL_STAGE_ORDER
from core.phase3_skill_catalog import (
    ATOMIC_SKILL_TAXONOMY_VERSION,
    RULE_SKILL_CATALOG,
)


PRIORITY_POLICY_VERSION = "phase3.priority_policy.v1"
PRIORITY_CALIBRATION_STATUS = "UNCALIBRATED_MVP"
MAX_PRIORITY_CANDIDATES = 64

PRIORITY_WEIGHTS = MappingProxyType(
    {
        "instructional_impact": 0.30,
        "recurrence": 0.25,
        "mastery_deficit": 0.20,
        "question_alignment": 0.15,
        "evidence_strength": 0.10,
    }
)

# These values are Phase 3 teaching-policy configuration, not Phase 2 bundle
# severity.  Their only present claim is to provide sensible, inspectable MVP
# ordering; they have not yet been psychometrically or experimentally fitted.
INSTRUCTIONAL_IMPACT_BY_SKILL = MappingProxyType(
    {
        "join.bridge_path": 0.95,
        "join.constraint": 0.95,
        "join.outer_preservation": 0.85,
        "subquery.cardinality": 0.80,
        "filter.boundary": 0.60,
        "filter.boolean_logic": 0.75,
        "null.three_valued_logic": 0.85,
        "aggregate.filter_placement": 0.80,
        "group.grain": 0.90,
        "group.key_completeness": 0.85,
        "group.key_redundancy": 0.55,
        "having.required": 0.65,
        "having.aggregate_boundary": 0.60,
        "filter.stage_placement": 0.75,
        "aggregate.fanout": 0.95,
        "aggregate.count_null": 0.70,
        "projection.case_coverage": 0.60,
        "projection.dedup": 0.65,
        "result.topn_order": 0.75,
        "result.order_offset": 0.65,
    }
)

EVIDENCE_STRENGTH_BY_GRADE = MappingProxyType(
    {
        "CAUSAL_VERIFIED": 1.00,
        "REPAIR_VERIFIED": 0.90,
    }
)

_ATOMIC_SKILL_ID = re.compile(
    r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$"
)
_CANONICAL_STAGE_RANK = {
    name: index for index, name in enumerate(LOGICAL_STAGE_ORDER)
}
# S1--S6 and the original six-slot labels remain accepted only as compatibility
# aliases.  Their ranks are anchored to Phase 2's canonical stage order, so a
# new Phase 2 stage cannot be silently omitted from this policy.
_STAGE_ALIASES = {
    "S1": "SOURCE_JOIN",
    "DATA_SOURCE": "SOURCE_JOIN",
    "S2": "ROW_FILTER",
    "S3": "GROUP_AGG",
    "GROUPING": "GROUP_AGG",
    "S4": "GROUP_FILTER",
    "S5": "PROJECTION",
    "S6": "ROOT_ORDER",
    "RESULT": "ROOT_ORDER",
}
_STAGE_RANK = MappingProxyType(
    {
        **_CANONICAL_STAGE_RANK,
        **{
            alias: _CANONICAL_STAGE_RANK[canonical]
            for alias, canonical in _STAGE_ALIASES.items()
        },
    }
)


class CausalSourceRole(str, Enum):
    """Causal tier assigned before any weighted score is considered."""

    FDP = "FDP"
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"


_SOURCE_ROLE_RANK = MappingProxyType(
    {
        CausalSourceRole.FDP: 0,
        CausalSourceRole.PRIMARY: 0,
        CausalSourceRole.SECONDARY: 1,
    }
)


class PriorityPolicyError(ValueError):
    """Raised when untrusted or malformed data reaches the scheduler."""


def _unit_interval(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PriorityPolicyError(f"{field_name} must be a finite number in [0, 1]")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise PriorityPolicyError(f"{field_name} must be a finite number in [0, 1]")
    return number


def instructional_impact_for_skill(skill_id: str) -> float:
    """Return the versioned Phase 3 impact configured for an atomic skill."""

    try:
        return INSTRUCTIONAL_IMPACT_BY_SKILL[skill_id]
    except KeyError as exc:
        raise PriorityPolicyError(
            f"no {PRIORITY_POLICY_VERSION} instructional impact for skill_id"
        ) from exc


def evidence_strength_for_grade(evidence_grade: str) -> float:
    """Map only Phase 2's strong evidence grades into a bounded score."""

    try:
        return EVIDENCE_STRENGTH_BY_GRADE[evidence_grade]
    except KeyError as exc:
        raise PriorityPolicyError(
            "evidence grade is not trusted by the priority policy"
        ) from exc


def _validate_policy_catalogue() -> None:
    expected = {item.skill_id for item in RULE_SKILL_CATALOG}
    configured = set(INSTRUCTIONAL_IMPACT_BY_SKILL)
    if configured != expected:
        raise RuntimeError(
            "priority policy must configure every atomic Phase 3 skill exactly once"
        )
    if not math.isclose(sum(PRIORITY_WEIGHTS.values()), 1.0, abs_tol=1e-12):
        raise RuntimeError("priority weights must sum to 1")
    for skill_id, value in INSTRUCTIONAL_IMPACT_BY_SKILL.items():
        _unit_interval(value, field_name=f"instructional impact for {skill_id}")


_validate_policy_catalogue()


@dataclass(frozen=True)
class CausalPriorityCandidate:
    """One trusted atomic negative observation enriched for teaching order."""

    skill_id: str
    taxonomy_version: str
    source_role: CausalSourceRole | str
    logical_stage: str
    phase2_candidate_id: str
    trusted_atomic_observation: bool
    instructional_impact: float
    recurrence: float
    mastery_deficit: float
    question_alignment: float
    evidence_strength: float
    suppressed: bool = False
    unresolved: bool = False

    def __post_init__(self) -> None:
        if self.trusted_atomic_observation is not True:
            raise PriorityPolicyError(
                "scheduler accepts only trusted atomic observations"
            )
        if type(self.suppressed) is not bool or type(self.unresolved) is not bool:
            raise PriorityPolicyError("suppressed and unresolved must be bool")
        if self.taxonomy_version != ATOMIC_SKILL_TAXONOMY_VERSION:
            raise PriorityPolicyError("unsupported atomic skill taxonomy version")
        if (
            not isinstance(self.skill_id, str)
            or len(self.skill_id) > 96
            or _ATOMIC_SKILL_ID.fullmatch(self.skill_id) is None
            or self.skill_id not in INSTRUCTIONAL_IMPACT_BY_SKILL
        ):
            raise PriorityPolicyError("unknown or malformed atomic skill_id")
        try:
            role = CausalSourceRole(self.source_role)
        except (TypeError, ValueError) as exc:
            raise PriorityPolicyError("unsupported causal source_role") from exc
        object.__setattr__(self, "source_role", role)
        if self.logical_stage not in _STAGE_RANK:
            raise PriorityPolicyError("unsupported logical_stage")
        if (
            not isinstance(self.phase2_candidate_id, str)
            or not self.phase2_candidate_id
            or len(self.phase2_candidate_id) > 128
        ):
            raise PriorityPolicyError("phase2_candidate_id must be a bounded string")
        for field_name in (
            "instructional_impact",
            "recurrence",
            "mastery_deficit",
            "question_alignment",
            "evidence_strength",
        ):
            object.__setattr__(
                self,
                field_name,
                _unit_interval(getattr(self, field_name), field_name=field_name),
            )
        configured_impact = instructional_impact_for_skill(self.skill_id)
        if not math.isclose(
            self.instructional_impact, configured_impact, abs_tol=1e-12
        ):
            raise PriorityPolicyError(
                "instructional_impact must come from the versioned Phase 3 policy"
            )
        if not any(
            math.isclose(self.evidence_strength, trusted, abs_tol=1e-12)
            for trusted in EVIDENCE_STRENGTH_BY_GRADE.values()
        ):
            raise PriorityPolicyError(
                "evidence_strength must come from a trusted evidence grade"
            )

    @property
    def priority_score(self) -> float:
        """Independent within-tier score; deliberately not a complementary sum."""

        return sum(
            PRIORITY_WEIGHTS[field_name] * getattr(self, field_name)
            for field_name in PRIORITY_WEIGHTS
        )

    @property
    def selection_eligible(self) -> bool:
        """Whether this candidate may enter the bounded teaching target set."""

        return not self.suppressed and not self.unresolved

    @property
    def source_role_rank(self) -> int:
        return _SOURCE_ROLE_RANK[self.source_role]

    @property
    def logical_stage_rank(self) -> int:
        return _STAGE_RANK[self.logical_stage]

    @property
    def sort_key(self) -> tuple[int, float, int, str, str]:
        return (
            self.source_role_rank,
            -self.priority_score,
            self.logical_stage_rank,
            self.phase2_candidate_id,
            self.skill_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "taxonomy_version": self.taxonomy_version,
            "source_role": self.source_role.value,
            "logical_stage": self.logical_stage,
            "phase2_candidate_id": self.phase2_candidate_id,
            "priority_score": self.priority_score,
            "source_role_rank": self.source_role_rank,
            "logical_stage_rank": self.logical_stage_rank,
            "components": {
                field_name: getattr(self, field_name)
                for field_name in PRIORITY_WEIGHTS
            },
            "selection_eligible": self.selection_eligible,
            "suppressed": self.suppressed,
            "unresolved": self.unresolved,
        }


@dataclass(frozen=True)
class PrioritySchedule:
    """Immutable and audit-friendly output from the causal scheduler."""

    ordered: tuple[CausalPriorityCandidate, ...]
    selected_targets: tuple[CausalPriorityCandidate, ...] = ()
    secondary_budget: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.ordered, tuple):
            raise PriorityPolicyError("ordered must be an immutable tuple")
        if not isinstance(self.selected_targets, tuple):
            raise PriorityPolicyError("selected_targets must be an immutable tuple")
        if (
            isinstance(self.secondary_budget, bool)
            or not isinstance(self.secondary_budget, int)
            or not 0 <= self.secondary_budget <= 1
        ):
            raise PriorityPolicyError("secondary_budget must be an integer in [0, 1]")
        if len(self.ordered) > MAX_PRIORITY_CANDIDATES:
            raise PriorityPolicyError("too many priority candidates")
        if any(not isinstance(item, CausalPriorityCandidate) for item in self.ordered):
            raise PriorityPolicyError("ordered contains an invalid candidate")
        identities = [
            (item.phase2_candidate_id, item.skill_id) for item in self.ordered
        ]
        if len(identities) != len(set(identities)):
            raise PriorityPolicyError("ordered contains duplicate candidates")
        if self.ordered != tuple(sorted(self.ordered, key=lambda item: item.sort_key)):
            raise PriorityPolicyError("ordered must use the canonical priority order")

        selected_identities = [
            (item.phase2_candidate_id, item.skill_id)
            for item in self.selected_targets
        ]
        if len(selected_identities) != len(set(selected_identities)):
            raise PriorityPolicyError("selected_targets contains duplicates")
        ordered_by_identity = dict(zip(identities, self.ordered))
        if any(
            identity not in ordered_by_identity
            or item is not ordered_by_identity[identity]
            for identity, item in zip(selected_identities, self.selected_targets)
        ):
            raise PriorityPolicyError("selected target is not from ordered candidates")
        if any(not item.selection_eligible for item in self.selected_targets):
            raise PriorityPolicyError("suppressed or unresolved target was selected")

        eligible = tuple(item for item in self.ordered if item.selection_eligible)
        required_primary = tuple(item for item in eligible if item.source_role_rank == 0)
        selected_primary = tuple(
            item for item in self.selected_targets if item.source_role_rank == 0
        )
        selected_secondary = tuple(
            item
            for item in self.selected_targets
            if item.source_role is CausalSourceRole.SECONDARY
        )
        if selected_primary != required_primary:
            raise PriorityPolicyError("all eligible primary/FDP targets must be selected")
        if len(selected_secondary) > self.secondary_budget:
            raise PriorityPolicyError("secondary target budget exceeded")
        if self.selected_targets != required_primary + selected_secondary:
            raise PriorityPolicyError(
                "selected targets must place primary/FDP before secondary targets"
            )

    @property
    def selected(self) -> CausalPriorityCandidate | None:
        return self.selected_targets[0] if self.selected_targets else None

    @property
    def selected_secondaries(self) -> tuple[CausalPriorityCandidate, ...]:
        """The independently added secondary targets within the support budget."""

        return tuple(
            item
            for item in self.selected_targets
            if item.source_role is CausalSourceRole.SECONDARY
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": PRIORITY_POLICY_VERSION,
            "calibration_status": PRIORITY_CALIBRATION_STATUS,
            "candidate_count": len(self.ordered),
            "selected_skill_id": (
                self.selected.skill_id if self.selected is not None else None
            ),
            "ordered": [item.to_dict() for item in self.ordered],
        }


def schedule_causal_priorities(
    candidates: Sequence[CausalPriorityCandidate],
    *,
    secondary_budget: int = 1,
) -> PrioritySchedule:
    """Order and select targets under the constrained priority policy.

    ``ordered`` remains the complete auditable order for compatibility and
    diagnostics.  ``selected_targets`` is the action-facing bounded set: every
    eligible FDP/PRIMARY target is placed before at most ``secondary_budget``
    eligible SECONDARY targets.  Suppressed and unresolved candidates may stay
    visible in ``ordered`` for audit, but can never be selected.
    """

    return ConstrainedPriorityScheduler(
        secondary_budget=secondary_budget
    ).schedule(candidates)


class ConstrainedPriorityScheduler:
    """Select teaching targets without pretending to perform graph search."""

    def __init__(self, *, secondary_budget: int = 1) -> None:
        if (
            isinstance(secondary_budget, bool)
            or not isinstance(secondary_budget, int)
            or not 0 <= secondary_budget <= 1
        ):
            raise PriorityPolicyError("secondary_budget must be an integer in [0, 1]")
        self.secondary_budget = secondary_budget

    def schedule(
        self,
        candidates: Sequence[CausalPriorityCandidate],
    ) -> PrioritySchedule:
        """Return complete order plus the bounded action-facing target set."""

        if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
            raise PriorityPolicyError("candidates must be a bounded sequence")
        if len(candidates) > MAX_PRIORITY_CANDIDATES:
            raise PriorityPolicyError("too many priority candidates")

        materialized = tuple(candidates)
        if any(not isinstance(item, CausalPriorityCandidate) for item in materialized):
            raise PriorityPolicyError("every item must be a CausalPriorityCandidate")

        identities = [
            (item.phase2_candidate_id, item.skill_id) for item in materialized
        ]
        if len(identities) != len(set(identities)):
            raise PriorityPolicyError("duplicate priority candidate identity")

        ordered = tuple(sorted(materialized, key=lambda item: item.sort_key))
        eligible = tuple(item for item in ordered if item.selection_eligible)
        primary_or_fdp = tuple(
            item for item in eligible if item.source_role_rank == 0
        )
        secondary = tuple(
            item for item in eligible if item.source_role is CausalSourceRole.SECONDARY
        )
        selected_targets = primary_or_fdp + secondary[: self.secondary_budget]
        return PrioritySchedule(
            ordered=ordered,
            selected_targets=selected_targets,
            secondary_budget=self.secondary_budget,
        )


# Both names are intentionally explicit in call sites and documentation.  The
# alias helps integrations migrate from the earlier function-oriented name
# without reintroducing the incorrect A* label.
PedagogicalTargetRanker = ConstrainedPriorityScheduler


__all__ = [
    "CausalPriorityCandidate",
    "ConstrainedPriorityScheduler",
    "CausalSourceRole",
    "EVIDENCE_STRENGTH_BY_GRADE",
    "INSTRUCTIONAL_IMPACT_BY_SKILL",
    "MAX_PRIORITY_CANDIDATES",
    "PRIORITY_CALIBRATION_STATUS",
    "PRIORITY_POLICY_VERSION",
    "PRIORITY_WEIGHTS",
    "PriorityPolicyError",
    "PrioritySchedule",
    "PedagogicalTargetRanker",
    "evidence_strength_for_grade",
    "instructional_impact_for_skill",
    "schedule_causal_priorities",
]
