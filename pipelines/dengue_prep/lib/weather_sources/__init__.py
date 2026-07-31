"""Pluggable weather source interface for the dengue_prep pipeline.

Built-in sources live alongside this file (openmeteo.py, filesystem.py).
External sources can be loaded by file path or dotted module path via source_mode config.

Every source module must expose a class named ``Source`` that subclasses ``WeatherSource``.

Contract
--------
``get_weather_all_regions`` is the primary method — return daily records for every
region of the requested type. The step handles incremental logic (what date ranges
are missing) and calls this with the appropriate start/end.

``get_weather_for_regions`` accepts a list of location dicts (each may carry
``region_id``, ``lat``, ``lon``, ``name``, etc.). The default implementation calls
``get_weather_all_regions`` and filters by region_id; override for efficiency when
the underlying API supports per-location queries.

Record schema (all units normalized by the step before writing)
---------------------------------------------------------------
    date         str   YYYY-MM-DD
    region_id    str   LGD code or equivalent identifier
    t2m          float temperature
    d2m          float dew point temperature
    tp           float total precipitation
    name         str   region name (optional)
    parent       str   parent region identifier (optional)
    parent_name  str   parent region name (optional)

Units
-----
Sources return data in their native units (°C / mm is typical).
Declare what the source provides via ``temperature_unit`` and
``precipitation_unit`` in the config. The step always normalizes
to Kelvin and metres before writing CSVs so the downstream pipeline
always sees ERA5-compatible units.

should_persist
--------------
Set to False for filesystem-like sources where the source files ARE the
data — the step skips incremental fetch/write logic and references the
source files directly.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Mapping

log = logging.getLogger(__name__)

_BUILTIN_DIR = os.path.dirname(__file__)


class WeatherSource(ABC):
    should_persist: bool = True

    @classmethod
    def build(cls, config: Mapping[str, Any]) -> "WeatherSource":  # noqa: ARG003
        """Default builder — subclasses with construction args should override.

        Sources that take no init args (openmeteo, filesystem) inherit this
        no-arg builder; parametrised sources (e.g. dashboard) override to
        pull their kwargs out of the config mapping.
        """
        return cls()

    @abstractmethod
    def get_weather_all_regions(
        self,
        start_date: str,
        end_date: str,
        region_type: str,
        config: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Fetch daily weather records for all regions of the given type.

        Parameters
        ----------
        start_date, end_date : str
            Inclusive date range in YYYY-MM-DD format.
        region_type : str
            e.g. "district" or "mandal".
        config : Mapping
            Full weather_download config dict plus any extra context keys
            (e.g. ``geojson_base_path``) added by the step.

        Returns
        -------
        list[dict]
            One dict per region per day. See module docstring for schema.
        """

    def get_weather_for_regions(
        self,
        locations: list[dict[str, Any]],
        start_date: str,
        end_date: str,
        region_type: str,
        config: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Fetch daily weather records for specific locations.

        Each entry in ``locations`` may contain any of:
            region_id, lat, lon, name, parent, parent_name

        Default: fetch all regions and filter to the requested region_ids.
        Override when the underlying API supports efficient per-location queries,
        or when locations are provided as raw lat/lon pairs.
        """
        region_ids = {loc["region_id"] for loc in locations if "region_id" in loc}
        all_records = self.get_weather_all_regions(
            start_date, end_date, region_type, config
        )
        if region_ids:
            return [r for r in all_records if r.get("region_id") in region_ids]
        return all_records


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------


def detect_temperature_unit(values: list[float]) -> str:
    """Detect whether values are in Celsius or Kelvin.

    No surface temperature on Earth falls between 100 °C and 150 K,
    so mean > 150 is unambiguously Kelvin.
    """
    if not values:
        return "celsius"
    mean = sum(values) / len(values)
    return "kelvin" if mean > 150 else "celsius"


def normalize_temperature(values: list[float], declared_unit: str) -> list[float]:
    """Convert temperature values to Kelvin, guarding against double-conversion."""
    detected = detect_temperature_unit(values)
    if declared_unit != detected:
        log.warning(
            "temperature_unit declared=%s but values look like %s — "
            "using detected unit to avoid double conversion",
            declared_unit,
            detected,
        )
        declared_unit = detected
    if declared_unit == "celsius":
        return [v + 273.15 for v in values]
    return list(values)


def normalize_precipitation(values: list[float], declared_unit: str) -> list[float]:
    """Convert precipitation values to metres."""
    if declared_unit == "mm":
        return [v / 1000.0 for v in values]
    return list(values)


def normalize_records(
    records: list[dict[str, Any]],
    temperature_unit: str,
    precipitation_unit: str,
) -> list[dict[str, Any]]:
    """Normalize t2m, d2m (→ Kelvin) and tp (→ metres) across a record list."""
    if not records:
        return records

    t2m_vals = [r["t2m"] for r in records if "t2m" in r]
    d2m_vals = [r["d2m"] for r in records if "d2m" in r]
    tp_vals = [r["tp"] for r in records if "tp" in r]

    t2m_norm = normalize_temperature(t2m_vals, temperature_unit) if t2m_vals else []
    d2m_norm = normalize_temperature(d2m_vals, temperature_unit) if d2m_vals else []
    tp_norm = normalize_precipitation(tp_vals, precipitation_unit) if tp_vals else []

    result = []
    t_i = d_i = p_i = 0
    for r in records:
        rec = dict(r)
        if "t2m" in r:
            rec["t2m"] = t2m_norm[t_i]
            t_i += 1
        if "d2m" in r:
            rec["d2m"] = d2m_norm[d_i]
            d_i += 1
        if "tp" in r:
            rec["tp"] = tp_norm[p_i]
            p_i += 1
        result.append(rec)
    return result


# ---------------------------------------------------------------------------
# Source loader
# ---------------------------------------------------------------------------


def load_source(
    source_mode: str, config: Mapping[str, Any] | None = None
) -> WeatherSource:
    """Load a WeatherSource by name or path.

    Resolution order
    ----------------
    1. If ``source_mode`` contains a path separator or starts with '.' →
       load as a file path (absolute or relative).
    2. If ``source_mode`` contains a '.' (dotted module path, e.g.
       ``ap_datasets.ap_state_api``) → resolve to ``ap_datasets/ap_state_api.py``
       relative to the current working directory and load from file.
    3. Otherwise → look for a built-in module at
       ``weather_sources/{source_mode}.py`` in this directory.

    The resolved module must expose a class named ``Source`` that
    subclasses ``WeatherSource``.
    """
    if os.sep in source_mode or "/" in source_mode or source_mode.startswith("."):
        file_path = source_mode
    elif "." in source_mode:
        # Dotted module path → convert to relative file path
        file_path = source_mode.replace(".", os.sep) + ".py"
    else:
        file_path = None

    if file_path is not None:
        spec = importlib.util.spec_from_file_location("_weather_source", file_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load weather source from path: {file_path!r}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    else:
        builtin_path = os.path.join(_BUILTIN_DIR, f"{source_mode}.py")
        if os.path.exists(builtin_path):
            spec = importlib.util.spec_from_file_location(
                f"weather_sources.{source_mode}", builtin_path
            )
            module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        else:
            module = importlib.import_module(
                f"pipelines.dengue_prep.lib.weather_sources.{source_mode}"
            )

    if not hasattr(module, "Source"):
        raise ImportError(
            f"Weather source {source_mode!r} must expose a class named 'Source'"
        )
    source_cls = module.Source
    if not (isinstance(source_cls, type) and issubclass(source_cls, WeatherSource)):
        raise TypeError(
            f"Weather source {source_mode!r}: 'Source' must be a subclass of "
            f"WeatherSource, got {source_cls!r}"
        )
    return source_cls.build(config or {})
