"""
supermemory.agents._output
==========================

Shared helpers for agents that produce file-writing output.

The compile (Process Inbox) and lint (Health Check) prompts both
instruct the LLM to emit blocks that look like::

    ### FILE: 02_TERMINOLOGY/compliance → naleving.md
    ---
    term_source: "compliance"
    ...
    ---

    # compliance → naleving
    ...

This module:

1. Parses those blocks out of a free-form reply (``parse_file_blocks``).
2. Validates target paths (``is_safe_vault_path``) so a malicious or
   confused reply can't write outside the vault or into system folders.
3. Writes the blocks to disk atomically (``write_file_blocks``).

No LLM calls happen here — this is pure text processing and I/O, which
makes it trivial to unit-test against captured reply fixtures.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

_LOG = logging.getLogger(__name__)

# Matches `### FILE: <path>` — tolerant of optional leading #'s and
# whitespace around the label. Captures the path up to the end of line.
_FILE_MARKER_RE = re.compile(
    r"^\s*#{2,4}\s*FILE\s*:\s*(?P<path>.+?)\s*$",
    re.MULTILINE,
)

# Folders agents are allowed to write into. Must match CONTENT_FOLDERS
# in vault.py plus 00_INBOX (for agents that drop reports there).
_ALLOWED_WRITE_FOLDERS: frozenset[str] = frozenset(
    {
        "00_INBOX",
        "01_CLIENTS",
        "02_TERMINOLOGY",
        "03_DOMAINS",
        "04_STYLE",
        "05_INDICES",
    }
)


# ─── Data ───────────────────────────────────────────────────────────────────


@dataclass
class FileBlock:
    """One `### FILE:` block parsed from an LLM reply."""

    relative_path: str
    content: str

    def __post_init__(self) -> None:
        # Normalise to forward slashes internally; we re-assemble with
        # the native separator at write time via Path.
        self.relative_path = self.relative_path.replace("\\", "/").strip()


# ─── Parsing ────────────────────────────────────────────────────────────────


def parse_file_blocks(reply: str) -> list[FileBlock]:
    """Extract every `### FILE:` block from an LLM reply.

    Blocks run from one FILE marker to the next (or EOF). Code-fence
    wrappers around each block are stripped if present — some models
    like to wrap the body in ```markdown.
    """
    if not reply:
        return []

    matches = list(_FILE_MARKER_RE.finditer(reply))
    if not matches:
        return []

    blocks: list[FileBlock] = []
    for i, m in enumerate(matches):
        start = m.end()  # content starts right after the marker line
        end = matches[i + 1].start() if i + 1 < len(matches) else len(reply)
        raw = reply[start:end].strip("\n")

        raw = _strip_code_fence_wrapper(raw)

        path = m.group("path").strip().strip("`").strip()
        if not path:
            continue
        blocks.append(FileBlock(relative_path=path, content=raw))

    return blocks


def _strip_code_fence_wrapper(text: str) -> str:
    """Remove a surrounding ```...``` fence if the whole block is wrapped."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text

    first_nl = stripped.find("\n")
    if first_nl < 0:
        return text
    inner = stripped[first_nl + 1 :]

    if inner.rstrip().endswith("```"):
        inner = inner.rstrip()
        inner = inner[: -len("```")].rstrip("\n")
    return inner


# ─── Safety ─────────────────────────────────────────────────────────────────


def is_safe_vault_path(vault_dir: Path, relative_path: str) -> bool:
    """Return True if ``relative_path`` is a safe write target in the vault.

    Rules:
    - Must resolve to a descendant of ``vault_dir`` (no ``..`` traversal).
    - First path segment must be an allowed folder (00_INBOX, 01–05).
    - No absolute paths, no drive letters.
    - Must end in ``.md``.
    """
    if not relative_path:
        return False

    # Reject absolute paths and drive letters outright
    p = Path(relative_path)
    if p.is_absolute() or (len(relative_path) > 1 and relative_path[1] == ":"):
        return False

    parts = p.parts
    if not parts or parts[0] not in _ALLOWED_WRITE_FOLDERS:
        return False

    if p.suffix.lower() != ".md":
        return False

    # Final resolution check — must stay inside vault_dir
    try:
        target = (vault_dir / p).resolve()
        target.relative_to(vault_dir.resolve())
    except (OSError, ValueError):
        return False

    return True


# ─── Writing ────────────────────────────────────────────────────────────────


@dataclass
class WriteResult:
    """Outcome of writing one FileBlock."""

    block: FileBlock
    ok: bool
    target_path: Path | None = None
    error: str | None = None
    was_created: bool = False
    was_updated: bool = False


def write_file_blocks(
    vault_dir: Path,
    blocks: list[FileBlock],
    *,
    dry_run: bool = False,
) -> list[WriteResult]:
    """Write each block to disk, skipping unsafe paths.

    Parameters
    ----------
    vault_dir
        The root of the target vault.
    blocks
        Parsed file blocks from :func:`parse_file_blocks`.
    dry_run
        If True, validate and report without touching disk. Used by
        the UI's "preview" mode before committing to writes.
    """
    results: list[WriteResult] = []
    vault_dir = vault_dir.resolve()

    for block in blocks:
        if not is_safe_vault_path(vault_dir, block.relative_path):
            results.append(
                WriteResult(
                    block=block,
                    ok=False,
                    error=f"unsafe or invalid target path: {block.relative_path!r}",
                )
            )
            continue

        target = (vault_dir / block.relative_path).resolve()
        existed = target.exists()

        if dry_run:
            results.append(
                WriteResult(
                    block=block,
                    ok=True,
                    target_path=target,
                    was_created=not existed,
                    was_updated=existed,
                )
            )
            continue

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(
                _ensure_trailing_newline(block.content),
                encoding="utf-8",
            )
            tmp.replace(target)
        except OSError as exc:
            _LOG.warning("Failed to write %s: %s", target, exc)
            results.append(
                WriteResult(
                    block=block,
                    ok=False,
                    target_path=target,
                    error=str(exc),
                )
            )
            continue

        results.append(
            WriteResult(
                block=block,
                ok=True,
                target_path=target,
                was_created=not existed,
                was_updated=existed,
            )
        )

    return results


def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


# ─── Summary formatting (for agent result reporting) ────────────────────────


def summarise_writes(results: list[WriteResult]) -> str:
    """Human-readable one-paragraph summary of a write batch."""
    if not results:
        return "No files produced."

    created = [r for r in results if r.ok and r.was_created]
    updated = [r for r in results if r.ok and r.was_updated]
    failed = [r for r in results if not r.ok]

    lines: list[str] = []
    if created:
        lines.append(f"Created {len(created)} file{_s(len(created))}:")
        lines.extend(f"  + {_rel(r)}" for r in created)
    if updated:
        lines.append(f"Updated {len(updated)} file{_s(len(updated))}:")
        lines.extend(f"  ~ {_rel(r)}" for r in updated)
    if failed:
        lines.append(f"Failed {len(failed)}:")
        lines.extend(f"  ! {r.block.relative_path} — {r.error}" for r in failed)

    return "\n".join(lines)


def _s(n: int) -> str:
    return "" if n == 1 else "s"


def _rel(result: WriteResult) -> str:
    return result.block.relative_path


__all__ = [
    "FileBlock",
    "WriteResult",
    "parse_file_blocks",
    "is_safe_vault_path",
    "write_file_blocks",
    "summarise_writes",
]
