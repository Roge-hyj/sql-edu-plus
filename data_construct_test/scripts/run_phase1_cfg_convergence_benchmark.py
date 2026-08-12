"""Large stratified SQL CFG attack corpus with convergence measurements."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import random
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from run_e2e_robustness_fuzzer import FACTORIES, positive_equivalence_case
from run_phase1_capability_samples import _case, _json_safe, run_case
from run_phase1_cfg_fragment_benchmark import (
    build_cases as build_fragment_cases,
    classify_failure,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "data_construct_test" / "outputs"
DEFAULT_WEB_CORPUS = OUTPUT_DIR / "web_sql_corpus.jsonl"
logging.getLogger("sqlglot").setLevel(logging.ERROR)
ROW_SCALES = (4, 8, 12, 16)
KNOWN_BOUNDARY_IDS = {
    "from_lateral_correlated",
    "group_rollup",
    "group_cube",
    "set_intersect_all",
    "set_except_all",
}
FULL_EVIDENCE_FIELDS = {
    "standard_ir",
    "student_ir",
    "ast_diff_graph",
    "execution_evidence",
    "mutation_evidence",
    "attributions",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generated-cases", type=int, default=100_000)
    parser.add_argument("--web-corpus", type=Path, default=DEFAULT_WEB_CORPUS)
    parser.add_argument("--web-cases", type=int, default=50_000)
    parser.add_argument("--web-mutations-per-query", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20260722)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--early-stop-after-saturated-batches", type=int, default=0)
    return parser.parse_args()


def _pair_hash(standard: str, student: str, schema: str, rows: int) -> str:
    raw = "\0".join((schema, standard, student, str(rows)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _base_variants() -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = []
    for original in build_fragment_cases():
        for scale in ROW_SCALES:
            case = dict(original)
            case["base_id"] = original["id"]
            case["id"] = f"base__{original['id']}__rows_{scale}"
            case["max_rows_per_table"] = scale
            case["origin"] = "fragment_stratum"
            case["family"] = original["production"]
            case["row_scale"] = scale
            variants.append(case)
    return variants


def _generated_variant(
    factory: Any,
    rng: random.Random,
    index: int,
    scale: int,
) -> dict[str, Any]:
    raw = factory(rng, index)
    expectation = "equivalent" if raw.get("expect_equiv") else "not_equivalent"
    family = str(raw["operator"])
    case_id = f"fuzz__{family.lower()}__{index:06d}__rows_{scale}"
    case = _case(
        case_id,
        family,
        expectation,
        raw["schema"],
        raw["standard"],
        raw["student"],
        raw.get("expected_kps") or [],
        cfg_labels=raw.get("expected_kps") or [],
        attack_kind=raw["tactic"],
        max_rows_per_table=scale,
        note=raw.get("note") or "",
    )
    case.update({
        "production": family,
        "alternative": raw["tactic"],
        "base_id": None,
        "origin": "parameterized_fuzz",
        "family": family,
        "row_scale": scale,
    })
    return case


def _load_web_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item.get("sql"), str):
            records.append(item)
    return records


def _replace_first(pattern: str, repl: str, sql: str, flags: int = re.IGNORECASE) -> str | None:
    mutated, count = re.subn(pattern, repl, sql, count=1, flags=flags)
    if count and mutated != sql:
        return mutated
    return None


def _limit_plus_one(match: re.Match[str]) -> str:
    return f"{match.group(1)}{int(match.group(2)) + 1}"


def _web_mutations(sql: str) -> list[tuple[str, str, list[str]]]:
    candidates: list[tuple[str, str, list[str]]] = []
    mutation_specs = [
        ("distinct_removed", r"\bSELECT\s+DISTINCT\b", "SELECT", ["distinct"]),
        ("gt_to_gte", r"(?<![<>=!])>\s*(-?\d+(?:\.\d+)?)", r">= \1", ["where", "where-comp"]),
        ("gte_to_gt", r">=\s*(-?\d+(?:\.\d+)?)", r"> \1", ["where", "where-comp"]),
        ("lt_to_lte", r"(?<![<>=!])<\s*(-?\d+(?:\.\d+)?)", r"<= \1", ["where", "where-comp"]),
        ("lte_to_lt", r"<=\s*(-?\d+(?:\.\d+)?)", r"< \1", ["where", "where-comp"]),
        ("is_null_to_not_null", r"\bIS\s+NULL\b", "IS NOT NULL", ["null-handling", "where"]),
        ("is_not_null_to_null", r"\bIS\s+NOT\s+NULL\b", "IS NULL", ["null-handling", "where"]),
        ("not_in_to_in", r"\bNOT\s+IN\s*\(", "IN (", ["in-list", "where"]),
        ("in_to_not_in", r"(?<!NOT\s)\bIN\s*\(", "NOT IN (", ["in-list", "where"]),
        ("union_to_union_all", r"\bUNION\b(?!\s+ALL)", "UNION ALL", ["union"]),
        ("union_all_to_union", r"\bUNION\s+ALL\b", "UNION", ["union"]),
        ("intersect_to_union", r"\bINTERSECT\b", "UNION", ["intersect", "union"]),
        ("except_removed", r"\bEXCEPT\b.+$", "", ["except"]),
        ("order_desc_to_asc", r"\bDESC\b", "ASC", ["order-by"]),
        ("order_asc_to_desc", r"\bASC\b", "DESC", ["order-by"]),
    ]
    for name, pattern, repl, labels in mutation_specs:
        mutated = _replace_first(pattern, repl, sql)
        if mutated:
            candidates.append((name, mutated, labels))

    mutated_limit = re.sub(r"(\bLIMIT\s+)(\d+)", _limit_plus_one, sql, count=1, flags=re.IGNORECASE)
    if mutated_limit != sql:
        candidates.append(("limit_plus_one", mutated_limit, ["limit-offset"]))
    return candidates


def _web_case_id(source_id: str, index: int, suffix: str, scale: int, sql: str) -> str:
    digest = hashlib.sha256(f"{source_id}\0{index}\0{suffix}\0{scale}\0{sql}".encode("utf-8")).hexdigest()[:16]
    return f"web__{source_id}__{suffix}__{digest}__rows_{scale}"


def _web_corpus_variants(
    records: list[dict[str, Any]],
    target_cases: int,
    mutations_per_query: int,
    rng: random.Random,
) -> list[dict[str, Any]]:
    if target_cases <= 0 or not records:
        return []
    shuffled = list(records)
    rng.shuffle(shuffled)
    variants: list[dict[str, Any]] = []
    for index, record in enumerate(shuffled):
        sql = str(record["sql"]).strip().rstrip(";")
        if not re.match(r"(?is)^\s*(select|with)\b", sql):
            continue
        schema = str(record.get("schema") or "")
        labels = list(record.get("cfg_labels") or ["select-basic"])
        source_id = str(record.get("source_id") or "unknown")
        scale = ROW_SCALES[index % len(ROW_SCALES)]
        identity = _case(
            _web_case_id(source_id, index, "identity", scale, sql),
            "WEB_CORPUS",
            "equivalent",
            schema,
            sql,
            sql,
            [],
            cfg_labels=labels,
            attack_kind="web_identity_control",
            max_rows_per_table=scale,
            note=f"External corpus identity guard from {source_id}",
        )
        identity.update({
            "production": "WebCorpus",
            "alternative": "identity",
            "base_id": record.get("id"),
            "origin": "web_corpus_identity",
            "family": "WEB_CORPUS_IDENTITY",
            "row_scale": scale,
            "source_id": source_id,
            "source_kind": record.get("source_kind"),
        })
        variants.append(identity)
        if len(variants) >= target_cases:
            break

        mutations = _web_mutations(sql)
        rng.shuffle(mutations)
        for mutation_name, mutated_sql, mutation_labels in mutations[:max(0, mutations_per_query)]:
            case = _case(
                _web_case_id(source_id, index, mutation_name, scale, mutated_sql),
                "WEB_CORPUS",
                "not_equivalent",
                schema,
                sql,
                mutated_sql,
                mutation_labels,
                cfg_labels=sorted(set(labels) | set(mutation_labels)),
                attack_kind=f"web_mutation:{mutation_name}",
                max_rows_per_table=scale,
                note=f"External corpus semantic mutation from {source_id}",
            )
            case.update({
                "production": "WebCorpus",
                "alternative": mutation_name,
                "base_id": record.get("id"),
                "origin": "web_corpus_mutation",
                "family": f"WEB_CORPUS_{mutation_name.upper()}",
                "row_scale": scale,
                "source_id": source_id,
                "source_kind": record.get("source_kind"),
            })
            variants.append(case)
            if len(variants) >= target_cases:
                break
        if len(variants) >= target_cases:
            break
    return variants


def build_corpus(
    generated_cases: int,
    seed: int,
    web_corpus: Path = DEFAULT_WEB_CORPUS,
    web_cases: int = 0,
    web_mutations_per_query: int = 2,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    corpus = _base_variants()
    factories = [*FACTORIES, positive_equivalence_case]
    order = list(range(generated_cases))
    rng.shuffle(order)
    for sequence, random_index in enumerate(order):
        factory = factories[sequence % len(factories)]
        scale = ROW_SCALES[(sequence // len(factories)) % len(ROW_SCALES)]
        corpus.append(_generated_variant(factory, rng, random_index, scale))
    corpus.extend(_web_corpus_variants(
        _load_web_records(web_corpus),
        web_cases,
        web_mutations_per_query,
        rng,
    ))
    rng.shuffle(corpus)
    return corpus


def _failure_signature(result: dict[str, Any]) -> str | None:
    if result["capability_bucket"] == "supported":
        return None
    error = re.sub(r"\b\d+\b", "#", str(result.get("error") or "none").lower())
    parts = (
        str(result.get("failure_class") or "unknown"),
        str(result.get("production") or "unknown"),
        str(result.get("alternative") or "unknown"),
        error,
    )
    return "|".join(parts)


def _wilson_interval(failures: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 1.0)
    probability = failures / total
    denominator = 1 + z * z / total
    center = (probability + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(
        probability * (1 - probability) / total + z * z / (4 * total * total)
    ) / denominator
    return (max(0.0, center - margin), min(1.0, center + margin))


def _classify_scope(result: dict[str, Any]) -> str:
    if result["capability_bucket"] == "supported":
        return "supported"
    if result.get("base_id") in KNOWN_BOUNDARY_IDS:
        return "known_boundary"
    return "unexpected_failure"


def _convergence(results: list[dict[str, Any]], batch_size: int) -> list[dict[str, Any]]:
    seen_all: set[str] = set()
    seen_unexpected: set[str] = set()
    batches: list[dict[str, Any]] = []
    for start in range(0, len(results), batch_size):
        chunk = results[start:start + batch_size]
        all_signatures = {
            signature for item in chunk
            if (signature := item.get("failure_signature"))
        }
        unexpected_signatures = {
            item["failure_signature"] for item in chunk
            if item["scope_status"] == "unexpected_failure" and item.get("failure_signature")
        }
        new_all = all_signatures - seen_all
        new_unexpected = unexpected_signatures - seen_unexpected
        seen_all.update(all_signatures)
        seen_unexpected.update(unexpected_signatures)
        batches.append({
            "batch": len(batches) + 1,
            "start": start,
            "end": start + len(chunk),
            "cases": len(chunk),
            "supported": sum(item["scope_status"] == "supported" for item in chunk),
            "known_boundaries": sum(item["scope_status"] == "known_boundary" for item in chunk),
            "unexpected_failures": sum(item["scope_status"] == "unexpected_failure" for item in chunk),
            "new_failure_signatures": sorted(new_all),
            "new_unexpected_signatures": sorted(new_unexpected),
            "cumulative_failure_signatures": len(seen_all),
            "cumulative_unexpected_signatures": len(seen_unexpected),
        })
    return batches


def summarize(
    results: list[dict[str, Any]],
    corpus: list[dict[str, Any]],
    batch_size: int,
) -> dict[str, Any]:
    scope_counts = Counter(item["scope_status"] for item in results)
    expectation_counts = Counter(item["expectation"] for item in results)
    family_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for item in results:
        family_counts[item["family"]][item["scope_status"]] += 1

    measured = [item for item in results if item["scope_status"] != "known_boundary"]
    unexpected = [item for item in measured if item["scope_status"] == "unexpected_failure"]
    lower, upper = _wilson_interval(len(unexpected), len(measured))
    negative = [item for item in measured if item["expectation"] == "not_equivalent"]
    equivalent = [item for item in measured if item["expectation"] == "equivalent"]
    syntax = [item for item in measured if item["expectation"] == "syntax_rejected"]
    convergence = _convergence(results, batch_size)
    trailing_saturated = 0
    for batch in reversed(convergence):
        if batch["new_unexpected_signatures"]:
            break
        trailing_saturated += 1

    base_cases = [item for item in corpus if item["origin"] == "fragment_stratum"]
    base_alternatives = {
        (item["production"], item["alternative"], item["base_id"])
        for item in base_cases
    }
    origin_counts = Counter(item.get("origin") or "unknown" for item in corpus)
    web_source_counts = Counter(
        str(item.get("source_id") or "unknown")
        for item in corpus
        if str(item.get("origin") or "").startswith("web_corpus")
    )
    return {
        "total_cases": len(results),
        "scope_counts": dict(scope_counts),
        "expectation_counts": dict(expectation_counts),
        "corpus_origin_counts": dict(origin_counts),
        "web_source_counts": dict(web_source_counts),
        "unexpected_failure_rate": round(len(unexpected) / len(measured), 8) if measured else 0,
        "unexpected_failure_wilson_95": [round(lower, 8), round(upper, 8)],
        "counterexample_detection_rate": round(
            sum(item["data_stage_met"] for item in negative) / len(negative), 8
        ) if negative else 1.0,
        "equivalence_preservation_rate": round(
            sum(item["data_stage_met"] for item in equivalent) / len(equivalent), 8
        ) if equivalent else 1.0,
        "syntax_rejection_rate": round(
            sum(item["data_stage_met"] for item in syntax) / len(syntax), 8
        ) if syntax else 1.0,
        "attribution_hit_rate": round(
            sum(item["attribution_stage_met"] for item in negative) / len(negative), 8
        ) if negative else 1.0,
        "mutation_executed_rate": round(
            sum(bool(item.get("mutation_summary", {}).get("executed")) for item in negative) / len(negative), 8
        ) if negative else 1.0,
        "unique_sql_database_pairs": len({
            _pair_hash(item["standard"], item["student"], item["schema"], item["row_scale"])
            for item in corpus
        }),
        "fragment_productions_covered": len({item["production"] for item in base_cases}),
        "fragment_alternatives_covered": len(base_alternatives),
        "fragment_row_scales": list(ROW_SCALES),
        "parameterized_families": len({item["family"] for item in corpus if item["origin"] == "parameterized_fuzz"}),
        "family_counts": {
            family: dict(counts) for family, counts in sorted(family_counts.items())
        },
        "failure_signatures": sorted({
            item["failure_signature"] for item in results if item.get("failure_signature")
        }),
        "unexpected_failure_signatures": sorted({
            item["failure_signature"] for item in unexpected if item.get("failure_signature")
        }),
        "convergence_batches": convergence,
        "trailing_batches_without_new_unexpected_signature": trailing_saturated,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 1 SQL CFG Convergence Benchmark",
        "",
        f"Generated at: `{payload['generated_at']}`",
        f"Seed: `{payload['seed']}`",
        "",
        "This is a stratified empirical bound, not a formal proof over arbitrary SQL and databases.",
        "",
        "## Summary",
        "",
        f"- Total attacks: `{summary['total_cases']}`",
        f"- Scope outcomes: `{summary['scope_counts']}`",
        f"- Corpus origins: `{summary['corpus_origin_counts']}`",
        f"- Web source counts: `{summary['web_source_counts']}`",
        f"- Unique SQL/database-scale pairs: `{summary['unique_sql_database_pairs']}`",
        f"- Fragment productions: `{summary['fragment_productions_covered']}`",
        f"- Fragment alternatives: `{summary['fragment_alternatives_covered']}`",
        f"- Parameterized families: `{summary['parameterized_families']}`",
        f"- Counterexample detection: `{summary['counterexample_detection_rate']:.4%}`",
        f"- Equivalence preservation: `{summary['equivalence_preservation_rate']:.4%}`",
        f"- Attribution hit rate: `{summary['attribution_hit_rate']:.4%}`",
        f"- Unexpected failure rate: `{summary['unexpected_failure_rate']:.4%}`",
        f"- Unexpected failure 95% Wilson interval: `{summary['unexpected_failure_wilson_95']}`",
        f"- Trailing saturated batches: `{summary['trailing_batches_without_new_unexpected_signature']}`",
        "",
        "## Convergence",
        "",
        "| batch | cases | supported | known boundary | unexpected | new failure signatures | new unexpected signatures |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for batch in summary["convergence_batches"]:
        lines.append(
            f"| {batch['batch']} | {batch['cases']} | {batch['supported']} | "
            f"{batch['known_boundaries']} | {batch['unexpected_failures']} | "
            f"{len(batch['new_failure_signatures'])} | {len(batch['new_unexpected_signatures'])} |"
        )
    lines.extend(["", "## Failure Signatures", ""])
    if not summary["failure_signatures"]:
        lines.append("No failure signature was observed.")
    else:
        lines.extend(f"- `{signature}`" for signature in summary["failure_signatures"])
    return "\n".join(lines) + "\n"


def _write_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(_json_safe(item), ensure_ascii=False) for item in items) + ("\n" if items else ""),
        encoding="utf-8",
    )


def _compact_result(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key not in FULL_EVIDENCE_FIELDS}


def _unique_evidence(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in results:
        pair_id = _pair_hash(
            item["standard"], item["student"], item["schema"], item["row_scale"]
        )
        if pair_id in seen:
            continue
        seen.add(pair_id)
        unique.append({"pair_id": pair_id, **item})
    return unique


def main() -> None:
    args = parse_args()
    if args.generated_cases < 0 or args.batch_size <= 0:
        raise SystemExit("generated-cases must be >= 0 and batch-size must be > 0")
    if args.web_cases < 0 or args.web_mutations_per_query < 0:
        raise SystemExit("web-cases and web-mutations-per-query must be >= 0")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    corpus = build_corpus(
        args.generated_cases,
        args.seed,
        args.web_corpus,
        args.web_cases,
        args.web_mutations_per_query,
    )
    results: list[dict[str, Any]] = []
    checkpoint_path = OUTPUT_DIR / "phase1_cfg_convergence_checkpoint.json"
    seen_unexpected_signatures: set[str] = set()
    saturated_batches = 0

    for index, case in enumerate(corpus, start=1):
        result = run_case(case)
        result["failure_class"] = classify_failure(result)
        result["scope_status"] = _classify_scope(result)
        result["failure_signature"] = _failure_signature(result)
        results.append(result)
        if index % args.batch_size == 0 or index == len(corpus):
            batch_unexpected = {
                item["failure_signature"] for item in results[-args.batch_size:]
                if item.get("scope_status") == "unexpected_failure" and item.get("failure_signature")
            }
            new_unexpected = batch_unexpected - seen_unexpected_signatures
            seen_unexpected_signatures.update(batch_unexpected)
            if new_unexpected:
                saturated_batches = 0
            else:
                saturated_batches += 1
            checkpoint_path.write_text(
                json.dumps({
                    "seed": args.seed,
                    "processed": index,
                    "total": len(corpus),
                    "scope_counts": dict(Counter(item["scope_status"] for item in results)),
                    "saturated_batches_without_new_unexpected_signature": saturated_batches,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"processed {index}/{len(corpus)}")
            if (
                args.early_stop_after_saturated_batches > 0
                and saturated_batches >= args.early_stop_after_saturated_batches
            ):
                print(
                    "early stop: "
                    f"{saturated_batches} consecutive batches without new unexpected signatures"
                )
                break

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed,
        "generated_cases": args.generated_cases,
        "web_corpus": str(args.web_corpus),
        "requested_web_cases": args.web_cases,
        "web_mutations_per_query": args.web_mutations_per_query,
        "fragment_base_cases": sum(item.get("origin") == "fragment_stratum" for item in corpus),
        "actual_web_cases": sum(str(item.get("origin") or "").startswith("web_corpus") for item in corpus),
        "batch_size": args.batch_size,
        "summary": summarize(results, corpus[:len(results)], args.batch_size),
    }
    report_path = OUTPUT_DIR / "phase1_cfg_convergence_report.json"
    markdown_path = OUTPUT_DIR / "phase1_cfg_convergence_report.md"
    all_path = OUTPUT_DIR / "phase1_cfg_convergence_all.jsonl"
    passed_path = OUTPUT_DIR / "phase1_cfg_convergence_supported.jsonl"
    failures_path = OUTPUT_DIR / "phase1_cfg_convergence_failures.jsonl"
    evidence_path = OUTPUT_DIR / "phase1_cfg_convergence_detailed_evidence.jsonl"
    report_path.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown(payload), encoding="utf-8")
    compact_results = [_compact_result(item) for item in results]
    _write_jsonl(all_path, compact_results)
    _write_jsonl(passed_path, [item for item in compact_results if item["scope_status"] == "supported"])
    _write_jsonl(failures_path, [item for item in results if item["scope_status"] != "supported"])
    _write_jsonl(evidence_path, _unique_evidence(results))
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
