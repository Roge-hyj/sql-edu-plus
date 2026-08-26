from __future__ import annotations

import json
import hashlib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = PROJECT_ROOT / "contracts/phase1_current_implementation.json"
FORMALIZATION_PATH = PROJECT_ROOT / "data_construct_test/outputs/phase1_current_formalization.json"

ALLOWED_STATUSES = {
    "TARGET",
    "IMPLEMENTED",
    "VERIFIED",
    "OUT_OF_SCOPE",
    "ENGINE_GAP",
    "INPUT_GAP",
    "UNDECIDED",
}
REQUIRED_LAYERS = {
    "CFG_PARSER",
    "IR_ASTDIFF",
    "SCHEMA",
    "WITNESS",
    "EXECUTOR",
    "RESOURCES",
    "VERDICT",
    "FEATURE",
    "FREEZE",
}


def _load_contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _resolve_repo_path(value: str) -> Path:
    return PROJECT_ROOT / value


def test_partial_formalization_is_fresh_and_fail_closed():
    contract = _load_contract()
    artifact = json.loads(FORMALIZATION_PATH.read_text(encoding="utf-8"))

    import hashlib

    assert contract["formalization_artifact"] == (
        "data_construct_test/outputs/phase1_current_formalization.json"
    )
    assert artifact["schema_version"] == "phase1.current-formalization.v1"
    assert artifact["formalization_status"] == "PARTIAL_CURRENT_IMPLEMENTATION"
    assert artifact["generated_from"]["current_contract"] == (
        "contracts/phase1_current_implementation.json"
    )
    assert artifact["generated_from"]["current_contract_sha256"] == hashlib.sha256(
        CONTRACT_PATH.read_bytes()
    ).hexdigest()
    assert artifact["resource_bounds"] == contract["resource_policy"]
    assert artifact["verdict_projection"] == contract["verdict_mapping"]
    assert artifact["layer_aliases"] == contract["formalization_layer_aliases"]
    grammar_path = PROJECT_ROOT / contract["cfg_grammar_artifact"]
    grammar = json.loads(grammar_path.read_text(encoding="utf-8"))
    assert artifact["cfg_grammar_artifact"] == contract["cfg_grammar_artifact"]
    assert artifact["cfg_grammar"] == grammar
    assert artifact["cfg_grammar_sha256"] == hashlib.sha256(
        grammar_path.read_bytes()
    ).hexdigest()
    assert artifact["cfg_implementation_boundary"] == contract[
        "cfg_implementation_boundary"
    ]
    assert artifact["formalization_constraints"] == contract["formalization_constraints"]
    # The formal artifact is intentionally partial at the global-SQL level,
    # but the bounded v25 freeze gate is promotable.
    assert artifact["freeze_gate"]["promotion_blocked"] is False
    assert "PARSER_CONSTRAINT" not in artifact["unmapped_layers"]
    assert "ENGINE" not in artifact["unmapped_layers"]
    assert "RESOURCE" not in artifact["unmapped_layers"]
    assert "executor.sqlite_bounded" in artifact["layers"]["ENGINE"]["capability_ids"]
    assert "resources.task_process_isolation" in artifact["layers"]["RESOURCE"]["capability_ids"]
    assert artifact["layers"]["PARSER_CONSTRAINT"]["materialization"] == (
        "derived_formalization_constraints"
    )
    assert artifact["layers"]["PARSER_CONSTRAINT"]["constraint_ids"] == [
        "parser.strict_single_dql",
        "parser.dialect_resolution",
    ]
    assert artifact["layers"]["SCHEMA"]["constraint_ids"] == [
        "schema.native_identifier_spelling",
    ]
    assert artifact["layers"]["ENGINE"]["constraint_ids"] == [
        "engine.mysql8046_linux_identifier_profile",
    ]
    capability_ids = {item["id"] for item in contract["capabilities"]}
    assert artifact["unmapped_layers"] == []
    assert artifact["capability_coverage"]["unmapped_capability_ids"] == []
    assert set(artifact["capability_coverage"]["current_capability_ids"]) == capability_ids
    assert set(artifact["capability_bindings"]) == capability_ids
    for capability_id in capability_ids:
        binding = artifact["capability_bindings"][capability_id]
        current_item = next(item for item in contract["capabilities"] if item["id"] == capability_id)
        assert binding["status"] == current_item["status"]
        assert binding["verification_status"] == current_item["verification_status"]
    grammar_families = set(grammar["feature_family_bindings"])
    bound_families = {
        family
        for binding in artifact["capability_bindings"].values()
        for family in binding.get("feature_families", [])
    }
    assert bound_families == grammar_families
    assert artifact["capability_bindings"]["freeze.pair_generation_scope"]["gate_fields"] == [
        "generation_failures",
        "determinate_label_mismatches",
        "repeat_run_stable",
        "acceptance_pass",
    ]
    for constraint in contract["formalization_constraints"]["PARSER_CONSTRAINT"]:
        assert constraint["source_capability_id"] in capability_ids
        assert constraint["machine_predicate"]
    for layer in ("SCHEMA", "ENGINE"):
        for constraint in contract["formalization_constraints"][layer]:
            assert constraint["source_capability_id"] in capability_ids
            assert constraint["machine_predicate"]


def test_mysql_engine_profile_matches_runtime_identifier_policy():
    contract = _load_contract()
    profile = contract["databases"]["phase1_judge"]["native_engine_profiles"]["mysql"]

    from core import native_engine_runner as native

    assert profile["image"] == "mysql:8.0.46"
    assert profile["server_variables"]["lower_case_table_names"] == (
        native._MYSQL_REQUIRED_LOWER_CASE_TABLE_NAMES
    )
    assert profile["fixture_identifier_policy"] == (
        native._MYSQL_FIXTURE_IDENTIFIER_POLICY
    )


def test_current_implementation_contract_has_stable_shape_and_scope_relation():
    contract = _load_contract()

    assert contract["schema_version"] == "phase1.current-implementation.v1"
    assert contract["contract_kind"] == "CURRENT_IMPLEMENTATION"
    assert contract["scope_model"]["relation"] == (
        "FrozenPairScope ⊆ RunnableScope ⊆ PolicyScope"
    )
    for scope_name in ("PolicyScope", "RunnableScope", "FrozenPairScope"):
        scope = contract["scope_model"][scope_name]
        assert scope["definition"]
        assert scope["machine_predicate"]


def test_current_contract_closes_every_product_target_capability():
    current = _load_contract()
    target = json.loads(
        (PROJECT_ROOT / "contracts/phase1_product_target.json").read_text(
            encoding="utf-8"
        )
    )
    current_by_id = {item["id"]: item for item in current["capabilities"]}
    target_by_id = {item["id"]: item for item in target["capability_targets"]}
    assert set(current_by_id) == set(target_by_id)
    for capability_id, target_item in target_by_id.items():
        current_item = current_by_id[capability_id]
        assert current_item["layer"]
        assert current_item["status"] in ALLOWED_STATUSES
        # Target promotion is intentionally stricter than current status: a
        # target marked VERIFIED must still wait for the current freeze gate.
        if target_item["target_status"] == "OUT_OF_SCOPE":
            assert current_item["status"] == "OUT_OF_SCOPE"


def test_every_capability_has_traceable_code_test_and_evidence():
    contract = _load_contract()
    capabilities = contract["capabilities"]
    ids = [item["id"] for item in capabilities]

    assert capabilities
    assert len(ids) == len(set(ids))
    for item in capabilities:
        assert item["layer"] in REQUIRED_LAYERS
        assert item["status"] in ALLOWED_STATUSES
        assert item["verification_status"] in ALLOWED_STATUSES
        assert item["dialects"]
        assert item["features"]
        assert item["code_entries"]
        assert item["tests"]
        assert item["evidence"]
        assert item["limits"]
        assert item["known_failures"]
        for entry in item["code_entries"]:
            path = _resolve_repo_path(entry["path"])
            assert path.is_file(), (item["id"], entry["path"])
            assert entry["line"] > 0
            assert entry["symbol"]
        for path_value in [*item["tests"], *item["evidence"]]:
            # Evidence may include a markdown anchor after the path.
            path = _resolve_repo_path(path_value.split("#", 1)[0])
            assert path.is_file(), (item["id"], path_value)


def test_current_contract_promotes_bounded_verified_capabilities():
    contract = _load_contract()
    freeze = contract["freeze_baseline"]
    verified = [
        item["id"]
        for item in contract["capabilities"]
        if item["status"] == "VERIFIED"
        or item["verification_status"] == "VERIFIED"
    ]

    assert freeze["generation_failures"] == 0
    assert freeze["determinate_label_mismatches"] == 0
    assert freeze["repeat_run_stable"] is True
    assert freeze["acceptance_pass"] is True
    assert contract["contract_decision"]["overall_status"] == "IMPLEMENTED"
    assert contract["contract_decision"]["overall_verification_status"] == "VERIFIED"
    assert len(verified) == 19
    assert contract["contract_decision"]["verification_scope"] == (
        "bounded Phase 1 implementation and frozen mutation/control pair gate; "
        "not a global SQL equivalence proof"
    )
    assert "executor.vendor_without_explicit_backend" not in verified


def test_resource_policy_matches_runtime_limits():
    contract = _load_contract()
    policy = contract["resource_policy"]

    import core.native_engine_runner as native
    import core.parseval_data_generator as parseval

    assert policy["worker"] == {
        "mode": "process",
        "start_method": "spawn",
        "max_concurrency": 2,
        "queue_limit": 8,
        "queue_timeout_seconds": 5.0,
        "run_timeout_seconds": 45.0,
        "memory_mb": 2048,
        "cpu_seconds": 50,
    }
    assert policy["witness"]["max_worlds"] == parseval._MAX_WITNESS_WORLDS
    assert policy["witness"]["max_attempts"] == parseval._MAX_WITNESS_ATTEMPTS
    assert policy["witness"]["max_rows_per_table"] == parseval._MAX_WITNESS_ROWS_PER_TABLE
    assert policy["witness"]["default_rows_per_table"] == 8
    assert policy["witness"]["max_recorded_result_rows"] == parseval._MAX_RECORDED_RESULT_ROWS
    assert policy["witness"]["max_generate_series_rows"] == parseval._MAX_SQLITE_GENERATE_SERIES_ROWS
    assert policy["sqlite"]["vm_instruction_budget"] == parseval._SQLITE_VM_INSTRUCTION_BUDGET
    assert policy["sqlite"]["progress_granularity"] == parseval._SQLITE_PROGRESS_GRANULARITY
    assert policy["sqlite"]["execution_time_budget_seconds"] == parseval._SQLITE_EXECUTION_TIME_BUDGET_SECONDS
    assert policy["native"]["statement_timeout_ms"] == native._STATEMENT_TIMEOUT_MS
    assert policy["native"]["max_result_rows"] == native._MAX_RESULT_ROWS
    assert policy["native"]["max_result_bytes"] == native._MAX_RESULT_BYTES
    assert policy["scope"]["max_ast_nodes_scanned"] == parseval._MAX_SCOPE_AST_NODES_SCANNED
    assert policy["scope"]["max_nodes"] == parseval._MAX_SCOPE_NODES
    assert policy["scope"]["max_edges"] == parseval._MAX_SCOPE_EDGES
    assert policy["scope"]["max_diffs"] == parseval._MAX_SCOPE_DIFFS
    assert policy["scope"]["max_diff_bindings"] == parseval._MAX_SCOPE_DIFF_BINDINGS
    assert policy["scope"]["max_path_depth"] == parseval._MAX_SCOPE_PATH_DEPTH


def test_known_risk_states_are_explicitly_preserved():
    contract = _load_contract()
    by_id = {item["id"]: item for item in contract["capabilities"]}

    assert by_id["resources.task_process_isolation"]["status"] == "IMPLEMENTED"
    assert (
        by_id["executor.vendor_without_explicit_backend"]["status"]
        == "OUT_OF_SCOPE"
    )
    assert by_id["verdict.api_authoritative_mapping"]["verification_status"] == "VERIFIED"


def test_verdict_contract_matches_runtime_projection_source():
    contract = _load_contract()
    from core.phase1_verdict import FAILURE_PROJECTIONS

    mapping = contract["verdict_mapping"]["production_failure_projection"]
    for internal_status, projection in mapping.items():
        runtime = FAILURE_PROJECTIONS[internal_status]
        assert runtime.status == projection["status"]
        assert runtime.equivalence_conclusion == projection["equivalence_conclusion"]


def test_verdict_mapping_is_machine_readable_and_fail_closed():
    contract = _load_contract()
    mapping = contract["verdict_mapping"]["production_failure_projection"]
    assert mapping["WRONG"] == {
        "status": "SUPPORTED",
        "equivalence_conclusion": "NOT_EQUIVALENT",
    }
    for status in ("INPUT_ERROR", "UNSUPPORTED", "SECURITY_REJECTED", "ENGINE_GAP", "ENGINE_ERROR", "TIMEOUT", "UNDECIDED"):
        assert mapping[status]["equivalence_conclusion"] == "UNDECIDED"
    assert "judge_status == WRONG" in contract["verdict_mapping"]["student_wrong_rule"]


def test_public_progress_evidence_is_traceable_and_never_claims_hidden_read():
    contract = _load_contract()
    evidence = contract["public_progress_evidence"]
    for section in evidence.values():
        assert section["hidden_partition_read"] is False
        for key in ("report", "manifest", "summary", "gold_summary", "production_summary"):
            value = section.get(key)
            if value:
                assert _resolve_repo_path(value).is_file(), value
    assert evidence["mutation_gap_audit"]["parse_failed"] == 0
    assert evidence["public_holdout"]["production_failures"] == []


def test_persisted_public_freeze_pair_regression_matches_contract():
    contract = _load_contract()
    section = contract["public_progress_evidence"]["public_freeze_pair_regression"]
    report_path = _resolve_repo_path(section["report"])
    manifest_path = _resolve_repo_path(section["input_snapshot_manifest"])
    public_path = _resolve_repo_path(section["input_public_snapshot"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert section["hidden_partition_read"] is False
    assert report["mode"] == "public_control_freeze_pair_regression"
    assert report["configuration"]["input_partition"] == "public"
    assert report["configuration"]["oracle_seeds"] == [0, 1, 2]
    assert report["configuration"]["row_scales"] == [4, 8, 16]
    assert report["configuration"]["max_rows_per_table"] == 32
    assert report["configuration"]["pair_builder_salt"] == "phase1-hidden-freeze-v1"
    assert report["configuration"]["input_path"] == str(public_path.relative_to(PROJECT_ROOT))
    assert report["corpus"]["manifest_path"] == str(manifest_path.relative_to(PROJECT_ROOT))
    assert report["corpus"]["snapshot_id"] == manifest["snapshot_id"]
    assert report["corpus"]["manifest_sha256"] == hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()
    assert report["corpus"]["public_file_sha256"] == hashlib.sha256(
        public_path.read_bytes()
    ).hexdigest()

    evaluation = report["public_evaluation"]
    generation = evaluation["pair_generation"]
    first = evaluation["first_run"]
    second = evaluation["second_run"]
    assert evaluation["hidden_partition_read"] is False
    assert evaluation["source_families"] == section["source_families"] == 4889
    assert evaluation["generated_pair_rows"] == section["generated_pair_rows"] == 9778
    assert generation["generation_failures"] == section["generation_failures"] == 0
    assert generation["scope_coverage_rate"] == section["scope_coverage_rate"] == 1.0
    assert first["determinate_label_mismatches"] == 0
    assert second["determinate_label_mismatches"] == 0
    assert evaluation["stable"] is True
    assert report["acceptance"] == {
        "generation_complete": True,
        "no_determinate_label_mismatch": True,
        "public_partition_only": True,
        "repeat_run_stable": True,
        "pass": True,
    }


def test_latest_public_full_replay_matches_recorded_summaries():
    contract = _load_contract()
    latest = contract["public_progress_evidence"]["public_holdout"][
        "latest_public_full_replay"
    ]
    gold = json.loads(_resolve_repo_path(latest["gold_summary"]).read_text(encoding="utf-8"))
    production = json.loads(
        _resolve_repo_path(latest["production_summary"]).read_text(encoding="utf-8")
    )
    mutation_manifest = json.loads(
        _resolve_repo_path(latest["mutation_manifest"]).read_text(encoding="utf-8")
    )

    assert latest["hidden_partition_read"] is False
    assert gold["hidden_partition_read"] is False
    assert mutation_manifest["coverage"]["all_fifteen_families_covered"] is True
    assert mutation_manifest["by_operator_family"]["distinct_removed"] == 1
    assert gold["selected_pairs"] == latest["gold_selected_pairs"] == 2102
    assert gold["verdicts"] == {
        "ENGINE_GAP": latest["gold_engine_gaps"],
        "EQUIVALENT": latest["gold_equivalent_controls"],
        "INPUT_GAP": latest["gold_input_gaps"],
        "NOT_EQUIVALENT": latest["gold_not_equivalent_witnesses"],
    }
    assert gold["verdicts"].get("UNDECIDED", 0) == latest["gold_undecided"] == 0
    assert gold["quality"]["structure_bound_rate"] == latest["gold_structure_bound_rate"] == 1.0
    assert (
        gold["quality"]["atomic_obligation_coverage_rate"]
        == latest["gold_atomic_obligation_coverage_rate"]
        == 1.0
    )
    assert gold["quality"]["labelled_pair_count"] == latest["gold_labelled_pair_count"] == 2102
    assert (
        gold["quality"]["labelled_verdict_match_count"]
        == latest["gold_labelled_verdict_match_count"]
        == 2043
    )
    assert production["hidden_partition_read"] is False
    assert production["selected_families"] == latest["production_selected_families"] == 1051
    assert production["statuses"] == {
        "EXCLUDED": latest["production_excluded"],
        "PASS": latest["production_pass"],
    }
    assert production["failures"] == latest["production_failures"] == []
