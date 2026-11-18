---
name: medallion-pipeline-design
description: Design and generate Databricks medallion pipelines (L1 bronze, L2 silver, L3 gold) for data landed from SQL Server via ADF into ADLS Gen2. Use when deciding what belongs in which layer, choosing a load strategy, making a layer idempotent, or writing the PySpark job for a layer.
---

# Medallion pipeline design

## When to use this

You are writing or reviewing a Databricks job for one layer of a medallion
architecture, and need to decide what the layer owns, how it loads, and how
it survives a re-run.

## The layer contract

| | **L1 — bronze** | **L2 — silver** | **L3 — gold** |
|---|---|---|---|
| Reads | ADLS Gen2 landing files | bronze Delta table | silver Delta tables |
| Writes | `<catalog>.bronze.<entity>` | `<catalog>.silver.<entity>` | `<catalog>.gold.<entity>` |
| Load | append | merge on business key | overwrite |
| Types | everything `string` | declared target types | declared target types |
| Names | source names, unchanged | `snake_case` | business names |
| Transforms | none | conform, dedup, standardise | join, aggregate, derive |
| Rejects bad rows? | never | optionally quarantine | never — fix upstream |

### L1 — bronze

Land what arrived, exactly as it arrived. The value of bronze is that it is
replayable: if the silver logic turns out to be wrong, you re-derive rather
than re-extract.

- Read with `.option("header", True)` and **no** `inferSchema`. Inference is
  non-deterministic across batches and will silently change a column's type
  between runs.
- Every source column lands as `string`.
- Add `source_file` (`F.input_file_name()`), `ingestion_timestamp`
  (`F.current_timestamp()`) and `batch_id` (a job parameter).
- Append. Never merge, never overwrite.

### L2 — silver

The conformance layer, and the only place the business key becomes meaningful.

- Cast to declared types. A cast that fails yields `null`, not an error — if
  that matters, count nulls before and after and fail the job on a jump.
- Trim all text. Lower-case anything used for matching (email, codes).
- Deduplicate **before** merging:
  ```python
  window = Window.partitionBy(*BUSINESS_KEY).orderBy(
      F.col("ingestion_timestamp").desc_nulls_last()
  )
  latest = (df.withColumn("_rn", F.row_number().over(window))
              .filter(F.col("_rn") == 1).drop("_rn"))
  ```
  Merging a batch that contains two rows for one key fails at runtime with a
  multiple-source-match error. This is the single most common defect in
  generated silver code.
- Merge on the business key, not on every column.

### L3 — gold

Shaped for the question being asked, not for the source.

- Name measures after their meaning: `total_order_amount`, never `sum_1`.
- Guard the fan-out: joining a dimension to a fact and then summing a
  dimension attribute double-counts. Aggregate the fact first, then join.
- Overwrite with `overwriteSchema`. Gold should be a pure function of silver,
  so a full rebuild is the simplest correct answer.

## Idempotency: the question to ask every time

> If this job runs twice on the same batch, is the target identical?

| Strategy | Why it is safe | What makes it unsafe |
|---|---|---|
| merge | Matched rows update in place | A duplicate business key in the source batch |
| overwrite | Target is fully replaced | A non-deterministic source (`current_date()` filters) |
| append | **Not safe by itself** | Needs an explicit guard |

Append needs the guard:

```python
if spark.catalog.tableExists(TARGET_TABLE):
    spark.sql(f"DELETE FROM {TARGET_TABLE} WHERE batch_id = '{batch_id}'")
```

## Code shape

Separate the pure transform from the I/O, so the rule is testable without a
cluster:

```python
def build_customer_silver(bronze_df: DataFrame, batch_id: str) -> DataFrame:
    """Pure: no reads, no writes, no side effects."""
    ...

def load() -> int:
    """All I/O lives here."""
    spark = get_spark()
    parameters = read_parameters()
    result = build_customer_silver(spark.table(SOURCE), parameters["batch_id"])
    ...
```

Parameterise through `dbutils.widgets.get(...)` with a local default, so the
module imports outside Databricks:

```python
def read_parameters() -> dict:
    try:
        dbutils  # noqa: F821 - provided by the Databricks runtime
    except NameError:
        return {"batch_id": "local-run", "source_path": SOURCE_OBJECT}
    return {"batch_id": dbutils.widgets.get("batch_id"),  # noqa: F821
            "source_path": dbutils.widgets.get("source_path")}  # noqa: F821
```

## Checklist before you call a layer done

- [ ] Module docstring names the layer, the target table and the load strategy
- [ ] Transform function is pure and returns a DataFrame
- [ ] L1 columns are all `string`; audit columns present
- [ ] L2 deduplicates before merging; merge condition is the business key only
- [ ] L3 aggregates the fact before joining the dimension
- [ ] Re-running the job leaves the target unchanged
- [ ] `logging`, not `print`
- [ ] No hard-coded paths, no inline secrets
- [ ] No `os.system`, `subprocess`, `eval` or `exec`

## In this repository

`agents/fallbacks.py:fallback_pipeline_code` renders a module that satisfies
every item above; read it for the reference shape. `tools.py:
validate_python_code` enforces the mechanical items.
