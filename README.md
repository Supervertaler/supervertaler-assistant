# Supervertaler Assistant

A cross-platform AI assistant for professional translators.

The Supervertaler Assistant is a desktop app (Python + PyQt6) that sits beside whatever CAT tool you use and helps you with the thinking side of translation work: looking up client preferences, terminology decisions, domain knowledge and style rules, and keeping that knowledge organised as your practice grows.

Its long-term memory lives in a **memory bank** – a plain-text, Obsidian-compatible folder of Markdown articles with `[[backlinks]]`. No vector database, no RAG. The Assistant reads the memory bank fresh on every turn and reasons over it with any LLM of your choice.

> The memory-bank approach is inspired by Andrej Karpathy's [LLM Knowledge Base](https://venturebeat.com/data/karpathy-shares-llm-knowledge-base-architecture-that-bypasses-rag-with-an) architecture – structured Markdown + an LLM reasoning over it, instead of embeddings and retrieval.

## What the Assistant does today

- **Chat** – ask the Assistant anything about your clients, terminology, domains or style. Every answer cites the specific memory-bank articles it drew from, via `[[backlink]]`s you can click open in Obsidian.
- **Process Inbox** – drop raw material (client briefs, glossaries, feedback emails, reference PDFs) into `00_INBOX/` and the Assistant compiles it into structured, interlinked articles in the right folders.
- **Health Check** – scan the memory bank for conflicting terminology, broken links, stale content and duplicates; auto-fix what can be fixed, flag the rest in a dated report.
- **Distill** – extract knowledge from TMX, DOCX, PDF and TBX files into inbox-ready Markdown articles, so you can seed a memory bank from existing assets.

Memory-bank maintenance is the Assistant's first feature. More translator-facing features are planned – file them under the same roof, under the same chat.

## Who this is for

Freelance and in-house translators who want a single place to keep track of how they work for each client, and an assistant that actually uses that knowledge. Runs on Windows, macOS and Linux via Python + PyQt6.

It plays nicely with the rest of the Supervertaler family and with anything else you use:

- [Supervertaler for Trados](https://github.com/Supervertaler/Supervertaler-for-Trados) – Trados Studio plugin that reads the **same** memory-bank format, so one memory bank serves both the standalone Assistant and your Trados projects.
- [Supervertaler](https://github.com/michaelbeijer/Supervertaler) – the cross-platform desktop translation workbench.
- Any other CAT tool or editor – the Assistant is host-agnostic; it just needs a folder to read and write.

## Install

```bash
git clone https://github.com/Supervertaler/supervertaler-assistant.git
cd supervertaler-assistant
python -m venv .venv
# Windows:        .venv\Scripts\activate
# macOS / Linux:  source .venv/bin/activate
pip install -e .
```

For Distill support (TMX / DOCX / PDF / TBX extraction), also install the optional extras:

```bash
pip install -e ".[distill]"
```

## Run

```bash
supervertaler-assistant
```

Or directly:

```bash
python -m supervertaler_assistant.ui.main_window
```

On first launch, open **Edit → Settings** and configure:

- **Memory bank folder** – point at any memory bank, or create a new one from the bundled [`skeleton/`](skeleton/) in this repo.
- **Model** – any LiteLLM-supported model (Anthropic, OpenAI, Google, Mistral, Groq, Ollama, Azure, Bedrock, and ~90 others).
- **API key** – can be left blank if the provider's env var is already set (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.).

The Assistant will try to auto-load a memory bank at `~/Supervertaler/memory-bank/` on startup if one exists there.

## The memory-bank format

The Assistant operates on an Obsidian-compatible memory bank with this structure:

```
my-memory-bank/
├── 00_INBOX/          Raw material drop zone
├── 01_CLIENTS/        Client profiles
├── 02_TERMINOLOGY/    Term articles with reasoning
├── 03_DOMAINS/        Domain knowledge
├── 04_STYLE/          Style guides
├── 05_INDICES/        Auto-generated indexes
└── 06_TEMPLATES/      Agent prompt templates
```

Every article is a Markdown file with YAML frontmatter and `[[backlinks]]`. The format is documented in full in [SPEC.md](SPEC.md) – the **Supervertaler Memory Bank Format Specification** – and is shared byte-for-byte with [Supervertaler for Trados](https://github.com/Supervertaler/Supervertaler-for-Trados). A memory bank created by either host is openable by the other.

A ready-to-use starting skeleton (with `.obsidian/` defaults, example articles and all prompt templates) lives in [`skeleton/`](skeleton/). Copy it to `~/Supervertaler/memory-bank/` to get started.

## Architecture

```
supervertaler_assistant/
├── memory_bank.py         Memory-bank reader (frontmatter, scoring, context building)
├── llm.py                 LiteLLM wrapper – one interface for ~100 providers
├── settings.py            Cross-platform persisted settings (platformdirs)
├── agents/
│   ├── query.py           Chat backend – reads the memory bank fresh each turn
│   ├── compile.py         Process Inbox
│   ├── lint.py            Health Check
│   ├── distill.py         TMX / DOCX / PDF / TBX extraction
│   └── _output.py         Shared `### FILE:` parser + safe memory-bank writes
├── ui/
│   ├── main_window.py     PyQt6 shell – toolbar, chat, menus
│   └── settings_dialog.py
└── templates/             Bundled copies of compile / lint / query / translate / distill prompts
```

## Design principles

- **No vector DB, no embeddings.** At translation-project scale (hundreds of articles, not millions), structured Markdown + LLM reasoning outperforms RAG – and stays auditable.
- **Human-readable and auditable.** Every answer traces to specific `.md` files you can open, read and edit by hand.
- **Self-healing.** Health Check catches inconsistencies, broken links and stale content automatically, in plain English, with file-level fixes.
- **Portable.** It's just Markdown. If any tool in this ecosystem disappears, your knowledge stays – openable in Obsidian, VS Code, or any text editor.
- **Bring your own LLM.** Including local models via Ollama or LM Studio – your memory bank never has to leave your machine.
- **One memory bank, many hosts.** The same folder works with the standalone Assistant and with Supervertaler for Trados. Pick whichever host fits the job.

## Licence

MIT – see [LICENSE](LICENSE).

## Part of the Supervertaler ecosystem

- [Supervertaler](https://github.com/michaelbeijer/Supervertaler) – desktop PyQt6 translation workbench
- [Supervertaler for Trados](https://github.com/Supervertaler/Supervertaler-for-Trados) – Trados Studio plugin (paid, source-available)
- [supervertaler-assistant](https://github.com/Supervertaler/supervertaler-assistant) – this repo: the standalone Assistant, the memory-bank format spec, and the starting skeleton
