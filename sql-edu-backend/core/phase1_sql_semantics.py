"""Low-level schema, value, and SQL semantic analysis helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from collections import Counter, defaultdict
import re
from sqlglot import exp
from core.ast_schema import ASTDiffNode
from core.witness_generation.schema_scope import SchemaCatalog
from core.witness_generation.obligations import stable_diff_id

from core.phase1_foundation import (
    NUMERIC_HINTS,
    _AGG_FUNC_TYPES,
    _MISSING,
    _boolean_absorption_rewrite_signature,
    _direct_from_table,
    _extract_column_name,
    _flatten_and,
    _generation_tactics_from_ast_diffs,
    _group_by_items,
    _is_cross_table_condition,
    _is_directly_negated,
    _is_inside_subquery,
    _like_counter_value,
    _like_escape_value,
    _like_truth_value,
    _literal_value,
    _normalized_predicate_operator,
    _outer_distinct_signature,
    _parse_sql,
    _predicate_source_column,
    _query_block_scope_key,
    _result_order_clause,
    _scrub_nested_query_bodies,
    _select_projection_repr,
    _set_operator_kp,
    _set_operator_modifier,
    _set_operator_name,
    _set_operator_node,
    _set_operator_signature,
    _split_schema_columns,
    _sql_of,
    _static_predicate_scalar,
    _top_select,
    _unqualified_sql,
    _unwrap_paren,
    _window_signature,
)



def parse_schema_text(schema: str) -> dict[str, list[str]]:
    """Parse compact schema text like table(col, col); [Order Details](...)."""
    tables: dict[str, list[str]] = {}
    for raw_part in schema.split(";"):
        part = raw_part.strip()
        if not part or "(" not in part or ")" not in part:
            continue
        name = part[: part.find("(")].strip()
        cols = part[part.find("(") + 1 : part.rfind(")")]
        table_name = _clean_identifier(name)
        # Tokenize respecting bracket/backtick/double-quote quoted identifiers
        col_tokens: list[str] = []
        for tok in _split_schema_columns(cols):
            tok = tok.strip()
            if not tok:
                continue
            m = re.match(r'(\[[^\]]+\]|`[^`]+`|"[^"]+")(\s+.*)?$', tok)
            if m:
                col_tokens.append(m.group(1))
            else:
                col_tokens.append(tok.split()[0])
        columns = [_clean_identifier(c) for c in col_tokens]
        columns = [col for col in columns if col]
        # Public teaching corpora occasionally contain duplicate display
        # headers. SQLite cannot create two physical columns with the same
        # normalized name, so keep the first occurrence deterministically.
        deduped: list[str] = []
        seen_columns: set[str] = set()
        for column in columns:
            normalized = _norm_name(column)
            if normalized in seen_columns:
                continue
            seen_columns.add(normalized)
            deduped.append(column)
        columns = deduped
        if table_name and columns:
            existing_name = next(
                (name for name in tables if _norm_name(name) == _norm_name(table_name)),
                None,
            )
            if existing_name is None:
                tables[table_name] = columns
            else:
                existing = tables[existing_name]
                known = {_norm_name(column) for column in existing}
                existing.extend(column for column in columns if _norm_name(column) not in known)
    return tables


def parse_schema_column_types(schema: str) -> dict[str, dict[str, str]]:
    """Parse optional compact column type hints from schema text.

    Supports both legacy `table(col, col)` and typed forms such as
    `orders(id BIGINT, created_at DATETIME, amount DECIMAL)`.
    """
    table_types: dict[str, dict[str, str]] = {}
    for raw_part in schema.split(";"):
        part = raw_part.strip()
        if not part or "(" not in part or ")" not in part:
            continue
        table_name = _clean_identifier(part[: part.find("(")].strip())
        if not table_name:
            continue
        cols = part[part.find("(") + 1 : part.rfind(")")]
        for tok in _split_schema_columns(cols):
            tok = tok.strip()
            if not tok:
                continue
            match = re.match(r'(\[[^\]]+\]|`[^`]+`|"[^"]+"|[A-Za-z_][\w$]*)(?:\s+(.+))?$', tok)
            if not match:
                continue
            column = _clean_identifier(match.group(1))
            type_hint = (match.group(2) or "").strip()
            if column and type_hint:
                table_types.setdefault(table_name, {})[column] = type_hint
    return table_types


def _atomic_student_variant(diff: ASTDiffNode) -> str | None:
    if diff.diff_type == "join_predicate_placement_changed":
        return str(diff.extra.get("student_query_sql") or "") or None
    if diff.diff_type in {"in_exists_equivalence", "null_sensitive_antijoin_equivalence"}:
        return str(
            diff.extra.get("student_query_sql")
            or diff.extra.get("student_sql")
            or ""
        ) or None
    if diff.diff_type == "correlated_predicate_changed":
        # The correlated detector compares the Exists nodes themselves.  A
        # NOT EXISTS mutation stores the polarity on the student's parent, so
        # replacing only the Exists node would silently produce the standard
        # query again and invalidate atomic attribution.
        if isinstance(diff.standard_node, exp.Expression) and isinstance(
            diff.student_node, exp.Expression
        ):
            replacement = diff.student_node.copy()
            if isinstance(diff.student_node.parent, exp.Not):
                replacement = exp.Not(this=replacement)
            root = diff.standard_node
            while isinstance(root.parent, exp.Expression):
                root = root.parent
            return _mutate_by_node_replacement(root, diff.standard_node, replacement)
    if diff.diff_type in {"limit_changed", "offset_changed"} and isinstance(
        diff.student_node, exp.Expression
    ):
        # Clause-addition diffs have no standard AST node. The student's clause
        # remains attached to its query block, so replay the complete student
        # query as the exact atomic variant.
        root = diff.student_node
        while isinstance(root.parent, exp.Expression):
            root = root.parent
        return _sql_of(root)
    standard_node = diff.standard_node
    if diff.diff_type in {
        "group_by_changed",
        "group_by_expression_changed",
        "grouping_grain_too_fine",
        "grouping_grain_too_coarse",
    }:
        standard_query_sql = str(diff.extra.get("standard_query_sql") or "")
        if standard_query_sql:
            root = _parse_sql(standard_query_sql)
            target = _top_select(root) if isinstance(root, exp.Expression) else None
            if isinstance(root, exp.Expression) and isinstance(target, exp.Select):
                student_group = (
                    diff.student_node
                    if isinstance(diff.student_node, exp.Group)
                    else None
                )
                if student_group is None:
                    student_query_sql = str(
                        diff.extra.get("student_query_sql") or ""
                    )
                    student_root = _parse_sql(student_query_sql)
                    student_select = (
                        _top_select(student_root)
                        if isinstance(student_root, exp.Expression)
                        else None
                    )
                    student_group = (
                        student_select.args.get("group")
                        if isinstance(student_select, exp.Select)
                        else None
                    )
                target.set(
                    "group",
                    student_group.copy()
                    if isinstance(student_group, exp.Group)
                    else None,
                )
                return _sql_of(root)
    if diff.diff_type == "predicate_added":
        student_node = diff.student_node
        standard_query_sql = str(diff.extra.get("standard_query_sql") or "")
        if not isinstance(student_node, exp.Expression) or not standard_query_sql:
            return None
        root = _parse_sql(standard_query_sql)
        target = _top_select(root) if isinstance(root, exp.Expression) else None
        if not isinstance(root, exp.Expression) or not isinstance(target, exp.Select):
            return None
        student_where = student_node.find_ancestor(exp.Where)
        if not isinstance(student_where, exp.Where):
            return None
        existing_where = target.args.get("where")
        if not isinstance(existing_where, exp.Where):
            if student_where.this is not student_node:
                return None
            target.set("where", exp.Where(this=student_node.copy()))
            return _sql_of(root)
        student_predicate = student_where.this
        if not isinstance(student_predicate, (exp.And, exp.Or)):
            return None
        existing_sql = _sql_of(existing_where.this).strip().upper()
        if existing_sql not in {
            _sql_of(student_predicate.left).strip().upper(),
            _sql_of(student_predicate.right).strip().upper(),
        }:
            return None
        target.set("where", student_where.copy())
        return _sql_of(root)
    if not isinstance(standard_node, exp.Expression):
        return None
    if diff.diff_type in {"set_operator_changed", "set_modifier_changed"}:
        student_node = diff.student_node
        if not isinstance(student_node, exp.Expression):
            return None
        if type(standard_node) is type(student_node):
            mutated = standard_node.copy()
            for argument in ("distinct", "by_name", "side", "kind"):
                mutated.set(argument, student_node.args.get(argument))
            replacement = mutated
        else:
            replacement = student_node
        root = standard_node
        while isinstance(root.parent, exp.Expression):
            root = root.parent
        if root is standard_node:
            return _sql_of(replacement)
        return _mutate_by_node_replacement(root, standard_node, replacement)
    root = standard_node
    while isinstance(root.parent, exp.Expression):
        root = root.parent
    replacement = diff.student_node if isinstance(diff.student_node, exp.Expression) else None
    if replacement is None and diff.diff_type in {
        "correlated_predicate_changed",
        "subquery_removed",
        "predicate_missing",
    }:
        # Removing a correlated EXISTS/IN predicate removes the enclosing
        # WHERE clause, not just the child AST node.  Popping the child would
        # render ``WHERE`` invalid and falsely mark the mutation unsupported.
        current: exp.Expression | None = standard_node
        while isinstance(current, exp.Expression):
            parent = current.parent
            if isinstance(parent, (exp.And, exp.Or)):
                sibling = parent.right if parent.left is current else parent.left
                if isinstance(sibling, exp.Expression):
                    return _mutate_by_node_replacement(root, parent, sibling)
            if isinstance(parent, exp.Where) and parent.this is current:
                query = parent.parent
                while isinstance(query, exp.Expression) and not isinstance(query, exp.Query):
                    query = query.parent
                if isinstance(query, exp.Query):
                    return _mutate_query_arg(root, query, "where", None)
            if isinstance(parent, exp.Where):
                break
            current = parent
    return _mutate_by_node_replacement(root, standard_node, replacement)


def _temporal_value_for_comparison(
    comparison: exp.Expression,
    literal: Any,
    *,
    true: bool,
) -> Any | None:
    """Choose a nearby value that satisfies/violates a scalar comparison."""
    operator = type(comparison)
    if literal is None:
        return None
    if isinstance(literal, (int, float, Decimal)) and not isinstance(literal, bool):
        if operator is exp.EQ:
            return literal if true else literal + 1
        if operator is exp.NEQ:
            return literal + 1 if true else literal
        if operator is exp.GT:
            return literal + 1 if true else literal
        if operator is exp.GTE:
            return literal if true else literal - 1
        if operator is exp.LT:
            return literal - 1 if true else literal
        if operator is exp.LTE:
            return literal if true else literal + 1
        return None
    parsed = _coerce_datetime(literal)
    if parsed is not None:
        delta = timedelta(days=1)
        if operator is exp.EQ:
            return parsed.strftime("%Y-%m-%d") if true else (parsed + delta).strftime("%Y-%m-%d")
        if operator is exp.NEQ:
            return (parsed + delta).strftime("%Y-%m-%d") if true else parsed.strftime("%Y-%m-%d")
        if operator is exp.GT:
            return (parsed + delta).strftime("%Y-%m-%d") if true else parsed.strftime("%Y-%m-%d")
        if operator is exp.GTE:
            return parsed.strftime("%Y-%m-%d") if true else (parsed - delta).strftime("%Y-%m-%d")
        if operator is exp.LT:
            return (parsed - delta).strftime("%Y-%m-%d") if true else parsed.strftime("%Y-%m-%d")
        if operator is exp.LTE:
            return parsed.strftime("%Y-%m-%d") if true else (parsed + delta).strftime("%Y-%m-%d")
    if operator is exp.EQ:
        return literal if true else f"__not_{literal}__"
    if operator is exp.NEQ:
        return f"__not_{literal}__" if true else literal
    return None


def _catalog_has_unary_unique_key(
    catalog: SchemaCatalog | None,
    ref: tuple[str, str],
) -> bool:
    if catalog is None:
        return False
    table = catalog.table(ref[0])
    if table is None:
        return False
    column = _norm_name(ref[1])
    if (
        len(table.primary_key) == 1
        and _norm_name(table.primary_key[0]) == column
    ):
        return True
    return any(
        len(constraint) == 1
        and _norm_name(constraint[0]) == column
        for constraint in table.unique_constraints
    )


def _simple_materialized_order_column(expression: str) -> str | None:
    node = _parse_sql(expression)
    if isinstance(node, exp.Ordered):
        node = node.this
    if not isinstance(node, exp.Column) or not node.name:
        return None
    return _norm_name(node.name)


def _order_materializer_values(column: str) -> tuple[Any, Any]:
    if _is_numeric_column(column):
        return 101, 202
    return "order_a", "order_z"


def _ordered_distinct_pair(
    left: Any,
    right: Any,
    column: str,
) -> tuple[Any, Any]:
    if left is None or right is None or left == right:
        return _order_materializer_values(column)
    try:
        ordered = sorted((left, right))
    except Exception:
        ordered = sorted((left, right), key=str)
    return ordered[0], ordered[1]




def _normalize_sqlite_order_aliases(
    sql: str,
    ast: exp.Expression | None,
) -> str:
    """Normalize unambiguous result aliases for SQLite ORDER BY execution."""

    if ast is None:
        return sql
    render_ast = ast.copy()
    changed = False
    has_explicit_null_placement = bool(
        re.search(r"(?is)\bNULLS\s+(?:FIRST|LAST)\b", sql)
    )
    for select in render_ast.find_all(exp.Select):
        order = select.args.get("order")
        if not isinstance(order, exp.Order):
            continue
        outputs: dict[str, list[int]] = {}
        for index, projection in enumerate(select.expressions, start=1):
            name = str(projection.alias_or_name or "").strip().lower()
            if name:
                outputs.setdefault(name, []).append(index)
        for ordered in order.expressions:
            expression = ordered.this if isinstance(ordered, exp.Ordered) else ordered
            if not isinstance(expression, exp.Column) or expression.table:
                continue
            # SQLGlot cannot reliably round-trip ``NULLS FIRST/LAST`` when
            # the key is rendered as a positional ORDER BY expression. Keep
            # the named column for explicit NULL placement so SQLite executes
            # the same ordering semantics as the source query.
            if has_explicit_null_placement and isinstance(ordered, exp.Ordered):
                continue
            positions = outputs.get(str(expression.name or "").lower(), [])
            if len(positions) != 1:
                continue
            expression.replace(exp.Literal.number(positions[0]))
            changed = True
    if not changed:
        return sql
    try:
        return render_ast.sql(dialect="sqlite")
    except Exception:
        return sql








def _simple_join_using_on_equivalent(
    using_ast: exp.Expression,
    on_ast: exp.Expression,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Recognize safe ``USING``/``NATURAL`` to explicit ``ON`` rewrites.

    ``USING`` and ``NATURAL`` also change the shape of ``SELECT *`` by
    coalescing duplicate key columns.  Restrict this fast path to explicit
    projections that do not observe those keys, where both forms have the
    same inner-join row semantics.
    """
    if (
        _set_operator_signature(using_ast) != _set_operator_signature(on_ast)
        or _window_signature(using_ast) != _window_signature(on_ast)
        or _outer_distinct_signature(using_ast) != _outer_distinct_signature(on_ast)
        or list(using_ast.find_all(exp.CTE))
        or list(on_ast.find_all(exp.CTE))
    ):
        return False
    using_select = _top_select(using_ast)
    on_select = _top_select(on_ast)
    if not isinstance(using_select, exp.Select) or not isinstance(on_select, exp.Select):
        return False
    using_joins = list(using_select.args.get("joins") or [])
    on_joins = list(on_select.args.get("joins") or [])
    if len(using_joins) != 1 or len(on_joins) != 1:
        return False
    using_join, on_join = using_joins[0], on_joins[0]
    if not isinstance(using_join, exp.Join) or not isinstance(on_join, exp.Join):
        return False
    if str(using_join.args.get("side") or using_join.args.get("kind") or "INNER").upper() not in {"", "INNER"}:
        return False
    if str(on_join.args.get("side") or on_join.args.get("kind") or "INNER").upper() not in {"", "INNER"}:
        return False
    if not isinstance(using_join.this, exp.Table) or not isinstance(on_join.this, exp.Table):
        return False
    using_from = _direct_from_table(using_select)
    on_from = _direct_from_table(on_select)
    if not using_from or not on_from:
        return False
    if _norm_name(using_from.name) != _norm_name(on_from.name):
        return False
    if _norm_name(using_join.this.name) != _norm_name(on_join.this.name):
        return False

    using_columns = [
        _norm_name(item.name)
        for item in (using_join.args.get("using") or [])
        if isinstance(item, exp.Identifier) and item.name
    ]
    is_natural = str(using_join.args.get("method") or "").upper() == "NATURAL"
    if not using_columns and not is_natural:
        return False
    if using_columns and is_natural:
        return False

    left_refs = {
        _norm_name(using_from.name),
        _norm_name(using_from.alias_or_name),
        _norm_name(on_from.name),
        _norm_name(on_from.alias_or_name),
    }
    right_refs = {
        _norm_name(using_join.this.name),
        _norm_name(using_join.this.alias_or_name),
        _norm_name(on_join.this.name),
        _norm_name(on_join.this.alias_or_name),
    }
    on_condition = on_join.args.get("on")
    if not isinstance(on_condition, exp.Expression):
        return False
    on_pairs: set[tuple[str, str]] = set()
    for predicate in _flatten_and(on_condition):
        if not isinstance(predicate, exp.EQ):
            return False
        columns = [predicate.left, predicate.right]
        if not all(isinstance(column, exp.Column) for column in columns):
            return False
        left_column, right_column = columns
        left_table = _norm_name(left_column.table)
        right_table = _norm_name(right_column.table)
        if left_table in left_refs and right_table in right_refs:
            pair = (left_column.name, right_column.name)
        elif right_table in left_refs and left_table in right_refs:
            pair = (right_column.name, left_column.name)
        else:
            return False
        on_pairs.add(tuple(_norm_name(item) for item in pair))
    if is_natural:
        left_schema = schema_catalog.table(using_from.name) if schema_catalog else None
        right_schema = schema_catalog.table(using_join.this.name) if schema_catalog else None
        if left_schema and right_schema:
            expected_columns = sorted(
                set(left_schema.columns) & set(right_schema.columns)
            )
        else:
            # Without a catalog, use the explicit ON pair as the bounded
            # fallback. A catalog-backed run remains authoritative for the
            # full NATURAL common-column set.
            expected_columns = sorted({left for left, right in on_pairs if left == right})
        expected_pairs = {(column, column) for column in expected_columns}
    else:
        expected_pairs = {(column, column) for column in using_columns}
    if not expected_pairs or on_pairs != expected_pairs:
        return False

    for select in (using_select, on_select):
        for expression in select.expressions or []:
            if expression.find(exp.Star):
                return False
            if any(
                _norm_name(column.name) in {item[0] for item in expected_pairs}
                for column in expression.find_all(exp.Column)
            ):
                return False
    if _select_projection_repr(using_ast) != _select_projection_repr(on_ast):
        return False
    for key in ("where", "group", "having", "order", "limit", "offset"):
        if _unqualified_sql(using_select.args.get(key)) != _unqualified_sql(on_select.args.get(key)):
            return False
    return True


def _schema_complete_star_projection_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    *,
    schema_catalog: SchemaCatalog | None,
) -> bool:
    """Recognize ``*`` versus the catalog's complete ordered column list."""
    if schema_catalog is None:
        return False
    standard = _top_select(standard_ast)
    student = _top_select(student_ast)
    if not isinstance(standard, exp.Select) or not isinstance(student, exp.Select):
        return False
    if any(
        list(ast.find_all(node_type))
        for ast in (standard_ast, student_ast)
        for node_type in (exp.CTE, exp.Subquery, exp.Union, exp.Intersect, exp.Except)
    ):
        return False
    if any(select.args.get("joins") for select in (standard, student)):
        return False
    standard_source = _direct_from_table(standard)
    student_source = _direct_from_table(student)
    if not standard_source or not student_source:
        return False
    if (
        standard_source.alias
        or student_source.alias
        or _norm_name(standard_source.name) != _norm_name(student_source.name)
    ):
        return False
    table_schema = schema_catalog.table(standard_source.name)
    if table_schema is None or not table_schema.columns:
        return False

    def projection_kind(
        select: exp.Select,
    ) -> tuple[str, tuple[str, ...]] | None:
        expressions = list(select.expressions or ())
        if len(expressions) == 1 and isinstance(expressions[0], exp.Star):
            return "star", tuple()
        columns: list[str] = []
        for expression in expressions:
            if isinstance(expression, exp.Alias) or not isinstance(expression, exp.Column):
                return None
            if expression.table:
                return None
            columns.append(_norm_name(expression.name))
        return "columns", tuple(columns)

    standard_projection = projection_kind(standard)
    student_projection = projection_kind(student)
    if standard_projection is None or student_projection is None:
        return False
    if {standard_projection[0], student_projection[0]} != {"star", "columns"}:
        return False
    explicit = (
        standard_projection[1]
        if standard_projection[0] == "columns"
        else student_projection[1]
    )
    expected = tuple(
        _norm_name(column.name)
        for column in table_schema.columns.values()
    )
    if explicit != expected:
        return False
    for key in (
        "distinct", "where", "group", "having", "order", "limit", "offset",
    ):
        if _unqualified_sql(standard.args.get(key)) != _unqualified_sql(
            student.args.get(key)
        ):
            return False
    return True


def _order_reference_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    *,
    schema_catalog: SchemaCatalog | None = None,
) -> bool:
    """Resolve safe ORDER BY ordinals and output aliases to projections."""

    def deterministic(expression: exp.Expression) -> bool:
        return not any(
            expression.find(node_type)
            for node_type in (exp.Func, exp.Subquery, exp.Window)
        )

    def normalized(ast: exp.Expression) -> tuple[str, bool]:
        copied = ast.copy()
        select = _top_select(copied)
        order = _result_order_clause(copied)
        if not isinstance(select, exp.Select) or not isinstance(order, exp.Order):
            return _sql_of(copied), False
        projections = list(select.expressions or [])
        aliases: dict[str, list[exp.Expression]] = defaultdict(list)
        for projection in projections:
            if isinstance(projection, exp.Alias) and projection.alias:
                aliases[_norm_name(projection.alias)].append(projection.this)
        source = _direct_from_table(select)
        source_columns: set[str] = set()
        if schema_catalog is not None and isinstance(source, exp.Table):
            table_schema = schema_catalog.table(source.name)
            if table_schema is not None:
                source_columns = {_norm_name(name) for name in table_schema.columns}

        changed = False
        for item in order.expressions or []:
            expression = item.this if isinstance(item, exp.Ordered) else item
            replacement: exp.Expression | None = None
            if isinstance(expression, exp.Literal) and not expression.is_string:
                try:
                    position = int(str(expression.this))
                except (TypeError, ValueError):
                    position = 0
                if str(position) == str(expression.this) and 1 <= position <= len(projections):
                    projected = projections[position - 1]
                    projected = projected.this if isinstance(projected, exp.Alias) else projected
                    if deterministic(projected):
                        replacement = projected
            elif isinstance(expression, exp.Column) and not expression.table:
                alias = _norm_name(expression.name)
                candidates = aliases.get(alias, [])
                if (
                    len(candidates) == 1
                    and alias not in source_columns
                    and deterministic(candidates[0])
                ):
                    replacement = candidates[0]
            if replacement is None:
                continue
            if isinstance(item, exp.Ordered):
                item.set("this", replacement.copy())
            else:
                item.replace(replacement.copy())
            changed = True
        return _sql_of(copied), changed

    standard = normalized(standard_ast)
    student = normalized(student_ast)
    return bool((standard[1] or student[1]) and standard[0] == student[0])


def _unreferenced_output_aliases_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Ignore only unreferenced aliases on the result-producing SELECT.

    Output labels are not part of Phase 1 row-value equivalence.  Aliases in
    CTEs and derived tables remain significant because they define columns
    visible to outer query blocks.  A top-level alias is also significant
    when another clause in the same block refers to it, so those cases are
    conservatively excluded from this rewrite.
    """
    if not isinstance(standard_ast, exp.Select) or not isinstance(
        student_ast, exp.Select
    ):
        return False

    def normalized(ast: exp.Select) -> tuple[str, bool] | None:
        copied = ast.copy()
        aliases = {
            _norm_name(item.alias)
            for item in copied.expressions
            if isinstance(item, exp.Alias) and item.alias
        }
        if not aliases:
            return _sql_of(copied), False

        # Projection aliases may be used by later clauses in the same SELECT.
        # Do not inspect nested SELECTs: their columns belong to another scope.
        for key, clause in copied.args.items():
            if key in {"expressions", "with", "with_"} or clause is None:
                continue
            nodes = clause if isinstance(clause, list) else [clause]
            for node in nodes:
                if not isinstance(node, exp.Expression):
                    continue
                for column in node.find_all(exp.Column):
                    if column.find_ancestor(exp.Select) is not copied:
                        continue
                    if not column.table and _norm_name(column.name) in aliases:
                        return None

        copied.set(
            "expressions",
            [
                item.this.copy() if isinstance(item, exp.Alias) else item.copy()
                for item in copied.expressions
            ],
        )
        return _sql_of(copied), True

    standard = normalized(standard_ast)
    student = normalized(student_ast)
    return bool(
        standard is not None
        and student is not None
        and (standard[1] or student[1])
        and standard[0] == student[0]
    )


def _between_closed_range_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Recognize positive BETWEEN as its two inclusive comparisons."""

    if not _rewrite_shape_compatible(standard_ast, student_ast):
        return False

    def normalized_signature(
        ast: exp.Expression,
    ) -> tuple[
        tuple[str, tuple[tuple[tuple[str, ...], ...] | None, ...]] | None,
        bool,
    ]:
        copied = ast.copy()
        changed = False
        for between in list(copied.find_all(exp.Between)):
            if (
                isinstance(between.parent, exp.Not)
                or between.args.get("symmetric")
                or between.this is None
                or between.args.get("low") is None
                or between.args.get("high") is None
            ):
                continue
            subject = between.this.copy()
            replacement = exp.and_(
                exp.GTE(
                    this=subject.copy(),
                    expression=between.args["low"].copy(),
                ),
                exp.LTE(
                    this=subject,
                    expression=between.args["high"].copy(),
                ),
            )
            between.replace(replacement)
            changed = True
        return _boolean_absorption_rewrite_signature(copied), changed

    standard_signature, standard_changed = normalized_signature(standard_ast)
    student_signature, student_changed = normalized_signature(student_ast)
    return bool(
        standard_changed != student_changed
        and standard_signature is not None
        and standard_signature == student_signature
    )


def _global_extreme_comparison_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    """Recognize equality to an unfiltered global MAX/MIN boundary.

    For rows read from relation R, ``x >= MAX_R(x)`` can only be true at the
    maximum and is therefore equivalent to equality (and symmetrically for
    ``MIN``/``<=``). The proof does not hold for filtered, grouped, joined or
    correlated subqueries, so those shapes are rejected here.
    """

    if not _rewrite_shape_compatible(standard_ast, student_ast):
        return False

    def signature(ast: exp.Expression) -> tuple[str, str] | None:
        if not isinstance(ast, exp.Select):
            return None
        copied = ast.copy()
        where = copied.args.get("where")
        comparison = _unwrap_paren(where.this) if isinstance(where, exp.Where) else None
        if not isinstance(comparison, (exp.EQ, exp.GTE, exp.LTE)):
            return None
        left = comparison.left
        right = comparison.right
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Subquery):
            return None
        inner = right.this
        if not isinstance(inner, exp.Select) or len(inner.expressions or ()) != 1:
            return None
        projected = inner.expressions[0]
        projected = projected.this if isinstance(projected, exp.Alias) else projected
        if not isinstance(projected, (exp.Max, exp.Min)):
            return None
        argument = projected.this
        if not isinstance(argument, exp.Column):
            return None
        if projected.args.get("distinct") or isinstance(argument, exp.Distinct):
            return None
        if any(
            copied.args.get(key)
            for key in ("joins", "group", "having", "limit", "offset", "with", "with_")
        ):
            return None
        if any(
            inner.args.get(key)
            for key in (
                "joins", "where", "group", "having", "limit",
                "offset", "order", "distinct", "with", "with_",
            )
        ):
            return None

        outer_table = _direct_from_table(copied)
        inner_table = _direct_from_table(inner)
        if (
            not isinstance(outer_table, exp.Table)
            or not isinstance(inner_table, exp.Table)
            or _norm_name(outer_table.name) != _norm_name(inner_table.name)
            or _norm_name(left.name) != _norm_name(argument.name)
        ):
            return None

        def belongs_to(column: exp.Column, table: exp.Table) -> bool:
            qualifier = _norm_name(column.table or "")
            return not qualifier or qualifier in {
                _norm_name(table.name),
                _norm_name(table.alias or ""),
            }

        if not belongs_to(left, outer_table) or not belongs_to(argument, inner_table):
            return None
        if isinstance(projected, exp.Max):
            if not isinstance(comparison, (exp.EQ, exp.GTE)):
                return None
            extreme = "MAX"
        else:
            if not isinstance(comparison, (exp.EQ, exp.LTE)):
                return None
            extreme = "MIN"
        where.set(
            "this",
            exp.EQ(this=left.copy(), expression=right.copy()),
        )
        return _sql_of(copied), extreme

    standard_signature = signature(standard_ast)
    student_signature = signature(student_ast)
    return (
        standard_signature is not None
        and standard_signature == student_signature
        and _sql_of(standard_ast) != _sql_of(student_ast)
    )


def _from_source_expression_signature(node: exp.Expression | None) -> str:
    """Describe a FROM expression without embedding its whole query body.

    A nested predicate, projection or DISTINCT edit changes the SQL text of a
    derived table, but it does not change the relation topology of the outer
    query.  Embedding the complete subquery here made those dependent edits
    look like an additional ``FROM`` mutation and prevented atomic repair
    attribution.  Keep the direct source/join shape while still detecting a
    real table or set-branch source change.
    """
    if isinstance(node, exp.Table):
        return f"TABLE:{_norm_name(node.name)}"
    if isinstance(node, exp.Subquery):
        alias = _norm_name(node.alias or "")
        return f"SUBQUERY:{alias}:{_from_source_query_shape(node.this)}"
    if isinstance(node, exp.Expression):
        return f"{type(node).__name__.upper()}:{_from_source_query_shape(node)}"
    return ""


def _from_source_query_shape(node: exp.Expression | None) -> str:
    """Return bounded direct relation topology for one query expression."""
    if isinstance(node, exp.SetOperation):
        return (
            f"SET:{type(node).__name__.upper()}"
            f"({_from_source_query_shape(node.this)}|"
            f"{_from_source_query_shape(node.expression)})"
        )
    select = node if isinstance(node, exp.Select) else None
    if select is None and isinstance(node, exp.Expression):
        select = node.find(exp.Select)
    if not isinstance(select, exp.Select):
        return type(node).__name__.upper() if isinstance(node, exp.Expression) else ""
    from_clause = select.args.get("from_") or select.args.get("from")
    source = from_clause.this if isinstance(from_clause, exp.From) else None
    source_shape = _from_source_expression_signature(source)
    joins: list[tuple[str, str]] = []
    for join in select.args.get("joins") or []:
        target = join.this
        side = str(join.args.get("side") or join.args.get("kind") or "INNER").upper()
        joins.append((_from_source_expression_signature(target), side))
    return f"BLOCK:{source_shape}:JOINS:{tuple(joins)}"


def _from_source_signature(ast: exp.Expression | None) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """Return direct FROM sources and JOIN table/type topology for each SELECT.

    This deliberately excludes ON predicates, which are normalized separately
    by ``_extract_join_graph`` so explicit and implicit inner joins remain a
    supported equivalence rewrite.
    """
    if ast is None:
        return ()
    signatures: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for select in ast.find_all(exp.Select):
        from_clause = select.args.get("from_") or select.args.get("from")
        source = from_clause.this if isinstance(from_clause, exp.From) else None
        # Alias changes do not alter the relation being read.  For derived
        # sources, retain only direct relation topology; nested predicate and
        # projection edits are reported by their own query-block diff.
        source_sql = _from_source_expression_signature(source)
        joins: list[tuple[str, str]] = []
        # A comma source is represented by sqlglot as ``CROSS`` JOIN.  When
        # the WHERE clause supplies a cross-table equality, the existing join
        # normalizer treats it as an INNER join; mirror that here so the
        # supported implicit-vs-explicit INNER JOIN rewrite remains valid.
        join_graph = _extract_join_graph(select)
        normalized_join_sides = {
            _norm_name(table): side
            for table, side, _ in join_graph.get("joins", [])
        }
        for join in select.args.get("joins") or []:
            target = join.this
            table_sql = _from_source_expression_signature(target)
            side = str(join.args.get("side") or join.args.get("kind") or "INNER").upper()
            if side == "CROSS":
                target_name = _norm_name(target.name) if isinstance(target, exp.Table) else ""
                side = normalized_join_sides.get(target_name, side)
            joins.append((table_sql, side))
        signatures.append((source_sql, tuple(joins)))
    return tuple(signatures)


def _from_source_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> list[ASTDiffNode]:
    """Emit a focused diff when a query reads a different source relation."""
    std_sig = _from_source_signature(standard_ast)
    stu_sig = _from_source_signature(student_ast)
    if std_sig == stu_sig:
        return []
    return [ASTDiffNode(
        clause_category="FROM",
        diff_type="from_source_changed",
        standard_node=standard_ast.find(exp.From),
        student_node=student_ast.find(exp.From),
        knowledge_point_id="select-basic",
        severity=0.76,
        extra={
            "standard_sources": std_sig,
            "student_sources": stu_sig,
            "standard_sql": _sql_of(standard_ast.find(exp.From)),
            "student_sql": _sql_of(student_ast.find(exp.From)),
        },
    )]


def _rewrite_shape_compatible(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    *,
    allow_cte_inline: bool = False,
) -> bool:
    """Guard semantic-rewrite shortcuts from crossing structural boundaries."""
    if _set_operator_signature(standard_ast) != _set_operator_signature(student_ast):
        return False
    if _window_signature(standard_ast) != _window_signature(student_ast):
        return False
    if _outer_distinct_signature(standard_ast) != _outer_distinct_signature(student_ast):
        return False
    if _from_source_signature(standard_ast) != _from_source_signature(student_ast):
        return False
    if not allow_cte_inline:
        std_ctes = tuple(_sql_of(node) for node in standard_ast.find_all(exp.CTE))
        stu_ctes = tuple(_sql_of(node) for node in student_ast.find_all(exp.CTE))
        if std_ctes != stu_ctes:
            return False
    return True


def _where_boolean_absorption_equivalent(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> bool:
    if not _rewrite_shape_compatible(standard_ast, student_ast):
        return False
    standard_signature = _boolean_absorption_rewrite_signature(standard_ast)
    student_signature = _boolean_absorption_rewrite_signature(student_ast)
    return (
        standard_signature is not None
        and standard_signature == student_signature
    )


def _extract_logical_skeleton(node: exp.Expression) -> dict[str, Any]:
    """Recursively extract the boolean skeleton of a WHERE expression.

    Returns a dict with:
      - ``operators``: sorted list of ``"AND"`` / ``"OR"`` tokens
      - ``leaves``:    sorted list of leaf comparison SQL strings
    """
    operators: list[str] = []
    leaves: list[str] = []

    def _walk(n: exp.Expression) -> None:
        if isinstance(n, exp.And):
            operators.append("AND")
            _walk(n.left)
            _walk(n.right)
        elif isinstance(n, exp.Or):
            operators.append("OR")
            _walk(n.left)
            _walk(n.right)
        elif isinstance(n, exp.Not):
            # Record NOT as a prefix on the leaf so that NOT(a=1) ≠ a=1.
            inner = n.this
            if isinstance(inner, (exp.And, exp.Or)):
                # NOT wrapping a boolean operator: record NOT and recurse
                operators.append("NOT")
                _walk(inner)
            else:
                # NOT wrapping a leaf comparison: serialise the whole NOT expression
                leaves.append(_sql_of(n))
        elif isinstance(n, exp.Paren):
            _walk(n.this)
        else:
            leaves.append(_sql_of(n))

    _walk(node)
    return {
        "operators": sorted(operators),
        "leaves": sorted(leaves),
    }


def _logical_operator_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> list[ASTDiffNode]:
    """Detect AND ↔ OR swaps inside WHERE clauses.

    If both queries have the same set of leaf comparisons but connect them
    with different boolean operators, emit ``logical_operator_changed``.
    """
    std_where = standard_ast.args.get("where") or standard_ast.find(exp.Where)
    stu_where = student_ast.args.get("where") or student_ast.find(exp.Where)
    if std_where is None or stu_where is None:
        return []

    std_skel = _extract_logical_skeleton(std_where.this)
    stu_skel = _extract_logical_skeleton(stu_where.this)

    # Different boolean operator structure → logical operator changed.
    # (Previously required identical leaves, but NOT on leaves changes the leaf text.)
    if std_skel["operators"] != stu_skel["operators"] or std_skel["leaves"] != stu_skel["leaves"]:
        # Only report if the structural difference is in the boolean skeleton,
        # not just a simple predicate value change (those are caught by comparison_ast_diffs).
        if std_skel["operators"] != stu_skel["operators"]:
            source = _direct_from_table(_top_select(standard_ast))
            return [ASTDiffNode(
                clause_category="LOGICAL",
                diff_type="logical_operator_changed",
                standard_node=std_where,
                student_node=stu_where,
                knowledge_point_id="where",
                severity=0.8,
                extra={
                    "standard_operators": std_skel["operators"],
                    "student_operators": stu_skel["operators"],
                    "leaves": std_skel["leaves"],
                    "standard_sql": _sql_of(std_where),
                    "student_sql": _sql_of(stu_where),
                    "standard_predicate_sql": _sql_of(std_where.this),
                    "student_predicate_sql": _sql_of(stu_where.this),
                    "standard_source_table": (
                        source.name if isinstance(source, exp.Table) else ""
                    ),
                },
            )]

    return []


def _predicate_negation_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
) -> list[ASTDiffNode]:
    """Keep predicate negation visible inside JOIN and CASE expressions.

    ``NOT`` is the parent of ``IN``/``IS`` in sqlglot, so comparing only the
    predicate node loses ``IN`` versus ``NOT IN`` and ``IS NULL`` versus
    ``IS NOT NULL``.  These predicates can also live outside WHERE, where the
    generic comparison pass intentionally does not inspect them.
    """
    diffs: list[ASTDiffNode] = []

    def paired_nodes(
        standard_nodes: list[exp.Expression],
        student_nodes: list[exp.Expression],
    ) -> list[tuple[exp.Expression, exp.Expression]]:
        """Pair unchanged predicate bodies before using positional fallback."""
        remaining_student = list(student_nodes)
        pairs: list[tuple[exp.Expression, exp.Expression]] = []
        for standard_node in standard_nodes:
            standard_key = re.sub(r"\s+", "", _sql_of(standard_node).lower())
            exact_index = next(
                (
                    index
                    for index, student_node in enumerate(remaining_student)
                    if re.sub(r"\s+", "", _sql_of(student_node).lower())
                    == standard_key
                ),
                None,
            )
            if exact_index is None:
                continue
            pairs.append((standard_node, remaining_student.pop(exact_index)))
        # A single changed body can still carry a polarity change.  Only use a
        # fallback when it is unambiguous; never pair B with C merely because
        # an intermediate IN was rewritten as EXISTS.
        if len(standard_nodes) - len(pairs) == 1 and len(remaining_student) == 1:
            standard_node = next(
                node for node in standard_nodes if all(node is not pair[0] for pair in pairs)
            )
            student_node = remaining_student[0]
            if _extract_column_name(standard_node) == _extract_column_name(student_node):
                pairs.append((standard_node, student_node))
        return pairs

    specs = (
        (exp.In, "IN", "in_predicate_negation_changed", "in-list"),
        (exp.Is, "NULL", "null_predicate_negation_changed", "null-handling"),
    )
    for node_type, clause, diff_type, kp_id in specs:
        standard_nodes = list(standard_ast.find_all(node_type))
        student_nodes = list(student_ast.find_all(node_type))
        for standard_node, student_node in paired_nodes(standard_nodes, student_nodes):
            standard_negated = _is_directly_negated(standard_node)
            student_negated = _is_directly_negated(student_node)
            if standard_negated == student_negated:
                continue
            standard_render_node = standard_node.parent if standard_negated else standard_node
            student_render_node = student_node.parent if student_negated else student_node
            standard_in = standard_node
            standard_inner = standard_in.args.get("query") if isinstance(standard_in, exp.In) else None
            standard_inner_select = (
                standard_inner.this
                if isinstance(standard_inner, exp.Subquery)
                and isinstance(standard_inner.this, exp.Select)
                else None
            )
            standard_outer_select = standard_node.find_ancestor(exp.Select)
            standard_outer_source = _direct_from_table(standard_outer_select)
            standard_inner_source = _direct_from_table(standard_inner_select)
            standard_projected = (
                standard_inner_select.expressions[0]
                if isinstance(standard_inner_select, exp.Select)
                and standard_inner_select.expressions
                else None
            )
            standard_projected = (
                standard_projected.this
                if isinstance(standard_projected, exp.Alias)
                else standard_projected
            )
            diffs.append(ASTDiffNode(
                clause_category=clause,
                diff_type=diff_type,
                target_column=_extract_column_name(standard_node),
                standard_node=standard_render_node,
                student_node=student_render_node,
                knowledge_point_id=kp_id,
                severity=0.82,
                extra={
                    "standard_negated": standard_negated,
                    "student_negated": student_negated,
                    "standard_sql": _sql_of(standard_render_node),
                    "student_sql": _sql_of(student_render_node),
                    "standard_source_table": (
                        standard_outer_source.name
                        if isinstance(standard_outer_source, exp.Table)
                        else ""
                    ),
                    "standard_membership_table": (
                        standard_inner_source.name
                        if isinstance(standard_inner_source, exp.Table)
                        else ""
                    ),
                    "standard_outer_column": _extract_column_name(standard_in.this),
                    "standard_membership_column": _extract_column_name(standard_projected),
                    "standard_in_values": tuple(
                        _literal_value(item)
                        for item in standard_in.expressions
                        if isinstance(item, exp.Literal)
                    ) if isinstance(standard_in, exp.In) else (),
                },
            ))
    return diffs


def _comparison_descriptor(node: exp.Expression) -> dict[str, Any] | None:
    if isinstance(node, exp.In) and isinstance(node.this, exp.Column):
        values = [_literal_value(item) for item in node.expressions if isinstance(item, exp.Literal)]
        return {"column": node.this.name, "op": "IN", "value": values[0] if values else None, "values": values, "value_kind": "literal", "sql": _sql_of(node), "node": node}
    if isinstance(node, exp.Between) and isinstance(node.this, exp.Column):
        return {
            "column": node.this.name,
            "op": "BETWEEN",
            "value": _literal_value(node.args.get("low")),
            "high": _literal_value(node.args.get("high")),
            "value_kind": "literal",
            "sql": _sql_of(node),
            "node": node,
        }
    if isinstance(node, exp.Is) and isinstance(node.this, exp.Column):
        return {"column": node.this.name, "op": "IS", "value": None, "value_is_null": True, "value_kind": "literal", "sql": _sql_of(node), "node": node}
    if isinstance(node, exp.Like) and isinstance(node.this, exp.Column):
        return {
            "column": node.this.name,
            "op": "LIKE",
            "value": _literal_value(node.expression),
            "escape": _like_escape_value(node),
            "value_kind": "literal",
            "sql": _sql_of(node),
            "node": node,
        }
    if isinstance(node, exp.Glob) and isinstance(node.this, exp.Column):
        return {
            "column": node.this.name,
            "op": "GLOB",
            "value": _literal_value(node.expression),
            "value_kind": "literal",
            "sql": _sql_of(node),
            "node": node,
        }
    left, right = getattr(node, "left", None), getattr(node, "right", None)
    if isinstance(left, exp.Column) and isinstance(right, exp.Column):
        return {
            "column": left.name,
            "left_table": left.table,
            "op": type(node).__name__.upper(),
            "value": _sql_of(right),
            "value_kind": "column",
            "right_column": right.name,
            "right_table": right.table,
            "sql": _sql_of(node),
            "node": node,
        }
    if isinstance(left, exp.Column) and isinstance(right, (exp.Literal, exp.Null)):
        return {
            "column": left.name,
            "op": type(node).__name__.upper(),
            "value": _literal_value(right),
            "value_kind": "literal",
            "value_is_null": isinstance(right, exp.Null),
            "sql": _sql_of(node),
            "node": node,
        }
    if isinstance(right, exp.Column) and isinstance(left, (exp.Literal, exp.Null)):
        return {
            "column": right.name,
            "op": type(node).__name__.upper(),
            "value": _literal_value(left),
            "value_kind": "literal",
            "value_is_null": isinstance(left, exp.Null),
            "sql": _sql_of(node),
            "node": node,
        }
    if isinstance(left, exp.Column) and right is not None:
        return {
            "column": left.name,
            "op": type(node).__name__.upper(),
            "value": _sql_of(right),
            "value_kind": "expression",
            "sql": _sql_of(node),
            "node": node,
        }
    if isinstance(right, exp.Column) and left is not None:
        return {
            "column": right.name,
            "op": type(node).__name__.upper(),
            "value": _sql_of(left),
            "value_kind": "expression",
            "sql": _sql_of(node),
            "node": node,
        }
    # Fallback: any expression on the left (function call, arithmetic, etc.)
    # compared to a literal on the right.  E.g. YEAR(hire_date) = 2020, x + 1 > 5.
    if left is not None and isinstance(right, (exp.Literal, exp.Null)):
        col_name = _extract_column_name(left)
        return {
            "column": col_name or _sql_of(left),
            "op": type(node).__name__.upper(),
            "value": _literal_value(right),
            "value_kind": "literal",
            "value_is_null": isinstance(right, exp.Null),
            "sql": _sql_of(node),
            "node": node,
        }
    # Mirror: literal on the left, expression on the right.
    if right is not None and isinstance(left, (exp.Literal, exp.Null)):
        col_name = _extract_column_name(right)
        return {
            "column": col_name or _sql_of(right),
            "op": type(node).__name__.upper(),
            "value": _literal_value(left),
            "value_kind": "literal",
            "value_is_null": isinstance(left, exp.Null),
            "sql": _sql_of(node),
            "node": node,
        }
    return None


def _extract_join_graph(ast: exp.Expression) -> dict[str, Any]:
    """Extract a normalised join graph from a query.

    Both explicit (``JOIN ... ON``) and implicit (``FROM a, b WHERE ...``)
    styles produce the same structure so they compare equal when
    semantically equivalent.

    Returns::

        {
            "joins": [(right_table, join_type, node), ...],
            "conditions": [sorted ON/condition SQL strings],
            "from_tables": [tables in FROM clause],
        }
    """
    joins: list[tuple[str, str, Any]] = []
    conditions: list[str] = []

    has_explicit_on = False  # True if any Join has an ON clause

    # ── JOIN nodes (explicit JOIN ... ON and implicit FROM a, b) ──
    for join_node in ast.find_all(exp.Join):
        jn = join_node.this
        if isinstance(jn, exp.Table):
            table = jn.name
        elif isinstance(jn, exp.Subquery) and jn.alias:
            table = jn.alias
        else:
            table = ""
        side = str(join_node.args.get("side") or join_node.args.get("kind") or "INNER").upper()
        joins.append((table, side, join_node))
        on = join_node.args.get("on")
        if on:
            has_explicit_on = True
            for pred in _flatten_and(on):
                conditions.append(_sql_of(pred))
        using = join_node.args.get("using") or []
        if using:
            # sqlglot stores USING columns as Identifier nodes rather than an
            # expression. Include each key in the same condition signature used
            # for ON predicates so changes such as USING(id) -> USING(code) are
            # visible to the AST diff graph.
            has_explicit_on = True
            conditions.append(
                f"USING ({', '.join(_sql_of(column) for column in using)})"
            )

    # ── FROM clause tables ──
    # Only extract the direct child of FROM (don't recurse into subqueries).
    from_clause = ast.args.get("from_") or ast.args.get("from")
    from_tables: list[str] = []
    if isinstance(from_clause, exp.From):
        child = from_clause.this
        if isinstance(child, exp.Table):
            from_tables.append(child.name)
        elif isinstance(child, exp.Subquery) and child.alias:
            from_tables.append(child.alias)

    # All known table names (FROM + Join nodes)
    all_tables = set(from_tables) | {t for t, _, _ in joins}

    # ── Implicit join: extract cross-table conditions from WHERE ──
    # sqlglot represents FROM a, b as From(a) + Join(b, no ON).
    # If no Join had an ON clause, cross-table WHERE predicates are join conditions.
    if not has_explicit_on and len(all_tables) > 1:
        where = ast.args.get("where") or ast.find(exp.Where)
        if where:
            for pred in _flatten_and(where.this):
                # Only EQ cross-table predicates are join conditions;
                # OR nodes and non-equality comparisons are filters, not joins.
                if _is_cross_table_condition(pred):
                    conditions.append(_sql_of(pred))

    if conditions:
        joins = [
            (table, "INNER" if side == "CROSS" else side, node)
            for table, side, node in joins
        ]

    return {
        "joins": joins,
        "conditions": sorted(conditions),
        "from_tables": sorted(from_tables),
    }


def _set_operator_ast_diffs(standard_ast: exp.Expression, student_ast: exp.Expression) -> list[ASTDiffNode]:
    std_op = _set_operator_name(standard_ast)
    stu_op = _set_operator_name(student_ast)
    std_node = _set_operator_node(standard_ast)
    stu_node = _set_operator_node(student_ast)
    std_modifier = _set_operator_modifier(std_node)
    stu_modifier = _set_operator_modifier(stu_node)
    def branch_metadata(node: exp.Expression | None) -> dict[str, Any]:
        select = node if isinstance(node, exp.Select) else node.find(exp.Select) if isinstance(node, exp.Expression) else None
        source = _direct_from_table(select)
        projection = []
        if isinstance(select, exp.Select):
            for item in select.expressions or ():
                expression = item.this if isinstance(item, exp.Alias) else item
                if isinstance(expression, exp.Column):
                    projection.append(expression.name)
        return {
            "source_table": source.name if isinstance(source, exp.Table) else "",
            "projection_columns": tuple(projection),
        }
    standard_left = branch_metadata(std_node.this if isinstance(std_node, exp.SetOperation) else None)
    standard_right = branch_metadata(std_node.expression if isinstance(std_node, exp.SetOperation) else None)
    student_left = branch_metadata(stu_node.this if isinstance(stu_node, exp.SetOperation) else None)
    student_right = branch_metadata(stu_node.expression if isinstance(stu_node, exp.SetOperation) else None)
    # No set operator in either → no diff
    if not std_op and not stu_op:
        return []
    # Detect both operator changes and duplicate semantics (UNION vs UNION ALL).
    if std_op != stu_op or std_modifier != stu_modifier:
        kp = _set_operator_kp(std_op or stu_op)
        diffs = [ASTDiffNode(
            clause_category=std_op or stu_op,
            diff_type="set_operator_changed",
            standard_node=std_node or standard_ast,
            student_node=stu_node or student_ast,
            knowledge_point_id=kp,
            extra={
                "standard_op": std_op,
                "student_op": stu_op,
                "standard_modifier": std_modifier,
                "student_modifier": stu_modifier,
                "standard_sql": _sql_of(std_node),
                "student_sql": _sql_of(stu_node),
                "standard_left_source_table": standard_left["source_table"],
                "standard_right_source_table": standard_right["source_table"],
                "standard_projection_columns": standard_left["projection_columns"],
                "student_left_source_table": student_left["source_table"],
                "student_right_source_table": student_right["source_table"],
                "student_projection_columns": student_left["projection_columns"],
            }
        )]
        if std_op == stu_op and std_modifier != stu_modifier:
            diffs.append(ASTDiffNode(
                clause_category=std_op or "SET OPERATION",
                diff_type="set_modifier_changed",
                standard_node=std_node,
                student_node=stu_node,
                knowledge_point_id=kp,
                severity=0.78,
                extra={
                    "operator": std_op,
                    "standard_modifier": std_modifier,
                    "student_modifier": stu_modifier,
                    "standard_sql": _sql_of(std_node),
                    "student_sql": _sql_of(stu_node),
                },
            ))
        return diffs
    return []


def _window_spec(node: exp.Window) -> dict[str, Any]:
    order_items: list[tuple[str, bool, bool]] = []
    order_columns: list[str] = []
    order = node.args.get("order")
    if isinstance(order, exp.Order):
        for item in order.expressions or ():
            ordered = item if isinstance(item, exp.Ordered) else None
            expression = ordered.this if ordered is not None else item
            if not isinstance(expression, exp.Expression):
                continue
            descending = bool(ordered.args.get("desc")) if ordered is not None else False
            # sqlglot normalizes SQLite's implicit NULL placement into the
            # Ordered node.  Keeping the semantic value here makes
            # ``ASC NULLS FIRST`` equal to plain ASC while preserving an
            # actual NULLS FIRST/LAST change.
            nulls_first = (
                bool(ordered.args.get("nulls_first"))
                if ordered is not None and ordered.args.get("nulls_first") is not None
                else not descending
            )
            expression_sql = _sql_of(expression)
            order_items.append((expression_sql, descending, nulls_first))
            if isinstance(expression, exp.Column):
                order_columns.append(_sql_of(expression))
    return {
        "partition_by": [_sql_of(item) for item in (node.args.get("partition_by") or [])],
        "order": _sql_of(node.args.get("order")),
        "frame": _sql_of(node.args.get("spec")),
        "order_items": tuple(order_items),
        "order_columns": tuple(order_columns),
    }


def _case_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    filter_subqueries: bool = True,
) -> list[ASTDiffNode]:
    """Detect direct CASE expression changes.

    Clause-level SELECT diffs can already reveal CASE changes, but that loses
    the teaching structure. This emits an explicit CASE diff so downstream
    feedback can point students to WHEN/THEN/ELSE logic.
    """
    _skip = _is_inside_subquery if filter_subqueries else (lambda _: False)
    std_cases = [_sql_of(node) for node in standard_ast.find_all(exp.Case) if not _skip(node)]
    stu_cases = [_sql_of(node) for node in student_ast.find_all(exp.Case) if not _skip(node)]
    if std_cases != stu_cases:
        standard_case = next(
            (node for node in standard_ast.find_all(exp.Case) if not _skip(node)),
            None,
        )
        student_case = next(
            (node for node in student_ast.find_all(exp.Case) if not _skip(node)),
            None,
        )
        standard_source = _direct_from_table(_top_select(standard_ast))
        case_metadata = {
            "standard_case_when_predicates": tuple(
                _sql_of(item.this)
                for item in (standard_case.args.get("ifs") or ())
                if isinstance(item, exp.If)
            ) if isinstance(standard_case, exp.Case) else (),
            "student_case_when_predicates": tuple(
                _sql_of(item.this)
                for item in (student_case.args.get("ifs") or ())
                if isinstance(item, exp.If)
            ) if isinstance(student_case, exp.Case) else (),
            "standard_source_table": (
                standard_source.name if isinstance(standard_source, exp.Table) else ""
            ),
        }
        standard_predicates = tuple(case_metadata["standard_case_when_predicates"])
        student_predicates = tuple(case_metadata["student_case_when_predicates"])
        # The unchanged WHEN branches do not need to be re-covered for a
        # single-branch mutation. Record the branch indexes that were removed
        # (or changed) so the semantic validator can require the causal path
        # instead of every unrelated CASE branch.
        case_metadata["required_case_branch_indexes"] = tuple(
            index
            for index, predicate in enumerate(standard_predicates)
            if index >= len(student_predicates) or predicate != student_predicates[index]
        )
        diffs = [ASTDiffNode(
            clause_category="CASE",
            diff_type="case_changed",
            standard_node=standard_ast.find(exp.Case),
            student_node=student_ast.find(exp.Case),
            knowledge_point_id="case",
            severity=0.68,
            extra={
                "standard_sql": " | ".join(std_cases) if std_cases else "",
                "student_sql": " | ".join(stu_cases) if stu_cases else "",
                **case_metadata,
            },
        )]
        std_nodes = [node for node in standard_ast.find_all(exp.Case) if not _skip(node)]
        stu_nodes = [node for node in student_ast.find_all(exp.Case) if not _skip(node)]
        for std_node, stu_node in zip(std_nodes, stu_nodes):
            std_default = std_node.args.get("default")
            stu_default = stu_node.args.get("default")
            if bool(std_default) != bool(stu_default):
                diffs.append(ASTDiffNode(
                    clause_category="CASE",
                    diff_type="case_else_missing" if std_default and not stu_default else "case_else_added",
                    standard_node=std_default,
                    student_node=stu_default,
                    knowledge_point_id="case",
                    severity=0.78,
                    extra={
                        "standard_sql": _sql_of(std_default),
                        "student_sql": _sql_of(stu_default),
                        **case_metadata,
                    },
                ))
            std_ifs = std_node.args.get("ifs") or []
            stu_ifs = stu_node.args.get("ifs") or []
            if len(std_ifs) != len(stu_ifs):
                diffs.append(ASTDiffNode(
                    clause_category="CASE",
                    diff_type="case_when_missing" if len(std_ifs) > len(stu_ifs) else "case_when_added",
                    standard_node=std_node,
                    student_node=stu_node,
                    knowledge_point_id="case",
                    severity=0.78,
                    extra={
                        "standard_when_count": len(std_ifs),
                        "student_when_count": len(stu_ifs),
                        **case_metadata,
                    },
                ))
        return diffs
    return []


def _subquery_predicate_context_sql(node: exp.Expression) -> str:
    current: exp.Expression = node
    parent = current.parent
    while parent is not None:
        if isinstance(parent, (exp.Where, exp.Having)):
            return _sql_of(_scrub_nested_query_bodies(current))
        if isinstance(parent, exp.Join):
            return _sql_of(_scrub_nested_query_bodies(current))
        current = parent
        parent = parent.parent
    return _sql_of(_scrub_nested_query_bodies(current))


def _aggregate_function_ast_diffs(
    standard_ast: exp.Expression,
    student_ast: exp.Expression,
    filter_subqueries: bool = True,
) -> list[ASTDiffNode]:
    """Detect when the same column uses a different aggregate function.

    E.g. ``AVG(score)`` → ``SUM(score)`` produces
    ``aggregate_function_changed`` with ``target_column="score"``.
    """
    _skip = _is_inside_subquery if filter_subqueries else (lambda _: False)
    def _collect_aggs(ast: exp.Expression) -> dict[str, tuple[str, exp.Expression]]:
        """Map column_name → (func_name, node) for each aggregate."""
        result: dict[str, tuple[str, exp.Expression]] = {}
        for node in ast.find_all(*_AGG_FUNC_TYPES):
            if _skip(node):
                continue
            col = node.find(exp.Column)
            col_name = col.name if col else "*"
            func_name = type(node).__name__.upper()
            result[col_name] = (func_name, node)
        return result

    std_aggs = _collect_aggs(standard_ast)
    stu_aggs = _collect_aggs(student_ast)

    diffs: list[ASTDiffNode] = []
    for col_name, (std_func, std_node) in std_aggs.items():
        if col_name in stu_aggs:
            stu_func, stu_node = stu_aggs[col_name]
            if std_func != stu_func:
                diffs.append(ASTDiffNode(
                    clause_category="AGGREGATE",
                    diff_type="aggregate_function_changed",
                    target_column=col_name,
                    standard_node=std_node,
                    student_node=stu_node,
                    knowledge_point_id="aggregate",
                    severity=0.7,
                    extra={
                        "standard_func": std_func,
                        "student_func": stu_func,
                        "column": col_name,
                        "standard_sql": _sql_of(std_node),
                        "student_sql": _sql_of(stu_node),
                        "standard_aggregate_function": std_func,
                        "student_aggregate_function": stu_func,
                        "standard_aggregate_argument": _sql_of(std_node.this) if std_node.this is not None else "*",
                        "student_aggregate_argument": _sql_of(stu_node.this) if stu_node.this is not None else "*",
                        "standard_group_columns": [sql for sql, _ in _group_by_items(standard_ast)],
                        "student_group_columns": [sql for sql, _ in _group_by_items(student_ast)],
                    },
                ))
    return diffs


def _extract_literal_constraints(sql: str) -> list[dict[str, Any]]:
    ast = _parse_sql(sql)
    if not ast:
        return []
    constraints: list[dict[str, Any]] = []
    for node in ast.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
        if _is_inside_subquery(node):
            continue
        left, right = node.left, node.right
        right_value = _expression_static_value(right)
        left_value = _expression_static_value(left)
        if isinstance(left, exp.Column) and right_value is not None:
            constraints.append({"column": left.name, "op": type(node).__name__, "value": right_value,
                                "table": left.table or None})
        elif isinstance(right, exp.Column) and left_value is not None:
            constraints.append({"column": right.name, "op": type(node).__name__, "value": left_value,
                                "table": right.table or None})
    for node in ast.find_all(exp.Like):
        if _is_inside_subquery(node):
            continue
        if isinstance(node.this, exp.Column) and isinstance(node.expression, exp.Literal):
            constraints.append({"column": node.this.name, "op": "LIKE", "value": _literal_value(node.expression),
                                "table": node.this.table or None})
    for node in ast.find_all(exp.In):
        if _is_inside_subquery(node):
            continue
        if isinstance(node.this, exp.Column):
            values = [_literal_value(item) for item in node.expressions if isinstance(item, exp.Literal)]
            if values:
                constraints.append({"column": node.this.name, "op": "IN", "value": values[0], "values": values,
                                    "table": node.this.table or None})
    for node in ast.find_all(exp.Between):
        if _is_inside_subquery(node):
            continue
        if isinstance(node.this, exp.Column):
            low_val = _expression_static_value(node.args.get("low"))
            high_val = _expression_static_value(node.args.get("high"))
            constraints.append({"column": node.this.name, "op": "BETWEEN", "value": low_val, "high": high_val,
                                "table": node.this.table or None})
            constraints.append({"column": node.this.name, "op": "BETWEEN", "value": high_val, "high": low_val,
                                "table": node.this.table or None})
    for node in ast.find_all(exp.Is):
        if _is_inside_subquery(node):
            continue
        if isinstance(node.this, exp.Column):
            is_not_null = isinstance(node.expression, exp.Not) or (
                hasattr(node, "args") and node.args.get("not")
            )
            constraints.append({
                "column": node.this.name,
                "op": "IS_NOT_NULL" if is_not_null else "IS_NULL",
                "value": None,
                "table": node.this.table or None
            })
    for node in ast.find_all(exp.NullSafeEQ, exp.NullSafeNEQ):
        if _is_inside_subquery(node):
            continue
        column = node.left if isinstance(node.left, exp.Column) else node.right
        if isinstance(column, exp.Column):
            constraints.append({
                "column": column.name,
                "op": "NULL_SAFE_COMPARISON",
                "value": None,
                "table": column.table or None,
            })
    # Handle NOT(IS NULL) pattern
    for node in ast.find_all(exp.Not):
        if _is_inside_subquery(node):
            continue
        inner = node.this
        if isinstance(inner, exp.Is) and isinstance(inner.this, exp.Column):
            constraints.append({
                "column": inner.this.name,
                "op": "IS_NOT_NULL",
                "value": None,
                "table": inner.this.table or None
            })
    return constraints


def _expression_static_value(node: exp.Expression | None) -> Any:
    if node is None:
        return None
    literal = _literal_value(node)
    if isinstance(node, exp.Literal):
        return literal
    if isinstance(node, exp.Neg):
        value = _expression_static_value(node.this)
        if isinstance(value, (int, float, Decimal)):
            return -value
        return None
    if isinstance(node, exp.Column) and not node.table:
        identifier = node.this
        if isinstance(identifier, exp.Identifier) and identifier.args.get("quoted"):
            return node.name
    return None






def _aggregate_group_probe_value(column: str, group: int, position: int) -> Any:
    """Return a unique, non-cyclic key for one aggregate probe group."""

    serial = group + position * 100
    if _is_date_column(column):
        return (datetime(2035, 1, 1) + timedelta(days=serial)).strftime("%Y-%m-%d")
    if _is_numeric_column(column):
        return 700000 + serial
    return f"__aggregate_group_{position}_{group}__"


def _positive_group_filter_value(
    column: str,
    constraints: list[dict[str, Any]],
    fallback: Any,
    index: int,
) -> Any:
    if _is_date_column(column):
        dates = sorted(
            str(value)
            for item in constraints
            for value in (item.get("value"), item.get("high"))
            if _coerce_datetime(value) is not None
        )
        if dates:
            base = _coerce_datetime(dates[0])
            if base is not None:
                return (base + timedelta(days=index % 2)).strftime("%Y-%m-%d")
    if isinstance(fallback, (int, float, Decimal)):
        return fallback + (index % 2)
    return fallback if index % 2 == 0 else f"{fallback}__group_alt"


def _apply_count_group_probe(
    rows: list[dict[str, Any]],
    group_col: str,
    boundary: int,
    *,
    group_cols: list[str] | None = None,
    value_col: str | None = None,
    distinct: bool = False,
) -> None:
    if not rows:
        return
    resolved_group_cols = list(dict.fromkeys(group_cols or [group_col]))
    exact = max(1, boundary)
    high = max(1, boundary + 1)
    low = max(1, boundary - 1)
    targets = [exact]
    remaining = len(rows) - exact
    if remaining >= high:
        targets.append(high)
        remaining -= high
    if remaining >= low:
        targets.append(low)
    elif remaining > 0:
        targets.append(remaining)
    group_names = ["Comp. Sci.", "Math", "Physics", "History", "Biology"]
    idx = 0
    for group_index, (group_name, count) in enumerate(zip(group_names, targets)):
        group_values = {
            column: (
                group_name
                if column == group_col
                and not _is_numeric_column(column)
                and not _is_date_column(column)
                else _group_probe_value(column, group_index, position)
            )
            for position, column in enumerate(resolved_group_cols)
        }
        for member_index in range(count):
            if idx >= len(rows):
                return
            rows[idx].update(group_values)
            if distinct and value_col and value_col != group_col:
                rows[idx][value_col] = f"__having_distinct_{group_name}_{member_index}__"
            idx += 1
    fallback_values = {
        column: (
            group_names[-1]
            if column == group_col
            and not _is_numeric_column(column)
            and not _is_date_column(column)
            else _group_probe_value(column, len(group_names) - 1, position)
        )
        for position, column in enumerate(resolved_group_cols)
    }
    while idx < len(rows):
        rows[idx].update(fallback_values)
        idx += 1


def _rich_comparison_truth_value(
    comparison: exp.Expression,
    desired: bool,
) -> tuple[exp.Column, Any] | None:
    """Find one column/literal pair and produce a value for the column."""
    if not isinstance(comparison, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        return None
    left_column = _predicate_source_column(comparison.left)
    right_column = _predicate_source_column(comparison.right)
    left_scalar = _static_predicate_scalar(comparison.left)
    right_scalar = _static_predicate_scalar(comparison.right)
    if left_column is not None and right_column is None and right_scalar is not _MISSING:
        source_on_left = True
        column, scalar = left_column, right_scalar
    elif right_column is not None and left_column is None and left_scalar is not _MISSING:
        source_on_left = False
        column, scalar = right_column, left_scalar
    else:
        return None

    operator = type(comparison)
    if not source_on_left:
        operator = {
            exp.GT: exp.LT,
            exp.GTE: exp.LTE,
            exp.LT: exp.GT,
            exp.LTE: exp.GTE,
        }.get(operator, operator)
    if operator is exp.EQ:
        value = scalar if desired else _counter_value(column.name, scalar)
    elif operator is exp.NEQ:
        value = _counter_value(column.name, scalar) if desired else scalar
    elif isinstance(scalar, (int, float, Decimal)) and not isinstance(scalar, bool):
        if operator is exp.GT:
            value = scalar + 1 if desired else scalar
        elif operator is exp.GTE:
            value = scalar if desired else scalar - 1
        elif operator is exp.LT:
            value = scalar - 1 if desired else scalar
        elif operator is exp.LTE:
            value = scalar if desired else scalar + 1
        else:
            return None
    else:
        return None
    return column, value


def _rich_predicate_truth_value(
    node: exp.Expression,
    desired: bool,
) -> tuple[exp.Column, Any] | None:
    """Produce a bounded source-cell assignment for common reachability forms."""
    if isinstance(node, exp.Not):
        return _rich_predicate_truth_value(node.this, not desired)
    if isinstance(node, exp.Like):
        column = _predicate_source_column(node.this)
        pattern = _static_predicate_scalar(node.expression)
        if column is None or pattern is _MISSING:
            return None
        return column, _like_truth_value(pattern, desired)
    if isinstance(node, exp.Is):
        column = _predicate_source_column(node.this)
        if column is None:
            return None
        scalar = _static_predicate_scalar(node.expression)
        if scalar is None:
            return column, None if desired else _seed_value(column.name, 0)
        return column, scalar if desired else _counter_value(column.name, scalar)
    if isinstance(node, exp.In):
        column = _predicate_source_column(node.this)
        values = [
            _static_predicate_scalar(item)
            for item in (node.expressions or ())
        ]
        values = [value for value in values if value is not _MISSING]
        if column is None or not values:
            return None
        return column, values[0] if desired else _counter_value(column.name, values[0])
    if isinstance(node, exp.Between):
        column = _predicate_source_column(node.this)
        low = _static_predicate_scalar(node.args.get("low"))
        high = _static_predicate_scalar(node.args.get("high"))
        if column is None or low is _MISSING or high is _MISSING:
            return None
        if desired:
            return column, low
        if isinstance(high, (int, float, Decimal)) and not isinstance(high, bool):
            return column, high + 1
        return column, _counter_value(column.name, high)
    comparison = _rich_comparison_truth_value(node, desired)
    if comparison is not None:
        return comparison
    return None


def _scalar_predicate_values(
    comparison: exp.Expression,
    scalar: Any,
    column: str,
    *,
    column_on_left: bool,
) -> tuple[Any, Any] | None:
    operator = _normalized_predicate_operator(
        comparison,
        column_on_left=column_on_left,
    )
    counter = _counter_value(column, scalar)
    if operator is exp.EQ:
        return scalar, counter
    if operator is exp.NEQ:
        return counter, scalar
    if not isinstance(scalar, (int, float, Decimal)) or isinstance(
        scalar, bool
    ):
        return None
    if operator is exp.GT:
        return scalar + 1, scalar
    if operator is exp.GTE:
        return scalar, scalar - 1
    if operator is exp.LT:
        return scalar - 1, scalar
    if operator is exp.LTE:
        return scalar, scalar + 1
    return None


def _seed_value(col: str, idx: int) -> Any:
    """
    根据列名分发基础测试数据，并强制包含单调性以检测 ORDER BY 错误。
    Generates a mock seed value for a column based on token name heuristics,
    ensuring monotonicity to expose ORDER BY/sorting logic bugs.
    """
    name = col.lower()

    # 姓名列循环生成
    if name == "name":
        return ["Alice", "Bob", "Carol", "Dave"][idx % 4]

    # 地理数据类型填充
    if name == "location":
        return f"POINT({idx} {idx})"

    # 日期字段：自增递增（单调性，支持 ORDER BY 校验）
    if _is_date_column(col):
        return f"2024-01-{(idx % 9) + 1:02d}"

    # 数字类型：idx + 1 单调递增自增，用于检测 >、>=、LIMIT 和聚合运算
    if _is_numeric_column(col):
        return idx + 1

    # 教学系统常用分类字段循环填充
    if "semester" in name:
        return ["Fall", "Spring", "Summer", "Winter"][idx % 4]
    if "grade" in name:
        return ["A", "B", "C", None][idx % 4]
    if "country" in name:
        return ["USA", "UK", "Germany", "Canada"][idx % 4]
    if "title" in name:
        return ["Sales Manager", "Marketing Lead", "Engineer", "Analyst"][idx % 4]
    if "dept" in name:
        return ["Comp. Sci.", "Math", "Physics", "History"][idx % 4]
    if "name" in name:
        return ["Alice", "Bob", "Carol", "Dave"][idx % 4]

    # 兜底生成唯一字符串，避免碰撞
    return f"{_clean_identifier(col)}_{idx + 1}"


def _counter_value(col: str, value: Any) -> Any:
    if value is None:
        return _seed_value(col, 3)
    if isinstance(value, (int, float, Decimal)):
        return value + 999
    text = str(value)
    if "%" in text or "_" in text:
        return _like_counter_value(text)
    if text:
        return f"not_{text}"
    return "counter_value"


def _counter_probe_value(item: dict[str, Any]) -> Any:
    op = str(item.get("op") or "").upper()
    value = item.get("value")
    values = item.get("values") or []
    if op in {"GT", ">"}:
        return value
    if op in {"GTE", "GE", ">="} and isinstance(value, (int, float, Decimal)):
        return value - 1
    if op in {"LT", "<"}:
        return value
    if op in {"LTE", "LE", "<="} and isinstance(value, (int, float, Decimal)):
        return value + 1
    if op == "EQ" and isinstance(value, (int, float, Decimal)):
        return value + 1
    if op == "NEQ" and isinstance(value, (int, float, Decimal)):
        return value
    if op == "IN" and values:
        if isinstance(values[0], (int, float, Decimal)):
            return max(values) + 1
        return f"not_{values[0]}"
    if op == "BETWEEN" and isinstance(value, (int, float, Decimal)):
        high = item.get("high")
        if isinstance(high, (int, float, Decimal)):
            return high + 1
        return value - 1
    if op == "LIKE" and isinstance(value, str):
        return _like_counter_value(value)
    if op == "IS":
        return "not_null"
    return _counter_value(str(item.get("column") or ""), value)


def _build_data_evidence(
    *,
    is_equivalent: bool,
    ordered: bool,
    standard_columns: list[str],
    student_columns: list[str],
    standard_rows: list[tuple[Any, ...]],
    student_rows: list[tuple[Any, ...]],
    standard_ast: exp.Expression | None,
    student_ast: exp.Expression | None,
    student_exec_error: str | None,
    ast_diffs: list[ASTDiffNode],
) -> dict[str, Any]:
    standard_counter = Counter(standard_rows)
    student_counter = Counter(student_rows)
    only_standard = list((standard_counter - student_counter).elements())[:5]
    only_student = list((student_counter - standard_counter).elements())[:5]
    duplicate_student = sum(count - 1 for count in student_counter.values() if count > 1)
    duplicate_standard = sum(count - 1 for count in standard_counter.values() if count > 1)
    suspected_cartesian = (
        _join_count(student_ast) > 0
        and not _has_join_on(student_ast)
        and len(student_rows) > max(len(standard_rows) * 2, len(standard_rows) + 3)
    )
    return {
        "sandbox_executed": True,
        "judge_status": "CORRECT" if is_equivalent else "WRONG",
        "student_exec_ok": student_exec_error is None,
        "student_exec_error": student_exec_error,
        "is_equivalent_on_generated_data": is_equivalent,
        "ordered_compare": ordered,
        "row_count_match": len(standard_rows) == len(student_rows),
        "standard_row_count": len(standard_rows),
        "student_row_count": len(student_rows),
        "columns_match": len(standard_columns) == len(student_columns),
        "column_names_match": standard_columns == student_columns,
        "standard_columns": standard_columns,
        "student_columns": student_columns,
        "standard_duplicate_row_count": duplicate_standard,
        "student_duplicate_row_count": duplicate_student,
        "suspected_cartesian_product": suspected_cartesian,
        "only_in_standard_sample": only_standard,
        "only_in_student_sample": only_student,
        "standard_sample_rows": standard_rows[:5],
        "student_sample_rows": student_rows[:5],
        "ast_diffs": [
            {
                **diff.extra,
                "diff_id": stable_diff_id(diff, index),
                "obligation_id": (
                    "obligation_"
                    + stable_diff_id(diff, index).removeprefix("diff_")
                ),
                "clause": diff.clause_category,
                "diff_type": diff.diff_type,
                "column": diff.target_column,
                "table": diff.target_table,
                "standard_sql": diff.extra.get("standard_sql") or _sql_of(diff.standard_node),
                "student_sql": diff.extra.get("student_sql") or _sql_of(diff.student_node),
            }
            for index, diff in enumerate(ast_diffs)
        ],
        "generation_tactics": _generation_tactics_from_ast_diffs(ast_diffs),
    }


def _mutate_by_node_replacement(
    ast: exp.Expression,
    target_node: exp.Expression,
    replacement_node: exp.Expression | None
) -> str | None:
    # ``find_all`` walks descendants and therefore does not include the root
    # itself.  Projection and clause summary diffs can legitimately target
    # that root, so handle the identity case before descendant indexing.
    if ast is target_node:
        return _sql_of(replacement_node) if replacement_node is not None else None
    mutated = ast.copy()
    target_type = type(target_node)
    orig_nodes = list(ast.find_all(target_type))
    idx = -1
    for i, node in enumerate(orig_nodes):
        if id(node) == id(target_node):
            idx = i
            break
    if idx == -1:
        return None

    mutated_nodes = list(mutated.find_all(target_type))
    if idx < len(mutated_nodes):
        node_to_mutate = mutated_nodes[idx]
        if replacement_node is not None:
            node_to_mutate.replace(replacement_node.copy())
        else:
            node_to_mutate.pop()
        return _sql_of(mutated)
    return None


def _mutate_query_arg(
    ast: exp.Expression,
    query: exp.Query,
    arg: str,
    replacement: exp.Expression | None,
) -> str | None:
    mutated = ast.copy()
    target_scope = _query_block_scope_key(query)
    target = next(
        (
            node
            for node in mutated.walk()
            if isinstance(node, exp.Query)
            and _query_block_scope_key(node) == target_scope
        ),
        None,
    )
    if not isinstance(target, exp.Query):
        return None
    target.set(arg, replacement.copy() if replacement is not None else None)
    return _sql_of(mutated)


def _mutate_query_expressions(
    ast: exp.Expression,
    query: exp.Query,
    expressions: list[exp.Expression],
) -> str | None:
    mutated = ast.copy()
    target_scope = _query_block_scope_key(query)
    target = next(
        (
            node
            for node in mutated.walk()
            if isinstance(node, exp.Select)
            and _query_block_scope_key(node) == target_scope
        ),
        None,
    )
    if not isinstance(target, exp.Select):
        return None
    target.set("expressions", [expression.copy() for expression in expressions])
    return _sql_of(mutated)


def _sqlite_type(col: str) -> str:
    if _is_date_column(col):
        return "TEXT"
    return "REAL" if _is_numeric_column(col) else "TEXT"


def _sqlite_declared_affinity(col: str, declared_type: str | None) -> str:
    """Map authoritative schema types to SQLite affinity without constraints."""
    if not declared_type:
        return _sqlite_type(col)
    declared = str(declared_type).upper()
    if "INT" in declared:
        return "INTEGER"
    if any(token in declared for token in ("CHAR", "CLOB", "TEXT", "DATE", "TIME", "UUID", "JSON")):
        return "TEXT"
    if "BLOB" in declared or not declared.strip():
        return "BLOB"
    if any(token in declared for token in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _is_date_column(col: str) -> bool:
    name = col.lower()
    tokens = [token for token in re.split(r"[^a-z0-9]+", name) if token]
    if name in {"date", "datetime", "timestamp"}:
        return True
    if (
        name.endswith("date")
        or name.endswith("datetime")
        or name.endswith("timestamp")
        or name.endswith("time")
    ):
        return True
    if name.endswith("_at") or name.endswith("_on"):
        return True
    if any(token in {"date", "bdate", "time"} for token in tokens):
        return True
    if any(token in {"start", "end"} for token in tokens) and any(token in {"date", "time"} for token in tokens):
        return True
    return False


def _is_numeric_column(col: str) -> bool:
    name = col.lower()
    if name in {
        "x", "y", "z", "n", "age", "people", "temperature", "month",
        "quarter", "rank", "row_num", "row_number", "tiv_2015", "tiv_2016",
    }:
        return True
    tokens = [token for token in re.split(r"[^a-z0-9]+", name) if token]
    if any(
        token in {
            "age", "people", "temperature", "allotment", "tiv",
            # Reporting corpora commonly omit SQL types and expose measures
            # such as ASSIGNABLE_AREA/GROSS_SQFT as bare identifiers.  Keep
            # these exact-token hints narrow so AREA_NAME remains text.
            "area", "sqft", "footage", "duration", "days", "minutes",
            "seconds",
        }
        for token in tokens
    ):
        return True
    return any(token in name for token in NUMERIC_HINTS)


def _is_key_column(col: str) -> bool:
    name = col.lower()
    return name == "id" or name.endswith("_id") or name.endswith("id") or name in {"ssn", "dno", "dnum", "pno"}


def _table_key_aliases(table_name: str) -> set[str]:
    tokens = [token for token in re.split(r"[_\\W]+", table_name) if token]
    aliases = {f"{table_name}_id", f"{table_name}id"}
    if table_name:
        aliases.add(f"{table_name.rstrip('s')}_id")
    if tokens:
        aliases.add(f"{tokens[-1]}_id")
        aliases.add(f"{tokens[-1]}id")
    common = {
        "employee": {"emp_id", "empid", "employee_id"},
        "department": {"dept_id", "deptid", "department_id", "dno", "dnum", "dnumber"},
        "course": {"course_id", "courseid"},
        "student": {"id", "student_id", "sid"},
        "instructor": {"id", "instructor_id", "iid"},
    }
    aliases.update(common.get(table_name, set()))
    return aliases


def _unique_key_value(col: str, idx: int, seen: set[Any], duplicate_value: Any) -> Any:
    base = _seed_value(col, idx)
    if isinstance(duplicate_value, (int, float)) and abs(duplicate_value) >= 100:
        base = duplicate_value + idx
    if isinstance(base, (int, float)):
        candidate: Any = base
        while candidate in seen:
            candidate += 1000
        return candidate
    candidate = str(base)
    suffix = 1
    while candidate in seen:
        suffix += 1
        candidate = f"{base}_{suffix}"
    return candidate


def _join_count(ast: exp.Expression | None) -> int:
    return len(list(ast.find_all(exp.Join))) if ast else 0


def _has_join_on(ast: exp.Expression | None) -> bool:
    return any(bool(join.args.get("on")) for join in ast.find_all(exp.Join)) if ast else False


def _prepare_sqlite_source(sql: str) -> str:
    """Canonicalize a validated SQLite query before deterministic rendering."""
    sql = sql.strip()
    # SQLite accepts RECURSIVE for both recursive and ordinary CTEs.  Keeping
    # one spelling makes execution evidence stable without changing meaning.
    sql = re.sub(r"(?is)^\s*WITH\s+(?!RECURSIVE\b)", "WITH RECURSIVE ", sql, count=1)
    return sql


def _rewrite_bare_offset(sql: str) -> str:
    pattern = re.compile(r"(?is)(\bLIMIT\s+[^\s;]+\s+)?\bOFFSET\s+(\d+)\b")

    def replace(match: re.Match) -> str:
        limit = match.group(1)
        if limit:
            return f"{limit}OFFSET {match.group(2)}"
        return f"LIMIT -1 OFFSET {match.group(2)}"

    return pattern.sub(replace, sql)


































def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        for pattern in (
            "%Y/%m/%d",
            "%m/%d/%Y",
            "%Y%m%d",
            "%H:%M:%S",
            "%I:%M:%S %p",
            "%I:%M %p",
            "%I%M%p",
        ):
            try:
                return datetime.strptime(text, pattern)
            except ValueError:
                continue
    return None






































def _positive_numeric_series_for_comparison(
    comparison: exp.Expression,
    boundary: int | float | Decimal,
    count: int,
) -> list[Any]:
    if isinstance(comparison, (exp.GT, exp.GTE, exp.EQ)):
        start = boundary + (1 if isinstance(comparison, exp.GT) else 0)
        return [start + index for index in range(count)]
    if isinstance(comparison, (exp.LT, exp.LTE)):
        start = boundary - (1 if isinstance(comparison, exp.LT) else 0)
        return [start - index for index in range(count)]
    return [boundary for _ in range(count)]


def _extend_order_series(values: list[Any], count: int) -> list[Any]:
    if not values:
        return list(range(count))
    unique: list[Any] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    if len(unique) >= count:
        return unique[:count]
    last = unique[-1]
    if isinstance(last, (int, float, Decimal)):
        while len(unique) < count:
            last = last + 1
            unique.append(last)
        return unique
    parsed = _coerce_datetime(last)
    if parsed is not None:
        while len(unique) < count:
            parsed = parsed + timedelta(days=1)
            unique.append(parsed.strftime("%Y-%m-%d"))
        return unique
    while len(unique) < count:
        unique.append(f"{last}__{len(unique):03d}")
    return unique


def _comparison_matches(node: exp.Expression, value: Any) -> bool:
    literal_node = node.right if isinstance(node.right, exp.Literal) else node.left
    literal = _literal_value(literal_node)
    if isinstance(node, exp.EQ):
        return value == literal
    if isinstance(node, exp.NEQ):
        return value != literal
    if not isinstance(value, (int, float, Decimal)) or not isinstance(
        literal, (int, float, Decimal)
    ):
        return False
    if isinstance(node, exp.GT):
        return value > literal
    if isinstance(node, exp.GTE):
        return value >= literal
    if isinstance(node, exp.LT):
        return value < literal
    if isinstance(node, exp.LTE):
        return value <= literal
    return False


def _comparison_truth_value(node: exp.Expression, desired: bool) -> Any | None:
    if not isinstance(node.left, exp.Column) or not isinstance(node.right, exp.Literal):
        return None
    literal = _literal_value(node.right)
    if isinstance(node, exp.EQ):
        if desired:
            return literal
        if isinstance(literal, (int, float, Decimal)):
            return literal + 999
        return f"not_{literal}"
    if isinstance(node, exp.NEQ):
        if not desired:
            return literal
        if isinstance(literal, (int, float, Decimal)):
            return literal + 999
        return f"not_{literal}"
    if not isinstance(literal, (int, float, Decimal)):
        return None
    if isinstance(node, exp.GT):
        return literal + 1 if desired else literal
    if isinstance(node, exp.GTE):
        return literal if desired else literal - 1
    if isinstance(node, exp.LT):
        return literal - 1 if desired else literal
    if isinstance(node, exp.LTE):
        return literal if desired else literal + 1
    return None


def _logical_leaf_key(node: exp.Expression) -> str:
    return _sql_of(_unwrap_paren(node))


def _logical_leaf_nodes(node: exp.Expression) -> list[exp.Expression]:
    leaves: list[exp.Expression] = []

    def walk(current: exp.Expression) -> None:
        current = _unwrap_paren(current)
        if isinstance(current, (exp.And, exp.Or)):
            walk(current.left)
            walk(current.right)
        else:
            leaves.append(current)

    walk(node)
    return leaves


def _eval_logical_tree(node: exp.Expression, values: dict[str, bool]) -> bool:
    node = _unwrap_paren(node)
    if isinstance(node, exp.And):
        return _eval_logical_tree(node.left, values) and _eval_logical_tree(node.right, values)
    if isinstance(node, exp.Or):
        return _eval_logical_tree(node.left, values) or _eval_logical_tree(node.right, values)
    return values[_logical_leaf_key(node)]


def _predicate_truth_assignment(node: exp.Expression, desired: bool) -> tuple[exp.Column, Any] | None:
    node = _unwrap_paren(node)
    if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        if isinstance(node.left, exp.Column) and isinstance(node.right, exp.Literal):
            value = _comparison_truth_value(node, desired)
            return (node.left, value) if value is not None else None
    if isinstance(node, exp.Like) and isinstance(node.this, exp.Column) and isinstance(node.expression, exp.Literal):
        pattern = str(_literal_value(node.expression))
        if desired:
            candidate = pattern.replace("%", "X").replace("_", "X")
        else:
            candidate = "__no_like_match__"
        return node.this, candidate
    return None


def _window_companion_aliases(
    specs: dict[str, list[tuple[exp.Expression, int | float]]],
    changed_aliases: set[str],
) -> set[str]:
    companions: set[str] = set()
    comparison_alias = {
        id(comparison): alias
        for alias, values in specs.items()
        for comparison, _ in values
    }
    for alias in changed_aliases:
        for comparison, _ in specs.get(alias, []):
            current = comparison.parent
            while isinstance(current, exp.And):
                for candidate in current.find_all(
                    exp.EQ,
                    exp.NEQ,
                    exp.GT,
                    exp.GTE,
                    exp.LT,
                    exp.LTE,
                ):
                    companion = comparison_alias.get(id(candidate))
                    if companion and companion != alias:
                        companions.add(companion)
                current = current.parent
    return companions


def _window_partition_columns(window: exp.Window) -> list[exp.Column]:
    columns: list[exp.Column] = []
    for expression in window.args.get("partition_by") or []:
        candidates = [expression] if isinstance(expression, exp.Column) else list(expression.find_all(exp.Column))
        for column in candidates:
            if column not in columns:
                columns.append(column)
    return columns


def _assign_window_groups(
    rows: list[dict[str, Any]],
    columns: list[str],
    group_size: int,
) -> None:
    if not rows or not columns:
        return
    group_size = max(1, group_size)
    for index, row in enumerate(rows):
        group = index // group_size
        for position, column in enumerate(columns):
            row[column] = _group_probe_value(column, group, position + 20)


def _assign_window_order_values(
    rows: list[dict[str, Any]],
    columns: list[str],
) -> None:
    for index, row in enumerate(rows):
        for position, column in enumerate(columns):
            if _is_date_column(column):
                row[column] = f"2024-02-{(index % 28) + 1:02d}"
            elif _is_numeric_column(column):
                row[column] = index * 10 + position + 1
            else:
                row[column] = f"__window_order_{position}_{index:04d}__"


def _group_probe_value(column: str, bucket: int, salt: int) -> Any:
    if _is_date_column(column):
        day = 1 + ((bucket + salt) % 28)
        return f"2024-01-{day:02d}"
    if _is_numeric_column(column):
        return 100 + salt * 10 + bucket
    return f"__group_{salt}_{bucket}__"


def _clean_identifier(value: str) -> str:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if value.startswith("`") and value.endswith("`"):
        value = value[1:-1]
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value.strip()


def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", _clean_identifier(value).lower())
