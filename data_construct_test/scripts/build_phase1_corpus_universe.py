"""Build a reproducible, family-stratified Phase 1 SQL corpus snapshot.

The input side accepts several historical JSON/JSONL corpus formats, then
admits only rows that the real Phase 1 mutation parser can parse and re-render.
The output side is strict: every retained record carries provenance, schema
trust, dialect, teaching categories, scenario axes, a stable family id, and a
deterministic partition.  Rows outside the implemented replay boundary are
counted as invalid input rather than deferred into a hidden generation job.

The builder uses a disk-backed SQLite index.  It therefore keeps memory
bounded while deduplicating large JSONL corpora and guarantees that the chosen
representative for a duplicate family does not depend on input file order.
Hidden records are written to a separate file and are never included in the
public report body; only counts and digests are exposed there.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterable, Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUTS = (
    PROJECT_ROOT / "data_construct_test/outputs/web_sql_corpus_phase1_20260815.jsonl",
    PROJECT_ROOT / "data_construct_test/outputs/phase1_cfg_supported_samples.jsonl",
)
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data_construct_test/outputs/phase1_corpus_universe"
DEFAULT_SOURCE_MANIFEST = PROJECT_ROOT / "data_construct_test/sources/web_sql_corpus_manifest.json"
SCHEMA_VERSION = 2
MAX_INPUT_LINE_BYTES = 16 * 1024 * 1024

_MUTATION_ADMISSION_MODULE: Any | None = None

CATEGORIES = (
    "select_projection",
    "where_logic_null",
    "in_between_like",
    "join_outer_on",
    "group_having_aggregate",
    "distinct_order_limit",
    "set_operations",
    "subqueries_correlation",
    "cte_recursive",
    "case",
    "window_functions",
    "dialect_features",
)

LABEL_TO_CATEGORY = {
    "select-basic": "select_projection",
    "alias": "select_projection",
    "where": "where_logic_null",
    "where-comp": "where_logic_null",
    "null-handling": "where_logic_null",
    "in-list": "in_between_like",
    "between": "in_between_like",
    "like": "in_between_like",
    "join-inner": "join_outer_on",
    "join-left": "join_outer_on",
    "join-right-full": "join_outer_on",
    "join-on": "join_outer_on",
    "complex-join": "join_outer_on",
    "group-by": "group_having_aggregate",
    "having": "group_having_aggregate",
    "agg-count": "group_having_aggregate",
    "aggregate": "group_having_aggregate",
    "distinct": "distinct_order_limit",
    "order-by": "distinct_order_limit",
    "limit-offset": "distinct_order_limit",
    "union": "set_operations",
    "intersect": "set_operations",
    "except": "set_operations",
    "subquery-in": "subqueries_correlation",
    "subquery-exists": "subqueries_correlation",
    "subquery-scalar": "subqueries_correlation",
    "subquery-correlated": "subqueries_correlation",
    "cte": "cte_recursive",
    "cte-recursive": "cte_recursive",
    "case": "case",
    "window-row-number": "window_functions",
    "window-agg": "window_functions",
    "window-rank": "window_functions",
}


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _normalize_sql(sql: str) -> str:
    """Normalize comments, literals and whitespace for family grouping."""
    text = re.sub(r"--[^\r\n]*", " ", sql)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"'(?:''|[^'])*'", "'<literal>'", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\b", "<number>", text)
    return _normalize_space(text).rstrip(";").lower()


def _mutation_admission_module() -> Any:
    """Load the authoritative mutation parser/render boundary once.

    Corpus admission and mutation generation must not silently drift into two
    different notions of "replayable SQL".  Loading the existing bounded
    builder here keeps the admission check aligned with the implementation
    that will consume the snapshot, while the lazy import avoids making the
    historical metadata-only helpers pay the import cost unless a snapshot is
    actually built.
    """
    global _MUTATION_ADMISSION_MODULE
    if _MUTATION_ADMISSION_MODULE is not None:
        return _MUTATION_ADMISSION_MODULE
    module_path = PROJECT_ROOT / "data_construct_test/scripts/build_phase1_mutation_layer.py"
    module_name = "_phase1_mutation_layer_for_corpus_admission"
    loaded = sys.modules.get(module_name)
    if loaded is None:
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load mutation admission module: {module_path}")
        loaded = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = loaded
        spec.loader.exec_module(loaded)
    _MUTATION_ADMISSION_MODULE = loaded
    return loaded


def _mutation_admission_ready(sql: str, dialect: str, schema: Any) -> bool:
    """Require the real mutation layer to parse and re-render this record.

    This is intentionally a generation-boundary check, not a broad SQL
    standards validator.  Vendor syntax and schema-aware identifier repairs
    are delegated to the same parser/generator used later; rows outside that
    implemented boundary are counted as invalid input instead of becoming
    hidden generation failures.
    """
    module = _mutation_admission_module()
    schema_text = str(schema or "")
    read_dialect = module._dialect_of({"dialect": dialect})
    tree = module._parse(sql, read_dialect, schema_text)
    return tree is not None and module._render(tree, read_dialect, schema_text) is not None


def _looks_like_embedded_record_payload(sql: str) -> bool:
    """Reject the known online-miner JSON-fragment corruption shape.

    A miner occasionally concatenated a UI label array and the next JSON
    object into the SQL field.  The marker is deliberately narrow: a normal
    SQL string literal containing JSON remains admissible.
    """
    text = sql.lstrip().lower()
    statement_prefixes = tuple(
        keyword + chr(34)
        for keyword in ("select", "with", "insert", "update", "delete", "merge")
    )
    if not text.startswith(statement_prefixes):
        return False
    return bool(re.search(r"\{\s*\"id\"\s*:", sql, flags=re.IGNORECASE))


def _normalize_schema(schema: Any, schema_catalog: Any) -> str:
    if isinstance(schema_catalog, dict):
        value = json.dumps(schema_catalog, sort_keys=True, separators=(",", ":"))
    elif isinstance(schema, (dict, list)):
        value = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    else:
        value = str(schema or "")
    value = re.sub(r"\s+", " ", value.strip())
    return value.lower()


def _explicit_lineage_id(item: dict[str, Any]) -> str:
    """Return an upstream family key when the source provides one.

    An explicit lineage key is authoritative for generated/curated sources:
    it lets those sources distinguish semantic question families from replay
    parameters such as table-name salts, row seeds, or threshold values.
    """
    return _first_text(
        item,
        "lineage_family_id",
        "question_family_id",
        "semantic_family_id",
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _candidate_priority(item: dict[str, Any]) -> int:
    """Prefer validated evidence while keeping family representative selection deterministic."""
    if item.get("gold_oracle") or item.get("observed_scenario_axes"):
        return 0
    if item.get("execution_evidence") or item.get("mutation_evidence"):
        return 1
    if _first_text(item, "student", "student_sql"):
        return 2
    if item.get("schema_catalog") or item.get("replay_eligible"):
        return 3
    return 4


def _read_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if len(raw) > MAX_INPUT_LINE_BYTES:
                continue
            try:
                item = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(item, dict):
                yield line_number, item


def _iter_input_records(paths: Iterable[Path]) -> Iterator[tuple[Path, int, dict[str, Any]]]:
    for path in paths:
        if path.is_dir():
            candidates = sorted(path.rglob("*.jsonl"))
        else:
            candidates = [path]
        for candidate in candidates:
            if not candidate.exists() or candidate.suffix.lower() not in {".jsonl", ".json"}:
                continue
            if candidate.suffix.lower() == ".json":
                try:
                    payload = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                values = payload if isinstance(payload, list) else payload.get("records", []) if isinstance(payload, dict) else []
                if isinstance(values, list):
                    for index, item in enumerate(values, start=1):
                        if isinstance(item, dict):
                            yield candidate, index, item
                continue
            for line_number, item in _read_jsonl(candidate):
                yield candidate, line_number, item


def _first_text(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _provenance_fields(
    item: dict[str, Any],
    source_info: dict[str, Any],
    captured_at: str,
) -> tuple[str, str, str | None, str]:
    """Resolve URL/date while preserving whether they came from the row or manifest.

    A source-level URL is useful for reproducibility, but it is not the same
    evidence as a row-level URL.  Keep that distinction explicit so reports do
    not turn a partially documented derived snapshot into complete lineage.
    """
    record_url = _first_text(item, "source_url", "url")
    manifest_url = str(source_info.get("url") or "").strip()
    if record_url:
        source_url, url_status = record_url, "record_declared"
    elif manifest_url:
        source_url, url_status = manifest_url, "source_manifest"
    else:
        source_url, url_status = "", "missing_upstream_record"

    record_capture = _first_text(
        item,
        "source_capture_at",
        "source_captured_at",
        "source_retrieved_at",
    )
    manifest_capture = str(source_info.get("captured_at") or "").strip()
    if record_capture:
        source_capture_at, capture_status = record_capture, "record_declared"
    elif manifest_capture:
        source_capture_at, capture_status = manifest_capture, "source_manifest"
    else:
        # A row may have been recovered from a local immutable snapshot whose
        # upstream HTTP response date was not retained.  The snapshot date is
        # still a reproducible capture boundary, but it must remain distinct
        # from an upstream record timestamp.
        source_capture_at, capture_status = captured_at, "snapshot_capture"
    return source_url, url_status, source_capture_at, capture_status


_DIALECT_ALIASES = {
    "postgresql": "postgres",
    "postgresql+psycopg": "postgres",
    "sqlserver": "tsql",
    "mssql": "tsql",
    "sqlite3": "sqlite",
}


def _normalize_dialect(value: Any) -> str:
    text = str(value or "generic").strip().lower()
    return _DIALECT_ALIASES.get(text, text or "generic")


def _load_source_dialects(path: Path | None) -> dict[str, str]:
    """Load authoritative source dialect defaults."""
    return {
        source_id: str(metadata.get("dialect") or "")
        for source_id, metadata in _load_source_metadata(path).items()
        if str(metadata.get("dialect") or "")
    }


def _load_source_metadata(path: Path | None) -> dict[str, dict[str, Any]]:
    """Load small, auditable source descriptors without retaining raw records."""
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    sources = payload.get("sources", []) if isinstance(payload, dict) else []
    if not isinstance(sources, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("id") or "").strip()
        if not source_id:
            continue
        result[source_id] = {
            "id": source_id,
            "name": source.get("name"),
            "kind": source.get("kind"),
            "url": source.get("url"),
            "local_path": source.get("local_path"),
            "format": source.get("format"),
            "dialect": _normalize_dialect(source.get("dialect")),
            "license_note": source.get("license_note"),
            "redistribution_allowed": source.get("redistribution_allowed"),
            "reference_only": bool(source.get("reference_only")),
            "extraction": source.get("extraction"),
            "captured_at": source.get("captured_at") or source.get("retrieved_at") or source.get("snapshot_date"),
        }
    return result


def _infer_vendor_dialect(sql: str) -> str | None:
    """Infer only syntax that is strongly associated with one vendor."""
    text = sql.lower()
    if re.search(r"\btop\s*(?:\(\s*\d+\s*\)|\d+)", text) or re.search(
        r"\b(?:outer\s+)?apply\b|\bgetdate\s*\(|\bdatediff\s*\(", text
    ) or re.search(r"\[[^\]]+\]", sql):
        return "tsql"
    if re.search(r"`[^`]+`", sql) or re.search(
        r"\b(?:ifnull|group_concat|date_format|straight_join)\s*\(", text
    ) or re.search(r"\bon\s+duplicate\s+key\s+update\b", text):
        return "mysql"
    if re.search(r"::\s*[a-z_][a-z0-9_]*\b", text) or re.search(
        r"\b(?:ilike|distinct\s+on|generate_series)\b", text
    ) or re.search(r"\breturning\b", text):
        return "postgres"
    if re.search(r"\bconnect\s+by\b|\bstart\s+with\b", text) or re.search(
        r"\b(?:rownum|nvl|listagg)\s*(?:\(|\b)", text
    ) or re.search(r"\bfrom\s+dual\b", text):
        return "oracle"
    return None


def _resolve_dialect(
    item: dict[str, Any],
    sql: str,
    source_dialects: dict[str, str],
) -> tuple[str, str]:
    declared = _normalize_dialect(item.get("dialect") or item.get("declared_sql_dialect"))
    source_id = _first_text(item, "source_id", "source", "dataset")
    if declared != "generic":
        return declared, "record_declared"
    source_dialect = source_dialects.get(source_id)
    if source_dialect:
        return source_dialect, "source_manifest"
    inferred = _infer_vendor_dialect(sql)
    if inferred:
        return inferred, "syntax_inferred"
    return "generic", "generic_fallback"


def _labels(item: dict[str, Any], sql: str) -> set[str]:
    values: set[str] = set()
    for key in ("cfg_labels", "labels", "knowledge_points", "ir_features"):
        value = item.get(key)
        if isinstance(value, list):
            values.update(str(entry).strip().lower() for entry in value if str(entry).strip())
    text = f" {sql.lower()} "
    checks = {
        "distinct": "distinct",
        "join": "join",
        "left join": "join-left",
        "right join": "join-right-full",
        "full join": "join-right-full",
        "group by": "group-by",
        "having": "having",
        "order by": "order-by",
        "limit": "limit-offset",
        "offset": "limit-offset",
        "union": "union",
        "intersect": "intersect",
        "except": "except",
        "over": "window-agg",
        "row_number": "window-row-number",
        "rank(": "window-rank",
        "dense_rank": "window-rank",
        "with ": "cte",
        "with recursive": "cte-recursive",
        "is null": "null-handling",
        "not in": "null-handling",
        "between": "between",
        " like ": "like",
        " case ": "case",
        " exists ": "subquery-exists",
    }
    for needle, label in checks.items():
        if needle in text:
            values.add(label)
    if re.search(r"\(\s*select\b", text):
        values.add("subquery-scalar")
    if " where " in text:
        values.add("where")
    if re.search(r"[<>=]", text):
        values.add("where-comp")
    if re.search(r"\b(count|sum|avg|min|max)\s*\(", text):
        values.add("agg-count")
    return values


def _categories(item: dict[str, Any], sql: str, dialect: str) -> list[str]:
    labels = _labels(item, sql)
    categories = {LABEL_TO_CATEGORY[label] for label in labels if label in LABEL_TO_CATEGORY}
    # Curated/synthesized rows may carry an explicit category that is more
    # specific than their compact CFG labels (notably dialect_features).
    supplied = item.get("categories")
    if isinstance(supplied, (list, tuple, set)):
        categories.update(
            str(value).strip()
            for value in supplied
            if str(value).strip() in CATEGORIES
        )
    if dialect != "generic":
        categories.add("dialect_features")
    if not categories:
        categories.add("select_projection")
    return [category for category in CATEGORIES if category in categories]


SCENARIO_CANDIDATES = (
    "mutation_ready",
    "null",
    "empty_result",
    "duplicate_candidate",
    "multi_table",
    "boundary_candidate",
    "schema_constraint",
    "paired_mutation",
    "dialect_feature",
)


def _explicit_scenario_axes(item: dict[str, Any]) -> set[str]:
    """Accept only axes explicitly backed by an upstream execution stage.

    ``scenario_axes`` is a legacy/template field and is intentionally treated
    as a candidate below.  This prevents a generator from claiming that its
    SQL text has already exercised NULL, empty-result, or duplicate worlds.
    """
    values: set[str] = set()
    for key in ("verified_scenario_axes", "observed_scenario_axes"):
        raw = item.get(key)
        if isinstance(raw, (list, tuple, set)):
            values.update(str(value).strip() for value in raw if str(value).strip())
    return values


def _explicit_scenario_candidates(item: dict[str, Any]) -> set[str]:
    """Collect structural/template candidates without calling them observed."""
    values: set[str] = set()
    for key in ("scenario_candidates", "scenario_axes"):
        raw = item.get(key)
        if isinstance(raw, (list, tuple, set)):
            values.update(str(value).strip() for value in raw if str(value).strip())
    return values


def _execution_observed(item: dict[str, Any]) -> bool:
    if item.get("executed") is True:
        return True
    evidence = item.get("execution_evidence")
    if isinstance(evidence, dict) and evidence.get("sandbox_executed") is True:
        return True
    oracle = item.get("gold_oracle")
    return isinstance(oracle, dict) and oracle.get("executed") is True


def _database_contains_null(database: Any) -> bool:
    if not isinstance(database, dict):
        return False
    for rows in database.values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and any(value is None for value in row.values()):
                return True
    return False


def _result_has_duplicate(rows: Any) -> bool:
    if not isinstance(rows, list):
        return False
    seen: set[str] = set()
    for row in rows:
        try:
            key = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            continue
        if key in seen:
            return True
        seen.add(key)
    return False


def _has_authoritative_constraint(item: dict[str, Any], schema_catalog: Any) -> bool:
    if isinstance(schema_catalog, dict):
        for table in schema_catalog.get("tables") or ():
            if not isinstance(table, dict):
                continue
            if table.get("primary_key") or table.get("foreign_keys") or table.get("unique_constraints"):
                return True
    trust = str(item.get("schema_trust") or "").strip().lower()
    return trust in {"authoritative_source_catalog", "source_structured", "source_declared"} and bool(
        re.search(r"(?i)\b(?:primary\s+key|foreign\s+key|unique|not\s+null)\b", str(item.get("schema") or ""))
    )


def _scenario_axes(
    item: dict[str, Any],
    sql: str,
    schema_catalog: Any,
    dialect: str,
) -> tuple[list[str], list[str]]:
    """Return observed axes and structural candidates separately.

    SQL text can establish that an attack is plausible, but cannot establish
    that a NULL, empty result, duplicate projection, or constraint was
    actually exercised.  Those axes require explicit upstream annotation or
    bounded execution evidence.
    """
    text = sql.lower()
    explicit = _explicit_scenario_axes(item)
    paired = bool(_first_text(item, "student", "student_sql")) and bool(
        _first_text(item, "sql", "standard", "query", "question_sql")
    )
    executed = _execution_observed(item)
    evidence = item.get("execution_evidence") if isinstance(item.get("execution_evidence"), dict) else {}
    boundary_evidence = item.get("boundary_evidence") or evidence.get("boundary_evidence")
    database = item.get("test_database")
    standard_rows = item.get("standard_rows")
    student_rows = item.get("student_rows")
    standard_count = item.get("standard_row_count")
    student_count = item.get("student_row_count")
    candidates = {"base"}
    candidates.update(
        axis
        for axis in _explicit_scenario_candidates(item)
        if axis in set(SCENARIO_CANDIDATES) | {"base"}
    )
    if " null" in text or "is null" in text or "not null" in text or "not in" in text:
        candidates.add("null")
    if executed and (standard_count == 0 or student_count == 0):
        candidates.add("empty_result")
    if re.search(r"\b(join|from)\b.*\b(join|from)\b", text, re.DOTALL):
        candidates.add("multi_table")
    if "group by" in text or re.search(r"\b(count|sum|avg|min|max)\s*\(", text):
        candidates.add("duplicate_candidate")
    if re.search(r"\b(limit|offset|having|between|[<>]=?)\b", text):
        candidates.add("boundary_candidate")
    if schema_catalog or re.search(r"(?i)\b(?:primary\s+key|foreign\s+key|unique|not\s+null)\b", str(item.get("schema") or "")):
        candidates.add("schema_constraint")
    if paired:
        candidates.add("paired_mutation")
        if item.get("mutation_evidence") or item.get("generation_tactics") or item.get("mutation_validation"):
            candidates.add("mutation_ready")
    if dialect != "generic":
        candidates.add("dialect_feature")

    observed = {"base"}
    observed.update(axis for axis in explicit if axis in set(SCENARIO_CANDIDATES) | {"base", "mutation_ready"})
    if "multi_table" in candidates:
        observed.add("multi_table")
    if dialect != "generic":
        observed.add("dialect_feature")
    if executed and _database_contains_null(database):
        observed.add("null")
    if executed and (standard_count == 0 or student_count == 0):
        observed.add("empty_result")
    if executed and (
        _result_has_duplicate(standard_rows)
        or _result_has_duplicate(student_rows)
        or int(evidence.get("standard_duplicate_row_count") or 0) > 0
        or int(evidence.get("student_duplicate_row_count") or 0) > 0
    ):
        observed.add("duplicate_candidate")
    if boundary_evidence:
        observed.add("boundary_candidate")
    if _has_authoritative_constraint(item, schema_catalog):
        observed.add("schema_constraint")
    if paired:
        observed.add("paired_mutation")
    if paired and (item.get("mutation_evidence") or item.get("generation_tactics") or item.get("mutation_validation")):
        observed.add("mutation_ready")
    return sorted(observed), sorted(candidates)


def _partition(family_id: str, seed: int, train_ratio: float, public_ratio: float) -> str:
    digest = hashlib.sha256(f"{seed}:{family_id}".encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") / float(1 << 64)
    if bucket < train_ratio:
        return "train"
    if bucket < train_ratio + public_ratio:
        return "public"
    return "hidden"


def _canonical_record(
    item: dict[str, Any],
    *,
    input_path: Path,
    line_number: int,
    captured_at: str,
    seed: int,
    train_ratio: float,
    public_ratio: float,
    source_dialects: dict[str, str],
    source_metadata: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    sql = _first_text(item, "sql", "standard", "standard_sql", "query", "question_sql")
    if not sql:
        return None
    schema = item.get("schema") or ""
    schema_catalog = item.get("schema_catalog")
    source_id = _first_text(item, "source_id", "source", "dataset") or input_path.stem
    source_info = source_metadata.get(source_id, {})
    source_url, source_url_status, source_capture_at, source_capture_status = _provenance_fields(
        item, source_info, captured_at
    )
    dialect, dialect_source = _resolve_dialect(item, sql, source_dialects)
    if _looks_like_embedded_record_payload(sql):
        return None
    if not _mutation_admission_ready(sql, dialect, schema):
        return None
    normalized_sql = _normalize_sql(sql)
    normalized_schema = _normalize_schema(schema, schema_catalog)
    lineage_family_id = _explicit_lineage_id(item)
    if lineage_family_id:
        family_id = _sha256_text(f"lineage\0{lineage_family_id}")
        structural_family_id = _sha256_text(f"lineage\0{lineage_family_id}")
        family_identity = "explicit_lineage"
    else:
        # Literal abstraction is useful for schema-backed teaching questions,
        # but it collapses unrelated schema-free queries such as SELECT 1 and
        # SELECT 2. Preserve the raw normalized SQL when no schema evidence is
        # available; exact duplicates still deduplicate deterministically.
        family_sql = normalized_sql if normalized_schema else _normalize_space(sql).rstrip(";").lower()
        family_id = _sha256_text(f"sql\0{family_sql}\0schema\0{normalized_schema}")
        structural_family_id = _sha256_text(f"sql\0{family_sql}")
        family_identity = "sql_schema"
    supplied_raw_text = _first_text(item, "raw_text", "text", "source_text")
    raw_text = supplied_raw_text or sql
    student_sql = _first_text(item, "student", "student_sql")
    labels = sorted(_labels(item, sql))
    scenario_axes, scenario_candidates = _scenario_axes(item, sql, schema_catalog, dialect)
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_id": f"family_{family_id[:20]}",
        "family_id": family_id,
        "structural_family_id": structural_family_id,
        "lineage_family_id": lineage_family_id or None,
        "family_identity": family_identity,
        "partition": _partition(family_id, seed, train_ratio, public_ratio),
        "source_id": source_id,
        "source_name": _first_text(item, "source_name", "name") or str(source_info.get("name") or source_id),
        "source_kind": _first_text(item, "source_kind", "kind") or str(source_info.get("kind") or "unknown"),
        "source_url": source_url,
        "source_url_status": source_url_status,
        "source_member": _first_text(item, "member", "source_member"),
        "source_metadata": source_info,
        "source_capture_at": source_capture_at,
        "source_capture_status": source_capture_status,
        "captured_at": captured_at,
        "input_file": str(input_path),
        "input_line": line_number,
        "raw_text": raw_text,
        "raw_text_kind": "source_record" if supplied_raw_text else "sql_fallback",
        "raw_text_sha256": _sha256_text(raw_text),
        "sql": sql,
        "sql_sha256": _sha256_text(sql),
        "normalized_sql": normalized_sql,
        "student_sql": student_sql or None,
        "schema": schema,
        "schema_sha256": _sha256_text(normalized_schema),
        "schema_catalog": schema_catalog,
        "schema_trust": _first_text(item, "schema_trust") or (
            "authoritative_source_catalog" if schema_catalog else "unknown"
        ),
        "replay_eligible": bool(item.get("replay_eligible", bool(schema or schema_catalog))),
        "dialect": dialect,
        "dialect_source": dialect_source,
        "categories": _categories(item, sql, dialect),
        "labels": labels,
        "scenario_axes": scenario_axes,
        "scenario_candidates": scenario_candidates,
        "expectation": _first_text(item, "expectation", "intent") or "unpaired",
        "attack_kind": _first_text(item, "attack_kind") or "source_query",
        "raw_record_sha256": _sha256_text(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        "provenance": {
            "source_url": source_url,
            "source_url_status": source_url_status,
            "captured_at": captured_at,
            "source_capture_at": source_capture_at,
            "source_capture_status": source_capture_status,
            "input_file": str(input_path),
            "input_line": line_number,
        },
    }
    # Execution evidence is produced only by an explicit bounded materializer
    # or audit stage.  Preserve it verbatim so downstream reports can
    # distinguish observed axes from SQL-structure candidates.
    for key in (
        "executed",
        "execution_evidence",
        "test_database",
        "standard_rows",
        "student_rows",
        "standard_row_count",
        "student_row_count",
        "boundary_evidence",
        "observed_scenario_axes",
    ):
        if key in item:
            record[key] = item[key]
    record["candidate_key"] = "\0".join(
        (
            f"{_candidate_priority(item):02d}",
            source_id,
            source_url,
            str(input_path),
            str(line_number),
            record["raw_record_sha256"],
        )
    )
    return record


def _validate_ratios(train_ratio: float, public_ratio: float) -> None:
    if train_ratio < 0 or public_ratio < 0 or train_ratio + public_ratio >= 1:
        raise ValueError("train/public ratios must be non-negative and leave hidden records")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _output_sha256(path: Path) -> str:
    return _file_sha256(path) if path.exists() else ""


def build_universe(
    inputs: Iterable[Path],
    output_dir: Path,
    *,
    captured_at: str,
    seed: int = 20260820,
    train_ratio: float = 0.8,
    public_ratio: float = 0.1,
    source_manifest: Path | None = DEFAULT_SOURCE_MANIFEST,
) -> dict[str, Any]:
    """Build one snapshot and return the public, SQL-free manifest."""
    _validate_ratios(train_ratio, public_ratio)
    datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    output_dir.mkdir(parents=True, exist_ok=True)
    index_path = output_dir / ".family_index.sqlite3"
    connection = sqlite3.connect(index_path)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS families (family_id TEXT PRIMARY KEY, candidate_key TEXT NOT NULL, record_json TEXT NOT NULL)"
        )
        connection.execute("DELETE FROM families")
        input_paths = [Path(path) for path in inputs]
        source_metadata = _load_source_metadata(source_manifest)
        source_dialects = {
            source_id: str(metadata.get("dialect") or "")
            for source_id, metadata in source_metadata.items()
            if str(metadata.get("dialect") or "")
        }
        input_fingerprints = [
            {"path": str(path), "sha256": _file_sha256(path)}
            for path in input_paths
            if path.is_file()
        ]
        total_input_records = 0
        invalid_records = 0
        valid_input_records = 0
        for input_path, line_number, item in _iter_input_records(input_paths):
            total_input_records += 1
            record = _canonical_record(
                item,
                input_path=input_path,
                line_number=line_number,
                captured_at=captured_at,
                seed=seed,
                train_ratio=train_ratio,
                public_ratio=public_ratio,
                source_dialects=source_dialects,
                source_metadata=source_metadata,
            )
            if record is None:
                invalid_records += 1
                continue
            valid_input_records += 1
            family_id = record["family_id"]
            candidate_key = record["candidate_key"]
            encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            connection.execute(
                "INSERT INTO families(family_id,candidate_key,record_json) VALUES(?,?,?) "
                "ON CONFLICT(family_id) DO UPDATE SET candidate_key=excluded.candidate_key, record_json=excluded.record_json "
                "WHERE excluded.candidate_key < families.candidate_key",
                (family_id, candidate_key, encoded),
            )
        connection.commit()

        snapshot_material = hashlib.sha256()
        counts = Counter()
        category_counts = Counter()
        source_counts = Counter()
        family_identity_counts = Counter()
        provenance_counts: dict[str, dict[str, Counter[str]]] = {
            partition: {
                field: Counter()
                for field in (
                    "source_url_status",
                    "source_capture_status",
                    "raw_text_kind",
                    "dialect_source",
                    "source_kind",
                )
            }
            for partition in ("train", "public", "hidden")
        }
        partition_paths = {
            partition: output_dir / f"{partition}.jsonl"
            for partition in ("train", "public", "hidden")
        }
        handles = {key: path.open("w", encoding="utf-8", newline="\n") for key, path in partition_paths.items()}
        try:
            for (encoded,) in connection.execute("SELECT record_json FROM families ORDER BY family_id"):
                record = json.loads(encoded)
                family_id = str(record["family_id"])
                snapshot_material.update(f"{family_id}\n".encode("utf-8"))
                snapshot_material.update(encoded.encode("utf-8"))
                snapshot_material.update(b"\n")
                partition = str(record["partition"])
                handles[partition].write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                counts[partition] += 1
                source_counts[(partition, record["source_id"])] += 1
                family_identity_counts[record.get("family_identity") or "unknown"] += 1
                for field, counter in provenance_counts[partition].items():
                    counter[str(record.get(field) or "unknown")] += 1
                for category in record["categories"]:
                    category_counts[(partition, category)] += 1
        finally:
            for handle in handles.values():
                handle.close()
        snapshot_id = snapshot_material.hexdigest()
        public_manifest = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "captured_at": captured_at,
            "seed": seed,
            "split": {
                "train_ratio": train_ratio,
                "public_ratio": public_ratio,
                "hidden_ratio": 1 - train_ratio - public_ratio,
                "unit": "question_family",
            },
            "inputs": input_fingerprints,
            "source_manifest": {
                "path": str(source_manifest) if source_manifest else None,
                "sha256": _file_sha256(source_manifest)
                if source_manifest and source_manifest.is_file()
                else None,
                "resolution": "record_declared > source_manifest > syntax_inferred > generic_fallback",
            },
            "total_input_records": total_input_records,
            "valid_input_records": valid_input_records,
            "invalid_input_records": invalid_records,
            "duplicate_input_records": max(0, valid_input_records - sum(counts.values())),
            "unique_question_families": sum(counts.values()),
            "family_identity_counts": dict(sorted(family_identity_counts.items())),
            "partition_counts": dict(sorted(counts.items())),
            "partition_category_counts": {
                partition: {
                    category: category_counts[(partition, category)]
                    for category in CATEGORIES
                }
                for partition in ("train", "public", "hidden")
            },
            "partition_source_counts": {
                partition: dict(sorted(
                    (source, count)
                    for (part, source), count in source_counts.items()
                    if part == partition
                ))
                for partition in ("train", "public", "hidden")
            },
            "provenance_status_counts": {
                partition: {
                    field: dict(sorted(counter.items()))
                    for field, counter in sorted(fields.items())
                }
                for partition, fields in sorted(provenance_counts.items())
            },
            "provenance_status_semantics": {
                "record_declared": "value was retained on the input record",
                "source_manifest": "value was recovered from the authoritative source manifest",
                "snapshot_capture": "the local immutable corpus snapshot capture date; upstream response date was not retained",
                "missing_upstream_record": "the retained snapshot has no row- or source-level value",
                "raw_text_kind": "source_record vs sql_fallback identifies raw text fidelity",
            },
            "files": {
                partition: {
                    "path": str(path),
                    "sha256": _output_sha256(path),
                }
                for partition, path in partition_paths.items()
            },
            "hidden_data_policy": "hidden.jsonl is excluded from public reports and must not be used by optimization jobs",
        }
        (output_dir / "manifest.json").write_text(
            json.dumps(public_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (output_dir / "README.txt").write_text(
            "This is a frozen Phase 1 corpus snapshot. Use train.jsonl and public.jsonl for development.\n"
            "Do not read hidden.jsonl from optimization or repair jobs.\n",
            encoding="utf-8",
        )
        return public_manifest
    finally:
        connection.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", type=Path, dest="inputs")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--captured-at", required=True, help="UTC ISO timestamp, e.g. 2026-08-20T00:00:00Z")
    parser.add_argument("--seed", type=int, default=20260820)
    parser.add_argument("--train-ratio", type=float, default=0.8)
    parser.add_argument("--public-ratio", type=float, default=0.1)
    parser.add_argument(
        "--source-manifest",
        type=Path,
        default=DEFAULT_SOURCE_MANIFEST,
        help="authoritative source metadata used when a record says generic",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    inputs = args.inputs or list(DEFAULT_INPUTS)
    manifest = build_universe(
        inputs,
        args.output_dir,
        captured_at=args.captured_at,
        seed=args.seed,
        train_ratio=args.train_ratio,
        public_ratio=args.public_ratio,
        source_manifest=args.source_manifest,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
