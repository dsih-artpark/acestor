"""CDS API weather data download, NetCDF parsing, and geocoding.

Ported from ``DownloadWeatherData.py`` and ``ParseAndExtractWeatherData.py``.
All functions are pure or take explicit dependencies — no module-level state.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import tempfile
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point
from sklearn.linear_model import LinearRegression

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Region bounds from geojson
# ---------------------------------------------------------------------------


def return_region_bounds(
    union_polygon: Any,
    resolution_deg: float = 0.1,
) -> tuple[float, float, float, float]:
    """Compute (N, W, S, E) bounding box snapped to *resolution_deg* grid with one-cell padding.

    Matches GBA DownloadWeatherData.py intent: resolution from config (docstring "p taken from
    config yaml file"); running code there uses 0.1° (×10, ±0.1). Commented block used p=10
    as a typo — p is resolution in degrees, so 1/p is the multiplier (e.g. p=0.1 → 10).
    """
    if resolution_deg <= 0:
        raise ValueError("resolution_deg must be positive")
    bound_w0, bound_s0, bound_e0, bound_n0 = union_polygon.bounds
    n = 1.0 / resolution_deg
    bound_w = np.round(float(np.floor(bound_w0 * n) / n) - resolution_deg, 2)
    bound_s = np.round(float(np.floor(bound_s0 * n) / n) - resolution_deg, 2)
    bound_e = np.round(float(np.ceil(bound_e0 * n) / n) + resolution_deg, 2)
    bound_n = np.round(float(np.ceil(bound_n0 * n) / n) + resolution_deg, 2)
    log.info(
        "Computed Region bounds (N, W, S, E): %s", (bound_n, bound_w, bound_s, bound_e)
    )
    return bound_n, bound_w, bound_s, bound_e


def compute_region_bounds_from_geojsons(
    geojson_folder: str | Path,
    resolution_deg: float = 0.1,
) -> tuple[float, float, float, float]:
    """Read all geojsons in a folder, union the polygons, return (N, W, S, E)."""
    geojson_folder = Path(geojson_folder)
    gdfs = [gpd.read_file(geojson_folder / f) for f in os.listdir(geojson_folder)]
    region_gdf = gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True))
    region_gdf = region_gdf.dissolve(by="parent_name").reset_index()
    region_gdf = region_gdf.to_crs(epsg=4326)
    return return_region_bounds(
        region_gdf.geometry.union_all(), resolution_deg=resolution_deg
    )


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def check_existing_months(cache_path: Path) -> set[tuple[int, int]]:
    """Scan the local cache for already-downloaded (year, month) pairs."""
    existing: set[tuple[int, int]] = set()
    if not cache_path.exists():
        return existing
    for year_folder in cache_path.iterdir():
        if year_folder.is_dir() and year_folder.name.isdigit():
            year = int(year_folder.name)
            for f in year_folder.glob("*"):
                if f.suffix in (".nc", ".zip", ".csv"):
                    parts = f.stem.split("_")
                    if len(parts) >= 2 and parts[1].isdigit():
                        existing.add((year, int(parts[1])))
    return existing


def get_missing_months(
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    existing: set[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Return (year, month) tuples in [start, end] that are not already cached."""
    all_months: list[tuple[int, int]] = []
    cur = start_date.replace(day=1)
    end = end_date.replace(day=1)
    while cur <= end:
        all_months.append((cur.year, cur.month))
        cur = (cur + pd.offsets.MonthBegin(1)).normalize()
    return [ym for ym in all_months if ym not in existing]


def get_recent_months() -> tuple[tuple[int, int], tuple[int, int]]:
    """Return (prev_month, current_month) as (year, month) tuples."""
    now = dt.datetime.now()
    if now.month == 1:
        prev = (now.year - 1, 12)
    else:
        prev = (now.year, now.month - 1)
    return prev, (now.year, now.month)


def delete_recent_cache(cache_path: Path) -> None:
    """Delete cached files for the current and previous month (always re-download)."""
    prev, cur = get_recent_months()
    for year, month in (prev, cur):
        prefix = f"{year}_{month:02d}"
        for root, _, files in os.walk(cache_path):
            for fname in files:
                if fname.startswith(prefix):
                    fp = Path(root) / fname
                    fp.unlink(missing_ok=True)
                    log.info("Deleted stale cache file: %s", fp)


# ---------------------------------------------------------------------------
# CDS API download
# ---------------------------------------------------------------------------


def download_month(
    *,
    dataset: str,
    region_bounds: tuple[float, float, float, float],
    variables: list[str],
    year: int,
    month: int,
    cache_path: Path,
    cds_url: str = "",
    cds_key: str = "",
) -> Path | None:
    """Download one month of ERA5 data from CDS and save to cache_path/year/year_month.nc."""
    import cdsapi

    folder = cache_path / str(year)
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / f"{year}_{month:02d}.zip"

    bound_n, bound_w, bound_s, bound_e = region_bounds
    request = {
        "product_type": ["reanalysis"],
        "variable": variables,
        "year": [str(year)],
        "month": [f"{month:02d}"],
        "day": [f"{d:02d}" for d in range(1, 32)],
        "time": [f"{h:02d}:00" for h in range(24)],
        "data_format": "netcdf",
        "download_format": "zip",
        "area": [bound_n, bound_w, bound_s, bound_e],
    }

    client_kwargs: dict[str, Any] = {}
    if cds_url:
        client_kwargs["url"] = cds_url
    if cds_key:
        client_kwargs["key"] = cds_key

    client = cdsapi.Client(**client_kwargs)
    try:
        client.retrieve(dataset, request, str(output))
        log.info("Downloaded %s %d-%02d -> %s", dataset, year, month, output)
        return output
    except Exception as e:
        msg = str(e)
        if "not available" in msg.lower():
            log.warning("No data available for %d-%02d, skipping", year, month)
            return None
        raise


def download_months(
    *,
    dataset: str,
    region_bounds: tuple[float, float, float, float],
    variables: list[str],
    months: list[tuple[int, int]],
    cache_path: Path,
    cds_url: str = "",
    cds_key: str = "",
) -> list[Path]:
    """Download multiple months, return list of downloaded file paths."""
    from tqdm import tqdm

    downloaded: list[Path] = []
    for year, month in tqdm(months, desc="Downloading months", unit="month"):
        p = download_month(
            dataset=dataset,
            region_bounds=region_bounds,
            variables=variables,
            year=year,
            month=month,
            cache_path=cache_path,
            cds_url=cds_url,
            cds_key=cds_key,
        )
        if p is not None:
            downloaded.append(p)
    return downloaded


# ---------------------------------------------------------------------------
# NetCDF parsing
# ---------------------------------------------------------------------------


def read_nc(filepath: str | Path) -> pd.DataFrame:
    """Read a NetCDF file, return a DataFrame with time/lat/lon + variables."""
    import xarray as xr

    with xr.open_dataset(str(filepath), engine="netcdf4") as ds:
        df = ds.to_dataframe().reset_index()
    if "valid_time" in df.columns:
        df.rename(columns={"valid_time": "time"}, inplace=True)
    df.sort_values(by=["time", "longitude", "latitude"], inplace=True)
    return df.reset_index(drop=True)


def read_nc_or_zip(filepath: str | Path) -> pd.DataFrame:
    """Read a .nc or disguised .zip file, return a flat DataFrame."""
    filepath = Path(filepath)
    if zipfile.is_zipfile(filepath):
        merged: pd.DataFrame | None = None
        with zipfile.ZipFile(filepath, "r") as z:
            for name in z.namelist():
                if name.endswith(".nc"):
                    with z.open(name) as f:
                        with tempfile.NamedTemporaryFile(
                            suffix=".nc", delete=False
                        ) as tmp:
                            tmp.write(f.read())
                            tmp_path = tmp.name
                    try:
                        cur = read_nc(tmp_path)
                    finally:
                        os.unlink(tmp_path)
                    if merged is None:
                        merged = cur
                    else:
                        common = list(set(merged.columns) & set(cur.columns))
                        merged = pd.merge(merged, cur, on=common)
        if merged is None:
            raise ValueError(f"No .nc files found inside zip: {filepath}")
        return merged
    return read_nc(filepath)


# ---------------------------------------------------------------------------
# Geocoding: grid points -> region centroids
# ---------------------------------------------------------------------------


def read_geojson(
    filepath: str | Path, target_crs: str = "EPSG:4326"
) -> gpd.GeoDataFrame:
    """Read a geojson file and reproject."""
    gdf = gpd.read_file(str(filepath))
    return gdf.to_crs(target_crs)


def get_region_gdfs(
    geojson_folder: str | Path, region_type: str
) -> list[gpd.GeoDataFrame]:
    """Load per-region GeoDataFrames from geojson_folder/{region_type}s/."""
    folder = Path(geojson_folder) / f"{region_type}s"
    result = []
    for root, _, files in os.walk(folder):
        for fname in files:
            if fname.startswith(region_type) and fname.endswith(".geojson"):
                result.append(read_geojson(Path(root) / fname))
    return result


def get_region_centroids(gdf: gpd.GeoDataFrame) -> Any:
    """Return the centroid point of a single-region GeoDataFrame (in original CRS)."""
    original_crs = gdf.crs
    projected = gdf.to_crs(epsg=3395)
    centroids_gdf = projected.copy()
    centroids_gdf["geometry"] = projected.geometry.centroid
    centroids_gdf = centroids_gdf.to_crs(original_crs)
    return centroids_gdf["geometry"].iloc[0]


def get_gridpoints(df: pd.DataFrame) -> gpd.GeoDataFrame:
    """Build a GeoDataFrame of unique grid points from a parsed NetCDF DataFrame."""
    lats = np.sort(df["latitude"].unique())
    lons = np.sort(df["longitude"].unique())
    grid = pd.DataFrame(
        {
            "latitude": np.tile(lats, len(lons)),
            "longitude": np.repeat(lons, len(lats)),
        }
    )
    return gpd.GeoDataFrame(
        grid,
        geometry=gpd.points_from_xy(grid.longitude, grid.latitude),
        crs="EPSG:4326",
    )


def filter_far_points(
    gdf_points: gpd.GeoDataFrame,
    gdf_map: gpd.GeoDataFrame,
    threshold_km: float = 25.0,
) -> gpd.GeoDataFrame:
    """Keep only grid points within *threshold_km* of a region polygon."""
    if gdf_map.crs != gdf_points.crs:
        gdf_points = gdf_points.to_crs(gdf_map.crs)
    map_m = gdf_map.to_crs(epsg=3395)
    pts_m = gdf_points.to_crs(epsg=3395)
    pts_m["_dist"] = pts_m.geometry.apply(
        lambda pt: map_m.geometry.apply(lambda poly: pt.distance(poly)).min()
    )
    filtered = pts_m[pts_m["_dist"] <= threshold_km * 1000].drop(columns=["_dist"])
    filtered = filtered.to_crs(gdf_points.crs)
    if "name" in gdf_map.columns:
        filtered["name"] = gdf_map.iloc[0]["name"]
    return filtered.reset_index(drop=True)


def compute_hourly_estimates(
    group: pd.DataFrame,
    region_centroid: Any,
    target_vars: list[str],
) -> pd.Series:
    """Fit a linear regression on (lon, lat) for each variable and predict at centroid."""
    features = group[["longitude", "latitude"]].values
    centroid_xy = np.array([[region_centroid.x, region_centroid.y]])
    estimates = {}
    for var in target_vars:
        vals = group[var].values
        if len(vals) < 2 or np.all(np.isnan(vals)):
            estimates[var] = np.nan
            continue
        model = LinearRegression()
        model.fit(features, vals)
        estimates[var] = model.predict(centroid_xy)[0]
    return pd.Series(estimates)


def estimate_at_centroids(
    region_data: pd.DataFrame | gpd.GeoDataFrame,
    region_centroid: Any,
    target_vars: list[str],
) -> pd.DataFrame:
    """Estimate weather variables at a region centroid for every time step."""
    rd = region_data.copy()
    rd["time"] = pd.to_datetime(rd["time"])
    cols = ["time", "latitude", "longitude"] + target_vars
    return (
        rd.groupby("time")[cols]
        .apply(
            compute_hourly_estimates,
            region_centroid=region_centroid,
            target_vars=target_vars,
        )
        .reset_index()
    )


def process_month_file(
    filepath: Path,
    w_params: list[str],
    region_gdfs: list[gpd.GeoDataFrame],
    region_centroids: list[Any],
    threshold_km: float = 25.0,
) -> pd.DataFrame:
    """Parse one NC/ZIP file and return hourly centroid values for all regions."""
    df = read_nc_or_zip(filepath)
    gdf_data = gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df.longitude, df.latitude),
        crs="EPSG:4326",
    )
    gdf_points = get_gridpoints(df)

    filtered_regions = [
        filter_far_points(gdf_points, gdf, threshold_km).reset_index(drop=True)
        for gdf in region_gdfs
    ]
    for i, gdf in enumerate(filtered_regions):
        gdf = deepcopy(gdf)
        gdf["geometry"] = gdf[["latitude", "longitude"]].apply(
            lambda row: Point(row.iloc[1], row.iloc[0]),
            axis=1,
        )
        filtered_regions[i] = gdf

    centroid_dfs: list[pd.DataFrame] = []
    for region_data_gdf, centroid, region_gdf in zip(
        filtered_regions, region_centroids, region_gdfs
    ):
        right = region_data_gdf.drop(columns=["latitude", "longitude"], errors="ignore")
        joined = gpd.sjoin(
            gdf_data,
            right,
            how="inner",
            predicate="intersects",
        ).reset_index(drop=True)
        if "index_right" in joined.columns:
            joined.drop(columns=["index_right"], inplace=True)

        df_c = estimate_at_centroids(joined, centroid, w_params)
        df_c["region_id"] = region_gdf.iloc[0].get("region_id", "")
        df_c["name"] = region_gdf.iloc[0].get("name", "")
        df_c["parent"] = region_gdf.iloc[0].get("parent", "")
        df_c["parent_name"] = region_gdf.iloc[0].get("parent_name", "")
        centroid_dfs.append(df_c)

    return pd.concat(centroid_dfs, ignore_index=True)


# ---------------------------------------------------------------------------
# Top-level: parse all cached NC files for a region type -> per-month CSVs
# ---------------------------------------------------------------------------


def parse_cached_netcdfs(
    *,
    cache_path: Path,
    output_path: Path,
    geojson_folder: str | Path,
    region_type: str,
    w_params: list[str],
    threshold_km: float = 25.0,
    months: list[tuple[int, int]] | None = None,
) -> list[str]:
    """Parse cached NetCDF files into per-month CSVs at output_path/{year}/{year}_{month}.csv.

    If *months* is given, only process those. Otherwise process all cached files.
    Returns list of output CSV paths.
    """
    region_gdfs = get_region_gdfs(geojson_folder, region_type)
    region_centroids = [get_region_centroids(gdf) for gdf in region_gdfs]

    nc_files: list[tuple[int, int, Path]] = []
    for year_dir in sorted(cache_path.iterdir()):
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        year = int(year_dir.name)
        for f in sorted(year_dir.glob("*")):
            if f.suffix not in (".nc", ".zip"):
                continue
            parts = f.stem.split("_")
            if len(parts) >= 2 and parts[1].isdigit():
                month = int(parts[1])
                if months is None or (year, month) in months:
                    nc_files.append((year, month, f))

    from tqdm import tqdm

    outputs: list[str] = []
    skipped = 0
    parsed = 0
    for year, month, nc_path in tqdm(
        nc_files, desc="Parsing NetCDF files", unit="file"
    ):
        dest_dir = output_path / str(year)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{year}_{month:02d}.csv"
        if dest.exists():
            outputs.append(str(dest))
            skipped += 1
            continue
        log.info("Parsing %s -> %s", nc_path, dest)
        try:
            result = process_month_file(
                nc_path, w_params, region_gdfs, region_centroids, threshold_km
            )
            result.to_csv(dest, index=False)
            outputs.append(str(dest))
            parsed += 1
        except Exception:
            log.exception("Failed to parse %s", nc_path)
    log.info(
        "parse_cached_netcdfs: %d newly parsed, %d reused from cache, %d total",
        parsed,
        skipped,
        len(outputs),
    )
    return outputs
