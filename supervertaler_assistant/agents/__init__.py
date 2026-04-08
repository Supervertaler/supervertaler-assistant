"""Supervertaler Assistant agents – wrappers around the four workflow prompts.

Each agent loads its prompt from ``supervertaler_assistant.templates``,
assembles the messages, and talks to the LLM via
:class:`supervertaler_assistant.llm.LlmClient`. Together they implement the
four memory-bank maintenance flows that the Assistant exposes in its UI.

Agents
------
- :class:`~supervertaler_assistant.agents.query.QueryAgent`      – chat; consults the memory bank
- :class:`~supervertaler_assistant.agents.compile.CompileAgent`  – Process Inbox
- :class:`~supervertaler_assistant.agents.lint.LintAgent`        – Health Check
- :class:`~supervertaler_assistant.agents.distill.DistillAgent`  – TMX/DOCX/PDF/TBX → inbox
"""

from .compile import CompileAgent, CompileResult
from .distill import DistillAgent, DistillResult, SourceFormat
from .lint import LintAgent, LintResult
from .query import ChatSession, ChatTurn, QueryAgent

__all__ = [
    "QueryAgent",
    "ChatSession",
    "ChatTurn",
    "CompileAgent",
    "CompileResult",
    "LintAgent",
    "LintResult",
    "DistillAgent",
    "DistillResult",
    "SourceFormat",
]
