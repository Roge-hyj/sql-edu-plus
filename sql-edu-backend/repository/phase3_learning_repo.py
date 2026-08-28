"""Persistence boundary for trusted Phase 3 learning observations."""

from collections.abc import Sequence
from dataclasses import dataclass
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.phase3_bkt import BKT_PARAMETERS_V1, BKTParameters, update_bkt
from core.phase3_skill_catalog import (
    ALLOWED_LOGICAL_STAGES_BY_TEACHING_STAGE,
    ATOMIC_SKILL_TAXONOMY_VERSION,
    RULE_SKILL_MAP,
    RULE_SKILL_MAP_VERSION,
    STRONG_EVIDENCE_GRADES,
)
from core.sql_knowledge_points import get_knowledge_point_by_id
from models.phase3_learning import (
    SkillObservationEvent,
    SkillObservationResult,
    SkillObservationSource,
    StudentSkillState,
)
from models.question_skill import (
    SQL_KNOWLEDGE_TAXONOMY_VERSION,
    QuestionSkill,
)
from models.submission import Submission
from models.user import User


_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_QUESTION_QMATRIX_SOURCE_VERSION = "question_skill_mapping.v1"
_TRUSTED_QMATRIX_PROVENANCE = frozenset({"AUTHOR_DECLARED", "GENERATED"})
_QMatrix_PROVENANCE_ALIASES = {
    "AI_GENERATED": "GENERATED",
    "INFERRED_REVIEWED": "INFERRED",
}
_ALLOWED_PHASE2_ROLES = frozenset({"FDP", "PRIMARY", "SECONDARY"})


def _enum_value(value: str | SkillObservationSource) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


def _canonical_qmatrix_provenance(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return _QMatrix_PROVENANCE_ALIASES.get(text, text)


def _known_skill(taxonomy_version: str, skill_id: str) -> bool:
    if taxonomy_version == SQL_KNOWLEDGE_TAXONOMY_VERSION:
        return get_knowledge_point_by_id(skill_id) is not None
    if taxonomy_version == ATOMIC_SKILL_TAXONOMY_VERSION:
        return skill_id in {item.skill_id for item in RULE_SKILL_MAP.values()}
    return False


def _phase2_logical_stage_is_valid(rule_id: str, logical_stage: str | None) -> bool:
    spec = RULE_SKILL_MAP.get(rule_id)
    if spec is None or logical_stage is None:
        return False
    return logical_stage in ALLOWED_LOGICAL_STAGES_BY_TEACHING_STAGE.get(
        spec.teaching_stage, frozenset()
    )


@dataclass(frozen=True, slots=True)
class TrustedSkillObservationInput:
    """A skill observation already admitted by the Phase 3 trust gate.

    This repository does not infer observations from SQL, Phase 2 display
    knowledge points, or reference-query syntax.  Callers must construct these
    inputs from the authoritative Q-matrix or the fail-closed Phase 2 rule map.
    """

    taxonomy_version: str
    skill_id: str
    is_correct: bool
    source_type: SkillObservationSource | str
    source_version: str
    evidence_grade: str | None = None
    phase2_candidate_id: str | None = None
    rule_id: str | None = None
    source_role: str | None = None
    logical_stage: str | None = None
    source_provenance: str | None = None

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("taxonomy_version", self.taxonomy_version, 64),
            ("skill_id", self.skill_id, 128),
        ):
            if len(value) > maximum or not _IDENTIFIER_RE.fullmatch(value):
                raise ValueError(f"invalid {name}")
        if not isinstance(self.is_correct, bool):
            raise TypeError("is_correct must be bool")
        source = _enum_value(self.source_type)
        if source not in {item.value for item in SkillObservationSource}:
            raise ValueError("unsupported observation source_type")
        if not self.source_version or len(self.source_version) > 64:
            raise ValueError("source_version must be 1..64 characters")
        for name, value, maximum in (
            ("evidence_grade", self.evidence_grade, 32),
            ("phase2_candidate_id", self.phase2_candidate_id, 128),
            ("rule_id", self.rule_id, 64),
            ("source_role", self.source_role, 32),
            ("logical_stage", self.logical_stage, 32),
            ("source_provenance", self.source_provenance, 32),
        ):
            if value is not None and len(str(value)) > maximum:
                raise ValueError(f"{name} is too long")

        if self.is_correct:
            if source != SkillObservationSource.QUESTION_QMATRIX.value:
                raise ValueError(
                    "positive observations require authoritative Q-matrix source"
                )
            if self.source_version != _QUESTION_QMATRIX_SOURCE_VERSION:
                raise ValueError("positive observations require Q-matrix source version")
            provenance = _canonical_qmatrix_provenance(self.source_provenance)
            if provenance not in _TRUSTED_QMATRIX_PROVENANCE:
                raise ValueError(
                    "positive observations require AUTHOR_DECLARED or GENERATED provenance"
                )
            if self.source_role != "PRIMARY":
                raise ValueError(
                    "positive observations require a PRIMARY Q-matrix role"
                )
            if not _known_skill(self.taxonomy_version, self.skill_id):
                raise ValueError("positive observation skill is not in its taxonomy")
            object.__setattr__(self, "source_provenance", provenance)
        else:
            if source != SkillObservationSource.PHASE2_RULE.value:
                raise ValueError(
                    "negative observations require verified Phase 2 rule source"
                )
            if self.source_version != RULE_SKILL_MAP_VERSION:
                raise ValueError("negative observations require the Phase 3 rule map")
            if self.taxonomy_version != ATOMIC_SKILL_TAXONOMY_VERSION:
                raise ValueError(
                    "negative observations require the atomic skill taxonomy"
                )
            if self.rule_id is None or self.rule_id not in RULE_SKILL_MAP:
                raise ValueError("negative observation rule_id is not in the rule map")
            mapped_skill = RULE_SKILL_MAP[self.rule_id].skill_id
            if self.skill_id != mapped_skill:
                raise ValueError(
                    "negative observation skill_id does not match its rule map entry"
                )
            if self.evidence_grade not in STRONG_EVIDENCE_GRADES:
                raise ValueError(
                    "negative observations require strong Phase 2 evidence"
                )
            if self.phase2_candidate_id is None:
                raise ValueError("negative observations require a Phase 2 candidate")
            if self.source_role not in _ALLOWED_PHASE2_ROLES:
                raise ValueError("negative observation source_role is invalid")
            if not _phase2_logical_stage_is_valid(self.rule_id, self.logical_stage):
                raise ValueError(
                    "negative observation logical_stage does not match its rule"
                )
            if self.source_provenance is not None:
                raise ValueError("Phase 2 negative observations cannot carry Q-matrix provenance")


@dataclass(frozen=True, slots=True)
class AppliedSkillObservation:
    event_id: int
    taxonomy_version: str
    skill_id: str
    is_correct: bool
    prior_mastery: float
    posterior_mastery: float
    next_prior: float
    state_version: int
    created: bool


class Phase3LearningRepository:
    """Apply BKT updates and audit events without owning the transaction."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_state(
        self,
        user_id: int,
        taxonomy_version: str,
        skill_id: str,
        *,
        for_update: bool = False,
    ) -> StudentSkillState | None:
        stmt = select(StudentSkillState).where(
            StudentSkillState.user_id == user_id,
            StudentSkillState.taxonomy_version == taxonomy_version,
            StudentSkillState.skill_id == skill_id,
        )
        if for_update:
            # Current read under MySQL REPEATABLE READ; required after waiting
            # on the per-user serialization lock.
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    async def list_states(
        self,
        user_id: int,
        *,
        taxonomy_version: str | None = None,
    ) -> list[StudentSkillState]:
        stmt = select(StudentSkillState).where(StudentSkillState.user_id == user_id)
        if taxonomy_version is not None:
            stmt = stmt.where(
                StudentSkillState.taxonomy_version == taxonomy_version
            )
        stmt = stmt.order_by(
            StudentSkillState.taxonomy_version,
            StudentSkillState.skill_id,
        )
        return list((await self.session.scalars(stmt)).all())

    async def list_recent_events(
        self,
        user_id: int,
        taxonomy_version: str,
        skill_id: str,
        *,
        limit: int = 10,
        for_update: bool = False,
    ) -> list[SkillObservationEvent]:
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        stmt = (
            select(SkillObservationEvent)
            .where(
                SkillObservationEvent.user_id == user_id,
                SkillObservationEvent.taxonomy_version == taxonomy_version,
                SkillObservationEvent.skill_id == skill_id,
            )
            .order_by(
                SkillObservationEvent.created_at.desc(),
                SkillObservationEvent.id.desc(),
            )
            .limit(limit)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return list((await self.session.scalars(stmt)).all())

    async def apply_trusted_observations(
        self,
        *,
        submission_id: int,
        user_id: int,
        question_id: int,
        observations: Sequence[TrustedSkillObservationInput],
        assistance_level: int = 1,
        answer_revealed: bool = False,
        parameters: BKTParameters = BKT_PARAMETERS_V1,
    ) -> list[AppliedSkillObservation]:
        """Persist a group of trusted observations exactly once.

        The caller owns commit/rollback.  A lock on the existing user row
        serializes state creation and update in MySQL, including the first
        observation for a skill where no state row exists yet.
        """

        if isinstance(assistance_level, bool) or not 1 <= assistance_level <= 4:
            raise ValueError("assistance_level must be an integer in [1, 4]")
        if not isinstance(answer_revealed, bool):
            raise TypeError("answer_revealed must be bool")

        # Canonicalize and reject contradictory duplicates before touching DB.
        unique: dict[tuple[str, str], TrustedSkillObservationInput] = {}
        for observation in observations:
            key = (observation.taxonomy_version, observation.skill_id)
            existing = unique.get(key)
            if existing is not None and existing != observation:
                raise ValueError("conflicting observations for the same skill")
            unique[key] = observation
        ordered = [unique[key] for key in sorted(unique)]
        if not ordered:
            return []

        if answer_revealed:
            raise ValueError(
                "answer-revealed submissions cannot create learning observations"
            )

        for observation in ordered:
            source = _enum_value(observation.source_type)
            if observation.is_correct:
                if answer_revealed:
                    raise ValueError(
                        "answer-revealed submissions cannot create positive observations"
                    )

        positive_observations = [item for item in ordered if item.is_correct]
        if positive_observations:
            rows = list(
                (
                    await self.session.scalars(
                        select(QuestionSkill).where(
                            QuestionSkill.question_id == question_id
                        )
                    )
                ).all()
            )
            authoritative_rows = {
                (row.taxonomy_version, row.skill_id): row for row in rows
            }
            for observation in positive_observations:
                row = authoritative_rows.get(
                    (observation.taxonomy_version, observation.skill_id)
                )
                row_provenance = _canonical_qmatrix_provenance(
                    getattr(row, "provenance", None)
                )
                row_role = _enum_value(getattr(row, "role", "")) if row else None
                if (
                    row is None
                    or row_role != "PRIMARY"
                    or row.observable_on_correct is not True
                    or row_provenance not in _TRUSTED_QMATRIX_PROVENANCE
                    or row_provenance
                    != _canonical_qmatrix_provenance(observation.source_provenance)
                ):
                    raise ValueError(
                        "positive observations require a matching persisted authoritative Q-matrix mapping"
                    )

        locked_user_id = await self.session.scalar(
            select(User.id).where(User.id == user_id).with_for_update()
        )
        if locked_user_id is None:
            raise ValueError("learning observation user does not exist")

        submission = await self.session.scalar(
            select(Submission)
            .where(Submission.id == submission_id)
            .with_for_update()
        )
        if submission is None:
            raise ValueError("learning observation submission does not exist")
        if submission.user_id != user_id or submission.question_id != question_id:
            raise ValueError("submission ownership does not match observation context")
        if any(
            observation.is_correct is not submission.is_correct
            for observation in ordered
        ):
            raise ValueError(
                "observation result does not match the judged submission result"
            )
        if submission.hint_level != assistance_level:
            raise ValueError(
                "observation assistance level does not match delivered submission hint level"
            )

        applied: list[AppliedSkillObservation] = []
        for observation in ordered:
            existing_event = await self.session.scalar(
                select(SkillObservationEvent)
                .where(
                    SkillObservationEvent.submission_id == submission_id,
                    SkillObservationEvent.taxonomy_version
                    == observation.taxonomy_version,
                    SkillObservationEvent.skill_id == observation.skill_id,
                )
                # MySQL's locking read is a current read even under the
                # default REPEATABLE READ isolation.  A plain snapshot could
                # miss an event committed while this transaction waited on
                # the per-user lock and then collide with the unique key.
                .with_for_update()
            )
            if existing_event is not None:
                expected_result = (
                    SkillObservationResult.CORRECT.value
                    if observation.is_correct
                    else SkillObservationResult.INCORRECT.value
                )
                expected_candidate_id = (
                    str(observation.phase2_candidate_id)
                    if observation.phase2_candidate_id is not None
                    else None
                )
                provenance_matches = (
                    existing_event.observation_result == expected_result
                    and existing_event.source_type
                    == _enum_value(observation.source_type)
                    and existing_event.source_version
                    == observation.source_version
                    and existing_event.evidence_grade
                    == observation.evidence_grade
                    and existing_event.phase2_candidate_id
                    == expected_candidate_id
                    and existing_event.rule_id == observation.rule_id
                    and existing_event.source_role == observation.source_role
                    and existing_event.logical_stage
                    == observation.logical_stage
                    and existing_event.source_provenance
                    == observation.source_provenance
                    and existing_event.assistance_level == assistance_level
                    and existing_event.answer_revealed is answer_revealed
                )
                if not provenance_matches:
                    raise ValueError(
                        "persisted observation conflicts with idempotent retry"
                    )
                applied.append(self._snapshot(existing_event, created=False))
                continue

            state = await self.session.scalar(
                select(StudentSkillState)
                .where(
                    StudentSkillState.user_id == user_id,
                    StudentSkillState.taxonomy_version
                    == observation.taxonomy_version,
                    StudentSkillState.skill_id == observation.skill_id,
                )
                .with_for_update()
            )
            prior = (
                state.next_prior
                if state is not None
                else parameters.initial_mastery
            )
            if (
                state is not None
                and state.bkt_parameter_version != parameters.version
            ):
                raise ValueError(
                    "BKT parameter version transition requires an explicit migration"
                )
            update = update_bkt(
                prior,
                is_correct=observation.is_correct,
                parameters=parameters,
            )

            if state is None:
                state = StudentSkillState(
                    user_id=user_id,
                    taxonomy_version=observation.taxonomy_version,
                    skill_id=observation.skill_id,
                    posterior_mastery=update.posterior_mastery,
                    next_prior=update.next_prior,
                    observation_count=1,
                    bkt_parameter_version=parameters.version,
                    state_version=1,
                )
                self.session.add(state)
            else:
                state.posterior_mastery = update.posterior_mastery
                state.next_prior = update.next_prior
                state.observation_count += 1
                state.bkt_parameter_version = parameters.version
                state.state_version += 1

            event = SkillObservationEvent(
                submission_id=submission_id,
                user_id=user_id,
                question_id=question_id,
                taxonomy_version=observation.taxonomy_version,
                skill_id=observation.skill_id,
                observation_result=(
                    SkillObservationResult.CORRECT.value
                    if observation.is_correct
                    else SkillObservationResult.INCORRECT.value
                ),
                source_type=_enum_value(observation.source_type),
                source_version=observation.source_version,
                evidence_grade=observation.evidence_grade,
                phase2_candidate_id=(
                    str(observation.phase2_candidate_id)
                    if observation.phase2_candidate_id is not None
                    else None
                ),
                rule_id=observation.rule_id,
                source_role=observation.source_role,
                logical_stage=observation.logical_stage,
                source_provenance=observation.source_provenance,
                assistance_level=assistance_level,
                answer_revealed=answer_revealed,
                prior_mastery=update.prior_mastery,
                posterior_mastery=update.posterior_mastery,
                next_prior=update.next_prior,
                bkt_parameter_version=parameters.version,
                state_version=state.state_version,
            )
            self.session.add(event)
            # Flush per item makes an immediate retry in this transaction see
            # the unique event and materializes its audit id.
            await self.session.flush()
            applied.append(self._snapshot(event, created=True))

        return applied

    @staticmethod
    def _snapshot(
        event: SkillObservationEvent,
        *,
        created: bool,
    ) -> AppliedSkillObservation:
        if event.id is None:
            raise RuntimeError("observation event has not been flushed")
        return AppliedSkillObservation(
            event_id=event.id,
            taxonomy_version=event.taxonomy_version,
            skill_id=event.skill_id,
            is_correct=(
                event.observation_result == SkillObservationResult.CORRECT.value
            ),
            prior_mastery=event.prior_mastery,
            posterior_mastery=event.posterior_mastery,
            next_prior=event.next_prior,
            state_version=event.state_version,
            created=created,
        )


__all__ = [
    "AppliedSkillObservation",
    "Phase3LearningRepository",
    "TrustedSkillObservationInput",
]
