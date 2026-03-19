"""Typed configuration dataclasses for each step in the GBA dengue pipeline.

Each dataclass has a ``from_raw`` classmethod that validates and provides
defaults, so the step's ``run()`` never touches raw dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def _section(config: Mapping[str, Any], path: str) -> Mapping[str, Any]:
    """Walk a dotted path into a nested mapping, returning {} on miss."""
    current: Any = config
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return {}
        current = current.get(part)
        if current is None:
            return {}
    return current if isinstance(current, Mapping) else {}


# ---------------------------------------------------------------------------
# Case download
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseDownloadConfig:
    enabled: bool
    source_storage: str
    source_prefix: str
    source_paths: list[str]
    dest_relpath: str

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> CaseDownloadConfig:
        return cls(
            enabled=bool(raw.get("enabled", False)),
            source_storage=raw.get("source_storage", ""),
            source_prefix=raw.get("source_prefix", ""),
            source_paths=list(raw.get("source_paths", [])),
            dest_relpath=raw.get("dest_relpath", "datasets/raw_linelist_data/linelist"),
        )


# ---------------------------------------------------------------------------
# Case parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseParseConfig:
    """Case parse settings — all fields must be set under ``data.case_parse`` in YAML (no hidden defaults).

    Order of ``region_types`` matters for downstream steps that consume a single case file: the **last**
    entry is used as ``ParseCaseDataResult.sampled_csv_path`` / ``.region_type`` (same order as SOT:
    ``corp`` then ``zone`` → zone drives cutoffs). All outputs are still in ``sampled_by_region_type``.
    """

    region_types: list[str]
    geojson_folder: str  # base geojson folder (e.g. "geojsons/geojsons_GBA")
    date_start: str
    date_end: (
        str  # inclusive; empty string → step uses "today" (see parse_nonstd_case_data)
    )

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> CaseParseConfig:
        required = ("region_types", "geojson_folder", "date_start", "date_end")
        missing = [k for k in required if k not in raw]
        if missing:
            raise ValueError(
                f"data.case_parse is missing required keys (no defaults): {missing}. "
                f"Define all of {list(required)} in your pipeline YAML."
            )

        def _as_str(v: Any) -> str:
            if v is None:
                return ""
            return str(v).strip()

        rt_raw = raw["region_types"]
        if not isinstance(rt_raw, list) or not rt_raw:
            raise ValueError(
                "data.case_parse.region_types must be a non-empty list of strings"
            )
        region_types = [_as_str(x) for x in rt_raw]
        if not all(region_types):
            raise ValueError(
                "data.case_parse.region_types entries must be non-empty strings"
            )
        if len(set(region_types)) != len(region_types):
            raise ValueError(
                f"data.case_parse.region_types must not contain duplicates: {region_types}"
            )

        geojson_folder = _as_str(raw["geojson_folder"])
        if not geojson_folder:
            raise ValueError("data.case_parse.geojson_folder must be non-empty")

        return cls(
            region_types=region_types,
            geojson_folder=geojson_folder,
            date_start=_as_str(raw["date_start"]),
            date_end=_as_str(raw["date_end"]),
        )


# ---------------------------------------------------------------------------
# Weather download
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WeatherDownloadConfig:
    enabled: bool
    source_mode: str  # "filesystem" or "cds"
    # filesystem mode
    source_storage: str
    source_prefix: str
    source_paths: list[str]
    dest_relpath: str
    # cds mode
    cds_dataset: str
    cds_variables: list[str]
    region_bounds: list[float] | None  # [N, W, S, E] or None for auto from geojson
    geojson_folder: str  # base geojson folder (e.g. "geojsons/geojsons_GBA")
    region_type: str  # "zone", "corp", "ward" — subfolder under geojson_folder
    cache_path: str  # where raw NetCDFs are cached
    parsed_output_path: str  # where parsed per-region CSVs are written
    w_params: list[
        str
    ]  # NetCDF variable short names for parsing (e.g. ["t2m", "d2m", "tp"])
    threshold_km: float  # max distance from region boundary for grid-point filtering
    bounds_resolution_deg: (
        float  # snap auto geojson bounds to this grid (e.g. 0.25 ERA5, ~0.1 ERA5-Land)
    )
    start_date: str  # inclusive download start (YYYY-MM-DD)
    end_date: str  # inclusive download end (YYYY-MM-DD); empty → run_date
    # cds credentials (override ~/.cdsapirc)
    cds_url: str
    cds_key: str

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> WeatherDownloadConfig:
        bounds_raw = raw.get("region_bounds")
        bounds: list[float] | None = None
        if isinstance(bounds_raw, list):
            bounds = [float(b) for b in bounds_raw]

        return cls(
            enabled=bool(raw.get("enabled", False)),
            source_mode=raw.get("source_mode", "filesystem"),
            source_storage=raw.get("source_storage", ""),
            source_prefix=raw.get("source_prefix", ""),
            source_paths=list(raw.get("source_paths", [])),
            dest_relpath=raw.get("dest_relpath", "datasets/raw_weather_data"),
            cds_dataset=raw.get("cds_dataset", "reanalysis-era5-land"),
            cds_variables=list(
                raw.get(
                    "cds_variables",
                    [
                        "2m_temperature",
                        "2m_dewpoint_temperature",
                        "total_precipitation",
                    ],
                )
            ),
            region_bounds=bounds,
            geojson_folder=raw.get("geojson_folder", ""),
            region_type=raw.get("region_type", "zone"),
            cache_path=raw.get("cache_path", "./datasets/netcdf"),
            parsed_output_path=raw.get("parsed_output_path", "./datasets/parsednetcdf"),
            w_params=list(raw.get("w_params", ["t2m", "d2m", "tp"])),
            threshold_km=float(raw.get("threshold_km", 25.0)),
            bounds_resolution_deg=float(raw.get("bounds_resolution_deg", 0.1)),
            start_date=raw.get("start_date", "2015-01-01"),
            end_date=raw.get("end_date", ""),
            cds_url=raw.get("cds_url", ""),
            cds_key=raw.get("cds_key", ""),
        )


# ---------------------------------------------------------------------------
# Weather parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WeatherParseConfig:
    region_type: str
    geojson_path: str
    weather_variables: list[str]

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> WeatherParseConfig:
        return cls(
            region_type=raw.get("region_type", "zone"),
            geojson_path=raw.get("geojson_path", ""),
            weather_variables=list(
                raw.get(
                    "weather_variables",
                    [
                        "2mTemperature",
                        "totalPrecipitation",
                        "2mDewpointTemperature",
                    ],
                )
            ),
        )


# ---------------------------------------------------------------------------
# Cutoff dates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CutoffConfig:
    case_min_regions: int
    weather_min_regions: int

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> CutoffConfig:
        return cls(
            case_min_regions=int(raw.get("case_min_regions", 2)),
            weather_min_regions=int(raw.get("weather_min_regions", 5)),
        )


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThresholdsConfig:
    region_type: str
    n_weeks: int
    historical_n_years: int | None
    excluded_years: list[int]

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> ThresholdsConfig:
        ny = raw.get("historical_n_years")
        return cls(
            region_type=raw.get("region_type", "zone"),
            n_weeks=int(raw.get("n_weeks", 4)),
            historical_n_years=int(ny) if ny is not None else None,
            excluded_years=[int(y) for y in raw.get("excluded_years", [2020, 2021])],
        )


# ---------------------------------------------------------------------------
# Train & predict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrainPredictConfig:
    spatial_res: str
    tempo_res: str
    data_features: list[str]
    lag_temp: list[int]
    lag_rf: list[int]
    years_to_exclude: list[int]
    list_alpha: list[float]

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> TrainPredictConfig:
        lag = raw.get("lag", {})
        return cls(
            spatial_res=raw.get("spatial_res", "zone"),
            tempo_res=raw.get("tempo_res", "W-MON"),
            data_features=list(
                raw.get(
                    "data_features",
                    [
                        "case",
                        "recordDate",
                        "recordYear",
                        "recordMonth",
                        "ISOWeek",
                        "2mTemperature",
                        "totalPrecipitation",
                        "2mDewpointTemperature",
                    ],
                )
            ),
            lag_temp=list(lag.get("lag_temp", [12])),
            lag_rf=list(lag.get("lag_rf", [4])),
            years_to_exclude=[
                int(y) for y in raw.get("years_to_exclude", [2020, 2021])
            ],
            list_alpha=[float(a) for a in raw.get("list_alpha", [1.0, 2.0])],
        )


# ---------------------------------------------------------------------------
# Assess thresholds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssessConfig:
    total_corp_regions: int
    total_zone_regions: int

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> AssessConfig:
        return cls(
            total_corp_regions=int(raw.get("total_corp_regions", 5)),
            total_zone_regions=int(raw.get("total_zone_regions", 10)),
        )


# ---------------------------------------------------------------------------
# Maps
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MapsConfig:
    geojson_base: str
    output_dir: str

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> MapsConfig:
        return cls(
            geojson_base=raw.get("geojson_base", "geojsons/geojsons_GBA"),
            output_dir=raw.get("output_dir", "plots"),
        )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportConfig:
    output_dir: str

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> ReportConfig:
        return cls(
            output_dir=raw.get("output_dir", "reports"),
        )
