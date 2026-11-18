"""Central configuration for the Agentic Data Engineering Accelerator.

Everything that another module might want to tune lives here: filesystem
layout, the local Ollama endpoint, and the model-routing table that maps a
logical reasoning *tier* onto a concrete model id for the active provider.

Why tiers instead of hard-coded model names?
--------------------------------------------
The accelerator is designed to run against Anthropic's Opus / Sonnet / Haiku
family, where each agent is matched to the cheapest model that can still do
its job.  The reference runtime for this project, however, is a *local*
`llama3.1:8b` served by Ollama on http://localhost:11434, so every tier
collapses onto that single local model.  Agents therefore never name a model
directly -- they name a tier, and this table decides what actually gets
called.  Switching the whole fleet to Anthropic is a one-flag change.

No credentials are read or stored here; the local runtime needs none.
"""

from pathlib import Path

# --------------------------------------------------------------------------
# Filesystem layout
# --------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent

PROMPTS_DIR = PROJECT_ROOT / "prompts"
AGENTS_DIR = PROJECT_ROOT / "agents"
SKILLS_DIR = PROJECT_ROOT / "skills"
DATA_DIR = PROJECT_ROOT / "data"
LANDING_DIR = DATA_DIR / "landing"          # simulated ADLS Gen2 landing zone
LOGS_DIR = PROJECT_ROOT / "logs"
OUTPUT_DIR = PROJECT_ROOT / "output"        # one sub-folder per run
TESTS_DIR = PROJECT_ROOT / "tests"

SOURCE_CATALOG_PATH = DATA_DIR / "source_catalog.json"

# --------------------------------------------------------------------------
# Local LLM runtime (the reference runtime for this project)
# --------------------------------------------------------------------------
OLLAMA_HOST = "http://localhost:11434"
LOCAL_MODEL_ID = "llama3.1:8b"

DEFAULT_PROVIDER = "ollama"

# --------------------------------------------------------------------------
# Reasoning tiers
# --------------------------------------------------------------------------
TIER_DEEP = "deep"          # hardest reasoning: mapping, code generation, review
TIER_BALANCED = "balanced"  # structured drafting: requirements, DQ rules, tests
TIER_FAST = "fast"          # cheap summarisation / routing / manifest text

TIERS = (TIER_DEEP, TIER_BALANCED, TIER_FAST)

# Which concrete model serves each tier, per provider.
MODEL_ROUTING = {
    # Reference runtime: one local model serves every tier.
    "ollama": {
        TIER_DEEP: LOCAL_MODEL_ID,
        TIER_BALANCED: LOCAL_MODEL_ID,
        TIER_FAST: LOCAL_MODEL_ID,
    },
    # Cloud runtime: Opus for the hard stages, Sonnet for drafting, Haiku for
    # the cheap ones. Not exercised by the default run; kept so the routing
    # decision is explicit and reviewable.
    "anthropic": {
        TIER_DEEP: "claude-opus-5-5",
        TIER_BALANCED: "claude-sonnet-5-5",
        TIER_FAST: "claude-haiku-5-5",
    },
    # Deterministic offline double used by the unit tests.
    "stub": {
        TIER_DEEP: "stub-deep",
        TIER_BALANCED: "stub-balanced",
        TIER_FAST: "stub-fast",
    },
}

# Decoding settings per tier. Data engineering artefacts must be reproducible,
# so temperature is 0 everywhere; the tiers differ in how much room they get.
TIER_OPTIONS = {
    TIER_DEEP: {"temperature": 0.0, "num_predict": 2048},
    TIER_BALANCED: {"temperature": 0.0, "num_predict": 1400},
    TIER_FAST: {"temperature": 0.0, "num_predict": 600},
}

# --------------------------------------------------------------------------
# Agent -> tier assignment
# --------------------------------------------------------------------------
# Keys match BaseAgent.key for every agent in agents/.
AGENT_MODEL_TIER = {
    "requirement_refiner": TIER_BALANCED,   # Sonnet-class: structured drafting
    "sttm_mapper": TIER_DEEP,               # Opus-class: cross-schema reasoning
    "pipeline_codegen": TIER_DEEP,          # Opus-class: PySpark generation
    "data_quality": TIER_BALANCED,          # Sonnet-class: rule authoring
    "test_generator": TIER_BALANCED,        # Sonnet-class: test authoring
    "code_reviewer": TIER_DEEP,             # Opus-class: defect detection
    "orchestrator": TIER_FAST,              # Haiku-class: run summary only
}

# --------------------------------------------------------------------------
# Medallion layers for the target Databricks pipeline
# --------------------------------------------------------------------------
# SQL Server -> ADF -> ADLS Gen2 -> Databricks L1 -> L2 -> L3
LAYERS = ("L1", "L2", "L3")

LAYER_INTENT = {
    "L1": (
        "Bronze / raw landing. Ingest the ADF-delivered files from ADLS Gen2 "
        "exactly as received. No business transformation. Add audit columns, "
        "keep every source column as a string where the source type is "
        "unreliable, and write an append-only Delta table."
    ),
    "L2": (
        "Silver / conformed. Deduplicate on the business key, cast columns to "
        "their declared target types, trim and standardise text, apply "
        "referential and null handling, and write a Delta table merged on the "
        "business key (SCD type 1 unless the mapping says otherwise)."
    ),
    "L3": (
        "Gold / curated. Build the analytics-ready dimensional or aggregate "
        "model described by the requirement, joining the L2 tables and "
        "producing business measures for BI consumption."
    ),
}

# Default Unity Catalog coordinates used in generated code.
CATALOG = "dea_accelerator"
SCHEMA_BY_LAYER = {"L1": "bronze", "L2": "silver", "L3": "gold"}

# --------------------------------------------------------------------------
# Agent loop guards
# --------------------------------------------------------------------------
MAX_AGENT_TURNS = 6        # LLM <-> tool round trips before an agent gives up
MAX_REPAIR_ATTEMPTS = 1    # retries when generated python fails to parse


def resolve_model(provider: str, tier: str) -> str:
    """Return the concrete model id serving ``tier`` for ``provider``."""
    if provider not in MODEL_ROUTING:
        raise ValueError(
            f"Unknown provider '{provider}'. Known: {sorted(MODEL_ROUTING)}"
        )
    if tier not in MODEL_ROUTING[provider]:
        raise ValueError(f"Unknown tier '{tier}'. Known: {sorted(TIERS)}")
    return MODEL_ROUTING[provider][tier]


def tier_for_agent(agent_key: str) -> str:
    """Return the reasoning tier assigned to ``agent_key``."""
    return AGENT_MODEL_TIER.get(agent_key, TIER_BALANCED)


def options_for_tier(tier: str) -> dict:
    """Return a copy of the decoding options for ``tier``."""
    return dict(TIER_OPTIONS.get(tier, TIER_OPTIONS[TIER_BALANCED]))


def ensure_directories() -> None:
    """Create the directories the accelerator writes into."""
    for directory in (LOGS_DIR, OUTPUT_DIR, LANDING_DIR):
        directory.mkdir(parents=True, exist_ok=True)
