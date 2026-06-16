"""Parse IHIP-format case data files (Excel or CSV) into daily aggregated counts.

Each row in the IHIP linelist represents one confirmed positive case. Aggregation
is a row count grouped by (region_id, date) → case_count.

Region ID resolution — three strategies, tried in this priority order:

1. LGD code column (if configured via ``lgd_code_column`` and present in the file)
   The LGD code (integer) maps directly to region_id as
   ``"{region_type}_{code}"``, e.g. District Code 502 → ``district_502``.
   No GeoJSON lookup required. Fast and exact. Preferred when available.

2. Geocoding (if ``geocoding.enabled`` is True in config)
   Used when the IHIP file has neither an LGD code column nor reliable lat/lon —
   only a free-text address column. Each row's address (composed with context
   columns for disambiguation) is geocoded via Google, validated against
   source-specific tokens (Murugeshpalya-bug guard — see ``lib/geocoding.py``),
   and the resulting coords are PIP'd against the region's geojson layer.

3. Spatial join (lat/lon point-in-polygon via GeoJSON)
   Used as a fallback when the file already has lat/lon columns and no other
   resolver is configured. Works for any region_type (district, ward, etc.).

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

from pipelines.dengue_prep.configs import PrepGeocodingConfig
from pipelines.dengue_prep.lib.geocoding import (
    DEFAULT_STOPWORDS,
    resolve_via_geocode,
)

log = logging.getLogger(__name__)

DEFAULT_DATE_COLUMN = "Sample Collected Date"
DEFAULT_LATITUDE_COLUMN = "Latitude"
DEFAULT_LONGITUDE_COLUMN = "Longitude"


def _read_file(path: Path, header_row: int = 0) -> pd.DataFrame:
    """Read a single IHIP file (Excel or CSV). For Excel, all sheets are concatenated.

    ``header_row`` is the 0-indexed row containing column headers. The standard
    IHIP exports have headers in row 0; some weekly state exports (e.g. BBMP)
    prepend a banner / title row, in which case set ``header_row=1``.
    """
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        xl = pd.ExcelFile(path)
        sheets = [xl.parse(sheet, header=header_row) for sheet in xl.sheet_names]
        return pd.concat(sheets, ignore_index=True)
    if suffix == ".csv":
        return pd.read_csv(path, low_memory=False, header=header_row)
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
    geocoding_cfg: PrepGeocodingConfig | None = None,
) -> str:
    """Determine the region_id resolution method for this file.

    Returns one of: ``'lgd'``, ``'geocode'``, ``'spatial'``.

    Priority order (highest first):
      1. ``'lgd'``    — if ``lgd_code_column`` is configured AND present in the file
      2. ``'geocode'`` — if ``geocoding_cfg.enabled`` AND the address column is present
      3. ``'spatial'`` — if ``lat_column``/``lon_column`` are present

    Logs a warning if a higher-priority resolver was configured but its
    column(s) are missing — falls through to the next available option.
    Raises if no resolver applies.
    """
    cols = set(df.columns)
    has_lgd = bool(lgd_code_column) and lgd_code_column in cols
    has_geocode = (
        geocoding_cfg is not None
        and geocoding_cfg.enabled
        and bool(geocoding_cfg.address_fields)
        and geocoding_cfg.address_fields[0] in cols
    )
    has_latlon = lat_column in cols and lon_column in cols

    if has_lgd:
        log.info(
            "ihip parser: file %r → using LGD code column %r",
            path.name,
            lgd_code_column,
        )
        return "lgd"

    if lgd_code_column and not has_lgd:
        log.warning(
            "ihip parser: file %r is missing configured LGD code column %r — "
            "trying next resolver",
            path.name,
            lgd_code_column,
        )

    if has_geocode:
        log.info(
            "ihip parser: file %r → using geocoding resolver (address_fields %r)",
            path.name,
            list(geocoding_cfg.address_fields),
        )
        return "geocode"

    if geocoding_cfg is not None and geocoding_cfg.enabled and not has_geocode:
        first = (
            geocoding_cfg.address_fields[0]
            if geocoding_cfg.address_fields
            else "(empty)"
        )
        log.warning(
            "ihip parser: file %r geocoding enabled but first address_field %r is missing — "
            "trying next resolver",
            path.name,
            first,
        )

    if has_latlon:
        log.info(
            "ihip parser: file %r → using lat/lon spatial join (columns %r, %r)",
            path.name,
            lat_column,
            lon_column,
        )
        return "spatial"

    geocode_fields = list(geocoding_cfg.address_fields) if geocoding_cfg else []
    raise ValueError(
        f"ihip parser: file {path.name!r} has no resolvable region_id source.\n"
        f"  configured LGD column: {lgd_code_column!r} (present={has_lgd})\n"
        f"  geocoding enabled: {bool(geocoding_cfg and geocoding_cfg.enabled)} "
        f"(address_fields={geocode_fields}, primary present={has_geocode})\n"
        f"  lat/lon columns: ({lat_column!r}, {lon_column!r}) (present={has_latlon})\n"
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
    null_mask = df[lat_column].isna() | df[lon_column].isna()
    if null_mask.any():
        raise ValueError(
            f"ihip parser: {null_mask.sum()} rows have missing {lat_column!r}/{lon_column!r}. "
            "Cannot perform spatial join. Either fix the data or configure "
            "lgd_code_column in your config to use LGD code lookup instead."
        )

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
        # Reproject to metric CRS for correct distance calculation
        unmatched_proj = unmatched_points.to_crs("EPSG:32644")
        regions_proj = gdf_regions[["region_id", "geometry"]].to_crs("EPSG:32644")
        nearest = gpd.sjoin_nearest(
            unmatched_proj, regions_proj, how="left"
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
        raise ValueError(
            f"ihip parser: {len(still_unmatched)} row(s) could not be matched to any "
            f"{region_type} polygon even after nearest-neighbour fallback.\n"
            f"Sample coordinates: {sample}\n"
            "Check that coordinates are valid and within the expected region."
        )

    return joined.set_index("idx")["region_id"].reindex(df.index).values


def _apply_geocode_resolver(
    df: pd.DataFrame,
    cfg: PrepGeocodingConfig,
    region_type: str,
    geojson_base: str,
    path: Path,
) -> pd.DataFrame:
    """Run the geocode → spatial-join resolver and apply drop/keep policies.

    Returns the (possibly row-filtered) DataFrame with ``region_id`` attached.
    Rows where geocoding failed get ``NaN`` initially; whether they're kept
    or dropped is controlled by ``cfg.require_validation``. Rows with empty
    addresses are dropped upfront when ``cfg.require_address`` is True.
    """
    primary_col = cfg.address_fields[0] if cfg.address_fields else ""

    if cfg.require_address and primary_col:
        if primary_col in df.columns:
            before = len(df)
            has_addr = df[primary_col].notna() & (
                df[primary_col].astype(str).str.strip() != ""
            )
            df = df.loc[has_addr].copy()
            dropped = before - len(df)
            if dropped:
                log.warning(
                    "ihip parser: file %r dropped %d row(s) with missing %r "
                    "(geocoding.require_address=true)",
                    path.name,
                    dropped,
                    primary_col,
                )

    # Geojson layer is loaded once internally by resolve_via_geocode (handles
    # both <base>/<region_type>/ and <base>/<region_type>s/ layouts).
    geo_dir = Path(geojson_base) / region_type
    if not geo_dir.is_dir():
        geo_dir = Path(geojson_base) / f"{region_type}s"

    stopwords = frozenset(DEFAULT_STOPWORDS | set(cfg.extra_stopwords))
    region_ids = resolve_via_geocode(
        df,
        address_fields=cfg.address_fields,
        fallback_address_fields=cfg.fallback_address_fields,
        cache_file=cfg.cache_file,
        geojson_dir=geo_dir,
        stopwords=stopwords,
        bounds=cfg.bounds,
        restrict_admin_area_tokens=cfg.restrict_admin_area_tokens,
    )
    df = df.copy()
    df["region_id"] = region_ids

    if cfg.require_validation:
        before = len(df)
        df = df.loc[df["region_id"].notna()].copy()
        dropped = before - len(df)
        if dropped:
            log.warning(
                "ihip parser: file %r dropped %d row(s) where geocoding "
                "failed validation or PIP'd outside %s polygons "
                "(geocoding.require_validation=true)",
                path.name,
                dropped,
                region_type,
            )
    return df


def parse_ihip_files(
    folder: str,
    region_type: str,
    geojson_base: str,
    date_column: str = DEFAULT_DATE_COLUMN,
    lgd_code_column: str | None = None,
    lat_column: str = DEFAULT_LATITUDE_COLUMN,
    lon_column: str = DEFAULT_LONGITUDE_COLUMN,
    filters: list[dict] | None = None,
    geocoding_cfg: PrepGeocodingConfig | None = None,
    header_row: int = 0,
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
        df = _read_file(path, header_row=header_row)
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
            df, lgd_code_column, lat_column, lon_column, path, geocoding_cfg
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
        elif method == "geocode":
            assert (
                geocoding_cfg is not None
            )  # for type-checker; _detect_resolution_method guarantees
            df = _apply_geocode_resolver(
                df, geocoding_cfg, region_type, geojson_base, path
            )
        else:
            df["region_id"] = _resolve_via_spatial_join(
                df, geojson_base, region_type, lat_column, lon_column
            )

        log.info(
            "ihip parser: file %r → %d rows, method=%s",
            path.name,
            len(df),
            method,
        )
        frames.append(df[["date", "region_id"]].copy())

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
