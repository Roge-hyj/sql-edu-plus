"""Build one evidence-backed capability assessment for the SQL teaching chain.

The existing audits answer different questions.  This evaluator does not
re-run them or merge their denominators blindly; it consumes their reports and
publishes one versioned scorecard with explicit denominators, confidence
intervals, exclusions, and stage ownership.

It is intentionally a bounded report composer.  It never loads a large SQL
dump, starts a model, or treats a SQLite compatibility result as native vendor
semantics.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUTS = PROJECT_ROOT / "data_construct_test" / "outputs"
SCHEMA_VERSION = "system.capability-evaluation.v1"
EVALUATOR_VERSION = "system-capability-evaluator.v1.2"


def _default_path(name: str) -> Path:
    return OUTPUTS / name


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="optional enriched external JSONL for provenance completeness",
    )
    parser.add_argument("--identity", type=Path, default=None)
    parser.add_argument("--identity-failures", type=Path, default=None)
    parser.add_argument("--mutation", type=Path, default=None)
    parser.add_argument("--mutation-failures", type=Path, default=None)
    parser.add_argument(
        "--phase1-report",
        type=Path,
        default=None,
        help="optional combined Phase 1 report with web_corpus_identity/mutation origins",
    )
    parser.add_argument(
        "--phase1-failures",
        type=Path,
        default=None,
        help="failure ledger paired with --phase1-report",
    )
    parser.add_argument(
        "--fuzzer",
        type=Path,
        default=_default_path("e2e_robustness_fuzzer_report.json"),
    )
    parser.add_argument(
        "--profiles",
        type=Path,
        default=_default_path("phase1_cfg_database_profiles_report.json"),
    )
    parser.add_argument(
        "--large",
        type=Path,
        default=None,
        help="optional large generated CFG/ASTDiff convergence report",
    )
    parser.add_argument(
        "--phase2",
        type=Path,
        default=_default_path("phase2_acceptance_report.json"),
    )
    parser.add_argument(
        "--full-chain",
        type=Path,
        default=_default_path("new_web_full_chain_audit_20260826.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_default_path("system_capability_evaluation_20260826.json"),
    )
    parser.add_argument("--markdown", type=Path, default=None)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="return non-zero when any declared gate is FAIL",
    )
    return parser.parse_args()


def _load_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _load_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        return []
    return rows


def _sha256(path: Path | None) -> str | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _input_record(path: Path | None) -> dict[str, Any]:
    return {
        "path": str(path) if path is not None else None,
        "exists": bool(path and path.exists()),
        "sha256": _sha256(path),
        "bytes": path.stat().st_size if path is not None and path.exists() else None,
    }


def _wilson(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    if trials <= 0:
        return (0.0, 1.0)
    successes = max(0, min(successes, trials))
    p = successes / trials
    denominator = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denominator
    margin = z * math.sqrt(
        p * (1 - p) / trials + z * z / (4 * trials * trials)
    ) / denominator
    return (max(0.0, center - margin), min(1.0, center + margin))


def _status_for_rate(
    successes: int,
    trials: int,
    threshold: float | None,
    *,
    statistical: bool,
    min_trials: int,
) -> str:
    if trials <= 0:
        return "NOT_APPLICABLE"
    if threshold is None:
        return "PASS"
    rate = successes / trials
    if rate < threshold:
        return "FAIL"
    if statistical:
        lower, _ = _wilson(successes, trials)
        if trials < min_trials or lower < threshold:
            return "WARN"
    return "PASS"


def _metric(
    *,
    stage: str,
    metric_id: str,
    successes: int,
    trials: int,
    threshold: float | None,
    source: str,
    denominator_policy: str,
    exclusions: Iterable[str] = (),
    notes: str = "",
    statistical: bool = True,
    min_trials: int = 30,
) -> dict[str, Any]:
    successes = max(0, min(int(successes), max(int(trials), 0)))
    trials = max(0, int(trials))
    lower, upper = _wilson(successes, trials)
    return {
        "stage": stage,
        "metric_id": metric_id,
        "successes": successes,
        "trials": trials,
        "rate": round(successes / trials, 8) if trials else None,
        "wilson_95": [round(lower, 8), round(upper, 8)] if trials else None,
        "threshold": threshold,
        "status": _status_for_rate(
            successes,
            trials,
            threshold,
            statistical=statistical,
            min_trials=min_trials,
        ),
        "statistical_gate": statistical,
        "minimum_trials": min_trials if statistical else None,
        "source": source,
        "denominator_policy": denominator_policy,
        "exclusions": sorted(set(str(item) for item in exclusions)),
        "notes": notes,
    }


def _metric_from_summary(
    metrics: list[dict[str, Any]],
    *,
    stage: str,
    metric_id: str,
    summary: dict[str, Any],
    field: str,
    threshold: float | None,
    source: str,
    denominator_policy: str,
    exclusions: Iterable[str] = (),
    notes: str = "",
    statistical: bool = True,
    min_trials: int = 30,
) -> None:
    item = (summary.get("stage_counts") or {}).get(field)
    if not isinstance(item, dict):
        return
    metrics.append(
        _metric(
            stage=stage,
            metric_id=metric_id,
            successes=int(item.get("passed") or 0),
            trials=int(item.get("total") or 0),
            threshold=threshold,
            source=source,
            denominator_policy=denominator_policy,
            exclusions=exclusions,
            notes=notes,
            statistical=statistical,
            min_trials=min_trials,
        )
    )


def _family_count(summary: dict[str, Any], family: str, outcome: str) -> int:
    families = summary.get("family_counts") or {}
    item = families.get(family) or {}
    return int(item.get(outcome) or 0)


def _failure_class(row: dict[str, Any]) -> str:
    text = " ".join(
        str(row.get(key) or "")
        for key in ("error", "failure_signature", "failure_class", "verdict_status")
    ).lower()
    if "schema_qualification_failed" in text or "missing_physical_columns" in text:
        return "INPUT_SCHEMA"
    if "unsupported_sqlite_feature" in text or "engine_gap" in text:
        return "BACKEND_BOUNDARY"
    expectation = str(row.get("expectation") or "")
    if expectation == "not_equivalent" and row.get("is_equivalent") is True:
        return "WITNESS_NO_COUNTEREXAMPLE"
    if expectation == "not_equivalent":
        mutation = row.get("mutation_evidence") or {}
        executed = int((mutation.get("summary") or {}).get("executed") or 0)
        if row.get("executed") and not executed:
            return "MUTATION_EVIDENCE_GAP"
        if row.get("executed") and row.get("is_equivalent") is False:
            return "MUTATION_EVIDENCE_GAP"
    if "ast_diff" in text:
        return "AST_DIFF_GAP"
    if "unclassified" in text:
        return "UNCLASSIFIED"
    return "SYSTEM_FAILURE"


def _failure_breakdown(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        counts[_failure_class(row)] += 1
    return dict(sorted(counts.items()))


def _corpus_provenance(path: Path | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows = _load_jsonl(path)
    if not rows:
        return {}, []
    complete = [
        row
        for row in rows
        if row.get("source_id")
        and row.get("source_url")
        and row.get("provenance_hash")
        and isinstance(row.get("schema_catalog"), dict)
    ]
    sources = sorted({str(row.get("source_id")) for row in rows if row.get("source_id")})
    return {
        "records": len(rows),
        "complete_records": len(complete),
        "source_ids": sources,
        "source_urls": sorted({str(row.get("source_url")) for row in rows if row.get("source_url")}),
        "schema_records": sum(isinstance(row.get("schema_catalog"), dict) for row in rows),
    }, rows


def _add_phase0(
    metrics: list[dict[str, Any]],
    *,
    corpus_path: Path | None,
    identity: dict[str, Any] | None,
) -> dict[str, Any]:
    details, _rows = _corpus_provenance(corpus_path)
    if details:
        metrics.append(
            _metric(
                stage="PHASE0_SOURCE",
                metric_id="external.provenance_completeness",
                successes=details["complete_records"],
                trials=details["records"],
                threshold=1.0,
                source=str(corpus_path),
                denominator_policy="all enriched records; source id/url/hash and schema catalog required",
                notes="External data provenance and authoritative schema are prerequisites, not a sampled SQL-judge rate.",
                statistical=False,
            )
        )
        metrics.append(
            _metric(
                stage="PHASE0_SOURCE",
                metric_id="external.schema_availability",
                successes=details["schema_records"],
                trials=details["records"],
                threshold=1.0,
                source=str(corpus_path),
                denominator_policy="all enriched records; schema_catalog must be present",
                notes="Completeness invariant for the supplied corpus, not a random-sample confidence claim.",
                statistical=False,
            )
        )
    elif identity:
        summary = identity.get("summary") or {}
        total = int(summary.get("total_cases") or 0)
        metrics.append(
            _metric(
                stage="PHASE0_SOURCE",
                metric_id="external.audit_record_coverage",
                successes=total,
                trials=total,
                threshold=None,
                source="identity report",
                denominator_policy="records represented by the external audit report",
                statistical=False,
                notes="Corpus JSONL was not supplied; this is audit-record coverage only.",
            )
        )
    return details


def _add_assessment_completeness(
    metrics: list[dict[str, Any]],
    *,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Require every artifact needed for a closed Phase0–Phase6 assessment.

    A report with only the artifacts that happen to be available is useful for
    development, but it must not be presented as a closed system assessment.
    In particular, the JSONL failure ledgers are required even when they are
    empty: their presence proves that the runner deliberately emitted the
    failure stream rather than that it was never collected.
    """

    combined_report_path = getattr(args, "phase1_report", None)
    combined_failures_path = getattr(args, "phase1_failures", None)
    combined_report_available = bool(
        combined_report_path and _load_json(combined_report_path) is not None
    )
    combined_failures_available = bool(
        combined_failures_path and combined_failures_path.exists()
    )
    requirements: dict[str, bool] = {
        "corpus": bool(args.corpus and _load_jsonl(args.corpus)),
        "identity": combined_report_available or _load_json(args.identity) is not None,
        "identity_failures": combined_failures_available or bool(
            args.identity_failures and args.identity_failures.exists()
        ),
        "mutation": combined_report_available or _load_json(args.mutation) is not None,
        "mutation_failures": combined_failures_available or bool(
            args.mutation_failures and args.mutation_failures.exists()
        ),
        "phase2": _load_json(args.phase2) is not None,
        "full_chain": _load_json(args.full_chain) is not None,
    }
    present = sum(requirements.values())
    total = len(requirements)
    missing = [name for name, available in requirements.items() if not available]
    metrics.append(
        _metric(
            stage="PHASE0_SOURCE",
            metric_id="assessment.required_evidence_completeness",
            successes=present,
            trials=total,
            threshold=1.0,
            source="capability evaluator input contract",
            denominator_policy="all seven artifacts required for a closed Phase0–Phase6 assessment",
            notes=(
                "All required reports and ledgers are present."
                if not missing
                else "Missing required evidence: " + ", ".join(missing)
            ),
            statistical=False,
        )
    )
    return {
        "closed": not missing,
        "required_artifacts": requirements,
        "missing": missing,
        "present": present,
        "total": total,
    }


def _add_phase1_report(
    metrics: list[dict[str, Any]],
    *,
    report: dict[str, Any] | None,
    failures_path: Path | None,
    label: str,
    identity_mode: bool,
) -> dict[str, Any]:
    if not report:
        return {"label": label, "available": False, "failure_breakdown": {}}
    summary = report.get("summary") or {}
    source = str(report.get("web_corpus") or report.get("generated_at") or label)
    failures = _load_jsonl(failures_path)
    breakdown = _failure_breakdown(failures)
    origin = "web_corpus_identity" if identity_mode else "web_corpus_mutation"
    total = int((summary.get("corpus_origin_counts") or {}).get(origin) or 0)
    if not total:
        total = int(summary.get("total_cases") or 0)

    for field, threshold in (
        ("structure", 0.99),
        ("data", 0.95 if identity_mode else 0.90),
        ("mutation", 0.90),
        ("attribution", 0.90),
        ("full_flow", 0.90 if not identity_mode else 0.95),
    ):
        _metric_from_summary(
            metrics,
            stage="PHASE1",
            metric_id=f"{label}.{field}",
            summary=summary,
            field=field,
            threshold=threshold,
            source=source,
            denominator_policy="report measured cases; source/schema gaps and explicit backend boundaries are excluded",
            exclusions=("input_schema_gap", "known_boundary"),
            notes=(
                "Identity controls measure preservation."
                if identity_mode
                else "Mutation cases measure counterexample and repair evidence, not only verdict correctness."
            ),
        )

    if identity_mode:
        input_gaps = breakdown.get("INPUT_SCHEMA", 0)
        backend_boundaries = breakdown.get("BACKEND_BOUNDARY", 0)
        eligible = max(0, total - input_gaps)
        supported = _family_count(summary, "WEB_CORPUS_IDENTITY", "supported")
        metrics.append(
            _metric(
                stage="PHASE1",
                metric_id=f"{label}.backend_compatibility_coverage",
                successes=supported,
                trials=eligible,
                threshold=0.95,
                source=source,
                denominator_policy="all external identities except source/schema-invalid cases; backend boundary remains a capability miss",
                exclusions=("INPUT_SCHEMA",),
                notes=f"backend_boundary={backend_boundaries}; input_schema={input_gaps}",
            )
        )
    else:
        negative = int((summary.get("expectation_counts") or {}).get("not_equivalent") or 0)
        if negative:
            for field, metric_id, threshold in (
                ("counterexample_detection_rate", "mutation.counterexample_detection", 0.90),
                ("mutation_executed_rate", "mutation.evidence_execution", 0.95),
                ("attribution_hit_rate", "mutation.attribution", 0.90),
            ):
                raw = summary.get(field)
                if raw is None:
                    continue
                successes = int(round(float(raw) * negative))
                metrics.append(
                    _metric(
                        stage="PHASE1",
                        metric_id=f"{label}.{metric_id}",
                        successes=successes,
                        trials=negative,
                        threshold=threshold,
                        source=source,
                        denominator_policy="all negative external mutations with expectation=not_equivalent",
                        exclusions=("INPUT_SCHEMA", "BACKEND_BOUNDARY"),
                        notes=f"reported_rate={raw}; integer successes reconstructed as round(rate * denominator)",
                    )
                )

    return {
        "label": label,
        "available": True,
        "total": total,
        "failure_breakdown": breakdown,
        "summary": summary,
        "failures": len(failures),
    }


def _add_combined_phase1_report(
    metrics: list[dict[str, Any]],
    *,
    report: dict[str, Any] | None,
    failures_path: Path | None,
) -> dict[str, Any]:
    """Score one report that contains identity and mutation origins.

    The convergence runner deliberately emits one report for a replay batch.
    Its ``family_counts`` provide the complete denominator while the failure
    ledger retains per-stage booleans for the non-supported cases.  Combining
    those two streams avoids treating a mixed identity/mutation denominator as
    either one, and avoids manufacturing a pair of synthetic reports merely
    for the scorecard.
    """
    if not report:
        return {"available": False}

    summary = report.get("summary") or {}
    source = str(report.get("web_corpus") or report.get("generated_at") or "combined Phase 1 report")
    failures = _load_jsonl(failures_path)
    family_counts = summary.get("family_counts") or {}

    def origin_totals(origin: str) -> tuple[int, int, int, list[dict[str, Any]]]:
        if origin == "web_corpus_identity":
            selected = {
                name: counts
                for name, counts in family_counts.items()
                if name == "WEB_CORPUS_IDENTITY"
            }
        else:
            selected = {
                name: counts
                for name, counts in family_counts.items()
                if str(name).startswith("WEB_CORPUS_")
                and name != "WEB_CORPUS_IDENTITY"
            }
        total = sum(
            sum(int(value or 0) for value in (counts or {}).values())
            for counts in selected.values()
        )
        supported = sum(
            int((counts or {}).get("supported") or 0)
            for counts in selected.values()
        )
        input_gaps = sum(
            int((counts or {}).get("input_schema_gap") or 0)
            for counts in selected.values()
        )
        rows = [row for row in failures if row.get("origin") == origin]
        if not total:
            total = supported + input_gaps + len(rows)
        return total, supported, input_gaps, rows

    stage_fields = (
        ("structure_stage_met", "structure"),
        ("data_stage_met", "data"),
        ("mutation_stage_met", "mutation"),
        ("attribution_stage_met", "attribution"),
        ("expectation_met", "full_flow"),
    )
    identity_total, identity_supported, identity_input, identity_failures = origin_totals(
        "web_corpus_identity"
    )
    mutation_total, mutation_supported, mutation_input, mutation_failures = origin_totals(
        "web_corpus_mutation"
    )

    def add_origin_metrics(
        *,
        label: str,
        total: int,
        supported: int,
        input_gaps: int,
        rows: list[dict[str, Any]],
        identity_mode: bool,
    ) -> dict[str, Any]:
        eligible = max(0, total - input_gaps)
        non_input_rows = [
            row for row in rows if row.get("scope_status") != "input_schema_gap"
        ]
        thresholds = (
            ("structure", 0.99),
            ("data", 0.95 if identity_mode else 0.90),
            ("mutation", 0.90),
            ("attribution", 0.90),
            ("full_flow", 0.95 if identity_mode else 0.90),
        )
        for field_name, metric_name in stage_fields:
            threshold = dict(thresholds)[metric_name]
            successes = supported + sum(
                bool(row.get(field_name)) for row in non_input_rows
            )
            metrics.append(
                _metric(
                    stage="PHASE1",
                    metric_id=f"{label}.{metric_name}",
                    successes=successes,
                    trials=eligible,
                    threshold=threshold,
                    source=source,
                    denominator_policy=(
                        "all external identity cases except input/schema gaps"
                        if identity_mode
                        else "all external negative mutations except input/schema gaps"
                    ),
                    exclusions=("input_schema_gap", "known_boundary"),
                    notes=(
                        "Derived from family_counts plus the per-case failure ledger."
                    ),
                )
            )

        if not identity_mode and eligible:
            executed = supported + sum(
                bool((row.get("mutation_summary") or {}).get("executed"))
                for row in non_input_rows
            )
            attribution = supported + sum(
                bool(row.get("attribution_stage_met"))
                for row in non_input_rows
            )
            metrics.append(
                _metric(
                    stage="PHASE1",
                    metric_id=f"{label}.counterexample_detection",
                    successes=supported + sum(
                        bool(row.get("data_stage_met"))
                        for row in non_input_rows
                    ),
                    trials=eligible,
                    threshold=0.90,
                    source=source,
                    denominator_policy="all external negative mutations except input/schema gaps",
                    exclusions=("input_schema_gap", "known_boundary"),
                    notes="Counterexample means the actual standard/student execution differed.",
                )
            )
            metrics.append(
                _metric(
                    stage="PHASE1",
                    metric_id=f"{label}.evidence_execution",
                    successes=executed,
                    trials=eligible,
                    threshold=0.95,
                    source=source,
                    denominator_policy="all external negative mutations except input/schema gaps",
                    exclusions=("input_schema_gap", "known_boundary"),
                )
            )
            metrics.append(
                _metric(
                    stage="PHASE1",
                    metric_id=f"{label}.negative_attribution",
                    successes=attribution,
                    trials=eligible,
                    threshold=0.90,
                    source=source,
                    denominator_policy="all external negative mutations except input/schema gaps",
                    exclusions=("input_schema_gap", "known_boundary"),
                )
            )
        return {
            "available": True,
            "label": label,
            "total": total,
            "eligible": eligible,
            "supported": supported,
            "input_schema_gaps": input_gaps,
            "failure_breakdown": _failure_breakdown(rows),
            "failures": len(rows),
        }

    identity_details = add_origin_metrics(
        label="external.combined.identity",
        total=identity_total,
        supported=identity_supported,
        input_gaps=identity_input,
        rows=identity_failures,
        identity_mode=True,
    )
    mutation_details = add_origin_metrics(
        label="external.combined.mutation",
        total=mutation_total,
        supported=mutation_supported,
        input_gaps=mutation_input,
        rows=mutation_failures,
        identity_mode=False,
    )
    return {
        "available": True,
        "source": source,
        "identity": identity_details,
        "mutation": mutation_details,
        "summary": summary,
        "failure_breakdown": _failure_breakdown(failures),
    }


def _add_existing_phase1(
    metrics: list[dict[str, Any]],
    *,
    fuzzer: dict[str, Any] | None,
    profiles: dict[str, Any] | None,
    large: dict[str, Any] | None,
) -> dict[str, Any]:
    details: dict[str, Any] = {}
    if fuzzer:
        summary = fuzzer.get("summary") or {}
        total = int(summary.get("total") or 0)
        passed = int((summary.get("status_counts") or {}).get("PASS") or 0)
        metrics.append(
            _metric(
                stage="PHASE1",
                metric_id="bounded_fuzzer.pass_rate",
                successes=passed,
                trials=total,
                threshold=0.99,
                source="e2e_robustness_fuzzer_report.json",
                denominator_policy="all bounded fuzzer cases",
                notes=f"validation_mode={summary.get('validation_mode')}; native_semantics_verified={summary.get('native_semantics_verified')}",
            )
        )
        details["fuzzer"] = {
            "total": total,
            "passed": passed,
            "native_semantics_verified": bool(summary.get("native_semantics_verified")),
        }
    if profiles:
        summary = profiles.get("summary") or {}
        total = int(summary.get("negative_cases") or 0)
        passed = int(summary.get("negative_cases_detected") or 0)
        metrics.append(
            _metric(
                stage="PHASE1",
                metric_id="database_profiles.negative_detection",
                successes=passed,
                trials=total,
                threshold=0.99,
                source="phase1_cfg_database_profiles_report.json",
                denominator_policy="all negative cases across targeted and randomized database profiles",
            )
        )
        executions = int(summary.get("executions") or 0)
        errors = int(summary.get("execution_errors") or 0)
        metrics.append(
            _metric(
                stage="PHASE1",
                metric_id="database_profiles.execution_cleanliness",
                successes=max(0, executions - errors),
                trials=executions,
                threshold=0.99,
                source="phase1_cfg_database_profiles_report.json",
                denominator_policy="all profile executions; execution_errors are failures",
            )
        )
        details["profiles"] = {
            "executions": executions,
            "negative_cases": total,
            "negative_cases_detected": passed,
            "execution_errors": errors,
        }
    if large:
        summary = large.get("summary") or {}
        source = str(large.get("web_corpus") or large.get("generated_at") or "large convergence report")
        for field, threshold in (
            ("structure", 0.99),
            ("data", 0.99),
            ("mutation", 0.95),
            ("attribution", 0.99),
            ("full_flow", 0.95),
        ):
            _metric_from_summary(
                metrics,
                stage="PHASE1",
                metric_id=f"generated.large.{field}",
                summary=summary,
                field=field,
                threshold=threshold,
                source=source,
                denominator_policy="all measured cases in the large parameterized convergence batch; input/schema gaps excluded",
                exclusions=("INPUT_SCHEMA", "BACKEND_BOUNDARY"),
                notes="Large generated regression batch; it complements, but does not replace, external SQL evidence.",
            )
        total = int(summary.get("measured_cases") or 0)
        unexpected_rate = float(summary.get("unexpected_failure_rate") or 0.0)
        if total:
            unexpected = min(total, max(0, int(round(unexpected_rate * total))))
            _metric(
                stage="PHASE1",
                metric_id="generated.large.unexpected_failure_free",
                successes=total - unexpected,
                trials=total,
                threshold=0.99,
                source=source,
                denominator_policy="all measured cases in the large parameterized convergence batch",
                notes=f"reported_unexpected_failure_rate={unexpected_rate}",
            )
        details["large_generated"] = {
            "total_cases": int(summary.get("total_cases") or 0),
            "measured_cases": total,
            "parameterized_families": int(summary.get("parameterized_families") or 0),
            "unexpected_failure_rate": unexpected_rate,
            "scope_counts": summary.get("scope_counts") or {},
        }
    return details


def _add_phase2(
    metrics: list[dict[str, Any]],
    report: dict[str, Any] | None,
) -> dict[str, Any]:
    if not report:
        return {"available": False}
    totals = report.get("totals") or {}
    groups = report.get("groups") or []
    group_passed = sum(bool(group.get("passed")) for group in groups if isinstance(group, dict))
    metrics.append(
        _metric(
            stage="PHASE2",
            metric_id="acceptance.group_pass_rate",
            successes=group_passed,
            trials=len(groups),
            threshold=1.0,
            source="phase2 acceptance report",
            denominator_policy="all declared Phase 2 acceptance groups",
            statistical=False,
        )
    )
    metrics.append(
        _metric(
            stage="PHASE2",
            metric_id="acceptance.test_pass_rate",
            successes=int(totals.get("passed") or 0),
            trials=int(totals.get("executed") or 0),
            threshold=1.0,
            source="phase2 acceptance report",
            denominator_policy="all executed acceptance tests",
            statistical=False,
        )
    )
    catalog = report.get("rule_catalog") or {}
    metrics.append(
        _metric(
            stage="PHASE2",
            metric_id="rule_catalog.exact_matrix_match",
            successes=1 if catalog.get("exact_matrix_match") else 0,
            trials=1,
            threshold=1.0,
            source="phase2 acceptance report",
            denominator_policy="one versioned Phase 2 rule catalog against its matrix",
            statistical=False,
            notes=f"declared={catalog.get('declared_rule_count')}; matrix={catalog.get('matrix_covered_rule_count')}",
        )
    )
    for group in groups:
        if not isinstance(group, dict):
            continue
        counts = group.get("execution", {}).get("counts") or {}
        executed = int(counts.get("executed") or 0)
        metrics.append(
            _metric(
                stage="PHASE2",
                metric_id=f"acceptance.group.{group.get('name', 'unknown')}",
                successes=int(counts.get("passed") or 0),
                trials=executed,
                threshold=1.0,
                source="phase2 acceptance report",
                denominator_policy="all executed tests in this acceptance group",
                statistical=False,
            )
        )
    return {
        "available": True,
        "groups": len(groups),
        "executed": int(totals.get("executed") or 0),
        "passed": int(totals.get("passed") or 0),
        "rule_catalog": catalog,
    }


def _add_contract_checks(
    metrics: list[dict[str, Any]],
    *,
    report: dict[str, Any] | None,
) -> dict[str, Any]:
    if not report:
        return {"available": False}
    checks = report.get("checks") or {}
    phase3_keys = {
        "correct_with_qmatrix_updated",
        "incorrect_mutation_updated_learning",
        "no_map_skips_bkt",
        "atomic_negative_uses_frozen_taxonomy",
    }
    phase4_keys = {
        "incorrect_mutation_judged_wrong",
    }
    phase5_keys = {
        "learner_does_not_receive_internal_diagnostics",
    }
    phase6_keys = {
        "replay_same_submission",
        "safety_has_no_learning_summary",
        "syntax_has_no_skill_observation",
    }
    groups = {
        "PHASE3": phase3_keys,
        "PHASE4": phase4_keys,
        "PHASE5": phase5_keys,
        "PHASE6": phase6_keys,
    }
    for stage, keys in groups.items():
        for key in sorted(keys):
            if key not in checks:
                continue
            metrics.append(
                _metric(
                    stage=stage,
                    metric_id=f"route_contract.{key}",
                    successes=1 if checks[key] else 0,
                    trials=1,
                    threshold=1.0,
                    source="new external full-chain audit",
                    denominator_policy="one explicit route branch invariant",
                    statistical=False,
                )
            )

    counts = report.get("counts") or {}
    submissions = int(counts.get("submissions") or 0)
    audits = int(counts.get("teaching_audits") or 0)
    if submissions:
        metrics.append(
            _metric(
                stage="PHASE6",
                metric_id="persistence.teaching_audit_coverage",
                successes=min(audits, submissions),
                trials=submissions,
                threshold=1.0,
                source="new external full-chain audit",
                denominator_policy="every audited submission must have one immutable teaching audit",
                statistical=False,
            )
        )
    steps = [item for item in report.get("steps", []) if isinstance(item, dict)]
    with_hints = sum(int(item.get("hint_length") or 0) > 0 for item in steps)
    if steps:
        metrics.append(
            _metric(
                stage="PHASE5",
                metric_id="feedback.nonempty_safe_output",
                successes=with_hints,
                trials=len(steps),
                threshold=1.0,
                source="new external full-chain audit",
                denominator_policy="all non-exception route steps must return a non-empty learner message",
                statistical=False,
            )
        )
    skill_events = int(counts.get("skill_observation_events") or 0)
    metrics.append(
        _metric(
            stage="PHASE3",
            metric_id="learning.observation_event_presence",
            successes=min(skill_events, 2),
            trials=2,
            threshold=1.0,
            source="new external full-chain audit",
            denominator_policy="one correct Q-matrix event and one incorrect Phase-2-rule event",
            statistical=False,
        )
    )
    return {
        "available": True,
        "checks": checks,
        "counts": counts,
        "skill_event_sources": report.get("skill_event_sources") or {},
        "skill_event_results": report.get("skill_event_results") or {},
    }


def _stage_status(metrics: list[dict[str, Any]], stage: str) -> str:
    selected = [item for item in metrics if item.get("stage") == stage]
    if not selected:
        return "NOT_APPLICABLE"
    if any(item.get("status") == "FAIL" for item in selected):
        return "FAIL"
    if any(item.get("status") == "WARN" for item in selected):
        return "WARN"
    if all(item.get("status") == "NOT_APPLICABLE" for item in selected):
        return "NOT_APPLICABLE"
    return "PASS"


def _render_markdown(payload: dict[str, Any]) -> str:
    stages = payload.get("stages") or {}
    metrics = payload.get("metrics") or []
    lines = [
        "# SQL 教学系统能力评估",
        "",
        f"- schema: `{payload.get('schema_version')}`",
        f"- generated_at: `{payload.get('generated_at')}`",
        f"- overall_status: **{payload.get('overall_status')}**",
        "",
        "本报告只汇总已运行的审计证据；SQLite compatibility 结果不被标记为原生 MySQL/PostgreSQL 语义证明。",
        "",
        "## 阶段状态",
        "",
        "| 阶段 | 状态 | 指标数 |",
        "|---|---:|---:|",
    ]
    for stage, info in stages.items():
        lines.append(f"| {stage} | {info.get('status')} | {info.get('metric_count', 0)} |")
    lines.extend([
        "",
        "## 指标",
        "",
        "| 阶段 | 指标 | 通过/总数 | 观测率 | 95% Wilson 区间 | 门槛 | 状态 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ])
    for item in metrics:
        interval = item.get("wilson_95")
        interval_text = "-" if interval is None else f"[{interval[0]:.4f}, {interval[1]:.4f}]"
        rate = "-" if item.get("rate") is None else f"{item['rate']:.2%}"
        threshold = "-" if item.get("threshold") is None else f"{item['threshold']:.2%}"
        lines.append(
            f"| {item.get('stage')} | `{item.get('metric_id')}` | "
            f"{item.get('successes')}/{item.get('trials')} | {rate} | {interval_text} | {threshold} | {item.get('status')} |"
        )
    failures = payload.get("failure_registry") or {}
    lines.extend(["", "## 失败/边界分类", ""])
    if not failures:
        lines.append("没有提供逐案失败 JSONL；请使用 `--*-failures` 获得输入/schema、后端边界和 witness gap 的逐类计数。")
    else:
        lines.append("| 报告 | 分类 | 数量 |")
        lines.append("|---|---|---:|")
        for report_name, counts in failures.items():
            for category, count in sorted(counts.items()):
                lines.append(f"| {report_name} | `{category}` | {count} |")
    lines.extend([
        "",
        "## 证据闭合",
        "",
    ])
    completeness = payload.get("assessment_completeness") or {}
    if completeness:
        lines.append(
            f"- closed: **{completeness.get('closed')}** "
            f"（{completeness.get('present', 0)}/{completeness.get('total', 0)} 个必需证据件）"
        )
        missing = completeness.get("missing") or []
        lines.append(
            "- missing: " + (", ".join(f"`{item}`" for item in missing) if missing else "无")
        )
    lines.extend([
        "",
        "## 解释规则",
        "",
        "- `INPUT_SCHEMA`：外部 SQL/schema 本身不能在权威 catalog 中解析，不计作判题引擎正确能力。",
        "- `BACKEND_BOUNDARY`：当前执行后端明确未承诺该方言能力，不能伪装成支持。",
        "- `WITNESS_NO_COUNTEREXAMPLE`：判定器没有被测试数据区分出来，属于造数能力缺口。",
        "- `MUTATION_EVIDENCE_GAP`：结果可能已区分，但变异修复/归因证据未闭合。",
        "- 经验指标的 `WARN` 表示点估计达到门槛但样本量或 Wilson 下界不足；不是“已证明”。",
    ])
    return "\n".join(lines) + "\n"


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    identity_path = args.identity
    mutation_path = args.mutation
    identity_failures = args.identity_failures
    mutation_failures = args.mutation_failures
    phase1_report = _load_json(args.phase1_report)
    identity = _load_json(identity_path)
    mutation = _load_json(mutation_path)
    fuzzer = _load_json(args.fuzzer)
    profiles = _load_json(args.profiles)
    large = _load_json(args.large)
    phase2 = _load_json(args.phase2)
    full_chain = _load_json(args.full_chain)

    metrics: list[dict[str, Any]] = []
    assessment_completeness = _add_assessment_completeness(metrics, args=args)
    corpus_details = _add_phase0(
        metrics,
        corpus_path=args.corpus,
        identity=identity,
    )
    if phase1_report is not None:
        combined_details = _add_combined_phase1_report(
            metrics,
            report=phase1_report,
            failures_path=args.phase1_failures,
        )
        identity_details = combined_details.get("identity", {})
        mutation_details = combined_details.get("mutation", {})
    else:
        identity_details = _add_phase1_report(
            metrics,
            report=identity,
            failures_path=identity_failures,
            label="external.identity",
            identity_mode=True,
        )
        mutation_details = _add_phase1_report(
            metrics,
            report=mutation,
            failures_path=mutation_failures,
            label="external.mutation",
            identity_mode=False,
        )
    existing_phase1 = _add_existing_phase1(
        metrics,
        fuzzer=fuzzer,
        profiles=profiles,
        large=large,
    )
    phase2_details = _add_phase2(metrics, phase2)
    contract_details = _add_contract_checks(metrics, report=full_chain)

    stage_names = [
        "PHASE0_SOURCE",
        "PHASE1",
        "PHASE2",
        "PHASE3",
        "PHASE4",
        "PHASE5",
        "PHASE6",
    ]
    stages = {
        stage: {
            "status": _stage_status(metrics, stage),
            "metric_count": sum(item.get("stage") == stage for item in metrics),
        }
        for stage in stage_names
    }
    statuses = [info["status"] for info in stages.values()]
    overall = "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else (
        "PASS" if any(status == "PASS" for status in statuses) else "NOT_APPLICABLE"
    )
    failure_registry = {}
    if identity_details.get("failure_breakdown"):
        failure_registry["external.identity"] = identity_details["failure_breakdown"]
    if mutation_details.get("failure_breakdown"):
        failure_registry["external.mutation"] = mutation_details["failure_breakdown"]

    input_paths = {
        "corpus": _input_record(args.corpus),
        "phase1_report": _input_record(args.phase1_report),
        "phase1_failures": _input_record(args.phase1_failures),
        "identity": _input_record(identity_path),
        "identity_failures": _input_record(identity_failures),
        "mutation": _input_record(mutation_path),
        "mutation_failures": _input_record(mutation_failures),
        "fuzzer": _input_record(args.fuzzer),
        "profiles": _input_record(args.profiles),
        "large": _input_record(args.large),
        "phase2": _input_record(args.phase2),
        "full_chain": _input_record(args.full_chain),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "purpose": "SQL teaching chain capability assessment",
            "phase_order": stage_names,
            "bounded_empirical_evidence": True,
            "native_vendor_semantics_claimed": False,
        },
        "assessment_policy": {
            "empirical_confidence": "Wilson 95% interval; point estimate alone is not proof",
            "known_boundaries": "reported separately and never silently counted as supported",
            "input_schema_gaps": "excluded from judge coverage but retained in Phase0/failure registry",
            "witness_gaps": "counted as data-generation capability gaps, not as correct verdicts",
            "qmatrix": "Phase3 positive observations require declared/generated observable question-skill mappings",
        },
        "inputs": input_paths,
        "assessment_completeness": assessment_completeness,
        "corpus": corpus_details,
        "metrics": metrics,
        "stages": stages,
        "failure_registry": failure_registry,
        "evidence_summary": {
            "external_identity": identity_details,
            "external_mutation": mutation_details,
            "existing_phase1": existing_phase1,
            "phase2": phase2_details,
            "phase3_to_phase6_route": contract_details,
        },
        "overall_status": overall,
        "strict_exit_would_fail": overall == "FAIL",
    }


def main() -> None:
    args = parse_args()
    payload = evaluate(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown = args.markdown or args.output.with_suffix(".md")
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text(_render_markdown(payload), encoding="utf-8")
    print(json.dumps({
        "overall_status": payload["overall_status"],
        "stage_status": {key: value["status"] for key, value in payload["stages"].items()},
        "metric_count": len(payload["metrics"]),
        "output": str(args.output),
        "markdown": str(markdown),
    }, ensure_ascii=False, indent=2))
    if args.strict and payload["overall_status"] == "FAIL":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
