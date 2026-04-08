"""
supermemory.ui.main_window
==========================

PyQt6 main window for the standalone SuperMemory app.

Layout
------
    ┌───────────────────────────────────────────────────┐
    │  Vault: C:\\Users\\me\\Supervertaler\\supermemory      │   ← header
    ├───────────────────────────────────────────────────┤
    │ SuperMemory  [↓ Process Inbox] [✔ Health Check]   │   ← toolbar
    │              [⚗ Distill] [N files in inbox] [↻]   │     (mirrors the
    │                                                   │     Trados plugin)
    ├───────────────────────────────────────────────────┤
    │                                                   │
    │  Chat history (QTextBrowser, markdown-rendered)   │
    │                                                   │
    ├───────────────────────────────────────────────────┤
    │  Input box                         [Send]         │
    └───────────────────────────────────────────────────┘

All four workflow actions are wired to live agents. Chat messages
stream into the history area token-by-token, then get re-rendered as
Markdown once streaming finishes so links / tables / code blocks
display properly.

Run with::

    python -m supermemory.ui.main_window
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
    QFileDialog,
    QHBoxLayout,
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
from ..settings import AppSettings, load_settings, save_settings
from ..vault import SuperMemoryReader
from .settings_dialog import SettingsDialog


# ─── Chat rendering helpers ────────────────────────────────────────────────


_MD = MarkdownIt("commonmark", {"linkify": True, "breaks": True}).enable("table")


def _render_markdown(text: str) -> str:
    """Convert Markdown to HTML for display in QTextBrowser.

    Resolves Obsidian-style ``[[backlinks]]`` to visible styled spans so
    the user sees where the agent drew its information from. We don't
    link to the files themselves (Qt can't open .md in an external
    handler reliably across platforms) — just signal "this is a vault
    reference".
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
        except Exception as exc:  # noqa: BLE001 — UI layer needs a string
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class AgentWorker(QThread):
    """Runs Compile / Lint / Distill off the UI thread.

    These agents don't stream — they do one blocking LLM call and then
    file I/O. We expose a generic ``result`` signal carrying whatever
    result object the agent produced.
    """

    finished_ok = pyqtSignal(object)  # CompileResult | LintResult | DistillResult
    failed = pyqtSignal(str)

    def __init__(self, run_callable) -> None:  # noqa: ANN001 — any zero-arg
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
        self.setWindowTitle("SuperMemory")

        # Persistent state
        self.app_settings = app_settings
        self.resize(
            max(400, app_settings.window_width),
            max(300, app_settings.window_height),
        )

        # Runtime state
        self.vault_dir: Path | None = None
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

        self._build_ui()
        self._apply_chat_css()

        # Auto-load vault from settings, or fall back to default
        self._try_initial_vault()

    # ── UI scaffold ─────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._build_menu()

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header: vault path
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 4, 8, 4)
        header_layout.addWidget(QLabel("Vault:"))
        self.lbl_vault_path = QLabel("(none)")
        self.lbl_vault_path.setStyleSheet("color: #888;")
        header_layout.addWidget(self.lbl_vault_path, stretch=1)
        btn_pick = QPushButton("Choose vault…")
        btn_pick.clicked.connect(self._pick_vault)
        header_layout.addWidget(btn_pick)
        layout.addWidget(header)

        # Toolbar — 4 buttons, mirrors SuperMemoryToolbar.cs
        toolbar = QToolBar("SuperMemory")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.act_process = QAction("⬇ Process Inbox", self)
        self.act_process.setToolTip(
            "Read new files from 00_INBOX/ and use AI to organise them into\n"
            "structured knowledge base articles (clients, terms, domains, style)."
        )
        self.act_process.triggered.connect(self._on_process_inbox)
        toolbar.addAction(self.act_process)

        self.act_health = QAction("✔ Health Check", self)
        self.act_health.setToolTip(
            "Scan the vault for conflicting terminology, broken links,\n"
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
        self.txt_input.setPlaceholderText("Ask SuperMemory…")
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
        act_pick = QAction("&Choose vault…", self)
        act_pick.setShortcut("Ctrl+O")
        act_pick.triggered.connect(self._pick_vault)
        file_menu.addAction(act_pick)

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
        act_about = QAction("&About SuperMemory", self)
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

    # ── Vault loading ───────────────────────────────────────────────────

    def _try_initial_vault(self) -> None:
        """Load a vault on startup — settings first, then the default."""
        if self.app_settings.vault_dir:
            path = Path(self.app_settings.vault_dir)
            if path.is_dir():
                self._load_vault(path)
                return

        default = Path.home() / "Supervertaler" / "supermemory"
        if default.is_dir():
            self._load_vault(default)

    def _pick_vault(self) -> None:
        start = self.app_settings.vault_dir or str(Path.home())
        picked = QFileDialog.getExistingDirectory(
            self, "Select SuperMemory vault", start
        )
        if picked:
            self._load_vault(Path(picked))

    def _load_vault(self, path: Path) -> None:
        reader = SuperMemoryReader(path)
        if not reader.vault_exists:
            QMessageBox.warning(
                self,
                "Not a SuperMemory vault",
                f"The folder\n{path}\n\ndoesn't contain the expected "
                "01_CLIENTS / 02_TERMINOLOGY / 03_DOMAINS / 04_STYLE folders.",
            )
            return

        self.vault_dir = path
        self.lbl_vault_path.setText(str(path))
        self.lbl_vault_path.setStyleSheet("color: #1e5a9e;")

        self.app_settings.vault_dir = str(path)
        save_settings(self.app_settings)

        self._rebuild_agents()
        self._refresh_inbox_count()

        # Reset chat with a system banner
        self._apply_chat_css()
        self._append_system(
            f"Loaded vault: <code>{html.escape(str(path))}</code><br>"
            f"Model: <b>{html.escape(self.app_settings.llm_model)}</b>"
        )

    def _rebuild_agents(self) -> None:
        """Rebuild the LLM client + agents after a settings or vault change."""
        if self.vault_dir is None:
            return
        self.llm_client = LlmClient(self.app_settings.to_llm_settings())
        self.query_agent = QueryAgent(self.llm_client, self.vault_dir)
        self.compile_agent = CompileAgent(self.llm_client, self.vault_dir)
        self.lint_agent = LintAgent(self.llm_client, self.vault_dir)
        self.distill_agent = DistillAgent(self.llm_client, self.vault_dir)

        self.session = ChatSession(
            vault_dir=self.vault_dir,
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
        if dlg.exec():
            self.app_settings = dlg.result_settings()
            save_settings(self.app_settings)
            self._refresh_status_bar()

            # If the vault changed, reload from scratch; otherwise just
            # rebuild the agents with the new LLM config.
            if self.vault_dir and str(self.vault_dir) != self.app_settings.vault_dir:
                new_vault = Path(self.app_settings.vault_dir)
                if new_vault.is_dir():
                    self._load_vault(new_vault)
                else:
                    self._rebuild_agents()
            else:
                self._rebuild_agents()
                self._refresh_inbox_count()

            self._append_system("Settings updated.")

    def _show_about(self) -> None:
        from .. import __version__

        QMessageBox.about(
            self,
            "About SuperMemory",
            f"<h3>SuperMemory {__version__}</h3>"
            "<p>A self-organising, LLM-maintained translation "
            "knowledge base.</p>"
            '<p><a href="https://github.com/Supervertaler/SuperMemory">'
            "github.com/Supervertaler/SuperMemory</a></p>",
        )

    # ── Agent button handlers ──────────────────────────────────────────

    def _on_process_inbox(self) -> None:
        if not self._require_vault():
            return
        if self.compile_agent is None:
            return
        if self._busy():
            return

        self._set_busy(True, "Processing inbox…")
        self._append_system("Running Process Inbox — this may take a minute…")

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
        if not self._require_vault():
            return
        if self.lint_agent is None:
            return
        if self._busy():
            return

        self._set_busy(True, "Running health check…")
        self._append_system("Running Health Check — scanning the vault…")

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
        if not self._require_vault():
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
        if not self._require_vault():
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
            if last.kb_summary:
                self._append_system(f"↳ {html.escape(last.kb_summary)}")
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
            '<div class="role-assistant">SuperMemory</div>'
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
            '<div class="role-assistant">SuperMemory</div>'
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

    def _require_vault(self) -> bool:
        if self.vault_dir is None:
            QMessageBox.warning(
                self, "No vault", "Pick a SuperMemory vault first (Ctrl+O)."
            )
            return False
        return True

    # ── Window lifecycle ───────────────────────────────────────────────

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt override
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
    app.setApplicationName("SuperMemory")
    app.setOrganizationName("Supervertaler")

    settings = load_settings()
    win = MainWindow(settings)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
