"""Shared agent machinery.

`BaseAgent` owns the parts every agent in this accelerator repeats:

* resolving its reasoning tier from `config.AGENT_MODEL_TIER`
* loading and rendering its system prompt from `prompts/`
* running the LLM <-> tool conversation loop until the model stops calling
  tools (the same loop shape used in the project notebooks)
* pulling structured output back out of an 8B model's reply, which is the
  part that actually decides whether this pipeline works

Subclasses implement `run(**inputs)` and return a `StageResult`.
"""

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any

import config
import tools as tool_module
from llm_client import add_assistant_message, add_user_message, message_tool_calls
from logger_setup import get_logger, log_event
from prompts import load_prompt

# Status values a stage can report.
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"   # produced output, but via a fallback
STATUS_FAILED = "failed"


@dataclass
class StageResult:
    """What every agent hands back to the orchestrator."""

    agent: str
    status: str = STATUS_OK
    output: Any = None
    artifacts: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    turns: int = 0
    tool_calls: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status != STATUS_FAILED

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "status": self.status,
            "artifacts": self.artifacts,
            "notes": self.notes,
            "turns": self.turns,
            "tool_calls": self.tool_calls,
        }


class BaseAgent:
    """Base class for every agent in the accelerator."""

    key = "base"
    prompt_name = None
    tool_names: tuple = ()
    description = ""

    def __init__(self, llm, run_dir=None):
        self.llm = llm
        self.run_dir = run_dir
        self.tier = config.tier_for_agent(self.key)
        self.model = llm.model_for(self.tier)
        self.logger = get_logger(f"agent.{self.key}")

    # ---------------------------------------------------------------- setup
    def build_system(self, **variables) -> str:
        """Render this agent's system prompt, tagged for logging/stubbing."""
        body = load_prompt(self.prompt_name or self.key, **variables)
        return f"[AGENT:{self.key}]\n{body}"

    def tool_schemas(self) -> list | None:
        return tool_module.schemas_for(self.tool_names) if self.tool_names else None

    # ------------------------------------------------------------ main loop
    def converse(self, user_content: str, system: str, max_turns: int = None):
        """Run the LLM/tool loop until the model stops requesting tools.

        Returns `(final_text, messages, turns, tool_calls_made)`.
        """
        max_turns = max_turns or config.MAX_AGENT_TURNS
        schemas = self.tool_schemas()

        messages = []
        add_user_message(messages, user_content)

        final_text = ""
        tool_calls_made = []
        turns = 0

        for turn in range(1, max_turns + 1):
            turns = turn
            text, message = self.llm.chat(
                messages,
                system=system,
                tier=self.tier,
                tools=schemas,
                agent=self.key,
            )
            add_assistant_message(messages, message)

            calls = message_tool_calls(message)
            if not calls:
                final_text = text or ""
                break

            tool_calls_made.extend(_call_names(calls))
            messages.extend(tool_module.run_tools(message))

            # Keep whatever prose the model emitted alongside its tool calls:
            # an 8B model sometimes answers and calls a tool in the same turn.
            if text:
                final_text = text
        else:
            self.logger.warning(
                "Agent %s hit the %d-turn limit without a final answer.",
                self.key,
                max_turns,
            )

        log_event(
            "agent_turn_complete",
            agent=self.key,
            turns=turns,
            tool_calls=tool_calls_made,
            final_chars=len(final_text),
        )
        return final_text, messages, turns, tool_calls_made

    # ---------------------------------------------------------- subclass API
    def run(self, **inputs) -> StageResult:  # pragma: no cover - abstract
        raise NotImplementedError

    # ------------------------------------------------------------- artifacts
    def write(self, relative_path: str, content) -> str:
        """Write an artifact and return its run-relative path."""
        if not isinstance(content, str):
            content = json.dumps(content, indent=2, default=str)
        tool_module.write_artifact(relative_path, content)
        return relative_path


def _call_names(calls) -> list:
    names = []
    for call in calls:
        func = call["function"] if isinstance(call, dict) else call.function
        names.append(func["name"] if isinstance(func, dict) else func.name)
    return names


# ==========================================================================
# Output extraction
# ==========================================================================
# An 8B model routinely wraps JSON in prose or a fence despite being told not
# to. These helpers recover the payload rather than failing the run.

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_PY_FENCE_RE = re.compile(r"```(?:python|py)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict | None:
    """Best-effort recovery of a single JSON object from a model reply."""
    if not text:
        return None

    candidates = []
    for match in _JSON_FENCE_RE.finditer(text):
        candidates.append(match.group(1).strip())
    candidates.append(text.strip())
    candidates.extend(_balanced_objects(text))

    for candidate in candidates:
        if not candidate:
            continue
        for attempt in (candidate, _strip_trailing_commas(candidate)):
            try:
                parsed = json.loads(attempt)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"items": parsed}
    return None


def _balanced_objects(text: str) -> list[str]:
    """Return substrings that look like balanced `{...}` blocks, longest first."""
    found = []
    starts = [index for index, char in enumerate(text) if char == "{"]
    for start in starts[:20]:          # guard against pathological input
        depth, in_string, escape = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    found.append(text[start : index + 1])
                    break
    return sorted(found, key=len, reverse=True)


_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _strip_trailing_commas(text: str) -> str:
    return _TRAILING_COMMA_RE.sub(r"\1", text)


def extract_python(text: str) -> str | None:
    """Recover a Python module from a model reply.

    Prefers a fenced block; falls back to the raw text when it parses.
    """
    if not text:
        return None

    blocks = [match.group(1) for match in _PY_FENCE_RE.finditer(text)]
    blocks.sort(key=len, reverse=True)
    for block in blocks:
        code = block.strip("\n")
        if _parses(code):
            return code
    if blocks:
        return blocks[0].strip("\n")      # return it anyway; validation reports why

    stripped = text.strip()
    return stripped if _parses(stripped) else None


def _parses(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False
