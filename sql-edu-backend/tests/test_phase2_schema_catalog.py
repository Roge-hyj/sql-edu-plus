import json

from core.phase2_schema_catalog import (
    JoinCardinality,
    MAX_COLUMNS_PER_TABLE,
    MAX_INPUT_BYTES,
    MAX_PUBLIC_OUTPUT_BYTES,
    MAX_TABLES,
    Phase2SchemaCatalog,
    SchemaConfidence,
    parse_schema_catalog,
)


def _university_catalog():
    return {
        "source": "fixture",
        "tables": [
            {
                "name": "student",
                "columns": [
                    {"name": "s_id", "data_type": "INTEGER", "nullable": False},
                    {"name": "name", "data_type": "VARCHAR(80)", "nullable": False},
                ],
                "primary_key": ["s_id"],
                "unique_constraints": [["s_id"]],
            },
            {
                "name": "course",
                "columns": [
                    {"name": "course_id", "data_type": "TEXT", "nullable": False},
                    {"name": "title", "data_type": "TEXT"},
                ],
                "primary_key": ["course_id"],
            },
            {
                "name": "takes",
                "columns": [
                    {"name": "s_id", "data_type": "INTEGER", "nullable": False},
                    {"name": "course_id", "data_type": "TEXT", "nullable": False},
                    {"name": "grade", "data_type": "TEXT", "nullable": True},
                ],
                "foreign_keys": [
                    {
                        "column": "s_id",
                        "references_table": "student",
                        "references_column": "s_id",
                    },
                    {
                        "columns": ["course_id"],
                        "references_table": "course",
                        "references_columns": ["course_id"],
                    },
                ],
                "unique_constraints": [["s_id", "course_id"]],
            },
        ],
    }


def test_online_string_column_preview_is_structure_only_and_rows_are_discarded():
    raw = json.dumps({
        "tables": [{
            "name": "student",
            "columns": ["s_id", "name"],
            "rows": [{"s_id": 1, "name": "secret sample"}],
        }]
    })
    catalog = parse_schema_catalog(raw)

    assert catalog.confidence is SchemaConfidence.STRUCTURE_ONLY
    assert catalog.table("STUDENT").column("S_ID").nullable is None
    public = catalog.public_facts()
    assert "rows" not in json.dumps(public)
    assert "secret sample" not in json.dumps(public)


def test_declared_catalog_normalizes_keys_types_nullability_and_uniques():
    catalog = Phase2SchemaCatalog.from_input(_university_catalog())
    takes = catalog.table("takes")

    assert catalog.confidence is SchemaConfidence.DECLARED
    assert takes is not None
    assert takes.column("grade").nullable is True
    assert takes.unique_constraints == (("s_id", "course_id"),)
    assert catalog.table("student").column("s_id").primary_key is True
    assert catalog.table("student").column("s_id").nullable is False


def test_bridge_path_and_fanout_queries_use_only_declared_relationships():
    catalog = parse_schema_catalog(_university_catalog())

    assert catalog.bridge_tables("student", "course") == ("takes",)
    assert catalog.join_path("student", "course") == ("student", "takes", "course")
    assert catalog.join_cardinality("takes", "student") is JoinCardinality.MANY_TO_ONE
    assert catalog.join_cardinality("student", "takes") is JoinCardinality.ONE_TO_MANY
    assert catalog.may_fan_out("student", "takes") is True
    assert catalog.may_fan_out("takes", "student") is False


def test_unique_grain_is_tristate_instead_of_guessing():
    declared = parse_schema_catalog(_university_catalog())
    structural = parse_schema_catalog({"tables": [{"name": "x", "columns": ["id"]}]})

    assert declared.uniquely_identifies("takes", ["course_id", "s_id", "grade"]) is True
    assert declared.uniquely_identifies("takes", ["s_id"]) is False
    assert structural.uniquely_identifies("x", ["id"]) is None
    assert structural.may_fan_out("x", "missing") is None


def test_column_level_primary_and_unique_flags_are_supported():
    catalog = parse_schema_catalog({
        "tables": [{
            "name": "account",
            "columns": [
                {"name": "id", "type": "bigint", "primary_key": True},
                {"name": "email", "type": "varchar(255)", "unique": True},
            ],
        }]
    })

    table = catalog.table("account")
    assert table.primary_key == ("id",)
    assert table.uniquely_identified_by(["email"]) is True
    assert table.column("id").data_type == "BIGINT"


def test_raw_spider_indexes_are_resolved_to_declared_facts():
    catalog = parse_schema_catalog({
        "db_id": "school",
        "table_names_original": ["student", "takes"],
        "column_names_original": [
            [-1, "*"], [0, "id"], [0, "name"], [1, "student_id"],
        ],
        "column_types": ["text", "number", "text", "number"],
        "primary_keys": [1],
        "foreign_keys": [[3, 1]],
    })

    assert catalog.confidence is SchemaConfidence.DECLARED
    assert catalog.table("student").primary_key == ("id",)
    assert catalog.join_cardinality("takes", "student") is JoinCardinality.MANY_TO_ONE


def test_compact_table_mapping_is_supported_without_inventing_constraints():
    catalog = parse_schema_catalog({"student": ["id", "name"], "course": ["id"]})

    assert catalog.confidence is SchemaConfidence.STRUCTURE_ONLY
    assert [item.name for item in catalog.tables] == ["course", "student"]
    assert catalog.bridge_tables("student", "course") == ()


def test_invalid_json_and_unsupported_values_degrade_to_unknown():
    assert parse_schema_catalog("{oops").confidence is SchemaConfidence.UNKNOWN
    assert parse_schema_catalog(None).confidence is SchemaConfidence.UNKNOWN
    assert parse_schema_catalog("{oops").limitations == ("SCHEMA_JSON_INVALID",)


def test_oversized_inputs_are_rejected_before_json_decoding():
    catalog = parse_schema_catalog("x" * (MAX_INPUT_BYTES + 1))
    assert catalog.confidence is SchemaConfidence.UNKNOWN
    assert catalog.limitations == ("SCHEMA_INPUT_TOO_LARGE",)


def test_table_and_column_bounds_reject_incomplete_catalogs():
    too_many_tables = {
        "tables": [{"name": f"t{i}", "columns": ["id"]} for i in range(MAX_TABLES + 1)]
    }
    catalog = parse_schema_catalog(too_many_tables)

    assert catalog.confidence is SchemaConfidence.UNKNOWN
    assert "SCHEMA_LIMIT_EXCEEDED" in catalog.limitations


def test_unresolved_or_malformed_foreign_keys_are_dropped_with_limitation():
    payload = _university_catalog()
    payload["tables"][2]["foreign_keys"].append({
        "column": "grade",
        "references_table": "missing_table",
        "references_column": "id",
    })
    catalog = parse_schema_catalog(payload)

    assert len(catalog.foreign_keys_from("takes")) == 2
    assert "UNRESOLVED_FOREIGN_KEY_DROPPED" in catalog.limitations


def test_unresolved_fk_alone_does_not_create_declared_confidence():
    catalog = parse_schema_catalog({
        "tables": [{
            "name": "child",
            "columns": ["parent_id"],
            "foreign_keys": [{
                "column": "parent_id",
                "references_table": "missing_parent",
                "references_column": "id",
            }],
        }]
    })

    assert catalog.confidence is SchemaConfidence.STRUCTURE_ONLY
    assert catalog.table("child").confidence is SchemaConfidence.STRUCTURE_ONLY


def test_untrusted_type_text_is_not_exposed_as_a_declared_type():
    catalog = parse_schema_catalog({
        "tables": [{
            "name": "student",
            "columns": [{"name": "id", "data_type": "SELECT SECRET ANSWER"}],
        }]
    })

    assert catalog.table("student").column("id").data_type is None
    assert "SELECT SECRET ANSWER" not in json.dumps(catalog.public_facts())
    assert "INVALID_COLUMN_TYPE_DROPPED" in catalog.limitations


def test_unsafe_identifiers_do_not_cross_the_catalog_boundary():
    catalog = parse_schema_catalog({
        "tables": [{"name": "users; DROP TABLE users", "columns": ["id"]}]
    })

    assert catalog.confidence is SchemaConfidence.UNKNOWN
    assert catalog.tables == ()


def test_public_facts_are_deterministic_and_json_safe():
    first = parse_schema_catalog(_university_catalog()).public_facts()
    second_payload = _university_catalog()
    second_payload["tables"] = list(reversed(second_payload["tables"]))
    second = parse_schema_catalog(second_payload).public_facts()

    assert first == second
    json.dumps(first, ensure_ascii=False)


def test_casefold_constraint_members_resolve_to_canonical_column_names():
    catalog = parse_schema_catalog({
        "tables": [
            {
                "name": "Parent",
                "columns": ["ID"],
                "primary_key": ["id"],
            },
            {
                "name": "Child",
                "columns": ["Parent_ID"],
                "unique_constraints": [["parent_id"]],
                "foreign_keys": [
                    {
                        "column": "parent_id",
                        "references_table": "PARENT",
                        "references_column": "id",
                    },
                    {
                        "column": "PARENT_ID",
                        "references_table": "parent",
                        "references_column": "ID",
                    },
                ],
            },
        ]
    })

    parent = catalog.table("parent")
    child = catalog.table("child")
    assert parent.primary_key == ("ID",)
    assert child.unique_constraints == (("Parent_ID",),)
    assert len(child.foreign_keys) == 1
    assert child.foreign_keys[0].columns == ("Parent_ID",)
    assert child.foreign_keys[0].references_table == "Parent"
    assert child.foreign_keys[0].references_columns == ("ID",)


def test_casefold_duplicate_columns_reject_the_ambiguous_table():
    catalog = parse_schema_catalog({
        "tables": [{"name": "ambiguous", "columns": ["ID", "id"]}]
    })

    assert catalog.confidence is SchemaConfidence.UNKNOWN
    assert catalog.tables == ()
    assert "INVALID_OR_DUPLICATE_COLUMN" in catalog.limitations


def test_duplicate_casefold_fk_target_members_are_dropped() -> None:
    catalog = parse_schema_catalog({
        "tables": [
            {"name": "parent", "columns": ["ID"], "primary_key": ["ID"]},
            {
                "name": "child",
                "columns": ["left_id", "right_id"],
                "foreign_keys": [{
                    "columns": ["left_id", "right_id"],
                    "references_table": "parent",
                    "references_columns": ["ID", "id"],
                }],
            },
        ]
    })

    assert catalog.foreign_keys_from("child") == ()
    assert "INVALID_FOREIGN_KEY_DROPPED" in catalog.limitations


def test_one_oversized_table_rejects_the_catalog_instead_of_using_partial_facts():
    catalog = parse_schema_catalog({
        "tables": [
            {"name": "safe", "columns": ["id"]},
            {
                "name": "oversized",
                "columns": [
                    f"column_{index}" for index in range(MAX_COLUMNS_PER_TABLE + 1)
                ],
            },
        ]
    })

    assert catalog.confidence is SchemaConfidence.UNKNOWN
    assert catalog.tables == ()
    assert catalog.limitations == ("SCHEMA_LIMIT_EXCEEDED",)


def test_public_facts_drop_constraints_that_reference_hidden_columns_or_tables():
    catalog = parse_schema_catalog({
        "tables": [
            {
                "name": "parent",
                "columns": ["a_id", "z_hidden"],
                "primary_key": ["a_id"],
            },
            {
                "name": "child",
                "columns": ["a_parent", "z_hidden_ref"],
                "unique_constraints": [["a_parent", "z_hidden_ref"]],
                "foreign_keys": [
                    {
                        "column": "a_parent",
                        "references_table": "parent",
                        "references_column": "a_id",
                    },
                    {
                        "column": "z_hidden_ref",
                        "references_table": "parent",
                        "references_column": "z_hidden",
                    },
                ],
            },
        ]
    })

    public = catalog.public_facts(max_tables=2, max_columns_per_table=1)
    child = next(item for item in public["tables"] if item["name"] == "child")
    assert child["columns"][0]["name"] == "a_parent"
    assert child["unique_constraints"] == []
    assert child["foreign_keys"] == [{
        "columns": ["a_parent"],
        "references_table": "parent",
        "references_columns": ["a_id"],
    }]
    assert "z_hidden" not in json.dumps(public)
    assert "PUBLIC_SCHEMA_SUMMARY_TRUNCATED" in public["limitations"]


def test_public_facts_do_not_expose_arbitrary_limitation_text_or_oversize_output():
    public = Phase2SchemaCatalog.unknown(
        "SELECT_SECRET_ANSWER_FROM_PRIVATE_TABLE"
    ).public_facts()

    encoded = json.dumps(public, ensure_ascii=False, separators=(",", ":"))
    assert "secret_answer" not in encoded
    assert public["limitations"] == ["SCHEMA_METADATA_UNAVAILABLE"]
    assert len(encoded.encode("utf-8")) <= MAX_PUBLIC_OUTPUT_BYTES


def test_separator_encoded_sql_identifier_is_rejected() -> None:
    catalog = parse_schema_catalog({
        "tables": [{
            "name": "safe",
            "columns": ["id", "SELECT_secret_FROM_private_table"],
        }]
    })

    assert catalog.confidence is SchemaConfidence.UNKNOWN
    assert catalog.tables == ()


def test_spider_constraint_arrays_are_bounded_before_iteration() -> None:
    catalog = parse_schema_catalog({
        "table_names_original": ["student"],
        "column_names_original": [[-1, "*"], [0, "id"]],
        "primary_keys": [1] * 1025,
    })

    assert catalog.confidence is SchemaConfidence.UNKNOWN
    assert catalog.limitations == ("SCHEMA_LIMIT_EXCEEDED",)


def test_maximal_public_summary_never_exceeds_the_byte_ceiling() -> None:
    payload = {
        "tables": [
            {
                "name": f"table_{table_index}",
                "columns": [
                    {
                        "name": f"column_{column_index}_" + "x" * 80,
                        "data_type": "TIMESTAMP WITHOUT TIME ZONE",
                    }
                    for column_index in range(24)
                ],
                "unique_constraints": [
                    [f"column_{column_index}_" + "x" * 80]
                    for column_index in range(24)
                ],
            }
            for table_index in range(8)
        ]
    }
    public = parse_schema_catalog(payload).public_facts(
        max_tables=8,
        max_columns_per_table=24,
    )
    encoded = json.dumps(
        public,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    assert len(encoded) <= MAX_PUBLIC_OUTPUT_BYTES
