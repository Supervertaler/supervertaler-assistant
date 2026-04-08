# SuperMemory

A self-organising, LLM-maintained translation knowledge base — the standalone cross-platform client.

Inspired by Andrej Karpathy's [LLM Knowledge Base](https://venturebeat.com/data/karpathy-shares-llm-knowledge-base-architecture-that-bypasses-rag-with-an) architecture. No vector database, no RAG — just structured Markdown, `[[backlinks]]`, and an LLM acting as your research librarian.

SuperMemory reads any [SuperMemory-format vault](https://github.com/Supervertaler/Supervertaler-SuperMemory) and provides:

- A **chat interface** that consults the vault on every turn, answering questions about your terminology, clients and domains with `[[backlink]]` citations.
- **Process Inbox** — drop raw material (client briefs, glossaries, feedback) into `00_INBOX/` and let the LLM compile it into structured, interlinked articles.
- **Health Check** — scan the vault for conflicting terminology, broken links, stale content and duplicates; auto-fix what can be fixed, flag the rest.
- **Distill** — extract knowledge from TMX, DOCX, PDF and TBX files into inbox-ready Markdown articles.

## Who this is for

Translators who want SuperMemory's workflow without being tied to Trados Studio or a specific workbench. Runs on Windows, macOS and Linux via Python + PyQt6. Works alongside:

- [Supervertaler for Trados](https://github.com/Supervertaler/Supervertaler-for-Trados) — Trados Studio plugin with the same SuperMemory feature
- [Supervertaler](https://github.com/michaelbeijer/Supervertaler) — the cross-platform desktop translation workbench
- Any other CAT tool or editor — SuperMemory is a vault-reading app, not a translation workbench itself

## Install

```bash
git clone https://github.com/Supervertaler/SuperMemory.git
cd SuperMemory
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS / Linux: source .venv/bin/activate
pip install -e .
```

For Distill support (TMX/DOCX/PDF/TBX extraction), also install the optional extras:

```bash
pip install -e ".[distill]"
```

## Run

```bash
supermemory
```

Or directly:

```bash
python -m supermemory.ui.main_window
```

On first launch, open **Edit → Settings** and configure:

- **Vault folder** — point at any SuperMemory vault (or create one from the [skeleton repo](https://github.com/Supervertaler/Supervertaler-SuperMemory))
- **Model** — any LiteLLM-supported model (Anthropic, OpenAI, Google, Mistral, Groq, Ollama, Azure, Bedrock, and ~90 others)
- **API key** — can be left blank if the provider's env var is already set (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.)

SuperMemory will try to auto-load the default vault at `~/Supervertaler/supermemory/` on startup if one exists there.

## The vault format

SuperMemory operates on an [Obsidian-compatible vault](https://github.com/Supervertaler/Supervertaler-SuperMemory) with this structure:

```
supermemory/
├── 00_INBOX/          Raw material drop zone
├── 01_CLIENTS/        Client profiles
├── 02_TERMINOLOGY/    Term articles with reasoning
├── 03_DOMAINS/        Domain knowledge
├── 04_STYLE/          Style guides
├── 05_INDICES/        Auto-generated indexes
└── 06_TEMPLATES/      Agent prompt templates
```

Every article is a Markdown file with YAML frontmatter and `[[backlinks]]`. The format is shared byte-for-byte with [Supervertaler for Trados](https://github.com/Supervertaler/Supervertaler-for-Trados) — a vault created by either tool is openable by the other.

## Architecture

```
supermemory/
├── vault.py              Vault reader (frontmatter, scoring, KB context)
├── llm.py                LiteLLM wrapper — one interface for ~100 providers
├── settings.py           Cross-platform persisted settings (platformdirs)
├── agents/
│   ├── query.py          Chat backend — reads context fresh each turn
│   ├── compile.py        Process Inbox
│   ├── lint.py           Health Check
│   ├── distill.py        TMX / DOCX / PDF / TBX extraction
│   └── _output.py        Shared `### FILE:` parser + safe vault writes
├── ui/
│   ├── main_window.py    PyQt6 shell — toolbar, chat, menus
│   └── settings_dialog.py
└── templates/            Bundled copies of compile/lint/query/translate/distill prompts
```

## Design principles

- **No vector DB, no embeddings.** At translation-project scale (~hundreds of articles) structured Markdown + LLM reasoning outperforms RAG.
- **Human-readable and auditable.** Every translation decision traces to a specific `.md` file you can open and read.
- **Self-healing.** Health Check catches inconsistencies, broken links and stale content automatically.
- **Portable.** It's just Markdown files. If any tool disappears, your knowledge stays.
- **Bring your own LLM.** Including local models via Ollama or LM Studio — your vault never has to leave your machine.

## License

MIT — see [LICENSE](LICENSE).

## Part of the Supervertaler ecosystem

- [Supervertaler](https://github.com/michaelbeijer/Supervertaler) — desktop PyQt6 translation workbench
- [Supervertaler for Trados](https://github.com/Supervertaler/Supervertaler-for-Trados) — Trados Studio plugin (paid, source-available)
- [Supervertaler-SuperMemory](https://github.com/Supervertaler/Supervertaler-SuperMemory) — vault format specification and skeleton
