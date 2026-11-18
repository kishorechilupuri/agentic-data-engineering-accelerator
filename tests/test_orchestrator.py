"""End-to-end tests for the orchestrator, on the deterministic provider.

This is the test that proves the accelerator actually works: one requirement
in, a complete set of reviewed, tested, quality-checked artifacts out.
"""

import json

import pytest

from agents import STAGE_ORDER, Orchestrator

REQUIREMENT = (
    "Build a curated gold table giving each customer their order count and "
    "total order amount."
)


@pytest.fixture(scope="module")
def manifest(tmp_path_factory):
    """Run the whole SDLC once and share the manifest across the tests."""
    from llm_client import LLMClient
    import tools as tool_module

    run_dir = tmp_path_factory.mktemp("e2e") / "run_test"
    orchestrator = Orchestrator(LLMClient(provider="stub"), run_id="test", run_dir=run_dir)
    result = orchestrator.run_pipeline(REQUIREMENT)
    result["_run_dir"] = run_dir
    tool_module.set_run_directory(run_dir)
    return result


def test_the_run_completes(manifest):
    assert manifest["overall_status"] in ("completed", "completed_with_degradation")


def test_every_stage_ran_and_none_failed(manifest):
    assert set(manifest["stages"]) == set(STAGE_ORDER)
    failed = [
        name for name, stage in manifest["stages"].items()
        if stage["status"] == "failed"
    ]
    assert not failed, f"Stages failed: {failed}"


def test_the_manifest_records_the_run_date_and_time(manifest):
    assert len(manifest["run_date"]) == 10        # YYYY-MM-DD
    assert len(manifest["run_time"]) == 8         # HH:MM:SS
    assert manifest["run_id"] == "test"


def test_the_manifest_records_which_model_served_each_agent(manifest):
    routing = manifest["model_routing"]
    assert routing["mapping"]["tier"] == "deep"
    assert routing["requirements"]["tier"] == "balanced"
    assert all(route["model"] for route in routing.values())


def test_requirements_were_produced(manifest):
    path = manifest["_run_dir"] / "requirements" / "requirements.json"
    spec = json.loads(path.read_text(encoding="utf-8"))
    assert spec["functional_requirements"]


def test_a_mapping_was_produced_for_every_layer(manifest):
    path = manifest["_run_dir"] / "mapping" / "source_to_target_mapping.json"
    mapping = json.loads(path.read_text(encoding="utf-8"))
    assert {item["layer"] for item in mapping["mappings"]} == {"L1", "L2", "L3"}


def test_the_mapping_is_also_available_as_reviewable_csv(manifest):
    path = manifest["_run_dir"] / "mapping" / "source_to_target_mapping.csv"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0].startswith("mapping_id,layer")
    assert len(lines) > 1


def test_a_pipeline_module_was_generated_per_layer(manifest):
    modules = sorted(
        path.name for path in (manifest["_run_dir"] / "pipelines").glob("*.py")
    )
    assert len(modules) >= 3


def test_every_generated_module_passes_static_validation(manifest):
    report = json.loads(
        (manifest["_run_dir"] / "pipelines" / "validation_report.json")
        .read_text(encoding="utf-8")
    )
    invalid = [path for path, result in report.items() if not result["valid"]]
    assert not invalid, f"Invalid modules: {invalid}"


def test_data_quality_rules_were_executed_against_real_data(manifest):
    results = json.loads(
        (manifest["_run_dir"] / "data_quality" / "dq_results.json")
        .read_text(encoding="utf-8")
    )
    assert results, "No dataset was checked"
    for dataset, result in results.items():
        assert result["row_count"] > 0
        assert result["rules_evaluated"] > 0


def test_data_quality_caught_the_seeded_defects(manifest):
    """The landing extracts carry known defects; the gate must notice."""
    results = json.loads(
        (manifest["_run_dir"] / "data_quality" / "dq_results.json")
        .read_text(encoding="utf-8")
    )
    total_failures = sum(result["failed"] for result in results.values())
    assert total_failures >= 1


def test_generated_tests_were_written_and_executed(manifest):
    report = json.loads(
        (manifest["_run_dir"] / "tests" / "pytest_report.json")
        .read_text(encoding="utf-8")
    )
    assert report["exit_code"] == 0, report["output_tail"]
    assert "passed" in report["summary"]


def test_a_code_review_was_produced(manifest):
    review = json.loads(
        (manifest["_run_dir"] / "review" / "code_review.json")
        .read_text(encoding="utf-8")
    )
    assert review["verdict"] in ("approve", "approve_with_comments", "request_changes")
    assert review["modules_reviewed"]


def test_the_run_summary_is_written_for_a_human(manifest):
    summary = (manifest["_run_dir"] / "RUN_SUMMARY.md").read_text(encoding="utf-8")
    assert "# Run test" in summary
    assert "## Stages" in summary
    assert "## Quality gates" in summary


def test_the_manifest_is_persisted(manifest):
    saved = json.loads(
        (manifest["_run_dir"] / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert saved["run_id"] == "test"
    assert saved["artifacts"]


def test_llm_usage_is_accounted_for(manifest):
    assert manifest["llm_stats"]["calls"] > 0
    assert manifest["llm_stats"]["provider"] == "stub"


# ------------------------------------------------------------------ stages
def test_a_subset_of_stages_can_be_run(tmp_path):
    from llm_client import LLMClient

    orchestrator = Orchestrator(
        LLMClient(provider="stub"), run_id="subset", run_dir=tmp_path / "subset"
    )
    result = orchestrator.run_pipeline(REQUIREMENT, stages=["requirements", "mapping"])
    assert set(result["stages"]) == {"requirements", "mapping"}


def test_the_run_stops_when_a_prerequisite_stage_fails(tmp_path, monkeypatch):
    from llm_client import LLMClient

    orchestrator = Orchestrator(
        LLMClient(provider="stub"), run_id="broken", run_dir=tmp_path / "broken"
    )

    def explode(**kwargs):
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(orchestrator.agents["requirements"], "run", explode)
    result = orchestrator.run_pipeline(REQUIREMENT)

    assert result["overall_status"] == "failed"
    assert result["stages"]["requirements"]["status"] == "failed"
    # Downstream stages must not have run on a missing specification.
    assert "mapping" not in result["stages"]


def test_dataset_selection_falls_back_to_the_whole_catalog(tmp_path):
    from llm_client import LLMClient

    orchestrator = Orchestrator(
        LLMClient(provider="stub"), run_id="ds", run_dir=tmp_path / "ds"
    )
    assert orchestrator._datasets(None) == [
        "customer", "sales_order_header", "sales_order_detail",
    ]


def test_dataset_selection_follows_the_l1_mapping(tmp_path):
    from llm_client import LLMClient

    orchestrator = Orchestrator(
        LLMClient(provider="stub"), run_id="ds2", run_dir=tmp_path / "ds2"
    )
    mapping = {"mappings": [
        {"layer": "L1", "target_object": "dea_accelerator.bronze.customer",
         "source_object": "adls://landing/customer.csv"},
    ]}
    assert orchestrator._datasets(mapping) == ["customer"]
