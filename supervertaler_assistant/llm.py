"""
supervertaler_assistant.llm
===========================

Thin wrapper around LiteLLM so every agent talks to models through one
uniform interface, regardless of provider.

LiteLLM supports OpenAI, Anthropic, Google (Gemini), Mistral, Ollama,
Azure, Bedrock, vLLM, LM Studio and ~100 other providers with identical
call signatures. We only use a small slice of its API here, but
centralising it means:

- Agents don't need to know which provider is in use.
- API keys come from environment variables (or an explicit settings
  dict), never hardcoded.
- Streaming and non-streaming use the same entry point.
- Cost/usage can be logged in one place later.

Model names follow LiteLLM conventions:
    openai/gpt-4o
    anthropic/claude-sonnet-4-5
    gemini/gemini-2.5-pro
    ollama/llama3.1:8b

Dependencies
------------
    pip install litellm

Author: Supervertaler project (MIT)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

import litellm

# Be quiet by default – we handle errors ourselves, and LiteLLM's chatter
# pollutes the app log.
litellm.suppress_debug_info = True


# ─── Settings ───────────────────────────────────────────────────────────────


@dataclass
class LlmSettings:
    """Per-request LLM configuration.

    Stored in the app's user settings file. The UI Settings dialog writes
    this; agents read it. Kept small on purpose – if you need a knob that
    isn't here, add it deliberately.
    """

    model: str = "anthropic/claude-sonnet-4-5"
    api_key: str | None = None        # None → read from env var for provider
    api_base: str | None = None       # for Ollama / local models / Azure
    temperature: float = 0.2           # low – we want deterministic translation
    max_tokens: int = 4096
    timeout_seconds: int = 120

    # Extra provider-specific kwargs passed straight to litellm.completion
    extra: dict = field(default_factory=dict)

    @property
    def provider(self) -> str:
        """'openai' | 'anthropic' | 'gemini' | 'ollama' | ..."""
        return self.model.split("/", 1)[0] if "/" in self.model else "openai"

    def api_key_env_var(self) -> str:
        """Convention for the env var holding the API key for this provider."""
        return {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "google": "GEMINI_API_KEY",
            "mistral": "MISTRAL_API_KEY",
            "groq": "GROQ_API_KEY",
            "ollama": "",  # local, no key needed
        }.get(self.provider, f"{self.provider.upper()}_API_KEY")


# ─── Message helpers ────────────────────────────────────────────────────────


Message = dict  # LiteLLM uses plain dicts: {"role": "user", "content": "..."}


def system(content: str) -> Message:
    return {"role": "system", "content": content}


def user(content: str) -> Message:
    return {"role": "user", "content": content}


def assistant(content: str) -> Message:
    return {"role": "assistant", "content": content}


# ─── Client ─────────────────────────────────────────────────────────────────


class LlmClient:
    """Single entry point for LLM calls.

    Not tied to any particular agent. Use `complete` for one-shot calls
    or `stream` when you want tokens as they arrive (chat UI).
    """

    def __init__(self, settings: LlmSettings) -> None:
        self.settings = settings
        # Push the API key into the env var LiteLLM expects, but only if
        # the user gave us one explicitly. If they didn't, we trust the
        # ambient environment (so power users can manage keys their way).
        if settings.api_key:
            env_var = settings.api_key_env_var()
            if env_var:
                os.environ[env_var] = settings.api_key

    # ── One-shot completion ─────────────────────────────────────────────

    def complete(self, messages: Iterable[Message]) -> str:
        """Return the full assistant reply as a string."""
        response = litellm.completion(
            model=self.settings.model,
            messages=list(messages),
            temperature=self.settings.temperature,
            max_tokens=self.settings.max_tokens,
            api_base=self.settings.api_base,
            timeout=self.settings.timeout_seconds,
            **self.settings.extra,
        )
        return response["choices"][0]["message"]["content"] or ""

    # ── Streaming completion ────────────────────────────────────────────

    def stream(self, messages: Iterable[Message]) -> Iterator[str]:
        """Yield partial assistant reply chunks as they arrive.

        Usage::

            for chunk in client.stream(msgs):
                chat_panel.append_assistant_text(chunk)
        """
        response = litellm.completion(
            model=self.settings.model,
            messages=list(messages),
            temperature=self.settings.temperature,
            max_tokens=self.settings.max_tokens,
            api_base=self.settings.api_base,
            timeout=self.settings.timeout_seconds,
            stream=True,
            **self.settings.extra,
        )
        for part in response:
            try:
                delta = part["choices"][0]["delta"].get("content") or ""
            except (KeyError, IndexError, AttributeError):
                delta = ""
            if delta:
                yield delta

    # ── Convenience: stream to a callback (for Qt main thread) ──────────

    def stream_to(
        self,
        messages: Iterable[Message],
        on_chunk: Callable[[str], None],
    ) -> str:
        """Stream and also collect into one string.

        Useful when the UI wants both per-token updates (for the chat
        typing effect) and the full final reply (for history storage).
        """
        full = []
        for chunk in self.stream(messages):
            full.append(chunk)
            on_chunk(chunk)
        return "".join(full)


# ─── Diagnostics ────────────────────────────────────────────────────────────


def validate_settings(settings: LlmSettings) -> tuple[bool, str]:
    """Quick preflight check without hitting the network.

    Returns (ok, message). Used by the Settings dialog to tell the user
    "looks configured" vs "no API key for anthropic/claude-sonnet-4-5".
    """
    if not settings.model:
        return False, "No model selected."

    if settings.provider == "ollama":
        # Local model, no key needed, just need api_base to be reachable.
        if not settings.api_base:
            return True, "Ollama: using default http://localhost:11434"
        return True, f"Ollama: {settings.api_base}"

    env_var = settings.api_key_env_var()
    if not settings.api_key and not os.environ.get(env_var):
        return False, f"No API key for {settings.provider} (set {env_var})."

    return True, f"{settings.provider} / {settings.model.split('/', 1)[-1]}"


__all__ = [
    "LlmSettings",
    "LlmClient",
    "Message",
    "system",
    "user",
    "assistant",
    "validate_settings",
]
