"""Configuration loading and access for acestor pipelines."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _resolve_env(value: str) -> str:
    """Replace ``${VAR}`` or ``${VAR:-default}`` placeholders with env values."""

    def _replacer(match: re.Match) -> str:
        expr = match.group(1)
        if ":-" in expr:
            var, default = expr.split(":-", 1)
        else:
            var, default = expr, ""
        return os.environ.get(var.strip(), default)

    return _ENV_PATTERN.sub(_replacer, value)


def _resolve_env_recursive(obj: Any) -> Any:
    """Walk an arbitrary nested structure and resolve env vars in all strings."""
    if isinstance(obj, str):
        return _resolve_env(obj)
    if isinstance(obj, dict):
        return {k: _resolve_env_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_recursive(item) for item in obj]
    return obj


@dataclass
class PipelineConfig:
    """Lightweight wrapper around a raw YAML configuration."""

    path: Path
    raw: Mapping[str, Any]

    @classmethod
    def from_yaml(cls, path: str | Path) -> "PipelineConfig":
        p = Path(path)
        with p.open("r", encoding="utf-8") as f:
            data: Mapping[str, Any] = yaml.safe_load(f) or {}
        resolved = _resolve_env_recursive(data)
        return cls(path=p, raw=resolved)
