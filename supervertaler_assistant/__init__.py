"""Supervertaler Assistant – standalone cross-platform client for memory banks.

The Supervertaler Assistant is an AI assistant for professional translators.
One of the things it can do is consult a memory bank – a structured
Obsidian-compatible folder of Markdown articles about clients, terminology,
domains and style. This standalone package is the Python/PyQt6 host for the
Assistant; a companion host ships inside the Supervertaler for Trados plugin.

Public API lives in :mod:`supervertaler_assistant.memory_bank` and
:mod:`supervertaler_assistant.llm`; the agents in
:mod:`supervertaler_assistant.agents` wire them together; the PyQt6 shell
lives in :mod:`supervertaler_assistant.ui`.
"""

__version__ = "0.1.0"
