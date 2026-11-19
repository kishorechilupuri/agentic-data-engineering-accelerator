# EVIDENCE.md

What the accelerator actually did when run end to end against a local
`llama3.1:8b`, including where it failed. Everything below is taken from the
recorded artifacts in [`evidence/`](evidence/), not from memory.

---

## 1. Summary

| | |
|---|---|
| **Run** | 2026-11-19, 02:58:25, run id `evidence`, scoped to `Sales.Customer` |
| **Model** | `llama3.1:8b` via Ollama (`http://localhost:11434`), CPU, every tier |
| **Outcome** | `completed_with_degradation` in **5,674 s (94.6 min)** |
| **LLM calls** | 16 calls, 5,666 s of model time (99.9% of wall-clock) |
| **Stages** | 4 `ok`, 2 `degraded`, 0 `failed` |

**The honest reading.** The pipeline ran all six stages unattended and
produced every artifact. The data-quality gate worked on real data. But the
run also showed that **two of the "ok" stages were not actually ok**: the code
generator's three modules parsed as Python and none of them would run, and
the gate that was supposed to catch that only checked syntax. That, and five
smaller defects, were found by reading the output rather than the status line,
and are fixed below. Three further problems are **not** fixed and are listed
in [section 6](#6-still-open).

The fixes were verified offline against the recorded output. They have **not**
been re-validated by a second 95-minute model run.

---

## 2. Stage results

| Stage | Tier | Status | Time | LLM calls | What came out |
|---|---|---|---|---|---|
| Requirements | balanced | `ok` | 178 s | 1 | Spec with 3 FRs; source tables grounded to the catalog |
| Mapping | deep | `ok` | 919 s | 4 | 3 mapping objects (L1, L2, L3) |
| Code generation | deep | `ok` ⚠ | 1,038 s | 3 | 3 PySpark modules, all passed the gate, **none runnable** |
| Data quality | balanced | `degraded` | 1,375 s | 2 | 6 rules from fallback; 2 failed, 1 blocking |
| Tests | balanced | `degraded` | 1,482 s | 3 | 3 of 3 suites from fallback; **6 tests failed** |
| Review | deep | `ok` | 615 s | 2 | `request_changes`, 5 findings |
| Run summary | fast | — | 65 s | 1 | **Junk** (a fabricated tool call) |

⚠ = status was `ok`, outcome was not. See D1.

Latency, from `logs/run_evidence.events.jsonl`:

| | calls | min | mean | max |
|---|---|---|---|---|
| `deep` | 9 | 100 s | 286 s | 455 s |
| `balanced` | 6 | 178 s | 505 s | 780 s |
| `fast` | 1 | 65 s | 65 s | 65 s |

All three tiers are the same local model here, so the spread tracks output
length and prompt size, not tier. Mean over all 16 calls is 5.9 minutes.

---

## 3. What the real run showed working

**Grounding held.** The first-ever run (section 8) invented `Sales.Order` and
`Sales.OrderItem`. In this run the specification's `source_tables` is exactly
`['Sales.Customer']`, and `--tables` kept the model from seeing the other two
tables at all.

**The data-quality gate caught the seeded defects on real data.** Six rules
executed against the 12-row landing extract:

| Rule | Check | Severity | Result |
|---|---|---|---|
| DQ-001 | `CustomerID` not_null | error | pass |
| DQ-002 | `CustomerID` unique | error | **fail** — 1 duplicate (`1001`) |
| DQ-003 | `CustomerID` row_count_min | error | pass (12 rows) |
| DQ-004 | `Email` regex | warn | **fail** — 1 malformed (`nina-kowalski-example-com`) |
| DQ-005 | `CreatedDate` date_format | warn | pass |
| DQ-006 | `ModifiedDate` date_format | warn | pass |

One blocking failure (the duplicate business key), one warning. This is the
check that stops a duplicate key reaching a `MERGE`, and it fired.

**Degradation was reported, not hidden.** Data quality and tests both say
`degraded` with a note naming what went wrong. The review's static findings
were merged in. The overall status is `completed_with_degradation`, not
`completed`.

**The review reached the right verdict.** `request_changes`, driven by one
`high` idempotency finding on L3, which is genuinely the module's worst flaw.

**Logging does what was asked.** `logs/run_evidence.log` opens with
`RUN DATE=2026-11-19 | RUN TIME=02:58:25`, and the JSONL stream records every
LLM call (agent, tier, model, latency, tool names) and every tool call.

---

## 4. What the real run exposed

| # | Defect | Evidence | Status |
|---|---|---|---|
| D1 | Code gate approved three modules that cannot run | `validation_report.json`: `valid: true` ×3; codegen `status=ok` | **Fixed** |
| D2 | L3 module appends on re-run, i.e. double counts — the exact thing the requirement forbids | `pipelines/l3_customer_orders.py` | **Detected** now; generation not fixed |
| D3 | Generated test suites failed 6 tests | `tests/pytest_report.json`: `KeyError: 'CustomerID'` | **Fixed** |
| D4 | DQ tool crashed twice on the model's own rules | log: `Invalid \escape: line 1 column 725` | **Fixed** |
| D5 | Run summary was a fabricated tool call, not prose | `run_manifest.json` → `summary` | **Fixed** |
| D6 | pytest summary line missing (`no summary line`) | `run_manifest.json` → `tests.summary` | **Fixed** |
| D7 | Review findings recorded absolute local paths | `review/code_review.json` | **Fixed** |

### D1 — the gate that only checked syntax

`validate_python_code` ran `ast.parse` plus a forbidden-construct scan. All
three model-written modules passed it, with only a "no docstring" warning.
Reading them shows what that missed (by inspection — there is no Spark in the
test environment, so none of this was executed):

| Module | What is wrong |
|---|---|
| **L1** | Never appends (no `mode`), so no re-run guard; reads `delta` from a CSV landing path; `batch_id = F.input_file_name()`; runs the job on import |
| **L2** | `dbutils.widgets.get("batch_id", "default")` — `get` takes one argument; `.load("dea_accelerator.bronze.customer")` passes a table name to a path reader; **no dedup and no merge**, so the L2 contract is unmet; overwrites `ingestion_timestamp` |
| **L3** | `customer_df.merge(orders_df, ...)` — a pandas method, not Spark; `F.col('COUNT(*)')` and `F.col('SUM(SalesAmount)')` are not columns; `select('customer_id', 'customer_id')` duplicates a column; reads a `silver.orders` table that does not exist; `target_df` is used before assignment (`UnboundLocalError`); ends with `mode("append")` when the table exists — **double counting on re-run** |

L3 also defines three different `main` variants and executes one at import.

**Fix.** `validate_python_code` now also lints Spark semantics (the checks
above) and enforces a per-layer load contract: L1 must append, L2 must
deduplicate and merge, L3 must not append. The codegen agent passes the layer
and the mapping's declared columns, so a violation goes back to the model as a
repair request, and if the repair fails the deterministic template is used.

### D3 — the generated tests failed their own run

All three model-written test modules were unusable (two had no extractable
code block, one had a `SyntaxError`), so the deterministic suites were used —
and **those** failed. The mapping said `business_key: ["CustomerID"]` while the
target column is `customer_id`, so `rows[0]["CustomerID"]` raised `KeyError` in
L1 and L3. A second test (`assert BUSINESS_KEY in EXPECTED_COLUMNS or
EXPECTED_COLUMNS`) asserted nothing and could never fail.

### D4 — a regex that crashed the DQ tool

The model sent its rules as a JSON string containing `^[^@\s]+@...`. A single
backslash before `s` is not valid JSON, `json.loads` rejected it, and the tool
failed twice. The model never recovered, and 23 minutes later the stage fell
back to catalog-derived rules.

---

## 5. Verification of the fixes

Offline, against the recorded artifacts. No model was involved.

| Check | Before | After |
|---|---|---|
| Real L1 module through the gate | valid | **rejected** — 1 error |
| Real L2 module through the gate | valid | **rejected** — 3 errors |
| Real L3 module through the gate | valid | **rejected** — 7 errors |
| Deterministic templates through the stricter gate | — | **7 of 7 pass, 0 warnings** |
| Generated suites for the run's real mapping | 6 failed | **29 passed** |
| DQ tool on a rule string of the same shape (single-backslash regex) | `Invalid \escape` | evaluates the rule, finds the malformed email |
| pytest summary inside the project | `no summary line` | `2 passed` |
| Orchestrator summary on a fabricated tool call | junk stored | deterministic summary used |

The real L3 module's seven gate errors:

```
DataFrame.merge does not exist in Spark (that is pandas); use DeltaTable.merge or MERGE INTO.
select() lists the same column more than once, which makes later references ambiguous.
load('dea_accelerator.silver.customer') passes a table name to a path reader; use spark.table(...)
load('dea_accelerator.silver.orders') passes a table name to a path reader; use spark.table(...)
dbutils.widgets.get takes exactly one argument (the widget name); it has no default-value parameter.
References column(s) that are in neither the source catalog nor the mapping: ['COUNT(*)', 'SUM(SalesAmount)'].
L3 must overwrite; appending to gold double counts on a re-run.
```

These are pinned by
[`tests/test_real_run_regressions.py`](tests/test_real_run_regressions.py),
which reads the real modules from `evidence/` and asserts each is rejected.

**What to expect from a re-run, not a result:** codegen should now report
`degraded`, not `ok`, because these modules would fail the gate and be
repaired or replaced by the template.

**Limits of the new gate.** The checks are AST heuristics, not execution.
They would not catch the wrong source format in L1, or `target_df` being used
before assignment in L3 (it is assigned later in the same function). A module
can still pass and fail on a cluster. The real fix for that is running the
generated code against a local Spark session, which is listed in the README as
future work and has not been done.

---

## 6. Still open

These were found in the same run. They are **not** fixed.

**O1 — Mapping is not validated against the catalog.** The stage reported
`ok`, but the L3 mapping lists `COUNT(*)` and `SUM(SalesAmount)` as source
columns — `SalesAmount` exists nowhere in the catalog. The L1 mapping also
breaks the rule that bronze lands every column as `string` (`CreatedDate` is
typed `timestamp`, `CustomerID` is cast), and L3 lists `customer_id` twice and
types `created_date` as `date`. `grounding.known_columns` exists but nothing
calls it. The code gate now catches the invented column when it surfaces in
code, which is downstream of where it should be caught.

**O2 — The scope I chose made the requirement unsatisfiable.** The requirement
asks for each customer's order count and total order amount. Orders live in
`Sales.SalesOrderHeader`, and I scoped the run to `Sales.Customer` to keep it
under an hour. The model had no orders table to join, so its L3 invents one
(`silver.orders`) and a column (`SalesAmount`). **This run is valid evidence
of the pipeline mechanics. It is not evidence that the gold model is correct,
and it should not be read as one.**

**O3 — Requirement coverage is not validated.** The prompt requires
functional requirements covering L1, L2 and L3. The model returned three, all
tagged L3, and the stage reported `ok`.

**O4 — Review quality is limited by the model.** The model read all three
modules but its findings cover only L3. Its `high` finding is right in direction, but the recommendation
("use `mode('append')` if the table exists, and `overwrite` otherwise") is the
defect itself. Its `medium` finding (use `cache()`) is generic. It missed the
nonexistent `.merge`, the use-before-assignment and the missing table. The
verdict was correct; the reasoning was not. The stricter static gate now
supplies some of what the model missed.

---

## 7. Run history

Three runs, in order. The first two are why the code looks the way it does.

| Run | Date/time | What happened |
|---|---|---|
| **1** `smoke1` | 2026-11-19 02:14 | First contact with `llama3.1:8b`, requirements stage only, **before grounding existed**. Returned a well-formed spec citing `Sales.Order` and `Sales.OrderItem`, which do not exist. The model had the discovery tools and never called them. → `agents/grounding.py`. Record: [`evidence/run_smoke1_before_grounding/`](evidence/run_smoke1_before_grounding/) |
| **2** | 2026-11-19 02:26 | Full run over all three tables. Grounding worked (all three real tables cited). I stopped it after 24 minutes: mapping one table took 12.7 min, projecting 3.5 hours total. → `--tables`. Not recorded in `evidence/`. |
| **3** `evidence` | 2026-11-19 02:58 | This document. Scoped to `Sales.Customer`. Record: [`evidence/run_2026-11-19_02-58-25/`](evidence/run_2026-11-19_02-58-25/) |

Mapping `Sales.Customer` (L1 + L2) took 12.7 min in run 2 and 9.7 min in run 3
(two calls, 128 s and 455 s). The same step varied by about a third between
runs, so treat the latencies above as one sample, not a benchmark.

---

## 8. Test suite

```
$ python -m pytest
293 passed
```

No Ollama, no Spark, no network. By file:

| File | Tests | Covers |
|---|---|---|
| `test_tools.py` | 48 | All 11 tools, dispatcher, DQ engine |
| `test_real_run_regressions.py` | 47 | Every defect in section 4, plus the real modules from `evidence/` |
| `test_fallbacks.py` | 47 | Deterministic generators for every stage |
| `test_agents.py` | 42 | Output extraction, each agent's contract |
| `test_grounding.py` | 27 | Fact injection, catalog validation, `--tables` scope |
| `test_prompts.py` | 27 | Placeholders, JSON-only and fenced-block contracts |
| `test_orchestrator.py` | 20 | End-to-end run on the stub provider |
| `test_logging_and_client.py` | 19 | Run-stamped logging, message helpers, client |
| `test_config.py` | 16 | Tier routing, model table, medallion layers |

When the run happened the suite was at 246 passing and had not caught D1–D7.
That is the point of this document: the suite tested the code against a stub
that returned well-formed output, and the real model did not.

---

## 9. Reproduce

```bash
ollama serve && ollama pull llama3.1:8b

python main.py --run-id evidence --tables Sales.Customer --quiet \
  -r "Build a curated gold table giving each customer their order count and total order amount. It must be re-runnable, must not double count on a re-run, and must reject a batch whose business keys are not unique."
```

Expect roughly 95 minutes on CPU. Model output is not bit-for-bit repeatable
across runs even at `temperature=0`; expect the same *shape* of result, not
the same text.

### Where the record is

```
evidence/run_2026-11-19_02-58-25/
├── RUN_SUMMARY.md  run_manifest.json
├── requirements/   mapping/   pipelines/   data_quality/   tests/   review/
└── logs/           run_evidence.log   run_evidence.events.jsonl
evidence/run_smoke1_before_grounding/
```

Absolute local paths in the recorded files were replaced with `<project>`
before committing; nothing else was altered. `RUN_SUMMARY.md` and
`run_manifest.json` still contain the junk summary from D5, and
`tests/pytest_report.json` still shows the 6 failures — they are the record.

---

## 10. Excerpts

Stage progression, from `logs/run_evidence.log`:

```
02:58:25 | PIPELINE START | run_id=evidence
02:58:25 | >>> STAGE REQUIREMENTS | agent=requirement_refiner | tier=balanced | model=llama3.1:8b
03:01:23 | <<< STAGE REQUIREMENTS | status=ok | 178.3s | artifacts=2
03:16:42 | <<< STAGE MAPPING | status=ok | 918.9s | artifacts=2
03:34:01 | <<< STAGE CODEGEN | status=ok | 1038.3s | artifacts=4
03:47:01 | WARNING | Tool run_data_quality_checks failed: Invalid \escape: line 1 column 725 (char 724)
03:56:55 | WARNING | No usable DQ rules returned; using fallback rules.
03:56:55 | <<< STAGE DATA_QUALITY | status=degraded | 1374.8s | artifacts=3
04:05:57 | WARNING | L1 test module could not be extracted from the model reply; ...
04:15:26 | WARNING | L2 test module could not be extracted from the model reply; ...
04:21:32 | WARNING | L3 test module was unusable (['SyntaxError line 1, offset 2725: unterminated string literal ...
04:21:37 | <<< STAGE TESTS | status=degraded | 1482.2s | artifacts=4
04:31:53 | <<< STAGE REVIEW | status=ok | 615.2s | artifacts=2
04:32:58 | PIPELINE END | run_id=evidence | status=completed_with_degradation | 5607.7s
```

The L3 module's idempotency logic, which the requirement ("must not double
count on a re-run") forbids:

```python
    # Check if the target table already exists
    if spark.catalog.tableExists("dea_accelerator.gold.customer_orders"):
        # If it exists, append to it
        target_df.write.format("delta").mode("append").saveAsTable(...)
    else:
        # If it doesn't exist, overwrite it
        target_df.write.format("delta").mode("overwrite").saveAsTable(...)
```

The run summary the orchestrator stored (D5):

```json
{"name": "get_run_summary", "parameters": {"run_record": "the run record provided in the prompt"}}
```
