import pytest
from sqlglot import exp

from core.sql_dialect_resolver import (
    DialectResolutionSource,
    DialectResolutionStatus,
    StrictSQLParseError,
    detect_dialect_features,
    normalize_sql_dialect,
    parse_single_query,
    resolve_sql_dialect,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("MySQL", "mysql"),
        ("standard", "standard"),
        ("ANSI", "standard"),
        ("ansi_sql", "standard"),
        ("mariadb", "mysql"),
        ("postgresql", "postgres"),
        ("PG", "postgres"),
        ("sql_server", "tsql"),
        ("MSSQL", "tsql"),
        ("sqlite", "sqlite"),
        ("oracle23ai", "oracle"),
    ],
)
def test_normalize_sql_dialect_aliases(value, expected):
    assert normalize_sql_dialect(value) == expected


def test_declared_dialect_has_priority_over_detected_or_default_dialect():
    resolution = resolve_sql_dialect(
        "SELECT id FROM users ORDER BY id LIMIT 2",
        declared_dialect="postgresql",
        default_dialect="mysql",
    )

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.source == DialectResolutionSource.DECLARED
    assert resolution.dialect == "postgres"
    assert resolution.parse_dialect == "postgres"
    assert isinstance(resolution.ast, exp.Query)


def test_declared_standard_sql_uses_generic_parser_and_concrete_default_engine():
    resolution = resolve_sql_dialect(
        "SELECT id FROM users ORDER BY id FETCH FIRST 2 ROWS ONLY",
        declared_dialect="ansi",
        default_dialect="postgres",
    )

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.source == DialectResolutionSource.DECLARED
    assert resolution.requested_dialect == "standard"
    assert resolution.dialect == "postgres"
    assert resolution.parse_dialect is None
    assert "SHARED_FETCH_FIRST" in resolution.detected_features


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT `id` FROM users",
        "SELECT TOP 2 id FROM users",
        "SELECT id::text FROM users",
        "SELECT id FROM users WHERE ROWNUM <= 2",
        "SELECT id FROM users LIMIT 2",
    ],
)
def test_declared_standard_sql_rejects_vendor_specific_syntax(sql):
    resolution = resolve_sql_dialect(
        sql,
        declared_dialect="standard",
        default_dialect="mysql",
    )

    assert resolution.status == DialectResolutionStatus.SYNTAX_ERROR
    assert resolution.requested_dialect == "standard"
    assert resolution.ast is None


def test_standard_sql_cannot_be_used_as_the_execution_default():
    resolution = resolve_sql_dialect(
        "SELECT id FROM users",
        default_dialect="standard",
    )

    assert resolution.status == DialectResolutionStatus.UNSUPPORTED_DIALECT
    assert "concrete execution engine" in (resolution.error or "")


def test_valid_declared_dialect_does_not_depend_on_default_configuration():
    resolution = resolve_sql_dialect(
        "SELECT id FROM users",
        declared_dialect="mysql",
        default_dialect="not-configured",
    )

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.source == DialectResolutionSource.DECLARED
    assert resolution.dialect == "mysql"


def test_declared_dialect_does_not_fall_back_when_parse_fails():
    resolution = resolve_sql_dialect(
        "SELECT TOP 2 id FROM users",
        declared_dialect="postgres",
        default_dialect="tsql",
    )

    assert resolution.status == DialectResolutionStatus.SYNTAX_ERROR
    assert resolution.dialect == "postgres"
    assert resolution.ast is None


@pytest.mark.parametrize(
    ("declared_dialect", "sql", "feature"),
    [
        ("postgres", "SELECT IFNULL(name, 'x') FROM users", "SHARED_IFNULL"),
        ("mysql", "SELECT id::text FROM users", "POSTGRES_CAST_OPERATOR"),
        ("tsql", "SELECT id FROM users LIMIT 1", "SHARED_LIMIT"),
        ("postgres", "SELECT id FROM users LIMIT 1, 2", "MYSQL_LIMIT_COMMA"),
        (
            "postgres",
            "SELECT a.id FROM a, b WHERE a.id = b.id(+)",
            "ORACLE_OUTER_JOIN_MARKER",
        ),
    ],
)
def test_declared_dialect_rejects_transpilable_foreign_syntax(
    declared_dialect,
    sql,
    feature,
):
    resolution = resolve_sql_dialect(sql, declared_dialect=declared_dialect)

    assert resolution.status == DialectResolutionStatus.SYNTAX_ERROR
    assert resolution.dialect == declared_dialect
    assert feature in (resolution.error or "")
    assert resolution.ast is None


def test_unknown_declared_dialect_is_not_silently_coerced_to_mysql():
    resolution = resolve_sql_dialect("SELECT 1", declared_dialect="snowflake")

    assert resolution.status == DialectResolutionStatus.UNSUPPORTED_DIALECT
    assert resolution.dialect is None
    assert "snowflake" in (resolution.error or "")


@pytest.mark.parametrize(
    ("sql", "dialect", "feature"),
    [
        ("SELECT TOP 2 id FROM users", "tsql", "TSQL_TOP"),
        ("SELECT `order` FROM `orders`", "mysql", "MYSQL_BACKTICK_IDENTIFIER"),
        (
            "SELECT DISTINCT ON (customer_id) * FROM orders ORDER BY customer_id",
            "postgres",
            "POSTGRES_DISTINCT_ON",
        ),
        ("SELECT amount::numeric FROM payments", "postgres", "POSTGRES_CAST_OPERATOR"),
        (
            "SELECT u.id FROM users u CROSS APPLY (SELECT TOP 1 id FROM orders) x",
            "tsql",
            "SHARED_APPLY",
        ),
        ("SELECT name FROM users WHERE name GLOB 'A*'", "sqlite", "SQLITE_GLOB"),
        (
            "SELECT employee_id FROM employees START WITH manager_id IS NULL "
            "CONNECT BY PRIOR employee_id = manager_id",
            "oracle",
            "ORACLE_CONNECT_BY",
        ),
        ("SELECT id FROM users LIMIT 1, 2", "mysql", "MYSQL_LIMIT_COMMA"),
        ("SELECT ISNULL(score, 0) FROM results", "tsql", "TSQL_ISNULL_FUNCTION"),
        ("SELECT NVL(score, 0) FROM results", "oracle", "ORACLE_NVL_FUNCTION"),
        ("SELECT SYSDATE FROM DUAL", "oracle", "ORACLE_SYSDATE"),
        ("SELECT [id] FROM [users]", "tsql", "TSQL_BRACKET_IDENTIFIER"),
    ],
)
def test_unique_high_confidence_feature_selects_dialect(sql, dialect, feature):
    resolution = resolve_sql_dialect(sql, default_dialect="mysql")

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.source == DialectResolutionSource.DETECTED
    assert resolution.dialect == dialect
    assert resolution.parse_dialect == dialect
    assert feature in resolution.detected_features


def test_generic_sql_keeps_generic_ast_and_uses_default_engine():
    resolution = resolve_sql_dialect(
        "SELECT id, name FROM users WHERE active = 1 ORDER BY id",
        default_dialect="postgres",
    )

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.source == DialectResolutionSource.DEFAULT
    assert resolution.execution_dialect == "postgres"
    assert resolution.parse_dialect is None
    assert isinstance(resolution.ast, exp.Query)


def test_sqlite_accepts_backtick_identifier_compatibility_syntax():
    resolution = resolve_sql_dialect(
        "SELECT `select` FROM `order`",
        declared_dialect="sqlite",
    )

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.dialect == "sqlite"
    assert resolution.ast is not None


@pytest.mark.parametrize(
    ("sql", "feature"),
    [
        ("SELECT id FROM users LIMIT 5", "SHARED_LIMIT"),
        (
            "SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM users QUALIFY rn = 1",
            "SHARED_QUALIFY",
        ),
        ("SELECT payload->>'name' FROM events", "SHARED_JSON_ARROW"),
        ("SELECT JSON_EXTRACT(payload, '$.name') FROM events", "SHARED_JSON_EXTRACT"),
        ("SELECT id FROM users ORDER BY id FETCH FIRST 5 ROWS ONLY", "SHARED_FETCH_FIRST"),
        ("SELECT value FROM GENERATE_SERIES(1, 5)", "SHARED_GENERATE_SERIES"),
    ],
)
def test_shared_features_do_not_override_default_engine(sql, feature):
    resolution = resolve_sql_dialect(sql, default_dialect="sqlite")

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.source == DialectResolutionSource.DEFAULT
    assert resolution.dialect == "sqlite"
    assert resolution.candidates == ()
    assert feature in resolution.detected_features


def test_shared_apply_requires_tsql_or_oracle_declaration_when_default_is_other_engine():
    resolution = resolve_sql_dialect(
        "SELECT u.id FROM users u CROSS APPLY (SELECT u.id AS id) x",
        default_dialect="mysql",
    )

    assert resolution.status == DialectResolutionStatus.DIALECT_CONFLICT
    assert resolution.candidates == ("oracle", "tsql")
    assert "SHARED_APPLY" in resolution.detected_features


@pytest.mark.parametrize(
    ("sql", "dialect", "feature"),
    [
        ("SELECT IF(score >= 60, 1, 0) FROM results", "mysql", "MYSQL_IF_FUNCTION"),
        (
            "SELECT DATE_FORMAT(created_at, '%Y-%m') FROM orders",
            "mysql",
            "MYSQL_DATE_FORMAT_FUNCTION",
        ),
        (
            "SELECT id FROM tags WHERE FIND_IN_SET('sql', labels)",
            "mysql",
            "MYSQL_FIND_IN_SET_FUNCTION",
        ),
        (
            "SELECT GROUP_CONCAT(name ORDER BY name SEPARATOR ',') FROM users",
            "mysql",
            "MYSQL_GROUP_CONCAT_OPTIONS",
        ),
        (
            "SELECT name FROM users WHERE name ~* '^sql'",
            "postgres",
            "POSTGRES_REGEX_OPERATOR",
        ),
        (
            "SELECT DATEADD(day, 1, created_at) FROM orders",
            "tsql",
            "TSQL_DATEADD_FUNCTION",
        ),
        (
            "SELECT DATEDIFF(day, started_at, ended_at) FROM jobs",
            "tsql",
            "TSQL_DATEDIFF_FUNCTION",
        ),
        ("SELECT LEN(name) FROM users", "tsql", "TSQL_LEN_FUNCTION"),
        (
            "SELECT DECODE(status, 'A', 1, 0) FROM users",
            "oracle",
            "ORACLE_DECODE_FUNCTION",
        ),
        (
            "SELECT NVL2(name, 1, 0) FROM users",
            "oracle",
            "ORACLE_NVL2_FUNCTION",
        ),
    ],
)
def test_common_teaching_vendor_features_select_their_native_dialect(
    sql, dialect, feature
):
    resolution = resolve_sql_dialect(sql, default_dialect="sqlite")

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.source == DialectResolutionSource.DETECTED
    assert resolution.dialect == dialect
    assert feature in resolution.detected_features

    standard = resolve_sql_dialect(
        sql,
        declared_dialect="standard",
        default_dialect="mysql",
    )
    assert standard.status == DialectResolutionStatus.SYNTAX_ERROR
    assert (
        feature in (standard.error or "")
        or feature in standard.detected_features
    )


@pytest.mark.parametrize("keyword", ["PIVOT", "UNPIVOT"])
def test_pivot_requires_tsql_or_oracle_declaration(keyword):
    if keyword == "PIVOT":
        sql = "SELECT * FROM sales PIVOT (SUM(amount) FOR quarter IN ([Q1])) p"
    else:
        sql = "SELECT * FROM sales UNPIVOT (amount FOR quarter IN (q1, q2)) u"

    ambiguous = resolve_sql_dialect(sql, default_dialect="mysql")
    assert ambiguous.status == DialectResolutionStatus.DIALECT_CONFLICT
    assert ambiguous.candidates == ("oracle", "tsql")
    assert "SHARED_PIVOT" in ambiguous.detected_features

    standard = resolve_sql_dialect(
        sql,
        declared_dialect="standard",
        default_dialect="mysql",
    )
    assert standard.status == DialectResolutionStatus.SYNTAX_ERROR
    assert "SHARED_PIVOT" in (standard.error or "")


def test_mutually_exclusive_high_confidence_features_report_conflict():
    resolution = resolve_sql_dialect(
        "SELECT `amount`::numeric FROM `payments`",
        default_dialect="mysql",
    )

    assert resolution.status == DialectResolutionStatus.DIALECT_CONFLICT
    assert resolution.dialect is None
    assert resolution.candidates == ("mysql", "postgres")
    assert "MYSQL_BACKTICK_IDENTIFIER" in resolution.detected_features
    assert "POSTGRES_CAST_OPERATOR" in resolution.detected_features


def test_standard_and_student_queries_are_resolved_to_one_engine():
    resolution = resolve_sql_dialect(
        (
            "SELECT id FROM users ORDER BY id",
            "SELECT TOP 2 id FROM users ORDER BY id",
        ),
        default_dialect="mysql",
    )

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.dialect == "tsql"
    assert resolution.source == DialectResolutionSource.DETECTED
    assert len(resolution.asts) == 2


def test_conflicting_features_across_standard_and_student_queries_are_rejected():
    resolution = resolve_sql_dialect(
        (
            "SELECT `id` FROM `users`",
            "SELECT id::text FROM users",
        )
    )

    assert resolution.status == DialectResolutionStatus.DIALECT_CONFLICT
    assert resolution.candidates == ("mysql", "postgres")


def test_comments_and_string_literals_do_not_create_dialect_candidates():
    sql = """
        SELECT '-- SELECT TOP 2' AS note,
               'payload::jsonb' AS cast_example,
               '`quoted` DISTINCT ON (x)' AS examples
        FROM users
        /* CROSS APPLY, CONNECT BY, and GLOB are documentation only */
        LIMIT 1
    """

    resolution = resolve_sql_dialect(sql, default_dialect="postgres")

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.source == DialectResolutionSource.DEFAULT
    assert resolution.dialect == "postgres"
    assert resolution.detected_features == ("SHARED_LIMIT",)


def test_dialect_markers_inside_quoted_identifiers_do_not_conflict():
    resolution = resolve_sql_dialect(
        "SELECT `[amount::numeric]` FROM `payments`",
        default_dialect="postgres",
    )

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.dialect == "mysql"
    assert resolution.candidates == ("mysql",)


def test_feature_detector_ignores_tsql_escaped_bracket_identifier_contents():
    sql = "SELECT id AS [x]] GLOB] FROM users"

    features = detect_dialect_features(sql)
    auto_resolution = resolve_sql_dialect(sql, default_dialect="mysql")
    resolution = resolve_sql_dialect(sql, declared_dialect="tsql")

    assert "SQLITE_GLOB" not in {feature.name for feature in features}
    assert auto_resolution.dialect != "sqlite"
    assert auto_resolution.candidates == ()
    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.dialect == "tsql"


def test_feature_detector_ignores_mysql_hash_comments_but_keeps_postgres_json_operator():
    comments = detect_dialect_features("SELECT 1 # TOP 2 ::numeric\nLIMIT 1")
    json_operator = detect_dialect_features("SELECT payload #>> '{name}' FROM events")

    assert [feature.name for feature in comments] == ["SHARED_LIMIT"]
    assert [feature.name for feature in json_operator] == ["POSTGRES_JSON_PATH_OPERATOR"]


def test_feature_detector_ignores_postgres_dollar_quoted_string_contents():
    features = detect_dialect_features(
        "SELECT $body$ TOP 2, value::numeric, `name` $body$ AS documentation"
    )

    assert features == ()


def test_tsql_top_expression_is_a_high_confidence_feature():
    resolution = resolve_sql_dialect(
        "SELECT TOP (@row_count) PERCENT id FROM users ORDER BY id",
        default_dialect="mysql",
    )

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.source == DialectResolutionSource.DETECTED
    assert resolution.dialect == "tsql"
    assert "TSQL_TOP" in resolution.detected_features


def test_tsql_select_variable_assignment_is_a_high_confidence_feature():
    resolution = resolve_sql_dialect(
        "SELECT @accept = COUNT(*) FROM request_accepted",
        default_dialect="mysql",
    )

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.source == DialectResolutionSource.DETECTED
    assert resolution.dialect == "tsql"
    assert "TSQL_SELECT_VARIABLE_ASSIGNMENT" in resolution.detected_features


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM users USE INDEX (idx_active) WHERE active = 1",
        "SELECT id FROM users FORCE KEY FOR ORDER BY (idx_name) ORDER BY name",
        "SELECT id FROM users IGNORE INDEX FOR JOIN (idx_user)",
    ],
)
def test_mysql_table_index_hints_select_mysql(sql):
    resolution = resolve_sql_dialect(sql, default_dialect="postgres")

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.source == DialectResolutionSource.DETECTED
    assert resolution.dialect == "mysql"
    assert resolution.parse_dialect == "mysql"
    assert "MYSQL_INDEX_HINT" in resolution.detected_features


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT DISTINCTROW id FROM users",
        "SELECT HIGH_PRIORITY id FROM users",
        "SELECT SQL_NO_CACHE id FROM users",
        "SELECT DISTINCT HIGH_PRIORITY SQL_SMALL_RESULT id FROM users",
        "SELECT STRAIGHT_JOIN SQL_BIG_RESULT id FROM users",
        "SELECT SQL_BUFFER_RESULT id FROM users",
    ],
)
def test_mysql_select_modifiers_select_mysql(sql):
    resolution = resolve_sql_dialect(sql, default_dialect="sqlite")

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.source == DialectResolutionSource.DETECTED
    assert resolution.dialect == "mysql"
    assert resolution.parse_dialect == "mysql"
    assert "MYSQL_SELECT_MODIFIER" in resolution.detected_features


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT distinctrow FROM metrics",
        "SELECT high_priority FROM metrics",
        "SELECT sql_no_cache FROM metrics",
        "SELECT sql_calc_found_rows FROM metrics",
        "SELECT straight_join FROM metrics",
    ],
)
def test_mysql_modifier_names_used_as_plain_columns_do_not_select_mysql(sql):
    resolution = resolve_sql_dialect(sql, default_dialect="postgres")

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.source == DialectResolutionSource.DEFAULT
    assert resolution.dialect == "postgres"
    assert resolution.candidates == ()


def test_mysql_straight_join_operator_still_selects_mysql():
    resolution = resolve_sql_dialect(
        "SELECT a.id FROM schema_a.accounts AS a "
        "STRAIGHT_JOIN balances b ON b.account_id = a.id",
        default_dialect="postgres",
    )

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.source == DialectResolutionSource.DETECTED
    assert resolution.dialect == "mysql"
    assert "MYSQL_STRAIGHT_JOIN" in resolution.detected_features


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM ONLY users",
        "SELECT child.id FROM ONLY parent JOIN ONLY child ON child.parent_id = parent.id",
    ],
)
def test_postgres_from_only_selects_postgres(sql):
    resolution = resolve_sql_dialect(sql, default_dialect="mysql")

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.source == DialectResolutionSource.DETECTED
    assert resolution.dialect == "postgres"
    assert resolution.parse_dialect == "postgres"
    assert "POSTGRES_FROM_ONLY" in resolution.detected_features


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM users SAMPLE (10)",
        "SELECT id FROM analytics.users SAMPLE BLOCK (10) SEED (42)",
        'SELECT "id" FROM "USERS" SAMPLE (10)',
        'SELECT "id" FROM "APP"."USERS" SAMPLE BLOCK (10)',
    ],
)
def test_oracle_sample_clause_selects_oracle(sql):
    resolution = resolve_sql_dialect(sql, default_dialect="postgres")

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.source == DialectResolutionSource.DETECTED
    assert resolution.dialect == "oracle"
    assert resolution.parse_dialect == "oracle"
    assert "ORACLE_SAMPLE" in resolution.detected_features


def test_new_exclusive_features_are_ignored_inside_comments_and_strings():
    resolution = resolve_sql_dialect(
        """
        SELECT 'FROM ONLY users SAMPLE (10)' AS note
        FROM users
        /* USE INDEX (idx_user), SELECT HIGH_PRIORITY id */
        WHERE note = 'IGNORE KEY FOR JOIN (idx_user)'
        LIMIT 1
        """,
        default_dialect="sqlite",
    )

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.source == DialectResolutionSource.DEFAULT
    assert resolution.dialect == "sqlite"
    assert resolution.detected_features == ("SHARED_LIMIT",)


def test_shared_syntax_still_does_not_become_exclusive_with_new_rules():
    resolution = resolve_sql_dialect(
        "SELECT payload->>'name' FROM events ORDER BY id LIMIT 5",
        default_dialect="sqlite",
    )

    assert resolution.status == DialectResolutionStatus.RESOLVED
    assert resolution.source == DialectResolutionSource.DEFAULT
    assert resolution.dialect == "sqlite"
    assert resolution.candidates == ()
    assert resolution.detected_features == ("SHARED_LIMIT", "SHARED_JSON_ARROW")


def test_detected_dialect_is_strictly_reparsed_before_resolution():
    resolution = resolve_sql_dialect(
        "SELECT id FROM users USE INDEX (idx_active",
        default_dialect="postgres",
    )

    assert resolution.status == DialectResolutionStatus.SYNTAX_ERROR
    assert resolution.dialect == "mysql"
    assert resolution.candidates == ("mysql",)
    assert "MYSQL_INDEX_HINT" in resolution.detected_features
    assert resolution.ast is None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM users; SELECT id FROM admins",
        "INSERT INTO users(id) VALUES (1)",
        "",
        "SELECT * FROM (users",
    ],
)
def test_parse_single_query_rejects_non_single_or_non_query_sql(sql):
    with pytest.raises(StrictSQLParseError):
        parse_single_query(sql)


def test_parse_single_query_accepts_cte_query():
    ast = parse_single_query(
        "WITH active AS (SELECT id FROM users WHERE enabled = 1) "
        "SELECT id FROM active"
    )

    assert isinstance(ast, exp.Query)
    assert ast.find(exp.CTE) is not None


def test_strict_parser_can_enforce_declared_dialect_compatibility():
    with pytest.raises(StrictSQLParseError, match="POSTGRES_CAST_OPERATOR"):
        parse_single_query(
            "SELECT id::text FROM users",
            dialect="mysql",
            enforce_dialect_compatibility=True,
        )


def test_invalid_generic_sql_without_exclusive_feature_is_syntax_error():
    resolution = resolve_sql_dialect("SELECT * FORM users", default_dialect="mysql")

    assert resolution.status == DialectResolutionStatus.SYNTAX_ERROR
    assert resolution.source is None
    assert resolution.ast is None


def test_raise_helper_identifies_standard_and_student_parse_roles():
    from core.sql_dialect_resolver import DialectResolutionError, resolve_sql_dialect_or_raise

    with pytest.raises(DialectResolutionError) as standard_error:
        resolve_sql_dialect_or_raise(
            standard_sql="SELECT TOP 2 id FROM users",
            student_sql="SELECT id FROM users",
            declared_dialect="postgres",
        )
    assert standard_error.value.code == "STANDARD_SQL_PARSE_ERROR"

    with pytest.raises(DialectResolutionError) as student_error:
        resolve_sql_dialect_or_raise(
            standard_sql="SELECT id FROM users",
            student_sql="SELECT TOP 2 id FROM users",
            declared_dialect="postgres",
        )
    assert student_error.value.code == "STUDENT_SQL_PARSE_ERROR"


def test_standard_and_student_conflict_resolution_is_attached_to_error():
    from core.sql_dialect_resolver import DialectResolutionError, resolve_sql_dialect_or_raise

    with pytest.raises(DialectResolutionError) as caught:
        resolve_sql_dialect_or_raise(
            standard_sql="SELECT `id` FROM `users`",
            student_sql="SELECT id::text FROM users",
        )

    assert caught.value.code == "DIALECT_CONFLICT"
    assert caught.value.resolution is not None
    assert caught.value.resolution.candidates == ("mysql", "postgres")
