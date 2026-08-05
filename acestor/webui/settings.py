"""Runtime-editable settings for the web UI.

Persisted as a small JSON file at ``~/.acestor/webui_settings.json``. Read
on every request (file is tiny, no perf concern) so edits take effect
without restarting the service.

Only paths + display knobs are editable — no secrets, no AWS creds.
Missing keys fall back to the defaults defined below.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

HOME = Path(os.environ.get("HOME", "/home/ubuntu"))
_SETTINGS_PATH = HOME / ".acestor" / "webui_settings.json"


def _project_root() -> Path:
    # acestor/webui/settings.py → parents: [webui, acestor, <root>]
    return Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    """Editable knobs. All paths resolved absolute at read time."""

    artifacts_root: str = ""
    configs_root: str = ""
    logs_root: str = ""
    ledger_path: str = ""
    attempts_path: str = ""
    recent_runs_limit: int = 50
    auto_refresh_sec: int = 30
    default_instance: str = "t3.large"
    default_lifecycle: str = "spot"

    def resolved(self, key: str) -> Path:
        raw = getattr(self, key)
        p = Path(raw).expanduser() if raw else _default(key)
        return p.resolve()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_FIELD_DEFAULTS = {
    "artifacts_root": lambda: _project_root() / "artifacts",
    "configs_root": lambda: _project_root() / "configs",
    "logs_root": lambda: _project_root() / "logs",
    "ledger_path": lambda: HOME / ".acestor" / "remote_runs.jsonl",
    "attempts_path": lambda: HOME / ".acestor" / "scheduler_attempts.jsonl",
}


def _default(key: str) -> Path:
    """Compute the built-in default Path for a settings key."""
    return _FIELD_DEFAULTS[key]()


def load() -> Settings:
    """Read persisted settings; unset fields inherit the defaults."""
    s = Settings()
    if _SETTINGS_PATH.exists():
        try:
            raw = json.loads(_SETTINGS_PATH.read_text())
            for k, v in raw.items():
                if hasattr(s, k):
                    setattr(s, k, v)
        except (json.JSONDecodeError, OSError):
            pass  # corrupt file → fall back to defaults
    return s


def save(settings: Settings) -> None:
    """Atomically write settings to disk."""
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings.as_dict(), indent=2))
    tmp.replace(_SETTINGS_PATH)


def defaults_display() -> dict[str, str]:
    """For UI: the built-in default value per key, as a string."""
    return {k: str(v()) for k, v in _FIELD_DEFAULTS.items()}
