"""Select a bounded, mutation-rich slice of the external SQL corpus.

The public corpus contains a few recursive or vendor-specific statements that
are useful for manual review but can make a batch gate unbounded. This selector
keeps executable teaching diversity while requiring a reproducible mutation
budget for the Phase 1 structure/data/mutation/full-flow gate.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import re
import sys
from typing import Any

from sqlglot import exp


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = PROJECT_ROOT / "data_construct_test" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from run_phase1_capability_samples import _case, run_case  # noqa: E402
from run_data_generation_boundary_tests import infer_schema  # noqa: E402
from core.parseval_data_generator import parse_schema_text  # noqa: E402


def _benchmark_helpers():
    # Delayed to avoid the benchmark importing this selector back at module
    # load time.  Both tools can therefore share normalization safely.
    from run_phase1_cfg_convergence_benchmark import (
        _parsed_web_query,
        _quote_unsafe_schema_identifiers,
        _web_mutations,
        _web_sql_dialect,
    )
    return _parsed_web_query, _quote_unsafe_schema_identifiers, _web_mutations, _web_sql_dialect


def _web_mutations(*args, **kwargs):
    return _benchmark_helpers()[2](*args, **kwargs)


def _web_sql_dialect(*args, **kwargs):
    return _benchmark_helpers()[3](*args, **kwargs)


def _quote_unsafe_schema_identifiers(*args, **kwargs):
    return _benchmark_helpers()[1](*args, **kwargs)


def _parsed_web_query(*args, **kwargs):
    return _benchmark_helpers()[0](*args, **kwargs)


UNBOUNDED_OR_RANDOM = re.compile(
    r"(?is)\b(?:with\s+recursive|connect\s+by|tablesample|sample\b|"
    r"cycle\b|search\s+(?:depth|breadth))"
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        type=Path,
        default=PROJECT_ROOT / "data_construct_test/outputs/web_sql_corpus_phase1_20260814.jsonl",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "data_construct_test/outputs/web_sql_corpus_phase1_gate.jsonl",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=PROJECT_ROOT / "data_construct_test/outputs/web_sql_corpus_phase1_gate_report.json",
    )
    parser.add_argument("--max-sql-length", type=int, default=800)
    parser.add_argument("--max-records", type=int, default=700)
    parser.add_argument("--minimum-records", type=int, default=500)
    parser.add_argument("--minimum-mutations", type=int, default=500)
    parser.add_argument(
        "--coverage-per-source",
        type=int,
        default=0,
        help="prioritize up to this many preflight-approved records per source",
    )
    parser.add_argument(
        "--coverage-per-label",
        type=int,
        default=0,
        help="prioritize up to this many preflight-approved records per CFG label",
    )
    parser.add_argument("--seed", type=int, default=20260814)
    parser.add_argument(
        "--single-table-only",
        action="store_true",
        help="keep one bounded top-level FROM table and no nested SELECT/JOIN",
    )
    parser.add_argument(
        "--preflight-identities",
        action="store_true",
        help="require each standard SQL identity control to pass the full Phase 1 chain",
    )
    return parser.parse_args()


def _load(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and isinstance(item.get("sql"), str):
            records.append(item)
    return records


def _normalize_generic_schema(item: dict[str, Any]) -> dict[str, Any]:
    """Replace token-scraped schemas with query-reference schemas.

    Generic text corpora do not ship a database catalog.  The collector's
    standard-library fallback intentionally over-approximates identifiers, so
    function names and derived aliases such as ``LAG``, ``row_num`` or CTE
    names can otherwise become physical columns and change query semantics.
    """

    if (
        str(item.get("extraction_method") or "") != "generic_recursive"
        and str(item.get("source_kind") or "") != "local_external_seed"
    ):
        return item
    sql = str(item.get("sql") or "").strip()
    _parsed_web_query, _, _, _ = _benchmark_helpers()
    inferred = infer_schema(sql)
    if not inferred:
        return item

    inferred_tables = parse_schema_text(inferred)
    original_tables = parse_schema_text(str(item.get("schema") or ""))
    parsed = _parsed_web_query(sql)
    if len(inferred_tables) == 1 and parsed:
        root, _ = parsed
        derived_aliases = {
            str(node.alias).lower()
            for node in root.find_all(exp.Alias)
            if node.alias
        }
        relation_aliases = {
            str(node.alias).lower()
            for node in root.find_all(exp.Table, exp.Subquery, exp.CTE)
            if node.alias
        }
        function_names = {
            str(node.sql_name()).lower()
            for node in root.find_all(exp.Func)
            if node.sql_name()
        }
        function_names |= {name.replace("_", "") for name in function_names}
        noise = derived_aliases | relation_aliases | function_names | {
            "asc", "desc", "day", "month", "year", "decimal",
        }
        physical_relations = {
            str(node.name or "").lower()
            for node in root.find_all(exp.Table)
            if node.name
        }
        qualified_physical_refs = relation_aliases | physical_relations
        ast_columns = set()
        for node in root.find_all(exp.Column):
            column_name = str(node.name or "").lower()
            table_name = str(node.table or "").lower()
            if not column_name:
                continue
            if table_name in qualified_physical_refs or column_name not in noise:
                ast_columns.add(column_name)
        inferred_name = next(iter(inferred_tables))
        original_columns = next(
            (
                columns
                for table_name, columns in original_tables.items()
                if table_name.lower() == inferred_name.lower()
            ),
            next(iter(original_tables.values()), []),
        )
        inferred_columns = list(inferred_tables[inferred_name])
        merged = [
            column
            for column in inferred_columns
            if not ast_columns or column.lower() in ast_columns
        ]
        merged_names = {column.lower() for column in merged}
        sql_tokens = {token.lower() for token in re.findall(r"\b[A-Za-z_]\w*\b", sql)}
        for column in original_columns:
            normalized = column.lower()
            if (
                normalized in sql_tokens
                and normalized not in noise
                and normalized not in merged_names
                and (not ast_columns or normalized in ast_columns)
            ):
                merged.append(column)
                merged_names.add(normalized)
        inferred = f"{inferred_name}({', '.join(merged)})"

    # Generic extraction frequently copies every referenced identifier into
    # every table.  That makes SQLite reject otherwise valid multi-table SQL
    # with "ambiguous column name" before data generation can run.  Keep
    # explicitly qualified references on their target table; for an
    # unqualified duplicate, retain one deterministic owner so the generated
    # fixture remains executable.
    normalized_tables = parse_schema_text(inferred)
    if len(normalized_tables) > 1 and parsed:
        qualified_by_column: dict[str, set[str]] = {}
        relation_aliases: dict[str, str] = {}
        for table_node in parsed[0].find_all(exp.Table):
            table_name = str(table_node.name or "").lower()
            alias = str(table_node.alias or "").lower()
            if table_name and alias:
                relation_aliases[alias] = table_name
        for node in parsed[0].find_all(exp.Column):
            column = str(node.name or "").lower()
            relation = str(node.table or "").lower()
            if column and relation:
                qualified_by_column.setdefault(column, set()).add(relation)
        table_names = list(normalized_tables)
        owners: dict[str, str] = {}
        for table in table_names:
            for column in normalized_tables[table]:
                key = column.lower()
                if key not in owners:
                    owners[key] = table
        for column, relations in qualified_by_column.items():
            matching = [
                table for table in table_names
                if table.lower() in relations
                or any(
                    relation_aliases.get(relation) == table.lower()
                    for relation in relations
                )
            ]
            if matching:
                owners[column] = matching[0]
        for table in table_names:
            normalized_tables[table] = [
                column for column in normalized_tables[table]
                if owners.get(column.lower()) == table
            ]
        inferred = "; ".join(
            f"{table}({', '.join(columns)})"
            for table, columns in normalized_tables.items()
        ) + ";"

    return {
        **item,
        "schema": inferred,
        "schema_normalization": "ast_query_references_v1",
        "source_schema": item.get("schema"),
    }


def _pair_key(item: dict[str, Any]) -> str:
    return hashlib.sha256(
        "\0".join(
            (
                str(item.get("source_id") or ""),
                str(item.get("sql") or ""),
                str(item.get("schema") or ""),
            )
        ).encode("utf-8")
    ).hexdigest()


def _identity_passes(item: dict[str, Any]) -> bool:
    _, _, _, _web_sql_dialect = _benchmark_helpers()
    sql = str(item.get("sql") or "").strip().rstrip(";")
    case = _case(
        f"preflight__{str(item.get('id') or _pair_key(item))}",
        "WEB_CORPUS_PREFLIGHT",
        "equivalent",
        str(item.get("schema") or ""),
        sql,
        sql,
        [],
        cfg_labels=list(item.get("cfg_labels") or ["select-basic"]),
        attack_kind="web_identity_preflight",
        max_rows_per_table=8,
        note="bounded external corpus identity preflight",
        sql_dialect=_web_sql_dialect(item),
        schema_catalog=item.get("schema_catalog"),
    )
    return bool(run_case(case).get("expectation_met"))


def select_records(
    records: list[dict[str, Any]],
    *,
    max_sql_length: int,
    max_records: int,
    minimum_records: int,
    minimum_mutations: int,
    seed: int,
    single_table_only: bool = False,
    preflight_identities: bool = False,
    coverage_per_source: int = 0,
    coverage_per_label: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    _parsed_web_query, _quote_unsafe_schema_identifiers, _web_mutations, _web_sql_dialect = _benchmark_helpers()
    eligible: list[tuple[dict[str, Any], int]] = []
    excluded = Counter()
    seen: set[str] = set()
    for raw_item in records:
        item = _normalize_generic_schema(raw_item)
        sql = str(item.get("sql") or "").strip()
        if not re.match(r"(?is)^(?:select|with)\b", sql):
            excluded["non_query"] += 1
            continue
        if len(sql) > max_sql_length:
            excluded["too_long"] += 1
            continue
        if UNBOUNDED_OR_RANDOM.search(sql):
            excluded["unbounded_or_random"] += 1
            continue
        if single_table_only and (
            len(re.findall(r"(?is)\bselect\b", sql)) != 1
            or len(re.findall(r"(?is)\bfrom\b", sql)) != 1
            or re.search(r"(?is)\bjoin\b", sql)
        ):
            excluded["not_single_table"] += 1
            continue
        key = _pair_key(item)
        if key in seen:
            excluded["duplicate"] += 1
            continue
        seen.add(key)
        mutation_count = len(_web_mutations(sql, str(item.get("schema") or "")))
        if mutation_count == 0:
            excluded["no_mutation_candidate"] += 1
            continue
        eligible.append((item, mutation_count))

    rng = random.Random(seed)
    rng.shuffle(eligible)
    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    selected_sources = Counter()
    selected_labels = Counter()
    preflight_results: dict[str, bool] = {}
    preflight_failed_sources = Counter()
    preflight_failed_labels = Counter()
    mutation_budget = 0

    def attempt(entry: tuple[dict[str, Any], int]) -> bool:
        nonlocal mutation_budget
        item, mutation_count = entry
        if len(selected) >= max_records:
            return False
        key = _pair_key(item)
        if key in selected_keys:
            return False
        if preflight_identities:
            if key not in preflight_results:
                preflight_results[key] = _identity_passes(item)
                if not preflight_results[key]:
                    excluded["identity_preflight_failed"] += 1
                    source = str(item.get("source_id") or "unknown")
                    preflight_failed_sources[source] += 1
                    preflight_failed_labels.update(item.get("cfg_labels") or [])
            if not preflight_results[key]:
                return False
        selected.append(item)
        selected_keys.add(key)
        mutation_budget += mutation_count
        selected_sources[str(item.get("source_id") or "unknown")] += 1
        selected_labels.update(item.get("cfg_labels") or [])
        return True

    eligible_sources = Counter(
        str(item.get("source_id") or "unknown") for item, _ in eligible
    )
    eligible_labels = Counter(
        label for item, _ in eligible for label in item.get("cfg_labels") or []
    )

    if coverage_per_source > 0:
        by_source: dict[str, list[tuple[dict[str, Any], int]]] = {}
        for entry in eligible:
            source = str(entry[0].get("source_id") or "unknown")
            by_source.setdefault(source, []).append(entry)
        for source in sorted(by_source, key=lambda value: (len(by_source[value]), value)):
            target = min(coverage_per_source, len(by_source[source]))
            for entry in by_source[source]:
                if selected_sources[source] >= target or len(selected) >= max_records:
                    break
                attempt(entry)

    if coverage_per_label > 0:
        by_label: dict[str, list[tuple[dict[str, Any], int]]] = {}
        for entry in eligible:
            for label in entry[0].get("cfg_labels") or []:
                by_label.setdefault(str(label), []).append(entry)
        for label in sorted(by_label, key=lambda value: (len(by_label[value]), value)):
            target = min(coverage_per_label, len(by_label[label]))
            for entry in by_label[label]:
                if selected_labels[label] >= target or len(selected) >= max_records:
                    break
                attempt(entry)

    for entry in eligible:
        if len(selected) >= max_records:
            break
        if mutation_budget >= minimum_mutations and len(selected) >= minimum_records:
            break
        attempt(entry)

    if len(selected) < minimum_records:
        raise RuntimeError(
            f"selected record count {len(selected)} is below required "
            f"minimum {minimum_records}"
        )
    if mutation_budget < minimum_mutations:
        raise RuntimeError(
            f"selected mutation budget {mutation_budget} is below required "
            f"minimum {minimum_mutations}"
        )

    report = {
        "input_records": len(records),
        "eligible_records": len(eligible),
        "selected_records": len(selected),
        "minimum_records": minimum_records,
        "candidate_mutations": mutation_budget,
        "minimum_mutations": minimum_mutations,
        "coverage_per_source": coverage_per_source,
        "coverage_per_label": coverage_per_label,
        "max_sql_length": max_sql_length,
        "seed": seed,
        "single_table_only": single_table_only,
        "preflight_identities": preflight_identities,
        "eligible_source_counts": dict(eligible_sources),
        "eligible_label_counts": dict(eligible_labels),
        "source_counts": dict(selected_sources),
        "label_counts": dict(selected_labels),
        "source_coverage_shortfalls": {
            source: min(coverage_per_source, count) - selected_sources[source]
            for source, count in eligible_sources.items()
            if coverage_per_source > 0
            and selected_sources[source] < min(coverage_per_source, count)
        },
        "label_coverage_shortfalls": {
            label: min(coverage_per_label, count) - selected_labels[label]
            for label, count in eligible_labels.items()
            if coverage_per_label > 0
            and selected_labels[label] < min(coverage_per_label, count)
        },
        "preflight_failed_source_counts": dict(preflight_failed_sources),
        "preflight_failed_label_counts": dict(preflight_failed_labels),
        "excluded_counts": dict(excluded),
    }
    return selected, report


def main() -> int:
    args = _args()
    if (
        args.max_sql_length <= 0
        or args.minimum_records < 500
        or args.max_records < args.minimum_records
        or args.minimum_mutations < 500
        or args.coverage_per_source < 0
        or args.coverage_per_label < 0
    ):
        raise SystemExit(
            "max-sql-length must be positive; minimum-records and "
            "minimum-mutations must be >= 500; max-records must be >= "
            "minimum-records; coverage targets must be >= 0"
        )
    selected, report = select_records(
        _load(args.input),
        max_sql_length=args.max_sql_length,
        max_records=args.max_records,
        minimum_records=args.minimum_records,
        minimum_mutations=args.minimum_mutations,
        seed=args.seed,
        single_table_only=args.single_table_only,
        preflight_identities=args.preflight_identities,
        coverage_per_source=args.coverage_per_source,
        coverage_per_label=args.coverage_per_label,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in selected) + "\n",
        encoding="utf-8",
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
