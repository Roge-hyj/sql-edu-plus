"""Run one fresh external SQL record through the complete application chain.

This is an audit harness, not a production path.  It consumes an enriched
record produced from a newly downloaded public corpus and calls the same
``routers.ai.check_sql`` function used by the API.  The in-memory database is
only the business store for this bounded rehearsal; Phase 1 still runs its
real bounded worker and the SQL/schema are taken from the external record.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import suppress
import json
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "sql-edu-backend"
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.phase3_skill_catalog import (  # noqa: E402
    ATOMIC_SKILL_TAXONOMY_VERSION,
)
from models import Base  # noqa: E402
from models.chat import ChatMessage  # noqa: E402
from models.phase3_learning import (  # noqa: E402
    Phase3BehaviorEvent,
    SkillObservationEvent,
    StudentSkillState,
)
from models.question import Question  # noqa: E402
from models.question_skill import (  # noqa: E402
    QuestionSkillProvenance,
    QuestionSkillRole,
)
from models.submission import Submission  # noqa: E402
from models.submission_teaching_audit import SubmissionTeachingAudit  # noqa: E402
from models.user import User  # noqa: E402
from repository.question_skill_repo import (  # noqa: E402
    QuestionSkillRepository,
    QuestionSkillSpec,
)
from routers.ai import SQLCheckRequest, check_sql  # noqa: E402
import routers.ai as ai_router  # noqa: E402

from run_phase1_cfg_convergence_benchmark import _web_mutations  # noqa: E402


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--corpus",
        type=Path,
        required=True,
        help="enriched JSONL containing external SQL and schema_catalog",
    )
    parser.add_argument("--sample-id", type=int, default=9)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/tmp/sql-edu-new-web/full_chain_audit.json"),
    )
    return parser.parse_args()


def _load_record(path: Path, sample_id: int) -> dict[str, Any]:
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("sample_id") == sample_id:
            return item
    raise ValueError(f"sample_id {sample_id} not found in {path}")


def _schema_preview(record: dict[str, Any]) -> str:
    catalog = record.get("schema_catalog")
    if not isinstance(catalog, dict) or not isinstance(catalog.get("tables"), list):
        raise ValueError("external record has no authoritative schema catalog")
    # Keep the source table/column names; the application sanitizer will apply
    # its learner-facing redaction and size limits at the route boundary.
    return json.dumps(
        {"tables": catalog["tables"]},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _response_summary(response: Any) -> dict[str, Any]:
    phase3 = response.phase3_learning or {}
    support = response.teaching_support or {}
    return {
        "submission_id": response.submission_id,
        "attempt_id": str(response.attempt_id),
        "idempotency_replayed": bool(getattr(response, "idempotency_replayed", False)),
        "is_correct": response.is_correct,
        "judge_status": response.judge_status,
        "is_safety_blocked": response.is_safety_blocked,
        "phase3_status": phase3.get("status"),
        "phase3_observation_count": phase3.get("observation_count"),
        "phase3_state_update_count": phase3.get("state_update_count"),
        "phase3_support_level": phase3.get("delivered_support_level"),
        "teaching_status": support.get("status"),
        "teaching_feedback_status": support.get("feedback_status"),
        "diagnostic_package_exposed": response.diagnostic_package is not None,
        "raw_observation_exposed": response.observation is not None,
        "error_attributions_exposed": bool(response.error_attributions),
        "hint_length": len(response.hint.get("overall_comment", "")),
    }


async def _count(session: AsyncSession, model: Any) -> int:
    value = await session.scalar(select(func.count()).select_from(model))
    return int(value or 0)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    print("loading external record", flush=True)
    record = _load_record(args.corpus, args.sample_id)
    standard_sql = str(record["sql"])
    mutations = _web_mutations(
        standard_sql,
        str(record["schema"]),
        "mysql",
        record.get("schema_catalog"),
    )
    selected_mutation = next(
        (item for item in mutations if item[0] == "is_not_null_to_null"),
        mutations[0] if mutations else None,
    )
    if selected_mutation is None:
        raise ValueError("the selected external record has no bounded semantic mutation")
    mutation_name, incorrect_sql, mutation_labels = selected_mutation

    previous_backend = ai_router.settings.PARSEVAL_EXECUTION_BACKEND
    ai_router.settings.PARSEVAL_EXECUTION_BACKEND = "sqlite"
    print("creating audit database", flush=True)
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    result: dict[str, Any] = {
        "source": {
            "source_id": record.get("source_id"),
            "source_url": record.get("source_url"),
            "sample_id": record.get("sample_id"),
            "provenance_hash": record.get("provenance_hash"),
            "schema_trust": record.get("schema_trust"),
            "table_count": len(record.get("schema_catalog", {}).get("tables", [])),
            "query_length": len(standard_sql),
        },
        "selected_mutation": {
            "name": mutation_name,
            "labels": mutation_labels,
            "student_query_length": len(incorrect_sql),
        },
        "steps": [],
    }

    async with engine.begin() as connection:
        print("creating audit tables", flush=True)
        await connection.run_sync(Base.metadata.create_all)
    print("audit tables ready", flush=True)
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        async with session_factory() as session:
            print("creating audit question", flush=True)
            user = User(
                email="new-web-audit@example.com",
                username="new-web-audit",
                password="audit-only-password",
            )
            question = Question(
                title=f"External audit sample {args.sample_id}",
                content=str(record.get("question") or "External SQL audit question"),
                difficulty=5,
                correct_sql=standard_sql,
                sql_dialect="mysql",
                schema_preview=_schema_preview(record),
            )
            session.add_all([user, question])
            await session.flush()
            await QuestionSkillRepository(session).replace_for_question(
                question.id,
                [
                    QuestionSkillSpec(
                        # The external query contains an explicit ``IS NOT
                        # NULL`` predicate; declare the matching atomic skill
                        # as an author-owned assessment mapping.  This is
                        # deliberately a Q-matrix declaration, not a runtime
                        # inference from the reference SQL.
                        skill_id="null.three_valued_logic",
                        taxonomy_version=ATOMIC_SKILL_TAXONOMY_VERSION,
                        role=QuestionSkillRole.PRIMARY,
                        observable_on_correct=True,
                    )
                ],
                provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
            )
            await session.commit()
            print("audit question ready", flush=True)

            async def submit(sql: str, *, label: str, attempt_id: str) -> dict[str, Any]:
                print(f"starting {label}", flush=True)
                try:
                    response = await check_sql(
                        payload=SQLCheckRequest(
                            student_sql=sql,
                            question_id=question.id,
                            attempt_id=attempt_id,
                        ),
                        user_id=user.id,
                        session=session,
                    )
                except Exception as exc:  # noqa: BLE001 - report branch outcome
                    item = {
                        "label": label,
                        "exception_type": type(exc).__name__,
                        "exception": str(exc)[:500],
                    }
                    result["steps"].append(item)
                    return item
                item = {"label": label, **_response_summary(response)}
                print(f"finished {label}: {item.get('judge_status')} / {item.get('phase3_status')}", flush=True)
                result["steps"].append(item)
                return item

            correct = await submit(
                standard_sql,
                label="correct_with_author_declared_qmatrix",
                attempt_id=str(uuid4()),
            )
            wrong = await submit(
                incorrect_sql,
                label="incorrect_external_mutation",
                attempt_id=str(uuid4()),
            )
            replay = await submit(
                incorrect_sql,
                label="same_attempt_replay",
                attempt_id=str(wrong.get("attempt_id") or uuid4()),
            )

            await QuestionSkillRepository(session).replace_for_question(
                question.id,
                [],
                provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
            )
            await session.commit()
            no_map = await submit(
                standard_sql,
                label="correct_without_qmatrix",
                attempt_id=str(uuid4()),
            )
            syntax = await submit(
                "SELECT * FROM (FAC_FLOOR",
                label="syntax_error",
                attempt_id=str(uuid4()),
            )
            safety = await submit(
                "DROP TABLE FAC_FLOOR",
                label="safety_block",
                attempt_id=str(uuid4()),
            )

            result["counts"] = {
                "submissions": await _count(session, Submission),
                "skill_observation_events": await _count(session, SkillObservationEvent),
                "student_skill_states": await _count(session, StudentSkillState),
                "behavior_events": await _count(session, Phase3BehaviorEvent),
                "teaching_audits": await _count(session, SubmissionTeachingAudit),
                "chat_messages": await _count(session, ChatMessage),
            }
            events = list((await session.scalars(select(SkillObservationEvent))).all())
            result["skill_event_sources"] = dict(
                Counter(str(event.source_type) for event in events)
            )
            result["skill_event_results"] = dict(
                Counter(str(event.observation_result) for event in events)
            )
            result["skill_event_details"] = [
                {
                    "source_type": event.source_type,
                    "observation_result": event.observation_result,
                    "taxonomy_version": event.taxonomy_version,
                    "skill_id": event.skill_id,
                    "rule_id": event.rule_id,
                    "candidate_id": event.phase2_candidate_id,
                    "logical_stage": event.logical_stage,
                    "evidence_grade": event.evidence_grade,
                    "source_provenance": event.source_provenance,
                    "assistance_level": event.assistance_level,
                    "answer_revealed": event.answer_revealed,
                }
                for event in events
            ]
            result["checks"] = {
                "correct_with_qmatrix_updated": correct.get("phase3_status") == "UPDATED",
                "incorrect_mutation_judged_wrong": (
                    wrong.get("is_correct") is False
                    and wrong.get("judge_status") == "WRONG"
                ),
                "incorrect_mutation_updated_learning": wrong.get("phase3_status") == "UPDATED",
                "replay_same_submission": (
                    replay.get("submission_id") == wrong.get("submission_id")
                    and replay.get("idempotency_replayed") is True
                ),
                "no_map_skips_bkt": no_map.get("phase3_status") == "SKIP_NO_ASSESSMENT_MAP",
                "syntax_has_no_skill_observation": (
                    syntax.get("phase3_status") == "SKIP_SYNTAX_ERROR"
                ),
                "safety_has_no_learning_summary": (
                    safety.get("is_safety_blocked") is True
                    and safety.get("phase3_status") is None
                ),
                "learner_does_not_receive_internal_diagnostics": all(
                    not item.get("diagnostic_package_exposed", False)
                    and not item.get("raw_observation_exposed", False)
                    and not item.get("error_attributions_exposed", False)
                    for item in result["steps"]
                    if "exception_type" not in item
                ),
                "atomic_negative_uses_frozen_taxonomy": all(
                    event.taxonomy_version == ATOMIC_SKILL_TAXONOMY_VERSION
                    for event in events
                    if event.observation_result == "INCORRECT"
                ),
            }
            result["overall_pass"] = all(result["checks"].values())
    finally:
        await engine.dispose()
        ai_router.settings.PARSEVAL_EXECUTION_BACKEND = previous_backend
    return result


async def _run_with_selector_heartbeat(args: argparse.Namespace) -> dict[str, Any]:
    """Keep aiosqlite callbacks observable on the WSL selector loop."""

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(0.01)

    task = asyncio.create_task(heartbeat())
    try:
        return await _run(args)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def main() -> None:
    args = _args()
    report = asyncio.run(_run_with_selector_heartbeat(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not report.get("overall_pass"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
