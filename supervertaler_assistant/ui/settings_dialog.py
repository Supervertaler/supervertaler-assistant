"""
supervertaler_assistant.ui.settings_dialog
==========================================

Settings dialog – memory banks root, bank management, LLM config, context hints.

Layout (top to bottom):

    Memory banks root: [path…………………………]  [Browse…]
    ┌───────────────────────────────────────┐
    │ Name             │ Articles           │   ← read-only table
    │ general          │ 0                  │     populated by
    │ translation      │ 12                 │     list_memory_banks()
    └───────────────────────────────────────┘
    [Create new bank…] [Rename…] [Delete…]

    Model: […………]  API key: […]  Temperature: […]  …
    Context hints (injected into every chat turn): project, domain, …

The bank list and its buttons operate **live on disk**: creating,
renaming, or deleting a bank happens the moment the user confirms,
independent of whether they click OK or Cancel on the dialog itself.
This matches the mental model of every other file-ops settings panel
in the app and keeps the undo surface small – nothing to roll back,
nothing to stage.

The non-file-ops fields (root text, LLM config, context hints) still
follow the traditional commit-on-OK model: the dialog edits a *copy*
of :class:`AppSettings` and the main window decides whether to accept
or discard the result.

Rename and delete are aware of ``last_active_bank``: if the operation
affects the bank that's currently selected in the main window, the
copy of ``AppSettings`` is patched so the dialog's result correctly
reflects which bank is active after the dialog closes.

Deletion double-confirms and performs a defence-in-depth path check
before calling :func:`shutil.rmtree` – the resolved bank path must
live strictly under the resolved root path, so no amount of symlink
trickery or stray ``..`` typing can point the delete at a folder
outside the user's memory-banks tree.
"""

from __future__ import annotations

import shutil
from copy import deepcopy
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..llm import validate_settings
from ..memory_bank import (
    SKELETON_FOLDERS,
    MemoryBankInfo,
    list_memory_banks,
    sanitise_bank_name,
)
from ..settings import AppSettings


# Common model choices. Users can also type freely – QComboBox is editable.
_MODEL_PRESETS: tuple[str, ...] = (
    "anthropic/claude-sonnet-4-5",
    "anthropic/claude-opus-4-5",
    "anthropic/claude-haiku-4-5",
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "openai/o1",
    "gemini/gemini-2.5-pro",
    "gemini/gemini-2.5-flash",
    "mistral/mistral-large-latest",
    "groq/llama-3.3-70b-versatile",
    "ollama/llama3.1:8b",
    "ollama/qwen2.5:14b",
)


class SettingsDialog(QDialog):
    """Modal editor for :class:`AppSettings`.

    Access the edited copy via :meth:`result_settings` after
    ``exec()`` returns ``QDialog.DialogCode.Accepted``.
    """

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Supervertaler Assistant – Settings")
        self.setMinimumWidth(560)
        self._settings = deepcopy(settings)
        self._build_ui()
        self._load_into_form()

    # ── UI construction ─────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        # Memory banks section ─────────────────────────────────────────
        outer.addWidget(_section_label("Memory banks"))

        root_form = QFormLayout()
        root_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        mb_row = QWidget()
        mb_layout = QHBoxLayout(mb_row)
        mb_layout.setContentsMargins(0, 0, 0, 0)
        self.txt_memory_banks_root = QLineEdit()
        self.txt_memory_banks_root.setPlaceholderText(
            "Parent folder that holds all your memory banks"
        )
        self.txt_memory_banks_root.setToolTip(
            "A single folder (e.g. ~/Supervertaler/memory-banks/) whose\n"
            "subfolders are individual memory banks. The toolbar dropdown\n"
            "lets you switch between them on the fly."
        )
        self.txt_memory_banks_root.editingFinished.connect(self._refresh_bank_list)
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._browse_memory_banks_root)
        mb_layout.addWidget(self.txt_memory_banks_root, stretch=1)
        mb_layout.addWidget(btn_browse)
        root_form.addRow("Memory banks root:", mb_row)
        outer.addLayout(root_form)

        # Bank list table
        self.tbl_banks = QTableWidget(0, 2, self)
        self.tbl_banks.setHorizontalHeaderLabels(["Name", "Articles"])
        self.tbl_banks.verticalHeader().setVisible(False)
        self.tbl_banks.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_banks.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.tbl_banks.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.tbl_banks.setAlternatingRowColors(True)
        self.tbl_banks.setMinimumHeight(140)
        header = self.tbl_banks.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tbl_banks.itemSelectionChanged.connect(self._update_bank_button_states)
        outer.addWidget(self.tbl_banks)

        # Bank management buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_create_bank = QPushButton("Create new bank…")
        self.btn_create_bank.clicked.connect(self._create_bank)
        btn_row.addWidget(self.btn_create_bank)
        self.btn_rename_bank = QPushButton("Rename…")
        self.btn_rename_bank.clicked.connect(self._rename_bank)
        btn_row.addWidget(self.btn_rename_bank)
        self.btn_delete_bank = QPushButton("Delete…")
        self.btn_delete_bank.clicked.connect(self._delete_bank)
        btn_row.addWidget(self.btn_delete_bank)
        outer.addLayout(btn_row)

        # LLM section ───────────────────────────────────────────────────
        outer.addSpacing(12)
        outer.addWidget(_section_label("LLM"))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        outer.addLayout(form)

        self.cmb_model = QComboBox()
        self.cmb_model.setEditable(True)
        self.cmb_model.addItems(_MODEL_PRESETS)
        self.cmb_model.currentTextChanged.connect(self._on_model_changed)
        form.addRow("Model:", self.cmb_model)

        self.txt_api_key = QLineEdit()
        self.txt_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_api_key.setPlaceholderText("Leave blank to read from environment")
        form.addRow("API key:", self.txt_api_key)

        self.txt_api_base = QLineEdit()
        self.txt_api_base.setPlaceholderText(
            "Optional – http://localhost:11434 for Ollama, Azure endpoint, etc."
        )
        form.addRow("API base URL:", self.txt_api_base)

        self.spin_temperature = QDoubleSpinBox()
        self.spin_temperature.setRange(0.0, 2.0)
        self.spin_temperature.setSingleStep(0.1)
        self.spin_temperature.setDecimals(2)
        form.addRow("Temperature:", self.spin_temperature)

        self.spin_max_tokens = QSpinBox()
        self.spin_max_tokens.setRange(128, 32768)
        self.spin_max_tokens.setSingleStep(256)
        form.addRow("Max reply tokens:", self.spin_max_tokens)

        self.spin_timeout = QSpinBox()
        self.spin_timeout.setRange(10, 1800)
        self.spin_timeout.setSuffix(" s")
        form.addRow("Request timeout:", self.spin_timeout)

        # Context hints ─────────────────────────────────────────────────
        outer.addSpacing(8)
        outer.addWidget(
            _section_label("Context hints (injected into every chat turn)")
        )
        form2 = QFormLayout()
        outer.addLayout(form2)

        self.txt_project = QLineEdit()
        self.txt_project.setPlaceholderText(
            "e.g. 'Acme Tender 2026' – used to auto-detect the client"
        )
        form2.addRow("Project name:", self.txt_project)

        self.txt_domain = QLineEdit()
        self.txt_domain.setPlaceholderText("e.g. 'EU Public Procurement'")
        form2.addRow("Domain:", self.txt_domain)

        self.txt_source_lang = QLineEdit()
        self.txt_source_lang.setPlaceholderText("e.g. 'Dutch' or 'nl-NL'")
        form2.addRow("Source language:", self.txt_source_lang)

        self.txt_target_lang = QLineEdit()
        self.txt_target_lang.setPlaceholderText("e.g. 'English (United Kingdom)'")
        form2.addRow("Target language:", self.txt_target_lang)

        # Status line – updates as user types the model name
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #666; font-style: italic;")
        outer.addSpacing(6)
        outer.addWidget(self.lbl_status)

        # OK / Cancel ───────────────────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # ── Helpers ─────────────────────────────────────────────────────────

    def _browse_memory_banks_root(self) -> None:
        start_dir = self.txt_memory_banks_root.text() or str(Path.home())
        picked = QFileDialog.getExistingDirectory(
            self, "Select memory banks root folder", start_dir
        )
        if picked:
            self.txt_memory_banks_root.setText(picked)
            self._refresh_bank_list()

    def _on_model_changed(self, _text: str) -> None:
        self._refresh_status()

    def _refresh_status(self) -> None:
        # Build a temporary LlmSettings to run validation
        tmp = self._collect_into_copy()
        llm = tmp.to_llm_settings()
        ok, msg = validate_settings(llm)
        prefix = "OK – " if ok else "⚠  "
        self.lbl_status.setText(prefix + msg)
        self.lbl_status.setStyleSheet(
            "color: #1e7a3a;" if ok else "color: #b8651a;"
        )

    def _load_into_form(self) -> None:
        s = self._settings
        self.txt_memory_banks_root.setText(s.memory_banks_root)
        self.cmb_model.setCurrentText(s.llm_model)
        self.txt_api_key.setText(s.llm_api_key)
        self.txt_api_base.setText(s.llm_api_base)
        self.spin_temperature.setValue(s.llm_temperature)
        self.spin_max_tokens.setValue(s.llm_max_tokens)
        self.spin_timeout.setValue(s.llm_timeout_seconds)
        self.txt_project.setText(s.project_name)
        self.txt_domain.setText(s.domain)
        self.txt_source_lang.setText(s.source_lang)
        self.txt_target_lang.setText(s.target_lang)
        self._refresh_status()
        self._refresh_bank_list()

    def _collect_into_copy(self) -> AppSettings:
        """Read the form into a fresh AppSettings (doesn't mutate self._settings)."""
        copy = deepcopy(self._settings)
        copy.memory_banks_root = self.txt_memory_banks_root.text().strip()
        copy.llm_model = self.cmb_model.currentText().strip()
        copy.llm_api_key = self.txt_api_key.text().strip()
        copy.llm_api_base = self.txt_api_base.text().strip()
        copy.llm_temperature = self.spin_temperature.value()
        copy.llm_max_tokens = self.spin_max_tokens.value()
        copy.llm_timeout_seconds = self.spin_timeout.value()
        copy.project_name = self.txt_project.text().strip()
        copy.domain = self.txt_domain.text().strip()
        copy.source_lang = self.txt_source_lang.text().strip()
        copy.target_lang = self.txt_target_lang.text().strip()
        return copy

    def _accept(self) -> None:
        self._settings = self._collect_into_copy()
        self.accept()

    # ── Bank list + management ──────────────────────────────────────────

    def _current_root_path(self) -> Path | None:
        """Resolve the root text field to a ``Path`` or ``None``.

        Returns ``None`` if the field is empty or doesn't point at a
        real directory – callers treat that as "no root, no actions".
        """
        raw = self.txt_memory_banks_root.text().strip()
        if not raw:
            return None
        try:
            p = Path(raw)
        except (TypeError, ValueError):
            return None
        if not p.is_dir():
            return None
        return p

    def _refresh_bank_list(self) -> None:
        """Rescan the root and repopulate the bank table."""
        self.tbl_banks.setRowCount(0)

        root = self._current_root_path()
        if root is None:
            self._update_bank_button_states()
            return

        banks = list_memory_banks(root)
        self.tbl_banks.setRowCount(len(banks))
        for row, info in enumerate(banks):
            name_item = QTableWidgetItem(info.name)
            name_item.setData(Qt.ItemDataRole.UserRole, str(info.path))
            name_item.setToolTip(str(info.path))
            # Use display_label as visible text when available – keeps
            # the hook for the later Master-Index-frontmatter read.
            if info.display_label:
                name_item.setText(f"{info.display_label}  ({info.name})")

            count_item = QTableWidgetItem(str(info.article_count))
            count_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )

            self.tbl_banks.setItem(row, 0, name_item)
            self.tbl_banks.setItem(row, 1, count_item)

        self._update_bank_button_states()

    def _update_bank_button_states(self) -> None:
        """Enable/disable bank management buttons based on current state."""
        has_root = self._current_root_path() is not None
        self.btn_create_bank.setEnabled(has_root)

        has_selection = self.tbl_banks.currentRow() >= 0 and has_root
        self.btn_rename_bank.setEnabled(has_selection)
        self.btn_delete_bank.setEnabled(has_selection)

    def _selected_bank(self) -> tuple[str, Path] | None:
        """Return (name, absolute_path) for the selected row, or ``None``.

        Reads the short identifier from the item's ``UserRole`` data, not
        from the visible text – the visible text may include a display
        label that isn't a valid folder name.
        """
        row = self.tbl_banks.currentRow()
        if row < 0:
            return None
        item = self.tbl_banks.item(row, 0)
        if item is None:
            return None
        path_str = item.data(Qt.ItemDataRole.UserRole)
        if not path_str:
            return None
        path = Path(str(path_str))
        return path.name, path

    def _create_bank(self) -> None:
        """Prompt for a name, create the bank skeleton, refresh the list."""
        root = self._current_root_path()
        if root is None:
            QMessageBox.warning(
                self,
                "No root folder",
                "Pick a memory banks root folder first so new banks have "
                "somewhere to live.",
            )
            return

        while True:
            raw, ok = QInputDialog.getText(
                self,
                "Create new memory bank",
                "Short name for the new bank (lowercase letters, digits,\n"
                "hyphens or underscores). Example: translation, general, medical.",
                text="",
            )
            if not ok:
                return

            name = sanitise_bank_name(raw)
            if not name:
                QMessageBox.warning(
                    self,
                    "Invalid name",
                    "Please enter a short name using letters, digits, hyphens "
                    "or underscores.",
                )
                continue

            target = root / name
            if target.exists():
                QMessageBox.warning(
                    self,
                    "Name already taken",
                    f"A folder named “{name}” already exists at:\n{target}\n\n"
                    "Pick a different name.",
                )
                continue

            try:
                for folder in SKELETON_FOLDERS:
                    (target / folder).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                QMessageBox.critical(
                    self,
                    "Could not create bank",
                    f"Creating\n  {target}\nfailed:\n\n{exc}",
                )
                return

            self._refresh_bank_list()

            # Select the freshly created row so the user sees it land.
            for row in range(self.tbl_banks.rowCount()):
                item = self.tbl_banks.item(row, 0)
                if item and item.data(Qt.ItemDataRole.UserRole) == str(
                    target.resolve()
                ):
                    self.tbl_banks.selectRow(row)
                    break
            return

    def _rename_bank(self) -> None:
        """Rename the selected bank's folder (and update last_active_bank)."""
        root = self._current_root_path()
        selection = self._selected_bank()
        if root is None or selection is None:
            return
        old_name, old_path = selection

        while True:
            raw, ok = QInputDialog.getText(
                self,
                "Rename memory bank",
                f"New short name for “{old_name}”:",
                text=old_name,
            )
            if not ok:
                return

            new_name = sanitise_bank_name(raw)
            if not new_name:
                QMessageBox.warning(
                    self,
                    "Invalid name",
                    "Please enter a short name using letters, digits, hyphens "
                    "or underscores.",
                )
                continue

            if new_name == old_name:
                return  # nothing to do

            new_path = root / new_name
            if new_path.exists():
                QMessageBox.warning(
                    self,
                    "Name already taken",
                    f"A folder named “{new_name}” already exists at:\n"
                    f"{new_path}\n\nPick a different name.",
                )
                continue

            try:
                old_path.rename(new_path)
            except OSError as exc:
                QMessageBox.critical(
                    self,
                    "Could not rename bank",
                    f"Renaming\n  {old_path}\nto\n  {new_path}\nfailed:\n\n{exc}",
                )
                return

            # If we just renamed the currently active bank, patch the
            # dialog's settings copy so the dialog's result reflects the
            # rename after the main window reads it.
            if self._settings.last_active_bank == old_name:
                self._settings.last_active_bank = new_name
                self._settings.memory_bank_dir = str(new_path)

            self._refresh_bank_list()
            for row in range(self.tbl_banks.rowCount()):
                item = self.tbl_banks.item(row, 0)
                if item and item.data(Qt.ItemDataRole.UserRole) == str(
                    new_path.resolve()
                ):
                    self.tbl_banks.selectRow(row)
                    break
            return

    def _delete_bank(self) -> None:
        """Delete the selected bank after two confirmations + a safety check."""
        root = self._current_root_path()
        selection = self._selected_bank()
        if root is None or selection is None:
            return
        name, path = selection

        # Defence in depth: the bank path must resolve strictly under
        # the root path. This protects against symlinks, stray ``..`` in
        # the root text, or a stale table row pointing outside the tree.
        try:
            root_resolved = root.resolve()
            path_resolved = path.resolve()
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Could not delete bank",
                f"Could not resolve paths:\n\n{exc}",
            )
            return

        if not _is_strictly_under(path_resolved, root_resolved):
            QMessageBox.critical(
                self,
                "Refusing to delete",
                f"The bank folder\n  {path_resolved}\n"
                f"does not live under the memory banks root\n  {root_resolved}\n\n"
                "Refusing to delete anything to be safe.",
            )
            return

        if name in ("", ".", ".."):
            QMessageBox.critical(
                self,
                "Refusing to delete",
                "Suspicious bank name – refusing to delete.",
            )
            return

        # First confirmation: standard yes/no.
        first = QMessageBox.question(
            self,
            "Delete memory bank?",
            f"Delete the memory bank “{name}”?\n\n"
            f"This will permanently remove:\n  {path_resolved}\n\n"
            "All clients, terminology, domains and style notes in this "
            "bank will be lost. This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if first != QMessageBox.StandardButton.Yes:
            return

        # Second confirmation: make the user type the bank name.
        typed, ok = QInputDialog.getText(
            self,
            "Confirm delete",
            f"Type the bank name “{name}” to confirm deletion:",
        )
        if not ok or typed.strip() != name:
            QMessageBox.information(
                self,
                "Delete cancelled",
                "The typed name did not match – nothing was deleted.",
            )
            return

        try:
            shutil.rmtree(path_resolved)
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Could not delete bank",
                f"Removing\n  {path_resolved}\nfailed:\n\n{exc}",
            )
            return

        # Clear the "active bank" pointer if we just deleted it.
        if self._settings.last_active_bank == name:
            self._settings.last_active_bank = ""
            self._settings.memory_bank_dir = ""

        self._refresh_bank_list()

    # ── Public ──────────────────────────────────────────────────────────

    def result_settings(self) -> AppSettings:
        """Return the edited settings (valid after ``exec()`` accepted)."""
        return self._settings


# ── Module helpers ─────────────────────────────────────────────────────


def _section_label(text: str) -> QLabel:
    label = QLabel(text)
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    label.setStyleSheet("color: #444; margin-top: 4px;")
    return label


def _is_strictly_under(child: Path, parent: Path) -> bool:
    """True if ``child`` lives strictly under ``parent`` (not equal, not outside).

    Both paths should already be resolved. ``Path.is_relative_to`` is
    available from Python 3.9 on; we also reject the trivial
    ``child == parent`` case since that would be "delete the root".
    """
    if child == parent:
        return False
    try:
        return child.is_relative_to(parent)
    except AttributeError:
        # Pre-3.9 fallback, should never hit for our supported Python.
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False


__all__ = ["SettingsDialog"]
