"""Prompt package for the Agentic Data Engineering Accelerator."""

from prompts.loader import (
    available_prompts,
    load_prompt,
    placeholders,
    prompt_path,
)

__all__ = ["available_prompts", "load_prompt", "placeholders", "prompt_path"]
