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
# Case download
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrepCaseDownloadConfig:
    enabled: bool
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

        return cls(
            enabled=bool(raw.get("enabled", False)),
            address_fields=tuple(_list("address_fields")),
            fallback_address_fields=tuple(_list("fallback_address_fields")),
            cache_file=_s(raw.get("cache_file", "./cache/geocode_cache.json"))
            or "./cache/geocode_cache.json",
            extra_stopwords=tuple(_s(w).lower() for w in _list("extra_stopwords")),
            require_address=bool(raw.get("require_address", True)),
            require_validation=bool(raw.get("require_validation", True)),
        )


@dataclass(frozen=True)
class PrepCaseParseConfig:
    region_types: list[str]
    date_start: str
    date_end: str  # empty → no end date filter applied
    date_column: str  # which column in the IHIP file to use as case date
    lgd_code_column: (
        str  # if set, use this column for LGD code → region_id (skips spatial join)
    )
    lat_column: str  # latitude column name for spatial join fallback
    lon_column: str  # longitude column name for spatial join fallback
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
            date_column=_s(raw.get("date_column", "Sample Collected Date"))
            or "Sample Collected Date",
            lgd_code_column=_s(raw.get("lgd_code_column", "")),
            lat_column=_s(raw.get("lat_column", "Latitude")) or "Latitude",
            lon_column=_s(raw.get("lon_column", "Longitude")) or "Longitude",
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
