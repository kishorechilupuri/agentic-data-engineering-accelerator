---
name: databricks-code-review
description: Review Databricks PySpark pipeline code before promotion — correctness against the mapping, idempotency, null and duplicate handling, schema evolution, performance and security. Use when reviewing a generated or hand-written medallion job, or deciding whether to approve or request changes.
---

# Databricks pipeline code review

## What a review is for

To find the defects that will page someone. Not to admire the structure. A
finding that does not name the problem, the layer and the fix is not a
finding.

## Priority order

Review in this order. A correctness defect found after you have spent the
review budget on naming conventions is a review that failed.

### 1. Correctness against the mapping

Read the mapping first, then the code.

- Is every mapped target column produced?
- Is anything renamed differently from the mapping?
- Are casts to the declared types, or has `decimal(18,2)` quietly become
  `double`?
- Is the join key the business key, or something that merely looks like it?

### 2. Idempotency

> If this runs twice on the same batch, is the target identical?

- **Append with no guard** — the classic L1 defect. A re-run doubles the rows.
  It must delete the current `batch_id` first, or be a merge.
- **Merge with a duplicate source key** — fails at runtime with a
  multiple-source-match error. The batch must be deduplicated *before* the
  merge, usually with `row_number()` over the business key.
- **Overwrite on a non-deterministic source** — `current_date()` in a filter
  makes yesterday's rebuild produce a different table.

### 3. Nulls and duplicates

- A `cast` that fails yields `null` silently. Casting a dirty string column to
  `int` can null out a business key and the job still reports success.
- An inner join where the mapping says left will drop rows.
- A join on a non-unique key fans out and every downstream `sum` is wrong.
- `coalesce` to a magic value (`0`, `''`, `'UNKNOWN'`) that the mapping does
  not declare.

### 4. Schema evolution

- A hard-coded `select` list breaks when the source adds a column. Usually
  correct for silver (you want to know), usually wrong for bronze (you want
  to land it).
- Bronze appends need `mergeSchema`; a gold overwrite needs
  `overwriteSchema`.

### 5. Performance

- `.collect()` or `.toPandas()` on a full DataFrame — pulls the dataset to the
  driver. Almost always a defect.
- A cross join, or a join with no condition.
- A Python UDF where a built-in exists. An order of magnitude slower and it
  blocks predicate pushdown.
- `.repartition(n)` with a literal `n` chosen for a dataset size nobody
  measured.
- A small dimension joined without a broadcast hint.
- `.count()` called repeatedly to log progress — each one is a full scan.

### 6. Security and hygiene

- An inline secret, connection string or storage key. Must be a widget or a
  secret-scope reference.
- `os.system`, `subprocess`, `eval`, `exec` — no legitimate use in a pipeline
  job.
- `print` instead of `logging`.
- A hard-coded `abfss://` path that differs between environments.
- A missing module docstring: a job whose target table you cannot determine
  without reading the body.

## Severity

| | Use when |
|---|---|
| `critical` | Will corrupt data or fail in production. Does not parse, double-counts, drops rows silently. |
| `high` | Wrong under a realistic condition: a duplicate key, a null, a late file. |
| `medium` | Correct today, fragile tomorrow. Hard-coded schema, missing error handling. |
| `low` | Readability, naming, docstrings. |

Any `critical` or `high` finding means `request_changes`. Do not approve with
comments and hope.

## Writing the finding

Bad: *"Consider improving error handling in the L2 job."*

Good: *"`l2_customer.py` merges without deduplicating the batch. The landing
extract contains two rows for `CustomerID` 1001, so `MERGE` will fail with a
multiple-source-match error on the first production run. Apply
`row_number()` over `BUSINESS_KEY` ordered by `ingestion_timestamp desc` and
keep row 1 before the merge."*

Problem, evidence, consequence, fix.

## Review only what you read

Do not report a finding about code you did not open, and do not invent a
problem the file does not have. A review containing one fabricated finding is
worth less than no review, because the next one will not be read.

## In this repository

`agents/review_agent.py` merges the model's findings with the static results
from `tools.py:validate_python_code`, and forces `request_changes` when any
merged finding is `critical` or `high` — so the review cannot approve around a
module that failed to parse.
