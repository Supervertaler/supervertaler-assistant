"""SuperMemory — standalone cross-platform client for SuperMemory vaults.

A self-organising, LLM-maintained translation knowledge base. Reads any
SuperMemory-compatible Obsidian vault and provides a chat interface plus
the four workflow actions: Process Inbox, Health Check, Distill, Refresh.

Public API lives in :mod:`supermemory.vault` and :mod:`supermemory.llm`;
the agents in :mod:`supermemory.agents` wire them together; the PyQt6
shell lives in :mod:`supermemory.ui`.
"""

__version__ = "0.1.0"
