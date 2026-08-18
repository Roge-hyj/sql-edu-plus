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

from sqlglot import exp

from run_e2e_robustness_fuzzer import FACTORIES, positive_equivalence_case
from run_phase1_capability_samples import _case, _json_safe, run_case
from run_phase1_cfg_fragment_benchmark import (
    build_cases as build_fragment_cases,
    classify_failure,
)
from core.parseval_data_generator import parse_schema_text
from core.sql_dialect_resolver import (
    DialectResolutionSource,
    parse_single_query,
    resolve_sql_dialect_or_raise,
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
    parser.add_argument(
        "--skip-fragment-stratum",
        action="store_true",
        help="run only the supplied/generated corpus and omit the built-in fragment stratum",
    )
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
    with path.open(encoding="utf-8") as stream:
      for line in stream:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and isinstance(item.get("sql"), str):
            records.append(item)
    return records


WEB_REQUIRED_LABELS = (
    "where", "distinct", "null-handling", "order-by", "limit-offset", "group-by",
    "having", "agg-count", "case", "join-inner", "join-left", "join-on",
    "subquery-scalar", "subquery-exists", "cte", "cte-recursive", "union",
    "intersect", "except", "window-agg",
)


def _stratified_web_order(
    records: list[dict[str, Any]],
    rng: random.Random,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Order web records by source and rare teaching labels before generation.

    The old implementation shuffled a capped prefix of the corpus.  Because
    WikiSQL appeared first and was much larger, that made a nominally online
    run effectively a WikiSQL-only benchmark.  This orderer consumes the full
    corpus, reserves one example for every available source and required label,
    then round-robins the remaining records by source.  The caller still stops
    at the requested variant budget, so every batch gets reproducible coverage
    without exploding memory or test duration.
    """

    valid: list[dict[str, Any]] = []
    seen_sql: set[str] = set()
    for record in records:
        sql = str(record.get("sql") or "").strip().rstrip(";")
        if not re.match(r"(?is)^\s*(select|with)\b", sql):
            continue
        key = hashlib.sha256(sql.lower().encode("utf-8")).hexdigest()
        if key in seen_sql:
            continue
        seen_sql.add(key)
        valid.append(record)

    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in valid:
        source = str(record.get("source_id") or "unknown")
        by_source[source].append(record)
        for label in record.get("cfg_labels") or []:
            by_label[str(label)].append(record)
    for bucket in (*by_source.values(), *by_label.values()):
        rng.shuffle(bucket)

    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()

    def add(record: dict[str, Any]) -> None:
        key = hashlib.sha256(str(record.get("sql") or "").strip().lower().encode("utf-8")).hexdigest()
        if key not in selected_keys:
            selected_keys.add(key)
            selected.append(record)

    # Hard source coverage: all source IDs that have at least one usable query
    # must appear in each sufficiently large web batch.
    for source in sorted(by_source):
        add(by_source[source][0])
    # Rare/teaching-feature coverage comes next, so small specialist files are
    # not drowned out by the large Spider/WikiSQL populations.
    for label in WEB_REQUIRED_LABELS:
        if by_label.get(label):
            add(by_label[label][0])

    # Round-robin by source gives a bounded, transparent share to every source;
    # once a small source is exhausted, its slot naturally disappears.
    source_order = sorted(by_source)
    positions = {source: 0 for source in source_order}
    while True:
        progressed = False
        for source in source_order:
            bucket = by_source[source]
            position = positions[source]
            while position < len(bucket):
                candidate = bucket[position]
                position += 1
                if hashlib.sha256(str(candidate.get("sql") or "").strip().lower().encode("utf-8")).hexdigest() not in selected_keys:
                    add(candidate)
                    progressed = True
                    break
            positions[source] = position
        if not progressed:
            break
    return selected, {
        "input_records": len(records),
        "usable_records": len(valid),
        "source_counts": {source: len(items) for source, items in sorted(by_source.items())},
        "reserved_source_count": len(by_source),
        "reserved_label_counts": {
            label: 1 for label in WEB_REQUIRED_LABELS if by_label.get(label)
        },
    }


def _parsed_web_query(sql: str) -> tuple[exp.Query, str | None] | None:
    try:
        resolution = resolve_sql_dialect_or_raise(
            declared_dialect=None,
            standard_sql=sql,
            student_sql=sql,
            default_dialect="mysql",
        )
    except Exception:
        return None
    if not resolution.asts:
        return None
    root = resolution.asts[0]
    parse_dialect = resolution.parse_dialect
    if (
        resolution.source == DialectResolutionSource.DEFAULT
        and resolution.resolved_dialect
    ):
        try:
            root = parse_single_query(sql, dialect=resolution.resolved_dialect)
            parse_dialect = resolution.resolved_dialect
        except Exception:
            pass
    # Text-to-SQL corpora often omit quotes around schema identifiers such as
    # ``1st_place``. The resolver recovers these as identifiers, but they must
    # stay quoted when the AST is rendered and parsed again by later stages.
    for identifier in root.find_all(exp.Identifier):
        name = str(identifier.this or "")
        if name and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
            identifier.set("quoted", True)
    return root, parse_dialect


def _render_mutation(root: exp.Query, dialect: str | None) -> str:
    return root.sql(dialect=dialect)


def _has_ancestor(node: exp.Expression, kind: type[exp.Expression]) -> bool:
    current = node.parent
    while current is not None:
        if isinstance(current, kind):
            return True
        current = current.parent
    return False


def _ancestors(node: exp.Expression) -> list[exp.Expression]:
    ancestors: list[exp.Expression] = []
    current = node.parent
    while current is not None:
        ancestors.append(current)
        current = current.parent
    return ancestors


def _comparison_labels(node: exp.Expression) -> list[str]:
    current = node.parent
    while current is not None:
        if isinstance(current, exp.Join):
            return ["join-on"]
        if isinstance(current, exp.Having):
            return ["having"]
        if isinstance(current, exp.Where):
            return ["where", "where-comp"]
        if isinstance(current, (exp.Case, exp.If)):
            return ["case"]
        current = current.parent
    return ["where", "where-comp"]


def _predicate_context_labels(
    node: exp.Expression,
    primary: str,
) -> list[str]:
    current = node.parent
    while current is not None:
        if isinstance(current, exp.Join):
            return ["join-on", primary]
        if isinstance(current, exp.Having):
            return ["having", primary]
        if isinstance(current, (exp.Case, exp.If)):
            return ["case", primary]
        if isinstance(current, exp.Where):
            return ["where", primary]
        current = current.parent
    return [primary]


def _literal_set_values(node: exp.Expression) -> set[str] | None:
    if isinstance(node, exp.Select) and len(node.expressions) == 1:
        expression = node.expressions[0]
        if isinstance(expression, exp.Alias):
            expression = expression.this
        if isinstance(expression, exp.Literal):
            return {expression.sql()}
        return None
    if isinstance(node, exp.Union):
        left = _literal_set_values(node.this)
        right = _literal_set_values(node.expression)
        if left is not None and right is not None:
            return left | right
    return None


def _projection_changes_source_shape(
    select: exp.Select,
    schema_text: str,
) -> bool:
    if select.args.get("group") or any(
        aggregate.find_ancestor(exp.Select) is select
        for aggregate in select.find_all(exp.AggFunc)
    ):
        return False
    with_clause = select.args.get("with_")
    cte_names = {
        str(cte.alias).lower()
        for cte in (with_clause.expressions if isinstance(with_clause, exp.With) else [])
        if cte.alias
    }
    from_clause = select.args.get("from_")
    joins = select.args.get("joins") or []
    if from_clause is None or joins or not isinstance(from_clause.this, exp.Table):
        return False
    if from_clause.this.name.lower() in cte_names:
        return False
    schema = parse_schema_text(schema_text)
    table_name = from_clause.this.name.lower()
    source_columns = [column.lower() for column in schema.get(table_name, [])]
    if not source_columns:
        return False
    projected_columns: list[str] = []
    for expression in select.expressions:
        if not isinstance(expression, exp.Column):
            return True
        projected_columns.append(expression.name.lower())
    return (
        len(projected_columns) != len(source_columns)
        or set(projected_columns) != set(source_columns)
    )


def _quote_unsafe_schema_identifiers(sql: str, schema_text: str) -> str:
    """Quote schema-owned identifiers that generic corpus SQL left bare."""
    reserved = {
        "all", "alter", "and", "as", "asc", "between", "by", "case",
        "check", "column", "create", "delete", "desc", "distinct", "drop",
        "else", "end", "except", "exists", "for", "from", "full", "group",
        "having", "in", "inner", "insert", "intersect", "into", "is", "join",
        "left", "like", "limit", "not", "null", "offset", "on", "or", "order",
        "outer", "primary", "references", "right", "select", "set", "table",
        "then", "union", "unique", "update", "values", "when", "where", "with",
    }
    unsafe = {
        identifier
        for table, columns in parse_schema_text(schema_text).items()
        for identifier in (table, *columns)
        if (
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", identifier)
            or identifier.lower() in reserved
        )
    }
    if not unsafe:
        return sql
    pattern = re.compile(
        r"(?<![A-Za-z0-9_$`\"\[])"
        + "(" + "|".join(re.escape(value) for value in sorted(unsafe, key=len, reverse=True)) + ")"
        + r"(?![A-Za-z0-9_$`\"\]])"
    )
    parts = re.split(r"('(?:''|[^'])*')", sql)
    for index in range(0, len(parts), 2):
        parts[index] = pattern.sub(lambda match: f"`{match.group(1)}`", parts[index])
    return "".join(parts)


def _web_mutations(
    sql: str,
    schema_text: str = "",
) -> list[tuple[str, str, list[str]]]:
    candidates: list[tuple[str, str, list[str]]] = []
    sql = _quote_unsafe_schema_identifiers(sql, schema_text)
    parsed = _parsed_web_query(sql)
    if parsed is None:
        return candidates
    original, dialect = parsed

    def add(name: str, root: exp.Query, labels: list[str]) -> None:
        try:
            mutated = _render_mutation(root, dialect)
        except Exception:
            # Some tutorial files contain dialect-specific functions that
            # sqlglot can parse but cannot render back to the declared dialect.
            # Keep the identity control and move on; a single bad mutation must
            # not abort an entire source-stratified batch.
            return
        if mutated != sql:
            candidates.append((name, mutated, labels))

    root = original.copy()
    distinct_select = next(
        (
            node
            for node in root.walk()
            if isinstance(node, exp.Select) and node.args.get("distinct")
        ),
        None,
    )
    distinct_is_redundant = bool(
        distinct_select is not None
        and (
            _has_ancestor(distinct_select, exp.In)
            or any(
                isinstance(ancestor, (exp.Union, exp.Intersect, exp.Except))
                and ancestor.args.get("distinct") is not False
                for ancestor in _ancestors(distinct_select)
            )
            or "@" in sql
        )
    )
    if distinct_select is not None and not distinct_is_redundant:
        distinct_select.set("distinct", None)
        add("distinct_removed", root, ["distinct"])

    comparisons = (
        (exp.GT, exp.GTE, "gt_to_gte"),
        (exp.GTE, exp.GT, "gte_to_gt"),
        (exp.LT, exp.LTE, "lt_to_lte"),
        (exp.LTE, exp.LT, "lte_to_lt"),
    )
    for source_type, target_type, name in comparisons:
        root = original.copy()
        node = next((item for item in root.walk() if isinstance(item, source_type)), None)
        if node is not None:
            if any(isinstance(ancestor, (exp.Any, exp.All)) for ancestor in _ancestors(node)):
                continue
            labels = _comparison_labels(node)
            node.replace(
                target_type(this=node.this.copy(), expression=node.expression.copy())
            )
            add(name, root, labels)

    root = original.copy()
    is_null = next(
        (
            node
            for node in root.walk()
            if isinstance(node, exp.Is)
            and isinstance(node.expression, exp.Null)
            and not isinstance(node.parent, exp.Not)
        ),
        None,
    )
    if is_null is not None:
        labels = _predicate_context_labels(is_null, "null-handling")
        is_null.replace(exp.Not(this=is_null.copy()))
        add("is_null_to_not_null", root, labels)

    root = original.copy()
    not_null = next(
        (
            node
            for node in root.walk()
            if isinstance(node, exp.Not)
            and isinstance(node.this, exp.Is)
            and isinstance(node.this.expression, exp.Null)
        ),
        None,
    )
    if not_null is not None:
        labels = _predicate_context_labels(not_null, "null-handling")
        not_null.replace(not_null.this.copy())
        add("is_not_null_to_null", root, labels)

    root = original.copy()
    not_in = next(
        (
            node
            for node in root.walk()
            if isinstance(node, exp.Not) and isinstance(node.this, exp.In)
        ),
        None,
    )
    if not_in is not None:
        labels = _predicate_context_labels(not_in, "in-list")
        not_in.replace(not_in.this.copy())
        add("not_in_to_in", root, labels)

    root = original.copy()
    in_predicate = next(
        (
            node
            for node in root.walk()
            if isinstance(node, exp.In) and not isinstance(node.parent, exp.Not)
        ),
        None,
    )
    if in_predicate is not None:
        labels = _predicate_context_labels(in_predicate, "in-list")
        in_predicate.replace(exp.Not(this=in_predicate.copy()))
        add("in_to_not_in", root, labels)

    root = original.copy()
    union = next((node for node in root.walk() if isinstance(node, exp.Union)), None)
    union_is_redundant = bool(
        union is not None
        and (
            _has_ancestor(union, exp.In)
            or (
                (left_values := _literal_set_values(union.this)) is not None
                and (right_values := _literal_set_values(union.expression)) is not None
                and left_values.isdisjoint(right_values)
            )
        )
    )
    if union is not None and not union_is_redundant:
        if union.args.get("distinct") is False:
            union.set("distinct", True)
            add("union_all_to_union", root, ["union"])
        else:
            union.set("distinct", False)
            add("union_to_union_all", root, ["union"])

    root = original.copy()
    intersect = next(
        (node for node in root.walk() if isinstance(node, exp.Intersect)),
        None,
    )
    if intersect is not None:
        replacement = exp.Union(
            this=intersect.this.copy(),
            expression=intersect.expression.copy(),
            distinct=True,
        )
        if intersect.parent is None:
            root = replacement
        else:
            intersect.replace(replacement)
        add("intersect_to_union", root, ["intersect", "union"])

    root = original.copy()
    except_node = next(
        (node for node in root.walk() if isinstance(node, exp.Except)),
        None,
    )
    if except_node is not None:
        replacement = except_node.this.copy()
        if except_node.parent is None:
            root = replacement
        else:
            except_node.replace(replacement)
        add("except_removed", root, ["except"])

    root = original.copy()
    ordered = next((node for node in root.walk() if isinstance(node, exp.Ordered)), None)
    if ordered is not None and ordered.args.get("desc") is True:
        ordered.set("desc", False)
        add("order_desc_to_asc", root, ["order-by"])
    elif ordered is not None and ordered.args.get("desc") is False:
        ordered.set("desc", True)
        add("order_asc_to_desc", root, ["order-by"])

    root = original.copy()
    limit = root.args.get("limit") if isinstance(root, exp.Select) else None
    if limit is not None and isinstance(limit.expression, exp.Literal) and limit.expression.is_int:
        limit.set("expression", exp.Literal.number(int(limit.expression.this) + 1))
        add("limit_plus_one", root, ["limit"])

    root = original.copy()
    projection = root if isinstance(root, exp.Select) else None
    if (
        projection is not None
        and projection.expressions
        and not all(isinstance(expression, exp.Star) for expression in projection.expressions)
        and _projection_changes_source_shape(projection, schema_text)
    ):
        projection.set("expressions", [exp.Star()])
        add("projection_to_star", root, ["select-basic"])
    return candidates


def _web_row_scale(sql: str, base_scale: int) -> int:
    """Raise bounded fixtures only when a small cardinality boundary needs it."""

    scale = base_scale
    if re.search(r"(?is)\bcount\s*\(", sql) and re.search(
        r"(?is)\b(?:having|group\s+by)\b", sql
    ):
        boundaries = [
            int(value)
            for value in re.findall(r"(?<![\w.])\d+(?![\w.])", sql)
            if 0 <= int(value) <= 32
        ]
        if boundaries:
            scale = max(scale, max(boundaries) + 1)
    for pattern in (r"(?is)\blimit\s+(\d+)", r"(?is)\btop\s+(?:\(\s*)?(\d+)"):
        matches = [int(value) for value in re.findall(pattern, sql) if int(value) <= 32]
        if matches:
            scale = max(scale, max(matches) + 1)
    return scale


def _web_case_id(source_id: str, index: int, suffix: str, scale: int, sql: str) -> str:
    digest = hashlib.sha256(f"{source_id}\0{index}\0{suffix}\0{scale}\0{sql}".encode("utf-8")).hexdigest()[:16]
    return f"web__{source_id}__{suffix}__{digest}__rows_{scale}"


def _web_sql_dialect(record: dict[str, Any]) -> str | None:
    raw = str(record.get("dialect") or "generic").strip().lower()
    aliases = {
        "ansi": "standard",
        "ansi_sql": "standard",
        "postgresql": "postgres",
        "sqlserver": "tsql",
        "sql_server": "tsql",
        "mssql": "tsql",
    }
    normalized = aliases.get(raw, raw)
    return normalized if normalized in {
        "standard", "mysql", "postgres", "tsql", "oracle", "sqlite"
    } else None


def _web_corpus_variants(
    records: list[dict[str, Any]],
    target_cases: int,
    mutations_per_query: int,
    rng: random.Random,
) -> list[dict[str, Any]]:
    if target_cases <= 0 or not records:
        return []
    ordered, _selection_stats = _stratified_web_order(records, rng)
    required_sources = {
        str(record.get("source_id") or "unknown")
        for record in ordered
        if str(record.get("source_id") or "unknown")
    }
    emitted_sources: set[str] = set()
    variants: list[dict[str, Any]] = []
    for index, record in enumerate(ordered):
        sql = str(record["sql"]).strip().rstrip(";")
        if not re.match(r"(?is)^\s*(select|with)\b", sql):
            continue
        schema = str(record.get("schema") or "")
        sql = _quote_unsafe_schema_identifiers(sql, schema)
        parsed = _parsed_web_query(sql)
        if parsed is not None:
            try:
                sql = _render_mutation(*parsed)
            except Exception:
                # Preserve the source text for identity testing when rendering
                # is unavailable; mutation generation below remains guarded.
                pass
        labels = list(record.get("cfg_labels") or ["select-basic"])
        source_id = str(record.get("source_id") or "unknown")
        source_dialect = _web_sql_dialect(record)
        scale = _web_row_scale(sql, ROW_SCALES[index % len(ROW_SCALES)])
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
            sql_dialect=source_dialect,
            schema_catalog=record.get("schema_catalog"),
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
            "source_sql_dialect": source_dialect,
        })
        variants.append(identity)
        emitted_sources.add(source_id)
        if len(variants) >= target_cases:
            if required_sources.issubset(emitted_sources):
                break

        mutations = _web_mutations(sql, schema)
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
                sql_dialect=source_dialect,
                schema_catalog=record.get("schema_catalog"),
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
                "source_sql_dialect": source_dialect,
            })
            variants.append(case)
            emitted_sources.add(source_id)
            if len(variants) >= target_cases and required_sources.issubset(emitted_sources):
                break
        if len(variants) >= target_cases and required_sources.issubset(emitted_sources):
            break
    missing_sources = sorted(required_sources - emitted_sources)
    if missing_sources:
        raise RuntimeError(
            "web stratification could not emit at least one parsed case for sources: "
            + ", ".join(missing_sources)
        )
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
    web_label_counts = Counter(
        label
        for item in corpus
        if str(item.get("origin") or "").startswith("web_corpus")
        for label in item.get("cfg_labels") or []
    )

    def stage_count(field: str) -> dict[str, Any]:
        passed = sum(bool(item.get(field)) for item in measured)
        total = len(measured)
        return {
            "passed": passed,
            "failed": total - passed,
            "total": total,
            "rate": round(passed / total, 8) if total else 1.0,
        }

    return {
        "total_cases": len(results),
        "scope_counts": dict(scope_counts),
        "expectation_counts": dict(expectation_counts),
        "corpus_origin_counts": dict(origin_counts),
        "web_source_counts": dict(web_source_counts),
        "web_sources_covered": sorted(web_source_counts),
        "web_label_counts": dict(web_label_counts),
        "web_required_labels_missing": sorted(
            label for label in WEB_REQUIRED_LABELS if not web_label_counts.get(label)
        ),
        "stage_counts": {
            "structure": stage_count("structure_stage_met"),
            "data": stage_count("data_stage_met"),
            "mutation": stage_count("mutation_stage_met"),
            "attribution": stage_count("attribution_stage_met"),
            "full_flow": stage_count("expectation_met"),
        },
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
        f"- Web labels: `{summary['web_label_counts']}`",
        f"- Missing required web labels: `{summary['web_required_labels_missing']}`",
        f"- Unique SQL/database-scale pairs: `{summary['unique_sql_database_pairs']}`",
        f"- Fragment productions: `{summary['fragment_productions_covered']}`",
        f"- Fragment alternatives: `{summary['fragment_alternatives_covered']}`",
        f"- Parameterized families: `{summary['parameterized_families']}`",
        f"- Counterexample detection: `{summary['counterexample_detection_rate']:.4%}`",
        f"- Equivalence preservation: `{summary['equivalence_preservation_rate']:.4%}`",
        f"- Mutation execution: `{summary['mutation_executed_rate']:.4%}`",
        f"- Attribution hit rate: `{summary['attribution_hit_rate']:.4%}`",
        f"- Unexpected failure rate: `{summary['unexpected_failure_rate']:.4%}`",
        f"- Unexpected failure 95% Wilson interval: `{summary['unexpected_failure_wilson_95']}`",
        f"- Trailing saturated batches: `{summary['trailing_batches_without_new_unexpected_signature']}`",
        "",
        "## Phase 1 Stage Counts",
        "",
        "| stage | passed | failed | total | rate |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for stage, counts in summary["stage_counts"].items():
        lines.append(
            f"| {stage} | {counts['passed']} | {counts['failed']} | "
            f"{counts['total']} | {counts['rate']:.4%} |"
        )
    lines.extend([
        "",
        "## Convergence",
        "",
        "| batch | cases | supported | known boundary | unexpected | new failure signatures | new unexpected signatures |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
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
    if args.skip_fragment_stratum:
        corpus = [item for item in corpus if item.get("origin") != "fragment_stratum"]
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
        "skip_fragment_stratum": args.skip_fragment_stratum,
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
