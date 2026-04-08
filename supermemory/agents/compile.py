"""
supermemory.agents.compile
==========================

Process Inbox agent.

Reads every unprocessed `.md` file from ``00_INBOX/`` (excluding files
already marked ``compiled: true`` and anything under ``_archive/``),
hands them to the LLM with the ``compile.md`` prompt, parses the
``### FILE:`` blocks from the reply, writes them into the vault, and
archives the source files.

Result reporting is structured (:class:`CompileResult`) so the UI can
render a proper progress/summary view rather than dumping the raw LLM
reply into the chat.

Running this does NOT consult the existing KB — Process Inbox produces
raw articles, it doesn't translate. Conflict detection is the lint
agent's job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import frontmatter

from ..llm import LlmClient, system, user
from ._output import (
    FileBlock,
    WriteResult,
    parse_file_blocks,
    summarise_writes,
    write_file_blocks,
)

_LOG = logging.getLogger(__name__)


# ─── Result ─────────────────────────────────────────────────────────────────


@dataclass
class CompileResult:
    """Outcome of one Process Inbox run."""

    inbox_files_read: list[Path] = field(default_factory=list)
    inbox_files_archived: list[Path] = field(default_factory=list)
    writes: list[WriteResult] = field(default_factory=list)
    llm_raw_reply: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def files_created(self) -> int:
        return sum(1 for w in self.writes if w.ok and w.was_created)

    @property
    def files_updated(self) -> int:
        return sum(1 for w in self.writes if w.ok and w.was_updated)

    def summary(self) -> str:
        if self.error:
            return f"Process Inbox failed: {self.error}"
        if not self.inbox_files_read:
            return "Inbox is empty — nothing to process."

        parts = [
            f"Processed {len(self.inbox_files_read)} inbox file"
            f"{'s' if len(self.inbox_files_read) != 1 else ''}:",
            summarise_writes(self.writes),
        ]
        if self.inbox_files_archived:
            parts.append(
                f"Archived {len(self.inbox_files_archived)} source file"
                f"{'s' if len(self.inbox_files_archived) != 1 else ''} to 00_INBOX/_archive/."
            )
        return "\n".join(parts)


# ─── Agent ──────────────────────────────────────────────────────────────────


class CompileAgent:
    """Process Inbox — compile raw inbox material into structured articles."""

    def __init__(self, llm: LlmClient, vault_dir: str | Path) -> None:
        self.llm = llm
        self.vault_dir = Path(vault_dir).resolve()
        self._prompt = _load_compile_prompt(self.vault_dir)

    # ── Public API ──────────────────────────────────────────────────────

    def list_unprocessed(self) -> list[Path]:
        """Return all `.md` files in 00_INBOX/ that haven't been compiled yet.

        A file counts as already compiled if its frontmatter contains
        ``compiled: true``. Hidden files and anything under ``_archive/``
        are skipped.
        """
        inbox = self.vault_dir / "00_INBOX"
        if not inbox.is_dir():
            return []

        found: list[Path] = []
        for md in inbox.glob("*.md"):
            if md.name.startswith("_"):
                continue
            try:
                post = frontmatter.load(md)
                if bool(post.metadata.get("compiled")):
                    continue
            except Exception:  # noqa: BLE001 — still include broken files
                pass
            found.append(md)
        return sorted(found)

    def run(self, *, dry_run: bool = False) -> CompileResult:
        """Process every unprocessed inbox file in one LLM call.

        Batching all files into one call gives the model the full
        picture — it can notice that two inbox files refer to the same
        client and merge them into one profile. This matches the
        behaviour of the Trados plugin's Process Inbox.
        """
        result = CompileResult()
        files = self.list_unprocessed()
        if not files:
            return result

        result.inbox_files_read = files

        try:
            reply = self._call_llm(files)
            result.llm_raw_reply = reply
            blocks = parse_file_blocks(reply)

            if not blocks:
                result.error = (
                    "Model reply contained no '### FILE:' blocks — nothing written."
                )
                return result

            result.writes = write_file_blocks(
                self.vault_dir, blocks, dry_run=dry_run
            )

            if not dry_run and any(w.ok for w in result.writes):
                result.inbox_files_archived = _archive_sources(
                    files, self.vault_dir, blocks
                )

        except Exception as exc:  # noqa: BLE001 — agent boundary
            _LOG.exception("Process Inbox failed")
            result.error = f"{type(exc).__name__}: {exc}"

        return result

    # ── Internals ───────────────────────────────────────────────────────

    def _call_llm(self, files: list[Path]) -> str:
        """Build the message list and run a non-streaming completion."""
        bundle = _bundle_inbox_files(files, self.vault_dir)
        messages = [
            system(self._prompt),
            user(
                "Process these inbox files. Produce one or more "
                "`### FILE: <path>` blocks in your reply with the full "
                "Markdown content of each new or updated article. Do not "
                "add explanatory prose outside the file blocks.\n\n"
                + bundle
            ),
        ]
        return self.llm.complete(messages)


# ─── Helpers ────────────────────────────────────────────────────────────────


def _load_compile_prompt(vault_dir: Path) -> str:
    """Load compile.md, preferring the vault's copy over the bundled fallback."""
    user_template = vault_dir / "06_TEMPLATES" / "compile.md"
    if user_template.is_file():
        try:
            return user_template.read_text(encoding="utf-8")
        except OSError:
            pass

    bundled = Path(__file__).resolve().parent.parent / "templates" / "compile.md"
    if bundled.is_file():
        return bundled.read_text(encoding="utf-8")

    return (
        "You are the SuperMemory Librarian. Read the inbox material and "
        "produce structured Markdown articles using `### FILE: <path>` "
        "blocks. Use [[backlinks]] for cross-references."
    )


def _bundle_inbox_files(files: list[Path], vault_dir: Path) -> str:
    """Concatenate inbox files into a labelled block for the LLM."""
    pieces: list[str] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as exc:
            _LOG.warning("Skipping unreadable inbox file %s: %s", f, exc)
            continue
        rel = f.relative_to(vault_dir).as_posix()
        pieces.append(f"### INBOX FILE: {rel}\n\n{text.strip()}\n")
    return "\n---\n\n".join(pieces)


def _archive_sources(
    files: list[Path],
    vault_dir: Path,
    blocks: list[FileBlock],
) -> list[Path]:
    """Move processed inbox files to ``00_INBOX/_archive/``.

    Before moving, stamp the source file's frontmatter with
    ``compiled: true``, ``compiled_date``, and a list of the files it
    was compiled into. Mirrors the "archive the inbox file" rule in
    compile.md.
    """
    archive_dir = vault_dir / "00_INBOX" / "_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    compiled_to = [b.relative_path for b in blocks]
    today = date.today().isoformat()
    archived: list[Path] = []

    for src in files:
        try:
            post = frontmatter.load(src)
        except Exception:
            post = frontmatter.Post(content=src.read_text(encoding="utf-8"))

        post.metadata["compiled"] = True
        post.metadata["compiled_date"] = today
        post.metadata["compiled_to"] = compiled_to

        try:
            src.write_text(frontmatter.dumps(post), encoding="utf-8")
        except OSError as exc:
            _LOG.warning("Could not stamp inbox file %s: %s", src, exc)
            continue

        target = archive_dir / src.name
        # If a file with the same name already exists in archive, suffix
        # it with a counter so we never clobber history.
        if target.exists():
            stem = src.stem
            suffix = src.suffix
            i = 1
            while True:
                candidate = archive_dir / f"{stem} ({i}){suffix}"
                if not candidate.exists():
                    target = candidate
                    break
                i += 1

        try:
            src.replace(target)
            archived.append(target)
        except OSError as exc:
            _LOG.warning("Could not archive %s: %s", src, exc)

    return archived


__all__ = ["CompileAgent", "CompileResult"]
