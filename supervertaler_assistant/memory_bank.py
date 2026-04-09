"""
supervertaler_assistant.memory_bank
===================================

Reads and queries a Supervertaler memory bank – a structured Obsidian-compatible
folder that the Supervertaler Assistant consults when helping with a translation.

A memory bank is the data structure the Assistant relies on to remember what it
knows about your clients, terminology, domains and style. This module scans
frontmatter for lightweight indexing, then loads full articles on demand, and
produces a `MemoryBankContext` for a given client / domain / language pair that
can be formatted straight into an LLM prompt.

Design goals
------------
- Format-compatible with the Trados plugin's `MemoryBankReader.cs` – same
  folder names, same frontmatter keys, same scoring rules – so a memory bank
  created in one host is readable by any other.
- Cross-platform (Windows, macOS, Linux) – no Win32 dependencies.
- Hardened parser: tolerates files wrapped in a ```markdown code fence
  (a common artefact of pasting LLM replies into Obsidian).
- No LLM dependency in this module – pure I/O + parsing. The agents
  (compile / lint / query / translate / distill) live in
  `supervertaler_assistant.agents.*` and consume the objects defined here.

Dependencies
------------
    pip install python-frontmatter

Author: Supervertaler project (MIT)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import frontmatter  # python-frontmatter – handles multi-line YAML properly


# ─── Constants ──────────────────────────────────────────────────────────────

CONTENT_FOLDERS: tuple[str, ...] = (
    "01_CLIENTS",
    "02_TERMINOLOGY",
    "03_DOMAINS",
    "04_STYLE",
)
"""Folders scanned for memory-bank articles. 00_INBOX, 05_INDICES, 06_TEMPLATES
are skipped – they are workflow folders, not knowledge."""

_EXAMPLE_PREFIX = "_example_"
_ARCHIVE_SEGMENT = "_archive"
_INDEX_TTL_SECONDS = 30  # re-scan at most every 30s unless forced

# Crude tokens-per-char estimate. Matches the C# reader for consistency.
_CHARS_PER_TOKEN = 4


# ─── Data classes ───────────────────────────────────────────────────────────


@dataclass
class ArticleIndex:
    """Lightweight index entry for a memory-bank article (frontmatter only)."""

    file_path: Path
    relative_path: str
    folder: str            # "01_CLIENTS" | "02_TERMINOLOGY" | ...
    file_name: str
    frontmatter: dict = field(default_factory=dict)
    file_size_bytes: int = 0

    def fm(self, key: str, default: str = "") -> str:
        """Return a frontmatter value as a string (joined if list)."""
        val = self.frontmatter.get(key)
        if val is None:
            return default
        if isinstance(val, list):
            # Strip [[wikilink]] decoration for substring matching
            return ", ".join(_strip_wikilinks(str(v)) for v in val)
        return _strip_wikilinks(str(val))


@dataclass
class MemoryBankContext:
    """Resolved memory-bank context for a translation – the articles loaded and ready."""

    # Client
    client_name: str | None = None
    client_profile_text: str | None = None
    client_profile_path: str | None = None

    # Domain
    domain_name: str | None = None
    domain_article_text: str | None = None
    domain_article_path: str | None = None

    # Style guide
    style_guide_text: str | None = None
    style_guide_path: str | None = None

    # Terminology (parallel lists – same ordering)
    terminology_articles: list[str] = field(default_factory=list)
    terminology_paths: list[str] = field(default_factory=list)

    # How the client was detected: "manual" | "project-name" | "none"
    detection_method: str = "none"

    # ─── Derived ────────────────────────────────────────────────────────

    @property
    def has_content(self) -> bool:
        return bool(
            (self.client_profile_text and self.client_profile_text.strip())
            or (self.domain_article_text and self.domain_article_text.strip())
            or (self.style_guide_text and self.style_guide_text.strip())
            or self.terminology_articles
        )

    @property
    def estimated_tokens(self) -> int:
        chars = 0
        for t in (
            self.client_profile_text,
            self.domain_article_text,
            self.style_guide_text,
        ):
            if t:
                chars += len(t)
        chars += sum(len(a) for a in self.terminology_articles)
        return chars // _CHARS_PER_TOKEN

    def trim_to_token_budget(self, max_tokens: int) -> None:
        """Drop lowest-priority content first until we fit the budget.

        Priority (kept in order): client > domain > style > terminology.
        """
        if max_tokens <= 0 or self.estimated_tokens <= max_tokens:
            return

        # 1. Trim terminology articles from the end
        while self.terminology_articles and self.estimated_tokens > max_tokens:
            self.terminology_articles.pop()
            self.terminology_paths.pop()

        # 2. Drop the style guide
        if self.estimated_tokens > max_tokens:
            self.style_guide_text = None
            self.style_guide_path = None

        # 3. Drop the domain article
        if self.estimated_tokens > max_tokens:
            self.domain_article_text = None
            self.domain_article_path = None

        # 4. Last resort: truncate the client profile
        if self.estimated_tokens > max_tokens and self.client_profile_text:
            max_chars = max_tokens * _CHARS_PER_TOKEN
            if len(self.client_profile_text) > max_chars:
                self.client_profile_text = (
                    self.client_profile_text[:max_chars] + "\n[... truncated ...]"
                )

    def summary(self) -> str | None:
        """Short human-readable summary for UI display."""
        parts: list[str] = []
        if self.client_profile_text:
            parts.append(f"client: {self.client_name or 'detected'}")
        if self.domain_article_text:
            parts.append(f"domain: {self.domain_name or 'detected'}")
        if self.style_guide_text:
            parts.append("style guide")
        if self.terminology_articles:
            n = len(self.terminology_articles)
            parts.append(f"{n} term article{'s' if n != 1 else ''}")
        if not parts:
            return None
        return f"Memory bank: {', '.join(parts)} (~{self.estimated_tokens} tokens)"


# ─── Reader ─────────────────────────────────────────────────────────────────


class MemoryBankReader:
    """Reads and queries a Supervertaler memory bank.

    Thread-safety: not thread-safe. Create one instance per thread or guard
    externally.
    """

    def __init__(self, memory_bank_dir: str | Path) -> None:
        self.memory_bank_dir = Path(memory_bank_dir).expanduser().resolve()
        self._index: list[ArticleIndex] | None = None
        self._index_built_at: datetime | None = None

    # ── Public API ──────────────────────────────────────────────────────

    @property
    def memory_bank_exists(self) -> bool:
        """True if the memory bank has at least one expected content folder."""
        return self.memory_bank_dir.is_dir() and any(
            (self.memory_bank_dir / f).is_dir() for f in CONTENT_FOLDERS
        )

    def refresh_index(self, force: bool = False) -> None:
        """Rebuild the lightweight frontmatter index.

        Caches for `_INDEX_TTL_SECONDS` unless `force=True`.
        """
        if not force and self._index is not None and self._index_built_at is not None:
            age = (datetime.now(timezone.utc) - self._index_built_at).total_seconds()
            if age < _INDEX_TTL_SECONDS:
                return

        entries: list[ArticleIndex] = []

        for folder in CONTENT_FOLDERS:
            folder_dir = self.memory_bank_dir / folder
            if not folder_dir.is_dir():
                continue

            for md_file in folder_dir.rglob("*.md"):
                name = md_file.name
                if name.lower().startswith(_EXAMPLE_PREFIX):
                    continue
                # Skip anything under an _archive subfolder
                rel = md_file.relative_to(self.memory_bank_dir)
                if any(part.lower() == _ARCHIVE_SEGMENT for part in rel.parts):
                    continue

                try:
                    entry = ArticleIndex(
                        file_path=md_file,
                        relative_path=str(rel).replace("\\", "/"),
                        folder=folder,
                        file_name=name,
                        file_size_bytes=md_file.stat().st_size,
                    )
                    entry.frontmatter = _read_frontmatter(md_file)
                    entries.append(entry)
                except OSError:
                    continue  # unreadable file – skip silently

        self._index = entries
        self._index_built_at = datetime.now(timezone.utc)

    def load_context(
        self,
        *,
        project_name: str | None = None,
        domain: str | None = None,
        source_lang: str | None = None,
        target_lang: str | None = None,
        token_budget: int = 4000,
        manual_client_profile: str | None = None,
    ) -> MemoryBankContext | None:
        """Load relevant memory-bank context for a translation task.

        Returns None if the memory bank doesn't exist or has no relevant content.
        """
        if not self.memory_bank_exists:
            return None
        self.refresh_index()
        if not self._index:
            return None

        ctx = MemoryBankContext()

        # ── Step 1: Resolve client profile ──────────────────────────────
        client_entry: ArticleIndex | None = None

        if manual_client_profile:
            # Manual override – exact filename match
            for e in self._index:
                if (
                    e.folder == "01_CLIENTS"
                    and e.file_name.lower() == manual_client_profile.lower()
                ):
                    client_entry = e
                    ctx.detection_method = "manual"
                    break

        if client_entry is None and project_name:
            client_entry = self._detect_client(project_name)
            ctx.detection_method = "project-name" if client_entry else "none"

        if client_entry is not None:
            ctx.client_name = client_entry.fm("client") or client_entry.file_path.stem
            ctx.client_profile_text = _read_full_article(client_entry.file_path)
            ctx.client_profile_path = client_entry.relative_path

        # ── Step 2: Resolve domain article ──────────────────────────────
        if domain:
            domain_entry = next(
                (
                    e
                    for e in self._index
                    if e.folder == "03_DOMAINS" and _matches_domain(e, domain)
                ),
                None,
            )
            if domain_entry is not None:
                ctx.domain_article_text = _read_full_article(domain_entry.file_path)
                ctx.domain_article_path = domain_entry.relative_path
                ctx.domain_name = (
                    domain_entry.fm("domain") or domain_entry.file_path.stem
                )

        # ── Step 3: Resolve style guide ─────────────────────────────────
        style_entry = self._find_style_guide(
            source_lang=source_lang,
            target_lang=target_lang,
            client_name=ctx.client_name,
        )
        if style_entry is not None:
            ctx.style_guide_text = _read_full_article(style_entry.file_path)
            ctx.style_guide_path = style_entry.relative_path

        # ── Step 4: Resolve terminology ─────────────────────────────────
        for term in self._find_terminology_articles(
            client_name=ctx.client_name,
            domain=domain,
            source_lang=source_lang,
            target_lang=target_lang,
        ):
            text = _read_full_article(term.file_path)
            if text and text.strip():
                ctx.terminology_articles.append(text)
                ctx.terminology_paths.append(term.relative_path)

        # ── Step 5: Apply token budget ──────────────────────────────────
        ctx.trim_to_token_budget(token_budget)

        return ctx if ctx.has_content else None

    @staticmethod
    def format_for_prompt(ctx: MemoryBankContext | None) -> str | None:
        """Format a memory-bank context as a prompt-ready Markdown section.

        Mirrors `MemoryBankReader.FormatForPrompt` in the C# plugin so the
        same prompts work across both hosts.
        """
        if ctx is None or not ctx.has_content:
            return None

        lines: list[str] = [
            "# MEMORY BANK",
            "",
            "The following context comes from the Supervertaler Assistant's memory bank.",
            "Use this information to inform your translations and terminology choices.",
            "Memory-bank decisions take priority over general assumptions.",
        ]

        if ctx.client_profile_text and ctx.client_profile_text.strip():
            heading = "## Client Profile"
            if ctx.client_name:
                heading += f": {ctx.client_name}"
            lines += ["", heading, "", ctx.client_profile_text.strip()]

        if ctx.domain_article_text and ctx.domain_article_text.strip():
            heading = "## Domain Knowledge"
            if ctx.domain_name:
                heading += f": {ctx.domain_name}"
            lines += ["", heading, "", ctx.domain_article_text.strip()]

        if ctx.style_guide_text and ctx.style_guide_text.strip():
            lines += ["", "## Style Guide", "", ctx.style_guide_text.strip()]

        if ctx.terminology_articles:
            lines += [
                "",
                "## Terminology Decisions",
                "",
                (
                    "These terms have been specifically chosen with reasoning. "
                    "Follow them exactly – rejected alternatives are listed so "
                    "you know what to avoid."
                ),
                "",
            ]
            for article in ctx.terminology_articles:
                lines.append(article.strip())
                lines.append("")

        return "\n".join(lines).rstrip()

    # ── Private helpers ─────────────────────────────────────────────────

    def _detect_client(self, project_name: str) -> ArticleIndex | None:
        """Best-effort client auto-detection from a Trados / Workbench
        project name. Matches `MemoryBankReader.DetectClient` semantics."""
        if not project_name or self._index is None:
            return None

        clients = [e for e in self._index if e.folder == "01_CLIENTS"]
        if not clients:
            return None

        needle = project_name.lower()

        # 1. Exact match on `client:` frontmatter field
        for c in clients:
            client_name = c.fm("client")
            if client_name and client_name.lower() in needle:
                return c

        # 2. Match on filename (without extension), minimum 3 chars to avoid
        #    accidental false positives on two-letter stems.
        for c in clients:
            stem = c.file_path.stem
            if len(stem) >= 3 and stem.lower() in needle:
                return c

        return None

    def _find_style_guide(
        self,
        *,
        source_lang: str | None,
        target_lang: str | None,
        client_name: str | None,
    ) -> ArticleIndex | None:
        if self._index is None:
            return None

        styles = [e for e in self._index if e.folder == "04_STYLE"]
        if not styles:
            return None

        # 1. Client-specific style guide (filename contains client name)
        if client_name:
            for s in styles:
                if client_name.lower() in s.file_name.lower():
                    return s

        # 2. Match by language pair
        if source_lang and target_lang:
            src = _extract_lang_code(source_lang)
            tgt = _extract_lang_code(target_lang)
            for s in styles:
                name = s.file_name.upper()
                fm_langs = s.fm("languages").upper()
                if (src in name and tgt in name) or (src in fm_langs and tgt in fm_langs):
                    return s

        # 3. Fallback: the first "General" style guide
        for s in styles:
            if "general" in s.file_name.lower():
                return s

        return None

    def _find_terminology_articles(
        self,
        *,
        client_name: str | None,
        domain: str | None,
        source_lang: str | None,
        target_lang: str | None,
    ) -> list[ArticleIndex]:
        if self._index is None:
            return []

        terms = [e for e in self._index if e.folder == "02_TERMINOLOGY"]
        if not terms:
            return []

        scored: list[tuple[ArticleIndex, int]] = []
        src = _extract_lang_code(source_lang) if source_lang else ""

        for t in terms:
            score = 0

            # Client match: +3
            if client_name:
                clients_field = t.fm("clients") or t.fm("client")
                if clients_field and client_name.lower() in clients_field.lower():
                    score += 3

            # Domain match: +2
            if domain:
                entry_domain = t.fm("domain")
                if entry_domain and (
                    domain.lower() in entry_domain.lower()
                    or entry_domain.lower() in domain.lower()
                ):
                    score += 2

            # Language match: +1
            if src:
                langs = t.fm("languages") or t.fm("source_language")
                if langs and src in langs.upper():
                    score += 1

            if score > 0:
                scored.append((t, score))

        # If nothing scored and the memory bank is small, return all term
        # articles – they're still useful general terminology.
        if not scored and len(terms) <= 20:
            return terms

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [entry for entry, _ in scored]


# ─── Module-private utilities ───────────────────────────────────────────────


_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _strip_wikilinks(value: str) -> str:
    """`[[Acme Corp]]` → `Acme Corp`."""
    return _WIKILINK_RE.sub(r"\1", value)


_LANG_ALIASES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("dutch", "nederland", "nl"), "NL"),
    (("french", "fran", "fr"), "FR"),
    (("german", "deutsch", "de"), "DE"),
    (("english", "en"), "EN"),
    (("spanish", "espa", "es"), "ES"),
    (("italian", "it"), "IT"),
    (("portuguese", "pt"), "PT"),
)


def _extract_lang_code(lang_display: str | None) -> str:
    """Normalise a display name or BCP-47 tag to a two-letter code.

    Matches `MemoryBankReader.ExtractLangCode` in the C# reader so detection
    results are identical across both hosts.
    """
    if not lang_display:
        return ""
    upper = lang_display.upper()
    for needles, code in _LANG_ALIASES:
        if any(n.upper() in upper for n in needles):
            return code
    return upper[:2] if len(upper) >= 2 else upper


def _matches_domain(entry: ArticleIndex, domain: str) -> bool:
    entry_domain = entry.fm("domain")
    if entry_domain:
        ed = entry_domain.lower()
        d = domain.lower()
        return d in ed or ed in d
    # Fallback: match filename
    return domain.lower() in entry.file_path.stem.lower()


def _read_frontmatter(md_file: Path) -> dict:
    """Parse a file's YAML frontmatter into a dict.

    Hardened against files that were pasted from an LLM reply and ended up
    wrapped in a ```markdown code fence – a common Obsidian authoring mistake
    that silently breaks the C# reader too. We strip a leading fence line
    before handing the text to python-frontmatter.
    """
    try:
        raw = md_file.read_text(encoding="utf-8")
    except OSError:
        return {}

    stripped = raw.lstrip()
    if stripped.startswith("```"):
        nl = stripped.find("\n")
        if nl < 0:
            return {}
        raw = stripped[nl + 1:]  # drop the opening fence line

    try:
        post = frontmatter.loads(raw)
        return dict(post.metadata) if post.metadata else {}
    except Exception:
        return {}


def _read_full_article(md_file: Path) -> str | None:
    """Read an article's full text, stripping a leading code fence wrapper
    so downstream prompt formatting is clean."""
    try:
        text = md_file.read_text(encoding="utf-8")
    except OSError:
        return None

    stripped = text.lstrip()
    if stripped.startswith("```"):
        nl = stripped.find("\n")
        if nl >= 0:
            text = stripped[nl + 1:]
            # Also drop a trailing closing fence if present
            text = re.sub(r"\n```\s*$", "", text)

    return text


# ─── Convenience for ad-hoc use ─────────────────────────────────────────────


def iter_articles(memory_bank_dir: str | Path) -> Iterable[ArticleIndex]:
    """Yield every indexed article in the memory bank (examples/archives skipped).

    Handy for the Health Check agent, which wants the full list without
    going through `load_context`.
    """
    reader = MemoryBankReader(memory_bank_dir)
    reader.refresh_index(force=True)
    return list(reader._index or [])


# ─── Multi-bank support ────────────────────────────────────────────────────


@dataclass
class MemoryBankInfo:
    """Lightweight descriptor of one memory bank sitting under a shared root.

    Used by the UI to populate the memory-bank dropdown without having to
    instantiate a full :class:`MemoryBankReader` for every bank.
    """

    name: str
    """Short folder identifier, e.g. ``"translation"`` or ``"general"``."""

    path: Path
    """Absolute path to the bank's root folder."""

    display_label: str | None = None
    """Optional pretty label for the UI. ``None`` means "use ``name``".

    Populated from the bank's ``05_INDICES/Master Index.md`` frontmatter in
    a later step; for now always ``None``.
    """

    article_count: int = 0
    """Number of knowledge articles across ``CONTENT_FOLDERS``.

    Counts ``.md`` files that are not ``_EXAMPLE_*`` and not inside an
    ``_archive/`` subtree. Frontmatter is not parsed – this is a fast file
    walk.
    """


def _count_articles(bank_dir: Path) -> int:
    """Cheap count of real articles in a bank (no frontmatter parsing)."""
    count = 0
    for folder in CONTENT_FOLDERS:
        d = bank_dir / folder
        if not d.is_dir():
            continue
        for md in d.rglob("*.md"):
            name_lower = md.name.lower()
            if name_lower.startswith(_EXAMPLE_PREFIX):
                continue
            if _ARCHIVE_SEGMENT in (p.lower() for p in md.parts):
                continue
            count += 1
    return count


def _looks_like_memory_bank(path: Path) -> bool:
    """True if ``path`` has at least one of the canonical content folders.

    This is the same "exists" rule :class:`MemoryBankReader` uses, so the
    dropdown only shows folders the reader would actually be able to open.
    """
    if not path.is_dir():
        return False
    return any((path / cf).is_dir() for cf in CONTENT_FOLDERS)


def list_memory_banks(memory_banks_root: str | Path) -> list[MemoryBankInfo]:
    """Return every memory bank directly under ``memory_banks_root``.

    Scans one level deep and skips:
    - non-directories
    - dot-folders (``.obsidian``, ``.git``, …)
    - folders that don't contain at least one ``CONTENT_FOLDERS`` entry

    Results are sorted alphabetically by folder name. If ``memory_banks_root``
    is empty, does not exist, or is not a directory, returns an empty list
    (never raises) – the UI treats that as "no banks yet".
    """
    # Guard against empty/None input explicitly: ``Path("")`` silently
    # resolves to the current working directory on every platform, which
    # would make an unconfigured install start scanning wherever the app
    # happened to be launched from.
    if not memory_banks_root:
        return []

    try:
        root = Path(memory_banks_root)
    except (TypeError, ValueError):
        return []

    if not root.is_dir():
        return []

    banks: list[MemoryBankInfo] = []
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []

    for child in children:
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        if not _looks_like_memory_bank(child):
            continue
        banks.append(
            MemoryBankInfo(
                name=child.name,
                path=child.resolve(),
                display_label=None,
                article_count=_count_articles(child),
            )
        )

    return banks


__all__ = [
    "CONTENT_FOLDERS",
    "ArticleIndex",
    "MemoryBankContext",
    "MemoryBankInfo",
    "MemoryBankReader",
    "iter_articles",
    "list_memory_banks",
]
