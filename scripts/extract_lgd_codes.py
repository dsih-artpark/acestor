"""Extract {region_id, lgd_code, name} from <state>_datasets/lgd_normalized/
into reference/lgd/<state>_<level>.csv — committed snapshots used by the
predictions writers.

The lgd_normalized/ CSVs are the canonical LGD-portal export (region_id,
region_code, region_type, name, name_local, parent_id, …). This script just
copies the three columns the pipeline needs and zero-pads region_ids to match
the format the pipeline emits.

Usage:
    uv run python scripts/extract_lgd_codes.py --state AP
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "reference" / "lgd"

PIPELINE_REGION_ID_WIDTH: dict[str, int] = {
    "district": 0,
    "mandal": 5,
    "block": 0,
    "village": 0,
}

LEVEL_FILE: dict[str, str] = {
    "district": "districts.csv",
    "mandal": "mandals.csv",
    "block": "blocks.csv",
    "village": "villages.csv",
}


def _pipeline_region_id(level: str, region_code: str) -> str:
    width = PIPELINE_REGION_ID_WIDTH[level]
    code = region_code.zfill(width) if width else region_code
    return f"{level}_{code}"


def _extract(source_csv: Path, level: str) -> list[dict]:
    rows = []
    with source_csv.open() as f:
        for r in csv.DictReader(f):
            code = r["region_code"]
            name = r.get("name", "") or ""
            if not code:
                raise ValueError(
                    f"{source_csv}: row has empty region_code: {r!r}. "
                    f"Fix the lgd_normalized source before regenerating."
                )
            rows.append(
                {
                    "region_id": _pipeline_region_id(level, code),
                    "lgd_code": code,
                    "name": name,
                }
            )
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
    lgd_root = data_root / "lgd_normalized"
    if not lgd_root.exists():
        raise SystemExit(f"lgd_normalized root not found: {lgd_root}")

    for level, fname in LEVEL_FILE.items():
        source = lgd_root / fname
        if not source.exists():
            print(f"skip: {source} (no {level} CSV)")
            continue
        rows = _extract(source, level)
        dest = OUT_DIR / f"{state.lower()}_{level}.csv"
        _write_csv(rows, dest)
        print(f"wrote {dest} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
