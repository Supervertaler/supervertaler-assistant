"""SuperMemory agents — wrappers around the four KB workflow prompts.

Each agent loads its prompt from ``supermemory.templates``, assembles
the messages, and talks to the LLM via :class:`supermemory.llm.LlmClient`.

Agents
------
- :class:`~supermemory.agents.query.QueryAgent`      — chat; consults the vault
- :class:`~supermemory.agents.compile.CompileAgent`  — Process Inbox
- :class:`~supermemory.agents.lint.LintAgent`        — Health Check
- :class:`~supermemory.agents.distill.DistillAgent`  — TMX/DOCX/PDF/TBX → inbox
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
