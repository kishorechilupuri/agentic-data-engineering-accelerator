"""Stage 1 -- Requirement Refinement Agent.

Turns a one-line business request into a structured, testable requirement
specification grounded in the real source catalog.

Model tier: `balanced` (Sonnet-class) -- structured drafting, not deep
reasoning.
"""

import json

from agents.base_agent import (
    STATUS_DEGRADED,
    STATUS_OK,
    BaseAgent,
    StageResult,
    extract_json,
)
from agents.fallbacks import fallback_requirements
from agents.grounding import catalog_brief, validate_table_names

REQUIRED_KEYS = ("summary", "functional_requirements")


class RequirementRefinerAgent(BaseAgent):
    key = "requirement_refiner"
    prompt_name = "requirement_refiner"
    description = "Refines a business request into a testable specification."
    tool_names = (
        "list_source_tables",
        "get_table_schema",
        "get_current_datetime",
        "batch_tool",
    )

    def run(self, requirement: str, allowed_tables=None) -> StageResult:
        """Refine `requirement`, optionally scoped to `allowed_tables`."""
        self.logger.info("Refining requirement: %s", requirement[:120])
        if allowed_tables:
            self.logger.info("Run is scoped to source tables: %s", allowed_tables)

        system = self.build_system(requirement=requirement)
        # Inject the catalog rather than trusting an 8B model to go and fetch
        # it: an ungrounded model invents table names that look right. A
        # scoped run sees only the tables in scope.
        user = (
            "SOURCE CATALOG (authoritative -- these are the only tables that "
            "exist):\n\n"
            f"{catalog_brief(tables=allowed_tables)}\n\n"
            "Using only the tables above, produce the requirement "
            "specification JSON described in your instructions. Reply with the "
            "JSON object only."
        )

        text, _messages, turns, calls = self.converse(user, system)
        spec = extract_json(text)
        notes = []
        status = STATUS_OK

        if not self._usable(spec):
            self.logger.warning(
                "No parseable specification returned; using deterministic fallback."
            )
            notes.append(
                "Model reply was not a parseable requirement specification; "
                "a catalog-derived fallback specification was used."
            )
            spec = fallback_requirements(requirement)
            status = STATUS_DEGRADED

        spec = self._normalise(spec, requirement)

        # Catch tables the model invented despite being given the catalog.
        recognised, invented = validate_table_names(spec.get("source_tables"))
        if invented:
            self.logger.warning(
                "Discarding source tables that are not in the catalog: %s", invented
            )
            notes.append(
                f"Model referenced non-existent source tables {invented}; they "
                "were discarded and the specification was grounded to the catalog."
            )
            spec["assumptions"].append(
                f"Hallucinated source tables {invented} were removed during "
                "catalog validation."
            )
            status = STATUS_DEGRADED
        # A scoped run is a hard boundary: anything outside it is dropped even
        # if it is a real table, so the run stays the size the operator asked for.
        if allowed_tables:
            in_scope = set(validate_table_names(allowed_tables)[0])
            out_of_scope = [name for name in recognised if name not in in_scope]
            if out_of_scope:
                self.logger.info(
                    "Dropping out-of-scope source tables: %s", out_of_scope
                )
                notes.append(
                    f"Source tables {out_of_scope} are real but outside the "
                    "requested scope; they were dropped."
                )
            recognised = [name for name in recognised if name in in_scope]
            fallback_tables = sorted(in_scope)
        else:
            fallback_tables = _all_catalog_tables()

        # If nothing usable survived, fall back rather than handing the mapping
        # agent an empty source list.
        spec["source_tables"] = recognised or fallback_tables

        path = self.write("requirements/requirements.json", spec)

        summary_path = self.write(
            "requirements/requirements.md", _render_markdown(spec)
        )

        self.logger.info(
            "Requirements ready | functional=%d | non_functional=%d | status=%s",
            len(spec.get("functional_requirements", [])),
            len(spec.get("non_functional_requirements", [])),
            status,
        )
        return StageResult(
            agent=self.key,
            status=status,
            output=spec,
            artifacts=[path, summary_path],
            notes=notes,
            turns=turns,
            tool_calls=calls,
        )

    @staticmethod
    def _usable(spec) -> bool:
        if not isinstance(spec, dict):
            return False
        if not all(key in spec for key in REQUIRED_KEYS):
            return False
        requirements = spec.get("functional_requirements")
        return isinstance(requirements, list) and len(requirements) > 0

    @staticmethod
    def _normalise(spec: dict, requirement: str) -> dict:
        """Fill in the fields the downstream stages rely on."""
        spec.setdefault("epic", "Unnamed data engineering epic")
        spec.setdefault("source_tables", [])
        spec.setdefault("target_entities", [])
        spec.setdefault("non_functional_requirements", [])
        spec.setdefault("assumptions", [])
        spec.setdefault("open_questions", [])
        spec["_original_request"] = requirement

        for index, item in enumerate(spec.get("functional_requirements", []), 1):
            if isinstance(item, dict):
                item.setdefault("id", f"FR-{index:03d}")
                item.setdefault("acceptance_criteria", [])
        return spec


def _all_catalog_tables() -> list:
    """Every table name in the source catalog, canonical spelling."""
    import tools as tool_module

    try:
        return [
            table["table_name"]
            for table in tool_module.list_source_tables()["tables"]
        ]
    except Exception:
        return []


def _render_markdown(spec: dict) -> str:
    """A reviewable markdown view of the specification."""
    lines = [
        f"# {spec.get('epic', 'Requirements')}",
        "",
        spec.get("summary", ""),
        "",
        "## Source tables",
        "",
    ]
    lines += [f"- `{table}`" for table in spec.get("source_tables", [])] or ["- none"]
    lines += ["", "## Target entities", ""]
    for entity in spec.get("target_entities", []):
        if isinstance(entity, dict):
            lines.append(
                f"- **{entity.get('layer', '?')}** `{entity.get('name', '?')}` "
                f"-- {entity.get('grain', '')}"
            )
    lines += ["", "## Functional requirements", ""]
    for item in spec.get("functional_requirements", []):
        if not isinstance(item, dict):
            continue
        lines.append(f"### {item.get('id')} ({item.get('layer', 'n/a')})")
        lines.append("")
        lines.append(item.get("statement", ""))
        lines.append("")
        for criterion in item.get("acceptance_criteria", []):
            lines.append(f"- [ ] {criterion}")
        lines.append("")
    lines += ["## Non-functional requirements", ""]
    for item in spec.get("non_functional_requirements", []):
        if isinstance(item, dict):
            lines.append(f"- **{item.get('id')}** {item.get('statement', '')}")
    lines += ["", "## Assumptions", ""]
    lines += [f"- {item}" for item in spec.get("assumptions", [])] or ["- none"]
    lines += ["", "## Open questions", ""]
    lines += [f"- {item}" for item in spec.get("open_questions", [])] or ["- none"]
    return "\n".join(lines) + "\n"
