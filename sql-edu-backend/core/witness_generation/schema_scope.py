"""Query-scope-aware schema qualification for witness generation.

This module deliberately performs no data generation.  It identifies physical
tables separately from CTE and derived relations and reports missing schema
objects before probes are allowed to run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable

import sqlglot
from sqlglot import exp


def _norm(value: str | None) -> str:
    return str(value or "").strip().strip('`"[]').lower()


@dataclass(frozen=True, order=True)
class ColumnRef:
    relation: str
    column: str
    query_scope: str


@dataclass
class ColumnSchema:
    name: str
    data_type: str = "TEXT"
    nullable: bool = True
    is_generated: bool = False
    # Distinguishes an explicit TEXT declaration from the compatibility
    # default used when a compact schema omits a type.
    has_explicit_type: bool = False


@dataclass
class ForeignKey:
    columns: tuple[str, ...]
    references_table: str
    references_columns: tuple[str, ...]


@dataclass
class TableSchema:
    name: str
    columns: dict[str, ColumnSchema]
    primary_key: tuple[str, ...] = ()
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    unique_constraints: list[tuple[str, ...]] = field(default_factory=list)


@dataclass
class SchemaCatalog:
    """Physical schema plus query-local namespaces.

    ``from_legacy`` keeps the public compact-schema API source compatible while
    making type and constraint information available to new witness tactics.
    Unknown nullability is represented conservatively as nullable; only an
    explicit ``NOT NULL`` or ``PRIMARY KEY`` declaration tightens it.
    """

    physical_tables: dict[str, TableSchema] = field(default_factory=dict)
    ctes: dict[str, QueryScope] = field(default_factory=dict)
    derived_tables: dict[str, QueryScope] = field(default_factory=dict)
    query_blocks: dict[str, QueryScope] = field(default_factory=dict)
    source: str = "legacy_compact_schema"
    database_id: str | None = None

    @classmethod
    def from_legacy(
        cls,
        schema: dict[str, list[str]],
        schema_types: dict[str, dict[str, str]] | None = None,
    ) -> "SchemaCatalog":
        type_map = schema_types or {}
        tables: dict[str, TableSchema] = {}
        for table_name, columns in schema.items():
            table_types = {
                str(name).lower(): str(value)
                for name, value in type_map.get(table_name, {}).items()
            }
            structured: dict[str, ColumnSchema] = {}
            primary: list[str] = []
            foreign_keys: list[ForeignKey] = []
            uniques: list[tuple[str, ...]] = []
            for column in columns:
                type_hint = table_types.get(str(column).lower(), "TEXT")
                upper = type_hint.upper()
                base_type = re.split(
                    r"\b(?:NOT\s+NULL|PRIMARY\s+KEY|REFERENCES|UNIQUE|CHECK|GENERATED|IDENTITY|AUTO_INCREMENT)\b",
                    type_hint,
                    maxsplit=1,
                    flags=re.IGNORECASE,
                )[0].strip()
                is_primary = "PRIMARY KEY" in upper
                structured[str(column).lower()] = ColumnSchema(
                    name=str(column),
                    data_type=base_type or "TEXT",
                    nullable=not ("NOT NULL" in upper or is_primary),
                    is_generated=any(
                        marker in upper
                        for marker in ("GENERATED", "IDENTITY", "AUTO_INCREMENT")
                    ),
                    has_explicit_type=str(column).lower() in table_types,
                )
                if is_primary:
                    primary.append(str(column))
                reference = re.search(
                    r"\bREFERENCES\s+([\w$\[\]`\".]+)\s*\(\s*([\w$\[\]`\"]+)\s*\)",
                    type_hint,
                    flags=re.IGNORECASE,
                )
                if reference:
                    foreign_keys.append(ForeignKey(
                        columns=(str(column),),
                        references_table=reference.group(1).strip("`\"[]"),
                        references_columns=(reference.group(2).strip("`\"[]"),),
                    ))
                if "UNIQUE" in upper:
                    uniques.append((str(column),))
            if primary:
                uniques.append(tuple(primary))
            tables[str(table_name).lower()] = TableSchema(
                name=str(table_name),
                columns=structured,
                primary_key=tuple(primary),
                foreign_keys=foreign_keys,
                unique_constraints=uniques,
            )
        return cls(physical_tables=tables)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SchemaCatalog":
        """Load the normalized JSON catalog attached to Spider records.

        The corpus layer intentionally stores only JSON primitives.  This
        constructor is the explicit boundary that restores typed columns,
        composite keys and foreign keys without guessing them from SQL text.
        Unknown Spider nullability remains conservative (nullable), except
        for primary-key columns whose non-nullability is part of SQL schema
        semantics.
        """

        raw_tables = payload.get("tables")
        if not isinstance(raw_tables, list) or not raw_tables:
            raise ValueError("schema catalog requires a non-empty tables list")

        tables: dict[str, TableSchema] = {}
        for raw_table in raw_tables:
            if not isinstance(raw_table, dict):
                raise ValueError("schema catalog table must be an object")
            table_name = str(raw_table.get("name") or "").strip()
            table_key = _norm(table_name)
            if not table_key or table_key in tables:
                raise ValueError(f"invalid or duplicate schema table: {table_name!r}")

            raw_columns = raw_table.get("columns")
            if not isinstance(raw_columns, list) or not raw_columns:
                raise ValueError(f"schema table {table_name!r} requires columns")
            column_payloads: dict[str, dict[str, Any]] = {}
            actual_names: dict[str, str] = {}
            for raw_column in raw_columns:
                if not isinstance(raw_column, dict):
                    raise ValueError(f"schema column in {table_name!r} must be an object")
                column_name = str(raw_column.get("name") or "").strip()
                column_key = _norm(column_name)
                if not column_key or column_key in column_payloads:
                    raise ValueError(
                        f"invalid or duplicate schema column {table_name!r}.{column_name!r}"
                    )
                column_payloads[column_key] = raw_column
                actual_names[column_key] = column_name

            primary_names = raw_table.get("primary_key") or [
                item.get("name")
                for item in raw_columns
                if isinstance(item, dict) and item.get("is_primary_key")
            ]
            primary: list[str] = []
            for name in primary_names:
                key = _norm(str(name))
                if key not in actual_names:
                    raise ValueError(f"unknown primary-key column {table_name!r}.{name!r}")
                if actual_names[key] not in primary:
                    primary.append(actual_names[key])
            primary_keys = {_norm(name) for name in primary}

            structured: dict[str, ColumnSchema] = {}
            for column_key, raw_column in column_payloads.items():
                nullable = raw_column.get("nullable")
                structured[column_key] = ColumnSchema(
                    name=actual_names[column_key],
                    data_type=str(raw_column.get("data_type") or "TEXT").strip() or "TEXT",
                    nullable=(
                        False
                        if column_key in primary_keys
                        else True if nullable is None else bool(nullable)
                    ),
                    is_generated=bool(raw_column.get("is_generated", False)),
                    has_explicit_type="data_type" in raw_column
                    and str(raw_column.get("data_type") or "").strip() != "",
                )

            foreign_keys: list[ForeignKey] = []
            for raw_key in raw_table.get("foreign_keys") or ():
                if not isinstance(raw_key, dict):
                    raise ValueError(f"foreign key in {table_name!r} must be an object")
                source_names = raw_key.get("columns") or [raw_key.get("column")]
                target_names = raw_key.get("references_columns") or [
                    raw_key.get("references_column")
                ]
                if not isinstance(source_names, (list, tuple)) or not isinstance(
                    target_names, (list, tuple)
                ):
                    raise ValueError(f"invalid foreign key in {table_name!r}")
                resolved_sources = tuple(
                    actual_names.get(_norm(str(name)), "") for name in source_names
                )
                resolved_targets = tuple(str(name or "").strip() for name in target_names)
                target_table = str(raw_key.get("references_table") or "").strip()
                if (
                    not target_table
                    or not resolved_sources
                    or len(resolved_sources) != len(resolved_targets)
                    or any(not name for name in (*resolved_sources, *resolved_targets))
                ):
                    raise ValueError(f"invalid foreign key in {table_name!r}")
                foreign_keys.append(ForeignKey(
                    columns=resolved_sources,
                    references_table=target_table,
                    references_columns=resolved_targets,
                ))

            unique_constraints: list[tuple[str, ...]] = []
            for raw_unique in raw_table.get("unique_constraints") or ():
                if not isinstance(raw_unique, (list, tuple)) or not raw_unique:
                    raise ValueError(f"invalid unique constraint in {table_name!r}")
                resolved = tuple(
                    actual_names.get(_norm(str(name)), "") for name in raw_unique
                )
                if any(not name for name in resolved):
                    raise ValueError(f"unknown unique column in {table_name!r}")
                if resolved not in unique_constraints:
                    unique_constraints.append(resolved)
            if primary and tuple(primary) not in unique_constraints:
                unique_constraints.append(tuple(primary))

            tables[table_key] = TableSchema(
                name=table_name,
                columns=structured,
                primary_key=tuple(primary),
                foreign_keys=foreign_keys,
                unique_constraints=unique_constraints,
            )

        return cls(
            physical_tables=tables,
            source=str(payload.get("source") or "serialized_schema_catalog"),
            database_id=(str(payload.get("db_id")).strip() or None)
            if payload.get("db_id") is not None
            else None,
        )

    def as_legacy(self) -> dict[str, list[str]]:
        return {
            table.name: [column.name for column in table.columns.values()]
            for table in self.physical_tables.values()
        }

    def as_legacy_types(self) -> dict[str, dict[str, str]]:
        """Render safe per-column hints for existing fixture backends.

        Composite constraints stay in ``TableSchema`` because representing a
        composite key as several column-level ``PRIMARY KEY`` declarations is
        invalid SQL.  The compatibility map includes only constraints that are
        genuinely unary.
        """

        result: dict[str, dict[str, str]] = {}
        for table in self.physical_tables.values():
            primary = {_norm(name) for name in table.primary_key}
            unary_primary = len(table.primary_key) == 1
            unary_unique = {
                _norm(constraint[0])
                for constraint in table.unique_constraints
                if len(constraint) == 1
            }
            unary_foreign = {
                _norm(key.columns[0]): key
                for key in table.foreign_keys
                if len(key.columns) == len(key.references_columns) == 1
            }
            table_types: dict[str, str] = {}
            for column in table.columns.values():
                key = _norm(column.name)
                parts = [column.data_type or "TEXT"]
                if unary_primary and key in primary:
                    parts.append("PRIMARY KEY")
                elif not column.nullable or key in primary:
                    parts.append("NOT NULL")
                if key in unary_unique and not (unary_primary and key in primary):
                    parts.append("UNIQUE")
                foreign = unary_foreign.get(key)
                if foreign is not None:
                    parts.append(
                        f"REFERENCES {foreign.references_table}({foreign.references_columns[0]})"
                    )
                table_types[column.name] = " ".join(parts)
            result[table.name] = table_types
        return result

    def table(self, name: str) -> TableSchema | None:
        return self.physical_tables.get(_norm(name))


@dataclass
class QueryScope:
    id: str
    parent_id: str | None
    physical_tables: dict[str, str] = field(default_factory=dict)
    ctes: set[str] = field(default_factory=set)
    derived_relations: set[str] = field(default_factory=set)
    referenced_columns: set[ColumnRef] = field(default_factory=set)


@dataclass
class SchemaQualification:
    scopes: list[QueryScope]
    physical_tables: set[str]
    missing_tables: set[str]
    missing_columns: set[ColumnRef]
    executable: bool
    boundary_reason: str | None = None
    catalog: SchemaCatalog | None = None


def _parse_single_query(sql: str, dialect: str | None) -> tuple[exp.Expression | None, str | None]:
    parse_dialect = None if not dialect or str(dialect).startswith("__") else dialect
    try:
        statements = sqlglot.parse(sql, read=parse_dialect)
    except Exception as exc:  # noqa: BLE001 - qualification reports parse boundaries.
        return None, f"sql_parse_failed: {exc}"
    statements = [
        statement
        for statement in statements
        if statement is not None
        and not (
            isinstance(statement, exp.Semicolon)
            and statement.this is None
        )
    ]
    if len(statements) != 1:
        return None, "multiple_sql_statements"
    root = statements[0]
    if not isinstance(root, exp.Query):
        return None, "non_query_statement"
    if any(root.find(node_type) is not None for node_type in (exp.Insert, exp.Update, exp.Delete, exp.Merge)):
        return None, "non_read_only_statement"
    return root, None


def _nearest_select(node: exp.Expression) -> exp.Select | None:
    parent: exp.Expression | None = node.parent
    while parent is not None:
        if isinstance(parent, exp.Select):
            return parent
        parent = parent.parent
    return None


def _scope_parent(select: exp.Select, scope_ids: dict[int, str]) -> str | None:
    parent = select.parent
    while parent is not None:
        if isinstance(parent, exp.Select):
            return scope_ids.get(id(parent))
        parent = parent.parent
    return None


def _visible_cte_names(root: exp.Expression) -> set[str]:
    # The first implementation is intentionally conservative: CTE aliases are
    # never physical tables within the statement that defines them.  The scope
    # objects preserve where references occur so this can later be tightened to
    # sibling/recursive visibility without changing callers.
    return {_norm(cte.alias_or_name) for cte in root.find_all(exp.CTE) if _norm(cte.alias_or_name)}


def _direct_subquery_aliases(select: exp.Select) -> set[str]:
    aliases: set[str] = set()
    for subquery in select.find_all(exp.Subquery):
        if _nearest_select(subquery) is select:
            alias = _norm(subquery.alias_or_name)
            if alias:
                aliases.add(alias)
    return aliases


def _schema_index(
    schema: dict[str, list[str]] | SchemaCatalog,
) -> tuple[dict[str, str], dict[str, set[str]]]:
    legacy = schema.as_legacy() if isinstance(schema, SchemaCatalog) else schema
    names = {_norm(table): table for table in legacy}
    columns = {
        _norm(table): {_norm(column) for column in table_columns}
        for table, table_columns in legacy.items()
    }
    return names, columns


def _sqlite_double_quoted_literal_fallback(
    column: exp.Column,
    *,
    dialect: str | None,
    candidates: list[str],
) -> bool:
    """Recognize SQLite's legacy DQS fallback without weakening other dialects.

    SQLite resolves ``"name"`` as an identifier when such a column is
    visible, but accepts it as a string literal when identifier resolution
    fails. Spider contains this historical SQLite spelling for values such as
    ``"Orange"``. Qualification must mirror that runtime rule; otherwise the
    safety pass invents a missing physical column before SQLite can execute
    the query. PostgreSQL and other identifier-only dialects remain strict.
    """

    normalized_dialect = str(dialect or "").strip().lower()
    identifier = column.this
    return bool(
        normalized_dialect == "sqlite"
        and not column.table
        and not candidates
        and isinstance(identifier, exp.Identifier)
        and identifier.args.get("quoted")
    )


def analyze_schema_qualification(
    sql: str,
    schema: dict[str, list[str]] | SchemaCatalog,
    *,
    dialect: str | None = None,
) -> SchemaQualification:
    root, boundary = _parse_single_query(sql, dialect)
    if root is None:
        return SchemaQualification([], set(), set(), set(), False, boundary)

    catalog = schema if isinstance(schema, SchemaCatalog) else SchemaCatalog.from_legacy(schema)

    selects = list(root.find_all(exp.Select))
    if isinstance(root, exp.Select) and root not in selects:
        selects.insert(0, root)
    selects = sorted(
        {id(select): select for select in selects}.values(),
        key=lambda node: sum(1 for _ in _ancestors(node)),
    )
    scope_ids = {id(select): f"scope_{index}" for index, select in enumerate(selects)}
    cte_names = _visible_cte_names(root)
    schema_names, schema_columns = _schema_index(catalog)
    scopes: list[QueryScope] = []
    all_physical: set[str] = set()
    missing_tables: set[str] = set()
    missing_columns: set[ColumnRef] = set()

    for select in selects:
        scope_id = scope_ids[id(select)]
        scope = QueryScope(
            id=scope_id,
            parent_id=_scope_parent(select, scope_ids),
            ctes=set(cte_names),
            derived_relations=_direct_subquery_aliases(select),
        )
        relation_aliases: dict[str, str] = {}
        # Names introduced by the projection are query-local symbols. They
        # are valid in ORDER BY/HAVING/QUALIFY in several supported dialects
        # and must not be mistaken for physical columns during qualification.
        projected_names = {
            _norm(expression.alias)
            for expression in select.expressions
            if isinstance(expression, exp.Alias) and _norm(expression.alias)
        }
        for table in select.find_all(exp.Table):
            if _nearest_select(table) is not select:
                continue
            name = _norm(table.name)
            alias = _norm(table.alias_or_name or table.name)
            if not name or name in cte_names:
                if alias:
                    scope.derived_relations.add(alias)
                continue
            canonical = schema_names.get(name, name)
            scope.physical_tables[alias or name] = canonical
            relation_aliases[alias or name] = name
            relation_aliases[name] = name
            all_physical.add(name)
            if name not in schema_names:
                missing_tables.add(name)

        visible_physical_tables = dict(scope.physical_tables)
        parent_id = scope.parent_id
        while parent_id:
            parent_scope = next(
                (item for item in scopes if item.id == parent_id),
                None,
            )
            if parent_scope is None:
                break
            for alias, canonical in parent_scope.physical_tables.items():
                visible_physical_tables.setdefault(alias, canonical)
            parent_id = parent_scope.parent_id

        for column in select.find_all(exp.Column):
            if _nearest_select(column) is not select or isinstance(column.this, exp.Star):
                continue
            qualifier = _norm(column.table)
            column_name = _norm(column.name)
            relation = relation_aliases.get(qualifier, qualifier)
            candidates: list[str] = []
            if not qualifier:
                candidates = [
                    canonical
                    for canonical in scope.physical_tables.values()
                    if column_name in schema_columns.get(_norm(canonical), set())
                ]
                candidates = list(dict.fromkeys(candidates))
                if not candidates:
                    candidates = [
                        canonical
                        for canonical in visible_physical_tables.values()
                        if column_name in schema_columns.get(_norm(canonical), set())
                    ]
                candidates = list(dict.fromkeys(candidates))
                if len(candidates) == 1:
                    relation = _norm(candidates[0])
                if _sqlite_double_quoted_literal_fallback(
                    column,
                    dialect=dialect,
                    candidates=candidates,
                ):
                    # This AST node is a value under SQLite's DQS fallback,
                    # not a physical column reference owned by the scope.
                    continue
            reference = ColumnRef(relation=relation, column=column_name, query_scope=scope_id)
            scope.referenced_columns.add(reference)
            if not qualifier and column_name in projected_names:
                continue
            if qualifier:
                # A qualified reference must resolve to a relation introduced
                # by this query block.  Previously an unknown qualifier was
                # left in ``relation`` and escaped the ``schema_columns``
                # check, allowing ``bad_alias.id`` to reach the generator and
                # fail later as an opaque SQLite error.
                known_relations = {
                    _norm(name) for name in visible_physical_tables
                } | scope.ctes | scope.derived_relations
                if qualifier not in known_relations:
                    missing_columns.add(reference)
                elif relation in schema_columns and column_name not in schema_columns[relation]:
                    missing_columns.add(reference)
                # CTE and derived-relation output columns are query-local.
                # Their full projection schema is resolved by the query
                # engine, so qualification intentionally defers that check.
            else:
                if len(candidates) != 1:
                    # Zero candidates means an unknown column; more than one
                    # means an ambiguous unqualified column in a multi-table
                    # block.  Both are schema errors for fixture generation.
                    # If the block exposes a CTE/derived relation, defer the
                    # result-column check because that relation is not present
                    # in the physical catalog.
                    if not scope.ctes and not scope.derived_relations:
                        missing_columns.add(reference)
        scopes.append(scope)

    reason = None
    if missing_tables:
        reason = "missing_physical_tables"
    elif missing_columns:
        reason = "missing_physical_columns"
    return SchemaQualification(
        scopes=scopes,
        physical_tables=all_physical,
        missing_tables=missing_tables,
        missing_columns=missing_columns,
        executable=not missing_tables and not missing_columns,
        boundary_reason=reason,
        catalog=catalog,
    )


def _ancestors(node: exp.Expression) -> Iterable[exp.Expression]:
    parent = node.parent
    while parent is not None:
        yield parent
        parent = parent.parent


def extract_physical_table_names(sql: str, *, dialect: str | None = None) -> set[str]:
    qualification = analyze_schema_qualification(sql, {}, dialect=dialect)
    return qualification.physical_tables
