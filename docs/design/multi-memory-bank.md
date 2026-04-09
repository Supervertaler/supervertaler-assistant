# Design note: multi-memory-bank support

**Status:** Draft – awaiting implementation
**Last updated:** 2026-04-09

This note captures the agreed design for letting a user keep several memory
banks side by side and switch between them on the fly. It is the plan of record
for the follow-up commits in both the standalone Supervertaler Assistant and
Supervertaler for Trados. No code has been written for any of this yet.

## The shape of the change

Today, the Supervertaler Assistant reads **one** memory bank from a fixed path
(`~/Supervertaler/memory-bank/`). We want it to read **one of several** memory
banks that all live under a shared parent folder, and we want the user to be
able to swap between them at any time without restarting anything.

Concretely, the on-disk layout changes from:

```
~/Supervertaler/
└── memory-bank/              ← single bank, fixed name
    ├── 00_INBOX/
    ├── 01_CLIENTS/
    ...
```

to:

```
~/Supervertaler/
└── memory-banks/             ← plural, the "root" that the app points at
    ├── translation/          ← one full memory bank
    │   ├── 00_INBOX/
    │   ├── 01_CLIENTS/
    │   ├── 02_TERMINOLOGY/
    │   ├── 03_DOMAINS/
    │   ├── 04_STYLE/
    │   ├── 05_INDICES/
    │   └── 06_TEMPLATES/
    ├── general/              ← another full memory bank
    │   ├── 00_INBOX/
    │   ├── 01_CLIENTS/
    │   ├── 02_TERMINOLOGY/
    │   ├── 03_DOMAINS/
    │   ├── 04_STYLE/
    │   ├── 05_INDICES/
    │   └── 06_TEMPLATES/
    └── medical/              ← a third one, etc.
        └── ...
```

Each individual bank is a fully-conformant memory bank in the sense of
[`SPEC.md`](../../SPEC.md). **The wire format does not change.** Multi-bank
support is purely an organisational layer above the existing format – nothing
a single-bank-aware client would read or write differently.

## Design decisions

These were agreed in chat on 2026-04-09 and are not up for discussion in the
implementation commits – if any of them feel wrong during implementation,
bring it back to a new design note revision instead of drifting.

1. **One active bank at a time.** No stacking, no merging, no "translation +
   general combined". Switching banks is a pure path swap. The reader and the
   agents always see exactly one bank's content.
2. **Selection is per chat session, switchable on the fly via a dropdown.**
   Memory banks are coarse-grained – a typical user has 2 to 5, not 50, and
   they are not tied to individual translation projects. The dropdown lives in
   the Assistant's toolbar and in the Trados plugin's Memory Bank toolbar.
   Switching is immediate: the reader and the agents are rebuilt against the
   new path, the chat history stays, and the next user turn reads from the new
   bank.
3. **Existing users get prompted once to name their current bank.** On first
   run after this change lands, if the legacy `~/Supervertaler/memory-bank/`
   folder exists and `~/Supervertaler/memory-banks/` does not, the app shows a
   one-shot dialog asking for a short name and moves the old folder to
   `~/Supervertaler/memory-banks/<name>/`. Default suggestion: `main`.
4. **Bank folder names are short identifiers, not display labels.** Use
   kebab-case or single lowercase words: `translation`, `general`, `medical`,
   `eu-procurement`. No spaces, no parentheses, no emoji. Long display labels,
   if we ever want them, live in the bank's own `05_INDICES/Master Index.md`
   frontmatter and are read by the UI when populating the dropdown – never
   derived from the folder name.
5. **Each bank has its own `06_TEMPLATES/`**, for symmetry with the current
   spec and because banks may diverge substantially over time (a medical bank
   might want different Process Inbox instructions from a general one).

## What changes where

### Spec (`SPEC.md`)

**No wire-format change.** An individual memory bank at
`memory-banks/translation/` is exactly the same artefact an old single-bank
client would have read at `memory-bank/`. Any v1.1 conformant client is also a
multi-bank-aware client's view of a single bank – they are byte-identical.

We may add a short **Appendix C** describing the multi-bank convention as an
optional organisational layer, so third-party tools know they can look for
siblings under a `memory-banks/` parent. This is purely informational and does
not impose any requirement on a single-bank client.

### Python package (`supervertaler_assistant/`)

- **`memory_bank.py`** – `MemoryBankReader` is unchanged. It already takes a
  path to a single bank and reads that bank. Good.
  New free function:
  ```python
  def list_memory_banks(memory_banks_root: str | Path) -> list[MemoryBankInfo]
  ```
  where `MemoryBankInfo` is a small dataclass `(name: str, path: Path,
  display_label: str | None, article_count: int)`. Used by the UI to populate
  the dropdown. A folder counts as a bank if it contains at least one of the
  canonical content folders (`01_CLIENTS`, `02_TERMINOLOGY`, `03_DOMAINS`,
  `04_STYLE`).

- **`settings.py`** – schema changes:
  ```python
  @dataclass
  class AppSettings:
      memory_banks_root: str = ""       # NEW – parent containing all banks
      last_active_bank: str = ""        # NEW – short name of the most recent pick
      # memory_bank_dir: REMOVED (but read on load for migration, see below)
      ...
  ```
  On load, if the persisted settings JSON still has the old `memory_bank_dir`
  key, map it to the new scheme: `memory_banks_root` becomes the parent of the
  old path, and `last_active_bank` becomes the old path's basename. This is a
  one-hop migration that runs automatically, zero user action.

- **`agents/query.py`** – `ChatSession.memory_bank_dir: Path` stays. The UI is
  responsible for swapping the session's `memory_bank_dir` and calling
  `_rebuild_agents()` when the user picks a new bank from the dropdown. The
  chat history array is not cleared on switch; the next turn just reads from
  the new path.

- **`ui/main_window.py`** – the current `lbl_memory_bank_path` label and
  `Choose memory bank…` button are replaced by:
  - A `QComboBox` labelled "Memory bank:" populated via
    `list_memory_banks(memory_banks_root)`, sorted alphabetically by display
    label (or by folder name if no label). The currently selected item is
    bolded. Bottom entry of the list is a separator plus "Manage banks…" which
    opens the settings dialog at the memory banks page.
  - An `Open folder` button that reveals the active bank in Explorer / Finder.
  - An `Open in Obsidian` button that opens an `obsidian://` URL for the
    active bank (nice-to-have, can come in a follow-up).

  When the combo selection changes, call `_load_memory_bank(path)` and
  `_rebuild_agents()` against the new path. Persist `last_active_bank` to
  settings.json on every change so the next launch reopens the same bank.

- **`ui/settings_dialog.py`** – the "Memory bank folder" field becomes
  "Memory banks root folder". Underneath it, show a read-only list of banks
  currently detected under that root (name, display label if any, article
  count), with buttons "Create new bank…", "Rename…", "Delete…". The delete
  button confirms twice and never touches files outside the bank folder.

- **One-time migration on first run after the upgrade.** In
  `main_window.py`'s startup path, before any reader is built:
  1. If `settings.memory_banks_root` is set and exists, use that. Done.
  2. Else, if `~/Supervertaler/memory-banks/` exists, use it as the root
     (covers users who created the new layout manually). Done.
  3. Else, if the legacy `~/Supervertaler/memory-bank/` folder exists, show a
     modal: *"Found an existing memory bank at `~/Supervertaler/memory-bank/`.
     Give it a short name so it can join the new multi-bank layout. Default:
     `main`."* On OK, move the folder to
     `~/Supervertaler/memory-banks/<name>/` via `Path.rename`, set
     `memory_banks_root = ~/Supervertaler/memory-banks/` and
     `last_active_bank = <name>`, save settings, carry on.
  4. Else, first-time user: create `~/Supervertaler/memory-banks/` empty and
     offer to copy the bundled `skeleton/` into it as `memory-banks/main/`.

### Trados plugin (`Supervertaler-for-Trados`)

- **`Settings/UserDataPath.cs`** – rename / replace:
  - `MemoryBankDir` → `MemoryBanksRoot` (plural, returns
    `<Root>/memory-banks/`).
  - New method `ActiveMemoryBankDir(string bankName)` returning
    `Path.Combine(MemoryBanksRoot, bankName)`.
  - Keep the legacy `supermemory/` and `memory-bank/` fallback behaviour, but
    the fallback now resolves to a one-hop migration into
    `memory-banks/main/` on first run (same migration as the Python side).
  - The old `[Obsolete] SuperMemoryDir` alias is retired in the same commit
    that introduces the new properties – by then nothing calls it.

- **`Controls/SuperMemoryToolbar.cs`** (file name still pending a later
  rename) – swap the static `_lblHeading = "Memory Bank"` label for a
  `ComboBox` populated from `MemoryBanksRoot`. Selection is mirrored to
  `settings.json` under a new key `active_memory_bank`. On selection change,
  the `AiAssistantViewPart` is notified and its cached `_kbReader` is rebuilt
  against the new path.

- **`AiAssistantViewPart.cs`** – change all call sites from
  `UserDataPath.MemoryBankDir` to a new helper
  `UserDataPath.ActiveMemoryBankDir(settings.ActiveMemoryBank)`. The active
  bank name is read from settings at reader-construction time and cached with
  the reader; on combo change the reader is thrown away and rebuilt.

- **`Settings/AiSettings.cs`** (or wherever the persisted settings dataclass
  lives) – new field `ActiveMemoryBank: string`.

- Migration – exact mirror of the Python migration: on first run, if
  `<Root>/memory-bank/` (or the even older `<Root>/supermemory/`) exists and
  `<Root>/memory-banks/` does not, show a first-run dialog asking for a short
  name, move the folder, persist the choice. The same dialog class can live
  in `Supervertaler.Trados.Controls.SetupDialog` or a dedicated new class.

### README (`README.md`)

Small updates once the code lands:

- Folder-layout example changes to the `memory-banks/` parent form.
- The "first launch" section mentions the memory-banks root folder in
  settings and the on-the-fly dropdown in the toolbar.
- "Auto-loads a memory bank at `~/Supervertaler/memory-bank/`" becomes
  "auto-loads the last-used bank from `~/Supervertaler/memory-banks/`, or
  prompts once to name an existing `memory-bank/` folder and move it there".

## Implementation order

The order is chosen to keep every intermediate commit shippable and
verifiable by hand. The plan is:

1. **Python: reader helper + settings schema.** Add `list_memory_banks()` in
   `memory_bank.py`, add the new settings fields, add the load-time migration
   from `memory_bank_dir` to `memory_banks_root + last_active_bank`. No UI
   change yet. Unit-testable in isolation.
2. **Python: UI swap.** Replace the header label / button with the combo box,
   wire up the on-change path swap, add the first-run "name your existing
   bank" dialog. End state: the standalone Assistant is fully multi-bank.
3. **Python: settings dialog refresh.** Root picker, bank list, create /
   rename / delete buttons. Nice-to-have can ship later; bare minimum is the
   root picker.
4. **Trados: `UserDataPath` + settings + migration.** Mirror step 1 but in
   C#. `MemoryBanksRoot`, `ActiveMemoryBankDir(name)`, new
   `ActiveMemoryBank` settings field, first-run migration dialog. Reader and
   caller sites updated to go through the new helper.
5. **Trados: Memory Bank toolbar dropdown.** Combo in
   `SuperMemoryToolbar.cs`, live switching, rebuild the cached reader. End
   state: the Trados plugin is fully multi-bank.
6. **Spec + README refresh.** Add the optional Appendix C to `SPEC.md`,
   update README folder layout and first-launch instructions. Bump
   `SPEC.md` to 1.2 (still backward-compatible, still no wire-format
   change).
7. **(Optional follow-ups)** Open-in-Obsidian button, per-bank display
   labels read from `05_INDICES/Master Index.md` frontmatter, bank-level
   settings overrides.

## What's explicitly out of scope

- **Cross-bank queries.** The Assistant will never look at more than one
  bank per turn. If you want results from translation and general, you
  switch the dropdown, ask again, switch back.
- **Per-Trados-project bank binding.** Banks are coarse-grained user-level
  things, not Trados project settings. A translator works out of one or two
  banks across many Trados projects.
- **Changes to the memory-bank wire format.** None. An individual bank is
  byte-for-byte what it was before.
- **A separate "memory bank manager" app.** The settings dialog and the
  toolbar combo are enough. No standalone management tool.
