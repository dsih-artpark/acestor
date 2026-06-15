"""Parse IHIP-format case data files (Excel or CSV) into daily aggregated counts.

Each row in the IHIP linelist represents one confirmed positive case. Aggregation
is a row count grouped by (region_id, date) → case_count.

Region ID resolution — two strategies, tried in this order:

1. LGD code column (if configured via ``lgd_code_column`` and present in the file)
   The LGD district code (integer) maps directly to region_id as
   ``"{region_type}_{code}"``, e.g. District Code 502 → ``district_502``.
   No GeoJSON lookup required. Fast and exact.

2. Spatial join (lat/lon point-in-polygon via GeoJSON)
   Used when no LGD code column is available or configured.
   Each row's (Latitude, Longitude) is matched against the GeoJSON polygons for
   the configured region_type. Works for any region_type (district, zone, etc.).

Multiple files dropped into the folder are all read, merged, and deduplicated.
If the same (region_id, date) appears across files, the last-processed value wins
(correction semantics — a re-uploaded corrected file overwrites the old count).
"""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

log = logging.getLogger(__name__)

DEFAULT_DATE_COLUMN = "Sample Collected Date"
DEFAULT_LATITUDE_COLUMN = "Latitude"
DEFAULT_LONGITUDE_COLUMN = "Longitude"

# Tolerance for snapping points just outside a polygon to its nearest region.
# Beyond this distance (in metres, EPSG:32644) a point is considered genuinely
# out of bounds and dropped with a warning rather than silently snapped.
SPATIAL_JOIN_NEAREST_TOLERANCE_M = 1000.0


def _read_file(path: Path) -> pd.DataFrame:
    """Read a single IHIP file (Excel or CSV). For Excel, all sheets are concatenated."""
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        xl = pd.ExcelFile(path)
        sheets = [xl.parse(sheet) for sheet in xl.sheet_names]
        return pd.concat(sheets, ignore_index=True)
    if suffix == ".csv":
        return pd.read_csv(path, low_memory=False)
    raise ValueError(
        f"ihip parser: unsupported file type {suffix!r} for file {path.name!r}. "
        "Supported formats: .xlsx, .xls, .csv"
    )


def _validate_columns(df: pd.DataFrame, date_column: str, path: Path) -> None:
    """Validate that the minimum required columns are present (date only)."""
    if date_column not in df.columns:
        raise ValueError(
            f"ihip parser: file {path.name!r} is missing required column: {date_column!r}.\n"
            f"Found columns: {sorted(df.columns.tolist())}"
        )


def _detect_resolution_method(
    df: pd.DataFrame,
    lgd_code_column: str | None,
    lat_column: str,
    lon_column: str,
    path: Path,
) -> str:
    """Determine whether to use LGD code or spatial join for this file.

    Returns 'lgd' or 'spatial'.
    Logs a warning if lgd_code_column was configured but is absent (falls back to spatial).
    Raises if neither LGD nor lat/lon columns are available.
    """
    cols = set(df.columns)
    has_lgd = lgd_code_column and lgd_code_column in cols
    has_latlon = lat_column in cols and lon_column in cols

    if lgd_code_column and not has_lgd:
        if has_latlon:
            log.warning(
                "ihip parser: file %r is missing configured LGD code column %r — "
                "falling back to lat/lon spatial join (using columns %r, %r).",
                path.name,
                lgd_code_column,
                lat_column,
                lon_column,
            )
            return "spatial"
        raise ValueError(
            f"ihip parser: file {path.name!r} is missing LGD code column {lgd_code_column!r} "
            f"and has no lat/lon columns ({lat_column!r}, {lon_column!r}). "
            f"Cannot resolve region_id.\n"
            f"Found columns: {sorted(cols)}"
        )

    if has_lgd:
        log.info(
            "ihip parser: file %r → using LGD code column %r",
            path.name,
            lgd_code_column,
        )
        return "lgd"

    if has_latlon:
        log.info(
            "ihip parser: file %r → using lat/lon spatial join (columns %r, %r)",
            path.name,
            lat_column,
            lon_column,
        )
        return "spatial"

    raise ValueError(
        f"ihip parser: file {path.name!r} has neither LGD code column nor "
        f"lat/lon columns ({lat_column!r}, {lon_column!r}). Cannot resolve region_id.\n"
        f"Found columns: {sorted(cols)}"
    )


def _load_geojson(geojson_base: str, region_type: str) -> gpd.GeoDataFrame:
    """Load all GeoJSON files for region_type into a single GeoDataFrame."""
    import glob

    base = Path(geojson_base)
    candidates = [base / region_type, base / f"{region_type}s"]
    files: list[str] = []
    for candidate in candidates:
        files = glob.glob(str(candidate / "*.geojson"))
        if files:
            break

    if not files:
        tried = [str(c) for c in candidates]
        raise FileNotFoundError(
            f"ihip parser: no GeoJSON files found. Tried: {tried}. "
            "Check data.geojson.base_path in your config."
        )

    gdfs = [gpd.read_file(f) for f in files]
    return gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True), crs=gdfs[0].crs)


def _resolve_via_lgd(
    df: pd.DataFrame,
    lgd_code_column: str,
    region_type: str,
) -> pd.Series:
    """Map LGD code column directly to region_id: e.g. 502 → 'district_502'."""
    codes = pd.to_numeric(df[lgd_code_column], errors="coerce")
    nulls = codes.isna().sum()
    if nulls > 0:
        raise ValueError(
            f"ihip parser: {nulls} rows have unparseable values in LGD code column "
            f"{lgd_code_column!r}."
        )
    return region_type + "_" + codes.astype(int).astype(str)


def _resolve_via_spatial_join(
    df: pd.DataFrame,
    geojson_base: str,
    region_type: str,
    lat_column: str,
    lon_column: str,
) -> pd.Series:
    """Spatial join: assign region_id based on which polygon each lat/lon falls in."""
    gdf_regions = _load_geojson(geojson_base, region_type)

    geometry = [Point(lon, lat) for lon, lat in zip(df[lon_column], df[lat_column])]
    gdf_points = gpd.GeoDataFrame(
        {"idx": df.index}, geometry=geometry, crs=gdf_regions.crs
    )

    joined = gpd.sjoin(
        gdf_points,
        gdf_regions[["region_id", "geometry"]],
        how="left",
        predicate="within",
    )

    # sjoin may produce duplicate rows if a point is on a shared boundary
    joined = joined.drop_duplicates(subset=["idx"], keep="first")

    # Fall back to nearest polygon for border-edge cases (points on boundaries or
    # just outside a polygon due to projection rounding)
    unmatched_idx = joined[joined["region_id"].isna()]["idx"].tolist()
    n_total = len(gdf_points)
    if unmatched_idx:
        log.warning(
            "ihip parser: %d of %d row(s) not matched via 'within' polygon — "
            "falling back to nearest %s polygon (border/rounding cases).",
            len(unmatched_idx),
            n_total,
            region_type,
        )
        unmatched_points = gdf_points[gdf_points["idx"].isin(unmatched_idx)].copy()
        # Reproject to metric CRS so max_distance is in metres.
        unmatched_proj = unmatched_points.to_crs("EPSG:32644")
        regions_proj = gdf_regions[["region_id", "geometry"]].to_crs("EPSG:32644")
        nearest = gpd.sjoin_nearest(
            unmatched_proj,
            regions_proj,
            how="left",
            max_distance=SPATIAL_JOIN_NEAREST_TOLERANCE_M,
        ).drop_duplicates(subset=["idx"], keep="first")
        idx_to_region = nearest.set_index("idx")["region_id"]
        joined.loc[joined["idx"].isin(unmatched_idx), "region_id"] = (
            joined.loc[joined["idx"].isin(unmatched_idx), "idx"]
            .map(idx_to_region)
            .values
        )

    still_unmatched = joined[joined["region_id"].isna()]["idx"].tolist()
    if still_unmatched:
        sample = df.loc[still_unmatched[:5], [lat_column, lon_column]].to_dict(
            "records"
        )
        log.warning(
            "ihip parser: dropping %d of %d row(s) whose coordinates fall outside "
            "every %s polygon (further than %.0fm from the nearest one). "
            "Sample: %s",
            len(still_unmatched),
            n_total,
            region_type,
            SPATIAL_JOIN_NEAREST_TOLERANCE_M,
            sample,
        )

    return joined.set_index("idx")["region_id"].reindex(df.index)


def parse_ihip_files(
    folder: str,
    region_type: str,
    geojson_base: str,
    date_column: str = DEFAULT_DATE_COLUMN,
    lgd_code_column: str | None = None,
    lat_column: str = DEFAULT_LATITUDE_COLUMN,
    lon_column: str = DEFAULT_LONGITUDE_COLUMN,
    filters: list[dict] | None = None,
) -> pd.DataFrame:
    """Read all IHIP files from folder, aggregate to (date, region_id, case_count).

    Args:
        folder: Path to the folder containing IHIP files.
        region_type: Region type string (e.g. "district") — used for spatial join
            and for constructing region_id from LGD codes.
        geojson_base: Base path to GeoJSON files. Used for spatial join if
            lgd_code_column is not set.
        date_column: Column in the IHIP file to use as the case date.
            Defaults to "Sample Collected Date".
        lgd_code_column: If set, use this column for direct LGD code → region_id
            mapping instead of spatial join. Example: "District Code".
        lat_column: Column name for latitude (used in spatial join). Defaults to "Latitude".
        lon_column: Column name for longitude (used in spatial join). Defaults to "Longitude".
        filters: List of {column, values} dicts. Rows must match ALL entries (AND);
            within each entry any value in the list matches (OR).
            Example: [{"column": "Confirmed Diagnosis", "values": ["Dengue"]}]

    Returns:
        DataFrame with columns: date (datetime), region_id (str), case_count (int)
    """
    folder_path = Path(folder)
    if not folder_path.is_dir():
        raise FileNotFoundError(
            f"ihip parser: folder not found: {folder!r}. "
            "Check data.case_download.source_path in your config."
        )

    files = sorted(
        p
        for p in folder_path.iterdir()
        if p.suffix.lower() in (".xlsx", ".xls", ".csv") and not p.name.startswith("~")
    )
    if not files:
        raise FileNotFoundError(
            f"ihip parser: no .xlsx/.xls/.csv files found in {folder!r}."
        )

    # Read, validate, and resolve region_id per file
    frames: list[pd.DataFrame] = []
    log.info(
        "ihip parser: found %d file(s) in %r: %s",
        len(files),
        str(folder_path),
        [f.name for f in files],
    )
    for path in files:
        df = _read_file(path)
        log.info("ihip parser: file %r → read %d rows", path.name, len(df))
        _validate_columns(df, date_column, path)

        # Row filters — AND across entries, OR within each entry's values list
        for f in filters or []:
            col, vals = f["column"], f["values"]
            if col not in df.columns:
                log.warning(
                    "ihip parser: file %r is missing filter column %r — skipping this filter.",
                    path.name,
                    col,
                )
                continue
            before = len(df)
            df = df[df[col].isin(vals)].copy()
            log.info(
                "ihip parser: file %r filtered %r in %r → kept %d/%d rows",
                path.name,
                col,
                vals,
                len(df),
                before,
            )
        method = _detect_resolution_method(
            df, lgd_code_column, lat_column, lon_column, path
        )

        # Parse dates per file — fail loudly on unparseable values
        try:
            df["date"] = pd.to_datetime(df[date_column], dayfirst=True)
        except Exception as exc:
            raise ValueError(
                f"ihip parser: could not parse date column {date_column!r} in {path.name!r}. "
                f"Ensure all values are valid dates. Error: {exc}"
            ) from exc

        n_before_dropna = len(df)
        df = df.dropna(subset=["date"])
        n_dropped_dates = n_before_dropna - len(df)
        if n_dropped_dates:
            log.warning(
                "ihip parser: file %r dropped %d row(s) with unparseable/missing dates "
                "(original date column %r — check for blank or malformed date values)",
                path.name,
                n_dropped_dates,
                date_column,
            )

        if method == "lgd":
            df["region_id"] = _resolve_via_lgd(df, lgd_code_column, region_type)
        else:
            n_before_coord_drop = len(df)
            df = df.dropna(subset=[lat_column, lon_column])
            n_dropped_coords = n_before_coord_drop - len(df)
            if n_dropped_coords:
                log.warning(
                    "ihip parser: file %r dropped %d row(s) with missing %r/%r "
                    "before spatial join.",
                    path.name,
                    n_dropped_coords,
                    lat_column,
                    lon_column,
                )
            if df.empty:
                continue
            df["region_id"] = _resolve_via_spatial_join(
                df, geojson_base, region_type, lat_column, lon_column
            )
            n_before_region_drop = len(df)
            df = df.dropna(subset=["region_id"])
            n_dropped_oob = n_before_region_drop - len(df)
            if n_dropped_oob:
                log.warning(
                    "ihip parser: file %r dropped %d row(s) whose coordinates did "
                    "not fall within any %s polygon.",
                    path.name,
                    n_dropped_oob,
                    region_type,
                )

        log.info(
            "ihip parser: file %r → %d rows, method=%s",
            path.name,
            len(df),
            method,
        )
        frames.append(df[["date", "region_id"]].copy())

    if not frames:
        log.warning(
            "ihip parser: all input rows were dropped (missing dates / coordinates "
            "/ out-of-bounds points). Returning empty result."
        )
        return pd.DataFrame(columns=["date", "region_id", "case_count"])

    combined = pd.concat(frames, ignore_index=True)
    log.info(
        "ihip parser: combined %d file(s) → %d total individual case rows before aggregation",
        len(frames),
        len(combined),
    )

    # Aggregate: each row = one case
    result = (
        combined.groupby(["region_id", "date"])
        .size()
        .reset_index(name="case_count")[["date", "region_id", "case_count"]]
        .sort_values(["date", "region_id"])
        .reset_index(drop=True)
    )
    log.info(
        "ihip parser: aggregated %d individual cases → %d unique (region_id, date) groups; "
        "date range %s → %s",
        len(combined),
        len(result),
        result["date"].min().date() if not result.empty else "n/a",
        result["date"].max().date() if not result.empty else "n/a",
    )
    return result
