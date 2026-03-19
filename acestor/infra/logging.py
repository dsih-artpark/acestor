"""Logging setup utilities for acestor."""

from __future__ import annotations

import logging
import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _RelativePathFormatter(logging.Formatter):
    """Formatter that exposes %(relpath)s as a path relative to the project root."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            record.relpath = os.path.relpath(record.pathname, _PROJECT_ROOT)
        except ValueError:
            record.relpath = record.pathname
        return super().format(record)


def create_logger(name: str, run_dir: Path | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = _RelativePathFormatter(
            "%(asctime)s [%(levelname)s] %(name)s:%(relpath)s:%(lineno)d - %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger
