"""Structure frontier tests.

This suite is intentionally adversarial.  It is not a pass gate.  It mixes
common supported structures with advanced SQL/PostgreSQL/dialect features to
show the current boundary of SQLStructureIR and ASTDiff.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from run_classic_structure_holdout_tests import case, render_markdown, run_case

OUTPUT_DIR = PROJECT_ROOT / "data_construct_test" / "outputs"


FRONTIER_CASES = [
    case("frontier_distinct_on", "PostgreSQL DISTINCT ON", "DISTINCT ON",
         "SELECT DISTINCT ON (location) location, time, report FROM weather_reports ORDER BY location, time DESC;",
         "SELECT DISTINCT location, time, report FROM weather_reports ORDER BY location, time DESC;",
         ir_features=["select-basic", "distinct", "distinct-on"], diff_clauses=["DISTINCT ON"], diff_types=["distinct_on_changed"]),
    case("frontier_ilike", "PostgreSQL ILIKE", "ILIKE",
         "SELECT name FROM customer WHERE name ILIKE 'ann%';",
         "SELECT name FROM customer WHERE name LIKE 'ann%';",
         ir_features=["select-basic", "where"], predicate_kinds=["ilike"], diff_clauses=["PREDICATE"], diff_types=["comparison_operator_changed"]),
    case("frontier_similar_to", "PostgreSQL SIMILAR TO", "SIMILAR TO",
         "SELECT name FROM customer WHERE name SIMILAR TO '(Ann|Anne)%';",
         "SELECT name FROM customer WHERE name LIKE 'Ann%';",
         ir_features=["select-basic", "where"], predicate_kinds=["similar_to"], diff_clauses=["PREDICATE"], diff_types=["comparison_operator_changed"]),
    case("frontier_regex_match", "PostgreSQL regex match", "Regex",
         "SELECT name FROM customer WHERE name ~ '^A';",
         "SELECT name FROM customer WHERE name LIKE 'A%';",
         ir_features=["select-basic", "where"], predicate_kinds=["regex"], diff_clauses=["PREDICATE"], diff_types=["comparison_operator_changed"]),
    case("frontier_rollup", "PostgreSQL GROUP BY ROLLUP", "ROLLUP",
         "SELECT region, product, SUM(amount) FROM sales GROUP BY ROLLUP(region, product);",
         "SELECT region, product, SUM(amount) FROM sales GROUP BY region, product;",
         ir_features=["select-basic", "group-by", "rollup", "aggregate"], diff_clauses=["GROUP BY", "ROLLUP"], diff_types=["rollup_changed"]),
    case("frontier_cube", "PostgreSQL GROUP BY CUBE", "CUBE",
         "SELECT region, product, SUM(amount) FROM sales GROUP BY CUBE(region, product);",
         "SELECT region, product, SUM(amount) FROM sales GROUP BY region, product;",
         ir_features=["select-basic", "group-by", "cube", "aggregate"], diff_clauses=["GROUP BY", "CUBE"], diff_types=["cube_changed"]),
    case("frontier_grouping_sets", "PostgreSQL GROUPING SETS", "GROUPING SETS",
         "SELECT region, product, SUM(amount) FROM sales GROUP BY GROUPING SETS ((region), (product));",
         "SELECT region, product, SUM(amount) FROM sales GROUP BY region, product;",
         ir_features=["select-basic", "group-by", "grouping-sets", "aggregate"], diff_clauses=["GROUP BY", "GROUPING SETS"], diff_types=["grouping_sets_changed"]),
    case("frontier_agg_filter", "PostgreSQL aggregate FILTER", "Aggregate FILTER",
         "SELECT city, COUNT(*) FILTER (WHERE temp_lo < 45) FROM weather GROUP BY city;",
         "SELECT city, COUNT(*) FROM weather GROUP BY city;",
         ir_features=["select-basic", "group-by", "aggregate", "aggregate-filter"], diff_clauses=["AGGREGATE FILTER"], diff_types=["aggregate_filter_changed"]),
    case("frontier_ordered_aggregate", "PostgreSQL ordered aggregate", "Ordered Aggregate",
         "SELECT STRING_AGG(name, ',' ORDER BY name) FROM customer;",
         "SELECT STRING_AGG(name, ',') FROM customer;",
         ir_features=["select-basic", "aggregate", "ordered-aggregate"], diff_clauses=["AGGREGATE"], diff_types=["aggregate_order_changed"]),
    case("frontier_lateral", "PostgreSQL LATERAL", "LATERAL",
         "SELECT c.name, x.total FROM customer c CROSS JOIN LATERAL (SELECT SUM(amount) AS total FROM orders o WHERE o.customer_id = c.id) x;",
         "SELECT c.name FROM customer c;",
         ir_features=["select-basic", "join-inner", "lateral", "subquery-correlated"], diff_clauses=["LATERAL", "JOIN"], diff_types=["lateral_changed"]),
    case("frontier_json_operator", "PostgreSQL JSON operator", "JSON Operator",
         "SELECT data ->> 'name' FROM events;",
         "SELECT data FROM events;",
         ir_features=["select-basic", "json-operator"], diff_clauses=["SELECT", "JSON"], diff_types=["json_operator_changed"]),
    case("frontier_array_any", "PostgreSQL ARRAY ANY", "ARRAY ANY",
         "SELECT name FROM customer WHERE id = ANY(ARRAY[1,2,3]);",
         "SELECT name FROM customer WHERE id IN (1,2,3);",
         ir_features=["select-basic", "where", "array-any"], diff_clauses=["PREDICATE"], diff_types=["array_any_changed"]),
    case("frontier_quantified_all", "PostgreSQL quantified ALL", "Quantified Comparison",
         "SELECT name FROM product WHERE price > ALL (SELECT price FROM product WHERE category_id = 1);",
         "SELECT name FROM product WHERE price > (SELECT MAX(price) FROM product WHERE category_id = 1);",
         ir_features=["select-basic", "where", "subquery-scalar", "quantified-comparison"], diff_clauses=["PREDICATE"], diff_types=["quantifier_changed"]),
    case("frontier_materialized_cte", "PostgreSQL MATERIALIZED CTE", "MATERIALIZED CTE",
         "WITH hot AS MATERIALIZED (SELECT city FROM weather WHERE temp_hi > 80) SELECT city FROM hot;",
         "WITH hot AS NOT MATERIALIZED (SELECT city FROM weather WHERE temp_hi > 80) SELECT city FROM hot;",
         ir_features=["select-basic", "cte", "cte-materialized"], diff_clauses=["CTE MATERIALIZED"], diff_types=["cte_materialization_changed"]),
    case("frontier_search_clause", "PostgreSQL recursive SEARCH", "Recursive SEARCH",
         "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 5) SEARCH DEPTH FIRST BY n SET ordercol SELECT n FROM t;",
         "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 5) SELECT n FROM t;",
         ir_features=["select-basic", "cte-recursive", "recursive-search"], diff_clauses=["CTE_RECURSIVE", "SEARCH"], diff_types=["recursive_search_changed"]),
    case("frontier_intersect_all", "PostgreSQL INTERSECT ALL", "INTERSECT ALL",
         "SELECT name FROM customer INTERSECT ALL SELECT name FROM archived_customer;",
         "SELECT name FROM customer INTERSECT SELECT name FROM archived_customer;",
         ir_features=["select-basic", "intersect", "set-all"], diff_clauses=["INTERSECT"], diff_types=["set_operator_changed"]),
    case("frontier_except_all", "PostgreSQL EXCEPT ALL", "EXCEPT ALL",
         "SELECT name FROM customer EXCEPT ALL SELECT name FROM banned_customer;",
         "SELECT name FROM customer EXCEPT SELECT name FROM banned_customer;",
         ir_features=["select-basic", "except", "set-all"], diff_clauses=["EXCEPT"], diff_types=["set_operator_changed"]),
    case("frontier_natural_join", "SQL NATURAL JOIN", "NATURAL JOIN",
         "SELECT * FROM city NATURAL JOIN country;",
         "SELECT * FROM city JOIN country ON city.country_id = country.id;",
         ir_features=["select-basic", "join-inner", "natural-join"], diff_clauses=["JOIN"], diff_types=["natural_join_changed"]),
    case("frontier_join_using", "SQL JOIN USING", "JOIN USING",
         "SELECT * FROM weather JOIN cities USING (city);",
         "SELECT * FROM weather JOIN cities ON weather.city = cities.name;",
         ir_features=["select-basic", "join-inner", "join-using"], diff_clauses=["JOIN ON"], diff_types=["join_using_changed"]),
    case("frontier_qualify", "QUALIFY", "QUALIFY",
         "SELECT name, ROW_NUMBER() OVER (PARTITION BY dept ORDER BY salary DESC) AS rn FROM employee QUALIFY rn = 1;",
         "SELECT name, ROW_NUMBER() OVER (PARTITION BY dept ORDER BY salary DESC) AS rn FROM employee;",
         ir_features=["select-basic", "window-row-number", "qualify"], diff_clauses=["QUALIFY"], diff_types=["qualify_changed"]),
    case("frontier_pivot", "SQL PIVOT", "PIVOT",
         "SELECT * FROM sales PIVOT (SUM(amount) FOR quarter IN ('Q1', 'Q2')) AS p;",
         "SELECT * FROM sales;",
         ir_features=["select-basic", "pivot"], diff_clauses=["PIVOT"], diff_types=["pivot_changed"]),
    case("frontier_offset_fetch", "SQL OFFSET FETCH", "OFFSET FETCH",
         "SELECT name FROM customer ORDER BY name OFFSET 10 ROWS FETCH NEXT 5 ROWS ONLY;",
         "SELECT name FROM customer ORDER BY name LIMIT 5 OFFSET 10;",
         ir_features=["select-basic", "order-by", "limit"], expect_no_diff=True),
    case("frontier_top_with_ties", "T-SQL TOP WITH TIES", "TOP WITH TIES",
         "SELECT TOP 5 WITH TIES name FROM customer ORDER BY score DESC;",
         "SELECT name FROM customer ORDER BY score DESC LIMIT 5;",
         ir_features=["select-basic", "order-by", "limit", "with-ties"], diff_clauses=["LIMIT"], diff_types=["top_with_ties_changed"]),
    case("frontier_window_exclude", "PostgreSQL window EXCLUDE", "Window EXCLUDE",
         "SELECT SUM(salary) OVER (ORDER BY salary ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW EXCLUDE CURRENT ROW) FROM empsalary;",
         "SELECT SUM(salary) OVER (ORDER BY salary ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM empsalary;",
         ir_features=["select-basic", "window-row-number", "window-exclude", "aggregate"], diff_clauses=["WINDOW"], diff_types=["window_over_changed"]),
    case("frontier_is_distinct_from", "PostgreSQL IS DISTINCT FROM", "IS DISTINCT FROM",
         "SELECT name FROM customer WHERE email IS DISTINCT FROM backup_email;",
         "SELECT name FROM customer WHERE email <> backup_email;",
         ir_features=["select-basic", "where"], predicate_kinds=["comparison"], diff_clauses=["PREDICATE"], diff_types=["comparison_operator_changed"]),
    case("frontier_cycle_clause", "PostgreSQL recursive CYCLE", "Recursive CYCLE",
         "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 5) CYCLE n SET is_cycle USING path SELECT n FROM t;",
         "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 5) SELECT n FROM t;",
         ir_features=["select-basic", "cte-recursive", "recursive-cycle"], diff_clauses=["CTE_RECURSIVE", "CYCLE"], diff_types=["recursive_cycle_changed"]),
    case("frontier_tablesample", "PostgreSQL TABLESAMPLE", "TABLESAMPLE",
         "SELECT name FROM customer TABLESAMPLE BERNOULLI (10);",
         "SELECT name FROM customer;",
         ir_features=["select-basic", "tablesample"], diff_clauses=["TABLESAMPLE"], diff_types=["tablesample_changed"]),
    case("frontier_for_update_skip_locked", "PostgreSQL FOR UPDATE SKIP LOCKED", "Locking Clause",
         "SELECT id FROM jobs WHERE status = 'ready' FOR UPDATE SKIP LOCKED;",
         "SELECT id FROM jobs WHERE status = 'ready';",
         ir_features=["select-basic", "where", "locking-clause"], diff_clauses=["LOCKING"], diff_types=["locking_clause_changed"]),
    case("frontier_with_ordinality", "PostgreSQL WITH ORDINALITY", "WITH ORDINALITY",
         "SELECT * FROM UNNEST(ARRAY['a','b']) WITH ORDINALITY AS t(val, ord);",
         "SELECT * FROM UNNEST(ARRAY['a','b']) AS t(val);",
         ir_features=["select-basic", "unnest", "with-ordinality"], diff_clauses=["WITH ORDINALITY"], diff_types=["with_ordinality_changed"]),
    case("frontier_rows_from", "PostgreSQL ROWS FROM", "ROWS FROM",
         "SELECT * FROM ROWS FROM (json_to_recordset(data), generate_series(1,3)) AS x(a int, b int);",
         "SELECT * FROM generate_series(1,3) AS x(b);",
         ir_features=["select-basic", "rows-from"], diff_clauses=["ROWS FROM"], diff_types=["rows_from_changed"]),
    case("frontier_values_from", "PostgreSQL VALUES in FROM", "VALUES FROM",
         "SELECT * FROM (VALUES (1, 'one'), (2, 'two')) AS v(id, label);",
         "SELECT * FROM (VALUES (1, 'one')) AS v(id, label);",
         ir_features=["select-basic", "values-source"], diff_clauses=["VALUES"], diff_types=["values_source_changed"]),
    case("frontier_within_group", "PostgreSQL ordered-set aggregate", "WITHIN GROUP",
         "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY salary) FROM employees;",
         "SELECT AVG(salary) FROM employees;",
         ir_features=["select-basic", "aggregate", "within-group"], diff_clauses=["AGGREGATE"], diff_types=["within_group_changed"]),
    case("frontier_row_comparison", "PostgreSQL row comparison", "Row Comparison",
         "SELECT name FROM customer WHERE (last_name, first_name) > ('Smith', 'Ann');",
         "SELECT name FROM customer WHERE last_name > 'Smith';",
         ir_features=["select-basic", "where", "row-comparison"], diff_clauses=["PREDICATE"], diff_types=["row_comparison_changed"]),
    case("frontier_tuple_in", "PostgreSQL row constructor IN", "Tuple IN",
         "SELECT name FROM customer WHERE (country, city) IN (('US', 'Boston'), ('FR', 'Paris'));",
         "SELECT name FROM customer WHERE country IN ('US', 'FR');",
         ir_features=["select-basic", "where", "tuple-in"], diff_clauses=["PREDICATE"], diff_types=["tuple_in_changed"]),
    case("frontier_window_groups", "PostgreSQL window GROUPS frame", "Window GROUPS",
         "SELECT SUM(salary) OVER (ORDER BY salary GROUPS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM empsalary;",
         "SELECT SUM(salary) OVER (ORDER BY salary ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM empsalary;",
         ir_features=["select-basic", "window-row-number", "window-groups", "aggregate"], diff_clauses=["WINDOW"], diff_types=["window_over_changed"]),
    case("frontier_range_interval_frame", "PostgreSQL RANGE interval frame", "Window RANGE INTERVAL",
         "SELECT SUM(amount) OVER (ORDER BY order_date RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW) FROM orders;",
         "SELECT SUM(amount) OVER (ORDER BY order_date ROWS BETWEEN 7 PRECEDING AND CURRENT ROW) FROM orders;",
         ir_features=["select-basic", "window-row-number", "window-range-interval", "aggregate"], diff_clauses=["WINDOW"], diff_types=["window_over_changed"]),
    case("frontier_filter_distinct", "PostgreSQL FILTER DISTINCT aggregate", "Aggregate FILTER DISTINCT",
         "SELECT COUNT(DISTINCT user_id) FILTER (WHERE paid) FROM events;",
         "SELECT COUNT(DISTINCT user_id) FROM events;",
         ir_features=["select-basic", "aggregate", "distinct", "aggregate-filter"], diff_clauses=["AGGREGATE FILTER"], diff_types=["aggregate_filter_changed"]),
    case("frontier_json_path", "PostgreSQL JSON path", "JSON Path",
         "SELECT data @@ '$.a[*] > 2' FROM events;",
         "SELECT data -> 'a' FROM events;",
         ir_features=["select-basic", "json-path"], diff_clauses=["JSON"], diff_types=["json_path_changed"]),
    case("frontier_array_overlap", "PostgreSQL array overlap", "Array Overlap",
         "SELECT name FROM product WHERE tags && ARRAY['sale','new'];",
         "SELECT name FROM product WHERE tags @> ARRAY['sale'];",
         ir_features=["select-basic", "where", "array-operator"], diff_clauses=["PREDICATE"], diff_types=["array_operator_changed"]),
    case("frontier_collate", "PostgreSQL COLLATE", "COLLATE",
         "SELECT name FROM customer ORDER BY name COLLATE \"C\";",
         "SELECT name FROM customer ORDER BY name;",
         ir_features=["select-basic", "order-by", "collate"], diff_clauses=["ORDER BY"], diff_types=["collation_changed"]),
    case("frontier_select_into", "PostgreSQL SELECT INTO", "SELECT INTO",
         "SELECT * INTO TEMP recent_orders FROM orders WHERE order_date > DATE '2024-01-01';",
         "SELECT * FROM orders WHERE order_date > DATE '2024-01-01';",
         ir_features=["select-basic", "select-into"], diff_clauses=["SELECT INTO"], diff_types=["select_into_changed"]),
    case("frontier_grouping_function", "PostgreSQL GROUPING function", "GROUPING Function",
         "SELECT region, GROUPING(region), SUM(amount) FROM sales GROUP BY ROLLUP(region);",
         "SELECT region, SUM(amount) FROM sales GROUP BY region;",
         ir_features=["select-basic", "group-by", "rollup", "grouping-function", "aggregate"], diff_clauses=["ROLLUP", "SELECT"], diff_types=["grouping_function_changed"]),
    case("frontier_lateral_function", "PostgreSQL LATERAL function", "LATERAL Function",
         "SELECT e.id, j.value FROM events e CROSS JOIN LATERAL jsonb_array_elements(e.data) AS j(value);",
         "SELECT e.id FROM events e;",
         ir_features=["select-basic", "join-inner", "lateral", "json-function"], diff_clauses=["LATERAL", "JOIN"], diff_types=["lateral_changed"]),
    case("frontier_window_exclude_ties", "PostgreSQL window EXCLUDE TIES", "Window EXCLUDE TIES",
         "SELECT SUM(salary) OVER (ORDER BY salary GROUPS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW EXCLUDE TIES) FROM empsalary;",
         "SELECT SUM(salary) OVER (ORDER BY salary GROUPS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM empsalary;",
         ir_features=["select-basic", "window-row-number", "window-exclude", "aggregate"], diff_clauses=["WINDOW"], diff_types=["window_over_changed"]),
    case("frontier_xmltable", "PostgreSQL XMLTABLE", "XMLTABLE",
         "SELECT x.* FROM XMLTABLE('/rows/row' PASSING doc COLUMNS id int PATH 'id') AS x;",
         "SELECT doc FROM xml_docs;",
         ir_features=["select-basic", "xmltable"], diff_clauses=["XMLTABLE"], diff_types=["xmltable_changed"]),
    case("frontier_merge_like_upsert_cte", "PostgreSQL data-modifying WITH", "Data-modifying WITH",
         "WITH moved AS (DELETE FROM products WHERE discontinued RETURNING *) SELECT * FROM moved;",
         "WITH moved AS (SELECT * FROM products WHERE discontinued) SELECT * FROM moved;",
         ir_features=["select-basic", "cte", "data-modifying-cte"], diff_clauses=["CTE"], diff_types=["data_modifying_cte_changed"]),
    case("frontier_recognize_date_trunc", "PostgreSQL date_trunc grouping", "Function Grouping",
         "SELECT date_trunc('month', order_date), SUM(amount) FROM orders GROUP BY date_trunc('month', order_date);",
         "SELECT order_date, SUM(amount) FROM orders GROUP BY order_date;",
         ir_features=["select-basic", "group-by", "aggregate", "function-grouping"], diff_clauses=["GROUP BY", "SELECT"], diff_types=["function_grouping_changed"]),
    case("frontier_generated_columns_alias", "PostgreSQL alias ordinal order", "Ordinal ORDER",
         "SELECT name, salary * 12 AS annual_salary FROM employees ORDER BY 2 DESC;",
         "SELECT name, salary * 12 AS annual_salary FROM employees ORDER BY annual_salary ASC;",
         ir_features=["select-basic", "order-by"], diff_clauses=["ORDER BY"], diff_types=["order_by_changed"]),
    case("frontier_on_conflict_subquery_style", "PostgreSQL excluded pseudo relation", "Excluded Pseudo Relation",
         "SELECT excluded.id FROM excluded;",
         "SELECT id FROM customer;",
         ir_features=["select-basic", "excluded-pseudo-relation"], diff_clauses=["FROM"], diff_types=["pseudo_relation_changed"]),
]


def summarize(results):
    by_structure = defaultdict(Counter)
    for result in results:
        by_structure[result["structure"]]["total"] += 1
        by_structure[result["structure"]]["hard_pass"] += int(result["hard_pass"])
        by_structure[result["structure"]]["strict_pass"] += int(result["strict_pass"])
    failures = [result for result in results if not result["strict_pass"]]
    return {
        "total": len(results),
        "strict_pass": len(results) - len(failures),
        "strict_fail": len(failures),
        "strict_pass_rate": (len(results) - len(failures)) / len(results) * 100 if results else 0,
        "failure_ids": [result["id"] for result in failures],
        "by_structure": {key: dict(value) for key, value in sorted(by_structure.items())},
    }


def render(results, summary):
    lines = [
        "# Structure Frontier Tests",
        "",
        "This suite is intentionally adversarial. Failures define current structure-layer limits.",
        "",
        f"- Total: `{summary['total']}`",
        f"- Strict pass: `{summary['strict_pass']}` (`{summary['strict_pass_rate']:.2f}%`)",
        f"- Strict fail: `{summary['strict_fail']}`",
        "",
        "## Cannot Pass Samples",
        "",
    ]
    failures = [result for result in results if not result["strict_pass"]]
    if not failures:
        lines.append("无。")
    for result in failures:
        issues = []
        for key, value in result["hard_failures"].items():
            if value:
                issues.append(f"{key}={value}")
        lines.extend([
            f"### `{result['id']}` / {result['structure']}",
            f"- Source: {result['source']}",
            f"- Standard: `{result['standard']}`",
            f"- Student: `{result['student']}`",
            f"- Issues: {'; '.join(issues)}",
            f"- Actual clauses: `{result['diff_clauses']}`",
            f"- Actual diff types: `{result['diff_types']}`",
            "",
        ])
    lines.extend([
        "## All Samples",
        "",
        "| id | structure | strict |",
        "|---|---|---:|",
    ])
    for result in results:
        lines.append(f"| `{result['id']}` | {result['structure']} | {'yes' if result['strict_pass'] else 'NO'} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results = [run_case(item) for item in FRONTIER_CASES]
    summary = summarize(results)
    payload = {"summary": summary, "results": results}
    json_path = OUTPUT_DIR / "structure_frontier_report.json"
    md_path = OUTPUT_DIR / "structure_frontier_report.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render(results, summary), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")


if __name__ == "__main__":
    main()
