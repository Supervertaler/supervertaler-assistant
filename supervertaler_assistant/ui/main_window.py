"""
supervertaler_assistant.ui.main_window
======================================

PyQt6 main window for the standalone Supervertaler Assistant app.

Layout
------
    ┌───────────────────────────────────────────────────┐
    │  Memory bank: [translation       ▾]  [Open…]      │   ← header
    ├───────────────────────────────────────────────────┤
    │ Memory Bank  [↓ Process Inbox] [✔ Health Check]   │   ← toolbar
    │              [⚗ Distill] [N files in inbox] [↻]   │     (mirrors the
    │                                                   │     Trados plugin)
    ├───────────────────────────────────────────────────┤
    │                                                   │
    │  Chat history (QTextBrowser, markdown-rendered)   │
    │                                                   │
    ├───────────────────────────────────────────────────┤
    │  Input box                         [Send]         │
    └───────────────────────────────────────────────────┘

The header combo lists every memory bank found under
``app_settings.memory_banks_root``. Switching the selection immediately
swaps the active bank: the reader and the agents are rebuilt against
the new path, the chat history stays put, and the user's next turn
reads from the new bank. This matches the Trados plugin's Memory Bank
toolbar dropdown (see ``docs/design/multi-memory-bank.md``).

All four workflow actions are wired to live agents. Chat messages
stream into the history area token-by-token, then get re-rendered as
Markdown once streaming finishes so links / tables / code blocks
display properly.

Run with::

    python -m supervertaler_assistant.ui.main_window
"""

from __future__ import annotations

import html
import sys
from pathlib import Path

from markdown_it import MarkdownIt
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QAction, QFont, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..agents import (
    ChatSession,
    CompileAgent,
    CompileResult,
    DistillAgent,
    DistillResult,
    LintAgent,
    LintResult,
    QueryAgent,
)
from ..llm import LlmClient, validate_settings
from ..memory_bank import MemoryBankInfo, MemoryBankReader, list_memory_banks
from ..settings import AppSettings, load_settings, save_settings
from .settings_dialog import SettingsDialog


# Sentinel userData for the synthetic "Manage banks…" row at the bottom
# of the dropdown. Picked as a string unlikely to collide with any real
# folder name.
_MANAGE_SENTINEL = "__supervertaler_manage_banks__"

# Default parent folder suggested to a first-time user.
_DEFAULT_BANKS_ROOT = Path.home() / "Supervertaler" / "memory-banks"

# Legacy single-bank path to migrate on first run if we find it.
_LEGACY_SINGLE_BANK = Path.home() / "Supervertaler" / "memory-bank"

# Regex that accepts short-identifier bank names: lowercase letters,
# digits, hyphen, underscore. Applied to the first-run migration name
# the user supplies so a nice "Main" becomes "main".
_BANK_NAME_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789-_"


def _sanitise_bank_name(raw: str) -> str:
    """Normalise a user-entered bank name into a short filesystem identifier.

    Spec: lowercase letters, digits, hyphen, underscore. Spaces become
    hyphens. Anything else is dropped. Leading / trailing separators are
    stripped. Returns an empty string if nothing survives sanitisation,
    which the caller should treat as a validation error.

    Examples:
        "Main"           → "main"
        "My Translation" → "my-translation"
        "eu procurement" → "eu-procurement"
        "foo!?bar"       → "foobar"
        "   "            → ""
    """
    lowered = raw.strip().lower().replace(" ", "-")
    cleaned = "".join(c for c in lowered if c in _BANK_NAME_CHARS)
    return cleaned.strip("-_")


# ─── Chat rendering helpers ────────────────────────────────────────────────


_MD = MarkdownIt("commonmark", {"linkify": True, "breaks": True}).enable("table")


def _render_markdown(text: str) -> str:
    """Convert Markdown to HTML for display in QTextBrowser.

    Resolves Obsidian-style ``[[backlinks]]`` to visible styled spans so
    the user sees where the Assistant drew its information from. We
    don't link to the files themselves (Qt can't open .md in an external
    handler reliably across platforms) – just signal "this is a memory
    bank reference".
    """
    # Resolve [[wikilink]] → <span class="wikilink">wikilink</span>
    # before handing to markdown-it so the Markdown parser sees plain
    # text, not unbalanced brackets.
    import re as _re

    def _repl(match: _re.Match) -> str:
        inner = match.group(1)
        return f'<span class="wikilink">{html.escape(inner)}</span>'

    text = _re.sub(r"\[\[([^\]]+)\]\]", _repl, text)
    return _MD.render(text)


# A small stylesheet injected once into the QTextBrowser so rendered
# Markdown looks civilised. Kept inline so we don't ship a .qss file.
_CHAT_CSS = """
<style>
  body { font-family: "Segoe UI", sans-serif; font-size: 10pt; color: #222; }
  code { background: #f3f3f3; padding: 1px 4px; border-radius: 3px; }
  pre  { background: #f3f3f3; padding: 8px; border-radius: 4px; }
  blockquote { border-left: 3px solid #ccc; margin: 6px 0; padding: 0 10px; color: #555; }
  table { border-collapse: collapse; margin: 6px 0; }
  th, td { border: 1px solid #ccc; padding: 4px 8px; }
  th { background: #f8f8f8; }
  .wikilink { color: #1e5a9e; background: #eaf2fc; padding: 0 4px; border-radius: 3px; }
  .role-user { color: #1e5a9e; font-weight: bold; }
  .role-assistant { color: #4a7c59; font-weight: bold; }
  .role-system { color: #888; font-size: 9pt; font-style: italic; }
  .error { color: #c0392b; }
</style>
"""


# ─── Worker threads ─────────────────────────────────────────────────────────


class ChatWorker(QThread):
    """Runs one chat turn off the UI thread."""

    chunk_received = pyqtSignal(str)
    finished_ok = pyqtSignal(str)   # full text
    failed = pyqtSignal(str)

    def __init__(
        self,
        agent: QueryAgent,
        session: ChatSession,
        question: str,
    ) -> None:
        super().__init__()
        self._agent = agent
        self._session = session
        self._question = question

    def run(self) -> None:
        try:
            chunks: list[str] = []
            for chunk in self._agent.stream_tokens(self._session, self._question):
                chunks.append(chunk)
                self.chunk_received.emit(chunk)
            self.finished_ok.emit("".join(chunks))
        except Exception as exc:  # noqa: BLE001 – UI layer needs a string
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class AgentWorker(QThread):
    """Runs Compile / Lint / Distill off the UI thread.

    These agents don't stream – they do one blocking LLM call and then
    file I/O. We expose a generic ``result`` signal carrying whatever
    result object the agent produced.
    """

    finished_ok = pyqtSignal(object)  # CompileResult | LintResult | DistillResult
    failed = pyqtSignal(str)

    def __init__(self, run_callable) -> None:  # noqa: ANN001 – any zero-arg
        super().__init__()
        self._run = run_callable

    def run(self) -> None:
        try:
            result = self._run()
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


# ─── Main window ────────────────────────────────────────────────────────────


class MainWindow(QMainWindow):
    def __init__(self, app_settings: AppSettings) -> None:
        super().__init__()
        self.setWindowTitle("Supervertaler Assistant")

        # Persistent state
        self.app_settings = app_settings
        self.resize(
            max(400, app_settings.window_width),
            max(300, app_settings.window_height),
        )

        # Runtime state
        self.memory_banks_root: Path | None = None
        self.memory_bank_dir: Path | None = None
        self.llm_client: LlmClient | None = None
        self.query_agent: QueryAgent | None = None
        self.compile_agent: CompileAgent | None = None
        self.lint_agent: LintAgent | None = None
        self.distill_agent: DistillAgent | None = None
        self.session: ChatSession | None = None

        self._chat_worker: ChatWorker | None = None
        self._agent_worker: AgentWorker | None = None
        self._assistant_stream_buffer = ""
        self._assistant_start_pos: int | None = None

        # Re-entrancy guard for the bank combo. Set to True while we
        # programmatically repopulate the items so ``currentIndexChanged``
        # doesn't trigger a spurious bank swap.
        self._suppress_combo_change = False

        self._build_ui()
        self._apply_chat_css()

        # Resolve the memory-banks root (with first-run migration if needed),
        # populate the dropdown, then load the last-active bank.
        self._try_initial_memory_banks_root()

    # ── UI scaffold ─────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._build_menu()

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header: memory bank dropdown (+ reveal-in-Explorer button)
        #
        # Replaces the single-bank label + "Choose…" button with a combo
        # populated from ``list_memory_banks(memory_banks_root)``. The
        # bottom row of the combo is a synthetic "Manage banks…" entry
        # (``_MANAGE_SENTINEL``) that opens the settings dialog so the
        # user can fix the root or create a new bank without leaving the
        # window. See ``docs/design/multi-memory-bank.md``.
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 4, 8, 4)
        header_layout.addWidget(QLabel("Memory bank:"))

        self.cmb_memory_bank = QComboBox()
        self.cmb_memory_bank.setMinimumWidth(220)
        self.cmb_memory_bank.setToolTip(
            "Active memory bank. Switching is immediate – the next chat\n"
            "turn reads from the new bank; chat history is preserved."
        )
        self.cmb_memory_bank.currentIndexChanged.connect(self._on_bank_combo_changed)
        header_layout.addWidget(self.cmb_memory_bank, stretch=1)

        self.btn_open_bank_folder = QPushButton("Open folder")
        self.btn_open_bank_folder.setToolTip(
            "Reveal the active memory bank folder in your file manager."
        )
        self.btn_open_bank_folder.clicked.connect(self._reveal_active_bank_folder)
        self.btn_open_bank_folder.setEnabled(False)
        header_layout.addWidget(self.btn_open_bank_folder)

        layout.addWidget(header)

        # Toolbar – 4 buttons, mirrors the Trados plugin's Memory Bank toolbar
        toolbar = QToolBar("Memory Bank")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.act_process = QAction("⬇ Process Inbox", self)
        self.act_process.setToolTip(
            "Read new files from 00_INBOX/ and have the Assistant organise them\n"
            "into structured memory bank articles (clients, terms, domains, style)."
        )
        self.act_process.triggered.connect(self._on_process_inbox)
        toolbar.addAction(self.act_process)

        self.act_health = QAction("✔ Health Check", self)
        self.act_health.setToolTip(
            "Scan the memory bank for conflicting terminology, broken links,\n"
            "stale or duplicate content. Fix what can be fixed, flag the rest."
        )
        self.act_health.triggered.connect(self._on_health_check)
        toolbar.addAction(self.act_health)

        self.act_distill = QAction("⚗ Distill", self)
        self.act_distill.setToolTip(
            "Extract knowledge from a translation file (TMX, DOCX, PDF, TBX)\n"
            "into a new 00_INBOX/ article ready for Process Inbox."
        )
        self.act_distill.triggered.connect(self._on_distill)
        toolbar.addAction(self.act_distill)

        toolbar.addSeparator()
        self.lbl_inbox_count = QLabel("Inbox: –")
        self.lbl_inbox_count.setStyleSheet("color: #888; padding: 0 8px;")
        toolbar.addWidget(self.lbl_inbox_count)

        self.act_refresh = QAction("↻", self)
        self.act_refresh.setToolTip("Refresh the inbox count")
        self.act_refresh.triggered.connect(self._refresh_inbox_count)
        toolbar.addAction(self.act_refresh)

        # Chat history
        self.chat = QTextBrowser()
        self.chat.setOpenExternalLinks(True)
        self.chat.setFont(QFont("Segoe UI", 10))
        layout.addWidget(self.chat, stretch=1)

        # Input row
        input_row = QWidget()
        input_layout = QHBoxLayout(input_row)
        input_layout.setContentsMargins(8, 8, 8, 8)
        self.txt_input = QLineEdit()
        self.txt_input.setPlaceholderText("Ask the Supervertaler Assistant…")
        self.txt_input.returnPressed.connect(self._on_send)
        input_layout.addWidget(self.txt_input, stretch=1)
        self.btn_send = QPushButton("Send")
        self.btn_send.clicked.connect(self._on_send)
        input_layout.addWidget(self.btn_send)
        layout.addWidget(input_row)

        # Status bar
        self._refresh_status_bar()

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("&File")
        act_pick_root = QAction("&Choose memory banks folder…", self)
        act_pick_root.setShortcut("Ctrl+O")
        act_pick_root.setStatusTip(
            "Pick the parent folder that holds all your memory banks."
        )
        act_pick_root.triggered.connect(self._pick_memory_banks_root)
        file_menu.addAction(act_pick_root)

        act_refresh_banks = QAction("&Refresh bank list", self)
        act_refresh_banks.setShortcut("F5")
        act_refresh_banks.triggered.connect(self._populate_bank_combo)
        file_menu.addAction(act_refresh_banks)

        file_menu.addSeparator()
        act_quit = QAction("&Quit", self)
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        edit_menu = menu_bar.addMenu("&Edit")
        act_settings = QAction("&Settings…", self)
        act_settings.setShortcut("Ctrl+,")
        act_settings.triggered.connect(self._open_settings)
        edit_menu.addAction(act_settings)

        help_menu = menu_bar.addMenu("&Help")
        act_about = QAction("&About Supervertaler Assistant", self)
        act_about.triggered.connect(self._show_about)
        help_menu.addAction(act_about)

    def _apply_chat_css(self) -> None:
        """Insert the stylesheet at the top of the chat document."""
        self.chat.setHtml(_CHAT_CSS)

    def _refresh_status_bar(self) -> None:
        ok, msg = validate_settings(self.app_settings.to_llm_settings())
        self.statusBar().showMessage(
            f"LLM: {msg}" + ("" if ok else "  ⚠  (open Settings to configure)")
        )

    # ── Memory bank loading ─────────────────────────────────────────────

    def _try_initial_memory_banks_root(self) -> None:
        """Resolve the memory-banks root on startup, migrating if needed.

        Cascade (see ``docs/design/multi-memory-bank.md`` §"One-time
        migration on first run after the upgrade"):

        1. ``settings.memory_banks_root`` is set and exists → use it.
        2. Default ``~/Supervertaler/memory-banks/`` already exists → use it.
        3. Legacy ``~/Supervertaler/memory-bank/`` exists → show the
           "name your existing bank" dialog, rename the folder into the
           new layout, then use it.
        4. First-time user → create ``~/Supervertaler/memory-banks/`` as
           an empty root so the settings dialog has somewhere to point.

        After the root is chosen the combo is populated and the
        ``last_active_bank`` (if still valid) is selected.
        """
        root = self._resolve_memory_banks_root()
        if root is None:
            # Extremely defensive: resolution can only return None if the
            # user explicitly cancelled the first-run migration dialog.
            # In that case we simply show an empty combo and leave the
            # user to pick a root from Settings.
            self.memory_banks_root = None
            self._populate_bank_combo()
            return

        self.memory_banks_root = root
        self.app_settings.memory_banks_root = str(root)
        save_settings(self.app_settings)

        self._populate_bank_combo()
        # _populate_bank_combo() auto-selects last_active_bank when it
        # can; if it couldn't (empty root, stale last_active_bank), the
        # user is left with an empty combo and no agents — same graceful
        # fallback as a missing memory bank in the previous single-bank
        # era.

    def _resolve_memory_banks_root(self) -> Path | None:
        """Decide which folder to use as the memory-banks root.

        Returns the resolved path or ``None`` if the user declined the
        first-run migration dialog.
        """
        # 1. Persisted value wins unconditionally (this is the
        # steady-state path for every run after the first one).
        persisted = self.app_settings.memory_banks_root
        if persisted:
            p = Path(persisted)
            if p.is_dir():
                return p

        # 2. Default parent folder already exists.
        if _DEFAULT_BANKS_ROOT.is_dir():
            return _DEFAULT_BANKS_ROOT

        # 3. Legacy single-bank layout → one-shot rename.
        if _LEGACY_SINGLE_BANK.is_dir():
            return self._migrate_legacy_single_bank()

        # 4. First-time user: create an empty root so the settings
        # dialog has a live folder to work with. We deliberately do not
        # copy the bundled skeleton here — that belongs in the settings
        # dialog's "Create new bank…" button (Step 3).
        try:
            _DEFAULT_BANKS_ROOT.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return _DEFAULT_BANKS_ROOT

    def _migrate_legacy_single_bank(self) -> Path | None:
        """Prompt the user to name their existing single-bank folder.

        On OK, moves ``~/Supervertaler/memory-bank/`` →
        ``~/Supervertaler/memory-banks/<name>/`` and persists
        ``last_active_bank`` = ``<name>``. On cancel returns ``None``.
        """
        while True:
            raw, ok = QInputDialog.getText(
                self,
                "Name your existing memory bank",
                "Supervertaler now supports several memory banks side by side.\n"
                f"\nFound an existing bank at:\n  {_LEGACY_SINGLE_BANK}\n\n"
                "Give it a short name so it can join the new layout.\n"
                "Use lowercase letters, digits, hyphens or underscores only.",
                text="main",
            )
            if not ok:
                return None

            name = _sanitise_bank_name(raw)
            if not name:
                QMessageBox.warning(
                    self,
                    "Invalid name",
                    "Please enter a short name using letters, digits, hyphens "
                    "or underscores.",
                )
                continue

            target_root = _DEFAULT_BANKS_ROOT
            target = target_root / name
            if target.exists():
                QMessageBox.warning(
                    self,
                    "Name already taken",
                    f"A folder named “{name}” already exists at:\n{target}\n\n"
                    "Pick a different name.",
                )
                continue

            try:
                target_root.mkdir(parents=True, exist_ok=True)
                _LEGACY_SINGLE_BANK.rename(target)
            except OSError as exc:
                QMessageBox.critical(
                    self,
                    "Could not move memory bank",
                    f"Moving\n  {_LEGACY_SINGLE_BANK}\nto\n  {target}\n"
                    f"failed:\n\n{exc}",
                )
                return None

            # Persist the result immediately so a crash during the next
            # few lines doesn't lose the migration.
            self.app_settings.memory_banks_root = str(target_root)
            self.app_settings.last_active_bank = name
            self.app_settings.memory_bank_dir = str(target)
            save_settings(self.app_settings)
            return target_root

    def _pick_memory_banks_root(self) -> None:
        """File menu: let the user point at a different memory-banks root."""
        start = self.app_settings.memory_banks_root or str(Path.home())
        picked = QFileDialog.getExistingDirectory(
            self, "Select memory banks folder (parent of all banks)", start
        )
        if not picked:
            return

        new_root = Path(picked)
        self.memory_banks_root = new_root
        self.app_settings.memory_banks_root = str(new_root)
        # Clear the remembered bank — the new root probably won't have
        # a bank with the same short name, and auto-selecting whatever
        # happens to match would be surprising.
        self.app_settings.last_active_bank = ""
        self.app_settings.memory_bank_dir = ""
        save_settings(self.app_settings)
        self._populate_bank_combo()

    def _populate_bank_combo(self) -> None:
        """Repopulate the header dropdown from ``memory_banks_root``.

        Tries to preserve the current selection across a refresh. If no
        bank matches ``last_active_bank``, the first bank in the list is
        auto-selected. If the root is empty, the combo shows a single
        disabled "No memory banks" placeholder entry.
        """
        self._suppress_combo_change = True
        try:
            self.cmb_memory_bank.clear()

            banks: list[MemoryBankInfo] = []
            if self.memory_banks_root is not None:
                banks = list_memory_banks(self.memory_banks_root)

            if not banks:
                self.cmb_memory_bank.addItem("(no memory banks)", userData=None)
                # Still add "Manage banks…" so the user has a way out.
                self.cmb_memory_bank.insertSeparator(1)
                self.cmb_memory_bank.addItem("Manage banks…", userData=_MANAGE_SENTINEL)
                self.cmb_memory_bank.setCurrentIndex(0)
                self.btn_open_bank_folder.setEnabled(False)
                self._unload_current_bank()
                return

            preferred = self.app_settings.last_active_bank or ""
            selected_row = 0
            for row, info in enumerate(banks):
                label = info.display_label or info.name
                suffix = (
                    f"  ·  {info.article_count} article"
                    f"{'s' if info.article_count != 1 else ''}"
                )
                self.cmb_memory_bank.addItem(label + suffix, userData=str(info.path))
                if info.name == preferred:
                    selected_row = row

            # Separator + "Manage banks…" at the bottom.
            self.cmb_memory_bank.insertSeparator(self.cmb_memory_bank.count())
            self.cmb_memory_bank.addItem("Manage banks…", userData=_MANAGE_SENTINEL)
        finally:
            self._suppress_combo_change = False

        # Now actually trigger a load for the auto-picked row.
        self.cmb_memory_bank.setCurrentIndex(selected_row)
        self._on_bank_combo_changed(selected_row)

    def _on_bank_combo_changed(self, index: int) -> None:
        """Handle a selection change in the memory bank dropdown.

        Three possibilities:
        - ``_MANAGE_SENTINEL`` row → open Settings, restore previous selection.
        - ``None`` userData (placeholder) → nothing to do.
        - A real path → load that memory bank.
        """
        if self._suppress_combo_change:
            return
        if index < 0:
            return

        data = self.cmb_memory_bank.itemData(index)

        if data == _MANAGE_SENTINEL:
            # Restore the previous selection (or row 0) so the combo
            # doesn't get stuck on "Manage banks…".
            self._suppress_combo_change = True
            try:
                previous_row = max(0, index - 2)  # skip the separator
                self.cmb_memory_bank.setCurrentIndex(previous_row)
            finally:
                self._suppress_combo_change = False
            self._open_settings()
            return

        if not data:
            return  # placeholder row

        self._load_memory_bank(Path(data))

    def _reveal_active_bank_folder(self) -> None:
        """Open the active memory bank folder in the OS file manager."""
        if self.memory_bank_dir is None or not self.memory_bank_dir.is_dir():
            return
        try:
            from PyQt6.QtCore import QUrl
            from PyQt6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.memory_bank_dir)))
        except Exception:  # noqa: BLE001 – best-effort, nothing to recover
            pass

    def _unload_current_bank(self) -> None:
        """Drop the active bank: clear agents and update the inbox label."""
        self.memory_bank_dir = None
        self.llm_client = None
        self.query_agent = None
        self.compile_agent = None
        self.lint_agent = None
        self.distill_agent = None
        self.session = None
        self._refresh_inbox_count()

    def _load_memory_bank(self, path: Path) -> None:
        reader = MemoryBankReader(path)
        if not reader.memory_bank_exists:
            QMessageBox.warning(
                self,
                "Not a memory bank folder",
                f"The folder\n{path}\n\ndoesn't contain the expected "
                "01_CLIENTS / 02_TERMINOLOGY / 03_DOMAINS / 04_STYLE folders.",
            )
            return

        self.memory_bank_dir = path
        self.btn_open_bank_folder.setEnabled(True)

        # Persist the selection so the next launch reopens the same bank.
        # memory_bank_dir is kept as a derived compat field – see
        # settings.py for the rationale.
        if self.memory_banks_root is not None:
            self.app_settings.memory_banks_root = str(self.memory_banks_root)
        self.app_settings.last_active_bank = path.name
        self.app_settings.memory_bank_dir = str(path)
        save_settings(self.app_settings)

        self._rebuild_agents()
        self._refresh_inbox_count()

        # Reset chat with a system banner
        self._apply_chat_css()
        self._append_system(
            f"Loaded memory bank: <code>{html.escape(str(path))}</code><br>"
            f"Model: <b>{html.escape(self.app_settings.llm_model)}</b>"
        )

    def _rebuild_agents(self) -> None:
        """Rebuild the LLM client + agents after a settings or memory bank change."""
        if self.memory_bank_dir is None:
            return
        self.llm_client = LlmClient(self.app_settings.to_llm_settings())
        self.query_agent = QueryAgent(self.llm_client, self.memory_bank_dir)
        self.compile_agent = CompileAgent(self.llm_client, self.memory_bank_dir)
        self.lint_agent = LintAgent(self.llm_client, self.memory_bank_dir)
        self.distill_agent = DistillAgent(self.llm_client, self.memory_bank_dir)

        self.session = ChatSession(
            memory_bank_dir=self.memory_bank_dir,
            project_name=self.app_settings.project_name or None,
            domain=self.app_settings.domain or None,
            source_lang=self.app_settings.source_lang or None,
            target_lang=self.app_settings.target_lang or None,
        )

    def _refresh_inbox_count(self) -> None:
        if self.compile_agent is None:
            self.lbl_inbox_count.setText("Inbox: –")
            self.act_process.setEnabled(False)
            return
        files = self.compile_agent.list_unprocessed()
        n = len(files)
        self.lbl_inbox_count.setText(
            f"Inbox: {n} file{'s' if n != 1 else ''}" if n > 0 else "Inbox: empty"
        )
        self.act_process.setEnabled(n > 0)

    # ── Settings ────────────────────────────────────────────────────────

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.app_settings, self)
        if not dlg.exec():
            return

        self.app_settings = dlg.result_settings()
        save_settings(self.app_settings)
        self._refresh_status_bar()

        # If the root changed, rescan and let _populate_bank_combo pick
        # an appropriate selection. If only the LLM config changed, just
        # rebuild the agents against the current bank.
        new_root = self.app_settings.memory_banks_root
        current_root = str(self.memory_banks_root) if self.memory_banks_root else ""
        if new_root != current_root:
            self.memory_banks_root = Path(new_root) if new_root else None
            self._populate_bank_combo()
        else:
            self._rebuild_agents()
            self._refresh_inbox_count()

        self._append_system("Settings updated.")

    def _show_about(self) -> None:
        from .. import __version__

        QMessageBox.about(
            self,
            "About Supervertaler Assistant",
            f"<h3>Supervertaler Assistant {__version__}</h3>"
            "<p>An AI assistant for professional translators. "
            "Consults a structured memory bank of clients, terminology, "
            "domains and style to help you translate consistently.</p>"
            '<p><a href="https://github.com/Supervertaler/supervertaler-assistant">'
            "github.com/Supervertaler/supervertaler-assistant</a></p>",
        )

    # ── Agent button handlers ──────────────────────────────────────────

    def _on_process_inbox(self) -> None:
        if not self._require_memory_bank():
            return
        if self.compile_agent is None:
            return
        if self._busy():
            return

        self._set_busy(True, "Processing inbox…")
        self._append_system("Running Process Inbox – this may take a minute…")

        agent = self.compile_agent
        self._agent_worker = AgentWorker(lambda: agent.run())
        self._agent_worker.finished_ok.connect(self._on_process_inbox_done)
        self._agent_worker.failed.connect(self._on_agent_failed)
        self._agent_worker.start()

    def _on_process_inbox_done(self, result: CompileResult) -> None:
        self._set_busy(False)
        self._append_system("Process Inbox complete.")
        self._append_assistant_markdown(f"### Process Inbox result\n\n{result.summary()}")
        self._refresh_inbox_count()

    def _on_health_check(self) -> None:
        if not self._require_memory_bank():
            return
        if self.lint_agent is None:
            return
        if self._busy():
            return

        self._set_busy(True, "Running health check…")
        self._append_system("Running Health Check – scanning the memory bank…")

        agent = self.lint_agent
        self._agent_worker = AgentWorker(lambda: agent.run())
        self._agent_worker.finished_ok.connect(self._on_health_check_done)
        self._agent_worker.failed.connect(self._on_agent_failed)
        self._agent_worker.start()

    def _on_health_check_done(self, result: LintResult) -> None:
        self._set_busy(False)
        self._append_system("Health Check complete.")

        body = [f"### Health Check result\n\n{result.summary()}"]
        if result.report_markdown:
            body.append("\n---\n")
            body.append(result.report_markdown)
        self._append_assistant_markdown("\n".join(body))
        self._refresh_inbox_count()

    def _on_distill(self) -> None:
        if not self._require_memory_bank():
            return
        if self.distill_agent is None:
            return
        if self._busy():
            return

        picked, _ = QFileDialog.getOpenFileName(
            self,
            "Pick a file to distill into the inbox",
            str(Path.home()),
            "Supported (*.tmx *.docx *.pdf *.tbx);;"
            "TMX (*.tmx);;DOCX (*.docx);;PDF (*.pdf);;TBX (*.tbx);;"
            "All files (*)",
        )
        if not picked:
            return

        source = Path(picked)
        self._set_busy(True, f"Distilling {source.name}…")
        self._append_system(f"Distilling <code>{html.escape(source.name)}</code>…")

        agent = self.distill_agent
        self._agent_worker = AgentWorker(lambda: agent.run(source))
        self._agent_worker.finished_ok.connect(self._on_distill_done)
        self._agent_worker.failed.connect(self._on_agent_failed)
        self._agent_worker.start()

    def _on_distill_done(self, result: DistillResult) -> None:
        self._set_busy(False)
        self._append_system("Distill complete.")
        self._append_assistant_markdown(f"### Distill result\n\n{result.summary()}")
        self._refresh_inbox_count()

    def _on_agent_failed(self, error: str) -> None:
        self._set_busy(False)
        self._append_system(
            f'<span class="error">Agent failed: {html.escape(error)}</span>'
        )

    # ── Chat ────────────────────────────────────────────────────────────

    def _on_send(self) -> None:
        if not self._require_memory_bank():
            return
        if self.query_agent is None or self.session is None:
            return
        question = self.txt_input.text().strip()
        if not question:
            return
        if self._busy():
            return

        self.txt_input.clear()
        self._append_user(question)
        self._append_assistant_stream_start()

        self._set_busy(True, "Chatting…")
        self._chat_worker = ChatWorker(self.query_agent, self.session, question)
        self._chat_worker.chunk_received.connect(self._on_chat_chunk)
        self._chat_worker.finished_ok.connect(self._on_chat_done)
        self._chat_worker.failed.connect(self._on_chat_failed)
        self._chat_worker.start()

    def _on_chat_chunk(self, chunk: str) -> None:
        # During streaming we just append plain text at the cursor.
        # When streaming finishes, we replace the streamed text with
        # the Markdown-rendered version.
        self._assistant_stream_buffer += chunk
        cursor = self.chat.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(chunk)
        self.chat.ensureCursorVisible()

    def _on_chat_done(self, full_text: str) -> None:
        self._set_busy(False)
        self._replace_streamed_with_markdown(full_text)
        if self.session and self.session.turns:
            last = self.session.turns[-1]
            if last.memory_bank_summary:
                self._append_system(f"↳ {html.escape(last.memory_bank_summary)}")
        self.txt_input.setFocus()

    def _on_chat_failed(self, error: str) -> None:
        self._set_busy(False)
        self._append_system(
            f'<span class="error">Error: {html.escape(error)}</span>'
        )

    # ── Rendering helpers ──────────────────────────────────────────────

    def _append_system(self, html_body: str) -> None:
        self.chat.append(f'<div class="role-system">{html_body}</div>')

    def _append_user(self, text: str) -> None:
        body = _render_markdown(text)
        self.chat.append(
            '<div style="margin:10px 0 4px 0;">'
            '<div class="role-user">You</div>'
            f"{body}</div>"
        )

    def _append_assistant_stream_start(self) -> None:
        """Open an empty assistant block that we'll stream plain text into."""
        self.chat.append(
            '<div style="margin:10px 0 4px 0;">'
            '<div class="role-assistant">Supervertaler Assistant</div>'
            "</div>"
        )
        cursor = self.chat.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._assistant_start_pos = cursor.position()
        self._assistant_stream_buffer = ""

    def _replace_streamed_with_markdown(self, full_text: str) -> None:
        """Swap the plain-text stream for Markdown-rendered HTML.

        We can't easily find-and-replace a QTextBrowser range while
        preserving formatting, so we take the simplest robust approach:
        undo the stream insertion, then append a fresh Markdown-rendered
        block.
        """
        if self._assistant_start_pos is None:
            return

        cursor = self.chat.textCursor()
        cursor.setPosition(self._assistant_start_pos)
        cursor.movePosition(
            QTextCursor.MoveOperation.End,
            QTextCursor.MoveMode.KeepAnchor,
        )
        cursor.removeSelectedText()

        body = _render_markdown(full_text)
        self.chat.append(body)
        self.chat.ensureCursorVisible()

        self._assistant_start_pos = None
        self._assistant_stream_buffer = ""

    def _append_assistant_markdown(self, markdown_text: str) -> None:
        """Append a complete assistant message (no streaming) as Markdown."""
        body = _render_markdown(markdown_text)
        self.chat.append(
            '<div style="margin:10px 0 4px 0;">'
            '<div class="role-assistant">Supervertaler Assistant</div>'
            f"{body}</div>"
        )
        self.chat.ensureCursorVisible()

    # ── Busy / guards ──────────────────────────────────────────────────

    def _busy(self) -> bool:
        return bool(
            (self._chat_worker and self._chat_worker.isRunning())
            or (self._agent_worker and self._agent_worker.isRunning())
        )

    def _set_busy(self, busy: bool, status: str = "") -> None:
        for act in (
            self.act_process,
            self.act_health,
            self.act_distill,
            self.act_refresh,
        ):
            act.setEnabled(not busy)
        self.btn_send.setEnabled(not busy)
        self.txt_input.setEnabled(not busy)
        if busy:
            self.statusBar().showMessage(status)
        else:
            self._refresh_status_bar()
            # Re-enable process-inbox only if inbox is non-empty
            self._refresh_inbox_count()

    def _require_memory_bank(self) -> bool:
        if self.memory_bank_dir is None:
            QMessageBox.warning(
                self, "No memory bank", "Pick a memory bank folder first (Ctrl+O)."
            )
            return False
        return True

    # ── Window lifecycle ───────────────────────────────────────────────

    def closeEvent(self, event) -> None:  # noqa: N802 – Qt override
        # Persist window size so next launch restores it
        self.app_settings.window_width = self.width()
        self.app_settings.window_height = self.height()
        try:
            save_settings(self.app_settings)
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)


# ─── Entry point ────────────────────────────────────────────────────────────


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Supervertaler Assistant")
    app.setOrganizationName("Supervertaler")

    settings = load_settings()
    win = MainWindow(settings)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
