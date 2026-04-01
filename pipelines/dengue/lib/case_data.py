"""Case-data processing functions.

Translated from GBA ``ParseNonStandardizedCaseData.py``.
All functions are pure: they accept DataFrames / scalars and return DataFrames.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

import geopandas as gpd
import pandas as pd

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fill_missing_dates(group: pd.DataFrame) -> pd.DataFrame:
    """Generate a full date range for a region and fill missing rows."""
    full_range = pd.date_range(start=group["date"].min(), end=group["date"].max())
    group = group.set_index("date").reindex(full_range).reset_index()
    group.rename(columns={"index": "date"}, inplace=True)
    return group


# ---------------------------------------------------------------------------
# Raw non-standardized input
# ---------------------------------------------------------------------------


def get_case_data(df: pd.DataFrame) -> gpd.GeoDataFrame:
    """Convert a raw non-standardized case DataFrame to a GeoDataFrame.

    Expects columns: ``Sample Collected Date``, ``lab_address_geocoded_long``,
    ``lab_address_geocoded_lat``.
    """
    cols = {
        "Sample Collected Date": "date",
        "lab_address_geocoded_long": "longitude",
        "lab_address_geocoded_lat": "latitude",
    }
    df0 = df[[c for c in cols if c in df.columns]].copy()
    df0.rename(columns=cols, inplace=True)
    df0["case"] = True
    df0["date"] = pd.to_datetime(df0["date"], errors="coerce").dt.tz_localize(None)
    df0["date"] = pd.to_datetime(df0["date"]).dt.date.astype("datetime64[ns]")
    return gpd.GeoDataFrame(
        df0,
        geometry=gpd.points_from_xy(df0["longitude"], df0["latitude"]),
        crs="EPSG:4326",
    )


# ---------------------------------------------------------------------------
# Spatial aggregation
# ---------------------------------------------------------------------------


def aggregate_daily(
    *,
    df: gpd.GeoDataFrame,
    gdf: gpd.GeoDataFrame,
    common_cols: list[str],
    w_params: list[str],
    operations: list[str],
) -> pd.DataFrame:
    """Spatially join case points to region polygons, aggregate per day per region.

    Mirrors GBA ``aggregate_daily`` from ``ParseNonStandardizedCaseData.py``.
    """
    mapped = {f"{col}_{func}": (col, func) for col, func in zip(w_params, operations)}
    df = gpd.sjoin(df, gdf[[*common_cols, "geometry"]], predicate="within", how="left")
    agg = df.groupby(["date", *common_cols], as_index=False).agg(**mapped)
    agg = agg.sort_values(["region_id", "date"], ascending=[True, True]).reset_index(
        drop=True
    )
    return agg


def aggregate_all_data_daily(
    dfs: list[pd.DataFrame],
    gdf: gpd.GeoDataFrame,
    common_cols: list[str],
    w_params: list[str],
    operations: list[str],
    date_start: pd.Timestamp | None = None,
    date_end: pd.Timestamp | None = None,
) -> list[pd.DataFrame]:
    """Process each raw DataFrame: geocode → spatial join → daily aggregate → fill gaps.

    Mirrors GBA ``aggregate_all_data_daily`` from ``ParseNonStandardizedCaseData.py``.
    Returns a list of per-file aggregated DataFrames (to be passed to ``concat_daily_dfs``).
    """
    results: list[pd.DataFrame] = []
    extra_cols = [c for c in common_cols[1:] if c != "date"]

    for raw_df in dfs:
        case_gdf = get_case_data(raw_df)
        case_gdf["date"] = pd.to_datetime(
            case_gdf["date"], errors="coerce"
        ).dt.date.astype("datetime64[ns]")
        # optional date filter
        if date_start is not None:
            case_gdf = case_gdf[case_gdf["date"] >= date_start]
        if date_end is not None:
            case_gdf = case_gdf[case_gdf["date"] <= date_end]
        case_gdf = case_gdf[
            case_gdf[["longitude", "latitude"]].notnull().all(axis=1)
        ].reset_index(drop=True)
        if case_gdf.empty:
            continue

        df_agg = aggregate_daily(
            df=case_gdf,
            gdf=gdf,
            common_cols=common_cols,
            w_params=w_params,
            operations=operations,
        )
        # Rename e.g. "case_sum" → "case"
        df_agg.rename(
            columns={"_".join([v1, v2]): v1 for v1, v2 in zip(w_params, operations)},
            inplace=True,
        )
        df_agg = df_agg.groupby("region_id", group_keys=False)[
            ["region_id", "date"] + extra_cols + w_params
        ].apply(_fill_missing_dates)
        df_agg[w_params] = df_agg[w_params].fillna(0)
        df_agg[common_cols] = df_agg[common_cols].ffill()
        results.append(df_agg)

    return results


def concat_daily_dfs(
    dfs: list[pd.DataFrame],
    common_cols: list[str],
    w_params: list[str],
) -> pd.DataFrame:
    """Concatenate per-file daily DataFrames, de-duplicate, fill missing dates.

    Mirrors GBA ``concat_files`` from ``ParseNonStandardizedCaseData.py``.
    """
    extra_cols = [c for c in common_cols[1:] if c != "date"]
    df = pd.concat(dfs, ignore_index=True).drop_duplicates()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date.astype(
        "datetime64[ns]"
    )
    df = df.groupby(["date", *common_cols], as_index=False)[w_params].agg("sum")
    df = df.sort_values(["region_id", "date"], ascending=[True, True]).reset_index(
        drop=True
    )
    df = df.groupby("region_id", group_keys=False)[
        ["region_id", "date"] + extra_cols + w_params
    ].apply(_fill_missing_dates)
    df[w_params] = df[w_params].fillna(0)
    df[common_cols] = df[common_cols].ffill()
    return df


# ---------------------------------------------------------------------------
# Standardized input (metadata.primaryDate + location.adminX.ID columns)
# ---------------------------------------------------------------------------

# Maps region_type → the admin-ID column expected in standardized linelist data.
# Exposed here so the step can also use it for detection.
STANDARDIZED_REGION_ADMIN_COL: dict[str, str] = {
    "state": "location.admin1.ID",
    "ut": "location.admin1.ID",
    "district": "location.admin2.ID",
    "corp": "location.admin2.ID",
    "subdistrict": "location.admin3.ID",
    "zone": "location.admin3.ID",
    "ulb": "location.admin3.ID",
    "ward": "location.admin5.ID",
    "village": "location.admin5.ID",
}


def aggregate_standardized_data_daily(
    dfs: list[pd.DataFrame],
    region_type: str,
    date_start: pd.Timestamp | None = None,
    date_end: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Process standardized linelist DataFrames into daily case counts per region.

    Standardized format has ``metadata.primaryDate`` as the date column and
    ``location.adminX.ID`` as the region identifier (no lat/lon or spatial join
    required).  Each row represents one case; this function counts rows per
    region per day.

    Parameters
    ----------
    dfs:
        List of raw DataFrames, each containing ``metadata.primaryDate`` and the
        appropriate ``location.adminX.ID`` column for *region_type*.
    region_type:
        One of the keys in ``STANDARDIZED_REGION_ADMIN_COL`` (e.g. ``"district"``,
        ``"zone"``).
    date_start / date_end:
        Optional inclusive date bounds applied after parsing.

    Returns
    -------
    DataFrame with columns ``region_id``, ``date``, ``case`` where missing dates
    within each region's range are filled with 0.
    """
    admin_col = STANDARDIZED_REGION_ADMIN_COL.get(region_type)
    if admin_col is None:
        raise ValueError(
            f"Unknown region_type={region_type!r} for standardized data. "
            f"Expected one of: {list(STANDARDIZED_REGION_ADMIN_COL)}"
        )

    list_dfs = []
    for df in dfs:
        if "metadata.primaryDate" not in df.columns or admin_col not in df.columns:
            continue
        sub = df[["metadata.primaryDate", admin_col]].copy()
        sub.rename(
            columns={"metadata.primaryDate": "date", admin_col: "region_id"},
            inplace=True,
        )
        list_dfs.append(sub)

    if not list_dfs:
        raise ValueError(
            f"None of the provided DataFrames contain both 'metadata.primaryDate' and "
            f"'{admin_col}' (required for region_type={region_type!r})."
        )

    df = pd.concat(list_dfs, ignore_index=True)

    # Strip trailing Z from ISO-format strings before parsing
    if df["date"].dtype == object:
        df["date"] = df["date"].str.replace("Z", "", regex=False)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.tz_localize(None)
    df["date"] = pd.to_datetime(df["date"]).dt.date.astype("datetime64[ns]")
    df = df.dropna(subset=["date", "region_id"])

    if date_start is not None:
        df = df[df["date"] >= date_start]
    if date_end is not None:
        df = df[df["date"] <= date_end]

    # Count rows per region per day (each row = 1 case)
    df_agg = df.groupby(["region_id", "date"]).size().reset_index(name="case")
    df_agg = df_agg.sort_values(["region_id", "date"]).reset_index(drop=True)

    # Fill missing dates within each region's range with 0
    df_agg = df_agg.groupby("region_id", group_keys=False)[
        ["region_id", "date", "case"]
    ].apply(_fill_missing_dates)
    df_agg["case"] = df_agg["case"].fillna(0)
    df_agg["region_id"] = df_agg["region_id"].ffill()

    return df_agg[["region_id", "date", "case"]].reset_index(drop=True)


def load_region_gdf(geojson_folder: str, region_type: str) -> gpd.GeoDataFrame:
    """Load and concat all geojson files for a region type from geojson_folder/{region_type}s/."""
    import os
    from pathlib import Path

    folder = Path(geojson_folder) / f"{region_type}s"
    gdfs = [gpd.read_file(folder / f).to_crs(epsg=4326) for f in os.listdir(folder)]
    return gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True))


# ---------------------------------------------------------------------------
# Rolling aggregation, sampling, and output (same across both source files)
# ---------------------------------------------------------------------------


def rolling_aggregate(df: pd.DataFrame, n_days: int = 7) -> pd.DataFrame:
    """Compute an N-day rolling sum of cases per region."""

    def _rolling_fn(group: pd.DataFrame) -> pd.DataFrame:
        group = group.sort_values("date").reset_index()
        group = group.set_index("date")
        rolling_df = (
            group["case"]
            .rolling(f"{n_days - 1}D", closed="both")
            .agg({"case": "sum"})
            .reset_index()
        )
        rolling_df = rolling_df.merge(
            group.reset_index()[["region_id", "date"]], on="date", how="left"
        )
        return rolling_df

    df["date"] = pd.to_datetime(df["date"])
    listcols = ["region_id", "date", "case"]
    result = df.groupby("region_id")[listcols].apply(_rolling_fn).reset_index(drop=True)
    return result[listcols].reset_index(drop=True)


def get_latest_sampling_day(
    day_given: pd.Timestamp,
    sampling_day_abbrev: str,
) -> str:
    """Return the date string of the latest occurrence of the sampling weekday."""
    day_week_before = day_given - dt.timedelta(days=7)
    return str(
        pd.date_range(start=day_week_before, end=day_given, freq=sampling_day_abbrev)[
            -1
        ].date()
    )


def sample_data(
    df: pd.DataFrame,
    end_date: str | None = None,
    sample_from: Literal["beginning", "end"] = "end",
    sampling_rate: int = 7,
) -> pd.DataFrame:
    """Sample rows at every ``sampling_rate`` dates.

    Dates are normalised to YYYY-MM-DD strings before comparison so that
    Timestamp objects (which str() to '2024-01-01 00:00:00') don't break the
    end_date comparison.
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


# ---------------------------------------------------------------------------
# Column rename for output
# ---------------------------------------------------------------------------

# From ParseNonStandardizedCaseData.py:
#   {"corp": "location.admin2.ID", "zone": "location.admin3.ID", "ward": "location.admin5.ID"}
_REGION_ADMIN_COL: dict[str, str] = {
    "corp": "location.admin2.ID",
    "zone": "location.admin3.ID",
    "ward": "location.admin5.ID",
    "district": "location.admin2.ID",
    "subdistrict": "location.admin3.ID",
}


def rename_columns_for_output(df: pd.DataFrame, region_type: str) -> pd.DataFrame:
    """Rename region_id / date columns to the canonical linelist names for downstream."""
    admin_col = _REGION_ADMIN_COL.get(region_type, "location.admin2.ID")
    out = df[["region_id", "date", "case"]].copy()
    out.rename(
        columns={"date": "metadata.primaryDate", "region_id": admin_col}, inplace=True
    )
    return out
