"""Shared pytest fixtures.

The suite runs entirely offline: no Ollama, no Spark, no network. Anything
that needs a model uses the `stub` provider.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools as tool_module  # noqa: E402
from llm_client import LLMClient  # noqa: E402


@pytest.fixture
def run_dir(tmp_path):
    """Point the artifact tools at an isolated folder for one test."""
    previous = tool_module._RUN_DIR["path"]
    directory = tool_module.set_run_directory(tmp_path / "run")
    yield directory
    tool_module._RUN_DIR["path"] = previous


@pytest.fixture
def stub_llm():
    """A deterministic LLM client that needs no server."""
    return LLMClient(provider="stub")


@pytest.fixture
def sample_mapping():
    """A small but structurally complete mapping object."""
    return {
        "mapping_id": "MAP-L2-CUSTOMER",
        "layer": "L2",
        "source_object": "dea_accelerator.bronze.customer",
        "target_object": "dea_accelerator.silver.customer",
        "business_key": ["customer_id"],
        "load_strategy": "merge",
        "columns": [
            {"source_column": "CustomerID", "target_column": "customer_id",
             "target_type": "int", "transformation": "cast(CustomerID as int)",
             "nullable": False},
            {"source_column": "Email", "target_column": "email",
             "target_type": "string", "transformation": "lower(trim(Email))",
             "nullable": True},
        ],
    }
