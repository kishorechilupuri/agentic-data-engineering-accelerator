"""Run-scoped logging for the Agentic Data Engineering Accelerator.

Every execution gets its own log file stamped with the run date and time:

    logs/run_2026-11-19_14-30-45.log        human readable
    logs/run_2026-11-19_14-30-45.events.jsonl   machine readable

The human-readable file is what you read during a run; the JSONL event file is
what EVIDENCE.md and any dashboard are built from, because each line is a
self-contained JSON object (stage, agent, model, tier, duration, status).
"""

import json
import logging
import sys
import threading
from datetime import datetime
from pathlib import Path

import config

ROOT_LOGGER_NAME = "dea"

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-26s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Populated by configure_logging(); read by get_logger() / log_event().
_RUN_CONTEXT = {
    "run_id": None,
    "started_at": None,
    "log_path": None,
    "events_path": None,
}
_EVENT_LOCK = threading.Lock()


def new_run_id(now: datetime | None = None) -> str:
    """Return a filesystem-safe run id built from the current date and time."""
    now = now or datetime.now()
    return now.strftime("%Y-%m-%d_%H-%M-%S")


def configure_logging(run_id: str | None = None, verbose: bool = True) -> dict:
    """Configure the ``dea`` logger tree for a single run.

    Returns the run context dict (run_id, started_at, log_path, events_path).
    Calling it twice replaces the handlers so a second run in the same process
    does not append to the first run's file.
    """
    config.ensure_directories()

    started_at = datetime.now()
    run_id = run_id or new_run_id(started_at)

    log_path = config.LOGS_DIR / f"run_{run_id}.log"
    events_path = config.LOGS_DIR / f"run_{run_id}.events.jsonl"

    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO if verbose else logging.WARNING)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    _RUN_CONTEXT.update(
        run_id=run_id,
        started_at=started_at,
        log_path=log_path,
        events_path=events_path,
    )

    logger.info("=" * 92)
    logger.info("RUN START | run_id=%s", run_id)
    logger.info(
        "RUN DATE=%s | RUN TIME=%s | log_file=%s",
        started_at.strftime("%Y-%m-%d"),
        started_at.strftime("%H:%M:%S"),
        log_path.name,
    )
    logger.info("=" * 92)

    return dict(_RUN_CONTEXT)


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the run-scoped ``dea`` root."""
    if not name.startswith(ROOT_LOGGER_NAME):
        name = f"{ROOT_LOGGER_NAME}.{name}"
    return logging.getLogger(name)


def run_context() -> dict:
    """Return the current run context (empty values before configuration)."""
    return dict(_RUN_CONTEXT)


def log_event(event: str, **fields) -> dict:
    """Append one structured event to the JSONL event log and the text log.

    Never raises: logging must not be able to fail a pipeline run.
    """
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "run_id": _RUN_CONTEXT.get("run_id"),
        "event": event,
    }
    record.update(fields)

    events_path: Path | None = _RUN_CONTEXT.get("events_path")
    if events_path is not None:
        try:
            with _EVENT_LOCK:
                with open(events_path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, default=str) + "\n")
        except OSError as exc:  # pragma: no cover - disk level failure
            get_logger("logger").warning("Could not write event log: %s", exc)

    detail = " ".join(
        f"{key}={value}" for key, value in fields.items() if key != "payload"
    )
    get_logger("event").debug("EVENT %s %s", event, detail)
    return record


def log_run_end(status: str, **fields) -> None:
    """Write the closing banner for the run."""
    logger = get_logger("run")
    started_at = _RUN_CONTEXT.get("started_at")
    elapsed = (
        (datetime.now() - started_at).total_seconds() if started_at else None
    )
    log_event("run_end", status=status, elapsed_seconds=elapsed, **fields)
    logger.info("=" * 92)
    logger.info(
        "RUN END | run_id=%s | status=%s | elapsed=%ss",
        _RUN_CONTEXT.get("run_id"),
        status,
        None if elapsed is None else round(elapsed, 2),
    )
    logger.info("=" * 92)
