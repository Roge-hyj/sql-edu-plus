"""Attach the authoritative Spider physical catalog to an existing JSONL.

Older cached Spider rows predate the catalog gate and retain only a comment
such as ``-- spider_db_id: department_management``.  This command repairs
those development records from the official ``tables.json`` snapshot.  It
never derives a schema from SQL identifiers and it refuses unknown database
ids instead of silently downgrading them.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from spider_schema_catalog import compact_schema, load_spider_catalog  # noqa: E402


DB_ID_RE = re.compile(r"(?im)^\s*--\s*spider_db_id\s*:\s*([^\s]+)\s*$")


def _db_id(record: dict[str, object]) -> str:
    for key in ("db_id", "database_id"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    match = DB_ID_RE.search(str(record.get("schema") or ""))
    return match.group(1).strip() if match else ""


def enrich(source: Path, destination: Path, tables_json: Path) -> dict[str, object]:
    catalog = load_spider_catalog(tables_json)
    counts = {"records": 0, "spider_records": 0, "enriched": 0, "already_authoritative": 0}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("r", encoding="utf-8") as reader, destination.open(
        "w", encoding="utf-8", newline="\n"
    ) as writer:
        for line_number, line in enumerate(reader, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                continue
            counts["records"] += 1
            source_id = str(record.get("source_id") or "").lower()
            if not source_id.startswith("spider_"):
                writer.write(json.dumps(record, ensure_ascii=False) + "\n")
                continue
            counts["spider_records"] += 1
            db_id = _db_id(record)
            entry = catalog.get(db_id.lower())
            if entry is None:
                raise ValueError(
                    f"Spider record at {source}:{line_number} has no tables.json entry for {db_id!r}"
                )
            if record.get("schema_catalog"):
                counts["already_authoritative"] += 1
            else:
                counts["enriched"] += 1
            record.update({
                "db_id": entry["db_id"],
                "schema": compact_schema(entry),
                "schema_catalog": entry,
                "schema_trust": "authoritative_source_catalog",
                "replay_eligible": True,
                "schema_recovery": "official_spider_tables_json_v1",
            })
            writer.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"source": str(source), "destination": str(destination), "tables_json": str(tables_json), **counts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tables-json", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(enrich(args.input, args.output, args.tables_json), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
