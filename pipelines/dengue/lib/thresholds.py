"""Threshold computation functions.

Translated from GBA ``GenerateThresholds.py`` and ``utils.py``
(``retHistoricalStats``, ``retPrevNWeeksStats``, ``compute_thresholds``).
"""

from __future__ import annotations

import logging
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
            return target_vals.mean(axis=1, skipna=True)
        elif stat == "std":
            return target_vals.std(axis=1, skipna=True)
        raise ValueError(f"Unsupported stat: {stat}")

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["region_id", "date"])

    parts = []
    for _, group in df.groupby("region_id"):
        g = group.reset_index(drop=True)
        col = f"Mean_N{n}week_k{k}days"
        g[col] = _process_group(g, "case", "mean", n, k, closed="left")
        g["Mean"] = _process_group(g, col, "mean", 3, k, closed="right")
        g["StdDev"] = _process_group(g, col, "std", 3, k, closed="right")
        parts.append(g)

    result = pd.concat(parts, ignore_index=True)
    result["threshold_method"] = "previousNweeks"
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

    best = (
        final.sort_values(
            ["startDatePredictedWeek", "Risk_Zone_Sum_Total"], ascending=[True, False]
        )
        .groupby("startDatePredictedWeek")
        .first()
        .reset_index()
    )
    return final, best
