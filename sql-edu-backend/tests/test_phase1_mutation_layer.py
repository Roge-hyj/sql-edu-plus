"""Unit tests for the Phase 1 mutation layer builder.

The builder is the only component that turns a single-query corpus family into
an evaluation pair, so these tests pin the properties the acceptance plan relies
on: one row per family per role, a real AST edit rather than a text edit, a gold
side re-printed from its own AST, and equivalence controls that stay equivalent.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "data_construct_test" / "scripts" / "build_phase1_mutation_layer.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("build_phase1_mutation_layer", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


def _mutate(sql: str, family: str, *, schema: str = "", index: int = 0):
    return MODULE.apply_mutation(sql, family, dialect=None, index=index, schema_text=schema)


def test_required_registry_covers_the_fifteen_declared_families():
    assert len(MODULE.REQUIRED_FAMILY_NAMES) == 15
    assert len(set(MODULE.REQUIRED_FAMILY_NAMES)) == 15
    # Every registered operator must be reachable by name from the dispatcher.
    for name in MODULE.REQUIRED_FAMILY_NAMES:
        assert name in MODULE.MUTATION_OPERATOR_BY_NAME


@pytest.mark.parametrize(
    ("family", "sql", "expected_fragment"),
    [
        ("comparison_strictness", "SELECT a FROM t WHERE a > 3", "a >= 3"),
        ("logical_connector", "SELECT a FROM t WHERE a = 1 AND b = 2", " OR "),
        ("join_type", "SELECT a FROM t JOIN u ON t.i = u.i", "LEFT JOIN"),
        ("group_by_key", "SELECT a, b FROM t GROUP BY a, b", "GROUP BY b"),
        ("having_threshold", "SELECT a FROM t GROUP BY a HAVING COUNT(*) > 2", "> 3"),
        ("distinct_removed", "SELECT DISTINCT a FROM t", "SELECT a FROM t"),
        ("order_direction", "SELECT a FROM t ORDER BY a", "ORDER BY a DESC"),
        ("limit_offset", "SELECT a FROM t ORDER BY a LIMIT 5", "LIMIT 6"),
        ("set_all_modifier", "SELECT a FROM t UNION SELECT a FROM u", "UNION ALL"),
        ("membership_predicate", "SELECT a FROM t WHERE a IN (1, 2)", "NOT"),
        ("null_predicate", "SELECT a FROM t WHERE a IS NULL", "a = NULL"),
        (
            "window_specification",
            "SELECT RANK() OVER (PARTITION BY a ORDER BY b) FROM t",
            "OVER (ORDER BY b)",
        ),
        (
            "case_branch",
            "SELECT CASE WHEN a > 1 THEN 'x' WHEN a > 0 THEN 'y' END FROM t",
            "CASE WHEN a > 0",
        ),
        (
            "recursive_step",
            "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 5) SELECT n FROM c",
            "n + 2",
        ),
    ],
)
def test_each_required_family_produces_its_edit(family, sql, expected_fragment):
    result = _mutate(sql, family)
    assert result is not None, f"{family} reported itself inapplicable"
    _, mutated, operator = result
    assert expected_fragment in mutated
    assert operator


def test_join_key_mutation_uses_a_column_the_query_already_references():
    result = _mutate(
        "SELECT t.x FROM t JOIN u ON t.i = u.i WHERE u.k = 1",
        "join_key_column",
        schema="t(i, x); u(i, k)",
    )
    assert result is not None
    _, mutated, _ = result
    # ``u.k`` is a real column of ``u``; an alias or table name must never be
    # promoted into the column position.
    assert "u.k" in mutated
    assert "u.u" not in mutated


def test_gold_side_is_reprinted_so_only_the_mutation_differs():
    result = _mutate("select  count(*)  from t where a > 1", "comparison_strictness")
    assert result is not None
    gold, mutated, _ = result
    assert gold == "SELECT COUNT(*) FROM t WHERE a > 1"
    assert mutated == "SELECT COUNT(*) FROM t WHERE a >= 1"


def test_mutation_is_rejected_when_it_does_not_change_the_query():
    assert _mutate("SELECT a FROM t", "comparison_strictness") is None
    assert _mutate("SELECT a FROM t", "recursive_step") is None


def test_set_all_modifier_avoids_sqlite_incompatible_intersect_all():
    # ``INTERSECT ALL`` does not parse in SQLite, so mutating it would only
    # produce an engine gap instead of a teaching pair.
    assert _mutate("SELECT a FROM t INTERSECT SELECT a FROM u", "set_all_modifier") is None
    assert _mutate("SELECT a FROM t UNION SELECT a FROM u", "set_all_modifier") is not None


def test_set_all_modifier_skips_proven_unique_monotone_recursive_sequence():
    # A strictly increasing recursive sequence cannot emit duplicate rows, so
    # UNION ALL -> UNION is an equivalence control rather than a student error.
    sql = (
        "WITH RECURSIVE nums(n) AS "
        "(SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < 11) "
        "SELECT n FROM nums"
    )
    assert _mutate(sql, "set_all_modifier") is None


def test_set_all_modifier_keeps_recursive_shapes_without_a_uniqueness_proof():
    # A recursive member that repeats the current value may produce duplicates;
    # the mutation must remain available because its effect is observable.
    sql = (
        "WITH RECURSIVE nums(n) AS "
        "(SELECT 1 UNION ALL SELECT n FROM nums WHERE n < 3) "
        "SELECT n FROM nums"
    )
    assert _mutate(sql, "set_all_modifier") is not None


def test_null_predicate_skips_contradictory_conjunction():
    # Replacing one side of an already impossible conjunction with ``= NULL``
    # cannot produce a distinguishing row.
    sql = "SELECT id FROM t WHERE value IS NULL AND NOT value IS NULL"
    assert _mutate(sql, "null_predicate", schema="t(id, value)") is None


def test_distinct_mutations_skip_membership_subqueries():
    # Duplicates inside ``IN (...)`` cannot change the answer, so removing
    # DISTINCT there is an equivalence-preserving rewrite, not a student error.
    assert _mutate(
        "SELECT a FROM t WHERE a IN (SELECT DISTINCT b FROM u)", "distinct_removed"
    ) is None


def test_in_subquery_to_exists_emits_single_parenthesized_select():
    result = _mutate(
        "SELECT a FROM t WHERE a IN (SELECT b FROM u)",
        "membership_predicate",
    )
    assert result is not None
    _, mutated, operator = result
    assert operator == "in_subquery_to_exists"
    assert mutated == "SELECT a FROM t WHERE EXISTS(SELECT b FROM u)"
    # The generated SQL must remain parseable rather than relying on a
    # dialect-specific tolerance for nested parentheses.
    assert MODULE._parse(mutated, None, "t(a); u(b)") is not None


def test_order_direction_flip_does_not_move_nulls():
    result = _mutate("SELECT a FROM t ORDER BY a DESC", "order_direction")
    assert result is not None
    _, mutated, _ = result
    assert mutated == "SELECT a FROM t ORDER BY a ASC"


def test_numeric_leading_wikitable_headers_use_schema_aware_parse_fallback():
    result = _mutate(
        "SELECT home_2nd_leg FROM t WHERE 2006_07 = 'dnp'",
        "equality_predicate",
        schema="t(2006_07, home_2nd_leg)",
    )
    assert result is not None
    gold, mutated, _ = result
    assert '"2006_07"' in gold
    assert '"2006_07"' in mutated
    assert "<>" in mutated
    assert 'home_"2nd_leg"' not in gold


def test_reserved_wikitable_headers_use_schema_aware_parse_fallback():
    result = _mutate(
        "SELECT MIN(from) FROM t WHERE for = 1",
        "equality_predicate",
        schema="t(from, for)",
    )
    assert result is not None
    gold, mutated, _ = result
    assert 'MIN("from")' in gold
    assert '"for" = 1' in gold
    assert '"for" <> 1' in mutated


def test_drop_column_header_is_quoted_by_schema_aware_parse_fallback():
    result = _mutate(
        "SELECT draw FROM t WHERE drop = '0' AND pens = '0'",
        "equality_predicate",
        schema="t(draw, drop, pens)",
    )
    assert result is not None
    gold, mutated, _ = result
    assert '"drop" = \'0\'' in gold
    assert '"drop" <> \'0\'' in mutated


def test_returning_column_header_is_quoted_by_schema_aware_parse_fallback():
    result = _mutate(
        "SELECT retitled_as_same FROM t WHERE returning = 'april 3'",
        "equality_predicate",
        schema="t(retitled_as_same, returning)",
    )
    assert result is not None
    gold, mutated, _ = result
    assert '"returning" = \'april 3\'' in gold
    assert '"returning" <> \'april 3\'' in mutated


def test_scraped_description_prefix_is_not_treated_as_sql():
    result = _mutate(
        "with employees whose salary is high. */ SELECT salary FROM employees WHERE salary > 10",
        "comparison_strictness",
        schema="employees(salary)",
    )
    assert result is not None
    assert result[1].endswith("salary >= 10")


def test_generic_sql_server_top_without_ties_has_bounded_parser_fallback():
    result = _mutate(
        "SELECT TOP 1 id FROM t ORDER BY id",
        "order_direction",
        schema="t(id)",
    )
    assert result is not None
    assert "LIMIT 1" in result[0]
    assert "LIMIT 1" in result[1]


def test_top_with_ties_is_not_silently_reduced_to_limit():
    tree = MODULE._parse(
        "SELECT TOP 1 WITH TIES id FROM t ORDER BY id",
        None,
        "t(id)",
    )
    assert tree is not None
    limit = tree.args.get("limit")
    assert limit is not None
    limit_options = limit.args.get("limit_options")
    assert limit_options is not None
    assert bool(limit_options.args.get("with_ties"))
    result = _mutate(
        "SELECT TOP 1 WITH TIES id FROM t ORDER BY id",
        "order_direction",
        schema="t(id)",
    )
    assert result is not None
    assert "WITH TIES" in result[0].upper()
    assert "WITH TIES" in result[1].upper()


def test_sqlite_recursive_cte_named_columns_survive_rendering():
    result = MODULE.apply_mutation(
        "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < 5) "
        "SELECT n FROM nums",
        "recursive_step",
        dialect="sqlite",
        index=0,
        schema_text="t(id)",
    )
    # The SQLite generator drops ``(n)`` on a CTE alias; the bounded generic
    # renderer fallback must preserve it so the recursive query remains
    # executable rather than becoming an ENGINE_GAP fixture.
    assert result is not None
    assert "nums(n)" in result[0]
    assert "nums(n)" in result[1]


def test_count_star_and_distinct_aggregate_supplementary_mutations_are_available():
    count_result = _mutate(
        "SELECT Age, COUNT(*) FROM editor GROUP BY Age",
        "count_star_to_count_column",
        schema="editor(Age, note)",
    )
    assert count_result is not None
    assert "COUNT(editor.Age)" in count_result[1]

    distinct_result = _mutate(
        "SELECT COUNT(DISTINCT department_id) FROM Degree_Programs",
        "aggregate_distinct_removed",
        schema="Degree_Programs(department_id, label)",
    )
    assert distinct_result is not None
    assert distinct_result[1] == "SELECT COUNT(department_id) FROM Degree_Programs"


def test_count_star_mutation_skips_primary_key_columns():
    result = _mutate(
        "SELECT 1 AS bucket FROM gap_group_duplicate_0171 GROUP BY score HAVING COUNT(*) >= 1",
        "count_star_to_count_column",
        schema="gap_group_duplicate_0171(id INT PRIMARY KEY, score INT, marker TEXT)",
    )
    assert result is not None
    assert "COUNT(gap_group_duplicate_0171.id)" not in result[1]
    assert "COUNT(gap_group_duplicate_0171.score)" in result[1]


def test_distinct_mutations_skip_single_column_primary_key_projection():
    schema = "gap_in_between_like_0001(id INT PRIMARY KEY, score INT)"
    assert _mutate(
        "SELECT DISTINCT id FROM gap_in_between_like_0001 WHERE id BETWEEN 1 AND 2",
        "distinct_removed",
        schema=schema,
    ) is None
    assert _mutate(
        "SELECT id FROM gap_in_between_like_0001 WHERE id BETWEEN 1 AND 2",
        "projection_distinct",
        schema=schema,
    ) is None


def test_distinct_mutations_remain_available_for_non_unique_projection():
    schema = "t(id INT PRIMARY KEY, score INT)"
    removed = _mutate("SELECT DISTINCT score FROM t", "distinct_removed", schema=schema)
    added = _mutate("SELECT score FROM t", "projection_distinct", schema=schema)
    assert removed is not None
    assert added is not None


def test_public_distinct_removed_fixture_is_reachable():
    fixture_path = (
        PROJECT_ROOT
        / "data_construct_test"
        / "outputs"
        / "phase1_public_distinct_removed_fixture_20260826.jsonl"
    )
    record = json.loads(fixture_path.read_text(encoding="utf-8").splitlines()[0])
    result = _mutate(
        record["sql"],
        "distinct_removed",
        schema=record["schema"],
    )

    assert result is not None
    assert result[2] == "distinct_removed"
    assert result[0] == "SELECT DISTINCT value FROM distinct_probe_20260826"
    assert result[1] == "SELECT value FROM distinct_probe_20260826"


def test_distinct_mutations_skip_table_level_single_column_unique_constraint():
    schema = "users(id INT, email TEXT, PRIMARY KEY (id), UNIQUE (email))"
    assert _mutate("SELECT DISTINCT email FROM users", "distinct_removed", schema=schema) is None
    assert _mutate("SELECT email FROM users", "projection_distinct", schema=schema) is None


def test_count_column_mutation_and_equivalence_control_cover_null_sensitivity():
    sql = "SELECT COUNT(manager_id) FROM employee"
    schema = "employee(id, manager_id)"
    mutation = _mutate(sql, "count_column_to_count_star", schema=schema)
    assert mutation is not None
    assert mutation[1] == "SELECT COUNT(*) FROM employee"

    control = MODULE.apply_equivalence(sql, dialect=None, index=0, schema_text=schema)
    assert control is not None
    assert control[2] == "count_column_rewritten_as_count_case"
    assert "COUNT(CASE WHEN NOT manager_id IS NULL THEN 1 END)" in control[1]


def test_count_column_to_star_skips_proven_non_nullable_columns():
    assert _mutate(
        "SELECT COUNT(manager_id) FROM employee",
        "count_column_to_count_star",
        schema="employee(id INT PRIMARY KEY, manager_id INT NOT NULL)",
    ) is None


def test_aggregate_distinct_removal_skips_proven_unique_arguments():
    assert _mutate(
        "SELECT COUNT(DISTINCT id) FROM employee",
        "aggregate_distinct_removed",
        schema="employee(id INT PRIMARY KEY, value INT)",
    ) is None


def test_null_equals_null_mutation_skips_proven_non_nullable_columns():
    assert _mutate(
        "SELECT id FROM employee WHERE id IS NULL",
        "null_predicate",
        schema="employee(id INT NOT NULL, value INT)",
    ) is None


def test_redundant_true_predicate_control_is_nontrivial_and_parseable():
    result = MODULE.apply_equivalence(
        "SELECT DISTINCT x FROM t", dialect=None, index=0, schema_text="t(x)"
    )
    assert result is not None
    _, rewritten, tactic = result
    assert tactic == "redundant_true_predicate"
    assert rewritten == "SELECT DISTINCT x FROM t WHERE 1 = 1"


def test_equivalence_tactics_prefer_a_rewrite_the_diff_engine_can_see():
    # A flat equality query has no BETWEEN, IN list or range comparison, so the
    # singleton-IN rewrite is what keeps the control pair non-trivial instead of
    # falling through to bare parentheses.
    result = MODULE.apply_equivalence("SELECT a FROM t WHERE a = 1", dialect=None, index=0)
    assert result is not None
    gold, rewritten, tactic = result
    assert tactic == "equality_rewritten_as_singleton_in"
    assert rewritten == "SELECT a FROM t WHERE a IN (1)"
    assert gold == "SELECT a FROM t WHERE a = 1"


def test_between_control_is_parenthesised_so_precedence_cannot_change():
    result = MODULE.apply_equivalence(
        "SELECT a FROM t WHERE z = 1 OR a BETWEEN 2 AND 3", dialect=None, index=0
    )
    assert result is not None
    _, rewritten, tactic = result
    assert tactic == "between_expanded_to_range"
    assert "(a >= 2 AND a <= 3)" in rewritten


def test_build_emits_one_row_per_family_and_role(tmp_path: Path):
    records = [
        {
            "family_id": "f" * 64,
            "record_id": "family_ffff",
            "partition": "public",
            "dialect": "generic",
            "categories": ["where_logic_null"],
            "schema": "t(a, b)",
            "sql": "SELECT a FROM t WHERE a > 3",
            "scenario_axes": ["base"],
        },
        {
            "family_id": "e" * 64,
            "record_id": "family_eeee",
            "partition": "public",
            "dialect": "generic",
            "categories": ["select_projection"],
            "schema": "t(a, b)",
            "sql": "SELECT DISTINCT a FROM t",
            "scenario_axes": ["base"],
        },
        # A duplicate family id must not produce a second pair.
        {
            "family_id": "f" * 64,
            "record_id": "family_ffff_again",
            "partition": "public",
            "dialect": "generic",
            "categories": ["where_logic_null"],
            "schema": "t(a, b)",
            "sql": "SELECT a FROM t WHERE a > 3",
            "scenario_axes": ["base"],
        },
    ]
    source = tmp_path / "corpus.jsonl"
    source.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "layer.jsonl"
    manifest_path = tmp_path / "manifest.json"

    manifest = MODULE.build(
        [source],
        output,
        manifest_path,
        salt="test-salt",
        max_families=0,
        emit_equivalence=True,
        progress_every=0,
    )

    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert manifest["families_read"] == 2
    assert manifest["skips"]["skipped_duplicate_family"] == 1
    assert manifest["contains_sql"] is False
    # The manifest is the artifact a hidden-partition run may publish, so no
    # query text of any kind may appear in it.
    serialised = json.dumps(manifest)
    for row in rows:
        assert row["sql"] not in serialised
        assert row["student_sql"] not in serialised

    mutations = [row for row in rows if row["mutation_layer_role"] == "mutation"]
    controls = [row for row in rows if row["mutation_layer_role"] == "equivalence"]
    assert len({row["family_id"] for row in mutations}) == len(mutations)
    assert all(row["expectation"] == "not_equivalent" for row in mutations)
    assert all(row["expectation"] == "equivalent" for row in controls)
    assert all("paired_mutation" in row["scenario_axes"] for row in rows)
    assert all("mutation_ready" in row["scenario_axes"] for row in mutations)
    # The family id is inherited, so the family denominator cannot grow.
    assert {row["family_id"] for row in rows} <= {"f" * 64, "e" * 64}


def test_build_is_deterministic_for_the_same_salt(tmp_path: Path):
    record = {
        "family_id": "a" * 64,
        "record_id": "family_aaaa",
        "partition": "train",
        "dialect": "generic",
        "categories": ["where_logic_null"],
        "schema": "t(a, b, c)",
        "sql": "SELECT a FROM t WHERE a > 3 AND b < 9",
        "scenario_axes": ["base"],
    }
    source = tmp_path / "corpus.jsonl"
    source.write_text(json.dumps(record) + "\n", encoding="utf-8")

    digests = []
    for run in range(2):
        output = tmp_path / f"layer_{run}.jsonl"
        MODULE.build(
            [source],
            output,
            tmp_path / f"manifest_{run}.json",
            salt="stable",
            max_families=0,
            emit_equivalence=True,
            progress_every=0,
        )
        digests.append(output.read_text(encoding="utf-8"))
    assert digests[0] == digests[1]
