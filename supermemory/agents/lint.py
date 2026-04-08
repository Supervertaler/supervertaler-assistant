"""
supermemory.agents.lint
=======================

Health Check agent.

Scans the vault, sends a snapshot to the LLM with the ``lint.md``
prompt, parses both the health check report and any ``### FILE:``
blocks the model wants to write back, and applies the writes.

The ``lint.md`` template specifies that the reply has two parts:

    # Part 1: Health Check Report
    ...

    # Part 2: Updated files
    ### FILE: ...
    ### FILE: ...

We split the two so the UI can:
1. Display the report as a formatted chat message
2. Write the files using the shared :mod:`supermemory.agents._output`
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..llm import LlmClient, system, user
from ..vault import CONTENT_FOLDERS, iter_articles
from ._output import (
    WriteResult,
    parse_file_blocks,
    summarise_writes,
    write_file_blocks,
)

_LOG = logging.getLogger(__name__)


# Cap on how many chars of vault content we hand to the LLM in one go.
# At ~4 chars/token this is ~30k tokens — well within the budget of
# current frontier models and leaves room for the prompt and reply.
_MAX_SNAPSHOT_CHARS = 120_000


# ─── Result ─────────────────────────────────────────────────────────────────


@dataclass
class LintResult:
    """Outcome of one Health Check run."""

    articles_scanned: int = 0
    report_markdown: str = ""
    writes: list[WriteResult] = field(default_factory=list)
    llm_raw_reply: str = ""
    truncated: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def summary(self) -> str:
        if self.error:
            return f"Health Check failed: {self.error}"
        if self.articles_scanned == 0:
            return "Vault has no content to check."

        bits = [
            f"Scanned {self.articles_scanned} article"
            f"{'s' if self.articles_scanned != 1 else ''}."
        ]
        if self.writes:
            bits.append(summarise_writes(self.writes))
        if self.truncated:
            bits.append(
                "⚠ Vault exceeds single-pass snapshot limit — results "
                "may be incomplete. Consider running compile more often "
                "to keep individual articles focused."
            )
        return "\n".join(bits)


# ─── Agent ──────────────────────────────────────────────────────────────────


class LintAgent:
    """Health Check — scans the KB for problems and applies auto-fixes."""

    def __init__(self, llm: LlmClient, vault_dir: str | Path) -> None:
        self.llm = llm
        self.vault_dir = Path(vault_dir).resolve()
        self._prompt = _load_lint_prompt(self.vault_dir)

    # ── Public API ──────────────────────────────────────────────────────

    def run(self, *, dry_run: bool = False) -> LintResult:
        """Run one full health check pass over the whole vault."""
        result = LintResult()

        try:
            snapshot, count, truncated = _build_snapshot(self.vault_dir)
            result.articles_scanned = count
            result.truncated = truncated

            if count == 0:
                return result

            messages = [
                system(self._prompt),
                user(
                    "Here is a snapshot of the current SuperMemory vault. "
                    "Perform the health check as specified in your system "
                    "prompt. Return Part 1 (the report in Markdown) and "
                    "Part 2 (`### FILE:` blocks for any files you auto-fixed "
                    "or created).\n\n"
                    + snapshot
                ),
            ]
            reply = self.llm.complete(messages)
            result.llm_raw_reply = reply

            result.report_markdown = _extract_report(reply)
            blocks = parse_file_blocks(reply)
            if blocks:
                result.writes = write_file_blocks(
                    self.vault_dir, blocks, dry_run=dry_run
                )

        except Exception as exc:  # noqa: BLE001 — agent boundary
            _LOG.exception("Health Check failed")
            result.error = f"{type(exc).__name__}: {exc}"

        return result


# ─── Snapshot building ──────────────────────────────────────────────────────


def _build_snapshot(vault_dir: Path) -> tuple[str, int, bool]:
    """Concatenate all KB articles into one labelled Markdown blob.

    Returns (snapshot, article_count, truncated). ``truncated`` is True
    if we hit the snapshot size cap before including every article —
    callers should surface that to the user.
    """
    articles = list(iter_articles(vault_dir))
    if not articles:
        return "", 0, False

    # Sort for deterministic output: by folder then filename
    articles.sort(key=lambda a: (a.folder, a.file_name.lower()))

    pieces: list[str] = []
    total_chars = 0
    included = 0
    truncated = False

    for entry in articles:
        try:
            text = entry.file_path.read_text(encoding="utf-8")
        except OSError:
            continue

        header = f"### VAULT FILE: {entry.relative_path}\n\n"
        chunk = header + text.strip() + "\n"

        if total_chars + len(chunk) > _MAX_SNAPSHOT_CHARS:
            truncated = True
            break

        pieces.append(chunk)
        total_chars += len(chunk)
        included += 1

    return "\n---\n\n".join(pieces), included, truncated


# ─── Prompt loading ─────────────────────────────────────────────────────────


def _load_lint_prompt(vault_dir: Path) -> str:
    """Load lint.md, preferring the vault's copy over the bundled fallback."""
    user_template = vault_dir / "06_TEMPLATES" / "lint.md"
    if user_template.is_file():
        try:
            return user_template.read_text(encoding="utf-8")
        except OSError:
            pass

    bundled = Path(__file__).resolve().parent.parent / "templates" / "lint.md"
    if bundled.is_file():
        return bundled.read_text(encoding="utf-8")

    return (
        "You are the SuperMemory maintenance librarian. Scan the provided "
        "vault snapshot and report inconsistencies, broken links, stale "
        "content, and duplicates. Apply auto-fixes via `### FILE:` blocks."
    )


# ─── Report extraction ─────────────────────────────────────────────────────


def _extract_report(reply: str) -> str:
    """Return just the Markdown report portion of the LLM reply.

    The lint template asks for two parts separated by the ``### FILE:``
    markers. Everything before the first ``### FILE:`` is the report.
    """
    if not reply:
        return ""

    lines = reply.splitlines()
    report_lines: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(("### FILE:", "## FILE:", "#### FILE:")):
            break
        report_lines.append(line)

    return "\n".join(report_lines).strip()


__all__ = ["LintAgent", "LintResult"]
