"""The Orchestrator -- the agent that runs the data-engineering SDLC.

It owns the run: it creates the run folder, drives each specialist agent in
order, passes each stage's output to the next, records a manifest, and asks a
cheap model for the closing human summary.

    requirement
        -> RequirementRefinerAgent      (balanced / Sonnet-class)
        -> SourceToTargetMappingAgent   (deep     / Opus-class)
        -> PipelineCodeGenAgent         (deep     / Opus-class)
        -> DataQualityAgent             (balanced / Sonnet-class)
        -> TestGenerationAgent          (balanced / Sonnet-class)
        -> CodeReviewAgent              (deep     / Opus-class)
        -> run summary                  (fast     / Haiku-class)

Model tier for the orchestrator itself: `fast` -- it only writes the summary.
"""

import json
from datetime import datetime

import config
import tools as tool_module
from agents.base_agent import STATUS_FAILED, BaseAgent, StageResult
from agents.codegen_agent import PipelineCodeGenAgent
from agents.dq_agent import DataQualityAgent
from agents.mapping_agent import SourceToTargetMappingAgent
from agents.requirement_agent import RequirementRefinerAgent
from agents.review_agent import CodeReviewAgent
from agents.test_agent import TestGenerationAgent
from logger_setup import get_logger, log_event

STAGE_ORDER = (
    "requirements",
    "mapping",
    "codegen",
    "data_quality",
    "tests",
    "review",
)


class Orchestrator(BaseAgent):
    key = "orchestrator"
    prompt_name = "orchestrator"
    description = "Runs the end-to-end data engineering SDLC."
    # No tools. Offered get_current_datetime, the model answered the summary
    # request with a fabricated tool call (`get_run_summary`) as plain text.
    tool_names = ()

    def __init__(self, llm, run_id: str, run_dir=None, tables=None):
        super().__init__(llm, run_dir=run_dir)
        self.run_id = run_id
        # Optional hard scope: run the SDLC for these source tables only.
        self.table_scope = list(tables) if tables else None
        self.run_dir = tool_module.set_run_directory(
            run_dir or (config.OUTPUT_DIR / f"run_{run_id}")
        )
        self.logger = get_logger("agent.orchestrator")

        self.agents = {
            "requirements": RequirementRefinerAgent(llm, self.run_dir),
            "mapping": SourceToTargetMappingAgent(llm, self.run_dir),
            "codegen": PipelineCodeGenAgent(llm, self.run_dir),
            "data_quality": DataQualityAgent(llm, self.run_dir),
            "tests": TestGenerationAgent(llm, self.run_dir),
            "review": CodeReviewAgent(llm, self.run_dir),
        }

    # ------------------------------------------------------------------
    def run_pipeline(self, requirement: str, stages=None) -> dict:
        """Execute the SDLC for one requirement and return the run manifest."""
        stages = tuple(stages or STAGE_ORDER)
        started = datetime.now()

        self.logger.info("-" * 88)
        self.logger.info("PIPELINE START | run_id=%s", self.run_id)
        self.logger.info("Requirement: %s", requirement)
        self.logger.info("Stages: %s", ", ".join(stages))
        self.logger.info("Table scope: %s", self.table_scope or "entire catalog")
        self.logger.info("Run folder: %s", self.run_dir)
        self.logger.info("-" * 88)
        log_event("pipeline_start", run_id=self.run_id, stages=list(stages))

        results: dict[str, StageResult] = {}
        requirements = mapping = None
        module_paths: list = []

        for stage in stages:
            self.logger.info(">>> STAGE %s | agent=%s | tier=%s | model=%s",
                             stage.upper(),
                             self.agents[stage].key,
                             self.agents[stage].tier,
                             self.agents[stage].model)
            stage_started = datetime.now()
            try:
                result = self._run_stage(
                    stage, requirement, requirements, mapping, module_paths
                )
            except Exception as exc:
                self.logger.exception("Stage %s failed: %s", stage, exc)
                result = StageResult(
                    agent=self.agents[stage].key,
                    status=STATUS_FAILED,
                    notes=[f"{type(exc).__name__}: {exc}"],
                )

            elapsed = (datetime.now() - stage_started).total_seconds()
            results[stage] = result
            log_event(
                "stage_complete",
                stage=stage,
                agent=result.agent,
                status=result.status,
                elapsed_seconds=round(elapsed, 2),
                artifacts=result.artifacts,
            )
            self.logger.info(
                "<<< STAGE %s | status=%s | %.1fs | artifacts=%d",
                stage.upper(),
                result.status,
                elapsed,
                len(result.artifacts),
            )

            if stage == "requirements" and result.ok:
                requirements = result.output
            elif stage == "mapping" and result.ok:
                mapping = result.output
            elif stage == "codegen" and result.ok:
                module_paths = [
                    path for path in result.artifacts if path.endswith(".py")
                ]

            if stage in ("requirements", "mapping") and not result.ok:
                self.logger.error(
                    "Stage %s failed and later stages depend on it; stopping.", stage
                )
                break

        manifest = self._build_manifest(requirement, stages, results, started)
        manifest["summary"] = self._write_summary(manifest)

        self.write("run_manifest.json", manifest)
        self.write("RUN_SUMMARY.md", _render_summary(manifest))

        self.logger.info("-" * 88)
        self.logger.info(
            "PIPELINE END | run_id=%s | status=%s | %.1fs",
            self.run_id,
            manifest["overall_status"],
            manifest["elapsed_seconds"],
        )
        self.logger.info("-" * 88)
        log_event("pipeline_end", **{
            key: manifest[key]
            for key in ("overall_status", "elapsed_seconds")
        })
        return manifest

    # ------------------------------------------------------------------
    def _run_stage(self, stage, requirement, requirements, mapping, module_paths):
        agent = self.agents[stage]

        if stage == "requirements":
            return agent.run(
                requirement=requirement, allowed_tables=self.table_scope
            )
        if stage == "mapping":
            return agent.run(requirements=requirements or {})
        if stage == "codegen":
            return agent.run(mapping=mapping or {"mappings": []})
        if stage == "data_quality":
            return agent.run(
                mapping=mapping or {"mappings": []},
                datasets=self._datasets(mapping),
            )
        if stage == "tests":
            return agent.run(mapping=mapping or {"mappings": []})
        if stage == "review":
            return agent.run(
                mapping=mapping or {"mappings": []},
                module_paths=module_paths,
            )
        raise ValueError(f"Unknown stage: {stage}")

    def _datasets(self, mapping) -> list:
        """Landing datasets to run DQ against: catalog ∩ what L1 maps."""
        try:
            catalog = tool_module.list_source_tables()
            available = [
                table["landing_dataset"]
                for table in catalog["tables"]
                if table.get("landing_dataset")
            ]
        except Exception as exc:
            self.logger.warning("Could not list source tables: %s", exc)
            return []

        if not mapping:
            return available

        mapped = set()
        for item in mapping.get("mappings", []):
            if item.get("layer") != "L1":
                continue
            target = str(item.get("target_object", "")).split(".")[-1].lower()
            source = str(item.get("source_object", "")).lower()
            for dataset in available:
                if dataset == target or dataset in source:
                    mapped.add(dataset)

        return sorted(mapped) or available

    # ------------------------------------------------------------------
    def _build_manifest(self, requirement, stages, results, started) -> dict:
        elapsed = (datetime.now() - started).total_seconds()

        statuses = [result.status for result in results.values()]
        if any(status == STATUS_FAILED for status in statuses):
            overall = "failed"
        elif any(status == "degraded" for status in statuses):
            overall = "completed_with_degradation"
        else:
            overall = "completed"

        dq = results.get("data_quality")
        tests = results.get("tests")
        review = results.get("review")

        return {
            "run_id": self.run_id,
            "run_date": started.strftime("%Y-%m-%d"),
            "run_time": started.strftime("%H:%M:%S"),
            "requirement": requirement,
            "table_scope": self.table_scope,
            "provider": self.llm.provider,
            # Keyed by stage name, so the manifest records which model served
            # which stage of this particular run.
            "model_routing": {
                stage: {"tier": agent.tier, "model": agent.model}
                for stage, agent in self.agents.items()
            },
            "stages_requested": list(stages),
            "stages": {name: result.to_dict() for name, result in results.items()},
            "artifacts": [
                path for result in results.values() for path in result.artifacts
            ],
            "data_quality": {
                "rules": len((dq.output or {}).get("rules", [])) if dq and dq.output else 0,
                "failed_rules": (dq.output or {}).get("failed_rule_count", 0) if dq and dq.output else 0,
                "blocking_failures": (dq.output or {}).get("blocking_failure_count", 0) if dq and dq.output else 0,
            },
            "tests": {
                "exit_code": ((tests.output or {}).get("execution") or {}).get("exit_code") if tests and tests.output else None,
                "summary": ((tests.output or {}).get("execution") or {}).get("summary") if tests and tests.output else None,
                "passed": ((tests.output or {}).get("execution") or {}).get("passed") if tests and tests.output else None,
            },
            "review": {
                "verdict": (review.output or {}).get("verdict") if review and review.output else None,
                "finding_count": len((review.output or {}).get("findings", [])) if review and review.output else 0,
            },
            "llm_stats": self.llm.stats(),
            "overall_status": overall,
            "elapsed_seconds": round(elapsed, 2),
            "run_folder": str(self.run_dir),
        }

    def _write_summary(self, manifest: dict) -> str:
        """Ask the cheap tier for the human-readable run summary."""
        record = {
            key: manifest[key]
            for key in (
                "run_id", "run_date", "run_time", "requirement",
                "overall_status", "data_quality", "tests", "review",
                "elapsed_seconds",
            )
        }
        record["stage_status"] = {
            name: stage["status"] for name, stage in manifest["stages"].items()
        }
        record["stage_notes"] = {
            name: stage["notes"]
            for name, stage in manifest["stages"].items()
            if stage["notes"]
        }

        system = self.build_system(run_record=json.dumps(record, indent=2, default=str))
        try:
            text, _messages, _turns, _calls = self.converse(
                "Write the run summary.", system, max_turns=2
            )
        except Exception as exc:
            self.logger.warning("Summary generation failed: %s", exc)
            return _deterministic_summary(manifest)

        text = (text or "").strip()
        # The prompt asks for prose. A reply that is JSON, or that is empty, is
        # not a summary a human can read, so say what happened ourselves.
        if not text or text[0] in "{[" or '"parameters"' in text:
            self.logger.warning(
                "Summary was not prose (%r); using the deterministic summary.",
                text[:60],
            )
            return _deterministic_summary(manifest)
        return text


def _deterministic_summary(manifest: dict) -> str:
    return (
        f"Run {manifest['run_id']} finished with status "
        f"{manifest['overall_status']} in {manifest['elapsed_seconds']}s. "
        f"{manifest['data_quality']['rules']} data-quality rules were executed "
        f"with {manifest['data_quality']['failed_rules']} failing. Generated "
        f"tests reported: {manifest['tests']['summary']}. Review verdict: "
        f"{manifest['review']['verdict']}."
    )


def _render_summary(manifest: dict) -> str:
    lines = [
        f"# Run {manifest['run_id']}",
        "",
        f"**Date:** {manifest['run_date']} **Time:** {manifest['run_time']} "
        f"**Status:** `{manifest['overall_status']}` "
        f"**Elapsed:** {manifest['elapsed_seconds']}s",
        "",
        "## Requirement",
        "",
        f"> {manifest['requirement']}",
        "",
        "## Summary",
        "",
        manifest.get("summary", ""),
        "",
        "## Stages",
        "",
        "| Stage | Agent | Tier | Model | Status | Turns | Artifacts |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    routing = manifest.get("model_routing", {})
    for name, stage in manifest["stages"].items():
        route = routing.get(name, {})
        lines.append(
            f"| {name} | {stage['agent']} | {route.get('tier', '-')} | "
            f"`{route.get('model', '-')}` | **{stage['status']}** | "
            f"{stage['turns']} | {len(stage['artifacts'])} |"
        )

    lines += [
        "",
        "## Quality gates",
        "",
        f"- Data quality: {manifest['data_quality']['rules']} rules, "
        f"{manifest['data_quality']['failed_rules']} failing, "
        f"{manifest['data_quality']['blocking_failures']} blocking",
        f"- Generated tests: `{manifest['tests']['summary']}` "
        f"(exit code {manifest['tests']['exit_code']})",
        f"- Code review: **{manifest['review']['verdict']}** with "
        f"{manifest['review']['finding_count']} finding(s)",
        "",
        "## Artifacts",
        "",
    ]
    lines += [f"- `{path}`" for path in manifest["artifacts"]] or ["- none"]

    notes = [
        (name, note)
        for name, stage in manifest["stages"].items()
        for note in stage.get("notes", [])
    ]
    if notes:
        lines += ["", "## Degradation notes", ""]
        lines += [f"- **{name}**: {note}" for name, note in notes]

    return "\n".join(lines) + "\n"
