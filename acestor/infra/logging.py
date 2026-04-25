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
        formatter = _RelativePathFormatter(
            "%(asctime)s [%(levelname)s] %(name)s:%(relpath)s:%(lineno)d - %(message)s"
        )
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        if run_dir is not None:
            run_dir = Path(run_dir)
            run_dir.mkdir(parents=True, exist_ok=True)
            log_path = run_dir / "run.log"
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
            logger.info("Log file: %s", log_path)

    return logger
