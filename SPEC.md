# Supervertaler Memory Bank Format – Specification

**Version:** 1.1
**Status:** Stable
**Last updated:** 2026-04-08

This document defines the Supervertaler memory bank format as an explicit contract. Any client that reads or writes a memory bank MUST conform to this spec, so that a memory bank created by one tool is fully interoperable with every other tool.

A **memory bank** is the structured, Obsidian-compatible folder of Markdown articles that the Supervertaler Assistant consults when answering questions, proposing terminology, or translating. It is the Assistant's long-term memory.

## Scope

This spec covers:

- Folder layout
- YAML frontmatter conventions per folder
- `[[backlinks]]` (wikilink) conventions
- Agent prompt templates
- The `### FILE:` output format used by write-capable agents
- Scoring rules for terminology relevance
- Token-budget trimming priority
- File-path safety rules for automated writes
- Archive conventions for processed inbox files

This spec does **not** prescribe:

- Which LLM provider or model a client uses
- The specific UI a client exposes
- Whether a client implements all four agents or only a subset

## Conformance levels

A client is a **reader** if it can load a memory bank and surface articles (e.g. render a chat answer that cites them).

A client is a **writer** if it can create or modify `.md` files inside the memory bank.

A client is **fully conformant** if it implements:

- The folder layout
- Frontmatter parsing (including the code-fence tolerance rule, §4.3)
- `[[backlinks]]` extraction
- The scoring rules (§7)
- The token-budget trimming priority (§8)
- The `### FILE:` output format (writers only, §9)
- The file-path safety rules (writers only, §10)
- The archive convention for processed inbox files (writers only, §11)

Known conformant clients as of this spec version:

- **Supervertaler for Trados** (C#/.NET Trados Studio plugin) – reader + writer
- **Supervertaler Assistant** standalone (Python/PyQt6 cross-platform app) – reader + writer

## 1. Folder layout

A memory bank is an Obsidian-compatible folder with this exact top-level structure:

```
<memory-bank-root>/
├── 00_INBOX/          Raw material drop zone
├── 01_CLIENTS/        Client profiles
├── 02_TERMINOLOGY/    Term articles with reasoning
├── 03_DOMAINS/        Domain knowledge
├── 04_STYLE/          Style guides
├── 05_INDICES/        Auto-generated indexes
└── 06_TEMPLATES/      Agent prompt templates
```

### 1.1 Numeric prefixes

Folder numeric prefixes (`00_` through `06_`) are part of the contract. They control sort order in Obsidian's file tree and in the agent prompts. Clients MUST NOT rename these folders.

### 1.2 Content folders

For the purposes of memory bank lookups, the **content folders** are:

```
01_CLIENTS, 02_TERMINOLOGY, 03_DOMAINS, 04_STYLE
```

`00_INBOX/` is raw, unprocessed material – it MUST NOT be included in memory bank context for translation or query agents. `05_INDICES/` is auto-generated and MAY be included. `06_TEMPLATES/` is prompt templates – it MUST NOT be included in memory bank context.

### 1.3 Archive subfolder

Processed inbox files live at `00_INBOX/_archive/`. Clients MUST create this folder on first archive write.

### 1.4 Optional user directories

Clients MUST tolerate the presence of `.obsidian/`, `.trash/`, and arbitrary user-created subfolders. They SHOULD be ignored by agents.

## 2. File naming

- All content files MUST have a `.md` extension.
- Filenames MAY contain Unicode characters, spaces, parentheses, and arrows (`→`), to match Obsidian conventions.
- Filenames MUST NOT contain path separators (`/`, `\`) or characters forbidden by Windows NTFS (`<>:"|?*`).
- Filenames SHOULD be descriptive enough to be human-browsable without opening the file (e.g. `compliance → naleving.md`, not `term-001.md`).

### 2.1 Reserved prefixes

Files beginning with `_EXAMPLE_` are example articles shipped with the skeleton. They are part of the memory bank's documentation and MUST NOT be processed by agents as real content. Readers MUST skip them when building memory bank context.

Files beginning with an underscore (`_`) other than `_EXAMPLE_` are reserved for future use. Clients SHOULD skip them conservatively.

## 3. Character encoding

All files in the memory bank MUST be UTF-8 (no BOM). Line endings MAY be `LF` or `CRLF`; clients MUST tolerate both on read and SHOULD preserve the existing convention on write.

## 4. YAML frontmatter

### 4.1 General rules

Every article in `01_CLIENTS/`, `02_TERMINOLOGY/`, `03_DOMAINS/`, and `04_STYLE/` MUST begin with a YAML frontmatter block delimited by `---` lines:

```markdown
---
key: value
list_key: ["item1", "item2"]
---

# Article title
...
```

The frontmatter MUST be valid YAML. Clients SHOULD use a proper YAML parser (e.g. `python-frontmatter`, `YamlDotNet`) rather than line-by-line parsing, so that multi-line strings, quoted values, and nested lists round-trip cleanly.

### 4.2 Keys that are part of this spec

All content folders:

- `last_updated` (ISO date, `YYYY-MM-DD`) – required
- `compiled_from` (string, source file path) – optional, set by writers

`01_CLIENTS/`:

| Key | Type | Required | Description |
|---|---|---|---|
| `client` | string | yes | Canonical client name |
| `languages` | list of strings | yes | Language pair hints (`"en-US → nl-BE"`) |
| `domains` | list of wikilinks | recommended | `["[[Legal]]", "[[Marketing]]"]` |

`02_TERMINOLOGY/`:

| Key | Type | Required | Description |
|---|---|---|---|
| `term_source` | string | yes | Source-language term |
| `term_target` | string | yes | Target-language term |
| `source_lang` | string | yes | BCP-47 code (`en-US`, `nl-BE`) |
| `target_lang` | string | yes | BCP-47 code |
| `domain` | wikilink | recommended | `"[[Legal]]"` |
| `clients` | list of wikilinks | recommended | `["[[Acme Corporation]]"]` |
| `status` | enum | yes | `approved`, `proposed`, or `rejected` |

`03_DOMAINS/`:

| Key | Type | Required | Description |
|---|---|---|---|
| `domain` | string | yes | Domain name |
| `languages` | list of strings | recommended | Applicable language pairs |
| `related_domains` | list of wikilinks | optional | Cross-references |

`04_STYLE/`:

| Key | Type | Required | Description |
|---|---|---|---|
| `scope` | string | yes | `general`, `client`, `domain`, etc. |
| `languages` | list of strings | yes | Applicable language pairs |

`00_INBOX/` (for archived, processed files – see §11):

| Key | Type | Required | Description |
|---|---|---|---|
| `compiled` | boolean | yes | `true` once Process Inbox has read it |
| `compiled_date` | ISO date | yes | When Process Inbox wrote the archive |
| `compiled_to` | list of strings | yes | Relative paths to generated articles |

### 4.3 Code-fence tolerance (MANDATORY)

LLM replies are often wrapped in a ` ```markdown` code fence. When a user pastes such a reply directly into a memory bank file, the frontmatter ends up inside a fenced block:

````markdown
```markdown
---
client: "Acme"
---

# Acme
...
```
````

Clients reading frontmatter MUST tolerate this pattern:

> If the first non-empty line of the file starts with ` ``` `, strip that line (and any matching closing fence) before looking for the `---` frontmatter delimiters.

This rule was added in spec v1.0 after two production files in a live memory bank were rendered invisible to clients that did strict frontmatter parsing. Both the Trados plugin and the Python standalone now implement it.

### 4.4 Unknown keys

Clients MUST preserve unknown frontmatter keys on round-trip writes. Future keys will be added to this spec without a major-version bump as long as they are additive.

## 5. Body content

After the frontmatter, the body is standard CommonMark + Obsidian extensions:

- `[[wikilinks]]` for internal references (§6)
- Tables, lists, code blocks, blockquotes
- Optional `# Heading` sections

Clients rendering article bodies SHOULD use a CommonMark renderer with tables enabled. Emoji are allowed but discouraged.

## 6. Wikilinks

The memory bank uses Obsidian-style wikilinks for all internal cross-references:

```markdown
See also: [[Acme Corporation]], [[Legal]], [[compliance → naleving]].
```

### 6.1 Resolution rules

A wikilink `[[Target]]` resolves to a file named `Target.md` in any content folder. Resolution is **case-insensitive** but **exact-match** on the rest of the name (no fuzzy matching).

Wikilinks with a pipe alias (`[[Target|display text]]`) are allowed; the resolver MUST use the part before the pipe.

### 6.2 Dead links

A wikilink that does not resolve is not an error; Health Check reports it but does not auto-remove it. This allows forward-references to articles that will exist after the next Process Inbox run.

## 7. Scoring rules for memory bank lookup

When assembling memory bank context for a query (chat turn, translation, etc.), clients MUST score candidate articles using this exact rule set, so that scoring is byte-for-byte reproducible across clients:

| Signal | Points | Description |
|---|---|---|
| Client match | **+3** | Article's `client` or `clients` list contains the active client (case-insensitive exact match) |
| Domain match | **+2** | Article's `domain` or `domains` list contains the active domain (case-insensitive exact match) |
| Language match | **+1** | Article's language tags contain the active source or target language code |

Ties are broken by:

1. Folder priority: `01_CLIENTS` > `02_TERMINOLOGY` > `03_DOMAINS` > `04_STYLE`
2. `last_updated` descending (newer first)
3. Filename ascending (stable lexicographic order)

Articles with a score of 0 MAY still be included if the token budget allows.

### 7.1 Language code extraction

Given a frontmatter value like `"en-US → nl-BE"`, the client MUST extract both sides as separate tokens and compare them case-insensitively. A value like `"en"` MUST match both `en-US` and `en-GB` (prefix match on the language subtag).

## 8. Token-budget trimming priority

When the assembled memory bank context exceeds the configured token budget, clients MUST trim in this priority order (most important retained last):

1. **Client profiles** – retained to the last drop
2. **Domain articles** – retained until client headroom is needed
3. **Style guides** – trimmed before terminology
4. **Terminology articles** – trimmed first

Within each tier, drop lowest-scoring articles first. The rationale is that client-specific decisions are the most expensive to re-derive and the most dangerous to miss; style guides are usually reference-sized and compressible.

Clients MUST NOT truncate individual articles mid-body to fit the budget. They MUST either include an article whole or drop it entirely. (Exception: Health Check snapshots in §12.)

## 9. Agent output format (writers only)

Write-capable agents produce LLM replies that contain zero or more file blocks. Each block is introduced by a `### FILE:` marker followed by the full file content:

````markdown
Here's my analysis...

### FILE: 01_CLIENTS/Acme Corporation.md
---
client: "Acme Corporation"
languages: ["en-US → nl-BE"]
last_updated: 2026-04-08
---

# Acme Corporation
...

### FILE: 02_TERMINOLOGY/compliance → naleving.md
---
term_source: "compliance"
...
---

# compliance → naleving
...
````

### 9.1 Marker format

The marker is matched by this regex (Python syntax, multi-line):

```regex
^\s*#{2,4}\s*FILE\s*:\s*(?P<path>.+?)\s*$
```

That is:

- 2 to 4 `#` characters (`##`, `###`, or `####` all valid)
- Optional whitespace
- The literal word `FILE` (case-sensitive)
- `:` (optionally surrounded by whitespace)
- The relative file path, trimmed

### 9.2 Block termination

A file block ends at the next `### FILE:` marker, or at the end of the LLM reply. Content between markers is the literal file body. Clients MUST NOT strip wrapping code fences from the block body – if the LLM wraps each block in ` ```markdown`, the client MUST strip exactly one outer fence and then apply §4.3 when parsing frontmatter.

### 9.3 Multiple blocks per reply

Compile and Lint agents routinely emit multiple blocks in a single LLM turn. Clients MUST support this.

## 10. File-path safety rules (writers only)

Before writing a file block to disk, clients MUST validate the target path:

1. **Must be a relative path.** Reject anything starting with `/`, `\`, a drive letter (`C:`), or a UNC prefix.
2. **Must not contain `..`** as any path segment. Reject symlink-escape attempts.
3. **Must end in `.md`** (case-insensitive).
4. **Must target an allowed folder.** The first path segment MUST be one of:
   - `00_INBOX`
   - `01_CLIENTS`
   - `02_TERMINOLOGY`
   - `03_DOMAINS`
   - `04_STYLE`
   - `05_INDICES`

   Agents MUST NOT write to `06_TEMPLATES/` – templates are human-authored.

5. **Path separators MAY be `/` or `\`.** Clients MUST normalise before comparison.

Blocks that fail validation MUST be skipped with a user-visible warning; the rest of the reply MUST still be processed.

### 10.1 Atomic writes

Writers MUST write files atomically (write to a sibling `.tmp` file, then rename/replace). This prevents half-written files if the client is killed mid-write. On Windows, `os.replace` / `File.Replace` guarantees atomic replacement.

### 10.2 Encoding on write

Writers MUST emit UTF-8 without a BOM. They SHOULD use `LF` line endings unless the existing file uses `CRLF`, in which case they SHOULD preserve it.

## 11. Archive convention for processed inbox files

After Process Inbox reads a file from `00_INBOX/` and writes one or more articles, it MUST:

1. Stamp the source file's frontmatter with:
   ```yaml
   compiled: true
   compiled_date: 2026-04-08
   compiled_to:
     - "01_CLIENTS/Acme Corporation.md"
     - "02_TERMINOLOGY/compliance → naleving.md"
   ```
2. Move the stamped file to `00_INBOX/_archive/`.
3. On filename collision in `_archive/`, append a timestamp suffix (`_20260408-171055`) rather than overwriting.

Readers listing inbox files MUST skip any file with `compiled: true` in its frontmatter, even if it still sits in `00_INBOX/` (e.g. because the client was killed before it could be moved). This makes the archive convention idempotent and crash-safe.

## 12. Health Check snapshots

When the Lint agent builds its whole-memory-bank snapshot, it MUST:

1. Walk the content folders (§1.2) in deterministic order: folder name ascending, then filename ascending.
2. Include each article's frontmatter + body, separated by a line-delimited header (`----- <relative path> -----`).
3. Cap the total snapshot at an implementation-defined character limit (Python standalone uses 120,000 chars ≈ 30k tokens).
4. If the cap is reached, truncate **by whole articles only** (never mid-body) and report `truncated: true` in the result.

This is the one place where clients MAY skip articles to fit a budget; §8 does not apply here because Lint operates on the whole memory bank, not a per-query context.

## 13. Template resolution

Agent prompts are loaded in this priority order:

1. **Memory bank override:** `<memory-bank>/06_TEMPLATES/<agent>.md` if present.
2. **Bundled default:** the client's built-in copy shipped with the tool.

This allows users to customise agent behaviour per-memory-bank without editing the client source. Clients MUST respect memory bank overrides silently (no warning) – this is a documented power-user feature.

### 13.1 Template file names

| Agent | Template filename |
|---|---|
| Process Inbox | `compile.md` |
| Health Check | `lint.md` |
| Query (chat) | `query.md` |
| Translate with memory bank | `translate_with_memory_bank.md` |
| Distill | `distill.md` |

## 14. Versioning

This document uses semantic versioning:

- **Major:** Breaking changes (e.g. renaming a required folder, changing scoring weights).
- **Minor:** Additive changes (new optional frontmatter keys, new agent template).
- **Patch:** Clarifications, typo fixes, no behavioural change.

Clients SHOULD embed the spec version they were built against in their about/diagnostics screen so that mismatches can be diagnosed.

## 15. Reference implementations

- [Supervertaler for Trados](https://github.com/Supervertaler/Supervertaler-for-Trados) – `src/Supervertaler.Trados/Core/MemoryBankReader.cs`
- [Supervertaler Assistant standalone](https://github.com/Supervertaler/supervertaler-assistant) – `supervertaler_assistant/memory_bank.py`

Both implementations are kept in lock-step with this spec; any discrepancy is a bug in the client, not in the spec.

## Appendix A – Minimal example memory bank

```
my-memory-bank/
├── 00_INBOX/
│   └── _archive/
├── 01_CLIENTS/
│   └── _EXAMPLE_Client_Profile.md
├── 02_TERMINOLOGY/
│   └── _EXAMPLE_Term_Article.md
├── 03_DOMAINS/
│   └── _EXAMPLE_Domain_Article.md
├── 04_STYLE/
│   └── _EXAMPLE_Style_Guide.md
├── 05_INDICES/
└── 06_TEMPLATES/
    ├── compile.md
    ├── lint.md
    ├── query.md
    ├── translate_with_memory_bank.md
    └── distill.md
```

A concrete instance of this layout is shipped under `skeleton/` in the [supervertaler-assistant repo](https://github.com/Supervertaler/supervertaler-assistant) and MAY be used as a starting point for new memory banks.

## Appendix B – Change log

- **1.1 (2026-04-08):** Renamed "vault" to "memory bank" throughout. Retired the "SuperMemory" product name; the spec is now the Supervertaler Memory Bank Format. Renamed `translate_with_kb.md` → `translate_with_memory_bank.md` in §13.1. Updated §15 reference implementation paths (`SuperMemoryReader.cs` → `MemoryBankReader.cs`, `supermemory/vault.py` → `supervertaler_assistant/memory_bank.py`). No wire-format changes – any conformant v1.0 client is also v1.1 conformant.
- **1.0 (2026-04-08):** Initial public version. Extracts the format contract from the original C# `SuperMemoryReader` and the new Python `supermemory.vault` module. Adds §4.3 code-fence tolerance after two production vault files were found to be silently unreadable because they had been pasted from an LLM reply wrapped in a fenced block.
