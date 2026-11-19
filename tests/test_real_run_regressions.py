"""Regressions pinned from the first real llama3.1:8b end-to-end run.

Every test here corresponds to something that actually happened, recorded in
`evidence/run_2026-11-19_02-58-25/` and written up in EVIDENCE.md. The theme
is the same each time: the pipeline reported success while the artifact was
broken, because the check behind it was too weak.
"""

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import config
import tools as tool_module
from agents import Orchestrator, fallbacks
from agents.review_agent import CodeReviewAgent
from llm_client import LLMClient

EVIDENCE = (
    Path(__file__).resolve().parents[1] / "evidence" / "run_2026-11-19_02-58-25"
)


# ======================================================================
# 1. DQ tool crashed on a regex the model wrote with a single backslash
# ======================================================================
def test_tolerant_loader_accepts_a_regex_with_a_single_backslash():
    """The run lost every DQ rule to `Invalid \\escape` on a pattern like \\s."""
    text = r'{"pattern": "^[^@\s]+@[^@\s]+\.[^@\s]+$"}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)
    assert tool_module._loads_tolerant(text)["pattern"] == r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def test_tolerant_loader_leaves_valid_json_untouched():
    assert tool_module._loads_tolerant(r'{"a": "line\nbreak", "b": "\\d"}') == {
        "a": "line\nbreak",
        "b": "\\d",
    }


def test_tolerant_loader_still_rejects_genuinely_broken_json():
    with pytest.raises(json.JSONDecodeError):
        tool_module._loads_tolerant("{not json at all")


def test_dq_tool_accepts_a_rule_string_of_the_shape_that_crashed_the_run():
    # The log records the error, not the string, so this reconstructs its shape.
    rules = (
        '[{"rule_id": "DQ-004", "dataset": "customer", "column": "Email", '
        r'"check": "regex", "params": {"pattern": "^[^@\s]+@[^@\s]+\.[^@\s]+$"}, '
        '"severity": "warn"}]'
    )
    result = tool_module.run_data_quality_checks("customer", rules)
    assert result["rules_evaluated"] == 1
    assert result["results"][0]["failed_count"] == 1      # the malformed email


# ======================================================================
# 2. The code gate passed modules that parse but cannot run
# ======================================================================
def _check(body: str, layer=None, mapping_columns=None) -> dict:
    code = '"""Doc."""\nfrom pyspark.sql import functions as F\n' + body
    return tool_module.validate_python_code(
        code=code, layer=layer, mapping_columns=mapping_columns
    )


def _errors(result) -> str:
    return " | ".join(result["errors"])


def test_gate_rejects_widgets_get_with_a_default_argument():
    result = _check("def f():\n    return dbutils.widgets.get('batch_id', 'x')\n")
    assert not result["valid"] and "exactly one argument" in _errors(result)


def test_gate_accepts_widgets_get_with_one_argument():
    assert _check("def f():\n    return dbutils.widgets.get('batch_id')\n")["valid"]


def test_gate_rejects_a_table_name_passed_to_load():
    result = _check(
        "def f(spark):\n    return spark.read.format('delta')"
        ".load('dea_accelerator.bronze.customer')\n"
    )
    assert "spark.table" in _errors(result)


def test_gate_accepts_a_path_passed_to_load():
    result = _check(
        "def f(spark):\n    return spark.read.format('delta').load('/mnt/landing/customer')\n"
    )
    assert result["valid"], result["errors"]


def test_gate_rejects_dataframe_merge_but_allows_deltatable_merge():
    pandas_style = _check("def f(a, b):\n    return a.merge(b, 'id', 'id')\n")
    assert "DataFrame.merge does not exist" in _errors(pandas_style)

    delta_style = _check(
        "from delta.tables import DeltaTable\n"
        "def f(spark, src):\n"
        "    t = DeltaTable.forName(spark, 'c.s.t')\n"
        "    t.alias('t').merge(src.alias('s'), 't.id = s.id').execute()\n"
    )
    assert "DataFrame.merge" not in _errors(delta_style)


def test_gate_rejects_a_literal_passed_to_withcolumn():
    result = _check("def f(df):\n    return df.withColumn('batch_id', 'abc')\n")
    assert "F.lit" in _errors(result)


def test_gate_accepts_a_wrapped_literal_in_withcolumn():
    assert _check("def f(df):\n    return df.withColumn('batch_id', F.lit('abc'))\n")["valid"]


def test_gate_rejects_duplicate_columns_in_a_select():
    result = _check("def f(df):\n    return df.select('customer_id', 'customer_id')\n")
    assert "more than once" in _errors(result)


def test_gate_rejects_columns_that_are_in_neither_catalog_nor_mapping():
    """The run's L3 module read F.col('COUNT(*)') and F.col('SUM(SalesAmount)')."""
    result = _check(
        "def f(df):\n"
        "    return df.select(F.col('COUNT(*)'), F.col('SUM(SalesAmount)'))\n"
    )
    assert not result["valid"]
    assert "COUNT(*)" in _errors(result) and "SalesAmount" in _errors(result)


def test_gate_accepts_catalog_columns_in_any_spelling():
    result = _check(
        "def f(df):\n    return df.select(F.col('CustomerID'), F.col('customer_id'), "
        "F.col('c.email'))\n"
    )
    assert result["valid"], result["errors"]


def test_gate_accepts_a_column_the_mapping_declares():
    code = "def f(df):\n    return df.select(F.col('order_count'))\n"
    assert not _check(code)["valid"]
    assert _check(code, mapping_columns=["order_count"])["valid"]


def test_gate_warns_on_a_write_with_no_explicit_mode():
    result = _check("def f(df):\n    df.write.format('delta').saveAsTable('c.s.t')\n")
    assert any("no explicit mode" in warning for warning in result["warnings"])


def test_gate_does_not_warn_when_the_mode_is_set():
    result = _check(
        "def f(df):\n    df.write.format('delta').mode('overwrite').saveAsTable('c.s.t')\n"
    )
    assert not any("no explicit mode" in warning for warning in result["warnings"])


def test_gate_warns_when_the_job_runs_on_import():
    result = _check("def load():\n    return 1\n\nload()\n")
    assert any("runs on import" in warning for warning in result["warnings"])


def test_gate_is_satisfied_by_a_main_guard():
    result = _check("def load():\n    return 1\n\nif __name__ == '__main__':\n    load()\n")
    assert not any("runs on import" in warning for warning in result["warnings"])


# ------------------------------------------------------ layer contracts
def test_l1_must_append():
    code = "def f(df):\n    df.write.mode('overwrite').saveAsTable('c.b.t')\n"
    assert "L1 must append" in _errors(_check(code, layer="L1"))
    assert _check(code.replace("overwrite", "append"), layer="L1")["valid"]


def test_l2_must_deduplicate_or_merge():
    plain = "def f(df):\n    return df.withColumn('a', F.lit(1))\n"
    assert "L2 must deduplicate" in _errors(_check(plain, layer="L2"))

    deduped = (
        "from pyspark.sql import Window\n"
        "def f(df):\n"
        "    w = Window.partitionBy('customer_id').orderBy('ingestion_timestamp')\n"
        "    return df.withColumn('r', F.row_number().over(w))\n"
    )
    assert _check(deduped, layer="L2")["valid"]

    merged = "def f(spark):\n    spark.sql('MERGE INTO c.s.t USING s ON t.id = s.id')\n"
    assert _check(merged, layer="L2")["valid"]


def test_l3_must_not_append():
    """Appending to gold double counts on a re-run, which the requirement forbids."""
    code = "def f(df):\n    df.write.mode('append').saveAsTable('c.g.t')\n"
    assert "L3 must overwrite" in _errors(_check(code, layer="L3"))
    assert _check(code.replace("append", "overwrite"), layer="L3")["valid"]


def test_layer_contracts_only_apply_when_a_layer_is_given():
    assert _check("def f(df):\n    return df\n")["valid"]


@pytest.mark.parametrize("layer", ["L1", "L2", "L3"])
def test_the_deterministic_templates_still_pass_the_stricter_gate(layer):
    for mapping in fallbacks.fallback_mapping()["mappings"]:
        if mapping["layer"] != layer:
            continue
        result = tool_module.validate_python_code(
            code=fallbacks.fallback_pipeline_code(layer, mapping),
            layer=layer,
            mapping_columns=[c["target_column"] for c in mapping["columns"]],
        )
        assert result["valid"], result["errors"]
        assert result["warnings"] == [], result["warnings"]


# --------------------------------------------- the real modules from the run
@pytest.mark.skipif(not EVIDENCE.exists(), reason="evidence folder absent")
@pytest.mark.parametrize(
    "module,layer",
    [("l1_customer", "L1"), ("l2_customer", "L2"), ("l3_customer_orders", "L3")],
)
def test_the_real_model_modules_that_once_passed_are_now_rejected(module, layer):
    """llama3.1:8b wrote these and the AST-only gate approved every one.

    They parse as Python, and none of them can run on Databricks.
    """
    code = (EVIDENCE / "pipelines" / f"{module}.py").read_text(encoding="utf-8")
    ast.parse(code)                              # the old gate's entire test
    result = tool_module.validate_python_code(code=code, layer=layer)
    assert not result["valid"], f"{module} should be rejected"
    assert result["errors"]


@pytest.mark.skipif(not EVIDENCE.exists(), reason="evidence folder absent")
def test_the_real_l3_module_is_rejected_for_the_double_counting_it_would_cause():
    code = (EVIDENCE / "pipelines" / "l3_customer_orders.py").read_text(encoding="utf-8")
    result = tool_module.validate_python_code(code=code, layer="L3")
    assert "L3 must overwrite" in _errors(result)


# ======================================================================
# 3. Generated tests failed: the business key was not a target column
# ======================================================================
def _model_shaped_mapping(layer: str) -> dict:
    """The shape llama3.1:8b produced: source-spelled key, snake_cased columns."""
    return {
        "mapping_id": f"MAP-{layer}-CUSTOMER",
        "layer": layer,
        "target_object": f"dea_accelerator.x.customer_{layer.lower()}",
        "business_key": ["CustomerID"],
        "columns": [
            {"source_column": "CustomerID", "target_column": "customer_id",
             "target_type": "int", "transformation": "cast"},
            {"source_column": "CustomerID", "target_column": "customer_id",
             "target_type": "int", "transformation": "direct"},
            {"source_column": "FirstName", "target_column": "first_name",
             "target_type": "string", "transformation": "trim"},
        ],
    }


def test_test_key_resolves_to_the_target_column_spelling():
    mapping = _model_shaped_mapping("L1")
    assert fallbacks._resolve_test_key(
        mapping["business_key"], mapping["columns"], ["customer_id", "first_name"]
    ) == "customer_id"


def test_test_key_prefers_an_exact_match():
    assert fallbacks._resolve_test_key(["id"], [], ["id", "x"]) == "id"


def test_test_key_falls_back_to_the_first_column_when_nothing_matches():
    assert fallbacks._resolve_test_key(["nope"], [], ["a", "b"]) == "a"


@pytest.mark.parametrize("layer", ["L1", "L2", "L3"])
def test_generated_suite_for_a_model_shaped_mapping_actually_passes(layer, tmp_path):
    """Six of these tests raised KeyError: 'CustomerID' in the real run."""
    (tmp_path / f"test_{layer.lower()}.py").write_text(
        fallbacks.fallback_tests(layer, _model_shaped_mapping(layer)), encoding="utf-8"
    )
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path), "-o", "addopts=", "-q",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stdout[-1500:]


def test_generated_suite_does_not_list_a_duplicated_target_column_twice():
    code = fallbacks.fallback_tests("L3", _model_shaped_mapping("L3"))
    namespace: dict = {}
    exec(compile(code, "<generated>", "exec"), namespace)   # our own template
    assert namespace["EXPECTED_COLUMNS"] == ["customer_id", "first_name"]


def test_generated_suite_asserts_the_key_is_an_output_column():
    code = fallbacks.fallback_tests("L1", _model_shaped_mapping("L1"))
    assert "assert BUSINESS_KEY in EXPECTED_COLUMNS\n" in code
    assert "or EXPECTED_COLUMNS" not in code            # the vacuous version


@pytest.mark.skipif(not EVIDENCE.exists(), reason="evidence folder absent")
def test_the_real_runs_mapping_now_yields_a_passing_generated_suite(tmp_path):
    mapping = json.loads(
        (EVIDENCE / "mapping" / "source_to_target_mapping.json").read_text(encoding="utf-8")
    )
    for item in mapping["mappings"]:
        (tmp_path / f"test_{item['layer'].lower()}.py").write_text(
            fallbacks.fallback_tests(item["layer"], item), encoding="utf-8"
        )
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path), "-o", "addopts=", "-q",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stdout[-1500:]


# ======================================================================
# 4. The pytest summary line vanished (-q from pytest.ini plus ours = -qq)
# ======================================================================
def test_run_pytest_reports_a_summary_even_inside_the_project():
    probe = config.PROJECT_ROOT / "output" / "_summary_probe"
    previous = tool_module._RUN_DIR["path"]
    try:
        tool_module.set_run_directory(probe)
        tool_module.write_artifact(
            "generated/test_ok.py",
            "def test_a():\n    assert 1\n\ndef test_b():\n    assert 2\n",
        )
        result = tool_module.run_pytest("generated", extra_args="-q --no-header")
    finally:
        tool_module._RUN_DIR["path"] = previous
        shutil.rmtree(probe, ignore_errors=True)

    assert result["passed"]
    assert result["summary"] != "no summary line"
    assert "2 passed" in result["summary"]


# ======================================================================
# 5. The run summary was a fabricated tool call, not prose
# ======================================================================
class _Msg:
    tool_calls: list = []

    def model_dump(self, exclude_none=False):
        return {"role": "assistant", "content": ""}


def _manifest(tmp_path, monkeypatch, reply: str) -> dict:
    orchestrator = Orchestrator(
        LLMClient(provider="stub"), run_id="summary", run_dir=tmp_path / "summary"
    )
    monkeypatch.setattr(orchestrator.llm, "chat", lambda *a, **k: (reply, _Msg()))
    return orchestrator.run_pipeline("x", stages=["requirements"])


def test_the_orchestrator_is_offered_no_tools():
    """Offered get_current_datetime, it invented a `get_run_summary` call."""
    from agents.orchestrator import Orchestrator as Cls

    assert Cls.tool_names == ()


@pytest.mark.parametrize(
    "reply",
    [
        '{"name": "get_run_summary", "parameters": {"run_record": "x"}}',
        "[1, 2, 3]",
        "",
    ],
)
def test_a_non_prose_summary_is_replaced_by_the_deterministic_one(tmp_path, monkeypatch, reply):
    manifest = _manifest(tmp_path, monkeypatch, reply)
    assert manifest["summary"].startswith("Run summary finished with status")
    assert "get_run_summary" not in manifest["summary"]


def test_a_prose_summary_is_kept(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path, monkeypatch, "All stages ran. Nothing failed.")
    assert manifest["summary"] == "All stages ran. Nothing failed."


# ======================================================================
# 6. Review findings recorded absolute local paths
# ======================================================================
def test_review_file_paths_are_made_run_relative(tmp_path):
    agent = CodeReviewAgent(LLMClient(provider="stub"), run_dir=tmp_path)
    absolute = str(tmp_path / "pipelines" / "l3_customer_orders.py")
    assert agent._relative(absolute) == "pipelines/l3_customer_orders.py"


def test_review_paths_outside_the_run_folder_are_left_alone(tmp_path):
    agent = CodeReviewAgent(LLMClient(provider="stub"), run_dir=tmp_path)
    assert agent._relative("/elsewhere/file.py") == "/elsewhere/file.py"
    assert agent._relative(None) is None
