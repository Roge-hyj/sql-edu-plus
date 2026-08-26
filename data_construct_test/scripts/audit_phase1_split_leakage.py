"""Audit family, SQL, schema, and mutation-lineage leakage across splits.

This is the only development utility that intentionally reads the hidden
partition, and it emits hashes/counts only.  Optimization and capability
readers continue to reject hidden input paths.  A non-zero exit code indicates
split leakage or a hidden record/path accidentally present in a public split.

Normalized SQL alone is reported as a template-overlap signal rather than a
hard failure: the same query shape can be a legitimate independent question
when its authoritative schema/domain differs.  The executable identity key is
the normalized SQL plus normalized schema.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from typing import Any


PARTITIONS = ("train", "public", "hidden")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _normalize_sql(value: Any) -> str:
    text = re.sub(r"--[^\r\n]*", " ", str(value or ""))
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"'(?:''|[^'])*'", "'<literal>'", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\b", "<number>", text)
    return _normalize_space(text).rstrip(";")


def _normalize_schema(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _normalize_space(value)


def _digest_keys(values: set[str]) -> str:
    digest = hashlib.sha256()
    for value in sorted(values):
        digest.update(value.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _load(path: Path, expected_partition: str) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                errors.append(f"invalid_json:{expected_partition}:{line_number}")
                continue
            if not isinstance(record, dict):
                errors.append(f"non_object:{expected_partition}:{line_number}")
                continue
            if str(record.get("partition") or expected_partition).lower() != expected_partition:
                errors.append(f"partition_field_mismatch:{expected_partition}:{line_number}")
            records.append(record)
    return records, errors


def _key_sets(records: list[dict[str, Any]]) -> dict[str, set[str]]:
    result = {
        "family": set(),
        "sql_schema": set(),
        "normalized_sql": set(),
        "raw_record": set(),
        "mutation_lineage": set(),
        "source_structural": set(),
        "lineage_family": set(),
    }
    for record in records:
        family = str(record.get("family_id") or "")
        sql = _normalize_sql(record.get("sql") or record.get("standard_sql"))
        schema = _normalize_schema(record.get("schema") or record.get("schema_catalog"))
        source = _normalize_space(record.get("source_id") or "unknown")
        structural = str(record.get("structural_family_id") or "")
        lineage = _normalize_space(record.get("lineage_family_id") or "")
        raw = str(record.get("raw_record_sha256") or "")
        if family:
            result["family"].add(family)
        result["sql_schema"].add(f"{sql}\0{schema}")
        result["normalized_sql"].add(sql)
        if raw:
            result["raw_record"].add(raw)
        if structural:
            result["source_structural"].add(f"{source}\0{structural}")
        if lineage:
            result["lineage_family"].add(lineage)
        # A paired mutation must stay with its standard-question lineage.  A
        # source id alone is intentionally not considered leakage because one
        # source naturally contributes questions to all three partitions.
        if record.get("student_sql"):
            mutation_key = lineage or f"{structural}\0{sql}"
            result["mutation_lineage"].add(f"{source}\0{mutation_key}")
    return result


def audit(universe_dir: Path, output: Path) -> dict[str, Any]:
    paths = {partition: universe_dir / f"{partition}.jsonl" for partition in PARTITIONS}
    loaded: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    for partition, path in paths.items():
        if not path.is_file():
            errors.append(f"missing_file:{partition}:{path}")
            loaded[partition] = []
            continue
        loaded[partition], file_errors = _load(path, partition)
        errors.extend(file_errors)
    keys = {partition: _key_sets(records) for partition, records in loaded.items()}
    pairwise: dict[str, dict[str, int]] = {}
    for left, right in (("train", "public"), ("train", "hidden"), ("public", "hidden")):
        pairwise[f"{left}_vs_{right}"] = {
            name: len(keys[left][name] & keys[right][name])
            for name in keys[left]
        }
    hidden_keys = keys["hidden"]
    public_hidden_exposure = {
        "hidden_file_sha256": _file_sha256(paths["hidden"]) if paths["hidden"].is_file() else None,
        "hidden_record_count": len(loaded["hidden"]),
        "hidden_family_digest": _digest_keys(hidden_keys["family"]),
        "hidden_sql_schema_digest": _digest_keys(hidden_keys["sql_schema"]),
        "hidden_mutation_lineage_digest": _digest_keys(hidden_keys["mutation_lineage"]),
    }
    hard_failures: list[str] = list(errors)
    hard_overlap_keys = {
        "family",
        "lineage_family",
        "sql_schema",
        "raw_record",
        "mutation_lineage",
    }
    template_overlaps: list[str] = []
    for pair, overlaps in pairwise.items():
        for name, count in overlaps.items():
            if not count:
                continue
            if name in hard_overlap_keys:
                hard_failures.append(f"overlap:{pair}:{name}:{count}")
            elif name in {"normalized_sql", "source_structural"}:
                template_overlaps.append(f"overlap:{pair}:{name}:{count}")
    # Public partitions must not carry hidden paths or hidden partition tags.
    for partition in ("train", "public"):
        for record in loaded[partition]:
            serialized = json.dumps(record, ensure_ascii=False, sort_keys=True).lower()
            if '"partition": "hidden"' in serialized or "hidden.jsonl" in serialized:
                hard_failures.append(f"hidden_exposure:{partition}:{record.get('family_id')}")
    result = {
        "schema_version": 1,
        "hidden_partition_read": True,
        "purpose": "one-time split leakage audit; hidden rows are not emitted",
        "partition_counts": {partition: len(loaded[partition]) for partition in PARTITIONS},
        "file_sha256": {
            partition: _file_sha256(path) if path.is_file() else None
            for partition, path in paths.items()
        },
        "pairwise_overlap_counts": pairwise,
        "hidden_digest_only": public_hidden_exposure,
        "leakage_policy": {
            "hard_keys": sorted(hard_overlap_keys),
            "template_overlap_keys": ["normalized_sql", "source_structural"],
            "template_overlap_policy": "report_only_when_normalized_schema_differs",
        },
        "template_overlaps": sorted(template_overlaps),
        "errors": errors,
        "hard_failures": hard_failures,
        "pass": not hard_failures,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = audit(args.universe_dir, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
