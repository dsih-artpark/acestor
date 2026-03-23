"""Weather-data processing functions.

Translated from GBA ``ParseAndExtractWeatherData.py`` (daily agg, rolling N-day,
sampling). NetCDF download/parse lives in ``lib/cds.py`` / download step; this
module assumes per-region CSVs that may be sub-daily (e.g. hourly ``time`` rows).

Daily aggregation matches SOT intent for ERA5-style fields: temperature and
dewpoint **mean** per day, precipitation **sum** per day (see SOT ``w_params`` /
``operations`` around daily aggregation).
"""

from __future__ import annotations

from typing import Literal

import pandas as pd

_COL_ALIASES: dict[str, str] = {
    "t2m": "2mTemperature",
    "d2m": "2mDewpointTemperature",
    "tp": "totalPrecipitation",
    "time": "date",
    "metadata.primaryDate": "date",
}

# Canonical names → pandas groupby/rolling reducer (aligned with SOT daily + rolling ops).
_DAILY_AGG: dict[str, str] = {
    "2mTemperature": "mean",
    "2mDewpointTemperature": "mean",
    "totalPrecipitation": "sum",
}
_ROLLING_AGG: dict[str, str] = {
    "2mTemperature": "mean",
    "2mDewpointTemperature": "mean",
    "totalPrecipitation": "sum",
}


def normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename known short / legacy column names to the canonical names."""
    rename_map = {old: new for old, new in _COL_ALIASES.items() if old in df.columns}
    return df.rename(columns=rename_map)


def _build_agg_map(
    rules: list[dict[str, str]], fallback: dict[str, str]
) -> dict[str, str]:
    """Convert a list of {name, op} into a name→op mapping with sane fallbacks."""
    mapping: dict[str, str] = {}
    for item in rules:
        name = item.get("name")
        op = item.get("op")
        if not name or not op:
            continue
        mapping[str(name)] = str(op)
    # fall back to defaults for any names that were not explicitly configured
    for name, op in fallback.items():
        mapping.setdefault(name, op)
    return mapping


def aggregate_daily(
    df: pd.DataFrame,
    weather_vars: list[str],
    daily_agg: list[dict[str, str]] | None = None,
) -> pd.DataFrame:
    """Aggregate sub-daily rows to one row per (region_id, date).

    Uses mean for temperature-like fields and sum for precipitation, consistent
    with ``ParseAndExtractWeatherData.py`` daily aggregation from hourly data.
    """
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["date"] = df["date"].dt.date.astype("datetime64[ns]")
    rules = daily_agg if daily_agg is not None else []
    agg_spec = _build_agg_map(rules, _DAILY_AGG)
    agg_dict: dict[str, str] = {}
    for var in weather_vars:
        if var not in df.columns:
            continue
        agg_dict[var] = agg_spec.get(var, "mean")
    if not agg_dict:
        raise ValueError(
            "aggregate_daily: none of weather_vars are present in columns "
            f"{list(df.columns)!r} (after normalise_columns)."
        )
    out = df.groupby(["region_id", "date"], as_index=False).agg(agg_dict)
    meta = [c for c in ("name", "parent", "parent_name") if c in df.columns]
    if meta:
        first = df.groupby(["region_id", "date"], as_index=False)[meta].first()
        out = out.merge(first, on=["region_id", "date"], how="left")
    return out.sort_values(["region_id", "date"]).reset_index(drop=True)


def rolling_aggregate(
    df: pd.DataFrame,
    weather_vars: list[str],
    n_days: int = 7,
    rolling_agg: list[dict[str, str]] | None = None,
) -> pd.DataFrame:
    """N-day rolling window per region (SOT ``rolling_aggregate_Ndays`` style).

    Temperature/dewpoint: rolling **mean**; precipitation: rolling **sum** over the window.
    """
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["region_id", "date"])

    rules = rolling_agg if rolling_agg is not None else []
    agg_spec = _build_agg_map(rules, _ROLLING_AGG)

    results: list[pd.DataFrame] = []
    window = f"{n_days - 1}D"
    for _, group in df.groupby("region_id"):
        g = group.set_index("date").sort_index()
        for var in weather_vars:
            if var not in g.columns:
                continue
            op = agg_spec.get(var, "mean")
            if op == "sum":
                g[var] = g[var].rolling(window, closed="both").sum()
            else:
                g[var] = g[var].rolling(window, closed="both").mean()
        results.append(g.reset_index())
    return pd.concat(results, ignore_index=True)


def sample_data(
    df: pd.DataFrame,
    end_date: str | None = None,
    sample_from: Literal["beginning", "end"] = "end",
    sampling_rate: int = 7,
) -> pd.DataFrame:
    """Sample at every ``sampling_rate`` dates (SOT ``sample_data``, ``sample_from='end'``).

    Dates are normalised to YYYY-MM-DD strings before comparison (same fix as case ``sample_data``).
    """
    dates = sorted(str(pd.Timestamp(d).date()) for d in df["date"].unique())
    if end_date is not None:
        end_str = str(pd.Timestamp(end_date).date())
        dates = [d for d in dates if d <= end_str]
    sampled = (
        dates[::-sampling_rate] if sample_from == "end" else dates[::sampling_rate]
    )
    date_strs = df["date"].apply(lambda d: str(pd.Timestamp(d).date()))
    return df[date_strs.isin(sampled)].reset_index(drop=True)


def rename_columns_for_output(
    df: pd.DataFrame,
    region_type: str,
) -> pd.DataFrame:
    """Rename pipeline columns back to linelist-style column names."""
    admin_col = {
        "zone": "location.admin3.ID",
        "corp": "location.admin2.ID",
        "district": "location.admin2.ID",
        "subdistrict": "location.admin3.ID",
    }.get(region_type, "location.admin2.ID")

    out = df.copy()
    if "region_id" in out.columns:
        out.rename(columns={"region_id": admin_col}, inplace=True)
    if "date" in out.columns:
        out.rename(columns={"date": "metadata.primaryDate"}, inplace=True)
    return out
