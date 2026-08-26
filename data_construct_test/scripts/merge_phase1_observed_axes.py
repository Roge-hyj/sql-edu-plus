"""Merge public Gold execution evidence into a mutation layer.

The mutation builder is deliberately oracle-independent, so its rows initially
carry only candidate/template axes.  This command is the explicit boundary
between generation and observation: it copies only ``observed_scenario_axes``
from an already executed public Gold audit, keyed by question family and
mutation-layer role.  Hidden inputs and hidden records are rejected.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any, Sequence


ROLES = {"mutation", "equivalence"}


def _reject_hidden(path: Path) -> None:
    if "hidden" in path.name.lower():
        raise ValueError(f"hidden input is forbidden: {path}")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    _reject_hidden(path)
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            if str(row.get("partition") or "").lower() == "hidden":
                raise ValueError(f"hidden record is forbidden: {path}:{line_number}")
            rows.append(row)
    return rows


def _key(row: dict[str, Any]) -> tuple[str, str]:
    family_id = str(row.get("family_id") or "").strip()
    role = str(row.get("mutation_layer_role") or "").strip()
    return family_id, role


def merge(source: Path, audit: Path | Sequence[Path], output: Path) -> dict[str, Any]:
    layer_rows = _read_jsonl(source)
    audit_paths = [audit] if isinstance(audit, Path) else list(audit)
    if not audit_paths:
        raise ValueError("at least one public audit input is required")
    evidence: dict[tuple[str, str], set[str]] = {}
    evidence_rows: Counter[tuple[str, str]] = Counter()
    duplicate_evidence = 0
    audit_row_count = 0
    for audit_path in audit_paths:
        for row in _read_jsonl(audit_path):
            audit_row_count += 1
            key = _key(row)
            if not key[0] or key[1] not in ROLES:
                continue
            axes = {str(value) for value in row.get("observed_scenario_axes") or [] if str(value).strip()}
            if key in evidence:
                duplicate_evidence += 1
            evidence.setdefault(key, set()).update(axes)
            evidence_rows[key] += 1

    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, Any]] = []
    matched = 0
    unmatched_audit_keys = set(evidence)
    for row in layer_rows:
        key = _key(row)
        if key in seen:
            raise ValueError(f"duplicate mutation-layer key: {key}")
        seen.add(key)
        enriched = dict(row)
        if key in evidence:
            enriched["observed_scenario_axes"] = sorted(evidence[key])
            enriched["observed_evidence_source"] = "public_gold_oracle_audit"
            enriched["observed_evidence_rows"] = evidence_rows[key]
            matched += 1
            unmatched_audit_keys.discard(key)
        else:
            # Preserve an explicit empty field for unexecuted families.  This
            # prevents downstream code from falling back to candidate axes.
            # Generator annotations are candidates, not Gold evidence.
            enriched["observed_scenario_axes"] = []
            enriched.setdefault("observed_evidence_source", None)
            enriched.setdefault("observed_evidence_rows", 0)
        rows.append(enriched)

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in sorted(rows, key=lambda item: (_key(item), str(item.get("record_id") or ""))):
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary = {
        "schema_version": 1,
        "source": str(source),
        "audit": str(audit_paths[0]) if len(audit_paths) == 1 else [str(path) for path in audit_paths],
        "output": str(output),
        "hidden_partition_read": False,
        "layer_rows": len(layer_rows),
        "audit_rows": audit_row_count,
        "audit_keys": len(evidence),
        "matched_layer_rows": matched,
        "unmatched_audit_keys": len(unmatched_audit_keys),
        "duplicate_audit_rows": duplicate_evidence,
        "unmatched_audit_key_digest": sorted(
            f"{family}\0{role}" for family, role in unmatched_audit_keys
        ),
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--audit", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)
    result = merge(args.source, args.audit, args.output)
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
