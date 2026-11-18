"""Stage 4 -- Data Quality Agent.

Authors the data-quality rule set from the mapping and the landing profiles,
then executes every rule against the real landed extracts so the rules are
proven rather than merely written.

Model tier: `balanced` (Sonnet-class) -- rule authoring from a known schema.
"""

import json

import tools as tool_module
from agents.base_agent import (
    STATUS_DEGRADED,
    STATUS_OK,
    BaseAgent,
    StageResult,
    extract_json,
)
from agents.fallbacks import fallback_dq_rules
from agents.grounding import landing_brief

SUPPORTED_CHECKS = {
    "not_null",
    "unique",
    "regex",
    "range",
    "allowed_values",
    "row_count_min",
    "date_format",
    "referential_integrity",
}


class DataQualityAgent(BaseAgent):
    key = "data_quality"
    prompt_name = "data_quality"
    description = "Authors and executes data-quality rules on landed data."
    tool_names = (
        "profile_landing_dataset",
        "run_data_quality_checks",
        "list_source_tables",
        "batch_tool",
    )

    def run(self, mapping: dict, datasets: list) -> StageResult:
        self.logger.info("Authoring DQ rules for datasets: %s", datasets)

        system = self.build_system(
            mapping_json=json.dumps(mapping, indent=2)[:5000],
            datasets=", ".join(datasets),
        )
        # Inject the profiles so thresholds are set from the real data.
        user = (
            "LANDED FILE PROFILES (authoritative -- use these exact column "
            "names, and set thresholds from what you see here):\n\n"
            f"{landing_brief(datasets)}\n\n"
            "Author the data-quality rules, run them with your tools, fix any "
            "rule that is wrong, then reply with the final rules JSON only."
        )

        text, _messages, turns, calls = self.converse(user, system)
        parsed = extract_json(text)
        notes = []
        status = STATUS_OK

        rules = self._clean(parsed, datasets)
        if not rules:
            self.logger.warning("No usable DQ rules returned; using fallback rules.")
            notes.append(
                "Model reply contained no usable rules; catalog-derived "
                "fallback rules were used."
            )
            rules = fallback_dq_rules(datasets)["rules"]
            status = STATUS_DEGRADED

        # Execute every rule for real, dataset by dataset.
        execution = {}
        for dataset in datasets:
            dataset_rules = [
                rule for rule in rules
                if str(rule.get("dataset", "")).split(".")[-1].lower() == dataset.lower()
            ]
            if not dataset_rules:
                self.logger.info("No rules targeted dataset '%s'.", dataset)
                continue
            try:
                execution[dataset] = tool_module.run_data_quality_checks(
                    dataset, dataset_rules
                )
            except Exception as exc:
                self.logger.error("DQ execution failed for %s: %s", dataset, exc)
                notes.append(f"DQ execution failed for {dataset}: {exc}")
                status = STATUS_DEGRADED

        rules_path = self.write(
            "data_quality/dq_rules.json",
            {"rules": rules, "dataset_count": len(datasets)},
        )
        results_path = self.write("data_quality/dq_results.json", execution)
        report_path = self.write(
            "data_quality/dq_report.md", _render_report(rules, execution)
        )

        failing = sum(
            result.get("failed", 0) for result in execution.values()
        )
        blocking = sum(
            result.get("error_severity_failures", 0) for result in execution.values()
        )
        self.logger.info(
            "DQ complete | rules=%d | datasets=%d | failed=%d | blocking=%d",
            len(rules),
            len(execution),
            failing,
            blocking,
        )

        return StageResult(
            agent=self.key,
            status=status,
            output={
                "rules": rules,
                "execution": execution,
                "failed_rule_count": failing,
                "blocking_failure_count": blocking,
            },
            artifacts=[rules_path, results_path, report_path],
            notes=notes,
            turns=turns,
            tool_calls=calls,
        )

    def _clean(self, parsed, datasets) -> list:
        """Drop rules the DQ engine cannot execute."""
        if not isinstance(parsed, dict):
            return []
        candidates = parsed.get("rules") or parsed.get("items") or []
        if isinstance(candidates, dict):
            candidates = [candidates]

        known = {dataset.lower() for dataset in datasets}
        cleaned = []
        for index, rule in enumerate(candidates, 1):
            if not isinstance(rule, dict):
                continue
            check = str(rule.get("check", "")).lower()
            dataset = str(rule.get("dataset", "")).split(".")[-1].lower()
            if check not in SUPPORTED_CHECKS:
                self.logger.debug("Discarding rule with unsupported check '%s'.", check)
                continue
            if dataset not in known:
                self.logger.debug("Discarding rule for unknown dataset '%s'.", dataset)
                continue
            rule["check"] = check
            rule["dataset"] = dataset
            rule.setdefault("rule_id", f"DQ-{index:03d}")
            rule.setdefault("params", {})
            rule.setdefault("severity", "error")
            if not isinstance(rule.get("params"), dict):
                rule["params"] = {}
            cleaned.append(rule)
        return cleaned


def _render_report(rules: list, execution: dict) -> str:
    lines = ["# Data quality report", ""]
    lines.append(f"Rules authored: **{len(rules)}**")
    lines.append("")

    for dataset, result in execution.items():
        lines.append(f"## `{dataset}`")
        lines.append("")
        lines.append(
            f"Rows: **{result.get('row_count')}** | "
            f"evaluated: **{result.get('rules_evaluated')}** | "
            f"passed: **{result.get('passed')}** | "
            f"failed: **{result.get('failed')}** | "
            f"status: **{result.get('overall_status')}**"
        )
        lines.append("")
        lines.append("| Rule | Column | Check | Severity | Status | Detail |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in result.get("results", []):
            lines.append(
                f"| {item.get('rule_id')} | `{item.get('column')}` | "
                f"{item.get('check')} | {item.get('severity')} | "
                f"**{item.get('status')}** | {item.get('detail', '')} |"
            )
        lines.append("")

    if not execution:
        lines.append("_No rules were executed._")
    return "\n".join(lines) + "\n"
