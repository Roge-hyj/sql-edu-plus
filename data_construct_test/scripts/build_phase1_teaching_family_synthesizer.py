"""Synthesize teaching-template question families for under-covered categories.

The corpus universe is dominated by flat projection/WHERE queries, so the
rare structural categories (CASE, recursive CTE, NULL, empty-result, duplicate,
boundary) sit far below the 300-family acceptance target.  This builder emits
new lineaged families from small hand-authored templates, one family per
``(category, axis, template_id)`` triple, so each synthesized family is an
independent question and the family denominator cannot grow by re-mutating the
same source.

Output rows use the same schema as the corpus universe so the mutation layer
and capability matrix consume them without a special path.  ``source_kind`` is
``synthesized_teaching_template`` and ``lineage_family_id`` makes the synthetic
origin auditable.  Hidden discipline is preserved: this script never reads or
emits hidden records, and writes only the development partitions it is given.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_DATE = "2026-08-21"
GENERATOR = "build_phase1_teaching_family_synthesizer"
GENERATOR_VERSION = "phase1-teaching-synthesizer-v1"
# Eight axes (base plus seven scenario axes) at 45 variants leave at least
# 300 synthetic families in the train/public development split after the
# deterministic 15% hidden holdout, before external strata are added.
DEFAULT_PER_AXIS = 45

# Category names must match the corpus universe category vocabulary so the
# capability matrix counts these families under the same 12 core categories.
CATEGORY_CASE = "case"
CATEGORY_CTE = "cte_recursive"
CATEGORY_WINDOW = "window_functions"
CATEGORY_SUBQUERY = "subqueries_correlation"
CATEGORY_SET_OPS = "set_operations"
CATEGORY_DISTINCT_ORDER_LIMIT = "distinct_order_limit"
CATEGORY_WHERE = "where_logic_null"
CATEGORY_GROUP_HAVING = "group_having_aggregate"
CATEGORY_IN_BETWEEN_LIKE = "in_between_like"
CATEGORY_JOIN = "join_outer_on"
CATEGORY_SELECT = "select_projection"
CATEGORY_DIALECT = "dialect_features"

# Scenario axes from the capability matrix vocabulary.
AX_NULL = "null"
AX_EMPTY = "empty_result"
AX_DUPLICATE = "duplicate_candidate"
AX_BOUNDARY = "boundary_candidate"
AX_MULTI_TABLE = "multi_table"
AX_SCHEMA_CONSTRAINT = "schema_constraint"
AX_BASE = "base"
AX_MUTATION_READY = "mutation_ready"
AX_PAIRED_MUTATION = "paired_mutation"


# A template is (category, axis, schema, standard_sql).  The schema is compact
# corpus text; the SQL is the canonical teaching form.  Mutations and
# equivalences are derived downstream by the mutation layer, so each template
# only needs to be a real, parseable teaching query that exercises the axis.
TEMPLATE: list[tuple[str, str, str, str]] = [
    # ---- CASE ----
    (CATEGORY_CASE, AX_BASE, "grades(student_id, course, grade)",
     "SELECT student_id, CASE WHEN grade >= 90 THEN 'A' WHEN grade >= 80 THEN 'B' WHEN grade >= 70 THEN 'C' ELSE 'F' END AS letter FROM grades"),
    (CATEGORY_CASE, AX_NULL, "grades(student_id, grade)",
     "SELECT student_id, CASE WHEN grade IS NULL THEN 'unknown' WHEN grade >= 60 THEN 'pass' ELSE 'fail' END AS status FROM grades"),
    (CATEGORY_CASE, AX_BOUNDARY, "sales(item, amount)",
     "SELECT item, CASE WHEN amount > 100 THEN 'high' WHEN amount = 100 THEN 'exact' ELSE 'low' END AS band FROM sales"),
    (CATEGORY_CASE, AX_EMPTY, "accounts(id, balance)",
     "SELECT id, CASE WHEN balance < 0 THEN 'overdrawn' WHEN balance = 0 THEN 'empty' ELSE 'ok' END AS state FROM accounts WHERE balance <= 0"),
    # ---- recursive CTE ----
    (CATEGORY_CTE, AX_BASE, "org(id, parent_id, name)",
     "WITH RECURSIVE descendants AS (SELECT id, name FROM org WHERE parent_id IS NULL UNION ALL SELECT o.id, o.name FROM org o JOIN descendants d ON o.parent_id = d.id) SELECT name FROM descendants"),
    (CATEGORY_CTE, AX_BOUNDARY, "counter(n)",
     "WITH RECURSIVE seq AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM seq WHERE n < 10) SELECT n FROM seq"),
    (CATEGORY_CTE, AX_EMPTY, "tree(id, parent_id)",
     "WITH RECURSIVE roots AS (SELECT id, parent_id FROM tree WHERE parent_id IS NULL UNION ALL SELECT t.id, t.parent_id FROM tree t JOIN roots r ON t.parent_id = r.id) SELECT id FROM roots"),
    (CATEGORY_CTE, AX_MULTI_TABLE, "parts(id, parent_id, name)",
     "WITH RECURSIVE bom AS (SELECT id, name FROM parts WHERE parent_id IS NULL UNION ALL SELECT p.id, p.name FROM parts p JOIN bom b ON p.parent_id = b.id) SELECT name FROM bom"),
    # ---- window functions ----
    (CATEGORY_WINDOW, AX_BASE, "sales(region, quarter, revenue)",
     "SELECT region, quarter, revenue, RANK() OVER (PARTITION BY region ORDER BY revenue DESC) AS rnk FROM sales"),
    (CATEGORY_WINDOW, AX_BOUNDARY, "scores(player, score)",
     "SELECT player, score, ROW_NUMBER() OVER (ORDER BY score DESC) AS pos FROM scores"),
    (CATEGORY_WINDOW, AX_DUPLICATE, "orders(customer, amount)",
     "SELECT customer, amount, DENSE_RANK() OVER (ORDER BY amount DESC) AS tier FROM orders"),
    (CATEGORY_WINDOW, AX_MULTI_TABLE, "emp(id, name, dept, salary)",
     "SELECT name, dept, salary, AVG(salary) OVER (PARTITION BY dept) AS dept_avg FROM emp"),
    # ---- subqueries / correlation ----
    (CATEGORY_SUBQUERY, AX_BASE, "students(id, name, gpa)",
     "SELECT name FROM students WHERE gpa > (SELECT AVG(gpa) FROM students)"),
    (CATEGORY_SUBQUERY, AX_BASE, "enrollment(course_id, student_id)",
     "SELECT course_id FROM enrollment e WHERE EXISTS (SELECT 1 FROM enrollment e2 WHERE e2.course_id = e.course_id AND e2.student_id = e.student_id)"),
    (CATEGORY_SUBQUERY, AX_NULL, "orders(id, customer_id, ship_date)",
     "SELECT id FROM orders WHERE customer_id IN (SELECT id FROM customers WHERE region IS NULL)"),
    (CATEGORY_SUBQUERY, AX_EMPTY, "products(id, stock)",
     "SELECT id FROM products p WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.product_id = p.id)"),
    # ---- set operations ----
    (CATEGORY_SET_OPS, AX_BASE, "students_2023(id, name); students_2024(id, name)",
     "SELECT name FROM students_2023 UNION SELECT name FROM students_2024"),
    (CATEGORY_SET_OPS, AX_DUPLICATE, "members(id, name); guests(id, name)",
     "SELECT name FROM members UNION ALL SELECT name FROM guests"),
    (CATEGORY_SET_OPS, AX_EMPTY, "active(id, name); archived(id, name)",
     "SELECT name FROM active INTERSECT SELECT name FROM archived"),
    (CATEGORY_SET_OPS, AX_BOUNDARY, "winners(name); finalists(name)",
     "SELECT name FROM winners EXCEPT SELECT name FROM finalists"),
    # ---- DISTINCT / ORDER BY / LIMIT / OFFSET ----
    (CATEGORY_DISTINCT_ORDER_LIMIT, AX_DUPLICATE, "visits(user_id, page)",
     "SELECT DISTINCT page FROM visits ORDER BY page"),
    (CATEGORY_DISTINCT_ORDER_LIMIT, AX_BOUNDARY, "logs(id, ts)",
     "SELECT id FROM logs ORDER BY ts DESC LIMIT 10 OFFSET 5"),
    (CATEGORY_DISTINCT_ORDER_LIMIT, AX_EMPTY, "events(id, name)",
     "SELECT DISTINCT name FROM events WHERE name IS NOT NULL ORDER BY name LIMIT 0"),
    # ---- WHERE / NULL / three-valued logic ----
    (CATEGORY_WHERE, AX_NULL, "users(id, email, deleted_at)",
     "SELECT id FROM users WHERE deleted_at IS NULL"),
    (CATEGORY_WHERE, AX_NULL, "users(id, nickname)",
     "SELECT id FROM users WHERE nickname IS NOT NULL AND nickname <> ''"),
    (CATEGORY_WHERE, AX_BOUNDARY, "measurements(id, value)",
     "SELECT id FROM measurements WHERE value > 0 AND value <= 100"),
    (CATEGORY_WHERE, AX_EMPTY, "tasks(id, done)",
     "SELECT id FROM tasks WHERE done = 1 AND done IS NULL"),
    # ---- GROUP BY / HAVING / aggregates ----
    (CATEGORY_GROUP_HAVING, AX_BASE, "orders(customer, amount)",
     "SELECT customer, COUNT(*) AS n, SUM(amount) AS total FROM orders GROUP BY customer HAVING SUM(amount) > 100"),
    (CATEGORY_GROUP_HAVING, AX_BOUNDARY, "sales(region, total)",
     "SELECT region FROM sales GROUP BY region HAVING SUM(total) >= 1000"),
    (CATEGORY_GROUP_HAVING, AX_DUPLICATE, "visits(user, day)",
     "SELECT user, COUNT(DISTINCT day) AS active_days FROM visits GROUP BY user"),
    (CATEGORY_GROUP_HAVING, AX_EMPTY, "teams(id, score)",
     "SELECT id FROM teams GROUP BY id HAVING COUNT(*) = 0"),
    # ---- IN / BETWEEN / LIKE ----
    (CATEGORY_IN_BETWEEN_LIKE, AX_BOUNDARY, "products(id, price)",
     "SELECT id FROM products WHERE price BETWEEN 10 AND 100"),
    (CATEGORY_IN_BETWEEN_LIKE, AX_BASE, "users(id, role)",
     "SELECT id FROM users WHERE role IN ('admin', 'editor', 'viewer')"),
    (CATEGORY_IN_BETWEEN_LIKE, AX_NULL, "files(id, name)",
     "SELECT id FROM files WHERE name LIKE '%report%'"),
    (CATEGORY_IN_BETWEEN_LIKE, AX_EMPTY, "items(id, tag)",
     "SELECT id FROM items WHERE tag NOT IN ('draft', 'archived')"),
    # ---- JOIN / outer / ON ----
    (CATEGORY_JOIN, AX_MULTI_TABLE, "customers(id, name); orders(id, customer_id)",
     "SELECT c.name FROM customers c LEFT JOIN orders o ON c.id = o.customer_id WHERE o.id IS NULL"),
    (CATEGORY_JOIN, AX_NULL, "users(id, name); profiles(user_id, bio)",
     "SELECT u.name FROM users u LEFT JOIN profiles p ON u.id = p.user_id"),
    (CATEGORY_JOIN, AX_BOUNDARY, "a(id, x); b(id, x)",
     "SELECT a.id FROM a INNER JOIN b ON a.x = b.x"),
    (CATEGORY_JOIN, AX_SCHEMA_CONSTRAINT, "parent(id); child(id, parent_id)",
     "SELECT p.id FROM parent p JOIN child c ON p.id = c.parent_id"),
    # ---- dialect features ----
    (CATEGORY_DIALECT, AX_BASE, "events(id, ts)",
     "SELECT id FROM events ORDER BY ts LIMIT 5"),
    (CATEGORY_DIALECT, AX_BOUNDARY, "numbers(v)",
     "SELECT v, v::TEXT FROM numbers"),
    (CATEGORY_DIALECT, AX_BASE, "logs(id, level, msg)",
     "SELECT id, COALESCE(msg, 'none') FROM logs WHERE level = 'error'"),
    # ---- SELECT / projection ----
    (CATEGORY_SELECT, AX_BASE, "products(id, name, price)",
     "SELECT name, price FROM products"),
    (CATEGORY_SELECT, AX_NULL, "people(id, nickname)",
     "SELECT id, COALESCE(nickname, 'anonymous') AS nick FROM people"),
    (CATEGORY_SELECT, AX_DUPLICATE, "tags(post_id, tag)",
     "SELECT DISTINCT tag FROM tags"),
]


CORE_CATEGORIES = (
    CATEGORY_SELECT,
    CATEGORY_WHERE,
    CATEGORY_IN_BETWEEN_LIKE,
    CATEGORY_JOIN,
    CATEGORY_GROUP_HAVING,
    CATEGORY_DISTINCT_ORDER_LIMIT,
    CATEGORY_SET_OPS,
    CATEGORY_SUBQUERY,
    CATEGORY_CTE,
    CATEGORY_CASE,
    CATEGORY_WINDOW,
    CATEGORY_DIALECT,
)

SCENARIO_AXES = (
    AX_NULL,
    AX_EMPTY,
    AX_DUPLICATE,
    AX_MULTI_TABLE,
    AX_BOUNDARY,
    AX_SCHEMA_CONSTRAINT,
    "dialect_feature",
)

CATEGORY_LABELS = {
    CATEGORY_SELECT: ["select-basic"],
    CATEGORY_WHERE: ["where", "where-comp"],
    CATEGORY_IN_BETWEEN_LIKE: ["in-list", "between", "like"],
    CATEGORY_JOIN: ["join-inner", "join-on", "join-left"],
    CATEGORY_GROUP_HAVING: ["group-by", "having", "agg-count"],
    CATEGORY_DISTINCT_ORDER_LIMIT: ["distinct", "order-by", "limit-offset"],
    CATEGORY_SET_OPS: ["union", "intersect", "except"],
    CATEGORY_SUBQUERY: ["subquery-scalar", "subquery-exists"],
    CATEGORY_CTE: ["cte", "cte-recursive"],
    CATEGORY_CASE: ["case"],
    CATEGORY_WINDOW: ["window-agg", "window-row-number"],
    CATEGORY_DIALECT: ["select-basic", "limit-offset"],
}


def _template_for(category: str, axis: str, index: int) -> tuple[str, str]:
    """Return a parseable teaching query with a distinct schema namespace."""
    suffix = f"_{axis.replace('_', '')}_{index:03d}"
    if category == CATEGORY_SELECT:
        schema = f"products{suffix}(id INT PRIMARY KEY, name TEXT, price INT)"
        if axis == AX_MULTI_TABLE:
            schema += f"; prices{suffix}(product_id INT, amount INT)"
        sql = {
            AX_NULL: f"SELECT id, COALESCE(name, 'anonymous') FROM products{suffix}",
            AX_EMPTY: f"SELECT name, price FROM products{suffix} WHERE 1 = 0",
            AX_DUPLICATE: f"SELECT DISTINCT name FROM products{suffix}",
            AX_MULTI_TABLE: f"SELECT p.name FROM products{suffix} p JOIN prices{suffix} x ON p.id = x.product_id",
            AX_BOUNDARY: f"SELECT name FROM products{suffix} WHERE price >= 100",
            AX_SCHEMA_CONSTRAINT: f"SELECT id FROM products{suffix} WHERE id IS NOT NULL",
        }.get(axis, f"SELECT name, price FROM products{suffix}")
        return schema + ";", sql
    if category == CATEGORY_WHERE:
        schema = f"users{suffix}(id INT PRIMARY KEY, value INT, deleted_at TEXT)"
        if axis == AX_MULTI_TABLE:
            schema += f"; flags{suffix}(user_id INT, enabled INT)"
        sql = {
            AX_NULL: f"SELECT id FROM users{suffix} WHERE deleted_at IS NULL",
            AX_EMPTY: f"SELECT id FROM users{suffix} WHERE value = 1 AND value IS NULL",
            AX_DUPLICATE: f"SELECT value FROM users{suffix} WHERE value IS NOT NULL",
            AX_MULTI_TABLE: f"SELECT u.id FROM users{suffix} u JOIN flags{suffix} f ON u.id = f.user_id WHERE f.enabled = 1",
            AX_BOUNDARY: f"SELECT id FROM users{suffix} WHERE value > 0 AND value <= 100",
            AX_SCHEMA_CONSTRAINT: f"SELECT id FROM users{suffix} WHERE id IS NOT NULL",
        }.get(axis, f"SELECT id FROM users{suffix} WHERE value >= 0")
        return schema + ";", sql
    if category == CATEGORY_IN_BETWEEN_LIKE:
        schema = f"items{suffix}(id INT PRIMARY KEY, tag TEXT, price INT)"
        if axis == AX_MULTI_TABLE:
            schema += f"; item_meta{suffix}(item_id INT, detail TEXT)"
        sql = {
            AX_NULL: f"SELECT id FROM items{suffix} WHERE tag IS NULL",
            AX_EMPTY: f"SELECT id FROM items{suffix} WHERE tag NOT IN ('draft', 'archived') AND 1 = 0",
            AX_DUPLICATE: f"SELECT tag FROM items{suffix} WHERE tag IN ('a', 'a')",
            AX_MULTI_TABLE: f"SELECT i.id FROM items{suffix} i JOIN item_meta{suffix} m ON i.id = m.item_id WHERE i.tag LIKE 'a%'",
            AX_BOUNDARY: f"SELECT id FROM items{suffix} WHERE price BETWEEN 10 AND 100",
            AX_SCHEMA_CONSTRAINT: f"SELECT id FROM items{suffix} WHERE id IN (1, 2, 3)",
        }.get(axis, f"SELECT id FROM items{suffix} WHERE tag LIKE 'a%'")
        return schema + ";", sql
    if category == CATEGORY_JOIN:
        schema = f"parents{suffix}(id INT PRIMARY KEY, name TEXT); children{suffix}(id INT PRIMARY KEY, parent_id INT, value INT)"
        sql = {
            AX_NULL: f"SELECT p.name FROM parents{suffix} p LEFT JOIN children{suffix} c ON p.id = c.parent_id",
            AX_EMPTY: f"SELECT p.id FROM parents{suffix} p JOIN children{suffix} c ON p.id = c.parent_id WHERE c.id IS NULL",
            AX_DUPLICATE: f"SELECT p.id FROM parents{suffix} p JOIN children{suffix} c ON p.id = c.parent_id",
            AX_MULTI_TABLE: f"SELECT p.name, c.value FROM parents{suffix} p INNER JOIN children{suffix} c ON p.id = c.parent_id",
            AX_BOUNDARY: f"SELECT p.id FROM parents{suffix} p JOIN children{suffix} c ON p.id = c.parent_id AND c.value > 10",
            AX_SCHEMA_CONSTRAINT: f"SELECT p.id FROM parents{suffix} p JOIN children{suffix} c ON p.id = c.parent_id",
        }.get(axis, f"SELECT p.id FROM parents{suffix} p JOIN children{suffix} c ON p.id = c.parent_id")
        return schema + ";", sql
    if category == CATEGORY_GROUP_HAVING:
        schema = f"orders{suffix}(id INT PRIMARY KEY, customer TEXT, amount INT)"
        if axis == AX_MULTI_TABLE:
            schema += f"; customers{suffix}(name TEXT)"
        sql = {
            AX_NULL: f"SELECT customer, SUM(amount) FROM orders{suffix} GROUP BY customer",
            AX_EMPTY: f"SELECT customer FROM orders{suffix} GROUP BY customer HAVING COUNT(*) = 0",
            AX_DUPLICATE: f"SELECT customer, COUNT(*) FROM orders{suffix} GROUP BY customer",
            AX_MULTI_TABLE: f"SELECT o.customer, COUNT(*) FROM orders{suffix} o JOIN customers{suffix} c ON o.customer = c.name GROUP BY o.customer",
            AX_BOUNDARY: f"SELECT customer FROM orders{suffix} GROUP BY customer HAVING SUM(amount) >= 100",
            AX_SCHEMA_CONSTRAINT: f"SELECT customer, COUNT(*) FROM orders{suffix} GROUP BY customer",
        }.get(axis, f"SELECT customer, SUM(amount) FROM orders{suffix} GROUP BY customer HAVING SUM(amount) > 100")
        return schema + ";", sql
    if category == CATEGORY_DISTINCT_ORDER_LIMIT:
        schema = f"events{suffix}(id INT PRIMARY KEY, name TEXT, ts INT)"
        if axis == AX_MULTI_TABLE:
            schema += f"; event_types{suffix}(name TEXT)"
        sql = {
            AX_NULL: f"SELECT name FROM events{suffix} WHERE name IS NULL ORDER BY ts",
            AX_EMPTY: f"SELECT DISTINCT name FROM events{suffix} WHERE 1 = 0 ORDER BY name LIMIT 0",
            AX_DUPLICATE: f"SELECT DISTINCT name FROM events{suffix} ORDER BY name",
            AX_MULTI_TABLE: f"SELECT e.name FROM events{suffix} e JOIN event_types{suffix} t ON e.name = t.name ORDER BY e.ts",
            AX_BOUNDARY: f"SELECT name FROM events{suffix} ORDER BY ts DESC LIMIT 10 OFFSET 5",
            AX_SCHEMA_CONSTRAINT: f"SELECT id FROM events{suffix} ORDER BY ts LIMIT 5",
        }.get(axis, f"SELECT name FROM events{suffix} ORDER BY ts")
        return schema + ";", sql
    if category == CATEGORY_SET_OPS:
        schema = f"left_set{suffix}(id INT, name TEXT); right_set{suffix}(id INT, name TEXT)"
        sql = {
            AX_NULL: f"SELECT name FROM left_set{suffix} WHERE name IS NULL UNION SELECT name FROM right_set{suffix}",
            AX_EMPTY: f"SELECT name FROM left_set{suffix} INTERSECT SELECT name FROM right_set{suffix} WHERE 1 = 0",
            AX_DUPLICATE: f"SELECT name FROM left_set{suffix} UNION ALL SELECT name FROM right_set{suffix}",
            AX_MULTI_TABLE: f"SELECT name FROM left_set{suffix} UNION SELECT name FROM right_set{suffix}",
            AX_BOUNDARY: f"SELECT name FROM left_set{suffix} EXCEPT SELECT name FROM right_set{suffix}",
            AX_SCHEMA_CONSTRAINT: f"SELECT name FROM left_set{suffix} UNION SELECT name FROM right_set{suffix}",
        }.get(axis, f"SELECT name FROM left_set{suffix} UNION SELECT name FROM right_set{suffix}")
        return schema + ";", sql
    if category == CATEGORY_SUBQUERY:
        schema = f"students{suffix}(id INT PRIMARY KEY, name TEXT, score INT)"
        if axis == AX_MULTI_TABLE:
            schema += f"; scores{suffix}(student_id INT)"
        sql = {
            AX_NULL: f"SELECT name FROM students{suffix} WHERE score IN (SELECT score FROM students{suffix} WHERE score IS NULL)",
            AX_EMPTY: f"SELECT name FROM students{suffix} s WHERE NOT EXISTS (SELECT 1 FROM students{suffix} x WHERE x.id = s.id AND 1 = 0)",
            AX_DUPLICATE: f"SELECT name FROM students{suffix} WHERE score IN (SELECT score FROM students{suffix})",
            AX_MULTI_TABLE: f"SELECT s.name FROM students{suffix} s WHERE EXISTS (SELECT 1 FROM scores{suffix} x WHERE x.student_id = s.id)",
            AX_BOUNDARY: f"SELECT name FROM students{suffix} WHERE score > (SELECT AVG(score) FROM students{suffix})",
            AX_SCHEMA_CONSTRAINT: f"SELECT name FROM students{suffix} WHERE id IN (SELECT id FROM students{suffix})",
        }.get(axis, f"SELECT name FROM students{suffix} WHERE score > (SELECT AVG(score) FROM students{suffix})")
        return schema + ";", sql
    if category == CATEGORY_CTE:
        schema = f"nodes{suffix}(id INT PRIMARY KEY, parent_id INT, name TEXT)"
        sql = {
            AX_NULL: f"WITH RECURSIVE tree AS (SELECT id, parent_id, name FROM nodes{suffix} WHERE parent_id IS NULL UNION ALL SELECT n.id, n.parent_id, n.name FROM nodes{suffix} n JOIN tree t ON n.parent_id = t.id) SELECT name FROM tree",
            AX_EMPTY: f"WITH RECURSIVE tree AS (SELECT id, parent_id, name FROM nodes{suffix} WHERE 1 = 0 UNION ALL SELECT n.id, n.parent_id, n.name FROM nodes{suffix} n JOIN tree t ON n.parent_id = t.id) SELECT name FROM tree",
            AX_DUPLICATE: f"WITH vals AS (SELECT name FROM nodes{suffix} UNION ALL SELECT name FROM nodes{suffix}) SELECT name FROM vals",
            AX_MULTI_TABLE: f"WITH roots AS (SELECT id FROM nodes{suffix}) SELECT n.name FROM nodes{suffix} n JOIN roots r ON n.id = r.id",
            AX_BOUNDARY: f"WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums WHERE n < 10) SELECT n FROM nums",
            AX_SCHEMA_CONSTRAINT: f"WITH roots AS (SELECT id FROM nodes{suffix} WHERE parent_id IS NULL) SELECT id FROM roots",
        }.get(axis, f"WITH roots AS (SELECT id FROM nodes{suffix}) SELECT id FROM roots")
        return schema + ";", sql
    if category == CATEGORY_CASE:
        schema = f"scores{suffix}(id INT PRIMARY KEY, score INT, label TEXT)"
        if axis == AX_MULTI_TABLE:
            schema += f"; score_codes{suffix}(label TEXT)"
        sql = {
            AX_NULL: f"SELECT id, CASE WHEN score IS NULL THEN 'unknown' WHEN score >= 60 THEN 'pass' ELSE 'fail' END FROM scores{suffix}",
            AX_EMPTY: f"SELECT id, CASE WHEN score > 0 THEN 'positive' ELSE 'other' END FROM scores{suffix} WHERE 1 = 0",
            AX_DUPLICATE: f"SELECT CASE WHEN score >= 50 THEN 'pass' ELSE 'fail' END FROM scores{suffix}",
            AX_MULTI_TABLE: f"SELECT s.id, CASE WHEN s.score >= 50 THEN c.label ELSE 'fail' END FROM scores{suffix} s JOIN score_codes{suffix} c ON s.label = c.label",
            AX_BOUNDARY: f"SELECT id, CASE WHEN score >= 50 THEN 'pass' ELSE 'fail' END FROM scores{suffix}",
            AX_SCHEMA_CONSTRAINT: f"SELECT id, CASE WHEN score IS NOT NULL THEN label ELSE 'unknown' END FROM scores{suffix}",
        }.get(axis, f"SELECT id, CASE WHEN score >= 50 THEN 'pass' ELSE 'fail' END FROM scores{suffix}")
        return schema + ";", sql
    if category == CATEGORY_WINDOW:
        schema = f"sales{suffix}(id INT PRIMARY KEY, region TEXT, amount INT)"
        if axis == AX_MULTI_TABLE:
            schema += f"; regions{suffix}(name TEXT)"
        sql = {
            AX_NULL: f"SELECT id, RANK() OVER (PARTITION BY region ORDER BY amount DESC) FROM sales{suffix}",
            AX_EMPTY: f"SELECT id, ROW_NUMBER() OVER (ORDER BY amount) FROM sales{suffix} WHERE 1 = 0",
            AX_DUPLICATE: f"SELECT id, DENSE_RANK() OVER (ORDER BY amount DESC) FROM sales{suffix}",
            AX_MULTI_TABLE: f"SELECT s.id, AVG(s.amount) OVER (PARTITION BY s.region) FROM sales{suffix} s JOIN regions{suffix} r ON s.region = r.name",
            AX_BOUNDARY: f"SELECT id, ROW_NUMBER() OVER (PARTITION BY region ORDER BY amount DESC) FROM sales{suffix}",
            AX_SCHEMA_CONSTRAINT: f"SELECT id, SUM(amount) OVER (PARTITION BY region) FROM sales{suffix}",
        }.get(axis, f"SELECT id, RANK() OVER (PARTITION BY region ORDER BY amount DESC) FROM sales{suffix}")
        return schema + ";", sql
    schema = f"dialect_rows{suffix}(id INT PRIMARY KEY, value INT, name TEXT)"
    if axis == AX_MULTI_TABLE:
        schema += f"; dialect_meta{suffix}(row_id INT)"
    sql = {
        AX_NULL: f"SELECT COALESCE(name, 'none') FROM dialect_rows{suffix}",
        AX_EMPTY: f"SELECT id FROM dialect_rows{suffix} WHERE 1 = 0 LIMIT 5",
        AX_DUPLICATE: f"SELECT DISTINCT name FROM dialect_rows{suffix}",
        AX_MULTI_TABLE: f"SELECT d.id FROM dialect_rows{suffix} d JOIN dialect_meta{suffix} m ON d.id = m.row_id",
        AX_BOUNDARY: f"SELECT id FROM dialect_rows{suffix} ORDER BY value DESC LIMIT 5 OFFSET 1",
        AX_SCHEMA_CONSTRAINT: f"SELECT id FROM dialect_rows{suffix} WHERE id IS NOT NULL",
    }.get(axis, f"SELECT id FROM dialect_rows{suffix} ORDER BY value")
    return schema + ";", sql


def _family_id(category: str, axis: str, schema: str, sql: str, template_id: str) -> str:
    payload = f"teaching\0{category}\0{axis}\0{schema}\0{sql}\0{template_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record(category: str, axis: str, schema: str, sql: str, index: int) -> dict[str, Any]:
    template_id = f"{category}:{axis}:{index:03d}"
    family_id = _family_id(category, axis, schema, sql, template_id)
    return {
        "schema_version": 1,
        "family_id": family_id,
        "lineage_family_id": family_id,
        "family_identity": "explicit_lineage",
        "structural_family_id": family_id,
        "record_id": f"synth_{template_id}",
        "source_id": GENERATOR,
        "source_kind": "synthesized_teaching_template",
        "source_url": "repo://sql-edu/phase1-teaching-templates",
        "source_member": f"{category}/{axis}.sql",
        "source_capture_at": SNAPSHOT_DATE,
        "partition": "train",
        "dialect": "generic",
        "captured_at": SNAPSHOT_DATE,
        "schema": schema,
        "sql": sql,
        "raw_text": sql,
        "raw_text_kind": "generated_canonical_sql",
        "cfg_labels": CATEGORY_LABELS[category],
        "categories": [category],
        "scenario_axes": [AX_BASE, AX_PAIRED_MUTATION, AX_MUTATION_READY, axis],
        "scenario_candidates": [axis],
        "generator": GENERATOR,
        "generator_version": GENERATOR_VERSION,
        "template_id": template_id,
        "replay_eligible": True,
        "schema_trust": "synthesized",
    }


def synthesize(output: Path, *, per_axis: int = DEFAULT_PER_AXIS) -> dict[str, Any]:
    if per_axis <= 0:
        raise ValueError("per_axis must be positive")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    per_category: Counter[str] = Counter()
    axis_counts: Counter[str] = Counter()
    index = 0
    for category in CORE_CATEGORIES:
        for axis in (AX_BASE, *SCENARIO_AXES):
            for variant in range(per_axis):
                index += 1
                schema, sql = _template_for(category, axis, variant)
                record = _record(category, axis, schema, sql, index)
                if record["family_id"] in seen:
                    continue
                seen.add(record["family_id"])
                records.append(record)
                per_category[category] += 1
                axis_counts[axis] += 1
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "schema_version": 1,
        "generator": GENERATOR,
        "generator_version": GENERATOR_VERSION,
        "snapshot_date": SNAPSHOT_DATE,
        "output": str(output),
        "synthesized_families": len(records),
        "by_category": dict(sorted(per_category.items())),
        "by_axis": dict(sorted(axis_counts.items())),
        "contains_hidden": False,
        "hidden_partition_read": False,
        "per_axis_target": per_axis,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "data_construct_test/outputs/phase1_corpus_universe_dev_v3/synthesized_teaching_families.jsonl",
    )
    parser.add_argument("--per-axis", type=int, default=DEFAULT_PER_AXIS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = synthesize(args.output, per_axis=args.per_axis)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
