"""LLM access layer for the accelerator.

The message helpers (`add_user_message`, `add_assistant_message`, `chat`)
follow the same shape used throughout the project notebooks: messages are a
plain list of dicts, and `chat` returns `(text, message)` where `message` is
the raw provider message object that may carry `tool_calls`.

Everything an agent needs is wrapped by :class:`LLMClient`, which adds:

* tier -> model resolution through :mod:`config`
* per-call logging (agent, tier, model, latency, tool-call names)
* a `stub` provider so the whole pipeline is testable with no server running
"""

import time

import config
from logger_setup import get_logger, log_event

logger = get_logger("llm")


# --------------------------------------------------------------------------
# Message helpers -- same style as the project notebooks
# --------------------------------------------------------------------------
def add_user_message(messages, content):
    """Append a user message. `content` is a plain string."""
    messages.append({"role": "user", "content": content})


def add_assistant_message(messages, message):
    """Append an assistant message.

    `message` is either a plain string or a provider message object (which may
    carry tool_calls and therefore must be serialised, not stringified).
    """
    if isinstance(message, str):
        messages.append({"role": "assistant", "content": message})
    elif isinstance(message, dict):
        messages.append(message)
    else:
        messages.append(message.model_dump(exclude_none=True))


def add_tool_results(messages, tool_result_messages):
    """Append the role='tool' results produced by tools.run_tools()."""
    messages.extend(tool_result_messages)


def message_tool_calls(message):
    """Return the tool_calls carried by a provider message (possibly empty)."""
    if message is None:
        return []
    if isinstance(message, list):
        message = message[-1] if message else None
        if message is None:
            return []
    if isinstance(message, dict):
        return message.get("tool_calls") or []
    return getattr(message, "tool_calls", None) or []


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------
class LLMClient:
    """Provider-agnostic chat client with tier-based model routing."""

    def __init__(self, provider: str = config.DEFAULT_PROVIDER, host: str = config.OLLAMA_HOST):
        self.provider = provider
        self.host = host
        self.call_count = 0
        self.total_latency = 0.0
        self._client = self._build_client()
        logger.info(
            "LLM client ready | provider=%s | host=%s | routing=%s",
            provider,
            host if provider == "ollama" else "n/a",
            config.MODEL_ROUTING[provider],
        )

    def _build_client(self):
        if self.provider == "ollama":
            import ollama

            return ollama.Client(host=self.host)
        if self.provider == "stub":
            from stub_llm import StubClient

            return StubClient()
        if self.provider == "anthropic":
            # Documented but not exercised by the default local run. Kept so
            # the Opus/Sonnet/Haiku routing in config.py is reachable.
            raise NotImplementedError(
                "The 'anthropic' provider is configured in config.MODEL_ROUTING "
                "but this build targets the local Ollama runtime. Wire an "
                "Anthropic SDK call here to enable it."
            )
        raise ValueError(f"Unknown provider: {self.provider}")

    def model_for(self, tier: str) -> str:
        return config.resolve_model(self.provider, tier)

    def chat(
        self,
        messages,
        system=None,
        tier=config.TIER_BALANCED,
        temperature=None,
        stop_sequences=None,
        tools=None,
        agent="-",
    ):
        """Send `messages` and return `(text, message)`.

        `system` is prepended as a system message rather than mutating the
        caller's list, exactly as in the project notebooks.
        """
        if system:
            messages = [{"role": "system", "content": system}] + messages

        model_id = self.model_for(tier)
        options = config.options_for_tier(tier)
        if temperature is not None:
            options["temperature"] = temperature
        options["stop"] = stop_sequences or []

        logger.debug(
            "chat -> agent=%s tier=%s model=%s messages=%d tools=%d",
            agent,
            tier,
            model_id,
            len(messages),
            len(tools or []),
        )

        started = time.perf_counter()
        response = self._client.chat(
            model=model_id,
            messages=messages,
            tools=tools,
            options=options,
        )
        elapsed = time.perf_counter() - started

        message = response.message
        text = message.content
        calls = message_tool_calls(message)

        self.call_count += 1
        self.total_latency += elapsed

        log_event(
            "llm_call",
            agent=agent,
            tier=tier,
            model=model_id,
            provider=self.provider,
            latency_seconds=round(elapsed, 2),
            response_chars=len(text or ""),
            tool_calls=[_call_name(call) for call in calls],
        )
        logger.info(
            "chat <- agent=%s model=%s latency=%.2fs chars=%d tool_calls=%s",
            agent,
            model_id,
            elapsed,
            len(text or ""),
            [_call_name(call) for call in calls] or "none",
        )
        return text, message

    def stats(self) -> dict:
        return {
            "provider": self.provider,
            "calls": self.call_count,
            "total_latency_seconds": round(self.total_latency, 2),
        }


def _call_name(call):
    func = call["function"] if isinstance(call, dict) else call.function
    return func["name"] if isinstance(func, dict) else func.name
