"""Tests for run-scoped logging and the LLM client wrapper."""

import json
import logging

import pytest

import config
import logger_setup
from llm_client import (
    LLMClient,
    add_assistant_message,
    add_tool_results,
    add_user_message,
    message_tool_calls,
)


@pytest.fixture
def isolated_logging(tmp_path, monkeypatch):
    """Configure logging into a temp folder and tear it down afterwards."""
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    previous = dict(logger_setup._RUN_CONTEXT)

    context = logger_setup.configure_logging(run_id="2026-11-19_10-00-00")
    yield context

    root = logging.getLogger(logger_setup.ROOT_LOGGER_NAME)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    logger_setup._RUN_CONTEXT.update(previous)


# --------------------------------------------------------------- logging
def test_the_log_file_is_named_for_the_run(isolated_logging):
    assert isolated_logging["log_path"].name == "run_2026-11-19_10-00-00.log"
    assert isolated_logging["events_path"].name == "run_2026-11-19_10-00-00.events.jsonl"


def test_the_log_header_records_the_run_date_and_time(isolated_logging):
    text = isolated_logging["log_path"].read_text(encoding="utf-8")
    assert "RUN START" in text
    assert "RUN DATE=" in text and "RUN TIME=" in text
    assert "run_id=2026-11-19_10-00-00" in text


def test_log_lines_carry_a_timestamp_level_and_logger_name(isolated_logging):
    logger_setup.get_logger("probe").info("hello from the probe")
    line = [
        line for line in isolated_logging["log_path"].read_text(encoding="utf-8").splitlines()
        if "hello from the probe" in line
    ][0]
    assert "INFO" in line
    assert "dea.probe" in line
    assert line[:4].isdigit()          # the year, i.e. the asctime prefix


def test_new_run_id_is_a_sortable_timestamp():
    run_id = logger_setup.new_run_id()
    assert len(run_id) == 19
    assert run_id[4] == "-" and run_id[10] == "_"


def test_events_are_appended_as_json_lines(isolated_logging):
    logger_setup.log_event("stage_complete", stage="mapping", status="ok")
    logger_setup.log_event("tool_call", tool="get_table_schema", status="ok")

    lines = isolated_logging["events_path"].read_text(encoding="utf-8").strip().splitlines()
    records = [json.loads(line) for line in lines]
    assert [record["event"] for record in records] == ["stage_complete", "tool_call"]
    assert records[0]["stage"] == "mapping"
    assert all(record["run_id"] == "2026-11-19_10-00-00" for record in records)
    assert all(record["ts"] for record in records)


def test_log_run_end_writes_the_closing_banner(isolated_logging):
    logger_setup.log_run_end("completed")
    text = isolated_logging["log_path"].read_text(encoding="utf-8")
    assert "RUN END" in text and "status=completed" in text


def test_get_logger_namespaces_under_the_run_root():
    assert logger_setup.get_logger("tools").name == "dea.tools"
    assert logger_setup.get_logger("dea.tools").name == "dea.tools"


def test_log_event_never_raises_without_a_configured_run(monkeypatch):
    monkeypatch.setitem(logger_setup._RUN_CONTEXT, "events_path", None)
    record = logger_setup.log_event("orphan_event", detail="no run")
    assert record["event"] == "orphan_event"


# ------------------------------------------------------------ message API
def test_add_user_message_appends_a_user_turn():
    messages = []
    add_user_message(messages, "whats 1+1?")
    assert messages == [{"role": "user", "content": "whats 1+1?"}]


def test_add_assistant_message_accepts_a_plain_string():
    messages = []
    add_assistant_message(messages, "2")
    assert messages[0] == {"role": "assistant", "content": "2"}


def test_add_assistant_message_serialises_a_provider_message():
    from stub_llm import StubMessage

    messages = []
    add_assistant_message(messages, StubMessage(content="hello"))
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == "hello"


def test_add_tool_results_extends_the_conversation():
    messages = []
    add_tool_results(messages, [{"role": "tool", "tool_name": "t", "content": "{}"}])
    assert messages[0]["role"] == "tool"


def test_message_tool_calls_handles_every_shape():
    from stub_llm import StubMessage

    assert message_tool_calls(None) == []
    assert message_tool_calls([]) == []
    assert message_tool_calls(StubMessage()) == []
    assert message_tool_calls({"tool_calls": [{"function": {"name": "t"}}]}) == [
        {"function": {"name": "t"}}
    ]


# -------------------------------------------------------------- LLM client
def test_the_client_resolves_a_model_per_tier():
    client = LLMClient(provider="stub")
    assert client.model_for(config.TIER_DEEP) == "stub-deep"
    assert client.model_for(config.TIER_FAST) == "stub-fast"


def test_the_client_returns_text_and_the_raw_message():
    client = LLMClient(provider="stub")
    messages = []
    add_user_message(messages, "hello")
    text, message = client.chat(
        messages, system="[AGENT:orchestrator]", tier=config.TIER_FAST
    )
    assert isinstance(text, str) and text
    assert message.content == text


def test_the_system_prompt_does_not_mutate_the_caller_list():
    client = LLMClient(provider="stub")
    messages = [{"role": "user", "content": "hello"}]
    client.chat(messages, system="[AGENT:orchestrator]", tier=config.TIER_FAST)
    assert len(messages) == 1


def test_the_client_counts_its_calls():
    client = LLMClient(provider="stub")
    for _ in range(3):
        client.chat([{"role": "user", "content": "x"}], tier=config.TIER_FAST)
    assert client.stats()["calls"] == 3
    assert client.stats()["provider"] == "stub"


def test_an_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="Unknown provider"):
        LLMClient(provider="gpt-whatever")


def test_the_anthropic_provider_is_declared_but_not_wired():
    """config routes Opus/Sonnet/Haiku, but this build targets local Ollama."""
    with pytest.raises(NotImplementedError, match="local Ollama runtime"):
        LLMClient(provider="anthropic")
