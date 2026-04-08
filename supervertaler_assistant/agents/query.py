"""
supervertaler_assistant.agents.query
====================================

Chat backend for the Supervertaler Assistant standalone app.

This is the simplest of the four agents because it has no file-writing
side effects – the user asks a question, the agent reads the memory
bank, and the LLM produces an answer with ``[[backlinks]]`` citations.

Design
------
- The **system prompt** is loaded from ``06_TEMPLATES/query.md`` in the
  user's memory bank when available, otherwise from the bundled template
  shipped with the app (``supervertaler_assistant/templates/query.md``).
  Users who edit their memory bank template get their edits picked up;
  users who don't always get a sensible default.

- **Memory bank context is injected fresh on every turn** rather than
  pre-loaded and sticky. This keeps the conversation grounded as the
  user pivots between topics (e.g. "now what about client X?"), and it
  means that memory bank edits become visible without restarting the
  chat. The 30s frontmatter-index cache in
  :class:`supervertaler_assistant.memory_bank.MemoryBankReader` keeps
  the cost low.

- **Streaming by default** so the Qt chat panel can render tokens as
  they arrive. A non-streaming `ask()` convenience is also provided for
  scripting / tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from ..llm import LlmClient, Message, assistant, system, user
from ..memory_bank import MemoryBankReader


# ─── Prompt loading ─────────────────────────────────────────────────────────


def _load_query_prompt(memory_bank_dir: Path) -> str:
    """Load the query agent's system prompt.

    Priority:
        1. ``{memory_bank}/06_TEMPLATES/query.md`` if the user has one
        2. bundled fallback in ``supervertaler_assistant/templates/query.md``
    """
    user_template = memory_bank_dir / "06_TEMPLATES" / "query.md"
    if user_template.is_file():
        try:
            return user_template.read_text(encoding="utf-8")
        except OSError:
            pass

    bundled = Path(__file__).resolve().parent.parent / "templates" / "query.md"
    if bundled.is_file():
        return bundled.read_text(encoding="utf-8")

    # Last-resort fallback – should never trigger if the app is packaged
    # correctly, but means the chat panel won't crash if someone deletes
    # the templates directory.
    return (
        "You are the Supervertaler Assistant. Answer the user's questions "
        "about their translation memory bank, citing sources with "
        "[[backlinks]]. If the memory bank is silent on a topic, say so."
    )


# ─── Conversation state ─────────────────────────────────────────────────────


@dataclass
class ChatTurn:
    """One exchange in the chat history.

    We store both the raw user question and the memory bank context that
    was injected for it, so the UI can show "this turn consulted X, Y, Z"
    if the user wants to inspect provenance.
    """

    user_text: str
    assistant_text: str = ""
    memory_bank_summary: str | None = None
    memory_bank_paths: list[str] = field(default_factory=list)


@dataclass
class ChatSession:
    """All state for a single chat session.

    The agent mutates `turns` as the user converses. Persist this to
    disk if you want chat history across app restarts.
    """

    memory_bank_dir: Path
    turns: list[ChatTurn] = field(default_factory=list)

    # Optional per-session context hints that flow into every memory
    # bank lookup. The Settings dialog / chat header lets the user pin a
    # client etc.
    project_name: str | None = None
    domain: str | None = None
    source_lang: str | None = None
    target_lang: str | None = None


# ─── The agent itself ───────────────────────────────────────────────────────


class QueryAgent:
    """Chat backend that consults the memory bank on every turn."""

    def __init__(self, llm: LlmClient, memory_bank_dir: str | Path) -> None:
        self.llm = llm
        self.memory_bank_dir = Path(memory_bank_dir)
        self.reader = MemoryBankReader(self.memory_bank_dir)
        self._system_prompt = _load_query_prompt(self.memory_bank_dir)

    # ── Public API ──────────────────────────────────────────────────────

    def ask(self, session: ChatSession, question: str) -> ChatTurn:
        """One-shot, non-streaming. Good for tests and scripting."""
        turn = ChatTurn(user_text=question)
        session.turns.append(turn)
        messages = self._build_messages(session, turn)
        turn.assistant_text = self.llm.complete(messages)
        return turn

    def ask_stream(
        self,
        session: ChatSession,
        question: str,
        on_chunk: Callable[[str], None],
    ) -> ChatTurn:
        """Streaming. `on_chunk` is called on the caller's thread for each
        partial token. Returns the fully populated turn once streaming
        finishes.
        """
        turn = ChatTurn(user_text=question)
        session.turns.append(turn)
        messages = self._build_messages(session, turn)

        chunks: list[str] = []

        def handle(chunk: str) -> None:
            chunks.append(chunk)
            on_chunk(chunk)

        self.llm.stream_to(messages, handle)
        turn.assistant_text = "".join(chunks)
        return turn

    def stream_tokens(
        self,
        session: ChatSession,
        question: str,
    ) -> Iterator[str]:
        """Pure generator variant – yields chunks directly.

        Convenient when the caller wants to drive streaming itself
        (e.g. from a Qt ``QThread`` that emits signals per chunk).
        """
        turn = ChatTurn(user_text=question)
        session.turns.append(turn)
        messages = self._build_messages(session, turn)

        chunks: list[str] = []
        for chunk in self.llm.stream(messages):
            chunks.append(chunk)
            yield chunk
        turn.assistant_text = "".join(chunks)

    # ── Internals ───────────────────────────────────────────────────────

    def _build_messages(self, session: ChatSession, turn: ChatTurn) -> list[Message]:
        """Assemble the full message list for one LLM call.

        Structure:
            [ system: query.md prompt ]
            [ system: MEMORY BANK section for this turn ]   ← fresh each turn
            [ user / assistant history (previous turns) ]
            [ user: this turn's question ]
        """
        messages: list[Message] = [system(self._system_prompt)]

        # Fresh memory bank snapshot for this turn
        ctx = self.reader.load_context(
            project_name=session.project_name,
            domain=session.domain,
            source_lang=session.source_lang,
            target_lang=session.target_lang,
            token_budget=4000,
        )
        if ctx is not None and ctx.has_content:
            mb_prompt = MemoryBankReader.format_for_prompt(ctx)
            if mb_prompt:
                messages.append(system(mb_prompt))
            turn.memory_bank_summary = ctx.summary()
            turn.memory_bank_paths = [
                p
                for p in [ctx.client_profile_path, ctx.domain_article_path, ctx.style_guide_path]
                if p
            ] + list(ctx.terminology_paths)

        # Historical turns (everything before the current one)
        for previous in session.turns[:-1]:
            messages.append(user(previous.user_text))
            if previous.assistant_text:
                messages.append(assistant(previous.assistant_text))

        # Current user question
        messages.append(user(turn.user_text))
        return messages


__all__ = ["QueryAgent", "ChatSession", "ChatTurn"]
