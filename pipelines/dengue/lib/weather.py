"""Weather-data processing functions.

Translated from GBA ``ParseAndExtractWeatherData.py`` (daily agg, rolling N-day,
sampling). NetCDF download/parse lives in ``lib/cds.py`` / download step; this
module assumes per-region CSVs that may be sub-daily (e.g. hourly ``time`` rows).

Daily aggregation matches SOT intent for ERA5-style fields: temperature and
dewpoint **mean** per day, precipitation **sum** per day (see SOT ``w_params`` /
``operations`` around daily aggregation).
"""

from __future__ import annotations

import logging
from typing import Literal

import pandas as pd

log = logging.getLogger(__name__)

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

    Each entry in ``daily_agg`` may have an optional ``output_name`` key so the
    same input column can yield multiple output columns (e.g. max/min/mean for
    temperature — matching the SOT ``ParseAndExtractWeatherData.py`` which produces
    ``t2m_max``, ``t2m_min``, ``t2m_mean`` from the same hourly ``t2m`` column).
    """
    df = df.copy()
    # Convert GMT → IST (+5h30m) before daily grouping, matching SOT
    # merge_file_daily_data: thisdf["time"] += timedelta(hours=5.5)
    df["date"] = (
        pd.to_datetime(df["date"], format="mixed") + pd.Timedelta(hours=5, minutes=30)
    ).dt.normalize()
    df["date"] = df["date"].dt.date.astype("datetime64[ns]")
    # Remove boundary dates: first and last contain partial-day data due to IST offset
    all_dates = sorted(df["date"].unique())
    if len(all_dates) > 2:
        dropped_boundary = [all_dates[0], all_dates[-1]]
        log.debug(
            "weather: trimmed boundary dates %s after GMT→IST conversion "
            "(partial-day rows — first/last dates contain incomplete data due to the +5h30m shift)",
            [str(d) for d in dropped_boundary],
        )
        df = df[df["date"].isin(all_dates[1:-1])].reset_index(drop=True)
    rules = daily_agg if daily_agg is not None else []

    # Build explicit (input_col, op, output_col) triples from rules.
    # output_col defaults to input_col when not specified.
    triples: list[tuple[str, str, str]] = []
    for item in rules:
        name = item.get("name")
        op = item.get("op")
        if not name or not op:
            continue
        output_name = str(item.get("output_name") or name)
        if name in df.columns:
            triples.append((name, op, output_name))

    # Fallback: cover any weather_var that has no explicit rule.
    covered_inputs = {inp for inp, _, _ in triples}
    for var in weather_vars:
        if var not in df.columns or var in covered_inputs:
            continue
        triples.append((var, _DAILY_AGG.get(var, "mean"), var))

    if not triples:
        raise ValueError(
            "aggregate_daily: none of weather_vars are present in columns "
            f"{list(df.columns)!r} (after normalise_columns)."
        )

    grouped = df.groupby(["region_id", "date"])
    parts: list[pd.DataFrame] = []
    for inp_col, op, out_col in triples:
        part = grouped[inp_col].agg(op).reset_index()
        part.rename(columns={inp_col: out_col}, inplace=True)
        parts.append(part.set_index(["region_id", "date"]))

    out = parts[0].copy()
    for part in parts[1:]:
        out = out.join(part, how="outer")
    out = out.reset_index()

    meta = [c for c in ("name", "parent", "parent_name") if c in df.columns]
    if meta:
        first = df.groupby(["region_id", "date"], as_index=False)[meta].first()
        out = out.merge(first, on=["region_id", "date"], how="left")
        nan_meta = {c: int(out[c].isna().sum()) for c in meta if out[c].isna().any()}
        if nan_meta:
            log.warning(
                "weather: after metadata left-join, %d (region_id, date) combinations "
                "have NaN in metadata columns %s — region name/parent info was not present "
                "in the source data for those rows",
                max(nan_meta.values()),
                nan_meta,
            )
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

    # Roll all weather_vars plus any column explicitly named in rolling_agg rules
    # (e.g. derived daily columns like 2mTemperature_max that are not raw inputs).
    extra = [item["name"] for item in rules if item.get("name")]
    all_vars = list(dict.fromkeys(list(weather_vars) + extra))

    results: list[pd.DataFrame] = []
    window = f"{n_days - 1}D"
    for _, group in df.groupby("region_id"):
        g = group.set_index("date").sort_index()
        for var in all_vars:
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
        "mandal": "location.admin4.ID",
    }.get(region_type, "location.admin2.ID")

    out = df.copy()
    if "region_id" in out.columns:
        out.rename(columns={"region_id": admin_col}, inplace=True)
    if "date" in out.columns:
        out.rename(columns={"date": "metadata.primaryDate"}, inplace=True)
    return out
