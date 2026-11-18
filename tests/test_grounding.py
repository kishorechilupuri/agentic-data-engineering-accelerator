"""Tests for the grounding layer.

The smoke run that motivated this module had llama3.1:8b invent
`Sales.Order` and `Sales.OrderItem`, neither of which exists. These tests pin
both halves of the fix: the facts go into the prompt, and whatever the model
says is checked against the catalog afterwards.
"""

import pytest

from agents.grounding import (
    catalog_brief,
    known_columns,
    known_tables,
    landing_brief,
    schema_brief,
    validate_table_names,
)


# ------------------------------------------------------------- injection
def test_catalog_brief_names_every_real_table():
    brief = catalog_brief()
    for table in ("Sales.Customer", "Sales.SalesOrderHeader", "Sales.SalesOrderDetail"):
        assert table in brief


def test_catalog_brief_states_the_closed_world_rule():
    assert "ONLY tables that exist" in catalog_brief()


def test_catalog_brief_carries_keys_types_and_relationships():
    brief = catalog_brief()
    assert "business key: CustomerID" in brief
    assert "nvarchar(255)" in brief
    assert "foreign key: CustomerID -> Sales.Customer.CustomerID" in brief


def test_catalog_brief_can_omit_columns():
    assert "columns:" not in catalog_brief(include_columns=False)


def test_catalog_brief_can_be_scoped_to_a_subset():
    """A scoped run must not show the model tables it may not use."""
    brief = catalog_brief(tables=["Sales.Customer"])
    assert "Sales.Customer" in brief
    assert "SalesOrderHeader" not in brief
    assert "SalesOrderDetail" not in brief


def test_catalog_brief_scope_accepts_any_spelling():
    assert "Sales.SalesOrderHeader" in catalog_brief(tables=["sales_order_header"])


def test_schema_brief_gives_full_column_detail():
    brief = schema_brief(["Sales.Customer"])
    assert "TABLE Sales.Customer" in brief
    assert "StateProvince" in brief
    assert "NOT NULL" in brief
    assert "change tracking: ModifiedDate" in brief


def test_schema_brief_skips_a_table_that_does_not_exist():
    brief = schema_brief(["Sales.Customer", "Sales.Unicorn"])
    assert "Sales.Customer" in brief
    assert "Unicorn" not in brief


def test_schema_brief_handles_an_empty_request():
    assert schema_brief([]) == "(no schemas available)"


def test_landing_brief_reports_the_real_shape_of_the_file():
    brief = landing_brief(["customer"])
    assert "DATASET 'customer'  (12 rows" in brief
    assert "CustomerID" in brief


def test_landing_brief_skips_a_dataset_with_no_extract():
    assert landing_brief(["unicorn"]) == "(no landing profiles available)"


# ------------------------------------------------------------- validation
def test_known_tables_accepts_every_spelling():
    lookup = known_tables()
    for alias in ("sales.customer", "customer", "Customer"):
        assert lookup[alias.lower()] == "Sales.Customer"
    assert lookup["sales_order_header"] == "Sales.SalesOrderHeader"


def test_validate_table_names_separates_real_from_invented():
    recognised, invented = validate_table_names(
        ["Sales.Customer", "Sales.Order", "sales_order_header", "Sales.OrderItem"]
    )
    assert recognised == ["Sales.Customer", "Sales.SalesOrderHeader"]
    assert invented == ["Sales.Order", "Sales.OrderItem"]


def test_validate_table_names_deduplicates_aliases():
    recognised, _ = validate_table_names(["Sales.Customer", "Customer", "customer"])
    assert recognised == ["Sales.Customer"]


@pytest.mark.parametrize("names", [None, [], ["nothing_real"]])
def test_validate_table_names_handles_empty_and_useless_input(names):
    recognised, invented = validate_table_names(names)
    assert recognised == []
    assert invented == list(names or [])


def test_known_columns_returns_the_real_column_set():
    columns = known_columns("Sales.Customer")
    assert "customerid" in columns and "email" in columns
    assert "orderid" not in columns


def test_known_columns_is_empty_for_an_unknown_table():
    assert known_columns("Sales.Unicorn") == set()


# ---------------------------------------------- agent-level integration
def test_requirement_agent_discards_hallucinated_tables(run_dir, stub_llm, monkeypatch):
    """The exact failure seen in the smoke run must now be caught."""
    import json

    from agents import RequirementRefinerAgent

    hallucinated = json.dumps({
        "summary": "Build it.",
        "source_tables": ["Sales.Customer", "Sales.Order", "Sales.OrderItem"],
        "functional_requirements": [
            {"id": "FR-001", "statement": "do it", "acceptance_criteria": ["x"]}
        ],
    })
    monkeypatch.setattr(stub_llm, "chat", lambda *a, **k: (hallucinated, _NoCalls()))

    result = RequirementRefinerAgent(stub_llm, run_dir).run(requirement="x")

    assert result.output["source_tables"] == ["Sales.Customer"]
    assert result.status == "degraded"
    assert any("Sales.Order" in note for note in result.notes)


def test_requirement_agent_keeps_a_fully_valid_table_list(run_dir, stub_llm, monkeypatch):
    import json

    from agents import RequirementRefinerAgent

    valid = json.dumps({
        "summary": "Build it.",
        "source_tables": ["Sales.Customer", "Sales.SalesOrderHeader"],
        "functional_requirements": [
            {"id": "FR-001", "statement": "do it", "acceptance_criteria": ["x"]}
        ],
    })
    monkeypatch.setattr(stub_llm, "chat", lambda *a, **k: (valid, _NoCalls()))

    result = RequirementRefinerAgent(stub_llm, run_dir).run(requirement="x")

    assert result.output["source_tables"] == ["Sales.Customer", "Sales.SalesOrderHeader"]
    assert result.status == "ok"


def test_requirement_agent_is_given_the_catalog_in_its_prompt(run_dir, stub_llm, monkeypatch):
    """Grounding by construction: the facts must reach the model."""
    from agents import RequirementRefinerAgent

    captured = {}

    def capture(messages, **kwargs):
        captured["user"] = messages[0]["content"]
        return '{"summary": "s", "functional_requirements": [{"statement": "x"}]}', _NoCalls()

    monkeypatch.setattr(stub_llm, "chat", capture)
    RequirementRefinerAgent(stub_llm, run_dir).run(requirement="x")

    assert "Sales.SalesOrderHeader" in captured["user"]
    assert "ONLY tables that exist" in captured["user"]


def test_mapping_agent_is_given_schemas_and_landing_profiles(run_dir, stub_llm, monkeypatch):
    from agents import SourceToTargetMappingAgent

    captured = []

    def capture(messages, **kwargs):
        captured.append(messages[0]["content"])
        return "not json", _NoCalls()

    monkeypatch.setattr(stub_llm, "chat", capture)
    SourceToTargetMappingAgent(stub_llm, run_dir).run(
        requirements={"source_tables": ["Sales.Customer"]}
    )

    # The per-table request carries the schema and the landed profile; the
    # gold request carries the silver tables it may join.
    table_request = captured[0]
    assert "TABLE Sales.Customer" in table_request
    assert "DATASET 'customer'" in table_request
    assert "dea_accelerator.silver.customer" in captured[-1]


def test_requirement_agent_enforces_a_scoped_run(run_dir, stub_llm, monkeypatch):
    """Real but out-of-scope tables are dropped, so the run stays bounded."""
    import json

    from agents import RequirementRefinerAgent

    everything = json.dumps({
        "summary": "Build it.",
        "source_tables": ["Sales.Customer", "Sales.SalesOrderHeader",
                          "Sales.SalesOrderDetail"],
        "functional_requirements": [
            {"id": "FR-001", "statement": "do it", "acceptance_criteria": ["x"]}
        ],
    })
    monkeypatch.setattr(stub_llm, "chat", lambda *a, **k: (everything, _NoCalls()))

    result = RequirementRefinerAgent(stub_llm, run_dir).run(
        requirement="x", allowed_tables=["Sales.Customer"]
    )

    assert result.output["source_tables"] == ["Sales.Customer"]
    assert any("outside the requested scope" in note for note in result.notes)


def test_scoped_run_shows_the_model_only_the_tables_in_scope(run_dir, stub_llm, monkeypatch):
    from agents import RequirementRefinerAgent

    captured = {}

    def capture(messages, **kwargs):
        captured["user"] = messages[0]["content"]
        return '{"summary": "s", "functional_requirements": [{"statement": "x"}]}', _NoCalls()

    monkeypatch.setattr(stub_llm, "chat", capture)
    RequirementRefinerAgent(stub_llm, run_dir).run(
        requirement="x", allowed_tables=["Sales.Customer"]
    )

    assert "Sales.Customer" in captured["user"]
    assert "SalesOrderDetail" not in captured["user"]


def test_orchestrator_passes_its_table_scope_to_the_requirement_agent(tmp_path):
    from agents import Orchestrator
    from llm_client import LLMClient

    orchestrator = Orchestrator(
        LLMClient(provider="stub"), run_id="scoped",
        run_dir=tmp_path / "scoped", tables=["Sales.Customer"],
    )
    result = orchestrator.run_pipeline("x", stages=["requirements"])

    assert result["table_scope"] == ["Sales.Customer"]
    spec_tables = orchestrator.agents["requirements"].run(
        requirement="x", allowed_tables=orchestrator.table_scope
    ).output["source_tables"]
    assert spec_tables == ["Sales.Customer"]


def test_an_unscoped_run_still_sees_the_whole_catalog(tmp_path):
    from agents import Orchestrator
    from llm_client import LLMClient

    orchestrator = Orchestrator(
        LLMClient(provider="stub"), run_id="unscoped", run_dir=tmp_path / "unscoped"
    )
    assert orchestrator.table_scope is None
    assert "SalesOrderDetail" in catalog_brief()


class _NoCalls:
    tool_calls: list = []

    def model_dump(self, exclude_none=False):
        return {"role": "assistant", "content": ""}
