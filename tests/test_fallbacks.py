"""Tests for the deterministic fallback generators.

These matter more than they look: when the 8B model returns prose instead of
JSON, these functions are what the developer actually receives.
"""

import ast
import json

import pytest

import config
from agents import fallbacks


@pytest.mark.parametrize(
    "source,expected",
    [
        ("CustomerID", "customer_id"),
        ("SalesOrderID", "sales_order_id"),
        ("Email", "email"),
        ("StateProvince", "state_province"),
        ("ProductSKU", "product_sku"),
    ],
)
def test_snake_case_conversion(source, expected):
    assert fallbacks.snake(source) == expected


@pytest.mark.parametrize(
    "sql_type,expected",
    [
        ("int", "int"),
        ("bigint", "bigint"),
        ("nvarchar(50)", "string"),
        ("nchar(2)", "string"),
        ("decimal(18,2)", "decimal(18,2)"),
        ("datetime2", "timestamp"),
        ("date", "date"),
        ("bit", "boolean"),
    ],
)
def test_sql_server_types_map_to_spark_types(sql_type, expected):
    assert fallbacks.target_type(sql_type) == expected


# -------------------------------------------------------------- requirements
def test_fallback_requirements_is_catalog_grounded():
    spec = fallbacks.fallback_requirements("Build a gold customer summary.")
    assert "Sales.Customer" in spec["source_tables"]
    assert len(spec["functional_requirements"]) >= 3
    assert {item["layer"] for item in spec["functional_requirements"]} == {"L1", "L2", "L3"}
    assert all(item["acceptance_criteria"] for item in spec["functional_requirements"])


def test_fallback_requirements_declares_itself_as_a_fallback():
    spec = fallbacks.fallback_requirements("x")
    assert spec["_generated_by"] == "fallback"
    assert any(fallbacks.FALLBACK_BANNER in item for item in spec["assumptions"])


# ------------------------------------------------------------------ mapping
def test_fallback_mapping_covers_all_three_layers():
    mapping = fallbacks.fallback_mapping()
    layers = {item["layer"] for item in mapping["mappings"]}
    assert layers == {"L1", "L2", "L3"}


def test_fallback_mapping_uses_the_right_load_strategy_per_layer():
    expected = {"L1": "append", "L2": "merge", "L3": "overwrite"}
    for item in fallbacks.fallback_mapping()["mappings"]:
        assert item["load_strategy"] == expected[item["layer"]]


def test_l1_lands_everything_as_string_with_audit_columns():
    l1 = [m for m in fallbacks.fallback_mapping()["mappings"] if m["layer"] == "L1"][0]
    targets = {column["target_column"] for column in l1["columns"]}
    assert {"source_file", "ingestion_timestamp", "batch_id"}.issubset(targets)
    source_columns = [
        column for column in l1["columns"]
        if column["target_column"] not in {"source_file", "ingestion_timestamp", "batch_id"}
    ]
    assert all(column["target_type"] == "string" for column in source_columns)


def test_l2_renames_to_snake_case_and_applies_real_types():
    l2 = [m for m in fallbacks.fallback_mapping()["mappings"] if m["layer"] == "L2"][0]
    by_target = {column["target_column"]: column for column in l2["columns"]}
    assert "customer_id" in by_target
    assert by_target["customer_id"]["target_type"] == "int"
    assert by_target["email"]["transformation"].startswith("lower(trim")


def test_l2_business_key_is_snake_cased():
    l2 = [m for m in fallbacks.fallback_mapping()["mappings"] if m["layer"] == "L2"][0]
    assert l2["business_key"] == ["customer_id"]


def test_l3_targets_the_gold_schema():
    l3 = [m for m in fallbacks.fallback_mapping()["mappings"] if m["layer"] == "L3"][0]
    assert l3["target_object"].startswith(f"{config.CATALOG}.gold.")
    assert any(column["transformation"] == "sum" for column in l3["columns"])


# -------------------------------------------------------------- pipeline code
@pytest.mark.parametrize("layer", ["L1", "L2", "L3"])
def test_fallback_pipeline_code_is_valid_python(layer):
    mapping = [m for m in fallbacks.fallback_mapping()["mappings"] if m["layer"] == layer][0]
    code = fallbacks.fallback_pipeline_code(layer, mapping)
    ast.parse(code)              # raises if the template is broken


@pytest.mark.parametrize("layer", ["L1", "L2", "L3"])
def test_fallback_pipeline_code_passes_the_project_validator(layer):
    import tools as tool_module

    mapping = [m for m in fallbacks.fallback_mapping()["mappings"] if m["layer"] == layer][0]
    result = tool_module.validate_python_code(
        code=fallbacks.fallback_pipeline_code(layer, mapping)
    )
    assert result["valid"], result["errors"]
    assert result["warnings"] == [], result["warnings"]


def test_l1_code_guards_its_append_for_idempotency():
    mapping = [m for m in fallbacks.fallback_mapping()["mappings"] if m["layer"] == "L1"][0]
    code = fallbacks.fallback_pipeline_code("L1", mapping)
    assert "DELETE FROM" in code and "batch_id" in code
    assert 'mode("append")' in code


def test_l2_code_merges_on_the_business_key():
    mapping = [m for m in fallbacks.fallback_mapping()["mappings"] if m["layer"] == "L2"][0]
    code = fallbacks.fallback_pipeline_code("L2", mapping)
    assert "MERGE INTO" in code
    assert "target.customer_id = source.customer_id" in code


def test_l2_code_deduplicates_before_merging():
    mapping = [m for m in fallbacks.fallback_mapping()["mappings"] if m["layer"] == "L2"][0]
    code = fallbacks.fallback_pipeline_code("L2", mapping)
    assert "row_number" in code and "Window.partitionBy" in code


def test_l2_keeps_the_column_its_dedup_window_orders_by():
    """Dropping ingestion_timestamp in the select would fail at runtime."""
    mapping = [m for m in fallbacks.fallback_mapping()["mappings"] if m["layer"] == "L2"][0]
    code = fallbacks.fallback_pipeline_code("L2", mapping)

    select_block = code.split("typed = bronze_df.select(")[1].split("    )")[0]
    assert 'alias("ingestion_timestamp")' in select_block

    window_block = code.split("window = Window.partitionBy")[1].split(")")[0:3]
    assert "ingestion_timestamp" in "".join(window_block)


def test_l3_code_overwrites_cleanly():
    mapping = [m for m in fallbacks.fallback_mapping()["mappings"] if m["layer"] == "L3"][0]
    code = fallbacks.fallback_pipeline_code("L3", mapping)
    assert 'mode("overwrite")' in code
    assert ".groupBy(" in code and ".agg(" in code


def test_generated_code_uses_logging_not_print():
    for item in fallbacks.fallback_mapping()["mappings"]:
        code = fallbacks.fallback_pipeline_code(item["layer"], item)
        assert "logger." in code
        assert "print(" not in code


def test_generated_code_parameterises_the_batch_id():
    mapping = [m for m in fallbacks.fallback_mapping()["mappings"] if m["layer"] == "L1"][0]
    code = fallbacks.fallback_pipeline_code("L1", mapping)
    assert "dbutils.widgets.get" in code


# ---------------------------------------------------------------- dq rules
def test_fallback_dq_rules_cover_the_key_dimensions():
    rules = fallbacks.fallback_dq_rules()["rules"]
    dimensions = {rule["dimension"] for rule in rules}
    assert {"completeness", "uniqueness", "volume"}.issubset(dimensions)
    assert len(dimensions) >= 4


def test_fallback_dq_rules_protect_every_business_key():
    rules = fallbacks.fallback_dq_rules()["rules"]
    key_rules = {
        (rule["dataset"], rule["check"])
        for rule in rules if rule["check"] in ("not_null", "unique")
    }
    assert ("customer", "not_null") in key_rules
    assert ("customer", "unique") in key_rules


def test_fallback_dq_rules_are_executable_by_the_dq_engine():
    import tools as tool_module

    rules = fallbacks.fallback_dq_rules(["customer"])["rules"]
    result = tool_module.run_data_quality_checks("customer", rules)
    assert result["rules_evaluated"] == len(rules)
    assert all(item["status"] != "skipped" for item in result["results"])


def test_fallback_dq_rules_can_be_filtered_to_one_dataset():
    rules = fallbacks.fallback_dq_rules(["customer"])["rules"]
    assert {rule["dataset"] for rule in rules} == {"customer"}


def test_date_rules_use_the_pattern_the_column_actually_lands_in():
    """A datetime2 column lands with a time part; a date column does not."""
    rules = fallbacks.fallback_dq_rules(["sales_order_header"])["rules"]
    by_column = {rule["column"]: rule for rule in rules if rule["check"] == "date_format"}
    assert by_column["OrderDate"]["params"]["format"] == "%Y-%m-%d"
    assert by_column["ModifiedDate"]["params"]["format"] == "%Y-%m-%d %H:%M:%S"


# ------------------------------------------------------------------- tests
@pytest.mark.parametrize("layer", ["L1", "L2", "L3"])
def test_fallback_tests_are_valid_python(layer):
    mapping = [m for m in fallbacks.fallback_mapping()["mappings"] if m["layer"] == layer][0]
    ast.parse(fallbacks.fallback_tests(layer, mapping))


def test_fallback_tests_define_real_assertions():
    mapping = [m for m in fallbacks.fallback_mapping()["mappings"] if m["layer"] == "L2"][0]
    code = fallbacks.fallback_tests("L2", mapping)
    assert code.count("def test_") >= 6
    assert "assert True" not in code


def test_fallback_tests_import_nothing_heavy():
    mapping = [m for m in fallbacks.fallback_mapping()["mappings"] if m["layer"] == "L2"][0]
    code = fallbacks.fallback_tests("L2", mapping).lower()
    for banned in ("import pyspark", "import pandas", "import delta"):
        assert banned not in code


# ------------------------------------------------------------------ review
def test_fallback_review_escalates_validation_errors():
    review = fallbacks.fallback_review({
        "pipelines/l1_customer.py": {
            "errors": ["SyntaxError line 3: bad"], "warnings": [],
        }
    })
    assert review["verdict"] == "request_changes"
    assert review["findings"][0]["severity"] == "critical"


def test_fallback_review_treats_warnings_as_comments():
    review = fallbacks.fallback_review({
        "pipelines/l2_customer.py": {"errors": [], "warnings": ["No docstring."]}
    })
    assert review["verdict"] == "approve_with_comments"
    assert review["findings"][0]["severity"] == "low"
    assert review["findings"][0]["layer"] == "L2"


def test_fallback_review_handles_an_empty_input():
    review = fallbacks.fallback_review({})
    assert review["findings"] == []
    assert review["verdict"] == "approve_with_comments"
