from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    PROJECT_ROOT / "data_construct_test/scripts/build_phase1_corpus_universe.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "build_phase1_corpus_universe",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def test_universe_deduplicates_families_and_preserves_provenance(tmp_path):
    module = _load_module()
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    base = {
        "source_id": "tutorial_a",
        "source_url": "https://example.test/tutorial.sql",
        "source_name": "Example tutorial",
        "source_kind": "course_site",
        "dialect": "postgresql",
        "sql": "SELECT name FROM users WHERE age > 18;",
        "schema": "users(id INT PRIMARY KEY, name TEXT, age INT);",
        "schema_trust": "authoritative_source_catalog",
        "replay_eligible": True,
        "cfg_labels": ["select-basic", "where", "where-comp"],
    }
    duplicate = dict(base)
    duplicate["sql"] = "  SELECT name FROM users WHERE age > 18 -- same family\n"
    other = dict(base)
    other["sql"] = "SELECT name FROM users WHERE age >= 18;"
    _write_jsonl(first, [base, other])
    _write_jsonl(second, [duplicate])

    manifest = module.build_universe(
        [first, second],
        tmp_path / "universe",
        captured_at="2026-08-20T00:00:00Z",
        seed=7,
    )

    assert manifest["unique_question_families"] == 2
    assert manifest["total_input_records"] == 3
    assert sum(manifest["partition_counts"].values()) == 2
    assert manifest["schema_version"] == 2
    for partition in ("train", "public", "hidden"):
        path = tmp_path / "universe" / f"{partition}.jsonl"
        assert path.exists()
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            assert record["source_url"] == "https://example.test/tutorial.sql"
            assert record["source_url_status"] == "record_declared"
            assert record["dialect"] == "postgres"
            assert record["provenance"]["captured_at"] == "2026-08-20T00:00:00Z"
            assert record["source_capture_status"] == "snapshot_capture"

    public_manifest = json.loads(
        (tmp_path / "universe/manifest.json").read_text(encoding="utf-8")
    )
    assert "hidden.jsonl" in json.dumps(public_manifest)
    assert "SELECT name FROM users" not in json.dumps(public_manifest)


def test_universe_partition_is_family_stable_when_input_order_changes(tmp_path):
    module = _load_module()
    records = [
        {
            "source_id": "source",
            "source_url": "https://example.test/a",
            "sql": f"SELECT value FROM t WHERE id = {index};",
            "schema": "t(id INT, value TEXT);",
            "cfg_labels": ["select-basic", "where"],
        }
        for index in range(20)
    ]
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    _write_jsonl(first, records)
    _write_jsonl(second, list(reversed(records)))

    first_manifest = module.build_universe(
        [first],
        tmp_path / "one",
        captured_at="2026-08-20T00:00:00Z",
        seed=99,
    )
    second_manifest = module.build_universe(
        [second],
        tmp_path / "two",
        captured_at="2026-08-20T00:00:00Z",
        seed=99,
    )

    assert first_manifest["partition_counts"] == second_manifest["partition_counts"]
    def family_partitions(directory: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        for partition in ("train", "public", "hidden"):
            for line in (directory / f"{partition}.jsonl").read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                result[record["family_id"]] = partition
        return result
    assert family_partitions(tmp_path / "one") == family_partitions(tmp_path / "two")


def test_universe_rejects_invalid_split_ratios(tmp_path):
    module = _load_module()
    source = tmp_path / "source.jsonl"
    _write_jsonl(source, [{"sql": "SELECT 1", "schema": ""}])

    try:
        module.build_universe(
            [source],
            tmp_path / "universe",
            captured_at="2026-08-20T00:00:00Z",
            train_ratio=0.9,
            public_ratio=0.2,
        )
    except ValueError as exc:
        assert "ratios" in str(exc)
    else:
        raise AssertionError("invalid split ratios must be rejected")


def test_universe_rejects_embedded_online_miner_record_payload(tmp_path):
    module = _load_module()
    source = tmp_path / "source.jsonl"
    _write_jsonl(
        source,
        [
            {
                "source_id": "online_miner",
                "sql": 'SELECT", "WHERE"]} {"id": "row-1", "sql": "SELECT id FROM t',
                "schema": "t(id INT);",
            },
            {
                "source_id": "valid_json_literal",
                "sql": "SELECT '{\"id\": 1}' AS payload",
            },
        ],
    )

    manifest = module.build_universe(
        [source],
        tmp_path / "universe",
        captured_at="2026-08-20T00:00:00Z",
        source_manifest=None,
    )

    assert manifest["total_input_records"] == 2
    assert manifest["invalid_input_records"] == 1
    assert manifest["unique_question_families"] == 1
    retained = [
        json.loads(line)
        for partition in ("train", "public", "hidden")
        for line in (tmp_path / "universe" / f"{partition}.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [record["source_id"] for record in retained] == ["valid_json_literal"]


def test_universe_resolves_dialect_from_manifest_before_syntax_fallback(tmp_path):
    module = _load_module()
    source = tmp_path / "source.jsonl"
    manifest = tmp_path / "sources.json"
    _write_jsonl(
        source,
        [
            {
                "source_id": "mysql_course",
                "dialect": "generic",
                "sql": "SELECT name FROM users",
            },
            {
                "source_id": "mysql_course",
                "dialect": "postgres",
                "sql": "SELECT name FROM users WHERE id::int > 1",
            },
            {
                "source_id": "plain_course",
                "sql": "SELECT 1",
            },
        ],
    )
    manifest.write_text(
        json.dumps(
            {
                "sources": [
                    {"id": "mysql_course", "dialect": "mysql"},
                    {"id": "plain_course", "dialect": "generic"},
                ]
            }
        ),
        encoding="utf-8",
    )

    module.build_universe(
        [source],
        tmp_path / "universe",
        captured_at="2026-08-20T00:00:00Z",
        source_manifest=manifest,
    )
    records = []
    for partition in ("train", "public", "hidden"):
        records.extend(
            json.loads(line)
            for line in (tmp_path / "universe" / f"{partition}.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        )
    by_sql = {record["sql"]: record for record in records}
    assert by_sql["SELECT name FROM users"]["dialect"] == "mysql"
    assert by_sql["SELECT name FROM users"]["dialect_source"] == "source_manifest"
    assert "dialect_features" in by_sql["SELECT name FROM users"]["categories"]
    assert "dialect_feature" in by_sql["SELECT name FROM users"]["scenario_axes"]
    assert by_sql["SELECT name FROM users WHERE id::int > 1"]["dialect"] == "postgres"
    assert by_sql["SELECT name FROM users WHERE id::int > 1"]["dialect_source"] == "record_declared"
    assert by_sql["SELECT 1"]["dialect"] == "generic"
    assert by_sql["SELECT 1"]["dialect_source"] == "source_manifest"


def test_universe_preserves_explicit_core_categories(tmp_path):
    module = _load_module()
    source = tmp_path / "source.jsonl"
    _write_jsonl(
        source,
        [{
            "source_id": "synthetic",
            "categories": ["dialect_features"],
            "cfg_labels": ["select-basic"],
            "sql": "SELECT id FROM dialect_rows",
            "schema": "dialect_rows(id INT, value INT)",
        }],
    )
    module.build_universe(
        [source],
        tmp_path / "universe",
        captured_at="2026-08-20T00:00:00Z",
        source_manifest=None,
    )
    record = next(
        json.loads(line)
        for partition in ("train", "public", "hidden")
        for line in (tmp_path / "universe" / f"{partition}.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    assert "dialect_features" in record["categories"]


def test_universe_uses_only_high_confidence_vendor_syntax_inference(tmp_path):
    module = _load_module()
    source = tmp_path / "source.jsonl"
    _write_jsonl(
        source,
        [
            {"sql": "SELECT TOP 1 [name] FROM users"},
            {"sql": "SELECT `name` FROM users"},
            {"sql": "SELECT id::int FROM users"},
            {"sql": "SELECT name FROM users"},
        ],
    )
    module.build_universe(
        [source],
        tmp_path / "universe",
        captured_at="2026-08-20T00:00:00Z",
        source_manifest=None,
    )
    records = []
    for partition in ("train", "public", "hidden"):
        records.extend(
            json.loads(line)
            for line in (tmp_path / "universe" / f"{partition}.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        )
    dialects = {record["sql"]: record["dialect"] for record in records}
    assert dialects["SELECT TOP 1 [name] FROM users"] == "tsql"
    assert dialects["SELECT `name` FROM users"] == "mysql"
    assert dialects["SELECT id::int FROM users"] == "postgres"
    assert dialects["SELECT name FROM users"] == "generic"


def test_universe_uses_explicit_lineage_to_collapse_parameterized_variants(tmp_path):
    module = _load_module()
    source = tmp_path / "source.jsonl"
    lineage = "fixture.case.score_boundary"
    _write_jsonl(
        source,
        [
            {
                "source_id": "fixture",
                "lineage_family_id": lineage,
                "sql": "SELECT CASE WHEN score >= 10 THEN 'pass' ELSE 'fail' END FROM salted_0001",
                "schema": "salted_0001(id INT, score INT)",
            },
            {
                "source_id": "fixture",
                "lineage_family_id": lineage,
                "sql": "SELECT CASE WHEN score >= 20 THEN 'pass' ELSE 'fail' END FROM salted_0002",
                "schema": "salted_0002(id INT, score INT)",
            },
        ],
    )

    manifest = module.build_universe(
        [source],
        tmp_path / "universe",
        captured_at="2026-08-20T00:00:00Z",
        seed=11,
        source_manifest=None,
    )

    assert manifest["total_input_records"] == 2
    assert manifest["unique_question_families"] == 1
    assert manifest["duplicate_input_records"] == 1
    record = next(
        json.loads(line)
        for partition in ("train", "public", "hidden")
        for line in (tmp_path / "universe" / f"{partition}.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    assert record["lineage_family_id"] == lineage
    assert record["family_identity"] == "explicit_lineage"


def test_universe_distinguishes_record_and_manifest_provenance(tmp_path):
    module = _load_module()
    source = tmp_path / "source.jsonl"
    _write_jsonl(
        source,
        [
            {
                "source_id": "manifest_source",
                "sql": "SELECT 1",
                "source_url": "https://example.test/row.sql",
                "source_capture_at": "2026-08-19T00:00:00Z",
            },
            {"source_id": "manifest_source", "sql": "SELECT 2"},
            {"source_id": "unknown_source", "sql": "SELECT 3"},
        ],
    )
    manifest = tmp_path / "sources.json"
    manifest.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "id": "manifest_source",
                        "url": "https://example.test/source.tar.gz",
                        "captured_at": "2026-08-18T00:00:00Z",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    module.build_universe(
        [source],
        tmp_path / "universe",
        captured_at="2026-08-20T00:00:00Z",
        source_manifest=manifest,
        train_ratio=0.8,
        public_ratio=0.1,
    )
    records = [
        json.loads(line)
        for partition in ("train", "public", "hidden")
        for line in (tmp_path / "universe" / f"{partition}.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_sql = {record["sql"]: record for record in records}
    assert by_sql["SELECT 1"]["source_url_status"] == "record_declared"
    assert by_sql["SELECT 1"]["source_capture_status"] == "record_declared"
    assert by_sql["SELECT 2"]["source_url_status"] == "source_manifest"
    assert by_sql["SELECT 2"]["source_capture_status"] == "source_manifest"
    assert by_sql["SELECT 3"]["source_url_status"] == "missing_upstream_record"
    assert by_sql["SELECT 3"]["source_capture_status"] == "snapshot_capture"


def test_scenario_axes_require_execution_evidence_beyond_structure_candidates(tmp_path):
    module = _load_module()
    source = tmp_path / "source.jsonl"
    _write_jsonl(
        source,
        [
            {
                "source_id": "plain",
                "sql": "SELECT id FROM users WHERE id IS NULL",
                "schema": "users(id INT);",
            },
            {
                "source_id": "executed",
                "sql": "SELECT id FROM users WHERE id IS NULL AND id <= 10",
                "schema": "users(id INT, marker TEXT);",
                "executed": True,
                "test_database": {"users": [{"id": None}, {"id": 1}]},
                "standard_row_count": 1,
                "student_row_count": 0,
                "execution_evidence": {
                    "sandbox_executed": True,
                    "standard_duplicate_row_count": 1,
                },
            },
        ],
    )
    module.build_universe(
        [source],
        tmp_path / "universe",
        captured_at="2026-08-20T00:00:00Z",
        source_manifest=None,
    )
    records = []
    for partition in ("train", "public", "hidden"):
        records.extend(
            json.loads(line)
            for line in (tmp_path / "universe" / f"{partition}.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        )
    by_source = {record["source_id"]: record for record in records}
    assert "null" in by_source["plain"]["scenario_candidates"]
    assert "null" not in by_source["plain"]["scenario_axes"]
    assert "null" in by_source["executed"]["scenario_axes"]
    assert "empty_result" in by_source["executed"]["scenario_axes"]
    assert "duplicate_candidate" in by_source["executed"]["scenario_axes"]
