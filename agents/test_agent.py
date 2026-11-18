"""Stage 5 -- Test Generation Agent.

Generates a pytest module per layer that pins the rules declared in the
mapping, writes them into the run folder, and then actually runs pytest on
them. A generated test that does not execute is not a test.

Model tier: `balanced` (Sonnet-class) -- test authoring from a known mapping.
"""

import json

import tools as tool_module
from agents.base_agent import (
    STATUS_DEGRADED,
    STATUS_OK,
    BaseAgent,
    StageResult,
    extract_python,
)
from agents.fallbacks import fallback_tests


class TestGenerationAgent(BaseAgent):
    # Its name starts with "Test", so pytest would otherwise try to collect
    # this agent as a test class.
    __test__ = False

    key = "test_generator"
    prompt_name = "test_generator"
    description = "Generates and runs pytest suites for the generated pipelines."
    tool_names = ("validate_python_code",)

    def run(self, mapping: dict) -> StageResult:
        mappings = mapping.get("mappings", [])
        self.logger.info("Generating test suites for %d mapping object(s).", len(mappings))

        artifacts, notes = [], []
        total_turns, all_calls = 0, []
        degraded = False

        for item in mappings:
            layer = item.get("layer", "L1")
            module_name = _module_name(item)
            relative_path = f"tests/test_{module_name}.py"

            code, turns, calls, fell_back, note = self._generate_one(item, layer, module_name)
            total_turns += turns
            all_calls.extend(calls)
            if note:
                notes.append(note)
            degraded = degraded or fell_back

            self.write(relative_path, code)
            artifacts.append(relative_path)

        # Run the whole generated suite in one pytest invocation.
        execution = tool_module.run_pytest("tests", extra_args="-q --no-header")
        report_path = self.write("tests/pytest_report.json", execution)
        artifacts.append(report_path)

        if not execution.get("passed"):
            notes.append(
                f"Generated test suite did not pass: {execution.get('summary')}"
            )
            degraded = True

        self.logger.info(
            "Test generation complete | suites=%d | pytest_exit=%s | %s",
            len(mappings),
            execution.get("exit_code"),
            execution.get("summary"),
        )

        return StageResult(
            agent=self.key,
            status=STATUS_DEGRADED if degraded else STATUS_OK,
            output={"suites": artifacts, "execution": execution},
            artifacts=artifacts,
            notes=notes,
            turns=total_turns,
            tool_calls=all_calls,
        )

    # ------------------------------------------------------------------
    def _generate_one(self, item: dict, layer: str, module_name: str):
        system = self.build_system(
            layer=layer,
            module_name=module_name,
            mapping_json=json.dumps(item, indent=2)[:5000],
        )
        user = (
            f"Write the pytest module for the {layer} mapping of "
            f"`{item.get('target_object')}`. Standard library and pytest only. "
            "Reply with a single ```python fenced block."
        )

        text, _messages, turns, calls = self.converse(user, system)
        code = extract_python(text)

        if code:
            validation = tool_module.validate_python_code(code=code)
            if validation["valid"] and _looks_like_tests(code):
                return code, turns, calls, False, None
            reason = (
                validation["errors"]
                if not validation["valid"]
                else "no executable test functions found"
            )
            note = (
                f"{layer} test module was unusable ({reason}); a deterministic "
                "suite was rendered from the mapping."
            )
        else:
            note = (
                f"{layer} test module could not be extracted from the model "
                "reply; a deterministic suite was rendered from the mapping."
            )

        self.logger.warning(note)
        return fallback_tests(layer, item), turns, calls, True, note


_BANNED_IMPORTS = ("pyspark", "databricks", "delta", "pandas", "numpy")


def _looks_like_tests(code: str) -> bool:
    """A usable generated suite defines real tests and imports nothing heavy."""
    if "def test_" not in code:
        return False
    lowered = code.lower()
    if any(f"import {name}" in lowered for name in _BANNED_IMPORTS):
        return False
    body = code.replace("assert True", "")
    return "assert" in body


def _module_name(item: dict) -> str:
    layer = str(item.get("layer", "l1")).lower()
    target = str(item.get("target_object", "unknown.table"))
    entity = target.split(".")[-1] or "entity"
    return f"{layer}_{entity}"
