"""Tests for the agents, run against the deterministic `stub` provider.

Two things are under test here: the output-extraction helpers that salvage
structured data from an 8B model's reply, and each agent's contract with the
orchestrator (status, artifacts, output shape).
"""

import json

import pytest

import config
import tools as tool_module
from agents import (
    STATUS_DEGRADED,
    STATUS_OK,
    AGENT_REGISTRY,
    CodeReviewAgent,
    DataQualityAgent,
    PipelineCodeGenAgent,
    RequirementRefinerAgent,
    SourceToTargetMappingAgent,
    TestGenerationAgent,
)
from agents.base_agent import extract_json, extract_python


# ====================================================================
# Output extraction -- the part that decides whether an 8B model is usable
# ====================================================================
def test_extract_json_reads_a_bare_object():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_reads_a_fenced_object():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_strips_surrounding_prose():
    text = 'Sure! Here is the mapping:\n{"a": 1}\nLet me know if you need more.'
    assert extract_json(text) == {"a": 1}


def test_extract_json_handles_nested_objects():
    payload = {"mappings": [{"layer": "L1", "columns": [{"a": 1}]}]}
    assert extract_json(f"Here you go:\n{json.dumps(payload)}") == payload


def test_extract_json_tolerates_a_trailing_comma():
    assert extract_json('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}


def test_extract_json_ignores_braces_inside_strings():
    assert extract_json('{"pattern": "^[^@]+@[^@]+$", "n": 1}')["n"] == 1


def test_extract_json_wraps_a_bare_array():
    assert extract_json("[1, 2, 3]") == {"items": [1, 2, 3]}


@pytest.mark.parametrize("text", ["", None, "no json at all", "{broken"])
def test_extract_json_returns_none_when_there_is_nothing_to_find(text):
    assert extract_json(text) is None


def test_extract_python_reads_a_fenced_block():
    assert extract_python("```python\nx = 1\n```") == "x = 1"


def test_extract_python_accepts_an_unlabelled_fence():
    assert extract_python("```\nx = 1\n```") == "x = 1"


def test_extract_python_prefers_the_block_that_parses():
    text = "```python\ndef broken(:\n```\n```python\nx = 1\n```"
    assert extract_python(text) == "x = 1"


def test_extract_python_falls_back_to_raw_parseable_text():
    assert extract_python("x = 1\n") == "x = 1"


def test_extract_python_returns_none_for_prose():
    assert extract_python("I would write a Spark job that reads the table.") is None


# ====================================================================
# Agent contracts
# ====================================================================
def test_every_registered_agent_declares_its_metadata():
    for key, agent_class in AGENT_REGISTRY.items():
        assert agent_class.key == key
        assert agent_class.description
        assert agent_class.prompt_name or agent_class.key


def test_every_agent_requests_only_known_tools():
    for agent_class in AGENT_REGISTRY.values():
        for name in agent_class.tool_names:
            assert name in tool_module.TOOL_SCHEMAS, name


def test_agents_resolve_their_model_from_the_tier_table(stub_llm):
    agent = SourceToTargetMappingAgent(stub_llm)
    assert agent.tier == config.TIER_DEEP
    assert agent.model == config.resolve_model("stub", config.TIER_DEEP)


def test_system_prompt_is_tagged_with_the_agent_key(stub_llm):
    system = RequirementRefinerAgent(stub_llm).build_system(requirement="x")
    assert system.startswith("[AGENT:requirement_refiner]")


# ---------------------------------------------------------- stage 1
def test_requirement_agent_produces_a_usable_specification(run_dir, stub_llm):
    result = RequirementRefinerAgent(stub_llm, run_dir).run(
        requirement="Build a gold customer sales summary."
    )
    assert result.status == STATUS_OK
    assert len(result.output["functional_requirements"]) >= 3
    assert "requirements/requirements.json" in result.artifacts
    assert (run_dir / "requirements" / "requirements.md").exists()


def test_requirement_agent_falls_back_when_the_model_rambles(run_dir, stub_llm, monkeypatch):
    monkeypatch.setattr(
        stub_llm, "chat", lambda *a, **k: ("I think you should build a pipeline.", _Msg())
    )
    result = RequirementRefinerAgent(stub_llm, run_dir).run(requirement="x")
    assert result.status == STATUS_DEGRADED
    assert result.notes
    assert result.output["functional_requirements"]


def test_requirement_agent_normalises_missing_ids(stub_llm):
    spec = RequirementRefinerAgent._normalise(
        {"functional_requirements": [{"statement": "do a thing"}]}, "req"
    )
    assert spec["functional_requirements"][0]["id"] == "FR-001"
    assert spec["functional_requirements"][0]["acceptance_criteria"] == []
    assert spec["_original_request"] == "req"


# ---------------------------------------------------------- stage 2
def test_mapping_agent_covers_all_three_layers(run_dir, stub_llm):
    result = SourceToTargetMappingAgent(stub_llm, run_dir).run(requirements={})
    layers = {item["layer"] for item in result.output["mappings"]}
    assert layers == {"L1", "L2", "L3"}
    assert (run_dir / "mapping" / "source_to_target_mapping.csv").exists()


def test_mapping_agent_discards_structurally_broken_objects(stub_llm):
    agent = SourceToTargetMappingAgent(stub_llm)
    cleaned = agent._clean({"mappings": [
        {"layer": "L1", "columns": [{"target_column": "a"}]},   # keep
        {"layer": "L9", "columns": [{"target_column": "b"}]},   # bad layer
        {"layer": "L2", "columns": []},                          # no columns
        {"layer": "L3"},                                         # no columns key
        "not a dict",                                            # not an object
    ]})
    assert len(cleaned) == 1
    assert cleaned[0]["layer"] == "L1"


def test_mapping_agent_supplements_missing_layers(run_dir, stub_llm, monkeypatch):
    """If the model only returns L1 for a table, L2 and L3 are filled in."""
    only_l1 = json.dumps({"mappings": [{
        "layer": "L1", "target_object": "dea_accelerator.bronze.customer",
        "columns": [{"target_column": "CustomerID"}],
    }]})
    monkeypatch.setattr(stub_llm, "chat", lambda *a, **k: (only_l1, _Msg()))
    result = SourceToTargetMappingAgent(stub_llm, run_dir).run(
        requirements={"source_tables": ["Sales.Customer"]}
    )
    assert {item["layer"] for item in result.output["mappings"]} == {"L1", "L2", "L3"}
    assert result.status == STATUS_DEGRADED


def test_mapping_agent_maps_one_table_per_request(run_dir, stub_llm, monkeypatch):
    """Each source table gets its own bounded request, plus one for gold."""
    scopes = []

    def capture(messages, system=None, **kwargs):
        scopes.append(system)
        return "not json", _Msg()

    monkeypatch.setattr(stub_llm, "chat", capture)
    SourceToTargetMappingAgent(stub_llm, run_dir).run(
        requirements={"source_tables": ["Sales.Customer", "Sales.SalesOrderHeader"]}
    )

    assert len(scopes) == 3                      # two tables + the gold layer
    assert "`Sales.Customer`" in scopes[0]
    assert "`Sales.SalesOrderHeader`" in scopes[1]
    assert "layer L3 only" in scopes[2]


def test_mapping_agent_covers_every_requested_table(run_dir, stub_llm, monkeypatch):
    monkeypatch.setattr(stub_llm, "chat", lambda *a, **k: ("prose", _Msg()))
    result = SourceToTargetMappingAgent(stub_llm, run_dir).run(
        requirements={"source_tables": ["Sales.Customer", "Sales.SalesOrderHeader"]}
    )
    targets = {item["target_object"] for item in result.output["mappings"]}
    assert "dea_accelerator.bronze.customer" in targets
    assert "dea_accelerator.silver.sales_order_header" in targets


def test_mapping_agent_defaults_the_load_strategy_per_layer(stub_llm):
    cleaned = SourceToTargetMappingAgent(stub_llm)._clean({"mappings": [
        {"layer": "L2", "columns": [{"target_column": "a"}]},
    ]})
    assert cleaned[0]["load_strategy"] == "merge"


# ---------------------------------------------------------- stage 3
def test_codegen_agent_writes_and_validates_a_module_per_mapping(run_dir, stub_llm, sample_mapping):
    result = PipelineCodeGenAgent(stub_llm, run_dir).run(
        mapping={"mappings": [sample_mapping]}
    )
    assert "pipelines/l2_customer.py" in result.artifacts
    assert (run_dir / "pipelines" / "l2_customer.py").exists()
    validation = result.output["validation"]["pipelines/l2_customer.py"]
    assert validation["valid"]


def test_codegen_agent_falls_back_to_a_template_on_unusable_output(
    run_dir, stub_llm, sample_mapping, monkeypatch
):
    monkeypatch.setattr(stub_llm, "chat", lambda *a, **k: ("I'd write some Spark.", _Msg()))
    result = PipelineCodeGenAgent(stub_llm, run_dir).run(
        mapping={"mappings": [sample_mapping]}
    )
    assert result.status == STATUS_DEGRADED
    code = (run_dir / "pipelines" / "l2_customer.py").read_text(encoding="utf-8")
    assert "MERGE INTO" in code
    assert result.output["validation"]["pipelines/l2_customer.py"]["valid"]


def test_codegen_agent_writes_a_validation_report(run_dir, stub_llm, sample_mapping):
    PipelineCodeGenAgent(stub_llm, run_dir).run(mapping={"mappings": [sample_mapping]})
    report = json.loads(
        (run_dir / "pipelines" / "validation_report.json").read_text(encoding="utf-8")
    )
    assert "pipelines/l2_customer.py" in report


# ---------------------------------------------------------- stage 4
def test_dq_agent_runs_its_rules_against_real_landed_data(run_dir, stub_llm):
    result = DataQualityAgent(stub_llm, run_dir).run(
        mapping={"mappings": []}, datasets=["customer"]
    )
    execution = result.output["execution"]["customer"]
    assert execution["row_count"] == 12
    assert execution["rules_evaluated"] >= 2
    # The landing extract has a duplicate CustomerID, so a rule must fail.
    assert result.output["failed_rule_count"] >= 1
    assert (run_dir / "data_quality" / "dq_report.md").exists()


def test_dq_agent_discards_rules_it_cannot_execute(stub_llm):
    agent = DataQualityAgent(stub_llm)
    cleaned = agent._clean({"rules": [
        {"dataset": "customer", "column": "CustomerID", "check": "not_null"},
        {"dataset": "customer", "column": "X", "check": "benfords_law"},
        {"dataset": "unknown_table", "column": "X", "check": "not_null"},
    ]}, ["customer"])
    assert len(cleaned) == 1
    assert cleaned[0]["check"] == "not_null"


def test_dq_agent_falls_back_when_no_rules_are_usable(run_dir, stub_llm, monkeypatch):
    monkeypatch.setattr(stub_llm, "chat", lambda *a, **k: ("Looks fine to me!", _Msg()))
    result = DataQualityAgent(stub_llm, run_dir).run(
        mapping={"mappings": []}, datasets=["customer"]
    )
    assert result.status == STATUS_DEGRADED
    assert result.output["rules"]
    assert result.output["execution"]["customer"]["rules_evaluated"] > 0


# ---------------------------------------------------------- stage 5
def test_test_agent_generates_and_runs_a_passing_suite(run_dir, stub_llm, sample_mapping):
    result = TestGenerationAgent(stub_llm, run_dir).run(
        mapping={"mappings": [sample_mapping]}
    )
    assert "tests/test_l2_customer.py" in result.artifacts
    assert result.output["execution"]["passed"], result.output["execution"]["output_tail"]


def test_test_agent_rejects_a_suite_that_imports_spark(stub_llm):
    from agents.test_agent import _looks_like_tests

    assert _looks_like_tests("def test_a():\n    assert 1\n")
    assert not _looks_like_tests("import pyspark\ndef test_a():\n    assert 1\n")
    assert not _looks_like_tests("x = 1\n")
    assert not _looks_like_tests("def test_a():\n    assert True\n")


def test_test_agent_falls_back_to_a_deterministic_suite(
    run_dir, stub_llm, sample_mapping, monkeypatch
):
    monkeypatch.setattr(stub_llm, "chat", lambda *a, **k: ("I'd test the mapping.", _Msg()))
    result = TestGenerationAgent(stub_llm, run_dir).run(
        mapping={"mappings": [sample_mapping]}
    )
    code = (run_dir / "tests" / "test_l2_customer.py").read_text(encoding="utf-8")
    assert code.count("def test_") >= 6
    assert result.output["execution"]["passed"], result.output["execution"]["output_tail"]


# ---------------------------------------------------------- stage 6
def test_review_agent_returns_a_verdict_and_findings(run_dir, stub_llm, sample_mapping):
    tool_module.write_artifact(
        "pipelines/l2_customer.py",
        '"""L2 job."""\nfrom pyspark.sql import SparkSession\ndef build(df):\n    return df\n',
    )
    result = CodeReviewAgent(stub_llm, run_dir).run(
        mapping={"mappings": [sample_mapping]},
        module_paths=["pipelines/l2_customer.py"],
    )
    assert result.output["verdict"] in (
        "approve", "approve_with_comments", "request_changes"
    )
    assert (run_dir / "review" / "code_review.md").exists()


def test_review_agent_escalates_a_module_that_fails_validation(run_dir, stub_llm):
    tool_module.write_artifact("pipelines/l1_broken.py", "def broken(:\n    pass\n")
    result = CodeReviewAgent(stub_llm, run_dir).run(
        mapping={"mappings": []}, module_paths=["pipelines/l1_broken.py"]
    )
    assert result.output["verdict"] == "request_changes"
    assert any(
        finding.get("source") == "static_validation"
        and finding["severity"] == "critical"
        for finding in result.output["findings"]
    )


def test_review_agent_normalises_severity_casing(stub_llm):
    review = CodeReviewAgent._normalise({
        "verdict": "approve",
        "findings": [{"summary": "x", "severity": "CRITICAL"}],
    })
    assert review["findings"][0]["severity"] == "critical"


def test_review_agent_drops_findings_with_no_summary(stub_llm):
    review = CodeReviewAgent._normalise({
        "verdict": "approve",
        "findings": [{"severity": "low"}, {"summary": "real", "severity": "low"}],
    })
    assert len(review["findings"]) == 1


class _Msg:
    """A model message carrying no tool calls."""

    tool_calls: list = []

    def model_dump(self, exclude_none=False):
        return {"role": "assistant", "content": ""}
