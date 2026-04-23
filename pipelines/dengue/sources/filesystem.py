from __future__ import annotations

import os
from typing import Any, Mapping

from acestor.core.sources import FileSystemSource

# Predefined filesystem sources for this pipeline.
# base_path is set to a placeholder here; call configure_from_yaml() in
# build_pipeline() so the value from data.geojson.base_path in the YAML
# (or GBA_FS_GEOJSONS_BASE env var) is applied before any step runs.
fs_geojsons = FileSystemSource(
    base_path=os.getenv("GBA_FS_GEOJSONS_BASE", ""),
    cache_enabled=False,
    cache_dir="",
    strategy="cloud",
)

fs_raw_case_data = FileSystemSource(
    base_path=os.getenv(
        "GBA_FS_RAW_CASE_BASE",
        "./datasets/raw_linelist_data/Bengaluru_IHIP_linelist",
    ),
    cache_enabled=bool(int(os.getenv("GBA_FS_RAW_CASE_CACHE_ENABLED", "1"))),
    cache_dir=os.getenv("GBA_FS_RAW_CASE_CACHE_DIR", "./cache/raw_case"),
    strategy=os.getenv("GBA_FS_RAW_CASE_STRATEGY", "local_first"),
)

fs_raw_weather_data = FileSystemSource(
    base_path=os.getenv(
        "GBA_FS_RAW_WEATHER_BASE",
        "./datasets/netcdf",
    ),
    cache_enabled=bool(int(os.getenv("GBA_FS_RAW_WEATHER_CACHE_ENABLED", "1"))),
    cache_dir=os.getenv("GBA_FS_RAW_WEATHER_CACHE_DIR", "./cache/raw_weather"),
    strategy=os.getenv("GBA_FS_RAW_WEATHER_STRATEGY", "local_first"),
)


def configure_from_yaml(raw: Mapping[str, Any]) -> None:
    """Apply pipeline YAML config to module-level sources.

    Call this once at the top of ``build_pipeline()`` so every step that calls
    ``get_geojson_base_dir()`` picks up the value from ``data.geojson.base_path``
    without needing any changes to step code.

    Priority: GBA_FS_GEOJSONS_BASE env var > data.geojson.base_path in YAML.
    """
    global fs_geojsons
    yaml_path = str(
        ((raw.get("data") or {}).get("geojson") or {}).get("base_path", "")
    ).strip()
    resolved = os.getenv("GBA_FS_GEOJSONS_BASE", "").strip() or yaml_path
    if resolved:
        fs_geojsons = FileSystemSource(
            base_path=resolved,
            cache_enabled=False,
            cache_dir="",
            strategy="cloud",
        )


def get_case_source() -> FileSystemSource:
    return fs_raw_case_data


def get_weather_source() -> FileSystemSource:
    return fs_raw_weather_data


def get_geojson_base_dir() -> str:
    """Local directory containing GeoJSONs for all region types.

    Expected layout: {geojson_base}/{region_type}s/*.geojson
    """
    return str(fs_geojsons.remote.base_path)


def get_geojson_region_dir(region_type: str) -> str:
    """Directory containing geojson files for one region type."""
    from pathlib import Path

    return str(Path(get_geojson_base_dir()) / f"{region_type}s")
