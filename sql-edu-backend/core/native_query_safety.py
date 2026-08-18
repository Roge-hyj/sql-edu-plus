"""AST safety policy for SQL sent to native judge engines.

Native runners execute inside an isolated namespace, but database accounts can
still expose catalogs, files, extensions, or administrative functions.  This
module is the mandatory allow-list boundary: exactly one read-only query may
refer only to unqualified fixture tables.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

import sqlglot
from sqlglot import ErrorLevel, exp
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.tokens import TokenType

from core.sql_dialect_resolver import (
    UnsupportedSQLDialectError,
    normalize_sql_dialect,
)


class NativeQuerySafetyError(ValueError):
    """A native query violates the sandbox policy.

    ``code`` is stable for API/error handling.  The detail is intentionally
    concise and must not include connection or host information.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


NATIVE_SQL_PARSE_ERROR: Final = "NATIVE_SQL_PARSE_ERROR"
NATIVE_SQL_UNSAFE_STATEMENT: Final = "NATIVE_SQL_UNSAFE_STATEMENT"
NATIVE_SQL_UNSAFE_SIDE_EFFECT: Final = "NATIVE_SQL_UNSAFE_SIDE_EFFECT"
NATIVE_SQL_UNSAFE_FUNCTION: Final = "NATIVE_SQL_UNSAFE_FUNCTION"
NATIVE_SQL_UNSAFE_OBJECT: Final = "NATIVE_SQL_UNSAFE_OBJECT"
NATIVE_SQL_UNSAFE_EXTERNAL_SOURCE: Final = "NATIVE_SQL_UNSAFE_EXTERNAL_SOURCE"


# These nodes can appear below an exp.Query, notably data-changing CTEs and
# SELECT INTO.  Names are resolved dynamically so the policy remains usable
# across the supported SQLGlot 29.x minor releases.
_SIDE_EFFECT_NODE_NAMES: Final = (
    "DML",
    "DDL",
    "Alter",
    "Analyze",
    "Attach",
    "Cache",
    "Command",
    "Commit",
    "Copy",
    "Detach",
    "Drop",
    "Execute",
    "Grant",
    "Hint",
    "Into",
    "Kill",
    "LoadData",
    "Lock",
    "NextValueFor",
    "Pragma",
    "PropertyEQ",  # MySQL SELECT @variable := value
    "QueryOption",
    "Revoke",
    "Rollback",
    "Set",
    "Transaction",
    "TruncateTable",
    "Uncache",
    "Use",
    "WithTableHint",
)
_SIDE_EFFECT_NODE_TYPES: Final = tuple(
    node_type
    for name in _SIDE_EFFECT_NODE_NAMES
    if isinstance((node_type := getattr(exp, name, None)), type)
)
_SIDE_EFFECT_FUNCTIONS: Final = frozenset({"nextval", "setval"})


_DANGEROUS_FUNCTIONS: Final = frozenset(
    {
        # PostgreSQL server files, large-object import/export, remote SQL, and
        # functions that alter server/session/backend state.
        "dblink",
        "dblink_connect",
        "dblink_connect_u",
        "dblink_disconnect",
        "dblink_exec",
        "lo_export",
        "lo_import",
        "lo_create",
        "lo_from_bytea",
        "lo_open",
        "lo_put",
        "lo_unlink",
        "lo_write",
        "lowrite",
        "pg_advisory_lock",
        "pg_advisory_lock_shared",
        "pg_advisory_unlock",
        "pg_advisory_unlock_all",
        "pg_advisory_unlock_shared",
        "pg_backup_start",
        "pg_backup_stop",
        "pg_cancel_backend",
        "pg_create_restore_point",
        "pg_file_rename",
        "pg_file_unlink",
        "pg_log_backend_memory_contexts",
        "pg_logical_emit_message",
        "pg_ls_archive_statusdir",
        "pg_ls_dir",
        "pg_ls_logdir",
        "pg_ls_tmpdir",
        "pg_ls_waldir",
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "pg_read_binary_file",
        "pg_read_file",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_notify",
        "pg_stat_file",
        "pg_switch_wal",
        "pg_terminate_backend",
        "pg_try_advisory_lock",
        "pg_try_advisory_lock_shared",
        "current_setting",
        "cursor_to_xml",
        "cursor_to_xmlschema",
        "database_to_xml",
        "database_to_xml_and_xmlschema",
        "database_to_xmlschema",
        "query_to_xml",
        "query_to_xml_and_xmlschema",
        "query_to_xmlschema",
        "schema_to_xml",
        "schema_to_xml_and_xmlschema",
        "schema_to_xmlschema",
        "set_config",
        "setval",
        "table_to_xml",
        "table_to_xml_and_xmlschema",
        "table_to_xmlschema",
        # MySQL server-file and synchronization functions.
        "get_lock",
        "load_file",
        "master_pos_wait",
        "release_all_locks",
        "release_lock",
        "service_get_read_locks",
        "service_release_locks",
        "sleep",
        "benchmark",
        "sys_eval",
        "sys_exec",
        # SQL Server remote/file rowsets and command execution surfaces.
        "bulkrowset",
        "fn_get_audit_file",
        "fn_trace_gettable",
        "opendatasource",
        "openquery",
        "openrowset",
        "xp_cmdshell",
        # Oracle directory/file handles and environment disclosure.
        "bfilename",
        "sys_context",
        # Cross-engine external scanners commonly parsed by SQLGlot.
        "csv_scan",
        "delta_scan",
        "glob",
        "http_get",
        "httpfs",
        "iceberg_scan",
        "parquet_scan",
        "program",
        "read_blob",
        "read_csv",
        "read_csv_auto",
        "read_file",
        "read_json",
        "read_json_auto",
        "read_ndjson",
        "read_parquet",
        "read_text",
    }
)
_DANGEROUS_FUNCTION_PREFIXES: Final = (
    "dbms_",
    "dblink_",
    "pg_advisory_",
    "pg_ls_",
    "pg_read_",
    "pg_sleep",
    "utl_",
    "xp_",
)

# XML support in the native engines can expose XPath/XQuery evaluation,
# external entities, URI resolvers, or table-valued row sources.  SQLGlot has
# dedicated nodes for a few XML constructs and leaves the rest as Anonymous
# functions, so keep both forms on an explicit deny-list.  This is deliberately
# broader than the server-file functions above: native teaching queries have
# no need for XML execution at all.
_UNSAFE_XML_FUNCTION_NAMES: Final = frozenset(
    {
        "appendchildxml",
        "deletexml",
        "extractvalue",
        "insertchildxml",
        "updatexml",
        "xmlagg",
        "xmlattributes",
        "xmlcast",
        "xmlcolattval",
        "xmlcomment",
        "xmlconcat",
        "xmlelement",
        "xmlforest",
        "xmlexists",
        "xmlisvalid",
        "xmlnamespaces",
        "xmlparse",
        "xmlpi",
        "xmlquery",
        "xmlroot",
        "xmlserialize",
        "xmltext",
        "xmltransform",
        "xmltype",
        "xmltable",
        "xmlvalidate",
        "dbms_xmlgen",
        "sys_xmlgen",
        "sys_xmlagg",
    }
)
_UNSAFE_XML_NODE_NAMES: Final = frozenset(
    {
        "CheckXml",
        "XMLGet",
        "XMLKeyValueOption",
        "XMLNamespace",
        "XMLTable",
        "XMLElement",
    }
)

# SQLGlot has dedicated nodes for functions it understands. Only the
# side-effect-free subset needed by SQL teaching exercises is executable.
# Everything else, especially Anonymous (vendor/user-defined) functions, is
# denied unless explicitly listed below.
_ALLOWED_FUNCTION_NODE_NAMES: Final = frozenset(
    {
        "Abs", "Acos", "Acosh", "AddMonths", "And", "AnyValue", "Array",
        "ArrayAgg", "ArrayAll", "ArrayAny", "ArrayAppend", "ArrayCompact",
        "ArrayConcat", "ArrayContains", "ArrayContainsAll", "ArrayDistinct",
        "ArrayExcept", "ArrayFilter", "ArrayFirst", "ArrayIntersect",
        "ArrayLast", "ArrayMax", "ArrayMin", "ArrayOverlaps", "ArrayPrepend",
        "ArrayRemove", "ArrayReverse", "ArraySize", "ArraySlice", "ArraySort",
        "ArraySum", "ArrayToString", "ArrayUnionAgg", "ArraysZip", "Ascii",
        "Asin", "Asinh", "Atan", "Atan2", "Atanh", "Avg", "Base64DecodeBinary",
        "Base64DecodeString", "Base64Encode", "BitLength", "BitwiseAndAgg",
        "BitwiseCount", "BitwiseOrAgg", "BitwiseXorAgg", "Booland", "Boolnot",
        "Boolor", "BoolxorAgg", "ByteLength", "Case", "Cast", "Cbrt", "Ceil",
        "CheckJson", "Chr", "Coalesce", "Collate", "Concat",
        "ConcatWs", "Contains", "Convert", "ConvertTimezone", "Corr", "Cos",
        "Cosh", "Cot", "Count", "CountIf", "CovarPop", "CovarSamp", "CumeDist",
        "CurrentDate", "CurrentDatetime", "CurrentTime", "CurrentTimestamp",
        "Date", "DateAdd", "DateBin", "DateDiff", "DateFromParts", "DateStrToDate",
        "DateSub", "DateTrunc", "Day", "DayOfMonth", "DayOfWeek", "DayOfWeekIso",
        "DayOfYear", "Dayname", "Decode", "DecodeCase", "Degrees", "DenseRank",
        "DiToDate", "EndsWith", "EqualNull", "Exists", "Exp", "Extract",
        "Factorial", "First", "FirstValue", "Flatten", "Floor", "Format",
        "FromBase", "FromBase32", "FromBase64", "GenerateDateArray",
        "GenerateSeries", "GenerateTimestampArray", "GetExtract", "Getbit",
        "Greatest", "GroupConcat", "Grouping", "GroupingId", "Hex",
        "HexDecodeString", "HexEncode", "Hour", "If", "Initcap", "Int64",
        "IsArray", "IsAscii", "IsInf", "IsNan", "IsNullValue", "JSONArray",
        "JSONArrayAgg", "JSONArrayAppend", "JSONArrayContains", "JSONArrayInsert",
        "JSONBContains", "JSONBContainsAllTopKeys", "JSONBContainsAnyTopKeys",
        "JSONBDeleteAtPath", "JSONBExists", "JSONBExtract", "JSONBExtractScalar",
        "JSONBool", "JSONCast", "JSONExists", "JSONExtract", "JSONExtractArray",
        "JSONExtractScalar", "JSONFormat", "JSONKeys", "JSONKeysAtDepth",
        "JSONObject", "JSONObjectAgg", "JSONRemove", "JSONSet", "JSONStripNulls",
        "JSONTable", "JSONType", "JSONValueArray", "Lag", "Last", "LastDay",
        "LastValue", "Lead", "Least", "Left", "Length", "Levenshtein", "List",
        "Ln", "Log", "LogicalAnd", "LogicalOr", "Lower", "MD5", "MD5Digest",
        "Map", "MapCat", "MapContainsKey", "MapDelete", "MapFromEntries",
        "MapInsert", "MapKeys", "MapPick", "MapSize", "Max", "Median", "Min",
        "Minute", "Mode", "Month", "Monthname", "MonthsBetween", "NextDay",
        "NthValue", "Ntile", "Nullif", "Nvl2", "ObjectAgg", "ObjectInsert", "Or",
        "Overlay", "Pad", "ParseDatetime", "ParseJSON", "ParseNumeric", "ParseTime",
        "PercentRank", "PercentileCont", "PercentileDisc", "Pi", "Pow", "Quarter",
        "Radians", "Rand", "RangeBucket", "Rank", "Reduce", "RegexpCount",
        "RegexpExtract", "RegexpExtractAll", "RegexpFullMatch", "RegexpILike",
        "RegexpInstr", "RegexpLike", "RegexpReplace", "RegexpSplit", "Repeat",
        "Replace", "Reverse", "Right", "Round", "RowNumber", "RtrimmedLength",
        "SHA", "SHA1Digest", "SHA2", "SHA2Digest", "Second", "Sign", "Sin", "Sinh",
        "SortArray", "Soundex", "Space", "Split", "SplitPart", "Sqrt", "StartsWith",
        "Stddev", "StddevPop", "StddevSamp", "StrPosition", "StrToDate", "StrToMap",
        "StrToTime", "String", "StringToArray", "Struct", "StructExtract", "Stuff",
        "Substring", "SubstringIndex", "Sum", "Systimestamp", "Tan", "Tanh", "Time",
        "TimeAdd", "TimeDiff", "TimeFromParts", "TimeStrToDate", "TimeStrToTime",
        "TimeSub", "TimeToStr", "TimeTrunc", "Timestamp", "TimestampAdd",
        "TimestampDiff", "TimestampFromParts", "TimestampSub", "TimestampTrunc",
        "ToArray", "ToBase32", "ToBase64", "ToBinary", "ToBoolean", "ToChar",
        "ToCodePoints", "ToDouble", "ToMap", "ToNumber", "Transform", "Translate",
        "Trim", "Trunc", "Try", "TryCast", "TsOrDsAdd", "TsOrDsDiff",
        "TsOrDsToDate", "TsOrDsToDateStr", "TsOrDsToDatetime", "TsOrDsToTime",
        "TsOrDsToTimestamp", "Typeof", "Unhex", "Unicode", "UnixDate", "UnixMicros",
        "UnixMillis", "UnixSeconds", "UnixToStr", "UnixToTime", "Unnest", "Upper",
        "UtcDate", "UtcTime", "UtcTimestamp", "Uuid", "Variance", "VariancePop",
        "Week", "WeekOfYear", "WidthBucket", "Xor", "Year",
        "YearOfWeek", "YearOfWeekIso",
    }
)
_ALLOWED_ANONYMOUS_FUNCTIONS: Final = {
    "mysql": frozenset({"field", "find_in_set", "now", "weekday"}),
    "postgres": frozenset(
        {"json_to_recordset", "jsonb_array_elements", "jsonb_array_elements_text", "row"}
    ),
    "tsql": frozenset(),
    "oracle": frozenset({"listagg", "trunc"}),
    "sqlite": frozenset(),
}

_SYSTEM_TABLE_EXACT: Final = frozenset(
    {
        "pg_tables",
        "pg_views",
        "pg_indexes",
        "pg_class",
        "pg_database",
        "pg_namespace",
        "pg_roles",
        "pg_user",
        "pg_shadow",
        "cat",
        "dict",
        "dictionary",
        "tab",
        "all_objects",
        "all_tables",
        "all_users",
        "all_views",
        "user_catalog",
        "user_objects",
        "user_tables",
        "user_users",
        "user_views",
        "syscolumns",
        "sysdatabases",
        "sysobjects",
        "systypes",
        "sysusers",
    }
)
_SYSTEM_TABLE_PREFIXES: Final = (
    "all_",
    "dba_",  # Oracle privileged data dictionary views
    "gv$",
    "gv_$",
    "pg_stat_",
    "pg_statio_",
    "pg_",
    "user_",
    "v$",
    "v_$",
)


def validate_native_query_safety(
    query: str | exp.Expression,
    dialect: str | None = None,
    allowed_tables: Iterable[str] | None = None,
) -> exp.Query:
    """Validate and return one side-effect-free native DQL AST.

    String input is parsed with SQLGlot using ``dialect``.  Pre-parsed ASTs are
    accepted so dialect resolution can parse once and validate immediately
    before serialization/execution.  When ``allowed_tables`` is supplied,
    every physical table must be one of those fixture names; CTE and derived
    table references remain scoped logical sources rather than physical tables.
    """

    if dialect is not None:
        try:
            normalize_sql_dialect(dialect)
        except UnsupportedSQLDialectError as exc:
            _fail(NATIVE_SQL_PARSE_ERROR, str(exc))
    normalized_allowed_tables = _normalize_allowed_tables(allowed_tables)
    ast = _coerce_single_query(query, dialect)
    _reject_unsafe_xml_nodes(ast)
    _reject_side_effect_nodes(ast)
    _reject_dangerous_functions(ast, dialect)
    _enforce_function_allowlist(ast, dialect)
    _reject_unsafe_tables(ast, normalized_allowed_tables)
    _reject_system_parameters(ast)
    return ast


def _coerce_single_query(
    query: str | exp.Expression,
    dialect: str | None,
) -> exp.Query:
    if isinstance(query, exp.Expression):
        if not isinstance(query, exp.Query):
            _fail(
                NATIVE_SQL_UNSAFE_STATEMENT,
                f"only DQL queries are allowed, got {type(query).__name__}",
            )
        return query

    if not isinstance(query, str) or not query.strip():
        _fail(NATIVE_SQL_UNSAFE_STATEMENT, "exactly one DQL query is required")

    try:
        normalized = normalize_sql_dialect(dialect) if dialect is not None else None
        _reject_external_token_sequences(query, normalized)
        statements = sqlglot.parse(
            query,
            read=normalized,
            error_level=ErrorLevel.RAISE,
        )
    except NativeQuerySafetyError:
        raise
    except UnsupportedSQLDialectError as exc:
        _fail(NATIVE_SQL_PARSE_ERROR, str(exc))
    except Exception as exc:
        _fail(NATIVE_SQL_PARSE_ERROR, f"SQL cannot be parsed: {exc}")

    parsed = [
        statement
        for statement in statements
        if statement is not None and not isinstance(statement, exp.Semicolon)
    ]
    if len(parsed) != 1:
        _fail(
            NATIVE_SQL_UNSAFE_STATEMENT,
            f"exactly one DQL query is required; parsed {len(parsed)} statements",
        )
    ast = parsed[0]
    if not isinstance(ast, exp.Query):
        _fail(
            NATIVE_SQL_UNSAFE_STATEMENT,
            f"only DQL queries are allowed, got {type(ast).__name__}",
        )
    return ast


def _normalize_allowed_tables(
    allowed_tables: Iterable[str] | None,
) -> frozenset[str] | None:
    """Normalize an optional physical-table allow-list.

    The public helper is intentionally permissive about the iterable shape so
    callers can pass a set, list, tuple, or a single table name.  Empty names
    are ignored; SQL identifiers are normalized with the same case-folding as
    the object policy.
    """

    if allowed_tables is None:
        return None
    values = (allowed_tables,) if isinstance(allowed_tables, str) else allowed_tables
    try:
        return frozenset(
            normalized
            for value in values
            if (normalized := _normalize_name(value))
        )
    except TypeError as exc:
        _fail(NATIVE_SQL_UNSAFE_OBJECT, "allowed_tables must be an iterable of names")
        raise AssertionError from exc


def _reject_external_token_sequences(sql: str, dialect: str | None) -> None:
    """Catch vendor file-output syntax SQLGlot cannot currently build an AST for.

    Token inspection preserves comment/string boundaries and is used only for
    syntax that otherwise fails before an AST exists; the remaining policy is
    entirely node based.
    """

    if dialect != "mysql":
        return
    if _contains_mysql_executable_comment(sql):
        _fail(
            NATIVE_SQL_UNSAFE_SIDE_EFFECT,
            "MySQL executable comments are not allowed in the native sandbox",
        )
    try:
        tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(sql)
    except Exception as exc:
        _fail(NATIVE_SQL_PARSE_ERROR, f"SQL cannot be tokenized: {exc}")
    for index, token in enumerate(tokens[:-1]):
        if token.token_type != TokenType.INTO:
            continue
        successor = tokens[index + 1].text.casefold()
        if successor in {"outfile", "dumpfile"}:
            _fail(
                NATIVE_SQL_UNSAFE_EXTERNAL_SOURCE,
                f"file output is not allowed in the native sandbox: {successor}",
            )


def _contains_mysql_executable_comment(sql: str) -> bool:
    """Return whether MySQL will execute a ``/*! ... */`` comment body.

    SQLGlot deliberately treats executable comments as ordinary comments and
    therefore drops the executable body from the AST.  Scan only the original
    lexical stream, skipping strings and quoted identifiers, so a literal such
    as ``'/*! text */'`` remains harmless.
    """

    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        successor = sql[index + 1] if index + 1 < length else ""

        if char in {"'", '"', "`"}:
            index = _skip_mysql_quoted(sql, index, char)
            continue
        if char == "-" and successor == "-":
            # MySQL recognizes ``--`` as a comment only when the second
            # hyphen is followed by whitespace or a control character.  A
            # token such as ``1--1`` is arithmetic, so do not skip its tail.
            after_comment = sql[index + 2] if index + 2 < length else ""
            if not after_comment or after_comment.isspace():
                index = _skip_to_line_end(sql, index + 2)
                continue
        if char == "#":
            index = _skip_to_line_end(sql, index + 1)
            continue
        if char == "/" and successor == "*":
            if index + 2 < length and sql[index + 2] == "!":
                return True
            terminator = sql.find("*/", index + 2)
            if terminator < 0:
                return False
            index = terminator + 2
            continue
        index += 1
    return False


def _skip_mysql_quoted(sql: str, start: int, quote: str) -> int:
    index = start + 1
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            if index + 1 < length and sql[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    return length


def _skip_to_line_end(sql: str, start: int) -> int:
    newline = sql.find("\n", start)
    return len(sql) if newline < 0 else newline + 1


def _reject_unsafe_xml_nodes(ast: exp.Query) -> None:
    for node in ast.walk():
        node_name = type(node).__name__
        if node_name in _UNSAFE_XML_NODE_NAMES:
            _fail(
                NATIVE_SQL_UNSAFE_EXTERNAL_SOURCE,
                f"XML execution is not allowed in the native sandbox: {node_name}",
            )
        if isinstance(node, exp.Anonymous):
            name = _function_name(node)
            if name in _UNSAFE_XML_FUNCTION_NAMES or name.startswith(("xml", "dbms_xml")):
                _fail(
                    NATIVE_SQL_UNSAFE_EXTERNAL_SOURCE,
                    f"XML execution is not allowed in the native sandbox: {name}",
                )
        if isinstance(node, exp.DataType) and node.this == exp.DataType.Type.XML:
            _fail(
                NATIVE_SQL_UNSAFE_EXTERNAL_SOURCE,
                "XML data interpretation is not allowed in the native sandbox",
            )


def _reject_side_effect_nodes(ast: exp.Query) -> None:
    for node in ast.walk():
        if isinstance(node, _SIDE_EFFECT_NODE_TYPES):
            _fail(
                NATIVE_SQL_UNSAFE_SIDE_EFFECT,
                f"side-effecting query construct is not allowed: {type(node).__name__}",
            )

        # Oracle sequence access is represented as a qualified column rather
        # than a function/node. NEXTVAL mutates the sequence; CURRVAL does not.
        if isinstance(node, exp.Column) and node.name.casefold() == "nextval":
            _fail(
                NATIVE_SQL_UNSAFE_SIDE_EFFECT,
                "side-effecting query construct is not allowed: NEXTVAL",
            )
        if isinstance(node, exp.Func) and _function_name(node) in _SIDE_EFFECT_FUNCTIONS:
            _fail(
                NATIVE_SQL_UNSAFE_SIDE_EFFECT,
                f"side-effecting function is not allowed: {_function_name(node)}",
            )


def _reject_dangerous_functions(ast: exp.Query, dialect: str | None) -> None:
    for function in ast.find_all(exp.Func):
        names = {_function_name(function)}
        names.update(_qualified_function_names(function))
        # SQLGlot normalizes Oracle DBMS_RANDOM.VALUE to RAND and erases the
        # original package name. It is still outside the teaching allow-list.
        if dialect is not None:
            try:
                normalized_dialect = normalize_sql_dialect(dialect)
            except UnsupportedSQLDialectError as exc:
                _fail(NATIVE_SQL_PARSE_ERROR, str(exc))
            if normalized_dialect == "oracle" and isinstance(function, exp.Rand):
                names.add("dbms_random")
        dangerous = next(
            (
                name
                for name in names
                if name in _DANGEROUS_FUNCTIONS
                or name.startswith(_DANGEROUS_FUNCTION_PREFIXES)
            ),
            None,
        )
        if dangerous:
            code = (
                NATIVE_SQL_UNSAFE_EXTERNAL_SOURCE
                if _is_external_function(dangerous)
                else NATIVE_SQL_UNSAFE_FUNCTION
            )
            _fail(code, f"function is not allowed in the native sandbox: {dangerous}")


def _enforce_function_allowlist(ast: exp.Query, dialect: str | None) -> None:
    normalized_dialect = None
    if dialect is not None:
        try:
            normalized_dialect = normalize_sql_dialect(dialect)
        except UnsupportedSQLDialectError as exc:
            _fail(NATIVE_SQL_PARSE_ERROR, str(exc))
    anonymous_allowed = set().union(*_ALLOWED_ANONYMOUS_FUNCTIONS.values())
    if normalized_dialect is not None:
        anonymous_allowed = set(_ALLOWED_ANONYMOUS_FUNCTIONS[normalized_dialect])

    for function in ast.find_all(exp.Func):
        qualified = _qualified_function_names(function)
        if qualified:
            _fail(
                NATIVE_SQL_UNSAFE_FUNCTION,
                "qualified/package function calls are not allowed in the native sandbox",
            )
        if isinstance(function, exp.Anonymous):
            name = _function_name(function)
            if name not in anonymous_allowed:
                _fail(
                    NATIVE_SQL_UNSAFE_FUNCTION,
                    f"function is outside the native sandbox allow-list: {name}",
                )
            continue
        node_name = type(function).__name__
        if node_name not in _ALLOWED_FUNCTION_NODE_NAMES:
            _fail(
                NATIVE_SQL_UNSAFE_FUNCTION,
                f"function is outside the native sandbox allow-list: {node_name}",
            )


def _function_name(function: exp.Func) -> str:
    if isinstance(function, exp.Anonymous):
        return _normalize_name(function.name)
    try:
        return _normalize_name(function.sql_name())
    except (AttributeError, TypeError):
        return _normalize_name(function.name)


def _qualified_function_names(function: exp.Func) -> set[str]:
    names: set[str] = set()
    current: exp.Expression = function
    while isinstance(current.parent, exp.Dot):
        current = current.parent
        parts = tuple(_dot_name_parts(current))
        if parts:
            names.add(".".join(parts))
            names.update(parts)
        if current.parent is None:
            break
    return names


def _dot_name_parts(node: exp.Expression) -> Iterable[str]:
    if isinstance(node, exp.Dot):
        yield from _dot_name_parts(node.this)
        yield from _dot_name_parts(node.expression)
    elif isinstance(node, exp.Identifier):
        yield _normalize_name(node.name)
    elif isinstance(node, exp.Func):
        yield _function_name(node)


def _is_external_function(name: str) -> bool:
    leaf = name.rsplit(".", 1)[-1]
    return (
        leaf in {
            "bfilename",
            "bulkrowset",
            "csv_scan",
            "delta_scan",
            "glob",
            "http_get",
            "httpfs",
            "iceberg_scan",
            "load_file",
            "opendatasource",
            "openquery",
            "openrowset",
            "parquet_scan",
            "program",
        }
        or leaf.startswith("dblink")
        or leaf.startswith("pg_read_")
        or leaf.startswith("read_")
        or leaf in {"lo_export", "lo_import"}
    )


def _reject_unsafe_tables(
    ast: exp.Query,
    allowed_tables: frozenset[str] | None,
) -> None:
    for scope in traverse_scope(ast):
        for table in scope.tables:
            source = scope.sources.get(table.alias_or_name)
            if isinstance(source, Scope):
                continue
            _reject_unsafe_table(table, allowed_tables)

    # Oracle package attributes without parentheses are parsed as qualified
    # columns (for example DBMS_UTILITY.GET_TIME), not function calls.
    for column in ast.find_all(exp.Column):
        qualifier = _normalize_name(column.table)
        if qualifier.startswith(("dbms_", "utl_")):
            _fail(
                NATIVE_SQL_UNSAFE_FUNCTION,
                f"package access is not allowed in the native sandbox: {qualifier}",
            )


def _reject_unsafe_table(
    table: exp.Table,
    allowed_tables: frozenset[str] | None,
) -> None:
    name = _normalize_name(table.name)
    db = table.args.get("db")
    catalog = table.args.get("catalog")

    if db is not None or catalog is not None:
        _fail(
            NATIVE_SQL_UNSAFE_OBJECT,
            f"schema/catalog-qualified table is not allowed: {table.sql()}",
        )

    identifier = table.this if isinstance(table.this, exp.Identifier) else None
    if identifier is not None and (
        identifier.args.get("temporary")
        or identifier.args.get("global_")
        or name.startswith(("#", "@"))
    ):
        _fail(
            NATIVE_SQL_UNSAFE_OBJECT,
            f"temporary or variable table is not allowed: {name}",
        )

    if "@" in name:
        _fail(
            NATIVE_SQL_UNSAFE_EXTERNAL_SOURCE,
            f"database-link table is not allowed: {name}",
        )

    # A table-valued function has no identifier name and is checked by the
    # function allow-list above. CTE references were removed by scope.
    if not name or name == "dual":
        return
    if name in _SYSTEM_TABLE_EXACT or name.startswith(_SYSTEM_TABLE_PREFIXES):
        _fail(
            NATIVE_SQL_UNSAFE_OBJECT,
            f"system catalog object is not allowed: {name}",
        )
    if allowed_tables is not None and name not in allowed_tables:
        _fail(
            NATIVE_SQL_UNSAFE_OBJECT,
            f"table is outside the native fixture allow-list: {name}",
        )



def _reject_system_parameters(ast: exp.Query) -> None:
    session_parameter = getattr(exp, "SessionParameter", None)
    parameter = getattr(exp, "Parameter", None)
    unsafe_parameter_types = tuple(
        node_type
        for node_type in (session_parameter, parameter)
        if isinstance(node_type, type)
    )
    if unsafe_parameter_types and any(
        isinstance(node, unsafe_parameter_types) for node in ast.walk()
    ):
        _fail(
            NATIVE_SQL_UNSAFE_OBJECT,
            "server/session parameters are not available in the native sandbox",
        )


def _normalize_name(value: object) -> str:
    return str(value or "").strip().casefold()


def _fail(code: str, message: str) -> None:
    raise NativeQuerySafetyError(code, message)
