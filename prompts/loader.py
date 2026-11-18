"""Prompt loading and rendering.

Prompts live beside this module as plain `.md` files so a data engineer can
edit them without touching Python. Placeholders use `${name}` (``string.Template``)
rather than `str.format` braces, because prompts contain literal JSON examples
full of `{` and `}`.
"""

from pathlib import Path
from string import Template

PROMPTS_PATH = Path(__file__).resolve().parent
PROMPT_SUFFIX = ".md"


def prompt_path(name: str) -> Path:
    """Return the file path for prompt `name`."""
    return PROMPTS_PATH / f"{name}{PROMPT_SUFFIX}"


def available_prompts() -> list[str]:
    """Return every prompt name available on disk, sorted."""
    return sorted(path.stem for path in PROMPTS_PATH.glob(f"*{PROMPT_SUFFIX}"))


def load_prompt(name: str, **variables) -> str:
    """Load prompt `name` and substitute any `${placeholder}` variables.

    Unknown placeholders are left untouched (``safe_substitute``) so a prompt
    can mention `${}`-looking text without breaking the run.
    """
    path = prompt_path(name)
    if not path.exists():
        raise FileNotFoundError(
            f"Prompt '{name}' not found. Available: {available_prompts()}"
        )
    text = path.read_text(encoding="utf-8")
    return Template(text).safe_substitute(**variables) if variables else text


def placeholders(name: str) -> set[str]:
    """Return the set of `${placeholder}` names used by prompt `name`."""
    text = prompt_path(name).read_text(encoding="utf-8")
    found = set()
    for match in Template.pattern.finditer(text):
        named = match.group("named") or match.group("braced")
        if named:
            found.add(named)
    return found
