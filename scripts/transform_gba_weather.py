"""One-time transform: convert the hourly GBA weather cache from the upstream
pipeline into the daily AP-style monthly CSV layout under gba_datasets/weather/.

Source layout (hourly, region IDs in legacy naming):
    <SRC>/<region_type>/<year>/<year>_<month>.csv
    columns: time, t2m, d2m, tp, region_id, name, parent, parent_name

Output layout (daily, dashboard region IDs):
    gba_datasets/weather/<region_type>/<year>/<year>_<month>.csv
    columns: date, region_id, t2m, d2m, tp, name, parent, parent_name

Aggregation: per (region_id, day) — t2m mean, d2m mean, tp sum (m/hour → m/day).
Region IDs are remapped from the legacy `_GBA_<dir>_<global_z>` naming to the
dashboard's `gba-<dir-letter>-<local_n>` naming, via centroid overlap of the
two geojson layers (computed once at startup, not hard-coded — re-runs against
updated geojsons stay correct).
"""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
SRC = (
    REPO
    / "dengue-model-pipeline-automation - GBA/src/dengue-model-pipeline/datasets/parsednetcdf"
)
SRC_GEO = (
    REPO
    / "dengue-model-pipeline-automation - GBA/src/dengue-model-pipeline/geojsons/geojsons_GBA"
)
DST = REPO / "gba_datasets/weather"
DST_GEO = REPO / "gba_datasets/geojsons/geojsons_GBA"

LEVELS = [("corp", "corps"), ("zone", "zones")]


def _load_layer(folder: Path) -> gpd.GeoDataFrame:
    frames = [gpd.read_file(p) for p in folder.iterdir() if p.suffix == ".geojson"]
    if not frames:
        raise FileNotFoundError(f"no geojsons in {folder}")
    return gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=frames[0].crs)


def build_region_mapping(level_folder: str) -> dict[str, dict]:
    """Return {src_region_id: {'region_id': dst_id, 'name': ..., 'parent': ..., 'parent_name': ...}}."""
    src = _load_layer(SRC_GEO / level_folder)
    dst = _load_layer(DST_GEO / level_folder)
    src["pt"] = src.geometry.representative_point()
    out: dict[str, dict] = {}
    for _, row in src.iterrows():
        pt = row["pt"]
        hit = dst[dst.geometry.contains(pt)]
        if not len(hit):
            hit = dst.iloc[[dst.geometry.distance(pt).idxmin()]]
        target = hit.iloc[0]
        out[row["region_id"]] = {
            "region_id": target["region_id"],
            "name": target["name"],
            "parent": target.get("parent", ""),
            "parent_name": target.get("parent_name", ""),
        }
    return out


def transform_file(
    path: Path, region_type: str, mapping: dict[str, dict]
) -> pd.DataFrame:
    df = pd.read_csv(path)
    # Drop the unnamed first column if present.
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]
    df["date"] = pd.to_datetime(df["time"]).dt.normalize()
    daily = df.groupby(["region_id", "date"], as_index=False).agg(
        t2m=("t2m", "mean"), d2m=("d2m", "mean"), tp=("tp", "sum")
    )
    # Remap region_id and recompute name/parent/parent_name from dashboard geojsons.
    unknown = daily[~daily["region_id"].isin(mapping)]
    if len(unknown):
        print(
            f"  WARN: {len(unknown)} row(s) in {path.name} have unmapped region_ids "
            f"(sample: {unknown['region_id'].unique()[:5].tolist()}); dropping.",
            file=sys.stderr,
        )
        daily = daily[daily["region_id"].isin(mapping)]
    daily["name"] = daily["region_id"].map(lambda r: mapping[r]["name"])
    daily["parent"] = daily["region_id"].map(lambda r: mapping[r]["parent"])
    daily["parent_name"] = daily["region_id"].map(lambda r: mapping[r]["parent_name"])
    daily["region_id"] = daily["region_id"].map(lambda r: mapping[r]["region_id"])
    daily["date"] = daily["date"].dt.strftime("%Y-%m-%d")
    return daily[
        ["date", "region_id", "t2m", "d2m", "tp", "name", "parent", "parent_name"]
    ]


def main() -> None:
    for region_type, geo_folder in LEVELS:
        print(f"=== {region_type} ===")
        mapping = build_region_mapping(geo_folder)
        print(f"  mapping size: {len(mapping)}")
        src_root = SRC / region_type
        if not src_root.is_dir():
            print(f"  no source for {region_type} at {src_root}; skipping.")
            continue
        for year_dir in sorted(src_root.iterdir()):
            if not year_dir.is_dir():
                continue
            for csv in sorted(year_dir.iterdir()):
                if csv.suffix != ".csv":
                    continue
                out_dir = DST / region_type / year_dir.name
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / csv.name
                daily = transform_file(csv, region_type, mapping)
                daily.to_csv(out_path, index=False)
                print(
                    f"  {csv.relative_to(SRC)} → {out_path.relative_to(REPO)}: "
                    f"{len(daily)} daily rows"
                )


if __name__ == "__main__":
    main()
