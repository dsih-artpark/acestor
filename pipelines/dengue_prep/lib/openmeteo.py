"""OpenMeteo historical weather download for dengue_prep pipeline.

Fetches ERA5-reanalysis data from the Open-Meteo archive API (free, no credentials).
Writes one CSV per month per region_type — same format as CDS-parsed CSVs — so the
existing parse_weather_data step works unchanged.

Uses the daily API endpoint (not hourly) — OpenMeteo pre-aggregates on their side:
    temperature_2m_mean → t2m   (mean °C per day)
    dew_point_2m_mean   → d2m   (mean °C per day)
    precipitation_sum   → tp    (sum mm per day)

Column names match CDS short names so weather.normalise_columns handles them.

Unit difference vs CDS/ERA5:
  Temperature / dew point: OpenMeteo returns °C, ERA5 returns K.
  Precipitation: OpenMeteo returns mm, ERA5 returns m.
  If you are mixing CDS and OpenMeteo data, set `convert_units: true` in config
  to convert OpenMeteo output to ERA5 units (t2m/d2m += 273.15 K, tp /= 1000).

Download modes
--------------
download_range  — batches of regions fetched sequentially for the full date range.
download_month  — one API call for all regions for a single calendar month.

Rate limits (free tier — API call *units*, not HTTP requests):
  600 units/min · 5 000 units/hour · 10 000 units/day
  Cost example: 50 regions × 1 year × 3 variables ≈ 391 units per call.
  For 688 mandals batched in groups of 50, each multi-year chunk costs ~1 800 units
  and exceeds the per-minute budget — batches are therefore run sequentially so the
  natural network latency (~1–2 s/call) keeps throughput within the hourly limit.
"""

from __future__ import annotations

import glob as _glob
import logging
import time
import warnings
from calendar import monthrange
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

log = logging.getLogger(__name__)


def load_region_gdfs(
    geojson_base: str | Path, region_type: str
) -> list[gpd.GeoDataFrame]:
    """Load per-region GeoDataFrames from geojson_base/{region_type}s/ (or {region_type}/).

    Returns one GeoDataFrame per GeoJSON file, reprojected to EPSG:4326.
    """
    base = Path(geojson_base)
    for folder in (base / f"{region_type}s", base / region_type):
        files = sorted(_glob.glob(str(folder / "*.geojson")))
        if files:
            break
    else:
        files = []
    return [gpd.read_file(f).to_crs("EPSG:4326") for f in files]


_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
_DAILY_VARS = ["temperature_2m_mean", "dew_point_2m_mean", "precipitation_sum"]

# OpenMeteo daily name → CDS short name (so normalise_columns picks them up)
_COL_RENAME = {
    "temperature_2m_mean": "t2m",
    "dew_point_2m_mean": "d2m",
    "precipitation_sum": "tp",
    "time": "time",  # kept as-is; normalise_columns maps time → date
}

# ---------------------------------------------------------------------------
# Rate limit state
# Probe results: each API call takes ~0.9-1.1s naturally, which limits us to
# ~60 req/min regardless. No artificial throttle needed. We only need to handle
# 429s reactively (read Retry-After and wait).
# ---------------------------------------------------------------------------
_rate_limited_until: float = 0.0
_total_api_calls: int = 0


def _mark_request_done() -> None:
    global _total_api_calls
    _total_api_calls += 1


def _record_rate_limit(response) -> float:
    """Read Retry-After from a 429 response, set global cooldown, return wait seconds."""
    global _rate_limited_until
    try:
        retry_after = int(response.headers.get("Retry-After", 60))
    except (ValueError, AttributeError):
        retry_after = 60
    wait = retry_after + 5  # small safety margin
    _rate_limited_until = time.monotonic() + wait
    return wait


# ---------------------------------------------------------------------------
# Low-level fetch helpers
# ---------------------------------------------------------------------------


def _fetch_multi(
    lats: list[float],
    lons: list[float],
    start_date: str,
    end_date: str,
    *,
    retries: int = 3,
    retry_delay: float = 5.0,
    timeout: int = 120,
) -> list[pd.DataFrame]:
    """Single HTTP call to OpenMeteo for multiple locations at once.

    Returns a list of DataFrames (one per location), in the same order as lats/lons.
    """
    params = {
        "latitude": ",".join(str(lat) for lat in lats),
        "longitude": ",".join(str(lon) for lon in lons),
        "start_date": start_date,
        "end_date": end_date,
        "daily": ",".join(_DAILY_VARS),
        "timezone": "UTC",
    }
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        # Honour any active 429 cooldown before sending
        rl_remaining = _rate_limited_until - time.monotonic()
        if rl_remaining > 0:
            log.info("OpenMeteo: rate-limit cooldown, waiting %.0fs ...", rl_remaining)
            time.sleep(rl_remaining)
        try:
            resp = requests.get(_ARCHIVE_URL, params=params, timeout=timeout)
            if resp.status_code == 429:
                wait = _record_rate_limit(resp)
                log.warning(
                    "OpenMeteo multi-fetch 429 rate-limited %s→%s (attempt %d/%d, waiting %.0fs)",
                    start_date,
                    end_date,
                    attempt,
                    retries,
                    wait,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            results = resp.json()
            _mark_request_done()
            # Single location returns dict, multiple returns list
            if isinstance(results, dict):
                results = [results]
            dfs = []
            for item in results:
                df = pd.DataFrame(item["daily"])
                df = df.rename(
                    columns={k: v for k, v in _COL_RENAME.items() if k in df.columns}
                )
                df["time"] = pd.to_datetime(df["time"])
                dfs.append(df)
            return dfs
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                log.warning(
                    "OpenMeteo multi-fetch attempt %d/%d failed %s→%s (waiting %.0fs): %s",
                    attempt,
                    retries,
                    start_date,
                    end_date,
                    retry_delay,
                    exc,
                )
                time.sleep(retry_delay)
    raise RuntimeError(
        f"OpenMeteo multi-fetch failed after {retries} attempts "
        f"{start_date}→{end_date}: {last_exc}"
    ) from last_exc


def _centroid(gdf) -> tuple[float, float]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        c = gdf.geometry.centroid.iloc[0]
    return round(c.y, 4), round(c.x, 4)


def _apply_unit_conversion(df: pd.DataFrame) -> pd.DataFrame:
    """Convert OpenMeteo native units to ERA5 units (°C→K for t2m/d2m, mm→m for tp)."""
    df = df.copy()
    df["t2m"] = df["t2m"] + 273.15
    df["d2m"] = df["d2m"] + 273.15
    df["tp"] = df["tp"] / 1000.0
    return df


def download_month(
    year: int,
    month: int,
    region_gdfs: list,
    output_path: Path,
    *,
    convert_units: bool = False,
    region_batch_size: int = 50,
) -> Path:
    """Download one month for all regions, batched to avoid URL-length limits.

    Splits regions into batches of `region_batch_size` (default 50) and makes one
    API call per batch, sequentially. Used for incremental runs (only missing months).
    Returns path of written CSV.
    """
    dest_dir = output_path / str(year)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{year}_{month:02d}.csv"

    _, last_day = monthrange(year, month)
    start_date = f"{year}-{month:02d}-01"
    end_date = f"{year}-{month:02d}-{last_day:02d}"

    lats = [_centroid(gdf)[0] for gdf in region_gdfs]
    lons = [_centroid(gdf)[1] for gdf in region_gdfs]
    n_batches = (len(region_gdfs) + region_batch_size - 1) // region_batch_size

    all_dfs: list[pd.DataFrame] = []
    for batch_idx in range(n_batches):
        batch_slice = slice(
            batch_idx * region_batch_size, (batch_idx + 1) * region_batch_size
        )
        batch_gdfs = region_gdfs[batch_slice]
        batch_lats = lats[batch_slice]
        batch_lons = lons[batch_slice]
        try:
            dfs = _fetch_multi(batch_lats, batch_lons, start_date, end_date)
        except Exception:
            log.exception(
                "OpenMeteo: failed to fetch %d-%02d (batch %d/%d, regions %d-%d)",
                year,
                month,
                batch_idx + 1,
                n_batches,
                batch_slice.start,
                min(batch_slice.stop, len(region_gdfs)) - 1,
            )
            continue
        for gdf, df in zip(batch_gdfs, dfs):
            row = gdf.iloc[0]
            if convert_units:
                df = _apply_unit_conversion(df)
            df["region_id"] = row.get("region_id", "")
            df["name"] = row.get("name", "")
            df["parent"] = row.get("parent", "")
            df["parent_name"] = row.get("parent_name", "")
            all_dfs.append(df)

    combined = (
        pd.concat(all_dfs, ignore_index=True)
        if all_dfs
        else pd.DataFrame(
            columns=[
                "time",
                "t2m",
                "d2m",
                "tp",
                "region_id",
                "name",
                "parent",
                "parent_name",
            ]
        )
    )
    combined.to_csv(dest, index=False)
    log.info(
        "OpenMeteo: wrote %d rows → %s (%d regions)", len(combined), dest, len(all_dfs)
    )
    return dest


def download_range(
    start: pd.Timestamp,
    end: pd.Timestamp,
    region_gdfs: list,
    output_path: Path,
    *,
    convert_units: bool = False,
    region_batch_size: int = 50,
) -> list[Path]:
    """Download full date range for all regions, batched to avoid URL-length limits.

    Splits regions into batches of `region_batch_size` (default 50) and makes one
    API call per batch, sequentially. Splits results into monthly CSVs (same layout
    as download_month) so incremental logic on subsequent daily runs works unchanged.

    Batches run sequentially — OpenMeteo's free tier costs ~391 API units per
    50-region call, which easily exceeds the 600-units/min budget if run in parallel.

    Returns list of written CSV paths.
    """
    lats = [_centroid(gdf)[0] for gdf in region_gdfs]
    lons = [_centroid(gdf)[1] for gdf in region_gdfs]

    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    from tqdm import tqdm

    n_batches = (len(region_gdfs) + region_batch_size - 1) // region_batch_size
    batch_results: dict[int, list[pd.DataFrame] | None] = {}
    pbar = tqdm(
        total=n_batches,
        desc=f"OpenMeteo {start_str}→{end_str}",
        unit="batch",
        dynamic_ncols=True,
    )

    for batch_idx in range(n_batches):
        batch_slice = slice(
            batch_idx * region_batch_size, (batch_idx + 1) * region_batch_size
        )
        batch_lats = lats[batch_slice]
        batch_lons = lons[batch_slice]
        try:
            dfs = _fetch_multi(batch_lats, batch_lons, start_str, end_str)
            batch_results[batch_idx] = dfs
            pbar.set_postfix(api_calls=_total_api_calls, status="done")
        except Exception:
            log.warning(
                "OpenMeteo batch: fetch failed for %s→%s (batch %d/%d, regions %d-%d) — will retry on next run",
                start.date(),
                end.date(),
                batch_idx + 1,
                n_batches,
                batch_slice.start,
                min(batch_slice.stop, len(region_gdfs)) - 1,
            )
            batch_results[batch_idx] = None
            pbar.set_postfix(api_calls=_total_api_calls, status="FAILED")
        pbar.update(1)

    pbar.close()

    failed_batches = sum(1 for v in batch_results.values() if v is None)
    all_region_dfs: list[pd.DataFrame] = []
    for batch_idx in range(n_batches):
        dfs = batch_results.get(batch_idx)
        if dfs is None:
            continue
        batch_slice = slice(
            batch_idx * region_batch_size, (batch_idx + 1) * region_batch_size
        )
        batch_gdfs = region_gdfs[batch_slice]
        for gdf, df in zip(batch_gdfs, dfs):
            row = gdf.iloc[0]
            if convert_units:
                df = _apply_unit_conversion(df)
            df["region_id"] = row.get("region_id", "")
            df["name"] = row.get("name", "")
            df["parent"] = row.get("parent", "")
            df["parent_name"] = row.get("parent_name", "")
            all_region_dfs.append(df)

    log.info(
        "OpenMeteo batch: fetched %d/%d regions (%d batches failed)",
        len(all_region_dfs),
        len(region_gdfs),
        failed_batches,
    )

    if not all_region_dfs:
        return []

    combined = pd.concat(all_region_dfs, ignore_index=True)
    combined["time"] = pd.to_datetime(combined["time"])

    # Only write CSVs when ALL batches succeeded — partial data would cause months
    # to appear complete on the next run and never be re-downloaded.
    # If any batch failed, skip writing so get_missing_months retries the whole chunk.
    if failed_batches > 0:
        log.warning(
            "OpenMeteo batch: %d/%d batches failed for %s→%s — skipping CSV write "
            "so next run retries the full chunk.",
            failed_batches,
            n_batches,
            start_str,
            end_str,
        )
        return []

    written: list[Path] = []
    for (year, month), group in combined.groupby(
        [combined["time"].dt.year, combined["time"].dt.month]
    ):
        dest_dir = output_path / str(int(year))
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{int(year)}_{int(month):02d}.csv"
        group.to_csv(dest, index=False)
        written.append(dest)
        log.info(
            "OpenMeteo batch: wrote %s (%d rows, %d regions)",
            dest.name,
            len(group),
            group["region_id"].nunique(),
        )

    return written


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def get_missing_months(
    start: pd.Timestamp,
    end: pd.Timestamp,
    output_path: Path,
) -> list[tuple[int, int]]:
    """Return (year, month) tuples that are missing or empty in output_path."""
    missing = []
    current = start.replace(day=1)
    while current <= end:
        y, m = current.year, current.month
        dest = output_path / str(y) / f"{y}_{m:02d}.csv"
        if not dest.exists() or sum(1 for _ in dest.open()) <= 1:
            missing.append((y, m))
        current += pd.DateOffset(months=1)
    return missing


def get_missing_chunks(
    start: pd.Timestamp,
    end: pd.Timestamp,
    output_path: Path,
    chunk_months: int = 12,
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Group missing months into date-range chunks of at most chunk_months each.

    Returns list of (chunk_start, chunk_end) Timestamps ready to pass to
    fetch_region_range / download_range.
    """
    missing = get_missing_months(start, end, output_path)
    if not missing:
        return []

    chunks: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for i in range(0, len(missing), chunk_months):
        group = missing[i : i + chunk_months]
        first_y, first_m = group[0]
        last_y, last_m = group[-1]
        _, last_day = monthrange(last_y, last_m)
        chunk_end = min(
            pd.Timestamp(f"{last_y}-{last_m:02d}-{last_day:02d}"),
            end,  # never exceed the requested end (respects ERA5 lag cap)
        )
        chunks.append(
            (
                pd.Timestamp(f"{first_y}-{first_m:02d}-01"),
                chunk_end,
            )
        )
    return chunks
