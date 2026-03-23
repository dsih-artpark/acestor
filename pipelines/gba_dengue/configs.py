"""Typed configuration dataclasses for each step in the dengue pipeline.

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
    geojson_folder: str  # base geojson folder (set explicitly in YAML)
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


@dataclass(frozen=True)
class CaseSufficiencyConfig:
    """Early gate after case parse (similar spirit to ``main.py`` can_run_predictions)."""

    enabled: bool
    min_total_rows: int
    min_distinct_regions: int
    min_date_span_days: int
    case_column: str
    # Empty strings → derive from ``ParseCaseDataResult.region_type`` (admin ID columns).
    region_column: str
    date_column: str

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> CaseSufficiencyConfig:
        return cls(
            enabled=bool(raw.get("enabled", True)),
            min_total_rows=int(raw.get("min_total_rows", 30)),
            min_distinct_regions=int(raw.get("min_distinct_regions", 2)),
            min_date_span_days=int(raw.get("min_date_span_days", 14)),
            case_column=str(raw.get("case_column", "case")),
            region_column=str(raw.get("region_column", "")),
            date_column=str(raw.get("date_column", "")),
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
    geojson_folder: str  # base geojson folder (set explicitly in YAML)
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
    """Weather parsing and aggregation settings.

    All fields are driven from ``data.weather_parse`` in the pipeline YAML.  The
    defaults match the current hard-coded behaviour so existing configs keep
    working.
    """

    region_type: str
    geojson_path: str
    weather_variables: list[str]
    # Optional config-driven aggregation knobs
    daily_agg: list[dict[str, str]]
    rolling_agg: list[dict[str, str]]
    rolling_n_days: int
    sampling_rate: int

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> WeatherParseConfig:
        def _as_str(v: Any) -> str:
            if v is None:
                return ""
            return str(v).strip()

        region_type = _as_str(raw.get("region_type", "zone"))
        geojson_path = _as_str(raw.get("geojson_path", ""))
        weather_variables = list(
            raw.get(
                "weather_variables",
                [
                    "2mTemperature",
                    "totalPrecipitation",
                    "2mDewpointTemperature",
                ],
            )
        )

        # Defaults mirror existing lib.weather behaviour:
        # - 2mTemperature / 2mDewpointTemperature: mean
        # - totalPrecipitation: sum
        default_daily = [
            {"name": "2mTemperature", "op": "mean"},
            {"name": "2mDewpointTemperature", "op": "mean"},
            {"name": "totalPrecipitation", "op": "sum"},
        ]
        default_rolling = default_daily

        daily_agg = list(raw.get("daily_agg", default_daily))
        rolling_agg = list(raw.get("rolling_agg", default_rolling))

        rolling_n_days = int(raw.get("rolling_n_days", 7))
        sampling_rate = int(raw.get("sampling_rate", 7))

        return cls(
            region_type=region_type,
            geojson_path=geojson_path,
            weather_variables=weather_variables,
            daily_agg=daily_agg,
            rolling_agg=rolling_agg,
            rolling_n_days=rolling_n_days,
            sampling_rate=sampling_rate,
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
    figure_title: str  # map suptitle (first line); second line is the prediction date

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> MapsConfig:
        return cls(
            geojson_base=raw.get("geojson_base", "geojsons"),
            output_dir=raw.get("output_dir", "plots"),
            figure_title=str(
                raw.get("figure_title") or "Dengue risk map",
            ).strip()
            or "Dengue risk map",
        )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportConfig:
    """Report step: JSON + summary ``.tex`` + LaTeX bundle zip always; PDF only if ``compile_pdf``."""

    output_dir: str
    compile_pdf: bool
    document_title: str
    caption_corp_scope: str
    caption_zone_scope: str
    bundle_prefix: str

    @classmethod
    def from_raw(
        cls,
        raw: Mapping[str, Any],
        *,
        pipeline: Mapping[str, Any] | None = None,
    ) -> ReportConfig:
        pipe = pipeline or {}

        doc = str(raw.get("document_title") or "").strip()
        if not doc:
            hint = str(pipe.get("title") or pipe.get("display_name") or "").strip()
            doc = (
                f"{hint} — summary report"
                if hint
                else "Dengue intelligence — summary report"
            )

        corp = str(raw.get("caption_corp_scope") or "").strip()
        if not corp:
            corp = "municipal corporations"
        zone = str(raw.get("caption_zone_scope") or "").strip()
        if not zone:
            zone = "planning zones"

        prefix = str(raw.get("bundle_prefix") or "").strip() or "Report"

        return cls(
            output_dir=str(raw.get("output_dir", "reports")),
            compile_pdf=bool(raw.get("compile_pdf", False)),
            document_title=doc,
            caption_corp_scope=corp,
            caption_zone_scope=zone,
            bundle_prefix=prefix,
        )
