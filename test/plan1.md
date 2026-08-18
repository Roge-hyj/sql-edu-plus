# Plan 1: Phase 1 SQL Structure Capability Evaluation

## Goal

Evaluate Phase 1 structure capability before testing data generation or mutation.

This plan measures whether the current system can recognize SQL structures inside the supported SQL fragment. It does not claim formal SQL semantic equivalence.

## Scope

The first evaluation target is:

- `SQLStructureIR.from_ast()`: can the system identify structures contained in a single SQL query?

Companion benchmarks cover the rest of the implemented Phase 1 flow:

- `extract_ast_diffs()`: can the system identify structural differences between standard SQL and student SQL?
- data generation: can the system synthesize counterexample databases?
- mutation tests: can the system isolate faulty clauses?
- attribution: can the system map combined evidence to the faulty knowledge point?

## Stage 1: IR Structure Recognition

For each SQL case, parse the SQL, build `SQLStructureIR`, and check whether expected structures are present.

Pass criteria:

- SQL parses as one query.
- IR build succeeds.
- Expected IR fields or predicate evidence are present.

Important distinction:

- Structures are captured by typed IR fields, including `projection`, `distinct`, `predicate_ir`, `logic_trees`, `joins`, `group_by`, `having_predicates`, `order_by`, `limit_offset`, `subqueries`, `ctes`, `set_operations`, `case_branches`, and `window_functions`.
- Predicate and logic SQL text remains available for backward compatibility, but comparison operators, `IN`, `BETWEEN`, `LIKE`, NULL predicates, and boolean trees are evaluated through typed nodes.
- Backend executability is a separate dimension. A structure can be fully typed and AST-diff supported while remaining an explicit SQLite execution boundary.

## CFG Productions To Cover

| Category | Structures to Test |
| --- | --- |
| SELECT | projection columns, expressions, `*`, aliases, computed columns |
| DISTINCT | top-level `DISTINCT`, `DISTINCT ON`, aggregate-level `COUNT(DISTINCT col)` |
| WHERE | basic predicates, compound predicates |
| Comparison | `=`, `<>`, `<`, `<=`, `>`, `>=`, `!=` |
| NULL | `IS NULL`, `IS NOT NULL`, `= NULL` |
| IN/BETWEEN/LIKE | `IN (...)`, `BETWEEN`, `LIKE`, `NOT LIKE` |
| Logic | `AND`, `OR`, `NOT`, parenthesized precedence |
| JOIN | INNER, LEFT, RIGHT, FULL, CROSS, self join, `LATERAL` source |
| JOIN ON | single condition, multiple conditions, non-equi condition |
| GROUP BY | single column, multiple columns, expression grouping, `GROUPING SETS`, `ROLLUP`, `CUBE` |
| HAVING | aggregate predicate, HAVING without GROUP BY |
| Aggregate | `COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, `COUNT DISTINCT`, aggregate `FILTER` |
| ORDER BY | ASC/DESC, multiple keys, expression, alias, ordinal |
| LIMIT/OFFSET | `LIMIT`, `OFFSET`, MySQL `LIMIT offset,count`, T-SQL `TOP` |
| Subquery | `IN (SELECT)`, scalar subquery, `EXISTS` |
| Correlated Subquery | outer-column reference |
| CTE | normal CTE, multiple CTEs, dependent CTEs |
| Recursive CTE | `WITH RECURSIVE`, `SEARCH`, `CYCLE` |
| Set Operation | `UNION`, `UNION ALL`, `INTERSECT`, `INTERSECT ALL`, `EXCEPT`, `EXCEPT ALL` |
| CASE | simple CASE, searched CASE |
| Window | `ROW_NUMBER`, `RANK`, `DENSE_RANK`, `SUM OVER`, frame, `QUALIFY` |
| SQLite Execution Boundary | `GROUPING SETS`, `LATERAL`, `ROLLUP`, `CUBE`, `INTERSECT ALL`, `EXCEPT ALL`; all remain typed structure cases |

## Metrics

- `parse_success_rate`
- `ir_build_success_rate`
- `case_support_rate`
- `category_support_rate`
- `first_class_ir_support_rate`
- `textual_predicate_support_rate`
- `execution_boundary_count` (tracked independently from structure support)
- `unexpected_failure_count`

## Result Buckets

- `first_class`: expected structures are captured by dedicated typed IR fields.
- `weak_textual`: structure is detected only inside predicate/projection SQL text, not as typed IR.
- `known_boundary`: legacy bucket for an untyped boundary case; the current benchmark has none.
- `unexpected_failure`: expected supported structure was not parsed or not captured.

`execution_boundary` is separate case metadata for a construct that is typed
and AST-diff supported but cannot be faithfully executed by the current SQLite
counterexample backend.

## Output Artifacts

The structure capability scripts generate:

- `test/phase1_structure/outputs/phase1_ir_structure_capability.json/.md`
- `test/phase1_structure/outputs/phase1_ast_diff_capability.json/.md`
- `test/phase1_structure/outputs/phase1_ast_diff_from_ir_capability.json/.md`

These artifacts should record both passed and failed examples so current capability can be quantified.

## Current Typed IR Implementation Status

The current typed layer in `SQLStructureIR` includes:

- `predicate_ir`: comparison, NULL, IN, BETWEEN, LIKE, and logical predicate nodes.
- `logic_trees`: nested boolean predicate trees by context.
- `aggregate_functions`: function name, arguments, DISTINCT flag, and context.
- `expression_ir`: typed summaries for projection, GROUP BY, and ORDER BY expressions.
- `set_operation_details`: set operator, ALL/DISTINCT flag, and branch SQL.
- `window_function_details`: window function, partition keys, order keys, and frame.
- advanced SELECT and aggregate structure: `distinct_on`, aggregate `FILTER`, and `qualify_predicates`.
- advanced grouping structure: `grouping_sets`, `rollup`, and `cube`.
- recursive and source structure: recursive `SEARCH`/`CYCLE` decorations and `lateral_sources`.

The original text-oriented fields remain for backward compatibility.

## Current Acceptance Baseline

- Typed IR structure: `77/77`, with 0 known gaps and 0 unexpected failures.
- Independent AST Diff: `53/53` supported.
- IR-to-AST Diff continuity: `77/77` supported.
- SQLite execution boundary metadata: 6 cases in each applicable structure report.
- Curated full flow: `47/47` at parse, structure, data, mutation, and attribution stages.
- Strict data generation: `195/195`, including `158/158` expected row-value counterexamples and 0 column-only counterexamples.
- Deterministic robustness fuzzer: `430/430` with seed `20260722`.

Run the complete fail-fast acceptance gate from the project root:

```bash
python data_construct_test/scripts/run_phase1_full_flow_gate.py
```

The gate uses the current Python executable and does not start Docker or an
external database service.
