"""Phase 3 v1 orchestration for one authoritative SQL-judge attempt.

The module keeps three responsibilities visibly separate:

* trusted observation admission (Q-matrix / verified Phase 2 rule);
* current teaching-target scheduling and support recommendation; and
* raw, versioned BKT persistence after a submission id exists.

It intentionally does not alter the Phase 1 verdict, generate a hint, infer a
student's mental state, or expose authoritative Q-matrix rows to the client.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.causal_priority_scheduler import (
    PRIORITY_CALIBRATION_STATUS,
    PRIORITY_POLICY_VERSION,
    CausalPriorityCandidate,
    PrioritySchedule,
    evidence_strength_for_grade,
    instructional_impact_for_skill,
    schedule_causal_priorities,
)
from core.phase3_bkt import BKT_PARAMETERS_V1, BKTParameters
from core.phase3_calibration import (
    ActiveBKTPolicy,
    UNCALIBRATED_STATUS,
    load_active_bkt_policy,
)
from core.phase3_observation import (
    ObservationBuildResult,
    ObservationResult,
    ObservationSpec,
    build_trusted_skill_observations,
)
from core.support_policy import (
    ADAPTATION_CALIBRATION_STATUS,
    CHALLENGE_INDEX_POLICY_VERSION,
    CHALLENGE_POLICY_VERSION,
    SUPPORT_POLICY_VERSION,
    ChallengeSignals,
    SupportDecision,
    SupportSignals,
    evaluate_challenge_index,
    evaluate_support_need,
)
from models.phase3_learning import SkillObservationEvent
from repository.phase3_behavior_repo import Phase3BehaviorEventRepository
from models.user import User
from repository.phase3_learning_repo import (
    Phase3LearningRepository,
    TrustedSkillObservationInput,
)
from repository.question_skill_repo import QuestionSkillRepository


LEARNING_UPDATE_SCHEMA_VERSION = "phase3.learning_update.v1"
RUNTIME_POLICY_VERSION = "phase3.runtime_policy.v1"
BEHAVIORAL_PROXY_VERSION = "phase3.behavioral_support_need.v1"
BEHAVIORAL_PROXY_STATUS = "BEHAVIORAL_SUPPORT_NEED_PROXY_V1"
ATTEMPT_CONTEXT_STATUS = "PRE_ATTEMPT_ASSISTANCE_NOT_TRACKED"
HISTORY_WINDOW = 10
FAILURE_STREAK_CAP = 3
SESSION_IDLE_RESET_SECONDS = 30 * 60
_RECURRENCE_DECAY = 0.80
_VALID_OBSERVATION_SOURCES = frozenset({"QUESTION_QMATRIX", "PHASE2_RULE"})
_SYNTAX_MARKERS = frozenset(
    {"SYNTAX_ERROR", "PARSE_ERROR", "NATIVE_SQL_PARSE_ERROR"}
)
_NON_SEMANTIC_MARKERS = frozenset(
    {
        "UNDECIDED",
        "PLATFORM_ERROR",
        "SAFETY_BLOCKED",
        "SAFETY_INTERCEPT",
    }
)


@dataclass(frozen=True, slots=True)
class SkillHistorySignals:
    """Bounded behavioral proxies from trusted observation events only.

    These fields describe recent audited interaction patterns.  They do not
    claim to measure fatigue, frustration, or any other latent mental state.
    ``None`` for ``behavioral_support_need`` means there is no active semantic
    failure evidence, not that a student's psychological need is zero.
    """

    recurrence: float
    failure_streak_norm: float
    recent_hint_ratio: float
    recent_unassisted_success: float
    behavioral_support_need: float | None = None
    active_event_count: int = 0
    semantic_failure_count: int = 0
    syntax_error_count: int = 0
    session_reset: bool = False
    semantic_failure_weight: float = 0.0


@dataclass(frozen=True, slots=True)
class Phase3AttemptPlan:
    """Read-only decision produced before the submission row is created."""

    expected_is_correct: bool
    admission: ObservationBuildResult
    persistence_inputs: tuple[TrustedSkillObservationInput, ...]
    schedule: PrioritySchedule
    support: SupportDecision | None
    # Exact trusted observation chosen for teaching.  Phase 4 must not infer
    # rule identity by joining display knowledge points or unrelated bundle
    # fields back onto the scheduler output.
    selected_target: ObservationSpec | None
    # The policy is captured during planning so the initial prior, persisted
    # state version, and public audit metadata cannot change halfway through
    # one submission attempt.
    bkt_parameters: BKTParameters = BKT_PARAMETERS_V1
    bkt_calibration_status: str = UNCALIBRATED_STATUS
    bkt_calibration_artifact_digest: str | None = None

    @property
    def selected(self) -> CausalPriorityCandidate | None:
        return self.schedule.selected

    @property
    def status(self) -> str:
        if not self.persistence_inputs:
            if self.admission.status == "SKIP_NO_ASSESSMENT_MAP":
                return "SKIP_NO_ASSESSMENT_MAP"
            return "NO_ELIGIBLE_OBSERVATION"
        return "READY"

    def no_update_summary(self) -> "Phase3LearningSummary":
        return Phase3LearningSummary(
            status=self.status,
            observation_count=len(self.persistence_inputs),
            state_update_count=0,
            priority_score=(
                self.selected.priority_score if self.selected is not None else None
            ),
            support_need=(
                self.support.support_need if self.support is not None else None
            ),
            recommended_support_level=(
                self.support.support_level if self.support is not None else None
            ),
            challenge_readiness=None,
            bkt_parameters=self.bkt_parameters,
            bkt_calibration_status=self.bkt_calibration_status,
            bkt_calibration_artifact_digest=self.bkt_calibration_artifact_digest,
        )


@dataclass(frozen=True, slots=True)
class Phase3LearningSummary:
    """Learner-safe aggregate; skill identities and Q-matrix rows stay private."""

    status: str
    observation_count: int
    state_update_count: int
    priority_score: float | None
    support_need: float | None
    recommended_support_level: int | None
    challenge_readiness: float | None
    behavioral_support_need: float | None = None
    behavioral_session_reset: bool = False
    semantic_failure_count: int = 0
    syntax_error_count: int = 0
    bkt_parameters: BKTParameters = BKT_PARAMETERS_V1
    bkt_calibration_status: str = UNCALIBRATED_STATUS
    bkt_calibration_artifact_digest: str | None = None

    @property
    def challenge_index(self) -> float | None:
        """Canonical name for the next-exercise difficulty signal."""

        return self.challenge_readiness

    def to_public_dict(
        self,
        *,
        support_recommendation_applied: bool = False,
        delivered_support_level: int | None = None,
    ) -> dict[str, Any]:
        def bounded(value: float | None) -> float | None:
            return round(value, 6) if value is not None else None

        if delivered_support_level is not None and (
            isinstance(delivered_support_level, bool)
            or not 1 <= delivered_support_level <= 4
        ):
            raise ValueError("delivered_support_level must be in [1, 4]")

        return {
            "schema_version": LEARNING_UPDATE_SCHEMA_VERSION,
            "runtime_policy_version": RUNTIME_POLICY_VERSION,
            "status": self.status,
            "observation_count": self.observation_count,
            "state_update_count": self.state_update_count,
            "bkt_parameter_version": self.bkt_parameters.version,
            "bkt_calibration_status": self.bkt_calibration_status,
            "bkt_calibration_artifact_digest": self.bkt_calibration_artifact_digest,
            "priority_policy_version": PRIORITY_POLICY_VERSION,
            # The internal score includes exact Q-matrix alignment.  Exposing
            # it would create a numeric oracle for PRIMARY/SUPPORTING roles.
            "support_policy_version": SUPPORT_POLICY_VERSION,
            "support_need": bounded(self.support_need),
            "recommended_support_level": self.recommended_support_level,
            # Phase 4 has not yet varied the generated hint.  Persisted event
            # assistance therefore records the actually delivered level, not
            # this recommendation.
            "support_recommendation_applied": support_recommendation_applied,
            "delivered_support_level": delivered_support_level,
            "challenge_policy_version": CHALLENGE_POLICY_VERSION,
            "challenge_index_policy_version": CHALLENGE_INDEX_POLICY_VERSION,
            "challenge_index": bounded(self.challenge_index),
            "challenge_readiness": bounded(self.challenge_readiness),
            "next_exercise_challenge_readiness": bounded(
                self.challenge_readiness
            ),
            "challenge_usage": "NEXT_EXERCISE_DIFFICULTY_ONLY",
            "challenge_aggregation_scope": "MIN_CURRENT_ATTEMPT_SKILLS",
            "behavioral_proxy_status": BEHAVIORAL_PROXY_STATUS,
            "behavioral_proxy_version": BEHAVIORAL_PROXY_VERSION,
            "behavioral_support_need": bounded(self.behavioral_support_need),
            "behavioral_session_reset": self.behavioral_session_reset,
            "semantic_failure_count": self.semantic_failure_count,
            "syntax_error_count": self.syntax_error_count,
            "attempt_context_status": ATTEMPT_CONTEXT_STATUS,
            "runtime_support_reachability": "L1_TO_L4_WITH_PHASE4_V1",
            "calibration_status": (
                PRIORITY_CALIBRATION_STATUS
                if PRIORITY_CALIBRATION_STATUS == ADAPTATION_CALIBRATION_STATUS
                else "UNCALIBRATED_MVP"
            ),
        }


def degraded_learning_summary() -> Phase3LearningSummary:
    """Stable fail-closed summary when Phase 3 cannot safely update state."""

    return Phase3LearningSummary(
        status="DEGRADED_NO_LEARNING_UPDATE",
        observation_count=0,
        state_update_count=0,
        priority_score=None,
        support_need=None,
        recommended_support_level=None,
        challenge_readiness=None,
    )


def _is_correct_event(event: Any) -> bool:
    return event.observation_result == "CORRECT"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _event_marker(event: Any, *names: str) -> str:
    for name in names:
        value = getattr(event, name, None)
        raw = getattr(value, "value", value)
        if isinstance(raw, str) and raw.strip():
            return raw.strip().upper()
    return ""


def _is_syntax_event(event: Any) -> bool:
    marker = _event_marker(
        event,
        "event_kind",
        "error_kind",
        "observation_result",
        "source_type",
    )
    return marker in _SYNTAX_MARKERS


def _is_observation_event(event: Any) -> bool:
    """Return true only for a persisted, semantically judged outcome."""

    if _is_syntax_event(event):
        return False
    kind = _event_marker(event, "event_kind", "error_kind")
    if kind in _NON_SEMANTIC_MARKERS:
        return False
    result = _event_marker(event, "observation_result")
    source = _event_marker(event, "source_type")
    return (
        result in {"CORRECT", "INCORRECT"}
        and source in _VALID_OBSERVATION_SOURCES
    )


def _active_history_window(
    events: Sequence[Any],
    *,
    reference_time: datetime,
) -> tuple[tuple[Any, ...], bool]:
    """Keep only the newest session, resetting after a long idle interval."""

    newest_time = _as_utc(getattr(events[0], "created_at", None))
    if newest_time is not None and (
        reference_time - newest_time
    ).total_seconds() > SESSION_IDLE_RESET_SECONDS:
        return (), True

    active: list[Any] = []
    previous_time = newest_time
    session_reset = False
    for event in events:
        event_time = _as_utc(getattr(event, "created_at", None))
        if (
            active
            and previous_time is not None
            and event_time is not None
            and (previous_time - event_time).total_seconds()
            > SESSION_IDLE_RESET_SECONDS
        ):
            session_reset = True
            break
        active.append(event)
        if event_time is not None:
            previous_time = event_time
    return tuple(active), session_reset


def _merge_history_events(
    *event_groups: Sequence[Any],
) -> tuple[Any, ...]:
    """Merge newest-first semantic and non-semantic audit streams.

    The event kinds intentionally remain separate at the model boundary.  The
    proxy needs one temporal window, however, so runtime reads merge them by
    their persisted timestamp.  Objects used by pure-function tests may omit a
    timestamp; in that case each input stream's existing newest-first order is
    preserved.
    """

    materialized = [event for group in event_groups for event in group]
    if not materialized:
        return ()
    if not any(_as_utc(getattr(event, "created_at", None)) is not None for event in materialized):
        return tuple(materialized[:HISTORY_WINDOW])

    epoch = datetime.min.replace(tzinfo=timezone.utc)

    def sort_key(index_and_event: tuple[int, Any]) -> tuple[datetime, int, int]:
        index, event = index_and_event
        event_time = _as_utc(getattr(event, "created_at", None)) or epoch
        raw_id = getattr(event, "id", 0)
        event_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else 0
        # Earlier input order wins a timestamp/id tie, keeping the result
        # deterministic for SQLite rows with equal second-resolution times.
        return event_time, event_id, -index

    ordered = sorted(enumerate(materialized), key=sort_key, reverse=True)
    return tuple(event for _, event in ordered[:HISTORY_WINDOW])


async def _recent_history_events(
    learning_repo: Phase3LearningRepository,
    behavior_repo: Phase3BehaviorEventRepository,
    *,
    user_id: int,
    taxonomy_version: str,
    skill_id: str,
    for_update: bool = False,
) -> tuple[Any, ...]:
    """Read the bounded semantic stream plus the user behavior stream."""

    semantic = await learning_repo.list_recent_events(
        user_id,
        taxonomy_version,
        skill_id,
        limit=HISTORY_WINDOW,
        for_update=for_update,
    )
    behavioral = await behavior_repo.list_recent_events(
        user_id,
        limit=HISTORY_WINDOW,
        for_update=for_update,
    )
    return _merge_history_events(semantic, behavioral)


def _behavioral_support_need(
    recurrence: float,
    failure_streak_norm: float,
) -> float:
    """Combine recent audited failure patterns into a named behavior proxy."""

    # This value is intentionally an observable support proxy, not a claim
    # about fatigue or frustration.  Keeping the weights explicit also makes
    # offline calibration and sensitivity analysis possible.
    return max(
        0.0,
        min(1.0, 0.65 * recurrence + 0.35 * failure_streak_norm),
    )


def summarize_skill_history(
    events: Sequence[Any],
    *,
    now: datetime | None = None,
) -> SkillHistorySignals:
    """Summarize newest-first trusted events into bounded policy inputs.

    Only audited observation outcomes participate.  A long idle interval
    starts a new behavioral window; platform failures, safety blocks,
    undecided results, and syntax/parse errors never become semantic failures.
    """

    bounded_events = tuple(events[:HISTORY_WINDOW])
    if not bounded_events:
        return SkillHistorySignals(0.0, 0.0, 0.0, 0.0)

    reference_time = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    if reference_time is None:
        raise TypeError("now must be a datetime")
    active_events, session_reset = _active_history_window(
        bounded_events,
        reference_time=reference_time,
    )
    if not active_events:
        return SkillHistorySignals(
            0.0,
            0.0,
            0.0,
            0.0,
            None,
            0,
            0,
            0,
            session_reset,
            0.0,
        )

    # Non-semantic records are deliberately absent from the recurrence
    # population.  They must not dilute a semantic failure ratio merely by
    # occupying one of the recent-event slots; the proxy is about audited
    # semantic attempts, while syntax/platform/safety/undecided outcomes are
    # tracked on their own axis.
    semantic_events: list[tuple[float, Any]] = []
    semantic_index = 0
    for event in active_events:
        if not _is_observation_event(event):
            continue
        semantic_events.append((_RECURRENCE_DECAY**semantic_index, event))
        semantic_index += 1
    total_weight = sum(weight for weight, _ in semantic_events)
    semantic_failure_events = [
        (weight, event)
        for weight, event in semantic_events
        if not _is_correct_event(event)
    ]
    semantic_failure_weight = sum(weight for weight, _ in semantic_failure_events)
    recurrence = (
        semantic_failure_weight / total_weight if total_weight > 0.0 else 0.0
    )

    failure_streak = 0
    for event in active_events:
        if not _is_observation_event(event):
            continue
        if _is_correct_event(event):
            break
        failure_streak += 1

    recent_hint_ratio = sum(
        1
        for event in active_events
        if _is_observation_event(event)
        and isinstance(getattr(event, "assistance_level", None), int)
        and not isinstance(getattr(event, "assistance_level", None), bool)
        and event.assistance_level > 1
    ) / len(semantic_events) if semantic_events else 0.0
    # ``assistance_level`` belongs to the feedback delivered *after* that
    # submission.  It cannot prove that the just-finished answer was produced
    # without prior help.  Until an explicit attempt-context link exists, the
    # honest fail-closed signal is zero.
    recent_unassisted_success = 0.0
    semantic_failure_count = len(semantic_failure_events)
    syntax_error_count = sum(1 for event in active_events if _is_syntax_event(event))
    behavioral_support_need = (
        _behavioral_support_need(
            recurrence,
            min(failure_streak / FAILURE_STREAK_CAP, 1.0),
        )
        if semantic_failure_count
        else None
    )
    return SkillHistorySignals(
        recurrence=recurrence,
        failure_streak_norm=min(failure_streak / FAILURE_STREAK_CAP, 1.0),
        recent_hint_ratio=recent_hint_ratio,
        recent_unassisted_success=recent_unassisted_success,
        behavioral_support_need=behavioral_support_need,
        active_event_count=len(semantic_events),
        semantic_failure_count=semantic_failure_count,
        syntax_error_count=syntax_error_count,
        session_reset=session_reset,
        semantic_failure_weight=semantic_failure_weight,
    )


def _question_alignment(question_skills: Sequence[Any], item: ObservationSpec) -> float:
    """Use only exact taxonomy+skill matches; no broad/atomic inference bridge."""

    best = 0.0
    for row in question_skills:
        if (
            getattr(row, "taxonomy_version", None) != item.taxonomy_version
            or getattr(row, "skill_id", None) != item.skill_id
        ):
            continue
        role = getattr(getattr(row, "role", None), "value", getattr(row, "role", None))
        if role == "PRIMARY":
            best = max(best, 1.0)
        elif role == "SUPPORTING":
            best = max(best, 0.5)
    return best


def _persistence_input(item: ObservationSpec) -> TrustedSkillObservationInput:
    # ObservationSpec owns the admission contract; the repository repeats the
    # source/result checks as a second defensive boundary.
    if hasattr(item, "to_persistence_kwargs"):
        return TrustedSkillObservationInput(**item.to_persistence_kwargs())
    return TrustedSkillObservationInput(
        taxonomy_version=item.taxonomy_version,
        skill_id=item.skill_id,
        is_correct=item.result is ObservationResult.CORRECT,
        source_type=item.source.value,
        source_version=item.source_version,
        evidence_grade=item.evidence_grade,
        phase2_candidate_id=item.phase2_candidate_id,
        rule_id=item.phase2_rule_id,
        source_role=item.source_role,
        logical_stage=item.logical_stage,
        source_provenance=item.qmatrix_provenance,
    )


async def prepare_phase3_attempt(
    session: AsyncSession,
    *,
    user_id: int,
    question_id: int,
    expected_is_correct: bool,
    diagnostic_package: Mapping[str, Any],
    answer_revealed: bool = False,
    bkt_policy: ActiveBKTPolicy | None = None,
    lock_for_update: bool = True,
) -> Phase3AttemptPlan:
    """Admit observations and calculate the pre-feedback Phase 3 decision.

    The normal route uses a read-only provisional plan before calling the
    external Phase 5 editor, then recomputes with ``lock_for_update=True``
    immediately before persistence.  Direct callers retain the original
    locked behavior by default.
    """

    if type(expected_is_correct) is not bool:
        raise TypeError("expected_is_correct must be bool")
    active_bkt_policy = bkt_policy or load_active_bkt_policy()
    question_skills = await QuestionSkillRepository(session).list_by_question_id(
        question_id
    )
    admission = build_trusted_skill_observations(
        diagnostic_package,
        question_skills,
        answer_revealed=answer_revealed,
    )
    expected_verdict = "CORRECT" if expected_is_correct else "INCORRECT"
    observations = (
        admission.observations if admission.verdict == expected_verdict else ()
    )
    persistence_inputs = tuple(_persistence_input(item) for item in observations)

    learning_repo = Phase3LearningRepository(session)
    behavior_repo = Phase3BehaviorEventRepository(session)
    priority_candidates: list[CausalPriorityCandidate] = []
    selected_history_by_identity: dict[tuple[str, str], SkillHistorySignals] = {}
    selected_mastery_by_identity: dict[tuple[str, str], float] = {}
    negative_observations = tuple(
        item
        for item in observations
        if item.result is ObservationResult.INCORRECT
        and item.trusted_atomic_observation
    )
    if negative_observations and lock_for_update:
        # Hold a short per-user lock from state/history sampling through the
        # eventual BKT write.  Without it, two simultaneous submissions could
        # persist correct BKT transitions yet base support/priority on the same
        # stale pre-state.
        locked_user_id = await session.scalar(
            select(User.id).where(User.id == user_id).with_for_update()
        )
        if locked_user_id is None:
            raise ValueError("Phase 3 attempt user does not exist")

    for item in negative_observations:
        state = await learning_repo.get_state(
            user_id,
            item.taxonomy_version,
            item.skill_id,
            for_update=lock_for_update,
        )
        mastery = (
            state.next_prior
            if state is not None
            else active_bkt_policy.parameters.initial_mastery
        )
        events = await _recent_history_events(
            learning_repo,
            behavior_repo,
            user_id=user_id,
            taxonomy_version=item.taxonomy_version,
            skill_id=item.skill_id,
            for_update=lock_for_update,
        )
        history = summarize_skill_history(events)
        identity = (item.taxonomy_version, item.skill_id)
        selected_history_by_identity[identity] = history
        selected_mastery_by_identity[identity] = mastery
        priority_candidates.append(
            CausalPriorityCandidate(
                skill_id=item.skill_id,
                taxonomy_version=item.taxonomy_version,
                source_role=item.source_role or "SECONDARY",
                logical_stage=item.logical_stage or "EXTENSION",
                phase2_candidate_id=item.phase2_candidate_id or item.observation_id,
                trusted_atomic_observation=True,
                instructional_impact=instructional_impact_for_skill(item.skill_id),
                recurrence=history.recurrence,
                mastery_deficit=1.0 - mastery,
                question_alignment=_question_alignment(question_skills, item),
                evidence_strength=evidence_strength_for_grade(item.evidence_grade),
            )
        )

    # The scheduler returns a full audit order but exposes at most one
    # independent secondary target to the action-facing selection set.
    schedule = schedule_causal_priorities(
        priority_candidates,
        secondary_budget=1,
    )
    selected_target = None
    if schedule.selected is not None:
        selected_target = next(
            (
                item
                for item in negative_observations
                if item.taxonomy_version == schedule.selected.taxonomy_version
                and item.skill_id == schedule.selected.skill_id
                and (item.phase2_candidate_id or item.observation_id)
                == schedule.selected.phase2_candidate_id
            ),
            None,
        )
        if selected_target is None:
            raise RuntimeError("selected causal target lost its trusted observation")
    support = None
    if schedule.selected is not None:
        identity = (
            schedule.selected.taxonomy_version,
            schedule.selected.skill_id,
        )
        history = selected_history_by_identity[identity]
        # The current supported failure is part of the streak that determines
        # the help recommendation, even though its audit event is written only
        # after the Submission row exists.
        current_failure_streak = min(
            history.failure_streak_norm + 1.0 / FAILURE_STREAK_CAP,
            1.0,
        )
        support = evaluate_support_need(
            SupportSignals(
                mastery=selected_mastery_by_identity[identity],
                failure_streak_norm=current_failure_streak,
                recent_hint_ratio=history.recent_hint_ratio,
                # This is a recent audited behavior proxy, not a claim about
                # psychological fatigue.  It is absent until semantic failure
                # evidence exists, so unknown is not treated as zero need.
                behavioral_support_need=history.behavioral_support_need,
                recent_unassisted_success=history.recent_unassisted_success,
            )
        )

    return Phase3AttemptPlan(
        expected_is_correct=expected_is_correct,
        admission=admission,
        persistence_inputs=persistence_inputs,
        schedule=schedule,
        support=support,
        selected_target=selected_target,
        bkt_parameters=active_bkt_policy.parameters,
        bkt_calibration_status=active_bkt_policy.calibration_status,
        bkt_calibration_artifact_digest=active_bkt_policy.artifact_digest_sha256,
    )


async def apply_phase3_attempt(
    session: AsyncSession,
    *,
    plan: Phase3AttemptPlan,
    submission_id: int,
    user_id: int,
    question_id: int,
    delivered_assistance_level: int = 1,
    answer_revealed: bool = False,
) -> Phase3LearningSummary:
    """Persist a prepared plan and calculate next-exercise readiness."""

    if not plan.persistence_inputs:
        return plan.no_update_summary()
    learning_repo = Phase3LearningRepository(session)
    behavior_repo = Phase3BehaviorEventRepository(session)
    applied = await learning_repo.apply_trusted_observations(
        submission_id=submission_id,
        user_id=user_id,
        question_id=question_id,
        observations=plan.persistence_inputs,
        assistance_level=delivered_assistance_level,
        answer_revealed=answer_revealed,
        parameters=plan.bkt_parameters,
    )

    newly_applied = sum(1 for item in applied if item.created)
    readiness_values: list[float] = []
    behavioral_values: list[float] = []
    behavioral_session_reset = False
    semantic_failure_counts: list[int] = []
    syntax_error_counts: list[int] = []
    readiness_items = applied if newly_applied else ()
    for item in readiness_items:
        state = await learning_repo.get_state(
            user_id,
            item.taxonomy_version,
            item.skill_id,
            for_update=True,
        )
        if state is None:
            raise RuntimeError("applied BKT state is missing")
        events = await _recent_history_events(
            learning_repo,
            behavior_repo,
            user_id=user_id,
            taxonomy_version=item.taxonomy_version,
            skill_id=item.skill_id,
            for_update=True,
        )
        history = summarize_skill_history(events)
        if history.behavioral_support_need is not None:
            behavioral_values.append(history.behavioral_support_need)
        semantic_failure_counts.append(history.semantic_failure_count)
        syntax_error_counts.append(history.syntax_error_count)
        behavioral_session_reset = behavioral_session_reset or history.session_reset
        readiness_values.append(
            evaluate_challenge_index(
                ChallengeSignals(
                    # next_prior, not a display EMA, feeds a future decision.
                    mastery=state.next_prior,
                    # Post-submission feedback metadata is not evidence that
                    # the answer itself was unassisted.
                    recent_unassisted_success=history.recent_unassisted_success,
                    behavioral_support_need=history.behavioral_support_need,
                )
            ).challenge_index
        )

    status = "UPDATED" if newly_applied else "ALREADY_APPLIED"
    return Phase3LearningSummary(
        status=status,
        observation_count=len(plan.persistence_inputs),
        state_update_count=newly_applied,
        priority_score=(
            plan.selected.priority_score if plan.selected is not None else None
        ),
        support_need=(
            plan.support.support_need if plan.support is not None else None
        ),
        recommended_support_level=(
            plan.support.support_level if plan.support is not None else None
        ),
        # Multiple PRIMARY skills remain separate states.  The conservative
        # minimum prevents one strong skill from hiding another weak target.
        challenge_readiness=min(readiness_values) if readiness_values else None,
        behavioral_support_need=(
            max(behavioral_values) if behavioral_values else None
        ),
        behavioral_session_reset=behavioral_session_reset,
        semantic_failure_count=(
            max(semantic_failure_counts) if semantic_failure_counts else 0
        ),
        syntax_error_count=max(syntax_error_counts) if syntax_error_counts else 0,
        bkt_parameters=plan.bkt_parameters,
        bkt_calibration_status=plan.bkt_calibration_status,
        bkt_calibration_artifact_digest=plan.bkt_calibration_artifact_digest,
    )


__all__ = [
    "ATTEMPT_CONTEXT_STATUS",
    "BEHAVIORAL_PROXY_STATUS",
    "BEHAVIORAL_PROXY_VERSION",
    "FAILURE_STREAK_CAP",
    "HISTORY_WINDOW",
    "LEARNING_UPDATE_SCHEMA_VERSION",
    "RUNTIME_POLICY_VERSION",
    "SESSION_IDLE_RESET_SECONDS",
    "Phase3AttemptPlan",
    "Phase3LearningSummary",
    "SkillHistorySignals",
    "apply_phase3_attempt",
    "degraded_learning_summary",
    "prepare_phase3_attempt",
    "summarize_skill_history",
]
