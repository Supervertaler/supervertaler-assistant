"""
supermemory.ui.settings_dialog
==============================

Settings dialog — vault path, LLM provider/model/key, context hints.

Kept deliberately simple: one modal dialog with a vertical form layout.
No tabs, no tree. Users should be able to read the whole thing at a
glance and close it in three clicks.

The dialog edits a *copy* of :class:`AppSettings`; the main window
decides whether to accept or discard the result.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QDoubleSpinBox,
    QWidget,
)

from ..llm import validate_settings
from ..settings import AppSettings


# Common model choices. Users can also type freely — QComboBox is editable.
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
        self.setWindowTitle("SuperMemory — Settings")
        self.setMinimumWidth(500)
        self._settings = deepcopy(settings)
        self._build_ui()
        self._load_into_form()

    # ── UI construction ─────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        outer.addLayout(form)

        # Vault ─────────────────────────────────────────────────────────
        vault_row = QWidget()
        vault_layout = QHBoxLayout(vault_row)
        vault_layout.setContentsMargins(0, 0, 0, 0)
        self.txt_vault = QLineEdit()
        self.txt_vault.setPlaceholderText("Path to your SuperMemory vault")
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._browse_vault)
        vault_layout.addWidget(self.txt_vault, stretch=1)
        vault_layout.addWidget(btn_browse)
        form.addRow("Vault folder:", vault_row)

        # LLM ───────────────────────────────────────────────────────────
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
            "Optional — http://localhost:11434 for Ollama, Azure endpoint, etc."
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
            "e.g. 'KB Tender 2026' — used to auto-detect the client"
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

        # Status line — updates as user types the model name
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

    def _browse_vault(self) -> None:
        start_dir = self.txt_vault.text() or str(Path.home())
        picked = QFileDialog.getExistingDirectory(
            self, "Select SuperMemory vault", start_dir
        )
        if picked:
            self.txt_vault.setText(picked)

    def _on_model_changed(self, _text: str) -> None:
        self._refresh_status()

    def _refresh_status(self) -> None:
        # Build a temporary LlmSettings to run validation
        tmp = self._collect_into_copy()
        llm = tmp.to_llm_settings()
        ok, msg = validate_settings(llm)
        prefix = "OK — " if ok else "⚠  "
        self.lbl_status.setText(prefix + msg)
        self.lbl_status.setStyleSheet(
            "color: #1e7a3a;" if ok else "color: #b8651a;"
        )

    def _load_into_form(self) -> None:
        s = self._settings
        self.txt_vault.setText(s.vault_dir)
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

    def _collect_into_copy(self) -> AppSettings:
        """Read the form into a fresh AppSettings (doesn't mutate self._settings)."""
        copy = deepcopy(self._settings)
        copy.vault_dir = self.txt_vault.text().strip()
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

    # ── Public ──────────────────────────────────────────────────────────

    def result_settings(self) -> AppSettings:
        """Return the edited settings (valid after ``exec()`` accepted)."""
        return self._settings


def _section_label(text: str) -> QLabel:
    label = QLabel(text)
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    label.setStyleSheet("color: #444; margin-top: 4px;")
    return label


__all__ = ["SettingsDialog"]
