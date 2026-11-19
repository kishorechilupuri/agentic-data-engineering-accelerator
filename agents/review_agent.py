"""Stage 6 -- Code Review Agent.

Reads back every generated pipeline module, validates it statically, and asks
the model for a structured review verdict. Static findings are always merged
in, so the review never silently approves code that failed validation.

Model tier: `deep` (Opus-class) -- defect detection is the point of the stage.
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
from agents.fallbacks import fallback_review

VALID_VERDICTS = ("approve", "approve_with_comments", "request_changes")
BLOCKING = ("critical", "high")


class CodeReviewAgent(BaseAgent):
    key = "code_reviewer"
    prompt_name = "code_reviewer"
    description = "Reviews generated Databricks pipeline code before promotion."
    tool_names = (
        "list_artifacts",
        "read_artifact",
        "validate_python_code",
    )

    def run(self, mapping: dict, module_paths: list) -> StageResult:
        self.logger.info("Reviewing %d generated module(s).", len(module_paths))

        # Static validation first: this is evidence, not opinion.
        validations = {}
        for path in module_paths:
            if not str(path).endswith(".py"):
                continue
            try:
                layer = _layer_of(path)
                validations[path] = tool_module.validate_python_code(
                    artifact_path=path,
                    layer=layer if layer in ("L1", "L2", "L3") else None,
                )
            except FileNotFoundError as exc:
                self.logger.warning("Could not read %s for review: %s", path, exc)

        system = self.build_system(mapping_json=json.dumps(mapping, indent=2)[:5000])
        user = (
            "Review the generated pipeline modules: "
            + ", ".join(f"`{path}`" for path in module_paths)
            + ". Read each one with your tools before judging it, then reply "
            "with the review JSON only."
        )

        text, _messages, turns, calls = self.converse(user, system)
        review = extract_json(text)
        notes = []
        status = STATUS_OK

        if not self._usable(review):
            self.logger.warning("No parseable review returned; using static findings.")
            notes.append(
                "Model reply was not a parseable review; the verdict was "
                "derived from static validation results only."
            )
            review = fallback_review(validations)
            status = STATUS_DEGRADED
        else:
            review = self._normalise(review)

        # The model tends to echo the absolute path it read from. Keep the
        # artifact portable (and free of local usernames) by making it relative.
        for finding in review["findings"]:
            finding["file"] = self._relative(finding.get("file"))

        static_findings = _static_findings(validations, start=len(review["findings"]))
        if static_findings:
            review["findings"].extend(static_findings)
            notes.append(
                f"{len(static_findings)} finding(s) added from static validation."
            )

        # The verdict must reflect the merged findings, not just the model's.
        if any(item["severity"] in BLOCKING for item in review["findings"]):
            review["verdict"] = "request_changes"

        review["modules_reviewed"] = module_paths
        review["static_validation"] = validations

        path = self.write("review/code_review.json", review)
        report_path = self.write("review/code_review.md", _render_report(review))

        by_severity = {}
        for finding in review["findings"]:
            by_severity[finding["severity"]] = by_severity.get(finding["severity"], 0) + 1

        self.logger.info(
            "Review complete | verdict=%s | findings=%d | %s",
            review["verdict"],
            len(review["findings"]),
            by_severity or "none",
        )

        return StageResult(
            agent=self.key,
            status=status,
            output=review,
            artifacts=[path, report_path],
            notes=notes,
            turns=turns,
            tool_calls=calls,
        )

    def _relative(self, path):
        """Express `path` relative to the run folder when it lies inside it."""
        if not path or not self.run_dir:
            return path
        normalised = str(path).replace("\\", "/")
        root = str(self.run_dir).replace("\\", "/").rstrip("/") + "/"
        return normalised[len(root):] if normalised.startswith(root) else path

    @staticmethod
    def _usable(review) -> bool:
        return (
            isinstance(review, dict)
            and isinstance(review.get("findings"), list)
            and review.get("verdict") in VALID_VERDICTS
        )

    @staticmethod
    def _normalise(review: dict) -> dict:
        cleaned = []
        for index, finding in enumerate(review.get("findings", []), 1):
            if not isinstance(finding, dict) or not finding.get("summary"):
                continue
            severity = str(finding.get("severity", "medium")).lower()
            finding["severity"] = (
                severity if severity in ("critical", "high", "medium", "low") else "medium"
            )
            finding.setdefault("finding_id", f"RV-{index:03d}")
            finding.setdefault("category", "correctness")
            finding.setdefault("recommendation", "")
            cleaned.append(finding)
        review["findings"] = cleaned
        review.setdefault("summary", "")
        return review


def _static_findings(validations: dict, start: int = 0) -> list:
    findings = []
    counter = start
    for path, result in validations.items():
        for error in result.get("errors", []):
            counter += 1
            findings.append({
                "finding_id": f"RV-S{counter:03d}",
                "severity": "critical",
                "category": "correctness",
                "file": path,
                "layer": _layer_of(path),
                "summary": f"Static validation error: {error}",
                "recommendation": "Regenerate or hand-fix the module before promotion.",
                "source": "static_validation",
            })
        for warning in result.get("warnings", []):
            counter += 1
            findings.append({
                "finding_id": f"RV-S{counter:03d}",
                "severity": "low",
                "category": "readability",
                "file": path,
                "layer": _layer_of(path),
                "summary": f"Static validation warning: {warning}",
                "recommendation": "Address before promotion.",
                "source": "static_validation",
            })
    return findings


def _layer_of(path: str) -> str:
    name = str(path).lower()
    for layer in ("l1", "l2", "l3"):
        if layer in name:
            return layer.upper()
    return "n/a"


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _render_report(review: dict) -> str:
    lines = [
        "# Code review",
        "",
        f"**Verdict:** `{review.get('verdict')}`",
        "",
        review.get("summary", ""),
        "",
        "## Findings",
        "",
    ]
    findings = sorted(
        review.get("findings", []),
        key=lambda item: _SEVERITY_ORDER.get(item.get("severity"), 9),
    )
    if not findings:
        lines.append("_No findings._")
    else:
        lines.append("| ID | Severity | Category | Layer | File | Finding | Recommendation |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for finding in findings:
            lines.append(
                f"| {finding.get('finding_id')} | **{finding.get('severity')}** | "
                f"{finding.get('category')} | {finding.get('layer', 'n/a')} | "
                f"`{finding.get('file', 'n/a')}` | {finding.get('summary')} | "
                f"{finding.get('recommendation', '')} |"
            )
    return "\n".join(lines) + "\n"
