"""Threshold computation functions.

Translated from GBA ``GenerateThresholds.py`` and ``utils.py``
(``retHistoricalStats``, ``retPrevNWeeksStats``, ``compute_thresholds``).
"""

from __future__ import annotations

import logging
import math
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThresholdContext:
    """Params a threshold method can read. Mirrors ThresholdsConfig fields."""

    n_weeks: int = 4
    historical_n_years: int | None = None
    excluded_years: list[int] = field(default_factory=list)
    included_years: list[int] = field(default_factory=list)
    # weighted_baseline knobs
    recent_weeks: int = 4
    sd_window_weeks: int = 8
    weight_recent: float = 0.7
    weight_seasonal: float = 0.3


_THRESHOLD_REGISTRY: dict[str, Callable] = {}


def register(name: str):
    """Function decorator that adds the function to _THRESHOLD_REGISTRY."""

    def decorator(fn: Callable) -> Callable:
        _THRESHOLD_REGISTRY[name] = fn
        return fn

    return decorator


def get_threshold_method(name: str) -> Callable:
    if name not in _THRESHOLD_REGISTRY:
        raise KeyError(
            f"Unknown threshold method '{name}'. Available: {sorted(_THRESHOLD_REGISTRY)}"
        )
    return _THRESHOLD_REGISTRY[name]


def align_dates_all_regions(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure every region has a row for every date in the global range, filling gaps with 0."""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    full_range = pd.date_range(start=df["date"].min(), end=df["date"].max())

    def _fill(group: pd.DataFrame) -> pd.DataFrame:
        group = group.set_index("date").reindex(full_range).reset_index()
        group.rename(columns={"index": "date"}, inplace=True)
        n_missing = group["case"].isna().sum()
        if n_missing:
            region_id = (
                group["region_id"].dropna().iloc[0]
                if group["region_id"].dropna().any()
                else "unknown"
            )
            log.debug(
                "thresholds: region '%s' — filled %d missing date(s) with case=0 "
                "(genuine zeros vs reporting gaps are indistinguishable at this stage)",
                region_id,
                n_missing,
            )
        group["case"] = group["case"].fillna(0)
        group["region_id"] = group["region_id"].ffill().bfill()
        return group

    listcols = ["region_id", "date", "case"]
    return (
        df.groupby("region_id", group_keys=False)[listcols]
        .apply(_fill)
        .reset_index(drop=True)[listcols]
    )


def _inflate_std(df: pd.DataFrame) -> pd.DataFrame:
    """Inflate StdDev to sqrt(Mean) where StdDev=0 and Mean>0 (PRISM-H §4.4)."""
    # PRISM-H §4.4: negative Mean indicates corrupt input — hard error
    neg_mask = df["Mean"] < 0
    if neg_mask.any():
        bad = df[neg_mask][["region_id", "date", "Mean"]].head(5).to_dict("records")
        raise ValueError(
            f"Negative Mean detected — should never occur (PRISM-H §4.4). "
            f"First offending rows: {bad}"
        )
    mask = (df["StdDev"] == 0) & (df["Mean"] > 0)
    df.loc[mask, "StdDev"] = np.sqrt(df.loc[mask, "Mean"])
    return df


def prev_nweeks_threshold_params(
    df: pd.DataFrame,
    n: int = 4,
    k: int = 7,
) -> pd.DataFrame:
    """Compute previous-N-weeks threshold parameters (Mean, StdDev) per region/date."""

    def _process_group(
        group: pd.DataFrame,
        value_col: str,
        stat: str,
        n: int,
        k: int,
        closed: str = "left",
    ) -> pd.Series:
        dates = group["date"]
        values = group[value_col]
        lookup = dict(zip(dates, values))
        # closed="left": range(0, n) — includes current date (SOT default)
        # closed != "left": range(1, n+1) — excludes current, looks back only (SOT closed=None)
        indices = range(n) if closed == "left" else range(1, n + 1)
        target = pd.DataFrame(
            {f"day_{i}": dates - pd.Timedelta(days=k * i) for i in indices}
        )
        target_vals = target.map(lambda d: lookup.get(d, np.nan))
        if stat == "mean":
            return target_vals.mean(axis=1, skipna=False)
        elif stat == "std":
            return target_vals.std(axis=1, skipna=False)
        raise ValueError(f"Unsupported stat: {stat}")

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["region_id", "date"])

    parts = []
    for _, group in df.groupby("region_id"):
        g = group.reset_index(drop=True)
        col = f"Mean_N{n}week_k{k}days"
        # ν: mean of t-1..t-4 (excludes current week per PRISM-H §4.2.2)
        g[col] = _process_group(g, "case", "mean", n, k, closed="right")
        # μ: mean of νₜ, νₜ₋₁, νₜ₋₂ (3 values including current ν)
        g["Mean"] = _process_group(g, col, "mean", 3, k, closed="left")
        # σ: std of νₜ, νₜ₋₁, νₜ₋₂, νₜ₋₃ (4 values per PRISM-H §4.2.2)
        g["StdDev"] = _process_group(g, col, "std", 4, k, closed="left")
        parts.append(g)

    result = pd.concat(parts, ignore_index=True)
    result["threshold_method"] = "previousNweeks"
    result = _inflate_std(result)
    return result[["region_id", "date", "case", "Mean", "StdDev", "threshold_method"]]


def historical_threshold_params(
    df: pd.DataFrame,
    n_years: int | None = None,
    excluded_years: list[int] | None = None,
    included_years: list[int] | None = None,
) -> pd.DataFrame:
    """Compute historical (same month + weekday) threshold parameters."""
    if excluded_years is None:
        excluded_years = []
    if included_years is None:
        included_years = []
    df = deepcopy(df)
    df["date"] = pd.to_datetime(df["date"])
    df["year"] = df["date"].dt.year
    df["month"] = df["date"].dt.month
    df["weekday"] = df["date"].dt.strftime("%a")

    all_years = sorted(df["year"].unique())
    rows = []
    for year in all_years:
        current = df[df["year"] == year].copy()
        cond = df["year"] < year
        if n_years is not None:
            cond = cond & (df["year"] >= year - n_years)
        if included_years:
            cond = cond & (df["year"].isin(included_years))
        if excluded_years:
            cond = cond & (~df["year"].isin(excluded_years))
        past = df[cond]

        if past.empty:
            log.warning(
                "thresholds: year %d has no usable historical past data "
                "(n_years=%s, included_years=%s, excluded_years=%s) → "
                "Mean=NaN, StdDev=NaN for all %d rows — thresholds will be degenerate "
                "and these regions will appear light gray on maps",
                year,
                n_years,
                included_years,
                excluded_years,
                len(current),
            )
            current["Mean"] = np.nan
            current["StdDev"] = np.nan
        else:
            stats = (
                past.groupby(["region_id", "month", "weekday"])["case"]
                .agg(Mean="mean", StdDev="std")
                .reset_index()
            )
            current = current.merge(
                stats, on=["region_id", "month", "weekday"], how="left"
            )
        rows.append(current)

    result = (
        pd.concat(rows, ignore_index=True)
        .sort_values(["region_id", "date"])
        .reset_index(drop=True)
    )
    result["threshold_method"] = "historical"
    result = _inflate_std(result)
    return result[["region_id", "date", "case", "Mean", "StdDev", "threshold_method"]]


@register("prev_nweeks")
def _prev_nweeks_method(df: pd.DataFrame, ctx: ThresholdContext) -> pd.DataFrame:
    return prev_nweeks_threshold_params(df, n=ctx.n_weeks)


@register("historical")
def _historical_method(df: pd.DataFrame, ctx: ThresholdContext) -> pd.DataFrame:
    return historical_threshold_params(
        df,
        n_years=ctx.historical_n_years,
        excluded_years=ctx.excluded_years,
        included_years=ctx.included_years,
    )


def weighted_baseline_threshold_params(
    df: pd.DataFrame,
    recent_weeks: int = 4,
    sd_window_weeks: int = 8,
    weight_recent: float = 0.7,
    weight_seasonal: float = 0.3,
) -> pd.DataFrame:
    """Compute weighted baseline (SOP) threshold parameters per region/date.

    Weighted Mean = weight_recent × mean(last recent_weeks)
                  + weight_seasonal × mean(same weeks, 52 weeks prior)
    StdDev = rolling std over sd_window_weeks recent weeks.

    When seasonal data is absent (cold start), weight falls back to recent only.
    threshold_method label: "weightedBaseline".
    """
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["region_id", "date"])

    parts = []
    for _, group in df.groupby("region_id"):
        g = group.reset_index(drop=True)
        lookup: dict = dict(zip(g["date"], g["case"]))

        means: list = []
        stds: list = []
        for _, row in g.iterrows():
            d = row["date"]

            recent_dates = [d - pd.Timedelta(weeks=i) for i in range(recent_weeks)]
            recent_vals = [lookup.get(rd, np.nan) for rd in recent_dates]
            recent_valid = [v for v in recent_vals if not np.isnan(v)]
            recent_mean = float(np.mean(recent_valid)) if recent_valid else np.nan

            seasonal_dates = [
                d - pd.Timedelta(weeks=52 + i) for i in range(recent_weeks)
            ]
            seasonal_vals = [lookup.get(sd, np.nan) for sd in seasonal_dates]
            seasonal_valid = [v for v in seasonal_vals if not np.isnan(v)]
            seasonal_mean = float(np.mean(seasonal_valid)) if seasonal_valid else np.nan

            if np.isnan(recent_mean) and np.isnan(seasonal_mean):
                wm = np.nan
            elif np.isnan(seasonal_mean):
                wm = recent_mean
            elif np.isnan(recent_mean):
                wm = seasonal_mean
            else:
                wm = weight_recent * recent_mean + weight_seasonal * seasonal_mean

            sd_dates = [d - pd.Timedelta(weeks=i) for i in range(sd_window_weeks)]
            sd_vals = [lookup.get(sd, np.nan) for sd in sd_dates]
            sd_valid = [v for v in sd_vals if not np.isnan(v)]
            sd_val = float(np.std(sd_valid, ddof=1)) if len(sd_valid) > 1 else np.nan

            means.append(wm)
            stds.append(sd_val)

        g["Mean"] = means
        g["StdDev"] = stds
        parts.append(g)

    result = pd.concat(parts, ignore_index=True)
    result["threshold_method"] = "weightedBaseline"
    result = _inflate_std(result)
    return result[["region_id", "date", "case", "Mean", "StdDev", "threshold_method"]]


@register("weighted_baseline")
def _weighted_baseline_method(df: pd.DataFrame, ctx: ThresholdContext) -> pd.DataFrame:
    return weighted_baseline_threshold_params(
        df,
        recent_weeks=ctx.recent_weeks,
        sd_window_weeks=ctx.sd_window_weeks,
        weight_recent=ctx.weight_recent,
        weight_seasonal=ctx.weight_seasonal,
    )


def icmr_quartile_zones(
    df: pd.DataFrame,
    *,
    prediction_col: str = "prediction",
    date_col: str = "startDatePredictedWeek",
) -> pd.DataFrame:
    """Assign ICMR quartile strata (A1–A4) as predictionZone per date.

    Cross-sectional: for each date, distinct predicted case values across all
    regions are ranked descending and divided into 4 equal strata.
    A1 Critical → zone 4, A2 High → zone 3, A3 Caution → zone 2, A4 Low → zone 1.

    Algorithm (ICMR doc / PRISM-H §5.1):
      values_per_stratum = ceil(n_distinct / 4)
      top values_per_stratum → A1, next → A2, next → A3, rest → A4
    """

    def _classify_date(group: pd.DataFrame) -> pd.DataFrame:
        preds = group[prediction_col].values
        distinct = sorted(set(preds), reverse=True)
        n_distinct = len(distinct)

        group = group.copy()

        # PRISM-H §5.4 — insufficient data guard: < 10 total predicted cases
        # across all geographies → set all zones to None ("Insufficient data")
        if group[prediction_col].sum() < 10:
            group["predictionZone"] = pd.NA
            return group

        if n_distinct == 0 or all(v == 0 for v in distinct):
            group["predictionZone"] = 1  # all A4 Low
            return group

        vps = math.ceil(n_distinct / 4)
        zone_map: dict[float, int] = {}
        for i, val in enumerate(distinct):
            stratum = min(i // vps, 3)  # 0=A1, 1=A2, 2=A3, 3=A4
            zone_map[val] = 4 - stratum  # A1→4, A2→3, A3→2, A4→1

        group["predictionZone"] = group[prediction_col].map(zone_map)
        return group

    parts = [_classify_date(group) for _, group in df.groupby(date_col)]
    return pd.concat(parts, ignore_index=True) if parts else df.copy()


def _aggregate_cases_to_weekly(
    case_data: pd.DataFrame,
    *,
    spatial_col: str,
    case_col: str,
) -> pd.DataFrame:
    """Aggregate dated rows to region × ISO year × ISO week.

    Missing weeks remain absent, matching the original endemic-channel code.
    Zero-case weeks are retained when the input explicitly contains a zero row.
    """
    date_col = next(
        (
            column
            for column in ("date", "recordDate", "metadata.primaryDate")
            if column in case_data
        ),
        None,
    )
    if date_col is None:
        if {"iso_year", "iso_week"}.issubset(case_data.columns):
            df = case_data[[spatial_col, "iso_year", "iso_week", case_col]].copy()
            df["iso_year"] = pd.to_numeric(df["iso_year"], errors="coerce")
            df["iso_week"] = pd.to_numeric(df["iso_week"], errors="coerce")
            df[case_col] = pd.to_numeric(df[case_col], errors="coerce")
            return (
                df.dropna(subset=["iso_year", "iso_week"])
                .groupby([spatial_col, "iso_year", "iso_week"], as_index=False)[case_col]
                .sum(min_count=1)
            )
        return case_data

    df = case_data[[spatial_col, date_col, case_col]].copy()
    dates = pd.to_datetime(df[date_col], errors="coerce")
    valid = dates.notna()
    df = df.loc[valid].copy()
    iso = dates.loc[valid].dt.isocalendar()
    df["iso_year"] = iso.year.astype(int).to_numpy()
    df["iso_week"] = iso.week.astype(int).to_numpy()
    df[case_col] = pd.to_numeric(df[case_col], errors="coerce")
    return (
        df.groupby([spatial_col, "iso_year", "iso_week"], as_index=False)[case_col]
        .sum(min_count=1)
    )


def percentile_historical_zones(
    df_predictions: pd.DataFrame,
    case_data: pd.DataFrame,
    *,
    spatial_col: str,
    percentile_cutoffs: list[float],
    prediction_col: str = "prediction",
    case_col: str = "case",
    percentile_week_pool: int | None = None,
    percentile_year_range: tuple[int, int] | None = None,
    low_season_floor: float | None = None,
    low_season_critical: float | None = None,
) -> pd.DataFrame:
    """Assign per-region historical-percentile bands as ``predictionZone``.

    By default, cutoffs come from each region's whole history. When both
    ``percentile_week_pool`` and ``percentile_year_range`` are supplied, use
    the seasonal endemic-channel method: for a forecast ISO week, pool that
    week and its neighboring weeks from the configured ISO-year range.
    ``N`` cutoffs yield ``N + 1`` bands, numbered from 1 (lowest).

    Predictions are weekly totals. Dated cases are aggregated by region, ISO
    year, and ISO week before computing cutoffs. Missing weeks are not
    synthesized; explicit zero rows remain in the distribution.

    Differs fundamentally from :func:`icmr_quartile_zones`:
      * ICMR cuts cross-sectionally per date (across all regions).
      * This cuts per-region across the region's own history.

    Issue #62 — adds a configurable third classification method alongside
    ``who`` and ``icmr``.
    """
    out = df_predictions.copy()
    out["predictionZone"] = pd.NA

    cutoffs_sorted = sorted(percentile_cutoffs)
    if (
        not cutoffs_sorted
        or any(not np.isfinite(cutoff) or cutoff < 0 or cutoff > 100 for cutoff in cutoffs_sorted)
        or len(set(cutoffs_sorted)) != len(cutoffs_sorted)
    ):
        raise ValueError("percentile_cutoffs must be unique finite percentiles in [0, 100].")
    n_bands = len(cutoffs_sorted) + 1
    seasonal = percentile_week_pool is not None or percentile_year_range is not None
    if seasonal and (percentile_week_pool is None or percentile_year_range is None):
        raise ValueError(
            "percentile_week_pool and percentile_year_range must be supplied together."
        )
    if seasonal and percentile_week_pool < 0:
        raise ValueError("percentile_week_pool must be >= 0.")
    if (low_season_floor is None) != (low_season_critical is None):
        raise ValueError("low_season_floor and low_season_critical must be supplied together.")
    if low_season_floor is not None:
        if not seasonal:
            raise ValueError("Low-season thresholds require seasonal percentile settings.")
        if (
            not np.isfinite(low_season_floor)
            or not np.isfinite(low_season_critical)
            or low_season_floor < 0
            or low_season_critical <= low_season_floor
        ):
            raise ValueError(
                "Low-season thresholds must be finite; critical must exceed floor."
            )
        if cutoffs_sorted != [50.0, 75.0, 90.0]:
            raise ValueError(
                "Low-season bands require percentile_cutoffs [50, 75, 90]."
            )

    history_source = _aggregate_cases_to_weekly(
        case_data, spatial_col=spatial_col, case_col=case_col
    )

    if seasonal:
        if not {"iso_year", "iso_week"}.issubset(history_source.columns):
            raise ValueError(
                "Seasonal percentile classification requires dated cases or "
                "iso_year/iso_week columns."
            )
        pred_date_col = next(
            (
                column
                for column in ("startDatePredictedWeek", "target_week", "recordDate")
                if column in out.columns
            ),
            None,
        )
        if pred_date_col is None:
            raise ValueError(
                "Seasonal percentile classification requires a forecast date column."
            )
        week_text = out[pred_date_col].astype("string").str.extract(
            r"^(\d{4})-W(\d{1,2})$"
        )
        target_week = pd.Series(pd.NA, index=out.index, dtype="Int64")
        target_year = pd.Series(pd.NA, index=out.index, dtype="Int64")
        is_iso_label = week_text[1].notna()
        target_week.loc[is_iso_label] = pd.to_numeric(
            week_text.loc[is_iso_label, 1], errors="coerce"
        ).astype("Int64")
        target_year.loc[is_iso_label] = pd.to_numeric(
            week_text.loc[is_iso_label, 0], errors="coerce"
        ).astype("Int64")
        pred_dates = pd.to_datetime(out[pred_date_col], errors="coerce")
        pred_iso = pred_dates.dt.isocalendar()
        target_week.loc[~is_iso_label] = pred_iso.loc[~is_iso_label, "week"].astype(
            "Int64"
        )
        target_year.loc[~is_iso_label] = pred_iso.loc[~is_iso_label, "year"].astype(
            "Int64"
        )
        out["_percentile_target_iso_week"] = target_week
        out["_percentile_target_iso_year"] = target_year

        start_year, end_year = percentile_year_range
        if start_year > end_year:
            raise ValueError("percentile_year_range start must be <= end.")
        historical = history_source.loc[
            history_source["iso_year"].between(start_year, end_year)
        ].copy()
        if historical.empty:
            return out.drop(
                columns=["_percentile_target_iso_week", "_percentile_target_iso_year"]
            )

        cached_cuts: dict[
            tuple[object, int, int], tuple[np.ndarray, float | None, float | None]
        ] = {}
        for region, region_preds in out.groupby(spatial_col, sort=False):
            region_hist = historical.loc[historical[spatial_col] == region]
            if region_hist.empty:
                continue
            for idx, row in region_preds.iterrows():
                target_week = row["_percentile_target_iso_week"]
                target_year = row["_percentile_target_iso_year"]
                raw_value = pd.to_numeric(
                    pd.Series([row[prediction_col]]), errors="coerce"
                ).iloc[0]
                if (
                    pd.isna(target_week)
                    or pd.isna(target_year)
                    or pd.isna(raw_value)
                ):
                    continue
                value = float(raw_value)
                if not np.isfinite(value):
                    continue
                target_week = int(target_week)
                target_year = int(target_year)
                if not 1 <= target_week <= 53:
                    raise ValueError(
                        f"Forecast ISO week must be between 1 and 53, got {target_week}."
                    )
                cache_key = (region, target_year, target_week)
                if cache_key not in cached_cuts:
                    # Never include the target ISO year or later, even when
                    # the configured range overlaps a historical evaluation.
                    prior_years = region_hist.loc[
                        region_hist["iso_year"] < target_year
                    ]
                    if prior_years.empty:
                        cached_cuts[cache_key] = (np.array([]), None, None)
                        continue
                    n_weeks = max(
                        [52, target_week]
                        + [
                            int(pd.Timestamp(year=int(year), month=12, day=28).isocalendar().week)
                            for year in prior_years["iso_year"].unique()
                        ]
                    )
                    offsets = {
                        ((target_week - 1 + delta) % n_weeks) + 1
                        for delta in range(
                            -percentile_week_pool, percentile_week_pool + 1
                        )
                    }
                    pool = prior_years.loc[
                        prior_years["iso_week"].isin(offsets), case_col
                    ].dropna()
                    if pool.empty:
                        cached_cuts[cache_key] = (np.array([]), None, None)
                        continue
                    values = pool.to_numpy(dtype=float)
                    values = values[np.isfinite(values)]
                    if values.size == 0:
                        cached_cuts[cache_key] = (np.array([]), None, None)
                        continue
                    cuts = np.percentile(values, cutoffs_sorted)
                    p50, p75 = (
                        np.percentile(values, [50.0, 75.0])
                        if low_season_floor is not None
                        else (None, None)
                    )
                    cached_cuts[cache_key] = (
                        cuts,
                        float(p50) if p50 is not None else None,
                        float(p75) if p75 is not None else None,
                    )
                cuts, p50, p75 = cached_cuts[cache_key]
                if len(cuts) == 0:
                    continue
                if value <= 0:
                    out.at[idx, "predictionZone"] = 1
                    continue
                band = int(np.searchsorted(cuts, value, side="right")) + 1
                if (
                    low_season_floor is not None
                    and p75 is not None
                    and p75 <= low_season_floor
                ):
                    if value >= low_season_critical:
                        band = 4
                    elif value >= low_season_floor:
                        band = 3
                    elif value >= max(float(p50), 1.0):
                        band = 2
                    else:
                        band = 1
                out.at[idx, "predictionZone"] = min(max(band, 1), n_bands)

        return out.drop(
            columns=["_percentile_target_iso_week", "_percentile_target_iso_year"]
        )

    for region, region_preds in out.groupby(spatial_col, sort=False):
        history = history_source.loc[
            history_source[spatial_col] == region, case_col
        ].dropna()
        if history.empty:
            continue
        cuts = np.percentile(history.values, cutoffs_sorted)
        preds = pd.to_numeric(region_preds[prediction_col], errors="coerce").values
        # np.searchsorted(cuts, x, side='right') returns the count of cuts
        # strictly less than (or equal to, for ties) x — i.e. the band index.
        bands = np.searchsorted(cuts, preds, side="right") + 1
        bands = np.clip(bands, 1, n_bands)
        # NaN predictions get nan from searchsorted via the to_numeric step:
        # explicitly preserve NaN as NA in the output.
        nan_mask = pd.isna(preds)
        out.loc[region_preds.index, "predictionZone"] = bands
        if nan_mask.any():
            out.loc[region_preds.index[nan_mask], "predictionZone"] = pd.NA

    return out


# PRISM-H §4.2 — method preference order (camelCase labels as they appear in thresholdMethod)
_METHOD_PRIORITY = ["historical", "previousNweeks", "weightedBaseline"]


def _select_best_method(final: pd.DataFrame) -> pd.DataFrame:
    """Select one row per date from the `final` assess_thresholds DataFrame.

    PRISM-H §4.2: historical is preferred when it has a non-null, non-zero
    Mean; prev_nweeks is the fallback; weighted_baseline is last resort.
    If no method in the priority list has a valid Mean, the first available
    row is returned (graceful degradation, no crash).
    """
    date_col = "startDatePredictedWeek"
    selected_rows: list[pd.DataFrame] = []

    for _date_val, group in final.groupby(date_col):
        picked: pd.DataFrame | None = None
        for method in _METHOD_PRIORITY:
            candidate = group[
                (group["thresholdMethod"] == method)
                & group["Mean"].notna()
                & (group["Mean"] > 0)
            ]
            if not candidate.empty:
                picked = candidate.iloc[[0]]
                break
        if picked is None:
            # Graceful fallback — return first row regardless
            picked = group.iloc[[0]]
        selected_rows.append(picked)

    if not selected_rows:
        return final.iloc[0:0].copy()
    return pd.concat(selected_rows, ignore_index=True)


def combine_thresholds(dfs: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate threshold DataFrames and sort."""
    cols = ["region_id", "date", "Mean", "StdDev", "threshold_method"]
    combined = pd.concat([d[cols] for d in dfs], ignore_index=True)
    return combined.sort_values(["region_id", "date", "threshold_method"]).reset_index(
        drop=True
    )


def assess_thresholds(
    df: pd.DataFrame,
    region_prefix: str = "corp",
    total_regions_overall: int = 31,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Evaluate threshold performance and pick the best method per prediction date.

    Translated from GBA ``AssessThresholdPerformance.py``.
    """
    df = df[df["regionID"].str.startswith(region_prefix)].reset_index(drop=True)
    total_threshold_types = df["thresholdMethod"].nunique()

    results = []
    for date_val, df_date in df.groupby("startDatePredictedWeek"):
        total_regions = df_date["regionID"].nunique()
        counts = df_date.groupby("regionID")["thresholdMethod"].nunique()
        complete_ids = counts[counts == total_threshold_types].index
        incomplete_ids = [
            r for r in df_date["regionID"].unique() if r not in complete_ids
        ]
        if incomplete_ids:
            log.warning(
                "thresholds: assess date=%s — %d of %d region(s) lack all %d threshold "
                "method(s) → excluded from method-comparison (only regions with all methods "
                "are used to pick the best threshold): %s",
                date_val,
                len(incomplete_ids),
                total_regions,
                total_threshold_types,
                incomplete_ids,
            )
        df_complete = df_date[df_date["regionID"].isin(complete_ids)]

        agg_c = (
            df_complete.groupby(["model", "thresholdMethod"])
            .agg(
                Risk_Zone_Sum=("predictionZone", "sum"),
                Row_Count=("predictionZone", "count"),
            )
            .reset_index()
        )
        agg_t = (
            df_date.groupby(["model", "thresholdMethod"])
            .agg(
                Risk_Zone_Sum_Total=("predictionZone", "sum"),
                Row_Count_Total=("predictionZone", "count"),
                Mean=("prediction", "mean"),
            )
            .reset_index()
        )
        merged = agg_c.merge(agg_t, on=["model", "thresholdMethod"])
        merged["dateOfComputingPrediction"] = df_date["dateOfComputingPrediction"].iloc[
            0
        ]
        merged["startDatePredictedWeek"] = date_val
        merged["Total_Region_Count"] = total_regions
        merged["Region_Count_Max"] = total_regions_overall
        results.append(merged)

    if not results:
        empty = pd.DataFrame()
        return empty, empty

    final = pd.concat(results, ignore_index=True)
    final["RatioCount.MethodVsTotal"] = (
        final["Row_Count_Total"] / final["Total_Region_Count"]
    )
    final.insert(0, "region_type", region_prefix.title())
    final = final.sort_values(
        ["startDatePredictedWeek", "thresholdMethod"]
    ).reset_index(drop=True)

    best = _select_best_method(final)
    return final, best
