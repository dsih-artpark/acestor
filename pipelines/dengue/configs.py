"""Typed configuration dataclasses for each step in the dengue pipeline.

Each dataclass has a ``from_raw`` classmethod that validates and provides
defaults, so the step's ``run()`` never touches raw dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    # Column rename map applied when writing agg_daily / agg_Ndays intermediate files
    intermediate_col_rename: dict[str, str]
    write_agg_daily: bool
    write_agg_ndays: bool

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

        default_rename: dict[str, str] = {
            "2mTemperature_max": "t2m_max",
            "2mTemperature_min": "t2m_min",
            "2mTemperature": "t2m_mean",
            "2mDewpointTemperature": "d2m_mean",
            "totalPrecipitation": "tp_sum",
        }
        intermediate_col_rename = dict(
            raw.get("intermediate_col_rename", default_rename)
        )

        return cls(
            region_type=region_type,
            weather_variables=weather_variables,
            daily_agg=daily_agg,
            rolling_agg=rolling_agg,
            rolling_n_days=rolling_n_days,
            sampling_rate=sampling_rate,
            intermediate_col_rename=intermediate_col_rename,
            write_agg_daily=bool(raw.get("write_agg_daily", True)),
            write_agg_ndays=bool(raw.get("write_agg_ndays", True)),
        )


# ---------------------------------------------------------------------------
# Prepared data (read by load_prepared_case_data / load_prepared_weather_data)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreparedDataConfig:
    """Location of prepared_data written by the dengue_prep pipeline (or dropped manually)."""

    base_dir: str  # e.g. "prepared_data"
    region_type: str  # e.g. "district"

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> PreparedDataConfig:
        return cls(
            base_dir=str(raw.get("base_dir", "prepared_data")).strip()
            or "prepared_data",
            region_type=str(raw.get("region_type", "district")).strip() or "district",
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
    list_alpha: list[float] = field(default_factory=lambda: [1.0, 2.0])
    methods: list[str] = field(default_factory=lambda: ["historical", "prev_nweeks"])
    classification_method: str = "who"  # "who" | "icmr"
    # weighted_baseline knobs
    recent_weeks: int = 4
    sd_window_weeks: int = 8
    weight_recent: float = 0.7
    weight_seasonal: float = 0.3

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> ThresholdsConfig:
        ny = raw.get("historical_n_years")
        return cls(
            region_type=raw.get("region_type", "zone"),
            n_weeks=int(raw.get("n_weeks", 4)),
            historical_n_years=int(ny) if ny is not None else 4,
            excluded_years=[int(y) for y in raw.get("excluded_years", [2020, 2021])],
            included_years=[int(y) for y in raw.get("included_years", [])],
            list_alpha=[float(a) for a in raw.get("list_alpha", [1.0, 2.0])],
            methods=list(raw.get("methods", ["historical", "prev_nweeks"])),
            classification_method=str(raw.get("classification_method", "who"))
            .strip()
            .lower()
            or "who",
            recent_weeks=int(raw.get("recent_weeks", 4)),
            sd_window_weeks=int(raw.get("sd_window_weeks", 8)),
            weight_recent=float(raw.get("weight_recent", 0.7)),
            weight_seasonal=float(raw.get("weight_seasonal", 0.3)),
        )


def resolve_threshold_config(
    base: ThresholdsConfig, raw_overrides: dict
) -> ThresholdsConfig:
    """Shallow-merge per-method YAML overrides onto a base ThresholdsConfig.

    region_type, methods, and classification_method are always inherited from base.
    Everything else is overridable per-method.
    """

    def _override(key, cast, default):
        return cast(raw_overrides[key]) if key in raw_overrides else default

    return ThresholdsConfig(
        region_type=base.region_type,
        methods=base.methods,
        classification_method=base.classification_method,
        n_weeks=_override("n_weeks", int, base.n_weeks),
        historical_n_years=_override(
            "historical_n_years",
            lambda v: int(v) if v is not None else None,
            base.historical_n_years,
        ),
        excluded_years=_override(
            "excluded_years", lambda v: [int(y) for y in v], list(base.excluded_years)
        ),
        included_years=_override(
            "included_years", lambda v: [int(y) for y in v], list(base.included_years)
        ),
        recent_weeks=_override("recent_weeks", int, base.recent_weeks),
        sd_window_weeks=_override("sd_window_weeks", int, base.sd_window_weeks),
        weight_recent=_override("weight_recent", float, base.weight_recent),
        weight_seasonal=_override("weight_seasonal", float, base.weight_seasonal),
    )


# ---------------------------------------------------------------------------
# Train & predict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrainPredictConfig:
    spatial_res: str
    data_features: list[str]
    lag_temp: list[int]
    lag_rainfall: list[int]
    lag_humidity: list[int]
    lag_cases: list[int]
    years_to_exclude: list[int]
    years_to_include: list[int]  # empty = no restriction; non-empty = only these years
    models: list[str]
    ensemble: str  # registered ensemble strategy name, or "none"
    output: str  # "ensemble" | "per_model" | "both"
    tune: bool  # False = use cache; True = force Optuna retune
    n_trials: int  # Optuna trials when tuning runs
    debug: bool  # True = save intermediate CSVs to artifacts/debug/<model>/

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> TrainPredictConfig:
        lag = raw.get("lag", {})
        ensemble = str(raw.get("ensemble", "mean")).strip() or "mean"
        output = str(raw.get("output", "ensemble")).strip() or "ensemble"

        if output not in ("ensemble", "per_model", "both"):
            raise ValueError(
                f"model.output must be one of 'ensemble' | 'per_model' | 'both', "
                f"got {output!r}"
            )
        if ensemble == "none" and output == "ensemble":
            raise ValueError(
                "model.ensemble='none' is incompatible with output='ensemble' "
                "(nothing would be combined). Use output='per_model' or 'both'."
            )

        return cls(
            spatial_res=raw.get("spatial_res", "zone"),
            data_features=list(raw.get("data_features", [])),
            lag_temp=list(lag.get("lag_temp", [12])),
            lag_rainfall=list(lag.get("lag_rainfall", [4])),
            lag_humidity=list(lag.get("lag_humidity", [4])),
            lag_cases=list(lag.get("lag_cases", [])),
            years_to_exclude=[
                int(y) for y in raw.get("years_to_exclude", [2020, 2021])
            ],
            years_to_include=[int(y) for y in raw.get("years_to_include", [])],
            models=list(raw.get("models", ["nbr", "tse"])),
            ensemble=ensemble,
            output=output,
            tune=bool(raw.get("tune", False)),
            n_trials=int(raw.get("n_trials", 50)),
            debug=bool(raw.get("debug", False)),
        )


def resolve_model_config(
    base: TrainPredictConfig, raw_overrides: Mapping[str, Any]
) -> TrainPredictConfig:
    """Merge per-model overrides onto a base TrainPredictConfig.

    Keys present in *raw_overrides* win over the base. The ``lag`` sub-dict
    is merged key-by-key so that specifying only ``lag_temp`` leaves
    ``lag_rainfall`` and ``lag_humidity`` at their base values.
    """
    if not raw_overrides:
        return base

    base_lag = {
        "lag_temp": list(base.lag_temp),
        "lag_rainfall": list(base.lag_rainfall),
        "lag_humidity": list(base.lag_humidity),
        "lag_cases": list(base.lag_cases),
    }
    override_lag = dict(raw_overrides.get("lag", {}))
    merged_lag = {**base_lag, **override_lag}

    merged: dict[str, Any] = {
        "spatial_res": base.spatial_res,
        "models": list(base.models),
        "ensemble": base.ensemble,
        "output": base.output,
        "lag": merged_lag,
        "data_features": list(base.data_features),
        "years_to_exclude": list(base.years_to_exclude),
        "years_to_include": list(base.years_to_include),
        "tune": base.tune,
        "n_trials": base.n_trials,
        "debug": base.debug,
    }

    for key in (
        "data_features",
        "years_to_exclude",
        "years_to_include",
        "tune",
        "n_trials",
        "debug",
    ):
        if key in raw_overrides:
            merged[key] = raw_overrides[key]

    return TrainPredictConfig.from_raw(merged)


# ---------------------------------------------------------------------------
# Assess thresholds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssessConfig:
    total_regions_by_type: dict[str, int]  # {region_type: total_count}

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> AssessConfig:
        totals: dict[str, int] = {}
        for region in ("corp", "zone", "ward", "district", "subdistrict"):
            key = f"total_{region}_regions"
            if key in raw:
                totals[region] = int(raw[key])
        return cls(total_regions_by_type=totals)


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
    primary: (
        str  # which prediction CSV feeds maps + report; "ensemble" or any model key
    )

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

        primary_caption = (
            str(raw.get("caption_primary") or "").strip() or "corporations"
        )
        secondary = str(raw.get("caption_secondary") or "").strip() or "zones"

        prefix = str(raw.get("bundle_prefix") or "").strip() or "Report"
        primary = str(raw.get("primary") or "").strip() or "ensemble"

        return cls(
            output_dir=str(raw.get("output_dir", "reports")),
            compile_pdf=bool(raw.get("compile_pdf", False)),
            document_title=doc,
            caption_primary=primary_caption,
            caption_secondary=secondary,
            bundle_prefix=prefix,
            primary=primary,
        )
