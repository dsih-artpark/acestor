"""Geocode-then-spatial-join resolver for line-list case data.

Used by ``dengue_prep`` to attach a ``region_id`` to each row when the source
file has neither an LGD code column nor reliable lat/lon — only free-text
address fields.

Strategy
--------
Each row gets up to **two composition attempts**, each a ``", "``-joined
concatenation of the column values listed in the config:

  attempt 1 = compose(address_fields)
  attempt 2 = compose(fallback_address_fields)     [optional]

For each attempt, the script:
  1. Sends the composed string to Google's Geocoding API (with persistent
     JSON cache, so repeated weekly runs are zero-cost for known addresses).
  2. Extracts validation tokens from the same fields used to compose — words
     >= 4 chars, with a stopword filter removing generic Bengaluru/Karnataka/
     "Corporation"/etc. terms that would otherwise carry through a wrong-area
     guess (the Murugeshpalya bug).
  3. Validates: Google's ``formatted_address`` must contain at least one
     token. Token-vs-word match is **fuzzy** — Levenshtein distance ≤ 1 —
     so "hebbala" validates against "Hebbal", "vimanpura" against "Vimanapura",
     etc., recovering rows that were rejected by pure substring match.
  4. PIPs the returned coords against the geojson region layer. Only rows
     that PIP into a ward count as "usable".

If attempt 1 didn't produce a usable region_id (geocode failure, validation
failure, or PIP'd outside the layer), **and** ``fallback_address_fields`` is
non-empty, attempt 2 runs the same pipeline using the fallback columns.

The high-level entry point :func:`resolve_via_geocode` returns a Series of
``region_id`` values aligned to the input DataFrame's index. Rows that
exhausted both attempts get ``NaN`` — caller decides whether to drop or
keep (per ``require_validation`` in the config).
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

# Tokens too generic to be meaningful validation signal in the Bengaluru region.
# Anything here gets stripped from the validation set so it can't carry a wrong-
# area Google result through validation. Callers can extend via the config's
# ``extra_stopwords`` list (e.g. add ``bommenahalli`` to suppress the Google
# directional-centroid fallback).
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
    fields: list[str] | tuple[str, ...],
    stopwords: frozenset[str] | set[str] = DEFAULT_STOPWORDS,
    min_len: int = 4,
) -> set[str]:
    """Tokens we expect to see in Google's ``formatted_address`` to trust it.

    Pulls words >= ``min_len`` chars from each listed field, lowercases,
    drops the ``Ward No.NN (..)`` prefix so the area inside the parens
    becomes the token, and filters out generic stopwords.
    """
    tokens: set[str] = set()
    for col in fields:
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


def compose_fields(
    row: pd.Series,
    fields: list[str] | tuple[str, ...],
) -> str:
    """``", "`` join the row's values across the given fields (skip empties)."""
    parts: list[str] = []
    for col in fields:
        if col not in row.index:
            continue
        val = row.get(col)
        if val is None or pd.isna(val):
            continue
        v = str(val).strip()
        if v:
            parts.append(v)
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Fuzzy substring match — Levenshtein distance ≤ 1
# ---------------------------------------------------------------------------


def _lev_le1(a: str, b: str) -> bool:
    """True if Levenshtein distance between ``a`` and ``b`` is 0 or 1.

    Catches the common Bengaluru-area spelling pairs: hebbala↔Hebbal,
    vimanpura↔Vimanapura, bellanduru↔Bellandur, chickpete↔Chickpet, etc.
    """
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(c1 != c2 for c1, c2 in zip(a, b)) <= 1
    # one insertion/deletion: ensure shorter is a subsequence of longer with
    # at most one skipped char in longer
    if la > lb:
        a, b = b, a
        la, lb = lb, la
    i = j = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
        else:
            j += 1
            if j - i > 1:
                return False
    return True


def formatted_address_contains_any(
    formatted: object,
    tokens: set[str],
) -> bool:
    """True iff ``formatted`` contains any validation token (exact OR Lev≤1).

    Pure substring fast path first; falls back to per-word fuzzy match if no
    exact hit. Returns ``False`` for empty/null formatted or empty token set
    (no signal → can't trust).
    """
    if formatted is None or pd.isna(formatted) or not tokens:
        return False
    fmt = str(formatted).lower()
    # Exact substring — fast and catches "domlur" ⊂ "Domlur Layout".
    if any(tok in fmt for tok in tokens):
        return True
    # Fuzzy per-word match for spelling variants.
    words = re.findall(r"[a-z]+", fmt)
    for tok in tokens:
        for word in words:
            if abs(len(tok) - len(word)) <= 1 and _lev_le1(tok, word):
                return True
    return False


# ---------------------------------------------------------------------------
# Persistent cache
# ---------------------------------------------------------------------------


class GeocodeCache:
    """JSON-backed cache for geocode results, keyed by normalised address.

    Persists across runs so weekly upload pipelines only pay Google for
    addresses they haven't seen before.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except json.JSONDecodeError as exc:
                log.warning(
                    "geocode cache %s is malformed (%s); starting fresh",
                    self.path,
                    exc,
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
# Single-row geocoder (one attempt)
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
    """Outcome of a single composition attempt."""

    lat: float | None = None
    long: float | None = None
    formatted_address: str | None = None
    location_type: str | None = None
    validated: bool = False
    attempted: bool = False  # False when the composed string was empty


def geocode_row_for_fields(
    row: pd.Series,
    fields: list[str] | tuple[str, ...],
    cache: GeocodeCache,
    geolocator: Any,
    stopwords: frozenset[str] | set[str] = DEFAULT_STOPWORDS,
) -> tuple[GeocodeResult, int]:
    """One geocode + validate attempt over the given fields.

    Composes the address from ``fields``, sends to Google (cached), and
    validates that the response contains at least one specific token from
    the same fields. Returns ``(GeocodeResult, n_api_calls_made)``.
    """
    composed = compose_fields(row, fields)
    if not composed:
        return (GeocodeResult(attempted=False), 0)
    tokens = extract_validation_tokens(row, fields, stopwords=stopwords)
    hit, made_call = _geocode_with_cache(composed, cache, geolocator)
    api_calls = int(made_call)
    validated = formatted_address_contains_any(hit.get("formatted_address"), tokens)
    return (
        GeocodeResult(
            lat=hit.get("lat") if validated else None,
            long=hit.get("long") if validated else None,
            formatted_address=hit.get("formatted_address"),
            location_type=hit.get("location_type"),
            validated=validated,
            attempted=True,
        ),
        api_calls,
    )


# ---------------------------------------------------------------------------
# Geolocator factory (lazy)
# ---------------------------------------------------------------------------


def make_geolocator() -> Any:
    """Construct a GoogleV3 geolocator. Reads ``GOOGLE_API_KEY`` from env."""
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY is not set. Put it in .env or export it in the environment."
        )
    try:
        from geopy.geocoders import GoogleV3
    except ModuleNotFoundError as exc:  # pragma: no cover
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
    """Read every ``*.geojson`` under ``geojson_dir`` into one GeoDataFrame."""
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
    return gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True),
        geometry="geometry",
        crs=frames[0].crs or "EPSG:4326",
    )


def _pip_assign(
    lat: pd.Series,
    long: pd.Series,
    layer: gpd.GeoDataFrame,
    region_id_col: str = "region_id",
) -> pd.Series:
    """Point-in-polygon. Returns a Series of region_ids aligned to ``lat.index``."""
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


def _geocode_pass(
    df: pd.DataFrame,
    fields: list[str],
    layer: gpd.GeoDataFrame,
    cache: GeocodeCache,
    geolocator: Any,
    stopwords: frozenset[str] | set[str],
    region_id_col: str,
    label: str,
) -> pd.Series:
    """Geocode all rows of ``df`` with ``fields``, PIP, return region_id Series."""
    lats: list[float | None] = [None] * len(df)
    longs: list[float | None] = [None] * len(df)
    n_validated = 0
    n_failed = 0
    api_calls = 0
    for i, (_, row) in enumerate(df.iterrows()):
        result, calls = geocode_row_for_fields(
            row, fields, cache, geolocator, stopwords
        )
        api_calls += calls
        if result.validated:
            lats[i] = result.lat
            longs[i] = result.long
            n_validated += 1
        elif result.attempted:
            n_failed += 1
    lat_s = pd.Series(lats, index=df.index, dtype="float64")
    long_s = pd.Series(longs, index=df.index, dtype="float64")
    region_ids = _pip_assign(lat_s, long_s, layer, region_id_col=region_id_col)
    pipped = int(region_ids.notna().sum())
    log.info(
        "geocode [%s]: %d/%d validated → %d PIP'd; %d failed validation; %d new Google call(s); %s",
        label,
        n_validated,
        len(df),
        pipped,
        n_failed,
        api_calls,
        cache.summary(),
    )
    return region_ids


def resolve_via_geocode(
    df: pd.DataFrame,
    *,
    address_fields: list[str] | tuple[str, ...],
    fallback_address_fields: list[str] | tuple[str, ...] = (),
    cache_file: Path | str,
    geojson_dir: Path | str,
    stopwords: frozenset[str] | set[str] | None = None,
    region_id_col: str = "region_id",
    region_name_col: str = "name",
) -> pd.Series:
    """Two-pass geocode → spatial-join. Returns Series of region_ids per row.

    Pass 1 composes the address from ``address_fields`` and goes through the
    full geocode + validate + PIP flow. Rows that produce a region_id are
    locked in.

    Pass 2 (only if ``fallback_address_fields`` is non-empty) takes the rows
    that didn't get a region_id in pass 1 and re-runs the same flow with the
    fallback composition. Cross-state Patient Addresses, missing-address rows
    where a Facility column is present, etc. are recovered here.

    Rows that exhaust both passes contain ``NaN`` in the returned Series; the
    caller decides whether to drop or keep them.
    """
    if not address_fields:
        raise ValueError("address_fields must contain at least one column name")
    missing = [c for c in address_fields if c not in df.columns]
    if missing:
        raise ValueError(
            f"address_fields not found in case data: {missing}. "
            f"Available columns: {list(df.columns)}"
        )
    if fallback_address_fields:
        missing_fb = [c for c in fallback_address_fields if c not in df.columns]
        if missing_fb:
            log.warning(
                "fallback_address_fields contains columns not in case data: %s "
                "(those will be skipped during compose)",
                missing_fb,
            )

    geojson_dir = Path(geojson_dir)
    cache = GeocodeCache(Path(cache_file))
    geolocator = make_geolocator()
    stopwords = stopwords if stopwords is not None else DEFAULT_STOPWORDS

    layer = _load_region_layer(
        geojson_dir, region_id_col=region_id_col, region_name_col=region_name_col
    )

    region_ids = _geocode_pass(
        df,
        list(address_fields),
        layer,
        cache,
        geolocator,
        stopwords,
        region_id_col,
        label="primary",
    )

    if fallback_address_fields:
        unresolved = df.index[region_ids.isna()]
        if len(unresolved):
            log.info(
                "geocode: %d row(s) didn't resolve on primary fields; "
                "trying fallback (%s)",
                len(unresolved),
                list(fallback_address_fields),
            )
            sub_df = df.loc[unresolved]
            fb_ids = _geocode_pass(
                sub_df,
                list(fallback_address_fields),
                layer,
                cache,
                geolocator,
                stopwords,
                region_id_col,
                label="fallback",
            )
            region_ids.loc[fb_ids.index] = fb_ids.values

    cache.flush()

    n_resolved = int(region_ids.notna().sum())
    log.info(
        "geocode (overall): %d/%d rows resolved to a region_id in %s",
        n_resolved,
        len(df),
        geojson_dir,
    )
    return region_ids


# ---------------------------------------------------------------------------
# Backwards-compat shim for the /tmp diagnostic scripts that already exist
# (compose_address, geocode_row_with_validation). Delegates to the new API.
# ---------------------------------------------------------------------------


def compose_address(
    row: pd.Series,
    primary_col: str | None,
    context_cols: list[str] | tuple[str, ...],
) -> str:
    """Legacy alias for ``compose_fields`` — combines primary + context into one list."""
    fields = ([primary_col] if primary_col else []) + list(context_cols)
    return compose_fields(row, fields)


def geocode_row_with_validation(
    row: pd.Series,
    address_col: str,
    context_cols: list[str] | tuple[str, ...],
    cache: GeocodeCache,
    geolocator: Any,
    stopwords: frozenset[str] | set[str] = DEFAULT_STOPWORDS,
) -> tuple[GeocodeResult, int]:
    """Legacy two-attempt path: ``[address_col, *context]`` then ``context`` only.

    Kept so the diagnostic scripts in ``/tmp/`` still work. New callers should
    use :func:`geocode_row_for_fields` directly with their own composition list.
    """
    fields_primary = [address_col, *context_cols]
    res1, calls1 = geocode_row_for_fields(
        row, fields_primary, cache, geolocator, stopwords
    )
    if res1.validated or not context_cols:
        return res1, calls1
    res2, calls2 = geocode_row_for_fields(
        row, list(context_cols), cache, geolocator, stopwords
    )
    return res2, calls1 + calls2
