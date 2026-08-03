"""Pluggable geojson-source interface for the dengue_prep pipeline.

Built-in sources live alongside this file (``filesystem.py``, ``dashboard.py``).
External sources can be loaded by file path or dotted module path via the
``source_mode`` config, exactly like case and weather sources.

Every source module must expose a class named ``Source`` that subclasses
:class:`GeojsonSource` (or, structurally, provides ``fetch``).

Contract
--------
``fetch(region_type, base_path)`` — ensure the ``{base_path}/{region_type}s/``
directory is populated with per-region ``{region_id}.geojson`` files. What
that means depends on the source:

- ``filesystem`` — verify the directory already exists (files were shipped
  with the repo or dropped in manually). No network access.
- ``dashboard`` — fetch the full FeatureCollection from
  ``GET /api/regions/{scope_id}?level={region_type}`` and split it into
  per-region files. Cached forever unless ``refresh=True``.

Return value is a :class:`GeojsonFetchResult` summarising what happened
(count fetched, count cached-hit, output dir), for the step to log.

Resolution
----------
The ``source_mode`` config value resolves to a Python module via:

1. Path-like input (contains ``/`` / ``\\`` / starts with ``.``) →
   load as a file path (absolute or relative).
2. Dotted input (``ap_datasets.custom_geojson``) → convert to
   ``ap_datasets/custom_geojson.py`` relative to cwd, load as file path.
3. Otherwise → look for a built-in at ``geojson_sources/<source_mode>.py``.

The resolved module must expose a class named ``Source`` returning an instance
compatible with :class:`GeojsonSource`.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping

# Reparented to the acestor.dengue_prep tree so INFO logs reach stdout + run.log
# (see the equivalent comment in case_sources/__init__.py for the full story).
log = logging.getLogger("acestor.dengue_prep.geojson_sources")

_BUILTIN_DIR = os.path.dirname(__file__)


@dataclass
class GeojsonFetchResult:
    """Summary of a fetch() call. Used by the step for logging."""

    region_type: str = ""
    output_dir: str = ""
    files_written: int = 0
    files_cached: int = 0  # would have been fetched but existing file was reused

    @property
    def total(self) -> int:
        return self.files_written + self.files_cached


class GeojsonSource(ABC):
    """Abstract base class for geojson sources.

    Subclasses must implement :meth:`fetch`. The :meth:`build` classmethod
    constructs the source from a config mapping — subclasses override it to
    translate their config keys into constructor arguments.
    """

    @classmethod
    @abstractmethod
    def build(cls, config: Mapping[str, Any]) -> "GeojsonSource":
        """Construct a source instance from the ``data.geojson`` config block."""

    @abstractmethod
    def fetch(self, region_type: str, base_path: str) -> GeojsonFetchResult:
        """Populate ``{base_path}/{region_type}s/`` with per-region geojson files."""


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def _load_module_from_path(path: str) -> Any:
    spec = importlib.util.spec_from_file_location("acestor_geojson_source_ext", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"geojson_sources: could not load module from path {path!r}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_source(source_mode: str, config: Mapping[str, Any]) -> GeojsonSource:
    """Resolve ``source_mode`` to a module and instantiate its ``Source`` class."""
    if not source_mode:
        source_mode = "filesystem"

    # Path-like → load as file path
    if any(c in source_mode for c in ("/", "\\")) or source_mode.startswith("."):
        mod = _load_module_from_path(source_mode)
    # Dotted path → treat as ``pkg/subpkg/leaf.py`` relative to cwd
    elif "." in source_mode:
        rel = source_mode.replace(".", os.sep) + ".py"
        if not os.path.isabs(rel):
            rel = os.path.join(os.getcwd(), rel)
        mod = _load_module_from_path(rel)
    # Built-in → look for a file next to this __init__.py
    else:
        builtin_path = os.path.join(_BUILTIN_DIR, f"{source_mode}.py")
        if not os.path.exists(builtin_path):
            raise ValueError(
                f"geojson_sources: unknown built-in source_mode={source_mode!r}. "
                f"Available: filesystem, dashboard. "
                f"For external sources, pass a dotted module or file path."
            )
        mod = importlib.import_module(
            f"pipelines.dengue_prep.lib.geojson_sources.{source_mode}"
        )

    source_cls = getattr(mod, "Source", None)
    if source_cls is None:
        raise ValueError(
            f"geojson_sources: module resolved for {source_mode!r} does not "
            f"expose a ``Source`` class."
        )
    if not (isinstance(source_cls, type) and issubclass(source_cls, GeojsonSource)):
        raise ValueError(
            f"geojson_sources: {source_mode!r}::Source must subclass GeojsonSource."
        )
    return source_cls.build(config)
