# CLAUDE.md

Guidance for Claude Code (and any AI assistant) working in this repository.

## What this is

An agentic accelerator for the data-engineering SDLC. Six specialist agents
turn a business requirement into requirements, a source-to-target mapping,
Databricks L1/L2/L3 PySpark jobs, data-quality rules, pytest suites and a
code review. The runtime is a **local `llama3.1:8b` on Ollama**
(`http://localhost:11434`).

```
SQL Server → ADF → ADLS Gen2 → Databricks L1 (bronze) → L2 (silver) → L3 (gold)
```

## Commands

```bash
python -m pytest                      # 239 tests; needs no Ollama, no Spark
python -m pytest tests/test_tools.py  # one module
python -m pytest -k dq                # one concern

python main.py --describe             # agents, tools, prompts, model routing
python main.py -r "<requirement>"     # full run against llama3.1:8b
python main.py -r "<req>" --provider stub          # offline, deterministic
python main.py -r "<req>" --stages requirements mapping
```

Run everything from the project root. `pytest.ini` sets `pythonpath = .`, and
`tests/conftest.py` puts the root on `sys.path`, so imports are flat:
`import config`, `import tools`, `from agents import ...`.

## Architecture: the three rules

**1. The mapping is the single source of truth.**
Stages 3–6 (code, DQ, tests, review) all derive from
`mapping/source_to_target_mapping.json`. If you need a new fact in generated
code, add it to the mapping schema — do not thread a second input through the
agents.

**2. Nothing is trusted because the model said it.**
Every generative stage is followed by a real tool that checks the result:
`validate_python_code` (AST + forbidden constructs), `run_data_quality_checks`
(executed against `data/landing/*.csv`), `run_pytest` (subprocess). If you add
a stage, add its gate.

**3. A degraded run is a successful run.**
Every generative stage has a deterministic fallback in `agents/fallbacks.py`.
When the model's output is unusable the stage returns `StageResult(status=
"degraded")` with an explanatory note, writes the fallback artifact, and the
pipeline continues. Never raise to signal "the model gave me rubbish" —
raising is for a genuinely broken environment.

## Where things live

| Concern | File | Note |
|---|---|---|
| Paths, endpoint, tier→model routing | `config.py` | The only place model ids appear |
| Run-scoped logging | `logger_setup.py` | Text log + JSONL event log per run |
| Chat, messages, tier resolution | `llm_client.py` | Returns `(text, message)` |
| Offline model double | `stub_llm.py` | Keyed off the `[AGENT:<key>]` marker |
| **Every tool** | `tools.py` | Implementation + schema + dispatcher, one file |
| Agent loop, output extraction | `agents/base_agent.py` | `converse`, `extract_json`, `extract_python` |
| Fact injection + validation | `agents/grounding.py` | Why the 8B model stays honest |
| Deterministic generators | `agents/fallbacks.py` | Pure, catalog-driven |
| Stage N agent | `agents/<stage>_agent.py` | One class, one `run()` |
| Run sequencing, manifest | `agents/orchestrator.py` | |
| System prompts | `prompts/*.md` | `${placeholder}`, not `{}` |

## Conventions

**Tools.** All tools go in `tools.py` — implementation, schema, and an entry
in both `TOOL_FUNCTIONS` and `TOOL_SCHEMAS`. Do not create a second tools
module. Schema descriptions are written for the model and must say *when* to
use the tool, not just what it does; `tests/test_tools.py` enforces a minimum
length. A tool returns a JSON-serialisable value or raises — `run_tools`
converts an exception into an error string the model can recover from.

**Prompts.** One `.md` per agent in `prompts/`, loaded by name. Placeholders
are `${name}` and rendered with `string.Template.safe_substitute`, because
prompts contain literal JSON full of braces. Never use `str.format` on a
prompt. If you add a placeholder, update the parametrised cases in
`tests/test_prompts.py` — an unfilled `${mapping_json}` reaching the model is
a silent, expensive failure, and that test is what catches it.

**Agents.** Subclass `BaseAgent`; set `key`, `prompt_name`, `description`,
`tool_names`; implement `run(**inputs) -> StageResult`. Register in
`agents/__init__.py:AGENT_REGISTRY` and assign a tier in
`config.AGENT_MODEL_TIER`. Call `self.converse(user, system)` for the
LLM↔tool loop. Write artifacts with `self.write(path, content)`.

**Models.** Agents name a *tier* (`deep` / `balanced` / `fast`), never a
model. `deep` is the Opus-class slot: mapping, code generation, review.
`balanced` is Sonnet-class: requirements, DQ, tests. `fast` is Haiku-class:
the run summary. Changing what a tier calls is a one-line edit in
`config.MODEL_ROUTING`.

**Logging.** `get_logger("<area>")` for prose, `log_event(name, **fields)` for
anything a report should count. Every run writes
`logs/run_<date>_<time>.log` and `logs/run_<date>_<time>.events.jsonl`.
Do not use `print` outside `main.py`'s final summary block.

**Grounding.** When an agent needs a schema, a profile or the catalog,
inject it into the first user message via `agents/grounding.py` rather than
relying on the model to call the tool. Then validate what comes back. This
is not belt-and-braces: an ungrounded `llama3.1:8b` invented `Sales.Order`
and `Sales.OrderItem` on the first real run.

**Bounded requests.** Do not ask the model for a large artifact in one reply.
The mapping agent issues one request per source table plus one for gold,
because a single all-layers reply gets truncated at `num_predict` and the
JSON becomes unparseable.

## Testing

The suite must stay runnable with **no Ollama, no Spark, no network**. Use
the `stub` provider (`stub_llm.py`) for anything that needs a model, and the
`run_dir` fixture for anything that writes artifacts.

When you change behaviour, the test should assert the *behaviour*, not the
implementation. Good: "DQ detects the duplicate business key in the landing
extract". Poor: "`_evaluate_rule` was called twice".

`data/landing/*.csv` carries deliberate defects — a duplicate `CustomerID`, a
blank email, a malformed email, a negative `TotalDue`, an out-of-domain
`OrderStatus`, an orphan `CustomerID`, a negative `OrderQty`. Tests assert on
those exact defects, so **changing the sample data breaks tests on purpose**.
If you edit a CSV, update `tests/test_tools.py` and `tests/test_grounding.py`
to match.

## Gotchas

- `TestGenerationAgent` sets `__test__ = False`; without it pytest tries to
  collect the agent class as a test suite.
- `tools.py` keeps the artifact root in a module-level `_RUN_DIR`. Tests must
  use the `run_dir` fixture, which saves and restores it.
- `run_pytest` shells out to `python -m pytest` with `cwd=PROJECT_ROOT`, so
  the accelerator's own suite triggers a nested pytest run. That is intended.
- `extract_json` deliberately salvages JSON from prose, fences and trailing
  commas. Do not "simplify" it — each branch exists because an 8B model did
  that.
- Prompt instructions wrap across lines, so assert on whitespace-normalised
  text (`" ".join(body.split())`), not on raw substrings.

## Safety

- No credentials in this repository, and none needed. If you wire the
  `anthropic` provider, read the key from the environment and keep it out of
  git. `.gitignore` already excludes `.env`, `*.key` and `secrets.json`.
- `validate_python_code` rejects `os.system`, `subprocess`, `eval`, `exec`
  and inline `dbutils.secrets.get` in *generated* code. Keep that list in
  sync with `_FORBIDDEN_PATTERNS` if the threat model changes.
- `write_artifact` refuses paths that escape the run folder. Do not relax it.
- `run_pytest` executes model-written code on the host. That is the explicit
  purpose of the test stage, but it is why the generated suites are forbidden
  from importing anything beyond the standard library and pytest.
