"""All agent tools for the Agentic Data Engineering Accelerator.

Every tool the agents can call is defined in this one module, together with
its JSON schema and the dispatcher (`run_tool` / `run_tools`) that turns a
model's tool_calls into `role="tool"` result messages.

Tool catalogue
--------------
Discovery      get_current_datetime, list_source_tables, get_table_schema
Profiling      profile_landing_dataset
Artifacts      write_artifact, read_artifact, list_artifacts
Validation     validate_python_code, run_data_quality_checks, run_pytest
Composition    batch_tool

Schemas follow the Ollama / OpenAI function-calling shape:
``{"type": "function", "function": {"name", "description", "parameters"}}``
"""

import ast
import csv
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import config
from logger_setup import get_logger, log_event

logger = get_logger("tools")

# A backslash that does not start a valid JSON escape sequence.
_BAD_JSON_ESCAPE = re.compile(r'\\(?!["\\/bfnrtu])')


def _loads_tolerant(text: str):
    """`json.loads` that survives the escapes an 8B model gets wrong.

    Models routinely put a regex such as `^\\S+@\\S+$` inside a JSON string and
    write the backslash once, which strict JSON rejects. The first real run
    lost every data-quality rule to exactly this (`Invalid \\escape`). On a
    parse failure we double any backslash that is not already a valid escape
    and try once more; a genuinely malformed document still raises.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_BAD_JSON_ESCAPE.sub(r"\\\\", text))

# --------------------------------------------------------------------------
# Run context: where write_artifact / read_artifact are rooted
# --------------------------------------------------------------------------
_RUN_DIR = {"path": None}


def set_run_directory(path) -> Path:
    """Point artifact tools at the current run's output folder."""
    run_dir = Path(path).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    _RUN_DIR["path"] = run_dir
    logger.info("Artifact root set to %s", run_dir)
    return run_dir


def get_run_directory() -> Path:
    """Return the current artifact root, defaulting to output/scratch."""
    if _RUN_DIR["path"] is None:
        set_run_directory(config.OUTPUT_DIR / "scratch")
    return _RUN_DIR["path"]


def _safe_artifact_path(relative_path: str) -> Path:
    """Resolve `relative_path` inside the run directory, refusing escapes."""
    root = get_run_directory()
    candidate = (root / str(relative_path).lstrip("/\\")).resolve()
    if root not in candidate.parents and candidate != root:
        raise ValueError(
            f"Artifact path '{relative_path}' escapes the run directory."
        )
    return candidate


# ==========================================================================
# 1. Discovery tools
# ==========================================================================
def get_current_datetime(date_format: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Return the current local date/time formatted with `date_format`."""
    return datetime.now().strftime(date_format)


def _load_catalog() -> dict:
    if not config.SOURCE_CATALOG_PATH.exists():
        raise FileNotFoundError(
            f"Source catalog not found at {config.SOURCE_CATALOG_PATH}"
        )
    with open(config.SOURCE_CATALOG_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def list_source_tables(system: str = "sql_server") -> dict:
    """List the tables available in the source system catalog."""
    catalog = _load_catalog()
    tables = [
        {
            "table_name": table["table_name"],
            "landing_dataset": table.get("landing_dataset"),
            "business_key": table.get("business_key", []),
            "row_estimate": table.get("row_estimate"),
            "description": table.get("description", ""),
        }
        for table in catalog.get("tables", [])
        if table.get("system", "sql_server") == system
    ]
    return {"system": system, "table_count": len(tables), "tables": tables}


def get_table_schema(table_name: str) -> dict:
    """Return the full column-level schema for one source table."""
    catalog = _load_catalog()
    wanted = table_name.strip().lower()
    for table in catalog.get("tables", []):
        names = {
            table["table_name"].lower(),
            table["table_name"].split(".")[-1].lower(),
            str(table.get("landing_dataset", "")).lower(),
        }
        if wanted in names:
            return table
    available = [table["table_name"] for table in catalog.get("tables", [])]
    raise ValueError(
        f"Table '{table_name}' not in catalog. Available: {available}"
    )


# ==========================================================================
# 2. Profiling tools
# ==========================================================================
def _landing_path(dataset: str) -> Path:
    name = str(dataset).split(".")[-1].strip().lower()
    path = config.LANDING_DIR / f"{name}.csv"
    if not path.exists():
        available = sorted(p.stem for p in config.LANDING_DIR.glob("*.csv"))
        raise FileNotFoundError(
            f"No landing extract for '{dataset}'. Available: {available}"
        )
    return path


def _read_landing(dataset: str) -> tuple[list[str], list[dict]]:
    path = _landing_path(dataset)
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return fieldnames, rows


def profile_landing_dataset(dataset: str, sample_rows: int = 3) -> dict:
    """Profile one ADLS Gen2 landing extract: counts, nulls, distincts, sample.

    This is what makes the mapping and DQ agents evidence-driven instead of
    guessing: they see the real shape of the delivered file.
    """
    columns, rows = _read_landing(dataset)
    row_count = len(rows)

    column_profiles = []
    for column in columns:
        values = [(row.get(column) or "").strip() for row in rows]
        non_empty = [value for value in values if value != ""]
        column_profiles.append(
            {
                "column": column,
                "null_or_blank_count": row_count - len(non_empty),
                "distinct_count": len(set(non_empty)),
                "min_length": min((len(v) for v in non_empty), default=0),
                "max_length": max((len(v) for v in non_empty), default=0),
                "inferred_type": _infer_type(non_empty),
                "sample_values": non_empty[:3],
            }
        )

    return {
        "dataset": dataset,
        "path": str(_landing_path(dataset)),
        "row_count": row_count,
        "column_count": len(columns),
        "columns": column_profiles,
        "sample": rows[: max(0, int(sample_rows))],
    }


_INT_RE = re.compile(r"^-?\d+$")
_DEC_RE = re.compile(r"^-?\d+\.\d+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")


def _infer_type(values: list[str]) -> str:
    if not values:
        return "unknown"
    if all(_INT_RE.match(value) for value in values):
        return "integer"
    if all(_DEC_RE.match(value) or _INT_RE.match(value) for value in values):
        return "decimal"
    if all(_DATE_RE.match(value) for value in values):
        return "timestamp"
    return "string"


# ==========================================================================
# 3. Artifact tools
# ==========================================================================
def write_artifact(relative_path: str, content: str) -> dict:
    """Write `content` to `relative_path` inside the current run folder."""
    path = _safe_artifact_path(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content, indent=2)
    path.write_text(text, encoding="utf-8")
    log_event("artifact_written", path=str(path), bytes=len(text))
    logger.info("Artifact written: %s (%d bytes)", relative_path, len(text))
    return {"status": "written", "path": str(path), "bytes": len(text)}


def read_artifact(relative_path: str) -> dict:
    """Read an artifact previously written in the current run folder."""
    path = _safe_artifact_path(relative_path)
    if not path.exists():
        raise FileNotFoundError(f"Artifact not found: {relative_path}")
    return {"path": str(path), "content": path.read_text(encoding="utf-8")}


def list_artifacts(subdir: str = "") -> dict:
    """List artifacts produced so far in the current run folder."""
    root = get_run_directory()
    base = _safe_artifact_path(subdir) if subdir else root
    if not base.exists():
        return {"root": str(base), "artifacts": []}
    artifacts = [
        {
            "path": str(item.relative_to(root)).replace("\\", "/"),
            "bytes": item.stat().st_size,
        }
        for item in sorted(base.rglob("*"))
        if item.is_file()
    ]
    return {"root": str(base), "count": len(artifacts), "artifacts": artifacts}


# ==========================================================================
# 4. Validation tools
# ==========================================================================
# Patterns that should never appear in generated Databricks code.
_FORBIDDEN_PATTERNS = (
    ("os.system(", "Shell escape in generated pipeline code."),
    ("subprocess.", "Subprocess call in generated pipeline code."),
    ("eval(", "Dynamic eval in generated pipeline code."),
    ("exec(", "Dynamic exec in generated pipeline code."),
    ("dbutils.secrets.get", "Secret read should be parameterised, not inline."),
)

# Cheap heuristics for "does this look like a real Spark job".
_SPARK_HINTS = ("spark", "DataFrame", "withColumn", "write", "read")

# Columns the pipeline adds itself, so a reference to one is never an invention.
_AUDIT_COLUMNS = {
    "sourcefile", "ingestiontimestamp", "batchid", "processedtimestamp",
    "refreshedtimestamp", "rownumber",
}
_DEDUP_CALLS = {"row_number", "dropDuplicates", "drop_duplicates"}


def _norm(name) -> str:
    """Compare column names ignoring case and underscores (CustomerID == customer_id)."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _catalog_column_names() -> set:
    try:
        catalog = _load_catalog()
    except (FileNotFoundError, ValueError):
        return set()
    return {
        _norm(column["name"])
        for table in catalog.get("tables", [])
        for column in table.get("columns", [])
    }


def _dotted(node) -> str:
    """Render `a.b.c` for a Name/Attribute chain, or '' for anything else."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _const_str(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _write_has_mode(call: ast.Call) -> bool:
    """True if a `.save...()` call has `.mode(...)` somewhere earlier in its chain."""
    node = call.func
    while isinstance(node, (ast.Attribute, ast.Call)):
        if isinstance(node, ast.Call):
            if _call_name(node) == "mode":
                return True
            node = node.func
        else:
            node = node.value
    return False


def _lint_pyspark(tree, code: str, layer=None, mapping_columns=()) -> tuple[list, list]:
    """Spark-semantics lint. Returns (errors, warnings).

    `ast.parse` only proves the file is Python. The first real run produced
    modules that parsed cleanly and still could not run: a pandas-style
    `DataFrame.merge`, a table name passed to `.load()`, a two-argument
    `dbutils.widgets.get`, and an L3 job that appended on re-run. Every check
    below corresponds to something the model actually wrote.
    """
    errors, warnings = [], []
    uses_delta = any(
        (isinstance(n, ast.Name) and n.id == "DeltaTable")
        or (isinstance(n, ast.alias) and n.name == "DeltaTable")
        for n in ast.walk(tree)
    )
    allowed = (
        _catalog_column_names() | _AUDIT_COLUMNS | {_norm(c) for c in mapping_columns}
    )
    unknown_columns, modes, seen = [], set(), set()

    def flag(message: str, bucket: list = errors) -> None:
        if message not in seen:
            seen.add(message)
            bucket.append(message)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        first = _const_str(node.args[0]) if node.args else None

        if _dotted(node.func) == "dbutils.widgets.get" and (
            len(node.args) + len(node.keywords) != 1
        ):
            flag("dbutils.widgets.get takes exactly one argument (the widget "
                 "name); it has no default-value parameter.")
        elif name == "load" and first and re.fullmatch(r"\w+\.\w+\.\w+", first):
            flag(f"load('{first}') passes a table name to a path reader; use "
                 "spark.table(...) for Unity Catalog tables.")
        elif name == "merge" and not uses_delta:
            flag("DataFrame.merge does not exist in Spark (that is pandas); "
                 "use DeltaTable.merge or MERGE INTO.")
        elif (
            name == "withColumn" and len(node.args) == 2
            and _const_str(node.args[1]) is not None
        ):
            flag("withColumn needs a Column as its second argument; wrap a "
                 "literal with F.lit(...).")
        elif name == "select":
            literals = [_const_str(arg) for arg in node.args]
            literals = [value for value in literals if value is not None]
            if len(literals) != len(set(literals)):
                flag("select() lists the same column more than once, which "
                     "makes later references ambiguous.")
        elif name == "mode" and first:
            modes.add(first.lower())
        elif name in ("saveAsTable", "save") and not _write_has_mode(node):
            flag("A write has no explicit mode(); the default fails on the "
                 "second run.", warnings)
        elif name == "col" and first and allowed:
            column = first.split(".")[-1]
            if _norm(column) not in allowed:
                unknown_columns.append(first)

    if unknown_columns:
        flag("References column(s) that are in neither the source catalog nor "
             f"the mapping: {sorted(set(unknown_columns))}.")

    defined = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    guarded = any(
        isinstance(node, ast.If) and "__name__" in ast.dump(node.test)
        for node in tree.body
    )
    runs_on_import = any(
        isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and _call_name(node.value) in defined
        for node in tree.body
    )
    if runs_on_import and not guarded:
        flag("The job runs on import; guard the entry point with "
             "`if __name__ == '__main__':` so it can be tested.", warnings)

    if layer == "L1" and "append" not in modes:
        flag("L1 must append (mode('append')); bronze keeps every landed batch.")
    if layer == "L2" and not (
        any(_call_name(n) in _DEDUP_CALLS for n in ast.walk(tree) if isinstance(n, ast.Call))
        or "MERGE INTO" in code.upper()
        or (uses_delta and any(
            _call_name(n) == "merge" for n in ast.walk(tree) if isinstance(n, ast.Call)
        ))
    ):
        flag("L2 must deduplicate on the business key and merge into silver; "
             "neither a dedup nor a MERGE was found.")
    if layer == "L3" and "append" in modes:
        flag("L3 must overwrite; appending to gold double counts on a re-run.")

    return errors, warnings


def validate_python_code(
    code: str = None,
    artifact_path: str = None,
    layer: str = None,
    mapping_columns=None,
) -> dict:
    """Parse generated Python and report syntax errors plus lint findings.

    Pass either the code itself or the path of an artifact already written.
    `layer` (L1/L2/L3) switches on that layer's contract: bronze must append,
    silver must deduplicate and merge, gold must overwrite. `mapping_columns`
    is the list of target columns the mapping declares, used to spot invented
    column names. Returns `{"valid": bool, "errors": [...], "warnings": [...]}`.
    """
    if code is None and artifact_path is None:
        raise ValueError("Provide either 'code' or 'artifact_path'.")
    if code is None:
        code = read_artifact(artifact_path)["content"]

    errors, warnings = [], []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        errors.append(
            f"SyntaxError line {exc.lineno}, offset {exc.offset}: {exc.msg}"
        )
        result = {"valid": False, "errors": errors, "warnings": warnings,
                  "functions": [], "line_count": len(code.splitlines())}
        log_event("code_validation", valid=False, errors=len(errors))
        return result

    functions = [
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    ]

    for pattern, reason in _FORBIDDEN_PATTERNS:
        if pattern in code:
            errors.append(f"Forbidden construct '{pattern}': {reason}")

    lint_errors, lint_warnings = _lint_pyspark(
        tree, code, layer=layer, mapping_columns=mapping_columns or ()
    )
    errors.extend(lint_errors)
    warnings.extend(lint_warnings)

    if not any(hint in code for hint in _SPARK_HINTS):
        warnings.append("No Spark DataFrame usage detected; is this a real job?")
    if '"""' not in code and "'''" not in code:
        warnings.append("Generated module has no docstring.")
    if not functions:
        warnings.append("Generated module defines no functions; not testable.")

    result = {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "functions": functions,
        "line_count": len(code.splitlines()),
    }
    log_event(
        "code_validation",
        valid=result["valid"],
        errors=len(errors),
        warnings=len(warnings),
        functions=len(functions),
    )
    return result


def run_data_quality_checks(dataset: str, rules) -> dict:
    """Execute data-quality rules against a landing extract.

    `rules` is a list of rule dicts (or a JSON string / `{"rules": [...]}`
    wrapper). Supported checks: not_null, unique, regex, range,
    allowed_values, row_count_min, date_format, referential_integrity.
    """
    rules = _coerce_rules(rules)
    columns, rows = _read_landing(dataset)
    row_count = len(rows)

    results = []
    for rule in rules:
        rule_dataset = str(rule.get("dataset", dataset)).split(".")[-1].lower()
        if rule_dataset != str(dataset).split(".")[-1].lower():
            continue
        results.append(_evaluate_rule(rule, columns, rows, row_count))

    failed = [item for item in results if item["status"] == "fail"]
    errors = [item for item in failed if item["severity"] == "error"]

    summary = {
        "dataset": dataset,
        "row_count": row_count,
        "rules_evaluated": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "error_severity_failures": len(errors),
        "overall_status": "fail" if errors else ("warn" if failed else "pass"),
        "results": results,
    }
    log_event(
        "dq_run",
        dataset=dataset,
        evaluated=len(results),
        failed=len(failed),
        status=summary["overall_status"],
    )
    logger.info(
        "DQ %s | evaluated=%d passed=%d failed=%d | status=%s",
        dataset,
        len(results),
        summary["passed"],
        len(failed),
        summary["overall_status"],
    )
    return summary


def _coerce_rules(rules):
    if isinstance(rules, str):
        rules = _loads_tolerant(rules)
    if isinstance(rules, dict):
        rules = rules.get("rules", [])
    return list(rules or [])


def _evaluate_rule(rule, columns, rows, row_count) -> dict:
    rule_id = rule.get("rule_id", "DQ-?")
    column = rule.get("column")
    check = str(rule.get("check", "")).lower()
    params = rule.get("params") or {}
    severity = str(rule.get("severity", "error")).lower()

    base = {
        "rule_id": rule_id,
        "column": column,
        "check": check,
        "severity": severity,
        "description": rule.get("description", ""),
    }

    if check != "row_count_min" and column and column not in columns:
        return {**base, "status": "fail", "failed_count": row_count,
                "detail": f"Column '{column}' not present in dataset."}

    values = [(row.get(column) or "").strip() for row in rows] if column else []
    non_empty = [value for value in values if value != ""]
    failures = 0
    detail = ""

    if check == "not_null":
        failures = len(values) - len(non_empty)
        detail = f"{failures} null/blank values."
    elif check == "unique":
        failures = len(non_empty) - len(set(non_empty))
        detail = f"{failures} duplicate values."
    elif check == "regex":
        pattern = re.compile(params.get("pattern", ".*"))
        failures = sum(1 for value in non_empty if not pattern.match(value))
        detail = f"{failures} values not matching {pattern.pattern!r}."
    elif check == "range":
        low, high = params.get("min"), params.get("max")
        for value in non_empty:
            try:
                number = float(value)
            except ValueError:
                failures += 1
                continue
            if low is not None and number < float(low):
                failures += 1
            elif high is not None and number > float(high):
                failures += 1
        detail = f"{failures} values outside [{low}, {high}]."
    elif check == "allowed_values":
        allowed = {str(value) for value in params.get("values", [])}
        failures = sum(1 for value in non_empty if value not in allowed)
        detail = f"{failures} values outside the allowed set."
    elif check == "row_count_min":
        minimum = int(params.get("min", 1))
        failures = 0 if row_count >= minimum else 1
        detail = f"row_count={row_count}, minimum={minimum}."
    elif check == "date_format":
        fmt = params.get("format", "%Y-%m-%d")
        for value in non_empty:
            try:
                datetime.strptime(value, fmt)
            except ValueError:
                failures += 1
        detail = f"{failures} values not parseable as {fmt}."
    elif check == "referential_integrity":
        ref_dataset = params.get("reference_dataset")
        ref_column = params.get("reference_column", column)
        try:
            _, ref_rows = _read_landing(ref_dataset)
        except (FileNotFoundError, AttributeError) as exc:
            return {**base, "status": "fail", "failed_count": 0,
                    "detail": f"Reference unavailable: {exc}"}
        reference = {(row.get(ref_column) or "").strip() for row in ref_rows}
        failures = sum(1 for value in non_empty if value not in reference)
        detail = f"{failures} values missing from {ref_dataset}.{ref_column}."
    else:
        return {**base, "status": "skipped", "failed_count": 0,
                "detail": f"Unsupported check '{check}'."}

    return {
        **base,
        "status": "pass" if failures == 0 else "fail",
        "failed_count": failures,
        "detail": detail,
    }


def run_pytest(target_path: str, extra_args: str = "-q") -> dict:
    """Run pytest against a generated test file or directory.

    `target_path` is relative to the current run folder. Returns the exit
    code and the tail of the captured output.
    """
    path = _safe_artifact_path(target_path)
    if not path.exists():
        raise FileNotFoundError(f"Test target not found: {target_path}")

    # `-o addopts=` neutralises this project's pytest.ini. Without it, a target
    # inside the project picks up `-q` from the ini and the `-q` below makes
    # `-qq`, which suppresses the "N passed" summary line entirely.
    command = [sys.executable, "-m", "pytest", str(path), "-o", "addopts="]
    command += [arg for arg in str(extra_args).split() if arg]

    logger.info("Running pytest: %s", " ".join(command))
    try:
        completed = subprocess.run(
            command,
            cwd=str(config.PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
            # Keep __pycache__ out of the run folder.
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        stdout, stderr, code = completed.stdout, completed.stderr, completed.returncode
    except subprocess.TimeoutExpired:
        stdout, stderr, code = "", "pytest timed out after 300s", -1

    output = (stdout + stderr).strip()
    tail = "\n".join(output.splitlines()[-25:])
    result = {
        "target": str(path),
        "exit_code": code,
        "passed": code == 0,
        "summary": _pytest_summary(output),
        "output_tail": tail,
    }
    log_event(
        "pytest_run",
        target=target_path,
        exit_code=code,
        summary=result["summary"],
    )
    logger.info("pytest %s | exit=%s | %s", target_path, code, result["summary"])
    return result


_SUMMARY_RE = re.compile(r"^(=+ .*(passed|failed|error).* =+)$", re.MULTILINE)


def _pytest_summary(output: str) -> str:
    matches = _SUMMARY_RE.findall(output)
    if matches:
        return matches[-1][0].strip("= ").strip()
    for line in reversed(output.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            return line.strip()
    return "no summary line"


# ==========================================================================
# 5. Composition tool
# ==========================================================================
def batch_tool(invocations) -> list:
    """Invoke several other tools in one turn.

    Each invocation is `{"name": <tool>, "arguments": <JSON string or dict>}`.
    """
    if isinstance(invocations, str):
        invocations = _loads_tolerant(invocations)
    results = []
    for invocation in invocations or []:
        name = invocation.get("name")
        arguments = invocation.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = _loads_tolerant(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as exc:
                results.append({"name": name, "error": f"Bad arguments JSON: {exc}"})
                continue
        try:
            results.append({"name": name, "result": run_tool(name, arguments)})
        except Exception as exc:  # surfaced back to the model, not raised
            results.append({"name": name, "error": f"{type(exc).__name__}: {exc}"})
    return results


# ==========================================================================
# Tool schemas
# ==========================================================================
get_current_datetime_schema = {
    "type": "function",
    "function": {
        "name": "get_current_datetime",
        "description": (
            "Retrieves the current date and time formatted according to the "
            "specified format string. Use it whenever an artifact needs to be "
            "stamped with the run date/time or a question refers to today, "
            "now, or the current date. Never guess the date."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date_format": {
                    "type": "string",
                    "description": (
                        "Python strftime format. '%Y-%m-%d' returns only the "
                        "date, '%H:%M:%S' only the time, and the default "
                        "'%Y-%m-%d %H:%M:%S' returns both."
                    ),
                }
            },
        },
    },
}

list_source_tables_schema = {
    "type": "function",
    "function": {
        "name": "list_source_tables",
        "description": (
            "Lists every table available in the source system catalog, with "
            "its business key, landing dataset name, row estimate and "
            "description. Call this first when you need to know what source "
            "data exists before designing a mapping or a pipeline."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "system": {
                    "type": "string",
                    "description": "Source system to list. Defaults to 'sql_server'.",
                }
            },
        },
    },
}

get_table_schema_schema = {
    "type": "function",
    "function": {
        "name": "get_table_schema",
        "description": (
            "Returns the full column-level schema of one source table: column "
            "names, SQL Server data types, nullability, primary key, business "
            "key and change-tracking column. Use it before writing any "
            "source-to-target mapping or ingestion code so column names and "
            "types are never invented."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": (
                        "Table to describe. Accepts the qualified name "
                        "('Sales.Customer'), the bare name ('Customer') or the "
                        "landing dataset name ('customer')."
                    ),
                }
            },
            "required": ["table_name"],
        },
    },
}

profile_landing_dataset_schema = {
    "type": "function",
    "function": {
        "name": "profile_landing_dataset",
        "description": (
            "Profiles a file that ADF has landed in ADLS Gen2: row count, and "
            "per column the null/blank count, distinct count, min and max "
            "length, inferred type and sample values, plus a few sample rows. "
            "Use it to ground data-quality rules and type casts in the data "
            "that actually arrived rather than in the declared schema."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "dataset": {
                    "type": "string",
                    "description": (
                        "Landing dataset name, e.g. 'customer', "
                        "'sales_order_header' or 'sales_order_detail'."
                    ),
                },
                "sample_rows": {
                    "type": "number",
                    "description": "How many sample rows to return. Defaults to 3.",
                },
            },
            "required": ["dataset"],
        },
    },
}

write_artifact_schema = {
    "type": "function",
    "function": {
        "name": "write_artifact",
        "description": (
            "Writes a file into the current run's output folder. Use it to "
            "persist deliverables such as generated PySpark jobs, mapping "
            "documents, data-quality rule sets and test files. Parent "
            "directories are created automatically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "relative_path": {
                    "type": "string",
                    "description": (
                        "Path inside the run folder, e.g. "
                        "'pipelines/l1_customer.py' or 'mapping/sttm.json'."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Full file content to write.",
                },
            },
            "required": ["relative_path", "content"],
        },
    },
}

read_artifact_schema = {
    "type": "function",
    "function": {
        "name": "read_artifact",
        "description": (
            "Reads back a file previously written into the current run's "
            "output folder. Use it to review generated code or to reuse an "
            "earlier stage's output."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "relative_path": {
                    "type": "string",
                    "description": "Path inside the run folder to read.",
                }
            },
            "required": ["relative_path"],
        },
    },
}

list_artifacts_schema = {
    "type": "function",
    "function": {
        "name": "list_artifacts",
        "description": (
            "Lists the artifacts produced so far in the current run, with "
            "their sizes. Use it to discover what earlier agents generated."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subdir": {
                    "type": "string",
                    "description": "Optional sub-folder to restrict the listing to.",
                }
            },
        },
    },
}

validate_python_code_schema = {
    "type": "function",
    "function": {
        "name": "validate_python_code",
        "description": (
            "Checks generated Python and reports syntax errors, forbidden "
            "constructs (shell escapes, eval/exec, inline secret reads), "
            "Spark mistakes that parse but cannot run (DataFrame.merge, a "
            "table name passed to load(), a two-argument "
            "dbutils.widgets.get, duplicate select columns, invented column "
            "names) and, when a layer is given, that layer's contract: L1 "
            "appends, L2 deduplicates and merges, L3 overwrites. Call it on "
            "every generated pipeline file before declaring it done, and fix "
            "anything it reports."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The Python source to validate.",
                },
                "layer": {
                    "type": "string",
                    "description": (
                        "Medallion layer the module implements: 'L1', 'L2' "
                        "or 'L3'. Enables that layer's load-strategy checks."
                    ),
                },
                "artifact_path": {
                    "type": "string",
                    "description": (
                        "Alternative to 'code': path of an already written "
                        "artifact inside the run folder."
                    ),
                },
            },
        },
    },
}

run_data_quality_checks_schema = {
    "type": "function",
    "function": {
        "name": "run_data_quality_checks",
        "description": (
            "Executes data-quality rules against a landed dataset and returns "
            "a per-rule pass/fail result with failure counts. Supported "
            "checks: not_null, unique, regex, range, allowed_values, "
            "row_count_min, date_format and referential_integrity. Use it to "
            "prove that the rules you authored actually hold on real data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "dataset": {
                    "type": "string",
                    "description": "Landing dataset to check, e.g. 'customer'.",
                },
                "rules": {
                    "type": "string",
                    "description": (
                        "JSON array of rule objects, or a JSON object with a "
                        "'rules' array. Each rule has rule_id, dataset, "
                        "column, check, params, severity and description."
                    ),
                },
            },
            "required": ["dataset", "rules"],
        },
    },
}

run_pytest_schema = {
    "type": "function",
    "function": {
        "name": "run_pytest",
        "description": (
            "Runs pytest against a generated test file or folder inside the "
            "current run and returns the exit code, the summary line and the "
            "tail of the output. Use it to confirm that generated test cases "
            "actually execute and pass."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_path": {
                    "type": "string",
                    "description": (
                        "Path inside the run folder, e.g. 'tests/test_l2_customer.py'."
                    ),
                },
                "extra_args": {
                    "type": "string",
                    "description": "Extra pytest arguments. Defaults to '-q'.",
                },
            },
            "required": ["target_path"],
        },
    },
}

batch_tool_schema = {
    "type": "function",
    "function": {
        "name": "batch_tool",
        "description": "Invoke multiple other tool calls simultaneously.",
        "parameters": {
            "type": "object",
            "properties": {
                "invocations": {
                    "type": "array",
                    "description": "The tool calls to invoke",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "The name of the tool to invoke",
                            },
                            "arguments": {
                                "type": "string",
                                "description": (
                                    "The arguments to the tool, encoded as a "
                                    "JSON string"
                                ),
                            },
                        },
                        "required": ["name", "arguments"],
                    },
                }
            },
            "required": ["invocations"],
        },
    },
}

# Name -> implementation, and name -> schema. Single source of truth.
TOOL_FUNCTIONS = {
    "get_current_datetime": get_current_datetime,
    "list_source_tables": list_source_tables,
    "get_table_schema": get_table_schema,
    "profile_landing_dataset": profile_landing_dataset,
    "write_artifact": write_artifact,
    "read_artifact": read_artifact,
    "list_artifacts": list_artifacts,
    "validate_python_code": validate_python_code,
    "run_data_quality_checks": run_data_quality_checks,
    "run_pytest": run_pytest,
    "batch_tool": batch_tool,
}

TOOL_SCHEMAS = {
    "get_current_datetime": get_current_datetime_schema,
    "list_source_tables": list_source_tables_schema,
    "get_table_schema": get_table_schema_schema,
    "profile_landing_dataset": profile_landing_dataset_schema,
    "write_artifact": write_artifact_schema,
    "read_artifact": read_artifact_schema,
    "list_artifacts": list_artifacts_schema,
    "validate_python_code": validate_python_code_schema,
    "run_data_quality_checks": run_data_quality_checks_schema,
    "run_pytest": run_pytest_schema,
    "batch_tool": batch_tool_schema,
}


def schemas_for(names) -> list:
    """Return the schema list for `names`, raising on an unknown tool."""
    selected = []
    for name in names:
        if name not in TOOL_SCHEMAS:
            raise ValueError(f"Unknown tool '{name}'. Known: {sorted(TOOL_SCHEMAS)}")
        selected.append(TOOL_SCHEMAS[name])
    return selected


# ==========================================================================
# Dispatcher
# ==========================================================================
def run_tool(tool_name: str, tool_input: dict):
    """Execute one tool by name with a dict of arguments."""
    if tool_name not in TOOL_FUNCTIONS:
        raise ValueError(f"Unknown tool name: {tool_name}")
    logger.debug("run_tool %s(%s)", tool_name, tool_input)
    return TOOL_FUNCTIONS[tool_name](**(tool_input or {}))


def run_tools(message) -> list:
    """Turn a model message's tool_calls into role='tool' result messages.

    Accepts an Ollama Message, a plain dict, or a messages list (in which case
    the last message is used). Tool failures are returned to the model as
    text so it can recover, rather than raising.
    """
    if isinstance(message, list):
        message = message[-1] if message else None
    if message is None:
        return []
    if isinstance(message, dict):
        tool_calls = message.get("tool_calls") or []
    else:
        tool_calls = getattr(message, "tool_calls", None) or []

    tool_result_messages = []
    for call in tool_calls:
        func = call["function"] if isinstance(call, dict) else call.function
        tool_name = func["name"] if isinstance(func, dict) else func.name
        raw_arguments = func["arguments"] if isinstance(func, dict) else func.arguments
        if isinstance(raw_arguments, str):
            try:
                tool_input = _loads_tolerant(raw_arguments) if raw_arguments.strip() else {}
            except json.JSONDecodeError:
                tool_input = {}
        else:
            tool_input = dict(raw_arguments or {})

        started = datetime.now()
        try:
            content = json.dumps(run_tool(tool_name, tool_input), default=str)
            status = "ok"
        except Exception as exc:
            content = f"Error: {type(exc).__name__}: {exc}"
            status = "error"
            logger.warning("Tool %s failed: %s", tool_name, exc)

        log_event(
            "tool_call",
            tool=tool_name,
            status=status,
            arguments=list(tool_input.keys()),
            elapsed_seconds=round((datetime.now() - started).total_seconds(), 3),
        )
        tool_result_messages.append(
            {"role": "tool", "tool_name": tool_name, "content": content}
        )

    return tool_result_messages
