# PLAN.md — Agentic Data Engineering Accelerator

The build plan, the decisions taken along the way, and what is left.

---

## Goal

Automate the data-engineering SDLC — requirement refinement, source-to-target
mapping, Databricks L1/L2/L3 code generation, data-quality checks, testing and
code review — with an agentic system running on a **local `llama3.1:8b`**
(Ollama, `http://localhost:11434`), for the platform:

```
SQL Server → ADF → ADLS Gen2 → Databricks L1 → L2 → L3
```

Success means a single command turns one sentence into a reviewed, tested,
quality-checked set of pipeline artifacts, with a complete audit trail — and
the run is honest about every point where the model fell short.

---

## Design decisions

### D1 — Agent per SDLC stage, not one agent with many tools

Six specialist agents, each with its own system prompt and a *narrow* tool
set, rather than one generalist. An 8B model follows a focused instruction far
better than a broad one, each stage gets a tier sized to its difficulty, and a
failure is attributable to a stage rather than to "the agent".

### D2 — Reasoning tiers, not model names

Agents name a tier (`deep` / `balanced` / `fast`); `config.MODEL_ROUTING` maps
tiers to models per provider. The tiers are sized for Opus / Sonnet / Haiku —
Opus-class for mapping, code generation and review, where an error ships a
broken pipeline; Sonnet-class for structured drafting; Haiku-class for the run
summary. The local runtime collapses all three onto `llama3.1:8b`. Moving to
hosted models is a routing-table change, not an agent change.

### D3 — The mapping is the single source of truth

Code generation, data quality, tests and review all derive from the mapping.
This is what stops the four artifacts drifting, which is the actual pain in
the manual process.

### D4 — Every generative stage has a verifying tool

Generated code → `validate_python_code` (AST parse + forbidden-construct
scan). Authored DQ rules → `run_data_quality_checks` against the real landed
CSVs. Generated tests → `run_pytest`, exit code recorded. The review merges
static findings so it cannot approve around a module that failed to parse.
Nothing is accepted on the model's word.

### D5 — Degradation is a first-class outcome

Every generative stage has a deterministic, catalog-driven fallback in
`agents/fallbacks.py`. When the model's output is unusable, the stage writes
the fallback artifact, returns `status="degraded"` with a note naming what
happened, and the run continues. An 8B model fails often enough that
"the run crashed" would be the normal outcome otherwise — and a developer
needs the artifact plus an honest label, not a stack trace.

### D6 — Grounding by construction *(added after the first real run)*

The first run against `llama3.1:8b` returned a well-formed specification
citing `Sales.Order` and `Sales.OrderItem` — neither exists. The model had
`list_source_tables` and `get_table_schema` available and never called them.

Fix, in two halves:

- **Inject the facts.** `agents/grounding.py` pre-fetches the catalog, the
  schemas and the landing profiles and puts them in the agent's first user
  message. Tools stay available for follow-ups.
- **Validate the reply.** `validate_table_names` checks every table the model
  named against the catalog. Invented names are discarded, recorded in the
  artifact's assumptions, and the stage is marked degraded.

### D7 — Bounded requests *(added after the first real run)*

Asking for every layer of every table in one reply overruns `num_predict` and
the JSON comes back truncated. The mapping agent now issues one request per
source table (L1 + L2) plus one for the gold layer.

### D8 — Tests that run without a cluster

Generated tests re-implement the mapping rule in plain Python over
`list[dict]` rows. They pin the *rule*, so a mapping change breaks a test, and
they execute in CI with no Spark. Verifying the DataFrame code itself needs a
Spark session and is listed below as future work.

---

## Build phases

### Phase 1 — Foundation ✅

- [x] `config.py` — paths, Ollama endpoint, tier→model routing, layer semantics
- [x] `logger_setup.py` — per-run text log and JSONL event log, stamped with
      run date and time
- [x] `llm_client.py` — `add_user_message` / `add_assistant_message` / `chat`
      in the project's established style, plus tier resolution and metrics
- [x] `stub_llm.py` — deterministic offline double so CI needs no model server

### Phase 2 — Tools ✅

- [x] All 11 tools in a single `tools.py` with schemas and dispatcher
- [x] Discovery: `get_current_datetime`, `list_source_tables`, `get_table_schema`
- [x] Profiling: `profile_landing_dataset`
- [x] Artifacts: `write_artifact`, `read_artifact`, `list_artifacts`
      (path-escape guarded)
- [x] Validation: `validate_python_code`, `run_data_quality_checks`, `run_pytest`
- [x] Composition: `batch_tool`
- [x] DQ engine: 8 check types across 6 dimensions

### Phase 3 — Prompts and agents ✅

- [x] Seven `.md` prompts with `${placeholder}` rendering
- [x] `BaseAgent` — LLM↔tool loop, `StageResult`, JSON/Python extraction
- [x] Stage 1 Requirement Refiner (balanced)
- [x] Stage 2 STTM Mapper (deep)
- [x] Stage 3 Pipeline Code Generator (deep) — validate → repair → template
- [x] Stage 4 Data Quality (balanced) — author → execute
- [x] Stage 5 Test Generator (balanced) — write → run
- [x] Stage 6 Code Review (deep) — model findings merged with static findings
- [x] Orchestrator (fast) — sequencing, manifest, run summary

### Phase 4 — Data and deterministic fallbacks ✅

- [x] `data/source_catalog.json` — 3 SQL Server tables with keys, types, FKs
- [x] `data/landing/*.csv` — ADLS Gen2 extracts with seeded defects
- [x] `agents/fallbacks.py` — pure generators for requirements, mapping,
      pipeline code, DQ rules, tests and review

### Phase 5 — Test suite ✅

- [x] 239 tests, offline, no Spark
- [x] Config and routing, tools, prompts, fallbacks, grounding, agents,
      logging and client, orchestrator end-to-end

### Phase 6 — Hardening after the first real run ✅

- [x] `agents/grounding.py` — fact injection and catalog validation (D6)
- [x] Mapping agent split into bounded per-table requests (D7)
- [x] Hallucinated-table test pinned to the exact observed failure

### Phase 7 — Documentation and evidence ✅

- [x] `README.md`, `CLAUDE.md`, `PLAN.md`
- [x] `skills/` — four reusable domain playbooks
- [x] `EVIDENCE.md` — a real run against `llama3.1:8b`, recorded as it happened

---

## Risks and how they were handled

| Risk | Handling |
|---|---|
| 8B model returns prose instead of JSON | `extract_json` salvages from fences, prose and trailing commas; a deterministic fallback runs if it still fails |
| 8B model hallucinates schema objects | Facts injected into the prompt; reply validated against the catalog; stage marked degraded (D6) |
| Long artifacts truncated at `num_predict` | One bounded request per source table (D7) |
| Generated code does not parse | AST gate, one repair round with the errors handed back, then a template rendered from the mapping |
| Generated tests are vacuous or need Spark | Rejected if they lack real assertions or import anything heavy; then executed for real |
| Review rubber-stamps broken code | Static findings merged in; verdict forced to `request_changes` on any critical or high finding |
| Run is unauditable | Text log plus JSONL event stream, every LLM and tool call recorded with latency |
| Local inference is slow | Accepted and documented; the tier routing exists so this moves to hosted models unchanged |

---

## Out of scope for this build

- Live SQL Server, ADF or Databricks connectivity (metadata and landed files
  are synthetic by design)
- Executing generated PySpark — it is parsed and linted, not run
- Streaming or CDC ingestion
- SCD type 2 history in the silver layer
- Star-schema inference in the gold layer

---

## Next

1. Spark-backed verification of generated modules in CI
2. A critic agent that attacks the mapping before code is generated
3. Emit DQ rules as Delta Live Tables expectations
4. Register the mapping as Unity Catalog lineage
5. SCD type 2 driven by the mapping's change-tracking column
6. Incremental re-planning when a source schema changes
7. `ollama` vs `anthropic` A/B on degradation rate and review findings
