"""Typed configuration dataclasses for the dengue_prep pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping


def _section(config: Mapping[str, Any], path: str) -> Mapping[str, Any]:
    current: Any = config
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return {}
        current = current.get(part)
        if current is None:
            return {}
    return current if isinstance(current, Mapping) else {}


# ---------------------------------------------------------------------------
# Output location
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrepOutputConfig:
    """Where the prep pipeline writes its prepared_data files."""

    base_dir: str  # e.g. "prepared_data"

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> PrepOutputConfig:
        return cls(
            base_dir=str(raw.get("base_dir", "prepared_data")).strip()
            or "prepared_data",
        )


# ---------------------------------------------------------------------------
# Geojson download
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrepGeojsonConfig:
    """Config for the download_geojsons step + downstream lookups.

    ``base_path`` is the cache directory (per-region files land under
    ``{base_path}/{region_type}s/{region_id}.geojson``). The prep step's
    behaviour is driven by ``source_mode``:

    - ``filesystem`` (default): files must already exist under ``base_path``.
      The step verifies + counts them, no network.
    - ``dashboard``: fetch from the disease-dashboard's
      ``GET /api/regions/{scope_id}?level=<region_type>`` endpoint, split
      into per-region files. Cached forever unless ``refresh=True``.
    """

    base_path: str
    source_mode: str = "filesystem"
    scope_id: str = (
        ""  # dashboard source: which scope to fetch (or DASHBOARD_SELECTED_REGION_ID env)
    )
    refresh: bool = False  # dashboard source: force re-download even if cached
    base_url: str = ""  # dashboard source: override base URL (or DASHBOARD_URL env)

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "PrepGeojsonConfig":
        return cls(
            base_path=str(raw.get("base_path", "")).strip(),
            source_mode=str(raw.get("source_mode", "filesystem")).strip()
            or "filesystem",
            scope_id=str(raw.get("scope_id", "")).strip(),
            refresh=bool(raw.get("refresh", False)),
            base_url=str(raw.get("base_url", "")).strip(),
        )


# ---------------------------------------------------------------------------
# Case download
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrepCaseDownloadConfig:
    enabled: bool
    # source_mode selects a case-source plugin (see pipelines/dengue_prep/lib/case_sources).
    # Empty falls back to source_backend for backwards compatibility.
    source_mode: str
    source_backend: str
    source_path: str
    cache_enabled: bool
    cache_dir: str
    cache_strategy: str
    filesystem_base_path: str
    s3_bucket: str
    s3_prefix: str
    source_storage: str
    source_prefix: str
    source_paths: list[str]
    dest_relpath: str

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> PrepCaseDownloadConfig:
        # source_path: config takes priority, then DENGUE_PREP_CASE_SOURCE env var
        source_path = str(raw.get("source_path", "")).strip()
        if not source_path:
            source_path = os.getenv("DENGUE_PREP_CASE_SOURCE", "").strip()
        return cls(
            enabled=bool(raw.get("enabled", False)),
            source_mode=str(raw.get("source_mode", "")).strip(),
            source_backend=str(raw.get("source_backend", "filesystem")).strip().lower()
            or "filesystem",
            source_path=source_path,
            cache_enabled=bool(raw.get("cache_enabled", True)),
            cache_dir=str(raw.get("cache_dir", "./cache/raw_case")).strip(),
            cache_strategy=str(raw.get("cache_strategy", "local_first")).strip()
            or "local_first",
            filesystem_base_path=str(raw.get("filesystem_base_path", "")).strip(),
            s3_bucket=str(raw.get("s3_bucket", "")).strip(),
            s3_prefix=str(raw.get("s3_prefix", "")).strip(),
            source_storage=raw.get("source_storage", ""),
            source_prefix=raw.get("source_prefix", ""),
            source_paths=list(raw.get("source_paths", [])),
            dest_relpath=raw.get("dest_relpath", "datasets/raw_linelist_data/linelist"),
        )


# ---------------------------------------------------------------------------
# Case parse
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrepGeocodingConfig:
    """Configures the geocode → spatial-join resolver for ``region_id``.

    When ``enabled`` is True and the IHIP file lacks an LGD code column, the
    case parser composes each row's address by concatenating the column values
    listed in ``address_fields``, sends to Google, validates that the response
    references at least one specific token from the same fields (fuzzy ≤ 1
    char), and PIPs the validated coords against the region's geojson layer.

    Rows that don't resolve on the primary fields are retried with
    ``fallback_address_fields`` if set — useful when ``Patient Address``
    can be cross-state or missing, and a ``Facility`` column gives a more
    reliable in-jurisdiction signal.

    Default is ``enabled=False`` — opt in per pipeline config.
    """

    enabled: bool
    address_fields: tuple[str, ...]
    fallback_address_fields: tuple[str, ...]
    cache_file: str
    extra_stopwords: tuple[str, ...]
    require_address: bool  # drop rows where the first address_field is empty
    require_validation: (
        bool  # drop rows that fail both passes (vs keeping w/ NaN region_id)
    )
    # (min_lat, min_lon, max_lat, max_lon) viewport biasing the geocoder.
    # Soft bias only — Google may still return a hit outside the box.
    bounds: tuple[float, float, float, float] | None
    # Substrings that must appear in Google's formatted_address (case-
    # insensitive) for the result to be accepted. Hard reject — used to
    # drop cross-state hits (e.g. require "Karnataka" so a Jhansi UP
    # geocode is thrown out before PIP).
    restrict_admin_area_tokens: tuple[str, ...]

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any] | None) -> PrepGeocodingConfig:
        raw = raw or {}

        def _s(v: Any) -> str:
            return "" if v is None else str(v).strip()

        def _list(key: str) -> list[str]:
            value = raw.get(key, [])
            if not isinstance(value, list):
                raise ValueError(
                    f"geocoding.{key} must be a list of column names "
                    f"(got {type(value).__name__})"
                )
            return [_s(c) for c in value if _s(c)]

        bounds_raw = raw.get("bounds")
        bounds: tuple[float, float, float, float] | None = None
        if bounds_raw is not None:
            if not isinstance(bounds_raw, list) or len(bounds_raw) != 4:
                raise ValueError(
                    "geocoding.bounds must be a list of 4 numbers "
                    "[min_lat, min_lon, max_lat, max_lon]"
                )
            bounds = (
                float(bounds_raw[0]),
                float(bounds_raw[1]),
                float(bounds_raw[2]),
                float(bounds_raw[3]),
            )

        return cls(
            enabled=bool(raw.get("enabled", False)),
            address_fields=tuple(_list("address_fields")),
            fallback_address_fields=tuple(_list("fallback_address_fields")),
            cache_file=_s(raw.get("cache_file", "./cache/geocode_cache.json"))
            or "./cache/geocode_cache.json",
            extra_stopwords=tuple(_s(w).lower() for w in _list("extra_stopwords")),
            require_address=bool(raw.get("require_address", True)),
            require_validation=bool(raw.get("require_validation", True)),
            bounds=bounds,
            restrict_admin_area_tokens=tuple(_list("restrict_admin_area_tokens")),
        )


def _normalise_date_column(raw: Any) -> list[str]:
    """Coerce ``date_column`` config value to a list of candidate column names.

    Accepts a single string (back-compat) or a list of strings. Falls back to
    ``["Sample Collected Date"]`` when unset. Strips whitespace and drops empties.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ["Sample Collected Date"]
    if isinstance(raw, str):
        return [raw.strip()]
    if isinstance(raw, list):
        cleaned = [str(x).strip() for x in raw if str(x).strip()]
        if not cleaned:
            raise ValueError(
                "data.case_parse.date_column list is empty after stripping — "
                "specify at least one candidate column name"
            )
        return cleaned
    raise ValueError(
        f"data.case_parse.date_column must be a string or list of strings; got {type(raw).__name__}"
    )


@dataclass(frozen=True)
class PrepCaseParseConfig:
    region_types: list[str]
    date_start: str
    date_end: str  # empty → no end date filter applied
    # One or more candidate column names for the case date. The parser tries
    # each in order and uses the first one where at least some rows parse as
    # dates. Configs may pass a single string (back-compat) or a list.
    # Post-``from_raw`` this is normalised to a list.
    date_column: list[str]
    lgd_code_column: (
        str  # if set, use this column for LGD code → region_id (skips spatial join)
    )
    lat_column: str  # latitude column name for spatial join fallback
    lon_column: str  # longitude column name for spatial join fallback
    region_id_column: (
        str
        # If set and present in the file, trust this column as the resolved
        # region_id verbatim — skips LGD / geocode / spatial join. Useful when
        # an upstream system (e.g. the dashboard) already attached region_ids
        # to every row. Values must match the ``<region_type>_<...>`` prefix.
    )
    header_row: (
        int  # 0-indexed row containing column headers (banner-row exports use 1)
    )
    filters: list[
        dict[str, Any]
    ]  # [{column: str, values: [str, ...]}, ...]; AND across entries, OR within values
    geocoding: PrepGeocodingConfig  # opt-in geocode → PIP resolver (default disabled)

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> PrepCaseParseConfig:
        rt_raw = raw.get("region_types", [])
        if not isinstance(rt_raw, list) or not rt_raw:
            raise ValueError(
                "data.case_parse.region_types must be a non-empty list of strings"
            )
        region_types = [str(x).strip() for x in rt_raw]

        def _s(v: Any) -> str:
            return "" if v is None else str(v).strip()

        raw_filters = raw.get("filters", [])
        filters: list[dict[str, Any]] = []
        for entry in raw_filters:
            col = _s(entry.get("column", ""))
            vals_raw = entry.get("values", [])
            vals = [
                str(v) for v in (vals_raw if isinstance(vals_raw, list) else [vals_raw])
            ]
            if col and vals:
                filters.append({"column": col, "values": vals})

        geocoding = PrepGeocodingConfig.from_raw(raw.get("geocoding"))
        if geocoding.enabled and not geocoding.address_fields:
            raise ValueError(
                "data.case_parse.geocoding.enabled=true requires address_fields to be "
                "a non-empty list of column names"
            )

        return cls(
            region_types=region_types,
            date_start=_s(raw.get("date_start", "")),
            date_end=_s(raw.get("date_end", "")),
            date_column=_normalise_date_column(raw.get("date_column")),
            lgd_code_column=_s(raw.get("lgd_code_column", "")),
            lat_column=_s(raw.get("lat_column", "Latitude")) or "Latitude",
            lon_column=_s(raw.get("lon_column", "Longitude")) or "Longitude",
            region_id_column=_s(raw.get("region_id_column", "")),
            header_row=int(raw.get("header_row", 0)),
            filters=filters,
            geocoding=geocoding,
        )


# ---------------------------------------------------------------------------
# Weather download
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrepWeatherDownloadConfig:
    enabled: bool
    source_mode: str
    source_backend: str
    source_path: str
    cache_enabled: bool
    cache_dir: str
    cache_strategy: str
    filesystem_base_path: str
    s3_bucket: str
    s3_prefix: str
    source_storage: str
    source_prefix: str
    source_paths: list[str]
    dest_relpath: str
    netcdf_cache_path: str
    parsed_output_path: str
    cds_variables: list[str]
    region_bounds: list[float] | None
    region_type: str
    w_params: list[str]
    threshold_km: float
    bounds_resolution_deg: float
    start_date: str
    end_date: str
    temperature_unit: str  # unit the source returns: "celsius" | "kelvin"
    precipitation_unit: str  # unit the source returns: "mm" | "m"
    # Force-refetch the last N days of past months on every run. Catches late
    # upstream revisions (ERA5 publishes with a ~5-day lag and later corrects,
    # dashboard re-imports on the same cadence). Set 0 to disable.
    backfill_days: int = 7

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> PrepWeatherDownloadConfig:
        bounds_raw = raw.get("region_bounds")
        bounds: list[float] | None = None
        if isinstance(bounds_raw, list):
            bounds = [float(b) for b in bounds_raw]
        return cls(
            enabled=bool(raw.get("enabled", False)),
            source_mode=raw.get("source_mode", "filesystem"),
            source_backend=str(raw.get("source_backend", "filesystem")).strip().lower()
            or "filesystem",
            source_path=str(raw.get("source_path", "")).strip(),
            cache_enabled=bool(raw.get("cache_enabled", True)),
            cache_dir=str(raw.get("cache_dir", "./cache/raw_weather")).strip(),
            cache_strategy=str(raw.get("cache_strategy", "local_first")).strip()
            or "local_first",
            filesystem_base_path=str(raw.get("filesystem_base_path", "")).strip(),
            s3_bucket=str(raw.get("s3_bucket", "")).strip(),
            s3_prefix=str(raw.get("s3_prefix", "")).strip(),
            source_storage=raw.get("source_storage", ""),
            source_prefix=raw.get("source_prefix", ""),
            source_paths=list(raw.get("source_paths", [])),
            dest_relpath=raw.get("dest_relpath", "datasets/raw_weather_data"),
            netcdf_cache_path=str(raw.get("netcdf_cache_path", "")).strip(),
            parsed_output_path=str(raw.get("parsed_output_path", "")).strip(),
            cds_variables=[
                str(v).strip() for v in raw.get("cds_variables", []) if str(v).strip()
            ],
            region_bounds=bounds,
            region_type=raw.get("region_type", "district"),
            w_params=list(raw.get("w_params", ["t2m", "d2m", "tp"])),
            threshold_km=float(raw.get("threshold_km", 25.0)),
            bounds_resolution_deg=float(raw.get("bounds_resolution_deg", 0.1)),
            start_date=raw.get("start_date", "2015-01-01"),
            end_date=raw.get("end_date", ""),
            temperature_unit=str(raw.get("temperature_unit", "celsius")).strip().lower()
            or "celsius",
            precipitation_unit=str(raw.get("precipitation_unit", "mm")).strip().lower()
            or "mm",
            backfill_days=int(raw.get("backfill_days", 7)),
        )


# ---------------------------------------------------------------------------
# Weather parse (daily aggregation only — no rolling, no sampling)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrepWeatherParseConfig:
    region_type: str
    weather_variables: list[str]
    daily_agg: list[dict[str, str]]

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> PrepWeatherParseConfig:
        region_type = str(raw.get("region_type", "district")).strip()
        weather_variables = list(
            raw.get(
                "weather_variables",
                ["2mTemperature", "totalPrecipitation", "2mDewpointTemperature"],
            )
        )
        default_daily = [
            {"name": "2mTemperature", "op": "mean"},
            {"name": "2mDewpointTemperature", "op": "mean"},
            {"name": "totalPrecipitation", "op": "sum"},
        ]
        daily_agg = list(raw.get("daily_agg", default_daily))
        return cls(
            region_type=region_type,
            weather_variables=weather_variables,
            daily_agg=daily_agg,
        )


# ---------------------------------------------------------------------------
# CDS source (env-backed, same pattern as dengue pipeline)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrepCDSConfig:
    dataset: str
    variables: list[str]
    cds_url: str
    cds_key: str
    cache_path: str
    parsed_output_path: str

    DEFAULT_DATASET = "reanalysis-era5-land"
    DEFAULT_VARIABLES = [
        "2m_temperature",
        "2m_dewpoint_temperature",
        "total_precipitation",
    ]
    DEFAULT_CACHE_PATH = "datasets/netcdf"
    DEFAULT_PARSED_OUTPUT_PATH = "datasets/parsednetcdf"

    @classmethod
    def from_env(cls) -> PrepCDSConfig:
        dataset = os.getenv("GBA_CDS_DATASET", "").strip() or cls.DEFAULT_DATASET
        variables_raw = os.getenv("GBA_CDS_VARIABLES", "").strip()
        variables = (
            [v.strip() for v in variables_raw.split(",") if v.strip()]
            if variables_raw
            else list(cls.DEFAULT_VARIABLES)
        )
        return cls(
            dataset=dataset,
            variables=variables,
            cds_url=os.getenv("CDS_API_URL", "").strip(),
            cds_key=os.getenv("CDS_API_KEY", "").strip(),
            cache_path=os.getenv("GBA_CDS_CACHE_PATH", "").strip()
            or cls.DEFAULT_CACHE_PATH,
            parsed_output_path=os.getenv("GBA_CDS_PARSED_OUTPUT_PATH", "").strip()
            or cls.DEFAULT_PARSED_OUTPUT_PATH,
        )
