"""Enrich a line list CSV with region codes from geocoded addresses.

Geocodes each row's address (with persistent disk cache), validates the result
against tokens drawn from the same columns used to compose the address, then
PIPs the coords against one or more region geojson layers. Multi-layer matches
are checked for parent-chain consistency; mismatches are flagged in an audit
CSV.

Layer-agnostic by design: pass any combination of geojson layers (corp /
zone / ward / mandal / block / district / ulb_ward / ...). Each layer's
output columns are auto-named from the geojson filename stem.

Usage example:
  uv run --active python scripts/attach_region_codes.py \\
    --input-file /tmp/Lform_raw.csv \\
    --output-file /tmp/Lform_enriched.csv \\
    --address-column "Patient Address" \\
    --geojson-file /tmp/gba_corps.geojson \\
    --geojson-file /tmp/gba_zones.geojson \\
    --geojson-file /tmp/gba_wards.geojson
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

# All geocoding primitives — cache, address composition, validation, fuzzy
# token match, Google geolocator factory — live in the prep library so the
# pipeline resolver and this CLI stay in lockstep. Don't duplicate them here.
from pipelines.dengue_prep.lib.geocoding import (
    GeocodeCache,
    geocode_row_with_validation,
    make_geolocator,
)

# Sibling-script helpers that are CLI-only (layer naming, env loading,
# point-in-polygon multi-layer mapping). Not in the lib because the lib only
# does single-layer PIP under one geojson_dir.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from geocode_spatial_agg import (  # noqa: E402
    load_dotenv_fallback,
    map_to_region,
    slugify,
)


def cached_geocode(
    df: pd.DataFrame,
    address_col: str,
    cache: GeocodeCache,
    context_cols: list[str] | None = None,
) -> pd.DataFrame:
    """Geocode rows with two-attempt validation and write geocode_* columns.

    Attempt 1: ``<address>, <context1>, <context2>, ...``
    Attempt 2 (if first fails validation): ``<context1>, <context2>, ...``

    With ``context_cols=None`` or ``[]``, falls back to a single unvalidated
    geocode call per row (legacy behaviour).
    """
    if address_col not in df.columns:
        raise ValueError(f"address column {address_col!r} not in CSV")
    context_cols = context_cols or []

    load_dotenv_fallback()
    geolocator = make_geolocator()
    df = df.copy()

    out_cols = {
        "formatted_address": "geocode_formatted_address",
        "lat": "geocode_lat",
        "long": "geocode_long",
        "location_type": "geocode_location_type",
    }
    for c in out_cols.values():
        if c not in df.columns:
            df[c] = pd.NA
    if context_cols:
        for extra in ("geocode_validated", "geocode_attempts"):
            if extra not in df.columns:
                df[extra] = pd.NA

    api_calls = 0
    n_validated = 0
    n_failed = 0
    for idx, row in df.iterrows():
        if not context_cols:
            # Legacy unvalidated path: single attempt on the primary column.
            result, calls = geocode_row_with_validation(
                row, address_col, [], cache, geolocator
            )
            api_calls += calls
            df.at[idx, out_cols["formatted_address"]] = result.formatted_address
            df.at[idx, out_cols["lat"]] = result.lat
            df.at[idx, out_cols["long"]] = result.long
            df.at[idx, out_cols["location_type"]] = result.location_type
            continue

        result, calls = geocode_row_with_validation(
            row, address_col, context_cols, cache, geolocator
        )
        api_calls += calls
        df.at[idx, out_cols["formatted_address"]] = result.formatted_address
        df.at[idx, out_cols["location_type"]] = result.location_type
        if result.validated:
            df.at[idx, out_cols["lat"]] = result.lat
            df.at[idx, out_cols["long"]] = result.long
            df.at[idx, "geocode_validated"] = True
            n_validated += 1
        elif result.attempted:
            df.at[idx, "geocode_validated"] = False
            n_failed += 1
        df.at[idx, "geocode_attempts"] = (
            1 if not result.attempted else (1 if result.validated else 2)
        )

    if context_cols:
        print(
            f"  geocoded {len(df)} rows; "
            f"{n_validated} validated, {n_failed} failed validation; "
            f"{api_calls} new Google call(s); {cache.summary()}"
        )
    else:
        print(
            f"  geocoded {len(df)} rows; {api_calls} new Google call(s); {cache.summary()}"
        )
    return df


def build_parent_map(
    geojson_paths: list[Path],
    id_col: str = "region_id",
) -> dict[str, str]:
    """Build a unified {region_id: parent_id} map across all supplied geojsons.

    Used by the consistency check to walk parent chains. Regions with no
    ``parent`` property are simply omitted from the map (they're treated
    as roots).
    """
    parents: dict[str, str] = {}
    for p in geojson_paths:
        try:
            data = json.loads(Path(p).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"WARN: could not read {p}: {exc}", file=sys.stderr)
            continue
        if isinstance(data, dict) and data.get("type") == "FeatureCollection":
            features = data.get("features", [])
        elif isinstance(data, dict) and data.get("type") == "Feature":
            features = [data]
        else:
            features = []
        for f in features:
            props = f.get("properties") or {}
            rid = props.get(id_col)
            pid = props.get("parent")
            if rid and pid:
                parents[rid] = pid
    return parents


def _ancestors(rid: str, parent_map: dict[str, str]) -> set[str]:
    """Return the set of {rid, parent(rid), grandparent(rid), ...}."""
    seen = {rid}
    cur = rid
    while cur in parent_map:
        cur = parent_map[cur]
        if cur in seen:  # cycle guard
            break
        seen.add(cur)
    return seen


def consistency_check(
    df: pd.DataFrame,
    layer_id_cols: list[str],
    parent_map: dict[str, str],
) -> pd.Series:
    """Return a boolean Series: True where the row's layer assignments share a chain.

    For any pair of assigned region_ids in a row, one must be in the other's
    ancestor chain — otherwise the row's PIP results disagree (e.g. point
    fell in a ward whose parent ULB doesn't match the corp it also matched).
    Rows with <2 assigned layers are trivially consistent.
    """
    consistent = pd.Series(True, index=df.index)
    for idx, row in df.iterrows():
        ids = [row[c] for c in layer_id_cols if pd.notna(row[c])]
        if len(ids) < 2:
            continue
        chains = [_ancestors(rid, parent_map) for rid in ids]
        for i, rid_i in enumerate(ids):
            for j, rid_j in enumerate(ids):
                if i == j:
                    continue
                if rid_i not in chains[j] and rid_j not in chains[i]:
                    consistent.at[idx] = False
                    break
            if not consistent.at[idx]:
                break
    return consistent


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--input-file", type=Path, required=True)
    p.add_argument("--output-file", type=Path, required=True)
    p.add_argument(
        "--address-column",
        required=True,
        help="The CSV column containing free-text address to geocode.",
    )
    p.add_argument(
        "--context-column",
        action="append",
        default=[],
        help=(
            "A source column whose value provides location context "
            "(e.g., Village Or Ward, Sub District, Ulb). Repeat once per column. "
            "When supplied: composed addresses are sent to Google AND the result "
            "is validated against tokens from these columns. Failed validation "
            "triggers one retry with the area-only composition (no primary address). "
            "Omit entirely to keep legacy single-call behaviour."
        ),
    )
    p.add_argument(
        "--geojson-file",
        type=Path,
        action="append",
        required=True,
        help="A region geojson layer (repeat per layer; order does not matter).",
    )
    p.add_argument(
        "--cache-file",
        type=Path,
        default=Path("./cache/geocode_cache.json"),
        help="Persistent geocode cache. Default: ./cache/geocode_cache.json",
    )
    p.add_argument(
        "--audit-file",
        type=Path,
        help=(
            "Write rows with any unmatched layer or layer inconsistency to "
            "this CSV. Default: <output-file>.audit.csv"
        ),
    )
    p.add_argument("--region-id-col", default="region_id")
    p.add_argument("--region-name-col", default="name")
    p.add_argument(
        "--skip-cross-validation",
        action="store_true",
        help="Don't verify that layer assignments share a parent chain.",
    )
    p.add_argument(
        "--require-address",
        action="store_true",
        help=(
            "Drop rows where --address-column is empty/null instead of carrying "
            "them through with empty geocode/region fields."
        ),
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.audit_file is None:
        args.audit_file = args.output_file.with_suffix(".audit.csv")
    args.output_file.parent.mkdir(parents=True, exist_ok=True)
    args.audit_file.parent.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(args.input_file)
    print(f"loaded {len(df):,} rows from {args.input_file}")

    if args.require_address:
        before = len(df)
        df = df[
            df[args.address_column].notna()
            & (df[args.address_column].astype(str).str.strip() != "")
        ].reset_index(drop=True)
        print(
            f"  --require-address: dropped {before - len(df):,} rows with missing {args.address_column!r}; {len(df):,} remain"
        )

    cache = GeocodeCache(args.cache_file)
    df = cached_geocode(df, args.address_column, cache, args.context_column)
    cache.flush()
    print(f"  cache flushed to {args.cache_file}")

    layer_id_cols: list[str] = []
    for gj in args.geojson_file:
        prefix = slugify(gj.stem)
        out_id = f"{prefix}_id"
        out_name = f"{prefix}_name"
        layer_id_cols.append(out_id)
        df = map_to_region(
            df,
            gj,
            "geocode_lat",
            "geocode_long",
            args.region_id_col,
            args.region_name_col,
            out_id,
            out_name,
        )

    if not args.skip_cross_validation:
        parent_map = build_parent_map(args.geojson_file, args.region_id_col)
        consistent = consistency_check(df, layer_id_cols, parent_map)
        df["region_consistent"] = consistent
        n_bad = int((~consistent).sum())
        print(f"\ncross-layer consistency: {n_bad}/{len(df)} row(s) failed")
    else:
        df["region_consistent"] = True

    unmatched_any = df[layer_id_cols].isna().any(axis=1)
    audit_mask = unmatched_any | ~df["region_consistent"]
    audit = df.loc[audit_mask].copy()

    df.to_csv(args.output_file, index=False)
    audit.to_csv(args.audit_file, index=False)

    print(f"\nwrote {len(df):,} rows → {args.output_file}")
    print(f"      {len(audit):,} rows to audit → {args.audit_file}")
    for col in layer_id_cols:
        matched = int(df[col].notna().sum())
        print(f"      {col}: {matched}/{len(df)} matched")


if __name__ == "__main__":
    main()
