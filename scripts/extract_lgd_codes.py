"""Extract {region_id, lgd_code, name} from per-state geojsons → committed CSVs.

Reads the per-state geojsons under <state-data-root>/geojsons/geojsons_<STATE>/
and writes one CSV per spatial level into reference/lgd/.

The geojson is the source of truth for region definitions; the CSVs are a
committed, version-controlled snapshot so pipeline code can look up LGD codes
without depending on the gitignored ap_datasets/ tree.

LGD source field by spatial level
---------------------------------
- district: the numeric suffix of region_id (e.g. 'district_515' → '515').
            District-level geojsons don't carry an India LGD property explicitly;
            the IHIP parser maps LGD codes directly into region_id, so the
            suffix IS the LGD district code.
- mandal:   'DMCodeInd' — full state+district+mandal LGD code (e.g. '55105206').

Usage:
    uv run python scripts/extract_lgd_codes.py --state AP
    uv run python scripts/extract_lgd_codes.py --state OD
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "reference" / "lgd"

LGD_PROPERTY_BY_LEVEL: dict[str, str] = {
    "district": "__region_id_suffix__",
    "mandal": "DMCodeInd",
}


def _read_properties(path: Path) -> dict:
    obj = json.loads(path.read_text())
    if obj.get("type") == "FeatureCollection":
        feats = obj.get("features") or []
        if not feats:
            return {}
        return feats[0].get("properties") or {}
    if obj.get("type") == "Feature":
        return obj.get("properties") or {}
    return obj.get("properties") or {}


def _extract(level_dir: Path, level: str) -> list[dict]:
    rows = []
    for path in sorted(level_dir.glob(f"{level}_*.geojson")):
        props = _read_properties(path)
        region_id = props.get("region_id") or path.stem
        name = props.get("name") or props.get("Name") or ""
        prop_name = LGD_PROPERTY_BY_LEVEL[level]
        if prop_name == "__region_id_suffix__":
            lgd_code = region_id.split("_", 1)[1] if "_" in region_id else region_id
        else:
            lgd_code = props.get(prop_name)
        if lgd_code in (None, ""):
            print(f"  warn: {path.name} has no {prop_name} — skipped")
            continue
        rows.append({"region_id": region_id, "lgd_code": str(lgd_code), "name": name})
    return rows


def _write_csv(rows: list[dict], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["region_id", "lgd_code", "name"])
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True, help="State code: AP, OD, …")
    ap.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="Override path to <state>_datasets/ (default: <state-lower>_datasets/)",
    )
    args = ap.parse_args()

    state = args.state.upper()
    data_root = args.data_root or REPO / f"{state.lower()}_datasets"
    geojson_root = data_root / "geojsons" / f"geojsons_{state}"
    if not geojson_root.exists():
        raise SystemExit(f"geojson root not found: {geojson_root}")

    plural_by_level = {"district": "districts", "mandal": "mandals", "ward": "wards"}
    for level, prop in LGD_PROPERTY_BY_LEVEL.items():
        level_dir = geojson_root / plural_by_level[level]
        if not level_dir.exists():
            print(f"skip: {level_dir} (no {level} geojsons)")
            continue
        rows = _extract(level_dir, level)
        dest = OUT_DIR / f"{state.lower()}_{level}.csv"
        _write_csv(rows, dest)
        print(f"wrote {dest} ({len(rows)} rows, lgd source: {prop})")


if __name__ == "__main__":
    main()
