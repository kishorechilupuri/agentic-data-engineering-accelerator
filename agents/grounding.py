"""Grounding helpers: put the facts in front of the model, not behind a tool.

A frontier model reliably calls `get_table_schema` before naming a column.
An 8B model often does not -- it answers from the prompt and invents
plausible-sounding names like `Sales.Order` when the catalog actually says
`Sales.SalesOrderHeader`.

So the accelerator does both: tools stay available for follow-up questions,
but the facts an agent certainly needs are *pre-fetched and injected* into
its first user message. The agents then validate what comes back against the
catalog, so a hallucinated table or column is caught rather than propagated
into generated code.
"""

import tools as tool_module
from logger_setup import get_logger

logger = get_logger("grounding")


def catalog_brief(
    include_columns: bool = True, max_columns: int = 12, tables=None
) -> str:
    """A compact, factual listing of the source system for prompt injection.

    `tables` restricts the listing to a subset (any accepted spelling), which
    is how a scoped run keeps the model from reaching for tables that are out
    of scope.
    """
    try:
        catalog = tool_module._load_catalog()
    except (FileNotFoundError, ValueError) as exc:
        logger.warning("Could not load the source catalog: %s", exc)
        return "(source catalog unavailable)"

    allowed = None
    if tables:
        allowed = set(validate_table_names(tables)[0])

    lines = [
        f"Source system: {catalog.get('system')} "
        f"({catalog.get('database')} on {catalog.get('server')})",
        f"Delivered by: {catalog.get('extracted_by')} into {catalog.get('landing_zone')}",
        "",
        "These are the ONLY tables that exist. Do not reference any other table:",
        "",
    ]

    for table in catalog.get("tables", []):
        if allowed is not None and table["table_name"] not in allowed:
            continue
        lines.append(
            f"- {table['table_name']}  (landing dataset: "
            f"{table.get('landing_dataset')}, ~{table.get('row_estimate')} rows)"
        )
        lines.append(f"    business key: {', '.join(table.get('business_key', []))}")
        lines.append(f"    {table.get('description', '')}")
        for reference in table.get("foreign_keys", []):
            lines.append(
                f"    foreign key: {reference['column']} -> {reference['references']}"
            )
        if include_columns:
            columns = table.get("columns", [])
            shown = columns[:max_columns]
            rendered = ", ".join(
                f"{column['name']} {column['sql_type']}"
                f"{'' if column.get('nullable', True) else ' NOT NULL'}"
                for column in shown
            )
            lines.append(f"    columns: {rendered}")
            if len(columns) > len(shown):
                lines.append(f"    ... and {len(columns) - len(shown)} more columns")
        lines.append("")

    return "\n".join(lines)


def schema_brief(table_names) -> str:
    """Full column detail for the named tables, for mapping and code work."""
    blocks = []
    for name in table_names or []:
        try:
            table = tool_module.get_table_schema(name)
        except ValueError:
            logger.debug("Skipping unknown table in schema brief: %s", name)
            continue
        lines = [
            f"TABLE {table['table_name']}  ->  landing dataset "
            f"'{table.get('landing_dataset')}'",
            f"  business key: {', '.join(table.get('business_key', []))}",
            f"  change tracking: {table.get('change_tracking_column', 'n/a')}",
            "  columns:",
        ]
        for column in table.get("columns", []):
            lines.append(
                f"    {column['name']:<22} {column['sql_type']:<16}"
                f"{'NULL' if column.get('nullable', True) else 'NOT NULL':<10}"
                f"{column.get('description', '')}"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "(no schemas available)"


def landing_brief(datasets, max_columns: int = 14) -> str:
    """Profiles of the files ADF actually landed, for DQ and type decisions."""
    blocks = []
    for dataset in datasets or []:
        try:
            profile = tool_module.profile_landing_dataset(dataset, sample_rows=1)
        except FileNotFoundError:
            logger.debug("No landing extract to profile for %s", dataset)
            continue
        lines = [
            f"DATASET '{dataset}'  ({profile['row_count']} rows, "
            f"{profile['column_count']} columns)",
            f"  {'column':<22}{'type':<12}{'nulls':<8}{'distinct':<10}sample",
        ]
        for column in profile["columns"][:max_columns]:
            sample = (column["sample_values"] or [""])[0][:28]
            lines.append(
                f"  {column['column']:<22}{column['inferred_type']:<12}"
                f"{column['null_or_blank_count']:<8}{column['distinct_count']:<10}{sample}"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "(no landing profiles available)"


# --------------------------------------------------------------------------
# Validation: catch what the model invented anyway
# --------------------------------------------------------------------------
def known_tables() -> dict:
    """Map every acceptable spelling of a table onto its canonical name."""
    try:
        catalog = tool_module._load_catalog()
    except (FileNotFoundError, ValueError):
        return {}

    lookup = {}
    for table in catalog.get("tables", []):
        canonical = table["table_name"]
        for alias in (
            canonical,
            canonical.split(".")[-1],
            table.get("landing_dataset", ""),
        ):
            if alias:
                lookup[alias.lower()] = canonical
    return lookup


def validate_table_names(names) -> tuple[list, list]:
    """Split `names` into (recognised canonical names, hallucinated names)."""
    lookup = known_tables()
    recognised, invented = [], []
    for name in names or []:
        canonical = lookup.get(str(name).strip().lower())
        if canonical:
            if canonical not in recognised:
                recognised.append(canonical)
        else:
            invented.append(name)
    return recognised, invented


def known_columns(table_name: str) -> set:
    """Return the column names of one source table, lower-cased."""
    try:
        table = tool_module.get_table_schema(table_name)
    except ValueError:
        return set()
    return {column["name"].lower() for column in table.get("columns", [])}
