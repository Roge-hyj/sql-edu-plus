from __future__ import annotations

from dataclasses import replace

import pytest

from core.error_diagnosis import (
    DIAGNOSIS_VERSION,
    PUBLIC_SCHEMA_VERSION,
    RULE_CATALOG,
    RULE_CATALOG_VERSION,
)
from core.phase3_skill_catalog import (
    ATOMIC_SKILL_TAXONOMY_VERSION,
    PROJECTION_SCHEMA_VERSION,
    ProjectionReasonCode,
    RULE_SKILL_CATALOG,
    RULE_SKILL_MAP,
    RULE_SKILL_MAP_DIGEST,
    RULE_SKILL_MAP_VERSION,
    RuleSkillCatalogError,
    project_phase2_skill_candidates,
    rule_skill_catalog_metadata,
    validate_rule_skill_catalog,
)


def _candidate(
    number: int,
    rule_id: str,
    stage: str,
    *,
    grade: str = "CAUSAL_VERIFIED",
    knowledge_points=None,
):
    return {
        "candidate_id": f"candidate_{number:016x}",
        "rule_id": rule_id,
        "stage": stage,
        "logical_stage": {
            "S1": "SOURCE_JOIN",
            "S2": "ROW_FILTER",
            "S3": "GROUP_AGG",
            "S4": "GROUP_FILTER",
            "S5": "PROJECTION",
            "S6": "ROOT_ORDER",
        }.get(stage, stage),
        "scope_id": "root",
        "knowledge_points": list(knowledge_points or []),
        "evidence_grade": grade,
        "evidence_refs": {
            "diff_ids": [f"diff_{number}"],
            "verified_diff_ids": [f"diff_{number}"],
            "unverified_diff_ids": [],
            "obligation_ids": [],
            "mutation_test_ids": [],
        },
    }


def _package(primary, secondary=(), **overrides):
    payload = {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "diagnosis_version": DIAGNOSIS_VERSION,
        "rule_catalog_version": RULE_CATALOG_VERSION,
        "verdict": "INCORRECT",
        "diagnosis_status": "SUPPORTED",
        "phase1": {
            "status": "SUPPORTED",
            "equivalence_conclusion": "NOT_EQUIVALENT",
            "judge_status": "WRONG",
        },
        "ordered_diff_pipeline": [
            {
                "diff_id": item["evidence_refs"]["diff_ids"][0],
                "evidence_grade": item["evidence_grade"],
            }
            for item in [primary, *secondary]
            if isinstance(item, dict)
            and item.get("evidence_refs", {}).get("diff_ids")
        ],
        "primary": primary,
        "secondary": list(secondary),
        "secondary_count": len(secondary),
    }
    payload.update(overrides)
    return payload


def _reason_codes(projection):
    return [item.reason_code for item in projection.skipped]


def test_catalog_is_a_stable_bijection_over_the_exact_phase2_mvp20():
    assert ATOMIC_SKILL_TAXONOMY_VERSION == "phase3.atomic_sql_skills.v1"
    expected_mapping = {
        "S1_MISSING_BRIDGE": "join.bridge_path",
        "S1_CARTESIAN_PRODUCT": "join.constraint",
        "S1_OUTER_JOIN_MISUSE": "join.outer_preservation",
        "S1_SUBQUERY_CARDINALITY": "subquery.cardinality",
        "S2_BOUNDARY": "filter.boundary",
        "S2_BOOLEAN_LOGIC": "filter.boolean_logic",
        "S2_NULL_LOGIC": "null.three_valued_logic",
        "S2_AGGREGATE_IN_WHERE": "aggregate.filter_placement",
        "S3_GRAIN_ENTITY_MISMATCH": "group.grain",
        "S3_GROUP_KEY_MISSING": "group.key_completeness",
        "S3_GROUP_KEY_REDUNDANT": "group.key_redundancy",
        "S4_HAVING_MISSING": "having.required",
        "S4_AGG_BOUNDARY": "having.aggregate_boundary",
        "S4_ROW_FILTER_IN_HAVING": "filter.stage_placement",
        "S5_FANOUT_AGGREGATE": "aggregate.fanout",
        "S5_COUNT_NULL_SENSITIVITY": "aggregate.count_null",
        "S5_CASE_INCOMPLETE": "projection.case_coverage",
        "S5_TOP_LEVEL_DEDUP": "projection.dedup",
        "S6_TOPN_WITHOUT_ORDER": "result.topn_order",
        "S6_ORDER_OFFSET": "result.order_offset",
    }

    assert RULE_SKILL_MAP_VERSION == "phase3.rule_skill_map.v1"
    assert len(RULE_SKILL_CATALOG) == 20
    assert tuple(item.rule_id for item in RULE_SKILL_CATALOG) == tuple(
        item.rule_id for item in RULE_CATALOG
    )
    assert len({item.skill_id for item in RULE_SKILL_CATALOG}) == 20
    assert {
        item.rule_id: item.skill_id for item in RULE_SKILL_CATALOG
    } == expected_mapping
    assert RULE_SKILL_MAP["S2_BOUNDARY"].skill_id == "filter.boundary"
    assert (
        RULE_SKILL_MAP["S2_NULL_LOGIC"].skill_id
        == "null.three_valued_logic"
    )
    assert (
        RULE_SKILL_MAP["S3_GROUP_KEY_MISSING"].skill_id
        == "group.key_completeness"
    )
    assert RULE_SKILL_MAP["S5_FANOUT_AGGREGATE"].skill_id == "aggregate.fanout"

    metadata = rule_skill_catalog_metadata()
    assert metadata == rule_skill_catalog_metadata()
    assert metadata["digest_sha256"] == RULE_SKILL_MAP_DIGEST
    assert metadata["skill_taxonomy_version"] == ATOMIC_SKILL_TAXONOMY_VERSION
    assert metadata["entry_count"] == 20


def test_catalog_validator_fails_if_phase2_rules_or_stages_drift():
    with pytest.raises(RuleSkillCatalogError, match="match the ordered"):
        validate_rule_skill_catalog(RULE_SKILL_CATALOG[:-1], RULE_CATALOG)

    stage_drift = (
        replace(RULE_SKILL_CATALOG[0], teaching_stage="S2"),
        *RULE_SKILL_CATALOG[1:],
    )
    with pytest.raises(RuleSkillCatalogError, match="stage mismatch"):
        validate_rule_skill_catalog(stage_drift, RULE_CATALOG)

    invalid_skill_id = (
        replace(RULE_SKILL_CATALOG[0], skill_id="not an.atomic.skill"),
        *RULE_SKILL_CATALOG[1:],
    )
    with pytest.raises(RuleSkillCatalogError, match="dotted identifier"):
        validate_rule_skill_catalog(invalid_skill_id, RULE_CATALOG)


def test_projection_uses_rule_mapping_not_candidate_knowledge_point_union():
    primary = _candidate(
        1,
        "S2_BOUNDARY",
        "S2",
        knowledge_points=["attacker.chosen_skill", "aggregate.fanout"],
    )
    duplicate_boundary = _candidate(
        2,
        "S2_BOUNDARY",
        "S2",
        grade="REPAIR_VERIFIED",
        knowledge_points=["another.display.only.value"],
    )
    fanout = _candidate(
        3,
        "S5_FANOUT_AGGREGATE",
        "S5",
        grade="REPAIR_VERIFIED",
        knowledge_points=["filter.boundary"],
    )

    projection = project_phase2_skill_candidates(
        _package(primary, [duplicate_boundary, fanout])
    )

    assert projection.status == "READY_WITH_SKIPS"
    assert [item.skill_id for item in projection.candidates] == [
        "filter.boundary",
        "aggregate.fanout",
    ]
    assert [item.source_role for item in projection.candidates] == [
        "PRIMARY",
        "SECONDARY",
    ]
    assert _reason_codes(projection) == [ProjectionReasonCode.DUPLICATE_SKILL]
    assert "attacker.chosen_skill" not in repr(projection.to_dict())
    assert projection.to_dict()["schema_version"] == PROJECTION_SCHEMA_VERSION
    assert (
        projection.to_dict()["skill_taxonomy_version"]
        == ATOMIC_SKILL_TAXONOMY_VERSION
    )


def test_projection_does_not_even_traverse_knowledge_points():
    class ExplosiveKnowledgePoints:
        def __iter__(self):
            raise AssertionError("candidate.knowledge_points must not be consumed")

        def __repr__(self):
            raise AssertionError("candidate.knowledge_points must not be rendered")

    primary = _candidate(4, "S3_GROUP_KEY_MISSING", "S3")
    primary["knowledge_points"] = ExplosiveKnowledgePoints()

    projection = project_phase2_skill_candidates(_package(primary))

    assert projection.status == "READY"
    assert projection.candidates[0].skill_id == "group.key_completeness"


def test_current_evidence_partition_requires_auditable_pipeline_and_grades():
    missing_pipeline = _candidate(14, "S2_BOUNDARY", "S2")
    missing_pipeline["evidence_refs"].update(
        {"verified_diff_ids": ["diff_14"], "unverified_diff_ids": []}
    )
    missing_pipeline_package = _package(missing_pipeline)
    missing_pipeline_package.pop("ordered_diff_pipeline")
    projection = project_phase2_skill_candidates(missing_pipeline_package)
    assert projection.status == "SKIPPED"
    assert _reason_codes(projection) == [
        ProjectionReasonCode.CANDIDATE_EVIDENCE_SCOPE_INVALID
    ]

    weak_pipeline = _candidate(15, "S2_BOUNDARY", "S2")
    weak_pipeline["evidence_refs"].update(
        {"verified_diff_ids": ["diff_15"], "unverified_diff_ids": []}
    )
    projection = project_phase2_skill_candidates(
        _package(
            weak_pipeline,
            ordered_diff_pipeline=[
                {"diff_id": "diff_15", "evidence_grade": "PAIR_DISTINGUISHED"}
            ],
        )
    )
    assert projection.status == "SKIPPED"
    assert _reason_codes(projection) == [
        ProjectionReasonCode.CANDIDATE_EVIDENCE_SCOPE_INVALID
    ]

    weak_unverified = _candidate(16, "S2_BOUNDARY", "S2")
    weak_unverified["evidence_refs"].update(
        {
            "diff_ids": ["diff_16", "diff_16_weak"],
            "verified_diff_ids": ["diff_16"],
            "unverified_diff_ids": ["diff_16_weak"],
        }
    )
    projection = project_phase2_skill_candidates(
        _package(
            weak_unverified,
            ordered_diff_pipeline=[
                {"diff_id": "diff_16", "evidence_grade": "CAUSAL_VERIFIED"},
                {"diff_id": "diff_16_weak", "evidence_grade": "PAIR_DISTINGUISHED"},
            ],
        )
    )
    assert projection.status == "READY"

    grade_mismatch = _candidate(17, "S2_BOUNDARY", "S2", grade="REPAIR_VERIFIED")
    grade_mismatch["evidence_refs"].update(
        {"verified_diff_ids": ["diff_17"], "unverified_diff_ids": []}
    )
    projection = project_phase2_skill_candidates(
        _package(
            grade_mismatch,
            ordered_diff_pipeline=[
                {"diff_id": "diff_17", "evidence_grade": "CAUSAL_VERIFIED"}
            ],
        )
    )
    assert projection.status == "SKIPPED"
    assert _reason_codes(projection) == [
        ProjectionReasonCode.CANDIDATE_EVIDENCE_SCOPE_INVALID
    ]


def test_partial_diagnosis_is_skipped_before_candidates_are_read():
    primary = _candidate(5, "S2_NULL_LOGIC", "S2")
    primary["knowledge_points"] = object()
    projection = project_phase2_skill_candidates(
        _package(primary, diagnosis_status="PARTIAL")
    )

    assert projection.status == "SKIPPED"
    assert projection.candidates == ()
    assert _reason_codes(projection) == [
        ProjectionReasonCode.PHASE2_DIAGNOSIS_PARTIAL
    ]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"schema_version": "phase2.public.v999"},
            ProjectionReasonCode.PHASE2_SCHEMA_VERSION_UNSUPPORTED,
        ),
        (
            {"diagnosis_version": "phase2-mvp-future"},
            ProjectionReasonCode.PHASE2_DIAGNOSIS_VERSION_UNSUPPORTED,
        ),
        (
            {"rule_catalog_version": "phase2.rules.future"},
            ProjectionReasonCode.PHASE2_RULE_CATALOG_VERSION_UNSUPPORTED,
        ),
        (
            {"verdict": "CORRECT", "diagnosis_status": "OPERATIONALLY_ACCEPTED"},
            ProjectionReasonCode.PHASE2_VERDICT_NOT_INCORRECT,
        ),
    ],
)
def test_unknown_contract_or_nonincorrect_verdict_fails_closed(overrides, reason):
    projection = project_phase2_skill_candidates(
        _package(_candidate(6, "S2_BOUNDARY", "S2"), **overrides)
    )

    assert projection.status == "SKIPPED"
    assert projection.candidates == ()
    assert _reason_codes(projection) == [reason]


def test_weak_evidence_and_unknown_rules_are_individually_skipped():
    primary = _candidate(7, "S2_BOUNDARY", "S2")
    weak = _candidate(
        8,
        "S2_BOOLEAN_LOGIC",
        "S2",
        grade="PAIR_DISTINGUISHED",
    )
    unknown = _candidate(9, "S9_FUTURE_RULE", "S9")

    projection = project_phase2_skill_candidates(_package(primary, [weak, unknown]))

    assert projection.status == "READY_WITH_SKIPS"
    assert [item.skill_id for item in projection.candidates] == ["filter.boundary"]
    assert _reason_codes(projection) == [
        ProjectionReasonCode.CANDIDATE_EVIDENCE_NOT_STRONG,
        ProjectionReasonCode.CANDIDATE_RULE_UNKNOWN,
    ]


def test_invalid_primary_fails_closed_before_secondary_projection():
    weak_primary = _candidate(
        7,
        "S2_BOOLEAN_LOGIC",
        "S2",
        grade="PAIR_DISTINGUISHED",
    )
    valid_secondary = _candidate(8, "S2_NULL_LOGIC", "S2")

    projection = project_phase2_skill_candidates(
        _package(weak_primary, [valid_secondary])
    )

    assert projection.status == "SKIPPED"
    assert projection.candidates == ()
    assert _reason_codes(projection) == [
        ProjectionReasonCode.CANDIDATE_EVIDENCE_NOT_STRONG,
    ]


def test_stage_or_secondary_shape_tampering_fails_closed_with_fixed_reasons():
    wrong_stage = _candidate(10, "S2_BOUNDARY", "S5")
    projection = project_phase2_skill_candidates(_package(wrong_stage))
    assert _reason_codes(projection) == [
        ProjectionReasonCode.CANDIDATE_STAGE_MISMATCH
    ]

    malformed = _package(_candidate(11, "S2_BOUNDARY", "S2"))
    malformed["secondary"] = {"not": "a ranked root list"}
    projection = project_phase2_skill_candidates(malformed)
    assert _reason_codes(projection) == [
        ProjectionReasonCode.PHASE2_SECONDARY_INVALID
    ]


def test_observation_candidate_ids_and_serialization_are_replay_stable():
    payload = _package(
        _candidate(12, "S2_NULL_LOGIC", "S2", grade="REPAIR_VERIFIED"),
        [_candidate(13, "S6_ORDER_OFFSET", "S6")],
    )

    first = project_phase2_skill_candidates(payload).to_dict()
    second = project_phase2_skill_candidates(payload).to_dict()

    assert first == second
    assert [item["skill_id"] for item in first["candidates"]] == [
        "null.three_valued_logic",
        "result.order_offset",
    ]
    assert all(
        item["proposed_observation"] == "INCORRECT"
        for item in first["candidates"]
    )
