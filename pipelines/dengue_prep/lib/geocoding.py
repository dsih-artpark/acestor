"""Geocode-then-spatial-join resolver for line-list case data.

Used by ``dengue_prep`` to attach a ``region_id`` to each row when the source
file has neither an LGD code column nor reliable lat/lon — only a free-text
address column. The strategy mirrors what ``scripts/attach_region_codes.py``
arrived at through iteration:

  1. Persistent JSON cache for geocode results (so repeated weekly runs only
     pay Google for new addresses).
  2. **Validated** geocoding: the composed address is sent to Google, but the
     returned ``formatted_address`` must contain at least one specific token
     from the source row's context columns. Generic tokens (city / state /
     "Corporation" / "Ward" / "Authority" / ...) are stripped from the
     validation set so they can't carry through a wrong-but-confident guess
     (the Murugeshpalya bug).
  3. **Retry** on validation failure with an area-only composition (no primary
     address, just the context columns).
  4. **Spatial join** the validated coords against the ``{region_type}s``
     geojson layer to produce ``region_id``.

Configuration lives in :class:`GeocodingConfig` (``dengue_prep/configs.py``).
The high-level entry point is :func:`resolve_via_geocode` which takes a
DataFrame plus the config and returns a Series of ``region_id`` values
aligned to the input index. Rows that fail geocoding, validation, or the
spatial join get NaN — the caller decides whether to drop or keep them.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Address normalisation + token extraction
# ---------------------------------------------------------------------------

_WARD_NO_PREFIX = re.compile(r"^ward\s*no\.?\s*\d+\s*\(?", re.IGNORECASE)

# Default stopwords for the Bengaluru region. Anything in this set is stripped
# from the validation token set so it can't carry a wrong-area Google result
# through validation. Callers can extend via ``GeocodingConfig.stopwords``.
DEFAULT_STOPWORDS: frozenset[str] = frozenset(
    {
        "bengaluru",
        "bangalore",
        "karnataka",
        "india",
        "urban",
        "rural",
        "district",
        "sub",
        "greater",
        "authority",
        "state",
        "city",
        "corporation",
        "ward",
        "zone",
    }
)


def normalize_address(addr: object) -> str:
    """Lowercase, strip leading ``#`` prefix, collapse whitespace.

    Returns ``""`` for nulls / empty so callers can short-circuit cache lookups.
    """
    if addr is None or pd.isna(addr):
        return ""
    s = str(addr).strip().lower()
    s = re.sub(r"^#\s*", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def extract_validation_tokens(
    row: pd.Series,
    context_cols: list[str] | tuple[str, ...],
    stopwords: frozenset[str] | set[str] = DEFAULT_STOPWORDS,
    min_len: int = 4,
) -> set[str]:
    """Tokens that must appear in Google's ``formatted_address`` to pass validation.

    Pulls words >= ``min_len`` chars from each context column, lowercases,
    drops the ``Ward No.NN (..)`` prefix so the area inside the parens becomes
    the token, and filters out stopwords. The remaining tokens are
    area-specific names (e.g. ``domlur``, ``hebbala``, ``south``).
    """
    tokens: set[str] = set()
    for col in context_cols:
        if col not in row.index:
            continue
        val = row.get(col)
        if val is None or pd.isna(val):
            continue
        s = _WARD_NO_PREFIX.sub("", str(val)).rstrip(")")
        s = re.sub(r"[^\w\s]", " ", s.lower())
        for word in s.split():
            if len(word) >= min_len and word not in stopwords:
                tokens.add(word)
    return tokens


def compose_address(
    row: pd.Series,
    primary_col: str | None,
    context_cols: list[str] | tuple[str, ...],
) -> str:
    """Join the primary column + context columns into a single address string.

    Pass ``primary_col=None`` for the area-only retry composition.
    """
    parts: list[str] = []
    if primary_col is not None and primary_col in row.index:
        val = row.get(primary_col)
        if val is not None and not pd.isna(val):
            parts.append(str(val).strip())
    for col in context_cols:
        if col not in row.index:
            continue
        val = row.get(col)
        if val is not None and not pd.isna(val):
            parts.append(str(val).strip())
    return ", ".join(p for p in parts if p)


def formatted_address_contains_any(formatted: object, tokens: set[str]) -> bool:
    """True iff Google's ``formatted_address`` contains any of the validation tokens.

    Substring match, case-insensitive. ``False`` for empty/null formatted or
    empty token set (no validation possible → can't trust).
    """
    if formatted is None or pd.isna(formatted) or not tokens:
        return False
    fmt = str(formatted).lower()
    return any(tok in fmt for tok in tokens)


# ---------------------------------------------------------------------------
# Persistent cache
# ---------------------------------------------------------------------------


class GeocodeCache:
    """JSON-backed cache for geocode results, keyed by normalised address.

    Designed to persist across runs — cross-week weekly uploads reuse cached
    results for repeat addresses, so the Google API bill stays roughly
    proportional to *new* addresses only.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except json.JSONDecodeError as exc:
                log.warning(
                    "geocode cache %s is malformed (%s); starting fresh", self.path, exc
                )
                self._data = {}
        self._stats = {"hits": 0, "misses": 0}

    def get(self, key: str) -> dict[str, Any] | None:
        if key in self._data:
            self._stats["hits"] += 1
            return self._data[key]
        self._stats["misses"] += 1
        return None

    def put(self, key: str, value: dict[str, Any]) -> None:
        self._data[key] = value

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))

    def summary(self) -> str:
        total = self._stats["hits"] + self._stats["misses"]
        return (
            f"cache: {self._stats['hits']} hits / {total} lookups, "
            f"{len(self._data)} stored"
        )


# ---------------------------------------------------------------------------
# Single-row geocoder with validation + retry
# ---------------------------------------------------------------------------


_EMPTY_RESULT: dict[str, Any] = {
    "formatted_address": None,
    "lat": None,
    "long": None,
    "location_type": None,
}


def _google_geocode_one(addr: str, geolocator: Any) -> dict[str, Any]:
    """Call Google once. Returns an empty-result dict on null/empty/error."""
    if addr is None or pd.isna(addr):
        return dict(_EMPTY_RESULT)
    text = str(addr).strip()
    if not text:
        return dict(_EMPTY_RESULT)
    try:
        loc = geolocator.geocode(text)
    except Exception as exc:  # noqa: BLE001 — log + continue
        log.warning("geocode failed for %r: %s: %s", text, type(exc).__name__, exc)
        return dict(_EMPTY_RESULT)
    if not loc:
        return dict(_EMPTY_RESULT)
    raw = loc.raw if isinstance(loc.raw, dict) else {}
    geom = raw.get("geometry", {}) if isinstance(raw, dict) else {}
    return {
        "formatted_address": loc.address,
        "lat": loc.latitude,
        "long": loc.longitude,
        "location_type": geom.get("location_type"),
    }


def _geocode_with_cache(
    addr: str, cache: GeocodeCache, geolocator: Any
) -> tuple[dict[str, Any], bool]:
    """Cached geocode. Returns (result, made_api_call)."""
    key = normalize_address(addr)
    if not key:
        return (dict(_EMPTY_RESULT), False)
    hit = cache.get(key)
    if hit is not None:
        return (hit, False)
    result = _google_geocode_one(addr, geolocator)
    cache.put(key, result)
    return (result, True)


@dataclass
class GeocodeResult:
    """Outcome of a single row's geocode attempt(s)."""

    lat: float | None = None
    long: float | None = None
    formatted_address: str | None = None
    location_type: str | None = None
    validated: bool = False
    attempts: int = 0  # 0 = address empty / not attempted; 1 or 2 otherwise


def geocode_row_with_validation(
    row: pd.Series,
    address_col: str,
    context_cols: list[str] | tuple[str, ...],
    cache: GeocodeCache,
    geolocator: Any,
    stopwords: frozenset[str] | set[str] = DEFAULT_STOPWORDS,
) -> tuple[GeocodeResult, int]:
    """Geocode one row with the validate-or-retry strategy.

    Returns ``(GeocodeResult, n_api_calls_made)``. The result's ``validated``
    flag is True iff Google's returned address contained at least one
    specific token from the row's context columns.
    """
    tokens = extract_validation_tokens(row, context_cols, stopwords=stopwords)

    addr1 = compose_address(row, address_col, context_cols)
    if not addr1:
        return (GeocodeResult(attempts=0), 0)

    hit, made_call = _geocode_with_cache(addr1, cache, geolocator)
    api_calls = int(made_call)

    if formatted_address_contains_any(hit.get("formatted_address"), tokens):
        return (
            GeocodeResult(
                lat=hit.get("lat"),
                long=hit.get("long"),
                formatted_address=hit.get("formatted_address"),
                location_type=hit.get("location_type"),
                validated=True,
                attempts=1,
            ),
            api_calls,
        )

    addr2 = compose_address(row, None, context_cols)
    if not addr2 or addr2 == addr1:
        return (GeocodeResult(validated=False, attempts=1), api_calls)

    hit2, made_call2 = _geocode_with_cache(addr2, cache, geolocator)
    api_calls += int(made_call2)

    if formatted_address_contains_any(hit2.get("formatted_address"), tokens):
        return (
            GeocodeResult(
                lat=hit2.get("lat"),
                long=hit2.get("long"),
                formatted_address=hit2.get("formatted_address"),
                location_type=hit2.get("location_type"),
                validated=True,
                attempts=2,
            ),
            api_calls,
        )
    return (GeocodeResult(validated=False, attempts=2), api_calls)


# ---------------------------------------------------------------------------
# Geolocator factory (lazy — only loads geopy + key when geocoding is enabled)
# ---------------------------------------------------------------------------


def make_geolocator() -> Any:
    """Construct a GoogleV3 geolocator. Reads ``GOOGLE_API_KEY`` from env.

    Raises ``RuntimeError`` if the key is missing or geopy is not installed.
    Callers should only invoke this once geocoding has been opted in via
    :class:`GeocodingConfig`.
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY is not set. Put it in .env or export it in the environment."
        )
    try:
        from geopy.geocoders import GoogleV3
    except ModuleNotFoundError as exc:  # pragma: no cover — dependency issue
        raise RuntimeError(
            "geopy is required for the geocoding resolver. Install with: uv pip install geopy"
        ) from exc
    return GoogleV3(api_key=api_key)


# ---------------------------------------------------------------------------
# Geojson layer loader + PIP
# ---------------------------------------------------------------------------


def _load_region_layer(
    geojson_dir: Path,
    region_id_col: str = "region_id",
    region_name_col: str = "name",
) -> gpd.GeoDataFrame:
    """Read every ``*.geojson`` under ``geojson_dir`` into one GeoDataFrame.

    Each file may be either a ``FeatureCollection`` or a single ``Feature``;
    both are flattened to features and concatenated. Features missing
    ``region_id_col`` or geometry are dropped with a warning count.
    """
    paths = sorted(Path(geojson_dir).glob("*.geojson"))
    if not paths:
        raise FileNotFoundError(
            f"no *.geojson files in {geojson_dir}; cannot run the spatial join"
        )

    frames: list[gpd.GeoDataFrame] = []
    for p in paths:
        try:
            gdf = gpd.read_file(p)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read %s: %s", p, exc)
            continue
        if region_id_col not in gdf.columns:
            log.warning("%s missing %r column; skipping", p, region_id_col)
            continue
        keep_cols = [region_id_col]
        if region_name_col in gdf.columns:
            keep_cols.append(region_name_col)
        frames.append(gdf[[*keep_cols, "geometry"]].copy())

    if not frames:
        raise ValueError(
            f"loaded 0 valid region features from {geojson_dir} "
            f"(expected at least one with a {region_id_col!r} property)"
        )

    combined = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True),
        geometry="geometry",
        crs=frames[0].crs or "EPSG:4326",
    )
    return combined


def _pip_assign(
    lat: pd.Series,
    long: pd.Series,
    layer: gpd.GeoDataFrame,
    region_id_col: str = "region_id",
) -> pd.Series:
    """Point-in-polygon. Returns a Series of region_ids aligned to ``lat.index``.

    Rows where either coord is NaN or the point falls outside all polygons
    get ``NaN``. Rows that land in multiple polygons (boundary overlap) are
    treated as ambiguous and also get NaN (with a warning count).
    """
    out = pd.Series(pd.NA, index=lat.index, dtype="object")
    mask = lat.notna() & long.notna()
    if not mask.any():
        return out

    points = gpd.GeoDataFrame(
        {"_idx": lat.index[mask]},
        geometry=gpd.points_from_xy(long[mask], lat[mask]),
        crs="EPSG:4326",
    )
    if layer.crs is not None and str(layer.crs) != "EPSG:4326":
        points = points.to_crs(layer.crs)

    joined = gpd.sjoin(points, layer, how="left", predicate="within")
    counts = joined["_idx"].value_counts()
    clean_idx = counts[counts == 1].index
    clean = joined[joined["_idx"].isin(clean_idx) & joined[region_id_col].notna()]

    out.loc[clean["_idx"].to_list()] = clean[region_id_col].to_list()

    ambiguous = int((counts > 1).sum())
    if ambiguous:
        log.warning(
            "_pip_assign: %d point(s) fell in multiple polygons; treated as ambiguous (NaN)",
            ambiguous,
        )
    return out


# ---------------------------------------------------------------------------
# High-level entry point — called by dengue_prep's ihip resolver
# ---------------------------------------------------------------------------


def resolve_via_geocode(
    df: pd.DataFrame,
    *,
    address_column: str,
    context_columns: list[str] | tuple[str, ...],
    cache_file: Path | str,
    geojson_dir: Path | str,
    stopwords: frozenset[str] | set[str] | None = None,
    region_id_col: str = "region_id",
    region_name_col: str = "name",
) -> pd.Series:
    """Geocode the address column, validate, retry, PIP, return region_ids.

    Returns a Series of region_id values aligned to ``df.index``. Rows that
    failed geocoding, validation, or the spatial join contain ``NaN``; the
    caller decides whether to drop them (per ``require_address`` /
    ``require_validation`` in the config) or keep them.

    Logs an aggregate summary at the end (validated / retried / failed counts,
    cache hit ratio, API call count).
    """
    if address_column not in df.columns:
        raise ValueError(
            f"address_column {address_column!r} not found in case data columns: "
            f"{list(df.columns)}"
        )

    geojson_dir = Path(geojson_dir)
    cache = GeocodeCache(Path(cache_file))
    geolocator = make_geolocator()
    stopwords = stopwords if stopwords is not None else DEFAULT_STOPWORDS

    lats: list[float | None] = [None] * len(df)
    longs: list[float | None] = [None] * len(df)
    api_calls = 0
    n_validated = 0
    n_retried = 0
    n_failed = 0
    for i, (_, row) in enumerate(df.iterrows()):
        result, calls = geocode_row_with_validation(
            row,
            address_column,
            context_columns,
            cache,
            geolocator,
            stopwords=stopwords,
        )
        api_calls += calls
        if result.validated:
            lats[i] = result.lat
            longs[i] = result.long
            n_validated += 1
            if result.attempts == 2:
                n_retried += 1
        elif result.attempts > 0:
            n_failed += 1
    cache.flush()

    log.info(
        "geocode: %d/%d validated (%d retried, %d failed validation); "
        "%d new Google call(s); %s",
        n_validated,
        len(df),
        n_retried,
        n_failed,
        api_calls,
        cache.summary(),
    )

    lat_s = pd.Series(lats, index=df.index, dtype="float64")
    long_s = pd.Series(longs, index=df.index, dtype="float64")

    layer = _load_region_layer(
        geojson_dir, region_id_col=region_id_col, region_name_col=region_name_col
    )
    region_ids = _pip_assign(lat_s, long_s, layer, region_id_col=region_id_col)

    n_pipped = int(region_ids.notna().sum())
    log.info(
        "geocode → PIP: %d/%d rows resolved to a region_id in %s",
        n_pipped,
        len(df),
        geojson_dir,
    )
    return region_ids
