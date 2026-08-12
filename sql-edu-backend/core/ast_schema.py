from dataclasses import dataclass, field
import re
from typing import Any, Dict, List, Optional

from sqlglot import exp


def _node_sql(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    try:
        return node.sql(dialect="sqlite", normalize=True)
    except Exception:
        return str(node)


def _serialize_ir_value(value: Any) -> Any:
    if isinstance(value, exp.Expression):
        return _node_sql(value)
    if isinstance(value, dict):
        return {str(key): _serialize_ir_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize_ir_value(item) for item in value]
    return value


def _function_name(node: Any) -> str:
    if node is None:
        return ""
    raw = ""
    if isinstance(node, exp.Anonymous):
        raw = str(node.this or "")
    elif isinstance(node, exp.Func):
        raw = type(node).__name__
    else:
        raw = type(node).__name__
    raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw)
    return raw.upper()


def _literal_value(node: Any) -> Any:
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Literal):
        if node.is_string:
            return str(node.this)
        text = str(node.this)
        try:
            return int(text)
        except Exception:
            try:
                return float(text)
            except Exception:
                return text
    return _node_sql(node)


def _expr_kind(node: Any) -> str:
    if node is None:
        return "unknown"
    if isinstance(node, exp.Alias):
        return _expr_kind(node.this)
    if isinstance(node, exp.Column):
        return "column"
    if isinstance(node, exp.Star):
        return "star"
    if isinstance(node, exp.Literal):
        return "literal"
    if isinstance(node, exp.Case):
        return "case"
    if isinstance(node, exp.Window):
        return "window"
    if isinstance(node, exp.Collate):
        return "collate"
    if isinstance(node, exp.AggFunc):
        return "aggregate"
    if isinstance(node, exp.Func):
        return "function"
    if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod)):
        return "arithmetic"
    return type(node).__name__.lower()


def _expression_summary(node: Any, context: str) -> dict[str, Any]:
    alias = node.alias if isinstance(node, exp.Alias) else None
    expr = node.this if isinstance(node, exp.Alias) else node
    item: dict[str, Any] = {
        "context": context,
        "kind": _expr_kind(expr),
        "sql": _node_sql(node),
        "alias": alias,
    }
    if isinstance(expr, exp.Column):
        item["table"] = str(expr.table or "")
        item["column"] = str(expr.name or "")
    elif isinstance(expr, exp.Literal):
        item["value"] = _literal_value(expr)
    elif isinstance(expr, exp.Func):
        item["function"] = _function_name(expr)
    elif isinstance(expr, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod)):
        item["operator"] = type(expr).__name__.upper()
        item["left"] = _node_sql(expr.this)
        item["right"] = _node_sql(expr.expression)
    return item


def _context_for_node(node: exp.Expression) -> str:
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.Window):
            return "WINDOW"
        if isinstance(parent, exp.Having):
            return "HAVING"
        if isinstance(parent, exp.Where):
            return "WHERE"
        if isinstance(parent, exp.Join):
            return "JOIN ON"
        if isinstance(parent, exp.Order):
            return "ORDER BY"
        if isinstance(parent, exp.Group):
            return "GROUP BY"
        if isinstance(parent, exp.Case):
            return "CASE"
        parent = parent.parent
    return "SELECT"


def _aggregate_summary(node: exp.AggFunc) -> dict[str, Any]:
    arg_nodes: list[Any] = []
    distinct = bool(node.args.get("distinct"))
    main_arg = node.this
    if isinstance(main_arg, exp.Distinct):
        distinct = True
        arg_nodes.extend(main_arg.expressions or [])
    elif main_arg is not None:
        arg_nodes.append(main_arg)
    arg_nodes.extend(node.expressions or [])
    return {
        "context": _context_for_node(node),
        "function": _function_name(node),
        "distinct": distinct,
        "args": [_node_sql(arg) for arg in arg_nodes],
        "sql": _node_sql(node),
    }


_COMPARISON_OPS: tuple[tuple[type[exp.Expression], str], ...] = (
    (exp.EQ, "="),
    (exp.NEQ, "<>"),
    (exp.LT, "<"),
    (exp.LTE, "<="),
    (exp.GT, ">"),
    (exp.GTE, ">="),
    (exp.NullSafeNEQ, "IS DISTINCT FROM"),
    (exp.NullSafeEQ, "IS NOT DISTINCT FROM"),
)


def _predicate_atom(node: exp.Expression, context: str, negated: bool = False) -> dict[str, Any] | None:
    if isinstance(node, exp.Escape) and isinstance(node.this, exp.Like):
        like_node = node.this
        return {
            "context": context,
            "kind": "like",
            "operator": "NOT LIKE" if negated else "LIKE",
            "left": _node_sql(like_node.this),
            "pattern": _literal_value(like_node.expression),
            "pattern_sql": _node_sql(like_node.expression),
            "escape": _literal_value(node.expression),
            "escape_sql": _node_sql(node.expression),
            "sql": _node_sql(node),
            "negated": negated,
        }
    for cls, op in _COMPARISON_OPS:
        if isinstance(node, cls):
            left = node.this
            right = node.expression
            if isinstance(right, (exp.Any, exp.All)):
                quantifier = "ANY" if isinstance(right, exp.Any) else "ALL"
                query = right.this
                return {
                    "context": context,
                    "kind": "quantified_comparison",
                    "operator": op,
                    "quantifier": quantifier,
                    "left": _node_sql(left),
                    "right": _node_sql(right),
                    "query": _node_sql(query),
                    "sql": _node_sql(node),
                    "negated": negated,
                }
            if isinstance(right, exp.Null) or isinstance(left, exp.Null):
                kind = "null_comparison"
            else:
                kind = "comparison"
            return {
                "context": context,
                "kind": kind,
                "operator": f"NOT {op}" if negated else op,
                "left": _node_sql(left),
                "right": _literal_value(right),
                "right_sql": _node_sql(right),
                "sql": _node_sql(node),
                "negated": negated,
            }
    if isinstance(node, exp.Is):
        right = node.expression
        operator = "IS NOT NULL" if negated and isinstance(right, exp.Null) else "IS NULL" if isinstance(right, exp.Null) else "IS NOT" if negated else "IS"
        return {
            "context": context,
            "kind": "null_check" if isinstance(right, exp.Null) else "is_predicate",
            "operator": operator,
            "left": _node_sql(node.this),
            "right": _literal_value(right),
            "right_sql": _node_sql(right),
            "sql": _node_sql(node),
            "negated": negated,
        }
    if isinstance(node, exp.In):
        query = node.args.get("query")
        values = [_literal_value(expr) for expr in node.expressions or []]
        return {
            "context": context,
            "kind": "in_subquery" if query is not None else "in_list",
            "operator": "NOT IN" if negated else "IN",
            "left": _node_sql(node.this),
            "values": values,
            "query": _node_sql(query) if query is not None else "",
            "sql": _node_sql(node),
            "negated": negated,
        }
    if isinstance(node, exp.Between):
        return {
            "context": context,
            "kind": "between",
            "operator": "NOT BETWEEN" if negated else "BETWEEN",
            "left": _node_sql(node.this),
            "low": _literal_value(node.args.get("low")),
            "high": _literal_value(node.args.get("high")),
            "low_sql": _node_sql(node.args.get("low")),
            "high_sql": _node_sql(node.args.get("high")),
            "sql": _node_sql(node),
            "negated": negated,
        }
    if isinstance(node, exp.Like):
        return {
            "context": context,
            "kind": "like",
            "operator": "NOT LIKE" if negated else "LIKE",
            "left": _node_sql(node.this),
            "pattern": _literal_value(node.expression),
            "pattern_sql": _node_sql(node.expression),
            "sql": _node_sql(node),
            "negated": negated,
        }
    return None


def _predicate_tree(node: exp.Expression | None, context: str, negated: bool = False) -> dict[str, Any] | None:
    if node is None:
        return None
    if isinstance(node, exp.Paren):
        return _predicate_tree(node.this, context, negated=negated)
    if isinstance(node, (exp.And, exp.Or)):
        return {
            "context": context,
            "kind": "logic",
            "operator": "AND" if isinstance(node, exp.And) else "OR",
            "sql": _node_sql(node),
            "children": [
                _predicate_tree(node.this, context),
                _predicate_tree(node.expression, context),
            ],
            "negated": negated,
        }
    if isinstance(node, exp.Not):
        child = _predicate_tree(node.this, context, negated=not negated)
        return {
            "context": context,
            "kind": "logic",
            "operator": "NOT",
            "sql": _node_sql(node),
            "children": [child],
            "negated": negated,
        }
    atom = _predicate_atom(node, context, negated=negated)
    if atom is not None:
        return atom
    return {
        "context": context,
        "kind": "raw",
        "sql": _node_sql(node),
        "negated": negated,
    }


def _flatten_predicates(tree: dict[str, Any] | None) -> list[dict[str, Any]]:
    if tree is None:
        return []
    items = [{key: val for key, val in tree.items() if key != "children"}]
    for child in tree.get("children") or []:
        if isinstance(child, dict):
            items.extend(_flatten_predicates(child))
    return items

@dataclass
class SQLStructureIR:
    """Standardized Intermediate Representation (IR) of a parsed SQL structure."""
    projection: List[str] = field(default_factory=list)          # SELECT projection columns/expressions
    distinct: bool = False                                       # SELECT DISTINCT
    where_predicates: List[str] = field(default_factory=list)    # WHERE predicates
    joins: List[Dict[str, Any]] = field(default_factory=list)    # Join structs: [{type, table, condition, node}]
    group_by: List[str] = field(default_factory=list)            # GROUP BY keys
    having_predicates: List[str] = field(default_factory=list)   # HAVING predicates
    order_by: List[Dict[str, Any]] = field(default_factory=list) # ORDER BY keys: [{column, direction, node}]
    limit_offset: Dict[str, Any] = field(default_factory=dict)   # {limit, offset}
    subqueries: List[Dict[str, Any]] = field(default_factory=list)# [{type, is_correlated, node, sql}]
    ctes: List[Dict[str, Any]] = field(default_factory=list)      # [{name, recursive, node, sql}]
    set_operations: List[str] = field(default_factory=list)      # UNION/INTERSECT/EXCEPT
    case_branches: List[Dict[str, Any]] = field(default_factory=list) # CASE WHEN branches
    window_functions: List[Dict[str, Any]] = field(default_factory=list) # Window configs
    predicate_ir: List[Dict[str, Any]] = field(default_factory=list) # Typed predicate atoms/logical nodes
    logic_trees: List[Dict[str, Any]] = field(default_factory=list)  # Typed predicate trees by context
    aggregate_functions: List[Dict[str, Any]] = field(default_factory=list) # Typed aggregate calls
    expression_ir: List[Dict[str, Any]] = field(default_factory=list) # Typed projection/order/group expressions
    set_operation_details: List[Dict[str, Any]] = field(default_factory=list) # Typed set operation nodes
    window_function_details: List[Dict[str, Any]] = field(default_factory=list) # Typed window configs
    table_references: List[Dict[str, Any]] = field(default_factory=list) # Physical/CTE table references
    from_sources: List[Dict[str, Any]] = field(default_factory=list) # Top-level FROM source summaries
    named_windows: List[Dict[str, Any]] = field(default_factory=list) # WINDOW clause definitions

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe snapshot without leaking sqlglot node objects."""
        return {
            "projection": _serialize_ir_value(self.projection),
            "distinct": self.distinct,
            "where_predicates": _serialize_ir_value(self.where_predicates),
            "joins": _serialize_ir_value(self.joins),
            "group_by": _serialize_ir_value(self.group_by),
            "having_predicates": _serialize_ir_value(self.having_predicates),
            "order_by": _serialize_ir_value(self.order_by),
            "limit_offset": _serialize_ir_value(self.limit_offset),
            "subqueries": _serialize_ir_value(self.subqueries),
            "ctes": _serialize_ir_value(self.ctes),
            "set_operations": _serialize_ir_value(self.set_operations),
            "case_branches": _serialize_ir_value(self.case_branches),
            "window_functions": _serialize_ir_value(self.window_functions),
            "predicate_ir": _serialize_ir_value(self.predicate_ir),
            "logic_trees": _serialize_ir_value(self.logic_trees),
            "aggregate_functions": _serialize_ir_value(self.aggregate_functions),
            "expression_ir": _serialize_ir_value(self.expression_ir),
            "set_operation_details": _serialize_ir_value(self.set_operation_details),
            "window_function_details": _serialize_ir_value(self.window_function_details),
            "table_references": _serialize_ir_value(self.table_references),
            "from_sources": _serialize_ir_value(self.from_sources),
            "named_windows": _serialize_ir_value(self.named_windows),
        }

    @classmethod
    def from_ast(cls, ast: exp.Expression) -> "SQLStructureIR":
        ir = cls()
        if ast is None:
            return ir
        
        # 1. SELECT projection & distinct
        select = ast.find(exp.Select)
        if select:
            ir.projection = [_node_sql(expr) for expr in select.expressions or []]
            # ``exp.Distinct`` is also the argument node for
            # COUNT(DISTINCT ...)/SUM(DISTINCT ...).  The IR field represents
            # SELECT DISTINCT only, so read the top SELECT flag directly.
            ir.distinct = bool(select.args.get("distinct"))
            ir.expression_ir.extend(
                _expression_summary(expr, "SELECT")
                for expr in select.expressions or []
            )
            
        # 2. WHERE predicates
        ir.where_predicates = [w.sql(dialect="sqlite") for w in ast.find_all(exp.Where)]
        
        # 3. JOINS
        for j in ast.find_all(exp.Join):
            side = str(j.args.get("side") or j.args.get("kind") or "INNER").upper()
            table = j.this.name if isinstance(j.this, exp.Table) else ""
            cond = _node_sql(j.args.get("on"))
            ir.joins.append({"type": side, "table": table, "condition": cond, "node": j})

        # 3b. Table and FROM source summaries.  This covers implicit joins
        # like FROM a, b, which do not always appear as exp.Join nodes.
        for table in ast.find_all(exp.Table):
            ir.table_references.append({
                "name": str(table.name or ""),
                "alias": str(table.alias_or_name or table.name or ""),
                "sql": _node_sql(table),
            })
        for from_node in ast.find_all(exp.From):
            source = from_node.this
            if source is not None:
                ir.from_sources.append({
                    "kind": _expr_kind(source),
                    "sql": _node_sql(source),
                    "tables": [str(table.name or "") for table in source.find_all(exp.Table)] if isinstance(source, exp.Expression) else [],
                })
            for source in from_node.expressions or []:
                ir.from_sources.append({
                    "kind": _expr_kind(source),
                    "sql": _node_sql(source),
                    "tables": [str(table.name or "") for table in source.find_all(exp.Table)] if isinstance(source, exp.Expression) else [],
                })
            
        # 4. GROUP BY
        group = ast.find(exp.Group)
        if group:
            ir.group_by = [_node_sql(expr) for expr in group.expressions or []]
            ir.expression_ir.extend(
                _expression_summary(expr, "GROUP BY")
                for expr in group.expressions or []
            )
            
        # 5. HAVING
        ir.having_predicates = [h.sql(dialect="sqlite") for h in ast.find_all(exp.Having)]
        
        # 6. ORDER BY
        order = ast.find(exp.Order)
        if order:
            for expr in order.expressions:
                if isinstance(expr, exp.Ordered):
                    ordered_expr = expr.this
                    col = _node_sql(ordered_expr)
                    direction = "DESC" if expr.args.get("desc") else "ASC"
                    nulls_first = expr.args.get("nulls_first")
                    nulls = "FIRST" if nulls_first is True else "LAST" if nulls_first is False else None
                    collation = _node_sql(ordered_expr.expression) if isinstance(ordered_expr, exp.Collate) else None
                    ir.order_by.append({
                        "column": col,
                        "direction": direction,
                        "nulls": nulls,
                        "collation": collation,
                        "node": expr,
                    })
                    ir.expression_ir.append(_expression_summary(ordered_expr, "ORDER BY"))
                    
        # 7. LIMIT & OFFSET
        limit_node = ast.find(exp.Limit) or ast.find(exp.Fetch)
        offset_node = ast.find(exp.Offset)
        if limit_node:
            limit_expr = getattr(limit_node, "expression", None) or limit_node.args.get("count")
            ir.limit_offset["limit"] = _node_sql(limit_expr)
        if offset_node:
            ir.limit_offset["offset"] = _node_sql(offset_node.expression)
            
        # 8. SET OPERATIONS
        for op_type in (exp.Union, exp.Intersect, exp.Except):
            for node in ast.find_all(op_type):
                op_name = op_type.__name__.upper()
                if op_name not in ir.set_operations:
                    ir.set_operations.append(op_name)
                ir.set_operation_details.append({
                    "operator": op_name,
                    "distinct": node.args.get("distinct") is not False,
                    "all": node.args.get("distinct") is False,
                    "left_sql": _node_sql(node.this),
                    "right_sql": _node_sql(node.expression),
                    "sql": _node_sql(node),
                })
                    
        # 9. SUBQUERIES
        for sub in ast.find_all(exp.Subquery):
            is_correlated = False
            inner_tables = {str(t.name).lower().strip('"`[]') for t in sub.find_all(exp.Table)}
            for t in sub.find_all(exp.Table):
                if t.alias:
                    inner_tables.add(str(t.alias).lower().strip('"`[]'))
            for col in sub.find_all(exp.Column):
                if col.table:
                    table_ref = str(col.table).lower().strip('"`[]')
                    if table_ref not in inner_tables:
                        is_correlated = True
                        break
            ir.subqueries.append({
                "type": "scalar_or_in",
                "is_correlated": is_correlated,
                "node": sub,
                "sql": sub.sql(dialect="sqlite")
            })
        for exists in ast.find_all(exp.Exists):
            is_correlated = False
            inner_tables = {str(t.name).lower().strip('"`[]') for t in exists.find_all(exp.Table)}
            for t in exists.find_all(exp.Table):
                if t.alias:
                    inner_tables.add(str(t.alias).lower().strip('"`[]'))
            for col in exists.find_all(exp.Column):
                if col.table:
                    table_ref = str(col.table).lower().strip('"`[]')
                    if table_ref not in inner_tables:
                        is_correlated = True
                        break
            ir.subqueries.append({
                "type": "exists",
                "is_correlated": is_correlated,
                "node": exists,
                "sql": exists.sql(dialect="sqlite")
            })
        for quantifier_type, quantifier_name in ((exp.Any, "any"), (exp.All, "all")):
            for quantifier in ast.find_all(quantifier_type):
                inner = quantifier.this
                is_correlated = False
                inner_tables = {str(t.name).lower().strip('"`[]') for t in inner.find_all(exp.Table)} if isinstance(inner, exp.Expression) else set()
                for t in inner.find_all(exp.Table) if isinstance(inner, exp.Expression) else []:
                    if t.alias:
                        inner_tables.add(str(t.alias).lower().strip('"`[]'))
                for col in inner.find_all(exp.Column) if isinstance(inner, exp.Expression) else []:
                    if col.table:
                        table_ref = str(col.table).lower().strip('"`[]')
                        if table_ref not in inner_tables:
                            is_correlated = True
                            break
                ir.subqueries.append({
                    "type": quantifier_name,
                    "is_correlated": is_correlated,
                    "node": quantifier,
                    "sql": quantifier.sql(dialect="sqlite"),
                })
            
        # 10. CTEs
        for cte in ast.find_all(exp.CTE):
            with_node = cte.find_ancestor(exp.With)
            is_recursive = bool(with_node.args.get("recursive")) if with_node else False
            ir.ctes.append({
                "name": getattr(cte, "alias_or_name", None) or getattr(cte, "alias", None) or "",
                "recursive": is_recursive,
                "node": cte,
                "sql": _node_sql(cte),
            })
            
        # 11. CASE BRANCHES
        for case in ast.find_all(exp.Case):
            ir.case_branches.append({
                "node": case,
                "sql": _node_sql(case),
            })
            
        # 12. WINDOW FUNCTIONS
        for window in ast.find_all(exp.Window):
            ir.window_functions.append({
                "node": window,
                "sql": _node_sql(window),
            })
            order = window.args.get("order")
            order_by = []
            if order is not None:
                for ordered in order.expressions or []:
                    ordered_expr = ordered.this
                    nulls_first = ordered.args.get("nulls_first")
                    nulls = "FIRST" if nulls_first is True else "LAST" if nulls_first is False else None
                    order_by.append({
                        "column": _node_sql(ordered_expr),
                        "direction": "DESC" if ordered.args.get("desc") else "ASC",
                        "nulls": nulls,
                        "collation": _node_sql(ordered_expr.expression) if isinstance(ordered_expr, exp.Collate) else None,
                        "sql": _node_sql(ordered),
                    })
            window_name = _node_sql(window.args.get("alias") or window.this) if isinstance(window.this, exp.Identifier) or window.args.get("alias") is not None else ""
            is_named_definition = isinstance(window.this, exp.Identifier) and window.args.get("over") is None
            is_named_reference = window.args.get("alias") is not None and not is_named_definition
            detail = {
                "function": _function_name(window.this),
                "window_name": window_name,
                "is_named_definition": is_named_definition,
                "is_named_reference": is_named_reference,
                "partition_by": [_node_sql(expr) for expr in window.args.get("partition_by") or []],
                "order_by": order_by,
                "frame": _node_sql(window.args.get("spec")),
                "sql": _node_sql(window),
            }
            if is_named_definition:
                ir.named_windows.append(detail)
            ir.window_function_details.append({
                **detail,
            })

        # 13. Typed predicate IR by context.
        for where in ast.find_all(exp.Where):
            tree = _predicate_tree(where.this, "WHERE")
            if tree is not None:
                ir.logic_trees.append(tree)
                ir.predicate_ir.extend(_flatten_predicates(tree))
        for having in ast.find_all(exp.Having):
            tree = _predicate_tree(having.this, "HAVING")
            if tree is not None:
                ir.logic_trees.append(tree)
                ir.predicate_ir.extend(_flatten_predicates(tree))
        for join in ast.find_all(exp.Join):
            on_node = join.args.get("on")
            tree = _predicate_tree(on_node, "JOIN ON")
            if tree is not None:
                ir.logic_trees.append(tree)
                ir.predicate_ir.extend(_flatten_predicates(tree))
        for case in ast.find_all(exp.Case):
            for if_node in case.args.get("ifs") or []:
                condition = if_node.this if isinstance(if_node, exp.If) else None
                tree = _predicate_tree(condition, "CASE WHEN")
                if tree is not None:
                    ir.logic_trees.append(tree)
                    ir.predicate_ir.extend(_flatten_predicates(tree))

        # 14. Typed aggregate function summaries.
        ir.aggregate_functions = [_aggregate_summary(node) for node in ast.find_all(exp.AggFunc)]
            
        return ir

    def feature_kps(self) -> list[str]:
        kps = ["select-basic"] if self.projection else []
        if self.where_predicates:
            kps.append("where")
        if self.order_by:
            kps.append("order-by")
        if self.limit_offset:
            kps.append("limit")
        if self.distinct:
            kps.append("distinct")
        if self.group_by:
            kps.append("group-by")
        if self.having_predicates:
            kps.append("having")
        if self.joins:
            kps.append("join-inner")
            for join in self.joins:
                side = str(join.get("type") or "").upper()
                if side == "LEFT":
                    kps.append("join-left")
                elif side == "RIGHT":
                    kps.append("join-right")
                elif side == "FULL":
                    kps.append("join-full")
                if join.get("condition"):
                    kps.append("join-on")
        if self.subqueries:
            if any(item.get("is_correlated") for item in self.subqueries):
                kps.append("subquery-correlated")
            kps.append("subquery-scalar")
        for cte in self.ctes:
            kps.append("cte-recursive" if cte.get("recursive") else "cte")
        if self.set_operations:
            kps.extend([op.lower() for op in self.set_operations])
        if self.case_branches:
            kps.append("case")
        if self.window_functions:
            kps.append("window-row-number")
        if self.aggregate_functions:
            kps.append("aggregate")
            if any(item.get("function") == "COUNT" for item in self.aggregate_functions):
                kps.append("agg-count")
        seen: set[str] = set()
        ordered: list[str] = []
        for kp in kps:
            if kp not in seen:
                seen.add(kp)
                ordered.append(kp)
        return ordered


@dataclass
class ASTDiffNode:
    """Atomic structural syntax difference between standard and student queries."""
    clause_category: str       # e.g., "WHERE", "JOIN", "HAVING", "LIMIT", "SELECT", "WINDOW", "CTE", "UNION"
    diff_type: str             # e.g., "projection_changed", "where_changed", "join_type_changed", etc.
    target_table: Optional[str] = None
    target_column: Optional[str] = None
    standard_node: Optional[Any] = None
    student_node: Optional[Any] = None
    knowledge_point_id: str = "select-basic"
    severity: float = 0.5
    extra: Dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        if key == "clause":
            return self.clause_category
        if key == "diff_type":
            return self.diff_type
        if key == "column":
            return self.target_column
        if key == "table":
            return self.target_table
        if hasattr(self, key):
            return getattr(self, key)
        return self.extra.get(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            val = self[key]
            return val if val is not None else default
        except Exception:
            return default

    def __contains__(self, key: str) -> bool:
        if key in ("clause", "diff_type", "column", "table"):
            return True
        if hasattr(self, key):
            return getattr(self, key) is not None
        return key in self.extra

    def matches_clause(self, *clauses: str) -> bool:
        normalized = {str(item).upper() for item in clauses if item}
        if not normalized:
            return False
        return str(self.clause_category or "").upper() in normalized

    def matches_diff_type(self, *diff_types: str) -> bool:
        normalized = {str(item) for item in diff_types if item}
        if not normalized:
            return False
        return str(self.diff_type or "") in normalized

    def to_dict(self) -> dict[str, Any]:
        # Serialize extra dict: convert any sqlglot expression nodes to SQL text
        safe_extra: dict[str, Any] = {}
        for key, val in (self.extra or {}).items():
            if isinstance(val, exp.Expression):
                safe_extra[key] = _node_sql(val)
            else:
                safe_extra[key] = val
        return {
            "clause": self.clause_category,
            "diff_type": self.diff_type,
            "table": self.target_table,
            "column": self.target_column,
            "knowledge_point_id": self.knowledge_point_id,
            "severity": self.severity,
            "standard_sql": _node_sql(self.standard_node),
            "student_sql": _node_sql(self.student_node),
            "extra": safe_extra,
        }
