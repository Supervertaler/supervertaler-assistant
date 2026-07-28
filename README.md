# Supervertaler Assistant (not pursued)

> **This project was not completed, and is not being developed.**
>
> For the memory-bank format — the specification and a starting skeleton — see
> **[Supervertaler/Supervertaler-SuperMemory](https://github.com/Supervertaler/Supervertaler-SuperMemory)**.

## What this was going to be

A standalone cross-platform desktop app (Python + PyQt6) that would sit beside any CAT tool and read the same **memory bank** as [Supervertaler for Trados](https://github.com/Supervertaler/Supervertaler-for-Trados) — chat over your accumulated client, terminology, domain and style knowledge, process an inbox of raw material into structured articles, run a health check, and distil TMX/DOCX/PDF/TBX files into draft notes.

It reached a working prototype and stopped there.

## Why it was dropped

The problem it existed to solve — *using a memory bank outside Trados Studio* — was solved a different way.

Since **v18.20.146** Supervertaler for Trados exposes memory banks over its [MCP server](https://docs.supervertaler.com/trados/mcp-server/), so any MCP client can read your bank directly: Claude Desktop, Claude Code, or anything else that speaks the protocol. You get chat over your knowledge without a second application to install, update and maintain, and using whatever AI subscription you already pay for.

Against that, a standalone app would have needed its own chat interface, its own provider plumbing, its own release process — to arrive at roughly the same place. So it stays here as reference rather than being finished.

## What's still useful here

The code remains readable and MIT-licensed, should anyone want it:

- `supervertaler_assistant/memory_bank.py` — memory-bank reader: frontmatter parsing, relevance scoring, context building
- `supervertaler_assistant/agents/` — the compile, lint, distil and query agents
- `supervertaler_assistant/llm.py` — a thin LiteLLM wrapper covering ~100 providers

The format specification and skeleton that used to live here have moved to [Supervertaler-SuperMemory](https://github.com/Supervertaler/Supervertaler-SuperMemory), which is where they are maintained.

## Licence

MIT — see [LICENSE](LICENSE).
