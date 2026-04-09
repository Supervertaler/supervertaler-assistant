"""
supervertaler_assistant.settings
================================

Persisted app settings – memory bank paths, LLM provider/model/key, UI state.

Settings live in a single JSON file at a cross-platform per-user path
computed by ``platformdirs``:

    Windows : %APPDATA%\\Supervertaler\\SupervertalerAssistant\\settings.json
    macOS   : ~/Library/Application Support/SupervertalerAssistant/settings.json
    Linux   : ~/.config/SupervertalerAssistant/settings.json

Design notes
------------
- **API keys are persisted in plaintext** in the settings file, protected
  only by user-account file permissions. This matches the approach taken
  by most desktop LLM tooling (Ollama, Continue.dev, etc.). If you want
  stronger protection later, swap to the OS keychain via ``keyring``.

- **Load is lossy-tolerant.** Missing or malformed keys fall back to
  defaults rather than raising – a corrupted settings file should never
  prevent the app from starting. The user can always fix it from the
  Settings dialog.

- **Backward-compat migration chain.** Three generations of keys coexist:

  1. ``vault_dir`` (earliest) – renamed to ``memory_bank_dir`` when the
     product dropped the "SuperMemory" branding.
  2. ``memory_bank_dir`` (single-bank era) – one fixed folder per user.
  3. ``memory_banks_root`` + ``last_active_bank`` (multi-bank era) – a
     parent folder that holds several banks and a short identifier naming
     the currently active one. See ``docs/design/multi-memory-bank.md``.

  ``load_settings()`` walks the chain so a really old install upgrades in
  one hop. ``memory_bank_dir`` is still populated as a derived value for
  now so existing callers keep working during the transition to the
  dropdown UI.

- The :class:`AppSettings` dataclass is serialisation-agnostic. JSON is
  the current storage format but nothing else in the codebase depends on
  that choice.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from platformdirs import user_config_dir

from .llm import LlmSettings

_LOG = logging.getLogger(__name__)

APP_NAME = "SupervertalerAssistant"
APP_AUTHOR = "Supervertaler"


# ─── Top-level settings model ───────────────────────────────────────────────


@dataclass
class AppSettings:
    """Everything the app needs to remember between runs."""

    # Memory bank – multi-bank era:
    #   memory_banks_root  = parent folder that holds every bank
    #                        (e.g. ~/Supervertaler/memory-banks/)
    #   last_active_bank   = short identifier of the currently selected bank
    #                        (e.g. "translation"), persisted so the next
    #                        launch reopens the same bank.
    #   memory_bank_dir    = derived convenience path equal to
    #                        <memory_banks_root>/<last_active_bank>, kept as
    #                        a real field so existing single-bank callers
    #                        keep working during the UI transition. Step 2
    #                        of the multi-bank rollout removes the last
    #                        callers and this field retires.
    memory_banks_root: str = ""
    last_active_bank: str = ""
    memory_bank_dir: str = ""

    # LLM (flattened from LlmSettings for easier JSON round-tripping)
    llm_model: str = "anthropic/claude-sonnet-4-5"
    llm_api_key: str = ""
    llm_api_base: str = ""
    llm_temperature: float = 0.2
    llm_max_tokens: int = 4096
    llm_timeout_seconds: int = 120

    # Per-session hints that flow into memory bank context lookups
    project_name: str = ""
    domain: str = ""
    source_lang: str = ""
    target_lang: str = ""

    # UI state
    window_width: int = 900
    window_height: int = 700

    # Forward-compat: anything we don't recognise on load, but want to
    # round-trip on save so users who manually added keys don't lose them.
    _extra: dict = field(default_factory=dict)

    # ── Conversion ──────────────────────────────────────────────────────

    def to_llm_settings(self) -> LlmSettings:
        """Project the LLM-related fields into an :class:`LlmSettings`."""
        return LlmSettings(
            model=self.llm_model,
            api_key=self.llm_api_key or None,
            api_base=self.llm_api_base or None,
            temperature=self.llm_temperature,
            max_tokens=self.llm_max_tokens,
            timeout_seconds=self.llm_timeout_seconds,
        )

    def update_llm(self, llm: LlmSettings) -> None:
        """Copy :class:`LlmSettings` values back into this object."""
        self.llm_model = llm.model
        self.llm_api_key = llm.api_key or ""
        self.llm_api_base = llm.api_base or ""
        self.llm_temperature = llm.temperature
        self.llm_max_tokens = llm.max_tokens
        self.llm_timeout_seconds = llm.timeout_seconds


# ─── Persistence ────────────────────────────────────────────────────────────


def settings_path() -> Path:
    """Return the absolute path to the settings JSON file (may not exist yet)."""
    return Path(user_config_dir(APP_NAME, APP_AUTHOR)) / "settings.json"


def _legacy_settings_path() -> Path:
    """Return the pre-rename settings path (``SuperMemory`` app dir).

    Used once on load to migrate users from the old ``SuperMemory`` install
    to the new ``SupervertalerAssistant`` one.
    """
    return Path(user_config_dir("SuperMemory", APP_AUTHOR)) / "settings.json"


def load_settings() -> AppSettings:
    """Load settings from disk, returning defaults on any failure.

    This function never raises – a broken settings file should not
    prevent the app from starting. Warnings go to the logger instead.
    """
    path = settings_path()
    if not path.is_file():
        # Try legacy path from the SuperMemory era.
        legacy = _legacy_settings_path()
        if legacy.is_file():
            path = legacy
        else:
            return AppSettings()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _LOG.warning("Could not read settings at %s: %s", path, exc)
        return AppSettings()

    if not isinstance(raw, dict):
        return AppSettings()

    _migrate_legacy_keys(raw)

    # Split known fields from unknowns for forward-compat round-tripping.
    known = {f for f in AppSettings.__dataclass_fields__ if not f.startswith("_")}
    clean = {k: v for k, v in raw.items() if k in known}
    extra = {k: v for k, v in raw.items() if k not in known}

    try:
        settings = AppSettings(**clean)
    except TypeError as exc:
        _LOG.warning("Settings file has incompatible schema: %s", exc)
        return AppSettings()

    settings._extra = extra
    return settings


def _migrate_legacy_keys(raw: dict) -> None:
    """In-place rewrite of a raw JSON settings blob to the current schema.

    Walks the full migration chain so a really old install upgrades in one
    hop:

    1. ``vault_dir`` → ``memory_bank_dir`` (drop the retired "vault" name).
    2. ``memory_bank_dir`` → ``memory_banks_root`` + ``last_active_bank``
       (split the single path into parent + short identifier, preparing
       for the multi-bank dropdown).
    3. Reconcile: if the multi-bank keys are present, always recompute
       ``memory_bank_dir`` from them so legacy callers that still read
       that field see a value consistent with the dropdown.
    """
    # 1. vault_dir → memory_bank_dir
    if "vault_dir" in raw and "memory_bank_dir" not in raw:
        raw["memory_bank_dir"] = raw.pop("vault_dir")
    else:
        raw.pop("vault_dir", None)

    # 2. memory_bank_dir → memory_banks_root + last_active_bank
    legacy_dir = raw.get("memory_bank_dir") or ""
    if legacy_dir and not raw.get("memory_banks_root"):
        try:
            old = Path(str(legacy_dir))
            if old.name:
                raw["memory_banks_root"] = str(old.parent)
                raw["last_active_bank"] = old.name
        except (TypeError, ValueError):
            # Silently skip – corrupted path string, fall through to defaults.
            pass

    # 3. Reconcile multi-bank keys into memory_bank_dir for legacy callers.
    root = str(raw.get("memory_banks_root") or "")
    name = str(raw.get("last_active_bank") or "")
    if root and name:
        try:
            raw["memory_bank_dir"] = str(Path(root) / name)
        except (TypeError, ValueError):
            pass


def save_settings(settings: AppSettings) -> None:
    """Write settings to disk, creating parent directories as needed.

    Atomic-ish: writes to ``settings.json.tmp`` first then replaces the
    target, so an interrupted write can't leave a corrupted file.
    """
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = asdict(settings)
    # Merge forward-compat extras back in
    data.update(settings._extra)
    data.pop("_extra", None)

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


__all__ = [
    "APP_NAME",
    "APP_AUTHOR",
    "AppSettings",
    "settings_path",
    "load_settings",
    "save_settings",
]
