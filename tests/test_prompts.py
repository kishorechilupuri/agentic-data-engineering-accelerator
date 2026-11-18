"""Tests for the prompt library.

A prompt whose placeholders are not filled ships `${mapping_json}` straight
to the model, which is a silent, expensive failure. These tests catch it.
"""

import pytest

from agents import AGENT_REGISTRY
from prompts import available_prompts, load_prompt, placeholders

EXPECTED_PROMPTS = {
    "requirement_refiner",
    "sttm_mapper",
    "pipeline_codegen",
    "data_quality",
    "test_generator",
    "code_reviewer",
    "orchestrator",
}


def test_every_expected_prompt_exists():
    assert EXPECTED_PROMPTS.issubset(set(available_prompts()))


def test_every_agent_prompt_name_resolves():
    for agent_class in AGENT_REGISTRY.values():
        name = agent_class.prompt_name or agent_class.key
        assert load_prompt(name).strip(), f"{name} is empty"


def test_loading_an_unknown_prompt_fails_loudly():
    with pytest.raises(FileNotFoundError, match="not found"):
        load_prompt("no_such_prompt")


@pytest.mark.parametrize("name", sorted(EXPECTED_PROMPTS))
def test_prompts_are_substantial(name):
    """Each prompt must actually instruct, not just name a role."""
    assert len(load_prompt(name)) > 400


@pytest.mark.parametrize(
    "name,variables",
    [
        ("requirement_refiner", {"requirement": "R"}),
        ("sttm_mapper", {"requirements_json": "{}", "catalog": "c", "scope": "s"}),
        ("pipeline_codegen", {"layer": "L1", "layer_intent": "i",
                              "mapping_json": "{}", "catalog": "c"}),
        ("data_quality", {"mapping_json": "{}", "datasets": "customer"}),
        ("test_generator", {"layer": "L1", "module_name": "m",
                            "mapping_json": "{}"}),
        ("code_reviewer", {"mapping_json": "{}"}),
        ("orchestrator", {"run_record": "{}"}),
    ],
)
def test_no_placeholder_survives_rendering(name, variables):
    rendered = load_prompt(name, **variables)
    assert "${" not in rendered, f"{name} has an unfilled placeholder"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("requirement_refiner", {"requirement"}),
        ("sttm_mapper", {"requirements_json", "catalog", "scope"}),
        ("pipeline_codegen", {"layer", "layer_intent", "mapping_json", "catalog"}),
        ("data_quality", {"mapping_json", "datasets"}),
        ("test_generator", {"layer", "module_name", "mapping_json"}),
        ("code_reviewer", {"mapping_json"}),
        ("orchestrator", {"run_record"}),
    ],
)
def test_prompt_placeholders_match_what_the_agents_pass(name, expected):
    assert placeholders(name) == expected


def test_structured_prompts_demand_json_only():
    for name in ("requirement_refiner", "sttm_mapper", "data_quality", "code_reviewer"):
        # Normalise line wrapping: the instruction spans a line break in the
        # prompt files, which is a formatting choice, not a contract change.
        body = " ".join(load_prompt(name).split())
        assert "JSON object and nothing else" in body


def test_code_prompts_demand_a_fenced_block():
    for name in ("pipeline_codegen", "test_generator"):
        assert "```python" in load_prompt(name)


def test_test_prompt_forbids_spark_in_generated_tests():
    body = load_prompt("test_generator").lower()
    assert "no spark" in body or "do not import" in body
