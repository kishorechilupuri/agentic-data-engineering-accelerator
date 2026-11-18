"""Stage 3 -- Databricks Pipeline Code Generation Agent.

Generates one PySpark module per mapping object (L1, L2, L3), validates each
one with the AST-based `validate_python_code` tool, gives the model one
chance to repair a broken module, and falls back to a deterministic template
if the repair also fails.

Model tier: `deep` (Opus-class) -- code generation is the least forgiving
stage in the pipeline.
"""

import json

import config
import tools as tool_module
from agents.base_agent import (
    STATUS_DEGRADED,
    STATUS_OK,
    BaseAgent,
    StageResult,
    extract_python,
)
from agents.fallbacks import fallback_pipeline_code
from llm_client import add_assistant_message, add_user_message


class PipelineCodeGenAgent(BaseAgent):
    key = "pipeline_codegen"
    prompt_name = "pipeline_codegen"
    description = "Generates Databricks PySpark jobs for L1, L2 and L3."
    tool_names = (
        "get_table_schema",
        "profile_landing_dataset",
        "validate_python_code",
    )

    def run(self, mapping: dict) -> StageResult:
        mappings = mapping.get("mappings", [])
        self.logger.info("Generating pipeline code for %d mapping object(s).", len(mappings))

        artifacts, notes, validations = [], [], {}
        total_turns, all_calls = 0, []
        degraded = False

        for item in mappings:
            layer = item.get("layer", "L1")
            module_name = _module_name(item)
            relative_path = f"pipelines/{module_name}.py"

            code, turns, calls, fell_back, note = self._generate_one(item, layer)
            total_turns += turns
            all_calls.extend(calls)
            if note:
                notes.append(note)
            degraded = degraded or fell_back

            self.write(relative_path, code)
            validation = tool_module.validate_python_code(code=code)
            validations[relative_path] = validation
            artifacts.append(relative_path)

            self.logger.info(
                "%s -> %s | valid=%s | errors=%d | warnings=%d",
                layer,
                relative_path,
                validation["valid"],
                len(validation["errors"]),
                len(validation["warnings"]),
            )

        self.write("pipelines/validation_report.json", validations)
        artifacts.append("pipelines/validation_report.json")

        invalid = [path for path, result in validations.items() if not result["valid"]]
        if invalid:
            notes.append(f"Modules still failing static validation: {invalid}")

        status = STATUS_DEGRADED if (degraded or invalid) else STATUS_OK
        return StageResult(
            agent=self.key,
            status=status,
            output={"modules": artifacts, "validation": validations},
            artifacts=artifacts,
            notes=notes,
            turns=total_turns,
            tool_calls=all_calls,
        )

    # ------------------------------------------------------------------
    def _generate_one(self, item: dict, layer: str):
        """Generate, validate and if necessary repair one module."""
        system = self.build_system(
            layer=layer,
            layer_intent=config.LAYER_INTENT.get(layer, ""),
            mapping_json=json.dumps(item, indent=2)[:6000],
            catalog=config.CATALOG,
        )
        user = (
            f"Generate the complete {layer} PySpark module for "
            f"`{item.get('target_object')}` from the mapping in your "
            "instructions.\n\n"
            "Target columns, in order, exactly as the mapping declares them:\n"
            + "\n".join(
                f"  {column.get('target_column')} "
                f"<- {column.get('source_column')} "
                f"[{column.get('target_type')}] via {column.get('transformation')}"
                for column in item.get("columns", [])
            )
            + "\n\nReply with the final module in a single ```python fenced block."
        )

        text, messages, turns, calls = self.converse(user, system)
        code = extract_python(text)

        if code:
            validation = tool_module.validate_python_code(code=code)
            if validation["valid"]:
                return code, turns, calls, False, None

            # One repair attempt, handing the model its own errors back.
            for _ in range(config.MAX_REPAIR_ATTEMPTS):
                self.logger.warning(
                    "%s module failed validation (%s); requesting a repair.",
                    layer,
                    validation["errors"],
                )
                add_user_message(
                    messages,
                    "Your module failed validation with these errors:\n"
                    + "\n".join(validation["errors"])
                    + "\n\nReturn the corrected, complete module in a single "
                    "```python fenced block. No commentary.",
                )
                repair_text, repair_message = self.llm.chat(
                    messages,
                    system=system,
                    tier=self.tier,
                    agent=self.key,
                )
                add_assistant_message(messages, repair_message)
                turns += 1
                repaired = extract_python(repair_text)
                if repaired:
                    validation = tool_module.validate_python_code(code=repaired)
                    if validation["valid"]:
                        return repaired, turns, calls, False, (
                            f"{layer} module needed one repair round before it parsed."
                        )
                    code = repaired

        note = (
            f"{layer} module could not be generated as valid Python by the "
            "model; a deterministic template was rendered from the mapping."
        )
        self.logger.warning(note)
        return fallback_pipeline_code(layer, item), turns, calls, True, note


def _module_name(item: dict) -> str:
    layer = str(item.get("layer", "l1")).lower()
    target = str(item.get("target_object", "unknown.table"))
    entity = target.split(".")[-1] or "entity"
    return f"{layer}_{entity}"
