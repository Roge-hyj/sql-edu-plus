"""Three-way acceptance matrix for the Phase 2 twenty-rule MVP.

Each declared rule owns three contracts:

* sufficient atomic evidence selects that rule as the blocking primary;
* a neighbouring, causally verified fault must not be labelled as that rule;
* the same structural signal without atomic evidence remains unresolved.

The fixtures intentionally model the rich Phase 1 boundary rather than calling
private Phase 2 helpers.  A failure here is an acceptance gap in the public
``diagnose_record`` contract; the expectations must not be weakened merely to
match an incomplete classifier.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from core.error_diagnosis import RULE_CATALOG, DiagnosticCandidate, diagnose_record


@dataclass(frozen=True)
class RuleCase:
    rule_id: str
    stage: str
    scope: str
    clause: str
    diff_type: str
    standard_fragment: str
    student_fragment: str
    standard_sql: str
    student_sql: str
    diff_extra: Mapping[str, Any] = field(default_factory=dict)
    evidence_extra: Mapping[str, Any] = field(default_factory=dict)
    schema: Mapping[str, Any] = field(default_factory=dict)
    additional_diffs: tuple[Mapping[str, Any], ...] = ()

    @property
    def slug(self) -> str:
        return self.rule_id.lower()

    @property
    def diff_id(self) -> str:
        return f"diff_matrix_{self.slug}"

    @property
    def obligation_id(self) -> str:
        return f"obligation_matrix_{self.slug}"

    def diff(self) -> dict[str, Any]:
        return {
            "diff_id": self.diff_id,
            "obligation_id": self.obligation_id,
            "query_scope": self.scope,
            "clause": self.clause,
            "diff_type": self.diff_type,
            "standard_sql": self.standard_fragment,
            "student_sql": self.student_fragment,
            "knowledge_point_id": f"kp-{self.slug}",
            "severity": 0.8,
            **dict(self.diff_extra),
        }

    def all_diffs(self) -> tuple[dict[str, Any], ...]:
        return (self.diff(), *(dict(item) for item in self.additional_diffs))

    @property
    def expected_diff_ids(self) -> tuple[str, ...]:
        return tuple(sorted(str(item["diff_id"]) for item in self.all_diffs()))

    @property
    def expected_obligation_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                str(item["obligation_id"])
                for item in self.all_diffs()
                if item.get("obligation_id")
            )
        )


def _case(
    rule_id: str,
    stage: str,
    clause: str,
    diff_type: str,
    standard_fragment: str,
    student_fragment: str,
    *,
    scope: str = "root",
    standard_sql: str = "SELECT id FROM sample",
    student_sql: str = "SELECT id FROM sample",
    diff_extra: Mapping[str, Any] | None = None,
    evidence_extra: Mapping[str, Any] | None = None,
    schema: Mapping[str, Any] | None = None,
    additional_diffs: tuple[Mapping[str, Any], ...] = (),
) -> RuleCase:
    return RuleCase(
        rule_id=rule_id,
        stage=stage,
        scope=scope,
        clause=clause,
        diff_type=diff_type,
        standard_fragment=standard_fragment,
        student_fragment=student_fragment,
        standard_sql=standard_sql,
        student_sql=student_sql,
        diff_extra=diff_extra or {},
        evidence_extra=evidence_extra or {},
        schema=schema or {},
        additional_diffs=additional_diffs,
    )


RULE_CASES: dict[str, RuleCase] = {
    "S1_MISSING_BRIDGE": _case(
        "S1_MISSING_BRIDGE",
        "S1",
        "JOIN",
        "join_missing",
        "JOIN takes t ON s.s_id = t.s_id",
        "",
        standard_sql=(
            "SELECT DISTINCT s.s_name FROM student s "
            "JOIN takes t ON s.s_id = t.s_id "
            "JOIN course c ON t.course_id = c.course_id"
        ),
        student_sql=(
            "SELECT s.s_name FROM student s "
            "JOIN course c ON s.dept_name = c.dept_name"
        ),
        diff_extra={"table": "takes"},
        schema={
            "tables": [
                {
                    "name": "student",
                    "columns": [
                        {"name": "s_id", "type": "INTEGER", "nullable": False},
                        {"name": "s_name", "type": "TEXT", "nullable": False},
                        {"name": "dept_name", "type": "TEXT", "nullable": True},
                    ],
                    "primary_key": ["s_id"],
                },
                {
                    "name": "course",
                    "columns": [
                        {"name": "course_id", "type": "TEXT", "nullable": False},
                        {"name": "dept_name", "type": "TEXT", "nullable": True},
                    ],
                    "primary_key": ["course_id"],
                },
                {
                    "name": "takes",
                    "columns": [
                        {"name": "s_id", "type": "INTEGER", "nullable": False},
                        {"name": "course_id", "type": "TEXT", "nullable": False},
                    ],
                    "foreign_keys": [
                        {
                            "column": "s_id",
                            "references_table": "student",
                            "references_column": "s_id",
                        },
                        {
                            "column": "course_id",
                            "references_table": "course",
                            "references_column": "course_id",
                        },
                    ],
                },
            ]
        },
    ),
    "S1_CARTESIAN_PRODUCT": _case(
        "S1_CARTESIAN_PRODUCT",
        "S1",
        "JOIN ON",
        "join_on_changed",
        "i.dept_name = d.dept_name",
        "",
        standard_sql=(
            "SELECT i.i_name, d.building FROM instructor i "
            "JOIN department d ON i.dept_name = d.dept_name"
        ),
        student_sql="SELECT i.i_name, d.building FROM instructor i, department d",
        evidence_extra={"suspected_cartesian_product": True},
    ),
    "S1_OUTER_JOIN_MISUSE": _case(
        "S1_OUTER_JOIN_MISUSE",
        "S1",
        "JOIN",
        "join_type_changed",
        "LEFT JOIN takes t ON s.s_id = t.s_id",
        "JOIN takes t ON s.s_id = t.s_id",
        standard_sql=(
            "SELECT s.s_name, COUNT(t.course_id) FROM student s "
            "LEFT JOIN takes t ON s.s_id = t.s_id GROUP BY s.s_id, s.s_name"
        ),
        student_sql=(
            "SELECT s.s_name, COUNT(t.course_id) FROM student s "
            "JOIN takes t ON s.s_id = t.s_id GROUP BY s.s_id, s.s_name"
        ),
    ),
    "S1_SUBQUERY_CARDINALITY": _case(
        "S1_SUBQUERY_CARDINALITY",
        "S1",
        "PREDICATE",
        "comparison_operator_changed",
        "credits IN (SELECT credits FROM course WHERE dept_name = 'Physics')",
        "credits = (SELECT credits FROM course WHERE dept_name = 'Physics')",
        scope="subquery:cardinality",
        standard_sql=(
            "SELECT title FROM course WHERE credits IN "
            "(SELECT credits FROM course WHERE dept_name = 'Physics')"
        ),
        student_sql=(
            "SELECT title FROM course WHERE credits = "
            "(SELECT credits FROM course WHERE dept_name = 'Physics')"
        ),
        diff_extra={
            "standard_op": "IN",
            "student_op": "EQ",
            "standard_value_kind": "expression",
            "student_value_kind": "expression",
        },
    ),
    "S2_BOUNDARY": _case(
        "S2_BOUNDARY",
        "S2",
        "WHERE",
        "comparison_operator_changed",
        "credits > 3",
        "credits >= 3",
        standard_sql="SELECT title FROM course WHERE credits > 3",
        student_sql="SELECT title FROM course WHERE credits >= 3",
        diff_extra={"standard_op": "GT", "student_op": "GTE", "column": "credits"},
    ),
    "S2_BOOLEAN_LOGIC": _case(
        "S2_BOOLEAN_LOGIC",
        "S2",
        "WHERE",
        "logical_precedence_tree_changed",
        "(dept_name = 'CS' OR dept_name = 'Math') AND salary > 80000",
        "dept_name = 'CS' OR dept_name = 'Math' AND salary > 80000",
        scope="cte:eligible",
        standard_sql=(
            "SELECT i_name FROM instructor WHERE "
            "(dept_name = 'CS' OR dept_name = 'Math') AND salary > 80000"
        ),
        student_sql=(
            "SELECT i_name FROM instructor WHERE "
            "dept_name = 'CS' OR dept_name = 'Math' AND salary > 80000"
        ),
    ),
    "S2_NULL_LOGIC": _case(
        "S2_NULL_LOGIC",
        "S2",
        "WHERE",
        "null_predicate_negation_changed",
        "grade IS NULL",
        "grade IS NOT NULL",
        scope="subquery:null_guard",
        standard_sql="SELECT * FROM takes WHERE grade IS NULL",
        student_sql="SELECT * FROM takes WHERE grade IS NOT NULL",
        diff_extra={"column": "grade"},
    ),
    "S2_AGGREGATE_IN_WHERE": _case(
        "S2_AGGREGATE_IN_WHERE",
        "S2",
        "WHERE",
        "aggregate_condition_in_where",
        "salary > (SELECT AVG(salary) FROM instructor)",
        "salary > AVG(salary)",
        standard_sql=(
            "SELECT i_name FROM instructor WHERE salary > "
            "(SELECT AVG(salary) FROM instructor)"
        ),
        student_sql="SELECT i_name FROM instructor WHERE salary > AVG(salary)",
    ),
    "S3_GRAIN_ENTITY_MISMATCH": _case(
        "S3_GRAIN_ENTITY_MISMATCH",
        "S3",
        "GROUP BY",
        "group_by_expression_changed",
        "d.dept_name",
        "d.building",
        scope="derived:department_stats",
        standard_sql=(
            "SELECT d.dept_name, COUNT(i.i_id) FROM instructor i "
            "JOIN department d ON i.dept_name = d.dept_name GROUP BY d.dept_name"
        ),
        student_sql=(
            "SELECT d.dept_name, COUNT(i.i_id) FROM instructor i "
            "JOIN department d ON i.dept_name = d.dept_name GROUP BY d.building"
        ),
    ),
    "S3_GROUP_KEY_MISSING": _case(
        "S3_GROUP_KEY_MISSING",
        "S3",
        "GROUP BY",
        "grouping_grain_too_coarse",
        "dept_name, semester",
        "dept_name",
        standard_sql=(
            "SELECT dept_name, semester, COUNT(course_id) FROM takes t "
            "JOIN course c ON t.course_id = c.course_id GROUP BY dept_name, semester"
        ),
        student_sql=(
            "SELECT dept_name, semester, COUNT(course_id) FROM takes t "
            "JOIN course c ON t.course_id = c.course_id GROUP BY dept_name"
        ),
    ),
    "S3_GROUP_KEY_REDUNDANT": _case(
        "S3_GROUP_KEY_REDUNDANT",
        "S3",
        "GROUP BY",
        "grouping_grain_too_fine",
        "s.s_id, s.s_name",
        "s.s_id, s.s_name, t.grade",
        standard_sql=(
            "SELECT s.s_id, s.s_name, COUNT(t.course_id) FROM student s "
            "JOIN takes t ON s.s_id = t.s_id GROUP BY s.s_id, s.s_name"
        ),
        student_sql=(
            "SELECT s.s_id, s.s_name, COUNT(t.course_id) FROM student s "
            "JOIN takes t ON s.s_id = t.s_id GROUP BY s.s_id, s.s_name, t.grade"
        ),
    ),
    "S4_HAVING_MISSING": _case(
        "S4_HAVING_MISSING",
        "S4",
        "HAVING",
        "predicate_missing",
        "COUNT(i_id) > 5",
        "",
        standard_sql=(
            "SELECT dept_name FROM instructor GROUP BY dept_name HAVING COUNT(i_id) > 5"
        ),
        student_sql="SELECT dept_name FROM instructor GROUP BY dept_name",
    ),
    "S4_AGG_BOUNDARY": _case(
        "S4_AGG_BOUNDARY",
        "S4",
        "HAVING",
        "comparison_operator_changed",
        "COUNT(course_id) >= 2",
        "COUNT(course_id) > 2",
        standard_sql=(
            "SELECT dept_name FROM course GROUP BY dept_name HAVING COUNT(course_id) >= 2"
        ),
        student_sql=(
            "SELECT dept_name FROM course GROUP BY dept_name HAVING COUNT(course_id) > 2"
        ),
        diff_extra={"standard_op": "GTE", "student_op": "GT", "column": "course_id"},
    ),
    "S4_ROW_FILTER_IN_HAVING": _case(
        "S4_ROW_FILTER_IN_HAVING",
        "S4",
        "WHERE",
        "where_changed",
        "WHERE dept_name = 'Comp. Sci.'",
        "",
        standard_sql=(
            "SELECT dept_name, AVG(salary) FROM instructor "
            "WHERE dept_name = 'Comp. Sci.' GROUP BY dept_name"
        ),
        student_sql=(
            "SELECT dept_name, AVG(salary) FROM instructor GROUP BY dept_name "
            "HAVING dept_name = 'Comp. Sci.'"
        ),
        additional_diffs=(
            {
                "diff_id": "diff_matrix_s4_row_filter_in_having_target",
                "obligation_id": "obligation_matrix_s4_row_filter_in_having_target",
                "query_scope": "root",
                "clause": "HAVING",
                "diff_type": "having_changed",
                "standard_sql": "",
                "student_sql": "HAVING dept_name = 'Comp. Sci.'",
                "knowledge_point_id": "kp-s4-row-filter-in-having",
                "severity": 0.8,
            },
        ),
    ),
    "S5_FANOUT_AGGREGATE": _case(
        "S5_FANOUT_AGGREGATE",
        "S5",
        "SELECT",
        "aggregate_distinct_changed",
        "COUNT(DISTINCT s.s_id)",
        "COUNT(s.s_id)",
        standard_sql=(
            "SELECT s.dept_name, COUNT(DISTINCT s.s_id) FROM student s "
            "JOIN takes t ON s.s_id = t.s_id GROUP BY s.dept_name"
        ),
        student_sql=(
            "SELECT s.dept_name, COUNT(s.s_id) FROM student s "
            "JOIN takes t ON s.s_id = t.s_id GROUP BY s.dept_name"
        ),
        diff_extra={
            "table": "student",
            "column": "s_id",
            "standard_aggregate_function": "COUNT",
            "student_aggregate_function": "COUNT",
            "standard_aggregate_distinct": True,
            "student_aggregate_distinct": False,
        },
        schema={
            "tables": [
                {
                    "name": "student",
                    "columns": [
                        {"name": "s_id", "type": "INTEGER", "nullable": False},
                        {"name": "dept_name", "type": "TEXT", "nullable": False},
                    ],
                    "primary_key": ["s_id"],
                },
                {
                    "name": "takes",
                    "columns": [
                        {"name": "s_id", "type": "INTEGER", "nullable": False},
                        {"name": "course_id", "type": "TEXT", "nullable": False},
                    ],
                    "foreign_keys": [
                        {
                            "column": "s_id",
                            "references_table": "student",
                            "references_column": "s_id",
                        }
                    ],
                },
            ]
        },
    ),
    "S5_COUNT_NULL_SENSITIVITY": _case(
        "S5_COUNT_NULL_SENSITIVITY",
        "S5",
        "SELECT",
        "aggregate_argument_changed",
        "COUNT(bonus)",
        "COUNT(*)",
        standard_sql="SELECT dept_name, COUNT(bonus) FROM instructor GROUP BY dept_name",
        student_sql="SELECT dept_name, COUNT(*) FROM instructor GROUP BY dept_name",
        diff_extra={"standard_source_table": "instructor", "column": "bonus"},
        schema={
            "tables": [
                {
                    "name": "instructor",
                    "columns": [
                        {"name": "dept_name", "type": "TEXT", "nullable": False},
                        {"name": "bonus", "type": "INTEGER", "nullable": True},
                    ],
                }
            ]
        },
    ),
    "S5_CASE_INCOMPLETE": _case(
        "S5_CASE_INCOMPLETE",
        "S5",
        "CASE",
        "case_else_missing",
        "CASE WHEN salary > 80000 THEN 'High' ELSE 'Normal' END",
        "CASE WHEN salary > 80000 THEN 'High' END",
        standard_sql=(
            "SELECT i_name, CASE WHEN salary > 80000 THEN 'High' "
            "ELSE 'Normal' END AS salary_level FROM instructor"
        ),
        student_sql=(
            "SELECT i_name, CASE WHEN salary > 80000 THEN 'High' "
            "END AS salary_level FROM instructor"
        ),
    ),
    "S5_TOP_LEVEL_DEDUP": _case(
        "S5_TOP_LEVEL_DEDUP",
        "S5",
        "DISTINCT",
        "distinct_changed",
        "TRUE",
        "FALSE",
        standard_sql="SELECT DISTINCT dept_name FROM instructor WHERE salary > 80000",
        student_sql="SELECT dept_name FROM instructor WHERE salary > 80000",
    ),
    "S6_TOPN_WITHOUT_ORDER": _case(
        "S6_TOPN_WITHOUT_ORDER",
        "S6",
        "ORDER BY",
        "top_n_ordering_missing",
        "salary DESC",
        "",
        standard_sql=(
            "SELECT i_name, salary FROM instructor ORDER BY salary DESC LIMIT 3"
        ),
        student_sql="SELECT i_name, salary FROM instructor LIMIT 3",
    ),
    "S6_ORDER_OFFSET": _case(
        "S6_ORDER_OFFSET",
        "S6",
        "OFFSET",
        "limit_changed",
        "OFFSET 1",
        "OFFSET 2",
        standard_sql=(
            "SELECT i_name FROM instructor ORDER BY salary DESC LIMIT 1 OFFSET 1"
        ),
        student_sql=(
            "SELECT i_name FROM instructor ORDER BY salary DESC LIMIT 1 OFFSET 2"
        ),
    ),
}


NEGATIVE_NEIGHBOUR: dict[str, str] = {
    "S1_MISSING_BRIDGE": "S1_CARTESIAN_PRODUCT",
    "S1_CARTESIAN_PRODUCT": "S1_OUTER_JOIN_MISUSE",
    "S1_OUTER_JOIN_MISUSE": "S1_SUBQUERY_CARDINALITY",
    "S1_SUBQUERY_CARDINALITY": "S2_BOUNDARY",
    "S2_BOUNDARY": "S2_BOOLEAN_LOGIC",
    "S2_BOOLEAN_LOGIC": "S2_NULL_LOGIC",
    "S2_NULL_LOGIC": "S2_AGGREGATE_IN_WHERE",
    "S2_AGGREGATE_IN_WHERE": "S2_BOUNDARY",
    "S3_GRAIN_ENTITY_MISMATCH": "S3_GROUP_KEY_MISSING",
    "S3_GROUP_KEY_MISSING": "S3_GROUP_KEY_REDUNDANT",
    "S3_GROUP_KEY_REDUNDANT": "S3_GRAIN_ENTITY_MISMATCH",
    "S4_HAVING_MISSING": "S4_AGG_BOUNDARY",
    "S4_AGG_BOUNDARY": "S4_ROW_FILTER_IN_HAVING",
    "S4_ROW_FILTER_IN_HAVING": "S4_HAVING_MISSING",
    "S5_FANOUT_AGGREGATE": "S5_COUNT_NULL_SENSITIVITY",
    "S5_COUNT_NULL_SENSITIVITY": "S5_CASE_INCOMPLETE",
    "S5_CASE_INCOMPLETE": "S5_TOP_LEVEL_DEDUP",
    "S5_TOP_LEVEL_DEDUP": "S5_CASE_INCOMPLETE",
    "S6_TOPN_WITHOUT_ORDER": "S6_ORDER_OFFSET",
    "S6_ORDER_OFFSET": "S6_TOPN_WITHOUT_ORDER",
}


def _sandbox_run(case: RuleCase, *, sufficient: bool) -> SimpleNamespace:
    diffs = case.all_diffs()
    effects = []
    if sufficient:
        effects.extend(
            {
                "diff_id": str(diff["diff_id"]),
                "obligation_id": str(diff["obligation_id"]),
                "world_id": "world_matrix",
                "constraints_satisfied": True,
                "distinguished": True,
                "pair_distinguished": True,
                "causal_attribution_verified": True,
                "standard_result": [["expected"]],
                "student_result": [["student"]],
            }
            for diff in diffs
        )
    scope_kind = (
        "ROOT"
        if case.scope == "root"
        else "CTE"
        if case.scope.startswith("cte:")
        else "DERIVED"
        if case.scope.startswith("derived:")
        else "SUBQUERY"
    )
    standard_scope_id = (
        "standard:root"
        if scope_kind == "ROOT"
        else f"standard:scope:{case.slug}"
    )
    student_scope_id = (
        "student:root"
        if scope_kind == "ROOT"
        else f"student:scope:{case.slug}"
    )
    scopes = [
        {
            "scope_id": standard_scope_id,
            "scope_kind": scope_kind,
            "side": "standard",
            "conceptual_scope_id": case.scope,
            "metadata_complete": True,
        },
        {
            "scope_id": student_scope_id,
            "scope_kind": scope_kind,
            "side": "student",
            "conceptual_scope_id": case.scope,
            "metadata_complete": True,
        },
    ]
    composition_edges: list[dict[str, str]] = []
    if scope_kind != "ROOT":
        scopes.extend(
            [
                {
                    "scope_id": "standard:root",
                    "scope_kind": "ROOT",
                    "side": "standard",
                    "conceptual_scope_id": "root",
                    "metadata_complete": True,
                },
                {
                    "scope_id": "student:root",
                    "scope_kind": "ROOT",
                    "side": "student",
                    "conceptual_scope_id": "root",
                    "metadata_complete": True,
                },
            ]
        )
        edge_type = {
            "CTE": "CTE_FEEDS",
            "DERIVED": "DERIVED_FEEDS",
            "SUBQUERY": "SUBQUERY_OF",
        }[scope_kind]
        composition_edges.extend(
            [
                {
                    "edge_type": edge_type,
                    "source_scope_id": standard_scope_id,
                    "target_scope_id": "standard:root",
                },
                {
                    "edge_type": edge_type,
                    "source_scope_id": student_scope_id,
                    "target_scope_id": "student:root",
                },
            ]
        )
    data_evidence = {
        "status": "SUPPORTED",
        "equivalence_conclusion": "NOT_EQUIVALENT",
        "judge_status": "WRONG",
        "ast_diffs": list(diffs),
        "obligation_effectiveness": effects,
        "selected_witness_world_id": "world_matrix",
        "witness_suite": {"worlds": []},
        "only_in_standard_sample": [["expected"]],
        "only_in_student_sample": [["student"]],
        "scope_metadata": {
            "status": "COMPLETE",
            "scopes": scopes,
            "composition_edges": composition_edges,
            "diff_bindings": [
                binding
                for diff in diffs
                for binding in (
                    {
                        "diff_id": str(diff["diff_id"]),
                        "scope_id": standard_scope_id,
                        "side": "standard",
                        "conceptual_scope_id": case.scope,
                        "binding_status": "EXACT_AST_PATH",
                    },
                    {
                        "diff_id": str(diff["diff_id"]),
                        "scope_id": student_scope_id,
                        "side": "student",
                        "conceptual_scope_id": case.scope,
                        "binding_status": "EXACT_PAIRED_AST_PATH",
                    },
                )
            ],
            "limitations": [],
        },
        **dict(case.evidence_extra),
    }
    return SimpleNamespace(
        executed=True,
        is_equivalent=False,
        error=None,
        standard_sqlite=case.standard_sql,
        student_sqlite=case.student_sql,
        standard_rows=[("expected",)],
        student_rows=[("student",)],
        standard_columns=["result"],
        student_columns=["result"],
        test_database={},
        data_evidence=data_evidence,
        mutation_evidence={"tests": []},
        ast_diffs=[],
        judge_status="WRONG",
        status="SUPPORTED",
        equivalence_conclusion="NOT_EQUIVALENT",
        boundary_evidence={},
    )


def _diagnose(case: RuleCase, *, sufficient: bool):
    return diagnose_record(
        sandbox_run=_sandbox_run(case, sufficient=sufficient),
        question="Matrix acceptance question",
        schema=deepcopy(case.schema),
        student_sql=case.student_sql,
    )


def _candidate_signature(package) -> tuple[tuple[Any, ...], ...]:
    candidates: list[DiagnosticCandidate] = []
    if package.primary is not None:
        candidates.append(package.primary)
    candidates.extend(package.secondary)
    candidates.extend(package.unresolved)
    return tuple(
        sorted(
            (
                candidate.candidate_id,
                candidate.rule_id,
                candidate.scope_id,
                candidate.diff_ids,
                candidate.obligation_ids,
                candidate.mutation_test_ids,
                candidate.blocking,
            )
            for candidate in candidates
        )
    )


def _assert_pipeline_contract(case: RuleCase, package, repeat) -> None:
    public = package.to_dict()
    repeated_public = repeat.to_dict()
    assert public["ordered_diff_pipeline"] == repeated_public["ordered_diff_pipeline"]
    assert len(public["ordered_diff_pipeline"]) == len(case.all_diffs())
    assert {
        item["diff_id"] for item in public["ordered_diff_pipeline"]
    } == set(case.expected_diff_ids)
    assert {
        item["obligation_id"] for item in public["ordered_diff_pipeline"]
    } == set(case.expected_obligation_ids)
    assert {
        item["scope_id"] for item in public["ordered_diff_pipeline"]
    } == {case.scope}
    assert _candidate_signature(package) == _candidate_signature(repeat)


def _assert_candidate_refs(case: RuleCase, candidate: DiagnosticCandidate) -> None:
    assert candidate.scope_id == case.scope
    assert candidate.diff_ids == case.expected_diff_ids
    assert candidate.obligation_ids == case.expected_obligation_ids
    assert candidate.mutation_test_ids == ()
    public = candidate.public_dict()
    assert public["candidate_id"] == candidate.candidate_id
    assert public["evidence_refs"] == {
        "diff_ids": list(case.expected_diff_ids),
        "verified_diff_ids": list(candidate.verified_diff_ids),
        "unverified_diff_ids": list(candidate.unverified_diff_ids),
        "obligation_ids": list(case.expected_obligation_ids),
        "mutation_test_ids": [],
    }


def test_matrix_covers_exactly_the_declared_twenty_rule_catalog() -> None:
    catalog = {rule.rule_id: rule.teaching_stage for rule in RULE_CATALOG}
    assert set(RULE_CASES) == set(catalog)
    assert set(NEGATIVE_NEIGHBOUR) == set(catalog)
    assert all(case.stage == catalog[rule_id] for rule_id, case in RULE_CASES.items())
    assert all(neighbour in RULE_CASES for neighbour in NEGATIVE_NEIGHBOUR.values())


@pytest.mark.parametrize("rule_id", sorted(RULE_CASES))
def test_sufficient_atomic_evidence_selects_blocking_primary(rule_id: str) -> None:
    case = RULE_CASES[rule_id]
    package = _diagnose(case, sufficient=True)
    repeat = _diagnose(case, sufficient=True)

    _assert_pipeline_contract(case, package, repeat)
    assert package.verdict == "INCORRECT"
    assert package.diagnosis_status == "SUPPORTED"
    assert package.primary is not None
    assert package.primary.rule_id == rule_id
    assert package.primary.teaching_stage == case.stage
    assert package.primary.blocking is True
    assert package.primary.evidence_grade == "CAUSAL_VERIFIED"
    _assert_candidate_refs(case, package.primary)
    assert package.narrative == repeat.narrative
    assert package.narrative["student_behavior"]
    assert "改变了数据流，证据指向" not in package.narrative["student_behavior"]
    if rule_id not in {"S2_BOUNDARY", "S4_AGG_BOUNDARY"}:
        assert "与边界有关的值" not in package.narrative["conflict_and_witness"]
    assert repeat.primary is not None
    assert repeat.primary.candidate_id == package.primary.candidate_id


@pytest.mark.parametrize("rule_id", sorted(RULE_CASES))
def test_adjacent_causal_fault_is_not_mislabelled_as_rule(rule_id: str) -> None:
    neighbour = RULE_CASES[NEGATIVE_NEIGHBOUR[rule_id]]
    package = _diagnose(neighbour, sufficient=True)
    repeat = _diagnose(neighbour, sufficient=True)

    _assert_pipeline_contract(neighbour, package, repeat)
    labelled_rules = {
        candidate.rule_id
        for candidate in (
            ([package.primary] if package.primary is not None else [])
            + list(package.secondary)
            + list(package.unresolved)
        )
    }
    labelled_rules.update(str(item.get("rule_id")) for item in package.suppressed)
    assert rule_id not in labelled_rules


@pytest.mark.parametrize("rule_id", sorted(RULE_CASES))
def test_structural_signal_without_atomic_evidence_remains_unresolved(rule_id: str) -> None:
    case = RULE_CASES[rule_id]
    package = _diagnose(case, sufficient=False)
    repeat = _diagnose(case, sufficient=False)

    _assert_pipeline_contract(case, package, repeat)
    assert package.verdict == "INCORRECT"
    assert package.primary is None
    assert package.secondary == []
    matching = [candidate for candidate in package.unresolved if candidate.rule_id == rule_id]
    assert len(matching) == 1
    candidate = matching[0]
    assert candidate.blocking is False
    assert candidate.evidence_grade in {"AST_ONLY", "OUTPUT_ONLY", "PAIR_DISTINGUISHED"}
    _assert_candidate_refs(case, candidate)
    repeated = [item for item in repeat.unresolved if item.rule_id == rule_id]
    assert len(repeated) == 1
    assert repeated[0].candidate_id == candidate.candidate_id
