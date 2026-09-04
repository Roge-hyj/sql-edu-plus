"""Minimal SQLite Phase 1 -> Phase 2 orchestration.

The reference SQL and complete witness worlds remain server-side.  Callers
should serialize only :meth:`PipelineResult.learner_hint` for a learner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from core.error_diagnosis import (
    DiagnosticPackage,
    diagnose_record,
    sanitize_public_package,
)
from core.parseval_data_generator import SandboxRun, generate_and_compare


HINT_SCHEMA_VERSION = "sqlite-phase12.hint.v1"
_HINT_SLOTS = (
    ("LOCATION", "student_behavior"),
    ("WITNESS", "conflict_and_witness"),
    ("REFLECTION", "guidance_question"),
)


@dataclass(frozen=True)
class PipelineResult:
    """Internal result containing rich Phase 1 evidence and Phase 2 diagnosis."""

    phase1: SandboxRun
    phase2: DiagnosticPackage

    def learner_hint(self, level: int = 1) -> dict[str, Any]:
        """Return one learner-safe hint about the primary diagnosed error.

        Levels disclose one slot at a time instead of returning all three:
        location, then a minimal witness, then a reflective question.  The
        method never serializes the reference SQL, mutation SQL, complete
        witness database, secondary candidates, or internal causal trace.
        """

        if level not in {1, 2, 3}:
            raise ValueError("hint level must be 1, 2, or 3")

        public = self.phase2.to_dict()
        kind, narrative_key = _HINT_SLOTS[level - 1]
        narrative = public.get("narrative")
        if not isinstance(narrative, Mapping):
            narrative = {}
        message = str(narrative.get(narrative_key) or "").strip()

        payload: dict[str, Any] = {
            "schema_version": HINT_SCHEMA_VERSION,
            "engine": "sqlite",
            "verdict": public.get("verdict"),
            "diagnosis_status": public.get("diagnosis_status"),
            "phase1": public.get("phase1"),
            "focus": public.get("primary"),
            "hint": {
                "level": level,
                "kind": kind,
                "message": message,
            },
        }
        if level == 2 and public.get("witness") is not None:
            payload["witness"] = public["witness"]

        return sanitize_public_package(
            payload,
            forbidden_values=self.phase2._forbidden_values,
        )


def run_pipeline(
    *,
    schema_text: str,
    reference_sql: str,
    student_sql: str,
    question: str = "",
    language: str = "zh-CN",
    max_rows_per_table: int = 8,
    schema_catalog: Mapping[str, Any] | None = None,
) -> PipelineResult:
    """Run bounded witness synthesis followed by evidence-only diagnosis."""

    phase1 = generate_and_compare(
        schema_text,
        reference_sql,
        student_sql,
        max_rows_per_table=max_rows_per_table,
        schema_catalog=dict(schema_catalog) if schema_catalog is not None else None,
    )
    phase2 = diagnose_record(
        sandbox_run=phase1,
        question=question,
        schema=schema_catalog if schema_catalog is not None else schema_text,
        student_sql=student_sql,
        language=language,
    )
    return PipelineResult(phase1=phase1, phase2=phase2)


__all__ = ["HINT_SCHEMA_VERSION", "PipelineResult", "run_pipeline"]
