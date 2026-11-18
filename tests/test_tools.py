"""Tests for the tool layer -- discovery, profiling, artifacts, validation,
data quality, pytest execution and the dispatcher."""

import json

import pytest

import tools as tool_module


# ------------------------------------------------------------------ schemas
def test_every_tool_has_a_schema_and_every_schema_has_a_tool():
    assert set(tool_module.TOOL_FUNCTIONS) == set(tool_module.TOOL_SCHEMAS)


def test_schemas_are_well_formed():
    for name, schema in tool_module.TOOL_SCHEMAS.items():
        assert schema["type"] == "function"
        function = schema["function"]
        assert function["name"] == name
        assert len(function["description"]) > 40, f"{name} needs a real description"
        assert function["parameters"]["type"] == "object"


def test_schemas_for_rejects_an_unknown_tool():
    with pytest.raises(ValueError, match="Unknown tool"):
        tool_module.schemas_for(["get_current_datetime", "summon_a_dragon"])


# ---------------------------------------------------------------- discovery
def test_get_current_datetime_honours_the_format():
    assert len(tool_module.get_current_datetime("%Y-%m-%d")) == 10
    assert ":" in tool_module.get_current_datetime("%H:%M:%S")


def test_list_source_tables_returns_the_sql_server_catalog():
    result = tool_module.list_source_tables()
    names = [table["table_name"] for table in result["tables"]]
    assert result["table_count"] == len(names) >= 3
    assert "Sales.Customer" in names


@pytest.mark.parametrize(
    "lookup", ["Sales.Customer", "Customer", "customer"]
)
def test_get_table_schema_accepts_every_name_form(lookup):
    table = tool_module.get_table_schema(lookup)
    assert table["table_name"] == "Sales.Customer"
    assert table["business_key"] == ["CustomerID"]
    assert any(column["name"] == "Email" for column in table["columns"])


def test_get_table_schema_rejects_an_unknown_table():
    with pytest.raises(ValueError, match="not in catalog"):
        tool_module.get_table_schema("Sales.Unicorn")


# ---------------------------------------------------------------- profiling
def test_profile_landing_dataset_reports_real_shape():
    profile = tool_module.profile_landing_dataset("customer", sample_rows=2)
    assert profile["row_count"] == 12
    assert len(profile["sample"]) == 2

    by_column = {item["column"]: item for item in profile["columns"]}
    # CustomerID 1001 is deliberately duplicated in the landing extract.
    assert by_column["CustomerID"]["distinct_count"] == 11
    # One customer has a blank email, one has a malformed one.
    assert by_column["Email"]["null_or_blank_count"] == 1
    assert by_column["CustomerID"]["inferred_type"] == "integer"


def test_profile_landing_dataset_rejects_an_unknown_dataset():
    with pytest.raises(FileNotFoundError, match="No landing extract"):
        tool_module.profile_landing_dataset("unicorn")


# ---------------------------------------------------------------- artifacts
def test_write_then_read_round_trips(run_dir):
    tool_module.write_artifact("pipelines/l1_customer.py", "print('hi')\n")
    assert tool_module.read_artifact("pipelines/l1_customer.py")["content"] == "print('hi')\n"


def test_write_artifact_serialises_non_strings(run_dir):
    tool_module.write_artifact("mapping/sttm.json", {"mappings": []})
    content = tool_module.read_artifact("mapping/sttm.json")["content"]
    assert json.loads(content) == {"mappings": []}


def test_read_artifact_rejects_a_missing_file(run_dir):
    with pytest.raises(FileNotFoundError):
        tool_module.read_artifact("nope.txt")


def test_artifact_paths_cannot_escape_the_run_folder(run_dir):
    with pytest.raises(ValueError, match="escapes the run directory"):
        tool_module.write_artifact("../../escaped.txt", "nope")


def test_list_artifacts_sees_what_was_written(run_dir):
    tool_module.write_artifact("pipelines/l1_customer.py", "x = 1\n")
    tool_module.write_artifact("pipelines/l2_customer.py", "y = 2\n")
    listing = tool_module.list_artifacts("pipelines")
    assert listing["count"] == 2
    assert {item["path"] for item in listing["artifacts"]} == {
        "pipelines/l1_customer.py",
        "pipelines/l2_customer.py",
    }


# --------------------------------------------------------------- validation
def test_validate_python_code_accepts_a_plausible_spark_job():
    code = (
        '"""L1 job."""\n'
        "from pyspark.sql import SparkSession\n"
        "def build(df):\n"
        "    return df.withColumn('a', df.b)\n"
    )
    result = tool_module.validate_python_code(code=code)
    assert result["valid"]
    assert result["errors"] == []
    assert "build" in result["functions"]


def test_validate_python_code_reports_a_syntax_error():
    result = tool_module.validate_python_code(code="def broken(:\n    pass\n")
    assert not result["valid"]
    assert "SyntaxError" in result["errors"][0]


@pytest.mark.parametrize(
    "snippet",
    ["os.system('rm -rf /')", "eval('1+1')", "exec('x=1')", "subprocess.run(['ls'])"],
)
def test_validate_python_code_blocks_forbidden_constructs(snippet):
    code = f'"""Doc."""\nimport os\ndef f(spark):\n    {snippet}\n'
    result = tool_module.validate_python_code(code=code)
    assert not result["valid"]
    assert any("Forbidden construct" in error for error in result["errors"])


def test_validate_python_code_warns_when_it_is_not_a_spark_job():
    result = tool_module.validate_python_code(code='"""Doc."""\ndef f():\n    return 1\n')
    assert result["valid"]
    assert any("Spark" in warning for warning in result["warnings"])


def test_validate_python_code_needs_an_input():
    with pytest.raises(ValueError, match="Provide either"):
        tool_module.validate_python_code()


def test_validate_python_code_can_read_an_artifact(run_dir):
    tool_module.write_artifact("pipelines/x.py", '"""Doc."""\nspark = 1\n')
    assert tool_module.validate_python_code(artifact_path="pipelines/x.py")["valid"]


# ------------------------------------------------------------ data quality
def _rule(rule_id, column, check, params=None, severity="error", dataset="customer"):
    return {
        "rule_id": rule_id, "dataset": dataset, "column": column,
        "check": check, "params": params or {}, "severity": severity,
        "description": "test rule",
    }


def test_dq_detects_the_duplicate_business_key():
    result = tool_module.run_data_quality_checks(
        "customer", [_rule("DQ-001", "CustomerID", "unique")]
    )
    assert result["results"][0]["status"] == "fail"
    assert result["results"][0]["failed_count"] == 1
    assert result["overall_status"] == "fail"


def test_dq_detects_the_blank_email():
    result = tool_module.run_data_quality_checks(
        "customer", [_rule("DQ-002", "Email", "not_null")]
    )
    assert result["results"][0]["failed_count"] == 1


def test_dq_detects_the_malformed_email_as_a_warning():
    result = tool_module.run_data_quality_checks(
        "customer",
        [_rule("DQ-003", "Email", "regex",
               {"pattern": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"}, severity="warn")],
    )
    assert result["results"][0]["failed_count"] == 1
    # A warn-severity failure must not block the pipeline.
    assert result["overall_status"] == "warn"


def test_dq_passes_a_clean_rule():
    result = tool_module.run_data_quality_checks(
        "customer", [_rule("DQ-004", "CustomerID", "not_null")]
    )
    assert result["overall_status"] == "pass"


def test_dq_detects_the_negative_order_total():
    result = tool_module.run_data_quality_checks(
        "sales_order_header",
        [_rule("DQ-005", "TotalDue", "range", {"min": 0},
               dataset="sales_order_header")],
    )
    assert result["results"][0]["failed_count"] == 1


def test_dq_detects_the_out_of_domain_order_status():
    result = tool_module.run_data_quality_checks(
        "sales_order_header",
        [_rule("DQ-006", "OrderStatus", "allowed_values",
               {"values": ["Open", "Shipped", "Cancelled", "Returned"]},
               dataset="sales_order_header")],
    )
    assert result["results"][0]["failed_count"] == 1    # the 'Pending' row


def test_dq_detects_the_orphan_customer_reference():
    result = tool_module.run_data_quality_checks(
        "sales_order_header",
        [_rule("DQ-007", "CustomerID", "referential_integrity",
               {"reference_dataset": "customer", "reference_column": "CustomerID"},
               dataset="sales_order_header")],
    )
    assert result["results"][0]["failed_count"] == 1    # CustomerID 9999


def test_dq_detects_the_negative_order_quantity():
    result = tool_module.run_data_quality_checks(
        "sales_order_detail",
        [_rule("DQ-008", "OrderQty", "range", {"min": 1},
               dataset="sales_order_detail")],
    )
    assert result["results"][0]["failed_count"] == 1


def test_dq_row_count_rule_passes_on_a_populated_extract():
    result = tool_module.run_data_quality_checks(
        "customer", [_rule("DQ-009", "", "row_count_min", {"min": 5})]
    )
    assert result["results"][0]["status"] == "pass"


def test_dq_flags_a_rule_pointing_at_a_missing_column():
    result = tool_module.run_data_quality_checks(
        "customer", [_rule("DQ-010", "NoSuchColumn", "not_null")]
    )
    assert result["results"][0]["status"] == "fail"
    assert "not present" in result["results"][0]["detail"]


def test_dq_skips_an_unsupported_check():
    result = tool_module.run_data_quality_checks(
        "customer", [_rule("DQ-011", "CustomerID", "benfords_law")]
    )
    assert result["results"][0]["status"] == "skipped"


def test_dq_accepts_rules_as_a_json_string_or_wrapper():
    rules = [_rule("DQ-012", "CustomerID", "not_null")]
    as_string = tool_module.run_data_quality_checks("customer", json.dumps(rules))
    as_wrapper = tool_module.run_data_quality_checks("customer", {"rules": rules})
    assert as_string["rules_evaluated"] == as_wrapper["rules_evaluated"] == 1


def test_dq_ignores_rules_aimed_at_another_dataset():
    result = tool_module.run_data_quality_checks(
        "customer",
        [_rule("DQ-013", "TotalDue", "not_null", dataset="sales_order_header")],
    )
    assert result["rules_evaluated"] == 0


# ------------------------------------------------------------------ pytest
def test_run_pytest_reports_a_passing_suite(run_dir):
    tool_module.write_artifact(
        "generated/test_ok.py", "def test_truth():\n    assert 1 + 1 == 2\n"
    )
    result = tool_module.run_pytest("generated")
    assert result["passed"]
    assert result["exit_code"] == 0
    assert "passed" in result["summary"]


def test_run_pytest_reports_a_failing_suite(run_dir):
    tool_module.write_artifact(
        "generated/test_bad.py", "def test_lies():\n    assert 1 == 2\n"
    )
    result = tool_module.run_pytest("generated")
    assert not result["passed"]
    assert result["exit_code"] != 0


def test_run_pytest_rejects_a_missing_target(run_dir):
    with pytest.raises(FileNotFoundError):
        tool_module.run_pytest("no/such/path")


# -------------------------------------------------------------- dispatcher
class _Call:
    def __init__(self, name, arguments):
        self.function = type("F", (), {"name": name, "arguments": arguments})()


class _Message:
    def __init__(self, calls):
        self.tool_calls = calls


def test_run_tools_executes_calls_and_returns_tool_messages():
    message = _Message([_Call("get_current_datetime", {"date_format": "%Y"})])
    results = tool_module.run_tools(message)
    assert len(results) == 1
    assert results[0]["role"] == "tool"
    assert results[0]["tool_name"] == "get_current_datetime"
    assert len(json.loads(results[0]["content"])) == 4     # a four-digit year


def test_run_tools_accepts_json_string_arguments():
    message = _Message([_Call("get_current_datetime", '{"date_format": "%Y"}')])
    assert tool_module.run_tools(message)[0]["tool_name"] == "get_current_datetime"


def test_run_tools_returns_errors_to_the_model_instead_of_raising():
    message = _Message([_Call("get_table_schema", {"table_name": "Nope"})])
    results = tool_module.run_tools(message)
    assert results[0]["content"].startswith("Error:")


def test_run_tools_handles_a_message_with_no_calls():
    assert tool_module.run_tools(_Message([])) == []
    assert tool_module.run_tools([]) == []


def test_run_tool_rejects_an_unknown_name():
    with pytest.raises(ValueError, match="Unknown tool name"):
        tool_module.run_tool("summon_a_dragon", {})


def test_batch_tool_runs_several_tools_in_one_turn():
    results = tool_module.batch_tool([
        {"name": "get_current_datetime", "arguments": '{"date_format": "%Y"}'},
        {"name": "list_source_tables", "arguments": "{}"},
    ])
    assert len(results) == 2
    assert results[1]["result"]["table_count"] >= 3


def test_batch_tool_isolates_a_failing_invocation():
    results = tool_module.batch_tool([
        {"name": "get_current_datetime", "arguments": "{}"},
        {"name": "get_table_schema", "arguments": '{"table_name": "Nope"}'},
    ])
    assert "result" in results[0]
    assert "error" in results[1]
