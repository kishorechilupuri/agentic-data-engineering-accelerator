"""Command-line entry point for the Agentic Data Engineering Accelerator.

Examples
--------
    # Full SDLC run against the local llama3.1:8b
    python main.py --requirement "Build a customer sales summary in gold."

    # Same, from a file
    python main.py --requirement-file my_requirement.txt

    # Only the first two stages
    python main.py -r "..." --stages requirements mapping

    # Offline, deterministic (no model server needed)
    python main.py -r "..." --provider stub

    # Inspect the installed agents, tools and prompts
    python main.py --describe
"""

import argparse
import json
import sys

import config
from logger_setup import configure_logging, get_logger, log_run_end

DEFAULT_REQUIREMENT = (
    "Build a curated gold table that gives each customer their order count "
    "and total order amount, sourced from the SQL Server sales tables that "
    "ADF lands in ADLS Gen2. It must be re-runnable, must not double count on "
    "a re-run, and must reject a batch whose business keys are not unique."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentic-de-accelerator",
        description=(
            "Agentic AI accelerator for the data-engineering SDLC: requirement "
            "refinement, source-to-target mapping, Databricks L1/L2/L3 code "
            "generation, data-quality checks, test generation and code review."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "-r", "--requirement",
        help="The business requirement to build from.",
    )
    source.add_argument(
        "-f", "--requirement-file",
        help="Path to a file containing the business requirement.",
    )
    parser.add_argument(
        "--provider",
        default=config.DEFAULT_PROVIDER,
        choices=sorted(config.MODEL_ROUTING),
        help="LLM provider. 'ollama' (default) uses the local llama3.1:8b; "
             "'stub' runs deterministically with no model server.",
    )
    parser.add_argument(
        "--host",
        default=config.OLLAMA_HOST,
        help=f"Ollama host. Default: {config.OLLAMA_HOST}",
    )
    parser.add_argument(
        "--stages",
        nargs="+",
        metavar="STAGE",
        help="Subset of stages to run, in order. Default: all.",
    )
    parser.add_argument(
        "--tables",
        nargs="+",
        metavar="TABLE",
        help="Scope the run to these source tables, e.g. --tables Sales.Customer. "
             "Accepts the qualified name, the bare name or the landing dataset. "
             "Default: the whole catalog. Useful on a slow local model, where "
             "each additional table adds two pipeline modules and two test "
             "suites to generate.",
    )
    parser.add_argument(
        "--run-id",
        help="Override the generated run id (default: the run date and time).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Log to the file only; keep the console almost silent.",
    )
    parser.add_argument(
        "--describe",
        action="store_true",
        help="Print the installed agents, tools, prompts and model routing, "
             "then exit without running the pipeline.",
    )
    return parser


def describe() -> None:
    """Print the accelerator's capability surface."""
    import tools as tool_module
    from agents import AGENT_REGISTRY, STAGE_ORDER
    from prompts import available_prompts

    print("\nAGENTS")
    print("-" * 78)
    for key, agent_class in AGENT_REGISTRY.items():
        tier = config.tier_for_agent(key)
        print(f"  {key:<22} tier={tier:<9} {agent_class.description}")
        if agent_class.tool_names:
            print(f"  {'':<22} tools: {', '.join(agent_class.tool_names)}")

    print("\nSTAGES")
    print("-" * 78)
    print("  " + " -> ".join(STAGE_ORDER))

    print("\nTOOLS")
    print("-" * 78)
    for name, schema in tool_module.TOOL_SCHEMAS.items():
        description = schema["function"]["description"].split(".")[0]
        print(f"  {name:<28} {description}.")

    print("\nPROMPTS")
    print("-" * 78)
    print("  " + ", ".join(available_prompts()))

    print("\nMODEL ROUTING")
    print("-" * 78)
    for provider, routing in config.MODEL_ROUTING.items():
        print(f"  {provider}:")
        for tier, model in routing.items():
            print(f"      {tier:<10} -> {model}")
    print()


def resolve_requirement(args) -> str:
    if args.requirement:
        return args.requirement.strip()
    if args.requirement_file:
        from pathlib import Path

        path = Path(args.requirement_file)
        if not path.exists():
            raise SystemExit(f"Requirement file not found: {path}")
        return path.read_text(encoding="utf-8").strip()
    return DEFAULT_REQUIREMENT


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.describe:
        describe()
        return 0

    context = configure_logging(run_id=args.run_id, verbose=not args.quiet)
    logger = get_logger("main")

    requirement = resolve_requirement(args)

    # Imported after logging is configured so module loggers attach correctly.
    from agents import STAGE_ORDER, Orchestrator
    from llm_client import LLMClient

    stages = tuple(args.stages) if args.stages else STAGE_ORDER
    unknown = [stage for stage in stages if stage not in STAGE_ORDER]
    if unknown:
        raise SystemExit(
            f"Unknown stage(s) {unknown}. Valid stages: {list(STAGE_ORDER)}"
        )

    logger.info("Provider=%s | host=%s | stages=%s",
                args.provider, args.host, ", ".join(stages))

    try:
        llm = LLMClient(provider=args.provider, host=args.host)
    except Exception as exc:
        logger.error("Could not reach the LLM provider: %s", exc)
        logger.error(
            "Is Ollama running? Try `ollama serve` and "
            "`ollama pull %s`, or re-run with --provider stub.",
            config.LOCAL_MODEL_ID,
        )
        log_run_end("failed", reason=str(exc))
        return 2

    orchestrator = Orchestrator(llm, run_id=context["run_id"], tables=args.tables)

    try:
        manifest = orchestrator.run_pipeline(requirement, stages=stages)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        log_run_end("interrupted")
        return 130
    except Exception as exc:
        logger.exception("Pipeline failed: %s", exc)
        log_run_end("failed", reason=str(exc))
        return 1

    log_run_end(manifest["overall_status"], run_folder=manifest["run_folder"])

    print()
    print("=" * 78)
    print(f"  Run folder : {manifest['run_folder']}")
    print(f"  Log file   : {context['log_path']}")
    print(f"  Event log  : {context['events_path']}")
    print(f"  Status     : {manifest['overall_status']}")
    print(f"  Artifacts  : {len(manifest['artifacts'])}")
    print(f"  DQ rules   : {manifest['data_quality']['rules']} "
          f"({manifest['data_quality']['failed_rules']} failing)")
    print(f"  Tests      : {manifest['tests']['summary']}")
    print(f"  Review     : {manifest['review']['verdict']}")
    print("=" * 78)
    print()

    return 0 if manifest["overall_status"] != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
