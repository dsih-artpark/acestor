"""Weather-data processing functions.

Translated from GBA ``ParseAndExtractWeatherData.py`` and
``WeatherPipelineFunctions.py``.  Only the aggregation / sampling helpers
are included here; the actual NetCDF parsing and centroid estimation are
complex and will be ported as-needed when the download sources are finalised.
"""

from __future__ import annotations

import pandas as pd

_COL_ALIASES: dict[str, str] = {
    "t2m": "2mTemperature",
    "d2m": "2mDewpointTemperature",
    "tp": "totalPrecipitation",
    "time": "date",
    "metadata.primaryDate": "date",
}


def normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename known short / legacy column names to the canonical names."""
    rename_map = {old: new for old, new in _COL_ALIASES.items() if old in df.columns}
    return df.rename(columns=rename_map)


def aggregate_daily(df: pd.DataFrame, weather_vars: list[str]) -> pd.DataFrame:
    """Aggregate sub-daily weather records to daily means per region."""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date.astype("datetime64[ns]")
    agg_dict = {var: "mean" for var in weather_vars if var in df.columns}
    return df.groupby(["region_id", "date"]).agg(agg_dict).reset_index()


def rolling_aggregate(
    df: pd.DataFrame,
    weather_vars: list[str],
    n_days: int = 7,
) -> pd.DataFrame:
    """Compute an N-day rolling mean for weather variables per region."""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["region_id", "date"])

    results = []
    for _, group in df.groupby("region_id"):
        g = group.set_index("date")
        for var in weather_vars:
            if var in g.columns:
                g[var] = g[var].rolling(f"{n_days - 1}D", closed="both").mean()
        results.append(g.reset_index())
    return pd.concat(results, ignore_index=True)


def sample_data(
    df: pd.DataFrame,
    end_date: str | None = None,
    sampling_rate: int = 7,
) -> pd.DataFrame:
    """Sample weather data at every ``sampling_rate`` dates from the end."""
    dates = sorted(df["date"].unique())
    if end_date is not None:
        dates = [d for d in dates if str(d) <= end_date]
    sampled = dates[::-sampling_rate]
    return df[df["date"].isin(sampled)].reset_index(drop=True)


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
