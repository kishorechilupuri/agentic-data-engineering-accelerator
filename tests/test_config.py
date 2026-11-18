"""Tests for the configuration and model-routing layer."""

import pytest

import config


def test_every_provider_covers_every_tier():
    for provider, routing in config.MODEL_ROUTING.items():
        missing = set(config.TIERS) - set(routing)
        assert not missing, f"Provider {provider} is missing tiers {missing}"


def test_local_runtime_targets_llama_31_8b():
    """The reference runtime is the local llama3.1:8b on every tier."""
    for tier in config.TIERS:
        assert config.resolve_model("ollama", tier) == "llama3.1:8b"


def test_ollama_host_is_the_local_endpoint():
    assert config.OLLAMA_HOST == "http://localhost:11434"


def test_cloud_routing_uses_opus_sonnet_and_haiku():
    """The hard stages route to Opus, drafting to Sonnet, summaries to Haiku."""
    anthropic = config.MODEL_ROUTING["anthropic"]
    assert "opus" in anthropic[config.TIER_DEEP]
    assert "sonnet" in anthropic[config.TIER_BALANCED]
    assert "haiku" in anthropic[config.TIER_FAST]


@pytest.mark.parametrize(
    "agent_key,expected_tier",
    [
        ("sttm_mapper", config.TIER_DEEP),
        ("pipeline_codegen", config.TIER_DEEP),
        ("code_reviewer", config.TIER_DEEP),
        ("requirement_refiner", config.TIER_BALANCED),
        ("data_quality", config.TIER_BALANCED),
        ("test_generator", config.TIER_BALANCED),
        ("orchestrator", config.TIER_FAST),
    ],
)
def test_agents_are_routed_to_the_intended_tier(agent_key, expected_tier):
    assert config.tier_for_agent(agent_key) == expected_tier


def test_unknown_agent_falls_back_to_the_balanced_tier():
    assert config.tier_for_agent("not_a_real_agent") == config.TIER_BALANCED


def test_resolve_model_rejects_unknown_provider_and_tier():
    with pytest.raises(ValueError, match="Unknown provider"):
        config.resolve_model("gpt-whatever", config.TIER_DEEP)
    with pytest.raises(ValueError, match="Unknown tier"):
        config.resolve_model("ollama", "galaxy_brain")


def test_decoding_is_deterministic_on_every_tier():
    """Generated artifacts must be reproducible, so temperature stays at 0."""
    for tier in config.TIERS:
        assert config.options_for_tier(tier)["temperature"] == 0.0


def test_options_for_tier_returns_a_copy():
    options = config.options_for_tier(config.TIER_DEEP)
    options["temperature"] = 0.9
    assert config.options_for_tier(config.TIER_DEEP)["temperature"] == 0.0


def test_medallion_layers_are_l1_l2_l3_with_intents():
    assert config.LAYERS == ("L1", "L2", "L3")
    for layer in config.LAYERS:
        assert config.LAYER_INTENT[layer]
        assert layer in config.SCHEMA_BY_LAYER
