"""Persistence boundary for non-semantic Phase 3 behavior events."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.phase3_learning import Phase3BehaviorEvent, Phase3BehaviorEventKind
from models.submission import Submission


_EVENT_KINDS = frozenset(item.value for item in Phase3BehaviorEventKind)


def _event_kind_value(value: Phase3BehaviorEventKind | str) -> str:
    raw = getattr(value, "value", value)
    if not isinstance(raw, str) or raw not in _EVENT_KINDS:
        raise ValueError("unsupported Phase 3 behavior event kind")
    return raw


class Phase3BehaviorEventRepository:
    """Store and read behavior events without exposing them as BKT evidence."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def record_once(
        self,
        *,
        submission_id: int,
        user_id: int,
        question_id: int,
        event_kind: Phase3BehaviorEventKind | str,
    ) -> Phase3BehaviorEvent:
        """Record one classified outcome for a committed submission context.

        The caller owns the surrounding transaction.  Repeating the same
        classification is idempotent; changing a submission's classification
        is rejected because it would corrupt the behavioral audit trail.
        """

        kind = _event_kind_value(event_kind)
        submission = await self.session.scalar(
            select(Submission)
            .where(Submission.id == submission_id)
            .with_for_update()
        )
        if submission is None:
            raise ValueError("behavior event submission does not exist")
        if submission.user_id != user_id or submission.question_id != question_id:
            raise ValueError("behavior event submission context does not match")

        existing = await self.session.scalar(
            select(Phase3BehaviorEvent)
            .where(Phase3BehaviorEvent.submission_id == submission_id)
            .with_for_update()
        )
        if existing is not None:
            if (
                existing.user_id != user_id
                or existing.question_id != question_id
                or existing.event_kind != kind
            ):
                raise ValueError("conflicting behavior event for submission")
            return existing

        event = Phase3BehaviorEvent(
            submission_id=submission_id,
            user_id=user_id,
            question_id=question_id,
            event_kind=kind,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def list_recent_events(
        self,
        user_id: int,
        *,
        question_id: int | None = None,
        limit: int = 10,
        for_update: bool = False,
    ) -> list[Phase3BehaviorEvent]:
        """Return newest-first behavior events for proxy/window calculation."""

        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        stmt = select(Phase3BehaviorEvent).where(
            Phase3BehaviorEvent.user_id == user_id
        )
        if question_id is not None:
            stmt = stmt.where(Phase3BehaviorEvent.question_id == question_id)
        stmt = stmt.order_by(
            Phase3BehaviorEvent.created_at.desc(),
            Phase3BehaviorEvent.id.desc(),
        ).limit(limit)
        if for_update:
            stmt = stmt.with_for_update()
        return list((await self.session.scalars(stmt)).all())


__all__ = ["Phase3BehaviorEventRepository"]
