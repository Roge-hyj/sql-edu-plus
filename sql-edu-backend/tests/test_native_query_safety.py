import pytest
from sqlglot import exp, parse_one

from core.native_query_safety import (
    NATIVE_SQL_PARSE_ERROR,
    NATIVE_SQL_UNSAFE_EXTERNAL_SOURCE,
    NATIVE_SQL_UNSAFE_FUNCTION,
    NATIVE_SQL_UNSAFE_OBJECT,
    NATIVE_SQL_UNSAFE_SIDE_EFFECT,
    NATIVE_SQL_UNSAFE_STATEMENT,
    NativeQuerySafetyError,
    validate_native_query_safety,
)


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("mysql", "SELECT u.id, COUNT(*) AS n FROM users AS u GROUP BY u.id"),
        (
            "postgres",
            "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n "
            "WHERE x < 3) SELECT x, ROW_NUMBER() OVER (ORDER BY x) FROM n",
        ),
        (
            "tsql",
            "WITH ranked AS (SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS rn "
            "FROM users) SELECT id FROM ranked WHERE rn = 1",
        ),
        ("oracle", "SELECT department_id, AVG(salary) FROM employees GROUP BY department_id"),
        ("postgres", "SELECT 'pg_read_file and information_schema are text' AS note"),
        ("mysql", "SELECT JSON_EXTRACT(payload, '$.name') FROM events"),
        ("mysql", "SELECT 'INTO OUTFILE /tmp/x' AS note"),
        ("oracle", "SELECT CURRENT_DATE FROM dual"),
        ("postgres", "SELECT 'drop insert update' AS note"),
    ],
)
def test_allows_normal_dql_features(dialect, sql):
    ast = validate_native_query_safety(sql, dialect)

    assert isinstance(ast, exp.Query)


def test_accepts_an_already_parsed_query_ast_without_copying_it():
    ast = parse_one("SELECT id FROM users", read="postgres")

    assert validate_native_query_safety(ast, "postgres") is ast


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT /*! SQL_NO_CACHE */ id FROM users",
        "/*!40101 SET @judge_bypass = 1 */ SELECT 1",
        "SELECT 1 /*!50000 UNION SELECT secret FROM mysql.user */",
    ],
)
def test_rejects_mysql_executable_comments(sql):
    with pytest.raises(NativeQuerySafetyError) as caught:
        validate_native_query_safety(sql, "mysql")

    assert caught.value.code == NATIVE_SQL_UNSAFE_SIDE_EFFECT


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT /* ordinary comment */ 1",
        "SELECT '/*! SET @judge_bypass = 1 */' AS note",
        "SELECT `/*! not executable */` FROM users",
        "SELECT 1 -- /*! not executable */",
        "SELECT 1 # /*! not executable */",
    ],
)
def test_mysql_executable_comment_check_respects_lexical_boundaries(sql):
    ast = validate_native_query_safety(sql, "mysql")

    assert isinstance(ast, exp.Query)


def test_mysql_double_hyphen_without_whitespace_does_not_hide_executable_comment():
    sql = "SELECT 1--1 /*!50000 + SLEEP(10) */"

    with pytest.raises(NativeQuerySafetyError) as caught:
        validate_native_query_safety(sql, "mysql")

    assert caught.value.code == NATIVE_SQL_UNSAFE_SIDE_EFFECT


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("postgres", "SELECT 1; SELECT 2"),
        ("mysql", "DELETE FROM users"),
        ("postgres", "COPY users TO PROGRAM 'id'"),
        ("tsql", "EXEC xp_cmdshell 'whoami'"),
        ("oracle", "DROP TABLE users"),
    ],
)
def test_rejects_non_query_or_multiple_statements(dialect, sql):
    with pytest.raises(NativeQuerySafetyError) as caught:
        validate_native_query_safety(sql, dialect)

    assert caught.value.code == NATIVE_SQL_UNSAFE_STATEMENT


def test_rejects_invalid_sql_with_a_stable_parse_code():
    with pytest.raises(NativeQuerySafetyError) as caught:
        validate_native_query_safety("SELECT ( FROM", "postgres")

    assert caught.value.code == NATIVE_SQL_PARSE_ERROR


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("tsql", "SELECT * INTO copied_users FROM users"),
        ("postgres", "SELECT * INTO copied_users FROM users"),
        ("mysql", "SELECT @answer := id FROM users"),
        ("postgres", "SELECT nextval('course_sequence')"),
        ("oracle", "SELECT course_sequence.NEXTVAL FROM dual"),
        ("tsql", "SELECT NEXT VALUE FOR course_sequence"),
        ("postgres", "SELECT * FROM users FOR UPDATE"),
    ],
)
def test_rejects_side_effects_hidden_inside_query_nodes(dialect, sql):
    with pytest.raises(NativeQuerySafetyError) as caught:
        validate_native_query_safety(sql, dialect)

    assert caught.value.code == NATIVE_SQL_UNSAFE_SIDE_EFFECT


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("postgres", "SELECT set_config('search_path', 'public', false)"),
        ("postgres", "SELECT pg_advisory_lock(42)"),
        ("postgres", "SELECT pg_terminate_backend(123)"),
        ("postgres", "SELECT pg_sleep_for(INTERVAL '1 second')"),
        ("postgres", "SELECT pg_notify('channel', 'message')"),
        ("postgres", "SELECT query_to_xml('SELECT * FROM pg_authid', false, false, '')"),
        ("postgres", "SELECT lo_create(12345)"),
        ("mysql", "SELECT SLEEP(10)"),
        ("mysql", "SELECT GET_LOCK('judge', 10)"),
        ("mysql", "SELECT BENCHMARK(1000000, SHA2('x', 256))"),
        ("tsql", "SELECT xp_cmdshell('whoami')"),
        ("oracle", "SELECT SYS_CONTEXT('USERENV', 'CURRENT_USER') FROM dual"),
        ("oracle", "SELECT DBMS_LOB.FILEOPEN(document) FROM files"),
        ("oracle", "SELECT DBMS_RANDOM.VALUE FROM dual"),
        ("oracle", "SELECT DBMS_UTILITY.GET_TIME FROM dual"),
        ("oracle", "SELECT UTL_FILE.FOPEN('DIR', 'FILE', 'R') FROM dual"),
    ],
)
def test_rejects_dangerous_or_side_effecting_functions(dialect, sql):
    with pytest.raises(NativeQuerySafetyError) as caught:
        validate_native_query_safety(sql, dialect)

    assert caught.value.code == NATIVE_SQL_UNSAFE_FUNCTION


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("postgres", "SELECT pg_advisory_xact_lock(42)"),
        ("postgres", "SELECT pg_export_snapshot()"),
        ("postgres", "SELECT public.side_effect_udf()"),
        ("oracle", "SELECT HTTPURITYPE('http://127.0.0.1/').GETCLOB() FROM dual"),
    ],
)
def test_rejects_unknown_user_defined_or_package_functions(dialect, sql):
    with pytest.raises(NativeQuerySafetyError) as caught:
        validate_native_query_safety(sql, dialect)

    assert caught.value.code in {
        NATIVE_SQL_UNSAFE_FUNCTION,
        NATIVE_SQL_UNSAFE_EXTERNAL_SOURCE,
    }


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        (
            "oracle",
            "SELECT * FROM XMLTABLE('/ROWSET/ROW' PASSING XMLTYPE(BFILENAME("
            "'XML_DIR', 'payload.xml'), NLS_CHARSET_ID('AL32UTF8')))",
        ),
        (
            "oracle",
            "SELECT XMLTYPE('<!DOCTYPE x [<!ENTITY secret SYSTEM "
            "\"file:///etc/passwd\">]><x>&secret;</x>') FROM dual",
        ),
        ("oracle", "SELECT EXTRACTVALUE(XMLTYPE('<a/>'), '/a') FROM dual"),
        ("postgres", "SELECT CAST('<a/>' AS XML)"),
        ("tsql", "SELECT CONVERT(xml, '<a/>')"),
    ],
)
def test_rejects_xml_interpretation_and_external_entity_surfaces(dialect, sql):
    with pytest.raises(NativeQuerySafetyError) as caught:
        validate_native_query_safety(sql, dialect)

    assert caught.value.code == NATIVE_SQL_UNSAFE_EXTERNAL_SOURCE


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("mysql", "SELECT /*+ SET_VAR(max_execution_time=0) */ 1"),
        ("tsql", "SELECT * FROM users WITH (TABLOCKX) OPTION(QUERYTRACEON 9481)"),
        ("oracle", "SELECT /*+ PARALLEL(64) */ id FROM users"),
    ],
)
def test_rejects_optimizer_resource_and_locking_hints(dialect, sql):
    with pytest.raises(NativeQuerySafetyError) as caught:
        validate_native_query_safety(sql, dialect)

    assert caught.value.code == NATIVE_SQL_UNSAFE_SIDE_EFFECT


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("postgres", "SELECT pg_read_file('/etc/passwd')"),
        ("postgres", "SELECT pg_catalog.pg_read_binary_file('/etc/passwd')"),
        ("postgres", "SELECT * FROM dblink('host=x', 'select 1') AS t(id int)"),
        ("postgres", "SELECT lo_import('/tmp/data')"),
        ("mysql", "SELECT LOAD_FILE('/etc/passwd')"),
        ("mysql", "SELECT * FROM users INTO OUTFILE '/tmp/answers.csv'"),
        ("mysql", "SELECT * FROM users INTO DUMPFILE '/tmp/answers.bin'"),
        ("tsql", "SELECT * FROM OPENROWSET('SQLNCLI', 'server=x', 'select 1') AS x"),
        ("tsql", "SELECT * FROM OPENDATASOURCE('SQLNCLI', 'server=x').db.dbo.t"),
        ("oracle", "SELECT BFILENAME('DATA_DIR', 'secret.txt') FROM dual"),
        ("oracle", "SELECT * FROM employees@remote_database"),
        ("sqlite", "SELECT * FROM READ_CSV('/tmp/answers.csv')"),
        ("postgres", "SELECT * FROM READ_PARQUET('/tmp/answers.parquet')"),
    ],
)
def test_rejects_external_file_and_remote_data_access(dialect, sql):
    with pytest.raises(NativeQuerySafetyError) as caught:
        validate_native_query_safety(sql, dialect)

    assert caught.value.code == NATIVE_SQL_UNSAFE_EXTERNAL_SOURCE


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("postgres", "SELECT * FROM pg_catalog.pg_tables"),
        ("postgres", "SELECT * FROM information_schema.tables"),
        ("postgres", "SELECT * FROM public.users"),
        ("postgres", "SELECT * FROM pg_tables"),
        ("postgres", "SELECT * FROM pg_class"),
        ("mysql", "SELECT * FROM mysql.user"),
        ("mysql", "SELECT * FROM sys.metrics"),
        ("tsql", "SELECT * FROM master.sys.databases"),
        ("tsql", "SELECT * FROM tempdb.dbo.items"),
        ("tsql", "SELECT * FROM dbo.users"),
        ("tsql", "SELECT * FROM sysobjects"),
        ("oracle", "SELECT * FROM SYS.USER$"),
        ("oracle", "SELECT * FROM V$SESSION"),
        ("oracle", "SELECT * FROM DBA_USERS"),
        ("oracle", "SELECT * FROM ALL_TABLES"),
        ("mysql", "SELECT @@global.secure_file_priv"),
        ("tsql", "SELECT @@VERSION"),
    ],
)
def test_rejects_qualified_or_system_catalog_objects(dialect, sql):
    with pytest.raises(NativeQuerySafetyError) as caught:
        validate_native_query_safety(sql, dialect)

    assert caught.value.code == NATIVE_SQL_UNSAFE_OBJECT


def test_table_alias_qualification_is_allowed():
    ast = validate_native_query_safety(
        "SELECT u.id FROM users AS u JOIN enrollments AS e ON e.user_id = u.id",
        "postgres",
    )

    assert isinstance(ast, exp.Query)


def test_allowed_tables_restricts_physical_sources_to_the_fixture():
    ast = validate_native_query_safety(
        "SELECT u.id FROM users AS u JOIN enrollments AS e ON e.user_id = u.id",
        "postgres",
        allowed_tables={"USERS", "enrollments"},
    )

    assert isinstance(ast, exp.Query)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM pg_authid",
        "SELECT * FROM sqlite_master",
        "SELECT * FROM duckdb_tables",
        "SELECT * FROM pragma_table_info",
        "SELECT * FROM information_schema",
        "SELECT * FROM sys",
        "SELECT * FROM secrets",
    ],
)
def test_allowed_tables_rejects_system_and_non_fixture_physical_objects(sql):
    with pytest.raises(NativeQuerySafetyError) as caught:
        validate_native_query_safety(sql, "postgres", allowed_tables={"users"})

    assert caught.value.code == NATIVE_SQL_UNSAFE_OBJECT


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("tsql", "SELECT * FROM sysfiles"),
        ("tsql", "SELECT * FROM dm_exec_sessions"),
        ("oracle", "SELECT * FROM product_component_version"),
        ("oracle", "SELECT * FROM nls_database_parameters"),
        ("oracle", "SELECT * FROM session_privs"),
    ],
)
def test_allowed_tables_rejects_unqualified_vendor_system_objects(dialect, sql):
    with pytest.raises(NativeQuerySafetyError) as caught:
        validate_native_query_safety(sql, dialect, allowed_tables={"users"})

    assert caught.value.code == NATIVE_SQL_UNSAFE_OBJECT


@pytest.mark.parametrize(
    "sql",
    [
        "WITH fixture_rows AS (SELECT * FROM users) SELECT * FROM fixture_rows",
        "SELECT * FROM (SELECT * FROM users) AS fixture_rows",
        "WITH RECURSIVE fixture_rows(n) AS (SELECT 1 UNION ALL SELECT n + 1 "
        "FROM fixture_rows WHERE n < 2) SELECT * FROM fixture_rows",
    ],
)
def test_allowed_tables_preserves_cte_and_derived_table_scope(sql):
    ast = validate_native_query_safety(sql, "postgres", allowed_tables={"users"})

    assert isinstance(ast, exp.Query)


def test_cte_name_cannot_hide_a_disallowed_physical_source_from_allowed_tables():
    sql = (
        "WITH users AS (SELECT id FROM secrets) "
        "SELECT id FROM users"
    )

    with pytest.raises(NativeQuerySafetyError) as caught:
        validate_native_query_safety(sql, "postgres", allowed_tables={"users"})

    assert caught.value.code == NATIVE_SQL_UNSAFE_OBJECT


def test_omitting_allowed_tables_keeps_compatibility_for_route_preflight():
    ast = validate_native_query_safety("SELECT * FROM course_fixture", "postgres")

    assert isinstance(ast, exp.Query)


def test_unqualified_recursive_cte_name_is_not_treated_as_system_object():
    ast = validate_native_query_safety(
        "WITH RECURSIVE pg_stat_path(n) AS (SELECT 1 UNION ALL "
        "SELECT n + 1 FROM pg_stat_path WHERE n < 2) SELECT * FROM pg_stat_path",
        "postgres",
    )

    assert isinstance(ast, exp.Query)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT relname FROM pg_class WHERE EXISTS ("
        "WITH pg_class AS (SELECT 1 AS n) SELECT n FROM pg_class)",
        "WITH x AS (WITH pg_class AS (SELECT 1 AS n) SELECT n FROM pg_class) "
        "SELECT relname FROM pg_class",
    ],
)
def test_cte_alias_does_not_exempt_out_of_scope_system_table(sql):
    with pytest.raises(NativeQuerySafetyError) as caught:
        validate_native_query_safety(sql, "postgres")

    assert caught.value.code == NATIVE_SQL_UNSAFE_OBJECT
