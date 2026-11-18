"""Deterministic offline stand-in for the Ollama client.

The unit tests must pass on a machine with no model server running, and CI
must not depend on an 8B model producing well-formed JSON. `StubClient`
mimics the small slice of the Ollama response surface the accelerator uses
(`response.message.content`, `response.message.tool_calls`,
`message.model_dump()`), and returns a canned, schema-valid answer per agent.

Agents tag their system prompt with a `[AGENT:<key>]` marker (see
`agents.base_agent.BaseAgent.system_prompt`), which is how the stub knows
which canned answer to return.
"""

import json
import re
from dataclasses import dataclass, field


@dataclass
class StubMessage:
    role: str = "assistant"
    content: str = ""
    tool_calls: list = field(default_factory=list)

    def model_dump(self, exclude_none: bool = False):
        payload = {"role": self.role, "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = self.tool_calls
        return payload


@dataclass
class StubResponse:
    message: StubMessage


# --------------------------------------------------------------------------
# Canned, schema-valid answers
# --------------------------------------------------------------------------
_REQUIREMENTS = {
    "epic": "Customer sales analytics on the Databricks medallion platform",
    "summary": "Land SQL Server sales data through ADF into ADLS Gen2 and "
               "curate it to a gold customer sales summary.",
    "source_tables": ["Sales.Customer", "Sales.SalesOrderHeader"],
    "target_entities": [
        {"layer": "L1", "name": "customer", "grain": "one row per landed source row"},
        {"layer": "L2", "name": "customer", "grain": "one row per CustomerID"},
        {"layer": "L3", "name": "customer_sales_summary", "grain": "one row per CustomerID"},
    ],
    "functional_requirements": [
        {"id": "FR-001", "statement": "Ingest Sales.Customer from ADLS Gen2 into L1 unchanged.",
         "acceptance_criteria": ["L1 row count equals landed file row count"]},
        {"id": "FR-002", "statement": "Deduplicate customers on CustomerID in L2.",
         "acceptance_criteria": ["L2 CustomerID is unique"]},
        {"id": "FR-003", "statement": "Publish total order amount per customer in L3.",
         "acceptance_criteria": ["L3 total_due equals the sum of L2 order amounts"]},
    ],
    "non_functional_requirements": [
        {"id": "NFR-001", "statement": "Each layer is idempotent and re-runnable."}
    ],
    "assumptions": ["ADF delivers one CSV per table per day."],
    "open_questions": ["Is a late-arriving-data window required?"],
}

_MAPPING = {
    "mappings": [
        {
            "mapping_id": "MAP-L1-CUSTOMER",
            "layer": "L1",
            "source_object": "adls://landing/customer.csv",
            "target_object": "dea_accelerator.bronze.customer",
            "business_key": ["CustomerID"],
            "load_strategy": "append",
            "columns": [
                {"source_column": "CustomerID", "target_column": "CustomerID",
                 "target_type": "string", "transformation": "direct", "nullable": False},
                {"source_column": "Email", "target_column": "Email",
                 "target_type": "string", "transformation": "direct", "nullable": True},
            ],
        },
        {
            "mapping_id": "MAP-L2-CUSTOMER",
            "layer": "L2",
            "source_object": "dea_accelerator.bronze.customer",
            "target_object": "dea_accelerator.silver.customer",
            "business_key": ["CustomerID"],
            "load_strategy": "merge",
            "columns": [
                {"source_column": "CustomerID", "target_column": "customer_id",
                 "target_type": "int", "transformation": "cast", "nullable": False},
                {"source_column": "Email", "target_column": "email",
                 "target_type": "string", "transformation": "lower(trim(Email))", "nullable": True},
            ],
        },
        {
            "mapping_id": "MAP-L3-CUSTOMER-SALES",
            "layer": "L3",
            "source_object": "dea_accelerator.silver.customer",
            "target_object": "dea_accelerator.gold.customer_sales_summary",
            "business_key": ["customer_id"],
            "load_strategy": "overwrite",
            "columns": [
                {"source_column": "customer_id", "target_column": "customer_id",
                 "target_type": "int", "transformation": "direct", "nullable": False},
                {"source_column": "TotalDue", "target_column": "total_due",
                 "target_type": "decimal(18,2)", "transformation": "sum", "nullable": True},
            ],
        },
    ]
}

_DQ_RULES = {
    "rules": [
        {"rule_id": "DQ-001", "dataset": "customer", "column": "CustomerID",
         "check": "not_null", "params": {}, "severity": "error",
         "description": "Business key must be present."},
        {"rule_id": "DQ-002", "dataset": "customer", "column": "CustomerID",
         "check": "unique", "params": {}, "severity": "error",
         "description": "Business key must be unique."},
        {"rule_id": "DQ-003", "dataset": "customer", "column": "Email",
         "check": "regex", "params": {"pattern": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"},
         "severity": "warn", "description": "Email should look like an address."},
        {"rule_id": "DQ-004", "dataset": "sales_order_header", "column": "TotalDue",
         "check": "range", "params": {"min": 0}, "severity": "error",
         "description": "Order total cannot be negative."},
    ]
}

_REVIEW = {
    "verdict": "approve_with_comments",
    "findings": [
        {"finding_id": "RV-001", "severity": "medium", "category": "idempotency",
         "layer": "L1", "summary": "Append load has no de-duplication guard on re-run.",
         "recommendation": "Filter the landed batch by ingestion date before appending."},
        {"finding_id": "RV-002", "severity": "low", "category": "readability",
         "layer": "L2", "summary": "Column casts are not centralised.",
         "recommendation": "Drive casts from the mapping dictionary."},
    ],
    "summary": "Pipeline structure is sound; address idempotency before promotion.",
}

_PIPELINE_CODE = '''"""Stub-generated Databricks job."""

from pyspark.sql import SparkSession, functions as F

spark = SparkSession.builder.getOrCreate()


def run():
    df = spark.read.format("delta").load("/mnt/landing/customer")
    df = df.withColumn("ingestion_timestamp", F.current_timestamp())
    df.write.format("delta").mode("append").saveAsTable(
        "dea_accelerator.bronze.customer"
    )
    return df.count()


if __name__ == "__main__":
    run()
'''

_TEST_CODE = '''"""Stub-generated pipeline tests."""

import pytest


def test_business_key_not_null():
    rows = [{"CustomerID": 1}, {"CustomerID": 2}]
    assert all(row["CustomerID"] is not None for row in rows)


def test_business_key_unique():
    rows = [{"CustomerID": 1}, {"CustomerID": 2}]
    keys = [row["CustomerID"] for row in rows]
    assert len(keys) == len(set(keys))
'''

_SUMMARY = (
    "Run completed. Requirements refined, source-to-target mapping produced "
    "for L1/L2/L3, Databricks PySpark jobs generated and syntax-checked, data "
    "quality rules executed against the landing extracts, pytest suites "
    "generated and run, and the generated code reviewed."
)

_CANNED = {
    "requirement_refiner": json.dumps(_REQUIREMENTS, indent=2),
    "sttm_mapper": json.dumps(_MAPPING, indent=2),
    "data_quality": json.dumps(_DQ_RULES, indent=2),
    "code_reviewer": json.dumps(_REVIEW, indent=2),
    "pipeline_codegen": f"```python\n{_PIPELINE_CODE}```",
    "test_generator": f"```python\n{_TEST_CODE}```",
    "orchestrator": _SUMMARY,
}

_AGENT_MARKER = re.compile(r"\[AGENT:([a-z_]+)\]")


class StubClient:
    """Minimal drop-in replacement for `ollama.Client`."""

    def __init__(self):
        self.calls = []

    def chat(self, model=None, messages=None, tools=None, options=None):
        messages = messages or []
        agent_key = self._agent_key(messages)
        self.calls.append({"model": model, "agent": agent_key})
        content = _CANNED.get(agent_key, "{}")
        return StubResponse(StubMessage(content=content))

    @staticmethod
    def _agent_key(messages):
        for message in messages:
            content = message.get("content") or ""
            match = _AGENT_MARKER.search(content)
            if match:
                return match.group(1)
        return "unknown"
