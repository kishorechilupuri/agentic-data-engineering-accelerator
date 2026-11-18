---
name: data-quality-rules
description: Author and execute data-quality rules for a medallion pipeline across the six DQ dimensions. Use when deciding what to check at which layer, setting a severity that blocks or warns, choosing thresholds from a data profile, or diagnosing a failing rule.
---

# Data quality rules

## The six dimensions

| Dimension | Question | Checks |
|---|---|---|
| Completeness | Is the value there? | `not_null` |
| Uniqueness | Is the key unique? | `unique` |
| Validity | Is it the right shape? | `regex`, `date_format`, `allowed_values` |
| Accuracy | Is the value plausible? | `range` |
| Consistency | Does it agree with related data? | `referential_integrity` |
| Volume | Did we get the right amount? | `row_count_min` |

Covering fewer than four of these is not a rule set, it is a null check.

## Rule schema

```json
{
  "rule_id": "DQ-001",
  "dataset": "customer",
  "layer": "L1",
  "column": "CustomerID",
  "check": "not_null",
  "params": {},
  "severity": "error",
  "dimension": "completeness",
  "description": "Business key must always be present."
}
```

| Check | `params` |
|---|---|
| `not_null`, `unique` | — |
| `regex` | `{"pattern": "..."}` |
| `range` | `{"min": 0, "max": 100}` (either bound optional) |
| `allowed_values` | `{"values": ["Open", "Shipped"]}` |
| `row_count_min` | `{"min": 1}` |
| `date_format` | `{"format": "%Y-%m-%d"}` |
| `referential_integrity` | `{"reference_dataset": "customer", "reference_column": "CustomerID"}` |

## Severity is a promotion decision, not a feeling

| | Meaning | Effect |
|---|---|---|
| `error` | The downstream layer would be wrong if this data passed | Blocks promotion |
| `warn` | Worth knowing, not worth stopping the business for | Logged, pipeline continues |

Use `error` for: business key null or duplicate, empty delivery, a mandatory
foreign key with no parent, a measure that is negative when it cannot be.

Use `warn` for: format heuristics (an email pattern is a guess about the
world, not a contract), optional-field completeness, distribution drift.

Getting this wrong is the main way a DQ framework gets switched off: a
3 a.m. page because 2% of emails lack an `@` trains everyone to ignore it.

## Set thresholds from the profile, not from the ideal

Profile the landed file first, then write the rule.

- A column that is 40% blank today gets a `warn`, with a ticket to fix the
  source. An `error` just means the pipeline never runs.
- `row_count_min` is set from observed volume with headroom, not from 1. A
  table that normally lands 140,000 rows and lands 12 has a broken extract,
  and `min: 1` will not catch it.
- A `range` bound comes from the business rule (`OrderQty >= 1`), not from
  the observed max.

## Which layer to check at

| Layer | Check for |
|---|---|
| L1 | Volume, arrival, parseability. The question is "did the extract work?" |
| L2 | The real gate: key uniqueness, mandatory fields, types, domains, referential integrity. This is where bad data stops. |
| L3 | Reconciliation: do the gold measures tie back to silver? |

## Reading a failure

A failing rule means one of two things, and you must decide which:

1. **The data is bad.** Keep the rule. It is doing its job. Fix the source or
   quarantine the rows.
2. **The rule is wrong.** Wrong column name, a regex that is too strict, a
   `date_format` that does not match how the column actually lands. Fix the
   rule and re-run.

The common instance of (2): a `datetime2` column lands as
`2026-01-15 09:30:00`, but the rule says `"%Y-%m-%d"` and every row fails.
Match the pattern to what the profile shows.

## Checklist

- [ ] Every business key has both `not_null` and `unique`
- [ ] Every dataset has `row_count_min` with a real threshold
- [ ] Every foreign key has `referential_integrity`
- [ ] Every monetary or quantity column has a `range`
- [ ] At least four dimensions are covered
- [ ] Every `error` rule would genuinely corrupt the next layer if it passed
- [ ] Every rule has a `description` saying what it protects
- [ ] Every rule has been executed, not just written

## In this repository

The engine is `tools.py:run_data_quality_checks`, executing against
`data/landing/*.csv`. The landing extracts carry deliberate defects — a
duplicate `CustomerID`, a blank and a malformed email, a negative `TotalDue`,
an out-of-domain `OrderStatus`, an orphan `CustomerID`, a negative `OrderQty`
— so a rule set that reports everything green is wrong.
