# Agentic Data Engineering Accelerator

An agentic AI system that compresses the data-engineering SDLC — requirement
refinement, source-to-target mapping, Databricks pipeline code generation,
data-quality checks, test generation and code review — into a single
auditable run, executed by a **local `llama3.1:8b`** served by Ollama.

```
SQL Server  ──ADF──▶  ADLS Gen2  ──▶  Databricks L1 ──▶ L2 ──▶ L3
                                       (bronze)    (silver) (gold)
```

---

## 1. Objective and problem statement

Building a medallion pipeline for one source table is mostly transcription,
not thought. An engineer reads a Confluence page, opens the source schema,
types a mapping spreadsheet, hand-writes three near-identical PySpark
notebooks, hand-writes data-quality rules that duplicate the mapping, writes
tests that duplicate both, and then waits for a reviewer. The same work is
repeated per table, per layer, per project. It is slow, it is inconsistent
between engineers, and the four artifacts drift apart the moment one of them
changes.

**The problem this project addresses:** the data-engineering SDLC produces
six artifacts that are all derivable from two inputs — the business
requirement and the source schema — yet they are produced by hand,
separately, and then drift.

**The approach:** make the mapping the single source of truth, and give each
SDLC stage to a specialist agent that derives its artifact from that mapping
and verifies the result with a real tool. Nothing is accepted because the
model said it; code is accepted because it parsed, rules are accepted
because they ran against landed data, tests are accepted because pytest
executed them.

---

## 2. Key features and capabilities

| Capability | What it actually does |
|---|---|
| **Requirement refinement** | Turns a one-line request into a specification with IDs, layer assignment and *checkable* acceptance criteria. Table names are validated against the catalog, so a hallucinated `Sales.Order` is discarded, not propagated. |
| **Source-to-target mapping** | Produces the L1/L2/L3 mapping as both JSON (machine) and CSV (the format engineers review in). Grounded in the declared schema *and* a profile of the file ADF actually landed. |
| **Databricks code generation** | One PySpark module per layer: L1 appends with a batch guard, L2 merges on the business key after windowed dedup, L3 joins and aggregates. Pure transform functions are kept separate from I/O so they are testable without a cluster. |
| **Static code gates** | Every generated module is AST-parsed and screened for shell escapes, `eval`/`exec`, inline secret reads, missing Spark usage and missing docstrings — before a human sees it. |
| **Data quality** | Eight check types across six DQ dimensions, executed for real against the landed CSVs. `error` severity blocks promotion; `warn` is logged. |
| **Test generation + execution** | A pytest suite per layer that pins the mapping's rules, then actually runs pytest and reports the exit code. A generated test that does not execute is not a test. |
| **Code review** | A structured verdict with severity-ranked findings, merged with the static validation results so the review cannot approve around a module that failed to parse. |
| **Graceful degradation** | Every generative stage has a deterministic, catalog-driven fallback. When the 8B model returns prose instead of JSON, the run still produces a complete artifact — explicitly marked `degraded` and explained in the notes. |
| **Full run audit trail** | A human-readable log and a JSONL event stream per run, both stamped with the run date and time, recording every LLM call (agent, tier, model, latency) and every tool call. |

---

## 3. LLMs, tools, frameworks and models

### Runtime

| | |
|---|---|
| **Model** | `llama3.1:8b` |
| **Server** | Ollama at `http://localhost:11434` |
| **Framework** | The `ollama` Python client. No agent framework — the loop is ~40 lines in `agents/base_agent.py`. |
| **Everything else** | Python standard library: `ast`, `csv`, `json`, `logging`, `subprocess`, `dataclasses`, `string.Template`. |

### Model routing (Opus / Sonnet / Haiku)

Agents never name a model. They name a **reasoning tier**, and
`config.MODEL_ROUTING` decides what that tier calls. The tiers are sized for
the Anthropic family — Opus where a mistake is expensive, Haiku where it is
not — and the local runtime collapses all three onto `llama3.1:8b`.

| Tier | Agents | Why that tier | `ollama` | `anthropic` |
|---|---|---|---|---|
| `deep` | STTM mapper, code generator, code reviewer | Cross-schema reasoning, code correctness, defect detection. A wrong answer here ships broken pipelines. | `llama3.1:8b` | `claude-opus-5-5` |
| `balanced` | Requirement refiner, data quality, test generator | Structured drafting from a schema that is already known. | `llama3.1:8b` | `claude-sonnet-5-5` |
| `fast` | Orchestrator | Writes the closing run summary from a record that is already complete. | `llama3.1:8b` | `claude-haiku-5-5` |

Switching the fleet to Anthropic is `--provider anthropic` plus wiring the
SDK call in `llm_client.LLMClient._build_client`. The routing table and the
tier assignments do not change.

### Agent tools (all in `tools.py`)

| Group | Tools |
|---|---|
| Discovery | `get_current_datetime`, `list_source_tables`, `get_table_schema` |
| Profiling | `profile_landing_dataset` |
| Artifacts | `write_artifact`, `read_artifact`, `list_artifacts` |
| Validation | `validate_python_code`, `run_data_quality_checks`, `run_pytest` |
| Composition | `batch_tool` |

---

## 4. Agentic architecture

```
                    ┌──────────────────────────────────────┐
   business         │          ORCHESTRATOR                │
   requirement ────▶│  run folder · stage sequencing ·     │
                    │  manifest · degradation policy       │
                    │           tier: fast                 │
                    └───────────────┬──────────────────────┘
                                    │
   ┌────────────────────────────────┼────────────────────────────────┐
   │                                │                                │
   ▼                                ▼                                ▼
┌─────────────────┐   ┌──────────────────────┐   ┌────────────────────┐
│ 1. REQUIREMENT  │   │ 2. STTM MAPPER       │   │ 3. CODE GENERATOR  │
│    REFINER      │──▶│                      │──▶│                    │
│ tier: balanced  │   │ tier: deep           │   │ tier: deep         │
│                 │   │ one request per      │   │ one module per     │
│ spec + testable │   │ table (L1+L2), one   │   │ mapping object,    │
│ acceptance      │   │ for gold (L3)        │   │ validate → repair  │
│ criteria        │   │                      │   │ → template         │
└────────┬────────┘   └──────────┬───────────┘   └─────────┬──────────┘
         │                       │                         │
         │                       │  ◀── mapping is the single source of truth
         │                       ├─────────────────┬───────┴──────────┐
         │                       ▼                 ▼                  ▼
         │            ┌──────────────────┐ ┌────────────────┐ ┌──────────────┐
         │            │ 4. DATA QUALITY  │ │ 5. TEST GEN    │ │ 6. CODE      │
         │            │ tier: balanced   │ │ tier: balanced │ │    REVIEW    │
         │            │ author → EXECUTE │ │ write → RUN    │ │ tier: deep   │
         │            │ on landed data   │ │ pytest         │ │ + static     │
         │            └──────────────────┘ └────────────────┘ │   findings   │
         │                                                    └──────────────┘
         │
         └──────────────────── every agent ────────────────────┐
                                                               ▼
   ┌───────────────────────────────────────────────────────────────────────┐
   │                            TOOL LAYER (tools.py)                       │
   ├───────────────┬────────────┬─────────────┬────────────────────────────┤
   │ DISCOVERY     │ PROFILING  │ ARTIFACTS   │ VALIDATION                  │
   │ list_source_  │ profile_   │ write_      │ validate_python_code (AST)  │
   │   tables      │  landing_  │  artifact   │ run_data_quality_checks     │
   │ get_table_    │  dataset   │ read_       │ run_pytest (subprocess)     │
   │   schema      │            │  artifact   │                             │
   │ get_current_  │            │ list_       │ batch_tool (composition)    │
   │   datetime    │            │  artifacts  │                             │
   └───────┬───────┴─────┬──────┴──────┬──────┴──────────────┬─────────────┘
           ▼             ▼             ▼                     ▼
    source_catalog   data/landing   output/run_<id>/    logs/run_<id>.log
       .json          *.csv          (artifacts)        + .events.jsonl
    (SQL Server     (ADLS Gen2
     metadata)       landing zone)
```

### Two design decisions worth knowing

**Grounding by construction, not by instruction.** The first real run against
`llama3.1:8b` produced a clean, well-formed specification that cited
`Sales.Order` and `Sales.OrderItem` — tables that do not exist. The model had
the discovery tools and simply did not call them. Telling it harder does not
fix this. So the accelerator *pre-fetches* the catalog, the schemas and the
landing profiles and injects them into the agent's first message, keeps the
tools available for follow-ups, and then **validates the reply against the
catalog** (`agents/grounding.py`). Hallucinated tables are discarded and the
stage is marked degraded, with the discarded names recorded in the artifact.

**Bounded requests.** Asking an 8B model for every layer of every table in
one reply overruns its output budget and the JSON comes back truncated. The
mapping agent therefore issues one request per source table (L1 + L2) and one
for the gold layer, so no single reply has to be large.

---

## 5. Functional overview and end-to-end data flow

```
 requirement (string)
   │
   ├─ 1. REQUIREMENT REFINER ───────────────────────────────────────────────
   │    in : requirement + injected source catalog
   │    out: requirements/requirements.json  ·  requirements.md
   │    gate: every table name is validated against the catalog
   │
   ├─ 2. STTM MAPPER ───────────────────────────────────────────────────────
   │    in : requirements + full schemas + landed-file profiles
   │    out: mapping/source_to_target_mapping.json  ·  .csv
   │    gate: layer, columns and load strategy structurally checked;
   │          missing layers back-filled from the catalog
   │
   ├─ 3. PIPELINE CODE GENERATOR ───────────────────────────────────────────
   │    in : one mapping object
   │    out: pipelines/l1_*.py, l2_*.py, l3_*.py  ·  validation_report.json
   │    gate: AST parse + forbidden-construct scan.
   │          invalid → one repair round with the errors handed back →
   │          still invalid → deterministic template from the mapping
   │
   ├─ 4. DATA QUALITY ──────────────────────────────────────────────────────
   │    in : mapping + landed-file profiles
   │    out: data_quality/dq_rules.json · dq_results.json · dq_report.md
   │    gate: rules are EXECUTED against data/landing/*.csv.
   │          `error` failures block promotion, `warn` failures are logged
   │
   ├─ 5. TEST GENERATOR ────────────────────────────────────────────────────
   │    in : mapping
   │    out: tests/test_l*.py  ·  pytest_report.json
   │    gate: suite must define real assertions, import nothing heavy, and
   │          pytest must actually run it
   │
   ├─ 6. CODE REVIEW ───────────────────────────────────────────────────────
   │    in : mapping + generated modules (read back from disk)
   │    out: review/code_review.json  ·  code_review.md
   │    gate: verdict is forced to `request_changes` if any finding —
   │          model-authored or static — is critical or high
   │
   └─ ORCHESTRATOR ─────────────────────────────────────────────────────────
        out: run_manifest.json  ·  RUN_SUMMARY.md
             logs/run_<id>.log  ·  logs/run_<id>.events.jsonl
```

### Layer semantics in the generated code

| Layer | Schema | Load | Idempotency mechanism |
|---|---|---|---|
| L1 | `bronze` | append | Deletes rows carrying the current `batch_id` before appending, so a re-run cannot double count. All columns land as `string` plus `source_file`, `ingestion_timestamp`, `batch_id`. |
| L2 | `silver` | merge | `row_number()` over the business key ordered by ingestion time keeps the latest row, then `MERGE INTO ... WHEN MATCHED UPDATE / WHEN NOT MATCHED INSERT`. Columns are snake_cased and cast to declared types. |
| L3 | `gold` | overwrite | Full replace with `overwriteSchema`, so the curated table is a pure function of silver. |

---

## 6. Project structure

```
Agentic Data Engineering Accelerator/
├── main.py                     CLI entry point
├── config.py                   paths, Ollama endpoint, tier → model routing
├── logger_setup.py             run-scoped logging (text + JSONL events)
├── llm_client.py               chat/message helpers, tier resolution, metrics
├── stub_llm.py                 deterministic offline double for the tests
├── tools.py                    ALL 11 tools + schemas + dispatcher
│
├── prompts/                    one system prompt per agent (editable .md)
│   ├── loader.py               ${placeholder} rendering via string.Template
│   ├── requirement_refiner.md
│   ├── sttm_mapper.md
│   ├── pipeline_codegen.md
│   ├── data_quality.md
│   ├── test_generator.md
│   ├── code_reviewer.md
│   └── orchestrator.md
│
├── agents/
│   ├── base_agent.py           LLM↔tool loop, StageResult, output extraction
│   ├── grounding.py            fact injection + catalog validation
│   ├── fallbacks.py            deterministic generators for every stage
│   ├── requirement_agent.py    stage 1
│   ├── mapping_agent.py        stage 2
│   ├── codegen_agent.py        stage 3
│   ├── dq_agent.py             stage 4
│   ├── test_agent.py           stage 5
│   ├── review_agent.py         stage 6
│   └── orchestrator.py         run sequencing and the manifest
│
├── skills/                     reusable domain playbooks (SKILL.md)
│   ├── medallion-pipeline-design/
│   ├── source-to-target-mapping/
│   ├── data-quality-rules/
│   └── databricks-code-review/
│
├── data/
│   ├── source_catalog.json     synthetic SQL Server metadata (3 tables)
│   └── landing/*.csv           simulated ADLS Gen2 extracts, defects included
│
├── tests/                      the accelerator's own suite (239 tests)
├── logs/                       run_<date>_<time>.log + .events.jsonl
├── output/run_<id>/            all artifacts for one run
│
├── README.md  ·  CLAUDE.md  ·  PLAN.md  ·  EVIDENCE.md
├── requirements.txt  ·  pytest.ini  ·  .gitignore
```

---

## 7. Setup and execution

### Prerequisites

```bash
# 1. Ollama with the model pulled
ollama serve
ollama pull llama3.1:8b
curl http://localhost:11434/api/tags      # should list llama3.1:8b

# 2. Python 3.10+
pip install -r requirements.txt
```

### Run

```bash
# Full SDLC against the local model
python main.py -r "Build a curated gold table giving each customer their
                   order count and total order amount."

# From a file
python main.py -f my_requirement.txt

# A subset of stages
python main.py -r "..." --stages requirements mapping

# Offline and deterministic — no model server needed
python main.py -r "..." --provider stub

# Inspect agents, tools, prompts and model routing
python main.py --describe
```

### Verify

```bash
python -m pytest                 # 239 tests, no Ollama or Spark required
```

### Where the output goes

```
output/run_<id>/     requirements · mapping · pipelines · data_quality ·
                     tests · review · run_manifest.json · RUN_SUMMARY.md
logs/run_<id>.log            human-readable, stamped with run date and time
logs/run_<id>.events.jsonl   one JSON object per LLM call, tool call and stage
```

### CLI reference

| Flag | Meaning |
|---|---|
| `-r, --requirement` | The business requirement |
| `-f, --requirement-file` | Read the requirement from a file |
| `--provider` | `ollama` (default), `stub`, `anthropic` |
| `--host` | Ollama endpoint, default `http://localhost:11434` |
| `--stages` | Subset of `requirements mapping codegen data_quality tests review` |
| `--run-id` | Override the generated run id (default: run date and time) |
| `--quiet` | File logging only |
| `--describe` | Print the capability surface and exit |

---

## 8. Assumptions

1. **The source catalog is authoritative.** `data/source_catalog.json` stands
   in for a real metadata store (Purview, a Unity Catalog crawl, or
   `INFORMATION_SCHEMA`). The accelerator never connects to SQL Server.
2. **ADF has already landed the data.** `data/landing/*.csv` stands in for the
   ADLS Gen2 landing zone. The accelerator does not orchestrate ADF.
3. **Generated pipeline code is not executed.** It targets a Databricks
   cluster with Delta and Unity Catalog. Locally it is parsed and linted, not
   run. The *generated tests* are executed, which is why they are written to
   use plain Python rather than Spark.
4. **Determinism over creativity.** Every tier runs at `temperature = 0`.
5. **No credentials anywhere.** The local runtime needs none, and generated
   code takes secrets as widget parameters rather than inline literals.

## 9. Limitations

1. **Local 8B inference is slow.** On CPU, a single `deep`-tier call takes
   minutes, so a full run is measured in tens of minutes to hours. The work is
   correct but not interactive. The tier routing exists precisely so this can
   be moved to hosted models without touching the agents.
2. **8B output quality is the binding constraint.** It hallucinated table
   names on the very first run. `agents/grounding.py` and the fallbacks in
   `agents/fallbacks.py` exist because of that, and the `degraded` status is a
   first-class outcome rather than an error.
3. **The DQ engine is row-wise Python over CSV.** It is a correct reference
   implementation for the landing zone and a specification for the Spark
   checks, but it is not the production engine — at volume this belongs in
   Delta Live Tables expectations or Great Expectations.
4. **Generated tests pin the mapping, not the Spark job.** They verify the
   declared rule, re-implemented in plain Python. Verifying the actual
   DataFrame code needs a Spark session in CI.
5. **L3 modelling is shallow.** The gold layer produces aggregates. It does
   not infer a star schema, surrogate keys or SCD type 2 history.
6. **Generated test code is executed by `run_pytest`.** That is the point of
   the stage, but it means model-written code runs on the host. In a shared
   environment, run the accelerator in a container.
7. **Single source system.** SQL Server only; no streaming, no CDC, no
   multi-source conformance.

## 10. Future enhancements

1. **Spark-backed verification** — run the generated modules against a local
   `pyspark` session with sample data, so correctness is proven rather than
   parsed.
2. **A critic loop on the mapping** — a second `deep` agent that attacks the
   mapping before any code is generated, which is where defects are cheapest
   to fix.
3. **Delta Live Tables emission** — express the DQ rules as DLT expectations
   so the rule set and the pipeline stop being two artifacts.
4. **Unity Catalog lineage registration** — push the mapping into the catalog
   so lineage is declared, not reconstructed.
5. **SCD type 2** — history-tracked dimensions in L2, driven by the mapping's
   change-tracking column.
6. **Incremental re-planning** — when a source schema changes, re-run only the
   affected mapping objects and their downstream artifacts.
7. **Hosted-model A/B** — same run on `ollama` and `anthropic`, compared on
   degradation rate and review findings, to quantify what the tier routing
   buys.
