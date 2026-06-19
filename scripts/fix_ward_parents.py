"""Repoint ward geojsons' `parent` from corp → zone via PIP.

Currently ward geojsons in gba_datasets/geojsons/geojsons_GBA/wards/ have
`parent: corp_gba-X` directly, skipping the zone layer. The dashboard's
canonical hierarchy is ward → zone → corp, so the rollup logic in
dengue_prep can't walk ward → zone via the parent chain.

Fix: for each ward, find the zone polygon that contains its centroid and
overwrite `parent`, `parent_name`, and `parentID` to point at that zone.
Zones already correctly point at their parent corp; only wards need updating.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
GEO = REPO / "gba_datasets/geojsons/geojsons_GBA"
WARDS = GEO / "wards"
ZONES = GEO / "zones"


def _load_layer(folder: Path) -> gpd.GeoDataFrame:
    frames = [gpd.read_file(p) for p in folder.iterdir() if p.suffix == ".geojson"]
    return gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=frames[0].crs)


def main() -> int:
    zones = _load_layer(ZONES)[["region_id", "name", "geometry"]].rename(
        columns={"region_id": "zone_id", "name": "zone_name"}
    )

    updated = 0
    no_match = []
    for path in sorted(WARDS.glob("*.geojson")):
        data = json.loads(path.read_text())
        feat = data["features"][0]
        props = feat["properties"]
        ward_id = props["region_id"]

        ward_gdf = gpd.GeoDataFrame.from_features([feat], crs=zones.crs)
        pt = ward_gdf.geometry.iloc[0].representative_point()

        hit = zones[zones.geometry.contains(pt)]
        if hit.empty:
            # Border-rounding fallback: nearest zone polygon.
            hit = zones.iloc[[zones.geometry.distance(pt).idxmin()]]
            if hit.empty:
                no_match.append(ward_id)
                continue

        zone_id = hit.iloc[0]["zone_id"]
        zone_name = hit.iloc[0]["zone_name"]

        before = (props.get("parent"), props.get("parent_name"))
        props["parent"] = zone_id
        props["parent_name"] = zone_name
        if "parentID" in props:
            props["parentID"] = zone_id
        after = (zone_id, zone_name)
        if before != after:
            path.write_text(json.dumps(data, indent=2))
            updated += 1

    print(f"updated {updated} ward geojsons (parent → zone)")
    if no_match:
        print(f"WARN: {len(no_match)} ward(s) had no zone match: {no_match[:10]}")
    return 0 if not no_match else 1


if __name__ == "__main__":
    raise SystemExit(main())
