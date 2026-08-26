"""Classify mutation-layer parse gaps without exposing SQL text.

The mutation builder deliberately skips records that cannot be parsed or that
contain no safe operator.  This audit makes that boundary reproducible: it
reads only the explicitly supplied train/public inputs, records bounded reason
counts and family digests, and never writes SQL or raw source text.  It is a
diagnostic, not an oracle, and it must not be pointed at the hidden partition.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_phase1_mutation_layer as mutation_layer  # noqa: E402


SCHEMA_VERSION = 1
STATEMENT_START = re.compile(
    r"(?is)^(?:SELECT|WITH|INSERT|UPDATE|DELETE|VALUES|MERGE|CREATE)\b"
)
TEMPLATE_MARKER = re.compile(
    r"(?is)\bTOP\s+(?:number\s*\|\s*percent|number|percent)\b"
    r"|\bcolumn_name\(s\)\b|\btable_name\b|\.\.\."
)
ENGINE_GAP_MARKER = re.compile(
    r"(?is)\bTOP\s+\d+\s+WITH\s+TIES\b"
    r"|\bOPTION\s*\(\s*MAXRECURSION\b|\bUNPIVOT\b"
)


def _sha256_lines(values: set[str]) -> str:
    digest = hashlib.sha256()
    for value in sorted(values):
        digest.update(value.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _strip_comments(value: str) -> str:
    value = re.sub(r"(?s)/\*.*?\*/", " ", value)
    value = re.sub(r"(?m)--[^\r\n]*", " ", value)
    return value.lstrip()


def _top_level_statement_count(value: str) -> int:
    """Count top-level statement starts with a bounded lexical scan.

    This intentionally does not attempt to parse SQL.  It ignores quoted
    strings, comments, bracketed identifiers, and nested SELECTs, and treats
    ``TOP n WITH TIES`` as one statement rather than mistaking its ``WITH``
    token for a second statement.
    """

    statement_words = ("SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "VALUES", "MERGE", "CREATE")
    depth = 0
    count = 0
    index = 0
    quote: str | None = None
    while index < len(value):
        char = value[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if value.startswith("--", index):
            newline = value.find("\n", index + 2)
            index = len(value) if newline < 0 else newline + 1
            continue
        if value.startswith("/*", index):
            close = value.find("*/", index + 2)
            index = len(value) if close < 0 else close + 2
            continue
        if char in "'\"`":
            quote = char
            index += 1
            continue
        if char == "[":
            close = value.find("]", index + 1)
            index = len(value) if close < 0 else close + 1
            continue
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth = max(depth - 1, 0)
            index += 1
            continue
        if depth == 0 and char == ";":
            index += 1
            continue
        if depth == 0 and (index == 0 or not (value[index - 1].isalnum() or value[index - 1] == "_")):
            matched = next(
                (
                    word
                    for word in statement_words
                    if value[index : index + len(word)].upper() == word
                    and (index + len(word) == len(value) or not value[index + len(word)].isalnum())
                ),
                None,
            )
            if matched is not None:
                before = value[:index].lower()
                if matched != "WITH" or not re.search(r"\btop\s+[^;]*$", before):
                    count += 1
                index += len(matched)
                continue
        index += 1
    return count


def _reason(sql: str, dialect: str | None, schema_text: str) -> str:
    """Return a conservative, non-semantic parse-gap class."""

    if TEMPLATE_MARKER.search(sql):
        return "INPUT_GAP_TEMPLATE"
    candidate = mutation_layer._strip_leading_prose(sql)
    target = _strip_comments(candidate)
    if target[:4].upper() == "WITH":
        # A CTE must have a name and an AS (...) body.  Otherwise this is the
        # common scraped instructional sentence beginning with “with”.
        if not re.match(r"(?is)^WITH\s+(?:RECURSIVE\s+)?[A-Za-z_][\w$]*\s+AS\s*\(", target):
            return "INPUT_GAP_PROSE"
    elif not STATEMENT_START.match(target):
        return "INPUT_GAP_PROSE"
    # A few scraped rows retain an exercise sentence before/after a query;
    # keep these as input gaps rather than pretending a parser repair exists.
    if re.match(r"(?is)^SELECT\s*(?:,|AND\b|EMPLOYEES\b)", target):
        return "INPUT_GAP_PROSE"
    if re.search(r"(?is)\bUSE\s+[A-Za-z_][\w$]*\s*$", target):
        return "INPUT_GAP_PROSE"
    if _top_level_statement_count(candidate) >= 2:
        return "INPUT_GAP_MULTI_STATEMENT"
    if ENGINE_GAP_MARKER.search(sql):
        return "ENGINE_GAP_SYNTAX"
    return "PARSE_ERROR_OTHER"


def _load_records(inputs: list[Path]) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in inputs:
        if "hidden" in path.name.lower():
            raise ValueError(f"hidden input is forbidden: {path}")
        for line_number, record in enumerate(mutation_layer._iter_records([path]), start=1):
            if not mutation_layer._usable(record):
                errors.append(f"unusable:{path.name}:{line_number}")
                continue
            records.append(record)
    return records, errors


def audit(inputs: list[Path]) -> dict[str, Any]:
    records, errors = _load_records(inputs)
    seen: set[str] = set()
    reason_counts: Counter[str] = Counter()
    by_dialect: dict[str, Counter[str]] = defaultdict(Counter)
    by_category: dict[str, Counter[str]] = defaultdict(Counter)
    by_reason_families: dict[str, set[str]] = defaultdict(set)
    parse_failed = 0
    for record in records:
        family_id = str(record["family_id"])
        if family_id in seen:
            continue
        seen.add(family_id)
        dialect = mutation_layer._dialect_of(record)
        schema_text = str(record.get("schema") or "")
        if mutation_layer._parse(str(record["sql"]), dialect, schema_text) is not None:
            continue
        parse_failed += 1
        reason = _reason(str(record["sql"]), dialect, schema_text)
        reason_counts[reason] += 1
        by_reason_families[reason].add(family_id)
        label = str(record.get("dialect") or "generic")
        by_dialect[label][reason] += 1
        for category in record.get("categories") or ():
            by_category[str(category)][reason] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "generator": "audit_phase1_mutation_gaps",
        "inputs": [str(path) for path in inputs],
        "hidden_partition_read": False,
        "contains_sql": False,
        "families_read": len(seen),
        "parse_failed": parse_failed,
        "loader_errors": len(errors),
        "reason_policy": {
            "INPUT_GAP_PROSE": "scraped instructional text does not contain one executable statement",
            "INPUT_GAP_TEMPLATE": "documentation/template placeholders are not executable SQL",
            "INPUT_GAP_MULTI_STATEMENT": "one family contains multiple top-level statements",
            "ENGINE_GAP_SYNTAX": "recognized vendor syntax is outside the available parser/executor scope",
            "PARSE_ERROR_OTHER": "remaining parser failure requiring manual review",
        },
        "by_reason": dict(sorted(reason_counts.items())),
        "by_dialect": {
            key: dict(sorted(value.items())) for key, value in sorted(by_dialect.items())
        },
        "by_category": {
            key: dict(sorted(value.items())) for key, value in sorted(by_category.items())
        },
        "reason_family_digests": {
            key: _sha256_lines(value) for key, value in sorted(by_reason_families.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = audit(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
