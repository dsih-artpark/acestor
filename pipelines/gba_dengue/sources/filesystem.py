from __future__ import annotations

import os

from acestor.core.sources import FileSystemSource

# Predefined filesystem sources for this pipeline.
# These can be adjusted via environment variables without touching YAML.
fs_geojsons = FileSystemSource(
    base_path=os.getenv("GBA_FS_GEOJSONS_BASE", "geojsons/geojsons_GBA"),
    cache_enabled=False,
    cache_dir="",
    strategy="cloud",
)

fs_raw_case_data = FileSystemSource(
    base_path=os.getenv(
        "GBA_FS_RAW_CASE_BASE",
        "./dengue-model-pipeline-automation - GBA/src/dengue-model-pipeline/datasets",
    ),
    cache_enabled=bool(int(os.getenv("GBA_FS_RAW_CASE_CACHE_ENABLED", "1"))),
    cache_dir=os.getenv("GBA_FS_RAW_CASE_CACHE_DIR", "./cache/raw_case"),
    strategy=os.getenv("GBA_FS_RAW_CASE_STRATEGY", "local_first"),
)

fs_raw_weather_data = FileSystemSource(
    base_path=os.getenv(
        "GBA_FS_RAW_WEATHER_BASE",
        "./dengue-model-pipeline-automation - GBA/src/dengue-model-pipeline/datasets/parsednetcdf/zone",
    ),
    cache_enabled=bool(int(os.getenv("GBA_FS_RAW_WEATHER_CACHE_ENABLED", "1"))),
    cache_dir=os.getenv("GBA_FS_RAW_WEATHER_CACHE_DIR", "./cache/raw_weather"),
    strategy=os.getenv("GBA_FS_RAW_WEATHER_STRATEGY", "local_first"),
)


def get_case_source() -> FileSystemSource:
    return fs_raw_case_data


def get_weather_source() -> FileSystemSource:
    return fs_raw_weather_data


def get_geojson_base_dir() -> str:
    """Local directory containing GeoJSONs for all region types.

    Expected layout: {geojson_base}/{region_type}s/*.geojson
    """
    # `fs_geojsons.remote` is a `FileStorage` alias (`FileSource`) with a `base_path`.
    return str(fs_geojsons.remote.base_path)


def get_geojson_region_dir(region_type: str) -> str:
    """Directory containing geojson files for one region type."""
    from pathlib import Path

    return str(Path(get_geojson_base_dir()) / f"{region_type}s")
