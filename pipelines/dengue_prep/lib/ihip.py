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
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point
from tqdm import tqdm

from pipelines.dengue_prep.configs import PrepGeocodingConfig
from pipelines.dengue_prep.lib.summary import CaseParseStats, FileParseStats
from pipelines.dengue_prep.lib.geocoding import (
    DEFAULT_STOPWORDS,
    resolve_via_geocode,
)

log = logging.getLogger(__name__)

DEFAULT_DATE_COLUMN = "Sample Collected Date"
DEFAULT_LATITUDE_COLUMN = "Latitude"
DEFAULT_LONGITUDE_COLUMN = "Longitude"

# Tolerance for snapping points just outside a polygon to its nearest region.
# Beyond this distance (in metres, EPSG:32644) a point is considered genuinely
# out of bounds and dropped with a warning rather than silently snapped.
SPATIAL_JOIN_NEAREST_TOLERANCE_M = 1000.0


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
    region_id_column: str | None = None,
) -> str:
    """Determine the region_id resolution method for this file.

    Returns one of: ``'region_id'``, ``'lgd'``, ``'geocode'``, ``'spatial'``.

    Priority order (highest first):
      1. ``'region_id'`` — if ``region_id_column`` is configured AND present in the
         file. Trust a pre-resolved column from an upstream system (e.g. the
         dashboard's `Region Id` export). Skips all on-the-fly resolution.
      2. ``'lgd'``    — if ``lgd_code_column`` is configured AND present in the file
      3. ``'geocode'`` — if ``geocoding_cfg.enabled`` AND the address column is present
      4. ``'spatial'`` — if ``lat_column``/``lon_column`` are present

    Logs a warning if a higher-priority resolver was configured but its
    column(s) are missing — falls through to the next available option.
    Raises if no resolver applies.
    """
    cols = set(df.columns)
    has_region_id = bool(region_id_column) and region_id_column in cols
    has_lgd = bool(lgd_code_column) and lgd_code_column in cols
    has_geocode = (
        geocoding_cfg is not None
        and geocoding_cfg.enabled
        and bool(geocoding_cfg.address_fields)
        and geocoding_cfg.address_fields[0] in cols
    )
    has_latlon = lat_column in cols and lon_column in cols

    if has_region_id:
        log.info(
            "ihip parser: file %r → using pre-resolved region_id column %r",
            path.name,
            region_id_column,
        )
        return "region_id"

    if region_id_column and not has_region_id:
        log.warning(
            "ihip parser: file %r is missing configured region_id_column %r — "
            "trying next resolver",
            path.name,
            region_id_column,
        )

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
        f"  configured region_id_column: {region_id_column!r} (present={has_region_id})\n"
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
    # Reproject every gdf onto the first one's CRS before concat. Two files can
    # both declare "WGS 84" yet fail the concat's common-CRS check because
    # their WKT strings differ (files exported at different times / by different
    # tools). Normalising via to_crs() collapses those into a single instance.
    target_crs = gdfs[0].crs
    gdfs = [g.to_crs(target_crs) for g in gdfs]
    return gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True), crs=target_crs)


@lru_cache(maxsize=32)
def _valid_region_ids(geojson_base: str, region_type: str) -> frozenset[str]:
    """Return the frozen set of region_ids present in the ``region_type`` geojsons.

    Cached across calls (per geojson_base + region_type) because prep parses
    many raw files and we don't want to re-scan the geojson tree for each one.
    """
    try:
        gdf = _load_geojson(geojson_base, region_type)
    except FileNotFoundError:
        # If no geojson exists for this region_type the caller can decide;
        # returning an empty set makes the allowlist a no-op.
        return frozenset()
    if "region_id" not in gdf.columns:
        return frozenset()
    return frozenset(gdf["region_id"].dropna().astype(str).unique().tolist())


@lru_cache(maxsize=32)
def _region_id_to_name(geojson_base: str, region_type: str) -> dict[str, str]:
    """Return ``{region_id: name}`` for every region in the ``region_type`` geojsons.

    Cached alongside :func:`_valid_region_ids`. Used by the prep-summary
    renderer to display human-readable names next to bare IDs.
    """
    try:
        gdf = _load_geojson(geojson_base, region_type)
    except FileNotFoundError:
        return {}
    if "region_id" not in gdf.columns or "name" not in gdf.columns:
        return {}
    return {
        str(rid): str(n)
        for rid, n in zip(gdf["region_id"], gdf["name"])
        if pd.notna(rid) and pd.notna(n)
    }


@lru_cache(maxsize=8)
def _scan_geojson_hierarchy(geojson_base: str) -> dict[str, dict[str, str]]:
    """Scan every ``*.geojson`` subdirectory under ``geojson_base`` and return
    a global lookup: ``{region_id: {"name", "parent_id", "region_type"}}``.

    ``region_type`` is derived from the containing subdirectory name (with
    a trailing ``'s'`` stripped if plural — matches the ``_load_geojson``
    convention).  This is state-agnostic: same code handles GBA's
    ``corp / zone / ward`` and Odisha's ``district / block / ulb / ulb_ward``
    without any hard-coded labels.
    """
    base = Path(geojson_base)
    out: dict[str, dict[str, str]] = {}
    if not base.exists():
        return out
    for subdir in sorted(base.iterdir()):
        if not subdir.is_dir():
            continue
        region_type = subdir.name[:-1] if subdir.name.endswith("s") else subdir.name
        for f in subdir.rglob("*.geojson"):
            try:
                gdf = gpd.read_file(f)
            except Exception:
                continue
            if "region_id" not in gdf.columns:
                continue
            for _, row in gdf.iterrows():
                rid = row.get("region_id")
                if pd.isna(rid):
                    continue
                parent = row.get("parent")
                out[str(rid)] = {
                    "name": (
                        str(row.get("name", "")) if pd.notna(row.get("name")) else ""
                    ),
                    "parent_id": str(parent) if pd.notna(parent) else "",
                    "region_type": region_type,
                }
    return out


def _walk_hierarchy(
    region_id: str, hierarchy: dict[str, dict[str, str]]
) -> list[dict[str, str]]:
    """Walk parents from ``region_id`` up to the root.

    Returns a list ordered **root → leaf**. Each entry has
    ``{"region_type", "id", "name"}``. Empty list if ``region_id`` isn't in
    ``hierarchy``.
    """
    chain: list[dict[str, str]] = []
    seen: set[str] = set()
    cur = region_id
    while cur and cur in hierarchy and cur not in seen:
        seen.add(cur)
        info = hierarchy[cur]
        chain.append(
            {"region_type": info["region_type"], "id": cur, "name": info["name"]}
        )
        cur = info["parent_id"]
    chain.reverse()
    return chain


def _resolve_via_region_id_column(
    df: pd.DataFrame,
    region_id_column: str,
    region_type: str,
    path: Path,
    geojson_base: str,
) -> pd.Series:
    """Trust a pre-resolved region_id column verbatim.

    Strips whitespace. Rows at the target ``<region_type>_*`` granularity are
    kept as-is; rows at a finer granularity (e.g. ward IDs in a corp-level run)
    are rolled up via the geojson ``parent`` chain. Rows whose ID can't be
    rolled up — wrong scope, broken chain — return NaN; the caller drops them.
    """
    raw = df[region_id_column].astype("string").str.strip()
    raw = raw.where(raw.notna() & (raw != ""), other=pd.NA)
    # Longest-prefix classification (not naive startswith) so ``ulb_ward_...``
    # doesn't get misclassified as already-at-``ulb`` for a target=ulb run —
    # ``ulb_ward`` starts with ``ulb_`` too. Scan the geojson tree once for the
    # set of known region types (dir names, minus trailing 's') and pick the
    # longest matching prefix per row.
    known_types = _scan_known_region_types(geojson_base)
    known_types_sorted = sorted(known_types, key=len, reverse=True)

    def _classify(rid: str) -> str | None:
        for t in known_types_sorted:
            if rid.startswith(f"{t}_"):
                return t
        return rid.split("_", 1)[0] if "_" in rid else None

    row_type = raw.map(lambda r: _classify(r) if pd.notna(r) else None)
    needs_rollup = raw.notna() & (row_type != region_type)
    expected_prefix = f"{region_type}_"
    if needs_rollup.any():
        parent_map = _build_parent_map(geojson_base)
        rolled = raw[needs_rollup].map(
            lambda rid: _walk_to_prefix(
                rid, expected_prefix, parent_map, classify=_classify
            )
        )
        n_resolved = int(rolled.notna().sum())
        n_unresolvable = int(rolled.isna().sum())
        log.info(
            "ihip parser: file %r rolled up %d of %d row(s) from %r-prefix IDs "
            "to %r via geojson parent chain",
            path.name,
            n_resolved,
            int(needs_rollup.sum()),
            ", ".join(
                sorted(
                    {str(s).split("_")[0] for s in raw[needs_rollup].dropna().head(20)}
                )
            ),
            expected_prefix.rstrip("_"),
        )
        if n_unresolvable:
            samples = raw[needs_rollup][rolled.isna()].head(5).tolist()
            log.warning(
                "ihip parser: file %r found %d row(s) in column %r whose region_id "
                "could not be rolled up to %r via parent chain — dropping. Sample: %s",
                path.name,
                n_unresolvable,
                region_id_column,
                expected_prefix,
                samples,
            )
        raw.loc[needs_rollup] = rolled
    return raw


def _scan_known_region_types(geojson_base: str) -> list[str]:
    """Return the region-type names present under ``geojson_base``.

    Dir names in the geojson tree are plural (``ulbs/``, ``ulb_wards/``,
    ``blocks/`` ...). Strip the trailing 's' so the returned names match the
    ``region_id`` prefix (``ulb_``, ``ulb_ward_``, ``block_`` ...).
    """
    out: list[str] = []
    try:
        for sub in Path(geojson_base).iterdir():
            if not sub.is_dir():
                continue
            name = sub.name[:-1] if sub.name.endswith("s") else sub.name
            out.append(name)
    except Exception:
        pass
    return out


def _build_parent_map(geojson_base: str) -> dict[str, str]:
    """Scan every geojson under ``geojson_base`` and return ``{region_id: parent_id}``."""
    import json

    parent_map: dict[str, str] = {}
    for path in Path(geojson_base).rglob("*.geojson"):
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        features = data.get("features", [data]) if isinstance(data, dict) else []
        for feat in features:
            props = feat.get("properties", {}) if isinstance(feat, dict) else {}
            rid = props.get("region_id")
            parent = props.get("parent")
            if rid and parent and rid not in parent_map:
                parent_map[rid] = parent
    return parent_map


def _walk_to_prefix(
    rid: Any,
    target_prefix: str,
    parent_map: dict[str, str],
    max_depth: int = 6,
    classify: Any = None,
) -> Any:
    """Walk the parent chain from ``rid`` until it matches the target level.

    ``classify`` (optional) is a callable that returns the region_type of a
    region_id via longest-prefix match against the known types. When provided,
    match on ``classify(cur) == target_prefix.rstrip('_')`` — avoids the
    ``ulb_ward_`` vs ``ulb_`` collision. Without it, falls back to naive
    ``startswith`` (compat).
    """
    target_type = target_prefix.rstrip("_")
    cur = rid
    for _ in range(max_depth):
        if not isinstance(cur, str) or not cur:
            return pd.NA
        matched = (
            classify(cur) == target_type
            if classify is not None
            else cur.startswith(target_prefix)
        )
        if matched:
            return cur
        nxt = parent_map.get(cur)
        if not nxt or nxt == cur:
            return pd.NA
        cur = nxt
    return pd.NA


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
    date_column: str | list[str] = DEFAULT_DATE_COLUMN,
    lgd_code_column: str | None = None,
    lat_column: str = DEFAULT_LATITUDE_COLUMN,
    lon_column: str = DEFAULT_LONGITUDE_COLUMN,
    filters: list[dict] | None = None,
    geocoding_cfg: PrepGeocodingConfig | None = None,
    header_row: int = 0,
    region_id_column: str | None = None,
    stats: "CaseParseStats | None" = None,
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
    # Normalise date_column to a list of candidates. String → single-element list.
    date_column_candidates: list[str] = (
        [date_column] if isinstance(date_column, str) else list(date_column)
    )
    if not date_column_candidates:
        raise ValueError(
            "parse_ihip_files: date_column must be a non-empty string or list of strings"
        )

    # Progress bar over files. Each file can carry hundreds of thousands of
    # rows and go through multiple stages (read → filter → resolve → date
    # parse), so per-file granularity is the useful signal without adding
    # per-row overhead. TTY-only — non-interactive log capture (cron, CI, log
    # files) stays clean.
    file_iter = tqdm(
        files,
        desc=f"parse_ihip[{region_type}]",
        unit="file",
        disable=not sys.stderr.isatty(),
        file=sys.stderr,
        leave=False,
    )
    for path in file_iter:
        if hasattr(file_iter, "set_postfix_str"):
            file_iter.set_postfix_str(path.name, refresh=False)
        df = _read_file(path, header_row=header_row)
        log.info("ihip parser: file %r → read %d rows", path.name, len(df))

        # Per-file stats accumulator (populated at each stage below).
        fstats = (
            FileParseStats(filename=path.name, read=len(df))
            if stats is not None
            else None
        )

        # Skip truly empty files (headers only, zero rows). Dashboard exports
        # for a window with no matching cases come back as ~5 KB xlsx with the
        # 30 IHIP headers but no data — the caller shouldn't have to filter
        # these out ahead of time.
        if df.empty:
            log.info(
                "ihip parser: file %r is empty (headers only) — skipping.",
                path.name,
            )
            if fstats is not None and stats is not None:
                stats.files.append(fstats)
            continue

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
        if fstats is not None:
            fstats.after_filter = len(df)
            if stats is not None and fstats.read > fstats.after_filter:
                stats.drop_reasons["Filtered out by row filters (e.g. non-Dengue)"] += (
                    fstats.read - fstats.after_filter
                )

        method = _detect_resolution_method(
            df,
            lgd_code_column,
            lat_column,
            lon_column,
            path,
            geocoding_cfg,
            region_id_column=region_id_column,
        )
        if fstats is not None:
            fstats.resolution_method = method

        # Parse dates per file. Walk the candidate list top-to-bottom and pick
        # the first column that yields any parseable rows. IHIP linelists are
        # dd/mm/yyyy; dashboard exports are ISO yyyy-mm-dd — try both formats
        # for each candidate and keep whichever gives more successes.
        chosen_col: str | None = None
        parsed_series: pd.Series | None = None
        candidate_summary: list[tuple[str, str, int]] = []  # (col, why, n_parseable)
        for candidate in date_column_candidates:
            if candidate not in df.columns:
                candidate_summary.append((candidate, "missing", 0))
                continue
            raw_dates = df[candidate]
            parsed_dayfirst = pd.to_datetime(raw_dates, dayfirst=True, errors="coerce")
            parsed_iso = pd.to_datetime(raw_dates, errors="coerce")
            parsed = (
                parsed_iso
                if parsed_iso.notna().sum() >= parsed_dayfirst.notna().sum()
                else parsed_dayfirst
            )
            n_parseable = int(parsed.notna().sum())
            candidate_summary.append((candidate, "present", n_parseable))
            if n_parseable > 0:
                chosen_col = candidate
                parsed_series = parsed
                break

        if chosen_col is None or parsed_series is None:
            # No candidate yielded any parseable rows — that's a hard failure,
            # not a silent drop. Surface which candidates were tried and how
            # many rows parsed for each so the operator can fix the source.
            detail = "; ".join(
                f"{c!r}:{why}({n} parseable)" for c, why, n in candidate_summary
            )
            raise ValueError(
                f"ihip parser: file {path.name!r} has no usable date column. "
                f"Tried candidates in order — {detail}. "
                f"Available columns in file: {sorted(df.columns.tolist())}."
            )

        if chosen_col != date_column_candidates[0]:
            log.info(
                "ihip parser: file %r using date candidate %r (earlier "
                "candidates absent or empty: %s)",
                path.name,
                chosen_col,
                [c for c, _, _ in candidate_summary if c != chosen_col],
            )
        else:
            log.info(
                "ihip parser: file %r using date column %r",
                path.name,
                chosen_col,
            )

        if fstats is not None:
            fstats.date_column_used = chosen_col
            fstats.date_column_was_fallback = chosen_col != date_column_candidates[0]

        df["date"] = parsed_series
        n_before_dropna = len(df)
        df = df.dropna(subset=["date"])
        n_dropped_dates = n_before_dropna - len(df)
        if n_dropped_dates:
            log.warning(
                "ihip parser: file %r dropped %d row(s) with unparseable/missing dates "
                "(chose date column %r — remaining rows without parseable dates in that column)",
                path.name,
                n_dropped_dates,
                chosen_col,
            )
            if stats is not None:
                stats.drop_reasons[
                    "Date column blank / unparseable in chosen candidate"
                ] += n_dropped_dates
        if fstats is not None:
            fstats.after_date_parse = len(df)

        if method == "region_id":
            assert region_id_column is not None
            df = df.copy()
            df["region_id"] = _resolve_via_region_id_column(
                df, region_id_column, region_type, path, geojson_base
            )
            n_before_drop = len(df)
            df = df.dropna(subset=["region_id"])
            n_dropped = n_before_drop - len(df)
            if n_dropped:
                log.warning(
                    "ihip parser: file %r dropped %d row(s) with missing or "
                    "malformed pre-resolved %r",
                    path.name,
                    n_dropped,
                    region_id_column,
                )
                if stats is not None:
                    stats.drop_reasons[
                        "region_id resolver: parent-chain didn't reach target level"
                    ] += n_dropped
        elif method == "lgd":
            df["region_id"] = _resolve_via_lgd(df, lgd_code_column, region_type)
        elif method == "geocode":
            assert (
                geocoding_cfg is not None
            )  # for type-checker; _detect_resolution_method guarantees
            df = _apply_geocode_resolver(
                df, geocoding_cfg, region_type, geojson_base, path
            )
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
                if stats is not None:
                    stats.drop_reasons[
                        "Spatial resolver: missing latitude/longitude"
                    ] += n_dropped_coords
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
                if stats is not None:
                    stats.drop_reasons[
                        f"Spatial resolver: point outside every {region_type} polygon"
                    ] += n_dropped_oob

        if fstats is not None:
            fstats.after_region_resolve = len(df)

        # Universal allowlist: keep only rows whose region_id exists in the
        # target geojson. Resolvers can produce region_ids for scopes we don't
        # actually model (e.g. LGD code 348 → 'district_348' when Odisha's
        # geojson holds 8 specific districts; a rolled-up parent chain landing
        # on a scope we don't own). Enforce the guard here so no downstream
        # step (train_and_predict, downscale) has to defend against unknown
        # regions.
        if not df.empty:
            valid_ids = _valid_region_ids(geojson_base, region_type)
            if valid_ids:
                keep = df["region_id"].isin(valid_ids)
                n_dropped_oos = int((~keep).sum())
                if n_dropped_oos:
                    dropped_ids = sorted(df.loc[~keep, "region_id"].unique().tolist())
                    log.warning(
                        "ihip parser: file %r dropping %d row(s) with region_id "
                        "not present in %s geojson (out of scope): %s",
                        path.name,
                        n_dropped_oos,
                        region_type,
                        dropped_ids[:10] + (["..."] if len(dropped_ids) > 10 else []),
                    )
                    df = df[keep].copy()
                    if stats is not None:
                        stats.drop_reasons[
                            f"Out-of-scope region_id (not in {region_type} geojson)"
                        ] += n_dropped_oos

        if fstats is not None:
            fstats.after_scope_check = len(df)
            fstats.kept = len(df)
            stats.files.append(fstats)  # type: ignore[union-attr]

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
