"""Pluggable case-source interface for the dengue_prep pipeline.

Built-in sources live alongside this file (``filesystem.py``, ``s3.py`` — future).
External sources can be loaded by file path or dotted module path via the
``source_mode`` config, exactly like weather sources.

Every source module must expose a class named ``Source`` that subclasses
:class:`CaseSource` (or, structurally, provides ``read`` and ``list_objects``).

Contract
--------
``list_objects(prefix)`` — return a list of source-relative paths / IDs that
identify the raw case files the parser will consume next. Paths are opaque to
the step; the source decides what a "path" means (a directory entry, an S3
key, a monthly bucket in an API, etc.).

``read(path)`` — return raw bytes for one such path. The step validates every
listed path is readable and non-empty; the next pipeline step (parse_case_data)
does the actual parsing.

Why this shape (vs weather's return-records shape)
--------------------------------------------------
Cases are already file-based downstream — the parse step reads xlsx/csv from
disk. Making sources return raw bytes lets API-backed sources (IHIP, custom
state APIs) fetch data, write to a local cache file, and hand the step the
path — same code path as filesystem/s3, no per-source special-casing in the
step.

Resolution
----------
The ``source_mode`` config value resolves to a Python module via:

1. Path-like input (contains ``/`` / ``\\`` / starts with ``.``) →
   load as a file path (absolute or relative).
2. Dotted input (``ap_datasets.ihip_api``) → convert to ``ap_datasets/ihip_api.py``
   relative to cwd, load as file path.
3. Otherwise → look for a built-in at ``case_sources/<source_mode>.py``.

The resolved module must expose a class named ``Source`` returning an instance
compatible with :class:`CaseSource`.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Mapping

# Under the acestor.dengue_prep tree so INFO logs reach stdout + run.log
# (acestor's create_logger attaches handlers only to that tree).
log = logging.getLogger("acestor.dengue_prep.case_sources")

_BUILTIN_DIR = os.path.dirname(__file__)


class CaseSource(ABC):
    """Abstract base class for case-data sources.

    Subclasses must implement :meth:`list_objects` and :meth:`read`. The
    :meth:`build` classmethod constructs the source from a config mapping —
    subclasses override it to translate their config keys into constructor
    arguments.
    """

    @classmethod
    @abstractmethod
    def build(cls, config: Mapping[str, Any]) -> "CaseSource":
        """Construct a source instance from the download-case-data config block."""

    @abstractmethod
    def list_objects(self, prefix: str = "") -> list[str]:
        """Return source-relative paths / IDs for available raw case files."""

    @abstractmethod
    def read(self, path: str) -> bytes:
        """Return raw bytes for a path previously returned by ``list_objects``."""


def load_source(source_mode: str, config: Mapping[str, Any]) -> CaseSource:
    """Load a CaseSource by name / path and build it from ``config``.

    See module docstring for resolution rules.
    """
    if os.sep in source_mode or "/" in source_mode or source_mode.startswith("."):
        file_path = source_mode
    elif "." in source_mode:
        file_path = source_mode.replace(".", os.sep) + ".py"
    else:
        file_path = None

    if file_path is not None:
        spec = importlib.util.spec_from_file_location("_case_source", file_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load case source from path: {file_path!r}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    else:
        builtin_path = os.path.join(_BUILTIN_DIR, f"{source_mode}.py")
        if os.path.exists(builtin_path):
            spec = importlib.util.spec_from_file_location(
                f"case_sources.{source_mode}", builtin_path
            )
            module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        else:
            module = importlib.import_module(
                f"pipelines.dengue_prep.lib.case_sources.{source_mode}"
            )

    if not hasattr(module, "Source"):
        raise ImportError(
            f"Case source {source_mode!r} must expose a class named 'Source'"
        )
    source_cls = module.Source
    if not (isinstance(source_cls, type) and issubclass(source_cls, CaseSource)):
        raise TypeError(
            f"Case source {source_mode!r}: 'Source' must be a subclass of CaseSource, "
            f"got {source_cls!r}"
        )
    return source_cls.build(config)
