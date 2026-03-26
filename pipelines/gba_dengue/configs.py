"""Typed configuration dataclasses for each step in the dengue pipeline.

Each dataclass has a ``from_raw`` classmethod that validates and provides
defaults, so the step's ``run()`` never touches raw dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
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


def _env(key: str, default: str) -> str:
    """Small helper for environment-backed defaults."""
    v = os.getenv(key, "").strip()
    return v or default


def _env_any(keys: tuple[str, ...], default: str) -> str:
    """Pick the first non-empty env var from `keys`."""
    for k in keys:
        v = os.getenv(k, "").strip()
        if v:
            return v
    return default


# ---------------------------------------------------------------------------
# Case download
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseDownloadConfig:
    enabled: bool
    source_backend: str  # "filesystem" or "s3"
    source_path: str  # filesystem path or s3://bucket/prefix
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
    def from_raw(cls, raw: Mapping[str, Any]) -> CaseDownloadConfig:
        return cls(
            enabled=bool(raw.get("enabled", False)),
            source_backend=str(raw.get("source_backend", "filesystem")).strip().lower()
            or "filesystem",
            source_path=str(raw.get("source_path", "")).strip(),
            cache_enabled=bool(raw.get("cache_enabled", True)),
            cache_dir=str(raw.get("cache_dir", "./cache/raw_case")).strip(),
            cache_strategy=str(raw.get("cache_strategy", "local_first")).strip()
            or "local_first",
            filesystem_base_path=str(
                raw.get(
                    "filesystem_base_path",
                    "./datasets/raw_linelist_data/Bengaluru_IHIP_linelist",
                )
            ).strip(),
            s3_bucket=str(raw.get("s3_bucket", "standardized-bucket")).strip(),
            s3_prefix=str(
                raw.get("s3_prefix", "EP0005DS0068-Bengaluru_IHIP_Dengue_LL/")
            ).strip(),
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
    """Case parse settings.

    ``region_types``, ``date_start``, and ``date_end`` come from pipeline YAML.
    GeoJSON location is resolved via the pipeline-local geojson source.

    Order of ``region_types`` matters for downstream steps that consume a single case file: the **last**
    entry is used as ``ParseCaseDataResult.sampled_csv_path`` / ``.region_type`` (same order as SOT:
    ``corp`` then ``zone`` → zone drives cutoffs). All outputs are still in ``sampled_by_region_type``.
    """

    region_types: list[str]
    date_start: str
    date_end: str  # inclusive; empty string → step uses "today" (see parse_case_data)

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> CaseParseConfig:
        required = ("region_types", "date_start", "date_end")
        missing = [k for k in required if k not in raw]
        if missing:
            raise ValueError(
                "data.case_parse is missing required keys: "
                f"{missing}. Define all of {list(required)}."
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

        return cls(
            region_types=region_types,
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
    source_backend: str  # "filesystem" or "s3"
    source_path: str  # filesystem path or s3://bucket/prefix
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
    # local netcdf cache (used by both "filesystem" and "cds" modes)
    netcdf_cache_path: str  # path to local zip cache (e.g. ./datasets/netcdf); empty → use CDSSource default
    parsed_output_path: (
        str  # where parsed per-region CSVs are written; empty → use CDSSource default
    )
    cds_variables: list[
        str
    ]  # CDS API variable names; empty → use GBA_CDS_VARIABLES env / CDSSource default
    # cds mode
    region_bounds: list[float] | None  # [N, W, S, E] or None for auto from geojson
    region_type: str  # "zone", "corp", "ward" — subfolder under geojson_folder
    w_params: list[
        str
    ]  # NetCDF variable short names for parsing (e.g. ["t2m", "d2m", "tp"])
    threshold_km: float  # max distance from region boundary for grid-point filtering
    bounds_resolution_deg: (
        float  # snap auto geojson bounds to this grid (e.g. 0.25 ERA5, ~0.1 ERA5-Land)
    )
    start_date: str  # inclusive download start (YYYY-MM-DD)
    end_date: str  # inclusive download end (YYYY-MM-DD); empty → run_date

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> WeatherDownloadConfig:
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
            region_type=raw.get("region_type", "zone"),
            w_params=list(raw.get("w_params", ["t2m", "d2m", "tp"])),
            threshold_km=float(raw.get("threshold_km", 25.0)),
            bounds_resolution_deg=float(raw.get("bounds_resolution_deg", 0.1)),
            start_date=raw.get("start_date", "2015-01-01"),
            end_date=raw.get("end_date", ""),
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
    included_years: list[int]  # empty = no restriction; non-empty = only these years

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> ThresholdsConfig:
        ny = raw.get("historical_n_years")
        return cls(
            region_type=raw.get("region_type", "zone"),
            n_weeks=int(raw.get("n_weeks", 4)),
            historical_n_years=int(ny) if ny is not None else None,
            excluded_years=[int(y) for y in raw.get("excluded_years", [2020, 2021])],
            included_years=[int(y) for y in raw.get("included_years", [])],
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
    years_to_include: list[int]  # empty = no restriction; non-empty = only these years
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
            years_to_include=[int(y) for y in raw.get("years_to_include", [])],
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
    output_dir: str
    figure_title: str  # map suptitle (first line); second line is the prediction date

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> MapsConfig:
        return cls(
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
    caption_primary: str
    caption_secondary: str
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

        primary = str(raw.get("caption_primary") or "").strip() or "corporations"
        secondary = str(raw.get("caption_secondary") or "").strip() or "zones"

        prefix = str(raw.get("bundle_prefix") or "").strip() or "Report"

        return cls(
            output_dir=str(raw.get("output_dir", "reports")),
            compile_pdf=bool(raw.get("compile_pdf", False)),
            document_title=doc,
            caption_primary=primary,
            caption_secondary=secondary,
            bundle_prefix=prefix,
        )
