"""Contract tests for the Phase 1 product target mode.

These tests validate the target policy itself. They do not promote current
implementation statuses; promotion still requires the freeze runner evidence.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TARGET_PATH = ROOT / "contracts/phase1_product_target.json"
CURRENT_PATH = ROOT / "contracts/phase1_current_implementation.json"
SOURCE_MANIFEST_PATH = ROOT / "data_construct_test/sources/web_sql_corpus_manifest.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_target_contract_is_distinct_from_current_contract_and_covers_same_capabilities():
    target = _load(TARGET_PATH)
    current = _load(CURRENT_PATH)

    assert target["contract_kind"] == "PRODUCT_TARGET"
    assert target["mode"] == "TARGET"
    assert target["current_contract"] == "contracts/phase1_current_implementation.json"
    assert {item["id"] for item in target["capability_targets"]} == {
        item["id"] for item in current["capabilities"]
    }
    assert all(item["target_status"] in {"VERIFIED", "OUT_OF_SCOPE"} for item in target["capability_targets"])
    assert target["promotion_pipeline"] == ["TARGET", "IMPLEMENTED", "VERIFIED"]


def test_target_contract_preserves_fail_closed_scope_and_formalization_layers():
    target = _load(TARGET_PATH)
    boundary = target["support_boundary"]
    formalization = target["formalization"]

    assert boundary["statement_kind"] == "exactly_one_read_only_dql_query"
    assert "DML_or_DDL" in boundary["out_of_scope"]
    assert "multiple_statements" in boundary["out_of_scope"]
    assert "global_equivalence_claim_without_a_valid_oracle_and_execution_evidence" in boundary["out_of_scope"]
    assert formalization["generated_from"].startswith("this_contract.")
    grammar_path = ROOT / formalization["cfg_grammar_artifact"]
    grammar = _load(grammar_path)
    assert grammar["start_symbol"] == "Submission"
    assert grammar["query_start_symbol"] == "Query"
    assert grammar["submission_shape"] == ["Query", "Query", "SchemaText"]
    assert not set(grammar["nonterminals"]) & set(grammar["terminals"])
    production_ids = {item["id"] for item in grammar["productions"]}
    symbols = set(grammar["nonterminals"]) | set(grammar["terminals"])
    assert len(production_ids) == len(grammar["productions"])
    assert all(
        item["lhs"] in grammar["nonterminals"]
        and set(item["rhs"]) <= symbols
        for item in grammar["productions"]
    )
    assert set(grammar["feature_family_bindings"]) == set(
        boundary["allowed_feature_families"]
    )
    assert formalization["required_layers"] == [
        "CFG_PARSER",
        "PARSER_CONSTRAINT",
        "IR_ASTDIFF",
        "FEATURE",
        "SCHEMA",
        "WITNESS",
        "ENGINE",
        "RESOURCE",
        "VERDICT",
        "FREEZE",
    ]
    assert set(formalization["required_predicates"]) == {
        "DeclaredSupport",
        "RunnableSupport",
        "FrozenPairSupport",
    }
    mysql_profile = boundary["engine_profiles"]["mysql"]
    assert mysql_profile["image"] == "mysql:8.0.46"
    assert mysql_profile["server_variables"]["lower_case_table_names"] == 0
    assert mysql_profile["fixture_identifier_policy"] == "preserve_source_spelling"


def test_target_contract_requires_independent_evaluation_and_strict_freeze_gate():
    target = _load(TARGET_PATH)
    data_policy = target["data_evidence_policy"]
    promotion = target["promotion_rules"]

    assert sum(data_policy["split_ratios"].values()) == 1.0
    assert data_policy["split_unit"] == "question_family_with_schema_and_mutation_lineage"
    assert set(data_policy["required_evaluation_slices"]) >= {
        "source_holdout",
        "temporal_holdout",
        "dialect_holdout",
        "schema_holdout",
        "feature_holdout",
        "property_based_synthetic",
    }
    assert "hidden" in data_policy["optimization_forbidden_inputs"]
    assert "no_hidden_partition_read_during_optimization" in promotion["verified_requires"]
    assert "generation_failures == 0" in promotion["verified_requires"]
    assert "determinate_label_mismatches == 0" in promotion["verified_requires"]
    assert "acceptance.pass == true" in promotion["verified_requires"]


def test_enabled_external_sources_have_auditable_provenance_fields():
    manifest = _load(SOURCE_MANIFEST_PATH)
    required = {"id", "captured_at", "license_note", "bias_risk", "extraction"}
    sources = manifest["sources"]
    assert sources
    assert len({source["id"] for source in sources}) == len(sources)
    for source in sources:
        if source.get("enabled"):
            # Reference-only sources may be retained as pointers without a
            # replay snapshot; evidence-bearing sources require capture time.
            base_required = required - {"captured_at"} if source.get("reference_only") else required
            assert base_required <= source.keys(), source["id"]
            if not source.get("reference_only"):
                assert source.get("captured_at"), source["id"]
            locator = source.get("url") or source.get("local_path") or source.get("archive_id")
            assert locator, source["id"]
            if source.get("url"):
                assert source["url"].startswith(("https://", "http://")), source["id"]
            assert source["license_note"].strip(), source["id"]
            assert source["bias_risk"].strip(), source["id"]
            assert isinstance(source["extraction"], dict), source["id"]


def test_product_exclusions_keep_removed_gamification_out_of_target_mode():
    target = _load(TARGET_PATH)
    assert set(target["product_exclusions"]) == {
        "gamification.timed_challenge",
        "gamification.xp",
        "gamification.level",
    }
