from __future__ import annotations

import json

from core.public_schema_preview import (
    MAX_OUTPUT_BYTES,
    sanitize_schema_preview_object,
)


def test_casefold_column_collisions_keep_one_canonical_column_and_constraints() -> None:
    preview = sanitize_schema_preview_object({
        "tables": [
            {
                "name": "Parent",
                "columns": ["ID", {"name": "id", "data_type": "TEXT"}],
                "primary_key": ["id"],
            },
            {
                "name": "Child",
                "columns": ["Parent_ID", "payload"],
                "primary_key": ["parent_id"],
                "unique_constraints": [
                    ["PARENT_ID"],
                    ["parent_id"],
                ],
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

    assert preview is not None
    parent = next(item for item in preview["tables"] if item["name"] == "Parent")
    child = next(item for item in preview["tables"] if item["name"] == "Child")
    assert parent["columns"] == [{"name": "ID"}]
    assert parent["primary_key"] == ["ID"]
    assert child["primary_key"] == ["Parent_ID"]
    assert child["unique_constraints"] == [["Parent_ID"]]
    assert child["foreign_keys"] == [{
        "columns": ["Parent_ID"],
        "references_table": "Parent",
        "references_columns": ["ID"],
    }]


def test_referenced_structural_columns_are_not_redacted_from_authoritative_schema() -> None:
    preview = sanitize_schema_preview_object(
        {
            "tables": [
                {
                    "name": "bookings",
                    "columns": ["bookid", "facid", "slots"],
                    "primary_key": ["bookid"],
                }
            ]
        },
        forbidden_sql=(
            "SELECT facid, SUM(slots) FROM cd.bookings "
            "GROUP BY facid HAVING SUM(slots) > 1000"
        ),
    )

    assert preview is not None
    assert [item["name"] for item in preview["tables"][0]["columns"]] == [
        "bookid",
        "facid",
        "slots",
    ]
    assert preview["tables"][0]["primary_key"] == ["bookid"]


def test_identifier_channel_rejects_separator_encoded_reference_sql() -> None:
    leaked = "SELECT_secret_answer_FROM_private_table"
    preview = sanitize_schema_preview_object({
        "tables": [{
            "name": "safe_table",
            "columns": ["id", leaked],
            "primary_key": [leaked],
            "rows": [{"id": 1, leaked: 99}],
        }]
    })

    assert preview is not None
    encoded = json.dumps(preview, ensure_ascii=False)
    assert leaked not in encoded
    assert preview["tables"][0]["columns"] == [{"name": "id"}]
    assert "primary_key" not in preview["tables"][0]


def test_invalid_or_oversized_constraint_is_dropped_atomically_not_shortened() -> None:
    preview = sanitize_schema_preview_object({
        "tables": [{
            "name": "account",
            "columns": ["id", "tenant_id"],
            "primary_key": ["id", "missing"],
            "unique_constraints": [
                ["id", "ID"],
                ["id", "missing"],
            ],
            "foreign_keys": [{
                "columns": ["id", "missing"],
                "references_table": "account",
                "references_columns": ["id", "tenant_id"],
            }],
        }]
    })

    assert preview is not None
    table = preview["tables"][0]
    assert "primary_key" not in table
    assert "unique_constraints" not in table
    assert "foreign_keys" not in table


def test_public_preview_output_remains_within_frozen_byte_limit() -> None:
    preview = sanitize_schema_preview_object({
        "tables": [
            {
                "name": f"table_{table_index}",
                "columns": [
                    f"column_{table_index}_{column_index}_" + "x" * 80
                    for column_index in range(64)
                ],
                "rows": [
                    {
                        f"column_{table_index}_{column_index}_" + "x" * 80: column_index
                        for column_index in range(64)
                    }
                    for _ in range(8)
                ],
            }
            for table_index in range(16)
        ]
    })

    # The sanitizer may reject an allow-listed payload whose serialized form
    # exceeds the byte cap; it must never return an oversized object.
    if preview is not None:
        encoded = json.dumps(
            preview,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        assert len(encoded) <= MAX_OUTPUT_BYTES
