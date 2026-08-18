"""Collect and normalize external SQL corpora for convergence benchmarks.

The collector is intentionally standard-library only. It keeps raw downloads in
an auditable cache, extracts SQL from JSON/JSONL/text/archive sources, dedupes by
normalized SQL text, and writes a compact JSONL corpus consumed by
run_phase1_cfg_convergence_benchmark.py.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import tarfile
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from spider_schema_catalog import compact_schema, load_spider_catalog
except ModuleNotFoundError:  # Imported by tests from the repository root.
    import sys

    _SCRIPT_DIR = Path(__file__).resolve().parent
    if str(_SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPT_DIR))
    from spider_schema_catalog import compact_schema, load_spider_catalog


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = PROJECT_ROOT / "data_construct_test" / "sources" / "web_sql_corpus_manifest.json"
OUTPUT_DIR = PROJECT_ROOT / "data_construct_test" / "outputs"
DEFAULT_CACHE_DIR = OUTPUT_DIR / "web_sql_raw"
DEFAULT_OUTPUT = OUTPUT_DIR / "web_sql_corpus.jsonl"
DEFAULT_REPORT = OUTPUT_DIR / "web_sql_corpus_report.json"

AGGREGATORS = ("", "MAX", "MIN", "COUNT", "SUM", "AVG")
WIKISQL_COND_OPS = ("=", ">", "<", "OP")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch and normalize external SQL corpora.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--max-per-source", type=int, default=5000)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--source-id", action="append", default=[])
    parser.add_argument("--local-file", action="append", type=Path, default=[])
    parser.add_argument("--offline-cache-only", action="store_true")
    parser.add_argument("--include-reference-only", action="store_true")
    parser.add_argument(
        "--spider-tables-json",
        type=Path,
        help="official Spider tables.json; required for Spider sources",
    )
    return parser.parse_args()


def _slug(value: str) -> str:
    slug = re.sub(r"\W+", "_", value.strip().lower()).strip("_")
    return slug or "value"


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def _normalize_sql(sql: str) -> str:
    sql = re.sub(r"--.*?$", " ", sql, flags=re.MULTILINE)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"\s+", " ", sql.strip())
    return sql.rstrip(";")


def _is_read_only_query(sql: str) -> bool:
    """Keep the equivalence corpus query-only and side-effect free."""

    normalized = _normalize_sql(sql)
    if not re.match(r"^(?:WITH|SELECT)\b", normalized, flags=re.IGNORECASE):
        return False
    if re.search(r"\b(?:INSERT|UPDATE|DELETE|MERGE|REPLACE|CREATE|ALTER|DROP|TRUNCATE)\b", normalized, re.IGNORECASE):
        return False
    if re.search(r"\bSELECT\s+.+?\bINTO\b", normalized, re.IGNORECASE | re.DOTALL):
        return False
    # A second WITH in the normalized text is a strong signal that a mined
    # corpus concatenated independent semicolon-free CTE examples. Nested CTEs
    # still use one leading WITH with comma-separated definitions.
    if len(re.findall(r"\bWITH\b", normalized, re.IGNORECASE)) > 1:
        return False
    return True


def _split_tutorial_sql(text: str) -> list[str]:
    """Split semicolon-free tutorial files at top-level CTE boundaries.

    Many teaching repositories put several independent ``WITH`` examples in a
    single file without semicolons. Treating the whole file as one statement
    can feed UPDATE/DELETE examples and multiple queries into the data
    generator, causing unbounded work. A line-start WITH is a reliable boundary
    for these files because nested SELECTs remain inside the current CTE.
    """

    starts = [match.start() for match in re.finditer(r"(?im)^WITH\b", text)]
    if len(starts) <= 1:
        return [text]
    starts.append(len(text))
    return [text[starts[index]:starts[index + 1]] for index in range(len(starts) - 1)]


def _labels(sql: str) -> list[str]:
    text = sql.lower()
    labels: set[str] = {"select-basic"}
    checks = [
        ("distinct", "distinct"),
        (" join ", "join-inner"),
        (" left join ", "join-left"),
        (" right join ", "join-right-full"),
        (" full join ", "join-right-full"),
        (" on ", "join-on"),
        (" group by ", "group-by"),
        (" having ", "having"),
        (" order by ", "order-by"),
        (" limit ", "limit-offset"),
        (" fetch first ", "limit-offset"),
        (" union ", "union"),
        (" intersect ", "intersect"),
        (" except ", "except"),
        (" over ", "window-agg"),
        (" with ", "cte"),
        (" recursive ", "cte-recursive"),
        (" is null", "null-handling"),
        (" coalesce", "null-handling"),
        (" between ", "between"),
        (" in ", "in-list"),
        (" like ", "like"),
        (" case ", "case"),
    ]
    for needle, label in checks:
        if needle in text:
            labels.add(label)
    if any(token in text for token in (" count(", " sum(", " avg(", " min(", " max(")):
        labels.add("agg-count")
    if re.search(r"[<>=]", text):
        labels.add("where-comp")
    if " where " in text:
        labels.add("where")
    if "select" in text and re.search(r"[+\-*/%]", text):
        labels.add("arithmetic")
    if " exists " in text:
        labels.add("subquery-exists")
    if re.search(r"\(\s*select\b", text):
        labels.add("subquery-scalar")
    return sorted(labels)


def _infer_schema(sql: str) -> str:
    tables: list[str] = []
    for match in re.finditer(
        r"\bfrom\s+([A-Za-z_][\w$]*)|\bjoin\s+([A-Za-z_][\w$]*)",
        sql,
        flags=re.IGNORECASE,
    ):
        table = match.group(1) or match.group(2)
        if table:
            tables.append(_slug(table))
    tables = list(dict.fromkeys(tables))
    if not tables:
        return ""

    columns = [
        _slug(value)
        for value in re.findall(r"(?<!['\"])\b([A-Za-z_][\w$]*)\b(?!['\"])", sql)
        if value.lower() not in {
            "select", "from", "where", "join", "left", "right", "full", "inner", "outer",
            "on", "and", "or", "not", "null", "is", "in", "exists", "group", "by",
            "having", "order", "limit", "offset", "with", "as", "case", "when", "then",
            "else", "end", "distinct", "union", "all", "intersect", "except", "over",
            "partition", "rows", "range", "between", "like", "true", "false",
        }
    ]
    cols = [col for col in dict.fromkeys(columns) if col not in tables]
    if not cols:
        cols = ["id", "value"]
    return "; ".join(f"{table}({', '.join(cols[:12])})" for table in tables) + ";"


def _read_manifest(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("sources", []))


def _cache_path(cache_dir: Path, source: dict[str, Any]) -> Path:
    url = str(source.get("url") or source["id"])
    suffix = Path(url.split("?", 1)[0]).suffix
    if url.endswith(".tar.bz2"):
        suffix = ".tar.bz2"
    elif url.endswith(".tar.gz"):
        suffix = ".tar.gz"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return cache_dir / f"{source['id']}__{digest}{suffix or '.dat'}"


def _download(source: dict[str, Any], cache_dir: Path, timeout: int, offline: bool) -> Path | None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, source)
    if path.exists() and path.stat().st_size:
        return path
    if offline:
        return None
    request = urllib.request.Request(
        str(source["url"]),
        headers={"User-Agent": "sql-edu-corpus-collector/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        path.write_bytes(response.read())
    return path


def _download_url(url: str, path: Path, timeout: int, offline: bool) -> Path | None:
    """Download one explicitly addressed page into the auditable cache."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size:
        return path
    if offline:
        return None
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "sql-edu-corpus-collector/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        path.write_bytes(response.read())
    return path


def _extract_sql_text(text: str) -> Iterable[dict[str, Any]]:
    stripped = text.strip()
    if not stripped:
        return
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        payload = None
    if payload is not None:
        yield from _extract_from_json(payload)
        return
    parsed_json_lines = False
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield from _extract_from_json(json.loads(line))
            parsed_json_lines = True
        except json.JSONDecodeError:
            pass
    # JSONL documents have already been fully traversed.  Falling through to
    # the broad SELECT/semicolon regex would double every row (and could
    # silently hit max_items before the source's tail was seen).
    if parsed_json_lines:
        return
    for document in _split_tutorial_sql(text):
        for match in re.finditer(r"\b(?:WITH|SELECT)\b.+?(?:;|$)", document, flags=re.IGNORECASE | re.DOTALL):
            sql = _normalize_sql(match.group(0))
            if sql and _is_read_only_query(sql):
                yield {"sql": sql}


def _extract_from_json(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("query", "SQL", "sql", "gold", "ans_sql", "answer_sql"):
            value = payload.get(key)
            if isinstance(value, str) and _is_read_only_query(value):
                yield {
                    "sql": _normalize_sql(value),
                    "schema": payload.get("schema") or payload.get("db_schema") or "",
                    "schema_catalog": payload.get("schema_catalog"),
                    "db_id": payload.get("db_id") or payload.get("database_id") or payload.get("table_id"),
                }
        for value in payload.values():
            yield from _extract_from_json(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _extract_from_json(value)


def _iter_archive_members(path: Path) -> Iterable[tuple[str, bytes]]:
    mode = "r:*"
    with tarfile.open(path, mode) as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            lower = member.name.lower()
            if not lower.endswith((".json", ".jsonl", ".sql", ".txt")):
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            yield member.name, extracted.read()


def _iter_raw_documents(path: Path) -> Iterable[tuple[str, bytes]]:
    lower = path.name.lower()
    if lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2")):
        yield from _iter_archive_members(path)
    else:
        yield path.name, path.read_bytes()


def _collect_wikisql(path: Path, source: dict[str, Any], max_items: int) -> list[dict[str, Any]]:
    tables: dict[str, dict[str, Any]] = {}
    examples: list[tuple[str, dict[str, Any]]] = []
    for member_name, raw in _iter_raw_documents(path):
        if not member_name.endswith(".jsonl"):
            continue
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "header" in item and "id" in item:
                tables[str(item["id"])] = item
            elif isinstance(item.get("sql"), dict):
                examples.append((member_name, item))

    records: list[dict[str, Any]] = []
    for member_name, item in examples:
        table = tables.get(str(item.get("table_id")))
        if not table:
            continue
        headers = [_slug(str(header)) for header in table.get("header", [])]
        if not headers:
            continue
        table_name = f"wikisql_{_slug(str(item.get('table_id')))}"
        sql_obj = item["sql"]
        sel_index = int(sql_obj.get("sel", 0))
        agg_index = int(sql_obj.get("agg", 0))
        if sel_index >= len(headers):
            continue
        select_expr = headers[sel_index]
        agg = AGGREGATORS[agg_index] if 0 <= agg_index < len(AGGREGATORS) else ""
        if agg:
            select_expr = f"{agg}({select_expr})"
        predicates = []
        for condition in sql_obj.get("conds", []):
            if not isinstance(condition, list) or len(condition) < 3:
                continue
            col_index, op_index, value = condition[:3]
            if not isinstance(col_index, int) or col_index >= len(headers):
                continue
            op = WIKISQL_COND_OPS[op_index] if isinstance(op_index, int) and 0 <= op_index < len(WIKISQL_COND_OPS) else "="
            if op == "OP":
                op = "="
            predicates.append(f"{headers[col_index]} {op} {_sql_literal(value)}")
        sql = f"SELECT {select_expr} FROM {table_name}"
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        schema = f"{table_name}({', '.join(headers)});"
        records.append(_record(source, sql, schema, member_name, "wikisql_structured"))
        if len(records) >= max_items:
            break
    return records


def _record(
    source: dict[str, Any],
    sql: str,
    schema: str,
    member_name: str,
    method: str,
    *,
    schema_catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sql = _normalize_sql(sql)
    schema = schema or _infer_schema(sql)
    digest = hashlib.sha256(f"{source['id']}\0{member_name}\0{sql}".encode("utf-8")).hexdigest()
    return {
        "id": f"websql_{digest[:20]}",
        "source_id": source["id"],
        "source_name": source.get("name") or source["id"],
        "source_kind": source.get("kind") or "unknown",
        "source_url": source.get("url"),
        "member": member_name,
        "extraction_method": method,
        "dialect": source.get("dialect") or "generic",
        "sql": sql,
        "schema": schema,
        "schema_catalog": schema_catalog,
        "cfg_labels": _labels(sql),
        "provenance_hash": digest,
    }


def _collect_generic(
    path: Path,
    source: dict[str, Any],
    max_items: int,
    spider_catalog: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for member_name, raw in _iter_raw_documents(path):
        text = raw.decode("utf-8", errors="replace")
        for item in _extract_sql_text(text):
            sql = item.get("sql")
            if not isinstance(sql, str):
                continue
            db_id = str(item.get("db_id") or "").strip()
            catalog_entry = item.get("schema_catalog")
            if source["id"].startswith("spider_"):
                if spider_catalog is None:
                    raise ValueError(
                        "Spider source requires --spider-tables-json; query-text schema inference is disabled"
                    )
                catalog_entry = spider_catalog.get(db_id.lower())
                if catalog_entry is None:
                    raise ValueError(f"Spider db_id is missing from tables.json: {db_id!r}")
                schema = compact_schema(catalog_entry)
            else:
                schema = str(item.get("schema") or "")
            records.append(_record(
                source,
                sql,
                schema,
                member_name,
                "generic_recursive",
                schema_catalog=catalog_entry,
            ))
            if len(records) >= max_items:
                return records
    return records


def _collect_hf_rows_api(
    source: dict[str, Any],
    cache_dir: Path,
    timeout: int,
    offline: bool,
    max_items: int,
    spider_catalog: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Collect Hugging Face datasets-server rows without requiring `datasets`.

    The endpoint returns a bounded page of JSON rows.  We page deterministically
    and cache each page separately, which makes offline replay possible after a
    successful online collection.
    """

    parsed = urlsplit(str(source["url"]))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    page_size = max(1, min(int(source.get("page_size", 100)), 100))
    offset = int(query.get("offset", "0") or 0)
    records: list[dict[str, Any]] = []
    while len(records) < max_items:
        page_query = dict(query)
        page_query["offset"] = str(offset)
        page_query["length"] = str(min(page_size, max_items - len(records)))
        page_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(page_query), parsed.fragment))
        digest = hashlib.sha256(page_url.encode("utf-8")).hexdigest()[:16]
        page_path = cache_dir / f"{source['id']}__page_{offset:08d}_{digest}.json"
        cached = _download_url(page_url, page_path, timeout, offline)
        if cached is None:
            break
        try:
            payload = json.loads(cached.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            break
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            break
        for wrapper in rows:
            item = wrapper.get("row") if isinstance(wrapper, dict) else None
            if not isinstance(item, dict):
                continue
            sql = item.get("query") or item.get("SQL") or item.get("sql")
            if not isinstance(sql, str) or not _is_read_only_query(sql):
                continue
            db_id = str(item.get("db_id") or "")
            if spider_catalog is None:
                raise ValueError(
                    "Spider source requires --spider-tables-json; query-text schema inference is disabled"
                )
            catalog_entry = spider_catalog.get(db_id.lower())
            if catalog_entry is None:
                raise ValueError(f"Spider db_id is missing from tables.json: {db_id!r}")
            records.append(_record(
                source,
                sql,
                compact_schema(catalog_entry),
                f"{query.get('split', 'split')}:{offset}",
                "hf_rows_api",
                schema_catalog=catalog_entry,
            ))
            if len(records) >= max_items:
                break
        offset += len(rows)
        if len(rows) < page_size:
            break
    return records


def collect_source(
    source: dict[str, Any],
    cache_dir: Path,
    timeout: int,
    offline: bool,
    max_items: int,
    spider_catalog: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    mode = (source.get("extraction") or {}).get("mode")
    if mode == "hf_rows_api":
        return _collect_hf_rows_api(source, cache_dir, timeout, offline, max_items, spider_catalog)
    path = source.get("local_path")
    if path:
        raw_path = Path(path)
        if not raw_path.is_absolute():
            raw_path = PROJECT_ROOT / raw_path
    else:
        downloaded = _download(source, cache_dir, timeout, offline)
        if downloaded is None:
            return []
        raw_path = downloaded
    if mode == "wikisql_structured":
        return _collect_wikisql(raw_path, source, max_items)
    if mode == "reference_only":
        return []
    return _collect_generic(raw_path, source, max_items, spider_catalog)


def main() -> None:
    args = parse_args()
    if args.max_per_source <= 0:
        raise SystemExit("--max-per-source must be > 0")
    spider_catalog = (
        load_spider_catalog(args.spider_tables_json)
        if args.spider_tables_json is not None
        else None
    )
    sources = _read_manifest(args.manifest)
    selected = set(args.source_id)
    if args.local_file:
        for index, local_file in enumerate(args.local_file, start=1):
            sources.append({
                "id": f"local_file_{index}_{_slug(local_file.stem)}",
                "name": str(local_file),
                "kind": "local_external_seed",
                "enabled": True,
                "reference_only": False,
                "local_path": str(local_file),
                "dialect": "generic",
                "extraction": {"mode": "json_recursive"},
            })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    all_records: list[dict[str, Any]] = []
    source_stats: dict[str, Any] = {}
    for source in sources:
        if selected and source["id"] not in selected:
            continue
        if not source.get("enabled") and not selected:
            continue
        if source.get("reference_only") and not args.include_reference_only:
            source_stats[source["id"]] = {"status": "reference_only_skipped", "records": 0}
            continue
        try:
            records = collect_source(
                source,
                args.cache_dir,
                args.timeout,
                args.offline_cache_only,
                args.max_per_source,
                spider_catalog,
            )
        except Exception as exc:  # noqa: BLE001 - report source-level failures without losing other corpora.
            source_stats[source["id"]] = {"status": "error", "records": 0, "error": str(exc)}
            continue
        source_stats[source["id"]] = {"status": "ok", "records": len(records)}
        all_records.extend(records)

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for record in all_records:
        key = hashlib.sha256(_normalize_sql(record["sql"]).lower().encode("utf-8")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)

    args.output.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in deduped) + ("\n" if deduped else ""),
        encoding="utf-8",
    )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest": str(args.manifest),
        "output": str(args.output),
        "total_records": len(deduped),
        "raw_records_before_dedupe": len(all_records),
        "source_stats": source_stats,
        "label_counts": dict(Counter(label for record in deduped for label in record["cfg_labels"])),
        "source_counts": dict(Counter(record["source_id"] for record in deduped)),
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
