# Skills

Reusable domain playbooks for the data-engineering SDLC. Each is a
self-contained `SKILL.md` with YAML frontmatter (`name`, `description`), so it
can be loaded by Claude Code or any agent runtime that reads skill
directories — and read directly by an engineer.

These encode the *domain* judgement the accelerator applies. The prompts in
`prompts/` are the operational instructions given to each agent; the skills
here are the reference material behind them.

| Skill | Use it when |
|---|---|
| [medallion-pipeline-design](medallion-pipeline-design/SKILL.md) | Deciding what belongs in L1/L2/L3, choosing a load strategy, making a layer idempotent, writing the PySpark job |
| [source-to-target-mapping](source-to-target-mapping/SKILL.md) | Authoring the STTM document, choosing target types, picking a business key, reviewing a mapping |
| [data-quality-rules](data-quality-rules/SKILL.md) | Choosing checks and dimensions, setting severity and thresholds, diagnosing a failing rule |
| [databricks-code-review](databricks-code-review/SKILL.md) | Reviewing a medallion job for correctness, idempotency, nulls, schema evolution, performance and security |

## The through-line

All four share one principle: **the mapping is the single source of truth**.
The pipeline implements it, the DQ rules guard it, the tests pin it, and the
review checks the code against it. When those four artifacts are written
independently they drift, and the drift is where production defects live.
