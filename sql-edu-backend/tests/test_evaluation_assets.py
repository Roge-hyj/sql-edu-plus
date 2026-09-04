from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_PATH = REPO_ROOT / "evaluation/cases/sqlite_phase12_verified.json"
BASELINE_PATH = REPO_ROOT / "evaluation/baselines/sqlite_phase12_baseline.json"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_curated_dataset_is_unique_bounded_and_declares_its_limits():
    payload = _load(DATASET_PATH)
    cases = payload["cases"]
    contexts = payload["contexts"]

    assert payload["format_version"] == 1
    assert payload["engine"] == "sqlite"
    assert len(cases) == 79
    assert payload["declared_counts"] == {
        "equivalent_control": 10,
        "fail_closed": 4,
        "phase1_operator": 19,
        "public_reference_mutation": 12,
        "teaching_core": 34,
    }
    assert len({case["id"] for case in cases}) == len(cases)

    triples = set()
    for case in cases:
        context = contexts.get(case.get("context_id"), {})
        schema = context.get("schema_text", case.get("schema_text", ""))
        triple = tuple(
            " ".join(value.split()).casefold()
            for value in (schema, case["reference_sql"], case["student_sql"])
        )
        assert triple not in triples
        triples.add(triple)
        assert max(len(value) for value in triple) <= 16_384

    exclusions = payload["selection_policy"]["known_rule_exclusions"]
    assert set(exclusions) == {"S1_MISSING_BRIDGE", "S5_FANOUT_AGGREGATE"}


def test_teaching_and_public_subsets_do_not_overstate_provenance():
    payload = _load(DATASET_PATH)
    cases = payload["cases"]
    teaching = [case for case in cases if case["suite"] == "teaching_core"]
    public = [
        case for case in cases
        if case["suite"] == "public_reference_mutation"
    ]

    assert len({case["family"] for case in teaching}) == 18
    for case in teaching:
        expected = case["expectation"]
        assert expected["require_phase1_witness"] is True
        assert expected["require_phase2_evidence"] is True
        assert expected["require_repair"] is True
        assert expected["require_safe_hints"] is True

    assert len(public) == 12
    assert len({case["source"]["url"] for case in public}) == 10
    for case in public:
        assert case["source"]["kind"] == "public_reference_synthetic_mutation"
        assert case["source"]["student_generation_method"] == "deterministic_mutation"
        assert case["source"]["url"].startswith(("https://github.com/", "https://www.postgresql.org/"))

        sql = f" {case['reference_sql']} {case['student_sql']} ".casefold()
        for marker in (
            "@", "dateadd(", "getdate(", "distinct on", "::", " ilike ",
            "interval ", "extract(", "generate_series(", " search breadth ",
        ):
            assert marker not in sql


def test_committed_baseline_matches_dataset_and_has_no_failures():
    raw = DATASET_PATH.read_bytes()
    dataset = json.loads(raw)
    baseline = _load(BASELINE_PATH)

    assert baseline["dataset"] == dataset["name"]
    assert baseline["dataset_sha256"] == hashlib.sha256(raw).hexdigest()
    assert baseline["engine"] == "sqlite"
    assert baseline["summary"]["cases"] == len(dataset["cases"])
    assert baseline["summary"]["passed"] == len(dataset["cases"])
    assert baseline["summary"]["failed"] == 0
    assert baseline["summary"]["determinism_mismatches"] == 0
    assert Counter(row["id"] for row in baseline["results"]) == Counter(
        case["id"] for case in dataset["cases"]
    )
