"""Bounded end-to-end audit for newly collected SQL teaching corpora.

The harness deliberately keeps two views of every attempt separate:

* internal contains finite Phase 0--3 evidence and Phase 4/6 audit rows;
* learner contains exactly what the real /ai/check-sql route returns.

Only public enriched JSONL records are consumed. Phase 1 uses the SQLite
compatibility backend in this environment; the report labels that boundary.
Every row count, candidate list and feedback text is bounded before writing.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import suppress
from dataclasses import asdict, is_dataclass
from enum import Enum
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# The application settings module intentionally requires deployment secrets.
# This bounded audit uses an in-memory business store and never sends mail;
# provide only process-local test defaults when the caller has not supplied a
# deployment environment.
os.environ.setdefault("DB_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("MAIL_USERNAME", "audit@example.invalid")
os.environ.setdefault("MAIL_PASSWORD", "audit-only")
os.environ.setdefault("MAIL_FROM", "audit@example.invalid")
os.environ.setdefault("SECRET_KEY", "real-teaching-audit-secret")
os.environ.setdefault("JWT_SECRET_KEY", "real-teaching-audit-jwt-secret")


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "sql-edu-backend"
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.ast_schema import SQLStructureIR  # noqa: E402
from core.error_attribution import evidence_weights_from_observation  # noqa: E402
from core.error_diagnosis import diagnose_record, render_diagnostic_feedback  # noqa: E402
from core.phase3_runtime import prepare_phase3_attempt  # noqa: E402
from core.phase3_skill_catalog import (  # noqa: E402
    ATOMIC_SKILL_TAXONOMY_VERSION,
    rule_skill_catalog_metadata,
)
from core.sql_dialect_resolver import resolve_sql_dialect_or_raise  # noqa: E402
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


DEFAULT_CORPORA = (
    PROJECT_ROOT / "data_construct_test/outputs/web_sql_xd_deng_20260827.jsonl",
    PROJECT_ROOT / "data_construct_test/outputs/web_sql_pgexercises_20260827.jsonl",
)

# Stable public IDs from the two newly collected corpora. The q-matrix values
# are author declarations used to exercise the positive-observation gate; they
# are not inferred from SQL text.
DEFAULT_CASES: tuple[dict[str, Any], ...] = (
    {
        "id": "websql_42680966fbb350c04ff7",
        "name": "基础过滤与比较边界",
        "qmatrix_skill": "filter.boundary",
        "preferred_mutations": ("lte_to_lt",),
        "manual_mutations": (),
    },
    {
        "id": "websql_d903838ccdde05e393d8",
        "name": "NULL 三值逻辑与 NOT IN",
        "qmatrix_skill": "null.three_valued_logic",
        "preferred_mutations": ("is_not_null_to_null", "not_in_to_in"),
        "manual_mutations": (),
    },
    {
        "id": "websql_b7e5151a0079c90c67aa",
        "name": "连接、聚合与 HAVING",
        "qmatrix_skill": "having.aggregate_boundary",
        "preferred_mutations": ("gte_to_gt",),
        "manual_mutations": (),
    },
    {
        "id": "websql_7f9e9b4c5549056f5847",
        "name": "LEFT JOIN 外连接保留",
        "qmatrix_skill": "join.outer_preservation",
        # A bounded world may contain no unmatched right row. Retain this
        # mutation to expose the honest witness/evidence boundary.
        "preferred_mutations": (),
        "manual_mutations": (
            (
                "left_join_to_inner",
                "SELECT Boxes.Code FROM Warehouses INNER JOIN Boxes ON Warehouses.Code = Boxes.Warehouse WHERE Location = 'Chicago'",
                ["join-left", "join-inner", "join-on"],
            ),
        ),
    },
    {
        "id": "websql_76fbe3b3fe2075799d28",
        "name": "EXISTS 相关子查询",
        "qmatrix_skill": "subquery.cardinality",
        "preferred_mutations": (),
        "manual_mutations": (
            (
                "exists_to_not_exists",
                "SELECT Name FROM Pieces WHERE NOT EXISTS ( SELECT * FROM Provides WHERE Provider = 'HAL' AND Piece = Pieces.Code )",
                ["subquery-exists", "where"],
            ),
        ),
    },
    {
        "id": "websql_d7ee65105635b0394491",
        "name": "DISTINCT、ORDER BY 与 LIMIT",
        "qmatrix_skill": "projection.dedup",
        "preferred_mutations": ("distinct_removed", "limit_plus_one"),
        "manual_mutations": (),
    },
    {
        "id": "websql_2714350591a6e7429930",
        "name": "PostgreSQL 聚合 HAVING 边界",
        "qmatrix_skill": "having.aggregate_boundary",
        "preferred_mutations": ("gt_to_gte",),
        "manual_mutations": (),
    },
    {
        "id": "websql_38a052dde8902aef5f14",
        "name": "CASE、连接与收入聚合",
        "qmatrix_skill": "projection.case_coverage",
        "preferred_mutations": (),
        "manual_mutations": (
            (
                "case_else_to_guest_cost",
                "select facs.name, sum(slots * case when memid = 0 then facs.guestcost else facs.guestcost end) as revenue from cd.bookings bks inner join cd.facilities facs on bks.facid = facs.facid group by facs.name order by revenue",
                ["case", "agg-count", "join-inner", "group-by"],
            ),
        ),
    },
    {
        "id": "websql_3a7ead7b6334fec57b90",
        "name": "窗口函数 ROW_NUMBER",
        "qmatrix_skill": "result.order_offset",
        "preferred_mutations": ("projection_to_star",),
        "manual_mutations": (),
    },
    {
        "id": "websql_97229672724f7316773e",
        "name": "UNION 去重语义",
        "qmatrix_skill": "projection.dedup",
        "preferred_mutations": ("union_to_union_all",),
        "manual_mutations": (),
    },
    {
        "id": "websql_941d35d9e054bf98154e",
        "name": "递归 CTE 与排序",
        "qmatrix_skill": "join.bridge_path",
        "preferred_mutations": ("order_desc_to_asc",),
        "manual_mutations": (),
    },
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=Path,
        action="append",
        help="enriched public JSONL; may be repeated (defaults to both new corpora)",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        help="stable record id; may be repeated (defaults to the 11 typical cases)",
    )
    parser.add_argument("--select-source", help="select records with this source_id")
    parser.add_argument("--select-label", help="select records containing this cfg label")
    parser.add_argument("--select-count", type=int, default=0)
    parser.add_argument("--max-rows", type=int, default=8)
    parser.add_argument(
        "--execution-backend",
        choices=("sqlite", "native", "mysql", "postgres"),
        default="sqlite",
        help=(
            "Phase 1 execution backend; sqlite is the compatibility rehearsal, "
            "native/explicit vendor values require a reachable Docker runner"
        ),
    )
    parser.add_argument(
        "--native-executor-url",
        default="",
        help="native runner URL for the selected vendor backend",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=PROJECT_ROOT
        / "data_construct_test/outputs/real_teaching_scenario_audit_20260827.json",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=PROJECT_ROOT
        / "data_construct_test/outputs/real_teaching_scenario_audit_20260827.md",
    )
    return parser.parse_args()


def _clip(value: Any, limit: int = 800) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    return value


def _safe(value: Any, *, depth: int = 0, max_depth: int = 7) -> Any:
    """Convert bounded dataclass/enums/containers to JSON-safe values."""

    if depth > max_depth:
        return "<depth-limited>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return _clip(value, 1600)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _safe(asdict(value), depth=depth + 1, max_depth=max_depth)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            return _safe(value.to_dict(), depth=depth + 1, max_depth=max_depth)
        except Exception:
            return f"<{type(value).__name__}>"
    if isinstance(value, Mapping):
        return {
            str(key): _safe(item, depth=depth + 1, max_depth=max_depth)
            for key, item in list(value.items())[:96]
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _safe(item, depth=depth + 1, max_depth=max_depth)
            for item in list(value)[:96]
        ]
    return _clip(str(value), 1600)


def _dialect(record: Mapping[str, Any]) -> str:
    raw = str(record.get("dialect") or "mysql").lower().strip()
    return {
        "postgresql": "postgres",
        "sqlserver": "tsql",
        "mssql": "tsql",
    }.get(raw, raw)


def _schema_preview(record: Mapping[str, Any]) -> str:
    catalog = record.get("schema_catalog")
    if not isinstance(catalog, Mapping) or not isinstance(catalog.get("tables"), list):
        raise ValueError("record has no authoritative schema_catalog.tables")
    return json.dumps(
        {"tables": catalog["tables"]}, ensure_ascii=False, separators=(",", ":")
    )


def _phase0(record: Mapping[str, Any]) -> dict[str, Any]:
    catalog = record.get("schema_catalog") or {}
    tables = catalog.get("tables") if isinstance(catalog, Mapping) else []
    table_facts = []
    for table in tables if isinstance(tables, list) else []:
        if not isinstance(table, Mapping):
            continue
        columns = table.get("columns") or []
        table_facts.append(
            {
                "name": table.get("name"),
                "column_count": len(columns) if isinstance(columns, list) else 0,
                "columns": [
                    {
                        "name": col.get("name"),
                        "data_type": col.get("data_type"),
                        "nullable": col.get("nullable"),
                        "primary_key": col.get("is_primary_key"),
                    }
                    for col in columns
                    if isinstance(col, Mapping)
                ],
                "primary_key": list(table.get("primary_key") or []),
                "foreign_key_count": len(table.get("foreign_keys") or []),
            }
        )
    schema_text = str(record.get("schema") or "")
    return {
        "record_id": record.get("id"),
        "source_id": record.get("source_id"),
        "source_name": record.get("source_name"),
        "source_kind": record.get("source_kind"),
        "source_url": record.get("source_url"),
        "member": record.get("member"),
        "extraction_method": record.get("extraction_method"),
        "provenance_hash": record.get("provenance_hash"),
        "schema_trust": record.get("schema_trust"),
        "replay_eligible": record.get("replay_eligible"),
        "dialect": _dialect(record),
        "schema_text_bytes": len(schema_text.encode("utf-8")),
        "schema_catalog_source": catalog.get("source") if isinstance(catalog, Mapping) else None,
        "schema_database_id": catalog.get("db_id") if isinstance(catalog, Mapping) else None,
        "tables": table_facts,
        "cfg_labels": sorted(str(item) for item in (record.get("cfg_labels") or [])),
    }


def _phase1_short(run: Any) -> dict[str, Any]:
    evidence = run.data_evidence or {}
    return {
        "executed": bool(run.executed),
        "status": run.status,
        "equivalence_conclusion": run.equivalence_conclusion,
        "judge_status": run.judge_status,
        "is_equivalent": run.is_equivalent,
        "error": _clip(run.error),
        "error_code": run.error_code,
        "boundary_evidence": _safe(run.boundary_evidence),
        "execution_backend": evidence.get("execution_backend"),
        "sql_dialect": evidence.get("sql_dialect"),
        "dialect_resolution": _safe(evidence.get("dialect_resolution")),
        "standard_row_count": len(run.standard_rows),
        "student_row_count": len(run.student_rows),
        "standard_columns": list(run.standard_columns)[:32],
        "student_columns": list(run.student_columns)[:32],
        "student_exec_ok": evidence.get("student_exec_ok"),
        "student_exec_error": _clip(evidence.get("student_exec_error")),
        "selected_witness_world_id": evidence.get("selected_witness_world_id"),
        "any_world_distinguished": evidence.get("any_world_distinguished"),
        "any_obligation_distinguished": evidence.get("any_obligation_distinguished"),
    }


def _rows(rows: Iterable[Any], limit: int = 12) -> list[Any]:
    result = []
    for row in list(rows)[:limit]:
        if isinstance(row, (tuple, list)):
            result.append([_safe(item, max_depth=3) for item in row[:24]])
        else:
            result.append(_safe(row, max_depth=3))
    return result


def _phase1_detail(run: Any) -> dict[str, Any]:
    evidence = run.data_evidence or {}
    diffs = []
    for item in run.ast_diffs[:32]:
        raw = item.to_dict() if hasattr(item, "to_dict") else dict(item)
        diffs.append(_safe(raw))
    suite = evidence.get("witness_suite") or {}
    worlds = suite.get("worlds") if isinstance(suite, Mapping) else []
    world_summary = []
    for world in worlds if isinstance(worlds, list) else []:
        if not isinstance(world, Mapping):
            continue
        execution = world.get("execution") or {}
        attempts = execution.get("attempts") if isinstance(execution, Mapping) else []
        world_summary.append(
            {
                "world_id": world.get("world_id"),
                "obligation_ids": list(world.get("obligation_ids") or [])[:32],
                "diff_ids": list(world.get("diff_ids") or [])[:32],
                "constraint_count": len(world.get("constraints") or []),
                "minimum_rows": _safe(world.get("minimum_rows")),
                "diagnostics": [
                    _clip(item, 240) for item in (world.get("diagnostics") or [])[:12]
                ],
                "attempts": [
                    {
                        "attempt": item.get("attempt"),
                        "pair_distinguished": item.get("pair_distinguished"),
                        "obligation_distinguished": item.get("obligation_distinguished"),
                        "causal_attribution_verified": item.get("causal_attribution_verified"),
                    }
                    for item in attempts[:12]
                    if isinstance(item, Mapping)
                ],
            }
        )
    obligations = []
    for item in (evidence.get("obligation_effectiveness") or [])[:48]:
        if not isinstance(item, Mapping):
            continue
        obligations.append(
            {
                "obligation_id": item.get("obligation_id"),
                "diff_id": item.get("diff_id"),
                "probe": item.get("probe"),
                "activated": item.get("activated"),
                "constraints_satisfied": item.get("constraints_satisfied"),
                "pair_distinguished": item.get("pair_distinguished"),
                "distinguished": item.get("distinguished"),
                "causal_attribution_verified": item.get("causal_attribution_verified"),
                "world_id": item.get("world_id"),
                "attempt_count": item.get("attempt_count"),
                "success_predicate": item.get("success_predicate"),
                "mutation_validation": _safe(item.get("mutation_validation")),
            }
        )
    mutation = run.mutation_evidence or {}
    mutation_tests = []
    for item in (mutation.get("tests") or [])[:32]:
        if not isinstance(item, Mapping):
            continue
        mutation_tests.append(
            {
                "clause": item.get("clause"),
                "knowledge_point_id": item.get("knowledge_point_id"),
                "action": item.get("action"),
                "mutation_scope": list(item.get("mutation_scope") or []),
                "query_scope": item.get("query_scope"),
                "replacement_exec_ok": item.get("replacement_exec_ok"),
                "replacement_equivalent": item.get("replacement_equivalent"),
                "fixed_by_replacement": item.get("fixed_by_replacement"),
                "removal_exec_ok": item.get("removal_exec_ok"),
                "removed_student_clause_equivalent": item.get("removed_student_clause_equivalent"),
                "diff_ids": list(item.get("diff_ids") or []),
                "obligation_ids": list(item.get("obligation_ids") or []),
                "binding_quality": item.get("binding_quality"),
            }
        )
    tables = []
    for name, values in (run.test_database or {}).items():
        tables.append(
            {
                "name": name,
                "row_count": len(values),
                "sample_rows": _safe(values[:4]),
            }
        )
    return {
        "summary": _phase1_short(run),
        "structure": {
            "ast_diff_count": len(run.ast_diffs),
            "ast_diff_types": dict(Counter(str(item.diff_type) for item in run.ast_diffs)),
            "ast_diffs": diffs,
            "scope_metadata": _safe(evidence.get("scope_metadata")),
        },
        "witness": {
            "world_count": suite.get("world_count") if isinstance(suite, Mapping) else None,
            "obligation_count": suite.get("obligation_count") if isinstance(suite, Mapping) else None,
            "uncovered_obligations": (
                list(suite.get("uncovered_obligations") or [])
                if isinstance(suite, Mapping)
                else []
            ),
            "planner_diagnostics": (
                list(suite.get("planner_diagnostics") or [])[:16]
                if isinstance(suite, Mapping)
                else []
            ),
            "worlds": world_summary,
            "obligation_effectiveness": obligations,
        },
        "data_generation": {
            "generation_tactics": _safe(evidence.get("generation_tactics")),
            "tables": tables,
            "standard_rows": _rows(run.standard_rows),
            "student_rows": _rows(run.student_rows),
            "only_in_standard_sample": _safe(evidence.get("only_in_standard_sample")),
            "only_in_student_sample": _safe(evidence.get("only_in_student_sample")),
        },
        "mutation": {
            "enabled": bool(mutation.get("enabled")),
            "summary": _safe(mutation.get("summary")),
            "diff_id_linked": mutation.get("diff_id_linked"),
            "tests": mutation_tests,
            "reason": _clip(mutation.get("reason")),
        },
    }


def _structure_ir(standard_sql: str, student_sql: str, dialect: str) -> dict[str, Any]:
    try:
        resolution = resolve_sql_dialect_or_raise(
            declared_dialect=dialect,
            standard_sql=standard_sql,
            student_sql=student_sql,
            default_dialect="mysql",
        )
        asts = resolution.asts
        return {
            "status": "READY",
            "parse_dialect": resolution.parse_dialect,
            "standard": SQLStructureIR.from_ast(asts[0]).to_dict() if len(asts) >= 1 else None,
            "student": SQLStructureIR.from_ast(asts[1]).to_dict() if len(asts) >= 2 else None,
        }
    except Exception as exc:
        return {"status": "UNAVAILABLE", "error_type": type(exc).__name__}


def _judge_detail(run: Any) -> dict[str, Any]:
    return {
        "is_correct": run.is_equivalent,
        "judge_status": run.judge_status,
        "phase1_status": run.status,
        "equivalence_conclusion": run.equivalence_conclusion,
        "comparison": {
            "is_equivalent_on_generated_data": run.is_equivalent,
            "standard_row_count": len(run.standard_rows),
            "student_row_count": len(run.student_rows),
            "standard_columns": list(run.standard_columns),
            "student_columns": list(run.student_columns),
        },
    }


def _phase2_detail(
    *,
    run: Any,
    record: Mapping[str, Any],
    student_sql: str,
    is_correct: bool,
) -> dict[str, Any]:
    schema = {"tables": (record.get("schema_catalog") or {}).get("tables", [])}
    attr: Any = None
    if not is_correct and run.status in {"SUPPORTED", "SUPPORTED_WITH_LIMITS"}:
        try:
            attr = evidence_weights_from_observation(
                student_sql=student_sql,
                answer_sql=str(record["sql"]),
                is_correct=False,
                error_message=(
                    f"result rows differ: standard={len(run.standard_rows)}, "
                    f"student={len(run.student_rows)}"
                ),
                judge_detail=_judge_detail(run),
                question_context={
                    "q": str(record.get("question") or record.get("member") or "")
                },
                mutation_detail=run.mutation_evidence,
                ast_diffs=[item.to_dict() for item in run.ast_diffs],
                sql_dialect=_dialect(record),
                dialect_resolution=(run.data_evidence or {}).get("dialect_resolution"),
            )
        except Exception as exc:
            attr = {"error_type": type(exc).__name__}
    try:
        package = diagnose_record(
            sandbox_run=run,
            attribution_result=attr,
            question=str(record.get("question") or record.get("member") or ""),
            schema=schema,
            student_sql=student_sql,
            language="zh-CN",
        )
        public = package.to_dict()
        internal = package.to_internal_dict()
        feedback = render_diagnostic_feedback(package, language="zh-CN")
    except Exception as exc:
        return {
            "status": "ERROR",
            "error_type": type(exc).__name__,
            "error": _clip(str(exc)),
            "attribution": None,
        }

    attr_payload = None
    if hasattr(attr, "to_dict"):
        raw = attr.to_dict()
        observation = raw.get("observation") or {}
        ast_observation = observation.get("E_AST") or {}
        attr_payload = {
            "observation": {
                "E_AST": {
                    "student_parse_ok": ast_observation.get("student_parse_ok"),
                    "standard_parse_ok": ast_observation.get("standard_parse_ok"),
                    "normalized_ast_equal": ast_observation.get("normalized_ast_equal"),
                    "ast_diff_count": len(ast_observation.get("ast_diffs") or []),
                },
                "E_data": _safe(observation.get("E_data")),
                "E_MUT": _safe(observation.get("E_MUT")),
            },
            "attributions": _safe(raw.get("attributions")),
        }
    elif isinstance(attr, Mapping):
        attr_payload = {"error_type": attr.get("error_type")}

    primary = public.get("primary") or {}
    return {
        "status": "READY",
        "public_package": _safe(public),
        "internal_package": _safe(internal),
        "learner_safe_diagnostic_feedback": _clip(feedback, 3000),
        "primary": {
            "candidate_id": primary.get("candidate_id"),
            "rule_id": primary.get("rule_id"),
            "stage": primary.get("stage"),
            "logical_stage": primary.get("logical_stage"),
            "evidence_grade": primary.get("evidence_grade"),
            "evidence_refs": _safe(primary.get("evidence_refs")),
        },
        "attribution": attr_payload,
    }


def _mutation_candidates(
    record: Mapping[str, Any], spec: Mapping[str, Any]
) -> list[tuple[str, str, list[str]]]:
    generated = _web_mutations(
        str(record["sql"]),
        str(record.get("schema") or ""),
        _dialect(record),
        record.get("schema_catalog"),
    )
    preferred = {
        name: index for index, name in enumerate(spec.get("preferred_mutations") or ())
    }
    generated = sorted(
        generated, key=lambda item: (preferred.get(item[0], 1000), item[0])
    )
    manual = [tuple(item) for item in (spec.get("manual_mutations") or ())]
    seen: set[str] = set()
    result = []
    for item in [*generated, *manual]:
        if item[0] in seen or item[1] == str(record["sql"]):
            continue
        seen.add(item[0])
        result.append((str(item[0]), str(item[1]), list(item[2])))
    return result


def _candidate_summary(name: str, sql: str, run: Any) -> dict[str, Any]:
    return {
        "name": name,
        "student_sql": sql,
        "phase1": _phase1_short(run),
        "ast_diff_types": [str(item.diff_type) for item in run.ast_diffs],
    }


def _plan_snapshot(plan: Any) -> dict[str, Any]:
    if plan is None:
        return {"status": "NONE"}
    return {
        "status": plan.status,
        "expected_is_correct": plan.expected_is_correct,
        "admission": _safe(plan.admission.to_dict()),
        "persistence_input_count": len(plan.persistence_inputs),
        "schedule": _safe(plan.schedule.to_dict()),
        "support": _safe(plan.support.to_dict()) if plan.support is not None else None,
        "selected_target": (
            _safe(plan.selected_target.to_dict())
            if plan.selected_target is not None
            else None
        ),
        "selected_priority": (
            _safe(plan.selected.to_dict()) if plan.selected is not None else None
        ),
    }


async def _count(session: AsyncSession, model: Any) -> int:
    value = await session.scalar(select(func.count()).select_from(model))
    return int(value or 0)


async def _server_audit(
    session: AsyncSession,
    *,
    user_id: int,
    question_id: int,
    submission_id: int | None,
) -> dict[str, Any]:
    audit = None
    if submission_id is not None:
        audit = await session.scalar(
            select(SubmissionTeachingAudit).where(
                SubmissionTeachingAudit.submission_id == submission_id
            )
        )
    events = list(
        (
            await session.scalars(
                select(SkillObservationEvent)
                .where(
                    SkillObservationEvent.user_id == user_id,
                    SkillObservationEvent.question_id == question_id,
                )
                .order_by(SkillObservationEvent.id)
            )
        ).all()
    )
    behavior = list(
        (
            await session.scalars(
                select(Phase3BehaviorEvent)
                .where(
                    Phase3BehaviorEvent.user_id == user_id,
                    Phase3BehaviorEvent.question_id == question_id,
                )
                .order_by(Phase3BehaviorEvent.id)
            )
        ).all()
    )
    states = list(
        (
            await session.scalars(
                select(StudentSkillState).where(StudentSkillState.user_id == user_id)
            )
        ).all()
    )
    messages = list(
        (
            await session.scalars(
                select(ChatMessage)
                .where(
                    ChatMessage.user_id == user_id,
                    ChatMessage.question_id == question_id,
                )
                .order_by(ChatMessage.id)
            )
        ).all()
    )
    audit_payload = None
    if audit is not None:
        audit_payload = {
            "audit_schema_version": audit.audit_schema_version,
            "support_need": audit.support_need,
            "recommended_support_level": audit.recommended_support_level,
            "delivered_support_level": audit.delivered_support_level,
            "support_recommendation_applied": audit.support_recommendation_applied,
            "recommendation_status": audit.recommendation_status,
            "support_policy_version": audit.support_policy_version,
            "action_policy_version": audit.action_policy_version,
            "feedback_policy_version": audit.feedback_policy_version,
            "generation_source": audit.generation_source,
            "feedback_status": audit.feedback_status,
            "degradation_code": audit.degradation_code,
            "answer_revealed": audit.answer_revealed,
            "target_candidate_id": audit.target_candidate_id,
            "target_rule_id": audit.target_rule_id,
            "target_observation_id": audit.target_observation_id,
            "target_skill_id": audit.target_skill_id,
            "target_logical_stage": audit.target_logical_stage,
            "target_evidence_grade": audit.target_evidence_grade,
            "feedback_sha256": audit.feedback_sha256,
            "action_snapshot": _safe(audit.action_snapshot),
        }
    return {
        "teaching_audit": audit_payload,
        "skill_observation_events": [
            {
                "id": event.id,
                "submission_id": event.submission_id,
                "taxonomy_version": event.taxonomy_version,
                "skill_id": event.skill_id,
                "observation_result": event.observation_result,
                "source_type": event.source_type,
                "source_version": event.source_version,
                "evidence_grade": event.evidence_grade,
                "phase2_candidate_id": event.phase2_candidate_id,
                "rule_id": event.rule_id,
                "logical_stage": event.logical_stage,
                "assistance_level": event.assistance_level,
                "prior_mastery": event.prior_mastery,
                "posterior_mastery": event.posterior_mastery,
                "next_prior": event.next_prior,
                "state_version": event.state_version,
            }
            for event in events
        ],
        "behavior_events": [
            {
                "id": event.id,
                "submission_id": event.submission_id,
                "event_kind": event.event_kind,
            }
            for event in behavior
        ],
        "student_skill_states": [
            {
                "taxonomy_version": state.taxonomy_version,
                "skill_id": state.skill_id,
                "posterior_mastery": state.posterior_mastery,
                "next_prior": state.next_prior,
                "observation_count": state.observation_count,
                "state_version": state.state_version,
                "bkt_parameter_version": state.bkt_parameter_version,
            }
            for state in states
        ],
        "chat_messages": [
            {"role": message.role, "content": _clip(message.content, 1200)}
            for message in messages[-8:]
        ],
    }


def _learner_response_summary(response: Any) -> dict[str, Any]:
    phase3 = response.phase3_learning or {}
    support = response.teaching_support or {}
    hint = response.hint or {}
    return {
        "submission_id": response.submission_id,
        "attempt_id": str(response.attempt_id),
        "idempotency_replayed": bool(response.idempotency_replayed),
        "is_correct": response.is_correct,
        "judge_status": response.judge_status,
        "is_safety_blocked": response.is_safety_blocked,
        "error_message": _clip(response.error_message, 800),
        "phase3_learning": _safe(phase3),
        "teaching_support": _safe(support),
        "hint_text": _clip(hint.get("overall_comment"), 3000),
        "learner_internal_fields_absent": (
            response.observation is None
            and not response.error_attributions
            and response.diagnostic_package is None
        ),
    }


async def _submit_route(
    session: AsyncSession,
    *,
    user: User,
    question: Question,
    sql: str,
    label: str,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    attempt_id = attempt_id or str(uuid4())
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
    except HTTPException as exc:
        await session.rollback()
        # SQLAlchemy expires ORM instances on rollback even when
        # ``expire_on_commit=False``. Refresh the two stable identities before
        # the next branch/case so reading ``.id`` never performs implicit IO
        # outside the async greenlet.
        await session.refresh(user)
        await session.refresh(question)
        return {
            "label": label,
            "attempt_id": attempt_id,
            "http_status": exc.status_code,
            "error": _safe(exc.detail),
            "learner_internal_fields_absent": True,
        }
    except Exception as exc:
        await session.rollback()
        await session.refresh(user)
        await session.refresh(question)
        return {
            "label": label,
            "attempt_id": attempt_id,
            "exception_type": type(exc).__name__,
            "exception": _clip(str(exc)),
            "learner_internal_fields_absent": False,
        }
    item = {"label": label, **_learner_response_summary(response)}
    item["server_audit"] = await _server_audit(
        session,
        user_id=user.id,
        question_id=question.id,
        submission_id=response.submission_id,
    )
    return item


def _branch_status(item: Mapping[str, Any]) -> str:
    if item.get("http_status"):
        detail = item.get("error")
        if isinstance(detail, Mapping):
            return str(detail.get("code") or detail.get("judge_status") or "HTTP_ERROR")
        return f"HTTP_{item['http_status']}"
    if item.get("exception_type"):
        return str(item["exception_type"])
    return str(item.get("judge_status") or "UNKNOWN")


async def _run_case(
    *,
    session: AsyncSession,
    user: User,
    record: Mapping[str, Any],
    spec: Mapping[str, Any],
    case_index: int,
    max_rows: int,
    execution_backend: str = "sqlite",
    native_executor_url: str | None = None,
) -> dict[str, Any]:
    standard_sql = str(record["sql"]).strip().rstrip(";")
    dialect = _dialect(record)
    schema_text = str(record.get("schema") or "")
    catalog = record.get("schema_catalog") or {}
    question = Question(
        title=f"真实语料审计 · {spec.get('name') or record.get('id')}",
        content=(
            f"公开教程 {record.get('member') or ''} 的可重放练习。"
            "请写出与题目要求等价的查询。"
        ),
        difficulty=5,
        correct_sql=standard_sql,
        sql_dialect=dialect,
        schema_preview=_schema_preview(record),
        required_output_columns=None,
    )
    session.add(question)
    await session.flush()
    qmatrix_skill = str(spec.get("qmatrix_skill") or "filter.boundary")
    qmatrix_spec = [
        QuestionSkillSpec(
            skill_id=qmatrix_skill,
            taxonomy_version=ATOMIC_SKILL_TAXONOMY_VERSION,
            role=QuestionSkillRole.PRIMARY,
            observable_on_correct=True,
        )
    ]
    await QuestionSkillRepository(session).replace_for_question(
        question.id,
        qmatrix_spec,
        provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
    )
    await session.commit()

    def direct(student_sql: str) -> Any:
        from core.parseval_data_generator import generate_and_compare

        return generate_and_compare(
            schema_text,
            standard_sql,
            student_sql,
            max_rows_per_table=max_rows,
            sql_dialect=dialect,
            default_sql_dialect="mysql",
            execution_backend=execution_backend,
            native_executor_url=native_executor_url,
            schema_catalog=catalog,
        )

    identity_run = direct(standard_sql)
    candidates = _mutation_candidates(record, spec)
    candidate_reports = []
    selected_name = None
    selected_sql = None
    selected_run = None
    for name, sql, _labels in candidates:
        try:
            run = direct(sql)
        except Exception as exc:
            candidate_reports.append(
                {
                    "name": name,
                    "student_sql": sql,
                    "exception_type": type(exc).__name__,
                    "exception": _clip(str(exc)),
                }
            )
            continue
        candidate_reports.append(_candidate_summary(name, sql, run))
        if selected_run is None:
            selected_name, selected_sql, selected_run = name, sql, run
        if (
            run.status in {"SUPPORTED", "SUPPORTED_WITH_LIMITS"}
            and run.equivalence_conclusion == "NOT_EQUIVALENT"
        ):
            selected_name, selected_sql, selected_run = name, sql, run
            break
    if selected_run is None:
        selected_name, selected_sql, selected_run = "no_mutation", standard_sql, identity_run

    identity_phase2 = _phase2_detail(
        run=identity_run,
        record=record,
        student_sql=standard_sql,
        is_correct=True,
    )
    wrong_phase2 = _phase2_detail(
        run=selected_run,
        record=record,
        student_sql=selected_sql,
        is_correct=(selected_run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"),
    )

    identity_plan: Any = None
    wrong_plan: Any = None
    try:
        identity_pkg = identity_phase2.get("public_package")
        if isinstance(identity_pkg, Mapping):
            identity_plan = await prepare_phase3_attempt(
                session,
                user_id=user.id,
                question_id=question.id,
                expected_is_correct=True,
                diagnostic_package=identity_pkg,
                answer_revealed=False,
            )
    except Exception as exc:
        identity_plan = {"error_type": type(exc).__name__, "error": _clip(str(exc))}
    try:
        wrong_pkg = wrong_phase2.get("public_package")
        if isinstance(wrong_pkg, Mapping):
            wrong_plan = await prepare_phase3_attempt(
                session,
                user_id=user.id,
                question_id=question.id,
                expected_is_correct=False,
                diagnostic_package=wrong_pkg,
                answer_revealed=False,
            )
    except Exception as exc:
        wrong_plan = {"error_type": type(exc).__name__, "error": _clip(str(exc))}

    branches: list[dict[str, Any]] = []
    full_matrix = case_index == 0
    correct = await _submit_route(
        session,
        user=user,
        question=question,
        sql=standard_sql,
        label="correct_with_author_declared_qmatrix",
    )
    branches.append(correct)
    wrong = await _submit_route(
        session,
        user=user,
        question=question,
        sql=selected_sql,
        label="incorrect_selected_mutation",
    )
    branches.append(wrong)
    if full_matrix and "submission_id" in wrong:
        replay = await _submit_route(
            session,
            user=user,
            question=question,
            sql=selected_sql,
            label="same_attempt_replay",
            attempt_id=str(wrong["attempt_id"]),
        )
        branches.append(replay)

    if full_matrix:
        await QuestionSkillRepository(session).replace_for_question(
            question.id,
            [],
            provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
        )
        await session.commit()
        branches.append(
            await _submit_route(
                session,
                user=user,
                question=question,
                sql=standard_sql,
                label="correct_without_qmatrix",
            )
        )
        branches.append(
            await _submit_route(
                session,
                user=user,
                question=question,
                sql="SELECT * FROM (FAC_FLOOR",
                label="syntax_error",
            )
        )
        branches.append(
            await _submit_route(
                session,
                user=user,
                question=question,
                sql="DROP TABLE Products",
                label="safety_block",
            )
        )

    identity_summary = _phase1_detail(identity_run)
    wrong_summary = _phase1_detail(selected_run)
    case_result = {
        "case_index": case_index,
        "case_name": spec.get("name"),
        "source": _phase0(record),
        "sql": {"standard": standard_sql, "selected_student": selected_sql},
        "selection": {
            "mutation_name": selected_name,
            "candidate_count": len(candidate_reports),
            "candidates": candidate_reports,
        },
        "phase1": {
            "identity": identity_summary,
            "selected_student": wrong_summary,
            "structure_ir": {
                "identity": _structure_ir(standard_sql, standard_sql, dialect),
                "selected_student": _structure_ir(standard_sql, selected_sql, dialect),
            },
        },
        "phase2": {"identity": identity_phase2, "selected_student": wrong_phase2},
        "phase3": {
            "qmatrix": {
                "provenance": "AUTHOR_DECLARED",
                "taxonomy_version": ATOMIC_SKILL_TAXONOMY_VERSION,
                "skill_id": qmatrix_skill,
                "role": "PRIMARY",
                "observable_on_correct": True,
            },
            "rule_skill_catalog": rule_skill_catalog_metadata(),
            "identity_plan": (
                _plan_snapshot(identity_plan)
                if not isinstance(identity_plan, Mapping)
                else identity_plan
            ),
            "selected_student_plan": (
                _plan_snapshot(wrong_plan)
                if not isinstance(wrong_plan, Mapping)
                else wrong_plan
            ),
        },
        "phase4_5_6": {
            "branches": branches,
            "branch_statuses": {
                item.get("label"): _branch_status(item) for item in branches
            },
        },
        "boundaries": [],
    }
    for run in (identity_run, selected_run):
        if run.status not in {"SUPPORTED", "SUPPORTED_WITH_LIMITS"}:
            case_result["boundaries"].append(
                {
                    "kind": "phase1_verdict_boundary",
                    "status": run.status,
                    "equivalence_conclusion": run.equivalence_conclusion,
                    "reason": run.boundary_evidence or run.error,
                }
            )
        if (run.data_evidence or {}).get("execution_backend") == "sqlite":
            case_result["boundaries"].append(
                {
                    "kind": "backend_boundary",
                    "message": (
                        "SQLite compatibility rehearsal; not native "
                        "MySQL/PostgreSQL semantic proof."
                    ),
                }
            )
    if (
        selected_run.standard_rows == selected_run.student_rows
        and selected_run.equivalence_conclusion == "UNDECIDED"
    ):
        case_result["boundaries"].append(
            {
                "kind": "witness_evidence_gap",
                "message": (
                    "Generated bounded world did not distinguish the selected "
                    "real-query mutation."
                ),
            }
        )
    return case_result


def _select_records(
    records: Mapping[str, Mapping[str, Any]], args: argparse.Namespace
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    if args.case_id:
        selected = []
        for identifier in args.case_id:
            if identifier not in records:
                raise ValueError(f"case id not found: {identifier}")
            spec = next(
                (item for item in DEFAULT_CASES if item["id"] == identifier),
                {
                    "id": identifier,
                    "name": identifier,
                    "qmatrix_skill": "filter.boundary",
                    "preferred_mutations": (),
                    "manual_mutations": (),
                },
            )
            selected.append((dict(records[identifier]), dict(spec)))
        return selected
    if args.select_source or args.select_label:
        rows = []
        for identifier in sorted(records):
            row = records[identifier]
            if args.select_source and str(row.get("source_id")) != args.select_source:
                continue
            labels = {str(item) for item in (row.get("cfg_labels") or [])}
            if args.select_label and args.select_label not in labels:
                continue
            rows.append(
                (
                    dict(row),
                    {
                        "id": identifier,
                        "name": f"selected:{identifier}",
                        "qmatrix_skill": "filter.boundary",
                        "preferred_mutations": (),
                        "manual_mutations": (),
                    },
                )
            )
        if args.select_count > 0:
            rows = rows[: args.select_count]
        return rows
    selected = []
    for spec in DEFAULT_CASES:
        if spec["id"] in records:
            selected.append((dict(records[spec["id"]]), dict(spec)))
    if not selected:
        raise ValueError("none of the default case IDs were found")
    return selected


def _markdown(report: Mapping[str, Any]) -> str:
    backend = str(report.get("backend") or "")
    if backend == "sqlite_compatibility_rehearsal":
        boundary_note = (
            "> SQLite 结果仅表示 SQLite compatibility rehearsal，不冒充原生 "
            "MySQL/PostgreSQL 语义证明。公开语料的 SQL、来源和 schema catalog 均保留 "
            "provenance；没有读取 hidden 明文。"
        )
    else:
        boundary_note = (
            "> 本报告通过 Docker 原生数据库执行器完成；结果只证明本报告列出的公开语料、"
            "见证数据和目标引擎版本，不外推为所有 SQL 的全局正确性。"
        )
    lines = [
        "# 新爬取真实 SQL 教学场景 Phase0–Phase6 审计",
        "",
        f"生成时间：`{report.get('generated_at')}`；后端演练：`{report.get('backend')}`。",
        "",
        boundary_note,
        "",
        "## 总览",
        "",
        f"- 语料：{', '.join(report.get('corpora') or [])}",
        f"- 典型案例：`{len(report.get('cases') or [])}`",
        f"- 路由分支总数：`{report.get('summary', {}).get('route_branch_count')}`",
        f"- learner-safe 字段隔离通过率：`{report.get('summary', {}).get('learner_safe_rate')}`",
        f"- Phase1 identity supported：`{report.get('summary', {}).get('identity_supported')}`",
        f"- Phase1 selected mutation supported wrong：`{report.get('summary', {}).get('selected_supported_wrong')}`",
        f"- witness/evidence gap：`{report.get('summary', {}).get('witness_evidence_gap_count')}`",
        "",
        "## 分支含义",
        "",
        "`CORRECT` 是当前有界世界中未发现反例的教学性接受；`WRONG` 是已在有界见证世界中区分；`JUDGE_UNDECIDED`/`KNOWN_GAP` 不写入提交或 BKT。语法错误和安全拦截写入行为审计，但不成为语义 skill observation。",
        "",
    ]
    for case in report.get("cases") or []:
        source = case.get("source") or {}
        p1i = (
            ((case.get("phase1") or {}).get("identity") or {}).get("summary") or {}
        )
        p1w = (
            ((case.get("phase1") or {}).get("selected_student") or {}).get("summary")
            or {}
        )
        p2w = (
            ((case.get("phase2") or {}).get("selected_student") or {}).get("primary")
            or {}
        )
        p3 = case.get("phase3") or {}
        branches = ((case.get("phase4_5_6") or {}).get("branches") or [])
        selected_phase1 = (case.get("phase1") or {}).get("selected_student") or {}
        selected_structure = selected_phase1.get("structure") or {}
        lines.extend(
            [
                f"## {case.get('case_name')} · `{source.get('record_id')}`",
                "",
                f"来源：`{source.get('source_name')}` / `{source.get('member')}`；方言：`{source.get('dialect')}`；schema trust：`{source.get('schema_trust')}`。",
                "",
                "### SQL",
                "",
                "标准答案（公开来源）：",
                "```sql",
                str((case.get("sql") or {}).get("standard") or ""),
                "```",
                "",
                f"本例选取变异：`{(case.get('selection') or {}).get('mutation_name')}`",
                "```sql",
                str((case.get("sql") or {}).get("selected_student") or ""),
                "```",
                "",
                "### 每环节实际记录",
                "",
                "| 环节 | 结果 | 关键证据 |",
                "|---|---|---|",
                f"| Phase0 provenance/schema | READY | `{len(source.get('tables') or [])}` tables; `{source.get('provenance_hash')}` |",
                f"| Phase1 identity | `{p1i.get('status')}/{p1i.get('equivalence_conclusion')}` | rows `{p1i.get('standard_row_count')}`; backend `{p1i.get('execution_backend')}` |",
                f"| Phase1 mutation | `{p1w.get('status')}/{p1w.get('equivalence_conclusion')}` | rows `{p1w.get('standard_row_count')}` vs `{p1w.get('student_row_count')}`; diffs `{selected_structure.get('ast_diff_count')}` |",
                f"| Phase2 diagnosis | `{((case.get('phase2') or {}).get('selected_student') or {}).get('status')}` | rule `{p2w.get('rule_id')}`; grade `{p2w.get('evidence_grade')}`; logical stage `{p2w.get('logical_stage')}` |",
                f"| Phase3 Q-matrix/BKT | `{(p3.get('selected_student_plan') or {}).get('status') if isinstance(p3.get('selected_student_plan'), Mapping) else 'see JSON'}` | declared `{(p3.get('qmatrix') or {}).get('skill_id')}` |",
                "| Phase4/5 learner delivery | see branches | deterministic safe template; no internal package returned |",
                f"| Phase6 persistence | `{len(branches)}` route branches | teaching audit / behavior / chat captured per branch |",
                "",
                "### Phase2 → Phase5 learner feedback",
                "",
                str(
                    ((case.get("phase2") or {}).get("selected_student") or {}).get(
                        "learner_safe_diagnostic_feedback"
                    )
                    or "（无：该分支为边界/未决）"
                ),
                "",
                "### Route branch trace",
                "",
                "| 分支 | HTTP/判题 | Phase3 | 学生可见反馈摘要 |",
                "|---|---|---|---|",
            ]
        )
        for branch in branches:
            phase3 = branch.get("phase3_learning") or {}
            hint = re.sub(
                r"\s+", " ", str(branch.get("hint_text") or branch.get("error") or "")
            )
            lines.append(
                f"| `{branch.get('label')}` | `{_branch_status(branch)}` | `{phase3.get('status', '-')}` | {hint[:180]} |"
            )
        boundaries = case.get("boundaries") or []
        if boundaries:
            lines.extend(["", "### 边界/缺口", ""])
            for item in boundaries:
                lines.append(
                    f"- `{item.get('kind')}`：{item.get('message') or item.get('reason')}"
                )
        lines.append("")
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.max_rows < 1 or args.max_rows > 32:
        raise ValueError("--max-rows must be between 1 and 32")
    corpus_paths = tuple(args.corpus or DEFAULT_CORPORA)
    records: dict[str, dict[str, Any]] = {}
    for path in corpus_paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            identifier = str(row.get("id") or "")
            if identifier:
                records[identifier] = row
    selected = _select_records(records, args)
    if not selected:
        raise ValueError("selector returned no records")

    execution_backend = str(args.execution_backend or "sqlite").strip().lower()
    native_executor_url = str(args.native_executor_url or "").strip() or None
    selected_dialects = {_dialect(record) for record, _spec in selected}
    if execution_backend != "sqlite":
        if not native_executor_url:
            raise ValueError(
                "--native-executor-url is required for a native audit; "
                "the result must not silently fall back to SQLite"
            )
        if execution_backend == "native" and not selected_dialects <= {
            "mysql",
            "postgres",
        }:
            raise ValueError(
                "native audit currently supports the Docker MySQL/PostgreSQL "
                "corpora only"
            )
        if execution_backend in {"mysql", "postgres"} and selected_dialects - {
            execution_backend
        }:
            raise ValueError(
                f"{execution_backend} audit selected records from another SQL dialect"
            )

    previous_backend = ai_router.settings.PARSEVAL_EXECUTION_BACKEND
    previous_worker_mode = getattr(ai_router.settings, "PARSEVAL_WORKER_MODE", "process")
    previous_native_urls = {
        "mysql": getattr(ai_router.settings, "PARSEVAL_MYSQL_URL", ""),
        "postgres": getattr(ai_router.settings, "PARSEVAL_POSTGRES_URL", ""),
    }
    previous_environment = {
        "PARSEVAL_EXECUTION_BACKEND": os.environ.get("PARSEVAL_EXECUTION_BACKEND"),
        "PARSEVAL_MYSQL_URL": os.environ.get("PARSEVAL_MYSQL_URL"),
        "PARSEVAL_POSTGRES_URL": os.environ.get("PARSEVAL_POSTGRES_URL"),
    }
    ai_router.settings.PARSEVAL_EXECUTION_BACKEND = execution_backend
    # The audit deliberately exercises the production process worker.  Spawned
    # workers import Settings afresh, so mirror the temporary audit settings in
    # the environment instead of relying on a parent-process object mutation.
    os.environ["PARSEVAL_EXECUTION_BACKEND"] = execution_backend
    if execution_backend != "sqlite":
        target_dialects = selected_dialects if execution_backend == "native" else {execution_backend}
        for target_dialect in target_dialects:
            setting_name = {
                "mysql": "PARSEVAL_MYSQL_URL",
                "postgres": "PARSEVAL_POSTGRES_URL",
            }[target_dialect]
            setattr(ai_router.settings, setting_name, native_executor_url or "")
            os.environ[setting_name] = native_executor_url or ""
    ai_router.settings.PARSEVAL_WORKER_MODE = "process"
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        async with session_factory() as session:
            user = User(
                email="real-teaching-audit@example.com",
                username="real-teaching-audit",
                password="audit-only-password",
            )
            session.add(user)
            await session.commit()
            cases = []
            for index, (record, spec) in enumerate(selected):
                cases.append(
                    await _run_case(
                        session=session,
                        user=user,
                        record=record,
                        spec=spec,
                        case_index=index,
                        max_rows=args.max_rows,
                        execution_backend=execution_backend,
                        native_executor_url=native_executor_url,
                    )
                )
            branch_items = [
                branch
                for case in cases
                for branch in (case.get("phase4_5_6") or {}).get("branches", [])
            ]
            learner_safe_count = sum(
                bool(item.get("learner_internal_fields_absent")) for item in branch_items
            )
            identity_supported = sum(
                (
                    ((case.get("phase1") or {}).get("identity") or {})
                    .get("summary", {})
                    .get("status")
                    in {"SUPPORTED", "SUPPORTED_WITH_LIMITS"}
                )
                for case in cases
            )
            selected_supported_wrong = sum(
                (
                    ((case.get("phase1") or {}).get("selected_student") or {})
                    .get("summary", {})
                    .get("status")
                    in {"SUPPORTED", "SUPPORTED_WITH_LIMITS"}
                    and (
                        ((case.get("phase1") or {}).get("selected_student") or {})
                        .get("summary", {})
                        .get("equivalence_conclusion")
                        == "NOT_EQUIVALENT"
                    )
                )
                for case in cases
            )
            gaps = sum(
                1
                for case in cases
                if any(
                    item.get("kind") == "witness_evidence_gap"
                    for item in case.get("boundaries", [])
                )
            )
            from datetime import datetime, timezone

            report = {
                "schema_version": "real_teaching_scenario_audit.v1",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "backend": (
                    "sqlite_compatibility_rehearsal"
                    if execution_backend == "sqlite"
                    else (
                        f"{next(iter(selected_dialects))}_native"
                        if execution_backend == "native" and len(selected_dialects) == 1
                        else f"{execution_backend}_native"
                    )
                ),
                "execution_backend": execution_backend,
                "corpora": [str(path) for path in corpus_paths],
                "selector": {
                    "case_ids": args.case_id or [],
                    "source": args.select_source,
                    "label": args.select_label,
                    "count": len(selected),
                },
                "summary": {
                    "case_count": len(cases),
                    "route_branch_count": len(branch_items),
                    "learner_safe_rate": (
                        round(learner_safe_count / len(branch_items), 6)
                        if branch_items
                        else 0.0
                    ),
                    "identity_supported": f"{identity_supported}/{len(cases)}",
                    "selected_supported_wrong": f"{selected_supported_wrong}/{len(cases)}",
                    "witness_evidence_gap_count": gaps,
                    "route_status_counts": dict(
                        Counter(_branch_status(item) for item in branch_items)
                    ),
                    "phase3_status_counts": dict(
                        Counter(
                            (item.get("phase3_learning") or {}).get(
                                "status", "HTTP_OR_NONE"
                            )
                            for item in branch_items
                        )
                    ),
                },
                "phase3_catalog": rule_skill_catalog_metadata(),
                "cases": cases,
                "database_counts": {
                    "submissions": await _count(session, Submission),
                    "skill_observation_events": await _count(
                        session, SkillObservationEvent
                    ),
                    "student_skill_states": await _count(session, StudentSkillState),
                    "behavior_events": await _count(session, Phase3BehaviorEvent),
                    "teaching_audits": await _count(session, SubmissionTeachingAudit),
                    "chat_messages": await _count(session, ChatMessage),
                },
                "checks": {
                    "all_learner_internal_fields_absent": learner_safe_count
                    == len(branch_items),
                    "full_branch_matrix_present": any(
                        {
                            "correct_with_author_declared_qmatrix",
                            "incorrect_selected_mutation",
                            "same_attempt_replay",
                            "correct_without_qmatrix",
                            "syntax_error",
                            "safety_block",
                        }
                        <= set(
                            (case.get("phase4_5_6") or {}).get(
                                "branch_statuses", {}
                            )
                        )
                        for case in cases
                    ),
                    "qmatrix_taxonomy_is_frozen": all(
                        (case.get("phase3") or {})
                        .get("qmatrix", {})
                        .get("taxonomy_version")
                        == ATOMIC_SKILL_TAXONOMY_VERSION
                        for case in cases
                    ),
                    "backend_boundary_is_explicit": (
                        execution_backend == "sqlite"
                        or bool(native_executor_url)
                    ),
                },
            }
    finally:
        await engine.dispose()
        ai_router.settings.PARSEVAL_EXECUTION_BACKEND = previous_backend
        for target_dialect, setting_value in previous_native_urls.items():
            setattr(
                ai_router.settings,
                {
                    "mysql": "PARSEVAL_MYSQL_URL",
                    "postgres": "PARSEVAL_POSTGRES_URL",
                }[target_dialect],
                setting_value,
            )
        for environment_name, environment_value in previous_environment.items():
            if environment_value is None:
                os.environ.pop(environment_name, None)
            else:
                os.environ[environment_name] = environment_value
        ai_router.settings.PARSEVAL_WORKER_MODE = previous_worker_mode
    report["overall_pass"] = all(report["checks"].values())
    return report


async def _run_with_heartbeat(args: argparse.Namespace) -> dict[str, Any]:
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
    report = asyncio.run(_run_with_heartbeat(args))
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(report.get("summary"), ensure_ascii=False, indent=2))
    print(f"JSON: {args.output_json}")
    print(f"Markdown: {args.output_md}")
    if not report.get("overall_pass"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()


async def _run_case(
    *,
    session: AsyncSession,
    user: User,
    record: Mapping[str, Any],
    spec: Mapping[str, Any],
    case_index: int,
    max_rows: int,
    execution_backend: str = "sqlite",
    native_executor_url: str | None = None,
) -> dict[str, Any]:
    standard_sql = str(record["sql"]).strip().rstrip(";")
    dialect = _dialect(record)
    schema_text = str(record.get("schema") or "")
    catalog = record.get("schema_catalog") or {}
    question = Question(
        title=f"真实语料审计 · {spec.get('name') or record.get('id')}",
        content=(
            f"公开教程 {record.get('member') or ''} 的可重放练习。"
            "请写出与题目要求等价的查询。"
        ),
        difficulty=5,
        correct_sql=standard_sql,
        sql_dialect=dialect,
        schema_preview=_schema_preview(record),
        required_output_columns=None,
    )
    session.add(question)
    await session.flush()
    qmatrix_skill = str(spec.get("qmatrix_skill") or "filter.boundary")
    qmatrix_spec = [
        QuestionSkillSpec(
            skill_id=qmatrix_skill,
            taxonomy_version=ATOMIC_SKILL_TAXONOMY_VERSION,
            role=QuestionSkillRole.PRIMARY,
            observable_on_correct=True,
        )
    ]
    await QuestionSkillRepository(session).replace_for_question(
        question.id,
        qmatrix_spec,
        provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
    )
    await session.commit()

    def direct(student_sql: str) -> Any:
        from core.parseval_data_generator import generate_and_compare

        return generate_and_compare(
            schema_text,
            standard_sql,
            student_sql,
            max_rows_per_table=max_rows,
            sql_dialect=dialect,
            default_sql_dialect="mysql",
            execution_backend=execution_backend,
            native_executor_url=native_executor_url,
            schema_catalog=catalog,
        )

    identity_run = direct(standard_sql)
    candidates = _mutation_candidates(record, spec)
    candidate_reports = []
    selected_name = None
    selected_sql = None
    selected_run = None
    for name, sql, _labels in candidates:
        try:
            run = direct(sql)
        except Exception as exc:
            candidate_reports.append(
                {
                    "name": name,
                    "student_sql": sql,
                    "exception_type": type(exc).__name__,
                    "exception": _clip(str(exc)),
                }
            )
            continue
        candidate_reports.append(_candidate_summary(name, sql, run))
        if selected_run is None:
            selected_name, selected_sql, selected_run = name, sql, run
        # Prefer a supported counterexample; otherwise retain the first honest
        # candidate so an evidence gap remains visible in the case report.
        if (
            run.status in {"SUPPORTED", "SUPPORTED_WITH_LIMITS"}
            and run.equivalence_conclusion == "NOT_EQUIVALENT"
        ):
            selected_name, selected_sql, selected_run = name, sql, run
            break
    if selected_run is None:
        selected_name, selected_sql, selected_run = "no_mutation", standard_sql, identity_run

    identity_phase2 = _phase2_detail(
        run=identity_run,
        record=record,
        student_sql=standard_sql,
        is_correct=True,
    )
    wrong_phase2 = _phase2_detail(
        run=selected_run,
        record=record,
        student_sql=selected_sql,
        is_correct=(selected_run.equivalence_conclusion == "NO_COUNTEREXAMPLE_FOUND"),
    )

    identity_plan: Any = None
    wrong_plan: Any = None
    try:
        identity_pkg = identity_phase2.get("public_package")
        if isinstance(identity_pkg, Mapping):
            identity_plan = await prepare_phase3_attempt(
                session,
                user_id=user.id,
                question_id=question.id,
                expected_is_correct=True,
                diagnostic_package=identity_pkg,
                answer_revealed=False,
            )
    except Exception as exc:
        identity_plan = {"error_type": type(exc).__name__, "error": _clip(str(exc))}
    try:
        wrong_pkg = wrong_phase2.get("public_package")
        if isinstance(wrong_pkg, Mapping):
            wrong_plan = await prepare_phase3_attempt(
                session,
                user_id=user.id,
                question_id=question.id,
                expected_is_correct=False,
                diagnostic_package=wrong_pkg,
                answer_revealed=False,
            )
    except Exception as exc:
        wrong_plan = {"error_type": type(exc).__name__, "error": _clip(str(exc))}

    branches: list[dict[str, Any]] = []
    full_matrix = case_index == 0
    correct = await _submit_route(
        session,
        user=user,
        question=question,
        sql=standard_sql,
        label="correct_with_author_declared_qmatrix",
    )
    branches.append(correct)
    wrong = await _submit_route(
        session,
        user=user,
        question=question,
        sql=selected_sql,
        label="incorrect_selected_mutation",
    )
    branches.append(wrong)
    if full_matrix and "submission_id" in wrong:
        replay = await _submit_route(
            session,
            user=user,
            question=question,
            sql=selected_sql,
            label="same_attempt_replay",
            attempt_id=str(wrong["attempt_id"]),
        )
        branches.append(replay)

    if full_matrix:
        await QuestionSkillRepository(session).replace_for_question(
            question.id,
            [],
            provenance=QuestionSkillProvenance.AUTHOR_DECLARED,
        )
        await session.commit()
        branches.append(
            await _submit_route(
                session,
                user=user,
                question=question,
                sql=standard_sql,
                label="correct_without_qmatrix",
            )
        )
        branches.append(
            await _submit_route(
                session,
                user=user,
                question=question,
                sql="SELECT * FROM (FAC_FLOOR",
                label="syntax_error",
            )
        )
        branches.append(
            await _submit_route(
                session,
                user=user,
                question=question,
                sql="DROP TABLE Products",
                label="safety_block",
            )
        )

    identity_summary = _phase1_detail(identity_run)
    wrong_summary = _phase1_detail(selected_run)
    case_result = {
        "case_index": case_index,
        "case_name": spec.get("name"),
        "source": _phase0(record),
        "sql": {"standard": standard_sql, "selected_student": selected_sql},
        "selection": {
            "mutation_name": selected_name,
            "candidate_count": len(candidate_reports),
            "candidates": candidate_reports,
        },
        "phase1": {
            "identity": identity_summary,
            "selected_student": wrong_summary,
            "structure_ir": {
                "identity": _structure_ir(standard_sql, standard_sql, dialect),
                "selected_student": _structure_ir(standard_sql, selected_sql, dialect),
            },
        },
        "phase2": {
            "identity": identity_phase2,
            "selected_student": wrong_phase2,
        },
        "phase3": {
            "qmatrix": {
                "provenance": "AUTHOR_DECLARED",
                "taxonomy_version": ATOMIC_SKILL_TAXONOMY_VERSION,
                "skill_id": qmatrix_skill,
                "role": "PRIMARY",
                "observable_on_correct": True,
            },
            "rule_skill_catalog": rule_skill_catalog_metadata(),
            "identity_plan": (
                _plan_snapshot(identity_plan)
                if not isinstance(identity_plan, Mapping)
                else identity_plan
            ),
            "selected_student_plan": (
                _plan_snapshot(wrong_plan)
                if not isinstance(wrong_plan, Mapping)
                else wrong_plan
            ),
        },
        "phase4_5_6": {
            "branches": branches,
            "branch_statuses": {
                item.get("label"): _branch_status(item) for item in branches
            },
        },
        "boundaries": [],
    }
    for run in (identity_run, selected_run):
        if run.status not in {"SUPPORTED", "SUPPORTED_WITH_LIMITS"}:
            case_result["boundaries"].append(
                {
                    "kind": "phase1_verdict_boundary",
                    "status": run.status,
                    "equivalence_conclusion": run.equivalence_conclusion,
                    "reason": run.boundary_evidence or run.error,
                }
            )
        if (run.data_evidence or {}).get("execution_backend") == "sqlite":
            case_result["boundaries"].append(
                {
                    "kind": "backend_boundary",
                    "message": (
                        "SQLite compatibility rehearsal; not native "
                        "MySQL/PostgreSQL semantic proof."
                    ),
                }
            )
    if (
        selected_run.standard_rows == selected_run.student_rows
        and selected_run.equivalence_conclusion == "UNDECIDED"
    ):
        case_result["boundaries"].append(
            {
                "kind": "witness_evidence_gap",
                "message": (
                    "Generated bounded world did not distinguish the selected "
                    "real-query mutation."
                ),
            }
        )
    return case_result


def _select_records(
    records: Mapping[str, Mapping[str, Any]], args: argparse.Namespace
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    if args.case_id:
        selected = []
        for identifier in args.case_id:
            if identifier not in records:
                raise ValueError(f"case id not found: {identifier}")
            spec = next(
                (item for item in DEFAULT_CASES if item["id"] == identifier),
                {
                    "id": identifier,
                    "name": identifier,
                    "qmatrix_skill": "filter.boundary",
                    "preferred_mutations": (),
                    "manual_mutations": (),
                },
            )
            selected.append((dict(records[identifier]), dict(spec)))
        return selected
    if args.select_source or args.select_label:
        rows = []
        for identifier in sorted(records):
            row = records[identifier]
            if args.select_source and str(row.get("source_id")) != args.select_source:
                continue
            labels = {str(item) for item in (row.get("cfg_labels") or [])}
            if args.select_label and args.select_label not in labels:
                continue
            rows.append(
                (
                    dict(row),
                    {
                        "id": identifier,
                        "name": f"selected:{identifier}",
                        "qmatrix_skill": "filter.boundary",
                        "preferred_mutations": (),
                        "manual_mutations": (),
                    },
                )
            )
        if args.select_count > 0:
            rows = rows[: args.select_count]
        return rows
    selected = []
    for spec in DEFAULT_CASES:
        if spec["id"] in records:
            selected.append((dict(records[spec["id"]]), dict(spec)))
    if not selected:
        raise ValueError("none of the default case IDs were found")
    return selected
