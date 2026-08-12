"""Classic SQL teaching holdout for structure IR and ASTDiff only.

The cases are inspired by well-known SQLZoo tutorials and PostgreSQL official
tutorial/reference examples, but the assertions are local and structure-only:
- no generated data
- no sandbox execution
- no attribution model

This is deliberately stricter than end-to-end correctness tests. It records:
- hard failures: missing expected IR structures or missing expected ASTDiffs
- noise: unexpected ASTDiff clauses outside an allowed set
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "sql-edu-backend"))

from core.ast_schema import SQLStructureIR
from core.parseval_data_generator import _parse_sql, extract_ast_diffs

OUTPUT_DIR = PROJECT_ROOT / "data_construct_test" / "outputs"


Case = dict[str, Any]


def case(
    id_: str,
    source: str,
    structure: str,
    standard: str,
    student: str,
    *,
    ir_features: list[str],
    predicate_kinds: list[str] | None = None,
    logic_ops: list[str] | None = None,
    diff_clauses: list[str] | None = None,
    diff_types: list[str] | None = None,
    allowed_extra_clauses: list[str] | None = None,
    expect_no_diff: bool = False,
) -> Case:
    return {
        "id": id_,
        "source": source,
        "structure": structure,
        "standard": standard,
        "student": student,
        "expected": {
            "ir_features": ir_features,
            "predicate_kinds": predicate_kinds or [],
            "logic_ops": logic_ops or [],
            "diff_clauses": diff_clauses or [],
            "diff_types": diff_types or [],
            "allowed_extra_clauses": allowed_extra_clauses or [],
            "expect_no_diff": expect_no_diff,
        },
    }


CASES: list[Case] = [
    case(
        "sqlzoo_select_projection",
        "SQLZoo SELECT basics style",
        "SELECT",
        "SELECT name, population FROM world;",
        "SELECT name FROM world;",
        ir_features=["select-basic"],
        diff_clauses=["SELECT"],
        diff_types=["projection_changed", "column_dropped"],
    ),
    case(
        "sqlzoo_distinct_continent",
        "SQLZoo world DISTINCT style",
        "DISTINCT",
        "SELECT DISTINCT continent FROM world;",
        "SELECT continent FROM world;",
        ir_features=["select-basic", "distinct"],
        diff_clauses=["DISTINCT"],
        diff_types=["distinct_changed"],
    ),
    case(
        "sqlzoo_where_country",
        "SQLZoo SELECT basics WHERE style",
        "WHERE",
        "SELECT population FROM world WHERE name = 'France';",
        "SELECT population FROM world;",
        ir_features=["select-basic", "where"],
        predicate_kinds=["comparison"],
        diff_clauses=["WHERE", "PREDICATE"],
        diff_types=["where_changed", "predicate_missing"],
    ),
    case(
        "sqlzoo_comparison_population",
        "SQLZoo world comparison style",
        "Comparison",
        "SELECT name FROM world WHERE population > 250000000;",
        "SELECT name FROM world WHERE population >= 250000000;",
        ir_features=["select-basic", "where"],
        predicate_kinds=["comparison"],
        diff_clauses=["WHERE", "PREDICATE"],
        diff_types=["comparison_operator_changed"],
    ),
    case(
        "sqlzoo_null_teacher",
        "SQLZoo Using NULL style",
        "NULL",
        "SELECT teacher.name FROM teacher LEFT JOIN dept ON teacher.dept = dept.id WHERE dept.name IS NULL;",
        "SELECT teacher.name FROM teacher LEFT JOIN dept ON teacher.dept = dept.id WHERE dept.name = NULL;",
        ir_features=["select-basic", "where", "join-inner", "join-left", "join-on"],
        predicate_kinds=["null_check"],
        diff_clauses=["WHERE", "PREDICATE", "NULL"],
        diff_types=["null_equality_changed"],
    ),
    case(
        "sqlzoo_in_scandinavia",
        "SQLZoo SELECT basics IN style",
        "IN",
        "SELECT name, population FROM world WHERE name IN ('Sweden', 'Norway', 'Denmark');",
        "SELECT name, population FROM world WHERE name IN ('Sweden', 'Norway');",
        ir_features=["select-basic", "where"],
        predicate_kinds=["in_list"],
        diff_clauses=["WHERE", "PREDICATE"],
        diff_types=["in_list_member_removed"],
    ),
    case(
        "sqlzoo_between_area",
        "SQLZoo SELECT basics BETWEEN style",
        "BETWEEN",
        "SELECT name, area FROM world WHERE area BETWEEN 200000 AND 250000;",
        "SELECT name, area FROM world WHERE area BETWEEN 250000 AND 300000;",
        ir_features=["select-basic", "where"],
        predicate_kinds=["between"],
        diff_clauses=["WHERE", "PREDICATE"],
        diff_types=["literal_changed"],
    ),
    case(
        "sqlzoo_like_name",
        "SQLZoo SELECT name LIKE style",
        "LIKE",
        "SELECT name FROM world WHERE name LIKE 'United%';",
        "SELECT name FROM world WHERE name LIKE 'Uni%';",
        ir_features=["select-basic", "where"],
        predicate_kinds=["like"],
        diff_clauses=["WHERE", "PREDICATE"],
        diff_types=["literal_changed"],
    ),
    case(
        "sqlzoo_logic_world",
        "SQLZoo world WHERE logic style",
        "Logic",
        "SELECT name FROM world WHERE continent = 'Europe' AND population > 50000000;",
        "SELECT name FROM world WHERE continent = 'Europe' OR population > 50000000;",
        ir_features=["select-basic", "where"],
        predicate_kinds=["logic", "comparison"],
        logic_ops=["AND"],
        diff_clauses=["WHERE", "LOGICAL"],
        diff_types=["logical_operator_changed"],
    ),
    case(
        "sqlzoo_join_games_city",
        "SQLZoo JOIN games/city style",
        "JOIN",
        "SELECT games.yr, city.country FROM games JOIN city ON games.city = city.name;",
        "SELECT games.yr FROM games;",
        ir_features=["select-basic", "join-inner", "join-on"],
        diff_clauses=["SELECT", "JOIN", "JOIN ON"],
        diff_types=["join_missing", "join_on_changed"],
    ),
    case(
        "sqlzoo_join_on_key",
        "SQLZoo JOIN ON style",
        "JOIN ON",
        "SELECT games.yr, city.country FROM games JOIN city ON games.city = city.name;",
        "SELECT games.yr, city.country FROM games JOIN city ON games.city = city.country;",
        ir_features=["select-basic", "join-inner", "join-on"],
        diff_clauses=["JOIN ON"],
        diff_types=["join_on_changed"],
    ),
    case(
        "sqlzoo_group_by_continent",
        "SQLZoo SUM and COUNT GROUP BY style",
        "GROUP BY",
        "SELECT continent, COUNT(*) FROM world GROUP BY continent;",
        "SELECT continent, COUNT(*) FROM world GROUP BY name;",
        ir_features=["select-basic", "group-by", "aggregate", "agg-count"],
        diff_clauses=["GROUP BY"],
        diff_types=["group_by_changed"],
    ),
    case(
        "sqlzoo_having_continent",
        "SQLZoo SUM and COUNT HAVING style",
        "HAVING",
        "SELECT continent FROM world GROUP BY continent HAVING SUM(population) > 100000000;",
        "SELECT continent FROM world GROUP BY continent HAVING SUM(population) >= 100000000;",
        ir_features=["select-basic", "group-by", "having", "aggregate"],
        predicate_kinds=["comparison"],
        diff_clauses=["HAVING", "PREDICATE"],
        diff_types=["having_changed", "comparison_operator_changed"],
    ),
    case(
        "postgres_aggregate_weather",
        "PostgreSQL tutorial aggregate style",
        "Aggregate",
        "SELECT city, MAX(temp_hi) FROM weather GROUP BY city;",
        "SELECT city, MIN(temp_hi) FROM weather GROUP BY city;",
        ir_features=["select-basic", "group-by", "aggregate"],
        diff_clauses=["SELECT", "AGGREGATE"],
        diff_types=["aggregate_function_changed"],
    ),
    case(
        "postgres_order_weather",
        "PostgreSQL tutorial querying/order style",
        "ORDER BY",
        "SELECT city, temp_lo, temp_hi FROM weather ORDER BY city, temp_lo DESC;",
        "SELECT city, temp_lo, temp_hi FROM weather ORDER BY city, temp_lo ASC;",
        ir_features=["select-basic", "order-by"],
        diff_clauses=["ORDER BY"],
        diff_types=["order_by_changed"],
    ),
    case(
        "postgres_limit_offset",
        "PostgreSQL SELECT reference LIMIT/OFFSET style",
        "LIMIT / OFFSET",
        "SELECT city FROM weather ORDER BY city LIMIT 5 OFFSET 2;",
        "SELECT city FROM weather ORDER BY city LIMIT 10 OFFSET 2;",
        ir_features=["select-basic", "order-by", "limit"],
        diff_clauses=["LIMIT"],
        diff_types=["limit_changed"],
    ),
    case(
        "sqlzoo_scalar_subquery_world",
        "SQLZoo SELECT within SELECT scalar subquery style",
        "Subquery",
        "SELECT name FROM world WHERE population > (SELECT population FROM world WHERE name = 'Russia');",
        "SELECT name FROM world WHERE population > (SELECT population FROM world WHERE name = 'Germany');",
        ir_features=["select-basic", "where", "subquery-scalar"],
        predicate_kinds=["comparison"],
        diff_clauses=["WHERE", "PREDICATE"],
        diff_types=["literal_changed"],
    ),
    case(
        "sqlzoo_correlated_bbc",
        "SQLZoo correlated subquery style",
        "Correlated Subquery",
        "SELECT name FROM bbc b1 WHERE population > 5 * (SELECT AVG(population) FROM bbc WHERE region = b1.region);",
        "SELECT name FROM bbc b1 WHERE population > 4 * (SELECT AVG(population) FROM bbc WHERE region = b1.region);",
        ir_features=["select-basic", "where", "subquery-correlated", "subquery-scalar", "aggregate"],
        predicate_kinds=["comparison"],
        diff_clauses=["WHERE", "CORRELATED SUBQUERY", "PREDICATE"],
        diff_types=["correlated_predicate_changed"],
    ),
    case(
        "postgres_cte_weather",
        "PostgreSQL WITH style",
        "CTE",
        "WITH hot AS (SELECT city FROM weather WHERE temp_hi > 80) SELECT city FROM hot;",
        "WITH hot AS (SELECT city FROM weather WHERE temp_hi > 90) SELECT city FROM hot;",
        ir_features=["select-basic", "cte"],
        diff_clauses=["WHERE", "PREDICATE", "CTE"],
        diff_types=["cte_changed", "literal_changed"],
    ),
    case(
        "postgres_recursive_cte_numbers",
        "PostgreSQL WITH RECURSIVE style",
        "Recursive CTE",
        "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 100) SELECT SUM(n) FROM t;",
        "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 99) SELECT SUM(n) FROM t;",
        ir_features=["select-basic", "cte-recursive", "union", "aggregate"],
        diff_clauses=["WHERE", "PREDICATE", "CTE_RECURSIVE"],
        diff_types=["recursive_cte_changed", "literal_changed"],
    ),
    case(
        "postgres_set_operation",
        "PostgreSQL SELECT set operation style",
        "Set Operation",
        "SELECT city FROM weather WHERE temp_hi > 80 UNION SELECT name FROM cities;",
        "SELECT city FROM weather WHERE temp_hi > 80 INTERSECT SELECT name FROM cities;",
        ir_features=["select-basic", "union"],
        diff_clauses=["UNION"],
        diff_types=["set_operator_changed"],
    ),
    case(
        "postgres_case_population",
        "PostgreSQL conditional expression style",
        "CASE",
        "SELECT name, CASE WHEN population > 100000000 THEN 'large' ELSE 'small' END FROM world;",
        "SELECT name, CASE WHEN population >= 100000000 THEN 'large' ELSE 'small' END FROM world;",
        ir_features=["select-basic", "case"],
        predicate_kinds=["comparison"],
        diff_clauses=["CASE"],
        diff_types=["case_changed"],
        allowed_extra_clauses=["SELECT", "PREDICATE"],
    ),
    case(
        "postgres_window_empsalary",
        "PostgreSQL tutorial window function style",
        "Window",
        "SELECT depname, empno, salary, ROW_NUMBER() OVER (PARTITION BY depname ORDER BY salary DESC) FROM empsalary;",
        "SELECT depname, empno, salary, ROW_NUMBER() OVER (ORDER BY salary DESC) FROM empsalary;",
        ir_features=["select-basic", "window-row-number"],
        diff_clauses=["SELECT", "WINDOW"],
        diff_types=["window_over_changed"],
    ),
    case(
        "dialect_top_limit",
        "PostgreSQL SELECT reference + T-SQL TOP boundary",
        "Dialect Boundary",
        "SELECT TOP 5 name FROM world ORDER BY population DESC;",
        "SELECT name FROM world ORDER BY population DESC LIMIT 5;",
        ir_features=["select-basic", "order-by", "limit"],
        expect_no_diff=True,
    ),
    case(
        "dialect_fetch_limit",
        "PostgreSQL FETCH FIRST vs LIMIT boundary",
        "Dialect Boundary",
        "SELECT name FROM world ORDER BY population DESC FETCH FIRST 5 ROWS ONLY;",
        "SELECT name FROM world ORDER BY population DESC LIMIT 5;",
        ir_features=["select-basic", "order-by", "limit"],
        expect_no_diff=True,
    ),
]


def build_cases() -> list[Case]:
    """Return a balanced strict holdout with at least 100 structure-only cases."""
    cases = list(CASES)

    for idx, (std_cols, stu_cols) in enumerate([
        ("name, capital", "name"),
        ("name, area", "area, name"),
        ("city, temp_hi, temp_lo", "city, temp_hi"),
        ("depname, empno, salary", "depname, salary, empno"),
    ], start=1):
        cases.append(case(
            f"select_projection_variant_{idx}",
            "Classic SELECT projection variant",
            "SELECT",
            f"SELECT {std_cols} FROM world;",
            f"SELECT {stu_cols} FROM world;",
            ir_features=["select-basic"],
            diff_clauses=["SELECT"],
            diff_types=["projection_changed"],
        ))

    for idx, (table, select_col, predicate) in enumerate([
        ("world", "name", "continent = 'Europe'"),
        ("weather", "city", "temp_hi > 80"),
        ("empsalary", "empno", "salary > 50000"),
        ("bbc", "name", "region = 'Europe'"),
    ], start=1):
        cases.append(case(
            f"where_missing_variant_{idx}",
            "Classic WHERE missing variant",
            "WHERE",
            f"SELECT {select_col} FROM {table} WHERE {predicate};",
            f"SELECT {select_col} FROM {table};",
            ir_features=["select-basic", "where"],
            predicate_kinds=["comparison"],
            diff_clauses=["WHERE", "PREDICATE"],
            diff_types=["where_changed", "predicate_missing"],
        ))

    for idx, col in enumerate(["continent", "region", "dept_name", "city"], start=1):
        table = "world" if col in {"continent", "region"} else "weather"
        cases.append(case(
            f"distinct_variant_{idx}",
            "Classic DISTINCT variant",
            "DISTINCT",
            f"SELECT DISTINCT {col} FROM {table};",
            f"SELECT {col} FROM {table};",
            ir_features=["select-basic", "distinct"],
            diff_clauses=["DISTINCT"],
            diff_types=["distinct_changed"],
        ))

    for idx, (col, std_op, stu_op, val) in enumerate([
        ("population", ">", ">=", "1000000"),
        ("area", "<", "<=", "50000"),
        ("temp_hi", ">=", ">", "80"),
        ("salary", "<=", "<", "50000"),
        ("population", "=", "<>", "0"),
    ], start=1):
        table = "weather" if col.startswith("temp") else "empsalary" if col == "salary" else "world"
        select_col = "city" if table == "weather" else "empno" if table == "empsalary" else "name"
        cases.append(case(
            f"comparison_variant_{idx}",
            "Classic comparison variant",
            "Comparison",
            f"SELECT {select_col} FROM {table} WHERE {col} {std_op} {val};",
            f"SELECT {select_col} FROM {table} WHERE {col} {stu_op} {val};",
            ir_features=["select-basic", "where"],
            predicate_kinds=["comparison"],
            diff_clauses=["WHERE", "PREDICATE"],
            diff_types=["comparison_operator_changed"],
        ))

    for idx, (expr, bad) in enumerate([
        ("dept.name IS NULL", "dept.name = NULL"),
        ("teacher.dept IS NOT NULL", "teacher.dept <> NULL"),
        ("city IS NULL", "city = NULL"),
        ("region IS NULL", "region = NULL"),
    ], start=1):
        join_sql = "teacher LEFT JOIN dept ON teacher.dept = dept.id"
        table_sql = join_sql if "dept." in expr or "teacher." in expr else "weather" if "city" in expr else "bbc"
        select_col = "teacher.name" if "teacher" in table_sql else "city" if table_sql == "weather" else "name"
        cases.append(case(
            f"null_variant_{idx}",
            "Classic NULL variant",
            "NULL",
            f"SELECT {select_col} FROM {table_sql} WHERE {expr};",
            f"SELECT {select_col} FROM {table_sql} WHERE {bad};",
            ir_features=["select-basic", "where"] + (["join-inner", "join-left", "join-on"] if "JOIN" in table_sql else []),
            predicate_kinds=["null_check"],
            diff_clauses=["WHERE", "PREDICATE", "NULL"],
            diff_types=["null_equality_changed"],
        ))

    for idx, (col, std_vals, stu_vals) in enumerate([
        ("name", "'France', 'Germany', 'Italy'", "'France', 'Germany'"),
        ("continent", "'Europe', 'Asia'", "'Europe', 'Africa'"),
        ("city", "'San Francisco', 'Hayward'", "'San Francisco'"),
        ("depname", "'develop', 'sales'", "'develop'"),
    ], start=1):
        table = "weather" if col == "city" else "empsalary" if col == "depname" else "world"
        select_col = "city" if table == "weather" else "empno" if table == "empsalary" else "name"
        cases.append(case(
            f"in_variant_{idx}",
            "Classic IN variant",
            "IN",
            f"SELECT {select_col} FROM {table} WHERE {col} IN ({std_vals});",
            f"SELECT {select_col} FROM {table} WHERE {col} IN ({stu_vals});",
            ir_features=["select-basic", "where"],
            predicate_kinds=["in_list"],
            diff_clauses=["WHERE", "PREDICATE"],
            diff_types=["in_list_member_removed"],
        ))

    for idx, (col, std_low, std_high, stu_low, stu_high, table, select_col) in enumerate([
        ("population", "1000000", "5000000", "2000000", "5000000", "world", "name"),
        ("area", "200000", "250000", "200000", "300000", "world", "name"),
        ("temp_hi", "70", "90", "75", "90", "weather", "city"),
        ("salary", "40000", "70000", "50000", "70000", "empsalary", "empno"),
    ], start=1):
        cases.append(case(
            f"between_variant_{idx}",
            "Classic BETWEEN variant",
            "BETWEEN",
            f"SELECT {select_col} FROM {table} WHERE {col} BETWEEN {std_low} AND {std_high};",
            f"SELECT {select_col} FROM {table} WHERE {col} BETWEEN {stu_low} AND {stu_high};",
            ir_features=["select-basic", "where"],
            predicate_kinds=["between"],
            diff_clauses=["WHERE", "PREDICATE"],
            diff_types=["literal_changed"],
        ))

    for idx, (pattern_a, pattern_b) in enumerate([
        ("A%", "B%"),
        ("%land", "%stan"),
        ("United%", "Uni%"),
        ("San%", "New%"),
    ], start=1):
        table = "weather" if idx == 4 else "world"
        col = "city" if table == "weather" else "name"
        cases.append(case(
            f"like_variant_{idx}",
            "Classic LIKE variant",
            "LIKE",
            f"SELECT {col} FROM {table} WHERE {col} LIKE '{pattern_a}';",
            f"SELECT {col} FROM {table} WHERE {col} LIKE '{pattern_b}';",
            ir_features=["select-basic", "where"],
            predicate_kinds=["like"],
            diff_clauses=["WHERE", "PREDICATE"],
            diff_types=["literal_changed"],
        ))

    for idx, (std_logic, stu_logic) in enumerate([
        ("continent = 'Europe' AND population > 50000000", "continent = 'Europe' OR population > 50000000"),
        ("temp_hi > 80 OR temp_lo < 20", "temp_hi > 80 AND temp_lo < 20"),
        ("salary > 50000 AND depname = 'sales'", "salary > 50000 OR depname = 'sales'"),
        ("name LIKE 'A%' AND area > 100000", "name LIKE 'A%' OR area > 100000"),
    ], start=1):
        table = "weather" if "temp_" in std_logic else "empsalary" if "salary" in std_logic else "world"
        select_col = "city" if table == "weather" else "empno" if table == "empsalary" else "name"
        cases.append(case(
            f"logic_variant_{idx}",
            "Classic logic variant",
            "Logic",
            f"SELECT {select_col} FROM {table} WHERE {std_logic};",
            f"SELECT {select_col} FROM {table} WHERE {stu_logic};",
            ir_features=["select-basic", "where"],
            predicate_kinds=["logic"],
            diff_clauses=["WHERE", "LOGICAL"],
            diff_types=["logical_operator_changed"],
        ))

    for idx, (std_on, stu_on) in enumerate([
        ("games.city = city.name", "games.city = city.country"),
        ("weather.city = cities.name", "weather.city = cities.location"),
        ("teacher.dept = dept.id", "teacher.id = dept.id"),
        ("a.id = b.a_id", "a.id = b.id"),
    ], start=1):
        from_sql = ["games JOIN city", "weather JOIN cities", "teacher JOIN dept", "a JOIN b"][idx - 1]
        cases.append(case(
            f"join_on_variant_{idx}",
            "Classic JOIN ON variant",
            "JOIN ON",
            f"SELECT * FROM {from_sql} ON {std_on};",
            f"SELECT * FROM {from_sql} ON {stu_on};",
            ir_features=["select-basic", "join-inner", "join-on"],
            diff_clauses=["JOIN ON"],
            diff_types=["join_on_changed"],
        ))

    for idx, (from_sql, select_col) in enumerate([
        ("games JOIN city ON games.city = city.name", "games.yr"),
        ("weather JOIN cities ON weather.city = cities.name", "weather.city"),
        ("teacher JOIN dept ON teacher.dept = dept.id", "teacher.name"),
        ("student JOIN advisor ON student.ID = advisor.s_ID", "student.name"),
    ], start=1):
        base_table = from_sql.split(" JOIN ")[0]
        cases.append(case(
            f"join_missing_variant_{idx}",
            "Classic JOIN missing variant",
            "JOIN",
            f"SELECT {select_col} FROM {from_sql};",
            f"SELECT {select_col} FROM {base_table};",
            ir_features=["select-basic", "join-inner", "join-on"],
            diff_clauses=["JOIN", "JOIN ON"],
            diff_types=["join_missing", "join_on_changed"],
        ))

    for idx, (std_group, stu_group) in enumerate([
        ("continent", "name"),
        ("city", "temp_hi"),
        ("depname", "empno"),
        ("region", "name"),
    ], start=1):
        table = "weather" if std_group == "city" else "empsalary" if std_group == "depname" else "world" if std_group == "continent" else "bbc"
        cases.append(case(
            f"group_by_variant_{idx}",
            "Classic GROUP BY variant",
            "GROUP BY",
            f"SELECT {std_group}, COUNT(*) FROM {table} GROUP BY {std_group};",
            f"SELECT {std_group}, COUNT(*) FROM {table} GROUP BY {stu_group};",
            ir_features=["select-basic", "group-by", "aggregate", "agg-count"],
            diff_clauses=["GROUP BY"],
            diff_types=["group_by_changed"],
        ))

    for idx, (agg, op_a, op_b, boundary) in enumerate([
        ("SUM", ">", ">=", "100000000"),
        ("COUNT", ">=", ">", "3"),
        ("AVG", ">", "<", "50000"),
        ("MAX", "<", "<=", "100000"),
    ], start=1):
        cases.append(case(
            f"having_variant_{idx}",
            "Classic HAVING variant",
            "HAVING",
            f"SELECT continent FROM world GROUP BY continent HAVING {agg}(population) {op_a} {boundary};",
            f"SELECT continent FROM world GROUP BY continent HAVING {agg}(population) {op_b} {boundary};",
            ir_features=["select-basic", "group-by", "having", "aggregate"],
            predicate_kinds=["comparison"],
            diff_clauses=["HAVING", "PREDICATE"],
            diff_types=["comparison_operator_changed"],
        ))

    for idx, (std_agg, stu_agg) in enumerate([("MAX", "MIN"), ("AVG", "SUM"), ("COUNT", "SUM"), ("MIN", "MAX")], start=1):
        cases.append(case(
            f"aggregate_variant_{idx}",
            "Classic aggregate variant",
            "Aggregate",
            f"SELECT city, {std_agg}(temp_hi) FROM weather GROUP BY city;",
            f"SELECT city, {stu_agg}(temp_hi) FROM weather GROUP BY city;",
            ir_features=["select-basic", "group-by", "aggregate"],
            diff_clauses=["SELECT", "AGGREGATE"],
            diff_types=["aggregate_function_changed"],
        ))

    for idx, (std_order, stu_order) in enumerate([
        ("population DESC", "population ASC"),
        ("city ASC, temp_lo DESC", "city ASC, temp_lo ASC"),
        ("salary DESC", "salary ASC"),
        ("name ASC NULLS LAST", "name ASC NULLS FIRST"),
    ], start=1):
        table = "weather" if "city" in std_order or "temp" in std_order else "empsalary" if "salary" in std_order else "world"
        select_col = "city" if table == "weather" else "empno" if table == "empsalary" else "name"
        cases.append(case(
            f"order_by_variant_{idx}",
            "Classic ORDER BY variant",
            "ORDER BY",
            f"SELECT {select_col} FROM {table} ORDER BY {std_order};",
            f"SELECT {select_col} FROM {table} ORDER BY {stu_order};",
            ir_features=["select-basic", "order-by"],
            diff_clauses=["ORDER BY"],
            diff_types=["order_by_changed"],
        ))

    for idx, (std_lim, stu_lim) in enumerate([("5", "10"), ("3 OFFSET 2", "3 OFFSET 4"), ("1", "2"), ("8 OFFSET 1", "9 OFFSET 1")], start=1):
        cases.append(case(
            f"limit_variant_{idx}",
            "Classic LIMIT/OFFSET variant",
            "LIMIT / OFFSET",
            f"SELECT name FROM world ORDER BY population DESC LIMIT {std_lim};",
            f"SELECT name FROM world ORDER BY population DESC LIMIT {stu_lim};",
            ir_features=["select-basic", "order-by", "limit"],
            diff_clauses=["LIMIT"],
            diff_types=["limit_changed"],
        ))

    for idx, (std_name, stu_name) in enumerate([("Russia", "Germany"), ("Brazil", "France"), ("China", "India"), ("Canada", "Mexico")], start=1):
        cases.append(case(
            f"subquery_variant_{idx}",
            "Classic scalar subquery variant",
            "Subquery",
            f"SELECT name FROM world WHERE population > (SELECT population FROM world WHERE name = '{std_name}');",
            f"SELECT name FROM world WHERE population > (SELECT population FROM world WHERE name = '{stu_name}');",
            ir_features=["select-basic", "where", "subquery-scalar"],
            predicate_kinds=["comparison"],
            diff_clauses=["WHERE", "PREDICATE"],
            diff_types=["literal_changed"],
        ))

    for idx, (std_factor, stu_factor) in enumerate([(5, 4), (3, 2), (2, 4), (10, 9)], start=1):
        cases.append(case(
            f"correlated_variant_{idx}",
            "Classic correlated subquery variant",
            "Correlated Subquery",
            f"SELECT name FROM bbc b1 WHERE population > {std_factor} * (SELECT AVG(population) FROM bbc WHERE region = b1.region);",
            f"SELECT name FROM bbc b1 WHERE population > {stu_factor} * (SELECT AVG(population) FROM bbc WHERE region = b1.region);",
            ir_features=["select-basic", "where", "subquery-correlated", "subquery-scalar", "aggregate"],
            predicate_kinds=["comparison"],
            diff_clauses=["WHERE", "CORRELATED SUBQUERY", "PREDICATE"],
            diff_types=["correlated_predicate_changed"],
        ))

    for idx, (std_boundary, stu_boundary) in enumerate([(80, 90), (70, 75), (60, 65), (100, 110)], start=1):
        cases.append(case(
            f"cte_variant_{idx}",
            "Classic CTE variant",
            "CTE",
            f"WITH hot AS (SELECT city FROM weather WHERE temp_hi > {std_boundary}) SELECT city FROM hot;",
            f"WITH hot AS (SELECT city FROM weather WHERE temp_hi > {stu_boundary}) SELECT city FROM hot;",
            ir_features=["select-basic", "cte"],
            diff_clauses=["WHERE", "PREDICATE", "CTE"],
            diff_types=["cte_changed", "literal_changed"],
        ))

    for idx, (std_limit, stu_limit) in enumerate([(100, 99), (10, 12), (5, 4), (20, 30)], start=1):
        cases.append(case(
            f"recursive_cte_variant_{idx}",
            "Classic recursive CTE variant",
            "Recursive CTE",
            f"WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < {std_limit}) SELECT SUM(n) FROM t;",
            f"WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < {stu_limit}) SELECT SUM(n) FROM t;",
            ir_features=["select-basic", "cte-recursive", "union", "aggregate"],
            diff_clauses=["WHERE", "PREDICATE", "CTE_RECURSIVE"],
            diff_types=["recursive_cte_changed", "literal_changed"],
        ))

    for idx, (std_op, stu_op) in enumerate([("UNION", "UNION ALL"), ("UNION", "INTERSECT"), ("EXCEPT", "UNION"), ("INTERSECT", "EXCEPT")], start=1):
        cases.append(case(
            f"set_operation_variant_{idx}",
            "Classic set operation variant",
            "Set Operation",
            f"SELECT name FROM world WHERE continent = 'Europe' {std_op} SELECT name FROM world WHERE population > 1000000;",
            f"SELECT name FROM world WHERE continent = 'Europe' {stu_op} SELECT name FROM world WHERE population > 1000000;",
            ir_features=["select-basic", std_op.split()[0].lower()],
            diff_clauses=[std_op.split()[0]],
            diff_types=["set_operator_changed"],
        ))

    for idx, (std_op, stu_op) in enumerate([(">", ">="), ("<", "<="), (">=", ">"), ("=", "<>")], start=1):
        cases.append(case(
            f"case_variant_{idx}",
            "Classic CASE variant",
            "CASE",
            f"SELECT name, CASE WHEN population {std_op} 100000000 THEN 'large' ELSE 'small' END FROM world;",
            f"SELECT name, CASE WHEN population {stu_op} 100000000 THEN 'large' ELSE 'small' END FROM world;",
            ir_features=["select-basic", "case"],
            predicate_kinds=["comparison"],
            diff_clauses=["CASE"],
            diff_types=["case_changed"],
            allowed_extra_clauses=["SELECT", "PREDICATE"],
        ))

    for idx, (std_win, stu_win) in enumerate([
        ("PARTITION BY depname ORDER BY salary DESC", "ORDER BY salary DESC"),
        ("PARTITION BY depname ORDER BY salary ASC", "PARTITION BY depname ORDER BY salary DESC"),
        ("ORDER BY salary ROWS BETWEEN 1 PRECEDING AND CURRENT ROW", "ORDER BY salary ROWS BETWEEN 2 PRECEDING AND CURRENT ROW"),
        ("PARTITION BY depname ORDER BY empno", "PARTITION BY depname ORDER BY salary"),
    ], start=1):
        cases.append(case(
            f"window_variant_{idx}",
            "Classic window variant",
            "Window",
            f"SELECT depname, empno, salary, ROW_NUMBER() OVER ({std_win}) FROM empsalary;",
            f"SELECT depname, empno, salary, ROW_NUMBER() OVER ({stu_win}) FROM empsalary;",
            ir_features=["select-basic", "window-row-number"],
            diff_clauses=["SELECT", "WINDOW"],
            diff_types=["window_over_changed"],
        ))

    for idx, (std_sql, stu_sql) in enumerate([
        ("SELECT TOP 5 name FROM world ORDER BY population DESC;", "SELECT name FROM world ORDER BY population DESC LIMIT 5;"),
        ("SELECT name FROM world ORDER BY population DESC FETCH FIRST 5 ROWS ONLY;", "SELECT name FROM world ORDER BY population DESC LIMIT 5;"),
        ("SELECT name FROM world ORDER BY population DESC LIMIT 5 OFFSET 2;", "SELECT name FROM world ORDER BY population DESC LIMIT 5 OFFSET 2;"),
        ("SELECT name FROM world ORDER BY population DESC LIMIT 5;", "SELECT name FROM world ORDER BY population DESC FETCH FIRST 5 ROWS ONLY;"),
    ], start=1):
        cases.append(case(
            f"dialect_boundary_variant_{idx}",
            "Classic dialect boundary variant",
            "Dialect Boundary",
            std_sql,
            stu_sql,
            ir_features=["select-basic", "order-by", "limit"],
            expect_no_diff=True,
        ))

    return cases


def select_final_100(cases: list[Case], seed: int) -> list[Case]:
    """Select 100 stratified-random balanced cases for the final structure gate."""
    rng = random.Random(seed)
    by_structure: dict[str, list[Case]] = defaultdict(list)
    for item in cases:
        by_structure[item["structure"]].append(item)

    selected: list[Case] = []
    for structure in sorted(by_structure):
        bucket = list(by_structure[structure])
        rng.shuffle(bucket)
        selected.extend(bucket[:4])

    extra_priority = ["Comparison", "CASE", "Correlated Subquery", "Dialect Boundary"]
    selected_ids = {item["id"] for item in selected}
    for structure in extra_priority:
        bucket = [item for item in by_structure.get(structure, []) if item["id"] not in selected_ids]
        rng.shuffle(bucket)
        for item in bucket:
            if item["id"] not in selected_ids:
                selected.append(item)
                selected_ids.add(item["id"])
                break

    if len(selected) != 100:
        raise RuntimeError(f"final100 selection produced {len(selected)} cases, expected 100")
    return selected


def build_online_final100_cases(seed: int) -> list[Case]:
    """Build a fresh 100-case online-inspired structure set.

    This intentionally does not reuse the original CASES pool.  The questions
    are modeled after public SQLZoo, PostgreSQL documentation, and common SQL
    tutorial examples, then mutated for structure-only standard/student diffs.
    """
    rng = random.Random(seed)
    out: list[Case] = []

    def add_many(structure: str, rows: list[tuple[str, str, str, dict[str, Any]]]) -> None:
        shuffled = list(rows)
        rng.shuffle(shuffled)
        for idx, (source, standard, student, expected) in enumerate(shuffled, start=1):
            out.append(case(
                f"online_{structure.lower().replace(' ', '_').replace('/', '_')}_{idx}",
                source,
                structure,
                standard,
                student,
                **expected,
            ))

    add_many("SELECT", [
        ("SQLZoo SELECT games style", "SELECT yr, city FROM games;", "SELECT yr FROM games;", {"ir_features": ["select-basic"], "diff_clauses": ["SELECT"], "diff_types": ["projection_changed"]}),
        ("SQLZoo Nobel style", "SELECT yr, winner, subject FROM nobel;", "SELECT winner, yr, subject FROM nobel;", {"ir_features": ["select-basic"], "diff_clauses": ["SELECT"], "diff_types": ["projection_changed"]}),
        ("PostgreSQL weather select style", "SELECT city, temp_lo, temp_hi FROM weather;", "SELECT city, temp_hi FROM weather;", {"ir_features": ["select-basic"], "diff_clauses": ["SELECT"], "diff_types": ["projection_changed"]}),
        ("SQL tutorial product select style", "SELECT product_name, unit_price FROM products;", "SELECT product_name FROM products;", {"ir_features": ["select-basic"], "diff_clauses": ["SELECT"], "diff_types": ["projection_changed"]}),
    ])

    add_many("DISTINCT", [
        ("SQLZoo Nobel DISTINCT style", "SELECT DISTINCT subject FROM nobel;", "SELECT subject FROM nobel;", {"ir_features": ["select-basic", "distinct"], "diff_clauses": ["DISTINCT"], "diff_types": ["distinct_changed"]}),
        ("SQLZoo BBC region style", "SELECT DISTINCT region FROM bbc;", "SELECT region FROM bbc;", {"ir_features": ["select-basic", "distinct"], "diff_clauses": ["DISTINCT"], "diff_types": ["distinct_changed"]}),
        ("PostgreSQL employee department style", "SELECT DISTINCT department_id FROM employees;", "SELECT department_id FROM employees;", {"ir_features": ["select-basic", "distinct"], "diff_clauses": ["DISTINCT"], "diff_types": ["distinct_changed"]}),
        ("SQLZoo city country style", "SELECT DISTINCT country FROM city;", "SELECT country FROM city;", {"ir_features": ["select-basic", "distinct"], "diff_clauses": ["DISTINCT"], "diff_types": ["distinct_changed"]}),
    ])

    add_many("WHERE", [
        ("SQLZoo WHERE filters style", "SELECT population FROM bbc WHERE name = 'Italy';", "SELECT population FROM bbc;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["comparison"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["where_changed", "predicate_missing"]}),
        ("PostgreSQL weather where style", "SELECT city FROM weather WHERE prcp > 0;", "SELECT city FROM weather;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["comparison"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["where_changed", "predicate_missing"]}),
        ("SQL tutorial employee where style", "SELECT first_name FROM employees WHERE job_id = 9;", "SELECT first_name FROM employees;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["comparison"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["where_changed", "predicate_missing"]}),
        ("SQLZoo movie where style", "SELECT title FROM movie WHERE yr = 1962;", "SELECT title FROM movie;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["comparison"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["where_changed", "predicate_missing"]}),
    ])

    add_many("Comparison", [
        ("SQLZoo BBC comparison style", "SELECT name FROM bbc WHERE area > 5000000;", "SELECT name FROM bbc WHERE area >= 5000000;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["comparison"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["comparison_operator_changed"]}),
        ("PostgreSQL weather comparison style", "SELECT city FROM weather WHERE temp_hi < 70;", "SELECT city FROM weather WHERE temp_hi <= 70;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["comparison"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["comparison_operator_changed"]}),
        ("SQL tutorial salary comparison style", "SELECT first_name FROM employees WHERE salary >= 9000;", "SELECT first_name FROM employees WHERE salary > 9000;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["comparison"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["comparison_operator_changed"]}),
        ("SQLZoo Nobel year comparison style", "SELECT winner FROM nobel WHERE yr = 1984;", "SELECT winner FROM nobel WHERE yr <> 1984;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["comparison"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["comparison_operator_changed"]}),
        ("PostgreSQL product comparison style", "SELECT product_name FROM products WHERE unit_price <= 20;", "SELECT product_name FROM products WHERE unit_price < 20;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["comparison"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["comparison_operator_changed"]}),
    ])

    add_many("NULL", [
        ("SQL tutorial IS NULL phone style", "SELECT first_name FROM employees WHERE phone_number IS NULL;", "SELECT first_name FROM employees WHERE phone_number = NULL;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["null_check"], "diff_clauses": ["WHERE", "PREDICATE", "NULL"], "diff_types": ["null_equality_changed"]}),
        ("SQLZoo teacher null style", "SELECT name FROM teacher WHERE dept IS NULL;", "SELECT name FROM teacher WHERE dept = NULL;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["null_check"], "diff_clauses": ["WHERE", "PREDICATE", "NULL"], "diff_types": ["null_equality_changed"]}),
        ("PostgreSQL nullable weather style", "SELECT city FROM weather WHERE prcp IS NULL;", "SELECT city FROM weather WHERE prcp <> NULL;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["null_check"], "diff_clauses": ["WHERE", "PREDICATE", "NULL"], "diff_types": ["null_equality_changed"]}),
        ("SQL tutorial order shipped null style", "SELECT order_id FROM orders WHERE shipped_at IS NULL;", "SELECT order_id FROM orders WHERE shipped_at = NULL;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["null_check"], "diff_clauses": ["WHERE", "PREDICATE", "NULL"], "diff_types": ["null_equality_changed"]}),
    ])

    add_many("IN", [
        ("SQLZoo WHERE filters IN style", "SELECT name FROM bbc WHERE name IN ('Ceylon', 'Iran', 'Sri Lanka');", "SELECT name FROM bbc WHERE name IN ('Iran', 'Sri Lanka');", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["in_list"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["in_list_member_removed"]}),
        ("SQL tutorial department IN style", "SELECT first_name FROM employees WHERE department_id IN (8, 9);", "SELECT first_name FROM employees WHERE department_id IN (8);", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["in_list"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["in_list_member_removed"]}),
        ("SQLZoo Nobel subject IN style", "SELECT winner FROM nobel WHERE subject IN ('Physics', 'Chemistry');", "SELECT winner FROM nobel WHERE subject IN ('Physics');", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["in_list"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["in_list_member_removed"]}),
        ("SQL tutorial product category IN style", "SELECT product_name FROM products WHERE category_id IN (1, 2, 3);", "SELECT product_name FROM products WHERE category_id IN (1, 2);", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["in_list"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["in_list_member_removed"]}),
    ])

    add_many("BETWEEN", [
        ("SQLZoo BETWEEN area style", "SELECT name FROM bbc WHERE area BETWEEN 207600 AND 244820;", "SELECT name FROM bbc WHERE area BETWEEN 207600 AND 300000;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["between"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["literal_changed"]}),
        ("SQL tutorial salary BETWEEN style", "SELECT first_name FROM employees WHERE salary BETWEEN 9000 AND 12000;", "SELECT first_name FROM employees WHERE salary BETWEEN 10000 AND 12000;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["between"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["literal_changed"]}),
        ("SQLZoo Nobel year BETWEEN style", "SELECT winner FROM nobel WHERE yr BETWEEN 1980 AND 1989;", "SELECT winner FROM nobel WHERE yr BETWEEN 1981 AND 1989;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["between"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["literal_changed"]}),
        ("PostgreSQL weather temp BETWEEN style", "SELECT city FROM weather WHERE temp_hi BETWEEN 60 AND 90;", "SELECT city FROM weather WHERE temp_hi BETWEEN 65 AND 90;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["between"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["literal_changed"]}),
    ])

    add_many("LIKE", [
        ("SQLZoo WHERE filters LIKE style", "SELECT name FROM bbc WHERE name LIKE 'D%';", "SELECT name FROM bbc WHERE name LIKE 'Da%';", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["like"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["literal_changed"]}),
        ("SQLZoo UNION Z names style", "SELECT name FROM actor WHERE name LIKE 'Z%';", "SELECT name FROM actor WHERE name LIKE 'Za%';", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["like"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["literal_changed"]}),
        ("SQL tutorial employee LIKE style", "SELECT first_name FROM employees WHERE last_name LIKE 'S%';", "SELECT first_name FROM employees WHERE last_name LIKE 'Sm%';", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["like"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["literal_changed"]}),
        ("SQLZoo movie LIKE style", "SELECT title FROM movie WHERE title LIKE '%Star%';", "SELECT title FROM movie WHERE title LIKE '%Stars%';", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["like"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["literal_changed"]}),
    ])

    add_many("Logic", [
        ("SQLZoo WHERE filters AND style", "SELECT name FROM bbc WHERE area < 2000 AND gdp > 5000000000;", "SELECT name FROM bbc WHERE area < 2000 OR gdp > 5000000000;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["logic"], "diff_clauses": ["WHERE", "LOGICAL"], "diff_types": ["logical_operator_changed"]}),
        ("PostgreSQL weather logic style", "SELECT city FROM weather WHERE temp_hi > 80 AND prcp = 0;", "SELECT city FROM weather WHERE temp_hi > 80 OR prcp = 0;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["logic"], "diff_clauses": ["WHERE", "LOGICAL"], "diff_types": ["logical_operator_changed"]}),
        ("SQL tutorial employee logic style", "SELECT first_name FROM employees WHERE salary > 9000 AND department_id = 8;", "SELECT first_name FROM employees WHERE salary > 9000 OR department_id = 8;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["logic"], "diff_clauses": ["WHERE", "LOGICAL"], "diff_types": ["logical_operator_changed"]}),
        ("SQLZoo Nobel logic style", "SELECT winner FROM nobel WHERE subject = 'Peace' AND yr > 2000;", "SELECT winner FROM nobel WHERE subject = 'Peace' OR yr > 2000;", {"ir_features": ["select-basic", "where"], "predicate_kinds": ["logic"], "diff_clauses": ["WHERE", "LOGICAL"], "diff_types": ["logical_operator_changed"]}),
    ])

    add_many("JOIN", [
        ("SQLZoo JOIN game-goal style", "SELECT game.mdate, goal.teamid FROM game JOIN goal ON game.id = goal.matchid;", "SELECT game.mdate FROM game;", {"ir_features": ["select-basic", "join-inner", "join-on"], "diff_clauses": ["SELECT", "JOIN", "JOIN ON"], "diff_types": ["join_missing", "join_on_changed"]}),
        ("PostgreSQL weather cities join style", "SELECT weather.city, cities.location FROM weather JOIN cities ON weather.city = cities.name;", "SELECT weather.city FROM weather;", {"ir_features": ["select-basic", "join-inner", "join-on"], "diff_clauses": ["SELECT", "JOIN", "JOIN ON"], "diff_types": ["join_missing", "join_on_changed"]}),
        ("SQL tutorial employee department join style", "SELECT e.first_name, d.department_name FROM employees e JOIN departments d ON e.department_id = d.department_id;", "SELECT e.first_name FROM employees e;", {"ir_features": ["select-basic", "join-inner", "join-on"], "diff_clauses": ["SELECT", "JOIN", "JOIN ON"], "diff_types": ["join_missing", "join_on_changed"]}),
        ("SQLZoo movie casting join style", "SELECT movie.title, actor.name FROM movie JOIN casting ON movie.id = casting.movieid JOIN actor ON casting.actorid = actor.id;", "SELECT movie.title FROM movie;", {"ir_features": ["select-basic", "join-inner", "join-on"], "diff_clauses": ["SELECT", "JOIN", "JOIN ON"], "diff_types": ["join_missing", "join_on_changed"]}),
    ])

    add_many("JOIN ON", [
        ("SQLZoo JOIN ON matchid style", "SELECT game.mdate, goal.teamid FROM game JOIN goal ON game.id = goal.matchid;", "SELECT game.mdate, goal.teamid FROM game JOIN goal ON game.id = goal.playerid;", {"ir_features": ["select-basic", "join-inner", "join-on"], "diff_clauses": ["JOIN ON"], "diff_types": ["join_on_changed"]}),
        ("PostgreSQL weather cities ON style", "SELECT weather.city, cities.location FROM weather JOIN cities ON weather.city = cities.name;", "SELECT weather.city, cities.location FROM weather JOIN cities ON weather.city = cities.location;", {"ir_features": ["select-basic", "join-inner", "join-on"], "diff_clauses": ["JOIN ON"], "diff_types": ["join_on_changed"]}),
        ("SQL tutorial employee department ON style", "SELECT e.first_name, d.department_name FROM employees e JOIN departments d ON e.department_id = d.department_id;", "SELECT e.first_name, d.department_name FROM employees e JOIN departments d ON e.employee_id = d.department_id;", {"ir_features": ["select-basic", "join-inner", "join-on"], "diff_clauses": ["JOIN ON"], "diff_types": ["join_on_changed"]}),
        ("SQLZoo movie casting ON style", "SELECT actor.name FROM casting JOIN actor ON casting.actorid = actor.id;", "SELECT actor.name FROM casting JOIN actor ON casting.movieid = actor.id;", {"ir_features": ["select-basic", "join-inner", "join-on"], "diff_clauses": ["JOIN ON"], "diff_types": ["join_on_changed"]}),
    ])

    add_many("GROUP BY", [
        ("SQLZoo SUM COUNT continent style", "SELECT region, COUNT(*) FROM bbc GROUP BY region;", "SELECT region, COUNT(*) FROM bbc GROUP BY name;", {"ir_features": ["select-basic", "group-by", "aggregate", "agg-count"], "diff_clauses": ["GROUP BY"], "diff_types": ["group_by_changed"]}),
        ("PostgreSQL weather city group style", "SELECT city, MAX(temp_hi) FROM weather GROUP BY city;", "SELECT city, MAX(temp_hi) FROM weather GROUP BY temp_hi;", {"ir_features": ["select-basic", "group-by", "aggregate"], "diff_clauses": ["GROUP BY"], "diff_types": ["group_by_changed"]}),
        ("SQL tutorial employee department group style", "SELECT department_id, COUNT(*) FROM employees GROUP BY department_id;", "SELECT department_id, COUNT(*) FROM employees GROUP BY job_id;", {"ir_features": ["select-basic", "group-by", "aggregate", "agg-count"], "diff_clauses": ["GROUP BY"], "diff_types": ["group_by_changed"]}),
        ("SQLZoo Nobel subject group style", "SELECT subject, COUNT(*) FROM nobel GROUP BY subject;", "SELECT subject, COUNT(*) FROM nobel GROUP BY yr;", {"ir_features": ["select-basic", "group-by", "aggregate", "agg-count"], "diff_clauses": ["GROUP BY"], "diff_types": ["group_by_changed"]}),
    ])

    add_many("HAVING", [
        ("SQLZoo SUM COUNT having style", "SELECT region FROM bbc GROUP BY region HAVING SUM(population) > 100000000;", "SELECT region FROM bbc GROUP BY region HAVING SUM(population) >= 100000000;", {"ir_features": ["select-basic", "group-by", "having", "aggregate"], "predicate_kinds": ["comparison"], "diff_clauses": ["HAVING", "PREDICATE"], "diff_types": ["comparison_operator_changed"]}),
        ("PostgreSQL weather having style", "SELECT city FROM weather GROUP BY city HAVING MAX(temp_hi) > 80;", "SELECT city FROM weather GROUP BY city HAVING MAX(temp_hi) >= 80;", {"ir_features": ["select-basic", "group-by", "having", "aggregate"], "predicate_kinds": ["comparison"], "diff_clauses": ["HAVING", "PREDICATE"], "diff_types": ["comparison_operator_changed"]}),
        ("SQL tutorial department having style", "SELECT department_id FROM employees GROUP BY department_id HAVING COUNT(*) > 5;", "SELECT department_id FROM employees GROUP BY department_id HAVING COUNT(*) >= 5;", {"ir_features": ["select-basic", "group-by", "having", "aggregate", "agg-count"], "predicate_kinds": ["comparison"], "diff_clauses": ["HAVING", "PREDICATE"], "diff_types": ["comparison_operator_changed"]}),
        ("SQLZoo Nobel having style", "SELECT subject FROM nobel GROUP BY subject HAVING COUNT(*) > 10;", "SELECT subject FROM nobel GROUP BY subject HAVING COUNT(*) < 10;", {"ir_features": ["select-basic", "group-by", "having", "aggregate", "agg-count"], "predicate_kinds": ["comparison"], "diff_clauses": ["HAVING", "PREDICATE"], "diff_types": ["comparison_operator_changed"]}),
    ])

    add_many("Aggregate", [
        ("PostgreSQL weather aggregate style", "SELECT city, MAX(temp_hi) FROM weather GROUP BY city;", "SELECT city, MIN(temp_hi) FROM weather GROUP BY city;", {"ir_features": ["select-basic", "group-by", "aggregate"], "diff_clauses": ["SELECT", "AGGREGATE"], "diff_types": ["aggregate_function_changed"]}),
        ("SQL tutorial employee aggregate style", "SELECT department_id, AVG(salary) FROM employees GROUP BY department_id;", "SELECT department_id, SUM(salary) FROM employees GROUP BY department_id;", {"ir_features": ["select-basic", "group-by", "aggregate"], "diff_clauses": ["SELECT", "AGGREGATE"], "diff_types": ["aggregate_function_changed"]}),
        ("SQLZoo BBC aggregate style", "SELECT region, SUM(population) FROM bbc GROUP BY region;", "SELECT region, AVG(population) FROM bbc GROUP BY region;", {"ir_features": ["select-basic", "group-by", "aggregate"], "diff_clauses": ["SELECT", "AGGREGATE"], "diff_types": ["aggregate_function_changed"]}),
        ("SQLZoo Nobel count aggregate style", "SELECT subject, COUNT(winner) FROM nobel GROUP BY subject;", "SELECT subject, SUM(winner) FROM nobel GROUP BY subject;", {"ir_features": ["select-basic", "group-by", "aggregate", "agg-count"], "diff_clauses": ["SELECT", "AGGREGATE"], "diff_types": ["aggregate_function_changed"]}),
    ])

    add_many("ORDER BY", [
        ("PostgreSQL weather order style", "SELECT city, temp_hi FROM weather ORDER BY temp_hi DESC;", "SELECT city, temp_hi FROM weather ORDER BY temp_hi ASC;", {"ir_features": ["select-basic", "order-by"], "diff_clauses": ["ORDER BY"], "diff_types": ["order_by_changed"]}),
        ("SQLZoo BBC order style", "SELECT name, population FROM bbc ORDER BY population DESC;", "SELECT name, population FROM bbc ORDER BY population ASC;", {"ir_features": ["select-basic", "order-by"], "diff_clauses": ["ORDER BY"], "diff_types": ["order_by_changed"]}),
        ("SQL tutorial employee order style", "SELECT first_name, salary FROM employees ORDER BY salary DESC, first_name ASC;", "SELECT first_name, salary FROM employees ORDER BY salary ASC, first_name ASC;", {"ir_features": ["select-basic", "order-by"], "diff_clauses": ["ORDER BY"], "diff_types": ["order_by_changed"]}),
        ("PostgreSQL nulls order style", "SELECT name FROM bbc ORDER BY name ASC NULLS LAST;", "SELECT name FROM bbc ORDER BY name ASC NULLS FIRST;", {"ir_features": ["select-basic", "order-by"], "diff_clauses": ["ORDER BY"], "diff_types": ["order_by_changed"]}),
    ])

    add_many("LIMIT / OFFSET", [
        ("PostgreSQL SELECT limit style", "SELECT name FROM bbc ORDER BY population DESC LIMIT 10;", "SELECT name FROM bbc ORDER BY population DESC LIMIT 5;", {"ir_features": ["select-basic", "order-by", "limit"], "diff_clauses": ["LIMIT"], "diff_types": ["limit_changed"]}),
        ("PostgreSQL OFFSET style", "SELECT city FROM weather ORDER BY city LIMIT 5 OFFSET 10;", "SELECT city FROM weather ORDER BY city LIMIT 5 OFFSET 5;", {"ir_features": ["select-basic", "order-by", "limit"], "diff_clauses": ["LIMIT"], "diff_types": ["limit_changed"]}),
        ("SQL tutorial employee limit style", "SELECT first_name FROM employees ORDER BY salary DESC LIMIT 3;", "SELECT first_name FROM employees ORDER BY salary DESC LIMIT 4;", {"ir_features": ["select-basic", "order-by", "limit"], "diff_clauses": ["LIMIT"], "diff_types": ["limit_changed"]}),
        ("PostgreSQL FETCH boundary style", "SELECT title FROM movie ORDER BY yr DESC FETCH FIRST 5 ROWS ONLY;", "SELECT title FROM movie ORDER BY yr DESC FETCH FIRST 8 ROWS ONLY;", {"ir_features": ["select-basic", "order-by", "limit"], "diff_clauses": ["LIMIT"], "diff_types": ["limit_changed"]}),
    ])

    add_many("Subquery", [
        ("SQLZoo SELECT SELECT IN style", "SELECT name FROM world WHERE continent IN (SELECT continent FROM world WHERE name = 'Bhutan');", "SELECT name FROM world WHERE continent IN (SELECT continent FROM world WHERE name = 'Nepal');", {"ir_features": ["select-basic", "where", "subquery-scalar"], "predicate_kinds": ["in_subquery"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["literal_changed"]}),
        ("SQLZoo derived table style", "SELECT name FROM (SELECT name, gdp/population AS gdp_per_capita FROM world) x WHERE gdp_per_capita > 20000;", "SELECT name FROM (SELECT name, gdp/population AS gdp_per_capita FROM world) x WHERE gdp_per_capita > 30000;", {"ir_features": ["select-basic", "where", "subquery-scalar"], "predicate_kinds": ["comparison"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["literal_changed"]}),
        ("SQL tutorial subquery department style", "SELECT first_name FROM employees WHERE department_id IN (SELECT department_id FROM departments WHERE location_id = 1700);", "SELECT first_name FROM employees WHERE department_id IN (SELECT department_id FROM departments WHERE location_id = 1800);", {"ir_features": ["select-basic", "where", "subquery-scalar"], "predicate_kinds": ["in_subquery"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["literal_changed"]}),
        ("PostgreSQL scalar subquery style", "SELECT city FROM weather WHERE temp_hi > (SELECT AVG(temp_hi) FROM weather);", "SELECT city FROM weather WHERE temp_hi >= (SELECT AVG(temp_hi) FROM weather);", {"ir_features": ["select-basic", "where", "subquery-scalar", "aggregate"], "predicate_kinds": ["comparison"], "diff_clauses": ["WHERE", "PREDICATE"], "diff_types": ["comparison_operator_changed"]}),
    ])

    add_many("Correlated Subquery", [
        ("SQL tutorial correlated employee style", "SELECT first_name FROM employees e1 WHERE salary > (SELECT AVG(salary) FROM employees e2 WHERE e2.department_id = e1.department_id);", "SELECT first_name FROM employees e1 WHERE salary >= (SELECT AVG(salary) FROM employees e2 WHERE e2.department_id = e1.department_id);", {"ir_features": ["select-basic", "where", "subquery-correlated", "subquery-scalar", "aggregate"], "predicate_kinds": ["comparison"], "diff_clauses": ["WHERE", "CORRELATED SUBQUERY", "PREDICATE"], "diff_types": ["correlated_predicate_changed"]}),
        ("SQLZoo correlated region style", "SELECT name FROM bbc b1 WHERE population > 5 * (SELECT AVG(population) FROM bbc b2 WHERE b2.region = b1.region);", "SELECT name FROM bbc b1 WHERE population > 6 * (SELECT AVG(population) FROM bbc b2 WHERE b2.region = b1.region);", {"ir_features": ["select-basic", "where", "subquery-correlated", "subquery-scalar", "aggregate"], "predicate_kinds": ["comparison"], "diff_clauses": ["WHERE", "CORRELATED SUBQUERY", "PREDICATE"], "diff_types": ["correlated_predicate_changed"]}),
        ("SQL tutorial exists correlated style", "SELECT department_name FROM departments d WHERE EXISTS (SELECT 1 FROM employees e WHERE e.department_id = d.department_id AND e.salary > 9000);", "SELECT department_name FROM departments d WHERE EXISTS (SELECT 1 FROM employees e WHERE e.department_id = d.department_id AND e.salary > 10000);", {"ir_features": ["select-basic", "where", "subquery-correlated", "subquery-scalar"], "predicate_kinds": ["raw"], "diff_clauses": ["WHERE", "CORRELATED SUBQUERY", "PREDICATE"], "diff_types": ["correlated_predicate_changed"]}),
        ("PostgreSQL correlated weather style", "SELECT city FROM weather w1 WHERE temp_hi > (SELECT AVG(temp_hi) FROM weather w2 WHERE w2.city = w1.city);", "SELECT city FROM weather w1 WHERE temp_hi < (SELECT AVG(temp_hi) FROM weather w2 WHERE w2.city = w1.city);", {"ir_features": ["select-basic", "where", "subquery-correlated", "subquery-scalar", "aggregate"], "predicate_kinds": ["comparison"], "diff_clauses": ["WHERE", "CORRELATED SUBQUERY", "PREDICATE"], "diff_types": ["correlated_predicate_changed"]}),
        ("SQL tutorial correlated count style", "SELECT product_name FROM products p WHERE unit_price > (SELECT AVG(unit_price) FROM products p2 WHERE p2.category_id = p.category_id);", "SELECT product_name FROM products p WHERE unit_price >= (SELECT AVG(unit_price) FROM products p2 WHERE p2.category_id = p.category_id);", {"ir_features": ["select-basic", "where", "subquery-correlated", "subquery-scalar", "aggregate"], "predicate_kinds": ["comparison"], "diff_clauses": ["WHERE", "CORRELATED SUBQUERY", "PREDICATE"], "diff_types": ["correlated_predicate_changed"]}),
    ])

    add_many("CTE", [
        ("PostgreSQL WITH select style", "WITH regional_sales AS (SELECT region, SUM(amount) AS total_sales FROM orders GROUP BY region HAVING SUM(amount) > 1000) SELECT region FROM regional_sales;", "WITH regional_sales AS (SELECT region, SUM(amount) AS total_sales FROM orders GROUP BY region HAVING SUM(amount) > 2000) SELECT region FROM regional_sales;", {"ir_features": ["select-basic", "cte", "having", "aggregate", "group-by"], "predicate_kinds": ["comparison"], "diff_clauses": ["HAVING", "PREDICATE", "CTE"], "diff_types": ["cte_changed", "literal_changed"]}),
        ("PostgreSQL weather CTE style", "WITH rainy AS (SELECT city FROM weather WHERE prcp > 0) SELECT city FROM rainy;", "WITH rainy AS (SELECT city FROM weather WHERE prcp >= 0) SELECT city FROM rainy;", {"ir_features": ["select-basic", "cte"], "diff_clauses": ["WHERE", "PREDICATE", "CTE"], "diff_types": ["cte_changed", "comparison_operator_changed"]}),
        ("SQL tutorial employee CTE style", "WITH high_paid AS (SELECT employee_id FROM employees WHERE salary > 9000) SELECT employee_id FROM high_paid;", "WITH high_paid AS (SELECT employee_id FROM employees WHERE salary > 10000) SELECT employee_id FROM high_paid;", {"ir_features": ["select-basic", "cte"], "diff_clauses": ["WHERE", "PREDICATE", "CTE"], "diff_types": ["cte_changed", "literal_changed"]}),
        ("SQLZoo derived to CTE style", "WITH per_capita AS (SELECT name, gdp/population AS v FROM world WHERE population > 1000000) SELECT name FROM per_capita WHERE v > 20000;", "WITH per_capita AS (SELECT name, gdp/population AS v FROM world WHERE population > 2000000) SELECT name FROM per_capita WHERE v > 20000;", {"ir_features": ["select-basic", "cte", "where"], "predicate_kinds": ["comparison"], "diff_clauses": ["PREDICATE", "CTE"], "diff_types": ["cte_changed", "literal_changed"]}),
    ])

    add_many("Recursive CTE", [
        ("PostgreSQL recursive numbers style", "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 10) SELECT SUM(n) FROM t;", "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 11) SELECT SUM(n) FROM t;", {"ir_features": ["select-basic", "cte-recursive", "union", "aggregate"], "diff_clauses": ["WHERE", "PREDICATE", "CTE_RECURSIVE"], "diff_types": ["recursive_cte_changed", "literal_changed"]}),
        ("PostgreSQL recursive sequence style", "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 2 FROM nums WHERE n < 20) SELECT n FROM nums;", "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 2 FROM nums WHERE n < 18) SELECT n FROM nums;", {"ir_features": ["select-basic", "cte-recursive", "union"], "diff_clauses": ["WHERE", "PREDICATE", "CTE_RECURSIVE"], "diff_types": ["recursive_cte_changed", "literal_changed"]}),
        ("PostgreSQL recursive tree style", "WITH RECURSIVE search_tree(id, depth) AS (SELECT id, 0 FROM tree WHERE parent IS NULL UNION ALL SELECT t.id, st.depth + 1 FROM tree t JOIN search_tree st ON t.parent = st.id WHERE st.depth < 5) SELECT id FROM search_tree;", "WITH RECURSIVE search_tree(id, depth) AS (SELECT id, 0 FROM tree WHERE parent IS NULL UNION ALL SELECT t.id, st.depth + 1 FROM tree t JOIN search_tree st ON t.parent = st.id WHERE st.depth < 4) SELECT id FROM search_tree;", {"ir_features": ["select-basic", "cte-recursive", "union", "where", "join-inner", "join-on"], "diff_clauses": ["PREDICATE", "CTE_RECURSIVE"], "diff_types": ["recursive_cte_changed", "literal_changed"]}),
        ("PostgreSQL recursive path style", "WITH RECURSIVE path(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM path WHERE n < 100) SELECT n FROM path;", "WITH RECURSIVE path(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM path WHERE n < 50) SELECT n FROM path;", {"ir_features": ["select-basic", "cte-recursive", "union"], "diff_clauses": ["WHERE", "PREDICATE", "CTE_RECURSIVE"], "diff_types": ["recursive_cte_changed", "literal_changed"]}),
    ])

    add_many("Set Operation", [
        ("SQLZoo UNION bbc actor style", "SELECT name FROM bbc WHERE name LIKE 'Z%' UNION SELECT name FROM actor WHERE name LIKE 'Z%';", "SELECT name FROM bbc WHERE name LIKE 'Z%' UNION ALL SELECT name FROM actor WHERE name LIKE 'Z%';", {"ir_features": ["select-basic", "union"], "diff_clauses": ["UNION"], "diff_types": ["set_operator_changed"]}),
        ("PostgreSQL INTERSECT style", "SELECT city FROM weather INTERSECT SELECT name FROM cities;", "SELECT city FROM weather UNION SELECT name FROM cities;", {"ir_features": ["select-basic", "intersect"], "diff_clauses": ["INTERSECT"], "diff_types": ["set_operator_changed"]}),
        ("PostgreSQL EXCEPT style", "SELECT name FROM bbc EXCEPT SELECT name FROM actor;", "SELECT name FROM bbc UNION SELECT name FROM actor;", {"ir_features": ["select-basic", "except"], "diff_clauses": ["EXCEPT"], "diff_types": ["set_operator_changed"]}),
        ("SQL tutorial set union style", "SELECT customer_id FROM customers UNION SELECT customer_id FROM orders;", "SELECT customer_id FROM customers INTERSECT SELECT customer_id FROM orders;", {"ir_features": ["select-basic", "union"], "diff_clauses": ["UNION"], "diff_types": ["set_operator_changed"]}),
    ])

    add_many("CASE", [
        ("PostgreSQL CASE expression style", "SELECT a, CASE WHEN a = 1 THEN 'one' WHEN a = 2 THEN 'two' ELSE 'other' END FROM test;", "SELECT a, CASE WHEN a = 1 THEN 'one' WHEN a = 3 THEN 'two' ELSE 'other' END FROM test;", {"ir_features": ["select-basic", "case"], "predicate_kinds": ["comparison"], "diff_clauses": ["CASE"], "diff_types": ["case_changed"], "allowed_extra_clauses": ["SELECT", "PREDICATE"]}),
        ("PostgreSQL CASE salary band style", "SELECT first_name, CASE WHEN salary > 10000 THEN 'high' ELSE 'normal' END FROM employees;", "SELECT first_name, CASE WHEN salary >= 10000 THEN 'high' ELSE 'normal' END FROM employees;", {"ir_features": ["select-basic", "case"], "predicate_kinds": ["comparison"], "diff_clauses": ["CASE"], "diff_types": ["case_changed"], "allowed_extra_clauses": ["SELECT", "PREDICATE"]}),
        ("SQL tutorial CASE order status style", "SELECT order_id, CASE WHEN shipped_at IS NULL THEN 'open' ELSE 'shipped' END FROM orders;", "SELECT order_id, CASE WHEN shipped_at IS NOT NULL THEN 'open' ELSE 'shipped' END FROM orders;", {"ir_features": ["select-basic", "case"], "predicate_kinds": ["null_check"], "diff_clauses": ["CASE"], "diff_types": ["case_changed"], "allowed_extra_clauses": ["SELECT", "PREDICATE"]}),
        ("SQLZoo CASE population style", "SELECT name, CASE WHEN population > 100000000 THEN 'big' ELSE 'small' END FROM bbc;", "SELECT name, CASE WHEN population > 50000000 THEN 'big' ELSE 'small' END FROM bbc;", {"ir_features": ["select-basic", "case"], "predicate_kinds": ["comparison"], "diff_clauses": ["CASE"], "diff_types": ["case_changed"], "allowed_extra_clauses": ["SELECT", "PREDICATE"]}),
        ("PostgreSQL simple CASE style", "SELECT a, CASE a WHEN 1 THEN 'one' WHEN 2 THEN 'two' ELSE 'other' END FROM test;", "SELECT a, CASE a WHEN 1 THEN 'one' WHEN 3 THEN 'two' ELSE 'other' END FROM test;", {"ir_features": ["select-basic", "case"], "diff_clauses": ["CASE"], "diff_types": ["case_changed"], "allowed_extra_clauses": ["SELECT", "PREDICATE"]}),
    ])

    add_many("Window", [
        ("PostgreSQL row_number window style", "SELECT depname, empno, salary, ROW_NUMBER() OVER (PARTITION BY depname ORDER BY salary DESC) FROM empsalary;", "SELECT depname, empno, salary, ROW_NUMBER() OVER (ORDER BY salary DESC) FROM empsalary;", {"ir_features": ["select-basic", "window-row-number"], "diff_clauses": ["SELECT", "WINDOW"], "diff_types": ["window_over_changed"]}),
        ("PostgreSQL named window style", "SELECT SUM(salary) OVER w FROM empsalary WINDOW w AS (PARTITION BY depname ORDER BY salary DESC);", "SELECT SUM(salary) OVER w FROM empsalary WINDOW w AS (PARTITION BY depname ORDER BY salary ASC);", {"ir_features": ["select-basic", "window-row-number", "aggregate"], "diff_clauses": ["WINDOW"], "diff_types": ["window_over_changed"]}),
        ("PostgreSQL rank window style", "SELECT depname, RANK() OVER (PARTITION BY depname ORDER BY salary DESC) FROM empsalary;", "SELECT depname, RANK() OVER (PARTITION BY depname ORDER BY empno DESC) FROM empsalary;", {"ir_features": ["select-basic", "window-row-number"], "diff_clauses": ["SELECT", "WINDOW"], "diff_types": ["window_over_changed"]}),
        ("PostgreSQL window frame style", "SELECT depname, SUM(salary) OVER (PARTITION BY depname ORDER BY salary ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM empsalary;", "SELECT depname, SUM(salary) OVER (PARTITION BY depname ORDER BY salary ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) FROM empsalary;", {"ir_features": ["select-basic", "window-row-number", "aggregate"], "diff_clauses": ["SELECT", "WINDOW"], "diff_types": ["window_over_changed"]}),
    ])

    add_many("Dialect Boundary", [
        ("PostgreSQL FETCH vs LIMIT style", "SELECT name FROM bbc ORDER BY population DESC FETCH FIRST 5 ROWS ONLY;", "SELECT name FROM bbc ORDER BY population DESC LIMIT 5;", {"ir_features": ["select-basic", "order-by", "limit"], "expect_no_diff": True}),
        ("T-SQL TOP vs LIMIT style", "SELECT TOP 10 winner FROM nobel ORDER BY yr DESC;", "SELECT winner FROM nobel ORDER BY yr DESC LIMIT 10;", {"ir_features": ["select-basic", "order-by", "limit"], "expect_no_diff": True}),
        ("PostgreSQL LIMIT OFFSET equivalent style", "SELECT city FROM weather ORDER BY city LIMIT 5 OFFSET 2;", "SELECT city FROM weather ORDER BY city LIMIT 5 OFFSET 2;", {"ir_features": ["select-basic", "order-by", "limit"], "expect_no_diff": True}),
        ("PostgreSQL FETCH reverse style", "SELECT title FROM movie ORDER BY yr DESC LIMIT 7;", "SELECT title FROM movie ORDER BY yr DESC FETCH FIRST 7 ROWS ONLY;", {"ir_features": ["select-basic", "order-by", "limit"], "expect_no_diff": True}),
        ("PostgreSQL LIMIT ALL boundary style", "SELECT name FROM bbc ORDER BY name LIMIT 5;", "SELECT name FROM bbc ORDER BY name FETCH FIRST 5 ROWS ONLY;", {"ir_features": ["select-basic", "order-by", "limit"], "expect_no_diff": True}),
    ])

    if len(out) != 100:
        raise RuntimeError(f"online final100 produced {len(out)} cases")
    rng.shuffle(out)
    return out


def predicate_kinds(ir: SQLStructureIR) -> set[str]:
    return {str(item.get("kind")) for item in ir.predicate_ir if item.get("kind")}


def logic_ops(ir: SQLStructureIR) -> set[str]:
    return {str(item.get("operator")) for item in ir.predicate_ir if item.get("kind") == "logic"}


def run_case(item: Case) -> dict[str, Any]:
    std_ast = _parse_sql(item["standard"])
    stu_ast = _parse_sql(item["student"])
    parse_ok = std_ast is not None and stu_ast is not None
    ir = SQLStructureIR.from_ast(std_ast) if std_ast is not None else SQLStructureIR()
    features = set(ir.feature_kps())
    pred = predicate_kinds(ir)
    ops = logic_ops(ir)
    diffs = extract_ast_diffs(item["standard"], item["student"]) if parse_ok else []
    diff_dicts = [diff.to_dict() for diff in diffs]
    clauses = {diff.clause_category for diff in diffs}
    types = {diff.diff_type for diff in diffs}
    expected = item["expected"]

    missing_features = sorted(set(expected["ir_features"]) - features)
    missing_predicates = sorted(set(expected["predicate_kinds"]) - pred)
    missing_logic = sorted(set(expected["logic_ops"]) - ops)
    if expected["expect_no_diff"]:
        missing_clauses: list[str] = []
        missing_types: list[str] = []
        unexpected_clauses = sorted(clauses)
    else:
        missing_clauses = sorted(set(expected["diff_clauses"]) - clauses)
        missing_types = sorted(set(expected["diff_types"]) - types)
        allowed = set(expected["diff_clauses"]) | set(expected["allowed_extra_clauses"])
        unexpected_clauses = sorted(clauses - allowed)

    hard_failures = {
        "parse_failed": not parse_ok,
        "missing_ir_features": missing_features,
        "missing_predicate_kinds": missing_predicates,
        "missing_logic_ops": missing_logic,
        "missing_diff_clauses": missing_clauses,
        "missing_diff_types": missing_types,
        "unexpected_diff_when_none_expected": unexpected_clauses if expected["expect_no_diff"] else [],
    }
    hard_pass = parse_ok and not any(bool(value) for value in hard_failures.values())
    noise = [] if expected["expect_no_diff"] else unexpected_clauses
    strict_pass = hard_pass and not noise
    return {
        **item,
        "parse_ok": parse_ok,
        "ir_features": sorted(features),
        "predicate_kinds": sorted(pred),
        "logic_ops": sorted(ops),
        "diff_clauses": sorted(clauses),
        "diff_types": sorted(types),
        "ast_diffs": diff_dicts,
        "hard_failures": hard_failures,
        "noise_unexpected_clauses": noise,
        "hard_pass": hard_pass,
        "strict_pass": strict_pass,
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_structure: dict[str, Counter[str]] = defaultdict(Counter)
    for result in results:
        by_structure[result["structure"]]["total"] += 1
        by_structure[result["structure"]]["hard_pass"] += int(result["hard_pass"])
        by_structure[result["structure"]]["strict_pass"] += int(result["strict_pass"])
    failures = [r for r in results if not r["hard_pass"]]
    noise = [r for r in results if r["hard_pass"] and not r["strict_pass"]]
    return {
        "total": len(results),
        "hard_pass": sum(1 for r in results if r["hard_pass"]),
        "strict_pass": sum(1 for r in results if r["strict_pass"]),
        "hard_pass_rate": _pct(sum(1 for r in results if r["hard_pass"]), len(results)),
        "strict_pass_rate": _pct(sum(1 for r in results if r["strict_pass"]), len(results)),
        "failures": len(failures),
        "noise_only": len(noise),
        "by_structure": {key: dict(value) for key, value in sorted(by_structure.items())},
        "failure_ids": [r["id"] for r in failures],
        "noise_ids": [r["id"] for r in noise],
    }


def _pct(num: int, den: int) -> float:
    return num / den * 100.0 if den else 0.0


def render_markdown(results: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    passed = [result for result in results if result["strict_pass"]]
    failed = [result for result in results if not result["strict_pass"]]
    lines = [
        "# Classic Structure Holdout Tests",
        "",
        "Structure-only holdout inspired by SQLZoo and PostgreSQL teaching examples.",
        "",
        f"- Total: `{summary['total']}`",
        f"- Hard pass: `{summary['hard_pass']}` (`{summary['hard_pass_rate']:.2f}%`)",
        f"- Strict pass: `{summary['strict_pass']}` (`{summary['strict_pass_rate']:.2f}%`)",
        f"- Hard failures: `{summary['failures']}`",
        f"- Noise only: `{summary['noise_only']}`",
        "",
        "## Final Pass / Fail",
        "",
        f"- 能够通过的样例：`{len(passed)}`",
        f"- 不能通过的样例：`{len(failed)}`",
        "",
        "### 不能通过的样例",
        "",
        "无。" if not failed else "",
    ]
    for result in failed:
        lines.append(
            f"- `{result['id']}` / {result['structure']}: "
            f"{json.dumps(result['hard_failures'], ensure_ascii=False)}; "
            f"noise={result['noise_unexpected_clauses']}"
        )
    lines.extend([
        "",
        "| id | structure | source | hard | strict | missing / noise |",
        "|---|---|---|---:|---:|---|",
    ])
    for result in results:
        issues = []
        for key, value in result["hard_failures"].items():
            if value:
                issues.append(f"{key}={value}")
        if result["noise_unexpected_clauses"]:
            issues.append(f"noise={result['noise_unexpected_clauses']}")
        lines.append(
            f"| `{result['id']}` | {result['structure']} | {result['source']} | "
            f"{'yes' if result['hard_pass'] else 'NO'} | {'yes' if result['strict_pass'] else 'NO'} | "
            f"{'; '.join(issues) if issues else ''} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run classic structure-only holdout tests.")
    parser.add_argument("--final100", action="store_true", help="Run the stratified-random final 100-case gate.")
    parser.add_argument("--online100", action="store_true", help="Run the fresh online-inspired 100-case gate without using the old case pool.")
    parser.add_argument("--seed", type=int, default=20260723)
    args = parser.parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.online100:
        selected_cases = build_online_final100_cases(args.seed)
    elif args.final100:
        selected_cases = select_final_100(build_cases(), args.seed)
    else:
        selected_cases = build_cases()
    results = [run_case(item) for item in selected_cases]
    summary = summarize(results)
    payload = {"summary": summary, "results": results}
    suffix = "_online100" if args.online100 else "_final100" if args.final100 else ""
    json_path = OUTPUT_DIR / f"classic_structure_holdout_report{suffix}.json"
    md_path = OUTPUT_DIR / f"classic_structure_holdout_report{suffix}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(results, summary), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")


if __name__ == "__main__":
    main()
