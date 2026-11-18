---
name: source-to-target-mapping
description: Author a source-to-target (STTM) mapping document for a medallion pipeline — the artifact that drives code generation, data quality and tests. Use when mapping SQL Server columns through bronze, silver and gold, choosing target types, or deciding a load strategy and business key.
---

# Source-to-target mapping

## Why the mapping is the artifact that matters

Pipeline code, data-quality rules and tests are all *derivable* from the
mapping. When they are written independently they drift: the rule says the
email is mandatory, the code coalesces it to empty string, the test asserts
neither. Make the mapping the single source of truth and the other three stay
consistent by construction.

## The schema

```json
{
  "mapping_id": "MAP-L2-CUSTOMER",
  "layer": "L1|L2|L3",
  "source_object": "<landing path, or catalog.schema.table>",
  "target_object": "<catalog>.<schema>.<table>",
  "business_key": ["customer_id"],
  "load_strategy": "append|merge|overwrite",
  "columns": [
    {
      "source_column": "CustomerID",
      "target_column": "customer_id",
      "target_type": "int",
      "transformation": "cast(CustomerID as int)",
      "nullable": false,
      "notes": "optional"
    }
  ],
  "notes": "optional mapping-level note"
}
```

One object per target table per layer. Three source tables produce seven
objects: three L1, three L2, one L3.

## Rules per layer

**L1** — keep source column names. Every column is `string`. Add
`source_file`, `ingestion_timestamp`, `batch_id`. `load_strategy: append`.

**L2** — rename to `snake_case`. Apply real target types. `load_strategy:
merge`, `business_key` is the snake_cased source business key.

**L3** — business names, derived measures, `load_strategy: overwrite`.

## Type mapping, SQL Server → Spark

| SQL Server | Target | Note |
|---|---|---|
| `int`, `smallint`, `tinyint` | `int` | |
| `bigint` | `bigint` | |
| `nvarchar(n)`, `varchar(n)`, `nchar(n)`, `char(n)` | `string` | Spark has no length constraint — enforce with a DQ rule if the length matters |
| `decimal(p,s)`, `numeric`, `money` | `decimal(18,2)` | Never `double` for money |
| `float`, `real` | `double` | |
| `date` | `date` | |
| `datetime`, `datetime2` | `timestamp` | |
| `bit` | `boolean` | Lands as `"0"`/`"1"`; cast explicitly |

## Profile before you map

The declared schema tells you what the source *promises*. The landed file
tells you what ADF *delivered*. Map against both.

- A column declared `int` that profiles as `string` has non-numeric values in
  it. Cast defensively and add a DQ rule; do not assume the declaration.
- A column with a high null count cannot carry `nullable: false` in L2 without
  a documented default.
- A business key whose distinct count is below the row count is not unique in
  the delivered batch. Record how you pick the survivor.

## Choosing the business key

Use the source's primary key unless you have a reason not to. The key must be
present in every row, stable across batches, and unique **after** your
deduplication rule — not necessarily unique in the raw landing file. If the
landed file has duplicates, that is expected; say in `notes` how the survivor
is chosen (usually the latest change-tracking value).

## Transformation vocabulary

Keep it small and mechanical, so a generator can implement it without guessing:

| Transformation | Meaning |
|---|---|
| `direct` | Copy unchanged |
| `cast(<col> as <type>)` | Type conversion |
| `trim(<col>)` | Strip surrounding whitespace |
| `lower(trim(<col>))` | Standardise for matching |
| `coalesce(<col>, <default>)` | Null substitution, default stated |
| `sum` / `count` / `max` | Aggregate (L3 only) |
| a SQL expression | Anything else — write it out in full |

Never write `transformation: "business logic"`. If it cannot be expressed
mechanically, it is not ready to be mapped.

## Reviewing a mapping

- [ ] Every source column either appears or is deliberately dropped
- [ ] No target column references a source column that does not exist
- [ ] Target types match the source types via the table above
- [ ] `nullable: false` only where the data supports it
- [ ] Business key is present in L2 and L3 and is in `business_key`
- [ ] Load strategy matches the layer
- [ ] L3 measures are named for what they mean
- [ ] Every non-obvious decision has a `notes` entry

## In this repository

The mapping agent (`agents/mapping_agent.py`) issues one bounded request per
source table plus one for gold, injecting the schema and the landed profile
into each. Output goes to `mapping/source_to_target_mapping.json` and a flat
`.csv` for human review.
