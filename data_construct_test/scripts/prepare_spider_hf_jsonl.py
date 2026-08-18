"""Convert Spider parquet rows with their real ``tables.json`` schemas.

Query-text schema guessing is deliberately forbidden here.  Every generated
record carries the exact Spider physical schema, column types, PKs and FKs so
Phase 1 can distinguish missing schema information from witness-generation
limits.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from spider_schema_catalog import compact_schema, load_spider_catalog

def convert(
    source: Path,
    destination: Path,
    split: str,
    catalog: dict[str, dict],
) -> int:
    import pandas as pd

    frame = pd.read_parquet(source, columns=["db_id", "query", "question"])
    rows: list[str] = []
    for item in frame.to_dict("records"):
        sql = item.get("query")
        if not isinstance(sql, str) or not sql.lstrip().lower().startswith(("select", "with")):
            continue
        db_id = str(item.get("db_id") or "")
        schema_catalog = catalog.get(db_id.lower())
        if schema_catalog is None:
            raise ValueError(f"Spider db_id is missing from tables.json: {db_id!r}")
        rows.append(json.dumps({
            "query": sql,
            "schema": compact_schema(schema_catalog),
            "schema_catalog": schema_catalog,
            "db_id": db_id,
            "question": item.get("question") or "",
            "split": split,
        }, ensure_ascii=False))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", type=Path, required=True)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--tables-json", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    options = parser.parse_args()
    catalog = load_spider_catalog(options.tables_json)
    counts = {
        "train": convert(options.train, options.output_dir / "spider_train.jsonl", "train", catalog),
        "validation": convert(options.validation, options.output_dir / "spider_validation.jsonl", "validation", catalog),
    }
    print(json.dumps(counts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
